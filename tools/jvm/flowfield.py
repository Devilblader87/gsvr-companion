#!/usr/bin/env python3
"""FLOW FIELD (docs/gpu_flow.md): the integer distance field to a goal set on the pathing grid, and the exact definition the GPU shader
(unity/Assets/W3VR/GpuProbe/GpuFlow.shader) must reproduce BYTE FOR BYTE. Nothing here is used by the engine (OFF by default).

Definition (all integers; every value < 2^24 so a float32 render-texture channel holds it exactly):
  walk[c]  = cell c is walkable: wpm ground mask (switch-gated, 0x06 or the old 0x02) clear and no footprint (the same test as Grid.walkable).
  step(c,n)= the A* neighbour rule of pathing.Grid.astar: n is one of the 8 neighbours, walk[n]; a diagonal needs BOTH orthogonal
             cells walkable (no corner cutting). Cost COST_S = 10 straight, COST_D = 14 diagonal (the flat A*'s integer octile).
  D(c)     = INF (16 777 215) for a blocked cell and for a cell with no way to a goal; 0 for a walkable goal cell; else the minimum over
             all step-chains of the summed costs = plain shortest-path distance (steps are symmetric, so goal->c == c->goal).
  DIR(c)   = 255 for a blocked cell / unreachable cell, 8 for a goal cell, else the index i in NB (pathing.NB order) of the FIRST neighbour
             direction minimising D(n) + cost(i) among valid steps (strict '<' while scanning i = 0..7 -> lowest index wins ties).
             A unit at c walks to c + NB[i]. (Only defined where D(c) < INF and c is not a goal.)
Any monotone relaxation that only ever offers real path costs converges to the same unique D, so the GPU may use many cheap passes
(Jacobi ping-pong): field_relax / field_relax_rays below are bit-level emulations of the shader passes, field_dijkstra is the reference.
"""
import heapq
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

INF = 0xFFFFFF
COST_S, COST_D = 10, 14
NO_WALK = 0x02
NO_FLY = 0x04
NO_GROUND = NO_WALK | NO_FLY       # EXT-ow3 SPEC-0020: the ground path type's terrain mask (0x06)
# EXT-ow3 SPEC-0020 review: the 0x06 mask is not confirmed by an oracle run yet, so one switch gates every ground test (the flat A*,
# PATH_AB, these flow fields); default OFF = the old 0x02-only rule.  W3_GROUND_UNFLYABLE=1 forces it on (tests / measurements);
# the twin is W3Pathing.GROUND_UNFLYABLE / groundUnflyableOn.  Pending: tools/oracle/probes/pathfind_rules.j
# (docs/decomp/port/EXT_Pathing.md sec. 6.1).
GROUND_UNFLYABLE = os.environ.get('W3_GROUND_UNFLYABLE') == '1'


def ground_mask():
    """the terrain bits that block a ground mover: 0x06 with the switch ON, else the old 0x02 rule (EXT-ow3 SPEC-0020 review)."""
    return NO_GROUND if GROUND_UNFLYABLE else NO_WALK
# pathing.NB order (fixed neighbour order of the engine's A*)
NB = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1))
NOGO, DGOAL = 255, 8


def walk_array(w, h, flags, occ=None):
    """1/0 per cell: walkable (terrain flag clear and, with occ, no footprint)"""
    n = w * h
    gm = ground_mask()
    if occ is None:
        return bytearray(0 if (flags[i] & gm) else 1 for i in range(n))
    return bytearray(0 if ((flags[i] & gm) or occ[i]) else 1 for i in range(n))


def field_dijkstra(w, h, walk, goals):
    """the reference: Dijkstra (heap of (d, cell)). goals = cells (index); blocked goals are ignored."""
    D = [INF] * (w * h)
    heap = []
    for g in goals:
        if walk[g] and D[g] != 0:
            D[g] = 0
            heap.append((0, g))
    heapq.heapify(heap)
    while heap:
        d, c = heapq.heappop(heap)
        if d != D[c]:
            continue
        cx, cy = c % w, c // w
        for dx, dy in NB:
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            n = ny * w + nx
            if not walk[n]:
                continue
            if dx and dy and (not walk[cy * w + nx] or not walk[ny * w + cx]):
                continue
            nd = d + (COST_D if dx and dy else COST_S)
            if nd < D[n]:
                D[n] = nd
                heapq.heappush(heap, (nd, n))
    return D


def field_init(w, h, walk, goals):
    D = [INF] * (w * h)
    for g in goals:
        if walk[g]:
            D[g] = 0
    return D


def relax_pass(w, h, walk, D):
    """ONE Jacobi pass exactly as the shader does it: new[c] = min(old[c], min over valid steps old[n] + cost); INF stays INF"""
    out = D[:]
    for cy in range(h):
        for cx in range(w):
            c = cy * w + cx
            if not walk[c]:
                continue
            best = D[c]
            for dx, dy in NB:
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                n = ny * w + nx
                if not walk[n]:
                    continue
                if dx and dy and (not walk[cy * w + nx] or not walk[ny * w + cx]):
                    continue
                v = D[n]
                if v >= INF:
                    continue
                v += COST_D if dx and dy else COST_S
                if v < best:
                    best = v
            out[c] = best
    return out


def relax_pass_rays(w, h, walk, D, L):
    """ONE Jacobi pass with ray scans: for each of the 8 directions walk up to L steps (stop at the first invalid step) and offer
    old[cell k steps away] + k * cost. Every offer is a real path cost, so the fixpoint is the same D; far fewer passes in corridors."""
    out = D[:]
    for cy in range(h):
        for cx in range(w):
            c = cy * w + cx
            if not walk[c]:
                continue
            best = D[c]
            for dx, dy in NB:
                cost = COST_D if dx and dy else COST_S
                x, y = cx, cy
                for k in range(1, L + 1):
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < w and 0 <= ny < h):
                        break
                    n = ny * w + nx
                    if not walk[n]:
                        break
                    if dx and dy and (not walk[y * w + nx] or not walk[ny * w + x]):
                        break
                    x, y = nx, ny
                    v = D[n]
                    if v < INF:
                        v += k * cost
                        if v < best:
                            best = v
            out[c] = best
    return out


def field_relax(w, h, walk, goals, rays=0, max_passes=1 << 30):
    """iterate passes until one changes nothing; returns (D, passes that changed something)"""
    D = field_init(w, h, walk, goals)
    passes = 0
    while passes < max_passes:
        N = relax_pass_rays(w, h, walk, D, rays) if rays else relax_pass(w, h, walk, D)
        if N == D:
            break
        D = N
        passes += 1
    return D, passes


def dir_field(w, h, walk, D):
    """DIR per cell (see module doc)"""
    out = bytearray(w * h)
    for cy in range(h):
        for cx in range(w):
            c = cy * w + cx
            d = D[c]
            if not walk[c] or d >= INF:
                out[c] = NOGO
                continue
            if d == 0:
                out[c] = DGOAL
                continue
            best, bi = INF, NOGO
            for i in range(8):
                dx, dy = NB[i]
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                n = ny * w + nx
                if not walk[n] or D[n] >= INF:
                    continue
                if dx and dy and (not walk[cy * w + nx] or not walk[ny * w + cx]):
                    continue
                v = D[n] + (COST_D if dx and dy else COST_S)
                if v < best:
                    best, bi = v, i
            out[c] = bi
    return out


def pack_rgba8(D):
    """a field as RGBA8 texels (little endian 24 bit value in RGB, A = 255): the readback format that works on every GPU/API"""
    b = bytearray()
    for v in D:
        b += bytes((v & 255, (v >> 8) & 255, (v >> 16) & 255, 255))
    return bytes(b)


def fnv1a(D):
    h = 2166136261
    for v in D:
        h = ((h ^ (v & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h


def load_map_grid(mapdir):
    """(w, h, flags) of work/maps/<dir>/war3map.wpm"""
    from w3map import parse_wpm
    with open(os.path.join(mapdir, 'war3map.wpm'), 'rb') as fh:
        d = parse_wpm(fh.read())
    return d['w'], d['h'], d['cells']


def largest_component_goal(w, h, walk):
    """a deterministic goal: the walkable cell with the largest (distance to it) among a few probes is overkill; take the walkable cell
    nearest the grid centre of the largest 8-connected (corner rule) component"""
    comp = [-1] * (w * h)
    best, bn, nid = None, 0, 0
    for s in range(w * h):
        if walk[s] and comp[s] < 0:
            comp[s] = nid
            st = [s]
            n = 0
            cells = []
            while st:
                c = st.pop()
                n += 1
                cells.append(c)
                cx, cy = c % w, c // w
                for dx, dy in NB:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        m = ny * w + nx
                        if walk[m] and comp[m] < 0 and not (dx and dy and (not walk[cy * w + nx] or not walk[ny * w + cx])):
                            comp[m] = nid
                            st.append(m)
            if n > bn:
                bn, best = n, cells
            nid += 1
    mx, my = w // 2, h // 2
    return min(best, key=lambda c: ((c % w - mx) ** 2 + (c // w - my) ** 2, c))


if __name__ == '__main__':
    import time
    mp = sys.argv[1]
    w, h, fl = load_map_grid(os.path.join(HERE, '..', '..', 'work', 'maps', mp))
    walk = walk_array(w, h, fl)
    goal = largest_component_goal(w, h, walk)
    t = time.time()
    D = field_dijkstra(w, h, walk, [goal])
    t1 = time.time() - t
    reach = sum(1 for v in D if v < INF)
    print(mp, w, h, 'goal', goal % w, goal // w, 'reach', reach, 'max', max(v for v in D if v < INF), 'dijkstra %.2fs' % t1, 'hash %08x' % fnv1a(D))
    for L in (0, 8, 32):
        t = time.time()
        R, p = field_relax(w, h, walk, [goal], rays=L)
        print('  relax rays=%d: passes %d  equal %s  %.1fs' % (L, p, R == D, time.time() - t))
