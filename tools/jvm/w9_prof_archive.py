"""W9R2-4 play profiles of the archive tower defences (maps of input/fanmaps_wc3search, see docs/map_audit.md section 8).

Floors are 80 % (rounded up) of the first accepted probe run: generic `w9_play.Play`, 1 human, 300 s, seed 1, trace off
[obs]. They are never lowered to make a failing run pass; a map that cannot meet its floors (or whose player 0 is defeated, or whose level never rises, or whose lives end at 0) is an `xfail` entry
(BUGS.md id + the first blocking observation in `XFAIL_NOTES`), never a silently weaker assert.

Level / wave / lives globals (read from the map's own war3map.j, [ext]):
  2070 WH40k TD 3.45          udg_Level (0 -> 3 in 300 s), udg_Lives (50 -> 29)
  6455 Green TD Revised a0.98 udg_Chances = lives (50 -> 32), udg_killsred = kills counter (0 -> 62); the wave number has no
                              scalar global (udg_level[] is an array of creep rawcodes, not a counter)
  8471 Naruto TD v2.9         udg_pointer = level (stays 0: the next round starts when player 12's food is 0, i.e. after the
                              wave of 25 is dead), udg_lives = lives (20, drops on a leak)
"""
import math

from . import w9_play as W
from .engine import SPS

PROFILES = {}
XFAIL_NOTES = {}      # BUGS id -> first blocking observation (test_w9_archive checks the BUGS.md heading exists)


def A(name, ok, detail=''):
    return (name, bool(ok), detail)


def top(series, default=None):
    v = [x for x in series if x is not None]
    return max(v) if v else default


def floor80(v):
    return int(math.ceil(0.8 * v))


def trained(rep):
    """units the controller ordered trained (log entries 'train ...' / 'hero ...'; the engine's `built` counts structures)."""
    return sum(1 for _t, what in rep['log'] if what.startswith(('train', 'hero')))


def td_asserts(towers, kills, level=None, lives=None, units=None, rise=True):
    """the criteria of docs/w9_criteria.md section 0 for a builder TD: floors are passed in (from the probe run).
    towers None = no builder in this map; units = floor of trained units instead (Castle Defense)."""
    def asserts(rep):
        out = [A('rejected/inputs <= 20 %', rep['rejected'] <= 0.2 * max(1, rep['inputs']), '%d/%d' % (rep['rejected'], rep['inputs']))]
        if towers is not None:
            out.append(A('towers_built >= %d' % towers, rep['towers_built'] >= towers, rep['towers_built']))
        if units is not None:
            out.append(A('units trained >= %d' % units, trained(rep) >= units, trained(rep)))
        out += [
            A('kills_by_player[0] >= %d' % kills, rep['kills'].get(0, 0) >= kills, rep['kills'].get(0, 0)),
            A('attacks > 0', rep['attacks'] > 0, rep['attacks']),
        ]
        if level:
            g, floor = level
            ser = [x for x in rep['series'].get(g, [])]
            v = top(ser)
            out.append(A('%s >= %d' % (g, floor), v is not None and v >= floor, '%s max %s' % (g, v)))
            # the floor alone can be met by the map's own setup (Random TD: level 1 at second 1): the level must also rise
            # above its value at 5 s, unless the profile says the map does not let it rise (rise=False, xfail instead)
            if rise:
                v5 = ser[min(4, len(ser) - 1)] if ser else None
                out.append(A('%s rises after 5 s' % g, v is not None and v5 is not None and v > v5, '%s at 5 s %s, max %s' % (g, v5, v)))
        if lives:
            g = lives
            s = [x for x in rep['series'].get(g, []) if x is not None]
            out.append(A('%s found, end > 0' % g, bool(s) and s[-1] > 0, '%s -> %s' % (s[0], s[-1]) if s else None))
        out.append(A('player 0 not defeated', rep['nat'].players[0].slot_state != 2, 'slot_state %s' % rep['nat'].players[0].slot_state))
        return out
    return asserts


# ---------------------------------------------------------------------------------------------------- the 3 that play
# probe: 2134 inputs (0 rejected), 6 towers hC07, kills[0] 40, attacks 117, missiles 105
PROFILES['fan_2070WH40kTD345'] = dict(
    key='fan_2070WH40kTD345', humans=1, seconds=300, make=lambda N: W.Play(N),
    report_globals=('udg_Level', 'udg_Lives'),
    asserts=td_asserts(floor80(6), floor80(40), level=('udg_Level', 3), lives='udg_Lives'))

# probe: 51 inputs, 23 towers n005, kills[0] 62, attacks 990, missiles 964
PROFILES['fan_6455GreenTDReviseda098'] = dict(
    key='fan_6455GreenTDReviseda098', humans=1, seconds=300, make=lambda N: W.Play(N),
    report_globals=('udg_Chances', 'udg_killsred'),
    asserts=td_asserts(floor80(23), floor80(62), level=('udg_killsred', floor80(62)), lives='udg_Chances'))

# probe: 6 inputs, 5 towers oC33 / hC01, kills[0] 24, attacks 93
PROFILES['fan_8471NarutoTDv29'] = dict(
    key='fan_8471NarutoTDv29', humans=1, seconds=300, make=lambda N: W.Play(N),
    report_globals=('udg_pointer', 'udg_lives'),
    asserts=td_asserts(floor80(5), floor80(24), lives='udg_lives'))


# ---------------------------------------------------------------------------------------------------- stalled maps
from .w9_prof_builders import ElementPlay, progress_assert      # noqa: E402
from .autoplay import SoakPlayer as SoakTowers  # noqa: E402

class ChooserPlay(ElementPlay):
    """X6.2M: the Element Chooser's Builds list also holds one 1500 gold weapon tower (hC63 Laser Cannon), so X10's
    'the player owns no weapon builder yet' test never opens; here the element phase is simply 'no element chosen yet'."""

    def on_turn(self, turn):
        if self.ax is not None and self.elem is None:
            for b in self.own():
                if b.is_bldg or not b.data.get('Builds') or b.build is not None or b.order != 0:
                    continue
                e = self.element_type(b)
                if e is None:
                    continue
                (g, lu, _k), tid, d = e
                st = b.owner.state
                if st.get(1, 0) >= g and st.get(2, 0) >= lu:
                    p = self.spot(tid, b)
                    if p is not None:
                        self.elem = tid
                        self.eng.post_input(self.player, 1, b.serial, tid, p[0], p[1])
                        self.log.append((self.eng.vm.steps * 5, 'element %s' % d.get('id4')))
                        return
                break
        W.Play.on_turn(self, turn)


class StartZonePlay(W.Play):
    """Element TD 2.5: the builder is created in the join zone (rect (2912,416)-(4512,1600), [obs] from the script) and only
    a builder still inside that zone when the 20 s vote timer ends is moved to the player's start location (arena with
    the creeps' loop; SetUnitPositionLoc to GetPlayerStartLocationLoc). The generic player walked out of the zone at
    0.4 s to build at the anchor, so its towers stood 6000 units from the loop (0 attacks): wait, then anchor."""
    wait_ms = 22000

    def on_turn(self, turn):
        if self.eng.vm.steps * 5 < self.wait_ms:
            return
        W.Play.on_turn(self, turn)


class GroundPlay(W.Play):
    """Random TD 2.0: the generic choice 'most expensive affordable tower' picked the 50 gold Anti Air Tower h01A (targets
    air only, targs1 16386: 3 towers, 0 attacks on the ground creeps), and the generic 'train the strongest Trains entry'
    bought `n010 Risk` 270 times, the map's gamble that doubles or loses ALL gold (ends at 0 gold, 'Sorry, you've lost
    all your money!'). Here: towers must hit ground (targs1 bit 0), the gamble type is never trained (not just capped).
    W9R3-3: the towers stand at the lane's end (see lane_x) and cheap towers come before upgrades."""
    gamble = ('Risk',)
    # W9R3-3 [obs creep trajectories 46-66 s + ext rects Area3SpawnOben2 / DeathzoneArea3]: the creeps of the player's area run the
    # straight line x = -8470, from the north (y -6900, Oben2) and from the south (y -17950, Rechts2 / Links1) to the Deathzone
    # (-8464, -12944); the generic anchor (the builder's start, 2000 units west of it) left the 19 towers out of range (39 attacks,
    # wave 1 leaks in full). The towers go next to the Deathzone, where both streams end, with the lane itself left open.
    lane_x, lane_y, lane_gap = -8470.0, (-12944.0, -12944.0), 192.0

    def lane_spot(self, tid, b):
        ay = min(max(b.y, self.lane_y[0]), self.lane_y[1])
        for r in (4, 8, 16, 24, 32):
            if r in self.full:
                continue
            cand = sorted(((i * i + j * j), j, i) for j in range(-r, r + 1) for i in range(-r, r + 1))
            for _d, j, i in cand:
                x, y = self.lane_x + 64.0 * i, ay + 64.0 * j
                if abs(x - self.lane_x) < self.lane_gap:       # the lane itself stays open (creeps walk, not fight)
                    continue
                ok, cx, cy = self.eng.can_place(b.owner, tid, x, y, b)
                if ok and self.tries.get((cx, cy), 0) < 4:
                    self.tries[(cx, cy)] = self.tries.get((cx, cy), 0) + 1
                    return cx, cy
            self.full.add(r)
        return None

    def spot(self, tid, b):
        return self.lane_spot(tid, b)

    upgrade_from = 30

    def upgrade(self, units, gold, now):
        # h02A (10 gold) is 61 dps, its upgrade h02E (60 gold) 142 dps: while towers are cheap, gold buys more of them
        if sum(1 for u in units if u.is_bldg and u.fighter) < self.upgrade_from:
            return False
        return SoakTowers.upgrade(self, units, gold, now)

    def towers(self, b):
        t = SoakTowers.towers(self, b)
        t = [(g, tid) for g, tid in t if int(self.N.unit_types[tid].get('targs1', 1) or 0) & 1 or
             int(self.N.unit_types[tid].get('targs2', 0) or 0) & 1]
        # the generic loop takes the LAST affordable entry: order by damage per second per gold, best last
        return sorted(t, key=lambda e: (self.dps(e[1]) / max(1, e[0]), e[1]))

    def dps(self, tid):
        d = self.N.unit_types[tid]
        dmg = float(d.get('dmgplus1', 0) or 0) + float(d.get('dice1', 1) or 1) * (float(d.get('sides1', 1) or 1) + 1) / 2
        return dmg / max(0.1, float(d.get('cool1', 1) or 1))

    def opts(self, b, key, gold, want):
        o = SoakTowers.opts(self, b, key, gold, want)
        return [(g, tid) for g, tid in o if str(self.N.unit_types[tid].get('name') or '') not in self.gamble]


class TrainPlay(W.Play):
    """Castle Defense: no builder at all; the player owns production buildings (Barracks hC02, hero shops ...) and trains
    the defenders. The soak player's phases (train 'other', heroes) only need an anchor, so the first own unit is it."""

    def on_turn(self, turn):
        if self.ax is None:
            own = self.own()
            if own and self.eng.vm.steps * 5 >= 1000:
                self.ax, self.ay = own[0].x, own[0].y
        W.Play.on_turn(self, turn)


class RacePickPlay(W.Play):
    """Ryoko TD: the player starts with a 'Magical Gateway' (n001) that SELLS the race builders (e002 Water, u000 ...: types
    with a Builds list; shop input kind 5, jvm/ltw_bot.py). Buying one makes the map create the Command Center and the
    builder; without the pick the lanes leak and the player is defeated at 111 s with 0 inputs."""
    picked = False
    walks = 0
    walk_next = 0
    SHOP_REACH = 300.0            # a buyer farther from the shop than this is walked next to it first (Aneu reach, shops.E_NO_ACTIVE)

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if not self.picked and now * 5 >= 10000:
            from .orders import ORDER_IDS
            for u in self.own():
                if getattr(u, 'shop', None):
                    for e in self.eng.shops.listing(u, self.player):
                        d = self.N.unit_types.get(e[1]) if e[0] == 1 else None
                        if d is None or not d.get('Builds') or e[4] <= 0:
                            continue
                        if e[6] == 0:
                            self.picked = True
                            self.eng.post_input(self.player, 5, u.serial, e[1])
                            self.log.append((now * 5, 'pick %s' % d.get('id4')))
                            return
                        if e[6] == 0x40 and self.walks < 12 and now >= self.walk_next:
                            # W9R3-3: code 64 = no own unit within the shop's reach: walk the nearest mobile unit next to it
                            near = sorted(((v.x - u.x) ** 2 + (v.y - u.y) ** 2, v.serial, v) for v in self.own()
                                          if not v.is_bldg and self.eng.can_move(v))
                            if near and near[0][0] > self.SHOP_REACH ** 2:
                                self.walks += 1
                                self.walk_next = now + SPS       # one walk order per second
                                self.eng.post_input(self.player, 1, near[0][2].serial, ORDER_IDS['move'], u.x, u.y)
                                self.log.append((now * 5, 'walk buyer to shop'))
                                return
        W.Play.on_turn(self, turn)


class PairPlay(W.Play):
    """a second scripted human next to player 0 (maps that end at once with a single human, e.g. Line Tower Wars Remix:
    'Red has won the game' at 20 s): player 1 plays with the same rules."""

    send_from, send_gap = 81, 12      # game seconds, as jvm/ltw_bot.py
    sent = 0

    def __init__(self, N, **kw):
        super().__init__(N, **kw)
        ids = list(N.slot_order or range(12))[:2]       # the human slots (Wintermaul Wars: 0 and 3, not 0 and 1)
        self.ids = ids
        self.other = W.Play(N, player=ids[1])
        self.next_send = self.send_from * 200

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if now >= self.next_send:
            self.next_send = now + self.send_gap * 200
            for pid in self.ids:
                self.send(pid, now)
        super().on_turn(turn)

    max_towers = 8                    # the income only comes from sending: stop building when 8 stand, keep the gold for units

    def towers(self, b):
        if sum(1 for u in self.own() if u.is_bldg and u.fighter) >= self.max_towers:
            return []
        return super().towers(b)

    def send(self, pid, now):
        """Line Tower Wars family: the player's shops (Sellunits) sell the units that walk the OTHER player's lane
        (input kind 5, jvm/ltw_bot.py): buy the most expensive affordable one."""
        gold = self.N.players[pid].state.get(1, 0)
        best = None
        for u in self.N.units:
            if u.owner.id == pid and u.alive and not u.dead and getattr(u, 'shop', None):
                for e in self.eng.shops.listing(u, pid):
                    if e[0] == 1 and e[6] == 0 and e[4] > 0 and e[2] <= gold and (best is None or e[2] > best[2]):
                        best = (u, e[1], e[2])
        if best is not None:
            self.eng.post_input(pid, 5, best[0].serial, best[1])
            self.sent += 1
            if pid == self.player:
                self.log.append((now * 5, 'send %s' % best[1]))


def ryoko_asserts(rep):
    """W9R3-3: the base asserts + the race builder was bought at the gateway (log 'pick') + the level is held at the end."""
    out = td_asserts(floor80(25), floor80(91), level=('bmx89ZTA', 4))(rep)
    lv = [x for x in rep['series'].get('bmx89ZTA', []) if x is not None]
    out.append(A('race builder bought at the gateway', any(w.startswith('pick ') for _t, w in rep['log']),
                 [w for _t, w in rep['log'] if w.startswith('pick ')][:1]))
    out.append(A('bmx89ZTA >= 2 held to the end', bool(lv) and lv[-1] >= 2, 'bmx89ZTA at the end %s' % (lv[-1] if lv else None)))
    return out


PROFILES['fan_3520RyokoTDversion24Full'] = dict(
    key='fan_3520RyokoTDversion24Full', humans=1, seconds=300, make=lambda N: RacePickPlay(N, bonus=150),
    # obfuscated script: bmx89ZTA = level (timer title "Level n Time"), axe92UPA[lane] = lives (the leak text 'n Lives remaining'; the
    # lane index is aqy68AEA[player], not read here). The gateway n001 sells the race builders; the pick works at 10 s (the buy
    # code is 0; a code 64 = no own unit in the Aneu reach would walk the nearest mobile unit to the shop first).
    # W9R2-4 probe (bonus 0): 257 inputs, 7 towers, kills[0] 33, attacks 337, bmx89ZTA 1 -> 2 (107 s), but DEFEATED at 245 s
    # (RemovePlayer(slot 0) + CustomDefeatRestartBJ / ReduceDifficultyBJ / LoadBJ): the start gold (100 + 10) buys 5 towers and the
    # kills pay too little to follow wave 2 (16 lives lost in 6 s at 231 s). W9R3-3: bonus 150 (test-only gold, input kind 3, every
    # 30 s of game time, as DBZ / ETD): 311 inputs (1 rejected), 25 towers, kills[0] 91, attacks 617, bmx89ZTA 1 -> 4, not defeated.
    report_globals=('bmx89ZTA',),
    asserts=ryoko_asserts)

class ElementPair(PairPlay, ChooserPlay):
    """Wintermaul Wars: element chooser (as X6.2M) + the second human + the shrine's attackers."""


PROFILES['fan_49416WintermaulWarsCrystal14'] = dict(
    key='fan_49416WintermaulWarsCrystal14', humans=2, seconds=300, make=lambda N: ElementPair(N, bonus=0),
    # probe (2 humans = slots 0 and 3; with 1 human the map ends at 9 s: GAMEOVER dialog): 79 inputs, 9 towers (element building + 8),
    # kills[0] 17 (+ 16 by slot 3), attacks 301; udg_LivesF1 / udg_LivesF2 = lives of the two teams (10 -> 9 / 10 -> 11); the map has no
    # level scalar (udg_Next_Level is a timer)
    report_globals=('udg_LivesF1', 'udg_LivesF2'),
    asserts=td_asserts(floor80(9), floor80(17), lives='udg_LivesF1'))

PROFILES['fan_9299LineTowerWarsRemixBetaUDK'] = dict(
    key='fan_9299LineTowerWarsRemixBetaUDK', humans=2, seconds=300, make=lambda N: PairPlay(N, bonus=0),
    # probe (2 humans: both players build 8 archer towers h003, then buy the dearest affordable unit every 12 s from 81 s):
    # 50 inputs, towers[0] 8, kills[0] 15 (+ 24 by player 2), attacks 241; udg_Income[1] 10 -> 30 (no lives global: the lives are
    # a player state; 'You have stolen 1 life from Player N' texts). The 1 human game ends at 20 s ('Red has won the game').
    report_globals=('udg_Income[1]', 'udg_Income[2]'),
    asserts=td_asserts(floor80(8), floor80(15), level=('udg_Income[1]', 30)))

PROFILES['fan_6545CastleDefense78finallocked'] = dict(
    key='fan_6545CastleDefense78finallocked', humans=1, seconds=300, make=lambda N: TrainPlay(N, bonus=0),
    # probe: 44 inputs (1 rejected), kills[0] 10 (+ 87 by player 10, 20 by 9: the waves fight each other too), attacks 3635;
    # udg_level 0 -> 2 (udg_levelwait = the level timer, udg_Bounty a flag); no lives global
    report_globals=('udg_level', 'udg_levelwait'),
    asserts=td_asserts(None, floor80(10), level=('udg_level', 2), units=floor80(8)))   # 8 trainings in the probe

def rtd_asserts(rep):
    """W9R3-3: the base asserts + the controller's two rules, checked on the result: every tower of player 0 can hit ground units
    (h01A is air-only) and the gamble type is never trained."""
    out = td_asserts(floor80(70), floor80(587), level=('udg_Level_Number', 3), lives='udg_Lives')(rep)
    lv = [x for x in rep['series'].get('udg_Lives', []) if x is not None]
    dead = rep['nat'].players[0].slot_state == 2
    out.append(A('defeat comes with the last life (slot state 2 iff udg_Lives reached 0)', dead == (bool(lv) and min(lv) <= 0),
                 'min lives %s, slot_state %s' % (min(lv) if lv else None, rep['nat'].players[0].slot_state)))
    nat = rep['nat']
    bad = sorted(set(u.data.get('id4') for u in nat.units if u.owner.id == 0 and u.is_bldg and u.fighter and
                     not (int(u.data.get('targs1', 0) or 0) & 1 or int(u.data.get('targs2', 0) or 0) & 1)))
    out.append(A('attacks >= %d' % floor80(1521), rep['attacks'] >= floor80(1521), rep['attacks']))
    out.append(A('no air-only tower, no gamble train', not bad and not any(w.startswith('train n010') for _t, w in rep['log']),
                 'air-only towers %s' % bad))
    return out


PROFILES['fan_5735RandomTD20'] = dict(
    key='fan_5735RandomTD20', humans=1, seconds=300, make=lambda N: GroundPlay(N, bonus=0),
    # W9R2-4 probe: 19 towers (h02A) at the builder's start, kills[0] 20, attacks 39, level 1 flat. W9R3-3 [obs creep trajectories]:
    # the creeps of the player's area walk the straight line x = -8470 from the north (y -6900) and from the south (y -17950) to the
    # Deathzone (-8464, -12944); the 19 towers stood 2000 units west of it. With the towers at the Deathzone (GroundPlay.lane_spot):
    # 121 inputs, 70 towers, kills[0] 587, attacks 1521, udg_Level_Number 1 -> 3. Still xfail W9R2-4-RTD (lives / defeated): wave 1 is
    # 200 creeps (9 spawn rects for the 9 players of the shared area, 1 human here); the builder places one tower per ~3 s (build
    # time 2 s + walking), 15-20 stand at 62 s and 10 lives (Random mode, seed 1; 50 with Normal + Easy) are gone at 66-73 s.
    report_globals=('udg_Level_Number', 'udg_Lives', 'udg_Lives_Lost'),
    asserts=rtd_asserts,
    xfail={'udg_Lives found, end > 0': 'W9R2-4-RTD', 'player 0 not defeated': 'W9R2-4-RTD'})

PROFILES['fan_8044ElementTD25'] = dict(
    key='fan_8044ElementTD25', humans=1, seconds=300, make=lambda N: StartZonePlay(N, bonus=0),
    # obfuscated script: O19790 = level (leaderboard label "Level "+I2S(O19790)), O6181[player-1] = lives (leaderboard value)
    # probe: 19 towers, kills[0] 97, attacks 837, O19790 0 -> 5, O6181[0] 50 (no leak)
    report_globals=('O19790', 'O6181[0]'),
    asserts=td_asserts(floor80(19), floor80(97), level=('O19790', 5), lives='O6181[0]'))

PROFILES['fan_1259WintermaulX62M'] = dict(
    key='fan_1259WintermaulX62M', humans=1, seconds=300, make=lambda N: ChooserPlay(N, bonus=0),
    report_globals=('udg_Level_Number', 'udg_Lives', 'udg_Lives_Lost'),
    # probe 9 towers (the element building + 8), kills[0] 60, attacks 149; udg_Level_Number 0 -> 1 at 2 s and flat for 300 s (the map's own
    # setup, no wave counter rises: xfail W9R2-4-X62M), udg_Lives 20 -> 40 -> 29, not defeated
    # W9R3-3 progress measure instead of the level: 61 deaths of owner 11 + 11 lives lost (udg_Lives_Lost) in 300 s, floor 58 = 80 %
    asserts=lambda rep: td_asserts(floor80(9), floor80(60), level=('udg_Level_Number', 1), lives='udg_Lives')(rep) +
    [progress_assert(58)(rep)],
    xfail={'udg_Level_Number rises after 5 s': 'W9R2-4-X62M'})


XFAIL_NOTES['W9R2-4-RTD'] = ('wave 1 is 220 creeps (h00B Dog, 100 hp, speed 350) from 11 spawn points; 16-17 towers stand when it arrives (50-65 s) and kill ~110 of them, the other ~110 reach the Deathzone: '
                             'Trig_Lives (one life per creep, RemoveUnit) -> CustomDefeatBJ at 0, player 0 removed at ~66-73 s. The lives / defeat rules themselves work (test_w9_rtd_lives.py): '
                             'a scenario property (BUGS W9-PYR-RTD), not an engine gap')
XFAIL_NOTES['W9R2-4-X62M'] = 'udg_Level_Number 0 -> 1 at 2 s (map setup), then flat for 300 s: no wave counter rises in the profile window'

# Tech Wars 1.9d (W9R2-4-TECH, kills 0 / attacks 0 with the pair controller of this file) moved to w9_prof_pvp.py (RtsPlay, W9R3-5):
# no entry here, so the registry has one source for that key.

