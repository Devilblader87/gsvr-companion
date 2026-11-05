#!/usr/bin/env python3
"""Line Tower Wars 7 v1.1~4: the builder stays in its own lane (BUGS LTW-LANE).

How the original does it [obs work/maps/fan_linetowerwars7v114]: no trigger (no leave-region / SetUnitPosition on h002),
no pathing-blocker destructable. The builder h002 has movement type "fly" (w3u umvt) and the .wpm puts a 24-cell wide
wall (flags 0xce: not walkable, not flyable, not buildable) with a 4-cell unwalkable-but-flyable rim on both sides
between every two lanes. A flyer cannot enter a cell with 0x04, so it never reaches the neighbour lane; a build order
there is carried out on arrival only, so no tower is built. The engine ignored 0x04 for flyers (straight line move).

  python3 -m jvm.ltw_lane                 checks (exit 1 on a failed check)
  python3 -m jvm.ltw_lane --export DIR    + DIR/<tag>.{trace,inputs} for the U# harness:
                                          W3_MAPS "fan_linetowerwars7v114*2@40=DIR/<tag>.trace,DIR/<tag>.inputs"
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                         # noqa: E402
from jvm import run as R            # noqa: E402

MAP = 'fan_linetowerwars7v114'
SECONDS = 40
O_MOVE = 851986
TOWER = 'h000'                      # Sharp Tower
OWN = (-6144.0, -3072.0)            # lane 1 (player 1's builder)
NEIGHBOUR = (-4096.0, -3072.0)      # lane 2 centre, same row
SITE = (-4096.0, -2816.0)           # a buildable spot of lane 2 (the own lane has the same one at x = -6144)


def tag():
    return '%s_lane_%ds' % (MAP, SECONDS)


def scenario(record=None):
    prog, machine, nat, lines = R.build(MAP, humans=2)
    eng = nat.eng
    if record is not None:
        orig = eng.post_input

        def post_input(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
            record.append((machine.turn, player, kind, unit_serial, oid, float(x), float(y), target_serial))
            return orig(player, kind, unit_serial, oid, x, y, target_serial)
        eng.post_input = post_input
    R.start(machine, nat)
    machine.run_until(5)
    p0 = nat.players[0]
    b = [u for u in nat.units if u.alive and u.owner is p0 and u.data.get('id4') == 'h002'][0]
    rep = {'start': (b.x, b.y)}
    machine.run_until(6)

    def towers():
        return [u for u in nat.units if u.alive and u.data.get('id4') == TOWER]
    n0 = len(towers())
    eng.post_input(0, 1, b.serial, O_MOVE, NEIGHBOUR[0], NEIGHBOUR[1])        # walk into the neighbour lane
    machine.run_until(16)
    rep['move_end'] = (b.x, b.y)
    eng.post_input(0, 1, b.serial, O_MOVE, OWN[0], OWN[1])                    # back home
    machine.run_until(22)
    rep['home'] = (b.x, b.y)
    eng.post_input(0, 1, b.serial, jass.rawcode(TOWER), SITE[0], SITE[1])    # build in the neighbour lane
    machine.run_until(32)
    rep['build_end'] = (b.x, b.y)
    rep['towers_after_foreign'] = len(towers()) - n0
    eng.post_input(0, 1, b.serial, jass.rawcode(TOWER), OWN[0], SITE[1])        # control: own lane
    machine.run_until(SECONDS)
    rep['towers_after_own'] = len(towers()) - n0
    rep['rejected'] = eng.stats['inputs_rejected']
    return nat, lines, rep


def check(verbose=True, export=None):
    fails = []

    def ok(c, what):
        if verbose:
            print('  %s %s' % ('ok  ' if c else 'FAIL', what))
        if not c:
            fails.append(what)
    rec = []
    nat, lines, rep = scenario(rec)
    if verbose:
        print(rep)
    ok(rep['start'][0] < -5500.0, 'the builder of player 1 starts in lane 1 %r' % (rep['start'],))
    ok(rep['move_end'][0] < -5400.0, 'a move order into lane 2 ends at the wall, not in lane 2 %r' % (rep['move_end'],))
    ok(rep['build_end'][0] < -5400.0 and rep['towers_after_foreign'] == 0,
       'a build order in lane 2 builds nothing, the builder stays in lane 1 %r' % (rep['build_end'],))
    ok(rep['towers_after_own'] == 1, 'control: a tower in the own lane is built (%d)' % rep['towers_after_own'])
    if export:
        t = tag()
        with open(os.path.join(export, t + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
        with open(os.path.join(export, t + '.inputs'), 'w') as fh:
            for r in rec:
                fh.write('%d %d %d %d %d %r %r %d\n' % r)
        if verbose:
            print('  wrote %s.{trace,inputs} (%d lines, %d inputs)' % (os.path.join(export, t), len(lines), len(rec)))
    return fails


def main():
    export = None
    if '--export' in sys.argv:
        export = sys.argv[sys.argv.index('--export') + 1]
        os.makedirs(export, exist_ok=True)
    fails = check(True, export)
    print('ltw_lane: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
