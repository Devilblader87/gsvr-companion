"""W9R3-6 genre profiles of the non-TD archive maps: escape, race, tag and survival
(docs/map_audit.md section 8 "Genre profiles (W9R3-6)", docs/w9_criteria.md section 5).

Clean room: every goal / progress name below was read from the map's own war3map.j ([ext]) or measured in a run ([obs]);
no map text is copied. The controllers are new (nothing is taken from w9_prof_hero.py); they only use the soak player's
`own()` and the engine's public input path (`post_input`), like a human.

  RunnerPlay    p0's mobile units walk to the next goal rect (checkpoint / finish / unit box) along the pathing grid (pathing.py)
                and are re-ordered when blocked, killed or teleported; keys (items) are picked up and carried to their lock
                rects; the progress is observed from the map's globals by the profile asserts.
  TagPlay       (World of Tree Tag) the tree leaves the camping zone, builds resource storages and flees from hunters.
  SurvivalPlay  (Dawn of the Dead) the Soul picks a hero (RunnerPlay into the Select rect), the hero attack-moves at the waves.

Race (Azeroth Grand Prix) has no controller of its own: the carts are driven by arrow-key events the port cannot receive
(BUGS W9R3-6-AGP), so its profile uses the generic player and xfails the checkpoint assert. Pyramid Escape uses RunnerPlay
up to the closed door (BUGS W9R3-6-PYR).

Floors are 80 % (rounded up) of the first accepted run, never lowered. A map that cannot meet its asserts is an `xfail`
entry (BUGS.md id + the first blocking observation in XFAIL_NOTES).
"""
import math

from . import w9_play as W
from . import natives as NT
from .autoplay import SoakPlayer

PROFILES = {}
XFAIL_NOTES = {}

SPS = 200
ORD_MOVE = 851986
ORD_ATTACK = 851983         # 'attack' = attack-move for a point target
ORD_SMART = 851971


def A(name, ok, detail=''):
    return (name, bool(ok), detail)


def floor80(v):
    return int(math.ceil(0.8 * v))


def rect_of(m, name):
    """a rect global of the map -> (minx, miny, maxx, maxy) or None."""
    if name not in m.p.global_by_name:
        return None
    h = m.global_value(name)
    r = m.obj(h, NT.Rect) if h else None
    return (r.minx, r.miny, r.maxx, r.maxy) if r is not None else None


class RunnerPlay(SoakPlayer):
    """walk p0's mobile unit(s) along the goal rects in order (rect globals `goals`, 'gg_rct_...'; a rect counts as
    reached when a runner stands inside it). One decision per game second and runner:
      * idle, or the runner moved < `stuck` units since the last decision: re-plan with Grid.find_path from its
        position to the goal centre and order a move to the first waypoint (so a blocked straight line becomes a walk
        around the obstacle; the engine's own A* limit is not relied on);
      * a runner that was teleported (map checkpoint) gets a new plan at once;
    `types` limits the runners to unit type ids (id4 strings), `attack` issues attack-move instead of move."""

    def __init__(self, N, goals, types=None, attack=False, stuck=24.0, period=1.0, player=0, goal_slack=0.0,
                 lead=1, items=(), locks=(), pick_range=6000.0):
        SoakPlayer.__init__(self, N, None, None, 1280.0, player=player, bonus=0, early=False)
        self.goals = list(goals)
        self.types = set(types) if types else None
        self.attack = attack
        self.stuck = stuck
        self.period = int(period * SPS)
        self.slack = goal_slack
        self.lead = lead
        self.items = set(items)       # item type id4s that unlock something: picked up when a path reaches them
        self.locks = list(locks)      # rect globals a runner holding such an item has to enter (gates, levers)
        self.opened = set()           # lock rects whose gate opened (the carried key was used up while inside)
        self.visit = {}               # lock rect -> frozenset of the carried item serials at its last unsuccessful visit
        self.pick_range = pick_range
        self.picked = 0               # pick-up orders given
        self.keys_seen = set()        # serials of the unlocking items a runner has carried
        self.gi = 0                   # index of the first goal rect not yet reached
        self.reached = []             # (game second, goal name)
        self.last = {}                # serial -> (step, x, y, order step)
        self.orders = 0
        self.replans = 0
        self.next = 0

    def rects(self, i):
        """the rect(s) of goal i: a rect global ('gg_rct_..'), ('unit', id4, half) = a box around every live unit of that
        type (the nearest one is walked to), ('xy', x, y, half)."""
        gd = self.goals[i]
        if isinstance(gd, str):
            r = rect_of(self.eng.vm, gd)
            return [r] if r is not None else []
        if gd[0] == 'xy':
            return [(gd[1] - gd[3], gd[2] - gd[3], gd[1] + gd[3], gd[2] + gd[3])]
        h = gd[2]
        return [(u.x - h, u.y - h, u.x + h, u.y + h) for u in self.N.units
                if u.alive and not u.dead and str(u.data.get('id4')) == gd[1]]

    def gname(self, i):
        gd = self.goals[i]
        return gd if isinstance(gd, str) else '%s:%s' % (gd[0], gd[1])

    def runners(self):
        out = []
        for u in self.own():
            if u.is_bldg or not self.eng.can_move(u):
                continue
            if self.types is not None and str(u.data.get('id4')) not in self.types:
                continue
            out.append(u)
        return out

    def inside(self, u, r):
        s = self.slack
        return r[0] - s <= u.x <= r[2] + s and r[1] - s <= u.y <= r[3] + s

    def on_turn(self, turn):
        now = self.eng.vm.steps
        run = self.runners()
        # goal bookkeeping: a runner inside the current goal rect advances it (and every later goal it is also in)
        while self.gi < len(self.goals):
            rs = self.rects(self.gi)
            if not rs and isinstance(self.goals[self.gi], str):
                self.gi += 1                   # a rect the map does not define
                continue
            if any(self.inside(u, r) for u in run for r in rs):
                self.reached.append((now // SPS, self.gname(self.gi)))
                self.gi += 1
                continue
            break
        if self.items:
            for u in run:
                for it in self.carried(u):
                    self.keys_seen.add(it.serial)
        for name in self.locks:
            r = rect_of(self.eng.vm, name)
            if r is None or name in self.opened:
                continue
            for u in run:
                if self.inside(u, r):
                    held = frozenset(it.serial for it in u.items if it is not None and str(it.data.get('id4')) in self.items)
                    prev = self.visit.get(name)
                    if prev is not None and len(held) < len(prev):
                        self.opened.add(name)           # a key was used up here
                    self.visit[name] = held if prev is None or not held <= prev else prev
        if now < self.next:
            return
        self.next = now + self.period
        if self.gi >= len(self.goals) and not self.items:
            return
        g = self.eng.grid
        for u in run:
            prev = self.last.get(u.serial)
            moved = prev is None or math.hypot(u.x - prev[1], u.y - prev[2]) >= self.stuck
            if not (u.order == 0 or not moved):
                self.last[u.serial] = (now, u.x, u.y, prev[3] if prev else now)
                continue
            tx, ty, tgt = self.target(u, g)
            if tx is None:
                continue
            if tgt is None and g is not None and prev is not None and not moved and not self.reachable(g, u, tx, ty):
                # the goal cannot be reached from here (a closed gate, a missing key): stand and look again every 10 s
                # instead of ordering the same move every second
                if now - prev[3] < 10 * SPS:
                    continue
            if tgt is not None:
                self.eng.post_input(self.player, 2, u.serial, ORD_SMART, 0.0, 0.0, tgt.serial)
                self.picked += 1
            else:
                if g is not None:
                    pts = g.find_route(u.x, u.y, tx, ty)
                    if pts:
                        tx, ty = pts[min(len(pts) - 1, self.lead - 1)]
                    self.replans += 1
                self.eng.post_input(self.player, 1, u.serial, ORD_ATTACK if self.attack else ORD_MOVE, tx, ty)
            self.orders += 1
            self.last[u.serial] = (now, u.x, u.y, now)

    def carried(self, u):
        return [it for it in u.items if it is not None and str(it.data.get('id4')) in self.items]

    def holds(self, u):
        return bool(self.carried(u))

    def reachable(self, g, u, x, y):
        if g is None:
            return True
        pts = g.find_route(u.x, u.y, x, y)
        return bool(pts) and g.last_reached

    def target(self, u, g):
        """(x, y, item) the runner goes to next: an unlocking item still lying around, then the lock rects (not yet
        opened and not visited with the same keys), then the goal rect."""
        if self.items:
            best = None
            for it in self.eng.itm.ground:
                if str(it.data.get('id4')) not in self.items or not it.live:
                    continue
                d = math.hypot(it.x - u.x, it.y - u.y)
                if d <= self.pick_range and (best is None or d < best[0]) and self.reachable(g, u, it.x, it.y):
                    best = (d, it)
            if best is not None and len(self.carried(u)) < 5:
                return best[1].x, best[1].y, best[1]
            held = frozenset(it.serial for it in self.carried(u))
            best = None
            for name in self.locks if held else ():
                r = rect_of(self.eng.vm, name)
                if r is None or name in self.opened:
                    continue
                prev = self.visit.get(name)
                if prev is not None and held <= prev:
                    continue                                # tried with these keys already
                cx, cy = (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
                d = math.hypot(cx - u.x, cy - u.y)
                if (best is None or d < best[0]) and self.reachable(g, u, cx, cy):
                    best = (d, cx, cy, name)
            if best is not None:
                return best[1], best[2], None
        if self.gi < len(self.goals):
            best = None
            for r in self.rects(self.gi):
                cx, cy = (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
                d = math.hypot(cx - u.x, cy - u.y)
                if best is None or d < best[0]:
                    best = (d, cx, cy)
            if best is not None:
                return best[1], best[2], None
        return None, None, None


class DoorPlay(RunnerPlay):
    """RunnerPlay that first breaks a destructable door: every runner gets an 'attack' target order (input kind 2, the destructable's
    serial) on the nearest living destructable of type `door` (id4) until none is left (BUGS W9-PYR-DEST); then the goals as RunnerPlay."""

    def __init__(self, N, goals, door, **kw):
        RunnerPlay.__init__(self, N, goals, **kw)
        self.door = door
        self.obj = None
        self.broke = None            # game second the door was dead
        self.door_orders = 0

    def living_door(self, u):
        """the door: the nearest destructable of type `door` to the first runner, chosen once (the map has several of that type)."""
        if self.obj is None:
            best = None
            for o in self.eng.dests:
                if o.id4 == self.door and o.dalive and o.life > 0 and not getattr(o, 'removed', False):
                    d = math.hypot(o.x - u.x, o.y - u.y)
                    if best is None or d < best[0]:
                        best = (d, o)
            self.obj = best[1] if best else None
        o = self.obj
        return o if o is not None and o.dalive and o.life > 0 and not getattr(o, 'removed', False) else None

    def on_turn(self, turn):
        if self.broke is None:
            now = self.eng.vm.steps
            run = self.runners()
            door = self.living_door(run[0]) if run else None
            if door is None:
                if run:
                    self.broke = now // SPS
                    self.reached.append((self.broke, 'dest:%s' % self.door))
            elif now >= self.next:
                self.next = now + self.period
                for u in run:
                    if u.order == 0 or u.gpt is None:
                        self.eng.post_input(self.player, 2, u.serial, ORD_ATTACK, 0.0, 0.0, door.dser)
                        self.door_orders += 1
                return
            else:
                return
        RunnerPlay.on_turn(self, turn)


class TagPlay(RunnerPlay):
    """World of Tree Tag (tree team): the Ent walks out of the camping zone (the map kills a structure that starts inside
    it and punishes camping there) and builds resource storages (`build`, the type that raises the map's income
    counters ResourceGainGold / ResourceGainWood on CONSTRUCT_FINISH) on free ground near it."""

    def __init__(self, N, build='eC09', zone='gg_rct_Inner_Camping_Zone', margin=420.0, **kw):
        RunnerPlay.__init__(self, N, goals=(), **kw)
        self.build = build
        self.zone = zone
        self.margin = margin
        self.built = 0
        self.tid = None
        self.spot_i = 0

    def ent(self):
        for u in self.own():
            if not u.is_bldg and u.data.get('Builds'):
                return u
        return None

    flee = 1500.0
    flee_to = 900.0

    def flee_from_hunters(self, b):
        """the tag rule: walk away from the nearest enemy fighter within `flee` units (a tree dies to one hit);
        True when a flee order was given (the Ent stops building meanwhile)."""
        best = None
        for u in self.N.units:
            if u.is_bldg or not u.alive or u.dead or u.hidden or not u.fighter or self.eng.ally(b.owner, u.owner):
                continue
            d = math.hypot(u.x - b.x, u.y - b.y)
            if d < self.flee and (best is None or d < best[0]):
                best = (d, u)
        if best is None:
            return False
        d, u = best
        d = max(d, 1.0)
        g = self.eng.grid
        for k in range(8):                       # the direction away from it, then turned in 45 degree steps
            ang = math.atan2(b.y - u.y, b.x - u.x) + (k // 2 + 0.0) * (math.pi / 4) * (1 if k % 2 == 0 else -1)
            tx, ty = b.x + self.flee_to * math.cos(ang), b.y + self.flee_to * math.sin(ang)
            if g is not None:
                pts = g.find_route(b.x, b.y, tx, ty)
                if not pts or not g.last_reached:
                    continue
                tx, ty = pts[0]
            self.eng.post_input(self.player, 1, b.serial, ORD_MOVE, tx, ty)
            self.orders += 1
            self.fled = getattr(self, 'fled', 0) + 1
            return True
        return False

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if now < self.next:
            return
        self.next = now + self.period
        b = self.ent()
        if b is None:
            return
        import jass
        if self.tid is None:
            self.tid = jass.rawcode(self.build)
        z = rect_of(self.eng.vm, self.zone) or (-320.0, -320.0, 320.0, 320.0)
        out = (z[0] - self.margin, z[1] - self.margin, z[2] + self.margin, z[3] + self.margin)
        outside = not (out[0] <= b.x <= out[2] and out[1] <= b.y <= out[3])
        if self.flee_from_hunters(b):
            return
        if b.build is not None or b.order not in (0, ORD_MOVE):
            return
        if not outside:
            # walk to the nearest point outside the margin box
            tx, ty = b.x, b.y
            if abs(b.x - (out[0] + out[2]) / 2) / (out[2] - out[0]) >= abs(b.y - (out[1] + out[3]) / 2) / (out[3] - out[1]):
                tx = out[2] + 128.0 if b.x >= (out[0] + out[2]) / 2 else out[0] - 128.0
            else:
                ty = out[3] + 128.0 if b.y >= (out[1] + out[3]) / 2 else out[1] - 128.0
            self.eng.post_input(self.player, 1, b.serial, ORD_MOVE, tx, ty)
            self.orders += 1
            return
        d = self.N.unit_types[self.tid]
        st = b.owner.state
        if st.get(1, 0) < int(d.get('gold', 0) or 0) or st.get(2, 0) < int(d.get('lumber', 0) or 0):
            return
        for k in range(24):
            ang = (self.spot_i + k) * 2.399963
            r = 192.0 + 24.0 * ((self.spot_i + k) % 7)
            ok, cx, cy = self.eng.can_place(b.owner, self.tid, b.x + r * math.cos(ang), b.y + r * math.sin(ang), b)
            if ok:
                self.spot_i += k + 1
                self.eng.post_input(self.player, 1, b.serial, self.tid, cx, cy)
                self.orders += 1
                self.built += 1
                return


class SurvivalPlay(RunnerPlay):
    """Dawn of the Dead style waves: the player first walks its starting unit into the pick rect `goals` (RunnerPlay), then
    its hero(es) / fighters hold the zone around `home` and attack-move at the nearest enemy fighter that comes within
    `reach` of them (re-ordered every `period` seconds, or at once when idle), and return home when none is left."""

    def __init__(self, N, goals, reach=1800.0, home=None, **kw):
        kw.setdefault('attack', False)
        RunnerPlay.__init__(self, N, goals=goals, **kw)
        self.reach = reach
        self.home = home
        self.fights = 0
        self.fight_next = 0

    def fighters(self):
        return [u for u in self.own() if not u.is_bldg and u.fighter and self.eng.can_move(u) and not u.hidden]

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if self.gi < len(self.goals) and any(u.hero for u in self.own()):
            self.reached.append((now // SPS, 'hero'))      # the pick turned the starting unit into a hero elsewhere
            self.gi = len(self.goals)
        RunnerPlay.on_turn(self, turn)
        if self.gi < len(self.goals) or now < self.fight_next:
            return
        self.fight_next = now + self.period
        fs = self.fighters()
        if not fs:
            return
        if self.home is None:
            self.home = (fs[0].x, fs[0].y)
        for u in fs:
            best = None
            for e in self.N.units:
                if e.is_bldg or not e.alive or e.dead or e.hidden or e.locust or self.eng.ally(u.owner, e.owner):
                    continue
                if e.owner.id >= 12 and not e.fighter:
                    continue
                d = math.hypot(e.x - u.x, e.y - u.y)
                if d <= self.reach and (best is None or d < best[0]):
                    best = (d, e)
            if best is not None:
                self.eng.post_input(self.player, 1, u.serial, ORD_ATTACK, best[1].x, best[1].y)
                self.fights += 1
            elif u.order == 0 and math.hypot(u.x - self.home[0], u.y - self.home[1]) > 300.0:
                self.eng.post_input(self.player, 1, u.serial, ORD_MOVE, self.home[0], self.home[1])
            else:
                continue
            self.orders += 1


# ---------------------------------------------------------------------------------------------------- the profiles
def common(rep):
    """rejected <= 20 % of the inputs (errors == 0 is added by w9_play.run_asserts)."""
    return [A('rejected/inputs <= 20 %', rep['rejected'] <= 0.2 * max(1, rep['inputs']), '%d/%d' % (rep['rejected'], rep['inputs']))]


class _Idle:
    """what the asserts read from a controller that is not one of ours (the sabotage run's generic player)."""
    reached, keys_seen, built, fights, picked = [], set(), 0, 0, 0


def ctl(rep):
    ap = rep['nat'].autoplayer
    return ap if hasattr(ap, 'reached') else _Idle()


def series(rep, g):
    return [x for x in rep['series'].get(g, []) if x is not None]


# --- Escape Gay Space v1.8 (5955): escape. Runner = the 'Dead Guy' hero H000 (hp 100, speed 270). Level 1 is closed by
# three key gates; the keys (kymn items) lie on the level and each opens its gate when the carrier enters the gate's
# trigger rect [ext: TriggerRegisterEnterRect + item compare]; the Level_2_Checkpoint rect (the Win trigger) sets the
# revive region of the next level (udg_ReviveRegion) and moves the hero on. udg_Continues (5) counts the revives.
EGS_GOALS = ['gg_rct_' + n for n in ('Level_2_Checkpoint', 'Level_3_Checkpoint', 'Region_030', 'Region_056',
                                     'Region_071', 'Region_085', 'Region_098', 'Region_157')]
EGS_LOCKS = ['gg_rct_' + n for n in ('Open_Gate_1', 'Region_005', 'Region_004', 'Region_013', 'Region_015', 'Region_045', 'Region_044',
                                    'Region_068', 'Region_086', 'Region_097', 'Region_096', 'Region_158', 'Region_156')]
# first accepted run (300 s, seed 1): 2 goals (Level_2 at 43 s, Level_3 at 105 s), 4 keys carried
EGS_FLOORS = dict(goals=floor80(2), keys=floor80(4))


def egs_asserts(rep):
    ap = ctl(rep)
    rr = series(rep, 'udg_ReviveRegion')
    cont = series(rep, 'udg_Continues')
    out = common(rep)
    out += [
        A('runner reaches Level_2_Checkpoint', any(n == 'gg_rct_Level_2_Checkpoint' for _t, n in ap.reached), ap.reached[:3]),
        A('goals reached >= %d' % EGS_FLOORS['goals'], len(ap.reached) >= EGS_FLOORS['goals'], len(ap.reached)),
        A('udg_ReviveRegion changes', len(set(rr)) >= 2, 'values %d, first %s last %s' % (len(set(rr)), rr[:1], rr[-1:])),
        A('keys carried >= %d' % EGS_FLOORS['keys'], len(ap.keys_seen) >= EGS_FLOORS['keys'], len(ap.keys_seen)),
        A('udg_Continues drops (the map revived the hero)', bool(cont) and min(cont) < cont[0], '%s -> %s' % (cont[:1], cont[-1:])),
    ]
    return out


PROFILES['fan_595510EscapeGaySpacev18'] = dict(
    key='fan_595510EscapeGaySpacev18', humans=1, seconds=300,
    make=lambda N: RunnerPlay(N, EGS_GOALS, items=('kymn',), locks=EGS_LOCKS),
    report_globals=('udg_ReviveRegion', 'udg_Continues'), asserts=egs_asserts,
    xfail={'runner reaches Level_2_Checkpoint': 'MOVE-PORT-PATROL-EGS', 'goals reached >= %d' % EGS_FLOORS['goals']: 'MOVE-PORT-PATROL-EGS',
           'udg_ReviveRegion changes': 'MOVE-PORT-PATROL-EGS', 'keys carried >= %d' % EGS_FLOORS['keys']: 'MOVE-PORT-PATROL-EGS'})
XFAIL_NOTES['MOVE-PORT-PATROL-EGS'] = ('the map\'s guards patrol (IssuePointOrder "patrol" 384 .. 1408 units); since MOVE-PORT the patrol turns round '
                                      '[obs runtime 1.26a R3] instead of stopping at the far point, and the RunnerPlay bot (plain move orders, no dodging) '
                                      'dies at every sweep: Level_2_Checkpoint not reached in 300 s, udg_Continues 5 -> 0 (before: checkpoint at 43 s)')


# --- Pyramid Escape v1.20 (1709): a vote room, then 20 challenges. Explorer h00L (speed 320) stands in a closed room; the
# door destructable ZTd7 (hp 200) must be broken (its death event breaks the pathing blockers around the vote room, starts the 90 s
# vote timer and enables DoneVote); the vote circles are the n005 units (TriggerRegisterUnitInRange 120). udg_GameNumber
# counts the challenges played (0 until the vote is done, Next_Challenge adds 1).
def pyr_asserts(rep):
    ap = ctl(rep)
    gn = series(rep, 'udg_GameNumber')
    out = common(rep)
    out += [
        A('door ZTd7 broken by an attack (destructable dies)', any(n == 'dest:ZTd7' for _t, n in ap.reached), ap.reached[:3]),
        A('runner stands at a vote circle', any(n.startswith('unit:n005') for _t, n in ap.reached), ap.reached[:3]),
        A('udg_GameNumber rises (vote done, challenge 1 starts)', bool(gn) and gn[-1] >= 1, '%s -> %s' % (gn[:1], gn[-1:])),
    ]
    return out


PROFILES['fan_170910PyramidEscapev120'] = dict(
    key='fan_170910PyramidEscapev120', humans=1, seconds=300,
    make=lambda N: DoorPlay(N, [('unit', 'n005', 90)], 'ZTd7', types=('h00L',)),
    report_globals=('udg_GameNumber', 'udg_TotalGames', 'udg_TeamLife'), asserts=pyr_asserts)
# W9-PYR-DEST: the xfail W9R3-6-PYR is closed (destructables can be attacked: engine.order_dest / dest_strike)


# --- Azeroth Grand Prix (3058): race. Carts are steered by the arrow KEYS (TriggerRegisterPlayerKeyEventBJ: arrow down / up
# events, which set udg_Turning / udg_Accelerating); udg_PlaceChkPtCur[idx] / udg_PlaceChkPtLap[idx] are the checkpoint and lap of a
# cart (-1 until it moves). The soak player's dialog click starts the race (lap dialog -> cart pick -> 3-2-1 -> GAME_DRIVING).
def agp_asserts(rep):
    st = series(rep, 'udg_GameGoingStatus[0]')
    drv = series(rep, 'udg_GAME_DRIVING')
    cp = series(rep, 'udg_PlaceChkPtCur[0]')
    out = common(rep)
    out += [
        A('race started (GameGoingStatus[0] == GAME_DRIVING)', bool(st) and bool(drv) and st[-1] == drv[-1], '%s -> %s' % (st[:1], st[-1:])),
        A('checkpoint progress (PlaceChkPtCur[0] >= 0)', bool(cp) and max(cp) >= 0, 'max %s' % (max(cp) if cp else None)),
    ]
    return out


PROFILES['fan_30588AzerothGrandPrix'] = dict(
    key='fan_30588AzerothGrandPrix', humans=1, seconds=300,
    make=lambda N: W.Play(N),
    report_globals=('udg_GameGoingStatus[0]', 'udg_GAME_DRIVING', 'udg_PlaceChkPtCur[0]', 'udg_PlaceChkPtLap[0]', 'udg_HowManyPicked'),
    asserts=agp_asserts, xfail={'checkpoint progress (PlaceChkPtCur[0] >= 0)': 'W9R3-6-AGP'})
XFAIL_NOTES['W9R3-6-AGP'] = ('the cart is driven by arrow-key events (EVENT_PLAYER_ARROW_*_DOWN / UP) and the port has no key input '
                             'path: the cart stands on the grid, PlaceChkPtCur stays -1 for 300 s')


# --- World of Tree Tag (9220): tag. Tree team (players 1-9: one 25 hp Ent each) against the fire team (players 10-12: an
# Infernal each, hidden for the first 40 s, then landed next to the Ents). With no fire player in the game the map ends at 5 s (Tree
# win), so the scenario has 10 humans (slot 9 = the first infernal, idle). Progress counter: udg_ResourceGainGold / Wood [idx] (1 / 2
# at the start, +1 / +2 per finished resource storage eC09 outside the camping zone).
def tag_asserts(rep):
    ap = ctl(rep)
    gg = series(rep, 'udg_ResourceGainGold[1]')
    gw = series(rep, 'udg_ResourceGainWood[1]')
    out = common(rep)
    out += [
        A('udg_ResourceGainGold[1] rises (>= %d)' % TAG_FLOORS['gain'], bool(gg) and max(gg) >= TAG_FLOORS['gain'] and max(gg) > gg[0],
          'start %s max %s' % (gg[:1], max(gg) if gg else None)),
        A('udg_ResourceGainWood[1] rises', bool(gw) and max(gw) > gw[0], 'start %s max %s' % (gw[:1], max(gw) if gw else None)),
        A('storages built >= %d' % TAG_FLOORS['built'], ap.built >= TAG_FLOORS['built'], ap.built),
    ]
    return out


# first accepted run (10 humans): udg_ResourceGainGold[1] 1 -> 3, 3 storages built
TAG_FLOORS = dict(gain=3, built=floor80(3))
PROFILES['fan_9220WoTT'] = dict(
    key='fan_9220WoTT', humans=10, seconds=300, make=lambda N: TagPlay(N),
    report_globals=('udg_ResourceGainGold[1]', 'udg_ResourceGainWood[1]', 'udg_bFireWin', 'udg_bTreeWin'), asserts=tag_asserts)


# --- opt-Dawn of the Dead 4.1F (5201): survival. The player's Soul (e000) picks a hero by walking into a Select_* rect (the pick
# creates the hero at a random 'ewsp' spawn); wave 1 is created from udg_Wave = 1 (set in the init) by timers: the first zombies
# 120 s, more at 140 / 190 / 240 / 290 s, wave 2 needs both zombie players' food used <= 5 (> 305 s): so within 300 s the progress
# counter is the kills (+ the hero's XP).
# first accepted run: hero at 1 s, kills_by_player[0] 44 (2 at 150 s), 71 fight orders, 80 inputs / 0 rejected
DOTD_FLOORS = dict(kills=floor80(44), fights=floor80(71))


def dotd_asserts(rep):
    ap = ctl(rep)
    k = rep['kills'].get(0, 0)
    k150 = rep['snap'].get(150, {}).get('kills0', 0)
    out = common(rep)
    out += [
        A('hero picked by 10 s', any(n == 'hero' and t <= 10 for t, n in ap.reached), ap.reached[:2]),
        A('kills_by_player[0] >= %d' % DOTD_FLOORS['kills'], k >= DOTD_FLOORS['kills'], k),
        A('kills rise after 150 s', k > k150, '%d at 150 s -> %d' % (k150, k)),
        A('fight orders >= %d' % DOTD_FLOORS['fights'], ap.fights >= DOTD_FLOORS['fights'], ap.fights),
    ]
    return out


PROFILES['fan_5201optDawnOfTheDead41F'] = dict(
    key='fan_5201optDawnOfTheDead41F', humans=1, seconds=300, snapshots=(150,),
    make=lambda N: SurvivalPlay(N, ['gg_rct_Select_Soldier']),
    report_globals=('udg_Wave', 'udg_waveskip'), asserts=dotd_asserts)
