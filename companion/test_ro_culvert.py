"""Prontera Culvert lane (w35, docs/design/ro_culvert.md): converter side. The four floors are packs of the RO map list (on demand in the world), the dark-dungeon object light,
the data generator (tools/ro_culvert_gen.py) on a synthetic rAthena fixture. No game data in git: the real-client checks run only when the owner's client / rAthena copy is present."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "rose_conv"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

from ro_conv import api, mappack, culvert, dungeons, towns  # noqa: E402

FLOORS = ["prt_sewb1", "prt_sewb2", "prt_sewb3", "prt_sewb4"]
ROOT = os.path.dirname(HERE)
RA_SRC = "/path/to/gsvr-testdata/w34/ra_src"


class Packs(unittest.TestCase):
    def test_floors_are_ids_27_to_30_on_demand(self):
        # w39: rebased onto the towns layout: the floors follow the 27 towns maps (ids 27..30), the dungeons lane follows them (31..42)
        order = list(api.MAP_ORDER)
        self.assertEqual(order[27:31], FLOORS)
        self.assertEqual(tuple(order[:27]), towns.NAMES)
        self.assertEqual(tuple(order[31:]), dungeons.NAMES)
        self.assertEqual(tuple(culvert.NAMES), tuple(FLOORS))
        self.assertEqual(culvert.ON_DEMAND, -2)                                    # == GsvrRoMaps.ClusterOnDemand
        for n in FLOORS:
            self.assertEqual(api.PACKS[n]["kind"], "zone")                          # never two RO builds at once
            self.assertIsNone(api.PACKS[n].get("start"))
            self.assertIn(n, api.PREBUILD_ORDER)                                    # the companion still builds the floors in the background (the world fetches them on demand)
            self.assertNotIn(n, api.NO_PREBUILD)
        self.assertEqual(api.SPRITE_PACK, "prontera")                              # floors are map only: no sprite set (2.1 MB less each)
        self.assertEqual(api.warm_after("prt_sewb2"), ["prt_sewb3", "prt_sewb4"])
        self.assertEqual(api.warm_after("prt_sewb4"), [])
        for m in towns.NAMES:                                                      # a town map never warms the floors in the background
            self.assertFalse(set(api.warm_after(m)) & set(FLOORS), m)

    def test_warps_table(self):
        rows = culvert.parse_rows(open(os.path.join(ROOT, "unity/Assets/GoldSrcVR/Editor/ro_culvert_warps.txt"), encoding="utf-8").read())
        self.assertEqual(len(rows), 12)
        g = culvert.links(rows)
        self.assertEqual({k: tuple(sorted(v)) for k, v in g.items()}, {k: tuple(sorted(v)) for k, v in culvert.LINKS.items()})
        self.assertEqual([r for r in rows if r[0] == "prt_fild05" and r[5] == "prt_sewb1"][0][1:5], (274, 208, 1, 1))     # rAthena's own entrance
        # every row's cells are inside the 8x map grids (cheap sanity); the real-pack walkability is ro_e2e --culvert (check.sh)
        for r in rows:
            self.assertTrue(0 <= r[1] < 400 and 0 <= r[2] < 400 and 0 <= r[6] < 400 and 0 <= r[7] < 400, r)

    def test_residency_mirrors_the_world(self):
        # GsvrRoMaps.LResident for ON-DEMAND maps: resident on itself / next to another on-demand map one warp away; the entrance map does NOT make B1 resident
        self.assertEqual(culvert.resident("prt_sewb2"), ["prt_sewb1", "prt_sewb2", "prt_sewb3"])
        self.assertEqual(culvert.resident("prt_sewb1"), ["prt_sewb1", "prt_sewb2"])
        self.assertEqual(culvert.resident("prt_fild05"), [])


class ObjectLight(unittest.TestCase):
    """dark maps darken the object textures (the loader's object shader has one fixed light); towns stay byte-identical"""
    def test_scale_by_rsw_light(self):
        L = lambda a, d: {"ambient": a, "diffuse": d}
        b1 = mappack.object_light_scale(L((0.2, 0.15, 0.2), (0.4, 0.4, 0.5)))         # prt_sewb1
        b4 = mappack.object_light_scale(L((0.3, 0.3, 0.3), (0.25, 0.25, 0.25)))       # prt_sewb4
        self.assertTrue(0.4 < b1 < 0.6, b1)
        self.assertTrue(0.45 < b4 < 0.65, b4)
        for lt in (L((0.55, 0.5, 0.5), (1.0, 1.0, 1.0)),                              # prontera
                   L((0.8, 0.8, 1.0), (0.9, 0.9, 0.9)),                               # prt_in
                   L((0.5, 0.5, 0.5), (1.0, 1.0, 1.0))):                              # prt_sewb2 / 3
            self.assertEqual(mappack.object_light_scale(lt), 1.0)
        self.assertEqual(mappack.object_light_scale(L((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))), 0.25)      # never fully black
        # a dark map is darker than a bright one (the check can fail)
        self.assertLess(b1, mappack.object_light_scale(L((0.5, 0.5, 0.5), (1.0, 1.0, 1.0))))

    def test_scale_rgb_keeps_alpha_and_identity(self):
        px = bytes([200, 100, 50, 255, 10, 20, 30, 0])
        self.assertIs(mappack.scale_rgb(px, 1.0), px)
        out = mappack.scale_rgb(px, 0.5)
        self.assertEqual(list(out), [100, 50, 25, 255, 5, 10, 15, 0])


class Generator(unittest.TestCase):
    MOB = """  - Id: 1051
    AegisName: THIEF_BUG
    Name: Thief Bug
    Level: 6
    Hp: 126
    BaseExp: 17
    JobExp: 5
    Attack: 18
    Attack2: 24
    Defense: 5
    Agi: 6
    Vit: 6
    Dex: 11
    AttackRange: 1
    ChaseRange: 12
    Size: Medium
    Race: Insect
    Element: Neutral
    ElementLevel: 3
    WalkSpeed: 150
    AttackDelay: 1288
    AttackMotion: 288
    DamageMotion: 768
    Ai: 07
    Drops:
      - Item: Jellopy
        Rate: 5500
  - Id: 1086
    AegisName: GOLDEN_BUG
    Name: Golden Thief Bug
    Level: 64
    Hp: 126000
    Attack: 870
    Attack2: 1145
    Size: Large
    Race: Insect
    Element: Fire
    ElementLevel: 2
    Ai: 07
    Class: Boss
    MvpExp: 7150
    MvpDrops:
      - Item: Gold_Ring
        Rate: 2000
"""
    ITEMS = "  - Id: 909\n    AegisName: Jellopy\n  - Id: 2615\n    AegisName: Gold_Ring\n"
    SEW = ("prt_sewb4,0,0\tmonster\tThief Bug\t1051,20\n"
           "prt_sewb4,0,0,0,0\tboss_monster\tGolden Thief Bug\t1086,1,3600000,600000,1\n// comment\n")

    def test_rows_on_a_synthetic_fixture(self):
        import ro_culvert_gen as G
        orig = G.IDS
        G.IDS = (1051, 1086)
        self.addCleanup(setattr, G, "IDS", orig)
        d = tempfile.mkdtemp(prefix="roculv")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name, txt in (("db_pre-re_mob_db.yml", self.MOB), ("db_pre-re_item_db_usable.yml", ""), ("db_pre-re_item_db_equip.yml", self.ITEMS), ("db_pre-re_item_db_etc.yml", "")):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(txt)
        sew = os.path.join(d, "sew.txt")
        with open(sew, "w", encoding="utf-8") as fh:
            fh.write(self.SEW)
        out = os.path.join(d, "out.txt")
        G.main(["x", d, sew, out])
        rows = [l for l in open(out, encoding="utf-8").read().splitlines() if not l.startswith("#")]
        mob = [r for r in rows if r.startswith("mob 1051 ")][0].split("#")[0].split()
        self.assertEqual(mob[1:5], ["1051", "6", "126", "18"])
        self.assertEqual(mob[22], "1")                                             # Medium
        self.assertEqual(mob[23], str(0x108B))                                     # Ai 07 -> MONSTER_TYPE_07
        self.assertIn("drop 1051 909 5500   # Jellopy", rows)
        self.assertIn("mobx 1086 Insect Fire 2 1 7150", rows)
        self.assertIn("mvpdrop 1086 2615 2000   # Gold_Ring", rows)
        self.assertIn("spawn 1051 20 0 0 0 0 0 0 prt_sewb4   # Thief Bug", rows)
        self.assertIn("spawn 1086 1 0 0 0 0 3600000 600000 prt_sewb4   # Golden Thief Bug (boss)", rows)

    @unittest.skipUnless(os.path.isdir(RA_SRC) and os.path.isfile("/tmp/cvt/prt_sew.txt"), "no rAthena copy")
    def test_checked_in_table_is_the_generator_output(self):
        import ro_culvert_gen as G
        d = tempfile.mkdtemp(prefix="roculv2")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        out = os.path.join(d, "o.txt")
        G.main(["x", RA_SRC, "/tmp/cvt/prt_sew.txt", out])
        with open(out, encoding="utf-8") as a, open(os.path.join(ROOT, "unity/Assets/GoldSrcVR/Editor/ro_culvert.txt"), encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
