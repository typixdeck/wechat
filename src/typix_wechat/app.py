"""Fullscreen GTK onboarding and direct native-client launch, never a browser."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from typix_store.transactions import PackageTransaction
from . import backend, pinned

APP_ID = "ai.typixdeck.wechat"
CSS = b"""
* { font-family: 'Noto Sans CJK SC', 'Noto Sans', sans-serif; }
window { background: #071018; color: #edf8ff; }
.title { font-size: 28px; font-weight: 800; }
.subtitle { color: #8ba6b8; font-size: 14px; }
.hero { font-size: 46px; font-weight: 800; color: #5de0a3; }
.info { background: #102331; border: 1px solid #254253; border-radius: 14px; padding: 18px; }
button { min-height: 40px; padding: 5px 16px; border: 1px solid #294857; border-radius: 10px;
 background: #102b38; color: #eef8ff; box-shadow: none; }
button:focus, button:hover { border-color: #5de0a3; background: #17463e; }
button:disabled { color: #657b87; background: #11212b; }
button.suggested-action { background: #1a6047; border-color: #5de0a3; }
progressbar trough { background: #15303b; border: 0; }
progressbar progress { background: #5de0a3; border: 0; }
"""


def label(text, style=None):
    result = Gtk.Label(label=text, xalign=0)
    result.set_line_wrap(True)
    result.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    result.set_max_width_chars(55)
    if style:
        result.get_style_context().add_class(style)
    return result


class WeChatApplication(Gtk.Application):
    def __init__(self, startup_error=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None
        self.busy = False
        self.installed = None
        self.cancel_event = threading.Event()
        self.transaction = None
        self.close_when_done = False
        self.startup_error = startup_error
        self.cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "typix-wechat/download"

    def do_activate(self):
        if self.window:
            if not self.busy:
                self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("微信 · TypixDeck")
        self.window.set_default_size(800, 600)
        self.window.connect("delete-event", self.on_close)
        self.window.connect("key-press-event", self.on_key)
        self.window.connect("map-event", lambda *_: GLib.idle_add(self.fullscreen))
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        root.set_border_width(24)
        self.window.add(root)
        header = Gtk.Box(spacing=12)
        header.pack_start(label("微信", "title"), True, True, 0)
        self.exit = Gtk.Button.new_with_label("返回  Esc")
        self.exit.connect("clicked", self.on_close)
        header.pack_end(self.exit, False, False, 0)
        root.pack_start(header, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.pack_start(scroll, True, True, 0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        scroll.add(content)
        self.hero = label("安装微信", "hero")
        content.pack_start(self.hero, False, False, 0)
        self.subtitle = label("安装完成后，桌面入口会直接打开微信。", "subtitle")
        content.pack_start(self.subtitle, False, False, 0)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.install_info = info
        info.get_style_context().add_class("info")
        info.pack_start(label("首次使用需要下载腾讯官方 ARM64 安装包"), False, False, 0)
        info.pack_start(label(f"审核版本 {pinned.VERSION} · 约 200 MiB 下载 · 约 1.3 GiB 可用空间", "subtitle"), False, False, 0)
        info.pack_start(label("本应用提供安装与启动集成，不捆绑官方客户端。点击安装后直接从腾讯下载，校验通过后由系统请求授权。微信账号、登录与使用条款由官方客户端处理。", "subtitle"), False, False, 0)
        content.pack_start(info, False, False, 0)
        self.status = label("正在检查系统与已安装版本…", "subtitle")
        root.pack_start(self.status, False, False, 0)
        self.progress = Gtk.ProgressBar()
        root.pack_start(self.progress, False, False, 0)
        actions = Gtk.Box(spacing=12)
        self.primary = Gtk.Button.new_with_label("下载并安装官方客户端")
        self.primary.get_style_context().add_class("suggested-action")
        self.primary.connect("clicked", self.on_primary)
        self.primary.set_sensitive(False)
        actions.pack_start(self.primary, True, True, 0)
        self.cancel = Gtk.Button.new_with_label("取消")
        self.cancel.connect("clicked", self.on_cancel)
        self.cancel.set_sensitive(False)
        actions.pack_start(self.cancel, False, False, 0)
        self.refresh = Gtk.Button.new_with_label("刷新  F5")
        self.refresh.connect("clicked", lambda *_: self.refresh_state())
        actions.pack_start(self.refresh, False, False, 0)
        root.pack_end(actions, False, False, 0)
        self.window.show_all()
        self.fullscreen()
        self.refresh_state(auto_launch=not self.startup_error)

    def fullscreen(self):
        self.window.fullscreen()
        return GLib.SOURCE_REMOVE

    def worker(self, function, finished):
        def run():
            try:
                value, error = function(), None
            except Exception as exc:
                value, error = None, str(exc)
            GLib.idle_add(finished, value, error)
        threading.Thread(target=run, daemon=True, name="typix-wechat-worker").start()

    def refresh_state(self, auto_launch=False):
        if self.busy:
            return
        self.primary.set_sensitive(False)
        self.refresh.set_sensitive(False)

        def read():
            backend.preflight()
            return backend.installed_version()

        def done(version, error):
            self.refresh.set_sensitive(True)
            self.installed = version
            self.update_content()
            self.primary.set_label("打开微信" if version else "下载并安装官方客户端")
            self.primary.set_sensitive(not error)
            self.status.set_text(error or self.startup_error or (f"已安装官方客户端 {version}" if version else "尚未安装。点击下方按钮开始；下载与安装均可查看进度。"))
            self.startup_error = None
            self.primary.grab_focus()
            if version and auto_launch and not error:
                self.start_native()
            return GLib.SOURCE_REMOVE
        self.worker(read, done)

    def update_content(self):
        self.hero.set_text("打开微信" if self.installed else "安装微信")
        self.subtitle.set_text("微信已安装。可以重新打开，或返回桌面。" if self.installed
                               else "安装完成后，桌面入口会直接打开微信。")
        self.install_info.set_visible(not self.installed)

    def set_busy(self, value):
        self.busy = value
        self.primary.set_sensitive(not value)
        self.refresh.set_sensitive(not value)
        self.exit.set_sensitive(not value)
        self.cancel.set_sensitive(value)

    def on_primary(self, *_args):
        if self.busy:
            return
        if self.installed:
            self.start_native()
            return
        self.cancel_event.clear()
        self.set_busy(True)
        self.status.set_text("正在从腾讯官方下载；可取消并稍后继续。")

        def progress(done, total):
            GLib.idle_add(self.download_progress, done, total)

        def ready(path, error):
            if error or self.cancel_event.is_set():
                self.operation_done(False, error or "已取消", [])
                return GLib.SOURCE_REMOVE
            self.cancel.set_sensitive(False)
            self.status.set_text("安装包已验证，等待系统安装授权…")
            self.transaction = PackageTransaction(self.transaction_progress, self.operation_done)
            self.transaction.start("InstallFiles", "(tas)", (0, [str(path)]))
            return GLib.SOURCE_REMOVE
        self.worker(lambda: backend.prepare_install(self.cache, self.cancel_event.is_set, progress), ready)

    def download_progress(self, done, total):
        if done < 0:
            self.status.set_text("下载已验证，等待系统暂存授权；尚未开始安装。")
            self.progress.pulse()
        elif total:
            self.progress.set_fraction(done / total)
            self.status.set_text(f"从腾讯官方下载：{done / 1048576:.1f} / {total / 1048576:.1f} MiB")
        return GLib.SOURCE_REMOVE

    def transaction_progress(self, message, percent, can_cancel):
        self.status.set_text(message)
        self.cancel.set_sensitive(can_cancel)
        if percent is not None:
            self.progress.set_fraction(percent / 100)
        else:
            self.progress.pulse()

    def operation_done(self, success, error, _packages):
        self.transaction = None
        self.set_busy(False)
        self.cancel.set_sensitive(False)
        self.status.set_text("官方客户端安装完成，点击“打开微信”登录。" if success else error or "操作未完成，请检查后重试")
        if success:
            self.installed = pinned.VERSION
            self.update_content()
            self.primary.set_label("打开微信")
            self.progress.set_fraction(1)
        if self.close_when_done:
            self.quit()

    def start_native(self):
        self.set_busy(True)
        self.cancel.set_sensitive(False)
        self.window.hide()

        def done(_value, error):
            if error:
                self.set_busy(False)
                self.window.show_all()
                self.update_content()
                self.fullscreen()
                self.cancel.set_sensitive(False)
                self.status.set_text(error)
            else:
                self.quit()
            return GLib.SOURCE_REMOVE
        self.worker(backend.launch_native, done)

    def on_cancel(self, *_args):
        if self.transaction:
            self.transaction.cancel()
        elif self.busy:
            self.cancel_event.set()
            self.cancel.set_sensitive(False)
            self.status.set_text("正在安全取消下载或暂存等待…")

    def on_close(self, *_args):
        if self.busy:
            self.status.set_text("请先取消并等待操作结束；系统提交期间不能强制退出。")
            return True
        self.quit()
        return True

    def on_key(self, _window, event):
        if event.keyval == Gdk.KEY_Escape:
            return self.on_close()
        if event.keyval == Gdk.KEY_F5:
            self.refresh_state()
            return True
        return False


def main():
    if os.geteuid() == 0:
        print("请以普通桌面用户运行微信集成，不要使用 sudo。", file=sys.stderr)
        return 1
    GLib.set_prgname(APP_ID)
    startup_error = None
    try:
        if backend.installed_version():
            # No setup window is created, mapped or registered on this path.
            backend.launch_native()
            return 0
    except Exception as exc:
        startup_error = str(exc)
    return WeChatApplication(startup_error=startup_error).run(sys.argv)
