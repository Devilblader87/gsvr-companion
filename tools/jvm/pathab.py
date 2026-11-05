#!/usr/bin/env python3
"""PATH-AB: the original's two-level ground path finder (docs/NOTES_pathing.md §2, plan steps 1 and 2 of §2.13).

Search A runs on a coarse map (CAcceleratorMap: per cell of 64 / 128 / 256 / 512 world units one of free / blocked / mixed), search B on the fine
32-unit grid between the waypoints of A. Behaviour only ([obs] = read in the game's code, [inf] = a choice where the notes are silent); no code of
the original. Mixin of pathing.Grid. Udon-friendly (the U# twin is W3PathAB.cs, byte-identical results): flat int lists allocated once, one
heap shared by both searches (they never overlap), no recursion (explicit stacks), no per-search allocation, every pop / push charges `wk`.

Units: A0 cell = 2 x 2 fine cells (64 u); level L cell = 2^L x 2^L A0 cells; the node coordinates are level-0 (A0) cell coordinates of the probe that
found the node.
"""
import math

AB_LEVELS = 4                       # A0 .. A3 (64 .. 512 world units) [obs 2.0 rule 6]
AB_BUDGET_A = 400                   # pop budget of search A for a single unit [obs 2.0 rule 2]
AB_BUDGET_A_CLUSTER = 5000          # ... for a group cluster (not used by the engine yet, 2.8)
AB_BUDGET_B = 700                   # pop budget of search B [obs]
AB_UNIT2 = 576                      # A cost = isqrt(576 (dx^2 + dy^2)) = 24 per A0 cell [obs 2.2]
AB_B_S, AB_B_D = 15, 21             # B step costs [obs 2.3]
AB_LEG16 = 160                      # A route length (1/16 A0 cell) that ends a B leg: 10 A0 cells = 640 u [obs 2.4: segment of 10 cells; inf: summed along the route, so a wall of short hops does not aim B at the goal]
AB_PUSH_A = 40                      # heap entries one A pop can add at most (4 sides x 8 + 4 corners + gate edge, rounded up)
AB_PUSH_B = 8
AB_RMAX = 3 * AB_BUDGET_A_CLUSTER + 16      # route arrays: a route has at most one entry per popped node plus a marker per gate edge
AB_FP_MAX = 64                      # cells of the footprint a goal point is looked up in
AB_ROWS = 4                         # path types: only the ground type (0) is built [inf: the engine paths ground units only]
CELL = SHARE_DLOG = _seg_box = None     # bound once by ab_build (pathing imports this module, so no import at the top)
ABN = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))     # B neighbour order NW N NE W E SW S SE [obs 2.3]
# EXT-ow3 SPEC-0020 / NOTES_pathing 3.2: the ground mask (unwalkable OR unflyable).  It is switch-gated (pathing.ground_mask, OFF =
# the old 0x02) and read once per grid: Grid.gmask.


class AbRoute:
    """PATH-AB step 4: the route object of a mover (NOTES_pathing 2.4): the A route of its last search A (A0 cells; a marker entry has k = the gate slot),
    kept in the mover and consumed leg by leg; the raw fine route of the leg in progress is the mover's own path (engine u.path)"""
    __slots__ = ('x', 'y', 'k', 'n', 'i', 'e', 'gt', 'ok', 'reached', 'nd', 'gx', 'gy')

    def __init__(self):
        self.x = []
        self.y = []
        self.k = []
        self.n = 0                  # entries (0 = no route)
        self.i = 0                  # entry the next leg starts from
        self.e = 0                  # end entry of the leg served last
        self.gt = 0                 # the marker entry that ends that leg (0 = none)
        self.ok = False             # that leg's fine search reached its aim: the next leg starts at its end (marker: after the landing entry)
        self.reached = False        # search A reached the goal
        self.nd = 0                 # squared A0 distance from the nearest node to the goal
        self.gx = self.gy = -1      # the goal's fine cell the route was searched for


class PathAB:
    ab_su = None                    # PATH-AB step 4: standing units per fine cell (key -> count, the engine's occs) that search B and the follower's sight see as blocked [obs steer 7.1.4: only STANDING units of every owner]
    ab_x1 = ab_x2 = -1              # ... except these cells (the mover's own, the goal's: a target stands there)
    ab_st = None                    # per level: bytearray of cell states (0 free, 1 blocked, 2 mixed); None = not built

    # ------------------------------------------------------------------ coarse map (3.3 / 3.4)
    def ab_build(self):
        global CELL, SHARE_DLOG, _seg_box
        from . import pathing as _pt
        CELL, SHARE_DLOG, _seg_box = _pt.CELL, _pt.SHARE_DLOG, _pt._seg_box
        w, h = self.w, self.h
        cw = [(w + 1) >> 1]
        ch = [(h + 1) >> 1]
        for lv in range(1, AB_LEVELS):
            cw.append((cw[lv - 1] + 1) >> 1)
            ch.append((ch[lv - 1] + 1) >> 1)
        self.ab_cw, self.ab_ch = cw, ch
        self.ab_st = [bytearray(cw[lv] * ch[lv]) for lv in range(AB_LEVELS)]
        self.ab_slot = bytearray(cw[0] * ch[0])
        self.ab_stamp = [[0] * (cw[lv] * ch[lv]) for lv in range(AB_LEVELS)]      # node memo per cell: search id
        self.ab_idx = [[0] * (cw[lv] * ch[lv]) for lv in range(AB_LEVELS)]        # ... node index
        cap = min(sum(cw[lv] * ch[lv] for lv in range(AB_LEVELS)), AB_PUSH_A * AB_BUDGET_A_CLUSTER) + 8       # PATH-AB S3: a search discovers at most PUSH_A nodes per pop (and a cell holds one node)
        self.ab_cap = cap
        self.ab_nx = [0] * cap
        self.ab_ny = [0] * cap
        self.ab_nl = [0] * cap
        self.ab_ns = [0] * cap          # gate slot of the cell
        self.ab_ng = [0] * cap
        self.ab_nh = [0] * cap
        self.ab_np = [0] * cap
        self.ab_nt = [0] * cap          # 0 fresh, 1 open, 2 closed
        self.ab_nv = [0] * cap          # generation (stale heap entries)
        self.ab_nw = [0] * cap          # reached through a gate edge
        self.ab_hcap = 0
        self.ab_hf = self.ab_hc = self.ab_hg = None
        self.ab_hn = 0
        self.ab_ensure_heap(AB_PUSH_A * AB_BUDGET_A_CLUSTER + 64)
        self.ab_bgen = [0] * (w * h)    # B: node generation per fine cell (valid when stamp == gen)
        self.ab_sid = 0
        self.ab_nn = 0
        self.ab_ovr = None              # (cx0, cy0, cx1, cy1) fine cells treated as free during one A search (target footprint) [obs 2.0 rule 12]
        self.ab_stk_l = [0] * 16
        self.ab_stk_a = [0] * 16
        self.ab_stk_q = [0] * AB_FP_MAX
        self.ar_x = [0] * AB_RMAX       # the A route of the last search (start .. end), marker entries have ar_k = slot
        self.ar_y = [0] * AB_RMAX
        self.ar_k = [0] * AB_RMAX
        self.ar_n = 0
        self.ab_reached = False
        self.ab_gax = self.ab_gay = 0
        self.ab_near = -1
        self.ab_near_d = 0
        self.ab_apops = 0
        self.ab_bpops = 0
        self.ab_cells = []              # the B route of the last search (cells start .. end)
        self.ab_bhit = False            # the last B search ended at the target footprint
        self.ab_bgoal = False           # ... at the goal cell
        if self.gslot is None and not any(self.occ_walk):
            self.ab_init_cells()            # PATH-AB S3: the first build of a grid without footprints and gates: straight loops over the flag bytes
        else:
            self.ab_rebuild(0, 0, w - 1, h - 1)

    def ab_init_cells(self):
        """ab_rebuild(0, 0, w - 1, h - 1) for a grid with no footprints and no gates (only the ground terrain mask counts): no per-cell calls, same states, same work charged."""
        w, h = self.w, self.h
        fl = self.flags
        gm = self.gmask
        cw, ch, st = self.ab_cw, self.ab_ch, self.ab_st
        st0 = st[0]
        w0 = cw[0]
        for ay in range(ch[0]):
            r0 = 2 * ay * w
            r1 = r0 + w
            two = 2 * ay + 1 < h
            o = ay * w0
            for ax in range(w0):
                fx = 2 * ax
                nb = 1 if fl[r0 + fx] & gm else 0
                if fx + 1 < w:
                    if fl[r0 + fx + 1] & gm:
                        nb += 1
                else:
                    nb += 1
                if two:
                    if fl[r1 + fx] & gm:
                        nb += 1
                    if fx + 1 < w:
                        if fl[r1 + fx + 1] & gm:
                            nb += 1
                    else:
                        nb += 1
                else:
                    nb += 2
                st0[o + ax] = 0 if nb == 0 else (1 if nb == 4 else 2)
        self.wk += w0 * ch[0]
        for lv in range(1, AB_LEVELS):
            pcw, pch, pst = cw[lv - 1], ch[lv - 1], st[lv - 1]
            cur, wl = st[lv], cw[lv]
            for ay in range(ch[lv]):
                for ax in range(wl):
                    nb = 0
                    mixed = False
                    for k in range(4):
                        qx = 2 * ax + (k & 1)
                        qy = 2 * ay + (k >> 1)
                        if qx >= pcw or qy >= pch:
                            nb += 1
                            continue
                        s = pst[qy * pcw + qx]
                        if s == 1:
                            nb += 1
                        elif s == 2:
                            mixed = True
                    cur[ay * wl + ax] = 2 if mixed else (0 if nb == 0 else (1 if nb == 4 else 2))
            self.wk += wl * ch[lv]

    def ab_ensure_heap(self, n):
        if self.ab_hcap >= n:
            return
        self.ab_hf = [0] * (n + 2)
        self.ab_hc = [0] * (n + 2)
        self.ab_hg = [0] * (n + 2)
        self.ab_hcap = n
        self.ab_hn = 0

    def ab_fb(self, cx, cy):
        """1 when the fine cell blocks the ground path type: outside the grid, the grid's ground mask, a walk footprint (counted like a blocked cell by
        the original's coarse builder); the override rectangle (target footprint during one search) is free."""
        if cx < 0 or cy < 0 or cx >= self.w or cy >= self.h:
            return 1
        o = self.ab_ovr
        if o is not None and o[0] <= cx <= o[2] and o[1] <= cy <= o[3]:
            return 0
        i = cy * self.w + cx
        if self.flags[i] & self.gmask or self.occ_walk[i]:
            return 1
        return 0

    def ab_rebuild(self, cx0, cy0, cx1, cy1):
        """rebuild the coarse cells covering the fine rectangle (inclusive, clamped): level 0 from the fine cells (any blocked child =
        mixed, 4 = blocked), levels 1 .. 3 from the children (missing / blocked count as blocked; a mixed child or a child with a gate slot
        makes the block mixed) [obs 3.4]. Synchronous on every footprint add / remove, never on SetTerrainPathable."""
        if self.ab_st is None:
            return
        if cx0 < 0:
            cx0 = 0
        if cy0 < 0:
            cy0 = 0
        if cx1 > self.w - 1:
            cx1 = self.w - 1
        if cy1 > self.h - 1:
            cy1 = self.h - 1
        if cx0 > cx1 or cy0 > cy1:
            return
        cw, ch, st = self.ab_cw, self.ab_ch, self.ab_st
        x0, y0, x1, y1 = cx0 >> 1, cy0 >> 1, cx1 >> 1, cy1 >> 1
        w, h = self.w, self.h
        gs = self.gslot
        st0 = st[0]
        w0 = cw[0]
        for ay in range(y0, y1 + 1):
            for ax in range(x0, x1 + 1):
                self.wk += 1
                fx, fy = 2 * ax, 2 * ay
                nb = self.ab_fb(fx, fy) + self.ab_fb(fx + 1, fy) + self.ab_fb(fx, fy + 1) + self.ab_fb(fx + 1, fy + 1)
                st0[ay * w0 + ax] = 0 if nb == 0 else (1 if nb == 4 else 2)
                sl = 0
                if gs is not None:
                    for k in range(4):
                        qx = fx + (k & 1)
                        qy = fy + (k >> 1)
                        if qx < w and qy < h and gs[qy * w + qx]:
                            sl = gs[qy * w + qx]
                            break
                self.ab_slot[ay * w0 + ax] = sl
        for lv in range(1, AB_LEVELS):
            x0 >>= 1
            y0 >>= 1
            x1 >>= 1
            y1 >>= 1
            pcw, pch, pst = cw[lv - 1], ch[lv - 1], st[lv - 1]
            cur, wl = st[lv], cw[lv]
            for ay in range(y0, y1 + 1):
                for ax in range(x0, x1 + 1):
                    self.wk += 1
                    nb = 0
                    mixed = False
                    for k in range(4):
                        qx = 2 * ax + (k & 1)
                        qy = 2 * ay + (k >> 1)
                        if qx >= pcw or qy >= pch:
                            nb += 1
                            continue
                        s = pst[qy * pcw + qx]
                        if s == 1:
                            nb += 1
                        elif s == 2:
                            mixed = True
                        if lv == 1 and self.ab_slot[qy * pcw + qx]:
                            mixed = True
                    cur[ay * wl + ax] = 2 if mixed else (0 if nb == 0 else (1 if nb == 4 else 2))

    def ab_rect(self, cells):
        """a footprint was added / removed: rebuild the coarse cells over the bounding rectangle of its walk cells"""
        x0 = y0 = 1 << 30
        x1 = y1 = -(1 << 30)
        for cx, cy, m in cells:
            if m & 1:
                if cx < x0:
                    x0 = cx
                if cx > x1:
                    x1 = cx
                if cy < y0:
                    y0 = cy
                if cy > y1:
                    y1 = cy
        if x0 <= x1:
            self.ab_rebuild(x0, y0, x1, y1)

    def ab_goal_rect(self, gx, gy):
        """the footprint a goal point stands in: when the goal cell is blocked by a footprint (walk counter, not terrain) the order is on a
        building / tree / mine [obs 2.0 rule 12: the target's footprint is free for search A and ends search B]; the footprint is the
        4-connected set of footprint-blocked cells around the goal cell, at most AB_FP_MAX cells -> bounding rectangle, else None [inf: the
        port keeps no target object in the path request]."""
        w, h = self.w, self.h
        if gx < 0 or gy < 0 or gx >= w or gy >= h or self.occ_walk[gy * w + gx] == 0:
            return None
        q = self.ab_stk_q
        q[0] = gy * w + gx
        qn = 1
        qi = 0
        x0 = x1 = gx
        y0 = y1 = gy
        sg = self.gen + 1
        self.gen = sg
        closed = self.closed
        closed[q[0]] = sg
        while qi < qn:
            c = q[qi]
            qi += 1
            self.wk += 1
            cx = c % w
            cy = c // w
            if cx < x0:
                x0 = cx
            if cx > x1:
                x1 = cx
            if cy < y0:
                y0 = cy
            if cy > y1:
                y1 = cy
            for k in range(4):
                nx = cx + (1 if k == 0 else (-1 if k == 1 else 0))
                ny = cy + (1 if k == 2 else (-1 if k == 3 else 0))
                if nx < 0 or ny < 0 or nx >= w or ny >= h:
                    continue
                n = ny * w + nx
                if self.occ_walk[n] == 0 or closed[n] == sg:
                    continue
                if qn >= AB_FP_MAX:
                    continue
                closed[n] = sg
                q[qn] = n
                qn += 1
        return (x0, y0, x1, y1)

    # ------------------------------------------------------------------ heap (2.0 rule 3; shared by both searches)
    def ab_push(self, f, c, gv):
        hf, hc, hg = self.ab_hf, self.ab_hc, self.ab_hg
        i = self.ab_hn + 1
        self.ab_hn = i
        while i > 1 and hf[i >> 1] >= f:
            p = i >> 1
            hf[i] = hf[p]
            hc[i] = hc[p]
            hg[i] = hg[p]
            i = p
        hf[i] = f
        hc[i] = c
        hg[i] = gv

    def ab_pop(self):
        """-> (node, generation) of the root; the heap keeps its rules: a tie goes to the right child, a moved-down entry stops at an equal key."""
        hf, hc, hg = self.ab_hf, self.ab_hc, self.ab_hg
        n = self.ab_hn
        rc, rg = hc[1], hg[1]
        lf, lc, lg = hf[n], hc[n], hg[n]
        n -= 1
        self.ab_hn = n
        if n >= 1:
            i = 1
            while True:
                c = 2 * i
                if c > n:
                    break
                if c < n and hf[c + 1] <= hf[c]:
                    c += 1
                if lf <= hf[c]:
                    break
                hf[i] = hf[c]
                hc[i] = hc[c]
                hg[i] = hg[c]
                i = c
            hf[i] = lf
            hc[i] = lc
            hg[i] = lg
        return rc, rg

    # ------------------------------------------------------------------ search A (2.2)
    def ab_lookup(self, lv, x, y):
        """node of the probe (lv, x, y): -1 blocked / outside / (level 0) mixed, -2 mixed above level 0 (refine), else the node index; a free
        cell climbs while the next coarser cell is free too (open field = one node per block of up to 8 x 8 cells) and keeps one node per cell."""
        cx = x >> lv
        cy = y >> lv
        cw, ch = self.ab_cw, self.ab_ch
        if cx < 0 or cy < 0 or cx >= cw[lv] or cy >= ch[lv]:
            return -1
        s = self.ab_st[lv][cy * cw[lv] + cx]
        if s == 1:
            return -1
        if s == 2:
            return -1 if lv == 0 else -2
        while lv < AB_LEVELS - 1:
            px = cx >> 1
            py = cy >> 1
            if self.ab_st[lv + 1][py * cw[lv + 1] + px] != 0:
                break
            lv += 1
            cx = px
            cy = py
        ci = cy * cw[lv] + cx
        sid = self.ab_sid
        if self.ab_stamp[lv][ci] == sid:
            return self.ab_idx[lv][ci]
        n = self.ab_nn
        self.ab_nn = n + 1
        self.ab_stamp[lv][ci] = sid
        self.ab_idx[lv][ci] = n
        self.ab_nx[n] = x
        self.ab_ny[n] = y
        self.ab_nl[n] = lv
        self.ab_ns[n] = self.ab_slot[ci] if lv == 0 else 0
        self.ab_nt[n] = 0
        self.ab_nv[n] = 0
        self.ab_nw[n] = 0
        return n

    @staticmethod
    def ab_cost(ax, ay, bx, by):
        dx = ax - bx
        dy = ay - by
        return math.isqrt(AB_UNIT2 * (dx * dx + dy * dy))

    def ab_relax(self, n, p, cost, gate):
        """the node n found from p [obs 0x6f6879f0]: a fresh node may become the nearest one (squared distance of its coordinates to the goal
        cell, strict <, first discovery decides); else only a strictly smaller g re-opens it (a closed node is re-opened, an open one gets a new
        generation so its heap entry goes stale); then parent, g, h stored and the node pushed with f = g + h."""
        ng = self.ab_ng[p] + cost
        t = self.ab_nt[n]
        if t == 0:
            dx = self.ab_nx[n] - self.ab_gax
            dy = self.ab_ny[n] - self.ab_gay
            d2 = dx * dx + dy * dy
            if d2 < self.ab_near_d:
                self.ab_near_d = d2
                self.ab_near = n
        elif self.ab_ng[n] <= ng:
            return
        elif t == 1:
            self.ab_nv[n] += 1
        self.ab_np[n] = p
        self.ab_ng[n] = ng
        hh = self.ab_cost(self.ab_nx[n], self.ab_ny[n], self.ab_gax, self.ab_gay)
        self.ab_nh[n] = hh
        self.ab_nt[n] = 1
        self.ab_nw[n] = gate
        self.ab_push(ng + hh, n, self.ab_nv[n])
        self.wk += 1

    def ab_step(self, n, r):
        """relax the found node r from n at the step cost between their stored coordinates"""
        self.ab_relax(r, n, self.ab_cost(self.ab_nx[r], self.ab_ny[r], self.ab_nx[n], self.ab_ny[n]), 0)

    def ab_side(self, n, side, bx, by, lv):
        """one side of the block of n (N y-1, E x+size, S y+size, W x-1; 2.2): the probe starts at the node's own coordinate along the side
        (clipped into the block), a mixed neighbour is refined into its two children that face the block (explicit stack, first half first).
        -> True when the side produced a neighbour."""
        s = 1 << lv
        horiz = side == 0 or side == 2
        if side == 0:
            fixed = by - 1
        elif side == 2:
            fixed = by + s
        elif side == 1:
            fixed = bx + s
        else:
            fixed = bx - 1
        sl, sa = self.ab_stk_l, self.ab_stk_a
        sl[0] = lv
        sa[0] = bx if horiz else by
        sp = 1
        pos = self.ab_nx[n] if horiz else self.ab_ny[n]
        got = False
        while sp > 0:
            sp -= 1
            l = sl[sp]
            a = sa[sp]
            size = 1 << l
            p = pos
            if p < a:
                p = a
            elif p > a + size - 1:
                p = a + size - 1
            r = self.ab_lookup(l, p, fixed) if horiz else self.ab_lookup(l, fixed, p)
            if r >= 0:
                self.ab_step(n, r)
                got = True
            elif r == -2:
                half = size >> 1
                sl[sp] = l - 1
                sa[sp] = a + half
                sl[sp + 1] = l - 1
                sa[sp + 1] = a
                sp += 2
        return got

    def ab_corner(self, n, x, y, lv):
        r = self.ab_lookup(lv, x, y)
        while r == -2 and lv > 0:
            lv -= 1
            r = self.ab_lookup(lv, x, y)
        if r >= 0:
            self.ab_step(n, r)

    def ab_expand(self, n):
        lv = self.ab_nl[n]
        s = 1 << lv
        bx = (self.ab_nx[n] >> lv) << lv
        by = (self.ab_ny[n] >> lv) << lv
        gn = self.ab_side(n, 0, bx, by, lv)
        ge = self.ab_side(n, 1, bx, by, lv)
        gs = self.ab_side(n, 2, bx, by, lv)
        gw = self.ab_side(n, 3, bx, by, lv)
        if gn and ge:
            self.ab_corner(n, bx + s, by - 1, lv)
        if ge and gs:
            self.ab_corner(n, bx + s, by + s, lv)
        if gs and gw:
            self.ab_corner(n, bx - 1, by + s, lv)
        if gw and gn:
            self.ab_corner(n, bx - 1, by - 1, lv)
        if lv == 0 and self.ab_ns[n] and self.gact:
            e = self.gtab.get(self.ab_ns[n])
            if e is not None and e[0]:
                lx, ly = e[1] >> 1, e[2] >> 1
                x, y = self.ab_nx[n], self.ab_ny[n]
                if max(abs(lx - x), abs(ly - y)) > 1:
                    r = self.ab_lookup(0, lx, ly)
                    if r >= 0:
                        self.ab_relax(r, n, 1, 1)

    def ab_a(self, sax, say, gax, gay, budget):
        """search A from the A0 cell (sax, say) to (gax, gay) [obs 0x6f6891d0 / 0x6f689950]. -> 0 no path (start outside the map), 1 the route is
        the goal point alone (same cell, blocked start cell, same node), 2 searched (route in ar_*, ab_reached), 3 nowhere to go (the nearest
        node is the start). Sets ab_apops."""
        self.ab_apops = 0
        self.ar_n = 0
        self.ab_reached = False
        cw0, ch0 = self.ab_cw[0], self.ab_ch[0]
        if sax < 0 or say < 0 or sax >= cw0 or say >= ch0:
            return 0
        if sax == gax and say == gay:
            self.ab_reached = True
            return 1
        self.ab_sid += 1
        self.ab_nn = 0
        ns = self.ab_lookup(0, sax, say)
        if ns == -1:
            self.ab_reached = True
            return 1
        ng = self.ab_lookup(0, gax, gay)
        if ns == ng:
            self.ab_reached = True
            return 1
        self.ab_ensure_heap(AB_PUSH_A * budget + 64)
        self.ab_gax, self.ab_gay = gax, gay
        dx, dy = sax - gax, say - gay
        self.ab_near_d = dx * dx + dy * dy
        self.ab_near = ns
        self.ab_hn = 0
        h0 = self.ab_cost(sax, say, gax, gay)
        self.ab_ng[ns] = 0
        self.ab_nh[ns] = h0
        self.ab_np[ns] = -1
        self.ab_nt[ns] = 1
        self.ab_push(h0, ns, 0)
        pops = 0
        found = -1
        while self.ab_hn > 0 and pops < budget:
            c, gv = self.ab_pop()
            pops += 1
            self.wk += 1
            if gv != self.ab_nv[c]:
                continue                                  # stale entry (counts as a pop)
            self.ab_nv[c] += 1
            self.ab_nt[c] = 2
            if c == ng:
                found = c
                break
            self.ab_expand(c)
        self.ab_apops = pops
        end = found if found >= 0 else self.ab_near
        self.ab_reached = found >= 0
        if end == ns:
            return 3
        # route: start .. end, a gate edge adds the marker (slot of the gate cell) between the cell and the landing
        m = 0
        c = end
        ax, ay, ak = self.ar_x, self.ar_y, self.ar_k
        while c >= 0:
            ax[m] = self.ab_nx[c]
            ay[m] = self.ab_ny[c]
            ak[m] = 0
            m += 1
            if self.ab_nw[c] and self.ab_np[c] >= 0:             # built backwards: the marker lands between the gate cell and the landing
                ax[m] = 0
                ay[m] = 0
                ak[m] = self.ab_ns[self.ab_np[c]]
                m += 1
            c = self.ab_np[c]
            self.wk += 1
        i, j = 0, m - 1
        while i < j:
            ax[i], ax[j] = ax[j], ax[i]
            ay[i], ay[j] = ay[j], ay[i]
            ak[i], ak[j] = ak[j], ak[i]
            i += 1
            j -= 1
        self.ar_n = m
        return 2

    # ------------------------------------------------------------------ search B (2.3)
    def ab_touch(self, cx, cy, k, t):
        """a cell of the ring around the footprint block {cx-k..cx} x {cy-k..cy} that is blocked and lies in the target footprint"""
        for y in range(cy - k - 1, cy + 2):
            for x in range(cx - k - 1, cx + 2):
                if cx - k <= x <= cx and cy - k <= y <= cy:
                    continue
                if t[0] <= x <= t[2] and t[1] <= y <= t[3] and not self.walkable(x, y):
                    return True
        return False

    def ab_free(self, x, y):
        """a fine cell search B and the follower's sight may enter: walkable, and no standing unit but the mover's own / the goal's"""
        if not self.walkable(x, y):
            return False
        su = self.ab_su
        if su is not None:
            k = y * self.w + x
            if k != self.ab_x1 and k != self.ab_x2 and su.get(k, 0) > 0:
                return False
        return True

    def ab_col(self, x, y0, y1):
        for y in range(y0, y1 + 1):
            if not self.ab_free(x, y):
                return False
        return True

    def ab_row(self, y, x0, x1):
        for x in range(x0, x1 + 1):
            if not self.ab_free(x, y):
                return False
        return True

    def ab_b(self, sx, sy, tx, ty, cls, tgt, budget):
        """search B from fine cell (sx, sy) to (tx, ty) for a unit of footprint class cls (block of (cls+1)^2 cells {x-cls..x} x {y-cls..y}),
        budget pops. Result in ab_cells (cells start .. end), ab_bhit / ab_bgoal, ab_bpops. -> True when the goal cell or the target footprint
        was reached; otherwise the cells lead to the nearest discovered node (the single cell start when that is the start)."""
        w, h = self.w, self.h
        pcs = (16 - 16 * cls) >> 5                      # STEER-4: the cell of the block's centre = node + pcs (0 for classes 0 / 1, -1 for 2 / 3)
        self.gen += 1
        gen = self.gen
        g, stamp, closed, parent, bgen = self.g, self.stamp, self.closed, self.parent, self.ab_bgen
        s = sy * w + sx
        t = ty * w + tx if (0 <= tx < w and 0 <= ty < h) else -1
        stamp[s] = gen
        closed[s] = 0
        g[s] = 0
        parent[s] = -1
        bgen[s] = 0
        dx, dy = sx - tx, sy - ty
        near = s
        near_d = dx * dx + dy * dy
        self.ab_ensure_heap(AB_PUSH_B * budget + 64)
        self.ab_hn = 0
        h0 = self.ab_hb(dx, dy)
        self.ab_push(h0, s, 0)
        pops = 0
        end = -1
        hit = False
        goal = False
        while self.ab_hn > 0 and pops < budget:
            c, gv = self.ab_pop()
            pops += 1
            self.wk += 1
            if gv != bgen[c]:
                continue
            bgen[c] += 1
            closed[c] = gen
            if c == t:
                end = c
                goal = True
                break
            cx = c % w
            cy = c // w
            if tgt is not None and self.ab_touch(cx + pcs, cy + pcs, 0, tgt):       # STEER-4 [inf]: the arrival at a target footprint is the one-cell ring around the cell of the unit's POSITION (the block's centre), not its block
                end = c
                hit = True
                break
            okE = self.ab_col(cx + 1, cy - cls, cy)
            okW = self.ab_col(cx - cls - 1, cy - cls, cy)
            okS = self.ab_row(cy + 1, cx - cls, cx)
            okN = self.ab_row(cy - cls - 1, cx - cls, cx)
            gc = g[c]
            for ddx, ddy in ABN:
                if ddx and ddy:
                    if not ((okE if ddx > 0 else okW) and (okS if ddy > 0 else okN)):
                        continue
                    if not self.ab_free(cx + (1 if ddx > 0 else -cls - 1), cy + (1 if ddy > 0 else -cls - 1)):
                        continue
                    cost = AB_B_D
                else:
                    if ddx:
                        ok = okE if ddx > 0 else okW
                    else:
                        ok = okS if ddy > 0 else okN
                    if not ok:
                        continue
                    cost = AB_B_S
                nx, ny = cx + ddx, cy + ddy
                n = ny * w + nx
                ng = gc + cost
                if stamp[n] == gen:
                    if g[n] <= ng:
                        continue
                    if closed[n] == gen:
                        closed[n] = 0
                    else:
                        bgen[n] += 1
                else:
                    stamp[n] = gen
                    closed[n] = 0
                    bgen[n] = 0
                    ex, ey = nx - tx, ny - ty
                    d2 = ex * ex + ey * ey
                    if d2 < near_d:
                        near_d = d2
                        near = n
                g[n] = ng
                parent[n] = c
                self.ab_push(ng + self.ab_hb(nx - tx, ny - ty), n, bgen[n])
                self.wk += 1
        self.ab_bpops = pops
        self.ab_bhit = hit
        self.ab_bgoal = goal
        if end < 0:
            end = near
        cells = []
        c = end
        while c >= 0:
            cells.append((c % w, c // w))
            c = parent[c]
            self.wk += 1
        cells.reverse()
        self.ab_cells = cells
        return goal or hit

    @staticmethod
    def ab_hb(dx, dy):
        """octagon approximation of 15 x the Euclid distance [obs 0x6f66db00]"""
        if dx < 0:
            dx = -dx
        if dy < 0:
            dy = -dy
        a = 15 * (dx if dx > dy else dy)
        b = 15 * (dy if dx > dy else dx)
        if (a >> 2) < b:
            b2 = b + (b >> 1)
            return a + (a >> 6) + (b2 >> 2) + (b2 >> 7) - (a >> 4)
        return a

    # ------------------------------------------------------------------ the route (2.4)
    def ab_los(self, x0, y0, x1, y1, cls=0):
        """the follower's line of sight [obs 2.6 0x6f67f6e0 / 0x6f49bd10]: from the position along the segment in steps of 1.0 cell; every step that
        enters a new cell tests the cells the footprint block of the unit's class newly covers; the end point itself is not tested; no cache"""
        dx = x1 - x0
        dy = y1 - y0
        ln = math.sqrt(dx * dx + dy * dy)
        if not ln > 32.0:
            return True
        sx = dx / ln * 32.0
        sy = dy / ln * 32.0
        o = 16.0 * cls                                  # STEER-4: the block of class cls is centred 16 cls units below / left of its node cell's centre: node = cell of (position + o)
        cx, cy = self.cell_of(x0 + o, y0 + o)
        k = 1
        while k * 32.0 < ln:
            self.wk += 1
            nx, ny = self.cell_of(x0 + sx * k + o, y0 + sy * k + o)
            if nx != cx or ny != cy:
                for yy in range(ny - cls, ny + 1):
                    for xx in range(nx - cls, nx + 1):
                        if cx - cls <= xx <= cx and cy - cls <= yy <= cy:
                            continue
                        if not self.ab_free(xx, yy):
                            return False
                cx = nx
                cy = ny
            k += 1
        return True

    def ab_find(self, x0, y0, x1, y1, tgt=None, cls=0, rt=None):
        """world points -> the raw fine route [(x, y)] (cell centres, no smoothing: the follower smooths, engine.ab_aim): search A plans the way, search B
        the walk to the end of the first long leg of it [inf, 2.4]; the A route lives in the mover's route object `rt` (AbRoute) and is consumed leg by leg;
        a route that is not complete (more legs, budget) has last_limited set and the engine asks again where it ends.
        last_exp = pops of both searches, last_reached = search A reached the goal."""
        if self.ab_st is None:
            self.ab_build()
        sx, sy = self.cell_of(x0, y0)
        gx, gy = self.cell_of(x1, y1)
        o = 16.0 * cls                                  # STEER-4: node cell of the block of class cls = cell of (position + 16 cls); route points are node centres - 16 cls
        sh = (16 + 16 * cls) >> 5                       # ... an A0-plain cell c is the node c + sh
        bsx, bsy = self.cell_of(x0 + o, y0 + o) if cls else (sx, sy)
        self.last_limited = False
        self.last_reached = True
        self.last_refused = False
        self.last_exp = 0
        if rt is None:
            rt = AbRoute()
        if not self.inside(sx, sy):
            self.last_reached = False
            return []                                   # start outside the grid: no path
        if sx == gx and sy == gy:
            return [(x1, y1)]                           # start cell == goal cell [obs 0x6f66d570]
        cb = self.ab_cb
        if cb is not None and not cb.pb_gate(1):
            self.last_refused = True                    # PATH-AB step 3: the mover's throttle or the player's quota refuses the request: no search, asked again next tick
            self.last_reached = False                   # (search B is asked first: it nearly always follows, and a refusal costs next to nothing)
            return []
        if tgt is None:
            tgt = self.ab_goal_rect(gx, gy)
        i0 = 0
        st = -1
        if rt.n > 0 and rt.gx == gx and rt.gy == gy:
            i0 = rt.i
            if rt.ok:
                i0 = rt.gt + 1 if rt.gt else rt.e       # the last leg's fine search reached its aim: the next leg starts there
            if i0 < rt.n - 1:
                st = 2                                  # a leg is left in the route (PATH-AB step 4: the route object of the mover, not a search)
            else:
                i0 = 0                                  # used up: a new search A, its route starts at entry 0
        if cb is not None and st < 0 and not cb.pb_gate(0):
            self.last_refused = True                    # ... and search A when the route in the mover has no leg left
            self.last_reached = False
            return []
        if st >= 0:
            n = rt.n
            for j in range(n):
                self.ar_x[j] = rt.x[j]
                self.ar_y[j] = rt.y[j]
                self.ar_k[j] = rt.k[j]
            self.ar_n = n
            self.ab_reached = rt.reached
            self.ab_near_d = rt.nd
            self.ab_apops = 0
            self.wk += (n >> 2) + 1
        else:
            if tgt is not None:
                self.ab_ovr = tgt
                self.ab_rebuild(tgt[0], tgt[1], tgt[2], tgt[3])
            st = self.ab_a(sx >> 1, sy >> 1, gx >> 1, gy >> 1, AB_BUDGET_A_CLUSTER)       # STEER-3: every ground order forms a cluster (even for one unit): its route has the A budget 5000 and the player's record R0 [obs 0x6f49ad00 / 0x6f49b2f0]
            if cb is not None:
                cb.pb_charge(0, self.ab_apops)
            if tgt is not None:
                self.ab_ovr = None
                self.ab_rebuild(tgt[0], tgt[1], tgt[2], tgt[3])
            self.st_asearch += 1
            self.st_apops += self.ab_apops
            rt.n = 0
            if st == 2:
                n = self.ar_n
                rt.x = self.ar_x[:n]
                rt.y = self.ar_y[:n]
                rt.k = self.ar_k[:n]
                rt.n = n
                rt.i = 0
                rt.ok = False
                rt.reached = self.ab_reached
                rt.nd = self.ab_near_d
                rt.gx = gx
                rt.gy = gy
                self.wk += (n >> 2) + 1
        pops = self.ab_apops
        self.last_exp = pops
        if st == 0:
            self.last_reached = False
            return []
        if st == 3:
            self.last_reached = self.ab_near_d <= 2     # the nearest node is the start: nowhere to go [obs 2.2]
            self.last_limited = True
            return []
        areached = self.ab_reached
        self.last_reached = areached or (st == 2 and self.ab_near_d <= 2)    # [inf] a goal in a blocked A0 cell: the neighbouring cell is as near as the unit can come (engine: a chase is not given up)
        n = self.ar_n
        ax, ay, ak = self.ar_x, self.ar_y, self.ar_k
        gate = 0                                        # slot of the marker that ends the leg (0 = none)
        final = True
        gt = 0
        e = 0
        if st == 1:
            aim_x, aim_y = gx, gy
            exact = True
        else:
            i = i0 + 1
            while True:
                e = n - 1
                gt = 0
                j = i
                acc = 0
                while j < n:
                    if ak[j] > 0:
                        e = j - 1
                        gt = j
                        break
                    ddx = ax[j] - ax[j - 1]
                    ddy = ay[j] - ay[j - 1]
                    acc += math.isqrt(256 * (ddx * ddx + ddy * ddy))
                    if acc >= AB_LEG16:
                        e = j
                        break
                    j += 1
                if gt == 0 and e == n - 1:
                    final = True
                    if areached:
                        aim_x, aim_y = gx, gy
                        exact = True
                    else:
                        aim_x, aim_y = 2 * ax[e] + 1, 2 * ay[e] + 1       # the nearest node's cell centre
                        exact = False
                    break
                final = False
                aim_x, aim_y = 2 * ax[e], 2 * ay[e]
                exact = False
                if aim_x + sh == bsx and aim_y + sh == bsy and not gt:
                    i = e + 1
                    continue
                if gt:
                    gate = int(ak[gt])
                break
        if final and tgt is not None and cls:
            cls = 0                                     # STEER-4 [inf]: the last leg to a target footprint (mine, tree, building) is searched as class 0: the arrival of an order on a target is its interaction range (the harvest reach, 144 + radii), which a block of
            o = 0.0                                     # the unit's class would stop up to a cell and a half short of
            sh = 0
            bsx, bsy = sx, sy
        aim_x += sh
        aim_y += sh
        cells = [(bsx, bsy)]
        reached = True
        if aim_x != bsx or aim_y != bsy:
            reached = self.ab_b(bsx, bsy, aim_x, aim_y, cls, tgt if final else None, AB_BUDGET_B)
            cells = self.ab_cells
            pops += self.ab_bpops
            self.st_bsearch += 1
            self.st_bpops += self.ab_bpops
            self.last_exp = pops
            if cb is not None:
                cb.pb_charge(1, self.ab_bpops)
        if st == 2:
            rt.i = i0                                   # the leg served: it started at entry i0 (asked again when its fine search did not reach the aim) ...
            rt.e = e                                    # ... and the next one starts at its end when the fine search reached its aim
            rt.gt = gt
            rt.ok = reached
        pts = []
        for k in range(1, len(cells)):
            px, py = self.center(cells[k][0], cells[k][1])
            pts.append((px - o, py - o) if cls else (px, py))
        if final and exact and self.ab_bgoal and pts:
            pts[-1] = (x1, y1)
        if gate:
            pts.append((-1.0e9, float(gate)))             # pathing.MARK_X; the landing point follows
            pts.append(self.gate_point(gate))
        if pts and pts[0][0] == -1.0e9:
            pts.insert(0, (x0, y0))                       # the unit stands on the gate cell: it is its own gate cell (the marker follows the point it stands at)
        self.last_limited = (not final) or (not areached) or (not reached)
        return pts
