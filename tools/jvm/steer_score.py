#!/usr/bin/env python3
"""STEER work loop: the acceptance scenarios of the steering / crowd port (docs/steer_progress.md) in one table, the two-level finder forced ON (W3_PATH_AB=1).

  cd tools && pypy3 -m jvm.steer_score                  every scenario, 3 at a time (processes)
  cd tools && pypy3 -m jvm.steer_score econ wmsolo      some of them
  W3_PATH_AB=0 ... flat finder (the reference column)

One line per scenario: the measured numbers and the target (flat finder / oracle). Python reference only."""
import contextlib
import io
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

ORDER = ['econ', 'wmsolo', 'maze', 'cf', 'ltw360', 'etd', 'dbz', 'harrow', 'booty', 'duskwood', 'aisoak', 'wmsoak']
TARGET = {
    'econ': 'oracle: 3 peasants 6.0 gold/s, 5 peasants 8 .. 11',
    'wmsolo': 'real lives 18 / 15 / 8 at 210 / 225 / 255 s (windows 17 .. 19 / 11 .. 16 / 5 .. 11)',
    'maze': 'oracle: first out ~87 s after the spawn, 18 out by 204 s; test: first out 60 .. 125 s, 10 .. 26 out by 204 s; 0 in a footprint',
    'cf': 'flat 517.8 s, 0 stuck',
    'ltw360': 'flat lives 9 : 41',
    'etd': 'flat: lost at 377 s',
    'dbz': 'level >= 2, defeat text, 0 errors',
    'harrow': 'flat: soldiers 9, closest ~300',
    'booty': 'flat: soldiers 13 / 7',
    'duskwood': 'flat: soldiers 6 / 11 / 11',
    'aisoak': 'flat: kills 10 (undead), > 0',
    'wmsoak': 'flat 487 units, 0 errors, 0 stuck',
}


def sc_econ():
    from jvm import test_melee_econ as T
    out = []
    for n in (3, 5):
        g = T.Game('human')
        mine = g.mine()
        ws = g.units('hpea')[:n]
        g0 = g.gold()
        for w in ws:
            g.order_target(w, T.SMART, mine.serial)
        g.run(120.0)
        out.append('%d peasants %.2f gold/s' % (n, (g.gold() - g0) / 120.0))
    return ', '.join(out)


def sc_etd():
    from jvm import etd_fast
    m, nat, lines, rec, seen = etd_fast.scenario(900, {'Game Length': 2})
    defeat = [s for s in seen if 'Defeat!' in s[1]]
    return 'Extra Short defeat at %s s' % (('%.0f' % defeat[0][0]) if defeat else 'never (900)')


def sc_dbz():
    from jvm import w9_play as W
    W.load_profiles()
    prof = W.PROFILES['fan_DBZUltimateTDProv53']
    rep = W.play(prof, 300)
    res = W.run_asserts(prof, rep)
    bad = [n for n, ok, _d, _xf in res if not ok]
    return 'asserts %d ok, failing %s | globals %s | errors %d | deaths11 %s' % (
        len(res) - len(bad), bad, rep['globals'], rep['n_errors'], rep['deaths'].get(11))


def sc_aisoak():
    from jvm import test_melee_ai as T
    h, vm, N, rows = T.race_soak('BootyBay', ('undead.ai', 'elf.ai'), 900)
    return ' | '.join('%s kills %d sent %d waves %d gold %d' % (r['script'], r['kills'], r['sent'], r['waves'], r['gold']) for r in rows) + ' | errors %d' % len(vm.errors)


def bench(name):
    from jvm import path_ab_bench as B
    B.run(name, B.SC[name])
    return None


def one(name):
    t0 = time.time()
    if name in ('wmsolo', 'maze', 'cf', 'ltw360', 'harrow', 'booty', 'duskwood', 'wmsoak'):
        bench({'maze': 'wmmaze'}.get(name, name))
        return
    r = {'econ': sc_econ, 'etd': sc_etd, 'dbz': sc_dbz, 'aisoak': sc_aisoak}[name]()
    print('%-8s %s  (%.0f s)' % (name, r, time.time() - t0))


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == '--one':
        one(args[1])
        return
    names = args or ORDER
    env = dict(os.environ)
    env.setdefault('W3_PATH_AB', '1')
    todo = list(names)
    run = []
    results = {}
    par = int(os.environ.get('STEER_PAR', '3'))
    while todo or run:
        while todo and len(run) < par:
            n = todo.pop(0)
            p = subprocess.Popen([sys.executable, '-m', 'jvm.steer_score', '--one', n], cwd=os.path.join(HERE, '..'), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            run.append((n, p))
        for n, p in list(run):
            if p.poll() is not None:
                out = p.stdout.read()
                results[n] = out.strip().splitlines()[-1] if out.strip() else '(no output)'
                run.remove((n, p))
                print(results[n])
                print('         target: %s' % TARGET.get(n, ''))
                sys.stdout.flush()
        time.sleep(0.5)


if __name__ == '__main__':
    main()
