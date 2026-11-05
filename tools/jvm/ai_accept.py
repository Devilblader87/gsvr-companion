"""docs/ai_plan.md S7: full-game acceptance of the real race scripts, 4 races x 3 computer levels x 2 maps.

  pypy3 -m jvm.ai_accept bootybay [seconds] [level,...]     12 two-player games: game (race r, level L): slot 0 = race r, slot 1 = race r + 1
                                                             (both at L), so every (race, level) plays once on each side
  pypy3 -m jvm.ai_accept duskwood [seconds] [level,...]     3 four-player games (all four races at the same level L)
  pypy3 -m jvm.ai_accept one MAP SECONDS L script,script,.. one game; prints the series every 150 s

Each game: 0 VM errors, no thread death, gold + lumber gathered, structures built, army, a wave sent, a kill, a hero skill.  Prints one
JSON-ish row per AI (the table of the S7 section).  Needs the player's install (work/core/common.j + the race scripts)."""
import json
import sys

from jvm import aivm as AV
from jvm import test_melee_ai as T

LEVEL_NAMES = ('easy', 'normal', 'insane')


def game(mapkey, scripts, level, seconds, series=150, teams=None):
    levels = [level] * len(scripts) if isinstance(level, int) else list(level)
    h, vm, N, rows = T.race_soak(mapkey, scripts, seconds, levels=levels, series=series, teams=teams)
    res = []
    for r in rows:
        s = r['slot']
        alive = sum(1 for a, _w in h.thread_states(s) if a)
        r = dict(r)
        r.update(secs=seconds, map=mapkey, level=levels[s], errors=len(vm.errors), thread_errors=len(h.slots[s].errors), alive=alive, unimpl=len(h.unimpl))
        res.append(r)
    return res


def verdict(r):
    bad = []
    if r['errors']:
        bad.append('vm errors %d' % r['errors'])
    if r['thread_errors']:
        bad.append('thread died')
    if r['alive'] < 4:
        bad.append('threads alive %d' % r['alive'])
    if r['gold'] <= 0 or r['lumber'] <= 0:
        bad.append('no gather')
    if r['structures'] < r['start'][1] + 3:
        bad.append('few structures')
    if r['waves'] < 1:
        bad.append('no wave')
    if r['kills'] < 1:
        bad.append('no kill')
    if r['gold'] > 18 * r['secs']:                       # AI-HALLMINE: more than 18 gold/s on average = a trip loop (two halls on two mines make ~ 15)
        bad.append('gold rate')
    if r['unimpl']:
        bad.append('unimpl')
    return ','.join(bad) or 'ok'


def show(rows):
    print('%-8s %-7s %-9s %3s %4s %5s %6s %5s %6s %7s %5s %5s %5s %5s %6s %-6s' % (
        'map', 'level', 'script', 'wk', 'str', 'army', 'halls', 'peak', 'gold', 'lumber', 'first', 'waves', 'sent', 'kills', 'skills', 'verdict'))
    for r in rows:
        print('%-8s %-7s %-9s %3d %4d %5d %3d/%-2d %5d %6d %7d %4.0fs %5d %5d %5d %6d %s' % (
            r['map'], LEVEL_NAMES[r['level']], r['script'], r['workers'], r['structures'], r['army'], r['halls'], r['hall_peak'], r['army_peak'],
            r['gold'], r['lumber'], r['first'], r['waves'], r['sent'], r['kills'], r['skills'], verdict(r)))
    sys.stdout.flush()


def main(argv):
    what = argv[1]
    if what == 'one':
        rows = game(argv[2], argv[5].split(','), int(argv[4]), int(argv[3]))
        show(rows)
        for r in rows:
            print(r['script'], 'series (s, workers, structures, army, halls, gold, lumber, sent):', r['series'])
        return
    secs = int(argv[2]) if len(argv) > 2 else 900
    levels = [int(x) for x in argv[3].split(',')] if len(argv) > 3 and what != 'team' else [0, 1, 2]
    if what == 'team':                                       # Duskwood 2 v 2 (slots 0 + 1 against 2 + 3): human + orc at level A against undead + elf at level B
        a, b = int(argv[3]), int(argv[4])
        rows = game('Duskwood', tuple(AV.RACE_SCRIPTS), [a, a, b, b], secs, teams=((0, 1), (2, 3)))
        show(rows)
        return
    allrows = []
    sc = AV.RACE_SCRIPTS
    if what == 'bootybay':
        for lv in levels:
            for i in range(4):
                rows = game('BootyBay', (sc[i], sc[(i + 1) % 4]), lv, secs)
                show(rows)
                allrows += rows
    elif what == 'duskwood':
        for lv in levels:
            rows = game('Duskwood', tuple(sc), lv, secs)
            show(rows)
            allrows += rows
    with open('/tmp/ai_accept_%s.json' % what, 'w') as fh:
        json.dump(allrows, fh)


if __name__ == '__main__':
    main(sys.argv)
