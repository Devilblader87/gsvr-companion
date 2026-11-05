"""HL-DETECT: Steam via registry / fixed drives, non-Steam GoldSrc scan, game-root validation, discovery report.

Run: python3 -I companion/test_hl_detect.py   (also part of companion/selftest.py)
winreg and the drive list are replaced (steam._registry, steam.fixed_drives) so this runs on macOS / Linux.
"""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import games, steam, war3                              # noqa: E402


class FakeRegistry:
    def __init__(self, values=None, subkeys=None):
        self.values = values or {}                     # (hive, key, value) -> str
        self.sub = subkeys or {}                       # (hive, key) -> [names]

    def read(self, hive, key, value):
        return self.values.get((hive, key, value))

    def subkeys(self, hive, key):
        return list(self.sub.get((hive, key), []))


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def goldsrc(root, mods=("valve", "cstrike"), exe="hl.exe"):
    touch(os.path.join(root, exe))
    for m in mods:
        os.makedirs(os.path.join(root, m, "maps"), exist_ok=True)
        touch(os.path.join(root, m, "maps", "x.bsp"), "b")
    return root


def steam_lib(root, installdir="Counter-Strike", appid=10, flags=4):
    os.makedirs(os.path.join(root, "steamapps", "common", installdir, "cstrike"), exist_ok=True)
    touch(os.path.join(root, "steamapps", "appmanifest_%d.acf" % appid),
          '"AppState"\n{\n "appid" "%d"\n "StateFlags" "%d"\n "installdir" "%s"\n}\n' % (appid, flags, installdir))
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hldetect")
        self._reg, self._drv = steam._registry, steam.fixed_drives
        steam._registry = FakeRegistry()
        steam.fixed_drives = lambda: []
        steam._scan_cache.clear()

    def tearDown(self):
        steam._registry, steam.fixed_drives = self._reg, self._drv
        steam._scan_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *a):
        return os.path.join(self.tmp, *a)


class SteamDiscovery(Base):
    def test_registry_steampath_and_exe(self):
        lib = steam_lib(self.p("Weird Place", "Steam"))
        steam._registry = FakeRegistry({("HKCU", r"Software\Valve\Steam", "SteamPath"): lib.replace("\\", "/")})
        self.assertEqual(steam.find_steam_roots(), [os.path.normpath(lib)])
        steam._registry = FakeRegistry({("HKCU", r"Software\Valve\Steam", "SteamExe"): os.path.join(lib, "steam.exe")})
        self.assertEqual(steam.find_steam_root(), os.path.normpath(lib))
        steam._registry = FakeRegistry({("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"): lib})
        self.assertEqual(steam.find_steam_root(), os.path.normpath(lib))
        steam._registry = FakeRegistry({("HKLM", r"SOFTWARE\Valve\Steam", "InstallPath"): lib})
        self.assertEqual(steam.find_steam_root(), os.path.normpath(lib))

    def test_fixed_drive_default_paths(self):
        for sub in ("Games/Steam", "SteamLibrary", "Program Files (x86)/Steam"):
            shutil.rmtree(self.tmp)
            os.makedirs(self.tmp)
            lib = steam_lib(self.p(*sub.split("/")))
            steam.fixed_drives = lambda: [self.tmp]
            self.assertEqual(steam.find_steam_roots(), [os.path.normpath(lib)], sub)

    def test_non_default_library_and_ownership(self):
        main = steam_lib(self.p("S"), installdir="Half-Life", appid=70)
        lib2 = steam_lib(self.p("Other Lib"))
        touch(os.path.join(main, "steamapps", "libraryfolders.vdf"),
              '"libraryfolders"\n{\n "0"\n {\n  "path" "%s"\n }\n "1"\n {\n  "path" "%s"\n }\n}\n'
              % (main.replace("\\", "\\\\"), lib2.replace("\\", "\\\\")))
        os.makedirs(os.path.join(main, "steamapps", "common", "Half-Life", "valve"))
        own = steam.ownership_from_steam(main)
        self.assertTrue(own["cstrike"]["allowed"])
        self.assertTrue(own["valve"]["allowed"])
        self.assertEqual(own["cstrike"]["source"], "steam")

    def test_report_lists_tried_places_and_manifests(self):
        lib = steam_lib(self.p("Steam"))
        steam._registry = FakeRegistry({("HKCU", r"Software\Valve\Steam", "SteamPath"): lib})
        txt = "\n".join(steam.discovery_report())
        self.assertIn("FOUND", txt)
        self.assertIn("registry HKCU", txt)
        self.assertIn("appmanifest_10.acf", txt)

    def test_report_no_steam(self):
        self.assertIn("no Steam installation", "\n".join(steam.discovery_report()))


class NonSteam(Base):
    def test_found_in_typical_places_and_names(self):
        steam.fixed_drives = lambda: [self.tmp]
        goldsrc(self.p("Counter-Strike 1.6"))
        goldsrc(self.p("Games", "Half-Life"), mods=("valve",))
        goldsrc(self.p("Program Files (x86)", "Sierra", "Half-Life"), mods=("valve", "gearbox"), exe="HL.EXE")
        goldsrc(self.p("Spiele", "hlds"), mods=("valve", "cstrike"), exe="hlds.exe")
        goldsrc(self.p("Random", "Stuff", "Half-Life"))             # drive-root dir without a hint name: not scanned
        got = {os.path.relpath(c["root"], self.tmp): c for c in steam.detect_non_steam()}
        self.assertEqual(sorted(got), sorted(["Counter-Strike 1.6", os.path.join("Games", "Half-Life"),
                                              os.path.join("Program Files (x86)", "Sierra", "Half-Life"),
                                              os.path.join("Spiele", "hlds")]))
        c = got["Counter-Strike 1.6"]
        self.assertEqual(c["source"], "detected-non-steam")
        self.assertEqual(c["mods"], ["cstrike", "valve"])

    def test_needs_valve_folder_and_exe(self):
        steam.fixed_drives = lambda: [self.tmp]
        touch(self.p("Games", "A", "hl.exe"))                       # no valve/
        os.makedirs(self.p("Games", "B", "valve"))                  # no exe
        self.assertEqual(steam.detect_non_steam(), [])

    def test_skips_steam_library(self):
        steam.fixed_drives = lambda: [self.tmp]
        goldsrc(self.p("Games", "Steam", "steamapps", "common", "Half-Life"), mods=("valve",))
        self.assertEqual(steam.detect_non_steam(), [])

    def test_depth_limit(self):
        steam.fixed_drives = lambda: [self.tmp]
        goldsrc(self.p("Games", "a", "b", "c", "Half-Life"))        # Games(1) a(2) b(3) c(4) HL(5): too deep
        self.assertEqual(steam.detect_non_steam(), [])
        steam._scan_cache.clear()
        goldsrc(self.p("Games", "a", "Half-Life"))                  # depth 3 below the drive
        self.assertEqual(len(steam.detect_non_steam()), 1)
        steam._scan_cache.clear()
        self.assertEqual(len(steam.detect_non_steam(max_depth=6)), 2)

    def test_time_budget(self):
        steam.fixed_drives = lambda: [self.tmp]
        for i in range(30):
            os.makedirs(self.p("Games", "d%02d" % i))
        goldsrc(self.p("Games", "zz", "Half-Life"))
        t = [0.0]

        def clock():                                                 # every look at the clock costs 1 s
            t[0] += 1.0
            return t[0]
        got = steam.detect_non_steam(budget=3.0, force=True, now=clock)
        self.assertEqual(got, [])                                    # budget ran out before reaching "zz"
        self.assertLess(t[0], 20)                                    # and the scan stopped early
        steam._scan_cache.clear()
        self.assertEqual(len(steam.detect_non_steam(budget=60.0)), 1)

    def test_cache(self):
        steam.fixed_drives = lambda: [self.tmp]
        goldsrc(self.p("Games", "CS"))
        self.assertEqual(len(steam.detect_non_steam()), 1)
        shutil.rmtree(self.p("Games", "CS"))
        self.assertEqual(len(steam.detect_non_steam()), 1)           # cached
        self.assertEqual(steam.detect_non_steam(force=True), [])

    @unittest.skipIf(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), "chmod 0 not enforced")
    def test_unreadable_dir_is_skipped(self):
        steam.fixed_drives = lambda: [self.tmp]
        goldsrc(self.p("Games", "ok"))
        os.makedirs(self.p("Games", "locked"))
        os.chmod(self.p("Games", "locked"), 0)
        try:
            self.assertEqual(len(steam.detect_non_steam()), 1)
        finally:
            os.chmod(self.p("Games", "locked"), 0o755)

    def test_registry_uninstall_entries(self):
        a = goldsrc(self.p("Elsewhere", "CS16"))
        b = goldsrc(self.p("Else2", "HL"), mods=("valve",))
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        steam._registry = FakeRegistry(
            {("HKLM", base + "\\CS", "InstallLocation"): a,
             ("HKCU", base + "\\HL", "DisplayIcon"): '"%s,0"' % os.path.join(b, "hl.exe"),
             ("HKLM", base + "\\Junk", "InstallLocation"): self.p("nothing")},
            {("HKLM", base): ["CS", "Junk"], ("HKCU", base): ["HL"]})
        found = steam.detect_non_steam()
        self.assertEqual(sorted(c["root"] for c in found), sorted([os.path.normpath(a), os.path.normpath(b)]))
        self.assertTrue(all(c["via"] == "registry uninstall entry" for c in found))

    def test_hint_line_when_no_cstrike(self):
        lines = steam.non_steam_hint_lines(False)
        self.assertTrue(any("Half-Life / Counter-Strike Ordner" in l for l in lines))
        self.assertEqual(steam.non_steam_hint_lines(True), [])


class GameRoot(Base):
    def test_describe_root(self):
        r = goldsrc(self.p("CS"), mods=("valve", "cstrike"))
        touch(os.path.join(r, "mymod", "liblist.gam"), "game")
        d = steam.describe_root(r)
        self.assertTrue(d["valid"])
        self.assertEqual(d["mods"], ["cstrike", "mymod", "valve"])
        self.assertIn("cstrike", d["reason"])
        self.assertFalse(steam.describe_root(self.p("nope"))["valid"])
        os.makedirs(self.p("empty"))
        self.assertFalse(steam.describe_root(self.p("empty"))["valid"])

    def test_manual_ownership_and_report(self):
        r = goldsrc(self.p("My Games", "CS 1.6"), mods=("cstrike",))
        own = steam.ownership_manual(r)
        self.assertTrue(own["cstrike"]["allowed"])
        self.assertFalse(own["valve"]["allowed"])
        self.assertEqual(own["cstrike"]["source"], "manual-override")
        self.assertIn("cstrike", "\n".join(steam.discovery_report(None, r)))
        idx = steam.build_index(own)
        self.assertEqual(list(idx["cstrike"]["maps"]), ["x"])


class GamesSectionTests(Base):
    """GAME-FOLDERS: status.json "games" (games.games_section) from fake Steam / manual / non-Steam / WC3 trees."""

    def by_id(self, gs):
        return {g["id"]: g for g in gs}

    def test_steam_manifest_and_not_found(self):
        lib = steam_lib(self.p("Steam"), "Counter-Strike", 10)
        own = steam.ownership_from_steam(lib)
        g = self.by_id(games.games_section(own))
        self.assertTrue(g["cstrike"]["found"])
        self.assertEqual(g["cstrike"]["source"], "steam-manifest")
        self.assertEqual(g["cstrike"]["root"], os.path.join(lib, "steamapps", "common", "Counter-Strike"))
        self.assertIn("cstrike", g["cstrike"]["mods"])
        self.assertEqual(g["cstrike"]["setting"], "hl_root")
        self.assertFalse(g["valve"]["found"])
        self.assertEqual(g["valve"]["source"], "none")
        self.assertTrue(g["valve"]["hint"])
        self.assertEqual(set(g), {m for _, (_, m) in steam.KNOWN_APPS.items()})        # no war3 entry without war3 ownership

    def test_manual_root(self):
        r = goldsrc(self.p("My Games", "HL"), mods=("valve", "svencoop"))
        g = self.by_id(games.games_section(steam.ownership_manual(r)))
        self.assertEqual((g["valve"]["source"], g["svencoop"]["source"]), ("manual", "manual"))
        self.assertEqual(g["svencoop"]["root"], os.path.normpath(r))
        self.assertEqual(g["valve"]["mods"], ["svencoop", "valve"])
        self.assertFalse(g["cstrike"]["found"])

    def test_non_steam_candidate_is_listed_unconfirmed(self):
        r = goldsrc(self.p("Games", "Counter-Strike 1.6"), mods=("cstrike",))
        cands = [{"root": r, "mods": ["cstrike"]}]
        g = self.by_id(games.games_section(steam.ownership_from_steam(None), nonsteam=cands))
        self.assertFalse(g["cstrike"]["found"])                          # not served until confirmed
        self.assertEqual((g["cstrike"]["source"], g["cstrike"]["root"]), ("non-steam-scan", r))

    def test_war3_sources_and_json(self):
        import json
        w = self.p("Warcraft III")
        own = {"war3": {"allowed": True, "game_root": w, "installs": [{"root": w, "kind": "mpq", "version": "1.26"}]}}
        orig = war3._registry_paths
        try:
            war3._registry_paths = lambda: []
            g = self.by_id(games.games_section(own, None, [self.p("fan")]))["war3"]
            self.assertEqual((g["source"], g["version"], g["maps_root"], g["setting"]), ("install-folder", "1.26", self.p("fan"), "war3_root"))
            self.assertEqual(games.games_section(own, [w])[-1]["source"], "manual")
            war3._registry_paths = lambda: [w + os.sep]
            self.assertEqual(games.games_section(own)[-1]["source"], "registry")
            g = self.by_id(games.games_section({"war3": {"allowed": False}}))["war3"]
            self.assertFalse(g["found"])
            self.assertTrue(g["hint"])
            json.dumps(games.games_section(own))                         # status.json serialisable
        finally:
            war3._registry_paths = orig

    def test_companion_status_has_games(self):
        """The running State publishes the section in status.json (same ownership it serves from)."""
        import types, companion
        r = goldsrc(self.p("HL"), mods=("valve", "cstrike"))
        a = types.SimpleNamespace(prebuilt_dir=None, prebuilt_mod=None, game_root=r, steam_root=None, mods=None, no_war3=True,
                                  war3_root=None, war3_maps=None, cache_dir=self.p("cache"), world_allow=None, relay_url="",
                                  vrchat_log_dir=None, wait_max=0)
        st = companion.State(a)
        gs = st.status_json()["games"]
        d = {g["id"]: g for g in gs}
        self.assertEqual((d["valve"]["found"], d["valve"]["source"]), (True, "manual"))
        self.assertFalse(d["svencoop"]["found"])
        self.assertNotIn("war3", d)                                      # --no-war3: no Warcraft III row


if __name__ == "__main__":
    unittest.main(verbosity=1)
