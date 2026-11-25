"""Update re-check scheduler (updsched.py) + the simple view's inline update line (firstrun_ui.FirstRun, headless parts)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import i18n  # noqa: E402
import updsched as u  # noqa: E402
from updsched import Scheduler, CHECK_EVERY, MIN_GAP, RETRY_FIRST  # noqa: E402


class Sched(unittest.TestCase):
    def test_not_due_right_after_start_then_every_6h(self):
        s = Scheduler(1000.0)
        self.assertFalse(s.due(1000.0 + 60))
        self.assertFalse(s.due(1000.0 + CHECK_EVERY - 1))
        self.assertTrue(s.due(1000.0 + CHECK_EVERY))
        s.started()
        s.finished(True, 1000.0 + CHECK_EVERY + 5)
        self.assertFalse(s.due(1000.0 + CHECK_EVERY + 100))
        self.assertTrue(s.due(1000.0 + 2 * CHECK_EVERY + 5))

    def test_setting_off_blocks_everything(self):
        s = Scheduler(0.0)
        self.assertFalse(s.due(CHECK_EVERY * 3, enabled=False))
        self.assertFalse(s.on_restore(CHECK_EVERY * 3, enabled=False))

    def test_never_during_install_or_while_in_flight(self):
        s = Scheduler(0.0)
        self.assertFalse(s.due(CHECK_EVERY * 2, installing=True))
        self.assertFalse(s.on_restore(CHECK_EVERY * 2, installing=True))
        s.started()
        self.assertFalse(s.due(CHECK_EVERY * 2))
        self.assertFalse(s.on_restore(CHECK_EVERY * 2))
        s.finished(True, CHECK_EVERY * 2)
        self.assertFalse(s.inflight)

    def test_restore_rechecks_only_when_last_check_is_old(self):
        s = Scheduler(0.0)
        self.assertFalse(s.on_restore(MIN_GAP - 1))
        self.assertTrue(s.on_restore(MIN_GAP))
        s.started()
        s.finished(True, MIN_GAP)
        self.assertFalse(s.on_restore(MIN_GAP + 60))          # minimise / restore cycling does not hit the feed

    def test_backoff_after_errors(self):
        s = Scheduler(0.0)
        t = CHECK_EVERY
        waits = []
        for _ in range(8):
            s.started()
            s.finished(False, t)
            waits.append(s.next_at - t)
            self.assertFalse(s.due(t + 1))
            self.assertFalse(s.on_restore(t + 1))              # restore does not shortcut the backoff
            t = s.next_at
        self.assertEqual(waits[0], RETRY_FIRST)
        self.assertEqual(waits[1], RETRY_FIRST * 2)
        self.assertTrue(all(b >= a for a, b in zip(waits, waits[1:])))
        self.assertEqual(waits[-1], CHECK_EVERY)               # capped at the normal interval
        s.started()
        s.finished(True, t)
        self.assertEqual(s.fails, 0)
        self.assertEqual(s.next_at, t + CHECK_EVERY)

    def test_gui_wires_the_scheduler(self):
        src = open(os.path.join(HERE, "gui.py"), encoding="utf-8").read()
        self.assertIn("updsched.Scheduler(", src)
        self.assertIn("sched.due(", src)
        self.assertIn("sched.on_restore(", src)
        self.assertIn('root.bind("<Map>"', src)
        self.assertIn("installing.is_set()", src.split("def update_tick")[1][:400])
        self.assertIn("first.update_result(info)", src)
        self.assertIn("on_check=", src)
        spec = open(os.path.join(HERE, "packaging", "gsvr.spec"), encoding="utf-8").read()
        self.assertIn('"updsched"', spec)


class FakeFirstRun:
    """FirstRun's update-line logic without Tk (the methods only need self.upd / self.phase / self.on_check)."""
    def __init__(self):
        import firstrun_ui
        self.cls = firstrun_ui.FirstRun
        self.upd = ("", None)
        self.phase = "home"
        self.calls = 0
        self.on_check = self._cb
        self.visible = False
        self.tk = type("T", (), {"TclError": Exception})

    def _cb(self):
        self.calls += 1

    def __getattr__(self, name):
        return lambda *a, **k: getattr(self.cls, name)(self, *a, **k)


class SimpleViewLine(unittest.TestCase):
    def setUp(self):
        i18n.set_lang("en")

    def tearDown(self):
        i18n.set_lang("en")

    def test_click_shows_checking_then_result(self):
        f = FakeFirstRun()
        f.check_clicked()
        self.assertEqual(f.calls, 1)
        self.assertEqual(f.upd_text(), i18n.tr("fr.upd_checking"))
        f.check_clicked()                                      # a second click while checking does nothing
        self.assertEqual(f.calls, 1)
        f.update_result({"status": "current", "message": "x"})
        self.assertEqual(f.upd_text(), i18n.tr("fr.upd_current"))

    def test_available_points_to_the_banner(self):
        f = FakeFirstRun()
        f.check_clicked()
        f.update_result({"status": "available", "latest": "0.9.99"})
        self.assertIn("0.9.99", f.upd_text())
        self.assertIn(i18n.tr("d.install"), f.upd_text().replace("Install", i18n.tr("d.install")))

    def test_failed_after_click(self):
        f = FakeFirstRun()
        f.check_clicked()
        f.update_result(None)
        self.assertEqual(f.upd_text(), i18n.tr("fr.upd_failed"))

    def test_background_checks_stay_quiet(self):
        f = FakeFirstRun()
        f.update_result({"status": "current"})
        self.assertEqual(f.upd_text(), "")
        f.update_result(None)
        self.assertEqual(f.upd_text(), "")
        f.update_result({"status": "available", "latest": "1.0.0"})      # but news is shown even unasked
        self.assertIn("1.0.0", f.upd_text())

    def test_all_languages(self):
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            for k in ("fr.version", "fr.check_upd", "fr.upd_checking", "fr.upd_current", "fr.upd_failed"):
                self.assertTrue(i18n.tr(k, v="0.9.54").strip(), (lang, k))
            self.assertIn("0.9.55", i18n.tr("fr.upd_avail", b="0.9.55"))
        i18n.set_lang("ja")
        self.assertNotEqual(i18n.tr("fr.check_upd"), "Check for updates")

    def test_home_screen_builds_the_line(self):
        """the version row of the checklist carries the update state (simple view = firstrun.checklist)"""
        import firstrun
        rows = {u: firstrun.checklist([], None, False, upd=(u, "0.9.99"), version="0.9.57")["rows"][2] for u in
                ("", "checking", "current", "available", "failed")}
        self.assertEqual(rows["available"]["button"], ("d.install", "install"))
        self.assertIn("0.9.99", rows["available"]["text"])
        self.assertEqual(rows[""]["button"], ("fr.check_upd", "check"))
        self.assertEqual(rows["failed"]["button"], ("fr.check_upd", "check"))
        self.assertIsNone(rows["current"]["button"])
        for r in rows.values():
            self.assertIn("0.9.57", r["text"])

    def test_real_widgets_if_tk_available(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception:                                      # noqa: BLE001 - no display / no Tk: the headless tests above cover the logic
            self.skipTest("no Tk display")
        try:
            import firstrun_ui

            class C:
                settings = {"first_run_done": True}

                def running(self):
                    return False

                def status(self):
                    return None

                def save_settings(self):
                    pass
            got = []
            f = firstrun_ui.FirstRun(root, C(), lambda fn: None, lambda: None, lambda code: None, on_check=lambda: got.append(1),
                                     detect_fn=lambda s: [], vrc_fn=lambda s, conn: "unknown")
            f.visible = True
            f.frame.pack()
            f.apply()
            self.assertIn(".", f.w["row_version"].cget("text"))
            f.check_clicked()
            self.assertIn(i18n.tr("cl.ver_checking", v="").strip(" ·"), f.w["row_version"].cget("text"))
            f.update_result({"status": "current"})
            self.assertIn(i18n.tr("cl.ver_current", v="").strip(" ·"), f.w["row_version"].cget("text"))
            self.assertEqual(got, [1])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
