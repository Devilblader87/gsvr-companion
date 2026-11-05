#!/usr/bin/env python3
"""Deterministic test player ("autoplayer") for the reference engine (docs/engine_w5.md §6).

It plays player 1 (index 0) the way a VRChat player will: it looks at the world at a turn boundary and posts
*input events* (engine.post_input = order natives on its own units, applied at the start of the next turn).
Script per map: from fixed game times on, whenever its builder is idle, build the next tower — the map's two
cheapest tower types (by gold, lumber, Builds order) alternating — at the first buildable spot around a fixed
anchor near the creep lane (candidates on the 64 grid, nearest first). Upgrades nothing, trains nothing.

  python3 tools/jvm/run.py --map AzureTD --seconds 120 --autoplay
"""
from .engine import SPS, _f

# map -> (anchor x, y, search radius, [game time (s) from which tower k may be ordered])
SCRIPTS = {
    'AzureTD': (-3904.0, 448.0, 1280.0, [62.0, 64.0, 66.0, 68.0, 70.0, 72.0, 74.0, 76.0]),
    'BlizzardTD': (-9152.0, 384.0, 1280.0, [4.0, 6.0, 8.0]),
    'SkibiTD': (-1536.0, 0.0, 1280.0, []),               # soak only: where the west lanes meet before the castle
}
PLAYER = 0
# map globals the report shows (progress / lives; read only, nothing of it is written into the repo)
REPORT_GLOBALS = {
    'AzureTD': ('udg_Round', 'udg_Corrupted', 'udg_kills[1]'),
    'BlizzardTD': ('udg_CurrentWave', 'udg_CountLeakedUnitsTL'),
    'SkibiTD': ('udg_Level', 'udg_LivesW', 'udg_LivesE'),
}


class AutoPlayer:
    def __init__(self, N, anchor_x, anchor_y, radius, times, player=PLAYER):
        self.N = N
        self.eng = N.eng
        self.ax, self.ay, self.radius = anchor_x, anchor_y, radius
        self.times = list(times)
        self.player = player
        self.next = 0
        self.log = []                 # (game ms, text) of the decisions (for the report, not the trace)
        self.types = None
        self.eng.vm.on_turn.append(self.on_turn)     # after the engine applied the inputs: posted now -> next turn

    def builder(self):
        for u in self.N.units:        # creation order
            if u.owner.id == self.player and u.alive and not u.dead and u.data.get('Builds'):
                return u
        return None

    def tower_types(self, b):
        if self.types is None:
            import jass
            cands = []
            for k, s in enumerate(str(b.data.get('Builds', '')).split(',')):
                s = s.strip()
                if len(s) != 4:
                    continue
                tid = jass.rawcode(s)
                d = self.N.unit_types.get(tid)
                if d is None or not d.get('bldg'):
                    continue
                if not any(int(d.get('weapsOn', 0) or 0) >> i & 1 and int(d.get('weapTp%d' % (i + 1), 0) or 0)
                           for i in (0, 1)):
                    continue
                cands.append((int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0), k, tid))
            cands.sort()
            self.types = [c[3] for c in cands[:2]]
        return self.types

    def spot(self, tid, b):
        """first placeable centre around the anchor (64 grid, nearest first, ties by y then x)."""
        r = int(self.radius // 64)
        cand = []
        for j in range(-r, r + 1):
            for i in range(-r, r + 1):
                x, y = self.ax + 64.0 * i, self.ay + 64.0 * j
                cand.append(((i * i + j * j), j, i, x, y))
        cand.sort()
        for _d, _j, _i, x, y in cand:
            ok, cx, cy = self.eng.can_place(b.owner, tid, x, y, b)
            if ok:
                return cx, cy
        return None

    def on_turn(self, turn):
        if self.next >= len(self.times):
            return
        now = self.eng.vm.steps
        if now < int(round(self.times[self.next] * SPS)):
            return
        b = self.builder()
        if b is None or b.build is not None or b.order != 0:
            return
        types = self.tower_types(b)
        if not types:
            self.next = len(self.times)
            return
        tid = types[self.next % len(types)]
        d = self.N.unit_types[tid]
        if b.owner.state.get(1, 0) < int(d.get('gold', 0) or 0):
            return                                        # wait for gold
        p = self.spot(tid, b)
        if p is None:
            self.next = len(self.times)
            return
        self.eng.post_input(self.player, 1, b.serial, tid, p[0], p[1])
        self.log.append((now * 5, 'build %s at %g,%g' % (d.get('id4'), p[0], p[1])))
        self.next += 1


def attach(N, map_name):
    s = SCRIPTS.get(map_name)
    if s is None:
        return None
    return AutoPlayer(N, s[0], s[1], s[2], s[3])


def map_globals(vm, map_name):
    out = {}
    for g in REPORT_GLOBALS.get(map_name, ()):
        name, idx = (g.split('[')[0], int(g.split('[')[1].rstrip(']'))) if '[' in g else (g, None)
        if name not in vm.p.global_by_name:
            continue
        v = vm.global_value(name)
        if idx is not None:
            v = v.get(idx, v.z) if isinstance(v, dict) else None
        out[g] = v if isinstance(v, (int, float)) else None
    return out


class SoakPlayer:
    """W8 soak player (not used by the 120 s goldens): plays player 1 for a whole game. Each turn: if the builder is
    idle, build the most expensive affordable tower type at the nearest free spot around the anchor (the search ring
    grows when it is full); if a unit with a Trains list (Azure's upgrade buildings) has an empty queue, train the
    cheapest affordable entry. Deterministic (creation order, fixed candidate order)."""

    def __init__(self, N, anchor_x, anchor_y, radius, player=PLAYER, bonus=0, early=False):
        self.N = N
        self.eng = N.eng
        self.ax, self.ay, self.radius = anchor_x, anchor_y, radius
        self.player = player
        self.bonus = bonus
        self.early = early                # W9-2: dialogs + builder training before the first builder exists
        self.log = []
        self.types = None
        self.full = set()             # rings already full
        self.tries = {}               # build spot -> orders sent there (unreachable spots stop being chosen)
        self.eng.vm.on_turn.append(self.on_turn)

    def own(self):
        return [u for u in self.N.units if u.owner.id == self.player and u.alive and not u.dead]

    def detects(self, d):
        ab = self.eng.ab
        for a in str(d.get('abilList', '') or '').split(','):
            a = a.strip()
            if len(a) == 4:
                import jass
                if ab.base(jass.rawcode(a)) in ('Adts', 'Atru', 'Adt1'):
                    return True
        return False

    def towers(self, b):
        """b's tower types (bldg with a weapon), cheapest first; per builder type."""
        if self.types is None:
            self.types = {}
        if b.tid not in self.types:
            import jass
            c = []
            for s in str(b.data.get('Builds', '')).split(','):
                s = s.strip()
                d = self.N.unit_types.get(jass.rawcode(s)) if len(s) == 4 else None
                if d and d.get('bldg') and (any(int(d.get('weapsOn', 0) or 0) >> i & 1 for i in (0, 1))
                                            or self.detects(d)):
                    c.append((int(d.get('gold', 0) or 0), jass.rawcode(s)))
            c.sort()
            self.types[b.tid] = c
        return self.types[b.tid]

    def spot(self, tid, b):
        r = int(self.radius // 64)
        while r <= 64:
            if r not in self.full:
                cand = sorted(((i * i + j * j), j, i) for j in range(-r, r + 1) for i in range(-r, r + 1))
                for _d, j, i in cand:
                    ok, cx, cy = self.eng.can_place(b.owner, tid, self.ax + 64.0 * i, self.ay + 64.0 * j, b)
                    if ok and self.tries.get((cx, cy), 0) < SOAK_SPOT_TRIES:
                        self.tries[(cx, cy)] = self.tries.get((cx, cy), 0) + 1   # never-starting spots are dropped
                        return cx, cy
                self.full.add(r)
            r += 8
        return None

    def upgrade(self, units, gold, now):
        """W8: the cheapest affordable upgrade (Upgrade list, requirements met) of a finished tower, oldest first.
        W9R3-3: affordable = what eng.order_upgrade accepts (RelativeUpgradeCost, lumber, food); a building whose
        upgrade was posted is not tried again for 1 s."""
        eng = self.eng
        retry = getattr(self, 'upg_retry', None)
        if retry is None:
            retry = self.upg_retry = {}
        best = None
        for b in units:
            if not b.is_bldg or not b.fighter or b.constructing or b.upg is not None:
                continue
            if retry.get(b.serial, -1) > now:
                continue
            for t in eng.upgrade_list(b.tid):
                if self.N.unit_types.get(t) is None:
                    continue
                ok, dg, _dl = eng.upgrade_check(b, t)       # the engine's own check and cost (shared, no copy)
                if not ok:
                    continue
                if best is None or dg < best[0]:
                    best = (dg, b, t)
        if best is None:
            return False
        _dg, b, t = best
        retry[b.serial] = now + SPS
        self.eng.post_input(self.player, 0, b.serial, t)
        self.log.append((now * 5, 'upgrade %s -> %s' % (b.data.get('id4'), self.N.unit_types[t].get('id4'))))
        return True

    def click_dialogs(self, now):
        """W8: a dialog shown to the player (DialogDisplay is recorded as UI text) is answered with its first button
        (Skibi: classic game type) one turn later — input kind 4 (NOTES_skibi §1)."""
        N = self.N
        hp = N.H(N.players[self.player])
        seen = getattr(self, 'dlg_seen', 0)
        texts = N.last_text
        self.dlg_seen = len(texts)
        for t in texts[seen:]:
            if t[1] == 'dialog' and t[2] == hp and t[4]:
                o = N.o(t[3])
                if o is not None and getattr(o, 'buttons', None):
                    self.eng.post_input(self.player, 4, t[3], 0)
                    self.log.append((now * 5, 'dialog button 0'))
                    return True
        return False

    def opts(self, b, key, gold, want):
        """affordable entries of b's Trains list: want 'builder' (types with Builds), 'other'; never demolish / sell."""
        import jass
        out = []
        for s in str(b.data.get(key, '') or '').split(','):
            s = s.strip()
            d = self.N.unit_types.get(jass.rawcode(s)) if len(s) == 4 else None
            if d is None or int(d.get('gold', 0) or 0) > gold:
                continue
            if self.early and int(d.get('lumber', 0) or 0) > b.owner.state.get(2, 0):
                continue                                  # W9-2: the engine refuses it (lumber; Burbenog trains cost 1)
            fu = int(d.get('fused', 0) or 0)
            if fu and not self.eng.food_ok(b.owner, fu):
                continue                                  # the engine would refuse it (food)
            if not self.eng.tech_ok(b.owner, jass.rawcode(s)) or not self.eng.req_ok(b.owner, jass.rawcode(s)):
                continue                                  # ... or the tech limit (heroes: 1 per type) / requirements
            nm = str(d.get('name') or '')
            if 'Demolish' in nm or 'Sell' in nm or (self.early and 'sell' in nm.lower()):
                continue                                  # W9-2: Green TD's '|c00FFFF00SELL|r' (ncop) sold every tower
            if (want == 'builder') != bool(d.get('Builds')):
                continue
            out.append((int(d.get('gold', 0) or 0), jass.rawcode(s)))
        out.sort()
        return out

    def pre_builder(self, now):
        """W9-2: no builder unit yet: train one (phase 1 of on_turn: a Trains entry with Builds of a finished building
        with an empty queue), or pick a governor / race at a chooser building."""
        units = self.own()
        if not units:
            return
        gold = units[0].owner.state.get(1, 0)
        for b in units:
            if b.is_bldg and not b.constructing and b.data.get('Trains') and not b.tq:
                for g, tid in self.opts(b, 'Trains', gold, 'builder'):
                    self.eng.post_input(self.player, 0, b.serial, tid)
                    self.log.append((now * 5, 'train builder %s' % self.N.unit_types[tid].get('id4')))
                    return
        # a chooser building (Burbenog 'Governor Chooser' hhou: no Trains, an Upgrade list = the governors): the first
        # affordable choice, once
        for b in units:
            if b.is_bldg and not b.constructing and b.upg is None and not b.data.get('Trains') and \
                    not getattr(b, 'chose', False):
                for t in self.eng.upgrade_list(b.tid):
                    d = self.N.unit_types.get(t)
                    if d is None or not self.eng.tech_ok(b.owner, t) or not self.eng.req_ok(b.owner, t):
                        continue
                    if int(d.get('gold', 0) or 0) - int(b.data.get('gold', 0) or 0) <= gold and d.get('Trains'):
                        b.chose = True
                        self.eng.post_input(self.player, 0, b.serial, t)
                        self.log.append((now * 5, 'choose %s' % d.get('id4')))
                        return

    def on_turn(self, turn):
        """one decision per turn, round robin over: build a tower, train a new builder type, upgrade a tower /
        research (cheapest affordable Trains entry)."""
        now = self.eng.vm.steps
        if self.ax is None:
            # W9-2: before the first builder exists (Skibi10: 'Classic' dialog; Burbenog: governors trained at a hhou)
            # the dialogs are answered and the builder-training phase runs; the anchor is the first builder's spot
            b = next((u for u in self.own() if u.data.get('Builds') and not u.is_bldg), None)
            if b is None:
                if self.early:                            # off for ETD / V1.51: their goldens answer dialogs later
                    if not self.click_dialogs(now):
                        self.pre_builder(now)
                return
            self.ax, self.ay = b.x, b.y                   # W9 fan maps: anchored at the first builder
        if self.click_dialogs(now):
            return
        if self.bonus and now >= getattr(self, 'next_bonus', 0):
            self.next_bonus = now + 30 * SPS          # test-only gold (input kind 3) every 30 s of game time
            self.eng.post_input(self.player, 3, 0, self.bonus)
            return
        units = self.own()
        if not units:
            return
        gold = units[0].owner.state.get(1, 0)
        lumber = units[0].owner.state.get(2, 0)
        self.phase = (getattr(self, 'phase', 0) + 1) % 4
        if self.phase == 3:
            self.heroes(units, gold, now)
            return
        towers = [u for u in units if u.is_bldg and u.fighter and not u.constructing]
        builders = [u for u in units if u.data.get('Builds') and not u.is_bldg]
        builders.sort(key=lambda u: -max([g for g, _t in self.towers(u)] or [0]))   # strongest builder first (stable)
        top = max([g for u in builders for g, _t in self.towers(u)] or [0])
        if self.phase == 0 and len(towers) >= SOAK_TOWERS and gold >= top:
            # full: demolish the cheapest tower (its Demolish entry) to make room for a stronger one
            towers.sort(key=lambda u: (int(u.data.get('gold', 0) or 0), u.serial))
            import jass
            weak = towers[0]
            for s_ in str(weak.data.get('Trains', '') or '').split(','):
                d = self.N.unit_types.get(jass.rawcode(s_.strip())) if len(s_.strip()) == 4 else None
                if d is not None and 'Demolish' in str(d.get('name') or '') and int(weak.data.get('gold', 0) or 0) < top:
                    self.eng.post_input(self.player, 0, weak.serial, jass.rawcode(s_.strip()))
                    self.log.append((now * 5, 'demolish %s' % weak.data.get('id4')))
                    return
        if self.phase == 0 and len(towers) < SOAK_TOWERS:
            ndet = sum(1 for u in units if u.is_bldg and self.detects(u.data))
            order = builders
            if ndet < SOAK_DETECTORS:          # invisible creeps (Agho) need detectors: builders that make one first
                order = [b for b in builders if any(self.detects(self.N.unit_types[t]) for _g, t in self.towers(b))] + builders
            for b in order:
                if b.build is None and b.order == 0 and not b.paused:
                    best = None
                    for g, tid in self.towers(b):
                        d = self.N.unit_types[tid]
                        if g <= gold and int(d.get('lumber', 0) or 0) <= lumber and \
                                any(int(d.get('weapsOn', 0) or 0) >> i & 1 for i in (0, 1)) and \
                                self.eng.req_ok(b.owner, tid):
                            best = tid
                    if ndet < SOAK_DETECTORS:
                        for g, tid in self.towers(b):
                            if g <= gold and self.detects(self.N.unit_types[tid]) and self.eng.req_ok(b.owner, tid):
                                best = tid
                    if best is not None:
                        p = self.spot(best, b)
                        if p is not None:
                            self.eng.post_input(self.player, 1, b.serial, best, p[0], p[1])
                            self.log.append((now * 5, 'build %s' % self.N.unit_types[best].get('id4')))
                            return
        elif self.phase == 1:
            have = set(u.tid for u in units)
            for b in units:
                if b.is_bldg and not b.constructing and b.data.get('Trains') and not b.tq:
                    for g, tid in self.opts(b, 'Trains', gold, 'builder'):
                        if tid not in have:
                            self.eng.post_input(self.player, 0, b.serial, tid)
                            self.log.append((now * 5, 'train builder %s' % self.N.unit_types[tid].get('id4')))
                            return
        else:
            if getattr(self, 'upgrade_on', False) and self.upgrade(units, gold, now):
                return
            for b in units:
                if b.is_bldg and not b.constructing and b.data.get('Trains') and not b.tq:
                    o = self.opts(b, 'Trains', gold, 'other')
                    if o:
                        self.eng.post_input(self.player, 0, b.serial, o[-1][1])        # the strongest affordable
                        self.log.append((now * 5, 'train %s' % self.N.unit_types[o[-1][1]].get('id4')))
                        return


    # ---------------------------------------------------------------- W8 heroes (exercise heroes.py / spells.py)
    def heroes(self, units, gold, now):
        """one hero action: train a hero type not owned yet, walk an idle hero to the anchor, learn a point, switch
        frost arrows on, or cast the first ready spell at the nearest enemy (unit spell) / its position (point)."""
        import jass
        from .spells import CLASSES, BUTTONS, T_UNIT, T_POINT
        from .orders import ORDER_IDS
        hs = [u for u in units if getattr(u, 'hero', False)]
        have = set(u.tid for u in hs)
        for b in units:
            if b.is_bldg and not b.constructing and b.data.get('Trains') and not b.tq:
                for s_ in str(b.data.get('Trains', '') or '').split(','):
                    s_ = s_.strip()
                    d = self.N.unit_types.get(jass.rawcode(s_)) if len(s_) == 4 else None
                    if d is None or not ('A' <= s_[0] <= 'Z') or jass.rawcode(s_) in have:
                        continue
                    if int(d.get('gold', 0) or 0) <= gold and self.eng.tech_ok(b.owner, jass.rawcode(s_)) and \
                            self.eng.food_ok(b.owner, int(d.get('fused', 0) or 0)):
                        self.eng.post_input(self.player, 0, b.serial, jass.rawcode(s_))
                        self.log.append((now * 5, 'train hero %s' % s_))
                        return
        ab, sp = self.eng.ab, self.eng.sp
        for h in hs:
            if h.cast is not None or h.order != 0:
                continue
            dx, dy = h.x - self.ax, h.y - self.ay
            if dx * dx + dy * dy > 400.0 * 400.0 and self.eng.can_move(h):
                self.eng.post_input(self.player, 1, h.serial, ORDER_IDS['move'], self.ax, self.ay)
                return
            if self.eng.itm.inventory(h) is not None:
                best = None
                for it in self.eng.itm.ground:                  # W8: pick up items near the tower zone
                    if not it.live or it.carrier is not None:
                        continue
                    a2 = (it.x - self.ax) ** 2 + (it.y - self.ay) ** 2
                    if a2 <= 1200.0 ** 2 and self.eng.itm.can_receive(h, it, True):
                        d2 = (it.x - h.x) ** 2 + (it.y - h.y) ** 2
                        if best is None or (d2, it.serial) < best[0]:
                            best = ((d2, it.serial), it)
                if best is not None and getattr(h, 'last_pick', None) is not best[1]:
                    h.last_pick = best[1]
                    self.eng.post_input(self.player, 2, h.serial, ORDER_IDS['smart'], 0.0, 0.0, best[1].serial)
                    self.log.append((now * 5, 'pickup'))
                    return
            if self.hero_learn(h, now):
                return
            if self.hero_cast(h, now):
                return

    def hero_learn(self, h, now):
        """W9R2-5 (extracted from heroes(), same behaviour): learn the first learnable hero ability when a skill point is
        free -> True if the learn order was posted."""
        ab = self.eng.ab
        if h.skill > 0:
            for aid in h.hslots:
                r = ab.rec(aid)
                if r is not None and h.abilities.get(aid, 0) < r['levels']:
                    lvl = h.abilities.get(aid, 0)
                    # the same rule as heroes.learn: the map's HeroAbilityLevelSkip (game constant, 3 in DBZ HA), not a literal 2
                    req = int(r.get('reqLevel', 1) or 0) + (int(r.get('levelSkip', 0) or 0) or self.eng.hero.skip) * lvl
                    if h.hlevel >= req:
                        self.eng.post_input(self.player, 0, h.serial, aid)
                        self.log.append((now * 5, 'learn'))
                        return True
        return False

    HEAL_CODES = ('AHhb', 'AOhw', 'Arej')              # unit spells that restore life: only a hurt target is worth them

    def friendly_target(self, h, aid, code, lv, now, reach, gap):
        """W9R3-4: the ally (or the hero itself) for a friendly unit spell, or None. [inf] an alliance-restricted targs mask
        without the enemy flag is the engine's own cue (ab.targ_ok); an enemy within reach is the 'fight is on' condition."""
        ab = self.eng.ab
        if gap and now - getattr(self, 'cast_at', {}).get((h.serial, aid), -10 ** 9) < gap:
            return None
        fight = False
        for v in self.N.units:
            if v.alive and not v.dead and v.owner.id != 15 and not self.eng.ally(h.owner, v.owner) and \
                    not v.invulnerable and not getattr(v, 'hidden', False) and not getattr(v, 'locust', False) and \
                    (v.x - h.x) ** 2 + (v.y - h.y) ** 2 <= reach * reach:
                fight = True
                break
        heal = code in self.HEAL_CODES
        if not fight and not heal:
            return None
        best = None
        for v in self.N.units:
            if not v.alive or v.dead or getattr(v, 'hidden', False) or getattr(v, 'locust', False):
                continue
            if v is not h and (v.owner.id == 15 or not self.eng.ally(h.owner, v.owner)):
                continue
            if v.is_bldg or getattr(v, 'invisible', 0) or not ab.targ_ok(h, v, lv['targs']):
                continue                                    # an invisible target: engine order() refuses it (W8-8, also for itself)
            d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
            if d2 > reach * reach or (heal and v.life >= 0.8 * v.maxlife):
                continue
            if best is None or (d2, v.serial) < best[0]:
                best = ((d2, v.serial), v)
        return best[1] if best else None

    def hero_cast(self, h, now, reach=None, instant=False, gap=0, friendly=False):
        """W9R2-5 (extracted from heroes(), same behaviour for reach=None): switch frost arrows on, or cast the first ready
        unit / point spell at the nearest enemy; reach=None: enemies inside the anchor's tower zone (1200), else enemies
        within `reach` of the hero; instant=True (W9R2-5 hero maps) also fires ready no-target spells while an enemy is within
        reach (an aura / self buff / nova), the same one at most every `gap` steps. friendly=True (W9R3-4): a unit spell whose
        targs allow no enemy (Inner Fire, Bloodlust, heals) goes to the hero or the nearest allied unit within reach that passes
        targ_ok while an enemy is within reach (heals: only a target below 80 % life), at most every `gap` steps per spell.
        -> True if an order was posted."""
        from .spells import CLASSES, BUTTONS, T_UNIT, T_POINT, T_NONE
        from .orders import ORDER_IDS
        ab, sp = self.eng.ab, self.eng.sp
        for aid, code, lv in ab.abil_list(h):
            if lv is None:
                continue
            if code == 'AHca' and aid not in h.arrow_on:
                self.eng.post_input(self.player, 0, h.serial, ORDER_IDS['coldarrows'])
                return True
            if code not in CLASSES or code in BUTTONS or h.mana < lv['Cost'] or sp.cooling(h, aid):
                continue
            oid = sp.order_id(aid)
            kind = CLASSES[code][0]
            if oid == 0 or kind not in ((T_UNIT, T_POINT, T_NONE) if instant else (T_UNIT, T_POINT)):
                continue
            if kind == T_NONE and gap and now - getattr(self, 'cast_at', {}).get((h.serial, aid), -10 ** 9) < gap:
                continue
            best = None
            for v in self.N.units:
                if not v.alive or v.dead or v.owner.id == self.player or self.eng.ally(h.owner, v.owner):
                    continue
                if v.invulnerable or (kind != T_NONE and not ab.targ_ok(h, v, lv['targs'])):
                    continue
                d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
                if reach is None:
                    ok = (v.x - self.ax) ** 2 + (v.y - self.ay) ** 2 <= 1200.0 ** 2     # inside the tower zone
                else:
                    ok = d2 <= reach * reach
                if ok and (best is None or (d2, v.serial) < best[0]):
                    best = ((d2, v.serial), v)
            if best is None and friendly and kind == T_UNIT and reach is not None:
                v = self.friendly_target(h, aid, code, lv, now, reach, gap * 3 if code not in self.HEAL_CODES else gap)
                if v is not None:
                    self.eng.post_input(self.player, 2, h.serial, oid, 0.0, 0.0, v.serial)
                    if not hasattr(self, 'cast_at'):
                        self.cast_at = {}
                    self.cast_at[(h.serial, aid)] = now
                    self.log.append((now * 5, 'cast %s ally' % code))
                    return True
            if best is None:
                continue
            v = best[1]
            if kind == T_NONE:
                self.eng.post_input(self.player, 0, h.serial, oid)
                if gap:
                    if not hasattr(self, 'cast_at'):
                        self.cast_at = {}
                    self.cast_at[(h.serial, aid)] = now
            elif kind == T_UNIT:
                self.eng.post_input(self.player, 2, h.serial, oid, 0.0, 0.0, v.serial)
            else:
                self.eng.post_input(self.player, 1, h.serial, oid, v.x, v.y)
            self.log.append((now * 5, 'cast %s' % code))
            return True
        return False


SOAK_DETECTORS = 6                                   # detector towers built before the others
SOAK_TOWERS = 120                                    # the soak player stops building at this many towers
SOAK_BONUS = {'AzureTD': 3000, 'BlizzardTD': 150, 'SkibiTD': 1000}     # test gold per 30 s (input kind 3), see SoakPlayer
SOAK_UPGRADES = ('SkibiTD',)
# W9 fan maps (work keys of tools/fanmap_check.py): soak slots; PvP maps need two
SOAK_FAN = {
    'fan_linetowerwars7v114': {'players': [0, 1], 'humans': 2, 'bonus': 0},
    # Wintermaul: the creeps of every defender lane run to the shared exit — all eight lanes need a soak player
    'fan_WintermaulV151': {'players': [0, 1, 2, 3, 4, 5, 6, 7], 'humans': 8, 'bonus': 150},
    'fan_ElementTDv50': {'players': [0], 'humans': 1, 'bonus': 150},
    # W9-2 builder TDs (profiles: tools/jvm/w9_play.py): 'early' = dialogs + governor / builder training before a builder exists
    'fan_WintermaulX10Final': {'players': [0], 'humans': 1, 'bonus': 0, 'early': True},
    'fan_10SkibisCastleTD': {'players': [0], 'humans': 1, 'bonus': 0, 'early': True},
    'fan_GreenTD360': {'players': [0], 'humans': 1, 'bonus': 0, 'early': True},
    'fan_4BurbenogTDv234eLegends': {'players': [0], 'humans': 1, 'bonus': 0, 'early': True},
    'fan_DBZUltimateTDProv53': {'players': [0], 'humans': 1, 'bonus': 150, 'early': True},
}
SOAK_SPOT_TRIES = 4                                   # orders per build spot before the soak player gives it up                          # maps whose soak player upgrades towers (NOTES_skibi §3)


def attach_soak(N, map_name):
    s = SCRIPTS.get(map_name)
    if s is None:
        f = SOAK_FAN.get(map_name)
        if f is None:
            return None
        # W9 fan maps: one soak player per listed slot, anchored at its first builder (set on the first turn)
        out = []
        for pid in f['players']:
            sp = SoakPlayer(N, None, None, f.get('radius', 1280.0), player=pid, bonus=f.get('bonus', 0),
                            early=f.get('early', False))
            sp.upgrade_on = True
            out.append(sp)
        return out[0]
    sp = SoakPlayer(N, s[0], s[1], s[2], bonus=SOAK_BONUS.get(map_name, 0))
    sp.upgrade_on = map_name in SOAK_UPGRADES
    return sp


# ---------------------------------------------------------------- maze scenario (Wintermaul, tools/jvm/maze.py)
# Wintermaul V1.51 with one human: all eight lanes spawn their first wave (40 Scouts `ushd`, hover, no weapon) at 82 s.
# Player 1 (Red) builds with its Element Chooser e000 (flying builder) Heaven Portals h002 (0 gold, 1 lumber, 8 x 8
# cells = 256 x 256, no weapon, invulnerable) as walls — no weapon, so every creep's whole walk can be measured (with
# real towers the wave dies inside the maze: Holy Guard Towers h003 killed 39 of 40):
#   * Red's lane (walkable + buildable x in [-4480, -3200) between cliffs, spawn y 4688, exit y < 1100): a classic
#     zig-zag of MAZE_ROWS walls, 4 pieces each at x = -4352 + 256 k (k 0..4 fill the lane), the gap alternately right
#     (k 4) and left (k 0); the last wall's gap-side piece is ordered at MAZE_LATE_S, while the wave walks (repath).
#   * Blue's lane (x in [-896, 384)): one wall of all 5 pieces at y 1984 = a full block (the map's script does not
#     forbid it; its tips 1 / 4 only warn "never block" / "attackers require at least one square").
MAZE_MAP = 'fan_WintermaulV151'
MAZE_TYPE = 'h002'
MAZE_SIZE = 256.0
MAZE_LUMBER = 40                      # test lumber (input kind 3 with target serial 1; gold: AT-4 / the soak player)
MAZE_ROWS = (3840.0, 3456.0, 3072.0, 2688.0, 2304.0, 1920.0, 1536.0)
MAZE_X0 = -4352.0
MAZE_LATE_S = 86.0
MAZE_BLOCK = (-768.0, 1984.0)         # Blue's lane: x of k 0, y


def maze_towers(block=True):
    """[(x, y, earliest game second)] in build order: Red's walls top-down (the last wall's gap-side piece late),
    then Blue's full block."""
    out, late = [], None
    for i, y in enumerate(MAZE_ROWS):
        ks = [0, 1, 2, 3] if i % 2 == 0 else [4, 3, 2, 1]          # even rows: gap right (k 4), odd: gap left (k 0)
        if i == len(MAZE_ROWS) - 1:
            late = (MAZE_X0 + MAZE_SIZE * ks[-1], y, MAZE_LATE_S)
            ks = ks[:-1]
        out += [(MAZE_X0 + MAZE_SIZE * k, y, 0.0) for k in ks]
    if block:
        out += [(MAZE_BLOCK[0] + MAZE_SIZE * k, MAZE_BLOCK[1], 0.0) for k in range(5)]
    out.append(late)
    return out


class MazePlayer:
    """Scripted player 1 of the maze scenario: test lumber, then the Element Chooser builds maze_towers() one after the
    other, each when it is idle and the piece's time has come. Deterministic: decisions only from the world state at
    turn boundaries."""

    def __init__(self, N, block=True, player=PLAYER):
        import jass
        self.N = N
        self.eng = N.eng
        self.player = player
        self.tid = jass.rawcode(MAZE_TYPE)
        self.towers = maze_towers(block)
        self.started = False
        self.next = 0
        self.log = []
        self.eng.vm.on_turn.append(self.on_turn)

    def chooser(self):
        for u in self.N.units:
            if u.owner.id == self.player and u.alive and not u.dead and \
                    MAZE_TYPE in [s.strip() for s in str(u.data.get('Builds', '')).split(',')]:
                return u
        return None

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if not self.started:
            self.eng.post_input(self.player, 3, 0, MAZE_LUMBER, 0.0, 0.0, 1)
            self.started = True
            return
        b = self.chooser()
        if b is None or b.order != 0 or b.build is not None or self.next >= len(self.towers):
            return
        x, y, t = self.towers[self.next]
        if now < int(round(t * SPS)):
            return
        self.eng.post_input(self.player, 1, b.serial, self.tid, x, y)
        self.log.append((now * 5, 'build %d at %g,%g' % (self.next, x, y)))
        self.next += 1


