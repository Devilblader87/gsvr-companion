#!/usr/bin/env python3
"""PATH-FIELD measurement driver (docs/path_field.md): path_ab_bench's scenarios plus the flow-field counters.

  cd tools && W3_PATH_FIELD=1 pypy3 -m jvm.path_field_bench wmmaze ltw ...

One line per scenario: wall seconds, A* searches / expansions charged (what the fields did not answer), engine work (Engine.work(), the counter
the Udon port charges; field jobs are NOT in it: reported as jobs / cells), field counters, behaviour numbers of the scenario.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

from jvm import path_ab_bench as B      # noqa: E402  (patches Grid / Engine to collect the grids)
from jvm import pathing as P            # noqa: E402
from jvm import pathfield as PF         # noqa: E402


_fc = PF.PathField.fld_cells


def _fld_cells(self, sx, sy, gx, gy, x1, y1):
    """bench wrapper: classify why a request got no route off a field (self.fld_why) and sum the expansions charged for routes that did"""
    b = (self.fld_stale, self.fld_unreach, self.fld_wait, self.fld_off, self.fld_jobs)
    r = _fc(self, sx, sy, gx, gy, x1, y1)
    if r is not None:
        self.fld_chg += self.last_exp
        return r
    if self.fld_ctx is None:
        k = 'noctx(chase)'
    elif self.gact:
        k = 'gate'
    elif self.fld_stale != b[0]:
        k = 'stale'
    elif self.fld_unreach != b[1]:
        k = 'unreach'
    elif self.fld_wait != b[2]:
        k = 'wait'
    elif self.fld_off != b[3]:
        k = 'off'
    elif self.fld_jobs != b[4]:
        k = 'newjob'
    else:
        k = 'demand'
    self.fld_why[k] = self.fld_why.get(k, 0) + 1
    self.why_last = k
    if k == 'demand' and os.environ.get('W3_DUMP'):
        bw = (self.w + 15) >> 4
        self.__dict__.setdefault('dump', []).append((self.fld_ctx[0], self.fld_ctx[1], (gy >> 4) * bw + (gx >> 4), sx, sy, gx, gy))
    return None


PF.PathField.fld_cells = _fld_cells
_as = P.Grid.astar


def _astar(self, *a, **k):
    r = _as(self, *a, **k)
    w = getattr(self, 'why_last', 'nofield')
    d = self.__dict__.setdefault('why_exp', {})
    d[w] = d.get(w, 0) + self.last_exp
    if w == 'demand' and os.environ.get('W3_DUMP') and self.__dict__.get('dump'):
        self.dump[-1] = self.dump[-1] + (self.last_exp,)
    self.why_last = 'nofield'
    return r


P.Grid.astar = _astar


for _kv in os.environ.get('W3_FLD', '').split(','):       # experiments: W3_FLD="FLD_N=3,FLD_SLOTS=16" overrides pathfield constants (FLD_NS follows)
    if '=' in _kv:
        setattr(PF, _kv.split('=')[0], int(_kv.split('=')[1]))
PF.FLD_NS = PF.FLD_SLOTS + PF.FLD_US


def run(name, fn):
    del B.GRIDS[:]
    del B.ENGS[:]
    t0 = time.time()
    extra = fn()
    dt = time.time() - t0
    s, p, wk, ab = B.stats()
    G = B.GRIDS
    fl = 'jobs %d jobcells %d installs %d served %d cells %d stale %d unreach %d wait %d off %d ujobs %d userved %d' % (
        sum(g.fld_jobs for g in G), sum(g.fld_jobcells for g in G), sum(g.fld_installs for g in G), sum(g.fld_served for g in G),
        sum(g.fld_cellsx for g in G), sum(g.fld_stale for g in G),
        sum(g.fld_unreach for g in G), sum(g.fld_wait for g in G), sum(g.fld_off for g in G),
        sum(g.fld_ujobs for g in G), sum(g.fld_userved for g in G))
    if os.environ.get('W3_DUMP'):
        import json
        json.dump([g.__dict__.get('dump', []) for g in G], open(os.environ['W3_DUMP'], 'w'))
    why = {}
    for g in G:
        for k, v in g.fld_why.items():
            why[k] = why.get(k, 0) + v
    wexp = {}
    for g in G:
        for k, v in g.__dict__.get('why_exp', {}).items():
            wexp[k] = wexp.get(k, 0) + v
    chg = sum(g.fld_chg for g in G)
    srv = sum(g.fld_served for g in G)
    fl += ' | REAL A* searches %d exp %d, field share of searches %.1f %% (%d), of search-stage requests %.1f %% miss %s REAL-exp-by-reason %s' % (s - srv, p - chg, 100.0 * srv / max(1, s), srv, 100.0 * srv / max(1, srv + sum(v for k, v in why.items() if not k.startswith(('job_', 'u_')))), why, wexp)
    print('%-8s %s wall %7.1f s  A* searches %7d  exp %10d  work %12d%s  | field %s | %s' % (
        name, 'FIELD' if any(g.fld_on for g in G) else 'off  ', dt, s, p, wk, ab, fl, extra))
    sys.stdout.flush()


if __name__ == '__main__':
    for n in sys.argv[1:] or list(B.SC):
        run(n, B.SC[n])
