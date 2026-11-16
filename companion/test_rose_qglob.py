"""ROSE w14 GLOBAL QUEST TEXTS: rose_qglob builds a QGLB section (QUST layout, quest + qitem tables of EVERY quest / quest item) with its own TXJA overlay (sub 1)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "rose_conv"))
import rosefmt as F          # noqa: E402
import rose_text as TX       # noqa: E402
import rose_quest as Q       # noqa: E402
import rose_qglob as G       # noqa: E402


class FakeVfs:
    def read(self, path):
        return path


def fake_stb(path):
    if path.endswith('LIST_QUEST.STB'):
        # row 0 empty; quest 1 / 2 / 3 (3 has no string id); cells[1] = time limit, last = string key
        return dict(rows=4, cells=[[], ['', '30', 'q1'], ['', '0', 'q2'], ['', '0', '']])
    if path.endswith('LIST_QUESTITEM.STB'):
        cells = [[]] + [[''] * 9 + [str(100 + n), 'i%d' % n] for n in range(1, 4)]
        return dict(rows=4, cells=cells)
    raise KeyError(path)


def fake_stl(path):
    # languages: ko, en, ja
    if path.endswith('LIST_QUEST_S.STL'):
        keys = {'q1': (0, 0), 'q2': (1, 0)}
        en = [['Quest One', 'Do the first thing', 'Start one', 'End one'], ['Quest Two', 'Do the second thing', 'Start two', 'End two']]
        ja = [['Kuesuto Ichi', 'Ichi desc', 'Ichi start', 'Ichi end'], ['', '', '', '']]
        return 'QEST01', keys, [[], en, ja]
    if path.endswith('LIST_QUESTITEM_S.STL'):
        keys = {'i1': (0, 0), 'i2': (1, 0)}
        return 'ITST01', keys, [[], [['Lucky Charm', ''], ['Old Key', '']], [['Omamori', ''], ['', '']]]
    raise KeyError(path)


class QGlob(unittest.TestCase):
    def setUp(self):
        self.o = (F.stb, F.stl)
        F.stb, F.stl = fake_stb, fake_stl

    def tearDown(self):
        F.stb, F.stl = self.o

    def test_global_section(self):
        TX.reset()
        secs, info, ja = G.build(FakeVfs())
        self.assertEqual([s[0] for s in secs], [b'QGLB'])
        self.assertEqual((info['quests'], info['quest_items']), (2, 2))
        d = Q.dec_qust(secs[0][3])
        self.assertEqual(Q.check(d), [])
        for t in ('trig', 'cond', 'rew', 'npc', 'node', 'qname'):
            self.assertEqual(d['tables'][t], [])
        qs = d['tables']['quest']
        self.assertEqual([q[0] for q in qs], [1, 2])
        self.assertEqual([Q.string_at(d['strings'], qs[0][k]) for k in (1, 2, 3, 4)], ['Quest One', 'Do the first thing', 'Start one', 'End one'])
        self.assertEqual(qs[0][5], 30)
        qi = d['tables']['qitem']
        self.assertEqual([(c, ic, Q.string_at(d['strings'], n)) for c, ic, _z, n in qi], [(13001, 101, 'Lucky Charm'), (13002, 102, 'Old Key')])

    def test_overlay_uses_sub_one(self):
        TX.reset()
        secs, info, ja = G.build(FakeVfs())
        self.assertEqual([s[0] for s in ja], [b'TXJA'])
        keys = list(TX.current().d)
        self.assertTrue(keys and all(k[0] == TX.D_QSTR and k[1] == G.GLOB_SUB for k in keys), keys)
        self.assertEqual(len(keys), 5)          # quest 1: name, description, start, end + item 1; quest 2 / item 2 have no Japanese text

    def test_no_table_no_section(self):
        def boom(p):
            raise KeyError(p)
        F.stb = boom
        TX.reset()
        secs, info, ja = G.build(FakeVfs())
        self.assertEqual((secs, ja), ([], []))


class AvatarBuildErrors(unittest.TestCase):
    """_avatar_bytes: a missing quest table skips QGLB cleanly, a real error fails the avatar build (never cached as good without QGLB)"""

    def setUp(self):
        import rose_conv
        import rose_qglob
        self.rc, self.qg = rose_conv, rose_qglob
        self.saved = (rose_conv._Vfs, rose_conv.RA.build, rose_qglob.build)
        rose_conv._Vfs = lambda install_dir, run: object()
        rose_conv.RA.build = lambda vfs, log=None, with_field=True: ([(b'SKEL', 1, 1, b'abcd')], {})

        class Run:
            def log(self, m):
                pass
        self.run = Run()

    def tearDown(self):
        self.rc._Vfs, self.rc.RA.build, self.qg.build = self.saved

    def test_no_quest_table_is_clean(self):
        self.qg.build = lambda vfs, warn=None: ([], dict(quests=0, quest_items=0, bytes=0), [])
        pack, warns = self.rc._avatar_bytes({}, "x", self.run)
        self.assertTrue(pack.startswith(b'RPG1'))

    def test_real_error_fails_the_build(self):
        def boom(vfs, warn=None):
            raise ValueError('LIST_QUEST parse error')
        self.qg.build = boom
        with self.assertRaises(ValueError):
            self.rc._avatar_bytes({}, "x", self.run)

    def test_qglob_section_is_added(self):
        self.qg.build = lambda vfs, warn=None: ([(b'QGLB', 1, 1, b'zzzz')], dict(quests=1, quest_items=0, bytes=4), [(b'TXJA', 1, 1, b'yyyy')])
        pack, warns = self.rc._avatar_bytes({}, "x", self.run)
        self.assertIn(b'QGLB', pack)
        self.assertIn(b'TXJA', pack)


if __name__ == '__main__':
    unittest.main()
