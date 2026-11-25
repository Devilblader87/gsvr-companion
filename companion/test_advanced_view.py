"""'Advanced view' link of the simple view: it opens the tabs and they STAY (owner 2026-11-24: "just loads the simple view again").
Root cause: a language picked in the simple view made leave_first_run restart the app, and a restart always opens the simple view."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gui          # noqa: E402
import gui_sim      # noqa: E402
import i18n         # noqa: E402


class Argv(unittest.TestCase):
    def test_restart_argv(self):
        self.assertEqual(gui.restart_argv(["py", "gui.py"], True), ["py", "gui.py", "--advanced"])
        self.assertEqual(gui.restart_argv(["py", "gui.py", "--advanced"], True), ["py", "gui.py", "--advanced"])   # never doubled
        self.assertEqual(gui.restart_argv(["py", "gui.py", "--advanced"], False), ["py", "gui.py"])               # one-shot


def run_sim(lang_switch):
    """one run_gui window in THIS process (the test runs it in a child: a second Tk() in one process crashes on macOS)"""
    out = {}

    class S(gui_sim.Sim):
        def ready(self, leave_first_run, nb, tabs):
            super().ready(leave_first_run, nb, tabs)
            r = self.root

            def click():
                if lang_switch:
                    i18n.set_lang("de")              # language chosen in the simple view (on_lang does exactly this)
                leave_first_run()
                try:
                    out["after_click"] = bool(nb.winfo_ismapped())
                except Exception:                    # noqa: BLE001 - the restart destroyed the window
                    pass
            r.after(400, click)
            for t in (1200, 2600, 4000):             # several poll ticks (pump_ui 200 ms, simple-view tick, status)
                r.after(t, lambda: out.setdefault("ticks", []).append(bool(nb.winfo_ismapped())))
            r.after(4400, r.destroy)

    popen = mock.Mock()
    i18n.set_lang("en")
    with mock.patch.object(gui.subprocess, "Popen", popen):
        gui.run_gui(S("en", view="simple"))
    out["argv"] = popen.call_args[0][0] if popen.called else None
    return out


class Drive(unittest.TestCase):
    def child(self, mode):
        try:
            p = subprocess.run([sys.executable, "-I", "-B", os.path.abspath(__file__), "--child", mode], capture_output=True, text=True, timeout=40)
        except subprocess.TimeoutExpired:                # Xcode's python 3.9 + Tk 8.5 never maps the window on macOS
            self.skipTest("Tk window did not come up")
        if p.returncode != 0 and "TclError" in p.stderr + p.stdout and "display" in p.stderr + p.stdout:
            self.skipTest("no display")
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_advanced_stays(self):
        out = self.child("plain")
        self.assertTrue(out["after_click"])
        self.assertEqual(out["ticks"], [True, True, True])
        self.assertIsNone(out["argv"])                   # no restart

    def test_language_switch_then_advanced_restarts_into_advanced(self):
        out = self.child("lang")
        self.assertIsNotNone(out["argv"])
        self.assertIn("--advanced", out["argv"])


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(run_sim(sys.argv[2] == "lang")), flush=True)
        os._exit(0)                                      # a restart leaves background threads of the sim controller behind
    else:
        unittest.main()
