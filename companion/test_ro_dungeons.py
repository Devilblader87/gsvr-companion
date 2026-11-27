"""RO dungeons lane (docs/design/ro_dungeons.md, w37): table == C# == warps == data rows, the loading plan, the generator on synthetic rAthena text, the pack checker, the real
packs / RSM survey on the Mac (skipped without the owner's client). Stdlib only.

  cd companion && python3 -I test_ro_dungeons.py
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
from ro_conv import api, dungeons  # noqa: E402

ED = os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Editor')
CS = os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Scripts', 'GsvrRoDungeon.cs')
CLIENT = '/path/to/gsvr-testdata Online/'
PACKS = os.environ.get('RO_DUNGEON_PACKS', '/path/to/gsvr-testdata')


def rd(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


def load_tool():
    spec = importlib.util.spec_from_file_location('ro_dungeons_data', os.path.join(ROOT, 'tools', 'ro_dungeons_data.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


T = load_tool()
WARPS = dungeons.parse_rows(rd(os.path.join(ED, 'ro_dungeon_warps.txt')))


class Table(unittest.TestCase):
    def test_world(self):
        self.assertEqual(len(dungeons.NAMES), 12)
        self.assertEqual(len(set(dungeons.NAMES)), 12)
        for n in dungeons.NAMES:
            self.assertIn(n, dungeons.LABEL)
            self.assertIn(dungeons.CLUSTER[n], dungeons.CLUSTERS)
            self.assertIn(n, dungeons.RAM_MB)
        self.assertEqual(set(dungeons.RAM_MB), set(dungeons.NAMES))

    def test_registered_in_api(self):
        for n in dungeons.NAMES:
            self.assertEqual(api.PACKS[n]['kind'], 'zone')
            self.assertIn(n, api.MAP_ORDER)
            self.assertIn(n, api.NO_PREBUILD)
            self.assertNotIn(n, api.PREBUILD_ORDER)               # never built in the background
        self.assertEqual(api.warm_after('prontera') and [m for m in api.warm_after('prontera') if m in dungeons.NAMES], [])
        self.assertEqual(api.warm_after('pay_dun00'), [])
        self.assertTrue(frozenset(dungeons.NAMES) <= api.NO_PREBUILD)          # w41: + the job-test maps (test_ro_jobs1.py)

    def test_links_equal_the_warp_table(self):
        g = dungeons.links(WARPS)
        got = dict((k, tuple(sorted(v))) for k, v in g.items())
        want = dict((k, tuple(sorted(v))) for k, v in dungeons.LINKS.items())
        self.assertEqual(got, want)

    def test_every_warp_has_a_way_back_except_the_known_one_way(self):
        g = dungeons.links(WARPS)
        for a, bs in g.items():
            for b in bs:
                self.assertIn(a, g.get(b, ()), '%s -> %s has no way back' % (a, b))

    def test_all_reachable_from_the_town_ends(self):
        g = dungeons.links(WARPS)
        seen = set(dungeons.TOWN_ENDS)
        todo = list(seen)
        while todo:
            c = todo.pop()
            for n in g.get(c, ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        self.assertEqual(set(dungeons.NAMES) - seen, set())

    def test_every_town_end_is_one_of_the_towns_lane_maps(self):
        for e in dungeons.TOWN_ENDS:
            self.assertNotIn(e, dungeons.NAMES)

    def test_residency_and_budget(self):
        worst = max(dungeons.resident_mb10(m) for m in dungeons.NAMES)
        self.assertLessEqual(worst, dungeons.DUNGEON_BUDGET_MB * 10)
        self.assertGreaterEqual(worst, 400)
        self.assertTrue(dungeons.over_budget(set(dungeons.NAMES)))              # the budget is real: all 12 maps do not fit
        self.assertFalse(dungeons.over_budget({'pay_dun00', 'pay_dun01'}))
        self.assertEqual(dungeons.resident('pay_dun02'), ['pay_dun01', 'pay_dun02', 'pay_dun03'])
        self.assertEqual(dungeons.resident('pay_arche'), [])
        loaded = {'pay_dun00', 'pay_dun01', 'pay_dun02', 'pay_dun03', 'pay_dun04', 'cmd_fild08'}
        self.assertEqual(dungeons.evict_pick(loaded, 'pay_dun04', {'pay_dun00': 1, 'pay_dun01': 2, 'pay_dun02': 3, 'cmd_fild08': 0}), 'cmd_fild08')
        self.assertIsNone(dungeons.evict_pick({'pay_dun03', 'pay_dun04'}, 'pay_dun04', {}))

    def test_mvp_table(self):
        self.assertEqual([m[0] for m in dungeons.MVPS], [1150, 1147])
        for _id, name, mp, x, y, d1, d2 in dungeons.MVPS:
            self.assertIn(mp, dungeons.NAMES)
        self.assertEqual(dungeons.MVPS[0][2:], ('pay_dun04', 120, 115, 3600000, 600000))
        self.assertEqual(dungeons.MVPS[1][2], 'anthell02')
        self.assertNotIn(1190, [m[0] for m in dungeons.MVPS])                    # Orc Lord is a field boss, not a dungeon MVP


class CsTable(unittest.TestCase):
    """GsvrRoDungeon.cs repeats the table (Udon has no static data): it must equal ro_conv.dungeons"""
    @classmethod
    def setUpClass(cls):
        cls.src = rd(CS)

    def cases(self, fn):
        body = self.src[self.src.index('public static int %s(' % fn):]
        body = body[:body.index('\n    }\n')]
        return dict((int(a), b.strip()) for a, b in re.findall(r'case (\d+): return ([^;]+);', body))

    def test_ram(self):
        c = self.cases('LRamMb10')
        self.assertEqual([int(c[i]) for i in range(12)], [dungeons.RAM_MB[n] for n in dungeons.NAMES])

    def test_link_masks(self):
        c = self.cases('LLinkMask')
        ids = dict((n, i) for i, n in enumerate(dungeons.NAMES))
        ids.update({'pay_arche': 12, 'gef_fild00': 13, 'moc_fild20': 14})
        for name, i in ids.items():
            want = 0
            for t in dungeons.LINKS.get(name, ()):
                want |= 1 << ids[t]
            expr = c[i]
            got = 0
            for b in re.findall(r'1 << (\d+)', expr):
                got |= 1 << int(b)
            self.assertEqual(got, want, name)

    def test_constants(self):
        self.assertIn('BudgetMb10 = %d' % (dungeons.DUNGEON_BUDGET_MB * 10), self.src)
        self.assertIn('LaneN = %d' % len(dungeons.NAMES), self.src)
        ids = re.search(r'PayDun00 = 0, PayDun01 = 1, PayDun02 = 2, PayDun03 = 3, PayDun04 = 4, CmdFild08 = 5, AntHell01 = 6, AntHell02 = 7, GefFild10 = 8, InOrcs01 = 9, OrcsDun01 = 10, OrcsDun02 = 11', self.src)
        self.assertTrue(ids)
        self.assertEqual(list(dungeons.NAMES), ['pay_dun00', 'pay_dun01', 'pay_dun02', 'pay_dun03', 'pay_dun04', 'cmd_fild08', 'anthell01', 'anthell02', 'gef_fild10', 'in_orcs01', 'orcsdun01', 'orcsdun02'])

    def test_payd04(self):
        xs = re.search(r'LPayd04X.*?case 0: return (\d+); case 1: return (\d+); case 2: return (\d+); \}\s*return (\d+);', self.src, re.S).groups()
        ys = re.search(r'LPayd04Y.*?case 0: return (\d+); case 1: return (\d+); case 2: return (\d+); \}\s*return (\d+);', self.src, re.S).groups()
        self.assertEqual(tuple(zip(map(int, xs), map(int, ys))), dungeons.PAYD04_ARRIVALS)


class Data(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.txt = rd(os.path.join(ED, 'ro_dungeons.txt'))
        cls.rows = [ln.split('#')[0].split() for ln in cls.txt.splitlines() if ln.strip() and not ln.startswith('#')]

    def kind(self, k):
        return [r for r in self.rows if r[0] == k]

    def test_spawn_maps_and_mobs(self):
        mobs = set(int(r[1]) for r in self.kind('mob'))
        for r in self.kind('spawn'):
            self.assertIn(r[1], dungeons.NAMES)
            self.assertIn(int(r[6]), mobs, r)
        self.assertGreater(len(self.kind('spawn')), 200)
        self.assertEqual(set(r[1] for r in self.kind('spawn')), set(n for n in dungeons.NAMES if n in ('pay_dun00', 'pay_dun01', 'pay_dun02', 'pay_dun03', 'pay_dun04', 'anthell01', 'anthell02', 'orcsdun01', 'orcsdun02')))

    def test_mvp_rows(self):
        mv = self.kind('mvp')
        self.assertEqual([(int(r[1]), r[2], int(r[5]), int(r[6])) for r in mv], [(1150, 'pay_dun04', 3600000, 600000), (1147, 'anthell02', 7200000, 600000), (1086, 'prt_sewb4', 3600000, 600000), (1190, 'gef_fild10', 7200000, 600000), (1272, 'gl_chyard', 3600000, 600000), (1583, 'beach_dun', 18000000, 600000), (1251, 'xmas_dun02', 3600000, 600000), (1112, 'treasure02', 7200000, 600000), (1389, 'gef_dun01', 3600000, 600000), (1046, 'gef_dun02', 7200000, 600000)])  # w52 mvpgen: rows for all MvpExp > 0 bosses; v11 dungeons3 adds Drake (treasure02), Dracula (gef_dun01), Doppelganger (gef_dun02) = 10 MVP rows
        for r in mv:
            mid = r[1]
            self.assertTrue([x for x in self.kind('mvpexp') if x[1] == mid], mid)
            self.assertIn(len([x for x in self.kind('mvpdrop') if x[1] == mid]), (1, 2, 3), mid)  # w52 mvpgen: the extra bosses have 1-3 MVP drops
            rows = [x for x in self.kind('mob') if x[1] == mid]  # the w52 extra bosses live in the main mob table, not in this dungeon one
            if rows:
                self.assertTrue(int(rows[0][23]) & 0x80000, 'MVP mode bit')

    def test_mob_rows_are_the_mobs_table_shape(self):
        for r in self.kind('mob'):
            self.assertGreaterEqual(len(r), 25)
            self.assertEqual([x.isdigit() for x in r[1:24]], [True] * 23, r[:5])

    def test_no_game_data_words(self):
        self.assertNotRegex(self.txt.lower(), r'\.(spr|act|gat|gnd|rsw|rsm|bmp)\b')


class Generator(unittest.TestCase):
    SPAWN = ('pay_dun00,0,0\tmonster\tZombie\t1015,20\n'
             'pay_dun00,140,140,5,5\tmonster\tBlack Mushroom\t1084,3,360000,180000\n'
             'pay_dun04,120,115,0,0\tboss_monster\tMoonlight Flower\t1150,1,3600000,600000,1\n'
             'prt_fild08,0,0\tmonster\tPoring\t1002,5\n'
             '// pay_dun01,0,0\tmonster\tX\t1,1\n')

    def test_parse_spawns(self):
        r = T.parse_spawns(self.SPAWN)
        self.assertEqual([x[:2] for x in r], [('pay_dun00', 0), ('pay_dun00', 140), ('pay_dun04', 120)])
        self.assertEqual(r[1][3:5], (5, 5))
        self.assertEqual(r[1][8:10], (360000, 180000))
        self.assertEqual(r[2][5], 'boss_monster')
        self.assertEqual(r[2][8:10], (3600000, 600000))

    def test_parse_warps(self):
        w = T.parse_warps('pay_dun00,184,33,0\twarp\tpayd01\t2,7,pay_dun01,19,33\nin_orcs01,1,2,0\twarp2\tx\t1,1,gef_fild10,5,6\nfoo bar\n')
        self.assertEqual(w[0], ('pay_dun00', 184, 33, 2, 7, 'pay_dun01', 19, 33, 'payd01'))
        self.assertEqual(len(w), 2)

    def test_yaml_reader_and_mode(self):
        y = ('  - Id: 1150\n    AegisName: MOONLIGHT\n    Name: Moonlight Flower\n    Level: 67\n    Hp: 120000\n    Ai: 21\n    Modes:\n      Mvp: true\n'
             '    MvpDrops:\n      - Item: Fox_Tail\n        Rate: 5000\n    Drops:\n      - Item: Elunium\n        Rate: 2600\n'
             '  - Id: 1002\n    AegisName: PORING\n    Name: Poring\n    Ai: 02\n')
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'm.yml')
            open(p, 'w').write(y)
            e = T.yaml_entries(p)
        self.assertEqual([x['Id'] for x in e], [1150, 1002])
        self.assertEqual(e[0]['MvpDrops'], [{'Item': 'Fox_Tail', 'Rate': '5000'}])
        self.assertEqual(e[0]['Drops'][0]['Item'], 'Elunium')
        self.assertTrue(T.mob_row(e[0], {}).startswith('mob 1150 67 120000 '))
        self.assertEqual(T.mob_row(e[0], {}).split()[23], str(0x3695 | 0x80000))       # Ai 21 + MVP bit
        self.assertEqual(T.mob_row(e[1], {}).split()[23], str(0x83))                    # Ai 02

    def test_build_on_a_synthetic_tree(self):
        with tempfile.TemporaryDirectory() as d:
            files = [('npc/pre-re/mobs/dungeons/pay_dun.txt', self.SPAWN), ('npc/pre-re/mobs/dungeons/anthell.txt', ''), ('npc/pre-re/mobs/dungeons/orcsdun.txt', ''),
                     ('npc/warps/dungeons/pay_dun.txt', 'pay_dun00,184,33,0\twarp\tpayd01\t2,7,pay_dun01,19,33\n')] + [(f, '') for f in T.WARP_FILES[1:]]
            files[3] = (files[3][0], files[3][1] + 'pay_arche,36,131,0\twarp\tx\t2,2,pay_dun00,21,183\n')
            for f, body in files:
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                open(os.path.join(d, f), 'w').write(body)
            db = os.path.join(d, 'db')
            os.makedirs(db)
            open(os.path.join(db, 'db_pre-re_mob_db.yml'), 'w').write(
                '  - Id: 1015\n    AegisName: ZOMBIE\n    Name: Zombie\n    Ai: 04\n  - Id: 1084\n    AegisName: BLACK_MUSHROOM\n    Name: Black Mushroom\n    Ai: 06\n'
                '  - Id: 1150\n    AegisName: MOONLIGHT\n    Name: Moonlight Flower\n    Ai: 21\n    MvpExp: 100\n    MvpDrops:\n      - Item: Fox_Tail\n        Rate: 5000\n')
            open(os.path.join(db, 'db_pre-re_item_db_etc.yml'), 'w').write('  - Id: 1022\n    AegisName: Fox_Tail\n')
            out, warps, miss = T.build(d, db)
        self.assertEqual(miss, [])
        self.assertIn('mvp 1150 pay_dun04 120 115 3600000 600000   # Moonlight Flower', out)
        self.assertIn('mvpdrop 1150 1022 5000   # Fox_Tail', out)
        self.assertIn('mvpexp 1150 100', out)
        self.assertIn('spawn pay_dun00 140 140 5 5 1084 3 360000 180000   # Black Mushroom', out)
        self.assertFalse([x for x in out if '1190' in x])                                # Orc Lord (a field boss of gef_fild10) is not this lane's dungeon MVP
        self.assertEqual(len(T.warp_lines(warps)) - 4, 2 + len(dungeons.BRIDGES))             # the dungeon row + the pay_arche entrance + the bridges

    def test_missing_mob_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            for f in T.SPAWN_FILES + T.WARP_FILES:
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                open(os.path.join(d, f), 'w').write(self.SPAWN if f == T.SPAWN_FILES[0] else '')
            open(os.path.join(d, 'db_pre-re_mob_db.yml'), 'w').write('  - Id: 1015\n    AegisName: ZOMBIE\n    Name: Zombie\n')
            _o, _w, miss = T.build(d, d)
        self.assertEqual(miss, [1084, 1150])


class Checker(unittest.TestCase):
    """the pack checker on synthetic packs: a good table passes, a bad cell fails"""
    def make(self, d, w=20, h=20, blocked=()):
        import rpg1 as P
        cells = bytearray([1] * (w * h))
        for (x, y) in blocked:
            cells[y * w + x] = 0
        return cells

    def test_check_detects_a_blocked_target(self):
        grids = {}
        for n in dungeons.NAMES + dungeons.TOWN_ENDS:
            grids[n] = (400, 400, lambda x, y: True)
        orig = T.read_grid
        T.read_grid = lambda p: grids[os.path.basename(p)[4:-5]]
        with tempfile.TemporaryDirectory() as d:
            for n in grids:
                open(os.path.join(d, 'ro__%s.pack' % n), 'wb').write(b'x')
            try:
                self.assertEqual(T.check(d), 0)
                grids['pay_dun01'] = (400, 400, lambda x, y: not (x == 19 and y == 33))           # the target of payd01
                self.assertEqual(T.check(d), 1)
            finally:
                T.read_grid = orig


@unittest.skipUnless(os.path.isfile(os.path.join(PACKS, 'ro__pay_dun00.pack')), 'no converted dungeon packs (Mac only)')
class RealPacks(unittest.TestCase):
    def test_check_real(self):
        self.assertEqual(T.check(PACKS), 0)

    def test_pack_sizes_and_ram(self):
        import rpg1 as P
        for n in dungeons.NAMES:
            buf = open(os.path.join(PACKS, 'ro__%s.pack' % n), 'rb').read()
            self.assertLess(len(buf), 25 * 1000 * 1000, n)
            s = P.read_pack(buf)['sections']
            d = s[b'TERR']['data']
            tv = tt = 0
            nn = P.TERR_HDR.unpack_from(d, 0)[0]
            for i in range(nn):
                r = P.TERR_REC.unpack_from(d, P.TERR_HDR.size + i * P.TERR_REC.size)
                tv += r[2]
                tt += r[3]
            d = s[b'OMSH']['data']
            ov = ot = 0
            n2 = P.OMSH_HDR.unpack_from(d, 0)[0]
            for i in range(n2):
                r = P.OMSH_REC.unpack_from(d, P.OMSH_HDR.size + i * P.OMSH_REC.size)
                ov += r[3]
                ot += r[4] // 3
            tex = sum(len(s[t]['data']) for t in (b'TATL', b'OTEX', b'TLMP') if t in s)
            mb10 = int(round(((tv + ov) * 88.0 + (tt + ot) * 24.0 + tex) / 1048576.0 * 10))
            self.assertLessEqual(abs(mb10 - dungeons.RAM_MB[n]), 1, '%s RAM table %d vs pack %d' % (n, dungeons.RAM_MB[n], mb10))


@unittest.skipUnless(os.path.isdir(CLIENT), 'no owner client (Mac only)')
class RsmSurvey(unittest.TestCase):
    """every model of the lane's maps is RSM 1.x (the converter has no RSM2 parser: a client update that brings RSM2 into one of them fails here)"""
    def test_rsm_versions(self):
        from ro_conv.grf import Vfs
        from ro_conv import rofmt
        v = Vfs(CLIENT)
        bad, seen = [], {}
        for m in dungeons.NAMES:
            rw = rofmt.rsw(v.read('data\\%s.rsw' % m))
            for fn in set(x['file'] for x in rw['models']):
                if fn not in seen:
                    d = v.read('data\\model\\' + fn)
                    seen[fn] = (d[4], d[5])
                if seen[fn][0] != 1:
                    bad.append((m, fn, seen[fn]))
        self.assertEqual(bad, [])
        self.assertGreater(len(seen), 100)


if __name__ == '__main__':
    unittest.main()
