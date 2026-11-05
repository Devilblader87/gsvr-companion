#!/bin/sh
# Builds the Linux release of the GSVR companion: companion/build/dist/GSVR/{GSVR, gsvr-cli, _internal/}
# and companion/build/release/gsvr-companion-linux-x64.tar.gz + .sha256 (no upload).
# Needs: python3 (3.10+) with venv + tkinter (Debian/Ubuntu: apt install python3-venv python3-tk), internet for pip.
# Build on the OLDEST distro you want to support (glibc is forward- but not backward-compatible).
set -eu
cd "$(dirname "$0")/../.."
BUILD=companion/build
VENV=$BUILD/venv-linux
PY=${PYTHON:-python3}

[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check -q -r companion/packaging/requirements-build.txt

"$VENV/bin/python" -I companion/selftest_update.py
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --distpath "$BUILD/dist" --workpath "$BUILD/work" companion/packaging/gsvr.spec

"$BUILD/dist/GSVR/gsvr-cli" --version
"$BUILD/dist/GSVR/gsvr-cli" --run-converter "$BUILD/dist/GSVR/_internal/tools/modelpack.py" --version
"$BUILD/dist/GSVR/gsvr-cli" --selftest

"$VENV/bin/python" -I companion/packaging/release.py package --platform linux-x64
echo "OK - archives in $BUILD/release/"
