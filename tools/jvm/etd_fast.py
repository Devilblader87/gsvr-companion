#!/usr/bin/env python3
"""Element TD v5.0 with game-option dialogs answered by choice (BUGS ETD-FAST): the soak player plays, every dialog it
is shown is answered with the button picked by --pick (message substring -> button index, default 0).

  python3 -m jvm.etd_fast [--seconds N] [--humans N] [--pick "Game Length=2,Extreme=1"] [--export DIR --tag TAG]
Prints the dialogs seen; --export writes DIR/TAG.{trace,inputs} for the U# harness.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                         # noqa: E402,F401
from jvm import autoplay as AP      # noqa: E402
from jvm import run as R            # noqa: E402

MAP = 'fan_ElementTDv50'


def scenario(seconds, picks, humans=1, setup=None):
    AP.SOAK_FAN[MAP] = dict(AP.SOAK_FAN.get(MAP, {}), players=[0], humans=humans)
    prog, m, nat, lines = R.build(MAP, autoplay='soak')
    eng = nat.eng
    rec = []
    seen = []
    orig = eng.post_input

    mine = {'on': False}

    def post_input(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
        if kind == 4 and not mine['on']:
            return None                      # the soak player's own first-button dialog click: this scenario chooses
        rec.append((m.turn + 1, player, kind, unit_serial, oid, float(x), float(y), target_serial))
        return orig(player, kind, unit_serial, oid, x, y, target_serial)
    eng.post_input = post_input
    st = {'n': 0}
    hp = {}

    def hook(turn):
        if not hp:
            for i in range(humans):          # player 0 first = the handle numbering of the soak player's click_dialogs
                hp[nat.H(nat.players[i])] = i
        texts = nat.last_text
        for t in texts[st['n']:]:
            if t[1] == 'dialog' and t[2] in hp and t[4]:
                o = nat.o(t[3])
                if o is not None and getattr(o, 'buttons', None):
                    idx = 0
                    for k, v in picks.items():
                        if k in (getattr(o, 'msg', '') or ''):
                            idx = v
                    seen.append((m.steps * 0.005, 'p%d ' % hp[t[2]] + getattr(o, 'msg', ''), idx, len(o.buttons)))
                    if idx < 0:                      # --pick X=-1: not clicked (its 6 s timer expires: GetClickedButton() == null)
                        continue
                    mine['on'] = True
                    eng.post_input(hp[t[2]], 4, t[3], idx)
                    mine['on'] = False
        st['n'] = len(texts)
    m.on_turn.append(hook)
    if setup is not None:
        setup(m, nat)                        # a test may watch / poke the running game (BUGS ETD-REINC soak assertions)
    R.start(m, nat)
    m.run_until(seconds)
    return m, nat, lines, rec, seen


def main():
    a = sys.argv
    secs = float(a[a.index('--seconds') + 1]) if '--seconds' in a else 120.0
    picks = {}
    if '--pick' in a:
        for kv in a[a.index('--pick') + 1].split(','):
            k, v = kv.rsplit('=', 1)
            picks[k] = int(v)
    humans = int(a[a.index('--humans') + 1]) if '--humans' in a else 1
    m, nat, lines, rec, seen = scenario(secs, picks, humans)
    for s in seen:
        print('dialog t=%.1f %r -> %d of %d' % s)
    print('units %d, trace lines %d, inputs %d' % (len(nat.units), len(lines), len(rec)))
    if '--export' in a:
        d = a[a.index('--export') + 1]
        tag = a[a.index('--tag') + 1]
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, tag + '.inputs'), 'w') as fh:
            for r in rec:
                fh.write('%d %d %d %d %d %r %r %d\n' % r)
        with open(os.path.join(d, tag + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
