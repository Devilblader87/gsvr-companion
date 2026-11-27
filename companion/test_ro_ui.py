"""RO interface pack 'roui' (w42 lane ui-a, docs/design/ro_ui.md), on SYNTHETIC files only (made-up pixels, nothing of the game):
  - ro_conv/ui.py packs the interface pieces + item / skill icons of a synthetic client into ONE ROUI atlas (magenta -> alpha, rows top first, names the world looks up),
  - missing pieces / icons are warnings, an atlas overflow drops icons (never interface pieces), a client without any piece is refused,
  - the companion contract: UI_PACKS + pack_version + build('roui'), the build manager serves it (rosebuild merges UI_PACKS) and prebuilds it right after the start map,
  - (Mac only, skipped without the owner's client) the real client has every interface piece and every skill icon.

  cd companion && python3 -I test_ro_ui.py
"""
import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ro_conv import api, grf, img, ui, items  # noqa: E402
import test_ro_e2e as E2E  # noqa: E402

REAL_CLIENT = os.environ.get("GSVR_RO_CLIENT", "/path/to/gsvr-testdata Online")
UIP = 'data\\texture\\유저인터페이스\\'


def solid(w, h, c):
    return bytes(c) * (w * h)


def ui_client(folder, pieces=('basewin_bg', 'titlebar_left', 'sys_close_off'), icons=(501,), skills=('sm_bash',)):
    files = E2E.client_files(sprites=False)
    for i, stem in enumerate(pieces):
        w, h = (280, 120) if stem == 'basewin_bg' else (12, 17)
        px = bytearray(solid(w, h, (10 + i, 120, 200, 255)))
        px[0:4] = b'\xff\x00\xff\xff'                                  # one magenta texel: transparent in the atlas
        files[UIP + 'basic_interface\\' + stem + '.bmp'] = img.write_bmp24(w, h, bytes(px))
    lines = []
    for iid in icons:
        files[UIP + 'item\\icon%d.bmp' % iid] = img.write_bmp24(24, 24, solid(24, 24, (200, 50, 50, 255)))
        lines.append('%d#icon%d#' % (iid, iid))
    for s in skills:
        files[UIP + 'item\\' + s + '.bmp'] = img.write_bmp24(24, 24, solid(24, 24, (50, 200, 50, 255)))
    files['data\\idnum2itemresnametable.txt'] = ('\n'.join(lines) + '\n').encode('cp949')
    os.makedirs(folder, exist_ok=True)
    grf.write_grf(os.path.join(folder, 'data.grf'), files)
    return folder


def section(pack):
    """the ROUI section bytes of an RPG1 pack (rpg1.read_pack: CRC checked)."""
    return ui.P.read_pack(pack)['sections'][ui.TAG_ROUI]['data']


class Pack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='roui_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rotxt_section_rides_in_the_pack(self):
        """w43 txt: with System/iteminfo.lub next to the client's data.grf the pack carries a ROTX section after ROUI; without it only ROUI."""
        import test_ro_rotxt as RT
        c = ui_client(os.path.join(self.tmp, 'c'))
        out = os.path.join(self.tmp, 'a.pack')
        api.build('roui', c, out)
        self.assertEqual(sorted(ui.P.read_pack(open(out, 'rb').read())['sections']), [ui.TAG_ROUI])
        os.makedirs(os.path.join(c, 'System'))
        with open(os.path.join(c, 'System', 'iteminfo.lub'), 'wb') as f:
            f.write(RT.Asm().chunk('tbl', RT.ITEMS))
        out = os.path.join(self.tmp, 'b.pack')
        r = api.build('roui', c, out)
        secs = ui.P.read_pack(open(out, 'rb').read())['sections']
        self.assertEqual(sorted(secs), sorted([ui.TAG_ROUI, ui.rotxt.TAG_ROTX]))
        items, _msgs, _marks = ui.rotxt.decode(secs[ui.rotxt.TAG_ROTX]['data'])
        self.assertEqual(items[501][0], 'Red Potion')
        self.assertTrue(set(items) <= set(ui.icon_ids()))                    # only the ids the world uses are packed (bag / drop / shop ids)

    def test_atlas_names_alpha_rows(self):
        c = ui_client(os.path.join(self.tmp, 'c'))
        out = os.path.join(self.tmp, 'ro__roui.pack')
        r = api.build('roui', c, out)
        self.assertEqual(r['name'], 'roui')
        W, H, tab, px = ui.dec_roui(section(open(out, 'rb').read()))
        for n in ('basewin_bg', 'titlebar_left', 'sys_close_off', 'ic_501', 'sk_5', 'white'):
            self.assertIn(n, tab)
        self.assertEqual(tab['ic_501'][:2], (ui.KIND_ICON, 501))
        self.assertEqual(tab['sk_5'][:2], (ui.KIND_SKILL, 5))
        k, iid, x, y, w, h = tab['basewin_bg']
        self.assertEqual((w, h), (280, 120))
        self.assertLessEqual(x + w, W)
        self.assertLessEqual(y + h, H)
        top_left = px[(y * W + x) * 4:(y * W + x) * 4 + 4]
        self.assertEqual(top_left[3], 0)                            # the magenta texel (file row 0 = the image TOP) is transparent, rows top first
        nxt = px[(y * W + x + 1) * 4:(y * W + x + 1) * 4 + 4]
        self.assertEqual(tuple(nxt), (10, 120, 200, 255))
        self.assertTrue(any('interface pieces not in the client' in w for w in r['warnings']))     # the synthetic client has 3 of the pieces

    def test_overflow_drops_icons_not_pieces(self):
        pieces = [('basewin_bg', ui.KIND_UI, 0, 280, 120, solid(280, 120, (1, 2, 3, 255)))]
        pieces += [('ic_%d' % i, ui.KIND_ICON, i, 24, 24, solid(24, 24, (9, 9, 9, 255))) for i in range(600)]
        warn = []
        W, H, rgba, table = ui.build_atlas(pieces, max_page=512, warn=warn)
        names = [t[0] for t in table]
        self.assertIn('basewin_bg', names)
        self.assertLess(len(names), 601)
        self.assertTrue(any(w.startswith('ui: atlas full') for w in warn))
        self.assertEqual(W, 512)

    def test_overflow_drops_minimaps_first(self):
        # w42 integrate: minimap pictures (KIND_MAP, ro_conv.uimap) go before any icon when the one page is full
        pieces = [('basewin_bg', ui.KIND_UI, 0, 280, 120, solid(280, 120, (1, 2, 3, 255)))]
        pieces += [('ic_%d' % i, ui.KIND_ICON, i, 24, 24, solid(24, 24, (9, 9, 9, 255))) for i in range(100)]
        pieces += [('map_m%d' % i, ui.KIND_MAP, 0, ui.MAP_PIC, ui.MAP_PIC, solid(ui.MAP_PIC, ui.MAP_PIC, (5, 5, 5, 255))) for i in range(60)]
        warn = []
        W, H, rgba, table = ui.build_atlas(pieces, max_page=512, warn=warn)
        names = [t[0] for t in table]
        self.assertIn('basewin_bg', names)
        self.assertEqual(sum(1 for n in names if n.startswith('ic_')), 100)          # every icon kept
        self.assertLess(sum(1 for n in names if n.startswith('map_')), 60)            # some pictures dropped
        self.assertTrue(any(w.startswith('ui: atlas full') for w in warn))

    def test_minimap_names_match_world(self):
        from ro_conv import uimap
        self.assertEqual(uimap.tex_name('prontera'), 'map_prontera')               # == GsvrRoUiB.LMiniTex
        self.assertGreaterEqual(ui.UI_REV, 2)                                       # the pack rebuilds once with the pictures

    def test_no_pieces_refused(self):
        c = ui_client(os.path.join(self.tmp, 'c'), pieces=())
        with self.assertRaises(api.BuildError):
            api.build('roui', c, os.path.join(self.tmp, 'x.pack'))

    def test_contract(self):
        self.assertIn('roui', api.UI_PACKS)
        self.assertNotIn('roui', api.PACKS)                         # the world loads every PACKS entry as a map
        self.assertEqual(api.PREBUILD_ORDER[:2], (api.MAP_ORDER[0], 'roui'))
        self.assertEqual(api.pack_version('roui'), 'ro-ui-%d' % ui.UI_REV)
        import rosebuild
        mc = rosebuild.merge_audio(api)
        self.assertIn('roui', mc.PACKS)
        self.assertEqual(mc.PACKS['roui']['kind'], 'ui')
        self.assertEqual(list(mc.PACKS)[-len(api.AUDIO_PACKS):], list(api.AUDIO_ORDER))      # the audio packs stay last

    def test_skill_table_matches_the_world(self):
        path = os.path.join(os.path.dirname(HERE), 'unity', 'Assets', 'GoldSrcVR', 'Editor', 'ro_skills.txt')
        if not os.path.exists(path):
            self.skipTest('no unity tree')
        rows = [l.split() for l in open(path, encoding='utf-8') if l.startswith('skill ')]
        self.assertEqual(sorted((int(r[1]), r[2].lower()) for r in rows), sorted(ui.SKILL_ICONS))


@unittest.skipUnless(os.path.exists(os.path.join(REAL_CLIENT, 'data.grf')), 'owner client not on this machine')
class RealClient(unittest.TestCase):
    def test_every_piece_and_skill_icon(self):
        v = grf.Vfs(REAL_CLIENT)
        warn = []
        pieces = ui.collect(v, warn)
        names = set(p[0] for p in pieces)
        for stem in ui.UI_FILES:
            self.assertIn(stem, names)
        for sid, _a in ui.SKILL_ICONS:
            self.assertIn('sk_%d' % sid, names)
        W, H, rgba, table = ui.build_atlas(pieces, warn=warn)
        self.assertLessEqual(W * H * 4, 4 * 1024 * 1024)
        self.assertFalse([w for w in warn if 'atlas full' in w])


if __name__ == '__main__':
    unittest.main()
