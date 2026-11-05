#!/usr/bin/env python3
"""Line Tower Wars 7 v1.1~4: sending units (BUGS C-SHOP). Player 1 buys a Footman (hfoo) at its "Basic Monsters"
shop h01I (Sellunits → the hidden Asel, NOTES_items §7.6; no Aneu → no buyer, the unit appears at the shop).

The map: CreateNeutralPassiveBuildings puts 7 × 4 shops (h01E / h01I / h01B / h01D, classification "sapper") on the
base strip y = -3392 inside gg_rct_Spawn; Init gives the four within 500 of each player's start location to that
player (SetUnitOwner) and removes the rest of Neutral Passive. A bought unit is created at the shop, i.e. inside
gg_rct_Spawn → trigger "spawn" (non-structure, non-sapper, not the builder h002):
  * one player in udg_ThePlayers → RemoveUnit (solo: sending is off, as the map intends);
  * else Income[owner] += GetUnitPointValue (hfoo 12 → "Player 1 (37)"), the unit goes to a random point of the
    next player's StartRegion with move + attack orders to that player's EndRegion.

  python3 -m jvm.ltw_send                 checks (exit 1 on a failed check)
  python3 -m jvm.ltw_send --export DIR    + DIR/<tag>.{trace,inputs} for the U# harness, per player count:
                                          W3_MAPS "fan_linetowerwars7v114*2@90=DIR/fan_linetowerwars7v114_send2_90s.trace,
                                          DIR/fan_linetowerwars7v114_send2_90s.inputs" (same with *1 / send1)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                         # noqa: E402
from jvm import run as R            # noqa: E402

MAP = 'fan_linetowerwars7v114'
SECONDS = 90
BUY_AT = 81                         # hfoo stockStart 80 s (the shop's start delay, NOTES_items §7.3)
SHOP_Y = -3392.0


def tag(humans):
    return '%s_send%d_%ds' % (MAP, humans, SECONDS)


def scenario(humans, record=None):
    """-> (nat, lines, report dict)."""
    prog, machine, nat, lines = R.build(MAP, humans=humans)
    eng = nat.eng
    if record is not None:
        orig = eng.post_input

        def post_input(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
            # posted between run_until calls (not inside a turn's on_turn): applied at the start of turn machine.turn
            record.append((machine.turn, player, kind, unit_serial, oid, float(x), float(y), target_serial))
            return orig(player, kind, unit_serial, oid, x, y, target_serial)
        eng.post_input = post_input
    R.start(machine, nat)
    machine.run_until(5)
    p0 = nat.players[0]
    pid = lambda p: nat.players.index(p)
    shops = sorted((u.data.get('id4'), pid(u.owner), u.x, u.y) for u in nat.units
                   if u.alive and u.data.get('id4') in ('h01E', 'h01I', 'h01B', 'h01D'))
    machine.run_until(BUY_AT)
    shop = [u for u in nat.units if u.alive and u.data.get('id4') == 'h01I' and u.owner is p0][0]
    gold0 = p0.state.get(1, 0)
    before = {u.serial for u in nat.units}
    n0 = len(lines)
    eng.post_input(0, 5, shop.serial, jass.rawcode('hfoo'))
    machine.run_until(SECONDS)
    bought = [u for u in nat.units if u.serial not in before and u.data.get('id4') == 'hfoo']
    created = [ln for ln in lines[n0:] if 'GetUnitPointValue' in ln or 'RemoveUnit(' in ln or 'LeaderboardSetItemLabel' in ln]
    rep = {'shops': shops, 'gold_before': gold0, 'gold_after': p0.state.get(1, 0), 'rejected': eng.stats['inputs_rejected'],
           'bought': [(pid(u.owner), u.x, u.y, u.order) for u in bought], 'lines': created}
    return nat, lines, rep


def check(verbose=True, export=None):
    fails = []

    def ok(c, what):
        if verbose:
            print('  %s %s' % ('ok  ' if c else 'FAIL', what))
        if not c:
            fails.append(what)
    for humans in (2, 1):
        rec = []
        nat, lines, rep = scenario(humans, rec)
        if verbose:
            print('%d player(s): %s' % (humans, {k: v for k, v in rep.items() if k != 'lines'}))
        own = [s for s in rep['shops'] if s[1] < humans]
        ok(len(rep['shops']) == 4 * humans and len(own) == 4 * humans and all(s[3] == SHOP_Y for s in rep['shops']),
           '%dp: the 4 shops of each player stand on the base strip, owned by the player (%d)' % (humans, len(rep['shops'])))
        ok(rep['rejected'] == 0 and rep['gold_before'] - rep['gold_after'] == 75,
           '%dp: the purchase went through, 75 gold paid' % humans)
        if humans == 1:
            ok(not rep['bought'] and any('RemoveUnit(' in ln for ln in rep['lines']),
               '1p: the bought footman is removed at once (spawn trigger, solo)')
        else:
            b = rep['bought']
            ok(len(b) == 1 and b[0][0] == 0 and -4608.0 <= b[0][1] <= -3584.0 and b[0][2] > -3008.0,
               '2p: the footman (player 1) walks down player 2\'s lane: %r' % (b,))
            ok(any('"Player 1 (37)"' in ln for ln in rep['lines']) and any('GetUnitPointValue' in ln and '-> 12' in ln for ln in rep['lines']),
               '2p: income 25 + point value 12 = 37 on the leaderboard')
        if export:
            t = tag(humans)
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
    print('ltw_send: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
