"""One dark design system for both views (ui_theme.py), the window always opening in the simple view, and the startup rules that keep
the Tk main thread free (owner 2026-11-24: Advanced view looked like before, window opened in the Advanced view, window lagged while
dragged in the first seconds). Tk-free; the real main-thread timing runs with GSVR_TK_TIMING=1 (ui_timing.py needs Tk 8.6)."""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import firstrun  # noqa: E402
import firstrun_ui  # noqa: E402
import i18n  # noqa: E402
import ui_theme  # noqa: E402


def src(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def run_gui_src():
    s = src("gui.py")
    return s[s.index("def run_gui("):s.index("# ===", s.index("def run_gui("))]


class Theme(unittest.TestCase):
    def test_one_palette_for_both_views(self):
        self.assertIs(firstrun_ui.COL, ui_theme.COL)
        self.assertIs(firstrun_ui.LIGHTS, ui_theme.LIGHTS)
        for k in ("green", "yellow", "red", "grey", "orange"):
            self.assertIn(ui_theme.LIGHTS[k], ui_theme.COL.values())

    def test_text_contrast(self):
        C = ui_theme.COL
        for fg, bg in ui_theme.TEXT_PAIRS:
            self.assertGreaterEqual(ui_theme.contrast(C[fg], C[bg]), 4.5, (fg, bg))
        for light in ("green", "yellow", "red"):                    # traffic lights on the cards
            self.assertGreaterEqual(ui_theme.contrast(ui_theme.LIGHTS[light], C["panel"]), 3.0, light)

    def test_advanced_view_uses_the_theme(self):
        s = run_gui_src()
        self.assertIn("ui_theme.apply_ttk(root, UIF)", s)
        self.assertIn("root_bg = C[\"bg\"]", s)
        self.assertNotRegex(s, r'"#(fff4c2|555|a00|c00000|a00000|006000)"')     # no light-theme colours left in the window
        self.assertNotRegex(s, r"(?<!t)tk\.Button\(")                                     # tk.Button ignores colours on macOS: ttk styles only
        self.assertIn("ui_theme.style_text(tk.Text(", s)
        self.assertIn("ui_theme.style_text(tk.Listbox(", s)
        for style in ("Primary.TButton", "Stop.TButton", "Link.TButton", "PanelName.TLabel", "PanelMuted.TLabel", "PanelBad.TLabel",
                      "Muted.TLabel", "Head.TLabel"):
            self.assertIn('"%s"' % style, src("ui_theme.py"), style)

    def test_all_old_tabs_and_functions_kept(self):
        s = run_gui_src()
        for k in ('tr("tab.worlds")', 'tr("tab.status")', 'tr("tab.games")', 'tr("tab.log")', 'tr("tab.settings")', "mod_box",
                  "btn_check", "btn_cache", "btn_clear", "btn_cchg", "btn_cdef", "hl_search", "war3_detect", "rose_build_all",
                  "copy_log", "lang_cb", "btn_install", "btn_easy", "show_build"):
            self.assertIn(k, s, k)

    def test_font_family_is_cached(self):
        for name in ("gui.py", "firstrun_ui.py"):
            self.assertNotIn("tkfont.families(", src(name), name)              # only ui_theme.families() (cached) may enumerate fonts

        class FakeTk:
            calls = 0

        class Root:
            tk = FakeTk()
        import tkinter.font as tkfont
        orig = tkfont.families

        def fams(root=None, displayof=None):
            FakeTk.calls += 1
            return ("Segoe UI", "Yu Gothic UI")
        tkfont.families = fams
        try:
            r = Root()
            self.assertEqual(ui_theme.family(r, "ja"), "Yu Gothic UI")
            self.assertEqual(ui_theme.family(r, "en"), "Segoe UI")
            ui_theme.family(r, "de")
            self.assertEqual(FakeTk.calls, 1)
        finally:
            tkfont.families = orig
            ui_theme._families.pop(str(Root.tk), None)


class StartView(unittest.TestCase):
    def test_always_simple(self):
        for done in (False, True):
            for view in (None, "simple", "advanced", "main"):
                st = {"first_run_done": done}
                if view:
                    st["view"] = view
                self.assertEqual(firstrun.start_view(st, False), "simple", st)
        self.assertEqual(firstrun.start_view({}, True), "firstrun")

    def test_gui_never_restores_or_stores_the_advanced_view(self):
        s = run_gui_src()
        self.assertIsNone(re.search(r"settings\.update\([^)]*\bview=", s))   # the last view is neither saved nor read
        self.assertIsNone(re.search(r"settings\.get\(\"view\"", s))
        self.assertIn("enter_simple()\n", s)                         # shown before the main loop: the tabs never flash up
        self.assertNotIn("root.after(50, enter_simple)", s)
        self.assertIn('tr("fr.easy")', s)                            # 'Simple view' button in the Advanced view
        self.assertIn('tr("fr.open_app")', src("firstrun_ui.py"))    # 'Advanced view' link in the simple view
        for lang in i18n.LANGS:
            self.assertTrue(i18n.tr_in(lang, "fr.easy") and i18n.tr_in(lang, "fr.open_app"))


class MainThread(unittest.TestCase):
    def test_no_blocking_start_on_the_tk_thread(self):
        """c.start() builds argv (may fetch the world list) and spawns the companion: only from a background thread"""
        s = run_gui_src()
        bare = [m.start() for m in re.finditer(r"(?<!_)c\.start\(\)", s)]
        allowed = s.index("def start_bg():")
        self.assertEqual([p for p in bare if not (allowed < p < allowed + 400)], [])
        self.assertIn("threading.Thread(target=c.official_worlds", s)
        self.assertIn("sys.setswitchinterval(GIL_SWITCH_S)", s)

    def test_simple_view_status_does_not_wait_for_the_game_scan(self):
        s = src("firstrun_ui.py")
        body = s[s.index("    def refresh(self"):s.index("    def compute(self")]
        self.assertIn('self.q.put(("games", ents))', body)
        self.assertIn('self.q.put(("inputs", (st, run, vrc)))', body)
        self.assertIn("self.det_busy", body)

    def test_searching_state_in_games_row(self):
        m = firstrun.checklist([], None, False, "unknown", None, ("", None), "0.9.57", True, scanning=True)
        row = m["rows"][0]
        self.assertEqual((row["key"], row["status"]), ("games", "busy"))
        self.assertEqual(row["text"], i18n.tr("fr.searching"))

    def test_next_highlight_is_no_rebuild(self):
        r = {"key": "untrusted", "status": "warn", "next": True, "button": None, "chips": None, "progress": None, "how": True}
        self.assertEqual(firstrun_ui.FirstRun._row_sig(r), firstrun_ui.FirstRun._row_sig(dict(r, next=False)))
        self.assertNotEqual(firstrun_ui.FirstRun._row_sig(r), firstrun_ui.FirstRun._row_sig(dict(r, status="ok")))

    @unittest.skipUnless(os.environ.get("GSVR_TK_TIMING") == "1", "opens a window: GSVR_TK_TIMING=1 [GSVR_TK_PY=python with Tk 8.6]")
    def test_startup_main_thread_timing(self):
        py = os.environ.get("GSVR_TK_PY") or sys.executable
        for view in ("simple", "advanced"):
            r = subprocess.run([py, "-I", "-B", os.path.join(HERE, "ui_timing.py"), "8", view], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])

    @unittest.skipUnless(os.environ.get("GSVR_TK_TIMING") == "1", "opens a window: GSVR_TK_TIMING=1 [GSVR_TK_PY=python with Tk 8.6]")
    def test_language_switch_window_fits(self):
        py = os.environ.get("GSVR_TK_PY") or sys.executable
        r = subprocess.run([py, "-I", "-B", os.path.join(HERE, "ui_langfit.py")], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
