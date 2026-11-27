"""ro_conv.roefx (w43 lane fx, docs/design/ro_fx.md) on SYNTHETIC client files written by this test (made-up pixels / keys, nothing of the game).

  cd companion && python3 -I test_ro_fx.py
  python3 -I test_ro_fx.py --write-pack <out>    (the pack tools/usharp_offline/rofxrules.cs reads with the compiled GsvrRoFx)
Optional real-data leg: RO_CLIENT=<client folder> python3 -I test_ro_fx.py  (every effect of WORLD_FX found in the install, no warning).
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ro_conv  # noqa: E402
from ro_conv import grf, img, roefx as X, sprite, strfx as S  # noqa: E402
import rpg1 as P  # noqa: E402

TEXTURES = ('ring_blue', 'ring_red', 'ring_white', 'ring_yellow', 'pok1', 'pok3', 'lens1', 'lens2', 'icearrow', 'arrown')


def key(frame, typ, pos=(320.0, 292.0), xy=(32, -32, -32, 32, -32, -32, 32, 32), color=(255.0, 255.0, 255.0, 200.0)):
    return dict(frame=frame, type=typ, pos=[float(v) for v in pos], xy=[float(v) for v in xy], angle=0.0, aniframe=0.0, anitype=0, delay=0.0, color=list(color), src=5, dst=2)


def provoke_str():
    return dict(version=148, fps=60, max_frame=20, layers=[dict(textures=['provoke.bmp'], keys=[key(0, 0), key(0, 1, pos=(0, -2), xy=(0,) * 8, color=(0, 0, 0, -5)), key(20, 0)])])


def bless_spr_act():
    """2 palette frames (8 x 6, 4 x 4) and an ACT with 3 frames (frame 1 empty), delay 2 ticks (48 ms) per frame"""
    spr = sprite.write_spr([(8, 6, bytes([1] * 48)), (4, 4, bytes([2] * 16))], palette=[(0, 0, 0)] + [(200, 180, 40)] * 255)
    fr = lambda si, x, y: dict(layers=[dict(x=x, y=y, spr=si)] if si >= 0 else [], event=-1)
    act = sprite.write_act([[fr(0, 0, -10), fr(-1, 0, 0), fr(1, 3, -20)]], delays=[2.0])
    return spr, act


def make_client(tmp, textures=TEXTURES, provoke=True, bless=True):
    files = {}
    for t in textures:
        files['data\\texture\\effect\\' + t + '.bmp'] = img.write_bmp24(16, 16, bytes((90, 160, 250, 255)) * 256)
    if provoke:
        files['data\\texture\\effect\\provoke.str'] = S.write_str(provoke_str())
        files['data\\texture\\effect\\provoke.bmp'] = img.write_bmp24(8, 8, bytes((250, 40, 40, 255)) * 64)
    if bless:
        spr, act = bless_spr_act()
        files[X.SPR_DIR + '축복.spr'] = spr
        files[X.SPR_DIR + '축복.act'] = act
    grf.write_grf(os.path.join(tmp, 'data.grf'), files)
    return tmp


def build_pack(folder):
    out = os.path.join(folder, 'ro__roefx.pack')
    r = ro_conv.build('roefx', folder, out)
    with open(out, 'rb') as f:
        raw = f.read()
    return r, raw, P.read_pack(raw, verify=True)


class Recipes(unittest.TestCase):
    def test_table_shape(self):
        for nm, parts in X.WORLD_FX.items():
            self.assertTrue(nm.isascii() and nm == nm.lower() and len(nm) <= 32, nm)
            self.assertTrue(parts, nm)
            for p in parts:
                self.assertIn(p['kind'], ('str', 'spr', 'cyl', 'puff', 'rays', 'flip', 'grow', 'tex'), nm)
                if p['kind'] == 'cyl':
                    self.assertTrue(0 <= p['anim'] <= X.ANIM_RING and 1 <= p['shown'] <= p['sides'] <= X.MAX_SIDES, nm)
        for nm in ('cast_neutral', 'cast_water', 'cast_fire', 'cast_wind', 'cast_earth', 'cast_holy', 'cast_poison', 'hit', 'hit_lens', 'magnumbreak', 'provoke', 'firewall1',
                   'firewall2', 'firehit1', 'windhit1', 'lightning', 'napalm', 'heal', 'heal_hit', 'incagi', 'blessing', 'levelup', 'joblevelup', 'portal', 'strafe_caster',
                   'ring_fire', 'ring_cold', 'tex_firebolt', 'tex_coldbolt', 'tex_arrow', 'tex_soul'):
            self.assertIn(nm, X.WORLD_FX, 'the world plays ' + nm)

    def test_client_numbers(self):
        """the conversions of the client's numbers (5 units a cell, 60 ticks a second)"""
        r = X.WORLD_FX['ring_fire'][0]
        self.assertEqual(r['anim'], X.ANIM_RING)
        self.assertAlmostEqual(r['height'], 1.2 * 60 / 5)                 # radius speed 1.2 units / tick = 14.4 cells / s
        self.assertAlmostEqual(r['accel'], (1.2 / 70) * -2 * 3600 / 5)     # units / tick^2 -> cells / s^2
        self.assertAlmostEqual(r['top'], 2.0)                             # band 10 units = 2 cells
        self.assertAlmostEqual(r['dur'], 0.5)                             # 30 ticks
        cones = [c for c in X.WORLD_FX['portal'] if c['kind'] == 'cyl']
        self.assertEqual(len(cones), 8, 'EF_WARPZONE2: 2 cones x 4 copies')
        self.assertEqual(sorted(set(c['delay'] for c in cones)), [0.0, 1.0, 2.0, 3.0])
        self.assertTrue(all(c['flags'] & X.F_REPEAT and c['period'] == 4.0 and c['dur'] == 4.0 and c['anim'] == 3 for c in cones))
        self.assertTrue(all(c['dur'] == 0.0 for nm in X.WORLD_FX if nm.startswith('cast_') for c in X.WORLD_FX[nm]), 'a cast aura lives as long as the cast')
        self.assertTrue(all(c['flags'] & X.F_ALPHA for c in X.WORLD_FX['cast_earth']) and not any(c['flags'] & X.F_ALPHA for c in X.WORLD_FX['cast_fire']), 'earth: blend 0 (alpha), the rest additive')
        sc = [c for c in X.WORLD_FX['strafe_caster'] if c['shown'] == 1]
        self.assertTrue(len(sc) == 18 and all(c['sides'] == 30 and c['flags'] & X.F_FACE and c['angle_rand'] == 360.0 for c in sc), 'EF_BASH: 10 + 8 single wedges of 30, facing the camera')

    def test_bake_puff_deterministic(self):
        p = X.WORLD_FX['heal'][2]
        a = X.bake_puff(p, X._rng('heal'))
        b = X.bake_puff(p, X._rng('heal'))
        self.assertEqual(a, b, 'fixed seed per effect')
        self.assertEqual(len(a), p['n'])
        for ly in a:
            fr = [k['frame'] for k in ly['keys']]
            self.assertEqual(fr, sorted(fr))
            st0 = S.sample_layer(ly['keys'], fr[0])
            stm = S.sample_layer(ly['keys'], (fr[0] + fr[-1]) // 2)
            self.assertAlmostEqual(st0['color'][3], 0.0, places=3)        # fade in
            self.assertGreater(stm['color'][3], 100.0)
            self.assertLess(S.sample_layer(ly['keys'], fr[-1])['color'][3], 1.0)   # gone at the end
            self.assertLess(stm['pos'][1], st0['pos'][1] + 1e-6, 'it rises (STR y down)')

    def test_bake_spr(self):
        spr, act = bless_spr_act()
        lys, imgs = X.bake_spr('bless', sprite.parse_spr(spr), sprite.parse_act(act), dur=0.5, yoff=-75.0, fps=30)
        self.assertEqual([t for t, _w, _h, _p in imgs], ['bless#0', 'bless#2'], 'an empty ACT frame has no image')
        ks = lys[0]['keys']
        self.assertEqual(ks[0]['frame'], 0)
        self.assertEqual(ks[-1]['frame'], 15, 'repeated for 0.5 s at 30 fps')
        step = max(1, int(round(2.0 * sprite.TICK_MS / 1000.0 * 30)))
        self.assertEqual(ks[1]['frame'], 2 * step, 'one ACT frame = 2 ticks of the ACT delay; the empty frame leaves a gap')
        self.assertEqual(ks[0]['aniframe'], 0.0)
        self.assertEqual(ks[1]['aniframe'], 1.0)
        self.assertEqual(ks[0]['xy'][:4], [4.0, -4.0, -4.0, 4.0], 'corners = the composed frame size')
        self.assertAlmostEqual(ks[0]['pos'][1], X.OY - 75.0 - 10.0 - 3.0 + 3.0, msg='head offset + the frame centre')


class Pack(unittest.TestCase):
    def test_build(self):
        with tempfile.TemporaryDirectory() as t:
            r, raw, p = build_pack(make_client(t))
            self.assertEqual(p['order'], [b'REFX', b'RFTX', b'RFCY'])
            effs = S.dec_refx(p['sections'][b'REFX']['data'])
            names = [e['name'] for e in effs]
            for nm in ('cast_fire', 'portal', 'provoke', 'blessing', 'hit', 'ring_fire', 'tex_coldbolt', 'tex_arrow'):
                self.assertIn(nm, names)
            self.assertNotIn('levelup', names, 'angel.str is not in this client: dropped with a warning')
            self.assertTrue(any('angel.str' in w for w in r['warnings']))
            rows = X.dec_rfcy(p['sections'][b'RFCY']['data'])
            self.assertEqual(p['sections'][b'RFCY']['count'], len(rows))
            portal = names.index('portal')
            prow = [x for x in rows if x['effect'] == portal]
            self.assertEqual(len(prow), 8)
            self.assertTrue(all(x['slot'] >= 0 and x['flags'] & X.F_REPEAT for x in prow))
            self.assertEqual(len(effs[names.index('cast_fire')]['layers']), 0, 'cylinders only: no REFX layers')
            self.assertEqual(len(effs[names.index('portal')]['layers']), 5, 'the pok1 sparkles')
            bl = effs[names.index('blessing')]
            self.assertEqual(len(bl['layers'][0]['texslots']), 2)
            self.assertTrue(all(s >= 0 for s in bl['layers'][0]['texslots']), 'the composed sprite frames are atlas slots')
            tx = effs[names.index('tex_coldbolt')]
            self.assertTrue(len(tx['layers']) == 1 and tx['layers'][0]['keys'] == [] and tx['layers'][0]['texslots'][0] >= 0, 'a texture-only effect: one layer, no keys')
            at = S.dec_rftx(p['sections'][b'RFTX']['data'])
            self.assertEqual(at['slots'], p['sections'][b'RFTX']['count'])
            self.assertEqual(max(x['slot'] for x in rows) < at['slots'], True)
            # determinism: the same client gives the same bytes
            r2, raw2, _p2 = build_pack(t)
            self.assertEqual(raw, raw2)

    def test_missing_textures(self):
        with tempfile.TemporaryDirectory() as t:
            r, _raw, p = build_pack(make_client(t, textures=('pok3',), provoke=False, bless=False))
            names = [e['name'] for e in S.dec_refx(p['sections'][b'REFX']['data'])]
            self.assertIn('portal', names)
            rows = X.dec_rfcy(p['sections'][b'RFCY']['data']) if b'RFCY' in p['sections'] else []
            self.assertEqual(rows, [], 'no ring texture: no cylinder rows (the world draws nothing for them)')
            self.assertTrue(any('textures missing' in w for w in r['warnings']))

    def test_api(self):
        self.assertIn('roefx', ro_conv.UI_PACKS)
        self.assertNotIn('roefx', ro_conv.PACKS, 'the world loads every PACKS entry as a map')
        self.assertEqual(ro_conv.pack_version('roefx'), 'ro-fx-%d' % X.FX_REV)
        self.assertEqual(ro_conv.PREBUILD_ORDER[1:3], ('roui', 'roefx'))
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ro_conv.BuildError):
                ro_conv.build('roefx', t, os.path.join(t, 'x.pack'))          # no data.grf

    def test_rfcy_codec(self):
        c = X.cyl('ring_blue', 3, 2.0, 3.3, 1.1, rgb=(0.5, 0.5, 1.0), alpha=0.4, dur=4.0, delay=1.0, repeat=True, period=4.0)
        d = X.enc_rfcy([(7, 3, c)])
        self.assertEqual(len(d), 16 + 80)
        row = X.dec_rfcy(d)[0]
        self.assertEqual((row['effect'], row['slot'], row['anim'], row['flags'], row['sides'], row['shown']), (7, 3, 3, X.F_FADE | X.F_REPEAT, 20, 20))
        self.assertAlmostEqual(row['top'], 3.3, places=5)
        with self.assertRaises(X.FxError):
            X.dec_rfcy(d[:-4])


@unittest.skipUnless(os.environ.get('RO_CLIENT'), 'RO_CLIENT=<client folder> for the real-data leg')
class RealClient(unittest.TestCase):
    def test_every_world_effect(self):
        warn = []
        effects, cyls, images, info = X.build_effects(grf.Vfs(os.environ['RO_CLIENT']), None, warn)
        self.assertEqual(warn, [])
        self.assertEqual(sorted(info['effects']), sorted(X.WORLD_FX))
        self.assertEqual(info['missing'], [])


def write_pack(path):
    with tempfile.TemporaryDirectory() as t:
        _r, raw, _p = build_pack(make_client(t))
    with open(path, 'wb') as f:
        f.write(raw)
    print('wrote', path, len(raw), 'bytes')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write-pack':
        write_pack(sys.argv[2])
        sys.exit(0)
    unittest.main()
