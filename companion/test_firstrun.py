"""First run: detection (synthetic folders / registry stubs), folder validation, FirstRunJob against a fake controller, the per-world
status list, vrchat:// links, worlds mirror of gsvr_worlds.json. No real game files, no Tk, no network."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import firstrun  # noqa: E402
import i18n  # noqa: E402
import rosefind  # noqa: E402
import worlds  # noqa: E402
from test_rosefind import FakeReg, make_install  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name
        self._conv = sys.modules.pop("rose_conv", None)
        sys.modules["rose_conv"] = None                      # rosefind falls back to the local data.idx check
        i18n.set_lang("en")

    def tearDown(self):
        self.tmp.cleanup()
        if self._conv is not None:
            sys.modules["rose_conv"] = self._conv
        else:
            sys.modules.pop("rose_conv", None)
        i18n.set_lang("en")


def hl_install(d):
    os.makedirs(os.path.join(d, "valve"), exist_ok=True)
    os.makedirs(os.path.join(d, "cstrike"), exist_ok=True)
    open(os.path.join(d, "hl.exe"), "wb").close()
    return d


class Detect(Base):
    def test_rose_variants_found_by_registry_and_folder_names(self):
        """SearchMMO / jROSE / iROSE style installs: registry display names, folder names, one folder deeper"""
        a = make_install(os.path.join(self.t, "SearchMMO", "ROSE Online"))
        b = make_install(os.path.join(self.t, "jROSE"))
        c = make_install(os.path.join(self.t, "iROSE-Client"))
        reg = FakeReg({("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\s"):
                       {"DisplayName": "SearchMMO ROSE", "InstallLocation": os.path.join(self.t, "SearchMMO")}})
        kw = dict(registry=reg, steam_roots=[], env={}, drives=[self.t])
        found = rosefind.find(**kw)
        self.assertIn(os.path.normpath(a), found)                 # parent registered, game one folder deeper
        self.assertIn(os.path.normpath(b), found)
        self.assertIn(os.path.normpath(c), found)

    def test_searchmmo_name_without_rose(self):
        d = make_install(os.path.join(self.t, "SearchMMO"))
        found = rosefind.find(registry=FakeReg({}), steam_roots=[], env={}, drives=[self.t])
        self.assertEqual(found, [os.path.normpath(d)])

    def test_scan_bounded_and_no_drives_off_windows(self):
        self.assertEqual(rosefind.find(registry=FakeReg({}), steam_roots=[], env={}, drives=[]), [])
        clock = [0.0]
        make_install(os.path.join(self.t, "a", "b", "ROSE"))
        res = rosefind.candidates(registry=FakeReg({}), steam_roots=[], env={}, drives=[self.t], budget=1.0, now=lambda: (clock.__setitem__(0, clock[0] + 2) or clock[0]))
        self.assertLessEqual(len(res), 1)                         # the deadline passes at once: the walk gives up

    def test_detect_entries(self):
        hl = hl_install(os.path.join(self.t, "Half-Life"))
        rose = make_install(os.path.join(self.t, "ROSE"))
        lv = lambda hl_root, w3, maps, scan: [      # noqa: E731
            {"id": "valve", "found": True, "root": hl, "source": "steam-manifest"},
            {"id": "cstrike", "found": False, "root": "", "source": "none"}]
        ents = firstrun.detect({}, local_view=lv, rose_find=lambda cfg: [(rose, "registry", "ok")])
        by = {e["id"]: e for e in ents}
        self.assertEqual([e["id"] for e in ents], list(firstrun.ORDER))
        self.assertTrue(by["valve"]["found"] and by["valve"]["root"] == hl and by["valve"]["world"] == "hl")
        self.assertFalse(by["cstrike"]["found"])
        self.assertTrue(by["rose"]["found"] and by["rose"]["how"] == "registry" and by["rose"]["world"] == "rose")
        self.assertEqual(by["gearbox"]["world"], "hl")
        self.assertEqual(firstrun.how_text("registry"), "Registry")

    def test_detect_survives_failures(self):
        def boom(*a):
            raise RuntimeError("x")
        ents = firstrun.detect({}, local_view=boom, rose_find=boom)
        self.assertTrue(all(not e["found"] for e in ents))


class Validate(Base):
    def test_rose_folder(self):
        d = make_install(os.path.join(self.t, "r"))
        self.assertEqual(firstrun.validate_folder("rose", d), (True, d))
        parent = os.path.join(self.t, "p")
        child = make_install(os.path.join(parent, "game"))
        ok, got = firstrun.validate_folder("rose", parent)
        self.assertTrue(ok)
        self.assertEqual(os.path.normpath(got), os.path.normpath(child))
        ok, msg = firstrun.validate_folder("rose", os.path.join(self.t, "none"))
        self.assertFalse(ok)
        self.assertIn("data.idx", msg)

    def test_goldsrc_folder(self):
        hl = hl_install(os.path.join(self.t, "hl"))
        self.assertEqual(firstrun.validate_folder("valve", hl), (True, hl))
        ok, msg = firstrun.validate_folder("valve", self.t)
        self.assertFalse(ok)
        self.assertIn("hl.exe", msg)

    def test_messages_in_each_language(self):
        for lang, needle in (("en", ""), ("de", ""), ("ja", "")):
            i18n.set_lang(lang)
            if needle:
                self.assertIn(needle, firstrun.not_found_text("rose"))
            self.assertIn("Discord", firstrun.not_found_text("rose"))
            self.assertIn("data.idx", firstrun.not_found_text("rose"))
            self.assertIn("hl.exe", firstrun.not_found_text("valve"))

    def test_apply_choices(self):
        s = {}
        ch = firstrun.apply_choices(s, [{"id": "rose", "found": True, "root": "R", "picked": True},
                                        {"id": "valve", "found": True, "root": "H", "picked": False},
                                        {"id": "cstrike", "found": True, "root": "C", "picked": True}])
        self.assertEqual(ch, {"rose_install": "R", "hl_root": "C"})
        self.assertEqual(s["rose_install"], "R")


class Progress(Base):
    def packs(self, fresh, total):
        return {"p%d" % i: {"state": "fresh" if i < fresh else "stale"} for i in range(total)}

    def test_rose_progress(self):
        rb = {"conv": "v", "install_ok": True, "packs": self.packs(1, 4), "current": {"progress": 0.5, "eta_s": 30}}
        pct, eta, done, err = firstrun.rose_progress(rb)
        self.assertEqual((pct, done, err), (37, False, ""))
        self.assertGreaterEqual(eta, 30)
        self.assertEqual(firstrun.rose_progress(dict(rb, packs=self.packs(4, 4), current=None))[:3], (100, 0, True))
        self.assertEqual(firstrun.rose_progress(None)[:3], (0, -1, False))
        self.assertTrue(firstrun.rose_progress({"conv": None, "install_ok": True, "packs": {"a": {}}})[3])

    def test_error_text(self):
        rb = {"conv": "v", "install_ok": True, "packs": {"a": {"state": "stale", "error": "boom"}}}
        self.assertEqual(firstrun.rose_progress(rb)[3], "boom")


class FakeController:
    def __init__(self, statuses, running=True):
        self.settings = {}
        self.saved = 0
        self.statuses = list(statuses)
        self._running = running
        self.active_port = 1
        self.fatal = None
        self.posts = []
        self.started = 0

    def save_settings(self):
        self.saved += 1

    def running(self):
        return self._running

    def start(self):
        self.started += 1
        self._running = True

    def log(self, *_):
        pass

    def status(self):
        return self.statuses.pop(0) if self.statuses else None

    def rose_post(self, path):
        self.posts.append(path)
        return {"ok": True}


class Job(Base):
    def entries(self):
        return [{"id": "rose", "name": "ROSE Online", "found": True, "root": "/x/ROSE", "world": "rose", "picked": True},
                {"id": "valve", "name": "Half-Life", "found": True, "root": "/x/HL", "world": "hl", "picked": False},
                {"id": "cstrike", "name": "CS", "found": False, "root": "", "world": "cs", "picked": False}]

    def rb(self, fresh, total=4, cur=None):
        return {"conv": "v", "install_ok": True, "current": cur, "queue": [],
                "packs": {"p%d" % i: {"state": "fresh" if i < fresh else "stale"} for i in range(total)}}

    def test_flow_to_done(self):
        t = [0.0]
        c = FakeController([])
        job = firstrun.FirstRunJob(c, self.entries(), now=lambda: t[0])
        self.assertEqual([e["id"] for e in job.entries], ["rose", "valve"])           # not found = not selected
        job.begin()
        self.assertEqual(c.saved, 1)
        self.assertEqual(c.settings["rose_install"], "/x/ROSE")
        self.assertEqual(job.step(None), "starting")                                  # no status yet
        t[0] = 10
        st = {"rose_build": self.rb(1, cur={"progress": 0.5, "eta_s": 20}), "build": [{"mod": "valve", "kind": "models"}]}
        self.assertEqual(job.step(st), "building")
        self.assertEqual(c.posts[0].split("?")[0], "/rose/install")
        self.assertIn("/rose/build_all", c.posts)
        self.assertGreater(job.eta, 0)
        self.assertEqual(len(job.rows), 2)
        n = len(c.posts)
        job.step(st)
        self.assertEqual(len(c.posts), n)                                             # install/build_all sent once
        t[0] = 30
        self.assertEqual(job.step({"rose_build": self.rb(4), "build": []}), "done")
        self.assertEqual(job.pct, 100)
        self.assertEqual(job.worlds, ["rose", "hl"])

    def test_title_follows_phase(self):
        self.assertEqual(firstrun.title_key("starting"), "fr.starting_helper")
        for ph in ("building", "done"):
            self.assertEqual(firstrun.title_key(ph), "fr.building")
        for k in ("fr.rose_early_hint", "fr.background", "fr.background_hint"):
            for lg in i18n.LANGS:
                i18n.set_lang(lg)
                self.assertTrue(i18n.tr(k))
        i18n.set_lang("en")

    def test_per_game_readiness(self):
        t = [0.0]
        job = firstrun.FirstRunJob(FakeController([]), self.entries(), now=lambda: t[0])
        job.begin()
        job.step({"rose_build": self.rb(1, cur={"progress": 0.5, "eta_s": 20}), "build": []})     # grace period: valve still "busy"
        g = {r["id"]: r for r in job.game_rows}
        self.assertFalse(g["rose"]["ready"])
        self.assertTrue(g["rose"]["can_open"])                       # ROSE opens while building, with a hint
        self.assertTrue(g["rose"]["hint"])
        self.assertFalse(g["valve"]["can_open"])
        t[0] = 30
        job.step({"rose_build": self.rb(1, cur={"progress": 0.5, "eta_s": 20}), "build": []})
        g = {r["id"]: r for r in job.game_rows}
        self.assertTrue(g["valve"]["ready"] and g["valve"]["can_open"] and g["valve"]["world"] == "hl")
        self.assertEqual(job.phase, "building")                      # ROSE still running: the job is not done
        self.assertGreater(g["rose"]["pct"], 0)

    def test_worlds_tab_shows_rose_building(self):
        ents = [{"id": "rose", "found": True, "root": "/x"}]
        st = {"rose_build": self.rb(1, cur={"progress": 0.5, "eta_s": 20})}
        row = next(r for r in firstrun.world_rows(ents, st, True) if r["key"] == "rose")
        self.assertEqual(row["light"], "yellow")
        self.assertTrue(row["can_open"])

    def test_main_window_keeps_all_old_tabs(self):
        src = open(os.path.join(HERE, "gui.py"), encoding="utf-8").read()
        order = [src.index(k) for k in ('scrollable(tr("tab.worlds")', 'scrollable(tr("tab.status")', 'scrollable(tr("tab.games")',
                                        'text=tr("tab.log")', 'scrollable(tr("tab.settings")')]
        self.assertEqual(order, sorted(order))                       # Worlds first, then every old tab
        self.assertIn("first_run_done", src)
        self.assertEqual(src.count("firstrun.start_view(c.settings, c.fresh_install)"), 1)

    def test_cancel(self):
        c = FakeController([])
        job = firstrun.FirstRunJob(c, self.entries())
        job.step({"rose_build": self.rb(0)})
        job.cancel()
        self.assertEqual(job.phase, "cancelled")
        self.assertIn("/rose/cancel?scope=batch", c.posts)
        self.assertEqual(job.step({"rose_build": self.rb(4)}), "cancelled")

    def test_helper_does_not_start(self):
        t = [0.0]
        c = FakeController([], running=False)
        c.fatal = "no free port\nmore"
        job = firstrun.FirstRunJob(c, self.entries(), now=lambda: t[0])
        job.step(None)
        self.assertEqual(job.phase, "error")
        self.assertIn("no free port", job.error)
        c2 = FakeController([])
        job2 = firstrun.FirstRunJob(c2, self.entries(), now=lambda: t[0])
        t[0] = 100
        job2.step(None)
        self.assertEqual(job2.phase, "error")

    def test_no_games(self):
        job = firstrun.FirstRunJob(FakeController([]), [])
        self.assertEqual(job.step({"build": []}), "error")


class WorldList(Base):
    def ents(self, **found):
        return [{"id": g, "found": found.get(g, False), "root": "x" if found.get(g) else ""} for g in firstrun.ORDER]

    def test_rows(self):
        e = self.ents(rose=True, valve=True)
        rb = {"conv": "v", "install_ok": True, "current": None, "packs": {"a": {"state": "fresh"}}}
        rows = {r["key"]: r for r in firstrun.world_rows(e, {"rose_build": rb, "build": []}, True)}
        self.assertEqual(rows["rose"]["light"], "green")
        self.assertTrue(rows["rose"]["can_open"])
        self.assertEqual(rows["hl"]["light"], "green")
        self.assertEqual(rows["cs"]["light"], "red")
        self.assertEqual(rows["cs"]["state"], "game not found")
        self.assertEqual(rows["cs"]["help"], "help.nf.goldsrc")
        self.assertIn("hl.exe", rows["cs"]["hint"])
        self.assertFalse(rows["cs"]["can_open"])
        self.assertTrue(rows["rose"]["url"].startswith("vrchat://launch?"))

    def test_states(self):
        e = self.ents(rose=True, valve=True)
        rb = {"conv": "v", "install_ok": True, "current": {"name": "z", "progress": 0.64},
              "packs": {"a": {"state": "fresh"}, "b": {"state": "stale"}}}
        r = {x["key"]: x for x in firstrun.world_rows(e, {"rose_build": rb, "build": [{"mod": "valve", "pct": 40}]}, True)}
        self.assertEqual(r["rose"]["light"], "yellow")
        self.assertIn("(1/2)", r["rose"]["state"])
        self.assertEqual(r["hl"]["state"], "building 40 %")
        rb2 = dict(rb, current=None)
        r = {x["key"]: x for x in firstrun.world_rows(e, {"rose_build": rb2, "build": []}, True)}
        self.assertEqual(r["rose"]["state"], "update needed")
        self.assertEqual(r["rose"]["help"], "help.update")
        r = {x["key"]: x for x in firstrun.world_rows(e, None, False)}
        self.assertEqual(r["rose"]["light"], "grey")
        self.assertEqual(r["rose"]["help"], "help.stopped")
        rb3 = {"conv": "v", "install_ok": True, "current": None, "packs": {"a": {"state": "stale", "error": "x"}}}
        r = {x["key"]: x for x in firstrun.world_rows(e, {"rose_build": rb3}, True)}
        self.assertEqual((r["rose"]["light"], r["rose"]["help"]), ("red", "help.error"))

    def test_japanese_rows(self):
        i18n.set_lang("ja")
        r = {x["key"]: x for x in firstrun.world_rows(self.ents(), None, False)}
        self.assertEqual(r["rose"]["state"], "ゲームが見つかりません")
        self.assertIn("data.idx", r["rose"]["hint"])

    def test_help_has_discord_text_in_all_languages(self):
        for lang in i18n.LANGS:
            self.assertIn(worlds.DISCORD_URL, i18n.tr_in(lang, "help.discord", url=worlds.DISCORD_URL))
            for k in ("help.nf.goldsrc", "help.nf.rose", "help.nf.war3", "help.update", "help.stopped", "help.error", "help.helper"):
                self.assertTrue(i18n.tr_in(lang, k))


class Worlds(Base):
    def test_launch_url(self):
        u = worlds.launch_url(worlds.WORLDS["rose"][0])
        self.assertEqual(u, "vrchat://launch?ref=vrchat.com&id=wrld_3a03fa73-b6c8-4116-8785-40fbe353b9b2")
        for bad in ("", "wrld_x", "http://x", "wrld_3a03fa73-b6c8-4116-8785-40fbe353b9b2&x=1"):
            with self.assertRaises(ValueError):
                worlds.launch_url(bad)
        self.assertFalse(worlds.open_link("file:///etc/passwd"))

    def test_mirror_of_world_json(self):
        p = os.path.join(os.path.dirname(HERE), "unity", "Assets", "GoldSrcVR", "Editor", "gsvr_worlds.json")
        if not os.path.isfile(p):
            self.skipTest("no repo checkout (frozen / source zip)")
        with open(p, encoding="utf-8") as fh:
            ids = {w["worldId"]: w["name"] for w in json.load(fh)["worlds"]}
        for k, (wid, name, _g) in worlds.WORLDS.items():
            if k == "war3":                                  # W3VR lives in its own unity project; the id is referenced in GsvrWorldStats.cs
                continue
            self.assertEqual(ids.get(wid), name, k)
        stats = os.path.join(os.path.dirname(p), "GsvrWorldStats.cs")
        if os.path.isfile(stats):
            with open(stats, encoding="utf-8") as fh:
                self.assertIn(worlds.WORLDS["war3"][0], fh.read())

    def test_game_mapping(self):
        self.assertEqual(worlds.world_for_game("gearbox"), "hl")
        self.assertEqual(worlds.world_for_game("svencoop"), "hl")                    # Sven is part of the hub, no own world row
        self.assertNotIn("sven", worlds.WORLD_ORDER)
        self.assertEqual(worlds.world_for_game("war3"), "war3")
        self.assertEqual(worlds.WORLD_ORDER[-1], "war3")
        self.assertTrue(worlds.launch_url(worlds.WORLDS["war3"][0]).startswith("vrchat://launch"))




class CancelRace(Base):
    def entries(self):
        return [{"id": "rose", "name": "ROSE", "found": True, "root": "/r", "world": "rose"}]

    def test_cancel_during_status_call_posts_nothing(self):
        job_box = []

        class C(FakeController):
            def status(self):
                job_box[0].cancel()                    # other thread cancels while step() is blocked in status()
                return {"rose_build": {"conv": True, "install_ok": True, "packs": {"a": {"state": "stale"}}}, "build": []}

        c = C([])
        job = firstrun.FirstRunJob(c, self.entries())
        job_box.append(job)
        self.assertEqual(job.step(), "cancelled")
        self.assertEqual(job.phase, "cancelled")
        self.assertFalse([p for p in c.posts if "install" in p or "build_all" in p])
        self.assertEqual(job.step({"rose_build": {}}), "cancelled")

    def test_cancel_posts_batch_scope_and_owns_phase(self):
        c = FakeController([])
        job = firstrun.FirstRunJob(c, self.entries())
        job.cancel()
        self.assertEqual(c.posts, ["/rose/cancel?scope=batch"])
        job.cancel()
        self.assertEqual(len(c.posts), 1)

    def test_ui_cancel_does_not_preset_phase(self):
        import inspect
        import firstrun_ui
        src = inspect.getsource(firstrun_ui.FirstRun.cancel)
        self.assertNotIn("phase =", src)


class LangAndBatchCancel(Base):
    def test_gui_error_text_translated(self):
        import gui
        i18n.set_lang("de")
        self.assertEqual(gui.rose_live_text({"state": "error", "text": "x"}), "Fehler: x")

    def test_child_env_carries_lang(self):
        import gui
        i18n.set_lang("ja")
        self.assertEqual(gui.Controller.child_env(object.__new__(gui.Controller))["GSVR_LANG"], "ja")

    def test_companion_main_sets_lang_from_env(self):
        import companion
        import inspect
        self.assertIn("GSVR_LANG", inspect.getsource(companion.main))

    def test_batch_cancel_keeps_demand(self):
        import threading
        import rosebuild
        m = object.__new__(rosebuild.Manager)
        m.cv = threading.Condition()
        ev_d, ev_b = threading.Event(), threading.Event()
        m.running = [{"name": "D", "kind": "demand", "cancel": ev_d, "requeue": True},
                     {"name": "B", "kind": "background", "cancel": ev_b, "requeue": True}]
        m.demand, m.queue, m.forced = "D2", ["Q"], {"Q"}
        notes = []
        m._notify = lambda n, f: notes.append(n)
        m.cancel(batch_only=True)
        self.assertFalse(ev_d.is_set())
        self.assertTrue(ev_b.is_set())
        self.assertEqual((m.demand, m.queue), ("D2", []))
        self.assertEqual(notes, [])


class Views(Base):
    def test_start_view(self):
        sv = firstrun.start_view
        self.assertEqual(sv({}, True), "firstrun")
        self.assertEqual(sv({"first_run_done": False}, False), "simple")      # owner 2026-11-24: ALWAYS the simple view
        self.assertEqual(sv({"first_run_done": True}, False), "simple")
        self.assertEqual(sv({"first_run_done": True, "view": "simple"}, False), "simple")
        self.assertEqual(sv({"first_run_done": True, "view": "advanced"}, False), "simple")   # a stored Advanced view is ignored
        self.assertEqual(sv({"first_run_done": False, "view": "advanced"}, False), "simple")
        self.assertEqual(sv({"first_run_done": True}, True), "simple")

    def test_view_default_in_settings_and_texts(self):
        import gui
        self.assertEqual(gui.DEFAULT_SETTINGS["view"], "simple")
        for lang in i18n.LANGS:
            for k in ("fr.open_app", "fr.easy", "fr.redo", "w.name.war3"):
                self.assertTrue(i18n.tr_in(lang, k), (lang, k))
        self.assertEqual(i18n.tr_in("en", "fr.open_app"), "Advanced view")
        self.assertEqual(i18n.tr_in("de", "fr.open_app"), "Erweiterte Ansicht")
        self.assertEqual(i18n.tr_in("de", "fr.easy"), "Einfache Ansicht")

    def test_war3_world_rows(self):
        ents = [{"id": g, "found": False, "root": ""} for g in firstrun.ORDER]
        rows = {r["key"]: r for r in firstrun.world_rows(ents, None, False)}
        self.assertEqual(rows["war3"]["light"], "red")
        self.assertEqual(rows["war3"]["help"], "help.nf.war3")
        self.assertFalse(rows["war3"]["can_open"])
        for e in ents:
            if e["id"] == "war3":
                e.update(found=True, root="x")
        st = {"build": []}
        r = {r["key"]: r for r in firstrun.world_rows(ents, st, True)}["war3"]
        self.assertEqual((r["light"], r["can_open"]), ("green", True))
        st = {"build": [{"kind": "war3", "mod": "war3", "pct": 40}]}
        r = {r["key"]: r for r in firstrun.world_rows(ents, st, True)}["war3"]
        self.assertEqual((r["light"], r["can_open"]), ("yellow", True))
        self.assertEqual(firstrun.apply_choices({}, [{"id": "war3", "found": True, "root": "D:/W3", "picked": True}]), {"war3_root": "D:/W3"})


class RoInNewDesign(Base):
    """ro-main merge (2026-11-25): the RO world in the redesigned simple view + Worlds tab"""

    def ents(self, **found):
        return [{"id": g, "name": firstrun.game_name(g), "found": found.get(g, False), "root": "x" if found.get(g) else "",
                 "how": "registry", "world": worlds.world_for_game(g), "picked": False} for g in firstrun.ORDER]

    def test_simple_view_with_rose_and_ro(self):
        """ROSE + RO found, setup done: the checklist offers the RO world as a second button (was a KeyError = no simple view)"""
        fresh = {"conv": "v", "install_ok": True, "current": None, "packs": {"a": {"state": "fresh"}}}
        st = {"world_ok": True, "build": [], "rose_build": fresh, "ro_build": fresh}
        m = firstrun.checklist(self.ents(rose=True, ro=True), st, True, "running", None, ("", None), "0.9.60", True)
        self.assertEqual(m["primary"]["action"], "open:rose")
        self.assertIn({"label": "Ragnarok Online", "action": "open:ro"}, m["secondary"])
        m = firstrun.checklist(self.ents(ro=True), st, True, "running", None, ("", None), "0.9.60", True)
        self.assertEqual(m["primary"]["action"], "open:ro")
        self.assertIn("Ragnarok Online", m["primary"]["label"])

    def test_ro_world_row_follows_ro_build(self):
        """the RO row is green only with every RO pack built (ro_build), not because the ROSE packs are"""
        fresh = {"conv": "v", "install_ok": True, "current": None, "packs": {"a": {"state": "fresh"}}}
        half = {"conv": "v", "install_ok": True, "current": {"progress": 0.5}, "queue": ["b"],
                "packs": {"a": {"state": "fresh"}, "b": {"state": "missing"}}}
        e = self.ents(rose=True, ro=True)
        rows = {r["key"]: r for r in firstrun.world_rows(e, {"rose_build": fresh, "ro_build": half, "build": []}, True)}
        self.assertEqual(rows["rose"]["light"], "green")
        self.assertEqual(rows["ro"]["light"], "yellow")
        self.assertEqual(rows["ro"]["state"], "building (1/2)")
        self.assertEqual(rows["ro"]["name"], "Ragnarok Online")
        rows = {r["key"]: r for r in firstrun.world_rows(e, {"rose_build": half, "ro_build": fresh, "build": []}, True)}
        self.assertEqual(rows["ro"]["light"], "green")
        self.assertEqual(rows["rose"]["light"], "yellow")
        rows = {r["key"]: r for r in firstrun.world_rows(e, {"rose_build": fresh, "ro_build": dict(fresh, conv=None), "build": []}, True)}
        self.assertEqual(rows["ro"]["light"], "red")
        self.assertIn("Ragnarok", rows["ro"]["hint"])


class StaleRows(unittest.TestCase):
    """w41: stale packs rebuild by themselves; no row ever asks to press Start while the helper runs"""
    def setUp(self):
        i18n.set_lang("de")

    def tearDown(self):
        i18n.set_lang("en")

    def rows(self, ro_build):
        e = [{"id": "ro", "found": True, "root": "x"}]
        return {x["key"]: x for x in firstrun.world_rows(e, {"ro_build": ro_build, "rose_build": None, "build": []}, True)}

    def test_label_no_private_test(self):
        for lang in ("en", "de", "ja"):
            i18n.set_lang(lang)
            self.assertNotIn("test", firstrun.tr("w.name.ro").lower().replace("ragnarok", ""))
            self.assertNotIn("テスト", firstrun.tr("w.name.ro"))
        self.assertEqual(firstrun.tr("w.name.ro"), "Ragnarok Online")

    def test_stale_idle_has_rebuild_and_no_press_start(self):
        rb = {"conv": "v", "install_ok": True, "current": None, "queue": [], "packs": {"a": {"state": "fresh"}, "b": {"state": "stale"}}}
        r = self.rows(rb)["ro"]
        self.assertTrue(r["can_rebuild"])
        self.assertEqual(r["rebuild"], "/ro/build_all")
        for lang in ("en", "de", "ja"):
            i18n.set_lang(lang)
            r = self.rows(rb)["ro"]
            self.assertNotIn("Start", r["hint"].replace("Neu bauen", ""))
            self.assertNotIn("スタート", r["hint"])
            self.assertNotIn("Start", firstrun.tr("help.update"))
            self.assertNotIn("スタート", firstrun.tr("help.update"))
        i18n.set_lang("de")
        self.assertEqual(firstrun.tr("w.rebuild"), "Neu bauen")

    def test_building_shows_x_of_n(self):
        packs = {"p%d" % i: {"state": "fresh" if i < 5 else "stale"} for i in range(43)}
        rb = {"conv": "v", "install_ok": True, "current": {"name": "p5", "progress": 0.5}, "queue": ["p6"], "packs": packs}
        r = self.rows(rb)["ro"]
        self.assertEqual(r["state"], "wird gebaut (5/43)")
        self.assertFalse(r["can_rebuild"])
        done = {"conv": "v", "install_ok": True, "current": None, "queue": [], "packs": {k: {"state": "fresh"} for k in packs}}
        r = self.rows(done)["ro"]
        self.assertEqual((r["light"], r["can_rebuild"]), ("green", False))


if __name__ == "__main__":
    unittest.main()
