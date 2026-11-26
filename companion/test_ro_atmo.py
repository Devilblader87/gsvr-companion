"""RO atmosphere lane (w42 atmo, docs/design/ro_atmo.md): RATM section (fog / bgm number / light), UI sounds, per map monster clips, ground lift data.
Run: python3 -I test_ro_atmo.py       Real client leg: RO_CLIENT=<folder with data.grf> python3 -I test_ro_atmo.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ro_conv import atmo, audio as A, grf, api   # noqa: E402
import test_ro_sound as TS                          # noqa: E402

FOG = """//EP.14.3
1@eom.rsw#
0.2#
0.4#
0x22700409#
0.5#

prontera.rsw#
0.23#
0.9#
0xffffffff#
0.3#

//comment between entries
prt_fild08.rsw#
0.25#
0.6#
0xffBAFF77#
0.3#
bad_map.rsw#
0.9#
0.1#
0xff000000#
0.3#
prt_fild08.rsw#
0.30#
0.7#
0xff112233#
0.4#
"""


class Fog(unittest.TestCase):
    def test_parse(self):
        f = atmo.parse_fog(FOG)
        self.assertEqual(sorted(f), ['1@eom', 'prontera', 'prt_fild08'], 'near > far is rejected, comments + blank lines skipped')
        self.assertEqual(f['prontera'], dict(near=0.23, far=0.9, argb=0xFFFFFFFF, factor=0.3))
        self.assertEqual(f['prt_fild08']['argb'], 0xFF112233, 'a later entry of the same map wins')

    def test_bgm_number(self):
        self.assertEqual(atmo.bgm_number('bgm\\08.mp3'), 8)
        self.assertEqual(atmo.bgm_number('bgm/120.MP3'), 120)
        self.assertEqual(atmo.bgm_number('bgm\\x.mp3'), -1)
        self.assertEqual(atmo.bgm_number(None), -1)
        self.assertEqual(atmo.bgm_number('bgm\\08.wav'), -1)

    def test_section_roundtrip(self):
        f = atmo.parse_fog(FOG)['prontera']
        b = atmo.enc_ratm(f, 8, dict(ambient=(0.55, 0.5, 0.5), diffuse=(1.0, 1.0, 1.0)))
        self.assertEqual(len(b), 64)
        d = atmo.dec_ratm(b)
        self.assertTrue(d['fog'])
        self.assertEqual((d['near'], d['far'], d['rgb'], d['alpha'], d['factor'], d['bgm']), (230, 900, 0xFFFFFF, 255, 300, 8))
        self.assertEqual(d['ambient'], (550, 500, 500))
        n = atmo.dec_ratm(atmo.enc_ratm(None, -1, None))
        self.assertFalse(n['fog'])
        self.assertEqual(n['bgm'], -1)
        self.assertEqual(n['flags'], 0)
        with self.assertRaises(ValueError):
            atmo.dec_ratm(b'\0' * 64)

    def test_golden_bytes(self):
        """the same hex is pinned in tools/usharp_offline/roatmorules.cs against GsvrRoWalk.LAtmoCheck: converter and world agree on the layout"""
        f = atmo.parse_fog('prontera.rsw#\n0.23#\n0.9#\n0xffffffff#\n0.3#\n')['prontera']
        b = atmo.enc_ratm(f, 8, dict(ambient=(0.55, 0.5, 0.5), diffuse=(1.0, 1.0, 1.0)))
        self.assertEqual(b.hex(), '0100000003000000e600000084030000ffffff00ff0000002c01000026020000f4010000f4010000e8030000e8030000e8030000080000000000000000000000')

    def test_metres(self):
        a, b = atmo.fog_metres(230, 900, 1.0)
        self.assertAlmostEqual(a, 0.23 * 48.0)
        self.assertAlmostEqual(b, 0.9 * 48.0, msg='240 units / 5 units per cell = 48 cells, 1 m per cell')
        a2, _b2 = atmo.fog_metres(230, 900, 2.0)
        self.assertAlmostEqual(a2, 2 * a)

    def test_section_from_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'data\\fogparametertable.txt': FOG.encode('cp949'), 'data\\mp3nametable.txt': b'prontera.rsw#bgm\\\\08.mp3#\n'}
            grf.write_grf(os.path.join(tmp, 'data.grf'), files)
            v = grf.Vfs(tmp)
            w = []
            tag, ver, cnt, data = atmo.build_section(v, 'PRONTERA', dict(ambient=(0.3,) * 3, diffuse=(1.0,) * 3), w)
            self.assertEqual((tag, ver, cnt), (b'RATM', 1, 1))
            d = atmo.dec_ratm(data)
            self.assertEqual((d['fog'], d['bgm'], d['rgb']), (True, 8, 0xFFFFFF))
            tag, ver, cnt, data = atmo.build_section(v, 'geffen', None, w)
            self.assertEqual((atmo.dec_ratm(data)['fog'], atmo.dec_ratm(data)['bgm']), (False, -1), 'a map without entries: no fog, no music number')
            self.assertEqual(w, [])
        with tempfile.TemporaryDirectory() as tmp:
            grf.write_grf(os.path.join(tmp, 'data.grf'), {'data\\x.txt': b'x'})
            w = []
            tag, ver, cnt, data = atmo.build_section(grf.Vfs(tmp), 'prontera', None, w)
            self.assertFalse(atmo.dec_ratm(data)['fog'])
            self.assertEqual(len(w), 2, 'both tables missing: two warnings, still a valid section')


class Sounds(unittest.TestCase):
    def test_ui_sounds_in_plan(self):
        names = [n for n, _c in A.sfx_plan(())]
        for n in ('fx/click', 'fx/cancel', 'fx/pickup', 'fx/equip', 'fx/swing/sword', 'fx/hit/sword', 'fx/levelup', 'fx/warp'):
            self.assertIn(n, names)

    def test_mon_plan(self):
        p = A.mon_plan(('poring', 'spore'))
        self.assertEqual([n for n, _c in p], ['mon/poring/atk', 'mon/poring/die', 'mon/poring/hurt', 'mon/spore/atk', 'mon/spore/die', 'mon/spore/hurt'])
        self.assertEqual(dict(p)['mon/poring/hurt'][0], 'poring_damage.wav')
        self.assertEqual(A.map_monsters('PRT_FILD08'), api.MOB_SPRITES['prt_fild08'])
        self.assertEqual(A.map_monsters('prontera'), ())

    def test_map_pack_carries_music_and_monsters(self):
        with tempfile.TemporaryDirectory() as tmp:
            cl = TS.make_client(tmp, wavs=('lunatic_attack.wav', 'lunatic_die.wav', 'poring_damage.wav', 'levelup.wav'), nametable='prt_fild08.rsw#bgm\\\\12.mp3#\n')
            out = os.path.join(tmp, 'o', 'snd_prt_fild08.pack')
            r = A.build('snd_prt_fild08', cl, out)
            self.assertEqual(r['info']['monster_clips'], 3, 'lunatic atk + die, poring hurt (the others have no wav in this client)')
            self.assertGreaterEqual(r['clips'], 3)
            self.assertTrue(os.path.getsize(out) > 0)


@unittest.skipUnless(os.environ.get('RO_CLIENT'), 'RO_CLIENT not set (real client leg)')
class RealClient(unittest.TestCase):
    def test_every_map(self):
        v = grf.Vfs(os.environ['RO_CLIENT'])
        from ro_conv import rofmt
        fog = atmo.parse_fog(v.read('data\\fogparametertable.txt').decode('cp949', 'replace'))
        self.assertGreater(len(fog), 200)
        bgm = A.parse_nametable(v.read('data\\mp3nametable.txt').decode('cp949', 'replace'))
        nofog, nobgm = [], []
        for m in api.MAP_ORDER:
            rw = rofmt.rsw(v.read('data\\%s.rsw' % m))
            t = atmo.build_section(v, m, rw['light'], [])
            d = atmo.dec_ratm(t[3])
            if not d['fog']:
                nofog.append(m)
            else:
                self.assertTrue(0 < d['near'] < d['far'] <= 1000, m)
            if d['bgm'] < 0:
                nobgm.append(m)
            else:
                self.assertEqual(d['bgm'], atmo.bgm_number(bgm[m]), m)
        print('  real client: %d maps, no fog entry: %s, no bgm entry: %s' % (len(api.MAP_ORDER), nofog, nobgm))
        for town in ('prontera', 'geffen', 'payon', 'alberta', 'morocc'):
            self.assertNotIn(town, nofog, town + ' has fog in the original')
        self.assertTrue(set(nobgm) <= {'job_sword1'})


if __name__ == '__main__':
    unittest.main()
