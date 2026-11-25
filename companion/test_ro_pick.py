"""Advanced view, Spiele tab: a Ragnarok Online folder picked with 'Change' shows up in the table AT ONCE (owner 2026-11-25: "after
choosing the RO folder I had to search games again"). Root cause: the pick cleared the row and waited for rofind.find, whose drive scan
takes seconds; a scan started before the pick could also overwrite the picked folder. The child process below runs the real run_gui
window (gui_sim, no network / disk / companion), with a slow fake drive scan."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gui          # noqa: E402
import tkinter.filedialog  # noqa: E402,F401  (patched: the folder dialog)
import gui_sim      # noqa: E402
import i18n         # noqa: E402

SCAN_S = 3.0        # the fake drive scan: much longer than the 0.9 s the test gives the row


def ro_row_texts(frame, name):
    """-> the texts of the Spiele table row whose first column is `name` (column order), [] when the row is missing"""
    row = None
    cells = {}
    for w in frame.winfo_children():
        try:
            gi = w.grid_info()
            txt = str(w.cget("text"))
        except Exception:                                # noqa: BLE001 - frames have no text
            continue
        cells[(int(gi["row"]), int(gi["column"]))] = txt
        if int(gi["column"]) == 0 and txt == name:
            row = int(gi["row"])
    if row is None:
        return []
    return [cells[k] for k in sorted(cells) if k[0] == row]


def find_frame_with(widget, name):
    """the grid frame (gm_frame) inside the Spiele tab that holds a label `name`"""
    for w in widget.winfo_children():
        try:
            if str(w.cget("text")) == name and w.grid_info():
                return widget
        except Exception:                                # noqa: BLE001
            pass
        f = find_frame_with(w, name)
        if f is not None:
            return f
    return None


def change_button(frame, name):
    """the 'Change' button of the row `name`"""
    row = None
    for w in frame.winfo_children():
        try:
            if int(w.grid_info()["column"]) == 0 and str(w.cget("text")) == name:
                row = int(w.grid_info()["row"])
        except Exception:                                # noqa: BLE001
            pass
    for w in frame.winfo_children():
        gi = w.grid_info()
        if row is not None and int(gi["row"]) == row and int(gi["column"]) == 3:
            return w.winfo_children()[0]
    return None


def run_child(folder):
    out = {}
    name = None
    scans = []

    def slow_find(configured=None, with_reasons=False, **kw):    # registry + drive scan of a big PC; nothing found before the pick
        scans.append(configured)
        time.sleep(SCAN_S)
        return [(configured, "configured", "ok")] if configured else []

    class S(gui_sim.Sim):
        def ready(self, leave_first_run, nb, tabs):
            super().ready(leave_first_run, nb, tabs)
            r = self.root
            gm_tab = tabs["games"]

            def pick():
                nonlocal name
                name = i18n.tr("game.ro")
                fr = find_frame_with(gm_tab, name)
                out["row_before"] = ro_row_texts(fr, name) if fr else []
                btn = change_button(fr, name) if fr else None
                out["button"] = btn is not None
                if btn is not None:
                    btn.invoke()
                    out["t_pick"] = time.monotonic()

            def look(key):
                fr = find_frame_with(gm_tab, name or i18n.tr("game.ro"))
                out[key] = ro_row_texts(fr, name) if fr else []
            # 0.3 s: leave_first_run (Advanced view); the startup RO scan is still running when the folder is picked (1.0 s)
            r.after(1000, pick)
            r.after(1900, lambda: look("row_soon"))            # 0.9 s after the pick: long before any scan could finish
            r.after(1000 + int(SCAN_S * 1000) + 1500, lambda: look("row_late"))   # the startup scan (nothing found) came back meanwhile
            r.after(1000 + int(SCAN_S * 1000) + 1800, r.destroy)

    i18n.set_lang("en")
    with mock.patch.object(gui.rofind, "find", slow_find), \
            mock.patch.object(gui.rofind, "check", lambda p: (p == folder, "data.grf found" if p == folder else "no data.grf")), \
            mock.patch("tkinter.filedialog.askdirectory", lambda **kw: folder), \
            mock.patch.object(gui.subprocess, "Popen", mock.Mock()):
        gui.run_gui(S("en", view="advanced", tab="games"))
    out["scans"] = len(scans)
    return out


class RoPick(unittest.TestCase):
    def test_picked_folder_shows_at_once(self):
        folder = os.path.join(tempfile.gettempdir(), "gsvr_ro_pick_test", "Ragnarok Online")
        try:
            p = subprocess.run([sys.executable, "-I", "-B", os.path.abspath(__file__), "--child", folder], capture_output=True, text=True,
                               timeout=60)
        except subprocess.TimeoutExpired:                # Xcode's python 3.9 + Tk 8.5 never maps the window on macOS
            self.skipTest("Tk window did not come up")
        if p.returncode != 0 and "TclError" in p.stderr + p.stdout and "display" in p.stderr + p.stdout:
            self.skipTest("no display")
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(out["button"], out)
        self.assertNotIn(folder, out["row_before"])     # the startup scan has not found anything yet
        self.assertIn(folder, out["row_soon"], out)      # shown at once, not after the next search
        self.assertIn(folder, out["row_late"], out)      # the older scan (started before the pick) did not undo it


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(run_child(sys.argv[2])), flush=True)
        os._exit(0)
    else:
        unittest.main()
