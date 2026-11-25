"""RO towns lane (docs/design/ro_towns.md): the map table of the towns + fields, its copies in the scene builder and the warp table, the residency
(loading plan) rule, the rAthena warp generator and the pack checker. Stdlib only, no game data (synthetic rAthena tree + synthetic packs);
the owner's converted packs / iRO client are checked too when they are on this machine (Mac only, else skipped).

  cd companion && python3 -I test_ro_towns.py
  GSVR_RO_TOWNS_PACKS=<dir with ro__<map>.pack>   real packs (default /path/to/gsvr-testdata/ro_out/towns/packs)
  GSVR_RO_CLIENT=<iRO client folder>               the RSM survey (default /path/to/gsvr-testdata/ro_data/Ragnarok Online)
"""
import collections
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "rose_conv"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from ro_conv import api, culvert, dungeons, towns  # noqa: E402
import rpg1 as P  # noqa: E402
import ro_towns_warps as TW  # noqa: E402

ED = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR", "Editor")
PACKS_DIR = os.environ.get("GSVR_RO_TOWNS_PACKS", "/path/to/gsvr-testdata/ro_out/towns/packs")
CLIENT = os.environ.get("GSVR_RO_CLIENT", "/path/to/gsvr-testdata/ro_data/Ragnarok Online")


def rd(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


def builder_array(src, name, kind=int):
    src = re.sub(r"//[^\n]*", "", src)          # line comments (the w39 culvert / dungeons rows carry notes)
    m = re.search(name + r"\s*=\s*\{([^}]*)\}", src)
    if not m:
        return None
    if kind is str:
        return re.findall(r'"([a-z0-9_]+)"', m.group(1))
    return [int(x) for x in m.group(1).replace("\n", " ").split(",") if x.strip()]


class Table(unittest.TestCase):
    def test_table(self):
        self.assertEqual(towns.NAMES[:5], ("prontera", "prt_in", "prt_church", "prt_fild08", "izlude"))
        self.assertEqual(len(set(towns.NAMES)), len(towns.NAMES))
        self.assertLessEqual(len(api.MAP_ORDER), towns.MAX_MAPS)          # GsvrRoMaps.MaxMaps = 64 (two-int masks, w39)
        self.assertEqual(towns.MAX_MAPS, 64)
        self.assertEqual(tuple(api.MAP_ORDER[:len(towns.NAMES)]), towns.NAMES)          # w37 integrate: the dungeons lane appends its maps after the towns (test_ro_dungeons.py)
        self.assertEqual(tuple(api.MAP_ORDER[len(towns.NAMES):]), culvert.NAMES + dungeons.NAMES)          # w39: ids 27..30 culvert, 31..42 dungeons
        for m, label, c, k in towns.WORLD:
            self.assertTrue(re.match(r"^[a-z0-9_]{1,32}$", m), m)
            self.assertTrue(label and k in ("town", "interior", "field"), m)
            self.assertTrue(c == "" or c in towns.CLUSTERS, m)
            self.assertEqual(api.PACKS[m]["kind"], "zone")                    # never two RO builds at once
            if c == "":
                self.assertEqual(k, "field", m)                              # only fields lie between clusters
        for c in towns.CLUSTERS:                                             # every cluster has its town
            self.assertIn("town", [k for m, l, cc, k in towns.WORLD if cc == c], c)
        self.assertEqual(api.PACKS["prontera"].get("start"), (156, 191))    # the prototype's entries are untouched
        self.assertEqual(api.SPRITE_PACK, "prontera")
        self.assertEqual(towns.cluster_id("prt_fild04"), -1)
        self.assertEqual(towns.cluster_id("payon"), towns.CLUSTERS.index("payon"))
        self.assertEqual(towns.cluster_id("nowhere"), -1)

    def test_builder_copies(self):
        src = rd(ED, "GsvrSceneBuilder.Ragnarok.cs")
        world = list(api.MAP_ORDER)          # w39: towns + culvert floors + dungeons
        self.assertEqual(builder_array(src, "RoMapNames", str), world)
        self.assertEqual(builder_array(src, "RoMapCluster"), towns.cluster_ids() + [culvert.ON_DEMAND] * (len(world) - len(towns.NAMES)))          # culvert + dungeons: ON DEMAND (-2)
        t, o = builder_array(src, "RoMapTerrNeed"), builder_array(src, "RoMapObjNeed")
        self.assertEqual((len(t), len(o)), (len(world), len(world)))
        self.assertTrue(all(0 < x <= 80 for x in t) and all(0 < x <= 700 for x in o), (t, o))   # converter limits: 80 terrain blocks, 700 chunks
        self.assertEqual(re.findall(r'"([^"]+)"', re.search(r"RoMapLabels\s*=\s*\{([^}]*)\}", src).group(1)),
                         [towns.LABEL[m] for m in towns.NAMES] + [culvert.LABEL[m] for m in culvert.NAMES] + [dungeons.LABEL[m] for m in dungeons.NAMES])          # the nav panel's names == the companion's labels
        self.assertEqual(re.findall(r'"([^"]+)"', re.search(r"RoClusterLabels\s*=\s*\{([^}]*)\}", src).group(1)), [towns.LABEL[c] if c != "morocc" else "Morroc" for c in towns.CLUSTERS])
        self.assertIn("maps.mapCluster = RoMapCluster; maps.ramBudgetMB = RoRamBudgetMB; maps.onDemandBudgetMB = RoOnDemandBudgetMB;", src)
        m = re.search(r"const float RoRamBudgetMB = (\d+)f;", src)
        self.assertTrue(m and 100 <= int(m.group(1)) <= 400)

    def test_links_equal_the_warp_table(self):
        warps = towns.parse_warps(rd(ED, "ro_warps.txt"))
        g = towns.links(warps)
        self.assertEqual({k: set(v) for k, v in towns.LINKS.items()}, {m: g.get(m, set()) for m in towns.NAMES})
        self.assertEqual(set(towns.LINKS), set(towns.NAMES))
        self.assertLessEqual(len(warps), 512)                                 # GsvrRoMaps.MaxWarps
        # every map can be reached from the start map
        seen, q = {"prontera"}, collections.deque(["prontera"])
        while q:
            for v in towns.LINKS[q.popleft()]:
                if v not in seen:
                    seen.add(v); q.append(v)
        self.assertEqual(seen, set(towns.NAMES))
        # every link between two different maps has a way back, except the desert crossroads (rAthena: its guards only lead to Morroc)
        oneway = sorted((a, b) for a in towns.LINKS for b in towns.LINKS[a] if a not in towns.LINKS[b])
        self.assertEqual(oneway, [("moc_fild01", "moc_fild20")])
        self.assertEqual(len([w for w in warps if w[8].startswith(TW.GUARD)]), len(TW.GUARDS))
        self.assertIn("prt_fild08 371 212 3 3 izlude 24 98 prtf005", rd(ED, "ro_warps.txt"))          # the prototype rows are unchanged

    def test_residency_rule(self):
        r = towns.resident
        self.assertEqual(r("prontera"), ["prontera", "prt_in", "prt_church", "prt_fild08", "izlude", "prt_fild05"])
        self.assertEqual(r("pay_fild01"), ["prontera", "moc_fild03", "pay_fild01", "payon", "payon_in01", "payon_in02", "payon_in03", "pay_arche", "pay_fild02"])
        self.assertEqual(r("prt_fild04"), ["prontera", "prt_fild05", "prt_fild04", "prt_fild00"])           # a route field: itself + its neighbours
        self.assertEqual(r("moc_fild20"), ["prontera", "moc_fild20", "morocc", "morocc_in"])
        self.assertEqual(r("geffen_in"), ["prontera", "gef_fild00", "geffen", "geffen_in"])
        for m in towns.NAMES:
            rs = r(m)
            self.assertEqual(rs[0], "prontera")                               # map 0 always (sprites, respawn)
            self.assertIn(m, rs)
            self.assertTrue(set(towns.LINKS[m]) <= set(rs), m)               # the map behind every exit is there before the player walks into it
        warps = towns.parse_warps(rd(ED, "ro_warps.txt"))
        self.assertEqual([r(m, warps) for m in towns.NAMES], [r(m) for m in towns.NAMES])       # LINKS and the table give the same sets


def write_tree(root, files, confs):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    for rel, lst in confs.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("// warps\n" + "".join("npc: %s\n" % x for x in lst))


def tiny_pack(path, w, h, walk):
    cells = bytearray(w * h)
    for (x, y) in walk:
        cells[y * w + x] = 1
    a = dict((k, 0) for k in P.AREA_FIELDS)
    a.update(zone_id=900, grid_w=w, grid_h=h, cell_size=1.0, height_unit=0.01)
    for k in P.AREA_FIELDS:
        if k.startswith(("cell_size", "origin", "world_off", "start", "bmin", "bmax", "height_unit", "water")):
            a[k] = float(a[k])
    with open(path, "wb") as fh:
        fh.write(P.write_pack([(b"AREA", 1, 1, P.enc_area(a)), (b"CELL", 1, w * h, bytes(cells))]))


class Generator(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rotowns")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def test_gen_rows_from_a_rathena_tree(self):
        T = "\t"
        files = {
            "npc/warps/cities/payon.txt": "//= Payon\n"
            + "payon,200,22,0" + T + "warp" + T + "pay01" + T + "2,2,pay_fild01,200,370\n"
            + "//payon,1,1,0" + T + "warp" + T + "old" + T + "1,1,pay_fild01,5,5\n"
            + "payon,9,9,0" + T + "warp" + T + "dun" + T + "1,1,pay_dun00,5,5\n"
            + "payon_in01,30,30,0" + T + "warp2" + T + "room" + T + "1,1,payon_in01,40,40\n",
            "npc/pre-re/warps/fields/payon_fild.txt": "pay_fild01,200,374,0" + T + "warp" + T + "payf" + T + "3,1,payon,200,26\n"
            + "payon,200,22,0" + T + "warp" + T + "dup" + T + "2,2,pay_fild01,1,1\n"
            + "prontera,1,1,0" + T + "warp" + T + "proto" + T + "1,1,prt_in,2,2\n",
            "npc/re/warps/cities/payon.txt": "payon,50,50,0" + T + "warp" + T + "renewal" + T + "1,1,pay_fild01,5,5\n",
        }
        write_tree(self.work, files, {"npc/scripts_warps.conf": ["npc/warps/cities/payon.txt", "npc/warps/cities/missing.txt"],
                                      "npc/pre-re/scripts_warps.conf": ["npc/pre-re/warps/fields/payon_fild.txt"]})
        rows = TW.gen_rows(self.work)
        names = [r[8] for r in rows]
        self.assertEqual(names[:3], ["pay01", "room", "payf"])               # commented / unknown map / renewal-only / prototype-only / duplicate spot skipped
        self.assertNotIn("renewal", names)
        self.assertNotIn("dup", names)
        self.assertNotIn("proto", names)
        self.assertEqual(rows[0], ("payon", 200, 22, 2, 2, "pay_fild01", 200, 370, "pay01"))
        self.assertEqual(len([r for r in rows if r[8].startswith(TW.GUARD)]), len(TW.GUARDS))
        self.assertEqual(TW.fmt(rows[0]), "payon 200 22 2 2 pay_fild01 200 370 pay01")

    def test_check_finds_bad_rows(self):
        for m in towns.NAMES:
            tiny_pack(os.path.join(self.work, "ro__%s.pack" % m), 20, 20, [(x, y) for x in range(20) for y in range(20) if (x, y) != (15, 15)])
        good = "prontera 2 2 1 1 prt_in 10 10 a\nprt_in 10 3 1 1 prontera 5 5 b\n"
        rows, have, fails = TW.check(self.work, good)
        self.assertEqual((len(rows), len(have), fails), (2, len(towns.NAMES), []))
        bad = good + "prontera 15 15 0 0 prt_in 11 11 nowalk\nprontera 8 8 1 1 prt_in 15 15 blocked\nprontera 8 12 1 1 prt_in 10 3 inspan\nprontera 3 9 1 1 izlude 50 1 outside\n"
        rows, have, fails = TW.check(self.work, bad)
        self.assertEqual(len(fails), 4, fails)
        self.assertTrue(any(f.startswith("nowalk: span") for f in fails))
        self.assertTrue(any(f.startswith("blocked: target") for f in fails))
        self.assertTrue(any(f.startswith("inspan: target prt_in 10,3 lies inside warp b") for f in fails))
        self.assertTrue(any(f.startswith("outside: target") for f in fails))
        # a guard is talked to: an arrival inside its area is no ping-pong (re-arm rule)
        rows, have, fails = TW.check(self.work, good + "prt_in 10 10 3 3 prontera 4 4 guard#01\n")
        self.assertEqual(fails, [])
        os.remove(os.path.join(self.work, "ro__izlude.pack"))
        self.assertEqual(TW.main(["check", self.work, "--warps", self._file(good)]), 1)            # a missing pack fails the check

    def _file(self, text):
        p = os.path.join(self.work, "w.txt")
        with open(p, "w") as fh:
            fh.write(text)
        return p


@unittest.skipUnless(os.path.isfile(os.path.join(PACKS_DIR, "ro__payon.pack")), "no converted towns packs on this machine")
class RealPacks(unittest.TestCase):
    """the owner's converted packs (Mac only): warps, sizes, RAM plan, the builder's pool needs"""
    def test_warps_against_the_packs(self):
        rows, have, fails = TW.check(PACKS_DIR, rd(ED, "ro_warps.txt"))
        self.assertEqual(have, sorted(towns.NAMES))
        self.assertEqual(fails, [])
        self.assertEqual(len(rows), 291)

    def test_plan(self):
        out, me, fails = TW.plan(PACKS_DIR, rd(ED, "ro_warps.txt"))
        self.assertEqual(fails, [])
        self.assertTrue(all(d["bytes"] < TW.PACK_PREFER for d in me.values()), {m: d["bytes"] for m, d in me.items()})     # every pack < 25 MB
        src = rd(ED, "GsvrSceneBuilder.Ragnarok.cs")
        n = len(towns.NAMES)
        self.assertEqual(builder_array(src, "RoMapTerrNeed")[:n], [me[m]["terr"] for m in towns.NAMES])
        self.assertEqual(builder_array(src, "RoMapObjNeed")[:n], [me[m]["chunks"] for m in towns.NAMES])
        budget = float(re.search(r"const float RoRamBudgetMB = (\d+)f;", src).group(1))
        peak = max(r[3] for r in out)
        biggest = max(d["mb"] for d in me.values())
        self.assertLess(peak + biggest, budget + 1e-6, "the largest resident set + one on-demand map fit the budget")
        self.assertGreater(sum(d["mb"] for d in me.values()), budget, "all maps at once would not fit (the budget is real)")


@unittest.skipUnless(os.path.isfile("/path/to/gsvr-testdata/ro_out/culvert/ro__prt_sewb1.pack") and os.path.isfile("/path/to/gsvr-testdata/ro_out/w37/ro__pay_dun00.pack"), "no converted culvert / dungeon packs on this machine")
class OnDemandPools(unittest.TestCase):
    """w39: the builder's pool needs of the culvert floors (ids 27..30) and the dungeons (31..42) are the measured packs"""
    def test_needs_equal_the_measured_packs(self):
        src = rd(ED, "GsvrSceneBuilder.Ragnarok.cs")
        t, o = builder_array(src, "RoMapTerrNeed"), builder_array(src, "RoMapObjNeed")
        n = len(towns.NAMES)
        for i, m in enumerate(culvert.NAMES + dungeons.NAMES):
            d = TW.measure(os.path.join("/path/to/gsvr-testdata/ro_out/culvert" if m in culvert.NAMES else "/path/to/gsvr-testdata/ro_out/w37", "ro__%s.pack" % m))
            self.assertEqual((t[n + i], o[n + i]), (d["terr"], d["chunks"]), m)


@unittest.skipUnless(os.path.isdir(CLIENT) and os.path.isfile(os.path.join(CLIENT, "data.grf")), "no iRO client on this machine")
class RsmSurvey(unittest.TestCase):
    """every model of the towns maps is RSM 1.x (the converter has no RSM2 parser: a client update that brings RSM2 into one of them fails here)"""
    def test_rsm_versions(self):
        from ro_conv.grf import Vfs
        from ro_conv import rofmt
        v = Vfs(CLIENT)
        bad, seen = [], {}
        for m in towns.NAMES:
            rw = rofmt.rsw(v.read("data\\%s.rsw" % m))
            for fn in set(x["file"] for x in rw["models"]):
                if fn not in seen:
                    d = v.read("data\\model\\" + fn)
                    seen[fn] = (d[4], d[5])
                if seen[fn][0] != 1:
                    bad.append((m, fn, seen[fn]))
        self.assertEqual(bad, [])
        self.assertGreater(len(seen), 500)


if __name__ == "__main__":
    unittest.main()
