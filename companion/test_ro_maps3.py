"""RO maps wave 3 part 1 (docs/design/ro_maps3.md, w53 lane "dungeons3"): Byalan + the Undersea Tunnel, the Sunken Ship, the Geffen Dungeon (13 maps, ids 62..74).

Table == builder == warps == data rows == generated module, registration (on demand, never prebuilt), the loading plan numbers, the MVP rows, the generator on a synthetic rAthena tree,
the real packs / RSM survey on the Mac (skipped without the owner's client). Stdlib only.

  cd companion && python3 -I test_ro_maps3.py
"""
import importlib.util
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'rose_conv'))
from ro_conv import api, dungeons, maps2, maps3, maps3_data, mobsets, towns  # noqa: E402

ED = os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Editor')
CLIENT = '/path/to/gsvr-testdata Online/'
PACKS = os.environ.get('RO_MAPS3_PACKS', '/path/to/gsvr-testdata')


def rd(*p):
    with open(os.path.join(*p), encoding='utf-8') as fh:
        return fh.read()


def load_tool():
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    spec = importlib.util.spec_from_file_location('ro_maps3_gen', os.path.join(ROOT, 'tools', 'ro_maps3_gen.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


T = load_tool()
WARPS = T.m2.parse_rows(rd(ED, 'ro_maps3_warps.txt'))


def builder_list(src, name, quoted):
    src = re.sub(r'//[^\n]*', '', src)
    body = re.search(name + r'\s*=\s*\{([^}]*)\}', src).group(1)
    return re.findall(r'"([^"]+)"', body) if quoted else [int(x) for x in re.findall(r'-?\d+', body)]


class Table(unittest.TestCase):
    def test_world(self):
        self.assertEqual(len(maps3.NAMES), 13)
        self.assertEqual(len(set(maps3.NAMES)), 13)
        self.assertEqual(maps3.MAPS3_BASE, len(towns.NAMES) + 4 + 12 + 7 + len(maps2.NAMES))               # 27 + 4 + 12 + 7 + 12 == 62
        self.assertEqual(tuple(api.MAP_ORDER[maps3.MAPS3_BASE:maps3.MAPS3_BASE + 13]), maps3.NAMES)
        self.assertEqual([maps3.world_id(m) for m in maps3.NAMES], list(range(62, 75)))
        self.assertLessEqual(len(api.MAP_ORDER), towns.MAX_MAPS)
        self.assertEqual(len(api.MAP_ORDER), 75)
        for n in maps3.NAMES:
            self.assertRegex(n, r'^[a-z0-9_]{1,32}$')
            self.assertIn(maps3.AREA[n], maps3.AREAS)
            self.assertTrue(maps3.LABEL[n])
            self.assertEqual(api.PACKS[n]['kind'], 'zone')
            self.assertIn(n, maps3.MEASURED)
            t, o, mb = maps3.MEASURED[n]
            self.assertTrue(0 < t <= 80 and 0 < o <= 700, n)                             # converter limits: 80 terrain blocks, 700 chunks
        self.assertEqual(set(maps3.MEASURED), set(maps3.NAMES))
        self.assertTrue(set(maps3.NO_MOBS) <= set(maps3.NAMES))
        self.assertEqual(sorted(maps3.AREAS), sorted(set(maps3.AREA.values())))

    def test_registered_in_api(self):
        for n in maps3.NAMES:
            self.assertIn(n, api.NO_PREBUILD)
            self.assertNotIn(n, api.PREBUILD_ORDER)                                      # never built in the background
            self.assertEqual(api.warm_after(n), [])
            self.assertIn('snd_' + n, api.AUDIO_PACKS)                                   # music + the map's monsters' sounds: automatic for every map of PACKS
        for t in ('prontera', 'geffen', 'izlude', 'alberta'):
            self.assertTrue(set(api.warm_after(t)).isdisjoint(maps3.NAMES))

    def test_town_ends_are_loaded_maps_of_other_lanes(self):
        for e in maps3.TOWN_ENDS:
            self.assertNotIn(e, maps3.NAMES)
            self.assertIn(e, api.MAP_ORDER)
            self.assertLess(api.MAP_ORDER.index(e), 27)                                  # towns lane maps

    def test_budget_and_residency(self):
        ram = dict((n, v[2]) for n, v in maps3.MEASURED.items())
        ram.update((n, v[2]) for n, v in maps2.MEASURED.items())
        ram.update(dungeons.RAM_MB)
        od = set(maps3.NAMES)
        g = {}
        for r in WARPS:
            if r[0] != r[5]:
                g.setdefault(r[0], set()).add(r[5])
        worst, at = 0, ''
        for cur in maps3.NAMES:
            res = [m for m in od if m == cur or m in g.get(cur, ())]
            s = sum(ram[m] for m in res)
            if s > worst:
                worst, at = s, cur
        self.assertLessEqual(worst, maps3.ONDEMAND_BUDGET_MB * 10, 'resident set of %s' % at)
        self.assertEqual(sorted(maps3.resident('iz_dun01')), ['iz_dun00', 'iz_dun01', 'iz_dun02'])
        self.assertEqual(maps3.resident('geffen'), [])


class Warps(unittest.TestCase):
    def test_links_equal_the_warp_table(self):
        got = dict((k, tuple(sorted(v))) for k, v in T.links_of([w for w in WARPS if not w[8].startswith('bridge#')]).items())
        want = dict((k, tuple(sorted(v))) for k, v in maps3_data.LINKS.items())
        self.assertEqual(got, want)

    def test_every_warp_has_a_way_back(self):
        g = T.links_of(WARPS)
        for a, bs in g.items():
            for b in bs:
                if a == 'gef_dun03' or b == 'gef_dun03':
                    continue                                                             # the event map (module doc): the entrance warp is commented out in rAthena, only the way out exists
                self.assertIn(a, g.get(b, ()), '%s -> %s has no way back' % (a, b))

    def test_gef_dun03_is_the_rathena_event_map(self):
        g = T.links_of(WARPS)
        self.assertEqual(g.get('gef_dun03'), {'gef_dun02'})                              # gefd03-1 exists ...
        self.assertNotIn('gef_dun03', g.get('gef_dun02', ()))                            # ... the way in (gefd03) is commented out in npc/pre-re/warps/dungeons/gef_dun.txt

    def test_all_but_the_event_map_reachable_from_the_loaded_towns_maps(self):
        g = T.links_of(WARPS)
        seen = set(maps3.TOWN_ENDS)
        todo = list(seen)
        while todo:
            c = todo.pop()
            for n in g.get(c, ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        self.assertEqual(set(maps3.NAMES) - seen, {'gef_dun03'})
        by = dict((w[8], w) for w in WARPS)
        for nm in ('bridge#byl1', 'bridge#byl2', 'bridge#sun1', 'bridge#sun2', 'bridge#gef1', 'bridge#gef2'):
            self.assertIn(nm, by)
        self.assertEqual(sorted(w[8] for w in WARPS if w[8].startswith('bridge#')), sorted(b[8] for b in maps3.BRIDGES))

    def test_bridge_arrivals_stand_outside_the_way_back(self):
        by = dict((b[8], b) for b in maps3.BRIDGES)
        for a, b in (('bridge#byl1', 'bridge#byl2'), ('bridge#sun1', 'bridge#sun2'), ('bridge#gef1', 'bridge#gef2')):
            x, y = by[a], by[b]
            self.assertEqual((x[5], y[5]), (y[0], x[0]))
            self.assertFalse(abs(x[6] - y[1]) <= y[3] and abs(x[7] - y[2]) <= y[4], a)
            self.assertFalse(abs(y[6] - x[1]) <= x[3] and abs(y[7] - x[2]) <= x[4], b)

    def test_rathena_rows_are_the_expected_ones(self):
        rows = [w for w in WARPS if not w[8].startswith('bridge#')]
        names = set(w[8] for w in rows)
        for nm in ('izd01', 'izd01-1', 'izd02', 'izd03', 'izd04', 'izd05', 'izd06', 'izd06-1', 'alb002', 'tre001', 'tre002', 'tre003', 'gefd01', 'gefd01-1', 'gefd02', 'gefd02-1', 'gefd03-1'):
            self.assertIn(nm, names)
        self.assertNotIn('gefd03', names)
        self.assertNotIn('gef005', names)                                                # gef_tower is not loaded: bridge#gef1 / 2 replace gef002 + gef005 / gef024
        self.assertEqual(len(rows), 51)
        self.assertEqual(len(WARPS), 57)

    def test_rows_use_lane_or_town_end_maps_only(self):
        ok = set(maps3.NAMES) | set(maps3.TOWN_ENDS)
        for w in WARPS:
            self.assertIn(w[0], ok)
            self.assertIn(w[5], ok)
            self.assertTrue(w[0] in maps3.NAMES or w[5] in maps3.NAMES)

    def test_total_fits_the_warp_table(self):
        n = 0
        for f in ('ro_warps.txt', 'ro_culvert_warps.txt', 'ro_dungeon_warps.txt', 'ro_jobmap_warps.txt', 'ro_maps2_warps.txt', 'ro_maps3_warps.txt'):
            n += len([ln for ln in rd(ED, f).splitlines() if ln.strip() and not ln.startswith('#')])
        self.assertLessEqual(n, 512)                                                     # GsvrRoMaps.MaxWarps
        self.assertEqual(n, 492)

    def test_chains_are_real_hops(self):
        g = T.links_of(WARPS)
        for ch in maps3.CHAINS:
            for a, b in zip(ch, ch[1:]):
                self.assertIn(b, g.get(a, ()), '%s -> %s' % (a, b))


class Builder(unittest.TestCase):
    def test_builder_tables(self):
        src = rd(ED, 'GsvrSceneBuilder.Ragnarok.cs')
        names = builder_list(src, 'RoMapNames', True)
        self.assertEqual(names, list(api.MAP_ORDER))
        b = maps3.MAPS3_BASE
        self.assertIn('RoMaps3Base = %d' % b, src)
        self.assertEqual(builder_list(src, 'RoMapLabels', True)[b:], [maps3.LABEL[m] for m in maps3.NAMES])
        self.assertEqual(builder_list(src, 'RoMapCluster', False)[b:], [-2] * len(maps3.NAMES))
        self.assertEqual(builder_list(src, 'RoMapTerrNeed', False)[b:], [maps3.MEASURED[m][0] for m in maps3.NAMES])
        self.assertEqual(builder_list(src, 'RoMapObjNeed', False)[b:], [maps3.MEASURED[m][1] for m in maps3.NAMES])
        self.assertIn('const float RoOnDemandBudgetMB = %df;' % maps3.ONDEMAND_BUDGET_MB, src)
        self.assertIn('RoMaps3WarpsPath', src)
        self.assertIn('ro_maps3_warps.txt', src)


class Data(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.txt = rd(ED, 'ro_maps3.txt')
        cls.rows = [ln.split('#')[0].split() for ln in cls.txt.splitlines() if ln.strip() and not ln.startswith('#')]
        cls.all = [ln.split('#')[0].split() for ln in rd(ED, 'ro_mobs_all.txt').splitlines() if ln.strip() and not ln.startswith('#')]

    def kind(self, k, rows=None):
        return [r for r in (self.rows if rows is None else rows) if r[0] == k]

    def test_spawns_map_to_lane_maps_and_known_classes(self):
        mobs = set(int(r[1]) for r in self.kind('mob'))
        sp = self.kind('spawn')
        per = {}
        for r in sp:
            self.assertIn(r[9], maps3.NAMES)
            self.assertIn(int(r[1]), mobs, r)
            per[r[9]] = per.get(r[9], 0) + int(r[2])
        self.assertEqual(set(per), set(n for n in maps3.NAMES if n not in maps3.NO_MOBS))
        self.assertTrue(all(v <= 320 for v in per.values()), per)                       # GsvrRoMobs.MaxMobs
        for n in maps3.NO_MOBS:
            self.assertNotIn(n, per)

    def test_mvps(self):
        by = dict((int(r[1]), int(r[3])) for r in self.kind('mob'))
        for mid, mp in maps3.MVPS:
            self.assertIn(mid, by)
            self.assertEqual(len([r for r in self.kind('spawn') if int(r[1]) == mid and r[9] == mp]), 1, mid)
        boss = set(int(ln.split()[1]) for ln in self.txt.splitlines() if ln.startswith('spawn ') and '(boss)' in ln)
        self.assertEqual(boss, set(dict(maps3.MVPS)))                                    # the only boss_monster rows of the lane are the three MVPs
        self.assertEqual(by[1626], 286900)                                               # Hellion Revenant (gef_dun03) is a plain monster row: the boss HP cap does not apply
        dg = rd(ED, 'ro_dungeons.txt')
        for mid, mp in maps3.MVPS:
            self.assertRegex(dg, r'(?m)^mvp %d %s ' % (mid, mp))
            self.assertRegex(dg, r'(?m)^mvpexp %d \d+' % mid)
            self.assertRegex(dg, r'(?m)^mvpdrop %d \d+ \d+' % mid)
        self.assertEqual(len(re.findall(r'(?m)^mvp \d+ ', dg)), 10)                      # 2 dungeon lane + 5 (w52) + 3 (w53) = 10 <= GsvrRoDungeon.SlotN
        src = rd(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Scripts', 'GsvrRoDungeon.cs')
        self.assertIn('SlotN = 12;', src)

    def test_merged_table_holds_the_lane(self):
        spawn_all = self.kind('spawn', self.all)
        have = set(r[9] for r in spawn_all)
        for n in maps3.NAMES:
            if n not in maps3.NO_MOBS:
                self.assertIn(n, have, n)
        cls = len(self.kind('mob', self.all))
        self.assertEqual(cls, len(set(r[1] for r in self.kind('mob', self.all))))      # a class once
        self.assertEqual(len(self.kind('mobel', self.all)), cls)
        self.assertLess(cls, 192)                                                        # GsvrRoMobs.MaxCls
        self.assertLess(len(self.kind('drop', self.all)), 1536)                          # MaxDropRows
        self.assertLess(len(self.kind('item', self.all)), 768)                           # MaxItems
        src = rd(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Scripts', 'GsvrRoMobs.cs')
        self.assertIn('MaxCls = 192, MaxDropRows = 1536, MaxItems = 768, MaxSpawns = 1024', src)
        self.assertLess(len(spawn_all), 1024)                                            # MaxSpawns (w53: 768 -> 1024, the merged table holds 800+)
        self.assertGreater(len(spawn_all), 768)

    def test_merge_is_reproducible(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import ro_fields_gen as F
        self.assertEqual(F.merge(ROOT), rd(ED, 'ro_mobs_all.txt'))                      # the committed merged table == a fresh merge of the repo files

    def test_sprite_sets_per_map(self):
        for n in maps3.NAMES:
            if n in maps3.NO_MOBS:
                self.assertNotIn(n, mobsets.MOB_SPRITES)
            else:
                self.assertTrue(len(mobsets.MOB_SPRITES[n]) >= 3, n)
                self.assertIn(n, mobsets.DROP_ITEMS)
        self.assertIn('drake', mobsets.MOB_SPRITES['treasure02'])
        self.assertIn('doppelganger', mobsets.MOB_SPRITES['gef_dun02'])
        self.assertIn('dracula', mobsets.MOB_SPRITES['gef_dun01'])

    def test_mapflags_of_the_lane(self):
        fl = rd(ED, 'ro_mapflags.txt')
        for n in maps3.NAMES:
            self.assertRegex(fl, r'(?m)^mapflag %s [a-z ]*nomemo' % n)                   # npc/mapflag/nomemo.txt lists every one of the 13
        self.assertRegex(fl, r'(?m)^mapflag gef_dun03 noteleport nomemo$')               # npc/mapflag/noteleport.txt: gef_dun03
        self.assertIn('# 60 of 75 maps flagged', fl)

    def test_no_game_data_words(self):
        self.assertNotRegex(self.txt.lower(), r'\.(spr|act|gat|gnd|rsw|rsm|bmp)\b')


class Generator(unittest.TestCase):
    def tree(self, d):
        files = {
            'npc/scripts_warps.conf': 'npc: npc/warps/a.txt\n',
            'npc/pre-re/scripts_warps.conf': '',
            'npc/warps/a.txt': ('izlu2dun,108,83,0\twarp\tizd01\t3,2,iz_dun00,168,168\n'
                                'iz_dun00,168,173,0\twarp\tizd01-1\t3,3,izlu2dun,108,88\n'
                                'gef_tower,153,28,0\twarp\tgef005\t4,2,gef_dun00,104,99\n'
                                'gef_dun00,104,103,0\twarp\tgef024\t3,3,gef_tower,153,31\n'
                                'treasure01,112,164,0\twarp\ttre01\t1,4,treasure01,97,164\n'
                                'geffen,10,10,0\twarp\tboth_ends_not_lane\t1,1,prt_fild00,5,5\n'
                                '// iz_dun00,1,1,0\twarp\tcommented\t1,1,iz_dun01,1,1\n'
                                '//gef_dun02,215,67,0\twarp\tgefd03\t2,1,gef_dun03,203,200\n'),
            'npc/pre-re/mobs/dungeons/iz_dun.txt': ('iz_dun00,0,0\tmonster\tZombie\t1015,5,5000,2000\n'
                                                    'iz_dun01,0,0\tmonster\tSkeleton\t1076,3\n'),
            'npc/pre-re/mobs/dungeons/treasure.txt': 'treasure02,101,151,8,8\tboss_monster\tDrake\t1112,1,7200000,600000,1\n',
            'npc/pre-re/mobs/dungeons/gef_dun.txt': 'gef_dun02,0,0,0,0\tboss_monster\tBig Boss\t1999,1,7200000,600000,1\n',
            'db/pre-re/mob_db.yml': ('  - Id: 1015\n    AegisName: ZOMBIE\n    Name: Zombie\n    Level: 15\n    Hp: 534\n    Ai: 04\n    Element: Undead\n    ElementLevel: 1\n    Race: Undead\n    Size: Medium\n'
                                     '    Drops:\n      - Item: Jellopy\n        Rate: 7000\n'
                                     '  - Id: 1076\n    AegisName: SKELETON\n    Name: Skeleton\n    Level: 10\n    Hp: 234\n    Ai: 04\n    Element: Undead\n    Race: Undead\n'
                                     '  - Id: 1112\n    AegisName: DRAKE\n    Name: Drake\n    Hp: 326666\n    Ai: 21\n    Modes:\n      Mvp: true\n'
                                     '  - Id: 1999\n    AegisName: BIG_BOSS\n    Name: Big Boss\n    Hp: 900000\n    Ai: 21\n'),
            'db/pre-re/item_db_usable.yml': '', 'db/pre-re/item_db_equip.yml': '',
            'db/pre-re/item_db_etc.yml': '  - Id: 909\n    AegisName: Jellopy\n    Type: Etc\n    Buy: 6\n    Weight: 10\n',
        }
        for f, body in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
            open(os.path.join(d, f), 'w').write(body)

    def test_lane_warps_and_bridges(self):
        with tempfile.TemporaryDirectory() as d:
            self.tree(d)
            rows = T.lane_warps(d)
        self.assertEqual([(r[0], r[1], r[5]) for r in rows], [('izlu2dun', 108, 'iz_dun00'), ('iz_dun00', 168, 'izlu2dun'), ('treasure01', 112, 'treasure01')])   # gef_tower / commented / not-lane rows are dropped
        lines = T.warp_lines(rows)
        self.assertEqual(len([ln for ln in lines if ' bridge#' in ln]), 6)
        self.assertEqual(T.links_of(rows)['izlu2dun'], {'iz_dun00', 'izlude'})
        self.assertNotIn('treasure01', T.links_of(rows))                                 # a same-map warp is no link

    def test_mobs_and_the_boss_rule_on_a_synthetic_tree(self):
        with tempfile.TemporaryDirectory() as d:
            self.tree(d)
            ml, per, skipped, ids = T.mob_lines(d)
        self.assertEqual(ids, [1015, 1076, 1112])
        self.assertEqual(per, {'iz_dun00': 5, 'iz_dun01': 3, 'treasure02': 1})
        self.assertEqual(skipped, [('gef_dun02', 1999, 'boss HP 900000')])               # a boss above the HP cap that is no listed MVP is skipped, listed; Drake (326666 HP, ALLOW_BOSS) is kept
        txt = '\n'.join(ml)
        self.assertIn('spawn 1015 5 0 0 0 0 5000 2000 iz_dun00', txt)
        self.assertIn('spawn 1112 1 101 151 8 8 7200000 600000 treasure02', txt)
        self.assertIn('drop 1015 909 7000', txt)
        self.assertIn('item 909 0 6 10 0 0 0 Jellopy', txt)
        self.assertIn('mobel 1015 9 1 1 1', txt)

    def test_py_text_is_importable(self):
        src = T.py_text({'a': {'b'}}, {'iz_dun00': (1, 2, 3)})
        ns = {}
        exec(compile(src, 'maps3_data', 'exec'), ns)
        self.assertEqual(ns['LINKS'], {'a': ('b',)})
        self.assertEqual(ns['MEASURED'], {'iz_dun00': (1, 2, 3)})

    def test_committed_files_are_the_generator_output(self):
        committed = rd(ED, 'ro_maps3_warps.txt').splitlines()
        rows = [ln for ln in committed if ln and not ln.startswith('#')]
        self.assertEqual(rows[-len(maps3.BRIDGES):], ['%s %d %d %d %d %s %d %d %s' % b for b in maps3.BRIDGES])

    def test_flood_and_reach(self):
        walls = {(2, 0), (2, 1), (2, 2)}
        grid = (5, 3, lambda x, y: (x, y) not in walls)
        self.assertEqual(T.flood(grid, 0, 0), set((x, y) for x in (0, 1) for y in range(3)))        # a wall column splits the map
        self.assertEqual(len(T.flood(grid, 4, 1)), 6)
        self.assertEqual(T.flood(grid, 2, 1), set())                                      # a blocked start reaches nothing
        diag = (3, 3, lambda x, y: (x, y) in ((0, 0), (1, 1), (2, 2)))
        self.assertEqual(T.flood(diag, 0, 0), {(0, 0)})                                   # no corner cutting through blocked cells


@unittest.skipUnless(all(os.path.isfile(os.path.join(PACKS, 'ro__%s.pack' % m)) for m in list(maps3.NAMES) + list(maps3.TOWN_ENDS)), 'no converted maps3 packs on this machine (Mac only)')
class RealPacks(unittest.TestCase):
    def test_measured_equals_the_packs(self):
        for n in maps3.NAMES:
            d = T.tw.measure(os.path.join(PACKS, 'ro__%s.pack' % n))
            self.assertEqual((d['terr'], d['chunks'], int(round(d['mb'] * 10))), maps3.MEASURED[n], n)

    def test_warps_spawns_and_chains_against_the_packs(self):
        self.assertEqual(T.check(PACKS), 0)

    def test_the_treasure_ship_needs_its_same_map_warps(self):
        # the reason the Sunken Ship chain stops at treasure01 (docs): from the arrival cell the span of tre002 (-> treasure02) is NOT walkable on the GAT grid
        grid = T.m2.read_grid(os.path.join(PACKS, 'ro__treasure01.pack'))
        cells = T.flood(grid, 69, 24)
        self.assertTrue(cells)
        self.assertFalse(any((x, y) in cells for x in range(66, 73) for y in range(176, 179)))


@unittest.skipUnless(os.path.isdir(CLIENT), 'no owner client (Mac only)')
class RsmSurvey(unittest.TestCase):
    """every model of the lane's maps is RSM 1.x (the converter has no RSM2 parser: a client update that brings RSM2 into one of them fails here)"""
    def test_rsm_versions(self):
        from ro_conv.grf import Vfs
        from ro_conv import rofmt
        v = Vfs(CLIENT)
        bad, seen = [], {}
        for m in maps3.NAMES:
            rw = rofmt.rsw(v.read('data\\%s.rsw' % m))
            for fn in set(x['file'] for x in rw['models']):
                if fn not in seen:
                    d = v.read('data\\model\\' + fn)
                    seen[fn] = (d[4], d[5])
                if seen[fn][0] != 1:
                    bad.append((m, fn, seen[fn]))
        self.assertEqual(bad, [])
        self.assertGreater(len(seen), 50)


if __name__ == '__main__':
    unittest.main()
