"""ro_conv audio + effects (w36 sound lane) on SYNTHETIC fixtures written by this test (no game data anywhere; the mp3 decoder is replaced by a generator).
docs/design/ro_sound.md

  cd companion && python3 -I test_ro_sound.py
Optional real-data leg: RO_CLIENT=<client folder> python3 -I test_ro_sound.py  (parses every .str of the install; skipped otherwise).
"""
import array
import io
import math
import os
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ro_conv  # noqa: E402
from ro_conv import grf, img, audio as A, strfx as S  # noqa: E402
import rpg1 as P  # noqa: E402
import rose_audio as AU  # noqa: E402


def wav_bytes(seconds=0.3, rate=22050, freq=440.0):
    n = int(seconds * rate)
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(i * freq * 6.2831853 / rate))) for i in range(n)))
    return b.getvalue()


def key(frame, typ, pos=(320.0, 292.0), xy=(32, -32, -32, 32, -32, -32, 32, 32), color=(255.0, 255.0, 255.0, 128.0), angle=0.0, src=5, dst=7, anitype=0, aniframe=0.0, delay=0.0):
    return dict(frame=frame, type=typ, pos=[float(v) for v in pos], xy=[float(v) for v in xy], angle=angle, aniframe=aniframe, anitype=anitype, delay=delay, color=list(color), src=src, dst=dst)


def sample_effect():
    """two layers: layer 0 moves down 32.25 px / frame from frame 10 and fades; layer 1 is a 3 texture flip book"""
    l0 = dict(textures=['glow1.bmp'], keys=[key(10, 0, pos=(320, 81)), key(10, 1, pos=(0, 32.25), xy=(0,) * 8, color=(0, 0, 0, -3.2)), key(14, 0, pos=(320, 210)), key(20, 0, pos=(320, 210))])
    l1 = dict(textures=['f0.bmp', 'f1.bmp', 'f2.bmp'], keys=[key(0, 0, anitype=2, delay=0.5), key(30, 0, anitype=2, delay=0.5)])
    return dict(version=148, fps=30, max_frame=30, layers=[l0, l1])


def make_client(tmp, wavs=(), strs=(), textures=(), nametable=None, bgm=None):
    """client folder with data.grf (GRF 0x200: wav\\, texture\\effect\\, mp3nametable.txt) and an optional loose BGM\\ folder"""
    files = {}
    for w in wavs:
        files['data\\wav\\' + w] = wav_bytes()
    for nm, e in strs:
        files['data\\texture\\effect\\' + nm] = S.write_str(e)
    for t in textures:
        px = bytes((200, 100, 50, 255)) * (16 * 16)
        files['data\\texture\\effect\\' + t] = img.write_bmp24(16, 16, px)
    if nametable is not None:
        files['data\\mp3nametable.txt'] = nametable.encode('cp949')
    grf.write_grf(os.path.join(tmp, 'data.grf'), files)
    if bgm:
        os.makedirs(os.path.join(tmp, 'BGM'))
        for nm in bgm:
            with open(os.path.join(tmp, 'BGM', nm), 'wb') as f:
                f.write(b'ID3 not really an mp3')
    return tmp


class NameTable(unittest.TestCase):
    def test_parse(self):
        t = A.parse_nametable('//맵이름.rsw#bgm\\\\음악번호#\n\nprt_church.rsw#bgm\\\\10.mp3#\nPrOnTeRa.rsw#bgm\\\\08.mp3#\n//alberta.rsw#bgm\\\\54.mp3#\nprontera.rsw#bgm\\\\99.mp3#\n')
        self.assertEqual(t['prt_church'], 'bgm\\10.mp3')
        self.assertEqual(t['prontera'], 'bgm\\99.mp3', 'a later line wins, case-insensitive map name')
        self.assertNotIn('alberta', t, 'a commented line is ignored')
        self.assertIsNone(A.bgm_for(t, 'geffen'))
        self.assertEqual(A.bgm_for(t, 'PRT_CHURCH'), 'bgm\\10.mp3')


class BgmLookup(unittest.TestCase):
    """w42 r154: the BGM folder next to data.grf is found whatever its case / the separators of the table (Linux container, Proton), and a cached pack built without it goes stale"""

    def test_case_and_separators(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, 'bGm'))
            with open(os.path.join(t, 'bGm', '08.MP3'), 'wb') as f:
                f.write(b'x')
            for rel in ('bgm\\08.mp3', 'BGM/08.mp3', 'Bgm\\\\08.Mp3'):
                hit = A.find_bgm(t, None, rel)
                self.assertIsNotNone(hit, rel)
                self.assertEqual(hit[0], 'file')
            self.assertIsNone(A.find_bgm(t, None, 'bgm\\09.mp3'))

    def test_install_extra_tracks_bgm_and_decoder(self):
        with tempfile.TemporaryDirectory() as t:
            make_client(t)
            before = ro_conv.install_extra(t)
            self.assertEqual(before[0].split()[:2], ['bgm', '0'])
            os.makedirs(os.path.join(t, 'bgm'))
            with open(os.path.join(t, 'bgm', '01.mp3'), 'wb') as f:
                f.write(b'x')
            after = ro_conv.install_extra(t)
            self.assertNotEqual(before, after, 'copying BGM\\ next to data.grf changes the fingerprint lines (cached music-less packs rebuild)')
            self.assertEqual(after[0].split()[:3], ['bgm', '1', '1'])
            self.assertTrue(after[1].startswith('decoder '))


class StrFormat(unittest.TestCase):
    def test_roundtrip(self):
        e = sample_effect()
        b = S.write_str(e)
        self.assertEqual(len(b), 36 + (4 + 128 + 4 + 4 * 124) + (4 + 3 * 128 + 4 + 2 * 124), 'header 36 + per layer 4 + 128 x tex + 4 + 124 x keys')
        p = S.parse_str(b)
        self.assertEqual(p['fps'], 30)
        self.assertEqual(len(p['layers']), 2)
        self.assertEqual(p['layers'][1]['textures'], ['f0.bmp', 'f1.bmp', 'f2.bmp'])
        self.assertEqual(S.write_str(p), b, 'write(parse(x)) == x')
        self.assertAlmostEqual(p['layers'][0]['keys'][1]['pos'][1], 32.25)

    def test_rejects(self):
        b = S.write_str(sample_effect())
        for bad, why in ((b[:-1], 'truncated'), (b + b'\0', 'trailing byte'), (b'XTRM' + b[4:], 'magic'), (b[:20], 'short')):
            with self.assertRaises(S.StrError, msg=why):
                S.parse_str(bad)

    def test_angle_units(self):
        e = sample_effect()
        e['layers'][0]['keys'][0]['angle'] = 90.0
        p = S.parse_str(S.write_str(e))
        self.assertAlmostEqual(p['layers'][0]['keys'][0]['angle'], 90.0, places=3, msg='1024 file units = 360 degrees, parsed to degrees')


class Semantics(unittest.TestCase):
    def test_base_plus_per_frame_delta(self):
        ks = sample_effect()['layers'][0]['keys']
        s = S.sample_layer(ks, 12)
        self.assertAlmostEqual(s['pos'][1], 81 + 2 * 32.25)
        self.assertAlmostEqual(S.sample_layer(ks, 14)['pos'][1], 210.0, msg='the next base key is the explicit state (81 + 4 x 32.25)')
        self.assertAlmostEqual(S.sample_layer(ks, 17)['pos'][1], 210.0, msg='no animated key after the base key: the state is held')
        self.assertAlmostEqual(S.sample_layer(ks, 12)['color'][3], 128.0 - 3.2 * 2)

    def test_visibility(self):
        ks = sample_effect()['layers'][0]['keys']
        self.assertIsNone(S.sample_layer(ks, 9), 'before the first key')
        self.assertIsNotNone(S.sample_layer(ks, 10))
        self.assertIsNotNone(S.sample_layer(ks, 20))
        self.assertIsNone(S.sample_layer(ks, 21), 'after the last key')
        self.assertIsNone(S.sample_layer([], 0))

    def test_texture_frame(self):
        ks = sample_effect()['layers'][1]['keys']
        self.assertEqual([S.texture_frame(S.sample_layer(ks, f), f, 3) for f in (0, 2, 4, 6, 8)], [0, 1, 2, 0, 1], 'anitype 2 = +0.5 per frame, looping')
        st = dict(aniframe=0.0, anitype=1, delay=1.0, start=0)
        self.assertEqual(S.texture_frame(st, 9, 3), 2, 'anitype 1 clamps to the last texture')
        self.assertEqual(S.texture_frame(st, 9, 1), 0)

    def test_corner_world(self):
        st = dict(pos=[320.0, 292.0], xy=[35, -35, -35, 35, -35, -35, 35, 35], angle=0.0)
        x, y = S.corner_world(st, 0)
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(y, 1.0, msg='STR y is down: -35 px = 1 m up')
        st['angle'] = 90.0
        x, y = S.corner_world(st, 0)
        self.assertAlmostEqual(x, 1.0, msg='90 degrees about the centre moves the right-top corner to the right-bottom position (y down in STR: x stays, y flips)')
        self.assertAlmostEqual(y, -1.0)
        x, y = S.corner_world(dict(pos=[320.0 + 35, 292.0 - 35], xy=[0, 0, 0, 0, 0, 0, 0, 0], angle=45.0), 2, 2.0)
        self.assertAlmostEqual(x, 2.0)
        self.assertAlmostEqual(y, 2.0, msg='a zero-size layer at pos (+35, -35) px sits 1 m right and up, scaled x2')
        self.assertEqual(S.blend_class(5, 7), S.BLEND_ADD)
        self.assertEqual(S.blend_class(5, 6), S.BLEND_ALPHA)


class Sections(unittest.TestCase):
    def test_refx_roundtrip(self):
        e = sample_effect()
        p = S.parse_str(S.write_str(e))
        d = S.enc_refx([('fx_a', p), ('fx_b', p)], {'glow1.bmp': 4, 'f0.bmp': 5})
        r = S.dec_refx(d)
        self.assertEqual([x['name'] for x in r], ['fx_a', 'fx_b'])
        self.assertEqual(r[0]['layers'][0]['texslots'], [4])
        self.assertEqual(r[1]['layers'][1]['texslots'], [5, -1, -1], 'a texture that is not in the atlas gets slot -1')
        k = r[0]['layers'][0]['keys']
        self.assertEqual(len(k), 4)
        self.assertEqual(k[1]['type'], 1)
        self.assertAlmostEqual(k[1]['pos'][1], 32.25, places=3)
        self.assertEqual(len(d) % 4, 0)
        with self.assertRaises(S.StrError):
            S.dec_refx(d + b'\0\0\0\0')

    def test_rftx(self):
        px = [bytes((i * 40 % 256, 20, 30, 255)) * (S.SLOT_PX * S.SLOT_PX) for i in range(3)]
        at = S.build_atlas(px)
        self.assertEqual((at['cols'], at['w'], at['h']), (2, 256, 256))
        d = S.enc_rftx(at)
        r = S.dec_rftx(d)
        self.assertEqual((r['slots'], r['mips'], r['fmt']), (3, 3, at['fmt']))
        with self.assertRaises(S.StrError):
            S.dec_rftx(d[:-16])


S_STANDINS = ('portal', 'warp', 'magnumbreak', 'bash')


class Packs(unittest.TestCase):
    def build(self, folder, name, **kw):
        out = os.path.join(folder, 'out_' + name + '.pack')
        r = ro_conv.build(name, folder, out, **kw)
        with open(out, 'rb') as f:
            raw = f.read()
        self.assertEqual(r['bytes'], len(raw))
        return r, P.read_pack(raw, verify=True)

    def test_common(self):
        with tempfile.TemporaryDirectory() as t:
            client = make_client(t, wavs=['levelup.wav', 'effect\\ef_portal.wav', '_attack_sword.wav', '_hit_sword.wav', '_enemy_hit1.wav', 'poring_attack.wav', 'poring_damage.wav',
                                          'poring_die.wav', 'effect\\ef_bash.wav'],
                                 strs=[('h_levelup.str', sample_effect())], textures=['glow1.bmp', 'f0.bmp', 'f1.bmp', 'f2.bmp', 'ring_blue.tga', 'whitelight.tga'])
            r, p = self.build(client, 'snd_common')
            self.assertEqual(p['order'], [b'SNDS'], 'w43 fx: snd_common has no effects any more (the roefx pack carries them, test_ro_fx.py)')
            names = sorted(c['name'] for c in AU.dec_snds(p['sections'][b'SNDS']['data']))
            self.assertEqual(names, ['fx/hit/sword', 'fx/hurt/0', 'fx/levelup', 'fx/portal', 'fx/swing/sword', 'mon/poring/atk', 'mon/poring/die', 'mon/poring/hurt', 'skill/5/fire'],
                             'exactly the wavs the client has (missing ones are skipped)')
            out2 = os.path.join(client, 'out_old.pack')
            r2 = A.build('snd_common', client, out2, effects=list(A.EFFECT_STR) + list(A.STAND_INS))          # the old REFX path stays usable for tools
            with open(out2, 'rb') as f:
                p2 = P.read_pack(f.read(), verify=True)
            self.assertEqual(p2['order'], [b'SNDS', b'REFX', b'RFTX'])
            effs = [e['name'] for e in S.dec_refx(p2['sections'][b'REFX']['data'])]
            self.assertIn('levelup', effs)
            self.assertIn('portal', effs, 'the procedural stand-ins are always written')
            self.assertNotIn('firewall', effs, 'a .str the client lacks is skipped with a warning')
            self.assertTrue(any('firewall' in w for w in r2['warnings']))
            self.assertEqual(p2['sections'][b'RFTX']['count'], r2['info']['textures'])
            lv = [e for e in S.dec_refx(p2['sections'][b'REFX']['data']) if e['name'] == 'levelup'][0]
            self.assertNotEqual(lv['layers'][0]['texslots'][0], -1)
            clips = AU.dec_snds(p['sections'][b'SNDS']['data'])
            self.assertTrue(all(c['loop_start'] == -1 for c in clips), 'effects do not loop')
            self.assertTrue(all(c['count'] <= int(A.SFX_SECONDS * A.SFX_RATE) + 2 for c in clips))

    def test_music(self):
        with tempfile.TemporaryDirectory() as t:
            nt = 'prontera.rsw#bgm\\\\08.mp3#\nprt_in.rsw#bgm\\\\08.mp3#\nizlude.rsw#bgm\\\\26.mp3#\n'
            client = make_client(t, nametable=nt, bgm=['08.mp3'])
            calls = []

            def fake(path, rate, seconds):
                calls.append((os.path.basename(path), rate))
                return array.array('f', [0.4 * math.sin(i * 0.02) + 0.3 for i in range(int(rate * 3.5))])
            orig = AU.decode_music
            AU.decode_music = fake
            try:
                r, p = self.build(client, 'snd_prontera')
                clips = AU.dec_snds(p['sections'][b'SNDS']['data'])
                self.assertEqual([c['name'] for c in clips], ['bgm/prontera'])
                c = clips[0]
                self.assertEqual((c['loop_start'], c['loop_end']), (0, c['count']), 'whole track, natural loop')
                self.assertEqual(calls, [('08.mp3', A.MUSIC_RATE)])
                self.assertLess(abs(c['samples'][0] - c['samples'][-1]), 0.06, 'seam de-clicked')
                r2, p2 = self.build(client, 'snd_izlude')                      # BGM\26.mp3 does not exist
                self.assertEqual(AU.dec_snds(p2['sections'][b'SNDS']['data']), [], 'no BGM file: valid empty pack, the world plays no music')
                self.assertTrue(any('not found' in w for w in r2['warnings']))
                r3, p3 = self.build(client, 'snd_prt_church')                  # no nametable row
                self.assertTrue(any('no music entry' in w for w in r3['warnings']))
            finally:
                AU.decode_music = orig

    def test_long_track_is_cut(self):
        with tempfile.TemporaryDirectory() as t:
            client = make_client(t, nametable='prontera.rsw#bgm\\\\08.mp3#\n', bgm=['08.mp3'])
            orig = AU.decode_music
            AU.decode_music = lambda path, rate, seconds: array.array('f', [0.5] * int(seconds * rate))      # always as long as asked: "longer than the budget"
            try:
                r, p = self.build(client, 'snd_prontera')
                c = AU.dec_snds(p['sections'][b'SNDS']['data'])[0]
                self.assertLessEqual(c['count'] * 4, A.MUSIC_BUDGET, 'the clip fits the pack budget')
                self.assertLess(c['samples'][-int(1.5 * c['rate'])], 0.35, 'faded out over the last 3 s (0.5 -> 0)')
                self.assertLess(abs(c['samples'][0] - c['samples'][-1]), 0.06, 'loop seam de-clicked')
                self.assertTrue(any('cut to the pack budget' in w for w in r['warnings']))
                self.assertLess(r['bytes'], 17000000, 'a music pack stays under the VRChat download budget')
            finally:
                AU.decode_music = orig

    def test_decoder_missing(self):
        with tempfile.TemporaryDirectory() as t:
            client = make_client(t, nametable='prontera.rsw#bgm\\\\08.mp3#\n', bgm=['08.mp3'])

            def boom(*a):
                raise ValueError('no ogg decoder')
            orig = AU.decode_music
            AU.decode_music = boom
            try:
                r, p = self.build(client, 'snd_prontera')
                self.assertEqual(AU.dec_snds(p['sections'][b'SNDS']['data']), [])
                self.assertTrue(any('no ogg decoder' in w for w in r['warnings']))
            finally:
                AU.decode_music = orig

    def test_tables(self):
        self.assertTrue(set(A.SKILL_EFFECT.values()) <= set(A.EFFECT_STR) | set(A.STAND_INS), 'every skill visual is an effect the pack builds')
        self.assertTrue(set(A.STAND_INS) <= set(S_STANDINS), 'every stand-in is implemented')
        for k in A.STAND_INS:
            e = A.stand_in(k)
            self.assertEqual(S.parse_str(S.write_str(e))['max_frame'], e['max_frame'], k + ' survives the STR codec')
            for ly in e['layers']:
                self.assertEqual(ly['keys'][0]['type'], 0, 'a layer starts with a base key')
        names = [n for n, _c in A.sfx_plan()]
        self.assertEqual(len(names), len(set(names)), 'clip names are unique')
        self.assertTrue(all(len(n) < 64 and n.isascii() for n in names), 'names fit the SNDS limit (ASCII, < 64)')
        self.assertTrue(set(A.SKILL_SOUNDS) >= {5, 7, 11, 19, 28}, 'the first job skills have sounds')

    def test_api(self):
        self.assertIn('snd_common', ro_conv.AUDIO_PACKS)
        self.assertTrue(all(('snd_' + m) in ro_conv.AUDIO_PACKS for m in ro_conv.PACKS))
        self.assertFalse(any(k.startswith('snd_') for k in ro_conv.PACKS), 'audio packs are not map packs (the world loads every PACKS entry as a map)')
        self.assertEqual(ro_conv.pack_version('snd_common'), 'ro-snd-%d' % A.AUDIO_REV)
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ro_conv.BuildError):
                ro_conv.build('snd_common', t, os.path.join(t, 'x.pack'))
        with self.assertRaises(Exception):
            ro_conv.build('snd_nope_but_not_audio', '.', 'x')


@unittest.skipUnless(os.environ.get('RO_CLIENT'), 'RO_CLIENT not set (real client leg)')
class RealClient(unittest.TestCase):
    def test_every_str_parses(self):
        v = grf.Vfs(os.environ['RO_CLIENT'])
        n = 0
        for k in sorted(v.names()):
            if k.endswith('.str') and k.startswith('data\\texture\\effect\\'):
                e = S.parse_str(v.read(k))
                self.assertEqual(S.parse_str(S.write_str(e))['layers'][0:1], e['layers'][0:1])
                n += 1
        self.assertGreater(n, 1000)


def write_cross_pack(path):
    """tools/usharp_offline/rosoundrules.cs input: a snd_common pack built from synthetic STR files + `<path>.expected` = the Python sample_layer / texture_frame / corner_world numbers per effect, layer, frame
    that the compiled runtime (GsvrRoFx.LSample / LTexIndex / LCornerX / LCornerY) must reproduce."""
    with tempfile.TemporaryDirectory() as t:
        e2 = dict(version=148, fps=24, max_frame=40, layers=[dict(textures=['glow1.bmp', 'f0.bmp'], keys=[
            key(2, 0, pos=(300, 250), xy=(20, -20, -20, 20, -30, -30, 30, 30), angle=0.0, anitype=1, delay=0.25, color=(255, 200, 100, 200)),
            key(2, 1, pos=(1.5, -2.0), xy=(0.5, 0.5, -0.5, -0.5, 0.25, 0.25, -0.25, -0.25), angle=11.25, color=(0, 5, 10, -4.0)),
            key(20, 0, pos=(330, 210), xy=(40, -40, -40, 40, -40, -40, 40, 40), angle=90.0, color=(255, 255, 255, 64), src=5, dst=6),
            key(30, 0, pos=(330, 210), xy=(40, -40, -40, 40, -40, -40, 40, 40), angle=90.0, color=(255, 255, 255, 0), src=5, dst=6)])])
        client = make_client(t, wavs=['levelup.wav', '_attack_sword.wav', '_hit_sword.wav', '_enemy_hit1.wav', 'poring_attack.wav', 'poring_die.wav', 'effect\\ef_bash.wav', 'effect\\ef_portal.wav'],
                             strs=[('h_levelup.str', sample_effect()), ('provoke.str', e2)], textures=['glow1.bmp', 'f0.bmp', 'f1.bmp', 'f2.bmp'])
        out = os.path.join(t, 'x.pack')
        A.build('snd_common', client, out, effects=list(A.EFFECT_STR) + list(A.STAND_INS))          # w43: the world's snd_common has no REFX any more; the cross check keeps the old path
        raw = open(out, 'rb').read()
        p = P.read_pack(raw)
        effs = S.dec_refx(p['sections'][b'REFX']['data'])
        lines = []
        src = {'levelup': sample_effect(), 'provoke': e2}
        for e in effs:
            if e['name'] not in src:
                continue
            parsed = S.parse_str(S.write_str(src[e['name']]))
            for li, ly in enumerate(parsed['layers']):
                for f in range(0, parsed['max_frame'] + 3):
                    st = S.sample_layer(ly['keys'], f)
                    if st is None:
                        lines.append('%s %d %d 0' % (e['name'], li, f))
                        continue
                    ti = S.texture_frame(st, f, len(ly['textures']))
                    cs = []
                    for c in range(4):
                        x, y = S.corner_world(st, c, 1.5)
                        cs += [x, y]
                    lines.append('%s %d %d 1 %d %d %s %s' % (e['name'], li, f, ti, S.blend_class(st['src'], st['dst']),
                                                             ' '.join('%.4f' % v for v in st['color']), ' '.join('%.4f' % v for v in cs)))
        with open(path, 'wb') as f:
            f.write(raw)
        with open(path + '.names', 'w') as f:
            f.write('\n'.join(n for n, _c in A.sfx_plan()) + '\n')
        with open(path + '.skills', 'w') as f:
            f.write(''.join('%d %s\n' % (i, A.SKILL_EFFECT[i]) for i in sorted(A.SKILL_EFFECT)))
        with open(path + '.expected', 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print('wrote %s (%d bytes), %d expected lines' % (path, len(raw), len(lines)))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write-pack':
        write_cross_pack(sys.argv[2])
    else:
        unittest.main()
