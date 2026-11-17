# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the GSVR companion — ONE-FOLDER build with two executables sharing one runtime:
#   GSVR(.exe)      windowed GUI (console=False)       → gui.run_gui()
#   gsvr-cli(.exe)  console: companion CLI, `--run-converter tools/pack.py|modelpack.py`, `--selftest`, `--version`
# Both start companion/gsvr_main.py, which dispatches on argv / exe name. The converter tools are bundled twice:
# as modules (so PyInstaller pulls in their stdlib imports: argparse, wave, array, …) and as data files in
# _internal/tools/*.py (builder.py hashes them for the cache key; --run-converter executes them via runpy).
# One-folder (not one-file): faster start, no temp-dir unpacking, fewer antivirus false positives, and the
# self-updater can swap individual files.
#
# Build:  companion\packaging\build_windows.bat   (Windows)  ·  companion/packaging/build_linux.sh   (Linux)
# Output: companion/build/dist/GSVR/
import os

SPEC_DIR = os.path.abspath(SPECPATH)                    # companion/packaging
COMP = os.path.dirname(SPEC_DIR)                         # companion/
ROOT = os.path.dirname(COMP)                             # repo root
TOOLS = os.path.join(ROOT, "tools")
import sys
sys.path.insert(0, SPEC_DIR)
import toolfiles                                         # BUGS C-SYNC: entries + their tools/ imports, no hand list
TOOL_FILES = toolfiles.tool_files(TOOLS)
_miss = toolfiles.missing_imports(TOOLS, TOOL_FILES)
if _miss:
    raise SystemExit("gsvr.spec: converter modules not bundled: " + ", ".join(_miss))
# the W5+ engine tables and JASS compiler of w3pack_jvm.py: package tools/jvm (synced without its tests)
JVM_FILES = sorted(f for f in os.listdir(os.path.join(TOOLS, "jvm")) if f.endswith(".py") and not f.startswith("test_"))
COMPANION_MODULES = ["companion", "gui", "updater", "runtime", "version", "builder", "models", "prebuilt", "games", "steam",
                     "vdf", "logwatch", "selftest_update", "war3", "mapshare", "csshare", "cachedir", "rose", "rosefind", "rosebuild",
                     "rose_conv_stub", "modules", "ed25519"]    # --selftest runs one stub ROSE build in a child (gsvr-cli --run-rose-build)
# w3pack.py decodes BLP (JPEG inside) with Pillow; the tools run via runpy, so PyInstaller must be told
PILLOW_MODULES = ["PIL", "PIL.Image", "PIL.ImageChops", "PIL.JpegImagePlugin", "PIL.BlpImagePlugin",
                  "PIL.ImageFont", "PIL.ImageDraw", "PIL.TgaImagePlugin", "PIL.PngImagePlugin"]   # HUD / menu fonts, previews
# tools/music.py decodes the CD music mp3s with the `miniaudio` wheel (dr_mp3, cffi extension). _cffi_backend is imported by the extension's C code only,
# so PyInstaller cannot see it. Without the package the frozen converter would silently build maps with no music: refuse (requirements-build.txt).
try:
    import miniaudio                                     # noqa: F401
except ImportError:
    raise SystemExit("gsvr.spec: the miniaudio package is not installed in the build venv (pip install -r companion/packaging/requirements-build.txt)")
MUSIC_MODULES = ["miniaudio", "_miniaudio", "_cffi_backend"]
# ROSE converter (companion/rose_conv, BYOG: builds the RPG1 packs from the player's own ROSE install; docs/design/rose_companion_build.md).
# The converter modules import each other as top-level names (rose_conv/__init__ puts its folder on sys.path), several only inside functions:
# every module of SOURCE_HASHES is a hidden import, rose_conv/ is on pathex so PyInstaller finds them. test_rose_packaging.py guards this list.
ROSE_CONV = os.path.join(COMP, "rose_conv")
with open(os.path.join(ROSE_CONV, "SOURCE_HASHES"), encoding="utf-8") as _f:
    ROSE_MODULES = ["rose_conv", "rose_qglob", "rose_skilltips"] + [os.path.splitext(ln.split()[-1])[0] for ln in _f.read().splitlines() if ln.strip()]
ROSE_DATA = [(os.path.join(ROSE_CONV, f), "rose_conv") for f in ("VERSION_SOURCE", "lobby_default.json")]
ICON = os.path.join(SPEC_DIR, "gsvr.ico")                # optional: drop a gsvr.ico here
icon = ICON if os.path.isfile(ICON) else None

a = Analysis(
    [os.path.join(COMP, "gsvr_main.py")],
    pathex=[COMP, TOOLS, os.path.join(COMP, "rose_conv")],
    binaries=[],
    datas=[(os.path.join(TOOLS, f), "tools") for f in TOOL_FILES] + [(os.path.join(TOOLS, "jvm", f), "tools/jvm")
                                                                    for f in JVM_FILES]
          + [(os.path.join(COMP, "README.md"), ".")] + ROSE_DATA,
    hiddenimports=COMPANION_MODULES + [os.path.splitext(f)[0] for f in TOOL_FILES]
                  + ["jvm"] + ["jvm." + os.path.splitext(f)[0] for f in JVM_FILES if f != "__init__.py"] + ["tkinter", "tkinter.ttk",
                                                                                    "tkinter.messagebox"] + PILLOW_MODULES + MUSIC_MODULES + ROSE_MODULES,
    hookspath=[],
    runtime_hooks=[],
    excludes=["unittest", "pydoc_data", "test", "lib2to3", "idlelib", "ensurepip", "distutils", "setuptools", "pip"],
    noarchive=False,
)
# Tcl/Tk data the GUI never uses: time zones (609 files), message catalogs (English fallback is built in) → ~750 files
# fewer in the install (the encodings stay: Tcl needs them for file names / console I/O)
DROP = ("_tcl_data/tzdata", "_tcl_data/msgs", "_tk_data/msgs", "_tk_data/demos", "_tk_data/images")
a.datas = [d for d in a.datas if not d[0].replace(os.sep, "/").startswith(DROP)]

pyz = PYZ(a.pure)

# -X utf8 for the frozen interpreter (converter + companion output must not depend on the console code page).
utf8 = [("X utf8", None, "OPTION")]

gui = EXE(pyz, a.scripts, utf8, exclude_binaries=True, name="GSVR", console=False, icon=icon,
          upx=False, strip=False, debug=False, bootloader_ignore_signals=False, disable_windowed_traceback=False)
cli = EXE(pyz, a.scripts, utf8, exclude_binaries=True, name="gsvr-cli", console=True, icon=icon,
          upx=False, strip=False, debug=False)
coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="GSVR")
