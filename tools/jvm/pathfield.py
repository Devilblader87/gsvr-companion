#!/usr/bin/env python3
"""PATH-FIELD (docs/path_field.md, maintainer 2026-10-31): flow fields for creep waves. A goal that many ground movers of one owner are sent to
gets an integer distance field (tools/jvm/flowfield.py: D = shortest octile path cost, DIR = first NB index minimising D(n) + cost); the
searches of the units that walk to it read their route off the field (DIR chain, smoothed like an A* route) instead of running an A*.

Behaviour only, switch-gated (PATH_FIELD, default OFF: nothing here runs and the traces equal main's). Mixin of pathing.Grid, the twin is
unity/Assets/W3VR/Scripts/W3PathField.cs (byte-identical). Udon-friendly: fixed tables, flat arrays, no recursion, bounded loops.

Rules (all deterministic functions of the sim steps, grid version and the request stream):
* ENABLE (order time, in the search stage of find_path: flat A* only, no gate active, ground unit that does not chase): the goal cell belongs to a
  goal BLOCK of 2^FLD_B x 2^FLD_B cells (the creeps of a wave aim at points spread over the lane end, not at one cell); a request counts for
  (block, owner) in a direct-mapped demand table; FLD_N requests inside FLD_WIN steps start a field for the block. The field's goal is the block's
  representative cell (the block centre, else the nearest walkable cell within FLD_RING rings); a route that reaches it goes on to the asker's
  own goal point when the line of sight from the representative to it is clear (else the A* answers).
* FIELD = f(walk map at the step the job was made): walk = wpm ground mask (Grid.gmask, switch-gated) clear and no footprint (the A*'s Grid.walkable). The job made at step s is
  INSTALLED at step I = s + FLD_K, at the start of that step (fld_tick). Until then the units keep walking on the old field / normal searches.
  The client computes the field on the GPU (or on the CPU) and stalls the sim at step I until it has arrived: never skips, so the traces do not
  depend on frame timing. At most FLD_PEND jobs are pending, FLD_SLOTS fields are kept (the least recently used one is replaced).
* GRID CHANGE: a field made for an older grid version stays in use while the cells of the route it gives are still walkable now (checked per
  step of the chain; blocking bumps never make it wrong, only a freed cell can: FLD_MONO / fld_free). A change that may have freed a cell queues a new job (at most one per slot
  per FLD_FREEREFRESH steps), a chain that meets a blocked cell queues one at once and gives the cells so far (FLD_STALEMIN) instead of the A*. FLD_N_FAST / FLD_WIN_FAST: per-map demand
  profile (docs/path_field.md "More requests from fields").
* ROUTE: DIR chain from the start cell, at most FLD_PIECE cells (then the unit asks again where the piece ends: last_limited), smoothed by the
  normal line-of-sight smoothing. Unreachable goal / start blocked / a stale chain: the normal search (A*, nearest reachable cell) answers.
* SIZE CLASS: the engine's flat A* has none (cls 0); a dilated field per class belongs to PATH-AB (cls > 0 gets no field here).
"""
import os

from . import flowfield as FF

PATH_FIELD = {'1': True, '0': False}.get(os.environ.get('W3_PATH_FIELD'))   # W3_PATH_FIELD=1 / 0 forces it for every map (measurements, tests); None = the per-map rule (fld_for)
# the maps the field is validated for (docs/path_field.md "Live enable rule"): (count, sum) of map_fp. The pack the client plays carries more (and other) functions than the
# reference's program (the UI program, Blizzard.j), so the fingerprint looks at the map's own GUI-trigger functions only; the twin: W3PathField.cs FLD_C_* / FLD_S_*
LIVE_MAPS = ((179, 3520656),        # fan_linetowerwars7v114
             (755, 16619593),       # fan_WintermaulV151
             (870, 7273147),        # fan_8402TechWars19d (FAST; docs/path_field.md "Tech Wars / Dawn of the Dead")
             (502, 12389584))       # fan_5201optDawnOfTheDead41F (FAST)


def trig_name(nm):
    """a GUI-trigger function name: starts with Trig_ / InitTrig_ (ordinal) and is pure ASCII 32 .. 126 (Python code points and C# UTF-16 units agree there; every other name is skipped on both sides; the twin: W3Vm.FldTrigName)."""
    for ch in nm:
        if ord(ch) < 32 or ord(ch) > 126:
            return False
    return nm.startswith('Trig_') or nm.startswith('InitTrig_')


def map_fp(prog):
    """(count, sum) over the functions named Trig_* / InitTrig_* (the map's GUI triggers): per name a 24-bit polynomial hash (no int32 overflow in either VM), summed mod 2^24
    (order independent). Pure function of the names, so both VMs agree (the twin: W3Vm.FldMapRule over fName)."""
    cnt = 0
    tot = 0
    for f in prog.functions:
        nm = f.name
        if trig_name(nm):
            h = 0
            for ch in nm:
                h = (h * 31 + ord(ch)) & 0xFFFFFF
            tot = (tot + h) & 0xFFFFFF
            cnt += 1
    return cnt, tot


# the second fingerprint, for maps WITHOUT GUI-trigger functions (hand-written or obfuscated JASS: "0 trigger functions" in the client log): (count, sum) of map_fp_short, the map's own
# functions with a name of 1 .. SHORT_MAX characters. An obfuscated script renames every function to two letters; common.j / Blizzard.j (any version: the shortest of 1.21 .. 1.31 has 5) and
# the engine's own '<init ...>' functions never have a name that short, so the pack of a 1.26 install and the reference program give the same pair. The twin: W3PathField.cs FLD_C_* / FLD_S_*.
SHORT_MAX = 3
LIVE_SHORT = ((1056, 3146752),)     # fan_KigaTDv30p (Kindergarten TD! v.3.0)


def short_name(nm):
    """a name of 1 .. SHORT_MAX characters, pure ASCII 33 .. 126 (the twin: W3Vm.FldShortName); the '<init ...>' names of the engine start with '<' and are skipped."""
    if len(nm) < 1 or len(nm) > SHORT_MAX or nm[0] == '<':
        return False
    for ch in nm:
        if ord(ch) < 33 or ord(ch) > 126:
            return False
    return True


def map_fp_short(prog):
    """(count, sum) over the functions with a short name (short_name): per name the 24-bit polynomial hash of map_fp, summed mod 2^24 (order independent)."""
    cnt = 0
    tot = 0
    for f in prog.functions:
        nm = f.name
        if short_name(nm):
            h = 0
            for ch in nm:
                h = (h * 31 + ord(ch)) & 0xFFFFFF
            tot = (tot + h) & 0xFFFFFF
            cnt += 1
    return cnt, tot


def fld_for(prog):
    """does a map run with flow fields? A forced switch (W3_PATH_FIELD / tests) wins, else the validated maps (LIVE_MAPS by GUI triggers, LIVE_SHORT by short function names)."""
    if PATH_FIELD is not None:
        return PATH_FIELD
    return map_fp(prog) in LIVE_MAPS or map_fp_short(prog) in LIVE_SHORT


FAST_MAPS = ((179, 3520656),        # fan_linetowerwars7v114: the creeps of both lanes follow the same few lane ends all game long: FLD_N_FAST requests make a field
             (870, 7273147),        # fan_8402TechWars19d: work -22 .. -26 % on 5 seeds, kills equal
             (502, 12389584))       # fan_5201optDawnOfTheDead41F: outcome inside the OFF spread on 10 seeds, work -16 % on average
FAST_SHORT = LIVE_SHORT             # fan_KigaTDv30p: 110 creeps of 8 lanes ask for the same few lane ends at 100 s; N_FAST 2 / WIN_FAST 2000 beat 6 / 1000 on the solo game (work -61 % vs -57 % OFF)


def fld_fast(prog):
    """the demand profile of this map: True = FLD_N_FAST requests inside FLD_WIN_FAST steps make a field (the maps of FAST_MAPS), else FLD_N inside FLD_WIN. Wintermaul's maze check stays green
    only with 6 / 1000 (N 4 and 5 pass, 3 and below fail: the maze is not built; with WIN 2000 and N 4 it fails too)."""
    return map_fp(prog) in FAST_MAPS or map_fp_short(prog) in FAST_SHORT


FLD_K = 24                          # install delay in steps (120 ms game time): the sim STALLS at the install step until the field has arrived (GPU latency never changes a trace)
FLD_N = 6                           # requests for one (goal cell, owner) inside FLD_WIN that make a field
FLD_N_FAST = 2                      # ... on the FAST_MAPS
FLD_WIN_FAST = 2000                 # ... inside this many steps (10 s)
FLD_WIN = 1000                      # demand window in steps (5 s)
FLD_PIECE = 128                     # cells of a route taken off a field at once
FLD_SLOTS = 8                       # fields kept
FLD_US = 4                          # slots (after the FLD_SLOTS ones) of the UNREACHABLE fields: towards the nearest reachable cell of the asker's component
FLD_NS = FLD_SLOTS + FLD_US
FLD_PEND = 4                        # jobs pending at once
FLD_IDLE = 24000                    # a field not asked for this many steps (120 s) is dropped (was 30 s: the waves of Wintermaul / the sends of Line Tower Wars come every 30 .. 60 s, so every wave paid its demand counts again)
FLD_REFRESH = 40                    # steps between two jobs of one slot (grid bumps inside the window are coalesced)
FLD_UREFRESH = 200   # steps between two jobs of one unreachable slot (the nearest reachable cell moves as the blockers are built / die)
FLD_STALEMIN = 3                    # >0: a chain that meets a blocked cell after this many cells while a renewal is pending gives the cells so far (partial) instead of the A* (0 = off)
FLD_OFFPART = 1                     # 1: an asker whose goal point is not in sight of the representative cell gets the route to it (partial); the last leg is asked there (0 = the A* answers)
FLD_MONO = 1                        # 1: a field stays valid across grid bumps that only BLOCK cells (a route that is still walkable is still a shortest one) and is renewed when a cell may have been freed or a chain meets a blocked cell; 0: renewed at every bump (the first version)
FLD_FREEREFRESH = 1000               # steps between two jobs of one slot after a change that may have freed a cell (FLD_MONO)
FLD_B = 4                           # goal block = 16 x 16 cells (512 units)
FLD_RING = 8                        # rings searched for the representative cell when the block centre is blocked
FLD_DEM = 64                        # demand table size (direct mapped)
FLD_EXPMIN = 100                    # a route off a field is charged to the path budget as at least this many A* expansions (FITTED [inf]: Wintermaul solo lives
#                                   at 210 / 225 / 255 s: 18 / 15 / 9 vs the original's 18 / 15 / 8; 0 -> 17 / 15 / 4, 150 -> 18 / 13 / 8, 500 -> 20 / 20 / 14)
# PERF-AUTOFIELD (maintainer 2026-11-03): every other map gets flow fields too when it behaves like a tower defense (docs/path_field.md "Automatic enable"). Pure function of the
# request stream (step, owner, unit serial, start cell, goal cell), no timing / GPU state; the twin: W3PathField.cs FldAuto*.
AUTO_N = 10                         # distinct units of one owner asking for a route to the same goal block inside AUTO_WIN steps make the map a TD
AUTO_WIN = 1000                     # steps (5 s): a wave spawns and asks within a few ticks
AUTO_N2 = 12                        # the slow variant (sends of Line Tower Wars: the units of one sender trickle in over a minute): distinct units inside AUTO_WIN2 steps (60 s)
AUTO_WIN2 = 12000
AUTO_FAR = 24                       # a request counts only when start and goal cell are this far apart (Chebyshev, cells = 768 units): a camp's creeps walking home do not
AUTO_DEM = 64                       # direct-mapped table of (goal block, owner)
AUTO_CHK = 12000                    # fallback: the failure counters are looked at every AUTO_CHK steps (60 s)
AUTO_FAILMIN = 48                   # fallback: A* fallbacks (field failed) + 2 x contradictions (field said unreachable, the A* reached the goal) inside one check window ...
AUTO_FAILDIV = 2                    # ... and at least served / AUTO_FAILDIV of them: flow fields are switched off for the rest of the game
NB = FF.NB


def PF_FORCED():
    return PATH_FIELD is True


class PathField:
    def fld_init(self):
        self.fld_on = PF_FORCED()       # the switch of this grid (the engine sets fld_for(program) after building it)
        self.fld_free = 0               # grid version of the last change that may have freed a cell (a structure removed, a gate, a pathing flag cleared): a field made before it may not be shortest
        self.fld_n = FLD_N              # requests inside fld_win steps that make a field (per map: fld_fast)
        self.fld_win = FLD_WIN
        self.fld_ctx = None             # (step, owner id) set by the engine around a unit's search; None = no field for this search
        self.fld_goal = [-1] * FLD_NS   # goal block of the slot (-1 = free)
        self.fld_rep = [-1] * FLD_NS    # representative cell of the installed field (its goal)
        self.fld_prep = [-1] * FLD_NS   # ... of the pending one
        self.fld_last = [0] * FLD_NS    # step of the last request that used / asked for the slot
        self.fld_cur = [None] * FLD_NS  # installed DIR field (bytearray w*h)
        self.fld_ver = [0] * FLD_NS     # grid version the installed field was made for
        self.fld_pend = [None] * FLD_NS  # pending DIR field
        self.fld_pver = [0] * FLD_NS
        self.fld_pat = [-1] * FLD_NS    # install step of the pending job (-1 = none)
        self.fld_jobt = [0] * FLD_NS    # step of the slot's last job
        self.fld_npend = 0
        self.fld_uexp = [0] * FLD_NS    # unreachable fields: the expansions of the A* that made the job (what the original's budget is charged for such a request)
        self.fld_uw = None              # (goal block, step) set by fld_cells: the A* that follows may start an unreachable-goal field (fld_uafter)
        self.fld_ujobs = 0              # statistics: jobs of unreachable fields / routes served off them
        self.fld_userved = 0
        self.dem_cell = [-1] * FLD_DEM
        self.dem_own = [0] * FLD_DEM
        self.dem_cnt = [0] * FLD_DEM
        self.dem_t0 = [0] * FLD_DEM
        # PERF-AUTOFIELD
        self.au_obs = False             # the engine sets it for a map without the validated rule (and no forced switch, no two-level finder): the demand is watched
        self.fld_auto = 0               # 1 = the flow fields run through the automatic rule, -1 = the automatic rule switched them off again (for good)
        self.au_blk = [-1] * AUTO_DEM
        self.au_own = [0] * AUTO_DEM
        self.au_t0 = [0] * AUTO_DEM
        self.au_cnt = [0] * AUTO_DEM
        self.au_ser = [0] * (AUTO_DEM * AUTO_N)
        self.au_t2 = [0] * AUTO_DEM     # the same table for the slow window
        self.au_cnt2 = [0] * AUTO_DEM
        self.au_ser2 = [0] * (AUTO_DEM * AUTO_N2)
        self.au_max2 = 0
        self.au_max = 0                 # statistics: the largest distinct-unit count seen in a window
        self.au_chk = 0                 # next check step
        self.au_b = (0, 0, 0)           # served / unreach / stale / contradictions at the last check
        self.fld_fb = 0                 # requests the A* answered because the field failed (the chain met a blocked cell with no renewal on its way / the start cannot reach the goal and no unreachable field helped)
        self.fld_contra = 0             # field said "unreachable" but the A* reached the goal
        self.fld_ur = False
        self.au_log = []                # messages for the harness (the client logs them)
        # statistics (harness / tests)
        self.fld_jobs = 0               # jobs made
        self.fld_jobcells = 0           # reachable cells of the fields computed (CPU Dijkstra work)
        self.fld_installs = 0
        self.fld_served = 0             # routes read off a field
        self.fld_cellsx = 0             # cells of those routes
        self.fld_stale = 0              # requests the normal search answered because the chain met a cell blocked since the field was made
        self.fld_unreach = 0            # ... because the start cannot reach the goal (the A* answers with the nearest reachable cell)
        self.fld_wait = 0               # ... because the field of the goal is not installed yet
        self.fld_off = 0                # ... because the asker's goal point is not in sight of the field's representative cell
        self.fld_chg = 0                # statistics: expansions charged to the budget for routes read off fields (what an A* would have been charged)
        self.fld_why = {}               # statistics: why a request that reached fld_cells got no route (reason -> count)

    # ------------------------------------------------------------------ service
    def fld_tick(self, now):
        """start of step `now`: install the pending fields that are due, drop the idle ones (both in slot order)."""
        if self.fld_auto > 0 and now >= self.au_chk:
            self.fld_auto_check(now)
            if self.fld_auto < 0:
                return
        if self.fld_npend:
            for s in range(FLD_NS):
                if self.fld_pat[s] >= 0 and self.fld_pat[s] <= now:
                    self.fld_cur[s] = self.fld_pend[s]
                    self.fld_rep[s] = self.fld_prep[s]
                    self.fld_ver[s] = self.fld_pver[s]
                    self.fld_pend[s] = None
                    self.fld_pat[s] = -1
                    self.fld_npend -= 1
                    self.fld_installs += 1
        for s in range(FLD_NS):
            if self.fld_goal[s] >= 0 and self.fld_pat[s] < 0 and now - self.fld_last[s] > FLD_IDLE:
                self.fld_goal[s] = -1
                self.fld_cur[s] = None

    # ------------------------------------------------------------------ automatic enable (PERF-AUTOFIELD)
    def fld_auto_note(self, now, owner, serial, sx, sy, gx, gy):
        """engine.repath, before the search of a ground unit that does not chase and whose order is not a player's input (au_obs only): start cell s, goal cell g. AUTO_N distinct units asking for the same goal block
        (from AUTO_FAR cells away) inside AUTO_WIN steps = a wave on a lane: the flow fields go on for this map."""
        if sx < 0 or sy < 0 or gx < 0 or gy < 0 or sx >= self.w or gx >= self.w or sy >= self.h or gy >= self.h:
            return
        dx = gx - sx
        dy = gy - sy
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        if (dx if dx > dy else dy) < AUTO_FAR:
            return
        bw = (self.w + (1 << FLD_B) - 1) >> FLD_B
        blk = (gy >> FLD_B) * bw + (gx >> FLD_B)
        k = (blk * 31 + owner) % AUTO_DEM
        newkey = self.au_blk[k] != blk or self.au_own[k] != owner
        if newkey or now - self.au_t0[k] > AUTO_WIN:
            self.au_t0[k] = now                 # a new (block, owner) or an expired window takes the entry
            self.au_cnt[k] = 0
        fast = False
        c = self.au_cnt[k]
        base = k * AUTO_N
        for i in range(c):
            if self.au_ser[base + i] == serial:
                break
        else:
            self.au_ser[base + c] = serial
            c += 1
            self.au_cnt[k] = c
            if c > self.au_max:
                self.au_max = c
            fast = c >= AUTO_N
        slow = False
        if newkey or now - self.au_t2[k] > AUTO_WIN2:
            self.au_t2[k] = now
            self.au_cnt2[k] = 0
        self.au_blk[k] = blk
        self.au_own[k] = owner
        c2 = self.au_cnt2[k]
        base2 = k * AUTO_N2
        for i in range(c2):
            if self.au_ser2[base2 + i] == serial:
                break
        else:
            self.au_ser2[base2 + c2] = serial
            c2 += 1
            self.au_cnt2[k] = c2
            if c2 > self.au_max2:
                self.au_max2 = c2
            slow = c2 >= AUTO_N2
        if fast or slow:
            self.fld_on = True
            self.fld_auto = 1
            self.au_obs = False
            self.au_chk = now + AUTO_CHK
            self.au_b = (self.fld_served, self.fld_fb, self.fld_contra)
            if fast:
                self.au_log.append('[W3VR-PATHFIELD] auto: ON (reason: %d units -> same goal, owner %d block %d, inside %d steps, step %d)' % (AUTO_N, owner, blk, AUTO_WIN, now))
            else:
                self.au_log.append('[W3VR-PATHFIELD] auto: ON (reason: %d units -> same goal, owner %d block %d, inside %d steps, step %d)' % (AUTO_N2, owner, blk, AUTO_WIN2, now))

    def fld_auto_check(self, now):
        """every AUTO_CHK steps on an automatically enabled map: too many failures of the field routing = off for the rest of the game."""
        b = self.au_b
        sv = self.fld_served - b[0]
        fb = self.fld_fb - b[1]
        co = self.fld_contra - b[2]
        fail = fb + 2 * co
        self.au_b = (self.fld_served, self.fld_fb, self.fld_contra)
        self.au_chk = now + AUTO_CHK
        if fail >= AUTO_FAILMIN and fail * AUTO_FAILDIV >= sv:
            self.fld_on = False
            self.fld_auto = -1
            for s in range(FLD_NS):
                self.fld_cur[s] = None
                self.fld_pend[s] = None
                self.fld_pat[s] = -1
                self.fld_goal[s] = -1
            self.fld_npend = 0
            self.au_log.append('[W3VR-PATHFIELD] auto: off for this map (reason: %d A* fallbacks + %d routes the A* contradicted vs %d routes served in %d steps, step %d)' % (fb, co, sv, AUTO_CHK, now))

    def fld_walk(self):
        w, h = self.w, self.h
        fl, oc = self.flags, self.occ_walk
        gm = self.gmask
        return bytearray(0 if ((fl[i] & gm) or oc[i]) else 1 for i in range(w * h))

    def fld_rep_of(self, blk):
        """the representative cell of a goal block (index) in the grid as it is now, -1 = none."""
        w = self.w
        bw = (w + (1 << FLD_B) - 1) >> FLD_B
        cx = ((blk % bw) << FLD_B) + (1 << (FLD_B - 1))
        cy = ((blk // bw) << FLD_B) + (1 << (FLD_B - 1))
        if cx >= w:
            cx = w - 1
        if cy >= self.h:
            cy = self.h - 1
        if self.walkable(cx, cy):
            return cy * w + cx
        r = self.nearest_walkable(cx, cy, FLD_RING)
        return -1 if r is None else r[1] * w + r[0]

    def fld_job(self, s, goal, now):
        """a job for slot s at step now: the field of the walk map as it is now to the cell `goal`, installed at now + FLD_K."""
        walk = self.fld_walk()
        self.fld_prep[s] = goal
        D = FF.field_dijkstra(self.w, self.h, walk, [goal])
        self.fld_pend[s] = FF.dir_field(self.w, self.h, walk, D)
        self.fld_pver[s] = self.version
        self.fld_pat[s] = now + FLD_K
        self.fld_jobt[s] = now
        self.fld_npend += 1
        self.fld_jobs += 1
        self.fld_jobcells += sum(1 for v in D if v < FF.INF)

    def fld_cells(self, sx, sy, gx, gy, x1, y1):
        """the search stage of find_path (start cell s, walkable goal cell g after the ring adjustment, goal point x1, y1): the cells start .. end of
        the route read off the field of the goal's block, or None (the A* answers). Sets last_limited / last_exp like a search."""
        self.fld_ur = False
        ctx = self.fld_ctx
        if ctx is None or self.gact:
            return None
        now, owner = ctx
        w = self.w
        if not (0 <= sx < w and 0 <= sy < self.h and 0 <= gx < w and 0 <= gy < self.h and self.walkable(gx, gy)):
            return None
        bw = (w + (1 << FLD_B) - 1) >> FLD_B
        blk = (gy >> FLD_B) * bw + (gx >> FLD_B)
        s = -1
        for k in range(FLD_SLOTS):
            if self.fld_goal[k] == blk:
                s = k
                break
        if s < 0:
            # demand: a direct-mapped table keyed by (goal block, owner)
            k = (blk * 31 + owner) % FLD_DEM
            if self.dem_cell[k] != blk or self.dem_own[k] != owner or now - self.dem_t0[k] > self.fld_win:
                self.dem_cell[k] = blk
                self.dem_own[k] = owner
                self.dem_cnt[k] = 0
                self.dem_t0[k] = now
            self.dem_cnt[k] += 1
            if self.dem_cnt[k] < self.fld_n or self.fld_npend >= FLD_PEND:
                return None
            rep = self.fld_rep_of(blk)
            if rep < 0:
                return None
            v = -1                          # a free slot, else the least recently used one without a pending job (lowest index on ties)
            for k2 in range(FLD_SLOTS):
                if self.fld_goal[k2] < 0:
                    v = k2
                    break
            if v < 0:
                for k2 in range(FLD_SLOTS):
                    if self.fld_pat[k2] < 0 and (v < 0 or self.fld_last[k2] < self.fld_last[v]):
                        v = k2
            if v < 0:
                return None
            s = v
            self.fld_goal[s] = blk
            self.fld_cur[s] = None
            self.fld_last[s] = now
            self.dem_cnt[k] = 0
            self.fld_why['job_new'] = self.fld_why.get('job_new', 0) + 1
            self.fld_job(s, rep, now)
            return None
        self.fld_last[s] = now
        cur = self.fld_cur[s]
        if cur is not None and (self.fld_free > self.fld_ver[s] if FLD_MONO else self.fld_ver[s] != self.version) and self.fld_pat[s] < 0 \
                and now - self.fld_jobt[s] >= (FLD_FREEREFRESH if FLD_MONO else FLD_REFRESH) and self.fld_npend < FLD_PEND:
            rep = self.fld_rep_of(blk)
            if rep >= 0:
                self.fld_why['job_free'] = self.fld_why.get('job_free', 0) + 1
                self.fld_job(s, rep, now)
        if cur is None:
            self.fld_wait += 1
            return None
        if not self.walkable(sx, sy):
            return None
        goal = self.fld_rep[s]
        cells = [(sx, sy)]
        cx, cy = sx, sy
        n = 0
        while True:
            c = cy * w + cx
            if c == goal:
                break
            if n >= FLD_PIECE:
                self.last_limited = True
                break
            k = cur[c]
            if k > 7:
                self.fld_unreach += 1
                self.fld_ur = True
                r = self.fld_ucells(sx, sy, blk, now)
                if r is None:
                    self.fld_fb += 1
                else:
                    self.fld_ur = False         # an unreachable field served the route: no A* runs, nothing to contradict
                return r     # the start cannot reach the representative cell: towards the nearest reachable cell
            dx, dy = NB[k]
            nx, ny = cx + dx, cy + dy
            if not self.walkable(nx, ny) or (dx and dy and (not self.walkable(cx + dx, cy) or not self.walkable(cx, cy + dy))):
                self.fld_stale += 1
                if FLD_MONO and self.fld_pat[s] < 0 and self.fld_npend < FLD_PEND:
                    rep = self.fld_rep_of(blk)      # blocked since the field was made: renewed now (not at the next bump)
                    if rep >= 0:
                        self.fld_why['job_stale'] = self.fld_why.get('job_stale', 0) + 1
                        self.fld_job(s, rep, now)
                if FLD_STALEMIN > 0 and n >= FLD_STALEMIN and self.fld_pat[s] >= 0:
                    self.last_limited = True        # a renewal is on its way: walk to the cell before the footprint and ask again there
                    break
                self.fld_fb += 1
                return None                 # the field is older than a footprint on its way: the normal search
            cells.append((nx, ny))
            cx, cy = nx, ny
            n += 1
        if cx + cy * w == goal and goal != gy * w + gx:
            px, py = self.center(cx, cy)
            if not self.los(px, py, x1, y1):
                self.fld_off += 1
                if FLD_OFFPART > 0 and n > 0:
                    self.last_limited = True    # the route to the representative cell; the last leg (short) is asked there
                    self.wk += n + 1
                    self.last_exp = n + 1 if n + 1 > FLD_EXPMIN else FLD_EXPMIN
                    self.fld_served += 1
                    self.fld_cellsx += n + 1
                    return cells
                return None                 # the asker's own goal is not in sight of the representative cell
            cells.append((gx, gy))
            n += 1
        self.wk += n + 1
        self.last_exp = n + 1 if n + 1 > FLD_EXPMIN else FLD_EXPMIN
        self.fld_served += 1
        self.fld_cellsx += n + 1
        return cells

    # ------------------------------------------------------------------ unreachable goals
    def fld_ucells(self, sx, sy, blk, now):
        """the start cannot reach the representative cell of the goal block (a fully blocked lane): the route towards the nearest reachable cell T of
        the start's component (the A*'s own answer: least octile distance to the goal, then lower g, then lower index), read off an unreachable field
        (slots FLD_SLOTS ..; its goal is T, made from the A* that answered the request that found no usable field). The creeps follow it to the
        blockade; there they stand like after the A* (cells [start]) and the combat rules take over. None = the A* answers (and may start the job)."""
        u = -1
        for k in range(FLD_SLOTS, FLD_NS):
            if self.fld_goal[k] == blk:
                u = k
                break
        cur = None if u < 0 else self.fld_cur[u]
        if u >= 0:
            self.fld_last[u] = now
        if cur is None or not self.walkable(sx, sy):
            self.fld_why['u_nofield'] = self.fld_why.get('u_nofield', 0) + 1
            if u < 0 or self.fld_pat[u] < 0:
                self.fld_uw = (blk, now)
            return None
        if self.fld_ver[u] != self.version and self.fld_pat[u] < 0 and now - self.fld_jobt[u] >= FLD_UREFRESH and self.fld_npend < FLD_PEND:
            self.fld_why['u_renew'] = self.fld_why.get('u_renew', 0) + 1
            self.fld_uw = (blk, now)                     # older than the grid: the A* answers this request and renews the field (the nearest cell may have moved)
            return None
        w = self.w
        goal = self.fld_rep[u]
        cells = [(sx, sy)]
        cx, cy = sx, sy
        n = 0
        while True:
            c = cy * w + cx
            if c == goal:
                break
            if n >= FLD_PIECE:
                self.last_limited = True
                break
            k = cur[c]
            if k > 7:
                self.fld_why['u_comp'] = self.fld_why.get('u_comp', 0) + 1
                if self.fld_pat[u] < 0:
                    self.fld_uw = (blk, now)             # another component than the field's: a new field from this start
                return None
            dx, dy = NB[k]
            nx, ny = cx + dx, cy + dy
            if not self.walkable(nx, ny) or (dx and dy and (not self.walkable(cx + dx, cy) or not self.walkable(cx, cy + dy))):
                self.fld_stale += 1
                self.fld_why['u_stale'] = self.fld_why.get('u_stale', 0) + 1
                if self.fld_pat[u] < 0:
                    self.fld_uw = (blk, now)
                return None
            cells.append((nx, ny))
            cx, cy = nx, ny
            n += 1
        self.fld_userved += 1
        self.wk += n + 1
        ue = self.fld_uexp[u]
        self.last_exp = ue if ue > n + 1 and ue > FLD_EXPMIN else (n + 1 if n + 1 > FLD_EXPMIN else FLD_EXPMIN)      # the budget is charged as the A* of the component was
        self.fld_served += 1
        self.fld_cellsx += n + 1
        return cells

    def fld_uafter(self, cells, gx, gy):
        """find_path, after the A* answered a request fld_ucells flagged: a goal it did not reach starts the unreachable field towards its last cell
        (the nearest reachable one)."""
        uw = self.fld_uw
        self.fld_uw = None
        if uw is None or not cells or self.gact or self.fld_npend >= FLD_PEND or cells[-1] == (gx, gy):
            return
        blk, now = uw
        w = self.w
        tx, ty = cells[-1]
        if tx < 0 or not self.walkable(tx, ty):
            return
        u = -1
        for k in range(FLD_SLOTS, FLD_NS):
            if self.fld_goal[k] == blk:
                u = k
                break
        if u >= 0:
            if self.fld_pat[u] >= 0 or now - self.fld_jobt[u] < FLD_UREFRESH:
                return
        else:
            for k in range(FLD_SLOTS, FLD_NS):
                if self.fld_goal[k] < 0:
                    u = k
                    break
            if u < 0:
                for k in range(FLD_SLOTS, FLD_NS):
                    if self.fld_pat[k] < 0 and (u < 0 or self.fld_last[k] < self.fld_last[u]):
                        u = k
            if u < 0:
                return
            self.fld_goal[u] = blk
            self.fld_cur[u] = None
        self.fld_last[u] = now
        self.fld_ujobs += 1
        self.fld_uexp[u] = self.last_exp
        self.fld_job(u, ty * w + tx, now)
