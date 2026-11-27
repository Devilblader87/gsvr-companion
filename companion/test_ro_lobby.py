"""w43 lobby: the login / character select / make-character pieces in the 'roui' atlas (ro_conv.ui LOGIN_FILES, UI_REV 3), SYNTHETIC fixtures (no game data).

  cd companion && python3 -I test_ro_lobby.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ro_conv import grf, img, ui  # noqa: E402

REAL_CLIENT = '/path/to/gsvr-testdata Online'
LOGIN_DIR = 'data\\texture\\유저인터페이스\\login_interface\\'


def solid(w, h, rgba):
    return bytes(rgba) * (w * h)


class FakeVfs:
    def __init__(self, files):
        self.files = files

    def exists(self, p):
        return p in self.files

    def read(self, p):
        if p not in self.files:
            raise KeyError(p)
        return self.files[p]


class Login(unittest.TestCase):
    def test_names_and_contract(self):
        self.assertGreaterEqual(ui.UI_REV, 3)                                    # the pack rebuilds once with the login pieces
        self.assertEqual(ui.login_name('win_select'), 'lg_win_select')
        self.assertEqual(len(set(ui.LOGIN_FILES)), len(ui.LOGIN_FILES))
        for st in ('str', 'agi', 'vit', 'int', 'dex', 'luk'):                    # the stat hexagon: every corner arrow, off + on
            self.assertIn('arw-%s0' % st, ui.LOGIN_FILES)
            self.assertIn('arw-%s1' % st, ui.LOGIN_FILES)
        for need in ('win_select', 'win_make', 'name-edit', 'btn_ok', 'btn_cancel'):
            self.assertIn(need, ui.LOGIN_FILES)
        self.assertTrue(ui.PATH_LOGIN.endswith('login_interface/%s.bmp'))
        # login pieces never collide with the world's other atlas names
        for stem in ui.LOGIN_FILES:
            self.assertTrue(ui.login_name(stem).startswith('lg_'))
            self.assertNotIn(ui.login_name(stem), ui.UI_FILES)

    def test_packed_into_the_atlas(self):
        files = {}
        for stem in ui.UI_FILES[:3]:
            files[ui.PATH_UI.replace('/', '\\') % stem if False else 'data\\texture\\유저인터페이스\\basic_interface\\' + stem + '.bmp'] = img.write_bmp24(8, 8, solid(8, 8, (10, 20, 30, 255)))
        for stem in ('win_select', 'arw-str0', 'arw-str1', 'btn_ok'):
            files[LOGIN_DIR + stem + '.bmp'] = img.write_bmp24(16, 12, solid(16, 12, (200, 100, 50, 255)))
        warn = []
        pieces = ui.collect_login(FakeVfsNorm(files), warn)
        names = {p[0]: p for p in pieces}
        for stem in ('win_select', 'arw-str0', 'arw-str1', 'btn_ok'):
            self.assertIn('lg_' + stem, names)
            self.assertEqual((names['lg_' + stem][3], names['lg_' + stem][4]), (16, 12))
            self.assertEqual(names['lg_' + stem][1], ui.KIND_UI)
        self.assertNotIn('lg_win_make', names)                                    # a missing piece only drops that sprite
        self.assertTrue([w for w in warn if 'login / character select pieces' in w])
        W, H, rgba, table = ui.build_atlas(pieces, warn=warn)
        sec = ui.enc_roui(W, H, rgba, table)
        self.assertEqual(ui.TAG_ROLG, b'ROLG')
        W2, H2, tab, _px = ui.dec_roui(sec)
        self.assertIn('lg_win_select', tab)
        self.assertEqual(tab['lg_win_select'][4:], (16, 12))


class FakeVfsNorm(FakeVfs):
    """paths normalised like grf.Vfs (lower, backslashes)"""

    def __init__(self, files):
        super().__init__({grf.norm(k): v for k, v in files.items()})

    def exists(self, p):
        return grf.norm(p) in self.files

    def read(self, p):
        k = grf.norm(p)
        if k not in self.files:
            raise KeyError(p)
        return self.files[k]


@unittest.skipUnless(os.path.exists(os.path.join(REAL_CLIENT, 'data.grf')), 'owner client not on this machine')
class RealClient(unittest.TestCase):
    def test_login_pieces_exist(self):
        v = grf.Vfs(REAL_CLIENT)
        warn = []
        lg = ui.collect_login(v, warn)
        names = set(p[0] for p in lg)
        for stem in ('win_select', 'win_make', 'name-edit', 'arw-str0', 'arw-luk1'):
            self.assertIn('lg_' + stem, names)
        W, H, rgba, table = ui.build_atlas(lg, warn=warn)
        self.assertLessEqual(W, 1024)
        self.assertFalse([w for w in warn if 'atlas full' in w])
        secs = ui.ui_sections(v, [])
        self.assertEqual([s[0] for s in secs], [ui.TAG_ROUI, ui.TAG_ROLG])


if __name__ == '__main__':
    unittest.main()
