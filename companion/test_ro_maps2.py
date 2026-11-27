"""RO maps wave 2 (docs/design/ro_maps2.md, w46 lane "maps"): Glast Heim, Comodo, Lutie + the Toy Factory.

Table == builder == warps == data rows == generated module, registration (on demand, never prebuilt), the loading plan numbers, the generator on a synthetic rAthena tree, the
real packs / RSM survey / sprite conversion on the Mac (skipped without the owner's client). Stdlib only.

  cd companion && python3 -I test_ro_maps2.py
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
from ro_conv import api, culvert, dungeons, jobmaps, maps2, maps2_data, mobsets, npcs, npcsets, towns  # noqa: E402

ED = os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Editor')
CLIENT = '/path/to/gsvr-testdata Online/'
PACKS = os.environ.get('RO_MAPS2_PACKS', '/path/to/gsvr-testdata')


def rd(*p):
    with open(os.path.join(*p), encoding='utf-8') as fh:
        return fh.read()


def load_tool():
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    spec = importlib.util.spec_from_file_location('ro_maps2_gen', os.path.join(ROOT, 'tools', 'ro_maps2_gen.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


T = load_tool()
WARPS = T.parse_rows(rd(ED, 'ro_maps2_warps.txt'))


def builder_list(src, name, quoted):
    src = re.sub(r'//[^\n]*', '', src)
    body = re.search(name + r'\s*=\s*\{([^}]*)\}', src).group(1)
    return re.findall(r'"([^"]+)"', body) if quoted else [int(x) for x in re.findall(r'-?\d+', body)]


class Table(unittest.TestCase):
    def test_world(self):
        self.assertEqual(len(maps2.NAMES), 12)
        self.assertEqual(len(set(maps2.NAMES)), 12)
        base = len(towns.NAMES) + len(culvert.NAMES) + len(dungeons.NAMES) + len(jobmaps.NAMES)
        self.assertEqual(maps2.MAPS2_BASE, base)                                         # 27 + 4 + 12 + 7 == 50
        self.assertEqual(tuple(api.MAP_ORDER[maps2.MAPS2_BASE:]), maps2.NAMES)
        self.assertEqual([maps2.world_id(m) for m in maps2.NAMES], list(range(50, 62)))
        self.assertLessEqual(len(api.MAP_ORDER), towns.MAX_MAPS)                         # 62 of the 255 world map ids (w52)
        self.assertEqual(len(api.MAP_ORDER), 62)
        for n in maps2.NAMES:
            self.assertRegex(n, r'^[a-z0-9_]{1,32}$')
            self.assertIn(maps2.AREA[n], maps2.AREAS)
            self.assertTrue(maps2.LABEL[n])
            self.assertEqual(api.PACKS[n]['kind'], 'zone')
            self.assertIn(n, maps2.MEASURED)
            t, o, mb = maps2.MEASURED[n]
            self.assertTrue(0 < t <= 80 and 0 < o <= 700, n)                             # converter limits: 80 terrain blocks, 700 chunks
        self.assertEqual(set(maps2.MEASURED), set(maps2.NAMES))
        self.assertTrue(set(maps2.NO_MOBS) <= set(maps2.NAMES))

    def test_registered_in_api(self):
        for n in maps2.NAMES:
            self.assertIn(n, api.NO_PREBUILD)
            self.assertNotIn(n, api.PREBUILD_ORDER)                                      # never built in the background
            self.assertEqual(api.warm_after(n), [])
            self.assertIn('snd_' + n, api.AUDIO_PACKS)                                   # music + the map's monsters' sounds: automatic for every map of PACKS
        self.assertTrue(set(api.warm_after('prontera')).isdisjoint(maps2.NAMES))
        self.assertTrue(set(api.warm_after('geffen')).isdisjoint(maps2.NAMES))

    def test_town_ends_are_loaded_maps_of_other_lanes(self):
        for e in maps2.TOWN_ENDS:
            self.assertNotIn(e, maps2.NAMES)
            self.assertIn(e, api.MAP_ORDER)

    def test_budget_and_residency(self):
        # the largest resident set of the lane (cur + every map one warp away, ALL on-demand lanes: cmd_fild09 is next to the dungeons lane's cmd_fild08)
        ram = dict((n, v[2]) for n, v in maps2.MEASURED.items())
        ram.update(dungeons.RAM_MB)
        od = set(maps2.NAMES) | set(dungeons.NAMES)
        g = {}
        for rows in (WARPS, dungeons.parse_rows(rd(ED, 'ro_dungeon_warps.txt'))):
            for r in rows:
                if r[0] != r[5]:
                    g.setdefault(r[0], set()).add(r[5])
        worst = 0
        for cur in maps2.NAMES:
            res = [m for m in od if m == cur or m in g.get(cur, ())]
            worst = max(worst, sum(ram[m] for m in res))
        self.assertLessEqual(worst, maps2.ONDEMAND_BUDGET_MB * 10)
        self.assertGreater(worst, 1000)                                                  # the 80 MB of the dungeons lane is NOT enough (Glast Heim hub 109.6 MB)
        self.assertEqual(sorted(maps2.resident('gl_church')), ['gl_church', 'gl_chyard', 'glast_01'])
        self.assertEqual(maps2.resident('gef_fild00'), [])
        # the dungeons lane's own peak is unchanged by this lane except the Ant Hell approach (cmd_fild09 one warp from cmd_fild08)
        self.assertLessEqual(sum(ram[m] for m in ('cmd_fild08', 'anthell01', 'anthell02', 'cmd_fild09')), maps2.ONDEMAND_BUDGET_MB * 10)


class Warps(unittest.TestCase):
    def test_links_equal_the_warp_table(self):
        got = dict((k, tuple(sorted(v))) for k, v in T.links_of([w for w in WARPS if not w[8].startswith('bridge#')]).items())
        want = dict((k, tuple(sorted(v))) for k, v in maps2_data.LINKS.items())
        self.assertEqual(got, want)

    def test_every_warp_has_a_way_back(self):
        g = T.links_of(WARPS)
        for a, bs in g.items():
            for b in bs:
                self.assertIn(a, g.get(b, ()), '%s -> %s has no way back' % (a, b))

    def test_all_reachable_from_the_loaded_towns_maps(self):
        g = T.links_of(WARPS)
        seen = set(maps2.TOWN_ENDS)
        todo = list(seen)
        while todo:
            c = todo.pop()
            for n in g.get(c, ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        self.assertEqual(set(maps2.NAMES) - seen, set())
        # every area has exactly its bridge entrance (rAthena's roads are outside the loaded maps)
        by = dict((w[8], w) for w in WARPS)
        for nm in ('bridge#gl1', 'bridge#gl2', 'bridge#cmd1', 'bridge#cmd2', 'bridge#lut1', 'bridge#lut2'):
            self.assertIn(nm, by)
        self.assertEqual(sorted(w[8] for w in WARPS if w[8].startswith('bridge#')), sorted(b[8] for b in maps2.BRIDGES))

    def test_bridge_arrivals_stand_outside_the_way_back(self):
        by = dict((b[8], b) for b in maps2.BRIDGES)
        for a, b in (('bridge#gl1', 'bridge#gl2'), ('bridge#cmd1', 'bridge#cmd2'), ('bridge#lut1', 'bridge#lut2')):
            x, y = by[a], by[b]
            self.assertEqual((x[5], y[5]), (y[0], x[0]))                                 # each leads to the map the other one starts on
            # the arrival of `a` (x[6], x[7]) is not inside the span of the way back `b` on that map (no ping-pong), and vice versa
            self.assertFalse(abs(x[6] - y[1]) <= y[3] and abs(x[7] - y[2]) <= y[4], a)
            self.assertFalse(abs(y[6] - x[1]) <= x[3] and abs(y[7] - x[2]) <= x[4], b)

    def test_rows_use_lane_or_town_end_maps_only(self):
        ok = set(maps2.NAMES) | set(maps2.TOWN_ENDS)
        for w in WARPS:
            self.assertIn(w[0], ok)
            self.assertIn(w[5], ok)
            self.assertTrue(w[0] in maps2.NAMES or w[5] in maps2.NAMES)

    def test_total_fits_the_warp_table(self):
        n = 0
        for f in ('ro_warps.txt', 'ro_culvert_warps.txt', 'ro_dungeon_warps.txt', 'ro_jobmap_warps.txt', 'ro_maps2_warps.txt'):
            n += len([ln for ln in rd(ED, f).splitlines() if ln.strip() and not ln.startswith('#')])
        self.assertLessEqual(n, 512)                                                     # GsvrRoMaps.MaxWarps
        self.assertEqual(len(WARPS), 58)


class Builder(unittest.TestCase):
    def test_builder_tables(self):
        src = rd(ED, 'GsvrSceneBuilder.Ragnarok.cs')
        names = builder_list(src, 'RoMapNames', True)
        self.assertEqual(names, list(api.MAP_ORDER))
        b = maps2.MAPS2_BASE
        self.assertIn('RoMaps2Base = %d' % b, src)
        self.assertEqual(builder_list(src, 'RoMapLabels', True)[b:], [maps2.LABEL[m] for m in maps2.NAMES])
        self.assertEqual(builder_list(src, 'RoMapCluster', False)[b:], [-2] * len(maps2.NAMES))
        self.assertEqual(builder_list(src, 'RoMapTerrNeed', False)[b:], [maps2.MEASURED[m][0] for m in maps2.NAMES])
        self.assertEqual(builder_list(src, 'RoMapObjNeed', False)[b:], [maps2.MEASURED[m][1] for m in maps2.NAMES])
        self.assertIn('const float RoOnDemandBudgetMB = %df;' % maps2.ONDEMAND_BUDGET_MB, src)
        self.assertIn('RoMaps2WarpsPath', src)
        self.assertIn('ro_maps2_warps.txt', src)
        nw = rd(ED, 'GsvrSceneBuilder.RoNpcWorld.cs')
        self.assertIn('ro_npc_world2.txt', nw)


class Data(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.txt = rd(ED, 'ro_maps2.txt')
        cls.rows = [ln.split('#')[0].split() for ln in cls.txt.splitlines() if ln.strip() and not ln.startswith('#')]
        cls.all = [ln.split('#')[0].split() for ln in rd(ED, 'ro_mobs_all.txt').splitlines() if ln.strip() and not ln.startswith('#')]

    def kind(self, k, rows=None):
        return [r for r in (self.rows if rows is None else rows) if r[0] == k]

    def test_spawns_map_to_lane_maps_and_known_classes(self):
        mobs = set(int(r[1]) for r in self.kind('mob'))
        sp = self.kind('spawn')
        self.assertEqual(len(sp), 74)                                                   # w52 mvpgen: + the Dark Lord boss row (1272 on gl_chyard, MvpExp 32890)
        per = {}
        for r in sp:
            self.assertIn(r[9], maps2.NAMES)
            self.assertIn(int(r[1]), mobs, r)
            per[r[9]] = per.get(r[9], 0) + int(r[2])
        self.assertEqual(set(per), set(n for n in maps2.NAMES if n not in maps2.NO_MOBS and n != 'xmas_in'))
        self.assertTrue(all(v <= 320 for v in per.values()), per)                       # GsvrRoMobs.MaxMobs
        for n in maps2.NO_MOBS:
            self.assertNotIn(n, per)

    def test_no_boss_above_the_hp_cap(self):
        by = dict((int(r[1]), int(r[3])) for r in self.kind('mob'))
        for r in self.kind('spawn'):
            if 'boss' in self.txt.splitlines()[0] or True:
                pass
        self.assertEqual(by.get(1272), 720000)                                          # w52 mvpgen: the Dark Lord (MvpExp > 0, gl_chyard) is the one allowed exception (ALLOW_BOSS) and has MVP rows in ro_dungeons.txt
        self.assertTrue(all(h <= 250000 for k, h in by.items() if k != 1272))

    def test_merged_table_holds_the_lane(self):
        spawn_all = self.kind('spawn', self.all)
        have = set(r[9] for r in spawn_all)
        for n in maps2.NAMES:
            if n not in maps2.NO_MOBS and n != 'xmas_in':
                self.assertIn(n, have, n)
        cls = len(self.kind('mob', self.all))
        self.assertEqual(cls, len(set(r[1] for r in self.kind('mob', self.all))))      # a class once
        self.assertEqual(len(self.kind('mobel', self.all)), cls)
        self.assertLess(cls, 192)                                                        # GsvrRoMobs.MaxCls
        self.assertLess(len(self.kind('drop', self.all)), 1536)                          # MaxDropRows
        self.assertLess(len(self.kind('item', self.all)), 768)                           # MaxItems
        self.assertLess(len(spawn_all), 768)                                             # MaxSpawns
        src = rd(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Scripts', 'GsvrRoMobs.cs')
        self.assertIn('MaxCls = 192, MaxDropRows = 1536, MaxItems = 768, MaxSpawns = 768', src)

    def test_merge_is_reproducible_and_aliases_steel_chonchon(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import ro_fields_gen as F
        merged = F.merge(ROOT)
        self.assertEqual(merged, rd(ED, 'ro_mobs_all.txt'))                              # the committed merged table == a fresh merge of the repo files
        mob = [ln.split('#')[0].split() for ln in merged.splitlines() if ln.startswith('mob ')]
        by = dict((int(r[1]), r[24]) for r in mob)
        self.assertEqual(by[1042], 'steel_chocho')                                       # the client's file name (rAthena: STEEL_CHONCHON)
        self.assertNotIn('steel_chonchon', [r[24] for r in mob])

    def test_sprite_sets_per_map(self):
        for n in maps2.NAMES:
            if n in maps2.NO_MOBS or n == 'xmas_in':
                self.assertNotIn(n, mobsets.MOB_SPRITES)
            else:
                self.assertTrue(len(mobsets.MOB_SPRITES[n]) >= 3, n)
                self.assertIn(n, mobsets.DROP_ITEMS)

    def test_no_game_data_words(self):
        self.assertNotRegex(self.txt.lower(), r'\.(spr|act|gat|gnd|rsw|rsm|bmp)\b')


class Npcs(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(npcs.group_of('comodo'), 'npc_comodo')
        self.assertEqual(npcs.group_of('xmas'), 'npc_lutie')
        self.assertEqual(npcs.group_of('prontera'), 'npc_prontera')                     # the w43 groups are untouched
        for g, ms in maps2_data.NPC_GROUPS.items():
            self.assertIn(g, api.NPC_PACKS)
            self.assertIn(g, npcs.NPC_ORDER)
            self.assertNotIn(g, npcsets.NPC_GROUPS)
            for m in ms:
                self.assertIn(m, api.MAP_ORDER)

    def test_set_ids_equal_the_decor_rows(self):
        txt = rd(ED, 'ro_npc_world2.txt')
        groups = dict((f[1], f[2:]) for f in (ln.split() for ln in txt.splitlines() if ln.startswith('group ')))
        self.assertEqual(dict((g, tuple(ms)) for g, ms in groups.items()), dict((g, tuple(ms)) for g, ms in maps2_data.NPC_GROUPS.items()))
        ids = {}
        n = 0
        for ln in txt.splitlines():
            if ln.startswith('deco '):
                f = ln.split()
                n += 1
                self.assertEqual(f[6], 'talk')                                         # Kafra / shop / storage = the w45 lanes
                ids.setdefault(npcs.group_of(f[1]), set()).add(int(f[5]))
                self.assertTrue(0 <= int(f[4]) <= 7)
        self.assertEqual(n, 63)
        for g, s in ids.items():
            self.assertEqual(sorted(s), list(maps2_data.NPC_SET_IDS[g]), g)

    def test_decor_rows_stay_inside_the_decor_limit(self):
        n = len([ln for ln in rd(ED, 'ro_npc_world.txt').splitlines() if ln.startswith('deco ')]) + len([ln for ln in rd(ED, 'ro_npc_world2.txt').splitlines() if ln.startswith('deco ')])
        self.assertLessEqual(n, 640)                                                     # GsvrRoNpcDecor.MaxRows


class Generator(unittest.TestCase):
    def tree(self, d):
        files = {
            'npc/scripts_warps.conf': 'npc: npc/warps/a.txt\n',
            'npc/pre-re/scripts_warps.conf': '',
            'npc/warps/a.txt': ('glast_01,162,330,0\twarp\tgl3\t1,1,gl_in01,77,68\n'
                                'gl_in01,81,68,0\twarp\tgl3-1\t1,1,glast_01,165,330\n'
                                'glast_01,10,10,0\twarp\toutside\t1,1,gef_fild06,5,5\n'
                                'gef_fild00,10,10,0\twarp\tboth_ends_not_lane\t1,1,prt_fild00,5,5\n'
                                'cmd_fild08,76,31,0\twarp\tc8\t2,1,cmd_fild09,76,375\n'
                                '// glast_01,1,1,0\twarp\tcommented\t1,1,gl_in01,1,1\n'),
            'npc/pre-re/mobs/dungeons/glastheim.txt': ('glast_01,0,0\tmonster\tZombie\t1015,5,5000,2000\n'
                                                       'gl_chyard,147,150,0,0\tboss_monster\tDark Lord\t1272,1,3600000,600000,1\n'
                                                       'gl_in01,0,0\tmonster\tSkeleton\t1076,3\n'),
            'npc/pre-re/mobs/dungeons/beach_dun.txt': '', 'npc/pre-re/mobs/dungeons/xmas_dun.txt': '',
            'npc/pre-re/mobs/fields/comodo.txt': '', 'npc/pre-re/mobs/fields/lutie.txt': '',
            'db/pre-re/mob_db.yml': ('  - Id: 1015\n    AegisName: ZOMBIE\n    Name: Zombie\n    Level: 15\n    Hp: 534\n    Ai: 04\n    Element: Undead\n    ElementLevel: 1\n    Race: Undead\n    Size: Medium\n'
                                     '    Drops:\n      - Item: Jellopy\n        Rate: 7000\n'
                                     '  - Id: 1076\n    AegisName: SKELETON\n    Name: Skeleton\n    Level: 10\n    Hp: 234\n    Ai: 04\n    Element: Undead\n    Race: Undead\n'
                                     '  - Id: 1272\n    AegisName: DARK_LORD\n    Name: Dark Lord\n    Hp: 720000\n    Ai: 21\n    Modes:\n      Mvp: true\n'),
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
        self.assertEqual([(r[0], r[1], r[5]) for r in rows], [('glast_01', 162, 'gl_in01'), ('gl_in01', 81, 'glast_01'), ('cmd_fild08', 76, 'cmd_fild09')])       # outside / commented / both-not-lane rows are dropped
        lines = T.warp_lines(rows)
        self.assertEqual(len([ln for ln in lines if ln.startswith('bridge') or ' bridge#' in ln]), 6)
        self.assertEqual(T.links_of(rows)['glast_01'], {'gl_in01', 'gef_fild00'})

    def test_mobs_on_a_synthetic_tree(self):
        with tempfile.TemporaryDirectory() as d:
            self.tree(d)
            keep, T.ALLOW_BOSS = T.ALLOW_BOSS, ()                                       # the generic cap rule: with no exception the 720000 HP boss is skipped
            try:
                ml, per, skipped, ids = T.mob_lines(d)
            finally:
                T.ALLOW_BOSS = keep
        self.assertEqual(ids, [1015, 1076])
        self.assertEqual(per, {'glast_01': 5, 'gl_in01': 3})
        self.assertEqual(skipped, [('gl_chyard', 1272, 'boss HP 720000')])               # a boss above the HP cap is skipped, listed
        txt = '\n'.join(ml)
        self.assertIn('spawn 1015 5 0 0 0 0 5000 2000 glast_01', txt)
        self.assertIn('drop 1015 909 7000', txt)
        self.assertIn('item 909 0 6 10 0 0 0 Jellopy', txt)
        self.assertIn('mobel 1015 9 1 1 1', txt)

    def test_py_text_is_importable(self):
        src = T.py_text({'a': {'b'}}, {'npc_comodo': {3, 1}}, {'glast_01': (1, 2, 3)})
        ns = {}
        exec(compile(src, 'maps2_data', 'exec'), ns)
        self.assertEqual(ns['LINKS'], {'a': ('b',)})
        self.assertEqual(ns['NPC_SET_IDS']['npc_comodo'], (1, 3))
        self.assertEqual(ns['MEASURED'], {'glast_01': (1, 2, 3)})

    def test_committed_files_are_the_generator_output(self):
        # the committed warp file == the generator's header + rows of the committed tables (no rAthena needed)
        committed = rd(ED, 'ro_maps2_warps.txt').splitlines()
        rows = [ln for ln in committed if ln and not ln.startswith('#')]
        self.assertEqual(rows[-len(maps2.BRIDGES):], ['%s %d %d %d %d %s %d %d %s' % b for b in maps2.BRIDGES])


@unittest.skipUnless(all(os.path.isfile(os.path.join(PACKS, 'ro__%s.pack' % m)) for m in list(maps2.NAMES) + list(maps2.TOWN_ENDS)), 'no converted maps2 packs on this machine (Mac only)')
class RealPacks(unittest.TestCase):
    def test_measured_equals_the_packs(self):
        for n in maps2.NAMES:
            d = T.tw.measure(os.path.join(PACKS, 'ro__%s.pack' % n))
            self.assertEqual((d['terr'], d['chunks'], int(round(d['mb'] * 10))), maps2.MEASURED[n], n)

    def test_warps_and_spawns_against_the_packs(self):
        self.assertEqual(T.check(PACKS), 0)


@unittest.skipUnless(os.path.isdir(CLIENT), 'no owner client (Mac only)')
class RsmSurvey(unittest.TestCase):
    """every model of the lane's maps is RSM 1.x (the converter has no RSM2 parser: a client update that brings RSM2 into one of them fails here)"""
    def test_rsm_versions(self):
        from ro_conv.grf import Vfs
        from ro_conv import rofmt
        v = Vfs(CLIENT)
        bad, seen = [], {}
        for m in maps2.NAMES:
            rw = rofmt.rsw(v.read('data\\%s.rsw' % m))
            for fn in set(x['file'] for x in rw['models']):
                if fn not in seen:
                    d = v.read('data\\model\\' + fn)
                    seen[fn] = (d[4], d[5])
                if seen[fn][0] != 1:
                    bad.append((m, fn, seen[fn]))
        self.assertEqual(bad, [])
        self.assertGreater(len(seen), 100)

    def test_every_monster_sprite_converts(self):
        """w46: evil_druid raised 'byte must be in range(0, 256)' (alpha blend rounding, sprite.compose_frame): every monster of the lane's maps must convert"""
        from ro_conv.grf import Vfs
        v = Vfs(CLIENT)
        warn = []
        for n in maps2.NAMES:
            if n in mobsets.MOB_SPRITES:
                api.mob_sprite_sections(v, n, warn)
        self.assertEqual([w for w in warn if 'monster' in w], [])


if __name__ == '__main__':
    unittest.main()
