"""RO items (docs/design/ro_items.md), converter + data side, on SYNTHETIC files only (made-up pixels / tables, nothing of the game):
  - ro_conv/lub.py reads compiled Lua 5.1 tables (built here byte by byte) and refuses anything else,
  - tools/ro_itemdb.py: rAthena-style YAML -> the numbers-only rows (job / class masks, location bits, scripts -> effect tokens, complex flag), no names, ascending ids,
  - ro_conv/items.py: headgear / weapon / shield / icon sprites of a synthetic client land in the start pack under hg_ / wp_ / sh_ / ic_ names, with the right clip layout,
  - the generated ro_items.txt / itemview.py are consistent, and (Mac only, skipped without the owner's client) the real client resolves every gear item of GEAR_ITEMS.

  cd companion && python3 -I test_ro_items.py
"""
import os
import struct
import sys
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from ro_conv import api, grf, img, items, lub, sprite as S  # noqa: E402
from ro_conv.itemview import ITEM_VIEW  # noqa: E402
import ro_itemdb as DB  # noqa: E402
import test_ro_e2e as E2E  # noqa: E402
import test_ro_sprite as RS  # noqa: E402

REAL_CLIENT = os.environ.get("GSVR_RO_CLIENT", "/path/to/gsvr-testdata/ro_data/Ragnarok Online")
RA_SRC = os.environ.get("GSVR_RA_SRC", "/path/to/gsvr-testdata/w34/ra_src")
DATA = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR", "Editor", "ro_items.txt")


# ---------------------------------------------------------------- a tiny Lua 5.1 chunk writer (the same layout the client's *.lub files have)
def _s(b):
    b = b.encode('cp949') if isinstance(b, str) else b
    return struct.pack('<I', len(b) + 1) + b + b'\0'


def _num(v):
    return b'\x03' + struct.pack('<d', v)


def _ins(op, a=0, b=0, c=0, bx=None):
    if bx is not None:
        return struct.pack('<I', op | (a << 6) | (bx << 14))
    return struct.pack('<I', op | (a << 6) | (c << 14) | (b << 23))


def lua_chunk(code, consts):
    """code = [bytes instr], consts = [str | number]; one main function, no nested functions / debug info."""
    f = _s('=t') + struct.pack('<II', 0, 0) + bytes((0, 0, 0, 8))
    f += struct.pack('<I', len(code)) + b''.join(code)
    f += struct.pack('<I', len(consts))
    for c in consts:
        f += _num(c) if isinstance(c, (int, float)) else b'\x04' + _s(c)
    f += struct.pack('<IIII', 0, 0, 0, 0)
    return b'\x1bLuaQ\x00\x01\x04\x04\x04\x08\x00' + f


def table_lub(name, pairs):
    """NAME = { k = v, ... } with constant keys / values (SETTABLE R0 K K), like AccessoryId.lub / WeaponTable.lub."""
    consts = []

    def k(x):
        if x not in consts:
            consts.append(x)
        return 256 + consts.index(x)
    code = [_ins(10, 0)]
    for key, val in pairs:
        code.append(_ins(9, 0, k(key), k(val)))
    consts.append(name)
    code.append(_ins(7, 0, bx=len(consts) - 1))
    return lua_chunk(code, consts)


def accname_lub(ids_table, pairs):
    """AccNameTable = { [ACCESSORY_IDs.NAME] = '_suffix' }: GETGLOBAL ids, GETTABLE with the constant name, SETTABLE."""
    consts = [ids_table]
    code = [_ins(10, 0)]
    for nm, sfx in pairs:
        for c in (nm, sfx):
            if c not in consts:
                consts.append(c)
        code.append(_ins(5, 1, bx=0))
        code.append(_ins(6, 2, 1, 256 + consts.index(nm)))
        code.append(_ins(9, 0, 2, 256 + consts.index(sfx)))
    consts.append('AccNameTable')
    code.append(_ins(7, 0, bx=len(consts) - 1))
    return lua_chunk(code, consts)


class Lub(unittest.TestCase):
    def test_table(self):
        r = lub.parse(table_lub('Weapon_IDs', [('WEAPONTYPE_NONE', 0), ('WEAPONTYPE_SWORD', 2), ('WEAPONTYPE_BOW', 11)]))
        self.assertEqual(r, [('Weapon_IDs', 'WEAPONTYPE_NONE', 0), ('Weapon_IDs', 'WEAPONTYPE_SWORD', 2), ('Weapon_IDs', 'WEAPONTYPE_BOW', 11)])

    def test_korean_string_and_gettable(self):
        r = lub.parse(accname_lub('ACCESSORY_IDs', [('ACCESSORY_GOGGLES', '_고글')]))
        self.assertEqual(r, [('AccNameTable', ('gt', 'ACCESSORY_IDs', 'ACCESSORY_GOGGLES'), '_고글')])

    def test_flat_last_wins(self):
        self.assertEqual(lub.flat(table_lub('T', [('a', 1), ('a', 2), ('b', 3)])), {'a': 2, 'b': 3})

    def test_refuses_garbage(self):
        for bad in (b'', b'not lua at all', b'\x1bLuaR\x00\x01\x04\x04\x04\x08\x00', b'\x1bLuaQ\x00\x01\x04\x08\x04\x08\x00' + b'\0' * 40):
            with self.assertRaises(lub.LubError):
                lub.parse(bad)

    def test_truncated(self):
        good = table_lub('T', [('a', 1), ('b', 2)])
        for n in (14, 30, len(good) - 5):
            with self.assertRaises((lub.LubError, struct.error)):
                lub.parse(good[:n])


# ---------------------------------------------------------------- the item database generator
YAML = """# header
Header:
  Type: ITEM_DB
  Version: 3

Body:
  - Id: 501
    AegisName: Red_Potion
    Name: Red Potion
    Type: Healing
    Buy: 50
    Weight: 70
    Script: |
      itemheal rand(45,65),0;
  - Id: 1201
    AegisName: Knife
    Name: Knife
    Type: Weapon
    SubType: Dagger
    Buy: 50
    Weight: 400
    Attack: 17
    Range: 1
    Slots: 3
    Jobs:
      All: true
      Knight: false
    Locations:
      Right_Hand: true
    WeaponLevel: 1
    Refineable: true
  - Id: 2101
    AegisName: Guard
    Name: Guard
    Type: Armor
    Buy: 500
    Defense: 3
    Gender: Female
    Locations:
      Left_Hand: true
    EquipLevelMin: 2
    View: 1
  - Id: 5005
    AegisName: Hat
    Name: Hat
    Type: Armor
    Buy: 100
    Defense: 3
    Locations:
      Head_Mid: true
      Head_Low: true
    View: 91
    Script: |
      bonus bStr,2;
      bonus2 bAddRace,RC_Demon,5;
  - Id: 601
    AegisName: Wing
    Name: Fly Wing
    Type: Delayconsume
    Buy: 60
    Script: |
      itemskill "AL_TELEPORT",1;
  - Id: 4001
    AegisName: Poring_Card
    Name: Poring Card
    Type: Card
    Buy: 20
    Locations:
      Armor: true
    Script: |
      bonus bLuk,2;
      bonus bFlee2,1;
  - Id: 2900
    AegisName: Cond
    Name: Cond
    Type: Armor
    Locations:
      Armor: true
    Classes:
      Upper: true
    Script: |
      if (getrefine() > 6) bonus bAtk,10;
  - Id: 700
    AegisName: Both
    Name: Both
    Type: Armor
    Locations:
      Both_Accessory: true
"""


class Itemdb(unittest.TestCase):
    def setUp(self):
        self.items = DB.parse_yaml(YAML)
        self.rows = dict(DB.row(i) for i in self.items)

    def test_parse(self):
        self.assertEqual(len(self.items), 8)
        self.assertEqual(self.items[1]['Jobs'], {'All': 'true', 'Knight': 'false'})
        self.assertIn('itemheal rand(45,65),0;', self.items[0]['Script'])

    def f(self, iid):
        return self.rows[iid].split(' ')

    def test_numbers(self):
        p = self.f(1201)
        self.assertEqual(len(p), 22)
        self.assertEqual((p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[9], p[10]), ('1201', '4', '1', '50', '25', '400', '17', '1', '3'))
        self.assertEqual(p[14], '2')             # Right_Hand
        self.assertEqual(p[15], '1')             # weapon level 1
        self.assertEqual(int(p[19]) & 1, 1)      # refineable

    def test_job_mask(self):
        p = self.f(1201)
        self.assertEqual(int(p[11]), DB.ALL_JOBS & ~(1 << DB.JOBS.index('Knight')))
        self.assertEqual(int(self.f(501)[11]), DB.ALL_JOBS)       # no Jobs = everyone

    def test_gender_level_loc(self):
        p = self.f(2101)
        self.assertEqual((p[13], p[14], p[17], p[20]), ('0', '32', '2', '1'))

    def test_headgear_spans(self):
        p = self.f(5005)
        self.assertEqual((p[14], p[20]), (str(512 | 1), '91'))

    def test_both_accessory_loc(self):
        self.assertEqual(self.f(700)[14], '136')

    def test_scripts(self):
        self.assertEqual(self.f(501)[21], 'itemheal=45:65:0:0')
        self.assertEqual(self.f(601)[21], 'itemskill=AL_TELEPORT:1')
        self.assertEqual(self.f(4001)[21], 'bLuk=2;bFlee2=1')
        self.assertEqual(self.f(5005)[21], 'bStr=2;bAddRace.RC_Demon=5')
        self.assertEqual(int(self.f(5005)[19]) & 2, 0)             # all literal: not complex
        self.assertEqual(int(self.f(2900)[19]) & 2, 2)             # an `if` marks the script complex
        self.assertEqual(self.f(2900)[12], str(2))                 # Classes: Upper only

    def test_no_names(self):
        text = DB.HEADER + '\n'.join(self.rows[k] for k in sorted(self.rows))
        for w in ('Red Potion', 'Knife', 'Guard', 'Red_Potion', 'Poring', 'Fly Wing', 'AegisName'):
            self.assertNotIn(w, text.replace('Header', ''))

    def test_check_file_catches_problems(self):
        d = tempfile.mkdtemp()
        try:
            good = os.path.join(d, 'g.txt')
            lines = [self.rows[k] for k in sorted(self.rows)]
            open(good, 'w').write('\n'.join(lines * 1) + '\n')
            n, probs = DB.check_file(good)
            self.assertEqual((n, probs), (len(lines), []))
            bad = os.path.join(d, 'b.txt')
            open(bad, 'w').write(lines[1] + '\n' + lines[0] + '\n' + lines[2].rsplit(' ', 1)[0] + '\n' + 'x ' * 21 + '-\n')
            n, probs = DB.check_file(bad)
            self.assertTrue(any('ascending' in p for p in probs) and any('fields' in p for p in probs) and any('not a number' in p for p in probs), probs)
        finally:
            shutil.rmtree(d)

    def test_views(self):
        d = tempfile.mkdtemp()
        try:
            open(os.path.join(d, DB.FILES[0]), 'w', encoding='utf-8').write(YAML)
            v = DB.views(d)
            self.assertEqual(v[1201], ('w', 1, 0))
            self.assertEqual(v[2101], ('s', 0, 1))
            self.assertEqual(v[5005], ('h', 513, 91))
            self.assertNotIn(501, v)
        finally:
            shutil.rmtree(d)


class Committed(unittest.TestCase):
    def test_file_checks(self):
        n, probs = DB.check_file(DATA)
        self.assertEqual(probs, [])
        self.assertGreater(n, 6000)

    def test_known_rows(self):
        rows = {}
        for ln in open(DATA, encoding='utf-8'):
            if ln and ln[0] != '#':
                p = ln.split()
                rows[int(p[0])] = p
        self.assertEqual(rows[501][21], 'itemheal=45:65:0:0')
        self.assertEqual((rows[1201][6], rows[1201][14], rows[1201][10]), ('17', '2', '3'))
        self.assertEqual(rows[601][21], 'itemskill=AL_TELEPORT:1')
        self.assertEqual(rows[602][21], 'itemskill=AL_TELEPORT:3')

    def test_view_table(self):
        self.assertEqual(ITEM_VIEW[1201], ('w', 1, 0))
        self.assertEqual(ITEM_VIEW[2101][0], 's')
        self.assertTrue(all(k[0] in 'hws' for k in ITEM_VIEW.values()))
        self.assertTrue(all(i in ITEM_VIEW for i in items.GEAR_ITEMS), [i for i in items.GEAR_ITEMS if i not in ITEM_VIEW])

    @unittest.skipUnless(os.path.isdir(RA_SRC), 'rAthena sources not on this machine')
    def test_regenerates_identically(self):
        text, n = DB.build(RA_SRC)
        self.assertEqual(open(DATA, encoding='utf-8').read(), text, 'ro_items.txt is stale: run tools/ro_itemdb.py --src <ra_src>')


# ---------------------------------------------------------------- the client side on a synthetic GRF
def _act_for(parts):
    """16 actions (idle + walk) x 8 dirs like the sprite fixtures: parts = [(frames per action, anchor or None, layer or None)]; a None layer = an empty frame (spr -1)."""
    acts = []
    for a in range(16):
        n = 3 if a < 8 else 2
        fr = []
        for f in range(n):
            lay = [RS._layer(0, -2, 0)] if parts == 'full' else [RS._layer(0, 0, -1)]
            fr.append(dict(layers=lay, event=-1, anchors=[(1, -10)] if parts == 'full' else []))
        acts.append(fr)
    return acts


def gear_files(with_missing=()):
    files = {}
    acc_ids = [('ACCESSORY_GOGGLES', 1), ('ACCESSORY_HAT', 2)]
    files['data\\lua files\\datainfo\\accessoryid.lub'] = table_lub('ACCESSORY_IDs', acc_ids)
    files['data\\lua files\\datainfo\\accname.lub'] = accname_lub('ACCESSORY_IDs', [('ACCESSORY_GOGGLES', '_고글'), ('ACCESSORY_HAT', '_모자')])
    files['data\\luafiles514\\lua files\\datainfo\\weapontable.lub'] = table_lub('WeaponNameTable', [])  # placeholder, replaced below
    # WeaponNameTable[Weapon_IDs.X] = '_name' (the client's real shape): index = WEAPONTYPE value
    consts = ['Weapon_IDs', 'WEAPONTYPE_SHORTSWORD', '_단검', 'WEAPONTYPE_SWORD', '_검', 'WeaponNameTable']
    code = [_ins(10, 0)]
    for nm, sfx in ((1, 2), (3, 4)):
        code += [_ins(5, 1, bx=0), _ins(6, 2, 1, 256 + nm), _ins(9, 0, 2, 256 + sfx)]
    code.append(_ins(7, 0, bx=5))
    ids = table_lub('Weapon_IDs', [('WEAPONTYPE_SHORTSWORD', 1), ('WEAPONTYPE_SWORD', 2)])
    # the two tables live in ONE chunk in the real file: build it as one function
    consts2 = ['Weapon_IDs', 'WEAPONTYPE_SHORTSWORD', 'WEAPONTYPE_SWORD', 1, 2, '_단검', '_검', 'WeaponNameTable']
    code2 = [_ins(10, 0)]
    code2 += [_ins(9, 0, 256 + 1, 256 + 3), _ins(9, 0, 256 + 2, 256 + 4)]
    code2.append(_ins(7, 0, bx=0))                                       # Weapon_IDs = {...}
    code2 += [_ins(10, 0)]
    for nm, sfx in ((1, 5), (2, 6)):
        code2 += [_ins(5, 1, bx=0), _ins(6, 2, 1, 256 + nm), _ins(9, 0, 2, 256 + sfx)]
    code2.append(_ins(7, 0, bx=7))
    files['data\\luafiles514\\lua files\\datainfo\\weapontable.lub'] = lua_chunk(code2, consts2)
    res = {501: 'redpotion', 1201: 'knife', 2101: 'guard', 9999: 'missing'}
    files['data\\idnum2itemresnametable.txt'] = ('//file start\r\n' + ''.join('%d#%s#\r\n' % kv for kv in res.items())).encode('cp949')
    spr = S.write_spr([RS._block(4, 4, 1)], palette=RS._pal())
    spr_empty = S.write_spr([RS._block(1, 1, 1)], palette=RS._pal())
    for sw, sx in (('남', 'm'), ('여', 'f')):
        for sfx in ('_고글', '_모자'):
            if sfx in with_missing:
                continue
            p = 'data\\sprite\\악세사리\\%s\\%s%s' % (sw, sw, sfx)
            files[p + '.spr'] = spr
            files[p + '.act'] = S.write_act(_act_for('full'))
        for nm in ('단검', '검'):
            p = 'data\\sprite\\인간족\\초보자\\초보자_%s_%s' % (sw, nm)
            files[p + '.spr'] = spr_empty
            acts = _act_for('empty')
            acts[8 + 0 + 0] = acts[8]
            files[p + '.act'] = S.write_act(acts)
        p = 'data\\sprite\\방패\\초보자\\초보자_%s_가드' % sw
        files[p + '.spr'] = spr_empty
        files[p + '.act'] = S.write_act(_act_for('empty'))
    bmp = img.write_bmp24(2, 2, bytes([255, 0, 255, 255, 10, 20, 30, 255] * 2))
    for r in ('redpotion', 'knife', 'guard'):
        files['data\\texture\\유저인터페이스\\item\\%s.bmp' % r] = bmp
    return files


class Converter(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def vfs(self, files):
        p = os.path.join(self.d, 'data.grf')
        grf.write_grf(p, files)
        return grf.Vfs(self.d)

    def test_tables(self):
        v = self.vfs(gear_files())
        self.assertEqual(items.acc_names(v), {1: '_고글', 2: '_모자'})
        self.assertEqual(items.weapon_names(v), {1: '_단검', 2: '_검'})
        self.assertEqual(items.item_res(v)[1201], 'knife')

    def test_plan(self):
        pl = items.plan([1201, 2101, 5005, 2201, 501, 2201], {1201: ('w', 1, 0), 2101: ('s', 0, 1), 5005: ('h', 513, 91), 2201: ('h', 512, 12)})
        names = [p[0] for p in pl]
        self.assertEqual(names, ['wp_1', 'wp_i1201', 'sh_1', 'hg_91', 'hg_12'])      # 501 has no look, the duplicate 2201 once

    def test_gear_sprites(self):
        v = self.vfs(gear_files())
        w = []
        view = {1201: ('w', 1, 0), 1101: ('w', 2, 0), 2101: ('s', 0, 1), 2201: ('h', 512, 1), 5001: ('h', 256, 2)}
        sp = items.gear_sprites(v, 'm', w, (1201, 1101, 2101, 2201, 5001), view)
        names = [s['name'] for s in sp]
        self.assertEqual(names, ['wp_1_m', 'wp_2_m', 'sh_1_m', 'hg_1_m', 'hg_2_m'])
        self.assertEqual(w, [])
        for s in sp:
            self.assertEqual(len(s['clips']) % 8, 0)
            self.assertEqual(s['kind'], S.KIND_OTHER)
        hg = sp[3]
        self.assertEqual(len(hg['clips'][0]['frames']), 1)          # idle keeps frame 0 only (head-turn poses)
        self.assertEqual(len(hg['clips'][8]['frames']), 2)          # walk keeps its frames
        self.assertIsNotNone(hg['clips'][0]['frames'][0]['anchor'])
        wp = sp[0]
        self.assertTrue(all(f['img_key'] is None for f in wp['clips'][0]['frames']))        # weapon idle = empty
        f = items.gear_sprites(v, 'f', None, (1201,), view)
        self.assertEqual([s['name'] for s in f], ['wp_1_f'])

    def test_missing_look_is_a_warning_not_an_error(self):
        v = self.vfs(gear_files(with_missing=('_모자',)))
        w = []
        sp = items.gear_sprites(v, 'm', w, (2201, 5001), {2201: ('h', 512, 1), 5001: ('h', 256, 2)})
        self.assertEqual([s['name'] for s in sp], ['hg_1_m'])
        self.assertEqual(len(w), 1)
        self.assertIn('hg_2', w[0])
        self.assertNotIn(self.d, w[0])
        # a view the client has no name for
        w = []
        items.gear_sprites(v, 'm', w, (5001,), {5001: ('h', 256, 77)})
        self.assertIn('no client name', w[0])

    def test_no_tables_is_a_warning(self):
        files = dict((k, v) for k, v in gear_files().items() if 'lub' not in k)
        v = self.vfs(files)
        w = []
        self.assertEqual(items.gear_sprites(v, 'm', w, (1201, 2201), {1201: ('w', 1, 0), 2201: ('h', 512, 1)}), [])
        self.assertTrue(any('accessory name tables' in x for x in w) and any('weapon table' in x for x in w), w)

    def test_icons(self):
        v = self.vfs(gear_files())
        w = []
        ic = items.icon_sprites(v, w, (501, 1201, 2101, 9999, 4001))
        self.assertEqual([s['name'] for s in ic], ['ic_501', 'ic_1201', 'ic_2101'])
        self.assertEqual(len(w), 1)
        self.assertIn('2 item icons', w[0])                          # 9999 has a table entry but no file, 4001 has no table entry
        s = ic[0]
        self.assertEqual(len(s['clips']), 8)
        k = s['clips'][0]['frames'][0]['img_key']
        px = s['images'][k]
        self.assertEqual(px[3], 0)                                   # the magenta key became transparent
        self.assertEqual(tuple(px[4:8]), (10, 20, 30, 255))

    def test_in_the_start_pack(self):
        files = E2E.client_files(True)
        files.update(gear_files())
        folder = os.path.join(self.d, 'cl')
        os.makedirs(folder)
        grf.write_grf(os.path.join(folder, 'data.grf'), files)
        v = grf.Vfs(folder)
        w = []
        secs = api.sprite_sections(v, warn=w)
        self.assertEqual([s[0] for s in secs], [S.TAG_RSAT, S.TAG_RSAN])
        info = S.dec_rsan(secs[1][3])
        names = [s['name'] for s in info['sprites']] if isinstance(info, dict) and 'sprites' in info else None
        self.assertIsNotNone(names, 'dec_rsan result shape')
        self.assertEqual(names[:3], ['body_m', 'head_m', 'poring'])           # the player part and its order are unchanged
        for want in ('wp_1_m', 'sh_1_m', 'hg_2_m', 'ic_501', 'hg_2_f'):
            self.assertIn(want, names)
        self.assertEqual(names.index('wp_1_m'), 3)                                # gear comes AFTER every other sprite (poring is the last of the player part)
        body = [s for s in info['sprites'] if s['name'] == 'body_m'][0]
        self.assertEqual(body['clips'], 16)                                    # the synthetic body has 2 anims: only those that exist are kept

    def test_without_gear_files_nothing_changes(self):
        files = E2E.client_files(True)
        folder = os.path.join(self.d, 'cl')
        os.makedirs(folder)
        grf.write_grf(os.path.join(folder, 'data.grf'), files)
        w = []
        secs = api.sprite_sections(grf.Vfs(folder), warn=w)
        names = [s['name'] for s in S.dec_rsan(secs[1][3])['sprites']]
        self.assertEqual(names, ['body_m', 'head_m', 'poring'])               # the synthetic client has no female Novice


@unittest.skipUnless(os.path.isfile(os.path.join(REAL_CLIENT, 'data.grf')), "the owner's client is not on this machine")
class RealClient(unittest.TestCase):
    """Read only on the owner's own client (BYOG): every GEAR_ITEMS look and every icon resolves, no warnings."""

    @classmethod
    def setUpClass(cls):
        cls.v = grf.Vfs(REAL_CLIENT)

    def test_gear_items_resolve(self):
        w = []
        sp = items.gear_sprites(self.v, 'm', w)
        self.assertEqual(w, [])
        names = [s['name'] for s in sp]
        for want in ('wp_1_m', 'wp_i1207_m', 'wp_2_m', 'sh_1_m', 'hg_12_m'):
            self.assertIn(want, names)
        goggles = [s for s in sp if s['name'] == 'hg_12_m'][0]
        self.assertTrue(goggles['clips'][0]['frames'][0]['anchor'] is not None and goggles['clips'][0]['frames'][0]['w'] > 0)

    def test_dagger_shows_in_attack_only(self):
        w = []
        sp = dict((s['name'], s) for s in items.gear_sprites(self.v, 'm', w, (1201,)))
        d = sp['wp_1_m']
        shown = [a for a in range(len(d['clips']) // 8) if any(f['w'] for c in d['clips'][a * 8:a * 8 + 8] for f in c['frames'])]
        self.assertIn(items.ANIM_ATTACK2, shown)                      # the dagger swing (attack2)
        self.assertNotIn(0, shown)                                    # idle: nothing (as in the original)
        self.assertNotIn(1, shown)                                    # walk: nothing

    def test_player_set_has_standby_and_attack(self):
        w = []
        secs = api.sprite_sections(self.v, warn=w)
        info = S.dec_rsan(secs[1][3])
        by = dict((x['name'], x) for x in info['sprites'])
        self.assertEqual(by['body_m']['clips'], 8 * (items.ANIM_ATTACK3 + 1))   # the table reaches anim 11 (standby 4, attack2 10, attack3 11)
        self.assertEqual(by['head_m']['clips'], 8 * (items.ANIM_ATTACK3 + 1))
        for a in (items.ANIM_STANDBY, items.ANIM_ATTACK2, items.ANIM_ATTACK3):
            c = info['clips'][by['body_m']['first_clip'] + a * 8]
            self.assertGreater(c[1], 0, 'body anim %d has frames' % a)
        self.assertEqual(w, [])
        self.assertLess(by['poring']['first_clip'], by['wp_1_m']['first_clip'])

    def test_icons_resolve(self):
        w = []
        ic = items.icon_sprites(self.v, w)
        self.assertEqual(w, [])
        self.assertEqual(len(ic), len(items.ICON_ITEMS))


if __name__ == '__main__':
    unittest.main(verbosity=1)
