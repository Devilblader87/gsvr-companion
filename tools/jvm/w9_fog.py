#!/usr/bin/env python3
"""FOG play check: runs a W9 hero-play profile (default x hero siege 3.53: FogEnable / FogMaskEnable on at init, the map's
3000 / 1500 / 1000 VISIBLE modifiers) and reports the visible / fogged / masked cell counts of the human slot over time
(docs/NOTES_fog.md, docs/BUGS.md FOG, docs/w9_criteria.md).

  python3 -m jvm.w9_fog [MAP] [--seconds 300] [--every 20] [--slot 0] [--mods]

Exit 1 when the fog never became active, or the map shows no fog at all (no masked / fogged cell), or the counts never move.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from jvm import w9_play as WP   # noqa: E402
from jvm import fog as FG       # noqa: E402


# the map's own modifiers (CreateFogModifierRadiusLocBJ(true, player, VISIBLE, centre of rect, radius) per player, started): the
# autoplayer does not reach the phase that creates them in 300 s, so --mods makes the same calls through the natives
MODS = ((100.0, 'udg_rect11', 3000.0), (160.0, 'udg_rect12', 1500.0), (220.0, 'udg_rect02', 1000.0))


def make_mods(m, nat, rect_global, radius):
    from jvm.natives import REG
    h = m.global_value(rect_global)
    ro = nat.o(h)
    if ro is None:
        return 0
    lo = REG['Location'](nat, (ro.minx + ro.maxx) * 0.5, (ro.miny + ro.maxy) * 0.5)
    out = 0
    for pl in nat.players[:12]:
        if pl.slot_state != 1:
            continue
        mod = REG['CreateFogModifierRadiusLoc'](nat, nat.H(pl), 4, nat.H(lo), radius, True, False)
        REG['FogModifierStart'](nat, nat.H(mod))
        out += 1
    return out


def run(key='fan_xherosiege353', seconds=300, every=20, slot=0, verbose=True, mods=False):
    WP.load_profiles()
    prof = WP.PROFILES[key]
    rows = []

    def hook(m, nat):
        nxt = [0.0]
        todo = list(MODS) if mods else []

        def on_turn(turn):
            t = m.steps * 0.005
            if todo and t >= todo[0][0]:
                _t, g, r = todo.pop(0)
                for mm in list(nat.fogmods):                       # the map destroys the previous one (DestroyFogModifier)
                    nat.fogmods.remove(mm)
                make_mods(m, nat, g, r)
            if t + 1e-9 >= nxt[0]:
                nxt[0] += every
                c = FG.fog_counts(nat, slot)
                ml = [mm.on for mm in nat.fogmods]
                rows.append((round(t, 1), c, nat.fog[0], nat.fog[1], sum(1 for x in ml if x), len(ml),
                             nat.fogg.rebuilds if nat.fogg else 0))
        m.on_turn.append(on_turn)
    rep = WP.play(prof, seconds, hook=hook, trace=False)
    if verbose:
        print('%s %d s, slot %d: errors %d' % (key, seconds, slot, rep['n_errors']))
        print('   t     visible fogged masked   fog mask  modifiers(on/all)  rebuilds')
        for t, c, f, mk, on, n, rb in rows:
            print('  %5.1f  %s   %d   %d     %d/%d        %d' % (t, ('%7s %6s %6s' % c) if c else '    (no grid yet)', f, mk, on, n, rb))
    return rep, rows


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    seconds = int(argv[argv.index('--seconds') + 1]) if '--seconds' in argv else 300
    every = int(argv[argv.index('--every') + 1]) if '--every' in argv else 20
    slot = int(argv[argv.index('--slot') + 1]) if '--slot' in argv else 0
    keys = [a for i, a in enumerate(argv) if not a.startswith('--') and (i == 0 or argv[i - 1] not in ('--seconds', '--every', '--slot'))]
    rep, rows = run(keys[0] if keys else 'fan_xherosiege353', seconds, every, slot, mods='--mods' in argv)
    seen = [r for r in rows if r[1]]
    ok = bool(seen) and any(r[1][1] + r[1][2] > 0 for r in seen) and len({r[1] for r in seen}) > 1
    print('FOG PLAY CHECK', 'PASS' if ok and rep['n_errors'] == 0 else 'FAIL')
    return 0 if ok and rep['n_errors'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
