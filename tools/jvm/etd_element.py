#!/usr/bin/env python3
"""Element TD v5.0: an element from the Summoning Centre (BUGS SH-BORN; maintainer 2026-10-25: "kein Licht-Upgrade").

The map (its help text, quest "Information"): 1 lumber every 5 rounds; spend it at the Elemental Summoning Centre
(h00S, dynamic stock on Asud: the six level-1 elementals) — the bought elemental (Light: Spirit Owl n00X) is created
at the centre, the map gives it to the creeps' player and it walks the lane; when it dies, the map researches the
element (Light = R004 level 1) and gives the player a marker unit. Then a Super Cannon / Arrow Tower (h01Y / h01V)
can be upgraded to the Light Cannon / Arrow Tower (h03E / h03D).

Scenario: the solo soak player (autoplay.SoakPlayer) plays; at 100 s test lumber +1 (input kind 3, target serial
1), at 100.5 s player 1 buys n00X at its centre (kind 5), at 165 s test gold +500 (kind 3). Checks: the elemental
was sold to the creeps' side and died, Light is researched, R004 makes h03E available.

  python3 -m jvm.etd_element               checks (exit 1 on a failed check)
  python3 -m jvm.etd_element --export DIR  + DIR/fan_ElementTDv50_element_240s.{trace,inputs} for the U# harness
                                            (W3_MAPS "fan_ElementTDv50@240=DIR/<tag>.trace,DIR/<tag>.inputs"; the client
                                            autotest W3AutoTest.SoakUpgradeCheck presses the upgrade on the card)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                         # noqa: E402
from jvm import autoplay as AP      # noqa: E402
from jvm import run as R            # noqa: E402

MAP = 'fan_ElementTDv50'
SECONDS = 240
TAG = '%s_element_%ds' % (MAP, SECONDS)


def scenario():
    AP.SOAK_FAN[MAP] = dict(AP.SOAK_FAN.get(MAP, {}), players=[0], humans=1)
    prog, m, nat, lines = R.build(MAP, autoplay='soak')
    eng = nat.eng
    rec = []
    orig = eng.post_input

    def post_input(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
        rec.append((m.turn + 1, player, kind, unit_serial, oid, float(x), float(y), target_serial))
        return orig(player, kind, unit_serial, oid, x, y, target_serial)
    eng.post_input = post_input
    todo = [100.0, 100.5, 165.0]
    sold = []

    def hook(turn):
        if not todo or m.steps * 0.005 < todo[0]:
            return
        w = todo.pop(0)
        if w == 100.0:
            eng.post_input(0, 3, 0, 1, 0.0, 0.0, 1)               # test lumber +1
        elif w == 100.5:
            cen = [u for u in nat.units if u.alive and u.data.get('id4') == 'h00S' and u.owner.id == 0][0]
            eng.post_input(0, 5, cen.serial, jass.rawcode('n00X'))
        else:
            eng.post_input(0, 3, 0, 500)                          # test gold for the upgrade
    m.on_turn.append(hook)
    R.start(m, nat)
    m.run_until(SECONDS)
    return nat, lines, rec


def check(verbose=True, export=None):
    nat, lines, rec = scenario()
    fails = []

    def ok(c, what):
        if verbose:
            print(('  ok   ' if c else '  FAIL ') + what)
        if not c:
            fails.append(what)
    owl = jass.rawcode('n00X')
    owls = [u for u in nat.units if u.tid == owl]
    p0 = nat.players[0]
    mark = [u for u in owls if u.owner.id == 0 and u.alive and not u.dead]
    ok(len(mark) == 1 and all(u.owner.id == 0 for u in owls),
       'the bought Spirit Owl died for the creeps (gone), player 1 got the marker owl (%d)' % len(mark))
    ok(p0.tech.get(jass.rawcode('R004'), 0) == 1, 'Light (R004) researched: %d' % p0.tech.get(jass.rawcode('R004'), 0))
    ok(nat.eng.req_ok(p0, jass.rawcode('h03E')) and not nat.eng.req_ok(p0, jass.rawcode('h038')),
       'the Light Cannon Tower is available, the Darkness one is not')
    if export:
        os.makedirs(export, exist_ok=True)
        with open(os.path.join(export, TAG + '.inputs'), 'w') as fh:
            for r in rec:
                fh.write('%d %d %d %d %d %r %r %d\n' % r)
        with open(os.path.join(export, TAG + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
        print('exported %s.{trace,inputs} -> %s (%d inputs, %d trace lines)' % (TAG, export, len(rec), len(lines)))
    return fails


def main():
    export = sys.argv[sys.argv.index('--export') + 1] if '--export' in sys.argv else None
    fails = check(True, export)
    print('etd_element: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
