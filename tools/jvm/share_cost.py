#!/usr/bin/env python3
"""BUGS PB-SHARE: the Udon cost of Wintermaul's spawn phase (game seconds 61..120, no inputs, one human) with and without route
sharing, from the Python engine's counters and the hand-counted Udon operation costs of BUGS P-AST (an A* expansion about 325
operations, a line-of-sight cell about 14, a share scan about 6 per route segment / record visited).

    pypy3 tools/jvm/share_cost.py [R ...]        R = SHARE_R in cells (0 = sharing off); default 0 and the SHARE_R of pathing.py
    pypy3 tools/jvm/share_cost.py --soak [R ...]   the 8-human soak game (autoplay 'soak', 600 s, as check.sh replays it) instead of the
                                                   one-human spawn window: expansions per window, shares, shares across grid versions
Prints per R: searches / expansions / LOS cells / shares in 61..120 s and in the burst 82..91 s, the modelled Udon seconds
(0.41 us per operation: 150.6k expansions = ~20 s of the 39.8 s window, BUGS P-SPAWN) and the engine-work window sum.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from jvm import run as R          # noqa: E402
from jvm import pathing as P      # noqa: E402

DEFAULT_MIN = P.SHARE_MIN_EXP
OP_EXP, OP_LOS, OP_SCAN, US_OP = 325, 14, 6, 0.41e-6
OTHER_S = 39.8 - 19.9 - 9.2       # BUGS P-SPAWN: window 39.8 s, A* ~50 %, smoothing LOS ~23 %: the rest does not change


def measure(radius, seconds=121, min_exp=None):
    P.SHARE_R = max(radius, 0)
    P.SHARE_MIN_EXP = (min_exp if min_exp is not None else DEFAULT_MIN) if radius > 0 else 1 << 60
    cnt = {'search': 0, 'exp': 0, 'los': 0, 'scan': 0}
    o_astar, o_los, o_find = P.Grid.astar, P.Grid.los, P.Grid.share_find

    def astar(self, *a, **k):
        r = o_astar(self, *a, **k)
        cnt['search'] += 1
        cnt['exp'] += self.last_exp
        return r

    def los(self, *a):
        w = self.wk
        r = o_los(self, *a)
        cnt['los'] += self.wk - w
        return r

    def find(self, *a):
        w = self.wk
        r = o_find(self, *a)
        cnt['scan'] += self.wk - w
        return r
    P.Grid.astar, P.Grid.los, P.Grid.share_find = astar, los, find
    rows = []

    def tick(sec, nat, machine):
        g = nat.eng.grid
        rows.append((sec, nat.eng.work(), dict(cnt), g.shr_hits if g else 0))
    try:
        R.run('fan_WintermaulV151', seconds, trace_path=os.path.join(tempfile.mkdtemp(), 'share.trace'), quiet=True, tick=tick)
    finally:
        P.Grid.astar, P.Grid.los, P.Grid.share_find = o_astar, o_los, o_find

    def window(a, b):                      # game seconds a+1 .. b (rows[i] is the state at the start of second i + 1)
        r0, r1 = rows[a], rows[b]
        d = {k: r1[2][k] - r0[2][k] for k in cnt}
        d['work'] = r1[1] - r0[1]
        d['shares'] = r1[3] - r0[3]
        return d
    out = {}
    for name, (a, b) in (('61-120', (60, 120)), ('burst 82-91', (81, 91))):
        d = window(a, b)
        d['udon_s'] = (OP_EXP * d['exp'] + OP_LOS * d['los'] + OP_SCAN * d['scan']) * US_OP
        out[name] = d
    return out


def measure_soak(radius, seconds=600):
    """the 8-human Wintermaul soak (R.build(autoplay='soak'), the game check.sh replays): A* searches / expansions / LOS cells / share
    scan operations per game-second window, shares taken (and how many across a grid version bump), Udon seconds as in `measure`."""
    P.SHARE_R = max(radius, 0)
    P.SHARE_MIN_EXP = DEFAULT_MIN if radius > 0 else 1 << 60
    cnt = {'search': 0, 'exp': 0, 'los': 0, 'scan': 0}
    o_astar, o_los, o_find = P.Grid.astar, P.Grid.los, P.Grid.share_find

    def astar(self, *a, **k):
        r = o_astar(self, *a, **k)
        cnt['search'] += 1
        cnt['exp'] += self.last_exp
        return r

    def los(self, *a):
        w = self.wk
        r = o_los(self, *a)
        cnt['los'] += self.wk - w
        return r

    def find(self, *a):
        w = self.wk
        r = o_find(self, *a)
        cnt['scan'] += self.wk - w
        return r
    P.Grid.astar, P.Grid.los, P.Grid.share_find = astar, los, find
    rows = []
    try:
        prog, machine, nat, _lines = R.build('fan_WintermaulV151', autoplay='soak', trace=False)
        R.start(machine, nat)
        for sec in range(1, seconds + 1):
            g = nat.eng.grid
            rows.append((dict(cnt), g.shr_hits if g else 0, g.shr_xver if g else 0))
            machine.run_until(sec)
        g = nat.eng.grid
        rows.append((dict(cnt), g.shr_hits, g.shr_xver))
    finally:
        P.Grid.astar, P.Grid.los, P.Grid.share_find = o_astar, o_los, o_find
    out = {}
    for name, (a, b) in (('0-600', (0, seconds)), ('61-120', (60, 120)), ('241-300', (240, 300))):
        d = {k: rows[b][0][k] - rows[a][0][k] for k in cnt}
        d['shares'] = rows[b][1] - rows[a][1]
        d['xver'] = rows[b][2] - rows[a][2]
        d['udon_s'] = (OP_EXP * d['exp'] + OP_LOS * d['los'] + OP_SCAN * d['scan']) * US_OP
        d['per_s'] = d['udon_s'] / (b - a)
        out[name] = d
    return out


if __name__ == '__main__':
    soak = '--soak' in sys.argv
    radii = [int(a) for a in sys.argv[1:] if a != '--soak'] or [0, P.SHARE_R]
    if soak:
        for rad in radii:
            for name, d in measure_soak(rad).items():
                print('soak R=%d %-8s searches %6d exp %9d los cells %9d scan ops %8d shares %5d (across a version bump %5d) | pathing Udon %.1f s = %.2f s per game second'
                      % (rad, name, d['search'], d['exp'], d['los'], d['scan'], d['shares'], d['xver'], d['udon_s'], d['per_s']))
            sys.stdout.flush()
        sys.exit(0)
    for rad in radii:
        res = measure(rad)
        for name, d in res.items():
            extra = (' + other %.1f = %.1f s' % (OTHER_S, d['udon_s'] + OTHER_S)) if name == '61-120' else ''
            print('R=%d %-12s searches %5d exp %8d los cells %8d scan ops %7d shares %4d work %9d | pathing Udon %.1f s%s'
                  % (rad, name, d['search'], d['exp'], d['los'], d['scan'], d['shares'], d['work'], d['udon_s'], extra))
        sys.stdout.flush()
