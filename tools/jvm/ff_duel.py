#!/usr/bin/env python3
"""Footman Frenzy XV 4.84, 1 vs 1 (BUGS FF-*): two players on different teams, the map's setup dialogs left to their
defaults (the host does not answer: 3v3v3v3, XV heroes, normal rules, trading on), both set the rally point of their
barracks ("Tier Zero", Trains empty / Revive=1) on the enemy barracks; the spawned footmen walk there and fight.

The map (old-style custom data: its own Units\\*.slk / *.txt, BUGS FF-SLK): 12 team slots (3v3v3v3, w3i forces), a
setup dialog chain behind PauseGameOn + a black cinematic filter (EnableUserUI(false)), then every player's footmen
spawn at his barracks every few seconds and run to its rally point (attack-move). Team elimination = victory; the
slots nobody plays are not in "all players" (ForceEnumPlayers takes occupied slots only, BUGS FF-ALL).

  python3 -m jvm.ff_duel                 checks (exit 1 on a failed check)
  python3 -m jvm.ff_duel --export DIR    + DIR/fan_FootmanFrenzyXV484_duel2_240s.{trace,inputs} for the U# harness:
                                         W3_MAPS "fan_FootmanFrenzyXV484*2@240=DIR/<tag>.trace,DIR/<tag>.inputs"
  python3 -m jvm.ff_duel --upgrades      BUGS FF-UPG: + the two players research Iron Forged Weapons (Rhme: attack dice) and Plating (Rhar: armor)
                                         at the Tier Zero barracks, level 1 (levels 2 / 3 need a Keep / Castle, UpgradeFunc Requires1 / 2;
                                         test gold at 90 s); the footmen carry the effects
                                         (with --export: DIR/fan_FootmanFrenzyXV484_upg2_240s.{trace,inputs})
  python3 -m jvm.ff_duel --revive        W9R3-1: + a hero revive at the Tier Zero barracks (docs/NOTES_production.md section 1.4):
                                         test-only inputs spawn a hero (kind 10) at 100 s and kill it (kind 11) at 120 s, the
                                         player queues it at the barracks (kind 0, order id = the hero type) at 125 s;
                                         REVIVE_START / FINISH are raised and the map's own REVIVE_FINISH trigger runs
                                         (with --export: DIR/fan_FootmanFrenzyXV484_revive2_240s.{trace,inputs})
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                         # noqa: E402
from jvm import run as R            # noqa: E402
from jvm import vm as V             # noqa: E402
from jvm.autoplay import SoakPlayer  # noqa: E402
from jvm.engine import SPS, O_ATTACK  # noqa: E402

MAP = 'fan_FootmanFrenzyXV484'
SECONDS = 240
SETUP_END = 80                      # the four dialog timeouts end at 76.4 s, the filter at 78.4 s (wait grid)
RALLY = 851980                      # "setrally"
HERO = jass.rawcode('Hpal')         # W9R3-1: one of the map's XV hero types (Primary STR, gold 1050, bldtm 55)


def tag(humans=2, revive=False, upgrades=False):
    return '%s_%s%d_%ds' % (MAP, 'revive' if revive else ('upg' if upgrades else 'duel'), humans, SECONDS)


# W9R3-5: the same duel for the other Footman Frenzy files. A map entry: how the army reaches the enemy.
#   'rally'  XV 4.84: the footmen are trained by the barracks (Trains empty, Revive=1) and walk to its rally point.
#   'army'   v6.7 (8821): the map creates its footmen at the start location by script (CreateNUnitsAtLoc) with no order and no
#            rally input [obs]: units grow 82 -> 322 and stand still. Each human answers the setup dialogs, then sends all of
#            its soldiers on attack-move to the opponent's barracks every `gap` s (O_ATTACK, input kind 1).
MAPS = {
    MAP: {'mode': 'rally', 'setup_end': SETUP_END, 'seconds': SECONDS},
    'fan_8821FootmanFrenzyv67': {'mode': 'army', 'setup_end': 30, 'seconds': 300, 'gap': 20},
}


class DuelPlay(SoakPlayer):
    """one human of the duel (the profile controller of the sweep, tools/jvm/w9_prof_pvp.py; `slots` = the human slots in
    play order, this controller plays slots[idx]). Installs itself on the VM's on_turn like every SoakPlayer."""

    def __init__(self, N, idx=0, slots=None, mode='rally', setup_end=SETUP_END, gap=20):
        slots = list(slots if slots is not None else (N.slot_order or range(12))[:2])
        SoakPlayer.__init__(self, N, None, None, 0.0, player=slots[idx])
        self.slots, self.mode, self.setup_end, self.gap = slots, mode, setup_end, gap
        self.enemy = slots[(idx + 1) % len(slots)]
        self.done = False
        self.next = None
        self.sent = 0

    def barracks(self, p):
        return next((u for u in self.N.units if u.owner.id == p and u.alive and not u.dead and u.data.get('id4') == 'hbar'), None)

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if self.mode == 'army' and self.click_dialogs(now):
            return
        if now < self.setup_end * SPS:
            return
        mine, theirs = self.barracks(self.player), self.barracks(self.enemy)
        if mine is None or theirs is None:
            return
        if self.mode == 'rally':
            if not self.done:
                self.done = True
                self.eng.post_input(self.player, 1, mine.serial, RALLY, theirs.x, theirs.y)
                self.log.append((now * 5, 'rally'))
            return
        if self.next is not None and now < self.next:
            return
        self.next = now + self.gap * SPS
        n = 0
        for u in self.own():
            if not u.is_bldg:
                self.eng.post_input(self.player, 1, u.serial, O_ATTACK, theirs.x, theirs.y)
                n += 1
        self.sent += n
        self.log.append((now * 5, 'attack-move %d units' % n))


UPG_AT = (90,)                        # BUGS FF-UPG: when both players queue level 1 (7 s each; levels 2 / 3 need hkee / hcas)
UPG_IDS = ('Rhme', 'Rhar')


def scenario(humans=2, record=None, key=MAP, slot_order=None, revive=False, upgrades=False):
    """-> (nat, machine, lines, report dict). key: the map (MAPS); slot_order: the user slots in the order humans take them
    (default: the map's w3i order, BUGS FF-SLOTS); a key with mode 'army' is played by DuelPlay controllers."""
    prog, machine, nat, lines = R.build(key, humans=humans)
    eng = nat.eng
    rv = {'REVIVE_START': 0, 'REVIVE_FINISH': 0, 'REVIVE_CANCEL': 0, 'fired_finish': 0, 'fires': 0}
    if revive:
        orig_he, orig_fire = nat.hero_event, nat.vm.fire

        def hero_event(u, suffix, data, slots=None):
            f0 = rv['fires']
            r = orig_he(u, suffix, data, slots)
            if suffix in rv:
                rv[suffix] += 1
                if suffix == 'REVIVE_FINISH':
                    rv['fired_finish'] += rv['fires'] - f0
                    rv['alive_at_finish'] = u.alive and not u.dead and u.life > 0
                    rv['pos_at_finish'] = (round(u.x), round(u.y))
            return r

        def fire(*a, **k):
            rv['fires'] += 1
            return orig_fire(*a, **k)
        nat.hero_event, nat.vm.fire = hero_event, fire
    if slot_order is not None:
        nat.slot_order = list(slot_order)
    cfg = MAPS[key]
    seconds = cfg['seconds']
    if cfg['mode'] == 'army':
        slots = list((nat.slot_order or range(12))[:humans])
        if record is not None:
            orig_ = eng.post_input

            def post_input_(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
                record.append((machine.turn, player, kind, unit_serial, oid, float(x), float(y), target_serial))
                return orig_(player, kind, unit_serial, oid, x, y, target_serial)
            eng.post_input = post_input_
        ctl = [DuelPlay(nat, i, slots, cfg['mode'], cfg['setup_end'], cfg.get('gap', 20)) for i in range(len(slots))]
        R.start(machine, nat)
        machine.run_until(seconds)
        idx = lambda p: nat.players.index(p)       # noqa: E731
        return nat, machine, lines, {
            'slots': slots, 'rejected': eng.stats['inputs_rejected'], 'kills': dict(eng.stats['kills']),
            'errors': list(machine.errors), 'attacks': eng.stats['attacks'], 'sent': sum(c.sent for c in ctl),
            'soldiers': {p: sum(1 for u in nat.units if u.alive and idx(u.owner) == p and not u.is_bldg) for p in slots}}
    if record is not None:
        orig = eng.post_input

        def post_input(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
            record.append((machine.turn, player, kind, unit_serial, oid, float(x), float(y), target_serial))
            return orig(player, kind, unit_serial, oid, x, y, target_serial)
        eng.post_input = post_input
    R.start(machine, nat)
    pid = lambda p: nat.players.index(p)       # noqa: E731
    machine.run_until(5)
    early = {pid(u.owner) for u in nat.units if u.alive}
    machine.run_until(SETUP_END)
    bars = {pid(u.owner): u for u in nat.units if u.alive and u.data.get('id4') == 'hbar' and pid(u.owner) < 12}
    slots = sorted(bars)
    # every human sets the rally point of his barracks on the next human's barracks
    for i, p in enumerate(slots):
        o = bars[slots[(i + 1) % len(slots)]]
        eng.post_input(p, 1, bars[p].serial, RALLY, o.x, o.y)
    hero_log = []
    upg_log = []
    if upgrades:
        for t in UPG_AT:                                                          # level 1 of two researches at the barracks
            machine.run_until(t)
            for p in slots:
                if t == UPG_AT[0]:
                    eng.post_input(p, 3, 0, 5000)                                  # test gold
                for rid in UPG_IDS:
                    eng.post_input(p, 0, bars[p].serial, jass.rawcode(rid))
            machine.run_until(t + 0.5)
            upg_log.append(sum(len(bars[p].tq) for p in slots))
    if revive:
        b0 = bars[slots[0]]
        machine.run_until(100)
        eng.post_input(slots[0], 10, 0, HERO, b0.x + 300.0, b0.y)                 # spawn the hero
        machine.run_until(101)
        hero = next((u for u in nat.units if u.alive and u.tid == HERO and pid(u.owner) == slots[0]), None)
        machine.run_until(120)
        if hero is not None and not hero.dead:
            eng.post_input(slots[0], 11, hero.serial, 0)                          # the scripted kill
        machine.run_until(125)
        hero_log.append(hero is not None and hero.dead)
        eng.post_input(slots[0], 0, b0.serial, HERO)                              # revive at the Tier Zero barracks
        machine.run_until(126)
        hero_log.append(len(b0.tq))
    machine.run_until(seconds)
    rep = {'early_owners': sorted(early), 'slots': slots, 'rejected': eng.stats['inputs_rejected'],
           'kills': dict(eng.stats['kills']), 'errors': list(machine.errors),
           'neutral_units': sum(1 for u in nat.units if u.alive and pid(u.owner) >= 12),
           'footmen': {p: sum(1 for u in nat.units if u.alive and pid(u.owner) == p and u.data.get('id4') == 'hfoo')
                       for p in slots},
           'reach_x': {p: (max if bars[p].x < 0 else min)(u.x for u in nat.units
                                                          if u.alive and pid(u.owner) == p and u.data.get('id4') == 'hfoo')
                       for p in slots},
           'pause_off': [ln for ln in lines if 'PauseGame(false)' in ln],
           'filter_off': [ln for ln in lines if 'DisplayCineFilter(false)' in ln],
           'bars': {p: (round(b.x), round(b.y)) for p, b in bars.items()}}
    if upgrades:
        rep.update({'upg_queued': upg_log,
                    'tech': {p: {r: nat.players[p].tech.get(jass.rawcode(r), 0) for r in UPG_IDS} for p in slots},
                    'dice': {p: sorted({u.upg_dice for u in nat.units if u.alive and pid(u.owner) == p and u.data.get('id4') == 'hfoo'})
                             for p in slots},
                    'armor': {p: sorted({u.armor_bonus for u in nat.units if u.alive and pid(u.owner) == p and u.data.get('id4') == 'hfoo'})
                              for p in slots}})
    if revive:
        rep.update({'revive': rv, 'hero_dead_at_125': hero_log[0], 'queue_at_126': hero_log[1],
                    'hero_regs': sum(len(p.unit_regs.get(nat.ev_id('EVENT_PLAYER_HERO_REVIVE_FINISH'), ())) for p in nat.players)})
    return nat, machine, lines, rep


def clicked_setup(humans=2):
    """player 0 answers the four setup dialogs (6v6 is not chosen: the first button each time) as soon as they show:
    the chain ends within a few seconds instead of the 76.4 s of timeouts. -> (shown dialogs, pause off t, filter off t, rejected)."""
    import re
    prog, machine, nat, lines = R.build(MAP, humans=humans)
    R.start(machine, nat)
    seen, shown = 0, []
    for t in range(1, 12):
        machine.run_until(t)
        for ln in lines[seen:]:
            m = re.match(r't=(\d+) thread=\d+ DialogDisplay\(h\w+, h(\w+), true\)', ln)
            if m:
                d = int(m.group(2), 16)
                shown.append(len(nat.o(d).buttons))
                nat.eng.post_input(0, 4, d, 0)
        seen = len(lines)
    pick = lambda key: [int(ln.split()[0][2:]) for ln in lines if key in ln]       # noqa: E731
    return shown, pick('PauseGame(false)'), pick('DisplayCineFilter(false)'), nat.eng.stats['inputs_rejected']


def check(verbose=True, export=None):
    fails = []

    def ok(c, what):
        if verbose:
            print('  %s %s' % ('ok  ' if c else 'FAIL', what))
        if not c:
            fails.append(what)
    rec = []
    nat, machine, lines, rep = scenario(2, rec)
    if verbose:
        print({k: v for k, v in rep.items() if k not in ('pause_off', 'filter_off')})
    ok(rep['slots'] == [0, 3], '2 players take slots 0 and 3, different teams (BUGS FF-SLOTS): %r' % rep['slots'])
    ok(not rep['errors'], 'no VM errors / unknown unit types (the map\'s own Units SLKs, BUGS FF-SLK): %r' % rep['errors'][:3])
    ok(set(rep['early_owners']) == {0, 3}, 'only the two players own units at 5 s (no army for the neutral four, '
                                           'BUGS FF-ALL): %r' % rep['early_owners'])
    ok(rep['neutral_units'] == 0, 'no unit of player 12..15 at %d s' % SECONDS)
    # the timeouts are 40 + 10 + 15 + 10 s = 75 s from t = 0.6 s; each wait resumes d + 0.1 .. 0.2 s later on the 0.1 s grid (docs/oracle.md,
    # TriggerSleepAction(d) [obs runtime 1.26a]; the model: next grid point strictly after now + d + 0.1), so four waits add ~0.8 s: 76.4 s
    # (the old d + one 25 ms turn model gave 75.5 s; W3_WAIT_GRID=0 restores it)
    off = (76400, 78400) if V.WAIT_GRID else (75500, 77500)
    ok(len(rep['pause_off']) == 1 and rep['pause_off'][0].startswith('t=%d ' % off[0]),
       'the game is unpaused at 76.4 s, after the four dialog timeouts (waits run under PauseGame, BUGS FF-PAUSE): %r' % rep['pause_off'])
    ok(len(rep['filter_off']) == 1 and rep['filter_off'][0].startswith('t=%d ' % off[1]),
       'the black cinematic filter ends at 78.4 s: %r' % rep['filter_off'])
    ok(rep['rejected'] == 0, 'both rally orders accepted (the barracks has Revive=1, BUGS FF-RALLY)')
    ok(all(rep['reach_x'][p] * (1 if rep['bars'][p][0] < 0 else -1) > -1500 for p in rep['slots']),
       'the footmen left the base for the enemy: %r' % rep['reach_x'])
    ok(rep['kills'].get(0, 0) > 0 and rep['kills'].get(3, 0) > 0,
       'the two players\' units fight each other (kills by both): %r' % rep['kills'])
    shown, pause_off, filter_off, rej = clicked_setup()
    ok(shown == [2, 3, 4, 2] and rej == 0 and pause_off and pause_off[0] <= 5000 and filter_off and filter_off[0] <= 7000,
       'answering the four dialogs (clicks, input kind 4) ends the setup at once: buttons %r, unpaused at %r ms, filter off at %r ms'
       % (shown, pause_off, filter_off))
    if export:
        t = tag(2)
        with open(os.path.join(export, t + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
        with open(os.path.join(export, t + '.inputs'), 'w') as fh:
            for r in rec:
                fh.write('%d %d %d %d %d %r %r %d\n' % r)
        if verbose:
            print('  wrote %s.{trace,inputs} (%d lines, %d inputs)' % (os.path.join(export, t), len(lines), len(rec)))
    return fails


def check_revive(verbose=True, export=None):
    """W9R3-1: the duel plus a hero that is killed and revived at the barracks (docs/NOTES_production.md section 1.4)."""
    fails = []

    def ok(c, what):
        if verbose:
            print('  %s %s' % ('ok  ' if c else 'FAIL', what))
        if not c:
            fails.append(what)
    rec = []
    nat, machine, lines, rep = scenario(2, rec, revive=True)
    rv = rep['revive']
    if verbose:
        print({k: v for k, v in rep.items() if k in ('revive', 'hero_dead_at_125', 'queue_at_126', 'hero_regs',
                                                     'rejected', 'errors')})
    ok(not rep['errors'], 'no VM errors with the hero revive and the map\'s own REVIVABLE / REVIVE_FINISH triggers: %r'
       % rep['errors'][:3])
    ok(rep['hero_dead_at_125'] is True, 'the scripted kill (input kind 11) killed the hero before 125 s')
    # PORT-Hero: the map's own revive orders (0xd003b…0xd0047 with the dead hero as the target) are handled now
    # (CAbilityRevive §4/§8.21), so the hero is already queued when the scripted type-order at 125 s arrives: that
    # duplicate is refused and the queue still holds exactly one entry.
    ok(rep['queue_at_126'] == 1 and rep['rejected'] <= 1,
       'the revive at the Tier Zero barracks (Revive=1, empty Trains) is queued once (the map’s target order; the scripted duplicate refused): queue %r, rejected %r'
       % (rep['queue_at_126'], rep['rejected']))
    ok(rv['REVIVE_START'] >= 1, 'EVENT_PLAYER_HERO_REVIVE_START raised: %r' % rv['REVIVE_START'])
    ok(rv['REVIVE_FINISH'] >= 1, 'EVENT_PLAYER_HERO_REVIVE_FINISH raised >= 1: %r' % rv['REVIVE_FINISH'])
    ok(rv['fired_finish'] >= 1 and rep['hero_regs'] >= 12,
       'the map\'s REVIVE_FINISH triggers (%d registrations) fire: %d runs for the hero' % (rep['hero_regs'], rv['fired_finish']))
    at = rv.get('pos_at_finish')
    ok(rv.get('alive_at_finish') and at is not None and abs(at[0] - rep['bars'][rep['slots'][0]][0]) < 600
       and abs(at[1] - rep['bars'][rep['slots'][0]][1]) < 600,
       'the hero is alive at REVIVE_FINISH, next to the barracks (then it follows the rally order): %r' % (at,))
    if export:
        t = tag(2, True)
        with open(os.path.join(export, t + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
        with open(os.path.join(export, t + '.inputs'), 'w') as fh:
            for r in rec:
                fh.write('%d %d %d %d %d %r %r %d\n' % r)
        if verbose:
            print('  wrote %s.{trace,inputs} (%d lines, %d inputs)' % (os.path.join(export, t), len(lines), len(rec)))
    return fails


def check_upgrades(verbose=True, export=None):
    """BUGS FF-UPG: the duel with four levels of Iron Forged Weapons + Plating researched at the Tier Zero barracks."""
    fails = []

    def ok(c, what):
        if verbose:
            print('  %s %s' % ('ok  ' if c else 'FAIL', what))
        if not c:
            fails.append(what)
    rec = []
    nat, machine, lines, rep = scenario(2, rec, upgrades=True)
    if verbose:
        print({k: v for k, v in rep.items() if k in ('upg_queued', 'tech', 'dice', 'armor', 'rejected', 'errors', 'kills')})
    ok(not rep['errors'], 'no VM errors: %r' % rep['errors'][:3])
    ok(rep['rejected'] == 0 and all(q == 4 for q in rep['upg_queued']),
       'every research order is accepted (the players\' two researches queued): queued %r, rejected %r'
       % (rep['upg_queued'], rep['rejected']))
    ok(all(t == {'Rhme': 1, 'Rhar': 1} for t in rep['tech'].values()), 'both researches reached level 1: %r' % rep['tech'])
    ok(all(d == [1] for d in rep['dice'].values()), 'every living footman carries the extra die of Iron Forged Weapons (ratd 1): %r' % rep['dice'])
    ok(all(a == [2.0] for a in rep['armor'].values()), 'and the armor bonus of Plating (defUp 2 x 1 level): %r' % rep['armor'])
    ok(rep['kills'].get(0, 0) > 0 and rep['kills'].get(3, 0) > 0, 'the armies still fight: %r' % rep['kills'])
    if export:
        t = tag(2, upgrades=True)
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
    if '--upgrades' in sys.argv:
        fails = check_upgrades(True, export)
        print('ff_duel --upgrades: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
        sys.exit(1 if fails else 0)
    if '--revive' in sys.argv:
        fails = check_revive(True, export)
        print('ff_duel --revive: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
        sys.exit(1 if fails else 0)
    fails = check(True, export)
    print('ff_duel: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
