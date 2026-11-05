"""Entry point of the frozen build (PyInstaller, packaging/gsvr.spec) — also works from source for testing.

    GSVR.exe                         GUI (default of the windowed exe)
    gsvr-cli.exe [companion args]    companion CLI (default of the console exe), same options as companion.py
    <exe> --gui                      GUI
    <exe> --companion [args]         companion CLI
    <exe> --run-converter <tools/pack.py|tools/modelpack.py> [args]
                                     run a bundled converter in-process (what `python -X utf8 -I tools/pack.py` is
                                     for a source install); only those two scripts from the bundled tools/ dir
    <exe> --selftest                 headless GUI-controller self-test (gui.py --selftest)
    <exe> --update-check             print the update check result (no install)
    <exe> --version                  print the version
    <exe> --diag                     version, install kind, UTF-8 mode, paths (support)
"""
import os, runpy, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # source runs with -I drop the script dir
import runtime                                                       # noqa: E402
from version import VERSION                                          # noqa: E402


def _utf8_streams():
    """-X utf8 semantics for the console output: never crash on a cp1252 console / pipe."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def run_converter(script, args):
    """Run tools/pack.py or tools/modelpack.py from the bundle in-process, as `python -I script args` would."""
    tools = os.path.realpath(runtime.tools_dir())
    path = os.path.realpath(script)
    if os.path.dirname(path) != tools or os.path.basename(path) not in runtime.CONVERTERS:
        print(f"--run-converter: only {', '.join(runtime.CONVERTERS)} from {tools} can be run (got {script})",
              file=sys.stderr)
        return 2
    sys.argv = [path] + list(args)
    sys.path.insert(0, tools)
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _utf8_streams()
    cli_default = os.path.basename(sys.executable).lower().startswith("gsvr-cli")
    if argv[:1] == ["--run-converter"]:
        if len(argv) < 2:
            print("usage: --run-converter <script> [args]", file=sys.stderr)
            return 2
        return run_converter(argv[1], argv[2:])
    if argv[:1] == ["--version"]:
        print(VERSION)
        return 0
    if argv[:1] == ["--diag"]:
        import updater
        print(f"version {VERSION}  kind {runtime.install_kind()}  platform {updater.platform_key()}  "
              f"python {sys.version.split()[0]}  utf8_mode {sys.flags.utf8_mode}  stdout {sys.stdout.encoding}")
        print(f"install_root {runtime.install_root()}\ntools_dir    {runtime.tools_dir()}\ncompanion    {runtime.companion_argv()}")
        return 0
    if argv[:1] == ["--companion"] or (cli_default and argv[:1] not in (["--gui"], ["--selftest"], ["--update-check"], ["--diag"])):
        import companion
        return companion.main(argv[1:] if argv[:1] == ["--companion"] else argv)
    if argv[:1] == ["--update-check"]:
        import gui, updater
        c = gui.Controller()
        try:
            r = updater.check_for_update(c.settings.get("update_base"), c.settings.get("update_token"),
                                         c.settings.get("prereleases", True))
        except updater.UpdateError as e:
            print("update check failed:", e)
            return 1
        print(r["message"])
        return 0
    import gui
    if "--selftest" in argv:
        return gui.selftest()
    gui.run_gui()
    return 0


if __name__ == "__main__":
    # BUGS L-SPAWN (warcraft3 docs/BUGS.md): w3pack bakes models in a 'spawn' process pool on Windows; a frozen
    # worker is this exe started with --multiprocessing-fork, which freeze_support() must catch before main() reads
    # argv (else the worker would start a second companion / GUI). No-op in a normal process.
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
