"""Reviewed upstream identity. A vendor replacement requires a reviewed update."""
URL = "https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_arm64.deb"
VERSION = "4.1.13.9"
SIZE = 209059272
SHA256 = "a6d115d24dfe3ed1b7e7de16cf6cc02acef8df5668150f702ac8d8c5256405fa"
PACKAGE = "wechat"
ARCH = "arm64"
DEPENDS = "fonts-noto-cjk | google-noto-cjk-fonts"
INSTALLED_KIB = 726458
RESERVE = 128 * 1024 * 1024
SUPPORTED_OS = frozenset({"raspios-bookworm", "raspios-trixie"})
NATIVE_IDS = frozenset({"wechat"})
