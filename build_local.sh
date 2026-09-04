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

# py2app bundles the C-extension .so files (libffi-backed _ctypes, _ssl, _hashlib,
# ...) but does not always vendor the @rpath dylibs they link against. Find every
# such dylib the bundle's .so files need and copy it from the interpreter's lib
# dir into Contents/Frameworks, where @rpath resolves. Without this the app fails
# at launch (libffi) or loses HTTPS/ssl at runtime (libssl/libcrypto).
python3 - "$APP" <<'PYV'
import os, sys, glob, subprocess, shutil
app = sys.argv[1]
libdir = os.path.join(sys.base_prefix, "lib")
fw = os.path.join(app, "Contents", "Frameworks")
os.makedirs(fw, exist_ok=True)

def rpath_deps(binary):
    out = subprocess.run(["otool", "-L", binary], capture_output=True, text=True).stdout
    deps = []
    for line in out.splitlines()[1:]:
        ref = line.strip().split(" ")[0]
        if ref.startswith("@rpath/"):
            deps.append(ref.split("/", 1)[1])
    return deps

pending = set()
for so in glob.glob(os.path.join(app, "**", "*.so"), recursive=True) + \
          glob.glob(os.path.join(fw, "*.dylib")):
    for dep in rpath_deps(so):
        pending.add(dep)

copied, missing = [], []
seen = set()
while pending:
    name = pending.pop()
    if name in seen:
        continue
    seen.add(name)
    dst = os.path.join(fw, name)
    if os.path.exists(dst):
        for d in rpath_deps(dst):
            pending.add(d)
        continue
    src = os.path.join(libdir, name)
    if os.path.exists(src):
        shutil.copy(src, dst)
        os.chmod(dst, 0o644)
        copied.append(name)
        for d in rpath_deps(dst):   # transitive deps (libssl -> libcrypto)
            pending.add(d)
    else:
        missing.append(name)

print("vendored dylibs:", copied or "(none needed)")
if missing:
    print("WARNING: could not find:", missing)
PYV

codesign --force --deep -s - "$APP"       # re-sign ad-hoc after vendoring

echo "Built: $APP"
