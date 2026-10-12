#!/bin/bash
# Build a standalone "Posture Guard.app" and a .dmg to share it.
#
#   ./build_app.sh
#
# Output: dist/Posture Guard.app and dist/PostureGuard-<version>-<arch>.dmg
# The app bundles Python and every library, so the person you share it with needs nothing installed.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
VERSION=$(cat VERSION)
ARCH=$(uname -m)
DMG="dist/PostureGuard-$VERSION-$ARCH.dmg"

if [ ! -x "$PY" ]; then
    echo "Set up the project first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
"$PY" -m pip install -q -r requirements.txt pyinstaller pillow

echo "==> Drawing the icon"
"$PY" packaging/make_icon.py

echo "==> Building the app (takes a minute or two)"
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath dist --workpath build packaging/PostureGuard.spec

# Ad-hoc signature, so macOS runs it and remembers its camera permission.
codesign --force --deep --sign - "dist/Posture Guard.app"

echo "==> Making the disk image"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp -R "dist/Posture Guard.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "Posture Guard" -srcfolder "$STAGE" -format UDZO "$DMG"

echo
echo "Built:"
echo "  dist/Posture Guard.app  ($(du -sh "dist/Posture Guard.app" | cut -f1))"
echo "  $DMG  ($(du -h "$DMG" | cut -f1))"
