#!/bin/bash
# Reproducible local build of Claude Switcher.app on this machine.
# Wraps the repo's build_app.sh with two steps it assumes are already done:
#   1. make the src/-layout package importable to py2app (pip install -e .)
#   2. vendor libffi.8.dylib, which py2app does not copy for this Python build
set -e
cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q rumps 'py2app>=0.28'
pip install -q -e .                       # step 1: src/ layout importable

mv pyproject.toml pyproject.toml.bak
trap 'mv pyproject.toml.bak pyproject.toml 2>/dev/null || true' EXIT
rm -rf build dist
python3 setup.py py2app

APP="dist/Claude Switcher.app"
FFI=$(python3 - <<'PY'
import glob, sys, os
base = sys.base_prefix
c = glob.glob(os.path.join(base, "lib", "libffi.8.dylib"))
print(c[0] if c else "")
PY
)
if [ -n "$FFI" ] && [ ! -f "$APP/Contents/Frameworks/libffi.8.dylib" ]; then
    cp "$FFI" "$APP/Contents/Frameworks/libffi.8.dylib"   # step 2: vendor libffi
    chmod u+w "$APP/Contents/Frameworks/libffi.8.dylib"
fi
codesign --force --deep -s - "$APP"       # re-sign ad-hoc after the edit

echo "Built: $APP"
