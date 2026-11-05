#!/usr/bin/env python3
"""Line Tower Wars bot ("Computer (easy)", docs/bot.md): a controller for an empty player slot.

It plays like a human: only through player inputs (engine.post_input = the same events the HUD sends) and only
from what that player could see (own gold / lumber, own units, the builder's build list and the type data, the
shops' sale lists). Deterministic: no random numbers, decisions are a function of the game state at a turn start.
The U# twin is W3Bot.cs (partial class W3Vm); both must make the same decisions at the same turns (the harness
replays the bot run and compares the traces). Off unless a slot is switched on (LtwBot.enabled / W3Vm.botMask).

Maze layout (v1, [inf] from the layout of real players): the lane is found from the map data, not hard-coded:
  * W = footprint width of the cheapest tower type (cells * 32). Probing placements (engine.can_place) in a
    column through the builder's start spot, in both directions, finds the lane: the direction with more
    buildable probes leads away from the builder (the creeps come from the far end = the entry side).
  * the lane's columns = the buildable run of tower centres in one row, one tower wide each;
  * row r (r = 0 at the entry side) is at W * 2 r from the entry row (one tower row + one free corridor of one
    tower width), holds a tower in every column but one; the gap is in column 0 for even r, in the last column for
    odd r: the creeps zig-zag.
Economy: ticks every BOT_EVERY turns. Sending share SEND_PCT % of everything spent (towers + sent units): when the
share is below it and a send unit is in stock, the bot buys the most expensive one it can pay (saves gold when it
cannot yet); a purchase at most every SEND_EVERY steps. Otherwise the idle builder builds the next missing tower.
"""
import math

import jass
from .engine import SPS, O_SMART, O_ATTACK

BOT_EVERY = 10            # turns between decisions (a turn is 5 steps = 25 ms: 0.25 s)
BOT_START = 3 * SPS       # steps: the map's Init is done
SEND_PCT = 35             # share of the spending that goes to sending units
SEND_EVERY = 10 * SPS     # steps between two purchases
MAXTRY = 40               # failed probes (1) / orders (10) of one cell before the cell is left out
KMAX = 64                 # probe range in tower widths
COLMAX = 7                # columns each side of the builder's column
ROWMAX = 32
COLS = 16


class LtwBot:
    def __init__(self, N, slot, enabled=True, melee_only=False):
        self.N = N
        self.eng = N.eng
        self.slot = slot
        self.pl = N.players[slot]
        self.enabled = enabled
        self.ai_paused = False          # PORT3-AI: disabled by PauseCompAI (resume only undoes this)
        self.melee_only = melee_only    # PORT3-AI: StartMeleeAI controls a melee slot only (never the LTW lane bot)
        self.state = 0            # 0 not planned, 1 planned, 2 no usable lane (stays off)
        self.tid = 0
        self.gold = 0
        self.lumber = 0
        self.W = 0.0
        self.d = 1
        self.cx0 = self.cy0 = 0.0
        self.entry_k = 0
        self.jl = 0
        self.n = 0
        self.nrows = 0
        self.tries = {}
        self.next_send = 0
        self.spent_tow = 0
        self.spent_send = 0
        self.log = []             # (steps, text) decisions, for tests / the report
        self.towers_ordered = 0
        self.sends = 0
        self.melee = None         # state 3: a melee map (the slot owns a town hall), MeleeBot below does the thinking
        self.eng.vm.on_turn.insert(0, self.on_turn)     # before apply_inputs: posted now -> applied this turn
        N.bots.append(self)                             # PORT3-AI: StartMeleeAI finds / pauses the slot's controller
        N.bot_slots.add(slot)

    # ---------------------------------------------------------------- helpers
    def builder(self):
        for u in self.N.units:
            if u.owner.id == self.slot and u.alive and not u.dead and u.data.get('Builds'):
                return u
        return None

    def cp(self, b, x, y):
        return self.eng.can_place(self.pl, self.tid, x, y, b)[0]

    def pick_tower(self, b):
        """cheapest building of the builder's list: (gold, lumber, more life first, list order)."""
        best = None
        for k, s in enumerate(str(b.data.get('Builds', '')).split(',')):
            s = s.strip()
            if len(s) != 4:
                continue
            tid = jass.rawcode(s)
            d = self.N.unit_types.get(tid)
            if d is None or not d.get('bldg'):
                continue
            key = (int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0), -float(d.get('hp', 0) or 0), k)
            if best is None or key < best[0]:
                best = (key, tid)
        return best[1] if best else 0

    # ---------------------------------------------------------------- lane discovery
    def scan(self, b):
        self.tid = self.pick_tower(b)
        d = self.N.unit_types.get(self.tid)
        if not self.tid or d is None:
            return False
        fp = self.eng.oriented(d)
        if fp is None:
            return False
        W = max(fp[0], fp[1]) * 32.0
        H = W / 2.0
        self.W = W
        _ok, cx0, cy0 = self.eng.can_place(self.pl, self.tid, b.x, b.y, b)
        best_d, best_cnt = 1, -1
        for dd in (1, -1):
            cnt = 0
            for k in range(1, KMAX + 1):
                if self.cp(b, cx0, cy0 + dd * k * W):
                    cnt += 1
            if cnt > best_cnt:
                best_cnt, best_d = cnt, dd
        if best_cnt < 3:
            return False
        dd = best_d
        okk = [False] * (KMAX + 1)
        for k in range(1, KMAX + 1):
            okk[k] = self.cp(b, cx0, cy0 + dd * k * W)
        kmin = 1
        while kmin <= KMAX and not okk[kmin]:
            kmin += 1
        last, miss = kmin, 0
        for k in range(kmin + 1, KMAX + 1):
            if okk[k]:
                last, miss = k, 0
            else:
                miss += 1
                if miss >= 3:
                    break
        kmax = last
        kmid = (kmin + kmax) // 2
        probes = (0, 1, -1, 2, -2)
        jl = jr = 0
        found = False
        for off in probes:
            km = kmid + off
            if km < kmin or km > kmax:
                continue
            ym = cy0 + dd * km * W
            if not self.cp(b, cx0, ym):
                continue
            # the run of buildable centres on the engine's 64 lattice (half a tower width), then as many whole
            # towers as fit, aligned to the run's left end: the rows seal the lane up to the one gap
            jl = 0
            while jl > -2 * COLMAX and self.cp(b, cx0 + (jl - 1) * H, ym):
                jl -= 1
            jr = 0
            while jr < 2 * COLMAX and self.cp(b, cx0 + (jr + 1) * H, ym):
                jr += 1
            found = True
            break
        if not found:
            return False
        n = (jr - jl) // 2 + 1
        if n < 3 or n > COLS:
            return False
        ek = kmax
        while ek > kmin:
            y = cy0 + dd * ek * W
            cnt = 0
            for c in range(n):
                if self.cp(b, cx0 + jl * H + c * W, y):
                    cnt += 1
            if cnt >= n - 1:
                break
            ek -= 1
        self.d, self.cx0, self.cy0 = dd, cx0, cy0
        self.entry_k, self.jl, self.n = ek, jl, n
        self.nrows = min((ek - kmin) // 2 + 1, ROWMAX)
        self.log.append((self.eng.vm.steps, 'lane: tower %s W %g dir %d columns %d rows %d entry k %d (x0 %g, entry y %g)' % (
            d.get('id4'), W, dd, n, self.nrows, ek, cx0 + jl * H, cy0 + dd * ek * W)))
        return True

    def cell_x(self, c):
        return self.cx0 + self.jl * self.W / 2.0 + c * self.W

    def cell_y(self, r):
        return self.cy0 + self.d * (self.entry_k - 2 * r) * self.W

    def gap(self, r):
        return 0 if r % 2 == 0 else self.n - 1

    # ---------------------------------------------------------------- decisions
    def is_melee(self):
        """a melee map: the slot owns a worker and a building that trains it (a town hall); LTW has no hall."""
        for u in self.N.units:
            if u.owner.id != self.slot or not u.alive or u.dead or u.is_bldg or not u.data.get('Builds'):
                continue
            for h in self.N.units:
                if h.owner.id == self.slot and h.alive and not h.dead and h.is_bldg and self.eng.trains(h, u.tid):
                    return True
        return False

    def on_turn(self, turn):
        if not self.enabled or self.state == 2 or turn % BOT_EVERY or self.eng.vm.steps < BOT_START:
            return
        if self.state == 3:
            self.melee.tick()
            return
        if self.melee_only and self.state == 0 and not self.is_melee():
            return
        if self.state == 0 and self.is_melee():
            self.state = 3
            self.melee = MeleeBot(self)
            self.melee.tick()
            return
        b = self.builder()
        if b is None:
            return
        if self.state == 0:
            if b.build is not None or b.order != 0:
                return
            self.state = 1 if self.scan(b) else 2
            if self.state == 2:
                self.log.append((self.eng.vm.steps, 'no usable lane'))
                return
        self.gold = self.pl.state.get(1, 0)
        self.lumber = self.pl.state.get(2, 0)
        now = self.eng.vm.steps
        if now >= self.next_send and self.spent_send * 100 < SEND_PCT * (self.spent_tow + self.spent_send):
            shop, tid, cost, saving = self.pick_send()
            if shop is not None:
                self.eng.post_input(self.slot, 5, shop.serial, tid, 0.0, 0.0, 0)
                self.spent_send += cost
                self.next_send = now + SEND_EVERY
                self.sends += 1
                self.log.append((now, 'send %s for %d' % (self.N.unit_types[tid].get('id4'), cost)))
                return
            if saving:
                return
        if b.build is None and b.order == 0:
            self.build_step(b, now)

    def pick_send(self):
        """-> (shop, type id, cost, saving): the most expensive unit the player can buy now, else saving = True when
        a unit is in stock but too dear."""
        sh = self.eng.shops
        best = None
        save = False
        for u in self.N.units:
            if u.owner.id != self.slot or not u.alive or u.dead or not getattr(u, 'shop', None):
                continue
            for kind, tid, g, lu, cur, mx, code in sh.listing(u, self.slot):
                if kind != 1:
                    continue
                if code == 0:
                    if best is None or g > best[2]:
                        best = (u, tid, g)
                elif code == 8:
                    save = True
        if best is not None:
            return best[0], best[1], best[2], False
        return None, 0, 0, save

    def build_step(self, b, now):
        d = self.N.unit_types[self.tid]
        g, lu = int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)
        if self.gold < g or self.lumber < lu:
            return
        W = self.W
        occ = set()
        for u in self.N.units:
            if u.owner.id != self.slot or not u.alive or u.dead or not u.is_bldg:
                continue
            cf = (u.x - (self.cx0 + self.jl * W / 2.0)) / W
            c = int(math.floor(cf + 0.5))
            kf = self.d * (u.y - self.cy0) / W
            k = int(math.floor(kf + 0.5))
            if abs(cf - c) > 0.01 or abs(kf - k) > 0.01 or c < 0 or c >= self.n or k > self.entry_k or (self.entry_k - k) % 2:
                continue
            r = (self.entry_k - k) // 2
            if r < self.nrows:
                occ.add((r, c))
        for r in range(self.nrows):
            gp = self.gap(r)
            for c in range(self.n):
                if c == gp or (r, c) in occ or self.tries.get((r, c), 0) >= MAXTRY:
                    continue
                x, y = self.cell_x(c), self.cell_y(r)
                if not self.cp(b, x, y):
                    self.tries[(r, c)] = self.tries.get((r, c), 0) + 1
                    continue
                self.eng.post_input(self.slot, 1, b.serial, self.tid, x, y)
                self.tries[(r, c)] = self.tries.get((r, c), 0) + 10
                self.spent_tow += g
                self.towers_ordered += 1
                self.log.append((now, 'build %s at %g,%g (row %d col %d)' % (d.get('id4'), x, y, r, c)))
                return


# ====================================================================== melee bot
MELEE_WORKERS = 12            # workers the hall trains up to
MELEE_FOOD_MARGIN = 5         # a food building when (food cap incl. buildings under construction) - used <= this
MELEE_WAVE = 8                # idle soldiers at home that make a wave
MELEE_WAVE6_AT = 180 * SPS    # the FIRST wave goes with 6 from here on ...
MELEE_WAVE4_AT = 230 * SPS    # ... and with 4 from here on (slow economies must not sit at home for the whole game)
MELEE_RAX_MAX = 3             # barracks-type buildings with spare gold
MELEE_HOME_R2 = 1500.0 * 1500.0
MELEE_PEND = 15 * SPS         # steps a build order may take to start before another one is allowed
MELEE_RAX_AT = 15 * SPS       # the barracks is wanted from here on (or with 6 workers)
MELEE_MELEE_RANGE = 128       # weapon range up to this = a melee soldier
MELEE_SPOT_FAIL = 5 * SPS     # steps without a new spot search after none was found
MELEE_SPOT_MINE2 = 250000.0   # (500)^2: building spots keep this far from the mine
MELEE_GOLDK = (0, 1, 2, 3, 5, 6, 8)  # of 12 consecutive worker assignments these go to the mine (rest: trees)
MELEE_OFFS = sorted((i * i + j * j, j, i) for j in range(-12, 13) for i in range(-12, 13) if 22 <= i * i + j * j <= 156)
PLAYER_FOOD_CAP, PLAYER_FOOD_USED = 4, 5


class MeleeBot:
    """Computer (easy) for a classic melee map (docs/bot.md "Melee bot"): a controller for a player slot that only
    posts player inputs and reads what the player could see. Called by LtwBot.on_turn (state 3) once per tick.

    All types come from the unit data of the slot's own start units, no race table:
      * worker  = the own non-building unit that has a build list; town hall = the own building that trains it;
      * food    = the cheapest building of the worker's build list that makes food and trains nothing;
      * barracks= the first building of the build list (data order) that trains a ground soldier: a non-hero,
                  non-flying unit with food use and a weapon; soldier = of that list the one that is buildable now
                  (requirements, tech limit), melee (range <= 128) first, then the cheapest.
    One action per tick: idle worker -> mine (gold) / nearest tree (lumber), attack wave, food building, barracks
    (second one with spare gold), worker at the hall, soldier at the barracks."""

    def __init__(self, bot):
        self.bot = bot
        self.N = bot.N
        self.eng = bot.eng
        self.slot = bot.slot
        self.pl = bot.pl
        self.planned = False
        self.wt = self.ft = self.rt = 0       # worker / food building / barracks type
        self.mark = 0                         # start units have serials up to this
        self.assign = 0                       # worker assignments so far (gold / trees pattern)
        self.pend_until = 0
        self.pend_cnt = 0
        self.fail_until = 0
        self.log = bot.log
        self.workers_trained = 0
        self.soldiers_trained = 0
        self.built = 0
        self.waves = 0
        self.sent = 0

    # ---------------------------------------------------------------- type predicates (unit data only)
    def soldier_type(self, tid):
        """a ground soldier type: no building, no hero, no worker, food use, a weapon, not flying."""
        d = self.N.unit_types.get(tid)
        if d is None or d.get('bldg') or d.get('Builds') or 0x41 <= (tid >> 24) <= 0x5a:
            return False
        if int(d.get('fused', 0) or 0) <= 0 or int(d.get('dice1', 0) or 0) <= 0 or int(d.get('movetp', 0) or 0) & 2:
            return False
        return True

    def trains_list(self, d):
        out = []
        for s in str(d.get('Trains', '') or '').split(','):
            s = s.strip()
            if len(s) == 4:
                out.append(jass.rawcode(s))
        return out

    def plan(self, w):
        """the build-list data of the first worker: food building type, barracks type."""
        ud = self.N.unit_types
        self.wt = w.tid
        mark = 0
        for u in self.N.units:
            if u.owner.id == self.slot and u.serial > mark:
                mark = u.serial
        self.mark = mark
        best_f = best_r = None
        for k, s in enumerate(str(w.data.get('Builds', '')).split(',')):
            s = s.strip()
            if len(s) != 4:
                continue
            tid = jass.rawcode(s)
            d = ud.get(tid)
            if d is None or not d.get('bldg'):
                continue
            tr = self.trains_list(d)
            if int(d.get('fmade', 0) or 0) > 0 and not tr:
                key = (int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0), k)
                if best_f is None or key < best_f[0]:
                    best_f = (key, tid)
            elif best_r is None and int(d.get('fmade', 0) or 0) == 0:
                for t in tr:
                    if self.soldier_type(t):
                        best_r = (k, tid)
                        break
        self.ft = best_f[1] if best_f else 0
        self.rt = best_r[1] if best_r else 0
        self.planned = True
        self.log.append((self.eng.vm.steps, 'melee plan: worker %s food %s barracks %s' % (
            ud[self.wt].get('id4'), ud[self.ft].get('id4') if self.ft else '-', ud[self.rt].get('id4') if self.rt else '-')))

    def pick_soldier(self, rax):
        """of the barracks' train list the type to train now: (melee first, gold, lumber, list order)."""
        ud = self.N.unit_types
        best = None
        for k, tid in enumerate(self.trains_list(rax.data)):
            if not self.soldier_type(tid):
                continue
            if not self.eng.req_ok(self.pl, tid) or not self.eng.tech_ok(self.pl, tid):
                continue
            d = ud[tid]
            key = (0 if int(d.get('rangeN1', 0) or 0) <= MELEE_MELEE_RANGE else 1, int(d.get('gold', 0) or 0),
                   int(d.get('lumber', 0) or 0), k)
            if best is None or key < best[0]:
                best = (key, tid)
        return best[1] if best else 0

    # ---------------------------------------------------------------- orders
    def harvest(self, u, hall):
        """idle worker -> the mine nearest to the hall (gold) or the tree nearest to it (lumber)."""
        al = str(u.data.get('abilList', '') or '')
        gold_ok = 'Ahar' in al or 'Awha' in al or 'Aaha' in al
        lum_ok = 'Ahar' in al or 'Awha' in al or 'Ahrl' in al
        hx, hy = (hall.x, hall.y) if hall is not None else (u.x, u.y)
        gold = gold_ok
        if gold_ok and lum_ok:
            gold = (self.assign % 12) in MELEE_GOLDK
            self.assign += 1
        mine = self.eng.hv._nearest_mine(u, hx, hy, 0.0) if gold else None
        if mine is not None:
            self.eng.post_input(self.slot, 2, u.serial, O_SMART, 0.0, 0.0, mine.serial)
            self.log.append((self.eng.vm.steps, 'gold u%d -> mine u%d' % (u.serial, mine.serial)))
            return True
        tree = self.eng.hv._nearest_tree(u, hx, hy, 0.0) if lum_ok else None
        if tree is not None:
            self.eng.post_input(self.slot, 2, u.serial, O_SMART, 0.0, 0.0, tree.dser)
            self.log.append((self.eng.vm.steps, 'lumber u%d -> tree %d' % (u.serial, tree.dser)))
            return True
        return False

    def spot(self, tid, builder, hall):
        """a placeable centre near the hall, nearest first (the 64 lattice), away from the mine."""
        mine = self.eng.hv._nearest_mine(None, hall.x, hall.y, 0.0)
        for _key, j, i in MELEE_OFFS:
            x, y = hall.x + 64.0 * i, hall.y + 64.0 * j
            if mine is not None and (x - mine.x) ** 2 + (y - mine.y) ** 2 <= MELEE_SPOT_MINE2:
                continue
            ok, cx, cy = self.eng.can_place(self.pl, tid, x, y, builder)
            if ok:
                return cx, cy
        return None

    def build(self, tid, hall, workers, now):
        """order the worker nearest to the hall (idle ones first) to build tid at a spot near the hall."""
        best = None
        for u in workers:
            key = (0 if u.order == 0 else 1, (u.x - hall.x) ** 2 + (u.y - hall.y) ** 2)
            if best is None or key < best[0]:
                best = (key, u)
        if best is None:
            return False
        w = best[1]
        sp = self.spot(tid, w, hall)
        if sp is None:
            self.fail_until = now + MELEE_SPOT_FAIL
            self.log.append((now, 'no spot for %s' % self.N.unit_types[tid].get('id4')))
            return False
        self.eng.post_input(self.slot, 1, w.serial, tid, sp[0], sp[1])
        self.built += 1
        self.log.append((now, 'build %s at %g,%g' % (self.N.unit_types[tid].get('id4'), sp[0], sp[1])))
        return True

    def enemy_start(self):
        """the start location of the first other playing, not allied slot (None if unknown)."""
        for pl in self.N.players[:12]:
            if pl is self.pl or pl.slot_state != 1 or self.eng.ally(self.pl, pl):
                continue
            loc = self.N.start_locs.get(pl.start_loc)
            if loc is not None:
                return loc
        return None

    # ---------------------------------------------------------------- the tick
    def tick(self):
        N, eng, slot, pl = self.N, self.eng, self.slot, self.pl
        now = eng.vm.steps
        if not self.planned:
            for u in N.units:
                if u.owner.id == slot and u.alive and not u.dead and not u.is_bldg and u.data.get('Builds'):
                    self.plan(u)
                    break
            if not self.planned:
                return
        ud = N.unit_types
        hall = None
        halls_idle = None
        queued_w = 0
        nbld = 0
        food_c = 0
        rax_n = 0
        rax_ready = None
        n_work = 0
        workers = []              # may build: not hidden, not constructing
        idle_h = None             # an idle harvester
        soldiers = []
        for u in N.units:
            if u.owner.id != slot or not u.alive or u.dead:
                continue
            if u.is_bldg:
                nbld += 1
                if u.constructing:
                    if u.tid == self.ft:
                        food_c += int(ud[u.tid].get('fmade', 0) or 0)
                    if u.tid == self.rt:
                        rax_n += 1
                    continue
                if u.tid == self.rt:
                    rax_n += 1
                    if len(u.tq) < 2 and (rax_ready is None or len(u.tq) < len(rax_ready.tq)):
                        rax_ready = u
                if eng.trains(u, self.wt):
                    if hall is None:
                        hall = u
                    for e in u.tq:
                        if e[0] == self.wt and e[1] == 0:
                            queued_w += 1
                    if not u.tq and halls_idle is None:
                        halls_idle = u
                continue
            if u.data.get('Builds'):
                n_work += 1
                if not u.hidden and not u.constructing and u.build is None:
                    workers.append(u)
            elif u.serial > self.mark and self.soldier_type(u.tid):
                soldiers.append(u)
            if idle_h is None and not u.hidden and not u.constructing and u.build is None and u.order == 0:
                al = str(u.data.get('abilList', '') or '')
                if ((u.data.get('Builds') or u.serial <= self.mark)
                        and ('Ahar' in al or 'Awha' in al or 'Aaha' in al or 'Ahrl' in al)):
                    idle_h = u
        gold = pl.state.get(1, 0)
        lumber = pl.state.get(2, 0)
        cap = pl.state.get(PLAYER_FOOD_CAP, 0)
        used = pl.state.get(PLAYER_FOOD_USED, 0)

        # 1. an idle worker harvests
        if idle_h is not None and self.harvest(idle_h, hall):
            return
        # 2. a wave of idle soldiers at home attack-moves to the enemy start location
        if hall is not None:
            home = [u for u in soldiers if u.order == 0 and not u.hidden]
            near = 0
            for u in home:
                if (u.x - hall.x) ** 2 + (u.y - hall.y) ** 2 <= MELEE_HOME_R2:
                    near += 1
            need = MELEE_WAVE
            if self.waves == 0:
                if now >= MELEE_WAVE4_AT:
                    need = 4
                elif now >= MELEE_WAVE6_AT:
                    need = 6
            if near >= need:
                loc = self.enemy_start()
                if loc is not None:
                    for u in home:
                        eng.post_input(slot, 1, u.serial, O_ATTACK, loc[0], loc[1])
                    self.waves += 1
                    self.sent += len(home)
                    self.log.append((now, 'wave %d: %d soldiers -> %g,%g' % (self.waves, len(home), loc[0], loc[1])))
                    return
        if hall is None:
            return
        pending = now < self.pend_until and nbld <= self.pend_cnt
        can_build = not pending and now >= self.fail_until and bool(workers)
        # 3. food
        if self.ft and cap + food_c < 100 and cap + food_c - used <= MELEE_FOOD_MARGIN and can_build:
            d = ud[self.ft]
            g, lu = int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)
            if gold >= g and lumber >= lu:
                if self.build(self.ft, hall, workers, now):
                    self.pend_until, self.pend_cnt = now + MELEE_PEND, nbld
                    return
            elif gold < g:
                return                                             # save
        # 4. barracks: one early, a second one with spare gold
        want = 0
        if self.rt:
            if now >= MELEE_RAX_AT or n_work + queued_w >= 6:
                want = 1
            if rax_n >= 1 and rax_n < MELEE_RAX_MAX and rax_ready is not None and gold >= 300 + 100 * rax_n \
                    and n_work >= MELEE_WORKERS:
                want = rax_n + 1
        if rax_n < want and can_build:
            d = ud[self.rt]
            g, lu = int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)
            if gold >= g and lumber >= lu:
                if self.build(self.rt, hall, workers, now):
                    self.pend_until, self.pend_cnt = now + MELEE_PEND, nbld
                    return
            elif gold < g and rax_n == 0:
                return                                             # save
        # 5. a worker at the hall
        if halls_idle is not None and n_work + queued_w < MELEE_WORKERS:
            d = ud[self.wt]
            if gold >= int(d.get('gold', 0) or 0) and lumber >= int(d.get('lumber', 0) or 0) \
                    and used + int(d.get('fused', 0) or 0) <= cap:
                eng.post_input(slot, 0, halls_idle.serial, self.wt)
                self.workers_trained += 1
                return
        # 6. a soldier at the barracks
        if rax_ready is not None:
            tid = self.pick_soldier(rax_ready)
            if tid:
                d = ud[tid]
                if gold >= int(d.get('gold', 0) or 0) and lumber >= int(d.get('lumber', 0) or 0) \
                        and used + int(d.get('fused', 0) or 0) <= cap:
                    eng.post_input(slot, 0, rax_ready.serial, tid)
                    self.soldiers_trained += 1
                    return


def attach(N, slots):
    """one bot per slot; each inserts itself at index 0 of vm.on_turn, so they are made last slot first: the turn then
    runs them in ascending slot order, like the U# bot (BotTurn)."""
    made = {s: LtwBot(N, s) for s in sorted(slots, reverse=True)}
    return [made[s] for s in slots]
