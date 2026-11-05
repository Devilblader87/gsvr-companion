"""W9R3-5 play profiles of the player-versus-player maps and the controller `RtsPlay` (see w9_play.py for the interface).

  fan_FootmanFrenzyXV484      2 humans (slots 0 and 3), 240 s: tools/jvm/ff_duel.py as a keyed profile (rally on the enemy barracks)
  fan_8821FootmanFrenzyv67    2 humans, 300 s: slots 0 and 9 (the map shows its setup dialogs to Player(9) [obs]), attack-move waves
  fan_linetowerwars7v114      2 humans (slot 0 scripted, slot 1 the bot of tools/jvm/bot.py), 900 s
  fan_8402TechWars19d         2 humans, 900 s (the 1500 s record is the slow test): RtsPlay, base-vs-base RTS

Floors come from the first accepted run and are never lowered to make a failing run pass (BUGS W9R3-5). Clean room: values
(unit ids, build lists, costs) are read from the map's own unit data at run time.
"""
import re

import jass

from . import w9_play as W
from . import ff_duel as FF
from . import ltw_bot as LB
from . import bot as B
from .engine import SPS, O_ATTACK, _f
from .w9_prof_archive import PairPlay

PROFILES = {}


def A(name, ok, detail=''):
    return (name, bool(ok), detail)


def rejected_ok(rep, frac=0.2):
    return A('rejected/inputs <= 20 %', rep['rejected'] <= frac * max(1, rep['inputs']), '%d/%d' % (rep['rejected'], rep['inputs']))


def clean_text(t):
    return re.sub(r'\|[cC][0-9a-fA-F]{8}|\|[rR]', '', t)


# ================================================================================================ Footman Frenzy (duels)
def make_duel(key, slot_order=None):
    cfg = FF.MAPS[key]

    def make(N):
        if slot_order is not None:
            N.slot_order = list(slot_order)         # before R.start: the humans take these user slots
        slots = list((N.slot_order or range(12))[:2])
        return [FF.DuelPlay(N, i, slots, cfg['mode'], cfg['setup_end'], cfg.get('gap', 20)) for i in range(2)]
    return make


def duel_asserts(chain_end=None):
    def asserts(rep):
        slots = list(rep['nat'].slot_order or range(12))[:2]
        k = rep['kills']
        out = [A('kills by both sides >= 1', all(k.get(p, 0) >= 1 for p in slots), {p: k.get(p, 0) for p in slots}),
               A('attacks > 0', rep['attacks'] > 0, rep['attacks']),
               rejected_ok(rep)]
        if chain_end is not None:
            off = [int(ln.split()[0][2:]) for ln in rep['lines'] if 'DisplayCineFilter(false)' in ln]
            out.append(A('setup chain ends <= %d s' % chain_end, bool(off) and off[0] <= chain_end * 1000, off[:1]))
        return out
    return asserts


PROFILES[FF.MAP] = dict(
    key=FF.MAP, humans=2, seconds=FF.SECONDS, make=make_duel(FF.MAP), report_globals=(),
    # ff_duel.py: slots 0 and 3, setup dialogs left to their defaults (chain ends 77.5 s), rally of both barracks on the enemy barracks
    asserts=duel_asserts(chain_end=80))

# v6.7: the host dialogs go to Player(9) (DialogDisplayBJ(.., Player(9)), unconditionally); with the default human slots 0 and 3
# nobody answers and the map idles (82 units forever, 0 attacks); slots 0 and 9 answer, the map then spawns its footmen by script
# (no rally exists: CreateNUnitsAtLoc) and the controller sends them on attack-move to the enemy barracks.
FF67 = 'fan_8821FootmanFrenzyv67'
PROFILES[FF67] = dict(
    key=FF67, humans=2, seconds=FF.MAPS[FF67]['seconds'], make=make_duel(FF67, slot_order=(0, 9)), report_globals=(),
    asserts=duel_asserts())


# ================================================================================================ Line Tower Wars 7
LTW7 = LB.MAP


class LtwPlay(W.Play):
    """Line Tower Wars 7: slot 0 is the scripted human (8 towers, then it sends the dearest affordable unit every 12 s from 81 s,
    as PairPlay of the archive profiles / ltw_bot.py), slot 1 is the bot of tools/jvm/bot.py (docs/bot.md). The controller also
    samples, once per second, the path of its own units (for the 'sends reach the other lane' assert)."""

    send_from, send_gap, max_towers = LB.SEND_FROM, LB.SEND_GAP, 8
    send = PairPlay.send
    sent = 0

    def opts(self, b, key, gold, want):
        """no unit training: the tower buildings' Trains entries (h011 ...) are refused by the engine and the lane is played by
        sending, so the generic 'train the strongest affordable' phase would only add rejected orders."""
        return [] if want == 'other' else W.Play.opts(self, b, key, gold, want)

    def towers(self, b):
        if sum(1 for u in self.own() if u.is_bldg and u.fighter) >= self.max_towers:
            return []                  # the income only comes from sending: keep the gold for units
        return W.Play.towers(self, b)

    def __init__(self, N, **kw):
        W.Play.__init__(self, N, **kw)
        self.bots = B.attach(N, [1])
        self.next_send = self.send_from * SPS
        self.tracks, self.tick = {}, 0

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if now >= self.next_send:
            self.next_send = now + self.send_gap * SPS
            self.send(self.player, now)
        if now >= self.tick:
            self.tick = now + SPS
            for u in self.N.units:
                if u.owner.id == self.player and u.alive and not u.dead and not u.is_bldg:
                    tr = self.tracks.setdefault(u.serial, [])
                    if not tr or tr[-1] != (u.x, u.y):
                        tr.append((u.x, u.y))
        W.Play.on_turn(self, turn)


# ================================================================================================ RtsPlay (Tech Wars, later LOTR / AWS)
def weapon(d):
    return any(int(d.get('weapsOn', 0) or 0) >> i & 1 for i in (0, 1))


class RtsPlay(W.Play):
    """Controller of a base-versus-base RTS (W9R3-5). One decision per turn, in this order:
      1. every `attack_gap` s all own soldiers (units with a weapon and no Builds list) get an attack-move (O_ATTACK) to the
         nearest enemy structure (distance from the first soldier, enemy = not allied, owner a player slot < 12);
      2. the main building (a bldg whose Trains list has a unit with Builds) trains builders up to `max_builders`;
      3. an idle builder builds the next production building: a type of its Builds list without a weapon (a tower is no
         production) that it may build now (tech_ok, req_ok, gold), the type it owns fewest of first (list order = the
         map's order: Tech Wars hC11 Rifleman Barracks, h00N, hC49, h00L ...), at most `max_each` of one type;
      4. a finished building with a Trains list and an empty queue trains the strongest affordable soldier until the food cap
         (food_ok); a finished production building with an Upgrade list is upgraded to its cheapest allowed variant when
         nothing else is affordable.
    All ids, costs and lists come from the map's unit data at run time. A soldier that the map spawns itself (Tech Wars: the
    production buildings create their units by script every 10 to 15 s, Trains lists are empty [obs]) needs steps 1 and 3 only."""

    def __init__(self, N, player=0, max_builders=2, max_each=1, attack_gap=20, upgrade=True, max_towers=10, tower_ratio=2, **kw):
        W.Play.__init__(self, N, player=player, **kw)
        self.max_builders, self.max_each, self.attack_gap, self.max_towers = max_builders, max_each, attack_gap, max_towers
        self.upgrade_prod = upgrade
        self.tower_ratio = tower_ratio
        self.wanted = False
        self.next_attack = None
        self.waves = 0
        self.radius = 768.0

    def trained_ids(self, u):
        return [jass.rawcode(t.strip()) for t in str(u.data.get('Trains', '') or '').split(',') if len(t.strip()) == 4]

    def is_soldier(self, u):
        return not u.is_bldg and not u.data.get('Builds') and weapon(u.data)

    def target(self, ref):
        best = None
        for u in self.N.units:
            if u.is_bldg and u.alive and not u.dead and u.owner.id < 12 and not self.eng.ally(self.N.players[self.player], u.owner):
                d = (u.x - ref.x) ** 2 + (u.y - ref.y) ** 2
                if best is None or (d, u.serial) < (best[0], best[1].serial):
                    best = (d, u)
        return best[1] if best else None

    def on_turn(self, turn):
        now = self.eng.vm.steps
        units = self.own()
        if not units:
            return
        if self.ax is None:
            b0 = next((u for u in units if u.is_bldg), units[0])
            self.ax, self.ay = b0.x, b0.y
        if self.click_dialogs(now):
            return
        st = units[0].owner.state
        gold, lumber = st.get(1, 0), st.get(2, 0)
        if self.next_attack is None:
            self.next_attack = now + self.attack_gap * SPS
        if now >= self.next_attack:
            self.next_attack = now + self.attack_gap * SPS
            army = [u for u in units if self.is_soldier(u)]
            tgt = self.target(army[0]) if army else None
            if tgt is not None:
                for u in army:
                    self.eng.post_input(self.player, 1, u.serial, O_ATTACK, tgt.x, tgt.y)
                self.waves += 1
                self.log.append((now * 5, 'attack-move %d units -> %s' % (len(army), tgt.data.get('id4'))))
                return
        done = [u for u in units if u.is_bldg and not u.constructing]
        builders = [u for u in units if not u.is_bldg and u.data.get('Builds')]
        # 2. more builders
        if len(builders) < self.max_builders:
            for h in done:
                if h.tq:
                    continue
                for g, tid in self.opts(h, 'Trains', gold, 'builder'):
                    d = self.N.unit_types[tid]
                    if self.eng.food_ok(h.owner, int(d.get('fused', 0) or 0)):
                        self.eng.post_input(self.player, 0, h.serial, tid)
                        self.log.append((now * 5, 'train builder %s' % d.get('id4')))
                        return
        # 3. production buildings and the defence towers
        have = {}
        for u in units:
            if u.is_bldg:
                have[u.tid] = have.get(u.tid, 0) + 1

        def family(tid, seen=None):
            seen = seen if seen is not None else set()
            if tid not in seen:
                seen.add(tid)
                for t in self.eng.upgrade_list(tid):
                    family(t, seen)
            return seen
        towers = sum(1 for u in units if u.is_bldg and u.fighter)
        prod = sum(1 for u in units if u.is_bldg and not weapon(u.data) and not any(
            (self.N.unit_types.get(t) or {}).get('Builds') for t in self.trained_ids(u)))
        self.wanted = False
        for b in builders:
            if b.build is not None or b.order != 0 or b.paused:
                continue
            cand, wanted = [], 0
            for k, s in enumerate(str(b.data.get('Builds', '')).split(',')):
                s = s.strip()
                d = self.N.unit_types.get(jass.rawcode(s)) if len(s) == 4 else None
                if d is None or not d.get('bldg') or weapon(d):
                    continue
                tid = jass.rawcode(s)
                if sum(have.get(t, 0) for t in family(tid)) >= self.max_each:
                    continue
                if not (self.eng.tech_ok(b.owner, tid) and self.eng.req_ok(b.owner, tid)):
                    continue
                wanted += 1                    # a production building we still want: save the gold for it
                if int(d.get('gold', 0) or 0) > gold or int(d.get('lumber', 0) or 0) > lumber:
                    continue
                cand.append((k, tid))
            self.wanted = self.wanted or bool(wanted)
            cand.sort()
            # a base needs defenders (the map's armies walk to the enemy bases): `tower_ratio` towers per production building
            if (not wanted or towers < min(self.max_towers, self.tower_ratio * prod)) and towers < self.max_towers:
                for g, tid in self.towers(b):
                    d = self.N.unit_types[tid]
                    if g <= gold and int(d.get('lumber', 0) or 0) <= lumber and weapon(d) and \
                            self.eng.tech_ok(b.owner, tid) and self.eng.req_ok(b.owner, tid):
                        cand.insert(0, (-1, tid))
                        break
            for _k, tid in cand:
                p = self.spot(tid, b)
                if p is not None:
                    self.eng.post_input(self.player, 1, b.serial, tid, p[0], p[1])
                    self.log.append((now * 5, 'build %s' % self.N.unit_types[tid].get('id4')))
                    return
        # 4. train soldiers / upgrade production
        for h in done:
            if h.tq or not h.data.get('Trains'):
                continue
            o = [(g, t) for g, t in self.opts(h, 'Trains', gold, 'other')
                 if self.eng.food_ok(h.owner, int(self.N.unit_types[t].get('fused', 0) or 0))]
            if o:
                self.eng.post_input(self.player, 0, h.serial, o[-1][1])
                self.log.append((now * 5, 'train %s' % self.N.unit_types[o[-1][1]].get('id4')))
                return
        if self.upgrade_prod and not self.wanted:
            best = None
            for h in done:
                if h.upg is not None or h.tq or h.data.get('Trains') or not h.data.get('Upgrade') or weapon(h.data):
                    continue
                for t in self.eng.upgrade_list(h.tid):
                    d = self.N.unit_types.get(t)
                    if d is None or not d.get('bldg') or have.get(t, 0) >= self.max_each:
                        continue
                    dg = int(d.get('gold', 0) or 0)
                    if not _f(self.eng.misc, 'RelativeUpgradeCost', 0.0):
                        dg -= int(h.data.get('gold', 0) or 0)         # the difference of the cumulative costs (melee data)
                    if dg <= gold and self.eng.tech_ok(h.owner, t) and self.eng.req_ok(h.owner, t):
                        if best is None or (dg, h.serial, t) < best[:3]:
                            best = (dg, h.serial, t, h)
            if best is not None:
                self.eng.post_input(self.player, 0, best[3].serial, best[2])
                self.log.append((now * 5, 'upgrade %s -> %s' % (best[3].data.get('id4'), self.N.unit_types[best[2]].get('id4'))))


# Known limit [obs, W9R3-5 review]: with max_each=1, max_towers=10 and no upgrade in reach this controller saturates early (inputs stay
# at 55, 3 rejected, in both the 900 s and the 1500 s run) and gold piles up unused (3100 at 1500 s). The 1500 s record therefore shows
# no extra play. Before reusing it for LOTR / AWS let it spend (raise max_each, allow upgrades when gold builds up).


class RtsPair(RtsPlay):
    """two humans on the slots of the map (Tech Wars: 0 and 3, opposite teams): the second human plays the same rules."""

    def __init__(self, N, **kw):
        ids = list(N.slot_order or range(12))[:2]
        RtsPlay.__init__(self, N, player=ids[0], **kw)
        self.other = RtsPlay(N, player=ids[1], **kw)


def texts(nat):
    return [(x[0] / 1000.0, clean_text(x[3])) for x in nat.last_text if x[1] == 'text']


def ltw_asserts(rep):
    nat, ap = rep['nat'], rep['nat'].autoplayer
    s1, s2 = rep['series']['udg_Lives[1]'], rep['series']['udg_Lives[2]']
    tx = texts(nat)
    lost0 = sum(1 for _t, x in tx if x == 'Player 2 stole a life from you')      # shown to slot 0
    lost1 = sum(1 for _t, x in tx if x == 'Player 1 stole a life from you')      # shown to slot 1
    start = s1[0] if s1 and s1[0] is not None else 25
    conserved = all(a + b == 2 * start for a, b in zip(s1, s2) if a is not None and b is not None)
    best, nseen = 0.0, 0
    for tr in ap.tracks.values():
        ln, dy, n_ = LB.detour(tr)
        if n_ >= 5:
            nseen += 1
            best = max(best, ln - dy)
    won = [t for t, x in tx if 'won the game' in x]
    last = (s1[-1], s2[-1]) if s1 else (None, None)
    ended = bool(won) or 0 in last
    return [
        rejected_ok(rep),
        A('towers_built[0] >= 8', rep['built'].get(0, 0) >= 8, rep['built'].get(0, 0)),
        A('bot towers built[1] >= 8', rep['built'].get(1, 0) >= 8, rep['built'].get(1, 0)),
        A('kills_by_player[0] >= 1', rep['kills'].get(0, 0) >= 1, rep['kills'].get(0, 0)),
        A('sends reach the other lane', nseen >= 2 and best > 400.0, '%d tracked, longest detour %.0f' % (nseen, best)),
        A('a leak costs exactly one life', conserved and (lost0 + lost1) > 0 and last[0] == start - lost0 + lost1,
          'lives %s, stolen from slot 0: %d, from slot 1: %d, sum kept: %s' % (last, lost0, lost1, conserved)),
        A('a side eliminated or the game ended by %d s' % rep['seconds'], ended, 'lives %s, end texts %s' % (last, won[:1])),
    ]


def make_ltw(N):
    return LtwPlay(N, bonus=0)


PROFILES[LTW7] = dict(
    key=LTW7, humans=2, seconds=900, make=make_ltw, report_globals=('udg_Lives[1]', 'udg_Lives[2]'), asserts=ltw_asserts)

# ================================================================================================ Tech Wars 1.9d
TECH = 'fan_8402TechWars19d'


def tech_asserts(rep):
    nat = rep['nat']
    prod = sum(1 for u in nat.units if u.owner.id == 0 and u.alive and u.is_bldg and not weapon(u.data) and not u.data.get('Trains'))
    return [
        A('kills_by_player[0] >= 1', rep['kills'].get(0, 0) >= 1, rep['kills'].get(0, 0)),
        A('attacks > 0', rep['attacks'] > 0, rep['attacks']),
        rejected_ok(rep),
        A('production buildings (player 0) >= 3', prod >= 3, prod),
        A('buildings finished (player 0, all types) >= 9', rep['towers_built'] >= 9, rep['towers_built']),
    ]


PROFILES[TECH] = dict(key=TECH, humans=2, seconds=900, make=lambda N: RtsPair(N), report_globals=(), asserts=tech_asserts)


# ================================================================================================ export for the U# harness
def export_ltw(out, seconds=None):
    """<out>/fan_linetowerwars7v114_play_<s>s.{trace,inputs}: like w9_play.export, but the inputs file holds only the scripted
    human's events (slot 0): the bot's inputs come from the U# bot itself (W3_MAPS "...*2b2@900=trace,inputs", docs/bot.md)."""
    import os
    prof = PROFILES[LTW7]
    rep = W.play(prof, seconds)
    tag = '%s_play_%ds' % (LTW7, rep['seconds'])
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, tag + '.trace'), 'w', encoding='utf-8', newline='') as fh:
        fh.write('\n'.join(rep['lines']) + '\n')
    mine = [r for r in rep['rec'] if r[1] == 0]
    with open(os.path.join(out, tag + '.inputs'), 'w') as fh:
        for r in mine:
            fh.write('%d %d %d %d %d %r %r %d\n' % r)
    print('exported %s.{trace,inputs} -> %s (%d human inputs of %d, %d trace lines)' % (tag, out, len(mine), len(rep['rec']), len(rep['lines'])))


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--export-ltw' in argv:
        export_ltw(argv[argv.index('--export-ltw') + 1], int(argv[argv.index('--seconds') + 1]) if '--seconds' in argv else None)
        return 0
    print('usage: python3 -m jvm.w9_prof_pvp --export-ltw DIR [--seconds N]   (the other keys: python3 -m jvm.w9_play KEY --export DIR)')
    return 2


if __name__ == '__main__':
    import sys
    sys.exit(main())
