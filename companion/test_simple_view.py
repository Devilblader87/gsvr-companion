"""Simple view = one checklist screen (firstrun.checklist): next row, titles per state, buttons, no paths / forbidden words, contrast."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import firstrun  # noqa: E402
import firstrun_ui  # noqa: E402
import i18n  # noqa: E402
import worlds  # noqa: E402

ROSE_ROOT = "C:\\Games\\ROSE Online"
HL_ROOT = "D:\\SteamLibrary\\steamapps\\common\\Half-Life"
FORBIDDEN = re.compile(r"\bfree\b|無料|kostenlos|gratis|searchmmo|jrose|irose|osrose|download a rose|install a rose|ROSEのクライアント"
                       r"|ROSE-Online-Client|client download", re.I)
PATHISH = re.compile(r"[A-Za-z]:\\|\\\\|/home/|/Users/|steamapps|common\\|wrld_")


def ents(rose=True, hl=True):
    out = []
    for g in firstrun.ORDER:
        found = (g == "rose" and rose) or (g == "valve" and hl)
        out.append({"id": g, "name": firstrun.game_name(g), "found": found, "root": (ROSE_ROOT if g == "rose" else HL_ROOT) if found else "",
                    "how": "steam" if found else "none", "world": worlds.world_for_game(g), "picked": False})
    return out


def st(seen=-1, world=None, **kw):   # seen: ignored, kept for the old call sites
    d = {"app": "gsvr-companion", "world": world, "world_ok": True, "build": [], "rose_build": {}}
    d.update(kw)
    return d


READY = dict(st=st(3, worlds.WORLDS["rose"][0]), running=True, vrc="running")


def cl(entries=None, s=None, running=True, vrc="running", job=None, upd=("current", None), first_done=True, scanning=False):
    return firstrun.checklist(ents() if entries is None else entries, st() if s is None else s, running, vrc, job, upd, "0.9.57",
                              first_done, scanning)


class Base(unittest.TestCase):
    def setUp(self):
        i18n.set_lang("en")

    def tearDown(self):
        i18n.set_lang("en")


class NextRow(Base):
    def test_rows_in_order(self):
        self.assertEqual([r["key"] for r in cl()["rows"]], list(firstrun.ROW_KEYS))
        self.assertEqual([r["num"] for r in cl()["rows"]], [1, 2, 3])

    def test_no_world_or_untrusted_row(self):
        """owner 2026-11-24: world contact + 'Allow Untrusted URLs' cannot be detected -> no rows, no 'world connected'"""
        m = cl()
        self.assertEqual([r["key"] for r in m["rows"]], ["games", "vrchat", "version"])
        self.assertEqual(m["state"], "ready")
        self.assertIsNone(m["next"])
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            self.assertNotIn("Untrusted", " ".join(r["title"] + r["text"] for r in cl()["rows"]))

    def test_untrusted_is_a_static_hint(self):
        for lang, word in (("en", "Settings › Comfort & Safety › Allow Untrusted URLs"), ("de", "Einstellungen › Comfort & Safety › Allow Untrusted URLs"),
                           ("ja", "設定 › Comfort & Safety › Allow Untrusted URLs")):
            i18n.set_lang(lang)
            self.assertIn(word, cl()["hint"])
            self.assertIn("untrusted", cl()["hint"])
            self.assertIn(cl()["hint"], firstrun.checklist_texts(cl()))
        self.assertTrue(cl(vrc="missing")["hint"])                        # step screen
        self.assertEqual(cl(running=False, first_done=False)["hint"], "")  # first start: one thing only
        self.assertEqual(cl(job={"phase": "building", "pct": 5, "eta": 60, "error": ""}, first_done=False)["hint"], "")

    def test_vrchat_missing_is_next(self):
        m = cl(vrc="missing")
        self.assertEqual(m["next"], "vrchat")
        self.assertEqual(m["state"], "step")
        self.assertEqual(m["title"], i18n.tr("cl.h_step"))
        self.assertEqual(cl(vrc="unknown")["state"], "ready")              # cannot check (no log folder here): not a step

    def test_vrchat_found_claims_nothing_about_running(self):
        """a quiet log (older than 180 s) is also what an idle, running VRChat looks like: no 'not running', no extra step"""
        m = cl(vrc="found")
        self.assertEqual(m["rows"][1]["status"], "ok")
        self.assertIsNone(m["next"])
        self.assertEqual(m["state"], "ready")
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            t = cl(vrc="found")["rows"][1]["text"]
            self.assertIsNone(re.search(r"not running|läuft nicht|未起動", t), t)

    def test_start_shows_one_thing(self):
        m = cl(running=False, first_done=False)
        self.assertEqual(m["next"], "games")

    def test_games_first_when_none_found(self):
        m = cl(entries=ents(False, False))
        self.assertEqual(m["next"], "games")
        self.assertEqual(m["rows"][0]["button"], ("cl.add", "add"))
        self.assertFalse(m["primary"]["enabled"])

    def test_ready_has_no_next(self):
        m = cl(s=READY["st"])
        self.assertIsNone(m["next"])
        self.assertTrue(all(r["status"] == "ok" for r in m["rows"]))

    def test_update_available_becomes_next_when_ready(self):
        m = cl(s=READY["st"], upd=("available", "0.9.60"))
        self.assertEqual(m["next"], "version")
        self.assertEqual(m["rows"][2]["button"], ("d.install", "install"))

    def test_error_row(self):
        m = cl(job={"phase": "error", "error": "C:\\Games\\ROSE Online\\data.idx: broken"})
        self.assertEqual(m["rows"][0]["status"], "bad")
        self.assertEqual(m["next"], "games")
        self.assertEqual(m["rows"][0]["button"], ("w.help", "help_error"))
        self.assertNotIn("data.idx", " ".join(firstrun.checklist_texts(m)))     # the raw error (may hold a path) stays in the log


class Titles(Base):
    def test_start(self):
        m = cl(running=False, first_done=False)
        self.assertEqual(m["state"], "start")
        self.assertEqual(m["title"], i18n.tr("cl.h_first"))
        self.assertEqual(m["primary"]["action"], "start")
        self.assertTrue(m["primary"]["enabled"])
        self.assertEqual(m["secondary"], [])
        self.assertEqual(m["rows"][0]["status"], "warn")

    def test_build(self):
        m = cl(job={"phase": "building", "pct": 46, "eta": 180, "error": ""}, first_done=False)
        self.assertEqual(m["state"], "build")
        self.assertEqual(m["title"], i18n.tr("cl.h_build"))
        self.assertEqual(m["rows"][0]["status"], "busy")
        self.assertEqual(m["rows"][0]["progress"], 46)
        self.assertEqual(m["rows"][0]["button"], ("fr.cancel", "cancel"))
        self.assertIn("46 %", m["rows"][0]["text"])
        self.assertFalse(m["primary"]["enabled"])
        self.assertIn("3 min", m["primary"]["label"])
        self.assertIsNone(m["next"])

    def test_connected_but_building_is_not_ready(self):
        rb = {"conv": True, "install_ok": True, "packs": {"a": {"state": "fresh"}, "b": {"state": "stale"}},
              "current": {"progress": 0.5, "eta_s": 60}}
        m = cl(s=st(rose_build=rb))
        self.assertNotEqual(m["state"], "ready")

    def test_build_from_status_without_job(self):
        rb = {"conv": True, "install_ok": True, "packs": {"a": {"state": "fresh"}, "b": {"state": "stale"}},
              "current": {"progress": 0.5, "eta_s": 60}}
        m = cl(s=st(rose_build=rb))
        self.assertEqual(m["state"], "build")
        self.assertEqual(m["rows"][0]["progress"], 75)
        self.assertEqual(m["rows"][0]["button"], ("cl.add", "add"))          # no job of ours to cancel

    def test_step_and_ready(self):
        m = cl(vrc="missing")
        self.assertEqual((m["state"], m["title"]), ("step", i18n.tr("cl.h_step")))
        m = cl(s=READY["st"])
        self.assertEqual(m["state"], "ready")
        self.assertEqual(m["title"], i18n.tr("cl.h_ready"))
        self.assertEqual(cl(s=READY["st"], upd=("available", "0.9.60"))["state"], "step")

    def test_titles_all_languages(self):
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            for k in ("cl.h_first", "cl.h_build", "cl.h_step", "cl.h_ready"):
                self.assertTrue(i18n.tr(k).strip())
            self.assertNotEqual(cl(vrc="missing")["title"], cl(s=READY["st"])["title"])


class Buttons(Base):
    def test_primary_is_first_found_world(self):
        m = cl()
        self.assertEqual(m["primary"]["action"], "open:rose")
        self.assertEqual(m["primary"]["label"], "Open ROSE Online world in VRChat")
        self.assertEqual([b["action"] for b in m["secondary"]], ["open:hl", "copy:rose"])
        self.assertEqual(m["secondary"][0]["label"], "Half-Life hub")

    def test_only_hl(self):
        m = cl(entries=ents(rose=False))
        self.assertEqual(m["primary"]["action"], "open:hl")
        self.assertEqual([b["action"] for b in m["secondary"]], ["copy:hl"])

    def test_actions_resolve_to_world_links(self):
        for k in worlds.WORLD_ORDER:
            self.assertTrue(worlds.launch_url(worlds.WORLDS[k][0]).startswith("vrchat://launch"))


class PrivacyAndWords(Base):
    def models(self):
        e = ents()
        for x in e:
            if x["found"]:
                x["picked"] = True
        cases = [cl(), cl(s=READY["st"]), cl(running=False, first_done=False), cl(entries=ents(False, False)),
                 cl(job={"phase": "building", "pct": 3, "eta": -1, "error": ""}), cl(job={"phase": "error", "error": ROSE_ROOT + " failed"}),
                 cl(entries=e, upd=("available", "1.0.0")), cl(vrc="missing", upd=("failed", None)), cl(scanning=True, entries=[]),
                 cl(s=st(4, "wrld_00000000-0000-0000-0000-000000000000"))]
        return cases

    def test_no_paths_on_main_screen(self):
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            for m in self.models():
                for t in firstrun.checklist_texts(m):
                    self.assertIsNone(PATHISH.search(t), (lang, t))
                    self.assertNotIn(ROSE_ROOT, t)
                    self.assertNotIn(HL_ROOT, t)

    def test_no_forbidden_words(self):
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            for m in self.models():
                for t in firstrun.checklist_texts(m):
                    self.assertIsNone(FORBIDDEN.search(t), (lang, t))
        for k, v in i18n.CATALOG.items():
            if k.startswith("cl."):
                for t in v:
                    self.assertIsNone(FORBIDDEN.search(t), (k, t))

    def test_help_on_discord(self):
        for t in i18n.CATALOG["cl.discord"]:
            self.assertIn("Discord", t)
        for t in i18n.CATALOG["cl.games_err"]:
            self.assertIn("Discord", t)

    def test_vrchat_menu_path(self):
        for t in i18n.CATALOG["cl.hint"]:
            self.assertIn("Comfort & Safety", t)
            self.assertNotIn("Avatar", t)

    def test_cl_keys_complete(self):
        for k, v in i18n.CATALOG.items():
            if k.startswith("cl."):
                self.assertEqual(len(v), 3, k)
                self.assertTrue(all(t.strip() for t in v), k)

    def test_ui_uses_no_root_on_screen(self):
        """firstrun_ui shows e["root"] only in the chip click dialog"""
        with open(os.path.join(HERE, "firstrun_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        uses = [ln for ln in src.splitlines() if '["root"]' in ln]
        self.assertTrue(any("showinfo" in ln for ln in uses))
        self.assertFalse([ln for ln in uses if "text=" in ln], uses)


class Chips(unittest.TestCase):
    def test_all_fit(self):
        self.assertEqual(firstrun.chips_fit([100, 80], [120, 90], 690, 6, 70), 2)

    def test_rest_collapses_into_more(self):
        n = firstrun.chips_fit([100, 80], [150, 110, 200, 110], 690, 6, 70)       # the six-game case: one line + '+N more'
        self.assertEqual(n, 2)
        self.assertLessEqual(100 + 6 + 80 + 6 + 150 + 6 + 110 + 6 + 70, 690)

    def test_nothing_found(self):
        self.assertEqual(firstrun.chips_fit([], [300, 300, 300], 690, 6, 70), 2)
        self.assertEqual(firstrun.chips_fit([], [], 690, 6, 70), 0)

    def test_many_found_wrap_then_fill(self):
        self.assertEqual(firstrun.chips_fit([400, 400], [100, 300], 690, 6, 70), 1)   # second line holds 400: 100 fits, 300 does not

    def test_more_label(self):
        for lang in i18n.LANGS:
            i18n.set_lang(lang)
            self.assertIn("4", i18n.tr("cl.more", n=4))
        i18n.set_lang("en")


class Dpi(unittest.TestCase):
    def test_noop_off_windows(self):
        if sys.platform != "win32":
            self.assertFalse(firstrun_ui.dpi_aware())

    def test_called_before_tk(self):
        with open(os.path.join(HERE, "gui.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertLess(src.index("firstrun_ui.dpi_aware()"), src.index("root = tk.Tk()"))


class VRChatState(Base):
    def test_states(self):
        self.assertEqual(firstrun.vrchat_state(None), "unknown")
        self.assertEqual(firstrun.vrchat_state(None, connected=True), "running")
        self.assertEqual(firstrun.vrchat_state("x", now=1000, newest_mtime=990), "running")
        self.assertEqual(firstrun.vrchat_state("x", now=10000, newest_mtime=10), "found")
        self.assertEqual(firstrun.vrchat_state("x", now=10000, newest_mtime=-1), "missing")

    def test_missing_folder(self):
        self.assertEqual(firstrun.vrchat_state(os.path.join(HERE, "no_such_vrchat_dir")), "missing")


def _lum(hexc):
    r, g, b = (int(hexc[i:i + 2], 16) / 255.0 for i in (1, 3, 5))

    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class Contrast(unittest.TestCase):
    def test_text_contrast(self):
        C = firstrun_ui.COL
        pairs = [("text", "bg"), ("text", "panel"), ("muted", "bg"), ("muted", "panel"), ("muted", "how"), ("text", "how"),
                 ("accent", "bg"), ("ink", "ok"), ("muted", "off"), ("text", "off"), ("ok", "panel"), ("warn", "panel"), ("bad", "panel")]
        for fg, bg in pairs:
            self.assertGreaterEqual(contrast(C[fg], C[bg]), 4.5, (fg, bg, round(contrast(C[fg], C[bg]), 2)))

    def test_graphics_contrast(self):
        C = firstrun_ui.COL
        self.assertGreaterEqual(contrast(C["idle"], C["panel"]), 3.0)        # numbered dot of a waiting row


class Demo(Base):
    def test_demo_states(self):
        want = {"start": "start", "first": "build", "step": "step", "ready": "ready"}
        for name in firstrun_ui.DEMO_STATES:
            settings, e, s, run, vrc, job, upd = firstrun_ui.demo_inputs(name)
            m = firstrun.checklist(e, s, run, vrc, job, upd, "0.9.57", bool(settings.get("first_run_done")))
            self.assertEqual(m["state"], want[name], name)


if __name__ == "__main__":
    unittest.main()
