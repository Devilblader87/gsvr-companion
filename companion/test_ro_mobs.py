"""RO w34 MONSTERS (docs/design/ro_mobs.md): the fight field's pack sections (monster + drop item sprites in RSAT / RSAN of ro__prt_fild08.pack, FF_SHIFT death
frames, one-direction item clips), the converter hooks (api.MOB_SPRITES / DROP_ITEMS / mob_sprite_sections / build) and the world's rAthena table
(unity/Assets/GoldSrcVR/Editor/ro_mobs.txt) on SYNTHETIC client data only (coloured blocks written here, nothing of the game).

  cd companion && python3 -I test_ro_mobs.py                     (unit tests)
  python3 -I companion/test_ro_mobs.py --write-field-pack OUT     (synthetic prt_fild08 pack with monsters for tools/usharp_offline/ro_e2e.cs --mobs)
"""
import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ro_conv  # noqa: E402
from ro_conv import api, grf, sprite as S  # noqa: E402
import test_ro_e2e as E  # noqa: E402  (the synthetic client of the e2e test)
import test_ro_sprite as RS  # noqa: E402
import rpg1 as P  # noqa: E402

ROOT = os.path.dirname(HERE)
TABLE = os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Editor', 'ro_mobs.txt')
FIELD = 'prt_fild08'


def _monster(big_ghost):
    """SPR / ACT of a monster with all 5 anims x 8 dirs; the dead anim's 2nd frame is a 3.5x-scaled 40 x 40 layer (a 140 px composite, like the iRO ghost) when big_ghost."""
    spr = S.write_spr([RS._block(6, 5, 1), RS._block(40, 40, 2)], palette=RS._pal())
    acts = []
    for a in range(40):
        anim = a // 8
        frames = [dict(layers=[RS._layer(0, -3, 0)], event=-1, anchors=[])]
        if anim == 4:
            frames.append(dict(layers=[RS._layer(0, -20, 1, sx=3.5, sy=3.5) if big_ghost else RS._layer(0, -20, 0)], event=-1, anchors=[]))
        acts.append(frames)
    return spr, S.write_act(acts)


def _item():
    """An item on the ground: ONE action (direction 0 only), like the client's item ACT files."""
    return S.write_spr([RS._block(4, 4, 3)], palette=RS._pal()), S.write_act([[dict(layers=[RS._layer(0, -2, 0)], event=-1, anchors=[])]])


def field_files(monsters=('poring', 'lunatic'), items=((909, 'jello'), (512, 'apple')), table=True):
    """The e2e synthetic client + its map copied as prt_fild08 + monsters / items / the CP949 item resource table."""
    files = E.client_files(True)
    for ext in ('rsw', 'gnd', 'gat'):
        files['data\\%s.%s' % (FIELD, ext)] = files['data\\prontera.%s' % ext]
    files['data\\%s.rsw' % FIELD] = E.RC.rsw_bytes(FIELD + '.gnd', FIELD + '.gat', [('box.rsm', (-30.0, 0.0, -30.0), (0, 0, 0), (1, 1, 1))], water=5.0)
    if 'poring' not in monsters:          # the e2e client's own small poring (the start map's sprite set) goes too
        for ext in ('.spr', '.act'):
            files.pop((api.PATH_MONSTER + 'poring').replace('/', '\\') + ext, None)
    for m in monsters:
        sb, ab = _monster(m == 'poring')
        files[(api.PATH_MONSTER + m).replace('/', '\\') + '.spr'] = sb
        files[(api.PATH_MONSTER + m).replace('/', '\\') + '.act'] = ab
    lines = ['//file start']
    for iid, res in items:
        sb, ab = _item()
        files[(api.PATH_ITEM + res).replace('/', '\\') + '.spr'] = sb
        files[(api.PATH_ITEM + res).replace('/', '\\') + '.act'] = ab
        lines.append('%d#%s#' % (iid, res))
    if table:
        files[api.ITEM_TABLE.replace('/', '\\')] = '\r\n'.join(lines).encode('cp949')
    return files


class _Vfs(object):
    def __init__(self, files):
        self.f = dict((k.replace('\\', '/').lower(), v) for k, v in files.items())

    def read(self, path):
        k = path.replace('\\', '/').lower()
        if k not in self.f:
            raise KeyError(path)
        return self.f[k]


def table_rows(kind):
    out = []
    with open(TABLE, encoding='utf-8') as fh:
        for ln in fh:
            ln = ln.split('#')[0].split()
            if ln and ln[0] == kind:
                out.append(ln[1:])
    return out


class Shift(unittest.TestCase):
    def test_shift_for(self):
        self.assertEqual(S.shift_for(100, 40, None), 0)
        self.assertEqual(S.shift_for(128, 40, 128), 0)
        self.assertEqual(S.shift_for(129, 40, 128), 1)
        self.assertEqual(S.shift_for(370, 300, 128), 2)          # the iRO poring ghost: 370 px -> 92 px
        self.assertEqual(S.shift_for(1 << 20, 1, 128), S.MAX_SHIFT)

    def test_big_frame_composed_smaller_and_flagged(self):
        sb, ab = _monster(True)
        sp = S.convert_sprite('poring', S.KIND_MONSTER, sb, ab, anims=api.MONSTER_ALL, max_side=api.MOB_MAX_SIDE)
        full = S.convert_sprite('poring', S.KIND_MONSTER, sb, ab, anims=api.MONSTER_ALL)
        f = sp['clips'][4 * 8]['frames'][1]
        g = full['clips'][4 * 8]['frames'][1]
        self.assertEqual(g['w'], 140)                    # 40 px x 3.5
        self.assertEqual(f['shift'], 1)
        self.assertLessEqual(max(f['w'], f['h']), api.MOB_MAX_SIDE)
        k = 1 << f['shift']
        for a, b in ((f['w'] * k, g['w']), (f['h'] * k, g['h']), (f['left'] * k, g['left']), (f['top'] * k, g['top'])):
            self.assertLessEqual(abs(a - b), k, 'scaled back within one shifted pixel: %d vs %d' % (a, b))
        self.assertEqual(sp['clips'][4 * 8]['frames'][0]['shift'], 0, 'a small frame of the same clip is not shifted')
        rsat, rsan = S.build_set([sp], ref=(None, None), ref_px=80.0)
        rs = S.dec_rsan(rsan)
        self.assertEqual(rs['ref_height_px'], 80.0)
        fr = rs['frames'][rs['clips'][4 * 8][0] + 1]
        self.assertEqual((fr[1] & S.FF_SHIFT_MASK) >> S.FF_SHIFT_POS, 1, 'RSAN frame flags carry the shift')
        self.assertEqual(rs['frames'][rs['clips'][4 * 8][0]][1] & S.FF_SHIFT_MASK, 0)

    def test_item_directions_filled(self):
        sb, ab = _item()
        it = S.convert_sprite('item_909', S.KIND_OTHER, sb, ab, anims=(0,), still_anims=(0,))
        self.assertEqual(len(it['clips']), 8)
        self.assertTrue(all(len(c['frames']) == 1 for c in it['clips']), 'every viewer direction shows the one item image')
        mon = S.convert_sprite('m', S.KIND_MONSTER, sb, ab, anims=(0,))
        self.assertEqual(sum(1 for c in mon['clips'] if c['frames']), 1, 'monsters keep their own directions (no filling)')


class Sections(unittest.TestCase):
    def test_mob_sections(self):
        warn = []
        secs = api.mob_sprite_sections(_Vfs(field_files()), FIELD, warn)
        self.assertEqual([t for t, _v, _c, _d in secs], [S.TAG_RSAT, S.TAG_RSAN])
        rs = S.dec_rsan(secs[1][3])
        names = [s['name'] for s in rs['sprites']]
        self.assertEqual(names[:2], ['lunatic', 'poring'], 'monsters first, in MOB_SPRITES order (missing ones skipped)')
        self.assertEqual(sorted(names[2:]), ['item_512', 'item_909'])
        for s in rs['sprites']:
            if s['kind'] == S.KIND_MONSTER:
                self.assertEqual(s['clips'], 40, '%s: five anims x 8 directions' % s['name'])
                dead = sum(rs['clips'][s['first_clip'] + 32 + d][1] for d in range(8))
                self.assertGreater(dead, 0, '%s: the death clip is in the pack' % s['name'])
        self.assertTrue(any(f[1] & S.FF_SHIFT_MASK for f in rs['frames']), 'the poring ghost frame is shifted')
        self.assertGreater(rs['ref_height_px'], 0.0, 'ref height = the Novice of the same client')
        self.assertTrue(any('pupa' in w for w in warn) and any('drops' in w for w in warn), warn)
        self.assertTrue(any('item 507' in w for w in warn), 'a drop item without a client resource name is a warning: %r' % warn)

    def test_no_monsters_no_sections(self):
        self.assertEqual(api.mob_sprite_sections(_Vfs(field_files(monsters=())), FIELD, []), [])
        self.assertEqual(api.mob_sprite_sections(_Vfs(field_files()), 'prt_in', []), [], 'maps without a monster list carry none')

    def test_no_item_table(self):
        warn = []
        rs = S.dec_rsan(api.mob_sprite_sections(_Vfs(field_files(table=False)), FIELD, warn)[1][3])
        self.assertEqual([s['name'] for s in rs['sprites']], ['lunatic', 'poring'])

    def test_build_field_pack(self):
        work = tempfile.mkdtemp(prefix='romobs')
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        cl = os.path.join(work, 'client')
        os.makedirs(cl)
        grf.write_grf(os.path.join(cl, 'data.grf'), field_files())
        out = os.path.join(work, 'ro__%s.pack' % FIELD)
        res = api.build(FIELD, cl, out)
        self.assertTrue(res['sprites'], res['warnings'])
        pk = P.read_pack(open(out, 'rb').read())
        self.assertIn(S.TAG_RSAT, pk['sections'])
        rs = S.dec_rsan(pk['sections'][S.TAG_RSAN]['data'])
        self.assertIn('poring', [s['name'] for s in rs['sprites']])
        self.assertNotIn('body_m', [s['name'] for s in rs['sprites']], 'the field pack has no player sprites')
        self.assertIn(b'CELL', pk['sections'], 'the map sections are still there')
        out2 = os.path.join(work, 'ro__prontera.pack')
        api.build('prontera', cl, out2)
        rs2 = S.dec_rsan(P.read_pack(open(out2, 'rb').read())['sections'][S.TAG_RSAN]['data'])
        self.assertNotIn('lunatic', [s['name'] for s in rs2['sprites']], 'the start map keeps its own sprite set')

    def test_pack_rev(self):
        self.assertGreaterEqual(api.PACK_REV.get(FIELD, 0), 1, 'old cached field packs (no monsters) are rebuilt')
        self.assertNotEqual(api.pack_version(FIELD), api.VERSION)


class Table(unittest.TestCase):
    """The world's rAthena table (Editor/ro_mobs.txt) and the converter's lists name the same monsters / items; spot rows against the rAthena numbers."""
    def test_lists_match_the_table(self):
        mobs = dict((int(r[0]), r) for r in table_rows('mob'))
        spawn_ids = [int(r[0]) for r in table_rows('spawn') if r[8] == FIELD]
        self.assertEqual(sorted(mobs[i][23] for i in set(spawn_ids)), sorted(api.MOB_SPRITES[FIELD]))
        drop_items = sorted(set(int(r[1]) for r in table_rows('drop') if int(r[0]) in spawn_ids))
        self.assertEqual(drop_items, sorted(api.DROP_ITEMS[FIELD]))
        items = set(int(r[0]) for r in table_rows('item'))
        self.assertTrue(set(drop_items) <= items, 'every drop has an item row: missing %r' % sorted(set(drop_items) - items))

    def test_rathena_rows(self):
        mobs = dict((int(r[0]), [int(x) for x in r[:23]]) for r in table_rows('mob'))
        # id lv hp atk1 atk2 def mdef str agi vit int dex luk range chase walk adelay amotion dmotion bexp jexp size mode (db/pre-re/mob_db.yml, rAthena defaults 1 for unset stats)
        self.assertEqual(mobs[1002], [1002, 1, 50, 7, 10, 0, 5, 1, 1, 1, 0, 6, 30, 1, 12, 400, 1872, 672, 480, 2, 1, 1, 0x83])
        self.assertEqual(mobs[1063][:5] + [mobs[1063][15], mobs[1063][19], mobs[1063][20], mobs[1063][21], mobs[1063][22]], [1063, 3, 60, 9, 12, 200, 6, 2, 0, 0x81])
        self.assertEqual(mobs[1008][2], 427)
        self.assertEqual(mobs[1008][22], 0, 'Pupa (Ai 06): neither moves nor attacks')
        spawns = dict((int(r[0]), int(r[1])) for r in table_rows('spawn') if r[8] == FIELD)
        self.assertEqual(spawns, {1002: 70, 1063: 40, 1008: 20, 1113: 10})
        bexp = [int(r[1]) for r in table_rows('bexp')]
        self.assertEqual(bexp[:10], [9, 16, 25, 36, 77, 112, 153, 200, 253, 320])
        self.assertEqual([int(r[1]) for r in table_rows('jexp')][:4], [10, 18, 28, 40])
        self.assertEqual([int(r[1]) for r in table_rows('basehp')][:3], [40, 45, 50])
        items = dict((int(r[0]), r) for r in table_rows('item'))
        self.assertEqual(items[1201][4], '17')          # Knife ATK 17, weapon level 1
        self.assertEqual(items[1201][6], '1')
        self.assertEqual(items[2301][5], '1')           # Cotton Shirt DEF 1 (pre-renewal)
        self.assertEqual(table_rows('novice')[0][:6], ['5'] * 6)


class World(unittest.TestCase):
    """w41 (docs/design/ro_mobs.md "w41 multi-map"): the merged table of the 43-map world is what tools/ro_fields_gen.py merge makes from the repo files, and every
    monster map's pack carries its own monsters + drop items (ro_conv/mobsets.py = generated from that table)."""
    @staticmethod
    def _gen():
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import ro_fields_gen
        return ro_fields_gen

    def test_merged_table_is_regenerated(self):
        g = self._gen()
        txt = g.merge(ROOT)
        cur = open(os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Editor', 'ro_mobs_all.txt'), encoding='utf-8').read()
        self.assertEqual(txt, cur, 'Editor/ro_mobs_all.txt is stale: run python3 -I tools/ro_fields_gen.py merge')

    def test_mobsets_match_the_table(self):
        g = self._gen()
        ms, di = g.mobsets(g.merge(ROOT))
        from ro_conv import mobsets
        self.assertEqual(ms, mobsets.MOB_SPRITES, 'companion/ro_conv/mobsets.py is stale: run python3 -I tools/ro_fields_gen.py merge')
        self.assertEqual(di, mobsets.DROP_ITEMS)
        self.assertEqual(len(ms), 36, 'every field / culvert / dungeon map of the world has monsters (27 + the 9 monster maps of the w46 maps2 lane)')
        for m in ms:
            self.assertIn(m, api.PACKS, '%s is a world map' % m)
            self.assertEqual(api.MOB_SPRITES[m], ms[m])
            self.assertGreaterEqual(api.PACK_REV.get(m, 0), api.MOB_PACK_REV, '%s: a pack built before w41 (map only) is rebuilt with its sprites' % m)
        self.assertEqual(api.MOB_SPRITES[FIELD], ('lunatic', 'pupa', 'poring', 'drops'), 'the w34 field keeps its list')
        for town in ('prontera', 'prt_in', 'izlude', 'payon', 'geffen', 'morocc', 'alberta'):
            self.assertNotIn(town, api.MOB_SPRITES, '%s has no monsters' % town)

    def test_other_map_sections(self):
        """A monster map other than the w34 field (pay_fild01) gets its own monsters + drop items into its pack (synthetic client)."""
        mons = api.MOB_SPRITES['pay_fild01']
        items = [(i, 'it%d' % i) for i in api.DROP_ITEMS['pay_fild01'][:6]]
        warn = []
        secs = api.mob_sprite_sections(_Vfs(field_files(monsters=mons, items=items)), 'pay_fild01', warn)
        self.assertEqual([t for t, _v, _c, _d in secs], [S.TAG_RSAT, S.TAG_RSAN])
        names = [s['name'] for s in S.dec_rsan(secs[1][3])['sprites']]
        self.assertEqual(names[:len(mons)], list(mons))
        self.assertEqual(sorted(n for n in names if n.startswith('item_')), sorted('item_%d' % i for i, _r in items))


def write_field_pack(out):
    work = tempfile.mkdtemp(prefix='romobs')
    try:
        cl = os.path.join(work, 'client')
        os.makedirs(cl)
        grf.write_grf(os.path.join(cl, 'data.grf'), field_files(monsters=api.MOB_SPRITES[FIELD], items=[(i, 'it%d' % i) for i in api.DROP_ITEMS[FIELD]]))
        res = api.build(FIELD, cl, out)
        print('field pack %s: %d bytes, sprites %s' % (out, res['bytes'], res['sprites']))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--write-field-pack':
        write_field_pack(sys.argv[2])
        sys.exit(0)
    unittest.main()
