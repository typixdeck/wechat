#!/bin/sh
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=${VERSION:-0.1.1-1}
STAGE="$APP_ROOT/build/package"
DIST="$APP_ROOT/dist"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/libexec" "$STAGE/usr/lib/python3/dist-packages" "$STAGE/usr/share/typix-wechat" "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/typix-wechat" "$DIST"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: typix-wechat
Version: $VERSION
Architecture: all
Maintainer: TypixDeck <dev@typixnode.com>
Section: net
Priority: optional
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, typix-store (>= 0.3.0-1), typix-launcher (>= 0.2.0-1), dpkg, packagekit, pkexec, ca-certificates, desktop-file-utils, shared-mime-info
X-Typix-Compatible-OS: raspios-bookworm, raspios-trixie
Description: Native WeChat installation and launcher integration for TypixDeck
 Complete GTK3 integration application with explicit official ARM64 client
 download, pinned verification, authorized staging and PackageKit installation.
 The proprietary upstream WeChat client is downloaded directly from Tencent
 on first explicit installation and is not redistributed in this package.
CONTROL
cat > "$STAGE/usr/bin/typix-wechat" <<'RUNNER'
#!/bin/sh
set -eu
exec /usr/bin/python3 -m typix_wechat "$@"
RUNNER
chmod 755 "$STAGE/usr/bin/typix-wechat"
cp -R "$APP_ROOT/src/typix_wechat" "$STAGE/usr/lib/python3/dist-packages/"
find "$STAGE/usr/lib/python3/dist-packages" -name '__pycache__' -type d -prune -exec rm -rf {} +
install -m 755 "$APP_ROOT/packaging/typix-wechat-stage" "$STAGE/usr/libexec/typix-wechat-stage"
install -m 644 "$APP_ROOT/packaging/typix-wechat.desktop" "$STAGE/usr/share/applications/typix-wechat.desktop"
install -m 644 "$APP_ROOT/packaging/native.desktop" "$STAGE/usr/share/typix-wechat/native.desktop"
install -m 644 "$APP_ROOT/README.md" "$STAGE/usr/share/doc/typix-wechat/README.md"
printf '%s\n' 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/' 'Upstream-Name: typix-wechat' 'Comment: This package contains TypixDeck integration code only. Tencent WeChat is not included.' > "$STAGE/usr/share/doc/typix-wechat/copyright"
dpkg-deb --root-owner-group --build "$STAGE" "$DIST/typix-wechat_${VERSION}_all.deb"
