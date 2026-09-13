"""Exercise the real Launcher supervisor with deterministic compositor events."""
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from typix_wechat import backend, pinned

try:
    from typix_launcher import fullscreen
except ImportError:
    fullscreen = None


@unittest.skipIf(fullscreen is None, "Typix Launcher runtime required")
class NativeLifecycleTests(unittest.TestCase):
    def run_client(self, initial=(), events=(), exit_code=7, available=True, clock_values=None):
        now = [0.0]
        self.requests = []
        self.considered = []
        self.child = Mock()
        self.child.poll.return_value = exit_code
        self.child.wait.return_value = exit_code or 0
        timeline = iter(events)
        pending = [next(timeline, None)]
        client = Mock()
        client.windows = {w.identifier: w for w in initial}

        def poll(_timeout):
            now[0] += 0.1
            if now[0] > 15:
                raise AssertionError("Supervisor failed to finish")
            if pending[0] and now[0] >= pending[0][0]:
                windows = {w.identifier: w for w in pending[0][1]}
                old = client.windows
                client.windows = windows
                for identifier in old.keys() - windows.keys():
                    client.on_closed(old[identifier])
                for window in windows.values():
                    client.on_done(window)
                pending[0] = next(timeline, None)

        def request(window):
            self.requests.append(window.identifier)
            window.states.add(fullscreen.FULLSCREEN)

        client.poll.side_effect = poll
        client.set_fullscreen.side_effect = request
        if not available:
            client.connect.side_effect = OSError("test compositor unavailable")
        with patch.object(backend, "preflight"), \
                patch.object(backend, "installed_version", return_value=pinned.VERSION), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(backend.subprocess, "Popen", return_value=self.child), \
                patch.object(backend.time, "monotonic", side_effect=clock_values or (lambda: now[0])), \
                patch.object(fullscreen, "ForeignToplevelClient", return_value=client), \
                patch.object(fullscreen, "application_ids", return_value=pinned.NATIVE_IDS), \
                patch.dict(fullscreen.os.environ, WAYLAND_DISPLAY="test-wayland"):
            backend.launch_native()

    def window(self, identifier=1, app_id="wechat", parent=0):
        return fullscreen.Toplevel(identifier, app_id=app_id, parent=parent, ready=True)

    def test_existing_inactive_window_survives_nonzero_forwarder(self):
        self.run_client(initial=[self.window()], events=[(1, [])], exit_code=7)
        self.assertEqual(self.requests, [1])
        self.child.kill.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_forwarder_can_exit_before_native_window_maps(self):
        self.run_client(events=[(.3, [self.window()]), (1, [])], exit_code=7)
        self.assertEqual(self.requests, [1])

    def test_window_close_releases_launcher_with_tray_process_alive(self):
        self.run_client(events=[(.1, [self.window()]), (1, [])], exit_code=None)
        self.child.kill.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_window_replacement_during_startup(self):
        self.run_client(events=[(.1, [self.window()]), (.3, []),
                                (.5, [self.window(2)]), (1.5, [])], exit_code=0)
        self.assertEqual(self.requests, [1, 2])

    def test_failed_process_without_native_window_reports_error(self):
        with self.assertRaisesRegex(backend.WeChatError, "退出码 7"):
            self.run_client()

    def test_successful_forwarder_without_window_is_recoverable(self):
        with self.assertRaisesRegex(backend.WeChatError, "未检测到微信窗口"):
            self.run_client(exit_code=0)

    def test_live_process_without_window_does_not_block_launcher_forever(self):
        with self.assertRaisesRegex(backend.WeChatError, "未检测到微信窗口"):
            self.run_client(exit_code=None)
        self.child.kill.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_deadline_crossing_between_poll_and_supervisor_never_waits_synchronously(self):
        with patch.object(backend.threading, "Thread") as reaper:
            with self.assertRaisesRegex(backend.WeChatError, "未检测到微信窗口"):
                self.run_client(exit_code=None, clock_values=[0, 9.999999, 10.000001])
        self.child.wait.assert_not_called()
        reaper.assert_called_once()
        self.child.kill.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_other_apps_and_transient_dialogs_do_not_mask_failure(self):
        with self.assertRaises(backend.WeChatError):
            self.run_client(initial=[self.window(1, "other"), self.window(2, parent=1)])
        self.assertEqual(self.requests, [])

    def test_compositor_unavailable_preserves_process_failure(self):
        with self.assertRaisesRegex(backend.WeChatError, "退出码 7"):
            self.run_client(available=False)


if __name__ == "__main__":
    unittest.main()
