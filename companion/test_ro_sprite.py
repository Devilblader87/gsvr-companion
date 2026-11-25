"""RO sprite lane (companion/ro_conv/sprite.py): SPR / ACT parsing, frame composing, atlas + animation table, on SYNTHETIC files only (made-up
pixels written by sprite.write_spr / write_act, no game data). docs/design/ro_sprites.md.

  cd companion && python3 -I test_ro_sprite.py
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ro_conv import sprite as S  # noqa: E402
import rpg1 as P  # noqa: E402


def _pal():
    pal = [(0, 0, 0)] * 256
    pal[1] = (255, 0, 0)
    pal[2] = (0, 255, 0)
    pal[3] = (0, 0, 255)
    return pal


def _block(w, h, c):
    return (w, h, bytes([c]) * (w * h))


def _layer(x, y, spr, **kw):
    d = dict(x=x, y=y, spr=spr, mirror=False, rgba=(255, 255, 255, 255), sx=1.0, sy=kw.get('sx', 1.0), angle=0, type=0)
    d.update(kw)
    return d


def _fixture_body():
    """2 anims x 8 dirs: idle (3 head-turn frames), walk (2 frames). frame 0 = 4x6 red, 1 = 4x6 green, 2 = 2x2 blue (asymmetric for mirror)."""
    spr = S.write_spr([_block(4, 6, 1), _block(4, 6, 2), (2, 1, bytes([3, 0]))], palette=_pal())
    acts = []
    for a in range(16):
        n = 3 if a < 8 else 2
        frames = []
        for f in range(n):
            frames.append(dict(layers=[_layer(0, -3, f % 2, mirror=(a % 8) >= 5)], event=-1, anchors=[(1, -10)]))
        acts.append(frames)
    return spr, S.write_act(acts, delays=[4.0] * 8 + [6.0] * 8)


def _fixture_head():
    spr = S.write_spr([_block(4, 4, 3)], palette=_pal())
    acts = [[dict(layers=[_layer(0, -2, 0)], event=-1, anchors=[(0, 1)])] * (3 if a < 8 else 2) for a in range(16)]
    return spr, S.write_act(acts)


class SprTest(unittest.TestCase):
    def test_rle_roundtrip(self):
        idx = bytes([0, 0, 0, 5, 0, 7, 7] + [0] * 300 + [9])
        self.assertEqual(S.rle_decode(S.rle_encode(idx), len(idx)), idx)

    def test_parse_versions(self):
        for ver in (0x0100, 0x0101, 0x0200, 0x0201):
            rgba = [(1, 2, bytes((10, 20, 30, 40, 50, 60, 70, 80)))] if ver >= 0x0200 else []
            b = S.write_spr([_block(3, 2, 1), (2, 2, bytes([0, 1, 2, 0]))], rgba, palette=_pal() if ver >= 0x0101 else None, version=ver)
            s = S.parse_spr(b)
            self.assertEqual(s['version'], ver)
            self.assertEqual(s['pal'][1], (2, 2, bytes([0, 1, 2, 0])))
            if ver >= 0x0200:
                # stored ABGR bottom-up, returned RGBA top-first
                self.assertEqual(s['rgba'][0], rgba[0])
            if ver >= 0x0101:
                self.assertEqual(s['palette'][1], (255, 0, 0))

    def test_rgba_storage_is_abgr_bottom_up(self):
        b = S.write_spr([], [(1, 2, bytes((1, 2, 3, 4, 5, 6, 7, 8)))], palette=_pal())
        # header 'SP' ver(2) n_pal(2) n_rgba(2) w h then the bottom row first, A B G R
        self.assertEqual(b[12:16], bytes((8, 7, 6, 5)))

    def test_palette_index0_transparent(self):
        s = S.parse_spr(S.write_spr([(2, 1, bytes([0, 1]))], palette=_pal()))
        w, h, px = S.spr_image(s, 0, 0)
        self.assertEqual(px, bytes((0, 0, 0, 0, 255, 0, 0, 255)))

    def test_bad(self):
        with self.assertRaises(S.FormatError):
            S.parse_spr(b'XX\x01\x02\0\0')
        with self.assertRaises(S.FormatError):
            S.parse_spr(S.write_spr([_block(4, 4, 1)], palette=_pal(), version=0x0200)[:20])


class ActTest(unittest.TestCase):
    def test_versions(self):
        acts = [[dict(layers=[_layer(3, -4, 0, mirror=True, rgba=(255, 128, 255, 255), sx=2.0, sy=1.5, angle=90, type=0)],
                      event=2, anchors=[(5, -7)])]]
        for ver in (0x0200, 0x0201, 0x0202, 0x0203, 0x0204, 0x0205):
            a = S.parse_act(S.write_act(acts, delays=[3.0], events=[b'atk.wav'], version=ver))
            L = a['actions'][0][0]['layers'][0]
            self.assertEqual((L['x'], L['y'], L['spr'], L['mirror'], L['angle']), (3, -4, 0, True, 90))
            self.assertEqual(L['sy'], 1.5 if ver >= 0x0204 else 2.0)
            self.assertEqual(a['actions'][0][0]['anchors'], [(5, -7)] if ver >= 0x0203 else [])
            self.assertEqual(a['delays'][0], 75.0 if ver >= 0x0202 else 100.0)
            self.assertEqual(a['events'], [b'atk.wav'] if ver >= 0x0201 else [])

    def test_truncated(self):
        b = S.write_act([[dict(layers=[_layer(0, 0, 0)], event=-1, anchors=[])]])
        with self.assertRaises(S.FormatError):
            S.parse_act(b[:30])


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.spr = S.parse_spr(S.write_spr([(2, 1, bytes([1, 2])), _block(2, 2, 3)], palette=_pal()))

    def test_centre_and_mirror(self):
        l, t, w, h, px = S.compose_frame(self.spr, dict(layers=[_layer(0, 0, 0)]))
        self.assertEqual((l, t, w, h), (-1, 0, 2, 1))
        self.assertEqual(px[:4], bytes((255, 0, 0, 255)))
        l, t, w, h, px = S.compose_frame(self.spr, dict(layers=[_layer(0, 0, 0, mirror=True)]))
        self.assertEqual(px[:4], bytes((0, 255, 0, 255)))

    def test_two_layers_bbox(self):
        l, t, w, h, px = S.compose_frame(self.spr, dict(layers=[_layer(0, 0, 0), _layer(5, -5, 1)]))
        self.assertEqual((l, t, w, h), (-1, -6, 7, 7))

    def test_scale_rotate_tint(self):
        l, t, w, h, px = S.compose_frame(self.spr, dict(layers=[_layer(0, 0, 1, sx=2.0, sy=2.0)]))
        self.assertEqual((w, h), (4, 4))
        self.assertTrue(all(px[i + 3] == 255 for i in range(0, len(px), 4)))
        l, t, w, h, px = S.compose_frame(self.spr, dict(layers=[_layer(0, 0, 0, angle=90)]))
        self.assertEqual((w, h), (1, 2))
        l, t, w, h, px = S.compose_frame(self.spr, dict(layers=[_layer(0, 0, 1, rgba=(255, 255, 255, 128))]))
        self.assertEqual(px[3], 128)

    def test_empty(self):
        self.assertEqual(S.compose_frame(self.spr, dict(layers=[_layer(0, 0, -1)])), (0, 0, 0, 0, b''))


class AtlasTest(unittest.TestCase):
    def test_no_overlap_and_pages(self):
        sizes = [(30, 40)] * 20 + [(7, 3)] * 50 + [(0, 0)]
        pages, places = S.pack_atlas(sizes, max_page=128)
        self.assertGreater(len(pages), 1)
        rects = {}
        for (w, h), (pg, x, y) in zip(sizes, places):
            if not w:
                continue
            W, H = pages[pg]
            self.assertTrue(x + w <= W and y + h <= H)
            for (pg2, x2, y2, w2, h2) in rects.get(pg, []):
                self.assertTrue(x + w <= x2 or x2 + w2 <= x or y + h <= y2 or y2 + h2 <= y)
            rects.setdefault(pg, []).append((pg, x, y, w, h))

    def test_last_page_half_height(self):
        # 8 frames of 60 x 30 = 4 shelves of 31 px = 124 px: needs the full 128 x 128
        pages, _ = S.pack_atlas([(60, 30)] * 8, max_page=1024)
        self.assertEqual(pages, [(128, 128)])
        pages, places = S.pack_atlas([(60, 30)] * 4, max_page=1024)       # 2 shelves = 62 px: fits 128 x 64 (half the RGBA32 bytes)
        self.assertEqual(pages, [(128, 64)])
        for pg, x, y in places:
            self.assertTrue(x + 60 <= 128 and y + 30 <= 64)
        pages, _ = S.pack_atlas([(200, 20)], max_page=1024)               # wider than 64 / 128: 256 wide, height floor 64
        self.assertEqual(pages, [(256, 64)])

    def test_too_big(self):
        with self.assertRaises(S.FormatError):
            S.pack_atlas([(300, 10)], max_page=256)


class SetTest(unittest.TestCase):
    def test_player_set(self):
        bs, ba = _fixture_body()
        hs, ha = _fixture_head()
        ps, pa = S.write_spr([_block(3, 3, 2)], palette=_pal()), S.write_act([[dict(layers=[_layer(0, -1, 0)], event=-1, anchors=[])]] * 40)
        sp = [S.convert_sprite('body_m', S.KIND_BODY, bs, ba, anims=(0, 1)),
              S.convert_sprite('head_m', S.KIND_HEAD, hs, ha, anims=(0, 1)),
              S.convert_sprite('poring', S.KIND_MONSTER, ps, pa)]
        pack = S.write_set_pack(sp)
        rp = P.read_pack(pack)
        rs = S.dec_rsan(rp['sections'][S.TAG_RSAN]['data'])
        pages = S.dec_rsat(rp['sections'][S.TAG_RSAT]['data'])
        self.assertEqual([s['name'] for s in rs['sprites']], ['body_m', 'head_m', 'poring'])
        self.assertEqual([s['clips'] for s in rs['sprites']], [16, 16, 40])
        body = rs['sprites'][0]
        # idle is reduced to 1 frame (head-turn poses), walk keeps 2, walk delay 6 ticks = 150 ms
        c_idle = rs['clips'][body['first_clip'] + 0]
        c_walk = rs['clips'][body['first_clip'] + 8]
        self.assertEqual((c_idle[1], c_walk[1], c_idle[2], c_walk[2]), (1, 2, 100, 150))
        # frame record: offset from the feet + anchor
        pg, flags, x, y, w, h, left, top, ax, ay = rs['frames'][c_idle[0]]
        self.assertEqual((w, h, left, top, ax, ay, flags & S.FF_ANCHOR), (4, 6, -2, -6, 1, -10, 1))
        # pixels at the stored rect (rows bottom-first in the page) are the red block
        W, H, data = pages[pg]
        row = H - 1 - y
        self.assertEqual(data[(row * W + x) * 4:(row * W + x) * 4 + 4], bytes((255, 0, 0, 255)))
        # ref height: body top -6, head top -4 moved by body anchor y (-10) - head anchor y (1) = -15
        self.assertEqual(rs['ref_height_px'], 15.0)
        # identical composites are stored once: body uses 2 colours x (plain, mirrored is identical for a solid block) -> few images
        self.assertLessEqual(sum(1 for _ in sp[0]['images']), 2)

    def test_build_player_set_reader(self):
        bs, ba = _fixture_body()
        hs, ha = _fixture_head()
        ps, pa = S.write_spr([_block(3, 3, 2)], palette=_pal()), S.write_act([[dict(layers=[_layer(0, -1, 0)], event=-1, anchors=[])]] * 40)
        files = {S.PATH_BODY['f'] + '.spr': bs, S.PATH_BODY['f'] + '.act': ba, S.PATH_HEAD['f'] + '.spr': hs, S.PATH_HEAD['f'] + '.act': ha,
                 S.PATH_PORING + '.spr': ps, S.PATH_PORING + '.act': pa}
        pack = S.build_player_set(files.__getitem__, sex='f')
        rs = S.dec_rsan(P.read_pack(pack)['sections'][S.TAG_RSAN]['data'])
        self.assertEqual(rs['sprites'][0]['name'], 'body_f')
        # GRF names are CP949: every client path must encode
        for p in files:
            p.encode('cp949')

    def test_frame_record_size(self):
        self.assertEqual((S.RSAN_HDR.size, S.RSAN_SPRITE.size, S.RSAN_CLIP.size, S.RSAN_FRAME.size, S.RSAT_PAGE.size), (36, 24, 8, 20, 20))


if __name__ == '__main__':
    unittest.main()
