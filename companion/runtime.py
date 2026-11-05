"""Where am I running? Source install (python + companion/*.py + tools/*.py) or a frozen PyInstaller build.

Frozen layout (one-folder build, see packaging/gsvr.spec):
    GSVR/GSVR.exe          windowed GUI           (Linux: GSVR/GSVR)
    GSVR/gsvr-cli.exe      console: companion, converter, selftest   (Linux: GSVR/gsvr-cli)
    GSVR/_internal/        python runtime + tools/*.py (data, hashed for the cache key and run by --run-converter)
    GSVR/gsvr-manifest.txt list of shipped files (updater: stale-file cleanup)

A frozen build has no python interpreter to run `python -I tools/pack.py`, so every "run a python script"
goes through `gsvr-cli --run-converter <tools/pack.py|tools/modelpack.py> ARGS` (gsvr_main.py), which runs
the bundled script in-process. UTF-8 mode is set by the spec (X utf8) and again by gsvr_main (stream reconfigure).
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))
WIN = sys.platform == "win32"
EXE_SUFFIX = ".exe" if WIN else ""
GUI_EXE = "GSVR" + EXE_SUFFIX
CLI_EXE = "gsvr-cli" + EXE_SUFFIX
CONVERTERS = ("pack.py", "modelpack.py", "w3pack.py", "previewpack.py")          # the only scripts --run-converter will execute


def install_root():
    """Folder the updater swaps files in. Frozen: the folder with GSVR.exe. Source: the folder with companion/ + tools/."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(HERE)


def bundle_dir():
    """Frozen: PyInstaller's data dir (_internal/ on PyInstaller 6). Source: same as install_root()."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", install_root())
    return install_root()


def tools_dir():
    return os.path.join(bundle_dir(), "tools")


def cli_exe():
    """Console executable of a frozen build (used for the companion process and the converter)."""
    return os.path.join(install_root(), CLI_EXE)


def gui_exe():
    return os.path.join(install_root(), GUI_EXE)


def console_python():
    """The console interpreter (pythonw has no stdout → the companion must run under python.exe)."""
    exe = sys.executable
    if WIN and os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(cand):
            exe = cand
    return exe


def companion_argv():
    """argv prefix that starts the companion CLI as a child process."""
    if FROZEN:
        return [cli_exe(), "--companion"]
    return [console_python(), "-X", "utf8", "-I", "-u", os.path.join(HERE, "companion.py")]


def script_prefix(frozen=None):
    """Converter command template prefix: how {python} runs {converter}."""
    frozen = FROZEN if frozen is None else frozen
    return "{python} --run-converter {converter}" if frozen else "{python} -I {converter}"


def script_argv(python, script, *args, frozen=None):
    """argv to run a converter script directly (for --help / --version probes)."""
    frozen = FROZEN if frozen is None else frozen
    if frozen:
        return [python, "--run-converter", script, *args]
    return [python, "-X", "utf8", "-I", script, *args]


def install_kind():
    return "frozen" if FROZEN else "source"


# maintainer 2026-10-23: a conversion must never take the PC from VRChat / SteamVR. The converter (and its bake pool,
# which inherits the priority) runs below normal priority: Windows BELOW_NORMAL_PRIORITY_CLASS, elsewhere nice +10.
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _nice():
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def low_priority_kwargs():
    """subprocess.run / Popen keyword arguments that start the child below normal priority."""
    if sys.platform == "win32":
        return {"creationflags": BELOW_NORMAL_PRIORITY_CLASS}
    return {"preexec_fn": _nice}

