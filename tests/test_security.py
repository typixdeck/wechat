import hashlib
import io
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from typix_wechat import backend, pinned, staging, transport


class Response(io.BytesIO):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}

    def geturl(self):
        return pinned.URL


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.payload = b"a bounded immutable upstream package fixture"
        self.pins = patch.multiple(pinned, SIZE=len(self.payload), SHA256=hashlib.sha256(self.payload).hexdigest())
        self.pins.start()
        self.addCleanup(self.pins.stop)
        self.fields = {"Package": pinned.PACKAGE, "Version": pinned.VERSION, "Architecture": pinned.ARCH,
                       "Depends": pinned.DEPENDS, "Installed-Size": str(pinned.INSTALLED_KIB)}

    def control(self, *_args, **_kwargs):
        return 0, "\n".join(f"{key}: {value}" for key, value in self.fields.items())

    def test_hash_size_and_every_control_field_are_required(self):
        path = self.directory / "package.deb"
        path.write_bytes(self.payload)
        with patch.object(backend, "inspect", self.control):
            backend.verify_package(path)
            for key in self.fields:
                with self.subTest(field=key):
                    old = self.fields[key]
                    self.fields[key] = "wrong"
                    with self.assertRaises(backend.WeChatError):
                        backend.verify_package(path)
                    self.fields[key] = old
            path.write_bytes(b"x" * len(self.payload))
            with self.assertRaisesRegex(backend.WeChatError, "校验不匹配"):
                backend.verify_package(path)
            path.write_bytes(b"short")
            with self.assertRaisesRegex(backend.WeChatError, "大小"):
                backend.verify_package(path)

    def test_download_resume_checks_range_and_full_final_hash(self):
        partial = self.directory / (pinned.SHA256 + ".part")
        partial.write_bytes(self.payload[:8])
        offsets = []

        def opener(offset):
            offsets.append(offset)
            return Response(self.payload[offset:], 206, {"Content-Length": str(pinned.SIZE - offset),
                            "Content-Range": f"bytes {offset}-{pinned.SIZE - 1}/{pinned.SIZE}"})

        with patch.object(backend, "check_space"), patch.object(backend, "inspect", self.control):
            result = backend.download(self.directory, opener=opener)
        self.assertEqual(offsets, [8])
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertFalse(partial.exists())

    def test_server_ignoring_range_restarts_without_appending_corruption(self):
        (self.directory / (pinned.SHA256 + ".part")).write_bytes(b"oldprefix")
        with patch.object(backend, "check_space"), patch.object(backend, "inspect", self.control):
            result = backend.download(self.directory, opener=lambda _offset: Response(self.payload))
        self.assertEqual(result.read_bytes(), self.payload)

    def test_mismatched_resume_range_is_rejected(self):
        (self.directory / (pinned.SHA256 + ".part")).write_bytes(self.payload[:8])
        response = Response(self.payload[8:], 206, {"Content-Range": "bytes 0-3/4"})
        with patch.object(backend, "check_space"):
            with self.assertRaisesRegex(backend.WeChatError, "续传"):
                backend.download(self.directory, opener=lambda _offset: response)

    def test_rolling_vendor_payload_is_rejected_and_bad_cache_removed(self):
        with patch.object(backend, "check_space"):
            with self.assertRaisesRegex(backend.WeChatError, "校验不匹配"):
                backend.download(self.directory, opener=lambda _offset: Response(b"x" * pinned.SIZE))
        self.assertFalse((self.directory / (pinned.SHA256 + ".part")).exists())
        self.assertFalse((self.directory / (pinned.SHA256 + ".deb")).exists())

    def test_cancelled_download_preserves_valid_partial_for_retry(self):
        partial = self.directory / (pinned.SHA256 + ".part")
        partial.write_bytes(self.payload[:8])
        with patch.object(backend, "check_space"):
            with self.assertRaisesRegex(backend.WeChatError, "取消"):
                backend.download(self.directory, cancelled=lambda: True, opener=Mock(side_effect=AssertionError("network after cancel")))
        self.assertEqual(partial.read_bytes(), self.payload[:8])

    def test_download_rejects_cache_symlink(self):
        target = self.directory / "user-document"
        target.write_bytes(b"preserve")
        (self.directory / (pinned.SHA256 + ".part")).symlink_to(target)
        with patch.object(backend, "check_space"):
            with self.assertRaises(OSError):
                backend.download(self.directory)
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_second_download_cannot_write_locked_partial(self):
        import fcntl
        partial = self.directory / (pinned.SHA256 + ".part")
        with partial.open("wb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(backend, "check_space"):
                with self.assertRaisesRegex(backend.WeChatError, "正在进行"):
                    backend.download(self.directory)

    def test_root_copy_is_independent_nofollow_and_owner_checked(self):
        source = self.directory / "input.deb"
        source.write_bytes(self.payload)
        copied = self.directory / "copied.deb"
        staging.copy_input(source, copied, os.getuid())
        source.write_bytes(b"x" * pinned.SIZE)
        self.assertEqual(copied.read_bytes(), self.payload)
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o444)
        linked = self.directory / "link.deb"
        linked.symlink_to(source)
        with self.assertRaises(OSError):
            staging.copy_input(linked, self.directory / "link-copy.deb", os.getuid())
        with self.assertRaises(backend.WeChatError):
            staging.copy_input(source, self.directory / "wrong-owner.deb", os.getuid() + 1)

    def test_root_copy_rejects_truncated_input(self):
        source = self.directory / "input.deb"
        source.write_bytes(b"short")
        with self.assertRaisesRegex(backend.WeChatError, "大小"):
            staging.copy_input(source, self.directory / "copy.deb", os.getuid())

    def test_nonempty_dpkg_audit_is_failure_even_exit_zero(self):
        with patch.object(backend, "inspect", return_value=(0, "unconfigured package")):
            with self.assertRaises(backend.WeChatError):
                backend.audit_packages()

    def test_low_space_prevents_download(self):
        with patch.object(backend.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(backend.WeChatError, "空间"):
                backend.check_space(self.directory)

    def test_stage_verifies_the_copy_and_rejects_tampering_before_publish(self):
        source = self.directory / "input.deb"
        source.write_bytes(b"x" * pinned.SIZE)
        target = self.directory / "verified"
        original_fstat = os.fstat

        def fstat(descriptor):
            details = original_fstat(descriptor)
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0) if details.st_size == 0 else details

        with patch.object(staging, "DESTINATION", target), patch.object(staging, "preflight"), \
                patch.object(staging, "audit_packages"), patch.object(staging, "check_space"), \
                patch.object(staging, "make_directory", lambda directory: directory.mkdir()), \
                patch.object(staging, "system_owned"), patch.object(staging.os, "fstat", fstat), \
                patch.object(staging, "verify_package", wraps=backend.verify_package) as verifier:
            with self.assertRaisesRegex(backend.WeChatError, "校验不匹配"):
                staging.stage(source, os.getuid())
        verified_input = verifier.call_args.args[0]
        self.assertNotEqual(verified_input, source)
        self.assertTrue(str(verified_input).startswith(str(target / ".stage-")))
        self.assertFalse((target / (pinned.SHA256 + ".deb")).exists())
        self.assertEqual([item.name for item in target.iterdir()], [".lock"])



class HeaderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            for _ in range(100):
                self.connection.sendall(b"a")
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class HeaderBoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), HeaderHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def test_real_slow_headers_cancel_before_response_return(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(pinned, "URL", self.url("/slow")):
                with self.assertRaisesRegex(backend.WeChatError, "取消"):
                    transport.open_pinned(0, cancelled.is_set)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            timer.cancel()

    def test_real_slow_headers_respect_total_deadline(self):
        started = time.monotonic()
        with patch.object(pinned, "URL", self.url("/slow")):
            with self.assertRaisesRegex(backend.WeChatError, "超时"):
                transport.open_pinned(0, timeout=0.2)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_upstream_redirect_is_refused_before_following(self):
        with patch.object(pinned, "URL", self.url("/redirect")):
            with self.assertRaisesRegex(backend.WeChatError, "跳转"):
                transport.open_pinned(0)

    def test_redirect_response_is_closed_before_refusal(self):
        response = Mock()
        with self.assertRaises(backend.WeChatError):
            backend.NoRedirect().redirect_request(None, response, 302, "Found", {}, "https://elsewhere.invalid/")
        response.close.assert_called_once()

    def test_abandoned_dns_workers_have_finite_concurrency(self):
        semaphore = threading.BoundedSemaphore(2)
        release = threading.Event()
        opener = Mock()

        def blocked(*_args, **_kwargs):
            release.wait(2)
            raise OSError("closed")

        opener.open.side_effect = blocked
        try:
            with patch.object(transport, "OPEN_SLOTS", semaphore), \
                    patch.object(transport.urllib.request, "build_opener", return_value=opener):
                for _ in range(2):
                    with self.assertRaisesRegex(backend.WeChatError, "超时"):
                        transport.open_pinned(0, timeout=0.1)
                with self.assertRaisesRegex(backend.WeChatError, "上次连接"):
                    transport.open_pinned(0, timeout=0.1)
                self.assertEqual(opener.open.call_count, 2)
                release.set()
                deadline = time.monotonic() + 1
                while semaphore._value != 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(semaphore._value, 2)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
