"""Half-Life AI node graph (CGraph / CTestHull), built offline from the map's info_node / info_node_air entities.

The game builds this graph at map start (CTestHull::BuildNodeGraph, about 2 s after the first node entity spawns),
saves it as maps/graphs/<map>.nod and every monster route uses it (CBaseMonster::FGetNodeRoute → CGraph::
FindNearestNode + FindShortestPath). VRChat has no game directory and Udon is ~100x slower than C#, so the port
runs the same build here at pack time and ships the result as one GSV3 section ("NODG", read by
GsvrPackLoader.cs:ReadNodeGraph). Rules and values: docs/decomp/hl_port/CGraph_part1.md, CGraph_part2.md,
G_CTestHull.md, G_CMessage_CEnvLight_CNodeEnt_….md (CNodeEnt). Clean-room: behaviour re-derived from those
ledgers / the decompile as data.

Build steps (in the game's order):
  1. nodes: info_node → node info 0, info_node_air → 2 (air); origin and peek origin = the entity origin, hint
     type / activity from the keys (16-bit), hint yaw = angles[1]; at most 1024 nodes (later ones are dropped).
  2. floor placement: air nodes keep their origin. A node whose point contents is water (−3) gets the water bit
     (4) and keeps its origin; every other node gets the land bit (1) and is dropped by a 384 u line trace
     (ignore monsters: world + SOLID_BSP brush entities) to hit z + 8 (origin and peek).
  3. LinkVisibleNodes: every ordered pair (i, j), i ≠ j, with equal type bits (info & 7): line trace origin_i →
     origin_j; start-solid rejects; clear → link without entity; blocked → reverse trace, and when the same
     non-world entity blocks both ways it becomes the link entity. 128 links per node (and 128 × nodes in total)
     abort the whole build (the game then has no graph).
  4. walk validation per link (hull bits 1 small, 2 human, 4 large, 8 fly, start 0xF): the test hull is put on the
     source origin and walked toward the destination yaw in ≤ 16 u WalkMove steps (last step = rest − 1); a
     failed step or a final 3-D distance > 64 u from the destination fails the hull. Small fails → clear 1|2|4 and
     skip the others; human fails → clear 2|4 and skip large; large fails → clear 4. Hulls: small (−12,−12,0)–
     (12,12,28), human (−16,−16,0)–(16,16,72), large (−32,−32,0)–(32,32,64); the fly bit is one large-hull
     (BSP hull 2) trace from source origin + 32 to destination peek + 32, cleared on start-solid / fraction < 1.
     A water source node swims (hull trace that must stay in water) instead of walking. A link with no hull bit
     left is removed (the node's last link takes its slot).
  5. RejectInlineLinks: link weight = 2-D distance; a link is removed when another link of the same node points
     the same way (2-D dot ≥ 0.998) to a nearer node.
  6. SortNodes: renumber in discovery order (node 0 first, then each node's link targets in old index order,
     unvisited nodes last) — the round-robin cover / hint searches walk this order.
  7. land nodes: origin z −= 8 (the peek origin keeps the +8).
  8. static routing tables for 4 hulls × 2 capability variants (mask 0, mask 0x340), ComputeStaticRoutingTables:
     per source, every still unknown destination (last to first) gets a FindShortestPath search (the game's
     100-entry heap, 0.001 relaxation margin, float sums) and every node of the found path gets its next hop toward
     every later node of it; a failed search stores source → source / dest → dest. A link entity passes per
     HandleLinkEnt in static mode (func_door / func_door_rotating need OPEN_DOORS 0x200 because every door starts
     closed; func_breakable passes; any other entity blocks). Graphs over 170 nodes use one search tree per source
     instead (same paths up to ties, ≥ 99.7 % identical next hops on c2a3d). Identical tables are stored once.

WalkMove emulation (engine SV_movetest / SV_CheckBottom with WALKMOVE_WORLDONLY: the world plus FL_WORLDBRUSH
entities, i.e. func_wall and its subclasses): move, lift 18 u, trace the box down 36 u (start-solid → retry from the
unlifted point), no ground → fail, then the bottom check (four corners 1 u below the box in solid, else traces from
18 u above the box bottom to 18 u below it: centre hit required, corners within 18 u of the centre). The engine's
whole-degree VecToYaw is used for the walk direction.

Fidelity (tools/test_nodegraph.py against the game's own maps/graphs/*.nod): c1a0 / c1a2 identical node order,
links and hull bits; over seven campaign maps ≥ 99 % of the links and hull bits agree. Maps that ship a .nod use it
directly (load_nod, CGraph::FLoadGraph), so the build only matters for mods / custom maps without one.
usage: python3 -I tools/nodegraph.py <map.bsp> [--json out.json] [--out section.bin]
"""
import heapq, math, os, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VERSION = 16                     # [obs CGraph::FLoadGraph] graph file version
MAX_NODES = 1024                 # [obs CGraph::AllocNodes / CNodeEnt::Spawn]
MAX_NODE_LINKS = 128             # [obs CGraph::LinkVisibleNodes]
NODE_LAND, NODE_AIR, NODE_WATER = 1, 2, 4
HULL_BITS = (1, 2, 4, 8)         # small, human, large, fly
CAP_DOORS_GROUP = 0x340          # [obs CGraph::FindShortestPath] route-variant selector
CAP_OPEN_DOORS = 0x200           # [obs CGraph::HandleLinkEnt]
STEP_SIZE = 18.0                 # engine sv_stepsize
DROP = 384.0                     # [obs CTestHull::BuildNodeGraph] floor trace length
LIFT = 8.0                       # node lift above the floor during the build
WALK_CHUNK = 16.0                # WalkMove step
WALK_TOL = 64.0                  # final distance tolerance
INLINE_DOT = 0.998               # [obs CGraph::RejectInlineLinks]
QUEUE_MAX = 100                  # [obs CGraph::FindShortestPath] priority queue size
SOLID, SKY, EMPTY, WATER = -2, -6, -1, -3
# walk-test hulls of CTestHull (entity mins/maxs, origin at the feet)
WALK_HULLS = (((-12.0, -12.0, 0.0), (12.0, 12.0, 28.0)),
              ((-16.0, -16.0, 0.0), (16.0, 16.0, 72.0)),
              ((-32.0, -32.0, 0.0), (32.0, 32.0, 64.0)))
BSP_HULL_MINS = {0: (0.0, 0.0, 0.0), 1: (-16.0, -16.0, -36.0), 2: (-32.0, -32.0, -32.0), 3: (-16.0, -16.0, -18.0)}
# brush classes that are not SOLID_BSP at graph-build time (volumes, non-solid brushes, monster-only clip)
NOT_BSP = ("trigger_", "func_ladder", "func_illusionary", "func_monsterclip", "func_water", "func_buyzone",
           "func_bomb_target", "func_hostage_rescue", "func_vip_safetyzone", "func_escapezone", "func_friction",
           "func_mortar_field", "func_tankcontrols", "func_traincontrols", "func_vehiclecontrols",
           "game_zone_player", "env_", "info_", "func_weaponcheck", "player_respawn_zone")
# FL_WORLDBRUSH entities (CFuncWall::Spawn and its subclasses): part of the "world" for SV_MoveNoEnts
BOTTOM_ENTS = "world"
ALLSOLID_ZERO = False
WALK_ENTS = "world"
YAW_INT = True
BOTTOM_LIFT = 18.0
ROUTES_EXACT = True
ROUTES_EXACT_MAX = 170       # [inf] the game's pair-by-pair fill is ~N³ in Python; larger graphs use one search tree per source
WORLD_BRUSH = ("func_wall", "func_wall_toggle", "func_conveyor")
# (classname, spawnflag that makes it non-solid)
PASSABLE_FLAG = {"func_door": 8, "func_door_rotating": 8, "momentary_door": 8, "func_train": 8, "func_tracktrain": 8,
                 "func_rotating": 64, "func_pendulum": 8, "func_rot_button": 1, "func_conveyor": 2,
                 "func_wall_toggle": 1}


_F32 = struct.Struct("<f")


def f32(v):
    """Round to the game's single precision (link weights and path sums are floats in CGraph)."""
    return _F32.unpack(_F32.pack(v))[0]


def num3(s):
    try:
        v = [float(x) for x in str(s).split()[:3]]
    except ValueError:
        v = []
    return tuple(v + [0.0] * (3 - len(v)))


def iflag(e, k="spawnflags"):
    try:
        return int(float(e.get(k, "0") or 0))
    except ValueError:
        return 0


def bsp_hull(mins, maxs):
    """Engine hull choice for a box (SV_HullForBsp): ≤ 8 wide → point, ≤ 36 wide → crouch (≤ 36 tall) or human,
    wider → large."""
    sx, sz = maxs[0] - mins[0], maxs[2] - mins[2]
    if sx <= 8:
        return 0
    if sx <= 36:
        return 3 if sz <= 36 else 1
    return 2


def door_open_offset(e, mins, maxs):
    """START_OPEN func_door: the brush sits at its open position at spawn (offset from the map origin)."""
    if "angles" in e:
        ang = num3(e["angles"])
    else:
        ang = (0.0, float(e.get("angle", "0") or 0), 0.0)
    if ang == (0.0, -1.0, 0.0):
        mv = (0.0, 0.0, 1.0)
    elif ang == (0.0, -2.0, 0.0):
        mv = (0.0, 0.0, -1.0)
    else:
        p, y = math.radians(ang[0]), math.radians(ang[1])
        mv = (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))
    lip = float(e.get("lip", "0") or 0)
    size = [maxs[q] - mins[q] for q in range(3)]
    travel = sum(abs(mv[q] * (size[q] - 2)) for q in range(3)) - lip
    return tuple(mv[q] * travel for q in range(3))


class World:
    """World + SOLID_BSP brush entities of one map (navgen.Clip trees), with the entity identity of a hit."""

    def __init__(self, C):
        self.C = C
        self.solid = []           # (entity index, classname, heads, origin, abs lo, abs hi, world brush)
        for ei, e in enumerate(C.ents):
            m = e.get("model", "")
            if not m.startswith("*"):
                continue
            cls = e.get("classname", "")
            if cls.startswith(NOT_BSP):
                continue
            sf = iflag(e)
            pf = PASSABLE_FLAG.get(cls)
            if pf and sf & pf:
                continue
            try:
                mi = int(m[1:])
            except ValueError:
                continue
            if not 0 < mi < len(C.models):
                continue
            heads, _o, mn, mx = C.models[mi]
            o = num3(e.get("origin", "0 0 0"))
            if cls == "func_door" and sf & 1:
                d = door_open_offset(e, mn, mx)
                o = (o[0] + d[0], o[1] + d[1], o[2] + d[2])
            elif cls == "func_door_rotating" and sf & 1:
                continue      # [inf] a start-open rotating door stands open: treated as not blocking the doorway
            lo = (o[0] + mn[0], o[1] + mn[1], o[2] + mn[2])
            hi = (o[0] + mx[0], o[1] + mx[1], o[2] + mx[2])
            self.solid.append((ei, cls, heads, o, lo, hi, cls in WORLD_BRUSH))
        self.ntrace = 0

    def contents(self, p):
        return self.C.contents(self.C.world[0], p[0], p[1], p[2])

    def _ents(self, hull, a, b, pad):
        lx, hx = min(a[0], b[0]) - pad[0], max(a[0], b[0]) + pad[0]
        ly, hy = min(a[1], b[1]) - pad[1], max(a[1], b[1]) + pad[1]
        lz, hz = min(a[2], b[2]) - pad[2], max(a[2], b[2]) + pad[2]
        for s in self.solid:
            lo, hi = s[4], s[5]
            if hx < lo[0] or lx > hi[0] or hy < lo[1] or ly > hi[1] or hz < lo[2] or lz > hi[2]:
                continue
            yield s

    def trace(self, hull, a, b, ents=True):
        """Point trace in BSP hull `hull` → (fraction, hit entity index (−1 world / none), startsolid, allsolid).
        ents: True = world + every SOLID_BSP entity (MOVE_NOMONSTERS), "world" = world + FL_WORLDBRUSH entities only
        (the engine's SV_MoveNoEnts / WALKMOVE_WORLDONLY), False = world only."""
        self.ntrace += 1
        C = self.C
        t = C._trace1(C.world[hull], a[0], a[1], a[2], b[0], b[1], b[2])
        frac, hit, ss, alls = t[0], -1, t[4], t[5]
        if ents and self.solid:
            m = BSP_HULL_MINS[hull]
            pad = (-m[0] + 1, -m[1] + 1, -m[2] + 1 if hull else 1)
            for ei, _cls, heads, o, _lo, _hi, wb in self._ents(hull, a, b, pad):
                if ents == "world" and not wb:
                    continue
                h = heads[hull]
                if h is None or h < 0:
                    continue                  # a model whose hull is one leaf (no tree to trace)
                e = C._trace1(h, a[0] - o[0], a[1] - o[1], a[2] - o[2], b[0] - o[0], b[1] - o[1], b[2] - o[2])
                # SV_ClipToLinks: an entity trace that is all-solid, start-solid or nearer replaces the result;
                # start-solid stays set once any part started in solid
                if e[5] or e[4] or e[0] < frac:
                    ss = ss or e[4]
                    frac, hit, alls = e[0], ei, e[5]
        if alls and ALLSOLID_ZERO:
            frac = 0.0
        return frac, hit, ss, alls

    def endpoint(self, a, b, f):
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)

    # ── WalkMove emulation ──

    def check_bottom(self, org, mins, maxs):
        lo = (org[0] + mins[0], org[1] + mins[1], org[2] + mins[2])
        hi = (org[0] + maxs[0], org[1] + maxs[1], org[2] + maxs[2])
        z = lo[2] - 1
        corners = ((lo[0], lo[1]), (hi[0], lo[1]), (lo[0], hi[1]), (hi[0], hi[1]))
        if all(self.contents((x, y, z)) == SOLID for x, y in corners):
            return True
        cx, cy = (lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5
        z0 = lo[2] + BOTTOM_LIFT
        a, b = (cx, cy, z0), (cx, cy, z0 - 2 * STEP_SIZE)
        f, _h, _s, _a = self.trace(0, a, b, ents=BOTTOM_ENTS)
        if f >= 1.0:
            return False
        mid = bottom = a[2] + (b[2] - a[2]) * f
        for x, y in corners:
            a, b = (x, y, z0), (x, y, z0 - 2 * STEP_SIZE)
            f, _h, _s, _a = self.trace(0, a, b, ents=BOTTOM_ENTS)
            ez = a[2] + (b[2] - a[2]) * f
            if f < 1.0 and ez > bottom:
                bottom = ez
            if f >= 1.0 or mid - ez > STEP_SIZE:
                return False
        return True

    def move_test(self, org, move, mins, maxs, hull, off):
        """One SV_movetest step (world only) → new origin or None."""
        nx, ny, nz = org[0] + move[0], org[1] + move[1], org[2] + move[2] + STEP_SIZE
        a = (nx - off[0], ny - off[1], nz - off[2])
        b = (a[0], a[1], a[2] - 2 * STEP_SIZE)
        f, _h, ss, alls = self.trace(hull, a, b, ents=WALK_ENTS)
        if alls:
            return None
        if ss:
            a = (a[0], a[1], a[2] - STEP_SIZE)
            f, _h, ss, alls = self.trace(hull, a, b, ents=WALK_ENTS)
            if alls or ss:
                return None
        if f >= 1.0:
            return None                       # walked off a ledge (no partial-ground state on the test hull)
        e = self.endpoint(a, b, f)
        new = (e[0] + off[0], e[1] + off[1], e[2] + off[2])
        if not self.check_bottom(new, mins, maxs):
            return None
        return new

    def swim_test(self, org, move, hull, off):
        a = (org[0] - off[0], org[1] - off[1], org[2] - off[2])
        b = (a[0] + move[0], a[1] + move[1], a[2] + move[2])
        f, _h, _s, _a = self.trace(hull, a, b, ents=WALK_ENTS)
        if f < 1.0:
            return None
        new = (org[0] + move[0], org[1] + move[1], org[2] + move[2])
        if self.contents(new) == EMPTY:
            return None
        return new

    def walk(self, src, dst, box, water):
        """CTestHull walk from src toward dst (yaw truncated to whole degrees like the engine's VecToYaw [inf])."""
        mins, maxs = box
        hull = bsp_hull(mins, maxs)
        m = BSP_HULL_MINS[hull]
        off = (m[0] - mins[0], m[1] - mins[1], m[2] - mins[2])
        dx, dy = dst[0] - src[0], dst[1] - src[1]
        if dx == 0 and dy == 0:
            yaw = 0.0
        else:
            yaw = math.degrees(math.atan2(dy, dx))
            if YAW_INT:
                yaw = float(int(yaw))
            if yaw < 0:
                yaw += 360.0
        cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        dist = math.hypot(dx, dy)
        org = src
        if dist > 0:
            done = 0.0
            while True:
                step = WALK_CHUNK
                if dist - 1.0 <= done + WALK_CHUNK:
                    step = (dist - done) - 1.0
                move = (cy * step, sy * step, 0.0)
                org = self.swim_test(org, move, hull, off) if water else self.move_test(org, move, mins, maxs, hull, off)
                if org is None:
                    return False
                done += WALK_CHUNK
                if done >= dist:
                    break
        return math.dist(org, dst) <= WALK_TOL


def collect_nodes(ents, log):
    nodes = []
    for e in ents:
        cls = e.get("classname", "")
        if cls not in ("info_node", "info_node_air"):
            continue
        if len(nodes) >= MAX_NODES:
            log("nodegraph: cNodes > MAX_NODES, node ignored")
            continue
        o = num3(e.get("origin", "0 0 0"))
        ht = iflag(e, "hinttype")
        ha = iflag(e, "activity")
        ang = num3(e["angles"]) if "angles" in e else (0.0, float(e.get("angle", "0") or 0), 0.0)
        # the keys are 16-bit shorts in the node
        ht = ((ht + 0x8000) & 0xFFFF) - 0x8000
        ha = ((ha + 0x8000) & 0xFFFF) - 0x8000
        nodes.append(dict(origin=list(o), peek=list(o), info=NODE_AIR if cls == "info_node_air" else 0,
                          hint_type=ht, hint_act=ha, hint_yaw=ang[1], links=[]))
    return nodes


def place_nodes(W, nodes):
    for n in nodes:
        if n["info"] & NODE_AIR:
            continue
        o = tuple(n["origin"])
        if W.contents(o) == WATER:
            n["info"] |= NODE_WATER
            continue
        n["info"] |= NODE_LAND
        b = (o[0], o[1], o[2] - DROP)
        f, _h, _s, _a = W.trace(0, o, b)
        z = o[2] + (b[2] - o[2]) * f + LIFT
        n["origin"][2] = z
        n["peek"][2] = z


def link_visible(W, nodes, log):
    """→ total links, or 0 when a node exceeds the link cap (the build fails)."""
    total = 0
    N = len(nodes)
    for i, a in enumerate(nodes):
        a["links"] = []
        oa = tuple(a["origin"])
        for j, b in enumerate(nodes):
            if i == j or (a["info"] ^ b["info"]) & 7:
                continue
            ob = tuple(b["origin"])
            f, hit, ss, _al = W.trace(0, oa, ob)
            if ss:
                continue
            ent = -1
            if f < 1.0:
                f2, hit2, _s2, _a2 = W.trace(0, ob, oa)
                if hit < 0 or hit != hit2:
                    continue                  # world (or two different blockers): no link
                ent = hit
            a["links"].append(dict(dst=j, ent=ent, info=0, weight=0.0))
            total += 1
            if len(a["links"]) == MAX_NODE_LINKS:
                log(f"nodegraph: node {i} has NodeLinks > MAX_NODE_INITIAL_LINKS - graph build failed")
                return 0
            if total > N * MAX_NODE_LINKS:
                log("nodegraph: TotalLinks > MAX_NODE_INITIAL_LINKS * NUMNODES - graph build failed")
                return 0
    return total


def validate_walks(W, nodes):
    rejected = 0
    for n in nodes:
        src = tuple(n["origin"])
        water = bool(n["info"] & NODE_WATER)
        keep = []
        for L in n["links"]:
            d = nodes[L["dst"]]
            dst = tuple(d["origin"])
            info = 0xF
            skip = False
            for h in range(3):
                if skip and h in (1, 2):
                    continue
                if not W.walk(src, dst, WALK_HULLS[h], water):
                    if h == 0:
                        info &= ~7; skip = True
                    elif h == 1:
                        info &= ~6; skip = True
                    else:
                        info &= ~4
            a = (src[0], src[1], src[2] + 32.0)
            pk = d["peek"]
            b = (pk[0], pk[1], pk[2] + 32.0)
            f, _h, ss, _al = W.trace(2, a, b)
            if ss or f < 1.0:
                info &= ~8
            L["info"] = info
            if info:
                keep.append(L)
            else:
                rejected += 1
        # the game copies the last link over a rejected one; the surviving order is the same set, so keep order
        n["links"] = swap_remove_order(n["links"], keep)
    return rejected


def swap_remove_order(links, keep):
    """Reproduce the order the game leaves behind when it removes links by copying the block's last link into the
    removed slot and re-testing that slot."""
    alive = set(id(L) for L in keep)
    arr = list(links)
    k = 0
    while k < len(arr):
        if id(arr[k]) not in alive:
            arr[k] = arr[-1]
            arr.pop()
            continue
        k += 1
    return arr


def reject_inline(nodes):
    rejected = 0
    for n in nodes:
        ox, oy = n["origin"][0], n["origin"][1]
        arr = n["links"]
        k = 0
        while k < len(arr):
            d = nodes[arr[k]["dst"]]["origin"]
            vx, vy = d[0] - ox, d[1] - oy
            dk = math.hypot(vx, vy)
            ux, uy = (vx / dk, vy / dk) if dk else (0.0, 0.0)
            arr[k]["weight"] = f32(dk)
            removed = False
            for m in range(len(arr)):
                if m == k:
                    continue
                e = nodes[arr[m]["dst"]]["origin"]
                wx, wy = e[0] - ox, e[1] - oy
                dm = math.hypot(wx, wy)
                tx, ty = (wx / dm, wy / dm) if dm else (0.0, 0.0)
                if ux * tx + uy * ty >= INLINE_DOT and dk > dm:
                    arr[k] = arr[-1]
                    arr.pop()
                    rejected += 1
                    removed = True
                    break
            if not removed:
                k += 1
    return rejected


def sort_nodes(nodes):
    """SortNodes: node 0 keeps 0; scanning nodes in old index order, each unnumbered link target gets the next
    number; nodes never reached are numbered last in old order. → new list + old→new map."""
    N = len(nodes)
    new = [-1] * N
    if N == 0:
        return nodes, new
    new[0] = 0
    nxt = 1
    for i in range(N):
        for L in nodes[i]["links"]:
            if new[L["dst"]] == -1:
                new[L["dst"]] = nxt
                nxt += 1
    for i in range(N):
        if new[i] == -1:
            new[i] = nxt
            nxt += 1
    out = [None] * N
    for i, n in enumerate(nodes):
        for L in n["links"]:
            L["dst"] = new[L["dst"]]
        out[new[i]] = n
    return out, new


def link_passes_static(ent_cls, cap):
    if ent_cls is None:
        return True
    if ent_cls in ("func_door", "func_door_rotating"):
        return bool(cap & CAP_OPEN_DOORS)     # every door is closed (at bottom) when the tables are built
    if ent_cls == "func_breakable":
        return True
    return False


def shortest_tree(nodes, src, hull_bit, cap, ent_cls):
    """Dynamic FindShortestPath from src with the game's 100-entry queue → previous-node array (−1 unreached)."""
    N = len(nodes)
    closest = [-1.0] * N
    prev = [-1] * N
    closest[src] = 0.0
    prev[src] = src
    heap = [(0.0, 0, src)]
    seq = 1
    while heap:
        d, _s, cur = heapq.heappop(heap)
        for L in nodes[cur]["links"]:
            if (L["info"] & hull_bit) != hull_bit:
                continue
            if L["ent"] >= 0 and not link_passes_static(ent_cls.get(L["ent"]), cap):
                continue
            t = L["dst"]
            nd = d + L["weight"]
            if closest[t] >= -0.5 and closest[t] - 0.001 <= nd:
                continue
            closest[t] = nd
            prev[t] = cur
            if len(heap) >= QUEUE_MAX:
                continue                      # "Queue is full!": distance kept, node not queued
            heapq.heappush(heap, (nd, seq, t))
            seq += 1
    return prev


def passable_links(nodes, hull_bit, cap, ent_cls):
    """Per node: [(dst, weight)] of the links one hull / capability variant may use (static HandleLinkEnt)."""
    out = []
    for n in nodes:
        out.append([(L["dst"], L["weight"]) for L in n["links"] if (L["info"] & hull_bit) == hull_bit
                    and (L["ent"] < 0 or link_passes_static(ent_cls.get(L["ent"]), cap))])
    return out


def dynamic_path(adj, src, dst):
    """CGraph::FindShortestPath without routing tables: a best-first search from src that stops when dst is popped,
    on the game's fixed binary heap (100 entries, a push to a full heap is dropped after the node's distance and
    predecessor were recorded); a relaxation must beat the stored distance by more than 0.001. → node list or []."""
    N = len(adj)
    closest = [-1.0] * N
    prev = [-1] * N
    closest[src] = 0.0
    prev[src] = src
    hn, hd = [src], [0.0]                     # heap arrays: node, distance
    pk = _F32.pack
    up = _F32.unpack
    while hn:
        cur, cd = hn[0], hd[0]
        last_n, last_d = hn.pop(), hd.pop()
        n = len(hn)
        if n:
            i = 0
            while 2 * i + 1 < n:
                c = 2 * i + 1
                if c + 1 < n and hd[c + 1] < hd[c]:
                    c += 1
                if last_d <= hd[c]:
                    break
                hn[i], hd[i] = hn[c], hd[c]
                i = c
            hn[i], hd[i] = last_n, last_d
        if cur == dst:
            break
        for t, w in adj[cur]:
            nd = up(pk(cd + w))[0]
            ct = closest[t]
            if ct >= -0.5 and ct - 0.001 <= nd:
                continue
            closest[t] = nd
            prev[t] = cur
            if len(hn) >= QUEUE_MAX:
                continue                      # "Queue is full!"
            hn.append(t); hd.append(nd)
            i = len(hn) - 1
            while i > 0:
                p = (i - 1) // 2
                if not hd[i] < hd[p]:
                    break
                hn[i], hn[p] = hn[p], hn[i]
                hd[i], hd[p] = hd[p], hd[i]
                i = p
    if closest[dst] < -0.5:
        return []
    out = [dst]
    c = dst
    while c != src:
        c = prev[c]
        out.append(c)
    out.reverse()
    return out


def route_table_exact(nodes, hull, cap, ent_cls):
    """ComputeStaticRoutingTables for one hull / capability variant: for every source, destinations from the last
    to the first that are still unknown get a dynamic search; every node on the found path then gets its next hop
    toward every later node of that path (overwriting); a failed search stores source → source and dest → dest."""
    N = len(nodes)
    R = [[-1] * N for _ in range(N)]
    adj = passable_links(nodes, HULL_BITS[hull], cap, ent_cls)
    for frm in range(N):
        row = R[frm]
        for to in range(N - 1, -1, -1):
            if row[to] != -1:
                continue
            p = dynamic_path(adj, frm, to) if frm != to else [frm, frm]
            if len(p) > 1:
                for k in range(len(p) - 1):
                    a, nx = p[k], p[k + 1]
                    ra = R[a]
                    for e in p[k + 1:]:
                        ra[e] = nx
            else:
                R[frm][to] = frm
                R[to][frm] = to
    out = bytearray(N * N * 2)
    for frm in range(N):
        struct.pack_into(f"<{N}H", out, frm * N * 2, *R[frm])
    return bytes(out)


def route_table(nodes, hull, cap, ent_cls):
    N = len(nodes)
    out = bytearray(N * N * 2)
    bit = HULL_BITS[hull]
    for s in range(N):
        prev = shortest_tree(nodes, s, bit, cap, ent_cls)
        row = [s] * N
        for t in range(N):
            if t == s or prev[t] < 0:
                continue
            # walk back to the node whose predecessor is s
            c, guard = t, 0
            while prev[c] != s and guard <= N:
                c = prev[c]
                guard += 1
                if c < 0:
                    break
            if c >= 0 and prev[c] == s:
                row[t] = c
        struct.pack_into(f"<{N}H", out, s * N * 2, *row)
    return bytes(out)


def build(path=None, C=None, log=print):
    """Map → graph dict (nodes, links, tables, slots) or None (no nodes / failed build)."""
    import navgen
    t0 = time.time()
    if C is None:
        C, _sz = navgen.load(path)
    nodes = collect_nodes(C.ents, log)
    if not nodes:
        return None
    W = World(C)
    place_nodes(W, nodes)
    total = link_visible(W, nodes, log)
    if total == 0:
        log(f"nodegraph: {len(nodes)} nodes, no links - no graph")
        return None
    t1 = time.time()
    walk_rej = validate_walks(W, nodes)
    t2 = time.time()
    inline_rej = reject_inline(nodes)
    nodes, _new = sort_nodes(nodes)
    for n in nodes:
        if n["info"] & NODE_LAND:
            n["origin"][2] -= LIFT
    ent_cls = {s[0]: s[1] for s in W.solid}
    tables, slots = [], []
    for h in range(4):
        for c in (0, CAP_DOORS_GROUP):
            exact = ROUTES_EXACT and len(nodes) <= ROUTES_EXACT_MAX
            tb = (route_table_exact if exact else route_table)(nodes, h, c, ent_cls)
            try:
                k = tables.index(tb)
            except ValueError:
                tables.append(tb)
                k = len(tables) - 1
            slots.append(k)
    t3 = time.time()
    links = []
    for i, n in enumerate(nodes):
        n["first"] = len(links)
        for L in n["links"]:
            links.append(dict(src=i, dst=L["dst"], info=L["info"], ent=L["ent"], weight=L["weight"]))
    unpaired = 0
    pairs = set((L["src"], L["dst"]) for L in links)
    for L in links:
        if (L["dst"], L["src"]) not in pairs:
            unpaired += 1
    g = dict(nodes=nodes, links=links, tables=tables, slots=slots, ent_cls=ent_cls,
             stats=dict(nodes=len(nodes), initial=total, walk_rejected=walk_rej, inline_rejected=inline_rej,
                        links=len(links), unpaired=unpaired, tables=len(tables), traces=W.ntrace,
                        t_link=round(t1 - t0, 2), t_walk=round(t2 - t1, 2), t_route=round(t3 - t2, 2)))
    log(f"nodegraph: {len(nodes)} nodes, {total} initial links, {walk_rej} walk-rejected, {inline_rej} inline-rejected, "
        f"{len(links)} links ({unpaired} one-way), {len(tables)} route tables, {W.ntrace} traces, "
        f"link {t1 - t0:.1f}s walk {t2 - t1:.1f}s route {t3 - t2:.1f}s")
    return g


# ───────────────────────── the game's own .nod files (CGraph::FLoadGraph) ─────────────────────────

NOD_HEADER = 0x20CC              # serialised CGraph object after the version int
NOD_NODE, NOD_LINK, NOD_DIST = 88, 24, 16


def find_nod(bsp, game_root=None, mod="valve"):
    """<mod>/maps/graphs/<map>.nod next to the BSP or in the mod's content dirs (the game writes it on the first
    load of a map; Valve ships the campaign's graphs)."""
    name = os.path.splitext(os.path.basename(bsp))[0]
    d = os.path.dirname(os.path.abspath(bsp))
    cands = [os.path.join(d, "graphs", name + ".nod")]
    try:
        from gamedirs import content_dirs, root_of_bsp
        roots = [game_root] if game_root else []
        roots.append(root_of_bsp(bsp))
        for r in roots:
            if r:
                cands += [os.path.join(x, "maps", "graphs", name + ".nod") for x in content_dirs(r, mod, fallback=None)]
    except Exception:                         # noqa: BLE001 — a missing helper only costs the extra candidates
        pass
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def decode_routes(route, start, N, frm):
    """One node's compressed route string → next node for every destination (FindShortestPath / NextNodeInRoute
    decoding): a negative byte −k = the next k destinations are reached directly (next = destination); a byte
    pair (k, delta) = the next k + 1 destinations go through node (from + delta) wrapped once into 0..N−1.
    None when the string runs out before all N destinations are covered."""
    row = []
    p = start
    while len(row) < N:
        if not 0 <= p < len(route):
            return None
        c = route[p] - 256 if route[p] > 127 else route[p]
        if c < 0:
            for _ in range(-c):
                row.append(len(row))
            p += 1
        else:
            if p + 1 >= len(route):
                return None
            d = route[p + 1] - 256 if route[p + 1] > 127 else route[p + 1]
            nxt = frm + d
            if nxt >= N:
                nxt -= N
            elif nxt < 0:
                nxt += N
            row.extend([nxt] * (c + 1))
            p += 2
    return row[:N]


def load_nod(path, ents, log=print):
    """Valve .nod (version 16) → graph dict like build(), or None when the file is not a usable graph for these
    entities (wrong version / short file / node set that does not match the map's info_node entities)."""
    b = open(path, "rb").read()
    if len(b) < 4 + NOD_HEADER:
        log(f"nodegraph: {path}: short file"); return None
    ver, = struct.unpack_from("<i", b, 0)
    if ver != VERSION:
        log(f"nodegraph: {path}: graph version {ver}, expected {VERSION}"); return None
    H = 4
    N, L, R = struct.unpack_from("<3i", b, H + 24)
    NH, = struct.unpack_from("<i", b, H + 8384)
    need = H + NOD_HEADER + N * NOD_NODE + L * NOD_LINK + N * NOD_DIST + R + NH * 2
    if N <= 0 or L < 0 or R < 0 or NH < 0 or need > len(b):
        log(f"nodegraph: {path}: truncated ({len(b)} < {need} bytes)"); return None
    o = H + NOD_HEADER
    nodes = []
    for k in range(N):
        v = struct.unpack_from("<6f", b, o)
        info, cnt, first = struct.unpack_from("<3i", b, o + 28)
        nb = struct.unpack_from("<8i", b, o + 40)
        ht, ha = struct.unpack_from("<2h", b, o + 80)
        hy, = struct.unpack_from("<f", b, o + 84)
        nodes.append(dict(origin=list(v[0:3]), peek=list(v[3:6]), info=info, count=cnt, first=first, nb=nb,
                          hint_type=ht, hint_act=ha, hint_yaw=hy, links=[]))
        o += NOD_NODE
    model_ent = {}
    for ei, e in enumerate(ents):
        m = e.get("model", "")
        if m.startswith("*") and m[:4] not in model_ent:
            model_ent[m[:4]] = ei
    raw_links = []
    for k in range(L):
        src, dst = struct.unpack_from("<2i", b, o)
        name = b[o + 12:o + 16].split(b"\0")[0].decode("latin-1", "replace")
        info, = struct.unpack_from("<i", b, o + 16)
        w, = struct.unpack_from("<f", b, o + 20)
        raw_links.append(dict(src=src, dst=dst, info=info, ent=model_ent.get(name, -1) if name else -1, weight=w,
                              model=name))
        o += NOD_LINK
    o += N * NOD_DIST
    route = b[o:o + R]
    if len(b) > need:
        log(f"nodegraph: {path}: {len(b) - need} bytes left over after the graph records")   # FLoadGraph warns too
    # sanity: every node's link block inside the pool, link endpoints inside the node array
    for k, n in enumerate(nodes):
        if n["first"] < 0 or n["count"] < 0 or n["first"] + n["count"] > L:
            log(f"nodegraph: {path}: node {k} links out of range"); return None
        n["links"] = [dict(dst=raw_links[q]["dst"], info=raw_links[q]["info"], ent=raw_links[q]["ent"],
                           weight=raw_links[q]["weight"]) for q in range(n["first"], n["first"] + n["count"])]
        if any(not 0 <= Lk["dst"] < N for Lk in n["links"]):
            log(f"nodegraph: {path}: link target out of range"); return None
    # stale check: the node set must be the map's info_node / info_node_air set (x/y; z was floor-placed)
    want = sorted((round(float(x), 0), round(float(y), 0)) for x, y, _z in
                  (num3(e.get("origin", "0 0 0")) for e in ents if e.get("classname") in ("info_node", "info_node_air")))[:MAX_NODES]
    have = sorted((round(n["origin"][0], 0), round(n["origin"][1], 0)) for n in nodes)
    if len(want) != N or want != have:
        log(f"nodegraph: {path}: {N} nodes do not match the map's {len(want)} node entities - stale graph ignored")
        return None
    tables, slots = [], []
    for h in range(4):
        for c in range(2):
            rows = bytearray(N * N * 2)
            for f, n in enumerate(nodes):
                row = decode_routes(route, n["nb"][h * 2 + c], N, f)
                if row is None:
                    log(f"nodegraph: {path}: bad route string (node {f} hull {h} cap {c})"); return None
                struct.pack_into(f"<{N}H", rows, f * N * 2, *row)
            tb = bytes(rows)
            try:
                k = tables.index(tb)
            except ValueError:
                tables.append(tb); k = len(tables) - 1
            slots.append(k)
    links = []
    for i, n in enumerate(nodes):
        n["first"] = len(links)
        for Lk in n["links"]:
            links.append(dict(src=i, dst=Lk["dst"], info=Lk["info"], ent=Lk["ent"], weight=Lk["weight"]))
    ent_cls = {ei: ents[ei].get("classname", "") for ei in set(Lk["ent"] for Lk in links if Lk["ent"] >= 0)}
    return dict(nodes=nodes, links=links, tables=tables, slots=slots, ent_cls=ent_cls, source="nod",
                stats=dict(nodes=N, links=L, tables=len(tables), route_bytes=R, hash_links=NH))


def section(g):
    """→ NODG section bytes (4-byte aligned). Layout (little endian):
       i32 version (16), i32 N nodes, i32 L links, i32 T tables, i32 slot[8] (hull × 2 + capability variant →
       table index); N × 12 × 4: f32 origin xyz, f32 peek xyz, i32 info, i32 firstLink, i32 linkCount,
       i32 hintType, i32 hintActivity, f32 hintYaw; L × 5 × 4: i32 src, i32 dst, i32 info (hull bits), i32 link
       entity (pack entity index, −1 none), f32 weight; T × N·N uint16 next node [from · N + to] (= from when
       unreachable), padded to 4."""
    nodes, links, tables = g["nodes"], g["links"], g["tables"]
    out = bytearray(struct.pack("<4i", VERSION, len(nodes), len(links), len(tables)))
    out += struct.pack("<8i", *g["slots"])
    for n in nodes:
        out += struct.pack("<6f5if", *n["origin"], *n["peek"], n["info"], n["first"], len(n["links"]),
                           n["hint_type"], n["hint_act"], n["hint_yaw"])
    for L in links:
        out += struct.pack("<4if", L["src"], L["dst"], L["info"], L["ent"], L["weight"])
    for t in tables:
        out += t
    out += b"\0" * (-len(out) % 4)
    return bytes(out)


def parse_section(b):
    """Inverse of section() (tests / tools)."""
    ver, N, L, T = struct.unpack_from("<4i", b, 0)
    slots = list(struct.unpack_from("<8i", b, 16))
    o = 48
    nodes = []
    for _ in range(N):
        v = struct.unpack_from("<6f5if", b, o); o += 48
        nodes.append(dict(origin=list(v[0:3]), peek=list(v[3:6]), info=v[6], first=v[7], count=v[8],
                          hint_type=v[9], hint_act=v[10], hint_yaw=v[11]))
    links = []
    for _ in range(L):
        v = struct.unpack_from("<4if", b, o); o += 20
        links.append(dict(src=v[0], dst=v[1], info=v[2], ent=v[3], weight=v[4]))
    tables = []
    for _ in range(T):
        tables.append(struct.unpack_from(f"<{N * N}H", b, o)); o += N * N * 2
    return dict(version=ver, nodes=nodes, links=links, tables=tables, slots=slots)


def next_hop(g, hull, cap, frm, to):
    N = len(g["nodes"])
    t = g["tables"][g["slots"][hull * 2 + (1 if cap & CAP_DOORS_GROUP else 0)]]
    if isinstance(t, (bytes, bytearray)):
        return struct.unpack_from("<H", t, 2 * (frm * N + to))[0]
    return t[frm * N + to]


def path(g, hull, cap, frm, to, limit=10):
    """Static-table FindShortestPath: [frm, …, to] capped at `limit` entries; [] when unreachable."""
    if frm == to:
        return [frm, frm]
    out = [frm]
    cur = frm
    while True:
        n = next_hop(g, hull, cap, cur, to)
        if n == cur:
            return []
        if len(out) == limit:
            return out
        out.append(n)
        cur = n
        if n == to:
            return out


def path_length(g, hull, cap, frm, to):
    """CGraph::PathLength over the static tables: link weights along the route, 0 when unreachable / start = dest."""
    if frm == to:
        return 0.0
    N = len(g["nodes"])
    links = {(L["src"], L["dst"]): L["weight"] for L in g["links"]}
    s, cur = 0.0, frm
    for _ in range(N):
        n = next_hop(g, hull, cap, cur, to)
        if n == cur or (cur, n) not in links:
            return 0.0
        s = f32(s + links[(cur, n)])
        if n == to:
            return s
        cur = n
    return 0.0


def write_probe(g, path_out):
    """Expectations for the UdonSharp harness (tools/usharp_offline/harness.cs HlNodeGraphRules): node origins,
    static paths (≤ 10 entries) and path lengths for a deterministic sample of pairs."""
    N = len(g["nodes"])
    with open(path_out, "w") as fh:
        fh.write(f"N {N} {len(g['links'])}\n")
        for k, n in enumerate(g["nodes"]):
            fh.write("O %d %r %r %r %d\n" % (k, *n["origin"], n["info"]))
        step = max(1, (N * N) // 400)
        for q in range(0, N * N, step):
            a, b = q // N, q % N
            for hull in (0, 1, 2):
                for cap in (0, CAP_DOORS_GROUP):
                    p = path(g, hull, cap, a, b)
                    fh.write("P %d %d %d %d %d %s\n" % (hull, 1 if cap else 0, a, b, len(p), " ".join(map(str, p))))
                    fh.write("L %d %d %d %d %r\n" % (hull, 1 if cap else 0, a, b, path_length(g, hull, cap, a, b)))


def main():
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("bsp")
    ap.add_argument("--json")
    ap.add_argument("--out", help="write the raw NODG section")
    ap.add_argument("--probe", help="write harness expectations (node origins, paths, path lengths)")
    ap.add_argument("--nod", action="store_true", help="use the game's maps/graphs/<map>.nod when it matches")
    a = ap.parse_args()
    g = None
    if a.nod:
        import navgen
        C, _ = navgen.load(a.bsp)
        nod = find_nod(a.bsp)
        g = load_nod(nod, C.ents) if nod else None
    if g is None:
        g = build(a.bsp)
    if g is None:
        print("no graph")
        return 1
    print(g["stats"])
    if a.probe:
        write_probe(g, a.probe)
    if a.out:
        open(a.out, "wb").write(section(g))
    if a.json:
        json.dump(dict(stats=g["stats"], nodes=[dict(o=n["origin"], info=n["info"], links=[(L["dst"], L["info"], L["ent"])
                                                                                        for L in n["links"]])
                                                 for n in g["nodes"]], slots=g["slots"]), open(a.json, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
