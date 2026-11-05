#!/usr/bin/env python3
"""Ground pathing for the W5 engine: the map's wpm grid (32-unit cells, NOTES_units §2) + building footprints,
A* (8 neighbours, integer costs, deterministic ties) with line-of-sight smoothing. Udon-friendly: flat int lists,
no recursion, one binary heap of packed integer keys, generation stamps instead of clearing arrays.

Cell (cx, cy) covers [ox + 32 cx, ox + 32 (cx + 1)) x [oy + 32 cy, ...); row 0 = south (NOTES_units §2 cell index =
(x - map min x) / 32). wpm flags: 0x02 not walkable, 0x04 not flyable, 0x08 not buildable (tools/w3map.parse_wpm).
Every loop iteration charges `wk` (the work counter the Udon port charges against its frame budget).
"""
import math
import os

from .pathab import PathAB
from .pathfield import PathField
from . import pathfield as PF
from . import flowfield as FF

CELL = 32.0
NO_WALK, NO_FLY, NO_BUILD = 0x02, 0x04, 0x08
# EXT-ow3 SPEC-0020 / NOTES_pathing 3.2 [obs 0x6f6648a0 .. 0x6f664900, both versions]: the ground path type's terrain mask is
# 0x06 -- an unwalkable OR an unflyable cell blocks a ground mover.  NO_WALK / NO_FLY stay the wpm bit meanings (buildable keeps 0x02).
NO_GROUND = NO_WALK | NO_FLY
# EXT-ow3 SPEC-0020 review: the 0x06 ground mask is NOT confirmed by an oracle run yet, so it is switch-gated; default OFF =
# the old 0x02-only rule.  The switch lives in flowfield (FF.GROUND_UNFLYABLE, env W3_GROUND_UNFLYABLE=1) so the flat A*, PATH_AB,
# the flow fields and the field jobs read one value; the twin is W3Pathing.GROUND_UNFLYABLE / groundUnflyableOn.
# Pending: tools/oracle/probes/pathfind_rules.j (docs/decomp/port/EXT_Pathing.md sec. 6.1).


def ground_mask():
    """the terrain bits that block a ground mover: 0x06 with the switch ON, else the old 0x02 rule (EXT-ow3 SPEC-0020 review)."""
    return FF.ground_mask()


COST_S, COST_D = 10, 14             # straight / diagonal step (integer octile)
GOAL_RING = 16                      # blocked goal: nearest walkable cell within 16 cells
CHASE_EXPAND = 2000                 # node limit when chasing a unit (A37)
RESULT_CACHE = 32                   # A27d: whole find_path results by (start point, goal point, limit, grid version)
PATH_CACHE = 32                     # A27c: A* results by (start, goal, limit, grid version), replaced round robin
MAX_EXPAND = 40000                  # single-A* node limit; CLrPath's own pop budgets are 700 fine / 400 coarse
                                    # (NIpse_CLrPath_p1 §2/§4), not a like-for-like node limit for this search
NB = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1))   # fixed neighbour order
LOS_MEMO = 8192                     # P-SPAWN: direct-mapped line-of-sight verdict memo of the smoothing loop
SHARE_N = 32                        # PB-SHARE: served routes kept for sharing (round robin)
SHARE_R = 3                         # PB-SHARE: a start within this many cells of a kept route's start shares it
SHARE_RP = 1                        # PB-SHARE: ... or within this many cells of a point of its polyline (fit: 1; BUGS PB-SHARE)
SHARE_DLOG = 64                     # PB-SHARE: grid versions whose changed-cell rectangle is remembered (a kept route older than that is void)
SHARE_SCAN = 4                      # PB-SHARE: route points tried (from the nearest segment's end) for the first one the asker sees
SHARE_MIN_EXP = 300                 # PB-SHARE: only routes of searches that needed at least this many A* expansions are kept (fit plateau 200 .. 400)
SEAL_AT = 1024                      # PERF-MELEE: a search that still has not found its goal asks every this many expansions whether it can stop
BALL_CAP = 900                      # PERF-MELEE: the ball test of a sealed goal looks at no more cells than this
SEAL_CAP = 600                      # PERF-MELEE: ... a flood fill from the goal cell that ends within this many cells without the start = sealed (search ends as at the limit)
TAIL_ON = False                     # PERF-MELEE: route tails are NOT in play: an answer from the tail differs from a fresh A* in equal-cost ties / the connector and changes games (Castle Fight round 406 -> 593 s). Tests switch fast_ok on by hand
TAIL_N = 8                          # PERF-MELEE: long routes kept for the units that walk on along them
TAIL_R = 3                          # PERF-MELEE: a start within this many cells of a kept route's cell may join it
TAIL_HEAD = 12                      # PERF-MELEE: a kept search that hit its node limit serves starts at its first TAIL_HEAD route cells only
TAIL_MIN = 3000                     # PERF-MELEE: only routes of searches that needed at least this many expansions are kept
KEY_H = 1 << 20                     # heap key = (f * 2^20 + h) * 2^20 + cell  (cell < 2^20, h < 2^20)
GATE_COST = 1                       # WAYGATE-PORT: a gate edge costs 1 against 24 per walked cell [obs NOTES_pathing 1.2] = 0.42 of a port step (10); rounded to 1
MARK_X = -1.0e9                     # WAYGATE-PORT: route marker = (MARK_X, slot) [the original: x 0.0, y = slot, NOTES_pathing 1.3]
GATE_WAIT = 20                      # follower: mover updates held when the landing has no free spot [obs 0x6f67fdc0]
AUTO = os.environ.get('W3_PATH_AB') not in ('0', '1')   # W3_PATH_AB unset: the per-map rule below; 0 / 1 force the switch for every map (measurements, the old-model tests)
PATH_AB = os.environ.get('W3_PATH_AB') == '1'     # the switch for a Grid built without an explicit `ab` and what the tests force; the engine decides per map: ab_for()
#                                   PATH-AB: ground path finding as the original does it (coarse search A + fine search B, pathab.py); False = the flat A*
PATH_AB_ALL = False                 # BUGS PATH-AB-5: the two-level finder on for EVERY map (the default of the later step; the C# twin: const PATH_AB_ALL of W3PathAB.cs)
PATH_AB_MELEE = True                # ... on for the melee maps (the map's script reaches MeleeStartingUnits, melee_program()); off elsewhere
GATE_SLOTS = 256                    # the pathing map's gate table: slot 0 = none, 1 .. 255 [obs 0x6f689bf0]


def _seg_box(ax, ay, bx, by, x0, y0, x1, y1):
    """PB-SHARE: does the segment a -> b touch the box (Liang-Barsky slabs)?"""
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0.0:
            if q < 0.0:
                return False
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
    return True


def melee_program(prog):
    """BUGS PATH-AB-5: is the map a melee map? True when the function MeleeStartingUnits (Blizzard.j) is reachable from main / config / the global
    initialisers over calls (CALL_J) and function constants (PUSH_C: trigger actions). Pure function of the compiled program, so both VMs agree
    (the C# twin: W3Vm.MeleeProgram over the pack's JCOD / JFUN / JINI, same rule: the functions in index order, a function ends where the next begins)."""
    from . import compiler as C
    fun = prog.functions
    nf = len(fun)
    target = -1
    for f in fun:
        if f.name == 'MeleeStartingUnits':
            target = f.index
            break
    if target < 0:
        return False
    call_j, push_c = C.OPS.index('CALL_J'), C.OPS.index('PUSH_C')
    seen = [False] * nf
    todo = []
    for f in fun:
        if f.name in ('main', 'config'):
            seen[f.index] = True
            todo.append(f.index)
    for fi in prog.ginit:
        if not seen[fi]:
            seen[fi] = True
            todo.append(fi)
    nops = len(prog.ops)
    while todo:
        fi = todo.pop()
        if fi == target:
            return True
        end = fun[fi + 1].entry if fi + 1 < nf else nops
        for pc in range(fun[fi].entry, end):
            op = prog.ops[pc]
            nxt = -1
            if op == call_j:
                nxt = prog.A[pc]
            elif op == push_c:
                nxt = prog.B[pc] - 1
            if 0 <= nxt < nf and not seen[nxt]:
                seen[nxt] = True
                todo.append(nxt)
    return False


def melee_on():
    """BUGS PATH-AB-5: does a melee map run the two-level finder in this process? (tests choose their expectations by it)"""
    return PATH_AB if (PATH_AB or not AUTO) else (PATH_AB_ALL or PATH_AB_MELEE)


def ab_for(prog):
    """BUGS PATH-AB-5: the switch for the map of this program: a forced PATH_AB (env / test) wins, else PATH_AB_ALL, else the melee rule."""
    if PATH_AB or not AUTO:
        return PATH_AB
    return PATH_AB_ALL or (PATH_AB_MELEE and melee_program(prog))


class Grid(PathAB, PathField):
    def __init__(self, w, h, flags, ox, oy, ab=None):
        self.w, self.h = w, h
        self.ab = PATH_AB if ab is None else ab      # BUGS PATH-AB-5: the switch of this map (Engine: ab_for(program))
        self.ox, self.oy = float(ox), float(oy)
        self.flags = bytearray(flags)
        self.gmask = ground_mask()       # EXT-ow3 SPEC-0020 review: the ground terrain mask, read once per grid (not per cell; W3 twins: gGMask)
        n = w * h
        self.occ_walk = [0] * n          # building footprints blocking walking (counter per cell)
        self.occ_build = [0] * n         # footprints blocking building
        self.version = 0                 # bumped on every footprint change (movers repath)
        self.g = [0] * n
        self.stamp = [0] * n             # g / parent valid when stamp == gen
        self.closed = [0] * n            # closed when == gen
        self.parent = [0] * n
        self.gen = 0
        self.fast_ok = False             # PERF-MELEE: set by engine.repath for the units of user slots (human or bot): route tails
        self.tails = []                  # PERF-MELEE: [(goal x, y, limit, version), cells, expansions]
        self.tl_i = 0
        self.tail_hits = 0
        self.seal = None                 # PERF-MELEE: flood-fill stamps of sealed(), made on first use
        self.seal_gen = 0
        self.wk = 0
        self.fld_init()                  # PATH-FIELD: flow fields for creep waves (pathfield.py; nothing happens while PF.PATH_FIELD is off)
        self.last_limited = False        # the last search stopped at its node limit
        self.last_reached = True         # the last search reached the goal cell
        self.last_refused = False        # PATH-AB step 3: the gate of the throttle / quota refused the last request (no search was made)
        self.ab_cb = None                # PATH-AB step 3: the requester (engine): pb_gate(kind) / pb_charge(kind, pops); None = no throttle (tests, tools)
        self.last_exp = 0                # A-PB1: the last search's A* expansions (cached with it: its budget cost)
        self.pcache = []                 # [(key, cells, limited)] (A27c)
        self.pc_i = 0
        self.rcache = []                 # [(key, waypoints, limited, reached)] (A27d)
        self.rc_i = 0
        self.lmemo = [None] * LOS_MEMO   # P-SPAWN: slot -> (version, x0, y0, x1, y1, verdict)
        self.shr = []                    # PB-SHARE: [[ver, goal cell x, y, goal x, y, start x, y, points, exp, length, exact, ver0]] (ver = last validated)
        self.shr_i = 0
        self.dlog = [None] * SHARE_DLOG  # PB-SHARE: version % SHARE_DLOG -> (version, cx0, cy0, cx1, cy1): the cells that version blocked (empty: x0 > x1)
        self.shr_xver = 0                # statistics: shared routes taken after a grid version bump (validity check passed)
        self.shr_hits = 0                # statistics (harness / tests): requests that took a shared route / routes stored
        self.shr_stored = 0
        self.los_hits = 0                # memo statistics (harness / tests)
        self.los_misses = 0
        self.gslot = None                # WAYGATE-PORT: the gate slot of every cell (+6 byte of the original's cell), allocated with the first gate
        self.gtab = {}                   # slot -> [active, landing cell x, landing cell y, cx0, cy0, cx1, cy1] (the area rectangle, cells)
        self.gact = 0                    # number of active gates (the original's counter): 0 = the A* never looks at the table
        self.st_search = 0               # PATH-AB statistics: searches served / pops (A* expansions) charged, both finders
        self.st_pops = 0
        self.st_asearch = self.st_apops = self.st_bsearch = self.st_bpops = 0     # PATH-AB: searches / pops of A and B
        if self.ab:
            self.ab_build()              # PATH-AB: the coarse map is built at map load; footprints rebuild their rectangle, SetTerrainPathable does not

    # ---------------------------------------------------------- cells
    def cell_of(self, x, y):
        return int(math.floor((x - self.ox) / CELL)), int(math.floor((y - self.oy) / CELL))

    def inside(self, cx, cy):
        return 0 <= cx < self.w and 0 <= cy < self.h

    def walkable(self, cx, cy):
        if not (0 <= cx < self.w and 0 <= cy < self.h):
            return False
        i = cy * self.w + cx
        return not (self.flags[i] & self.gmask) and self.occ_walk[i] == 0

    def flyable(self, cx, cy):
        """wpm 0x04 clear [obs fan_linetowerwars7v114: the 24-cell wall between two lanes is 0xce, the builder (movetp fly)
        never crosses it]; outside the grid: flyable (the map border is not this test's business)."""
        if not (0 <= cx < self.w and 0 <= cy < self.h):
            return True
        return not (self.flags[cy * self.w + cx] & NO_FLY)

    def buildable(self, cx, cy):
        if not (0 <= cx < self.w and 0 <= cy < self.h):
            return False
        i = cy * self.w + cx
        return not (self.flags[i] & (NO_WALK | NO_BUILD)) and self.occ_walk[i] == 0 and self.occ_build[i] == 0

    def center(self, cx, cy):
        return self.ox + CELL * cx + 16.0, self.oy + CELL * cy + 16.0

    def add_footprint(self, cells, sign):
        """cells: [(cx, cy, mask)] mask bit 1 walk, 4 build (unitdata.footprint)."""
        w = self.w
        for cx, cy, m in cells:
            self.wk += 1
            if 0 <= cx < self.w and 0 <= cy < self.h:
                i = cy * w + cx
                if m & 1:
                    self.occ_walk[i] += sign
                if m & 4:
                    self.occ_build[i] += sign
        self.version += 1
        if sign < 0:
            self.fld_free = self.version        # PATH-FIELD: cells may be free now (a structure died): the fields older than this are not shortest any more
        self.delta(*self._blocked_rect(cells, sign))
        if self.ab_st is not None:
            self.ab_rect(cells)

    def _blocked_rect(self, cells, sign):
        """PB-SHARE: bounding cell rectangle of the cells a footprint change made unwalkable (sign +1, walk mask); empty otherwise."""
        if sign <= 0:
            return 1, 1, 0, 0
        x0 = y0 = 1 << 30
        x1 = y1 = -(1 << 30)
        for cx, cy, m in cells:
            if m & 1 and 0 <= cx < self.w and 0 <= cy < self.h:
                if cx < x0:
                    x0 = cx
                if cx > x1:
                    x1 = cx
                if cy < y0:
                    y0 = cy
                if cy > y1:
                    y1 = cy
        return x0, y0, x1, y1

    def delta(self, cx0, cy0, cx1, cy1):
        """PB-SHARE: records the cells the version bump just made (self.version) changed."""
        self.dlog[self.version % SHARE_DLOG] = (self.version, cx0, cy0, cx1, cy1)

    # ---------------------------------------------------------- waygates (NOTES_pathing 1; WAYGATE-PORT)
    def gate_rect(self, x, y, a, b):
        """the cells of the area: those whose centre lies in the rectangle centre +- (a, b), clamped to the map [obs runtime 1.26a
        waygate_route S2 / S3 with a = b = DataA / 2 = 200: the first area cell on the route is the one centred at u 260, a cell
        whose centre is 2 units outside is not an entry; the static reading of 0x6f665a00 (floor at both corners, +- DataA) does not fit]."""
        cx0 = max(int(math.ceil((x - a - self.ox) / CELL - 0.5)), 0)
        cy0 = max(int(math.ceil((y - b - self.oy) / CELL - 0.5)), 0)
        cx1 = min(int(math.floor((x + a - self.ox) / CELL - 0.5)), self.w - 1)
        cy1 = min(int(math.floor((y + b - self.oy) / CELL - 0.5)), self.h - 1)
        return cx0, cy0, cx1, cy1

    def gate_add(self, rect):
        """ability add: the first free slot >= 1 (0 = none, table full) is written over the rectangle (a later registration wins
        the common cells); inactive, landing cell (0, 0) [obs 0x6f080800, 0x6f080b10]."""
        slot = 0
        for k in range(1, GATE_SLOTS):
            if k not in self.gtab:
                slot = k
                break
        if slot == 0:
            return 0
        if self.gslot is None:
            self.gslot = bytearray(self.w * self.h)
        cx0, cy0, cx1, cy1 = rect
        w = self.w
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                self.wk += 1
                self.gslot[cy * w + cx] = slot
        self.gtab[slot] = [False, 0, 0, cx0, cy0, cx1, cy1]
        self.ab_rebuild(cx0, cy0, cx1, cy1)               # PATH-AB: the gate slot byte lives in the coarse level-0 cells
        return slot

    def gate_remove(self, slot, rect):
        """ability / unit removal: slot 0 over the rectangle of the unit's CURRENT position (not the registration's), the slot is
        freed; an active gate leaves the counter [obs 0x6f458f70, 0x6f080850]."""
        e = self.gtab.pop(slot, None)
        if e is None:
            return
        cx0, cy0, cx1, cy1 = rect
        w = self.w
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                self.wk += 1
                self.gslot[cy * w + cx] = 0
        self.ab_rebuild(cx0, cy0, cx1, cy1)
        if e[0]:
            self.gact -= 1
            self.gate_bump()

    def gate_set(self, slot, active, dcx, dcy):
        """WaygateActivate / WaygateSetDestination: the table entry; a change that an active gate shows to the search bumps the
        grid version (path caches, shared routes and the line-of-sight memo are keyed by it; no cell is blocked: empty delta)."""
        e = self.gtab.get(slot)
        if e is None:
            return
        was = e[0]
        if active is not None and active != was:
            e[0] = active
            self.gact += 1 if active else -1
        if dcx is not None and (e[1] != dcx or e[2] != dcy):
            e[1], e[2] = dcx, dcy
            if e[0]:
                self.gate_bump()
        if e[0] != was:
            self.gate_bump()

    def gate_bump(self):
        self.version += 1
        self.fld_free = self.version        # PATH-FIELD: a gate opens / closes cells to the search (no field while a gate is active; the fields before it are void)
        self.delta(1, 1, 0, 0)

    def gate_point(self, slot):
        """the landing point of a slot: the cell's lower corner (WaygateGetDestination of the real game returns multiples of 32
        from the map minimum, NOTES_pathing 1.11)."""
        e = self.gtab[slot]
        return self.ox + CELL * e[1], self.oy + CELL * e[2]

    def gate_open(self, slot):
        e = self.gtab.get(slot)
        return e is not None and e[0]

    def gate_line_hit(self, x0, y0, x1, y1):
        """does the segment touch the area of an active gate (grown by one cell)? A search whose straight line avoids every area
        cannot use a gate (a gate cell off the line has f above the goal's)."""
        for e in self.gtab.values():
            if e[0] and _seg_box(x0, y0, x1, y1, self.ox + CELL * (e[3] - 1), self.oy + CELL * (e[4] - 1),
                                 self.ox + CELL * (e[5] + 2), self.oy + CELL * (e[6] + 2)):
                return True
        return False

    # ---------------------------------------------------------- line of sight
    def los(self, x0, y0, x1, y1):
        """all cells crossed by the segment are walkable (grid traversal; at an exact corner crossing both side
        cells must be walkable)."""
        cx, cy = self.cell_of(x0, y0)
        ex, ey = self.cell_of(x1, y1)
        if not self.walkable(ex, ey):
            return False
        dx, dy = x1 - x0, y1 - y0
        sx = 1 if dx > 0 else -1
        sy = 1 if dy > 0 else -1
        if dx != 0:
            nxb = self.ox + CELL * (cx + (1 if dx > 0 else 0))
            tmx = (nxb - x0) / dx
            tdx = CELL / abs(dx)
        else:
            tmx = tdx = float('inf')
        if dy != 0:
            nyb = self.oy + CELL * (cy + (1 if dy > 0 else 0))
            tmy = (nyb - y0) / dy
            tdy = CELL / abs(dy)
        else:
            tmy = tdy = float('inf')
        n = abs(ex - cx) + abs(ey - cy)
        while n > 0:
            self.wk += 1
            if tmx < tmy:
                cx += sx
                tmx += tdx
                n -= 1
            elif tmy < tmx:
                cy += sy
                tmy += tdy
                n -= 1
            else:                                   # exact corner: both side cells, then the diagonal
                if not self.walkable(cx + sx, cy) or not self.walkable(cx, cy + sy):
                    return False
                cx += sx
                cy += sy
                tmx += tdx
                tmy += tdy
                n -= 2
            if not self.walkable(cx, cy):
                return False
        return True

    def los_memo(self, x0, y0, x1, y1):
        """P-SPAWN (performance, same verdict): los() is a pure function of the two points and the grid; the creeps of a spawn
        blob share the tails of their smoothed routes, so the same segments between cell centres come again. Direct-mapped
        table keyed by the exact points and the footprint version (a footprint change makes every entry stale). A hit charges
        no work (the Udon twin charges nothing either)."""
        v = (x0 * 3.1 + y0 * 5.3 + x1 * 7.7 + y1 * 11.9)
        slot = int(math.floor(v)) & (LOS_MEMO - 1)
        e = self.lmemo[slot]
        if e is not None and e[0] == self.version and e[1] == x0 and e[2] == y0 and e[3] == x1 and e[4] == y1:
            self.los_hits += 1
            return e[5]
        self.los_misses += 1
        r = self.los(x0, y0, x1, y1)
        self.lmemo[slot] = (self.version, x0, y0, x1, y1, r)
        return r

    # ---------------------------------------------------------- A*
    @staticmethod
    def heur(ax, ay, bx, by):
        dx = abs(ax - bx)
        dy = abs(ay - by)
        return COST_S * (dx if dx > dy else dy) + (COST_D - COST_S) * (dy if dx > dy else dx)

    def astar_cached(self, sx, sy, gx, gy, limit):
        """A27c: a wave's units share start and goal cells — the search result only depends on them, the limit and
        the grid version."""
        key = (sx, sy, gx, gy, limit, self.version)
        for k, cells, lim, ex in self.pcache:
            if k == key:
                self.last_limited = lim
                self.last_exp = ex
                return cells
        tl = self.tail_find(sx, sy, gx, gy, limit)
        if tl is not None:
            return tl
        cells = self.astar(sx, sy, gx, gy, limit)
        if self.fast_ok and self.last_exp >= TAIL_MIN and not self.gact and cells:
            tk = (gx, gy, limit, self.version)
            te = (tk, cells, self.last_exp, self.last_limited)
            if len(self.tails) < TAIL_N:
                self.tails.append(te)
            else:
                self.tails[self.tl_i] = te
                self.tl_i = (self.tl_i + 1) % TAIL_N
        e = (key, cells, self.last_limited, self.last_exp)
        if len(self.pcache) < PATH_CACHE:
            self.pcache.append(e)
        else:
            self.pcache[self.pc_i] = e
            self.pc_i = (self.pc_i + 1) % PATH_CACHE
        return cells

    def tail_find(self, sx, sy, gx, gy, limit):
        """PERF-MELEE [inf, invisible]: a unit that walks a long route in pieces (cut_piece) searches the rest of the same route
        again at every piece, and so does every unit of its group from a cell near it. The optimal route from a cell on / near
        the cells of a kept search with the same goal, limit and grid version is the rest of that route (sub-path optimality;
        equal-cost alternatives may differ): the rest of the kept cells from the LAST one within TAIL_R cells of the start cell that
        a connector (greedy 8-neighbour steps, no corner cutting) reaches from the start is the answer, its expansions the kept
        search's, scaled by the share of the route left. A kept search that hit its node limit serves only starts near the route's
        beginning (the same node window), never its end (that is a new search). No gate in play."""
        if not self.fast_ok or not self.tails or self.gact or not self.walkable(sx, sy):
            return None
        ver = self.version
        for tk, cells, ex, lim in self.tails:
            if tk[0] != gx or tk[1] != gy or tk[2] != limit or tk[3] != ver:
                continue
            n = len(cells)
            self.wk += (n >> 3) + 1
            lo = 0
            if lim:
                hi = TAIL_HEAD if TAIL_HEAD < n - TAIL_HEAD else n - TAIL_HEAD
                if hi < 1:
                    continue
            else:
                hi = n - 1                       # the last cell is the goal itself
            i = hi
            con = None
            while i >= lo:
                cx, cy = cells[i]
                ddx, ddy = cx - sx, cy - sy
                if -TAIL_R <= ddx <= TAIL_R and -TAIL_R <= ddy <= TAIL_R:
                    con = self.tail_connect(sx, sy, cx, cy)
                    if con is not None:
                        break
                i -= 1
            if con is None:
                continue
            out = con + cells[i:]
            self.last_limited = lim
            self.last_exp = ex if lim else max(1, ex * (len(out) - 1) // max(1, n - 1))
            self.tail_hits += 1
            return out
        return None

    def tail_connect(self, sx, sy, tx, ty):
        """PERF-MELEE: the cells from the start to the target cell (both excluded for the target) by greedy 8-neighbour steps -> [start,
        ...] ending next to the target, or None when a step is blocked."""
        out = [] if sx == tx and sy == ty else [(sx, sy)]
        cx, cy = sx, sy
        while cx != tx or cy != ty:
            ax = tx - cx
            ay = ty - cy
            dx = 1 if ax > 0 else (-1 if ax < 0 else 0)
            dy = 1 if ay > 0 else (-1 if ay < 0 else 0)
            if dx and dy:
                if self.walkable(cx + dx, cy + dy) and self.walkable(cx + dx, cy) and self.walkable(cx, cy + dy):
                    cx += dx
                    cy += dy
                elif (ax if ax > 0 else -ax) >= (ay if ay > 0 else -ay) and self.walkable(cx + dx, cy):
                    cx += dx
                elif self.walkable(cx, cy + dy):
                    cy += dy
                elif self.walkable(cx + dx, cy):
                    cx += dx
                else:
                    return None
            elif dx:
                if not self.walkable(cx + dx, cy):
                    return None
                cx += dx
            else:
                if not self.walkable(cx, cy + dy):
                    return None
                cy += dy
            if cx != tx or cy != ty:
                out.append((cx, cy))
        return out

    def astar(self, sx, sy, gx, gy, limit=MAX_EXPAND):
        """cells -> list of cells start..end (end = goal, or the reached cell closest to it by heuristic, ties
        lower g then lower index). The start cell is expanded even if blocked (a unit inside a new footprint
        walks out)."""
        import heapq
        w = self.w
        self.gen += 1
        gen = self.gen
        s = sy * w + sx
        if s < 0 or s >= w * self.h:
            self.last_limited = False
            self.last_reached = False
            self.last_exp = 0
            return []                                 # a start cell outside the grid: no path [obs NOTES_pathing 2.3]
        goal = gy * w + gx
        g, stamp, closed, parent = self.g, self.stamp, self.closed, self.parent
        gs = self.gslot if self.gact else None          # WAYGATE-PORT: only with an active gate does the search look at the table
        g[s] = 0
        stamp[s] = gen
        parent[s] = -1
        h0 = self.heur(sx, sy, gx, gy)
        heap = [((h0 * KEY_H + h0) * KEY_H) + s]
        best, best_h, best_g = s, h0, 0
        expanded = 0
        seal_res = 0                              # PERF-MELEE: 0 not asked yet, 1 the goal is sealed off, 2 it is not
        self.last_exp = 0
        self.last_limited = False
        while heap:
            key = heapq.heappop(heap)
            c = key % KEY_H
            if closed[c] == gen:
                continue
            closed[c] = gen
            self.wk += 1
            expanded += 1
            self.last_exp = expanded
            if c == goal:
                best = c
                break
            cx, cy = c % w, c // w
            hc = self.heur(cx, cy, gx, gy)
            gc = g[c]
            if hc < best_h or (hc == best_h and (gc < best_g or (gc == best_g and c < best))):
                best, best_h, best_g = c, hc, gc
            if expanded >= limit:
                self.last_limited = True
                break
            if expanded % SEAL_AT == 0 and gs is None and seal_res != 2:
                if seal_res == 0:
                    if self.seal is None:
                        self.seal = [0] * (w * self.h)
                    self.seal_gen += 1
                    seal_res = 1 if self.pocket(goal) else 2
                if seal_res == 1 and self.ball_done(goal, best_h):
                    self.last_limited = True      # PERF-MELEE: the goal is sealed off and no cell nearer to it than the best one can still turn up:
                    self.last_exp = limit         # the node limit would end the search with this very answer 38000 expansions later
                    break
            for dx, dy in NB:
                nx, ny = cx + dx, cy + dy
                if not self.walkable(nx, ny):
                    continue
                if dx and dy and (not self.walkable(cx + dx, cy) or not self.walkable(cx, cy + dy)):
                    continue                        # no corner cutting
                n = ny * w + nx
                if closed[n] == gen:
                    continue
                ng = gc + (COST_D if dx and dy else COST_S)
                if stamp[n] == gen and g[n] <= ng:
                    continue
                stamp[n] = gen
                g[n] = ng
                parent[n] = c
                hn = self.heur(nx, ny, gx, gy)
                heapq.heappush(heap, ((ng + hn) * KEY_H + hn) * KEY_H + n)
                self.wk += 1
            if gs is not None and gs[c]:
                # a gate edge [obs NOTES_pathing 1.2]: every cell of the area leads to the landing cell (walkable, not next to this
                # cell) for GATE_COST; relaxed like a step (strict improvement)
                ge = self.gtab.get(gs[c])
                if ge is not None and ge[0] and max(abs(ge[1] - cx), abs(ge[2] - cy)) > 1 and self.walkable(ge[1], ge[2]):
                    n = ge[2] * w + ge[1]
                    ng = gc + GATE_COST
                    if closed[n] != gen and not (stamp[n] == gen and g[n] <= ng):
                        stamp[n] = gen
                        g[n] = ng
                        parent[n] = c
                        hn = self.heur(ge[1], ge[2], gx, gy)
                        heapq.heappush(heap, ((ng + hn) * KEY_H + hn) * KEY_H + n)
                        self.wk += 1
        out = []
        c = best
        while c >= 0:
            out.append((c % w, c // w))
            if c == s:
                self.wk += 1
                break
            p = parent[c]
            if gs is not None and max(abs(p % w - c % w), abs(p // w - c // w)) > 1:
                out.append((-gs[p], -1))              # the edge into c is a gate crossing: P, marker, c
            c = p
            self.wk += 1
        out.reverse()
        return out

    def pocket(self, n0):
        """PERF-MELEE: is the walkable cell n0 in a pocket the running search can never reach? A breadth-first flood over the cells the A*
        walks (8 neighbours, no corner cutting) that ends within SEAL_CAP cells without touching a cell this search has expanded (those, the
        start among them, are reachable). A verified pocket is stamped 2 * seal_gen + 1, a failed flood 2 * seal_gen (the same region is
        not flooded twice in one search)."""
        w = self.w
        if not self.walkable(n0 % w, n0 // w):
            return False
        sl, closed, gen = self.seal, self.closed, self.gen
        pm = 2 * self.seal_gen
        q = [n0]
        sl[n0] = pm
        qi = 0
        ok = True
        while qi < len(q):
            c = q[qi]
            qi += 1
            self.wk += 1
            if closed[c] == gen or qi > SEAL_CAP:
                ok = False
                break
            cx, cy = c % w, c // w
            for dx, dy in NB:
                nx, ny = cx + dx, cy + dy
                if not self.walkable(nx, ny):
                    continue
                if dx and dy and (not self.walkable(cx + dx, cy) or not self.walkable(cx, cy + dy)):
                    continue
                n = ny * w + nx
                if sl[n] >= pm:
                    continue
                sl[n] = pm
                q.append(n)
        if ok:
            for c in q:
                sl[c] = pm + 1
        return ok

    def ball_done(self, goal, best_h):
        """PERF-MELEE [exact]: every walkable cell whose heuristic to the goal is <= the best one's is in a pocket the search can never reach
        or already expanded: the unexpanded rest cannot improve (h, g, index) of the best cell, so the search result is final. Gives up
        for a ball of more than BALL_CAP cells."""
        w = self.w
        r = best_h // COST_S
        if (2 * r + 1) * (2 * r + 1) > BALL_CAP:
            return False
        gx, gy = goal % w, goal // w
        sl, closed, gen = self.seal, self.closed, self.gen
        pm = 2 * self.seal_gen
        for dy in range(-r, r + 1):
            ady = dy if dy >= 0 else -dy
            for dx in range(-r, r + 1):
                adx = dx if dx >= 0 else -dx
                hh = COST_S * (adx if adx > ady else ady) + (COST_D - COST_S) * (ady if adx > ady else adx)
                if hh > best_h:
                    continue
                self.wk += 1
                x, y = gx + dx, gy + dy
                if not self.walkable(x, y):
                    continue
                n = y * w + x
                if sl[n] == pm + 1 or closed[n] == gen:
                    continue
                if sl[n] == pm or not self.pocket(n):
                    return False
        return True

    def nearest_walkable(self, gx, gy, rmax=GOAL_RING):
        """closest walkable cell by square rings around (gx, gy) (ring r, then octile distance, then y, then x)."""
        for r in range(1, rmax + 1):
            best = None
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    self.wk += 1
                    if self.walkable(gx + dx, gy + dy):
                        k = (self.heur(0, 0, dx, dy), dy, dx)
                        if best is None or k < best[0]:
                            best = (k, gx + dx, gy + dy)
            if best is not None:
                return best[1], best[2]
        return None

    def find_path(self, x0, y0, x1, y1, limit=MAX_EXPAND, tgt=None, cls=0, rt=None):
        """A27d (performance, same result): find_path is a function of its points, the limit and the grid (version) —
        a wave's units ask from the same spawn point to the same goal (Wintermaul: 459 calls, 25 distinct per 120 s);
        the line-of-sight smoothing was nearly all of the path work."""
        if self.ab:
            return self.ab_find(x0, y0, x1, y1, tgt, cls, rt)       # PATH-AB: no result cache (NOTES_pathing 2.13 item 3): every request is a search under the throttle / quota; rt = the mover's route object (step 4)
        key = (x0, y0, x1, y1, limit, self.version)
        for k, pts, lim, rch, ex in self.rcache:
            if k == key:
                self.last_limited = lim
                self.last_reached = rch
                self.last_exp = ex
                return list(pts)
        pts = self._find_path(x0, y0, x1, y1, limit)
        e = (key, list(pts), self.last_limited, self.last_reached, self.last_exp)
        if len(self.rcache) < RESULT_CACHE:
            self.rcache.append(e)
        else:
            self.rcache[self.rc_i] = e
            self.rc_i = (self.rc_i + 1) % RESULT_CACHE
        return pts

    # ---------------------------------------------------------- PB-SHARE
    def share_store(self, x0, y0, gx, gy, pts, exp):
        """PB-SHARE [inf]: keeps the full smoothed route of a search that needed >= SHARE_MIN_EXP A* expansions (start point,
        goal point of the request, grid version) for the units that ask for the same goal cell from nearby."""
        if exp < SHARE_MIN_EXP or not pts or self.gact:
            return                                              # WAYGATE-PORT: with an active gate a route may carry a marker: no sharing
        gcx, gcy = self.cell_of(gx, gy)
        tot = 0.0
        px, py = x0, y0
        for wx, wy in pts:
            dx, dy = wx - px, wy - py
            tot += math.sqrt(dx * dx + dy * dy)
            px, py = wx, wy
        self.wk += len(pts)
        e = [self.version, gcx, gcy, gx, gy, x0, y0, list(pts), exp, tot, pts[-1][0] == gx and pts[-1][1] == gy, self.version]
        self.shr_stored += 1
        if len(self.shr) < SHARE_N:
            self.shr.append(e)
        else:
            self.shr[self.shr_i] = e
            self.shr_i = (self.shr_i + 1) % SHARE_N

    def share_find(self, x, y, gx, gy):
        """PB-SHARE [inf]: a kept route of this grid version with the same goal cell whose start or polyline passes within
        SHARE_R cells of (x, y) is the asker's route: it walks (line of sight) to the first of the next SHARE_SCAN points of
        that route it sees and follows the rest. -> (waypoints, expansions) or None. The expansions (= the budget cost, like
        a cache hit) are the leader's, scaled by the share of the route left."""
        if not self.shr or self.gact:
            return None
        scx, scy = self.cell_of(x, y)
        if not self.walkable(scx, scy):
            return None
        gcx, gcy = self.cell_of(gx, gy)
        ver = self.version
        lim2 = SHARE_R * CELL * SHARE_R * CELL
        limp2 = SHARE_RP * CELL * SHARE_RP * CELL
        for rec in self.shr:
            self.wk += 1
            if rec[1] != gcx or rec[2] != gcy or rec[0] < 0 or ver - rec[0] > SHARE_DLOG:
                continue
            same = rec[3] == gx and rec[4] == gy
            if not same and not rec[10]:
                continue
            pts = rec[7]
            n = len(pts)
            ax, ay = rec[5], rec[6]
            best = -1
            bd = 0.0
            sd2 = (x - ax) * (x - ax) + (y - ay) * (y - ay)       # distance to the route's start
            for i in range(n):
                self.wk += 1
                bx, by = pts[i]
                abx, aby = bx - ax, by - ay
                l2 = abx * abx + aby * aby
                t = 0.0
                if l2 > 0.0:
                    t = ((x - ax) * abx + (y - ay) * aby) / l2
                    if t < 0.0:
                        t = 0.0
                    elif t > 1.0:
                        t = 1.0
                qx, qy = x - (ax + abx * t), y - (ay + aby * t)
                d2 = qx * qx + qy * qy
                if best < 0 or d2 < bd:
                    best = i
                    bd = d2
                ax, ay = bx, by
            if best < 0 or (sd2 > lim2 and bd > limp2):
                continue
            if rec[0] != ver and not self.share_valid(rec, ver):
                continue
            found = -1
            for j in range(best, min(best + SHARE_SCAN, n)):
                self.wk += 1
                if self.los(x, y, pts[j][0], pts[j][1]):
                    found = j
                    break
            if found < 0 or self.los(x, y, gx, gy):          # no way in, or a straight line to the goal (a search answers that for free)
                continue
            out = pts[found:]
            if not same:
                px, py = out[-2] if len(out) > 1 else (x, y)
                if not self.los(px, py, gx, gy):
                    continue
                out[-1] = (gx, gy)
            rem = 0.0
            px, py = x, y
            for wx, wy in out:
                dx, dy = wx - px, wy - py
                rem += math.sqrt(dx * dx + dy * dy)
                px, py = wx, wy
            self.wk += len(out)
            exp = rec[8]
            if rem < rec[9]:
                exp = int(rec[8] * rem / rec[9])
                if exp < 1:
                    exp = 1
            self.shr_hits += 1
            if rec[11] != ver:
                self.shr_xver += 1
            return out, exp
        return None

    def share_valid(self, rec, ver):
        """PB-SHARE [inf]: a kept route of an older grid version stays usable when none of the cells the bumps since then
        blocked lies within one cell of its polyline (start -> points): rectangle (grown by one cell) against each segment, bbox
        first, then the exact segment-box test. A bump without a remembered rectangle, or more than SHARE_DLOG bumps, void it.
        A route that passes is stamped with the current version (the next check starts there). Bounded: SHARE_DLOG x segments."""
        pts = rec[7]
        for v in range(rec[0] + 1, ver + 1):
            d = self.dlog[v % SHARE_DLOG]
            self.wk += 1
            if d is None or d[0] != v:
                rec[0] = -1
                return False
            if d[1] > d[3] or d[2] > d[4]:
                continue
            rx0 = self.ox + CELL * (d[1] - 1)
            ry0 = self.oy + CELL * (d[2] - 1)
            rx1 = self.ox + CELL * (d[3] + 2)
            ry1 = self.oy + CELL * (d[4] + 2)
            ax, ay = rec[5], rec[6]
            for bx, by in pts:
                self.wk += 1
                if (max(ax, bx) >= rx0 and min(ax, bx) <= rx1 and max(ay, by) >= ry0 and min(ay, by) <= ry1
                        and _seg_box(ax, ay, bx, by, rx0, ry0, rx1, ry1)):
                    rec[0] = -1
                    return False
                ax, ay = bx, by
        rec[0] = ver
        return True

    def find_route(self, x0, y0, x1, y1):
        """tools (profile drivers that order a unit to the corner of a route): find_path with the line-of-sight smoothing of the flat finder; with PATH_AB the search returns the raw route
        (the engine's follower smooths it while walking), here it is smoothed up front like the flat finder's"""
        pts = self.find_path(x0, y0, x1, y1)
        if self.ab and pts:
            sx, sy = self.cell_of(x0, y0)
            pts = self.smooth(x0, y0, pts, sx, sy)
        return pts

    def _find_path(self, x0, y0, x1, y1, limit=MAX_EXPAND):
        """world points -> waypoints [(x, y)] (excluding the start; last = the goal point if its cell was reached,
        else the centre of the closest reached cell). Straight segment if the line is clear. A gate crossing is stored as
        three entries: the gate cell P, the marker (MARK_X, slot), the landing point (NOTES_pathing 1.3); the smoothing never
        merges across it."""
        sx, sy = self.cell_of(x0, y0)
        gx, gy = self.cell_of(x1, y1)
        self.last_limited = False
        self.last_reached = True
        self.last_exp = 0
        if self.walkable(sx, sy) and self.los(x0, y0, x1, y1) and not (self.gact and self.gate_line_hit(x0, y0, x1, y1)):
            return [(x1, y1)]
        if not self.walkable(gx, gy):
            alt = self.nearest_walkable(gx, gy)
            if alt is not None:                     # A27: blocked goal -> the nearest walkable cell (ring search)
                gx, gy = alt
                x1, y1 = self.center(gx, gy)
        cells = self.fld_cells(sx, sy, gx, gy, x1, y1) if self.fld_on else None        # PATH-FIELD: the route read off the goal's flow field
        if cells is None:
            cells = self.astar_cached(sx, sy, gx, gy, limit)
            if self.fld_ur:
                self.fld_ur = False
                if cells and self.fld_auto > 0 and cells[-1] == (gx, gy):
                    self.fld_contra += 1                        # PERF-AUTOFIELD: the field said "unreachable", the A* reached the goal
            if self.fld_on and self.fld_uw is not None:
                self.fld_uafter(cells, gx, gy)                  # PATH-FIELD: an unreachable goal: the nearest reachable cell gets a field
        if not cells:
            return []                               # start cell outside the grid
        reached = cells[-1] == (gx, gy)
        self.last_reached = reached
        pts = []
        for i in range(1, len(cells)):
            cx, cy = cells[i]
            if cx < 0:
                pts.append((MARK_X, float(-cx)))     # the marker; the cell after it is the landing
            elif cells[i - 1][0] < 0:
                pts.append(self.gate_point(-cells[i - 1][0]))
            else:
                pts.append(self.center(cx, cy))
        if reached:
            if pts and pts[-1][0] != MARK_X and not (len(pts) > 1 and pts[-2][0] == MARK_X):
                pts[-1] = (x1, y1)
            else:
                pts.append((x1, y1))                  # the goal cell is the landing cell: walk on to the goal point
        if not pts:
            return []
        return self.smooth(x0, y0, pts, sx, sy)

    def smooth(self, x0, y0, pts, sx, sy):
        """smoothing: keep a point only where the line from the last kept point to the next one is blocked; legs split at a marker"""
        n = len(pts)
        out = []
        ax, ay = x0, y0
        start_ok = self.walkable(sx, sy)
        i = 0
        while i < n - 1:
            nx, ny = pts[i + 1]
            if nx == MARK_X:
                out.append(pts[i])                  # the gate cell P: kept
                i += 1
                continue
            if pts[i][0] == MARK_X:
                out.append(pts[i])                  # the marker and the landing point: kept
                out.append((nx, ny))
                ax, ay = nx, ny
                start_ok = True
                i += 2
                continue
            if start_ok and self.los_memo(ax, ay, nx, ny):
                i += 1
                continue
            out.append(pts[i])
            ax, ay = pts[i]
            start_ok = True
            i += 1
        if i == n - 1:
            out.append(pts[-1])
        if out[0][0] == MARK_X:
            out.insert(0, (x0, y0))                    # the unit stands in the gate area: it is its own gate cell
        return out
