"""ROSE w27 CART ANIMATION (companion-owned rose_conv/rose_cartvis.py): extended driver + vehicle skeletons, merged clips (driver root under the seat dummy), SKMO vehicle rows,
the ACRS rename, on synthetic data only (made-up skeletons / tables, no game files). docs/design/rose_carts.md section 7; the runtime twin is tools/usharp_offline/rosew27cartrules.cs.

  cd companion && python3 -I test_rose_cartvis.py
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401  (puts the converter folder on sys.path, installs the wrappers)
import rosechar as C  # noqa: E402
import rose_chars as RC  # noqa: E402
import rose_carts as CARTS  # noqa: E402
import rose_avatar_pack as RA  # noqa: E402
import rose_skillvis as SV  # noqa: E402
import rose_bont  # noqa: E402
import rpg1 as P  # noqa: E402
import rose_cartvis as V  # noqa: E402


def _stb(rows, width=70):
    n = max(rows) + 1
    cells = [[''] * width for _ in range(n)]
    for r, cs in rows.items():
        for c, v in cs.items():
            cells[r][c] = str(v)
    return dict(rows=n, cells=cells)


def _skel(spec, nreal, name):
    """spec = [(parent, lpos)] -> skeleton dict like build_skeleton (roots carry the faces-+Z turn)"""
    joints = []
    for i, (par, lp) in enumerate(spec):
        rot = C.FLIP_Q if par < 0 else C.IDENT_Q
        pos = C.qrot(C.FLIP_Q, lp) if par < 0 else lp
        joints.append(dict(parent=par, flags=0 if i < nreal else C.JF_DUMMY, name='j%d' % i, lpos=pos, lrot=rot))
    C._globals(joints)
    return dict(name=name, nreal=nreal, joints=joints, flags=P.SKF_FACES_PLUS_Z)


AV = _skel([(-1, (0.0, 0.0, 1.0)), (0, (0.0, 0.0, 0.5)), (0, (0.2, 0.0, 0.0))], 2, 'AVATAR')          # root (pelvis 1 m up in file z), spine, one dummy
VEH = _skel([(-1, (0.0, 0.0, 0.3)), (0, (0.5, 0.0, 0.0)), (0, (0.0, 0.4, 0.6))], 2, 'CART')            # root, wheel, dummy 0 = the seat


def _clip(skel, frames, tracks, loop=True, events=()):
    return dict(name='c', frames=frames, fps=30, src_fps=30, loop=loop, events=list(events), dropped=0,
                tracks=[dict(joint=j, pos=p, rot=r, scale=None) for j, p, r in tracks])


def _close(a, b, eps=1e-5):
    return all(abs(x - y) <= eps for x, y in zip(a, b))


class Skeleton(unittest.TestCase):
    def test_ext_layout(self):
        e = V.ext_skeleton(AV, VEH, 'AV+CART')
        self.assertEqual(len(e['joints']), 6)
        self.assertEqual(e['base'], 3)
        self.assertEqual(e['nreal'], AV['nreal'])                       # avatar dummies keep their index (effects, weapon attach)
        self.assertEqual([j['parent'] for j in e['joints']], [-1, 0, 0, -1, 3, 3])
        for i in range(3):                                              # avatar joints unchanged: every avatar mesh skins as before
            self.assertTrue(_close(e['joints'][i]['gpos'], AV['joints'][i]['gpos']))
        for i in range(3):                                              # vehicle joints: same globals as on their own skeleton
            self.assertTrue(_close(e['joints'][3 + i]['gpos'], VEH['joints'][i]['gpos']))
        for k, j in enumerate(e['joints']):
            self.assertTrue(j['parent'] < k)                            # parents first (GsvrRpg1Chars.ValidateSkeletons)
        self.assertEqual(len(P.dec_skel(P.enc_skel([e]))[0]['joints']), 6)

    def test_seat_root(self):
        # dummy = FLIP (a flipped vehicle root with the seat at the origin): the driver's root stays exactly what it was
        root = ((0.1, 0.9, 0.2), C.qnorm(C.qmul(C.FLIP_Q, (0.0, 0.0, 0.3826834, 0.9238795))))
        p, q = V.seat_root(((0.0, 0.0, 0.0), C.FLIP_Q), root)
        self.assertTrue(_close(p, root[0]) and _close(q, root[1]))
        # a seat 0.6 m up and 0.5 m behind: the root moves with it
        p, q = V.seat_root(((0.0, 0.6, -0.5), C.FLIP_Q), ((0.0, 0.0, 0.0), C.FLIP_Q))
        self.assertTrue(_close(p, (0.0, 0.6, -0.5)) and _close(q, C.FLIP_Q))


class Clip(unittest.TestCase):
    def test_merged(self):
        e = V.ext_skeleton(AV, VEH, 'AV+CART')
        bob = [C.qrot(C.FLIP_Q, (0.0, 0.0, 0.3 + 0.1 * k)) for k in range(4)]          # the vehicle root rises 10 cm per frame (file z = up)
        spin = [C.qnorm((math.sin(0.2 * k), 0.0, 0.0, math.cos(0.2 * k))) for k in range(4)]
        vclip = _clip(VEH, 4, [(0, bob, None), (1, None, spin)], events=[(2, 33)])
        dclip = _clip(AV, 2, [(1, None, [C.IDENT_Q, (0.0, 0.0, 0.7071068, 0.7071068)])])
        m = V.merged_clip(AV, VEH, e, 7, vclip, dclip, 'x')
        self.assertEqual((m['frames'], m['skeleton'], m['loop'], m['events']), (4, 7, True, [(2, 33)]))
        byj = {t['joint']: t for t in m['tracks']}
        self.assertEqual(sorted(byj), [0, 1, 3, 4])                     # avatar root + the driver's track, then the vehicle's shifted by 3
        self.assertIs(byj[4]['pos'], None)
        self.assertEqual(byj[1]['rot'][3], (0.0, 0.0, 0.7071068, 0.7071068))          # the driver clip loops (frame 3 = its frame 1)
        for k in range(4):
            g = C.global_pose(VEH, C.clip_local_pose(VEH, vclip, k))
            want = V.seat_root(g[VEH['nreal']], (AV['joints'][0]['lpos'], AV['joints'][0]['lrot']))
            self.assertTrue(_close(byj[0]['pos'][k], want[0], 1e-4), (k, byj[0]['pos'][k], want[0]))
        eg = C.global_pose(e, C.clip_local_pose(e, m, 3))
        vg = C.global_pose(VEH, C.clip_local_pose(VEH, vclip, 3))
        self.assertTrue(_close(eg[3][0], vg[0][0], 1e-4) and _close(eg[5][0], vg[2][0], 1e-4))          # the vehicle part of the merged pose = the vehicle clip
        enc = P.enc_canm([m], 30)
        self.assertEqual(len(P.dec_canm(enc)[0]), 1)

    def test_loop_seam(self):
        """review: a looping row of n frames whose driver clip has dn frames (n no multiple of dn) wraps without a driver pose jump"""
        e = V.ext_skeleton(AV, VEH, 'AV+CART')
        dn = 7
        ang = [C.qnorm((0.0, 0.0, math.sin(math.pi * k / dn), math.cos(math.pi * k / dn))) for k in range(dn)]          # one full turn per driver loop
        dclip = _clip(AV, dn, [(1, None, ang)])
        for n, loop in ((17, True), (11, True), (17, False)):
            vclip = _clip(VEH, n, [(1, None, [C.IDENT_Q] * n)], loop=loop)
            rot = {t['joint']: t for t in V.merged_clip(AV, VEH, e, 7, vclip, dclip, 'x')['tracks']}[1]['rot']
            step = lambda a, b: 1.0 - abs(sum(x * y for x, y in zip(a, b)))
            worst = max(step(rot[k], rot[k + 1]) for k in range(n - 1))
            if loop:
                self.assertLessEqual(step(rot[n - 1], rot[0]), worst + 1e-9, (n, rot[n - 1], rot[0]))          # the wrap is no bigger than a normal step
            else:
                self.assertEqual(rot[:dn], [tuple(q) for q in ang])          # play once: the driver at its own rate


class Budget(unittest.TestCase):
    def test_skill_decision_without_the_cart(self):
        """review: the cart clips never flip the skill clips' pre-bake decision; they are pre-baked only while the pack with them fits, else only they stay lazy"""
        import rose_bont
        joints = [dict(parent=-1, flags=0, name="root", lpos=(0.0, 1.0, 0.0), lrot=(0.0, 0.0, 0.0, 1.0), gpos=(0.0, 1.0, 0.0), grot=(0.0, 0.0, 0.0, 1.0)),
                  dict(parent=0, flags=0, name="arm", lpos=(0.5, 0.0, 0.0), lrot=(0.0, 0.0, 0.0, 1.0), gpos=(0.5, 1.0, 0.0), grot=(0.0, 0.0, 0.0, 1.0))]
        skel = P.enc_skel([dict(name="s", nreal=2, flags=1, joints=joints), dict(name="s+v", nreal=2, flags=1, joints=joints + [dict(joints[1], parent=1, name="seat")])])
        def clip(c, sk, fr):
            return dict(name="c%d" % c, skeleton=sk, frames=fr, loop=False, src_fps=30, events=[],
                        tracks=[dict(joint=0, pos=[(0.0, 1.0 + 0.01 * k, 0.0) for k in range(fr)], rot=None, scale=None)])
        canm = P.enc_canm([clip(0, 0, 2), clip(1, 0, 2), clip(2, 0, 2), clip(3, 0, 2), clip(4, 1, 200), clip(5, 1, 200)], 30)
        avat = P.enc_avat([], [], [(0, 0, 0, 0), (0, 0, 10, 1)])
        skmo = SV.enc_skmo([(91, 0, 0, 2, False), (91, 0, 10, 3, False), (281, 0, 0, 4, True), (281, 1, 0, 5, True)])
        secs = [(b"SKEL", 1, 2, skel), (b"CANM", 1, 6, canm), (b"AVAT", 1, 0, avat), (b"SKMO", 1, 4, skmo)]
        rows = SV.dec_skmo(skmo)
        self.assertEqual(V.cart_clip_ids(rows, {281}, 6), [4, 5])
        n = 6
        rest = 32 + sum((len(d) + 3) & ~3 for _t, _v, _c, d in secs) + 24 * (len(secs) + 1)
        full = rest + SV.bont_bytes_estimate(skel, canm, [True] * n)
        cost = V.cart_cost(skel, canm, skmo, [4, 5])
        self.assertGreater(cost, SV.bont_bytes_estimate(skel, canm, [True] * n) - SV.bont_bytes_estimate(skel, canm, [True] * 4 + [False] * 2))
        skel0 = P.enc_skel([dict(name="s", nreal=2, flags=1, joints=joints)])          # the same pack before this lane: no cart clips, no extended skeleton, no cart SKMO rows
        canm0 = P.enc_canm([clip(0, 0, 2), clip(1, 0, 2), clip(2, 0, 2), clip(3, 0, 2)], 30)
        skmo0 = SV.enc_skmo([(91, 0, 0, 2, False), (91, 0, 10, 3, False)])
        secs0 = [(b"SKEL", 1, 1, skel0), (b"CANM", 1, 4, canm0), (b"AVAT", 1, 0, avat), (b"SKMO", 1, 2, skmo0)]
        full0 = 32 + sum((len(d) + 3) & ~3 for _t, _v, _c, d in secs0) + 24 * (len(secs0) + 1) + SV.bont_bytes_estimate(skel0, canm0, [True] * 4)
        self.assertLess(abs(full - cost - full0), 64, (full, cost, full0))          # the decision sees the old pack size (within padding / name rounding)
        keep, km = SV.SKILL_BONT_MAX, V._bstate['motions']
        def flags(cap):
            SV.SKILL_BONT_MAX = cap
            bt = rose_bont.from_sections(secs)
            fo = rose_bont.HEADER.unpack_from(bt[3], 0)[9]
            return list(bt[3][fo:fo + n])
        try:
            V._bstate['motions'] = {281}
            self.assertEqual(flags(full + 16), [1] * 6)                                  # everything fits: all pre-baked (as before)
            self.assertEqual(flags(full - cost + 16), [1, 1, 1, 1, 0, 0])                  # fits only without the cart: skill clips stay pre-baked, only the cart clips lazy
            self.assertEqual(flags(full - cost - 64), [1, 1, 0, 0, 0, 0])                  # too big even without the cart: skill (and cart) clips lazy, as before
            V._bstate['motions'] = set()
            self.assertEqual(flags(full - cost + 16), [1, 1, 0, 0, 0, 0])                  # without the lane's state: rose_skillvis alone (counts the cart clips as skill clips)
        finally:
            SV.SKILL_BONT_MAX, V._bstate['motions'] = keep, km
        self.assertIsNone(V._bstate['only'])


class Tables(unittest.TestCase):
    def test_bases_and_arms(self):
        pat = _stb({1: {4: 511, 40: 281, 41: 261, 69: 'K1'}, 2: {4: 511, 40: 281, 41: 261, 69: 'K2'}, 3: {4: 512, 40: 311, 41: 261, 69: 'K3'},
                    4: {4: 552, 40: 3, 69: 'K4'}, 5: {4: 551, 69: 'K5'}, 6: {4: 511, 40: 290, 41: 261, 69: 'K6'}, 7: {4: 512, 40: 311, 41: 261}})          # last cell = the string id
        b, warn = V.vehicle_bases(pat)
        self.assertEqual(b, {False: (281, 261), True: (311, 261)})
        self.assertEqual(len(warn), 1)                                  # body row 6 disagrees; row 7 has no name (not an item)
        self.assertEqual(V.arms_types(pat), {False: [0], True: [0, 3]})
        self.assertEqual(V.vehicle_bases(None), ({}, []))

    def test_item_motion_is_the_base(self):
        """review: the runtime reads the body's base motion from the ITEM record (IR_MOTION, rose_items type 14) - it must be the cell vehicle_bases keys the clips by, and the arms' type too"""
        import rose_items as RI
        pat = _stb({1: {4: 511, 40: 281, 41: 261, 69: 'K1'}, 2: {4: 552, 40: 3, 69: 'K2'}})
        self.assertEqual(RI.item_record(14001, pat['cells'][1], 'x')['motion'], V.vehicle_bases(pat)[0][False][0])
        self.assertIn(RI.item_record(14002, pat['cells'][2], 'y')['motion'], V.arms_types(pat)[True])
        self.assertEqual(V.CART_ACTIONS, (0, 1, 2))                     # the client maps a cart's attack 2 / 3 to attack 1: they are not baked
        self.assertFalse(V.GEAR_ALL_ARMS)                               # the measured budget (1.1 MB CANM + 5.7 MB BONT per pack) keeps the gear to arms type 0

    def test_skmo_rows_are_skill_only(self):
        rows = [(281, 0, 0, 4, True), (282, 0, 0, 5, True), (283, 0, 0, 6, False), (91, 0, 0, 7, False)]
        d = SV.enc_skmo(rows)
        self.assertEqual(sorted(SV.dec_skmo(d, 8)), sorted(rows))
        self.assertEqual(SV.skmo_clip(rows, 283, False, 0, 3), 6)       # an arms type without a row plays type 0
        self.assertEqual(SV.skill_only_clips(rows, None, None, 8), [4, 5, 6, 7])          # lazy unless BONT pre-bakes them (the SKILL_BONT_MAX budget)

    def test_rename(self):
        secs = [(b'SKEL', 1, 1, b'a'), (b'ACAR', 1, 2, b'b'), (b'OTEX', 1, 0, b'')]
        self.assertEqual([t for t, _v, _c, _d in V.rename_cart_section(secs)], [b'SKEL', b'ACRS', b'OTEX'])

    def test_hooks(self):
        self.assertIs(CARTS.add_cart_items, V.skinned_cart_items)
        self.assertTrue(RA.build.__name__ == 'build' and RA.build is not V._orig['avatar_build'])
        self.assertIsNot(RC.CharBuilder.add_avatar, V._orig['add_avatar'])
        V.install()                                                     # idempotent
        self.assertIs(CARTS.add_cart_items, V.skinned_cart_items)
        self.assertEqual(rose_conv.WRAPPER_REV, 9)

    def test_add_vehicle_clips_without_tables(self):
        class _CB:
            avatar = None
            warn = __import__('collections').Counter()
        self.assertEqual(V.add_vehicle_clips(_CB(), (0, 1), None, None, None), [])


class SkeletonBudget(unittest.TestCase):
    """w27 zonefix (round r127): Zant had 59 npc skeletons + 2 avatar + 4 extended = 65 > GsvrRpg1Chars.MaxSkeletons, the runtime refused its characters ('bad section structure
    (SKEL)') and the travel 21 -> 1 went back to 21. Every pack must keep SKEL <= the runtime limit."""

    def test_runtime_twin(self):
        import re
        cs = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'unity', 'Assets', 'GoldSrcVR', 'Scripts', 'GsvrRpg1Chars.cs')
        with open(cs, encoding='utf-8') as fh:
            m = re.search(r'MaxSkeletons\s*=\s*(\d+)', fh.read())
        self.assertIsNotNone(m)
        self.assertEqual(V.RUNTIME_MAX_SKELETONS, int(m.group(1)))

    def test_reserve_adds_up(self):
        import rose_skills as SK
        self.assertLessEqual(V.summon_cap(SK.MAX_SUMMON_SKELETONS) + V.AVATAR_SKELETONS + V.EXT_SKELETONS, V.RUNTIME_MAX_SKELETONS)
        self.assertIsNot(SK.pick_summon_actors, V._orig['pick_summon_actors'])          # zone_summon_ids calls the capped picker

    def test_summon_picker_reserves_the_extended_skeletons(self):
        import rose_skills as SK
        base = list(range(1, 41))                       # 40 zone npcs, one skeleton each
        summons = list(range(1000, 1100))               # 100 summons, each with its own skeleton (Zant-like)
        skel_of = lambda n: n                           # noqa: E731
        add, drop = SK.pick_summon_actors(base, summons, skel_of)
        self.assertEqual(len(base) + len(add), V.RUNTIME_MAX_SKELETONS - V.AVATAR_SKELETONS - V.EXT_SKELETONS)
        self.assertEqual(len(add) + len(drop), len(summons))
        self.assertEqual(len(V._orig['pick_summon_actors'](base, summons, skel_of)[0]), 20)          # the vendored cap alone (60) left no room: 60 + 2 + 4 = 66

    def _cb(self, n):
        class _CB:
            pass
        cb = _CB()
        cb.skels = [_skel([(-1, (0.0, 0.0, 0.0))], 1, 's%d' % i) for i in range(n)]
        cb.skel_idx = {}
        return cb

    def test_ext_fit_and_guard(self):
        cb = self._cb(61)
        self.assertEqual(V.ext_needed(cb, [59, 60], (False, True)), 4)
        self.assertFalse(V.ext_fit(cb, [59, 60], (False, True)))          # 61 + 4 = 65: the Zant pack of r127
        self.assertTrue(V.ext_fit(self._cb(60), [58, 59], (False, True)))
        cb = self._cb(64)
        with self.assertRaises(ValueError):
            V.ext_index(cb, 0, False, cb.skels[1])
        self.assertEqual(len(cb.skels), 64)

    def test_add_vehicle_clips_over_budget_adds_nothing(self):
        cb = self._cb(61)
        cb.avatar = dict(genders=[dict(skeleton=59), dict(skeleton=60)])
        cb.warn = __import__('collections').Counter()
        cb.vfs = None
        orig = V._veh_skeleton
        V._veh_skeleton = lambda vfs, gear: _skel([(-1, (0.0, 0.0, 0.0)), (0, (0.0, 1.0, 0.0))], 1, 'veh%d' % gear)
        try:
            pat = _stb({1: {4: 511, 40: 100, 41: 200, 69: 'cart'}, 2: {4: 512, 40: 300, 41: 400, 69: 'gear'}})
            self.assertEqual(V.add_vehicle_clips(cb, (0, 1), None, None, pat), [])
        finally:
            V._veh_skeleton = orig
        self.assertEqual(len(cb.skels), 61)
        self.assertTrue(any('skeleton budget' in k for k in cb.warn))


if __name__ == '__main__':
    unittest.main()
