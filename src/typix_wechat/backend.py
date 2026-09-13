"""No-GUI upstream download, exact package verification, staging and lifecycle."""
from __future__ import annotations

import hashlib
import fcntl
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import pinned


class WeChatError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise WeChatError(message)


def check_cancel(cancelled):
    require(not cancelled(), "已取消，尚未开始安装；再次点击可继续下载")


def inspect(command, limit=65536, timeout=20):
    """Read-only child process with bounded output and total wall time."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "软件包检查超时")
            if not select.select([process.stdout], [], [], min(0.2, remaining))[0]:
                continue
            data = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
            if not data:
                break
            output.extend(data)
            require(len(output) <= limit, "软件包检查输出超过限制")
        return process.wait(timeout=max(0.01, deadline - time.monotonic())), output.decode("utf-8")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()


def preflight():
    from typix_store.catalog import DeviceProfile
    profile = DeviceProfile.from_environment()
    require(profile.arch == "arm64" and profile.os in pinned.SUPPORTED_OS,
            "官方客户端需要 Raspberry Pi OS ARM64（Bookworm / Trixie）")
    return profile


def audit_packages():
    code, output = inspect(["/usr/bin/dpkg", "--audit"], timeout=30)
    require(code == 0 and not output.strip(), "系统有未完成的软件包事务；请管理员先检查 dpkg --audit")


def installed_version():
    code, output = inspect(["/usr/bin/dpkg-query", "-W", "-f=${Status}\n${Version}\n${Architecture}", pinned.PACKAGE])
    lines = output.strip().splitlines()
    if code == 0 and len(lines) == 3 and lines[0] == "install ok installed" and lines[2] == pinned.ARCH:
        return lines[1]
    return None


def check_space(cache, staged=False):
    if not staged:
        require(shutil.disk_usage(cache).free >= pinned.SIZE + pinned.RESERVE, "下载空间不足，请释放约 330 MiB 后重试")
    required = pinned.INSTALLED_KIB * 1024 + pinned.SIZE * (1 if staged else 2) + pinned.RESERVE
    require(shutil.disk_usage("/").free >= required, "系统空间不足以暂存和展开官方客户端，请释放约 1.3 GiB 后重试")


def verify_package(path, cancelled=lambda: False):
    require(path.stat().st_size == pinned.SIZE, "官方安装包大小已变化；请等待 Typix 微信更新，不会安装未知版本")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(262144), b""):
            check_cancel(cancelled)
            digest.update(data)
    require(digest.hexdigest() == pinned.SHA256, "官方安装包校验不匹配；已拒绝安装，请等待更新")
    fields = ["Package", "Version", "Architecture", "Depends", "Installed-Size"]
    code, output = inspect(["/usr/bin/dpkg-deb", "-f", str(path), *fields])
    require(code == 0, "无法读取官方安装包元数据")
    actual = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        require(separator and key in fields and key not in actual, "官方包元数据格式异常")
        actual[key] = value.strip()
    require(actual.get("Package") == pinned.PACKAGE and actual.get("Version") == pinned.VERSION
            and actual.get("Architecture") == pinned.ARCH and actual.get("Installed-Size") == str(pinned.INSTALLED_KIB),
            "官方包名称、版本、架构或展开大小与审核记录不一致")
    normalize = lambda value: re.sub(r"\s+", "", value)
    require(normalize(actual.get("Depends", "")) == normalize(pinned.DEPENDS), "官方包依赖与审核记录不一致")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        response.close()
        raise WeChatError("官方下载地址发生跳转，请等待集成包更新")


def download(cache, cancelled=lambda: False, progress=lambda *_: None, opener=None):
    from .transport import open_pinned
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    check_space(cache)
    final, partial = cache / (pinned.SHA256 + ".deb"), cache / (pinned.SHA256 + ".part")
    if os.path.lexists(final):
        details = final.lstat()
        require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid(), "下载缓存不是当前用户所有的普通文件")
        try:
            verify_package(final, cancelled)
            return final
        except WeChatError:
            check_cancel(cancelled)
            final.unlink()
    descriptor = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WeChatError("已有微信下载正在进行，请稍后重试") from exc
        details = os.fstat(descriptor)
        require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid(), "下载缓存必须是当前用户所有的普通文件")
        offset = details.st_size
        require(0 <= offset <= pinned.SIZE, "缓存大小异常，请清理微信下载缓存后重试")
        if offset < pinned.SIZE:
            check_cancel(cancelled)
            deadline = time.monotonic() + 1200
            response = opener(offset) if opener else open_pinned(offset, cancelled)
            with response, os.fdopen(descriptor, "r+b", closefd=False) as output:
                require(response.geturl() == pinned.URL, "下载来源不是固定的腾讯官方地址")
                if response.status == 206 and offset:
                    require(response.headers.get("Content-Range") == f"bytes {offset}-{pinned.SIZE - 1}/{pinned.SIZE}", "官方下载续传范围不匹配")
                else:
                    require(response.status == 200, "官方下载未返回完整软件包")
                    offset = 0
                    output.truncate(0)
                length = response.headers.get("Content-Length")
                require(length is None or int(length) == pinned.SIZE - offset, "官方包大小已更新，请等待集成更新")
                output.seek(offset)
                while offset < pinned.SIZE:
                    check_cancel(cancelled)
                    require(time.monotonic() < deadline, "下载超时，已保留缓存，可以重试继续")
                    require(shutil.disk_usage(cache).free >= pinned.RESERVE, "下载已暂停以保留磁盘空间，释放空间后重试")
                    data = response.read1(min(131072, pinned.SIZE - offset))
                    check_cancel(cancelled)
                    require(time.monotonic() <= deadline, "下载超时，已保留缓存，可以重试继续")
                    require(bool(data), "下载中断，已保留缓存，可以重试继续")
                    output.write(data)
                    offset += len(data)
                    progress(offset, pinned.SIZE)
                check_cancel(cancelled)
                require(time.monotonic() < deadline, "下载超过总期限")
                require(not response.read1(1), "官方安装包超过审核大小")
                check_cancel(cancelled)
                require(time.monotonic() <= deadline, "下载超过总期限")
                output.flush()
                os.fsync(output.fileno())
        verify_package(partial, cancelled)
        os.replace(partial, final)
        return final
    except WeChatError as exc:
        if "校验不匹配" in str(exc) or "超过审核大小" in str(exc):
            partial.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def stage_package(path, cancelled=lambda: False):
    from typix_store.client import require_system_owned
    check_cancel(cancelled)
    helper = Path("/usr/libexec/typix-wechat-stage")
    require_system_owned(helper)
    process = subprocess.Popen(["/usr/bin/pkexec", str(helper), str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 180
    while True:
        try:
            output, error = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancelled() or time.monotonic() >= deadline:
                threading.Thread(target=process.communicate, daemon=True, name="wechat-stage-reaper").start()
                raise WeChatError("已取消等待暂存授权，尚未开始安装；请关闭残留授权提示")
    require(process.returncode == 0, "系统暂存授权被拒绝或校验失败：" + error.strip()[:400])
    expected = Path("/var/cache/typix-wechat/verified") / (pinned.SHA256 + ".deb")
    require(output.strip() == str(expected), "系统暂存路径异常")
    check_cancel(cancelled)
    require_system_owned(expected)
    verify_package(expected, cancelled)
    check_space(expected.parent, staged=True)
    return expected


def prepare_install(cache, cancelled=lambda: False, progress=lambda *_: None):
    preflight()
    audit_packages()
    current = installed_version()
    if current:
        code, _ = inspect(["/usr/bin/dpkg", "--compare-versions", current, "gt", pinned.VERSION])
        require(code != 0, "已安装更新的官方客户端，不会降级")
    path = download(cache, cancelled, progress)
    progress(-1, 0)
    result = stage_package(path, cancelled)
    audit_packages()
    check_cancel(cancelled)
    return result


def launch_native():
    """Return after the native window closes, including a tray-resident client."""
    from typix_launcher.fullscreen import FullscreenSession
    preflight()
    require(installed_version() is not None and Path("/usr/bin/wechat").exists(), "官方客户端尚未正确安装")
    entry = Path("/usr/share/typix-wechat/native.desktop")

    class NativeSession(FullscreenSession):
        def _consider(self, window):
            # Opening WeChat explicitly selects its existing main window too.
            # Keep this exception local: generic Launcher baseline protection
            # still applies to all other apps and to transient dialogs.
            if window.ready and not window.parent and window.app_id.casefold() in pinned.NATIVE_IDS:
                self._baseline.discard(window.identifier)
            super()._consider(window)

    with NativeSession(entry) as session:
        child = subprocess.Popen(["/usr/bin/wechat"], cwd=Path.home(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        class WindowLifetime:
            seen = False
            missing_window = False

            def poll(self):
                if session.client is not None:
                    windows = [window for window in session.client.windows.values()
                               if window.ready and not window.parent and window.app_id.casefold() in pinned.NATIVE_IDS]
                    for window in windows:
                        # Existing handles may emit no new done event. Seed
                        # them after wait() has established its startup deadline.
                        session._consider(window)
                    if windows:
                        self.seen = True
                        return None
                    if self.seen:
                        # Closing a visible window succeeds even if a forwarding
                        # helper exited nonzero or the tray process stays alive.
                        return 0
                    if time.monotonic() < session._deadline:
                        # A single-instance helper can exit before its forwarded
                        # activation maps a window. Allow bounded startup time.
                        return None
                    code = child.poll()
                    if code in (None, 0):
                        self.missing_window = True
                        return 1
                    return code
                return child.poll()

            def wait(self):
                # The supervisor may cross its deadline after our last poll.
                # Its fallback must not synchronously wait for a windowless
                # live process when compositor observation was established.
                if session.client is not None and not self.seen:
                    self.missing_window = True
                    return 1
                return child.wait()

        lifetime = WindowLifetime()
        code = session.wait(lifetime)
        if child.poll() is None:
            # Reap only; never terminate the client's optional tray process.
            threading.Thread(target=child.wait, daemon=True, name="wechat-native-reaper").start()
        require(not lifetime.missing_window, "未检测到微信窗口，请稍后重试；也可检查客户端是否停留在托盘。")
        require(code == 0, f"官方客户端启动失败（退出码 {code}），请检查系统运行依赖")
