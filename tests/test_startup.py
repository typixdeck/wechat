"""No setup GUI on installed launches; missing client and errors stay recoverable."""
import unittest
from unittest.mock import patch

try:
    from typix_wechat import app
except ImportError:
    app = None


@unittest.skipIf(app is None, "GTK3 runtime required")
class StartupTests(unittest.TestCase):
    def setUp(self):
        for target, options in [
            ("os.geteuid", {"return_value": 1000}),
            ("GLib.set_prgname", {}),
            ("backend.installed_version", {"return_value": "4.1.13.9"}),
            ("backend.launch_native", {}),
            ("WeChatApplication", {}),
        ]:
            component, name = target.rsplit(".", 1) if "." in target else ("", target)
            owner = app
            for part in component.split(".") if component else []:
                owner = getattr(owner, part)
            guard = patch.object(owner, name, **options)
            setattr(self, name, guard.start())
            self.addCleanup(guard.stop)
        self.WeChatApplication.return_value.run.return_value = 0

    def test_installed_client_never_creates_setup_window(self):
        self.assertEqual(app.main(), 0)
        self.launch_native.assert_called_once()
        self.WeChatApplication.assert_not_called()

    def test_missing_client_opens_setup_without_starting_native(self):
        self.installed_version.return_value = None
        self.assertEqual(app.main(), 0)
        self.launch_native.assert_not_called()
        self.WeChatApplication.assert_called_once_with(startup_error=None)

    def test_real_native_error_opens_recovery_without_auto_retry(self):
        self.launch_native.side_effect = RuntimeError("test startup failure")
        self.assertEqual(app.main(), 0)
        self.WeChatApplication.assert_called_once_with(startup_error="test startup failure")

    def test_package_check_failure_is_recoverable(self):
        self.installed_version.side_effect = RuntimeError("test package check failure")
        self.assertEqual(app.main(), 0)
        self.launch_native.assert_not_called()
        self.WeChatApplication.assert_called_once_with(startup_error="test package check failure")

    def test_root_never_launches_native_or_gui(self):
        self.geteuid.return_value = 0
        self.assertEqual(app.main(), 1)
        self.launch_native.assert_not_called()
        self.WeChatApplication.assert_not_called()


if __name__ == "__main__":
    unittest.main()
