"""Bot navigation mesh generator for CS 1.6 maps (players without Condition Zero have no czero/maps/*.nav).

BSP v30 (or a GSV3 pack) → the same structure nav5.parse() returns, so pack.py's nav_sections() takes it as a drop-in.
Clean-room re-implementation of the idea of the game's own generator (cs.so: CCSBot::LearnStep / AddNode,
GenerateNavigationAreaMesh, ConnectGeneratedAreas, MergeGeneratedAreas, SplitX/Y, MarkJumpAreas, ComputeHidingSpots);
parameters and their provenance ([obs] read from the decompile, [inf] our choice): decomp/cstrike/NOTES_nav.md.

  1. nodes (game semantics): flood fill from every info_player_start / info_player_deathmatch (+ ladder ends) on a
     25 u grid; neighbour ground by layered downward line traces (GetGroundHeight), rise ≤ 58 u, drop ≤ 200 u, a
     clear line at waist height (36 u, retried at 18 u), walkable ground (normal.z ≥ 0.7) along the step. Drops
     > 58 u therefore link one way. Higher floor layers within jump height become extra (non-grid) links.
     Player check on top (ours): a hull-1 box (else hull-3 → crouch node) must fit within 16 u of the node and the
     box must rise / move / drop between both fit positions — this honours player clip brushes, which hull 0 lacks.
  2. cells: 4 mutually bi-linked nodes. Cells grow into planar regions (all corners within 5 u of the seed cell's
     plane, same crouch flag); inside a region rectangles are cut greedily largest-first (50×50 cells down to 1×1);
     then areas sharing a full edge with the same flags and normals within 0.99 merge, and areas with an aspect
     ratio outside [0.332, 3.01] are halved on the grid.
  3. areas: extents = NW/SE corner nodes, neZ/swZ = NE/SW corner heights; jump flag when the area plane is steeper
     than normal.z 0.7. Connections from bi-linked cell neighbours (both ways), extra layer links, and one-way
     drop-down links.
  4. hiding spots at area corners whose two edges have no neighbour within 20 u (cover flag: ≥ 8 of 16 waist-height
     rays blocked within 100 u, or a ceiling right above); places from func_bomb_target (BombsiteA/B), hostages,
     func_buyzone and spawns (TSpawn/CTSpawn). No approach areas / encounter paths / sniper flags (pack.py
     does not use them).
usage: python3 -I tools/navgen.py <map.bsp | pack.bin> [--out map.nav] [--png out.png] [--json out.json]
"""
import math, os, re, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STEP = 25.0             # [obs] GenerationStepSize
STEP_HEIGHT = 18.0      # [obs] StepHeight = ducking waist height in LearnStep; also our box-fit lift [inf]
JUMP_CROUCH = 58.0      # [obs] JumpCrouchHeight; LearnStep rejects rises > 58.1
MAX_DROP = 200.0        # [obs] LearnStep rejects drops > 200
MIN_NORMAL_Z = 0.7      # [obs] LearnStep ground normal.z < 0.7 → not walkable; MarkJumpAreas threshold
NODE_Z_TOL = 11.25      # [obs] CNavNode::GetNode tolerance
PLANE_TOL = 5.0         # [obs] TestArea off-plane tolerance
MAX_RECT = 50           # [obs] GenerateNavigationAreaMesh starts at 50×50 nodes
ASPECT = (0.332226, 3.01)  # [obs] SplitX/SplitY
COPLANAR = 0.99         # [obs] CNavArea::IsCoplanar
HULL1_HALF, HULL3_HALF = 36.0, 18.0
FIT_OFFSETS = sorted(((ox, oy) for ox in range(-16, 17, 4) for oy in range(-16, 17, 4)), key=lambda o: o[0] ** 2 + o[1] ** 2)
EPS = 0.03125           # DIST_EPSILON
SOLID, SKY = -2, -6
DIRS = ((0, -1), (1, 0), (0, 1), (-1, 0))   # north (−y), east, south, west
OPP = (2, 3, 0, 1)
# CFuncMonsterClip (func_monsterclip) is solid to NPCs only: pack.py keeps it a player volume (INVISIBLE_CLASSES),
# while the monster nav mesh must route around its brush model (HL pev->flags & FL_MONSTERCLIP).
BLOCKING = ("func_wall", "func_wall_toggle", "func_pushable", "func_conveyor", "func_monsterclip")


# ───────────────────────── collision ─────────────────────────

class Clip:
    """Plane/node collision trees of one map (hull 0 = BSP nodes with leaf contents, hulls 1-3 = clipnodes) plus the
    solid brush entities, i.e. the pack's PLAN/HNOD/MODL data. Line/box traces like the engine's SV_RecursiveHullCheck."""

    def __init__(self, planes, nodes, models, ents):
        self.px, self.py, self.pz, self.pd, self.pt = [], [], [], [], []
        for nx, ny, nz, d in planes:
            self.px.append(nx); self.py.append(ny); self.pz.append(nz); self.pd.append(d)
            t = 3
            for k, v in enumerate((nx, ny, nz)):
                if abs(v) > 0.99999:
                    t = k
            self.pt.append(t)
        self.npl = [n[0] for n in nodes]
        self.c0 = [n[1] for n in nodes]
        self.c1 = [n[2] for n in nodes]
        self.models = models            # [(heads[4], origin, mins, maxs)] — index 0 = world
        self.ents = ents
        self.world = models[0][0]
        self.blockers = []              # (heads, origin, mins, maxs) of solid brush entities
        for e in ents:
            m = e.get("model", "")
            if not m.startswith("*"):
                continue
            cls = e.get("classname", "")
            try:
                mi = int(m[1:])
                sf = int(float(e.get("spawnflags", "0") or 0))
            except ValueError:
                continue
            solid = cls in BLOCKING or (cls == "func_breakable" and sf & 1)
            if solid and 0 < mi < len(models):
                heads, _o, mn, mx = models[mi]
                o = num3(e.get("origin", "0 0 0"))
                self.blockers.append((heads, o, mn, mx))
        self.ntrace = 0

    def contents(self, num, x, y, z):
        px, py, pz, pd, pt, npl, c0, c1 = self.px, self.py, self.pz, self.pd, self.pt, self.npl, self.c0, self.c1
        while num >= 0:
            p = npl[num]
            t = pt[p]
            if t == 0: d = x - pd[p] if px[p] > 0 else -x - pd[p]
            elif t == 1: d = y - pd[p] if py[p] > 0 else -y - pd[p]
            elif t == 2: d = z - pd[p] if pz[p] > 0 else -z - pd[p]
            else: d = px[p] * x + py[p] * y + pz[p] * z - pd[p]
            num = c0[num] if d >= 0 else c1[num]
        return num

    def solid_at(self, hull, x, y, z):
        c = self.contents(self.world[hull], x, y, z)
        if c == SOLID or c == SKY:
            return True
        for heads, o, mn, mx in self.blockers:
            if mn[0] - 17 <= x - o[0] <= mx[0] + 17 and mn[1] - 17 <= y - o[1] <= mx[1] + 17 and \
               mn[2] - 37 <= z - o[2] <= mx[2] + 37:
                c = self.contents(heads[hull], x - o[0], y - o[1], z - o[2])
                if c == SOLID or c == SKY:
                    return True
        return False

    def _trace1(self, head, x1, y1, z1, x2, y2, z2):
        """One tree: → [fraction, nx, ny, nz, startsolid, allsolid]."""
        px, py, pz, pd, pt, npl, c0, c1 = self.px, self.py, self.pz, self.pd, self.pt, self.npl, self.c0, self.c1
        tr = [1.0, 0.0, 0.0, 0.0, False, True]
        contents = self.contents
        dx, dy, dz = x2 - x1, y2 - y1, z2 - z1

        def rec(num, f1, f2):
            if num < 0:
                if num != SOLID and num != SKY:
                    tr[5] = False
                else:
                    tr[4] = True
                return True
            p = npl[num]
            ax, ay, az = x1 + dx * f1, y1 + dy * f1, z1 + dz * f1
            bx, by, bz = x1 + dx * f2, y1 + dy * f2, z1 + dz * f2
            t = pt[p]
            if t == 2:
                s = 1.0 if pz[p] > 0 else -1.0
                t1 = s * az - pd[p]; t2 = s * bz - pd[p]
            elif t == 0:
                s = 1.0 if px[p] > 0 else -1.0
                t1 = s * ax - pd[p]; t2 = s * bx - pd[p]
            elif t == 1:
                s = 1.0 if py[p] > 0 else -1.0
                t1 = s * ay - pd[p]; t2 = s * by - pd[p]
            else:
                t1 = px[p] * ax + py[p] * ay + pz[p] * az - pd[p]
                t2 = px[p] * bx + py[p] * by + pz[p] * bz - pd[p]
            if t1 >= 0 and t2 >= 0:
                return rec(c0[num], f1, f2)
            if t1 < 0 and t2 < 0:
                return rec(c1[num], f1, f2)
            if t1 < 0:
                frac = (t1 + EPS) / (t1 - t2); side = 1
            else:
                frac = (t1 - EPS) / (t1 - t2); side = 0
            frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
            mf = f1 + (f2 - f1) * frac
            near, far = (c0[num], c1[num]) if side == 0 else (c1[num], c0[num])
            if not rec(near, f1, mf):
                return False
            mx, my, mz = x1 + dx * mf, y1 + dy * mf, z1 + dz * mf
            c = contents(far, mx, my, mz)
            if c != SOLID and c != SKY:
                return rec(far, mf, f2)
            if tr[5]:
                return False
            sgn = 1.0 if side == 0 else -1.0
            tr[1], tr[2], tr[3] = px[p] * sgn, py[p] * sgn, pz[p] * sgn
            while True:
                c = contents(head, x1 + dx * mf, y1 + dy * mf, z1 + dz * mf)
                if c != SOLID and c != SKY:
                    break
                frac -= 0.1
                if frac < 0:
                    break
                mf = f1 + (f2 - f1) * frac
            tr[0] = max(mf, 0.0)
            return False
        rec(head, 0.0, 1.0)
        return tr

    def trace(self, hull, a, b):
        """World + solid brush entities → (fraction, end, normal_z, startsolid). Engine semantics: a segment that
        never leaves solid keeps fraction 1 (startsolid set); fraction < 1 only when it enters solid from open space."""
        self.ntrace += 1
        x1, y1, z1 = a; x2, y2, z2 = b
        tr = self._trace1(self.world[hull], x1, y1, z1, x2, y2, z2)
        if self.blockers:
            lx, hx, ly, hy, lz, hz = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2), min(z1, z2), max(z1, z2)
            for heads, o, mn, mx in self.blockers:
                if hx - o[0] < mn[0] - 17 or lx - o[0] > mx[0] + 17 or hy - o[1] < mn[1] - 17 or \
                   ly - o[1] > mx[1] + 17 or hz - o[2] < mn[2] - 37 or lz - o[2] > mx[2] + 37:
                    continue
                t = self._trace1(heads[hull], x1 - o[0], y1 - o[1], z1 - o[2], x2 - o[0], y2 - o[1], z2 - o[2])
                ss = tr[4] or t[4]
                if t[0] < tr[0]:
                    tr = [t[0], t[1], t[2], t[3], ss, False]
                tr[4] = ss
        f = tr[0]
        self.last_normal = (tr[1], tr[2], tr[3])
        return f, (x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, z1 + (z2 - z1) * f), tr[3], tr[4]


def num3(s):
    try:
        v = [float(x) for x in s.split()[:3]]
    except ValueError:
        v = []
    return tuple(v + [0.0] * (3 - len(v)))


def parse_entities(text):
    out, cur = [], None
    for m in re.finditer(r'\{|\}|"([^"]*)"\s*"([^"]*)"', text):
        tok = m.group(0)
        if tok == "{":
            cur = {}
        elif tok == "}":
            if cur is not None:
                out.append(cur)
            cur = None
        elif cur is not None:
            cur.setdefault(m.group(1), m.group(2))
    return out


def load(path):
    """BSP v30 or GSV3 pack → (Clip, bsp file size or 0)."""
    head = open(path, "rb").read(4)
    if head == b"GSV3":
        import contextlib, io, gsv3_check
        with contextlib.redirect_stdout(io.StringIO()):
            P = gsv3_check.decode(path)
        ents = parse_entities(P["text"])
        models = {}
        for r in P["recs"]:
            models[r["model"]] = (r["head"], r["origin"], r["mins"], r["maxs"])
        ml = [models.get(i, ((-1, -1, -1, -1), (0, 0, 0), (0, 0, 0), (0, 0, 0))) for i in range(max(models) + 1)]
        return Clip(P["planes"], P["nodes"], ml, ents), 0
    from bsp30 import Bsp
    b = Bsp(path)
    H0 = len(b.nodes)
    def c0(c): return c if c >= 0 else b.leaf_contents[-(c + 1)]
    def c1(c): return c + H0 if c >= 0 else c
    nodes = [(pl, c0(a), c0(bb)) for pl, a, bb in b.nodes] + [(pl, c1(a), c1(bb)) for pl, a, bb in b.clipnodes]
    models = []
    for m in b.models:
        h = m[9:13]
        models.append(((c0(h[0]) if h[0] < 0 else h[0], c1(h[1]), c1(h[2]), c1(h[3])), m[6:9], m[0:3], m[3:6]))
    planes = [p[:4] for p in b.planes]
    return Clip(planes, nodes, models, b.entity_list()), os.path.getsize(path)


# ───────────────────────── nodes ─────────────────────────

class Node:
    __slots__ = ("i", "x", "y", "z", "gx", "gy", "crouch", "to", "cell", "nz", "fit", "n3", "extra")

    def __init__(self, i, gx, gy, z, nz):
        self.i, self.gx, self.gy, self.z, self.nz = i, gx, gy, z, nz
        self.x, self.y = gx * STEP, gy * STEP
        self.to = [None, None, None, None]
        self.cell = None
        self.n3 = (0.0, 0.0, 1.0)
        self.extra = []         # [(direction, node)] links to other floor layers of the neighbour column
        self.fit = None         # (hull, x, y, origin z) of a nearby player box position; None = no player fits here
        self.crouch = False


class Gen:
    """Node flood fill with line traces (hull 0), after the game's LearnStep/AddNode: ground by layered downward
    traces, a clear line at waist height between the two grounds, walkable ground along the step."""

    def __init__(self, clip, log=print):
        self.C = clip
        self.log = log
        self.nodes = []
        self.grid = {}          # (gx, gy) → [Node]

    def find(self, gx, gy, z):
        for n in self.grid.get((gx, gy), ()):
            if abs(n.z - z) < NODE_Z_TOL:
                return n
        return None

    def add(self, gx, gy, z, nz):
        n = Node(len(self.nodes), gx, gy, z, nz)
        n.fit = self.box_fit(n.x, n.y, z)
        n.crouch = n.fit is not None and n.fit[0] == 3
        self.nodes.append(n)
        self.grid.setdefault((gx, gy), []).append(n)
        return n

    def box_fit(self, x, y, z):
        """Player check [inf]: the line-trace nodes may hug walls and ignore player clip brushes (hull 0 has none),
        so a node is only kept when a standing (hull 1) or else crouching (hull 3) box fits within 16 u of it."""
        solid_at = self.C.solid_at
        for hull, half in ((1, HULL1_HALF), (3, HULL3_HALF)):
            for lift in (1.0, STEP_HEIGHT):
                oz = z + half + lift
                for ox, oy in FIT_OFFSETS:
                    if not solid_at(hull, x + ox, y + oy, oz):
                        return hull, x + ox, y + oy, oz
        return None

    def passable(self, a, b):
        """Player check [inf]: box (hull 1, or hull 3 if either end is crouch-only) rises from a's fit position to the
        higher of both origins and moves across to b's fit position without hitting anything (clips included)."""
        hull = 1 if a.fit[0] == 1 and b.fit[0] == 1 else 3
        half = HULL1_HALF if hull == 1 else HULL3_HALF
        az = a.fit[3] if a.fit[0] == hull else a.z + half + 1.0
        bz = b.fit[3] if b.fit[0] == hull else b.z + half + 1.0
        top = max(az, bz)
        tr, solid_at = self.C.trace, self.C.solid_at
        oax, oay = a.fit[1] - a.x, a.fit[2] - a.y
        obx, oby = b.fit[1] - b.x, b.fit[2] - b.y
        # both fit positions, or one of them with the other end shifted by the same offset (doorway corners)
        for ax, ay, bx, by in ((a.fit[1], a.fit[2], b.fit[1], b.fit[2]), (a.fit[1], a.fit[2], b.x + oax, b.y + oay),
                               (a.x + obx, a.y + oby, b.fit[1], b.fit[2])):
            if solid_at(hull, ax, ay, az) or solid_at(hull, bx, by, bz):
                continue
            if top > az + 0.01:
                f, _e, _n, ss = tr(hull, (ax, ay, az), (ax, ay, top))
                if f < 1.0 or ss:
                    continue
            f, _e, _n, ss = tr(hull, (ax, ay, top), (bx, by, top))
            if f < 1.0 or ss:
                continue
            if top > bz + 0.01:                 # and the drop to b's origin is open (not through a floor)
                f, _e, _n, ss = tr(hull, (bx, by, top), (bx, by, bz))
                if f < 1.0 or ss:
                    continue
            return True
        return False

    def ground(self, x, y, z, all_layers=None):
        """GetGroundHeight: downward rays from z+1, z+11 … z+91 collect floor layers; the lowest layer with ≥ 36 u
        to the next one wins → (height, normal z, normal) or None. [obs] (we stop early once the space above is open)
        all_layers: optional list that receives every layer found."""
        tr = self.C.trace
        layers = []
        k = 0
        while k < 10:
            sz = z + 1.0 + 10.0 * k
            f, end, nz, ss = tr(0, (x, y, sz), (x, y, sz - 9999.9))
            if not ss and f < 1.0:
                if not layers or end[2] > layers[-1][0]:
                    layers.append((end[2], nz, self.C.last_normal))
                # nothing solid between here and the top start → every higher ray hits the same floor
                f2, _e2, _n2, ss2 = tr(0, (x, y, sz), (x, y, z + 91.0))
                if f2 >= 1.0 and not ss2:
                    break
            k += 1
        if all_layers is not None:
            all_layers.extend(layers)
        if not layers:
            return None
        for i in range(len(layers) - 1):
            if layers[i + 1][0] - layers[i][0] >= HULL1_HALF:
                return layers[i]
        return layers[-1]

    def waist_clear(self, n, bx, by, bz):
        tr = self.C.trace
        for waist in (36.0, 18.0):
            f, _e, _n, ss = tr(0, (n.x, n.y, n.z + waist), (bx, by, bz + waist))
            if f >= 1.0 and not ss:
                return True
        return False

    def step(self, n, d):
        """LearnStep for one direction → (ground, [other layers]) of the neighbour; ground = (z, normal z, normal) or
        None. [obs] except [inf]: waist height 18 u is retried for crouch passages, ground samples every 5 u instead
        of 2 u, and the other floor layers within jump/drop range are returned as extra (non-grid) links — the game
        keeps only the lowest layer with headroom, which misses e.g. jumping onto a platform that has a crawl space
        underneath."""
        bx, by = n.x + DIRS[d][0] * STEP, n.y + DIRS[d][1] * STEP
        layers = []
        g = self.ground(bx, by, n.z, layers)
        if g is None:
            return None, []
        extras = [h for h in layers if h[0] >= g[0] + HULL1_HALF and h[0] - n.z <= JUMP_CROUCH + 0.1
                  and h[1] >= MIN_NORMAL_Z and self.waist_clear(n, bx, by, h[0])]
        bz = g[0]
        dz = bz - n.z
        if dz > JUMP_CROUCH + 0.1 or -dz > MAX_DROP or not self.waist_clear(n, bx, by, bz):
            return None, extras
        for k in range(1, 6):
            t = k / 5.0
            sx, sy, sz = n.x + (bx - n.x) * t, n.y + (by - n.y) * t, n.z + dz * t
            h = g if k == 5 else self.ground(sx, sy, sz)
            if h is None or h[1] < MIN_NORMAL_Z:
                return None, extras
        return g, extras

    def seed(self, x, y, z):
        """Ground node at the grid point nearest to (x, y) below z (≤ 2 rings around it)."""
        gx0, gy0 = int(round(x / STEP)), int(round(y / STEP))
        for ring in range(3):
            for gx in range(gx0 - ring, gx0 + ring + 1):
                for gy in range(gy0 - ring, gy0 + ring + 1):
                    if max(abs(gx - gx0), abs(gy - gy0)) != ring:
                        continue
                    g = self.ground(gx * STEP, gy * STEP, z)
                    if g is None or g[1] < MIN_NORMAL_Z or z - g[0] > 300.0:
                        continue
                    n = self.find(gx, gy, g[0])
                    if n is None:
                        n = self.add(gx, gy, g[0], g[1])
                        n.n3 = g[2]
                    return n
        return None

    def flood(self, seeds):
        queue = [s for s in seeds if s is not None and s.fit is not None]
        seen = set(n.i for n in queue)
        k = 0
        while k < len(queue):
            n = queue[k]; k += 1
            for d in range(4):
                if n.to[d] is not None:
                    continue
                r, extras = self.step(n, d)
                gx, gy = n.gx + DIRS[d][0], n.gy + DIRS[d][1]
                for li, g in enumerate([r] + extras):
                    if g is None:
                        continue
                    m = self.find(gx, gy, g[0])
                    if m is None:
                        m = self.add(gx, gy, g[0], g[1])
                        m.n3 = g[2]
                    if m.fit is None or m is n.to[d] or not self.passable(n, m):
                        continue
                    if li == 0:
                        n.to[d] = m
                    else:
                        n.extra.append((d, m))
                    if m.i not in seen:
                        seen.add(m.i)
                        queue.append(m)


# ───────────────────────── cells → areas ─────────────────────────

def bilinked(a, d):
    b = a.to[d]
    return b is not None and b.to[OPP[d]] is a


class Cell:
    __slots__ = ("nw", "ne", "se", "sw", "gx", "gy", "crouch", "region", "area", "covered")

    def __init__(self, nw, ne, se, sw):
        self.nw, self.ne, self.se, self.sw = nw, ne, se, sw
        self.gx, self.gy, self.crouch = nw.gx, nw.gy, nw.crouch
        self.region = self.area = None
        self.covered = False


def build_cells(nodes):
    cells = []
    for n in nodes:
        if not (bilinked(n, 1) and bilinked(n, 2)):
            continue
        e, s = n.to[1], n.to[2]
        se = e.to[2]
        if se is None or not bilinked(e, 2) or s.to[1] is not se or not bilinked(s, 1):
            continue
        c = Cell(n, e, se, s)
        n.cell = c
        cells.append(c)
    return cells


def plane_of(c):
    """Plane through the cell's NW corner spanned by NE and SW → (nx, ny, nz, d)."""
    ux, uz = c.ne.x - c.nw.x, c.ne.z - c.nw.z
    vy, vz = c.sw.y - c.nw.y, c.sw.z - c.nw.z
    nx, ny, nz = -uz * vy, -ux * vz, ux * vy
    l = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / l, ny / l, nz / l
    return nx, ny, nz, nx * c.nw.x + ny * c.nw.y + nz * c.nw.z


def regions(cells):
    """Grow planar regions over linked cells: corners within PLANE_TOL of the seed plane, same crouch flag."""
    out = []
    for seed in cells:
        if seed.region is not None:
            continue
        zs = (seed.nw.z, seed.ne.z, seed.se.z, seed.sw.z)
        if max(zs) - min(zs) <= 2.0:        # flat seed: horizontal plane through its mean height [inf]
            nx, ny, nz, d = 0.0, 0.0, 1.0, sum(zs) / 4
        else:                               # sloped: plane of its corners [inf] (game: start node's ground normal)
            nx, ny, nz, d = plane_of(seed)
        rid = len(out)
        seed.region = rid
        members, stack = [seed], [seed]

        def ok(c):
            if c.region is not None or c.crouch != seed.crouch:
                return False
            for n in (c.nw, c.ne, c.se, c.sw):
                if abs(nx * n.x + ny * n.y + nz * n.z - d) > PLANE_TOL:
                    return False
            return True
        while stack:
            c = stack.pop()
            # neighbour cells: north = cell of nw.to[N]; west = cell of nw.to[W]; east = cell of ne; south = cell of sw
            nb = []
            if bilinked(c.nw, 0) and c.nw.to[0].cell is not None and c.nw.to[0].cell.sw is c.nw:
                nb.append(c.nw.to[0].cell)
            if bilinked(c.nw, 3) and c.nw.to[3].cell is not None and c.nw.to[3].cell.ne is c.nw:
                nb.append(c.nw.to[3].cell)
            if c.ne.cell is not None:
                nb.append(c.ne.cell)
            if c.sw.cell is not None:
                nb.append(c.sw.cell)
            for m in nb:
                if ok(m):
                    m.region = rid
                    members.append(m)
                    stack.append(m)
        out.append(members)
    return out


def rectangles(members):
    """Greedy largest-first rectangles of a planar region (2D grid of unique (gx, gy)) → [(gx, gy, w, h)]."""
    at = {(c.gx, c.gy): c for c in members}
    run = {}

    def rerun(gy, gx_from):
        x = gx_from
        nxt = 0
        while (x, gy) in at:
            c = at[(x, gy)]
            nxt = 0 if c.covered else nxt + 1
            run[(x, gy)] = nxt
            x -= 1
    xs = sorted(at, key=lambda k: (k[1], -k[0]))
    for key in xs:                      # east→west per row
        c = at[key]
        r = 0 if c.covered else run.get((key[0] + 1, key[1]), 0) + 1
        run[key] = r
    order = sorted(at, key=lambda k: (k[1], k[0]))
    maxrun = max(run.values())
    gys = [k[1] for k in at]
    maxh = max(gys) - min(gys) + 1
    rects = []
    w = h = MAX_RECT
    left = len(at)
    while w >= 1 and h >= 1 and left:
        if w <= maxrun and h <= maxh:
            for key in order:
                c = at[key]
                if c.covered or run[key] < w:
                    continue
                gx, gy = key
                ok = True
                for k in range(1, h):
                    r = run.get((gx, gy + k))
                    if r is None or r < w:
                        ok = False; break
                if not ok:
                    continue
                for k in range(h):
                    for j in range(w):
                        at[(gx + j, gy + k)].covered = True
                    rerun(gy + k, gx + w - 1)
                left -= w * h
                rects.append((gx, gy, w, h))
        if w < h:
            h -= 1
        else:
            w -= 1
    return rects, at


class Area:
    __slots__ = ("id", "w", "h", "crouch", "nw", "ne", "se", "sw", "flags", "connect", "spots", "place", "cells")


def new_area(nw, ne, se, sw, w, h, flags):
    a = Area()
    a.nw, a.ne, a.se, a.sw, a.w, a.h, a.flags = nw, ne, se, sw, w, h, flags
    a.crouch = bool(flags & 1)
    a.connect = [set(), set(), set(), set()]
    a.spots, a.place, a.cells = [], 0, []
    return a


def area_normal(a):
    ux, uz = a.ne.x - a.nw.x, a.ne.z - a.nw.z
    vy, vz = a.sw.y - a.nw.y, a.sw.z - a.nw.z
    nx, ny, nz = -uz * vy, -ux * vz, ux * vy
    l = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (nx / l, ny / l, nz / l) if l > 0 else (0.0, 0.0, 1.0)


def coplanar(a, b):
    na, nb = area_normal(a), area_normal(b)
    return na[0] * nb[0] + na[1] * nb[1] + na[2] * nb[2] > COPLANAR


def merge_areas(areas):
    """MergeGeneratedAreas [obs]: neighbours sharing a full edge (the same two corner nodes), same flags, normals
    within 0.99 → one area; repeated until nothing merges."""
    alive = set(areas)
    by_nw = {a.nw.i: a for a in areas}
    changed = True
    while changed:
        changed = False
        for a in areas:
            if a not in alive:
                continue
            for b, east in ((by_nw.get(a.ne.i), True), (by_nw.get(a.sw.i), False)):
                if b is None or b is a or b not in alive or b.flags != a.flags:
                    continue
                if east and not (b.nw is a.ne and b.sw is a.se):
                    continue
                if not east and not (b.nw is a.sw and b.ne is a.se):
                    continue
                if not coplanar(a, b):
                    continue
                if east:
                    a.ne, a.se, a.w = b.ne, b.se, a.w + b.w
                else:
                    a.sw, a.se, a.h = b.sw, b.se, a.h + b.h
                a.cells += b.cells
                alive.discard(b)
                del by_nw[b.nw.i]
                changed = True
                break
    return [a for a in areas if a in alive]


def walk(n, d, k):
    for _ in range(k):
        n = n.to[d]
    return n


def split_area(a):
    """SplitX/SplitY [obs]: aspect ratio outside [0.332, 3.01] → halves on the grid, recursively."""
    asp = a.w / a.h
    if asp > ASPECT[1] and a.w >= 2:
        k = a.w // 2
        b = new_area(walk(a.nw, 1, k), a.ne, a.se, walk(a.sw, 1, k), a.w - k, a.h, a.flags)
        a.ne, a.se, a.w = b.nw, b.sw, k
    elif asp < ASPECT[0] and a.h >= 2:
        k = a.h // 2
        b = new_area(walk(a.nw, 2, k), walk(a.ne, 2, k), a.se, a.sw, a.w, a.h - k, a.flags)
        a.sw, a.se, a.h = b.nw, b.ne, k
    else:
        return [a]
    def inside(c): return a.nw.x <= c.nw.x < a.ne.x - 1e-3 and a.nw.y <= c.nw.y < a.sw.y - 1e-3
    b.cells = [c for c in a.cells if not inside(c)]
    a.cells = [c for c in a.cells if inside(c)]
    return split_area(a) + split_area(b)


def area_normal_z(a):
    return area_normal(a)[2]


def area_z(a, x, y):
    u = (x - a.nw.x) / max(1e-6, a.ne.x - a.nw.x)
    v = (y - a.nw.y) / max(1e-6, a.sw.y - a.nw.y)
    u, v = min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)
    north = a.nw.z + (a.ne.z - a.nw.z) * u
    south = a.sw.z + (a.se.z - a.sw.z) * u
    return north + (south - north) * v


class AreaGrid:
    def __init__(self, areas, cell=200.0):
        self.cell, self.g = cell, {}
        for a in areas:
            for i in range(int(math.floor(a.nw.x / cell)), int(math.floor(a.se.x / cell)) + 1):
                for j in range(int(math.floor(a.nw.y / cell)), int(math.floor(a.se.y / cell)) + 1):
                    self.g.setdefault((i, j), []).append(a)

    def at(self, x, y):
        return [a for a in self.g.get((int(math.floor(x / self.cell)), int(math.floor(y / self.cell))), ())
                if a.nw.x <= x <= a.se.x and a.nw.y <= y <= a.se.y]


def connect(areas, cells):
    for c in cells:
        a = c.area
        n = c.nw
        # bi-linked neighbours: north and west of every cell (south/east follow by symmetry)
        for d, m, back in ((0, n.to[0], "sw"), (3, n.to[3], "ne")):
            if m is None or not bilinked(n, d) or m.cell is None or getattr(m.cell, back) is not n:
                continue
            b = m.cell.area
            if b is not a:
                a.connect[d].add(b); b.connect[OPP[d]].add(a)
    # extra links between floor layers: every area touching the node → every area touching the target node
    corner = {}
    for c in cells:
        for n in (c.nw, c.ne, c.se, c.sw):
            corner.setdefault(n.i, set()).add(c.area)
    for c in cells:
        for n in (c.nw, c.ne, c.se, c.sw):
            for d, m in n.extra:
                for a in corner.get(n.i, ()):
                    for b in corner.get(m.i, ()):
                        if b is not a and all(b not in s_ for s_ in a.connect):
                            a.connect[d].add(b)
    grid = AreaGrid(areas)
    # one-way drop-downs: a link out of an area edge that is not linked back (drop > jump height)
    for c in cells:
        a = c.area
        for d, pair in ((0, (c.nw, c.ne)), (1, (c.ne, c.se)), (2, (c.sw, c.se)), (3, (c.nw, c.sw))):
            ts = [n.to[d] for n in pair if n.to[d] is not None and not bilinked(n, d)]
            if not ts:
                continue
            ex = (pair[0].x + pair[1].x) / 2 + DIRS[d][0] * STEP / 2
            ey = (pair[0].y + pair[1].y) / 2 + DIRS[d][1] * STEP / 2
            tz = min(t.z for t in ts)
            best, bd = None, 1e9
            for b in grid.at(ex, ey):
                if b is a:
                    continue
                dz = abs(area_z(b, ex, ey) - tz)
                if dz < bd:
                    best, bd = b, dz
            if best is not None and bd <= STEP_HEIGHT + 12.0 and best not in a.connect[d] and \
               all(best not in s for s in a.connect):
                a.connect[d].add(best)


# ───────────────────────── hiding spots / places ─────────────────────────

def in_cover(C, x, y, z):
    f, _e, _n, _s = C.trace(0, (x, y, z + 36.0), (x, y, z + 56.0))        # [obs] ceiling right above
    if f < 1.0:
        return True
    hits = 0
    for k in range(16):                                                    # [obs] 16 rays, 100 u, > 7 blocked
        ang = k * math.pi / 8
        f, _e, _n, _s = C.trace(0, (x, y, z + 36.0), (x + 100 * math.cos(ang), y + 100 * math.sin(ang), z + 72.0))
        hits += f < 1.0
    return hits > 7


def hiding_spots(C, areas):
    sid = 0
    for a in areas:
        if a.flags & 2:
            continue
        x0, y0, x1, y1 = a.nw.x, a.nw.y, a.se.x, a.se.y
        # coverage of each edge by bi-directionally connected, non-jump neighbours
        cov = []
        for d in range(4):
            lo, hi = 1e9, -1e9
            for b in a.connect[d]:
                if b.flags & 2 or a not in b.connect[OPP[d]]:
                    continue
                if d in (0, 2):
                    lo, hi = min(lo, b.nw.x), max(hi, b.se.x)
                else:
                    lo, hi = min(lo, b.nw.y), max(hi, b.se.y)
            cov.append((lo, hi))
        def open_lo(d, v): return cov[d][0] - v < 20.0      # [obs] 20 u: neighbour reaches the corner
        def open_hi(d, v): return v - cov[d][1] < 20.0
        corners = (
            (not open_lo(0, x0)) + (not open_lo(3, y0)) == 2 and (x0 + 12.5, y0 + 12.5, a.nw.z),
            (not open_hi(0, x1)) + (not open_lo(1, y0)) == 2 and (x1 - 12.5, y0 + 12.5, a.ne.z),
            (not open_lo(2, x0)) + (not open_hi(3, y1)) == 2 and (x0 + 12.5, y1 - 12.5, a.sw.z),
            (not open_hi(2, x1)) + (not open_hi(1, y1)) == 2 and (x1 - 12.5, y1 - 12.5, a.se.z))
        for p in corners:
            if not p:
                continue
            if any((p[0] - s["pos"][0]) ** 2 + (p[1] - s["pos"][1]) ** 2 + (p[2] - s["pos"][2]) ** 2 < 900.0
                   for s in a.spots):                                     # [obs] 30 u apart
                continue
            sid += 1
            a.spots.append({"id": sid, "pos": p, "flags": 1 if in_cover(C, *p) else 0})


# stock maps whose first func_bomb_target is site B (read off the CZ meshes' place names, navgen_validate) [inf]
BOMB_ORDER_SWAPPED = ("de_chateau", "de_dust2", "de_inferno", "de_train", "de_vertigo")


def places(C, areas, mapname=""):
    """Place names from map volumes (first match wins): bombsites (func_bomb_target / info_bomb_target),
    hostages (hostage_entity ± 200 u), buyzones (team key, else the nearer spawn kind), spawn surroundings (± 160 u).
    Order and radii are our choice [inf]; the game's own places come from hand-painted CZ meshes."""
    ents = C.ents
    names, idx = [], {}

    def pid(n):
        if n not in idx:
            names.append(n); idx[n] = len(names)
        return idx[n]
    vols = []
    bombs = [e for e in ents if e.get("classname") in ("func_bomb_target", "info_bomb_target")]
    letters = "BACDEFGH" if mapname in BOMB_ORDER_SWAPPED else "ABCDEFGH"
    for k, e in enumerate(bombs):
        vols.append(("Bombsite" + letters[min(k, 7)], *box_of(C, e, 64.0 if e.get("classname") == "info_bomb_target" else 0.0)))
    for e in ents:
        if e.get("classname") == "hostage_entity" and "origin" in e:
            s = num3(e["origin"])
            vols.append(("Hostages", (s[0] - 200, s[1] - 200, s[2] - 100), (s[0] + 200, s[1] + 200, s[2] + 100)))
    tsp = [num3(e["origin"]) for e in ents if e.get("classname") == "info_player_deathmatch" and "origin" in e]
    ctsp = [num3(e["origin"]) for e in ents if e.get("classname") == "info_player_start" and "origin" in e]
    for e in ents:
        if e.get("classname") != "func_buyzone":
            continue
        lo, hi = box_of(C, e, 0.0)
        team = e.get("team", "0")
        if team not in ("1", "2"):
            c = tuple((lo[q] + hi[q]) / 2 for q in range(3))
            dt = min((math.dist(c, s) for s in tsp), default=1e9)
            dc = min((math.dist(c, s) for s in ctsp), default=1e9)
            team = "1" if dt < dc else "2"
        vols.append(("TSpawn" if team == "1" else "CTSpawn", lo, hi))
    for s in tsp:
        vols.append(("TSpawn", (s[0] - 160, s[1] - 160, s[2] - 100), (s[0] + 160, s[1] + 160, s[2] + 100)))
    for s in ctsp:
        vols.append(("CTSpawn", (s[0] - 160, s[1] - 160, s[2] - 100), (s[0] + 160, s[1] + 160, s[2] + 100)))
    for a in areas:
        cx, cy = (a.nw.x + a.se.x) / 2, (a.nw.y + a.se.y) / 2
        cz = area_z(a, cx, cy) + 36.0
        for nm, lo, hi in vols:
            if lo[0] <= cx <= hi[0] and lo[1] <= cy <= hi[1] and lo[2] - 48 <= cz <= hi[2] + 48:
                a.place = pid(nm)
                break
    return names


def box_of(C, e, pad):
    m = e.get("model", "")
    o = num3(e.get("origin", "0 0 0"))
    if m.startswith("*"):
        try:
            _h, _o, mn, mx = C.models[int(m[1:])]
            return (mn[0] + o[0] - pad, mn[1] + o[1] - pad, mn[2] + o[2] - pad), (mx[0] + o[0] + pad, mx[1] + o[1] + pad, mx[2] + o[2] + pad)
        except (ValueError, IndexError):
            pass
    return (o[0] - pad, o[1] - pad, o[2] - pad), (o[0] + pad, o[1] + pad, o[2] + pad)


# ───────────────────────── driver ─────────────────────────

def generate(path, log=print):
    t0 = time.time()
    C, bsp_size = load(path)
    G = Gen(C, log)
    ents = C.ents
    spawns = [num3(e["origin"]) for e in ents
              if e.get("classname") in ("info_player_start", "info_player_deathmatch") and "origin" in e]
    seeds = [G.seed(*s) for s in spawns]
    # ladder ends (func_ladder volumes): seed the floors above and below, like the game's BuildLadders seeds [inf]
    for e in ents:
        if e.get("classname") != "func_ladder":
            continue
        lo, hi = box_of(C, e, 0.0)
        cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
        for ox, oy in ((40, 0), (-40, 0), (0, 40), (0, -40)):
            sx = cx + ox + (hi[0] - lo[0]) / 2 * (1 if ox > 0 else -1 if ox < 0 else 0)
            sy = cy + oy + (hi[1] - lo[1]) / 2 * (1 if oy > 0 else -1 if oy < 0 else 0)
            seeds.append(G.seed(sx, sy, hi[2] + 20.0))
            seeds.append(G.seed(sx, sy, lo[2] + 40.0))
    G.flood(seeds)
    t1 = time.time()
    cells = build_cells(G.nodes)
    regs = regions(cells)
    areas = []
    for members in regs:
        rects, at = rectangles(members)
        for gx, gy, w, h in rects:
            a = new_area(at[(gx, gy)].nw, at[(gx + w - 1, gy)].ne, at[(gx + w - 1, gy + h - 1)].se,
                         at[(gx, gy + h - 1)].sw, w, h, 1 if at[(gx, gy)].crouch else 0)
            a.cells = [at[(gx + j, gy + k)] for k in range(h) for j in range(w)]
            areas.append(a)
    areas = [b for a in merge_areas(areas) for b in split_area(a)]
    for a in areas:
        for c in a.cells:
            c.area = a
    for i, a in enumerate(areas):
        a.id = i + 1
        if area_normal_z(a) < MIN_NORMAL_Z:          # [obs] MarkJumpAreas
            a.flags |= 2
    connect(areas, cells)
    t2 = time.time()
    hiding_spots(C, areas)
    pl = places(C, areas, os.path.splitext(os.path.basename(path))[0].lower())
    t3 = time.time()
    nav = {"version": 5, "bsp_size": bsp_size, "places": pl, "areas": [], "trailing": 0, "generated": True}
    for a in areas:
        nav["areas"].append({
            "id": a.id, "flags": a.flags,
            "lo": (a.nw.x, a.nw.y, a.nw.z), "hi": (a.se.x, a.se.y, a.se.z), "neZ": a.ne.z, "swZ": a.sw.z,
            "connect": [sorted(b.id for b in a.connect[d]) for d in range(4)],
            "spots": a.spots, "approach": [], "encounters": [], "place": a.place})
    log(f"navgen {os.path.basename(path)}: {len(G.nodes)} nodes, {len(cells)} cells, {len(regs)} regions, "
        f"{len(areas)} areas, {sum(len(a.spots) for a in areas)} spots, {len(pl)} places, {C.ntrace} traces; "
        f"nodes {t1 - t0:.1f}s areas {t2 - t1:.1f}s spots {t3 - t2:.1f}s total {t3 - t0:.1f}s")
    return nav


def write_nav(nav):
    """→ .nav v5 bytes (readable by nav5.parse and by the game's LoadNavigationMap)."""
    out = bytearray(struct.pack("<III", 0xFEEDFACE, 5, nav.get("bsp_size") or 0))
    out += struct.pack("<H", len(nav["places"]))
    for p in nav["places"]:
        b = p.encode("latin-1") + b"\0"
        out += struct.pack("<H", len(b)) + b
    out += struct.pack("<I", len(nav["areas"]))
    for a in nav["areas"]:
        out += struct.pack("<IB3f3f2f", a["id"], a["flags"], *a["lo"], *a["hi"], a["neZ"], a["swZ"])
        for d in range(4):
            out += struct.pack(f"<I{len(a['connect'][d])}I", len(a["connect"][d]), *a["connect"][d])
        out += struct.pack("<B", min(255, len(a["spots"])))
        for s in a["spots"][:255]:
            out += struct.pack("<I3fB", s["id"], *s["pos"], s["flags"])
        out += struct.pack("<B", 0) + struct.pack("<I", 0) + struct.pack("<H", a["place"])
    return bytes(out)


def main():
    args = sys.argv[1:]
    opts = {}
    for k in ("--out", "--png", "--json"):
        if k in args:
            i = args.index(k); opts[k] = args[i + 1]; del args[i:i + 2]
    import nav5
    for p in args:
        nav = generate(p)
        bad = nav5.check(nav)
        print(f"  check: {bad} bad references")
        if "--out" in opts:
            data = write_nav(nav)
            open(opts["--out"], "wb").write(data)
            assert nav5.check(nav5.parse(data)) == 0
        if "--png" in opts:
            nav5.render(nav, opts["--png"])
        if "--json" in opts:
            import json
            json.dump(nav, open(opts["--json"], "w"))


if __name__ == "__main__":
    main()
