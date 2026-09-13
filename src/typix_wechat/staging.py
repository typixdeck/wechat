"""Authorized fixed-pin copy/verification only; never starts an installer/GUI."""
from __future__ import annotations

import fcntl
import os
import shutil
import signal
import stat
import sys
import syslog
import tempfile
from pathlib import Path

from . import pinned
from .backend import WeChatError, audit_packages, check_space, preflight, require, verify_package

DESTINATION = Path("/var/cache/typix-wechat/verified")


def system_owned(path, directory=False):
    require(path.is_absolute(), "系统路径必须是绝对路径")
    for item in reversed((path, *path.parents)):
        details = item.lstat()
        require(details.st_uid == 0 and not details.st_mode & 0o022 and not stat.S_ISLNK(details.st_mode), "系统暂存路径必须由 root 管理且普通用户不可改写")
        require(stat.S_ISDIR(details.st_mode) if item != path or directory else stat.S_ISREG(details.st_mode), "系统暂存路径类型异常")


def make_directory(path):
    for item in reversed((path, *path.parents)):
        if not os.path.lexists(item):
            system_owned(item.parent, directory=True)
            item.mkdir(mode=0o755)
        system_owned(item, directory=True)


def copy_input(source, destination, uid):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        require(stat.S_ISREG(details.st_mode) and details.st_uid == uid and details.st_size == pinned.SIZE,
                "输入必须是当前用户所有、大小匹配的普通官方安装包")
        with os.fdopen(descriptor, "rb", closefd=False) as stream, destination.open("xb") as output:
            total = 0
            while True:
                data = stream.read(min(262144, pinned.SIZE + 1 - total))
                if not data:
                    break
                total += len(data)
                require(total <= pinned.SIZE, "复制过程中安装包大小改变")
                output.write(data)
            require(total == pinned.SIZE, "复制得到的安装包不完整")
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o444)
    finally:
        os.close(descriptor)


def stage(source, uid):
    preflight()
    audit_packages()
    make_directory(DESTINATION)
    target = DESTINATION / (pinned.SHA256 + ".deb")
    lock = os.open(DESTINATION / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        details = os.fstat(lock)
        require(stat.S_ISREG(details.st_mode) and details.st_uid == 0 and not details.st_mode & 0o022, "系统暂存锁异常")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WeChatError("已有微信安装包正在校验，请稍后重试") from exc
        if os.path.lexists(target):
            system_owned(target)
            verify_package(target)
            check_space(DESTINATION, staged=True)
            return target
        usage = sum(item.lstat().st_size for item in DESTINATION.iterdir() if stat.S_ISREG(item.lstat().st_mode))
        require(usage <= 2 * pinned.SIZE, "系统微信暂存缓存已满，请管理员清理旧安装包后重试")
        require(shutil.disk_usage(DESTINATION).free >= pinned.SIZE + pinned.RESERVE, "系统暂存空间不足")
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=DESTINATION) as temporary:
            copied = Path(temporary) / "wechat.deb"
            copy_input(source, copied, uid)
            # The immutable root copy, rather than the submitted path, is the
            # sole input to verification and subsequent PackageKit installation.
            verify_package(copied)
            system_owned(copied)
            check_space(DESTINATION, staged=True)
            os.replace(copied, target)
        syslog.openlog("typix-wechat-stage")
        syslog.syslog(syslog.LOG_NOTICE, f"verified uid={uid} package={pinned.PACKAGE} version={pinned.VERSION} sha256={pinned.SHA256}")
        return target
    finally:
        os.close(lock)


def main():
    try:
        require(os.geteuid() == 0 and len(sys.argv) == 2, "暂存须通过普通用户发起的 pkexec 授权")
        uid = int(os.environ.get("PKEXEC_UID", "0"))
        require(uid > 0, "无法识别发起授权的普通用户")

        def expired(*_args):
            raise WeChatError("系统暂存校验超时，未开始安装")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 120)
        try:
            print(stage(Path(sys.argv[1]), uid), flush=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        return 0
    except (OSError, ValueError) as exc:
        syslog.openlog("typix-wechat-stage")
        syslog.syslog(syslog.LOG_WARNING, "upstream staging rejected")
        print("微信暂存失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
