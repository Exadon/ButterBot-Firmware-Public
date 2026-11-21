#!/bin/bash
# Builds dist/DimensionHopper.app (and dist/DimensionHopper-mac.zip to share it) on a Mac. Run from anywhere:
#   bash tools/updater/build_mac.sh
# The app is built for the Mac's own processor (Apple silicon on a current Mac or CI runner).
set -euo pipefail
cd "$(dirname "$0")"

# The build's Python environment. ".nosync": iCloud Drive (which syncs ~/Documents) leaves this folder alone - inside
# a synced folder it scatters "file 2.py" copies through the packages, and PyInstaller then analyses thousands of
# extra modules (a 3-minute build turns into hours)
VENV=.venv.nosync
if [ ! -d "$VENV" ]; then
    # CI (actions/setup-python) says which Python to use; locally, 3.13 like the Windows build (the speech-to-text
    # packages publish wheels up to it)
    if [ -n "${pythonLocation:-}" ]; then "$pythonLocation/bin/python3" -m venv "$VENV"
    elif command -v python3.13 >/dev/null; then python3.13 -m venv "$VENV"
    else python3 -m venv "$VENV"; fi
fi
PY="$VENV/bin/python"
echo "Python: $($PY --version 2>&1) ($(uname -m))"
if pgrep -fq "DimensionHopper.app/Contents/MacOS/DimensionHopper"; then
    echo "Dimension Hopper is running - quit it first." >&2; exit 1
fi
$PY -m pip install -q --disable-pip-version-check -r requirements.txt
# Voice-command list for the Guide tab, regenerated from the firmware sources on every build
$PY gen_guide_data.py

rm -rf dist/DimensionHopper.app dist/DimensionHopper-mac.zip
# Build and sign outside the repo: a folder synced by iCloud Drive (like ~/Documents) tags the bundle with Finder info,
# and codesign refuses to sign it ("resource fork, Finder information, or similar detritus not allowed")
WORK=$(mktemp -d "${TMPDIR:-/tmp}/dimensionhopper.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
# --onedir: macOS wants a real .app folder (PyInstaller is dropping one-file .app bundles). The PNG icon is turned
# into the .icns a Mac app needs (PyInstaller does that with Pillow).
"$VENV/bin/pyinstaller" --noconfirm --clean --onedir --windowed --distpath "$WORK/dist" --workpath "$WORK/build" \
    --name DimensionHopper --icon portal.png --osx-bundle-identifier io.github.exadon.dimensionhopper \
    --add-data "portal.png:." --add-data "bot.png:." --add-data "controller.png:." --add-data "clockstar.png:." \
    --add-data "bot_guide.png:." --add-data "controller_guide.png:." --add-data "guide_data.json:." \
    --add-data "screen_data.json:." --add-data "phonemes.txt.gz:." --add-data "CMUDICT_LICENSE:." \
    --collect-all esptool --collect-all numpy --collect-all faster_whisper --collect-all ctranslate2 \
    --collect-all tokenizers --collect-submodules anthropic --collect-submodules keyring \
    --hidden-import keyring.backends.macOS \
    --collect-all bleak --hidden-import objc --collect-submodules CoreBluetooth --collect-submodules Foundation \
    --collect-submodules libdispatch \
    dimension_hopper.py

APP="$WORK/dist/DimensionHopper.app"
PLIST="$APP/Contents/Info.plist"
VERSION=$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' dimension_hopper.py)
# macOS stops an app that uses Bluetooth or the local network without saying why it does
/usr/libexec/PlistBuddy -c "Delete :NSBluetoothAlwaysUsageDescription" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSBluetoothAlwaysUsageDescription string Dimension Hopper connects to ButterBot over Bluetooth when you use the Controller tab wirelessly." "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :NSLocalNetworkUsageDescription" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSLocalNetworkUsageDescription string ButterBot talks to the AI brain in Dimension Hopper over your home network." "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :NSHighResolutionCapable" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST"
# Editing Info.plist breaks PyInstaller's signature: sign again (ad hoc - not notarized, see README)
xattr -cr "$APP"  # codesign refuses files carrying Finder info / extended attributes
codesign --force --deep --sign - "$APP"

codesign --verify --deep --strict "$APP"
mkdir -p dist
ditto -c -k --keepParent "$APP" dist/DimensionHopper-mac.zip
ditto "$APP" dist/DimensionHopper.app
echo "Built: dist/DimensionHopper.app ($VERSION)"
echo "Zipped: dist/DimensionHopper-mac.zip"
