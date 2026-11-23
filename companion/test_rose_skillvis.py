"""ROSE w25 SKILL VISUALS (companion-owned rose_conv/rose_skillvis.py): the skills' own motion clips (SKMO) and the ammo projectiles (EFCT ammo table), on synthetic
data only (made-up tables, no game files). docs/design/rose_w25_skillvis.md; the runtime twin is checked in tools/usharp_offline/rosew25skillvisrules.cs.

  cd companion && python3 -I test_rose_skillvis.py
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401  (puts the converter folder on sys.path, installs the wrappers)
import rpg1  # noqa: E402
import rose_bont  # noqa: E402
import rose_chars as RC  # noqa: E402
import rose_effects as E  # noqa: E402
import rose_skillvis as V  # noqa: E402


def _stb(rows, width=80):
    n = max(rows) + 1
    cells = [[''] * width for _ in range(n)]
    for r, cs in rows.items():
        for c, v in cs.items():
            cells[r][c] = str(v)
    return dict(rows=n, cells=cells)


def _skill(typ=3, page=1, icon=5, cast=0, cast_speed=0, rep=0, rep_count=0, act=0, weapons=(), level=1, learn=1, job=0, key=None):
    c = {4: page, 5: typ, 51: icon, 52: cast, 53: cast_speed, 54: rep, 55: rep_count, 68: act, 2: level, 3: learn, 35: job}
    for i, w in enumerate(weapons):
        c[30 + i] = w
    if key is not None:
        c[79] = key
    return c


class MotionNeeds(unittest.TestCase):
    def test_rules(self):
        sk = _stb({
            1: _skill(cast=111, act=91, weapons=(231,)),                 # bow skill: the bow's motion type (10) + the no-weapon column
            2: _skill(typ=6, cast=101, act=103),                         # spell without a weapon: every motion type the avatar holds
            3: _skill(typ=1, page=0, level=0, learn=0, act=4),           # basic action (a start skill when named): never cast
            4: _skill(typ=15, act=60),                                   # passive
            5: _skill(cast=311, act=313, weapons=(512,)),                # only a weapon class the avatar cannot hold
            6: _skill(cast=20, rep=21, rep_count=2, act=22),             # repeats its casting-repeat motion twice: baked looping
            7: _skill(cast=20, rep=23, rep_count=0, act=22),             # repeat motion never shown (count 0)
            8: _skill(typ=16, act=55),                                   # emote
        })
        n = V.motion_needs(sk, item_ids=set())
        self.assertEqual(n[(111, False)], {0, 10})
        self.assertEqual(n[(91, False)], {0, 10})
        self.assertEqual(n[(101, False)], set(RC.WEAPON_MOTION_TYPES))
        self.assertEqual(n[(21, True)], set(RC.WEAPON_MOTION_TYPES))
        for m in ((4, False), (60, False), (311, False), (313, False), (23, True), (55, False)):
            self.assertNotIn(m, n)
        self.assertIn((20, False), n)
        self.assertIn((22, False), n)

    def test_item_skills_and_names(self):
        sk = _stb({1: _skill(typ=10, page='', icon=0, act=51), 2: _skill(cast=111, act=91, key='K2'), 3: _skill(cast=112, act=92, key='K3')})
        self.assertNotIn((51, False), V.motion_needs(sk, item_ids={1}))          # magic item skill: no clip (keeps the old cast)
        n = V.motion_needs(sk, set(), names={'K2': 'Double Attack', 'K3': ''})
        self.assertIn((91, False), n)
        self.assertNotIn((92, False), n)                                  # a row without a name is in no skill window
        self.assertEqual(V.motion_needs(None), {})


class _FakeCB:
    """the CharBuilder parts add_skill_clips uses: avatar skeletons, _motion_path (find_first rules), clip cache by (path, skel, loop)"""
    def __init__(self, files):
        self.files = files                    # (motion, wt, gender) -> path
        self.avatar = dict(genders=[dict(skeleton=0), dict(skeleton=1)])
        self.clips = {}

    def _motion_path(self, tm, fm, m, wt, g):
        p = self.files.get((m, wt, g)) or self.files.get((m, wt, 0))
        if p is None and wt != 0:
            p = self.files.get((m, 0, g)) or self.files.get((m, 0, 0))
        return p

    def clip(self, path, skel, loop):
        k = (RC._norm(path), skel, bool(loop))
        if k not in self.clips:
            self.clips[k] = len(self.clips)
        return self.clips[k]


class Clips(unittest.TestCase):
    def test_rows_dedup_and_fallback(self):
        cb = _FakeCB({(91, 0, 0): 'M/A.ZMO', (91, 10, 0): 'M/BOW.ZMO', (91, 1, 0): 'm/a.zmo', (91, 0, 1): 'M/A_F.ZMO'})
        rows = V.add_skill_clips(cb, (0, 1), None, None, {(91, False): {0, 1, 10}})
        keyed = {(m, g, w, lp): c for m, g, w, c, lp in rows}
        self.assertIn((91, 0, 0, False), keyed)
        self.assertIn((91, 0, 10, False), keyed)
        self.assertNotIn((91, 0, 1, False), keyed)                      # the sword column names the no-weapon file: no row, the runtime falls back
        self.assertIn((91, 1, 0, False), keyed)
        self.assertIn((91, 1, 10, False), keyed)                         # female bow: the male bow file (FILE_MOTION gender fallback), her own clip (other skeleton)
        self.assertNotEqual(keyed[(91, 0, 0, False)], keyed[(91, 1, 0, False)])
        self.assertEqual(V.skmo_clip(rows, 91, False, 0, 1), keyed[(91, 0, 0, False)])
        self.assertEqual(V.skmo_clip(rows, 91, False, 0, 10), keyed[(91, 0, 10, False)])
        self.assertEqual(V.skmo_clip(rows, 92, False, 0, 0), -1)
        self.assertEqual(V.add_skill_clips(type('N', (), {'avatar': None})(), (0, 1), None, None, {(91, False): {0}}), [])

    def test_codec(self):
        rows = [(91, 0, 10, 7, False), (20, 0, 0, 9, True), (20, 0, 0, 8, False), (91, 1, 0, 6, False)]
        d = V.enc_skmo(rows)
        self.assertEqual(struct.unpack_from('<4I', d, 0), (4, 8, 16, 0))
        back = V.dec_skmo(d, clip_count=10)
        self.assertEqual(back, sorted(rows, key=lambda r: V.key(r[0], r[4], r[1], r[2])))
        self.assertEqual(back[0], (20, 0, 0, 8, False))                  # once before loop for one motion
        with self.assertRaises(ValueError):
            V.dec_skmo(d, clip_count=9)
        with self.assertRaises(ValueError):
            V.enc_skmo([(91, 0, 0, 1, False), (91, 0, 0, 2, False)])
        bad = bytearray(d)
        bad[16:24], bad[24:32] = d[24:32], d[16:24]
        with self.assertRaises(ValueError):
            V.dec_skmo(bytes(bad))
        self.assertEqual(V.key(91, False, 1, 10), (91 << 17) | (1 << 8) | 10)


def _bont_pack(clipmap, nclips):
    joints = [dict(parent=-1, flags=0, name="root", lpos=(0.0, 1.0, 0.0), lrot=(0.0, 0.0, 0.0, 1.0), gpos=(0.0, 1.0, 0.0), grot=(0.0, 0.0, 0.0, 1.0)),
              dict(parent=0, flags=0, name="arm", lpos=(0.5, 0.0, 0.0), lrot=(0.0, 0.0, 0.0, 1.0), gpos=(0.5, 1.0, 0.0), grot=(0.0, 0.0, 0.0, 1.0))]
    skel = rpg1.enc_skel([dict(name="s", nreal=2, flags=1, joints=joints)])
    clips = [dict(name="c%d" % c, skeleton=0, frames=2, loop=False, src_fps=30, events=[],
                  tracks=[dict(joint=0, pos=[(0.0, 1.0, 0.0), (0.0, 1.25, 0.0)], rot=None, scale=None)]) for c in range(nclips)]
    canm = rpg1.enc_canm(clips, 30)
    avat = rpg1.enc_avat([], [], list(clipmap))
    return skel, canm, avat


class Bones(unittest.TestCase):
    def test_skill_only_and_lazy(self):
        skel, canm, avat = _bont_pack([(0, 0, 0, 0), (0, 0, 10, 1), (0, 8, 0, 2)], 6)
        rows = [(91, 0, 0, 2, False), (91, 0, 10, 3, False), (101, 0, 0, 4, False), (101, 1, 0, 5, False)]
        self.assertEqual(V.skill_only_clips(rows, avat, None, 6), [3, 4, 5])          # clip 2 is also the AVAT attack row
        self.assertEqual(V.mark_skill_lazy([False, True, False, False, False, False], rows, avat, None), [False, True, False, True, True, True])

    def test_bont_policy(self):
        """small pack: the skill clips are pre-baked (flag 1); above SKILL_BONT_MAX they are left out (flag 0) while the weapon sets stay baked"""
        skel, canm, avat = _bont_pack([(0, 0, 0, 0), (0, 0, 10, 1)], 4)
        skmo = V.enc_skmo([(91, 0, 0, 2, False), (91, 0, 10, 3, False)])
        secs = [(b"SKEL", 1, 1, skel), (b"CANM", 1, 4, canm), (b"AVAT", 1, 0, avat), (b"SKMO", 1, 2, skmo)]
        bt = rose_bont.from_sections(secs)
        self.assertEqual(bt[0], b"BONT")
        n = 4
        fo = rose_bont.HEADER.unpack_from(bt[3], 0)[9]
        self.assertEqual(list(bt[3][fo:fo + n]), [1, 1, 1, 1])
        keep = V.SKILL_BONT_MAX
        try:
            V.SKILL_BONT_MAX = 64
            bt2 = rose_bont.from_sections(secs)
        finally:
            V.SKILL_BONT_MAX = keep
        self.assertEqual(list(bt2[3][fo:fo + n]), [1, 1, 0, 0])          # weapon set clip 1 baked (lazy_too=False only for the skill clips), the skill clips 2, 3 not
        self.assertLess(len(bt2[3]), len(bt[3]))
        self.assertEqual(len(bt[3]), V.bont_bytes_estimate(skel, canm, [True] * 4))
        self.assertEqual(len(bt2[3]), V.bont_bytes_estimate(skel, canm, [True, True, False, False]))
        self.assertEqual(rose_bont.from_sections(secs[:3])[3][fo:fo + n], bytes([1, 1, 1, 1]))          # no SKMO: the vendored behaviour
        self.assertIsNone(V._state['skmo'])
        self.assertFalse(V._state['skip'])


class _FakeB:
    def __init__(self):
        self.added = []

    def add_file(self, fid, path):
        self.added.append(fid)
        return fid != 99                     # file 99 has no drawable part


class Ammo(unittest.TestCase):
    def test_rows_and_section(self):
        le = _stb({5: {9: 21, 11: 33, 15: 3000}, 6: {11: 99, 15: 2500}, 7: {9: 22}}, 18)
        nat = _stb({301: {4: 431, 17: 5}, 302: {4: 431}, 311: {4: 432, 17: 6}, 321: {4: 433, 17: 7}}, 30)
        fe = {21: 'a.eft', 22: 'b.eft', 33: 'c.eft', 99: 'x.eft'}
        b = _FakeB()
        rows = V.ammo_rows(b, fe, le, nat)
        self.assertEqual(rows, [dict(id=301, hit=21, bullet=33, speed=3000), dict(id=321, hit=22, bullet=0, speed=0)])          # 311: its bullet has nothing to draw
        data = bytes(E.HDR.size + 5)
        out = V.append_ammo(data, rows)
        self.assertEqual(len(out) % 4, 0)
        self.assertEqual(V.dec_ammo(out), [(301, 21, 33, 3000), (321, 22, 0, 0)])
        self.assertEqual(out[:E.HDR.size][:4 * 43], data[:4 * 43])
        self.assertEqual(V.append_ammo(data, []), data)
        self.assertEqual(V.dec_ammo(data), [])
        with self.assertRaises(ValueError):
            V.append_ammo(data, rows + rows)
        bad = bytearray(out)
        struct.pack_into('<I', bad, 4 * V.HDR_AMMO_OFF, len(out))
        with self.assertRaises(ValueError):
            V.dec_ammo(bytes(bad))


class Installed(unittest.TestCase):
    def test_hooks(self):
        self.assertTrue(V._orig)
        self.assertIs(rose_bont.from_sections, V._patched_from_sections)
        self.assertIs(rose_bont.mark_lazy, V._patched_mark_lazy)
        self.assertIsNot(RC.CharBuilder.add_avatar, V._orig['add_avatar'])
        self.assertIsNot(E.enc_efct, V._orig['enc_efct'])
        V.install()                          # idempotent
        self.assertIs(rose_bont.from_sections, V._patched_from_sections)
        self.assertEqual(rose_conv.WRAPPER_REV, 8)
        self.assertEqual(rose_conv.PACK_REV["avatar"], 5)          # the ammo table rides on WRAPPER_REV 8 (VERSION of every pack changes)
        self.assertIn(b"SKMO", [V.TAG_SKMO])


if __name__ == "__main__":
    unittest.main()
