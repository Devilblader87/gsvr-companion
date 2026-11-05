#!/usr/bin/env python3
"""PATH-AB measurement driver: the same scenarios with the flat A* (W3_PATH_AB=0) and the A + B path finder (W3_PATH_AB=1).

  cd tools && W3_PATH_AB=1 pypy3 -m jvm.path_ab_bench ltw wmmaze wmsolo wmsoak cf booty harrow duskwood

Prints one line per scenario: wall seconds, searches served (cache hits included), pops / A* expansions charged to the budget (Grid.st_pops),
engine work units (Engine.work(): the counter the Udon port charges), plus the behaviour numbers of the scenario. Python only (the Udon twin
returns the same counts: its trace is byte-identical).
"""
import contextlib
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

from jvm import pathing as P        # noqa: E402
from jvm import engine as E         # noqa: E402

GRIDS, ENGS = [], []
_gi, _ei = P.Grid.__init__, E.Engine.__init__


def _grid_init(self, *a, **k):
    _gi(self, *a, **k)
    GRIDS.append(self)


STUCK_EVERY = 2000                  # steps (10 s) between samples; a mover that has not left its place for STUCK_N samples (120 s) is stuck
STUCK_N = 12


def _eng_init(self, *a, **k):
    _ei(self, *a, **k)
    ENGS.append(self)
    self.stuck_seen = {}            # serial -> (x, y, samples in place)
    self.stuck_max = 0
    self.stuck_ever = set()
    step0 = self.step

    def step(dt, _s=step0, _e=self):
        _s(dt)
        n = _e.vm.steps
        if n % STUCK_EVERY == 0 and n > 0:
            seen = _e.stuck_seen
            cur = {}
            for u in list(_e.N.moving):
                if not u.alive or u.dead or u.order == 0 or u.atk_target is not None or u.gpt is not None:
                    continue                # (a unit standing to attack is not stuck)
                o = seen.get(u.serial)
                same = o[2] + 1 if o is not None and o[0] == u.x and o[1] == u.y else 0
                cur[u.serial] = (u.x, u.y, same)
                if same >= STUCK_N:
                    _e.stuck_ever.add(u.serial)
            _e.stuck_seen = cur
            _e.stuck_max = max(_e.stuck_max, sum(1 for v in cur.values() if v[2] >= STUCK_N))
    self.step = step


P.Grid.__init__ = _grid_init
E.Engine.__init__ = _eng_init


def stats():
    s = sum(g.st_search for g in GRIDS)
    p = sum(g.st_pops for g in GRIDS)
    ab = '' if not P.PATH_AB else ' A %d/%d B %d/%d' % (sum(g.st_asearch for g in GRIDS), sum(g.st_apops for g in GRIDS),
                                                         sum(g.st_bsearch for g in GRIDS), sum(g.st_bpops for g in GRIDS))
    wk = sum(e.work() for e in ENGS)
    ab += ' stuck(>=120 s) max %d ever %d' % (max([e.stuck_max for e in ENGS] or [0]), sum(len(e.stuck_ever) for e in ENGS))
    return s, p, wk, ab


def run(name, fn):
    del GRIDS[:]
    del ENGS[:]
    t0 = time.time()
    extra = fn()
    dt = time.time() - t0
    s, p, wk, ab = stats()
    print('%-8s %s wall %7.1f s  searches %7d  pops %10d  work %12d%s  | %s' % (
        name, 'AB ' if P.PATH_AB else 'flat', dt, s, p, wk, ab, extra))
    sys.stdout.flush()


def sc_ltw():
    from jvm import w9_play as W
    from jvm import w9_prof_pvp as PV
    rep = W.play(PV.PROFILES[PV.LTW7], 900, seed=int(os.environ.get('W3_SEED', '1')))
    lives = rep.get('series') or {}
    res = W.run_asserts(PV.PROFILES[PV.LTW7], rep)
    ends = [i + 1 for k, v in lives.items() for i, x in enumerate(v) if x == 0][:1]
    return 'end %s s seed %s lives %s errors %d asserts %d/%d%s' % (ends, os.environ.get('W3_SEED', '1'), {k: (v[-1] if v else None) for k, v in lives.items()}, rep['n_errors'],
                                                  sum(1 for r in res if r[1]), len(res), ''.join(' FAIL:%s' % r[0] for r in res if not r[1]))


def sc_wmmaze():
    from jvm import maze
    fails, m, b = maze.check(False)
    return 'fails %d %s maze %s' % (len(fails), [str(f)[:90] for f in fails[:3]], {k: m[k] for k in ('lane', 'exited', 'blue', 'trapped', 'inside')})


def sc_wmsolo():
    from importlib import import_module
    sys.path.insert(0, os.path.join(HERE, '..', 'oracle'))
    vp = import_module('vmprobe')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vp.main('fan_WintermaulV151', os.path.join(HERE, '..', 'oracle', 'probes', 'wm_creepmap.j'), '256')
    import re
    lives = {int(t): int(v) for t, v in re.findall(r'W3M (\d+) lives (\d+)', buf.getvalue())}
    return 'lives ' + ' '.join('%d:%d' % (t, lives[t]) for t in (150, 180, 210, 225, 240, 255) if t in lives)


def sc_wmsoak():
    from jvm import run as R
    prog, m, nat, lines = R.build('fan_WintermaulV151', trace=False, humans=8, autoplay='soak')
    R.start(m, nat)
    m.run_until(600)
    return 'units %d errors %d' % (len(nat.units), len(m.errors))


def sc_cf():
    from jvm import test_w9_hybrid as H
    import re
    nat, machine, players, rec = H.play(H.CF, int(os.environ.get("CF_SECONDS", "700")))
    won = [t[0] for t in nat.last_text if t[1] == 'text' and 'have won the match' in re.sub(r'\|[cC][0-9a-fA-F]{8}|\|[rR]', '', t[3])]
    return 'round ends at %s' % (('%.1f s' % (min(won) / 1000.0)) if won else 'never (%s s)' % os.environ.get('CF_SECONDS', '700'))


def melee(key, slots):
    from jvm import test_melee_bot as MB
    o = MB.play(key, slots)
    return 'errors %d rejected %d ' % (len(o['errors']), o['rejected']) + ' '.join(
        'slot%d %s soldiers %d waves %d closest %.0f income %d' % (s, r['wt'], r['soldiers'], r['bot'].melee.waves, r['min_dist'], r['income'])
        for s, r in o['bots'].items())


def sc_ltw360():
    from jvm import w9_play as W
    from jvm import w9_prof_pvp as PV
    rep = W.play(PV.PROFILES[PV.LTW7], 360)
    lives = rep.get('series') or {}
    return 'lives %s errors %d' % ({k: (v[-1] if v else None) for k, v in lives.items()}, rep['n_errors'])


def sc_kiga():
    """Kindergarten TD (fan_KigaTDv30p, w9_prof_kiga): 8 humans build, 8 lanes of creeps run to one tree house; KIGA_SECONDS (330) s, KIGA_PLAYERS (8) humans, KIGA_BONUS test gold per 30 s, W3_SEED. Behaviour: level V, lives E, leaks, kills, creeps in a footprint."""
    from jvm import w9_play as W
    from jvm import w9_prof_kiga as K
    sec = int(os.environ.get('KIGA_SECONDS', '330'))
    n = int(os.environ.get('KIGA_PLAYERS', '8'))
    prof = dict(K.PROFILES['fan_KigaTDv30p'], humans=n, make=lambda N: K.make_players(N, n, int(os.environ.get('KIGA_BONUS', K.BONUS))))
    inside = [0]

    def hook(m, nat):
        eng = nat.eng
        last = [0]

        def on_turn(turn):
            if m.steps - last[0] < 200:         # once per game second: a ground creep whose cell is inside a living structure's footprint (maze.py's check)
                return
            last[0] = m.steps
            blk = set()
            for b in nat.units:
                if b.alive and not b.dead and b.is_bldg and getattr(b, 'footprint', None):
                    blk.update((cx, cy) for cx, cy, mk in b.footprint if mk & 1)
            for u in nat.units:
                if u.owner.id == 11 and u.alive and not u.dead and not u.is_bldg and not eng.flying(u) and u.pathing and eng.grid.cell_of(u.x, u.y) in blk:
                    inside[0] += 1
        m.on_turn.append(on_turn)
    rep = W.play(prof, sec, seed=int(os.environ.get('W3_SEED', '1')), hook=hook, trace=False)
    s = rep['series']
    res = W.run_asserts(prof, rep)
    return 'V %s E %s leaks %s kills %d deaths(creeps) %d towers %d inputs %d/%d errors %d inside %d asserts %d/%d%s' % (
        s['V'][-1], s['E'][-1], s['N'][-1], sum(rep['kills'].values()), rep['deaths'].get(11, 0), rep['built'].get(0, 0), rep['inputs'], rep['rejected'], rep['n_errors'], inside[0],
        sum(1 for r in res if r[1]), len(res), ''.join(' FAIL:%s' % r[0] for r in res if not r[1]))


SC = {'kiga': sc_kiga, 'ltw360': sc_ltw360, 'ltw': sc_ltw, 'wmmaze': sc_wmmaze, 'wmsolo': sc_wmsolo, 'wmsoak': sc_wmsoak, 'cf': sc_cf,
      'booty': lambda: melee('BootyBay', (0, 1)), 'harrow': lambda: melee('Harrow', (1,)),
      'duskwood': lambda: melee('Duskwood', (1, 2, 3))}

if __name__ == '__main__':
    for n in sys.argv[1:] or list(SC):
        run(n, SC[n])
