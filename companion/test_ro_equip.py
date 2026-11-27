"""RO equipment look part 2 (w43, docs/design/ro_equip.md), converter side, on SYNTHETIC files (made-up pixels / palettes, nothing of the game):
  - ro_conv/imf.py: parse / write round trip, refusals, head_behind (lower priority value = nearer the viewer),
  - sprite.convert_sprite(indexed=True): the red channel carries the palette index, index 0 stays transparent, the own palette is kept,
  - equip.py: palette row layout (hair rows, clothes rows), .pal files read, fallback to the sprite's own palette, RPAL round trip + refusals,
    IMF flags on body frames (and only on bodies, only where the head layer is behind), robe sprites, indexed hair / body in the start pack,
  - the pack carries the RPAL section and the old sprite order is unchanged; (skipped without the owner's client) the real client resolves.

  cd companion && python3 -I test_ro_equip.py
"""
import os
import struct
import sys
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ro_conv import api, equip, grf, imf, sprite as S  # noqa: E402
import test_ro_e2e as E2E  # noqa: E402
import test_ro_sprite as RS  # noqa: E402

REAL_CLIENT = os.environ.get("GSVR_RO_CLIENT", "/path/to/gsvr-testdata Online")


def _pal_file(seed):
    return b''.join(bytes(((seed + i) & 255, (seed * 3 + i) & 255, (seed * 7 + i) & 255, 0)) for i in range(256))


def _act16():
    """2 anims x 8 dirs, 3 + 2 frames, one 4x6 layer."""
    acts = []
    for a in range(16):
        acts.append([dict(layers=[RS._layer(0, -3, 0)], event=-1, anchors=[(0, -10)]) for _ in range(3 if a < 8 else 2)])
    return acts


def _imf_for(head_behind_actions):
    """body layer priority 1 everywhere; head layer 0 (in front) except in the listed (action, frame): 2 (behind)."""
    body, head = [], []
    for a in range(16):
        n = 3 if a < 8 else 2
        body.append([(1, 0, 0)] * n)
        head.append([(2 if (a, f) in head_behind_actions else 0, 0, 0) for f in range(n)])
    return [body, head]


class Imf(unittest.TestCase):
    def test_round_trip(self):
        layers = _imf_for({(3, 1)})
        im = imf.parse(imf.write(layers))
        self.assertEqual(len(im['layers']), 2)
        self.assertEqual(im['layers'][1][3][1][0], 2)
        self.assertAlmostEqual(im['version'], 1.01, places=3)

    def test_head_behind_rule(self):
        im = imf.parse(imf.write(_imf_for({(3, 1), (9, 0)})))
        self.assertTrue(imf.head_behind(im, 3, 1))
        self.assertTrue(imf.head_behind(im, 9, 0))
        self.assertFalse(imf.head_behind(im, 3, 0))            # head 0 < body 1: in front
        self.assertFalse(imf.head_behind(im, 99, 0))           # no such action: in front
        eq = imf.parse(imf.write([[[(1, 0, 0)]], [[(1, 0, 0)]]]))
        self.assertFalse(imf.head_behind(eq, 0, 0))            # equal priorities: in front
        one = imf.parse(imf.write([[[(1, 0, 0)]]]))
        self.assertFalse(imf.head_behind(one, 0, 0))           # no head layer

    def test_refuses_garbage(self):
        for bad in (b'', b'\0' * 8, struct.pack('<fii', 1.01, 0, 99), struct.pack('<fii', 1.01, 0, 1) + struct.pack('<i', 5)):
            with self.assertRaises(ValueError):
                imf.parse(bad)
        good = imf.write(_imf_for(set()))
        with self.assertRaises(ValueError):
            imf.parse(good[:-5])


class Indexed(unittest.TestCase):
    def setUp(self):
        self.spr = S.write_spr([RS._block(4, 6, 1), RS._block(4, 6, 2), (2, 1, bytes([3, 0]))], palette=RS._pal())
        self.act = S.write_act(_act16())

    def test_red_channel_is_the_index(self):
        sp = S.convert_sprite('x', S.KIND_BODY, self.spr, self.act, anims=(0,), indexed=True)
        px = list(sp['images'].values())[0]
        self.assertTrue(all(px[i] == 1 and px[i + 1] == 0 and px[i + 2] == 0 and px[i + 3] == 255 for i in range(0, len(px), 4)))
        self.assertEqual(sp['palette'][1], (255, 0, 0))           # the own palette travels with the sprite
        self.assertEqual(len(sp['palette']), 256)

    def test_plain_conversion_has_no_palette_key_and_real_colours(self):
        sp = S.convert_sprite('x', S.KIND_BODY, self.spr, self.act, anims=(0,))
        self.assertNotIn('palette', sp)
        px = list(sp['images'].values())[0]
        self.assertEqual(tuple(px[0:4]), (255, 0, 0, 255))

    def test_index_zero_stays_transparent(self):
        spr = S.write_spr([(2, 1, bytes([0, 5]))], palette=RS._pal())
        act = S.write_act([[dict(layers=[RS._layer(0, 0, 0)], event=-1, anchors=[])]] * 8)
        sp = S.convert_sprite('x', S.KIND_BODY, spr, act, anims=(0,), indexed=True)
        px = list(sp['images'].values())[0]
        self.assertEqual(px[3], 0)                                # index 0
        self.assertEqual(tuple(px[4:8]), (5, 0, 0, 255))


class Layout(unittest.TestCase):
    def test_rows(self):
        self.assertEqual(equip.hair_row(0, 1, 0), 0)
        self.assertEqual(equip.hair_row(0, 1, 8), 8)
        self.assertEqual(equip.hair_row(0, 2, 0), equip.HAIR_COLORS)
        self.assertEqual(equip.hair_row(1, 1, 0), equip.HAIR_STYLES * equip.HAIR_COLORS)
        last = equip.hair_row(1, equip.HAIR_STYLES, equip.HAIR_COLORS - 1)
        self.assertEqual(last, 2 * equip.HAIR_STYLES * equip.HAIR_COLORS - 1)
        self.assertEqual(equip.body_row(0, 0), last + 1)
        self.assertEqual(equip.body_row(1, equip.BODY_COLORS - 1), equip.row_count() - 1)
        seen = set()
        for sx in (0, 1):
            for st in range(1, equip.HAIR_STYLES + 1):
                for c in range(equip.HAIR_COLORS):
                    seen.add(equip.hair_row(sx, st, c))
            for c in range(equip.BODY_COLORS):
                seen.add(equip.body_row(sx, c))
        self.assertEqual(len(seen), equip.row_count())            # no two looks share a row

    def test_names(self):
        self.assertEqual(equip.hair_name(3, 'f'), 'hi_3_f')
        self.assertEqual(equip.body_name('m'), 'bi_m')
        self.assertEqual(equip.robe_name(7, 'm'), 'rb_7_m')


def _client_files(styles=(1, 2), with_pal=True, with_imf=True):
    files = {}
    spr = S.write_spr([RS._block(4, 6, 1), RS._block(4, 6, 2), (2, 1, bytes([3, 0]))], palette=RS._pal())
    act = S.write_act(_act16())
    for sw, sx in (('남', 'm'), ('여', 'f')):
        for st in styles:
            p = 'data\\sprite\\인간족\\머리통\\%s\\%d_%s' % (sw, st, sw)
            files[p + '.spr'] = spr
            files[p + '.act'] = act
            if with_pal:
                for c in range(equip.HAIR_COLORS):
                    files['data\\palette\\머리\\머리%d_%s_%d.pal' % (st, sw, c)] = _pal_file(st * 10 + c + (100 if sx == 'f' else 0))
        p = 'data\\sprite\\인간족\\몸통\\%s\\초보자_%s' % (sw, sw)
        files[p + '.spr'] = spr
        files[p + '.act'] = act
        if with_pal:
            for c in range(equip.BODY_COLORS):
                files['data\\palette\\몸\\초보자_%s_%d.pal' % (sw, c)] = _pal_file(200 + c)
        if with_imf:
            files['data\\imf\\초보자_%s.imf' % sw] = imf.write(_imf_for({(3, 0), (9, 1)}))
    return files


class Converter(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def vfs(self, files):
        grf.write_grf(os.path.join(self.d, 'data.grf'), files)
        return grf.Vfs(self.d)

    def test_hair_sprites_are_indexed_and_named(self):
        v = self.vfs(_client_files())
        hs = equip.hair_sprites(v, 'm', [], styles=2)
        self.assertEqual([h['name'] for h in hs], ['hi_1_m', 'hi_2_m'])
        px = list(hs[0]['images'].values())[0]
        self.assertEqual(px[0], 1)

    def test_rpal_reads_pal_files(self):
        v = self.vfs(_client_files())
        w = []
        sp, rpal = equip.equip_sprites(v, w)
        info = equip.dec_rpal(rpal)
        self.assertEqual(info['rows'], equip.row_count())
        data = info['data']

        def row(r):
            return data[r * 1024:(r + 1) * 1024]
        r = row(equip.hair_row(0, 2, 3))                          # style 2, colour 3, male: seed 23
        self.assertEqual(tuple(r[4 * 5:4 * 5 + 4]), ((23 + 5) & 255, (23 * 3 + 5) & 255, (23 * 7 + 5) & 255, 255))
        r = row(equip.hair_row(1, 1, 0))                          # female style 1 colour 0: seed 110
        self.assertEqual(r[0], 110)
        r = row(equip.body_row(1, 4))                             # clothes colour 4: seed 204
        self.assertEqual(r[0], 204)
        self.assertEqual(r[3], 255)                               # every colour opaque

    def test_missing_pal_falls_back_to_the_own_palette(self):
        v = self.vfs(_client_files(with_pal=False))
        w = []
        sp, rpal = equip.equip_sprites(v, w)
        data = equip.dec_rpal(rpal)['data']
        r = data[equip.hair_row(0, 1, 5) * 1024:(equip.hair_row(0, 1, 5) + 1) * 1024]
        self.assertEqual(tuple(r[4:8]), (255, 0, 0, 255))         # the fixture's palette index 1 = red
        self.assertTrue(any('no palette files' in x for x in w))

    def test_style_without_a_sprite_still_has_rows(self):
        v = self.vfs(_client_files(styles=(1,)))
        w = []
        sp, rpal = equip.equip_sprites(v, w)
        self.assertEqual(sorted(s['name'] for s in sp if s['name'].startswith('hi_')), ['hi_1_f', 'hi_1_m'])
        self.assertEqual(equip.dec_rpal(rpal)['rows'], equip.row_count())
        self.assertTrue(any('hair styles not in the client data' in x for x in w))

    def test_nothing_in_the_client_gives_nothing(self):
        v = self.vfs({'data\\x.txt': b'x'})
        sp, rpal = equip.equip_sprites(v, [])
        self.assertEqual(sp, [])
        self.assertIsNone(rpal)

    def test_rpal_refusals(self):
        v = self.vfs(_client_files())
        _sp, rpal = equip.equip_sprites(v, [])
        with self.assertRaises(ValueError):
            equip.dec_rpal(rpal[:-1])
        with self.assertRaises(ValueError):
            equip.dec_rpal(struct.pack('<I', 9) + rpal[4:])

    def test_imf_flags_only_on_bodies_and_only_where_behind(self):
        v = self.vfs(_client_files())
        sp, _rpal = equip.equip_sprites(v, [])
        done = equip.apply_imf_all(v, sp, [])
        self.assertEqual(done, {'bi_m': 2, 'bi_f': 2})
        bi = [s for s in sp if s['name'] == 'bi_m'][0]
        self.assertEqual(bi['clips'][3]['frames'][0]['order'], S.FF_HEAD_BEHIND)
        self.assertEqual(bi['clips'][9]['frames'][0]['order'], 0)
        self.assertEqual(bi['clips'][9]['frames'][1]['order'], S.FF_HEAD_BEHIND)
        hi = [s for s in sp if s['name'] == 'hi_1_m'][0]
        self.assertNotIn('order', hi['clips'][3]['frames'][0])    # heads carry no flag

    def test_no_imf_is_not_an_error(self):
        v = self.vfs(_client_files(with_imf=False))
        sp, _rpal = equip.equip_sprites(v, [])
        self.assertEqual(equip.apply_imf_all(v, sp, []), {})

    def test_imf_paths(self):
        self.assertEqual(equip.imf_path('body_m'), 'data/imf/초보자_남.imf')
        self.assertEqual(equip.imf_path('bi_f'), 'data/imf/초보자_여.imf')
        self.assertEqual(equip.imf_path('body_j7_m'), 'data/imf/기사_남.imf')
        self.assertIsNone(equip.imf_path('body_j999_m'))
        self.assertIsNone(equip.imf_path('poring'))

    def test_build_set_writes_the_flag_and_it_survives_dec_rsan(self):
        v = self.vfs(_client_files())
        sp, _rpal = equip.equip_sprites(v, [])
        equip.apply_imf_all(v, sp, [])
        _rsat, rsan = S.build_set(sp, ref=(sp[-1], None), ref_px=40.0)
        info = S.dec_rsan(rsan)
        body = [s for s in info['sprites'] if s['name'] == 'bi_m'][0]
        flagged = 0
        for c in range(body['first_clip'], body['first_clip'] + body['clips']):
            first, cnt, _delay = info['clips'][c]
            for f in info['frames'][first:first + cnt]:
                flagged += 1 if f[1] & S.FF_HEAD_BEHIND else 0          # frame = (page, flags, ...)
        self.assertEqual(flagged, 2)

    def test_start_pack_gets_the_section_and_keeps_the_old_order(self):
        files = E2E.client_files(True)
        files.update(_client_files(styles=(1,)))
        folder = os.path.join(self.d, 'cl')
        os.makedirs(folder)
        grf.write_grf(os.path.join(folder, 'data.grf'), files)
        w = []
        secs = api.sprite_sections(grf.Vfs(folder), warn=w)
        self.assertEqual([s[0] for s in secs], [S.TAG_RSAT, S.TAG_RSAN, equip.TAG_RPAL])
        names = [s['name'] for s in S.dec_rsan(secs[1][3])['sprites']]
        self.assertEqual(names[:3], ['body_m', 'head_m', 'poring'])
        for want in ('hi_1_m', 'hi_1_f', 'bi_m', 'bi_f'):
            self.assertIn(want, names)
        self.assertEqual(equip.dec_rpal(secs[2][3])['rows'], equip.row_count())

    def test_robes(self):
        # a robe table needs compiled Lua: without one nothing is built and nothing raises
        v = self.vfs(_client_files())
        self.assertEqual(equip.robe_table(v), {})
        w = []
        self.assertEqual(equip.robe_sprites(v, 'm', w), [])
        self.assertTrue(any('robe view 1' in x for x in w))

    def test_equip_look_can_be_switched_off(self):
        files = E2E.client_files(True)
        files.update(_client_files(styles=(1,)))
        folder = os.path.join(self.d, 'cl2')
        os.makedirs(folder)
        grf.write_grf(os.path.join(folder, 'data.grf'), files)
        api.EQUIP_LOOK = False
        try:
            secs = api.sprite_sections(grf.Vfs(folder), warn=[])
        finally:
            api.EQUIP_LOOK = True
        self.assertEqual([s[0] for s in secs], [S.TAG_RSAT, S.TAG_RSAN])


@unittest.skipUnless(os.path.isfile(os.path.join(REAL_CLIENT, 'data.grf')), "the owner's client is not on this machine")
class RealClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = grf.Vfs(REAL_CLIENT)

    def test_every_style_and_colour_has_a_palette(self):
        miss = []
        for sw in ('남', '여'):
            for st in range(1, equip.HAIR_STYLES + 1):
                if not self.v.exists(equip.PATH_HEAD % (sw, st, sw) + '.spr'):
                    miss.append(('spr', sw, st))
                for c in range(equip.HAIR_COLORS):
                    if not self.v.exists(equip.PATH_PAL_HAIR % (st, sw, c)):
                        miss.append(('pal', sw, st, c))
            for c in range(equip.BODY_COLORS):
                if not self.v.exists(equip.PATH_PAL_BODY % (sw, c)):
                    miss.append(('body', sw, c))
        self.assertEqual(miss, [])

    def test_novice_imf_flags_some_frames_but_not_idle(self):
        d = self.v.read(equip.PATH_IMF % (equip.NOVICE, '남'))
        im = imf.parse(d)
        self.assertEqual(len(im['layers']), 2)
        idle = [imf.head_behind(im, a, 0) for a in range(8)]
        self.assertEqual(idle, [False] * 8)                    # the head is in front of the body when standing
        n = sum(1 for a in range(len(im['layers'][0])) for f in range(len(im['layers'][0][a])) if imf.head_behind(im, a, f))
        self.assertGreater(n, 50)


if __name__ == '__main__':
    unittest.main()
