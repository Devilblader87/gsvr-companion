"""ROSE w23 MUSIC: full-length zone BGM in SNDS (companion-owned rose_conv/rose_music.py), on synthetic data (no game files, made-up tracks; the ogg decoder is replaced
by a generator, so neither miniaudio nor ffmpeg is needed). docs/design/rose_music_full.md

  cd companion && python3 -I test_rose_music.py
"""
import array
import math
import os
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401  (puts the converter folder on sys.path, installs the wrappers)
import rose_audio as AU  # noqa: E402
import rose_music as M  # noqa: E402

RATE = AU.MUSIC_RATE


def _tone(seconds, rate=RATE, end_value=None):
    n = int(seconds * rate)
    a = array.array('f', [0.3 * math.sin(i * 0.05) for i in range(n)])
    if end_value is not None:
        a[-1] = end_value
    return a


def _tables(click_path):
    rows = [[''] for _ in range(AU.FS_BUTTON)] + [[click_path]]
    return dict(sound=dict(cells=rows, keys=[]), npc=dict(cells=[]), skill=dict(cells=[]), weapon=dict(cells=[]), hitsound=dict(cells=[]))


class _Client:
    """temp client folder: Sound/ui/click.wav (16 bit, 0.2 s) + Sound/BGM/day.ogg / night.ogg (placeholder files: decode_music is patched)"""
    def __init__(self, tracks):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name
        os.makedirs(os.path.join(r, 'Sound', 'ui'))
        os.makedirs(os.path.join(r, 'Sound', 'BGM'))
        with wave.open(os.path.join(r, 'Sound', 'ui', 'click.wav'), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(i * 0.3))) for i in range(4410)))
        for nm in tracks:
            with open(os.path.join(r, 'Sound', 'BGM', nm), 'wb') as f:
                f.write(b'OggS')
        self.root = r
        self.tracks = tracks
        self.decoded = []

    def decode(self, path, rate, seconds):
        self.decoded.append((os.path.basename(path), seconds))
        full = self.tracks[os.path.basename(path)]
        n = int(seconds * rate) if seconds else len(full)
        return array.array('f', full[:n])

    def close(self):
        self.tmp.cleanup()


class MusicFull(unittest.TestCase):
    def setUp(self):
        self._dec = AU.decode_music
        self.tables = _tables('Sound\\ui\\click.wav')

    def tearDown(self):
        AU.decode_music = self._dec

    def _build(self, tracks, cells, **kw):
        c = _Client(tracks)
        AU.decode_music = c.decode
        try:
            out = AU.build_clips(c.root, self.tables, cells, [], **kw)
        finally:
            c.close()
        return out, c

    def test_installed_once(self):
        self.assertIsNotNone(M._orig)
        self.assertIsNot(AU.build_clips, M._orig)
        w = AU.build_clips
        M.install()
        self.assertIs(AU.build_clips, w, "install() is idempotent")

    def test_whole_track_natural_loop(self):
        """Breezy Hills case: a 114.2 s track (longer than the old 64 s excerpt) is stored whole, loop 0..end, no cross fade, float32 in the section"""
        trk = _tone(114.2)
        (clips, info, warn), c = self._build({'day.ogg': trk}, [''] * 5 + ['Sound\\BGM\\day.ogg', 'Sound\\BGM\\day.ogg'])
        bgm = [x for x in clips if x['name'] == 'bgm/day']
        self.assertEqual(len(bgm), 1)
        self.assertIs(clips[0], bgm[0], "bgm/day first (the vendored order)")
        self.assertEqual(len(bgm[0]['samples']), len(trk))
        self.assertEqual(bgm[0]['loop'], (0, len(trk)))
        self.assertEqual(list(bgm[0]['samples'][:1000]), list(trk[:1000]), "the start is the original start (no cross fade in front)")
        m = info['music']['bgm/day']
        self.assertTrue(m['full_track'])
        self.assertFalse(m['crossfade_loop'])
        self.assertAlmostEqual(m['seconds'], 114.2, places=1)
        self.assertEqual(warn, [])
        self.assertNotIn('bgm/night', info['music'], "night music stays off by default (the world plays bgm/day only)")
        sec = AU.enc_snds(clips)
        dec = {d['name']: d for d in AU.dec_snds(sec)}
        self.assertEqual(dec['bgm/day']['count'], len(trk))
        self.assertEqual((dec['bgm/day']['loop_start'], dec['bgm/day']['loop_end']), (0, len(trk)))
        self.assertLess(abs(dec['bgm/day']['samples'][5000] - trk[5000]), 1e-6, "samples stay float32 (old worlds BlockCopy the block as float[])")
        self.assertEqual(info['sample_bytes'], len(trk) * 4 + len(dec['ui/click']['samples']) * 4)

    def test_effects_unchanged(self):
        """the effects are exactly what the vendored function builds (same budget, effects cap budget // 4)"""
        (clips, info, _w), _c = self._build({'day.ogg': _tone(20)}, [''] * 5 + ['Sound\\BGM\\day.ogg'])
        c2 = _Client({'day.ogg': _tone(20)})
        try:
            ref, rinfo, _ = M._orig(c2.root, self.tables, [''] * 5 + ['Sound\\BGM\\day.ogg'], [], music=False)
        finally:
            c2.close()
        fx = [(x['name'], list(x['samples'])) for x in clips if not x['name'].startswith('bgm/')]
        self.assertEqual(fx, [(x['name'], list(x['samples'])) for x in ref])
        self.assertEqual(info['effects'], rinfo['effects'])
        self.assertEqual(info['skipped'], rinfo['skipped'])

    def test_declick(self):
        x = _tone(5, end_value=0.25)
        x[0] = 0.0
        n = len(x)
        self.assertTrue(M.declick_seam(x, RATE))
        self.assertEqual(len(x), n)
        self.assertLess(abs(x[-1] - x[0]), 0.02, "the loop end meets the start")
        y = _tone(5, end_value=0.01)
        y[0] = 0.0
        y0 = array.array('f', y)
        self.assertFalse(M.declick_seam(y, RATE), "a smooth seam is left alone")
        self.assertEqual(y, y0)
        self.assertFalse(M.declick_seam(array.array('f', [0.0, 0.9]), RATE), "shorter than the ramp")
        # in the build: a loud track end gets the ramp, the rest of the track is untouched
        trk = _tone(30, end_value=-0.6)
        (clips, info, _w), _c = self._build({'day.ogg': trk}, [''] * 5 + ['Sound\\BGM\\day.ogg'])
        bgm = clips[0]['samples']
        self.assertTrue(info['music']['bgm/day']['declick'])
        k = int(M.DECLICK_SECONDS * RATE)
        self.assertEqual(list(bgm[:len(trk) - k]), list(trk[:len(trk) - k]))

    def test_budget_and_night(self):
        """MUSIC_BUDGET is per track: day and night of the longest zone both stay whole; a track longer than the budget falls back to the old excerpt
        + cross fade, with a warning"""
        cap = M.track_seconds_cap(M.MUSIC_BUDGET, RATE)
        self.assertGreater(cap, 152.8, "the longest retail BGM (Eldeon 152.8 s) fits whole")
        cells = [''] * 5 + ['Sound\\BGM\\day.ogg', 'Sound\\BGM\\night.ogg']
        (clips, info, warn), c = self._build({'day.ogg': _tone(152.8), 'night.ogg': _tone(140.0)}, cells, night_music=True)
        self.assertTrue(info['music']['bgm/day']['full_track'] and info['music']['bgm/night']['full_track'], "day + night both whole (per-track budget)")
        small = M.MUSIC_BUDGET // 4
        cap = M.track_seconds_cap(small, RATE)
        (clips, info, warn), c = self._build({'day.ogg': _tone(40), 'night.ogg': _tone(cap + 20)}, cells, night_music=True, music_budget=small)
        self.assertEqual([x['name'] for x in clips if x['name'].startswith('bgm/')], ['bgm/day', 'bgm/night'])
        self.assertTrue(info['music']['bgm/day']['full_track'])
        n = info['music']['bgm/night']
        self.assertFalse(n['full_track'])
        self.assertTrue(n['crossfade_loop'])
        self.assertLessEqual(n['seconds'], cap + 0.01)
        self.assertTrue(any('night.ogg is longer' in w for w in warn), warn)
        self.assertLessEqual(info['sample_bytes'], 2 * small + AU.AUDIO_BUDGET // 4)

    def test_music_seconds_still_caps(self):
        """--music-seconds (command line converter option) keeps working as an upper bound"""
        (clips, info, _w), c = self._build({'day.ogg': _tone(100)}, [''] * 5 + ['Sound\\BGM\\day.ogg'], music_seconds=30)
        self.assertFalse(info['music']['bgm/day']['full_track'])
        self.assertLessEqual(info['music']['bgm/day']['seconds'], 30.01)

    def test_no_music_cell_or_file(self):
        (clips, info, warn), _c = self._build({}, [''] * 5 + ['Sound\\BGM\\gone.ogg'])
        self.assertEqual([x['name'] for x in clips], ['ui/click'])
        self.assertTrue(any('not found' in w for w in warn), warn)
        (clips, info, warn), _c = self._build({}, [], music=False)
        self.assertEqual(info['music'], {})

    def test_pack_rev(self):
        """every pack with SNDS rebuilds once for the full music; the avatar pack (no SNDS) keeps its version"""
        self.assertEqual(M.MUSIC_REV, 1)
        self.assertEqual(rose_conv.PACK_REV["avatar"], 5)
        self.assertGreaterEqual(rose_conv.PACK_REV["breezyhills"], 2)


if __name__ == '__main__':
    unittest.main()
