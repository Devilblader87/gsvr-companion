"""MAP-FOLDERS-2 regression: the war3 map index must match the player's folders (maintainer 2026-10-27).

Run: python3 -I companion/test_war3_index.py   (also part of companion/selftest.sh)
Fixture = the maintainer's layout: a 1.26 MPQ install with Maps/{DoTA 30, Download 14, FrozenThrone 94, Scenario 9,
Tower Defends 14, World of WK 14, X Hero 50} + 43 loose maps, Campaigns and ghost/maps next to Maps, and
--war3-maps = the install root.
"""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runtime, war3                                   # noqa: E402

COUNTS = {"DoTA": 30, "Download": 14, "FrozenThrone": 94, "Scenario": 9, "Tower Defends": 14, "World of WK": 14,
          "X Hero": 50}
LOOSE = 43


def _w(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"HM3W" + str(content).encode())      # distinct content per file


def build_install(root):
    os.makedirs(root, exist_ok=True)
    for fn in ("war3.mpq", "War3x.mpq"):
        with open(os.path.join(root, fn), "wb") as fh:
            fh.write(b"MPQ\x1a" + b"\0" * 28)
    n = 0
    for folder, cnt in COUNTS.items():
        for i in range(cnt):
            n += 1                                      # every 3rd name has spaces (no safe key -> sha key)
            name = "%s Map %d.w3x" % (folder, i) if i % 3 == 0 else "%s_m%d.w3x" % (folder.replace(" ", ""), i)
            _w(os.path.join(root, "Maps", folder, name), n)
    for i in range(LOOSE):
        n += 1
        _w(os.path.join(root, "Maps", ("loose%d.w3x" % i) if i % 2 else ("Loose Map %d.w3m" % i)), n)
    # same base name, different content, in two folders: the install listing keeps one key, both must be listed
    _w(os.path.join(root, "Maps", "Scenario", "samename.w3x"), "a")
    _w(os.path.join(root, "Maps", "Download", "samename.w3x"), "b")
    _w(os.path.join(root, "Campaigns", "Human Campaign.w3m"), "c1")      # not in the game's map list
    _w(os.path.join(root, "Campaigns", "Orc.w3x"), "c2")
    _w(os.path.join(root, "ghost", "maps", "Ghost Map.w3x"), "g")


def folder_counts(maps):
    out = {}
    for k, e in maps.items():
        if k != "__glue":
            out[e["folder"]] = out.get(e["folder"], 0) + 1
    return out


class Index(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w3idx")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "Warcraft III v1.26")
        build_install(self.root)
        self.cache = os.path.join(self.tmp, "cache")
        runtime.tools_dir = lambda: os.path.join(os.path.dirname(HERE), "tools")     # w3mapfolders, war3fs

    def index(self, fan):
        own = war3.ownership(self.root)
        self.assertTrue(own["war3"]["allowed"], own["war3"]["reason"])
        return war3.build_index(own, fan, self.cache)["war3"]["maps"]

    def check_maintainer(self, maps):
        want = dict(COUNTS, Download=COUNTS["Download"] + 1, Scenario=COUNTS["Scenario"] + 1)
        want[""] = LOOSE
        self.assertEqual(folder_counts(maps), want)
        self.assertEqual(sum(1 for e in maps.values() if e.get("dir") == "fan"), 0)
        paths = [os.path.normcase(e["path"]) for k, e in maps.items() if k != "__glue"]
        self.assertEqual(len(paths), len(set(paths)), "one entry per map file")
        self.assertEqual(len(paths), sum(COUNTS.values()) + LOOSE + 2)
        self.assertFalse([p for p in paths if "campaigns" in p or "ghost" in p], "outside Maps: not listed")

    def test_fan_folder_is_install_root(self):
        self.check_maintainer(self.index([self.root]))

    def test_fan_folder_trailing_sep_and_forward_slashes(self):
        self.check_maintainer(self.index([self.root + os.sep]))
        self.check_maintainer(self.index([self.root.replace(os.sep, "/")]))

    def test_fan_folder_is_maps_folder(self):
        self.check_maintainer(self.index([os.path.join(self.root, "Maps")]))

    def test_fan_folder_is_maps_folder_other_spelling(self):
        # MAP-DUP (maintainer 2026-10-29): fan folder "D:/WC3/Maps" vs the install's "D:\\WC3\\Maps" listed every map twice
        # (sha-X and sha-X-<path hash>, the second failed to load); here the same folder in another spelling
        for fan in (os.path.join(self.root, ".", "Maps"), os.path.join(self.root, "Maps") + os.sep + os.sep):
            maps = self.index([fan])
            self.check_maintainer(maps)
            sha = [k for k in maps if k.startswith("sha-")]
            self.assertFalse([k for k in sha if k.count("-") > 1 and k.rsplit("-", 1)[0] in maps], fan)

    def test_fan_folder_inside_maps(self):
        self.check_maintainer(self.index([os.path.join(self.root, "Maps", "Download")]))

    def test_identical_copy_in_second_user_folder(self):
        # a byte-identical spaced-name map in Maps/DoTA and Maps/Download: both folders list it (dedup by path)
        src = os.path.join(self.root, "Maps", "DoTA", "DoTA Map 0.w3x")
        dst = os.path.join(self.root, "Maps", "Download", "DoTA Map 0.w3x")
        shutil.copy(src, dst)
        for fan in (None, [self.root]):
            maps = self.index(fan)
            fl = sorted(e["folder"] for k, e in maps.items() if k != "__glue" and e["title"] == "DoTA Map 0")
            self.assertEqual(fl, ["DoTA", "Download"], fan)

    def test_no_fan_folder(self):
        self.check_maintainer(self.index(None))

    def test_scan_watches_install_maps(self):
        self.index(None)
        self.assertIn(os.path.join(self.root, "Maps"), war3.LAST_SCAN["dirs"])

    def test_separate_fan_folder_keeps_subfolders(self):
        fan = os.path.join(self.tmp, "MyFan")
        _w(os.path.join(fan, "TD", "Nice TD.w3x"), "f1")
        _w(os.path.join(fan, "TD", "Deep", "Deeper.w3x"), "f2")
        _w(os.path.join(fan, "flat.w3x"), "f3")
        # a fan copy of an install map (same content) is not listed twice: the install entry wins
        shutil.copy(os.path.join(self.root, "Maps", "FrozenThrone", "FrozenThrone_m1.w3x"),
                    os.path.join(fan, "TD", "copy_of_ft.w3x"))
        maps = self.index([fan])
        by = {os.path.basename(e["path"]): e for k, e in maps.items() if k != "__glue"}
        self.assertEqual(by["Nice TD.w3x"]["folder"], "MyFan/TD")
        self.assertEqual(by["Deeper.w3x"]["folder"], "MyFan/TD/Deep")
        self.assertEqual(by["flat.w3x"]["folder"], "Download")        # directly in the fan folder: "Download"
        self.assertNotIn("copy_of_ft.w3x", by)
        self.assertEqual(by["FrozenThrone_m1.w3x"]["folder"], "FrozenThrone")
        self.assertEqual(sum(1 for e in maps.values() if e.get("dir") == "fan"), 1)


if __name__ == "__main__":
    unittest.main()
