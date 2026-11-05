# Map hologram preview bundle "GSPV" for the mod hub (docs/hub_concept.md section 3, unity GsvrHubHologram.cs).
# Reads BSP v30 maps of the player's OWN install (parse-only), keeps ONLY the world faces (model 0: no brush entities,
# no lightmaps, no collision, no entity text except the player spawn points), welds + decimates them (quadric edge
# collapse) to a triangle budget and packs a whole series of maps into one small bundle. Stdlib only (runs inside the
# companion, also as a frozen build via --run-converter).
#
# usage: python3 -I previewpack.py --game-root DIR --mod MOD --bsp NAME=PATH [--bsp NAME=PATH ...] [--tris 24000]
#                                  [--max-bytes 10485760] out.gspv
#        python3 -I previewpack.py --version
#
# ── GSPV layout (little endian, everything 4-byte aligned) ──
#   header 32 B   0 char[4] "GSPV"   4 i32 version = 1   8 i32 mapCount   12 i32 totalBytes (= file size)
#                 16 i32 flags (0)   20 i32 triBudget per map (informational)   24, 28 reserved 0
#   directory     mapCount * 80 B:  0 char[32] map name (ASCII, lower case, zero padded)   32 i32 V vertices   36 i32 T triangles
#                 40 i32 M spawn markers (their prisms are the LAST vertices / triangles, vertex lum = 2.0)   44 i32 dataOffset (absolute)
#                 48 f32 hx  52 f32 hy  56 f32 hz   half extents of the world geometry (Unity metres, y up)
#                 60 f32 sx  64 f32 sy  68 f32 sz   first spawn position (centred, Unity metres), valid when M > 0
#                 72, 76 reserved 0
#   data per map  V * 16 B: float x, y, z, lum  (position centred on the world bounding box centre, Unity axes
#                 x = gs x, y = gs z (up), z = gs y; metres = inches * 0.0254; lum 0..1 = average brightness of the
#                 face textures, 2.0 = spawn marker)  then  T * 12 B: int32 a, b, c  (clockwise seen from the front in
#                 Unity, front = the side the BSP face looks to, so single-sided rendering shows the inside of rooms)
# Normals are NOT stored: the shader takes them from screen-space derivatives (flat shading, no vertex splitting).
import argparse, hashlib, heapq, math, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # -I drops the script dir from sys.path

VERSION = "gspv-1.2"
MAGIC = b"GSPV"
HEADER = 32
ENTRY = 80
INCH = 0.0254
DEFAULT_TRIS = 24000        # typical budget per map (docs/hub_concept.md 3.4)
HARD_TRIS = 40000           # hard cap per map
MAX_MARKERS = 6
BYTES_PER_VERT = 16
BYTES_PER_TRI = 12
MAX_BUNDLE = 24 * 1024 * 1024
SPAWN_CLASSES = ("info_player_start", "info_player_coop", "info_player_deathmatch", "info_player_team1", "info_player_team2")


# ───────────────────────── geometry helpers (pure, tested) ─────────────────────────

def gs_to_unity(x, y, z):
    return (x * INCH, z * INCH, y * INCH)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _len(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def tri_area(p, a, b, c):
    n = _cross(_sub(p[b], p[a]), _sub(p[c], p[a]))
    return 0.5 * _len(n)


def weld(raw_tris, quantum=0.004):
    """raw_tris: list of ((x,y,z) * 3, lum). -> (positions, lum per vertex, triangles). Vertices closer than the quantum merge
    (rounded grid), degenerate and duplicate triangles go, vertex lum = mean of the incident triangles'."""
    index, pos, lsum, lcnt, tris, seen = {}, [], [], [], [], set()
    inv = 1.0 / quantum
    for corners, lum in raw_tris:
        ids = []
        for p in corners:
            key = (round(p[0] * inv), round(p[1] * inv), round(p[2] * inv))
            i = index.get(key)
            if i is None:
                i = len(pos)
                index[key] = i
                pos.append(p)
                lsum.append(0.0)
                lcnt.append(0)
            ids.append(i)
        a, b, c = ids
        if a == b or b == c or a == c:
            continue
        k = (a, b, c)
        s = tuple(sorted(k))
        if s in seen:
            continue
        seen.add(s)
        tris.append(k)
        for i in ids:
            lsum[i] += lum
            lcnt[i] += 1
    lum = [lsum[i] / lcnt[i] if lcnt[i] else 0.5 for i in range(len(pos))]
    return pos, lum, tris


def compact(pos, lum, tris):
    """Drops unused vertices and degenerate / duplicate triangles, keeps the order. -> (pos, lum, tris)."""
    remap, npos, nlum, ntris, seen = {}, [], [], [], set()
    for a, b, c in tris:
        if a == b or b == c or a == c:
            continue
        s = tuple(sorted((a, b, c)))
        if s in seen:
            continue
        seen.add(s)
        out = []
        for v in (a, b, c):
            j = remap.get(v)
            if j is None:
                j = len(npos)
                remap[v] = j
                npos.append(tuple(pos[v]))
                nlum.append(lum[v])
            out.append(j)
        ntris.append(tuple(out))
    return npos, nlum, ntris


def cull_small(pos, lum, tris, limit):
    """Pre-pass for huge maps: drop the smallest-area triangles until at most `limit` remain (ties by index)."""
    if len(tris) <= limit:
        return pos, lum, tris
    order = sorted(range(len(tris)), key=lambda i: (tri_area(pos, *tris[i]), i))
    drop = set(order[:len(tris) - limit])
    return compact(pos, lum, [t for i, t in enumerate(tris) if i not in drop])


def simplify(pos, lum, tris, target, boundary_weight=1.0):
    """Quadric edge collapse (Garland-Heckbert, error evaluated at the two ends and the midpoint) down to `target` triangles.
    Deterministic (heap ties by vertex ids). Folding collapses (a surviving triangle normal turning by more than ~75 degrees)
    are refused. -> (pos, lum, tris) compacted."""
    if len(tris) <= target:
        return compact(pos, lum, tris)
    nv = len(pos)
    P = [list(p) for p in pos]
    Lm = list(lum)
    T = [list(t) for t in tris]
    alive = [True] * len(T)
    live = len(T)
    vt = [set() for _ in range(nv)]
    Q = [[0.0] * 10 for _ in range(nv)]

    def add_plane(v, a, b, c, d, w):
        q = Q[v]
        q[0] += w * a * a; q[1] += w * a * b; q[2] += w * a * c; q[3] += w * a * d
        q[4] += w * b * b; q[5] += w * b * c; q[6] += w * b * d
        q[7] += w * c * c; q[8] += w * c * d; q[9] += w * d * d

    edge_count = {}
    for ti, (a, b, c) in enumerate(T):
        vt[a].add(ti); vt[b].add(ti); vt[c].add(ti)
        n = _cross(_sub(P[b], P[a]), _sub(P[c], P[a]))
        ln = _len(n)
        if ln < 1e-12:
            continue
        area = 0.5 * ln
        nx, ny, nz = n[0] / ln, n[1] / ln, n[2] / ln
        d = -(nx * P[a][0] + ny * P[a][1] + nz * P[a][2])
        for v in (a, b, c):
            add_plane(v, nx, ny, nz, d, area)
        for e in ((a, b), (b, c), (c, a)):
            k = (e[0], e[1]) if e[0] < e[1] else (e[1], e[0])
            if k in edge_count:
                edge_count[k][0] += 1
            else:
                edge_count[k] = [1, nx, ny, nz]
    # open edges (sheets, removed sky / trigger faces, T-junction gaps): a plane through the edge, perpendicular to its face
    for (a, b), (cnt, nx, ny, nz) in sorted(edge_count.items()):
        if cnt != 1:
            continue
        ed = _sub(P[b], P[a])
        el = _len(ed)
        if el < 1e-9:
            continue
        pn = _cross(ed, (nx, ny, nz))
        pl = _len(pn)
        if pl < 1e-12:
            continue
        pn = (pn[0] / pl, pn[1] / pl, pn[2] / pl)
        d = -_dot(pn, P[a])
        w = boundary_weight * el * el
        add_plane(a, pn[0], pn[1], pn[2], d, w)
        add_plane(b, pn[0], pn[1], pn[2], d, w)

    def err(q, x, y, z):
        return (q[0] * x * x + 2 * q[1] * x * y + 2 * q[2] * x * z + 2 * q[3] * x
                + q[4] * y * y + 2 * q[5] * y * z + 2 * q[6] * y
                + q[7] * z * z + 2 * q[8] * z + q[9])

    ver = [0] * nv
    dead = [False] * nv
    heap = []

    def push(a, b):
        qa, qb = Q[a], Q[b]
        q = [qa[i] + qb[i] for i in range(10)]
        pa, pb = P[a], P[b]
        mid = ((pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5, (pa[2] + pb[2]) * 0.5)
        best, bp = None, None
        for cand in (mid, pa, pb):
            e = err(q, cand[0], cand[1], cand[2])
            if best is None or e < best:
                best, bp = e, cand
        dx, dy, dz = pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2]
        cost = max(best, 0.0) + 1e-7 * (dx * dx + dy * dy + dz * dz)       # flat areas: shortest edges first
        heapq.heappush(heap, (cost, a, b, ver[a], ver[b], bp[0], bp[1], bp[2]))

    done = set()
    for ti, (a, b, c) in enumerate(T):
        for x, y in ((a, b), (b, c), (c, a)):
            k = (x, y) if x < y else (y, x)
            if k not in done:
                done.add(k)
                push(k[0], k[1])

    def flips(a, b, p):
        """True when moving a and b to p turns the normal of a surviving triangle by more than ~75 degrees (or degenerates it)."""
        for v in (a, b):
            for ti in vt[v]:
                t = T[ti]
                if (a in t) and (b in t):
                    continue
                o = [P[x] for x in t]
                n0 = _cross(_sub(o[1], o[0]), _sub(o[2], o[0]))
                q = [p if (x == a or x == b) else P[x] for x in t]
                n1 = _cross(_sub(q[1], q[0]), _sub(q[2], q[0]))
                l0, l1 = _len(n0), _len(n1)
                if l0 < 1e-12:
                    continue
                if l1 < 1e-14:
                    return True
                if _dot(n0, n1) < 0.25 * l0 * l1:
                    return True
        return False

    while live > target and heap:
        cost, a, b, va, vb, px, py, pz = heapq.heappop(heap)
        if dead[a] or dead[b] or ver[a] != va or ver[b] != vb:
            continue
        p = (px, py, pz)
        if flips(a, b, p):
            continue
        for ti in sorted(vt[b]):
            t = T[ti]
            if a in t:
                alive[ti] = False
                live -= 1
                for v in t:
                    vt[v].discard(ti)
            else:
                t[t.index(b)] = a
                vt[a].add(ti)
        vt[b] = set()
        dead[b] = True
        P[a] = [px, py, pz]
        Q[a] = [Q[a][i] + Q[b][i] for i in range(10)]
        Lm[a] = 0.5 * (Lm[a] + Lm[b])
        ver[a] += 1
        nb = set()
        for ti in vt[a]:
            for v in T[ti]:
                if v != a:
                    nb.add(v)
        for v in sorted(nb):
            if a < v:
                push(a, v)
            else:
                push(v, a)
    return compact(P, Lm, [tuple(t) for i, t in enumerate(T) if alive[i]])


def bounds(pos):
    xs = [p[0] for p in pos]; ys = [p[1] for p in pos]; zs = [p[2] for p in pos]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def marker_prisms(spawns, ext):
    """Spawn beacons: a thin 3-sided prism per spawn (6 vertices, 12 triangles: both windings, so single-sided rendering shows it from
    every side). spawns: Unity-metre positions. -> (positions, lum list (2.0), triangles)."""
    pos, lum, tris = [], [], []
    r = max(0.004 * ext, 0.12)
    h = max(0.05 * ext, 1.4)
    for sx, sy, sz in spawns:
        base = len(pos)
        ring = [(sx + r * math.cos(k * 2.0943951), sz + r * math.sin(k * 2.0943951)) for k in range(3)]
        for q in ring:
            pos.append((q[0], sy, q[1]))
        for q in ring:
            pos.append((q[0], sy + h, q[1]))
        lum += [2.0] * 6
        for k in range(3):
            k2 = (k + 1) % 3
            a, b, c, d = base + k, base + k2, base + 3 + k2, base + 3 + k
            tris += [(a, b, c), (a, c, d), (a, c, b), (a, d, c)]
    return pos, lum, tris


# ───────────────────────── BSP -> triangles ─────────────────────────

def _lum_table(pal):
    out = []
    for i in range(256):
        r, g, b = pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]
        out.append((0.299 * r + 0.587 * g + 0.114 * b) / 255.0)
    return out


def miptex_lum(buf, base):
    """Average brightness (0..1) of a miptex at buf[base:], read from the 1/8 size mip (cheap). None = no pixel data."""
    name, w, h, o0, o1, o2, o3 = struct.unpack_from("<16sII4I", buf, base)
    if o3 == 0 or w < 8 or h < 8 or w > 4096 or h > 4096:
        return None
    n = (w // 8) * (h // 8)
    idx = buf[base + o3:base + o3 + n]
    pal_off = base + o3 + n + 2
    pal = buf[pal_off:pal_off + 768]
    if len(idx) < n or len(pal) < 768:
        return None
    masked = name.startswith(b"{")
    lut = _lum_table(pal)
    tot, cnt = 0.0, 0
    for v in idx:
        if masked and v == 255:
            continue
        tot += lut[v]
        cnt += 1
    return tot / cnt if cnt else None


def texture_lums(bsp, used, game_root, mod, log=print):
    """{miptex index: lum 0..1} for the textures in `used` (embedded in the BSP first, then the WADs of the worldspawn 'wad' key)."""
    from wad3 import Wad, find_wads
    ents = bsp.entity_list()
    ws = ents[0] if ents else {}
    wads = []
    try:
        found, _missing, fallback = find_wads(ws.get("wad", ""), game_root, mod) if game_root else ([], [], [])
    except Exception as e:      # noqa: BLE001
        log(f"  wad search failed: {e}")
        found, fallback = [], []
    for p in list(found) + list(fallback):
        try:
            wads.append(Wad(p))
        except Exception:       # noqa: BLE001
            pass
    out = {}
    for mi in sorted(used):
        name, _w, _h, embedded = bsp.textures[mi]
        v = None
        if embedded:
            try:
                v = miptex_lum(bsp.data, bsp.miptex_offset(mi))
            except struct.error:
                v = None
        if v is None:
            for wd in wads:
                fp = wd.lumps.get(name.lower())
                if fp is not None:
                    try:
                        v = miptex_lum(wd.data, fp)
                    except struct.error:
                        v = None
                    if v is not None:
                        break
        out[mi] = 0.5 if v is None else v
    return out


def world_triangles(bsp, game_root, mod, log=print):
    """-> (raw triangles ((p, p, p), lum) in Unity metres, spawn positions in Unity metres). World model only, skipped textures
    (sky, clip, trigger, origin, null ...) removed, every triangle oriented so Unity's clockwise-front rule faces the BSP face's side."""
    from wad3 import is_skipped
    m0 = bsp.models[0]
    first, num = m0[14], m0[15]
    faces = []
    for f in bsp.faces[first:first + num]:
        mi = bsp.texinfo[f[4]][2]
        if mi < 0 or mi >= len(bsp.textures) or is_skipped(bsp.textures[mi][0]):
            continue
        faces.append((f, mi))
    lums = texture_lums(bsp, {mi for _f, mi in faces}, game_root, mod, log)
    raw = []
    for f, mi in faces:
        poly = [gs_to_unity(*p) for p in bsp.face_polygon(f)]
        if len(poly) < 3:
            continue
        pl = bsp.planes[f[0]]
        nrm = gs_to_unity(pl[0], pl[1], pl[2])
        if f[1]:
            nrm = (-nrm[0], -nrm[1], -nrm[2])
        lum = lums.get(mi, 0.5)
        for i in range(1, len(poly) - 1):
            a, b, c = poly[0], poly[i], poly[i + 1]
            n = _cross(_sub(b, a), _sub(c, a))
            if _len(n) < 1e-12:
                continue
            if _dot(n, nrm) < 0:
                b, c = c, b
            raw.append(((a, b, c), lum))
    spawns = []
    ents = bsp.entity_list()
    for cls in SPAWN_CLASSES:
        for e in ents:
            if e.get("classname") != cls:
                continue
            try:
                o = [float(v) for v in e.get("origin", "").split()]
            except ValueError:
                continue
            if len(o) == 3:
                spawns.append(gs_to_unity(*o))
    return raw, spawns[:MAX_MARKERS]


# ───────────────────────── per map + bundle ─────────────────────────

class MapPreview:
    def __init__(self, name, pos, lum, tris, nmark, half, start):
        self.name, self.pos, self.lum, self.tris, self.nmark, self.half, self.start = name, pos, lum, tris, nmark, half, start


def prepare_map(name, bsp_path, game_root, mod, log=print):
    """Parse + weld one map -> (welded positions, lum, triangles, spawns). Raises on a broken BSP."""
    from bsp30 import Bsp
    bsp = Bsp(bsp_path)
    raw, spawns = world_triangles(bsp, game_root, mod, log)
    if not raw:
        raise ValueError("no visible world faces")
    pos, lum, tris = weld(raw)
    return pos, lum, tris, spawns


def finish_map(name, pos, lum, tris, spawns, target):
    """Decimate to `target` triangles, centre, add the spawn markers -> MapPreview."""
    target = max(200, min(int(target), HARD_TRIS))
    if len(tris) > target * 4:
        pos, lum, tris = cull_small(pos, lum, tris, target * 4)
    pos, lum, tris = simplify(pos, lum, tris, target)
    lo, hi = bounds(pos)
    c = ((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, (lo[2] + hi[2]) * 0.5)
    half = ((hi[0] - lo[0]) * 0.5, (hi[1] - lo[1]) * 0.5, (hi[2] - lo[2]) * 0.5)
    ext = max(half) * 2.0
    cpos = [(p[0] - c[0], p[1] - c[1], p[2] - c[2]) for p in pos]
    sp = [(s[0] - c[0], s[1] - c[1], s[2] - c[2]) for s in spawns]
    mp, ml, mt = marker_prisms(sp, ext)
    base = len(cpos)
    cpos = cpos + mp
    lum = [min(max(v, 0.0), 1.0) for v in lum] + ml
    tris = list(tris) + [(a + base, b + base, c2 + base) for a, b, c2 in mt]
    if len(tris) > HARD_TRIS:
        raise ValueError(f"{len(tris)} triangles over the hard cap {HARD_TRIS}")
    return MapPreview(name, cpos, lum, tris, len(sp), half, sp[0] if sp else (0.0, 0.0, 0.0))


def allocate_targets(counts, tri_budget, max_bytes):
    """Per-map triangle targets: min(budget, welded count); when the bundle would be over max_bytes, every target is scaled down
    by the same factor (floor 1500). Vertex estimate = 0.6 per triangle."""
    t = [min(tri_budget, c) for c in counts]
    est = sum(int(x * 0.6) * BYTES_PER_VERT + x * BYTES_PER_TRI for x in t)
    if est > max_bytes:
        f = max_bytes / float(est)
        t = [max(1500, min(c, int(x * f))) for x, c in zip(t, counts)]
    return t


def pack_bundle(maps, tri_budget=DEFAULT_TRIS):
    """MapPreview list -> GSPV bytes."""
    n = len(maps)
    off = HEADER + ENTRY * n
    dir_bytes = bytearray()
    body = bytearray()
    for m in maps:
        v, t = len(m.pos), len(m.tris)
        name = m.name.encode("ascii")[:31]
        dir_bytes += name + b"\0" * (32 - len(name))
        dir_bytes += struct.pack("<4i", v, t, m.nmark, off + len(body))
        dir_bytes += struct.pack("<6f", m.half[0], m.half[1], m.half[2], m.start[0], m.start[1], m.start[2])
        dir_bytes += struct.pack("<2i", 0, 0)
        vb = bytearray()
        for p, l in zip(m.pos, m.lum):
            vb += struct.pack("<4f", p[0], p[1], p[2], l)
        body += vb
        tb = bytearray()
        for a, b, c in m.tris:
            tb += struct.pack("<3i", a, b, c)
        body += tb
    total = off + len(body)
    head = MAGIC + struct.pack("<7i", 1, n, total, 0, tri_budget, 0, 0)
    return bytes(head) + bytes(dir_bytes) + bytes(body)


def parse_bundle(data):
    """GSPV bytes -> list of dicts (name, V, T, M, half, start, pos, lum, tris). Raises ValueError with the reason. Same arithmetic
    as GsvrHubHologram.ParseBundle and companion/holo.py validate_hologram."""
    if len(data) < HEADER or data[:4] != MAGIC:
        raise ValueError("bad magic")
    ver, n, total, _fl, _bud, _r0, _r1 = struct.unpack_from("<7i", data, 4)
    if ver != 1 or not 0 <= n <= 256 or total != len(data) or HEADER + ENTRY * n > len(data):
        raise ValueError("bad header")
    out = []
    expect = HEADER + ENTRY * n
    for i in range(n):
        o = HEADER + ENTRY * i
        name = data[o:o + 32].split(b"\0")[0].decode("ascii")
        v, t, m, off = struct.unpack_from("<4i", data, o + 32)
        hx, hy, hz, sx, sy, sz = struct.unpack_from("<6f", data, o + 48)
        if v < 3 or t < 1 or t > HARD_TRIS or v > 3 * t or m < 0 or m > MAX_MARKERS or off != expect or off % 4:
            raise ValueError(f"bad directory entry {i} ({name})")
        end = off + v * BYTES_PER_VERT + t * BYTES_PER_TRI
        if end > len(data):
            raise ValueError(f"entry {i} ({name}) runs past the end")
        flat = struct.unpack_from(f"<{v * 4}f", data, off)
        idx = struct.unpack_from(f"<{t * 3}i", data, off + v * BYTES_PER_VERT)
        if min(idx) < 0 or max(idx) >= v:
            raise ValueError(f"entry {i} ({name}) index out of range")
        out.append(dict(name=name, V=v, T=t, M=m, half=(hx, hy, hz), start=(sx, sy, sz),
                        pos=[flat[k * 4:k * 4 + 3] for k in range(v)], lum=[flat[k * 4 + 3] for k in range(v)],
                        tris=[idx[k * 3:k * 3 + 3] for k in range(t)]))
        expect = end
    if expect != len(data):
        raise ValueError("trailing bytes")
    return out


def build_bundle(entries, game_root, mod, tri_budget=DEFAULT_TRIS, max_bytes=10 * 1024 * 1024, log=print):
    """entries: [(name, bsp path)] -> (bundle bytes, report list). Maps that fail to parse are skipped (reported)."""
    import bprog
    bprog.plan(prepare=70, finish=25, pack=5)
    prepared, report = [], []
    for ei, (name, path) in enumerate(entries):
        bprog.progress("prepare", ei, len(entries))
        try:
            pos, lum, tris, spawns = prepare_map(name, path, game_root, mod, log)
            prepared.append((name, pos, lum, tris, spawns))
        except Exception as e:      # noqa: BLE001
            log(f"  SKIP {name}: {e}")
            report.append(dict(name=name, error=str(e)[:160]))
    targets = allocate_targets([len(p[3]) for p in prepared], tri_budget, max_bytes)
    bprog.progress("prepare", len(entries), len(entries))
    maps = []
    for mi, ((name, pos, lum, tris, spawns), tg) in enumerate(zip(prepared, targets)):
        bprog.progress("finish", mi, len(prepared))
        m = finish_map(name, pos, lum, tris, spawns, tg)
        maps.append(m)
        report.append(dict(name=name, welded=len(tris), tris=len(m.tris), verts=len(m.pos), markers=m.nmark))
        log(f"  {name}: {len(tris)} welded tris -> {len(m.tris)} tris, {len(m.pos)} verts, {m.nmark} spawn markers")
    if not maps:
        raise ValueError("no map could be converted")
    bprog.progress("finish", len(prepared), len(prepared))
    data = pack_bundle(maps, tri_budget)
    bprog.progress("pack", 1, 1)
    if len(data) > MAX_BUNDLE:
        raise ValueError(f"bundle {len(data)} bytes over the limit {MAX_BUNDLE}")
    return data, report


def synthetic_bundle():
    """Three small procedural maps (a bumpy grid, no game data) for the offline checks of the Udon side."""
    maps = []
    for k, (name, bump) in enumerate((("synth1", 1.0), ("synth2", 2.5), ("synth3", 0.0))):
        raw = []
        for i in range(24):
            for j in range(24):
                def h(x, z):
                    return bump * math.exp(-((x - 12) ** 2 + (z - 12) ** 2) / 40.0)
                p = [(x * 0.5, h(x, z), z * 0.5) for x, z in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))]
                raw.append(((p[0], p[3], p[2]), 0.3 + 0.05 * k))
                raw.append(((p[0], p[2], p[1]), 0.6))
        pos, lum, tris = weld(raw)
        maps.append(finish_map(name, pos, lum, tris, [(1.0, 0.0, 1.0), (5.0, 0.0, 5.0)], 700 + 200 * k))
    return pack_bundle(maps)


def manifest(data):
    """One line per map: name V T M idxsum hx hy hz (idxsum = sum of the triangle indices; the Udon harness recomputes it)."""
    out = []
    for m in parse_bundle(data):
        out.append("%s %d %d %d %d %.6f %.6f %.6f" % (m["name"], m["V"], m["T"], m["M"], sum(sum(t) for t in m["tris"]), *m["half"]))
    return "\n".join(out) + "\n"


def safe_name(n):
    return bool(n) and len(n) <= 31 and all(ch.isalnum() or ch in "_-" for ch in n) and n == n.lower()


def main(argv=None):
    ap = argparse.ArgumentParser(description="GSPV map hologram preview bundle builder")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--game-root")
    ap.add_argument("--mod", default="valve")
    ap.add_argument("--bsp", action="append", default=[], help="NAME=PATH of a .bsp; repeat for the series")
    ap.add_argument("--tris", type=int, default=DEFAULT_TRIS, help="triangle budget per map (cap %d)" % HARD_TRIS)
    ap.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--synthetic", action="store_true", help="write a bundle of three procedural maps (offline checks), no game files")
    ap.add_argument("--manifest", help="print the manifest (name V T M index-sum half extents) of an existing bundle file and exit")
    ap.add_argument("out", nargs="?")
    a = ap.parse_args(argv)
    if a.version:
        print(VERSION)
        return 0
    if a.manifest:
        with open(a.manifest, "rb") as fh:
            sys.stdout.write(manifest(fh.read()))
        return 0
    if a.synthetic:
        if not a.out:
            ap.error("need the output file")
        with open(a.out, "wb") as fh:
            fh.write(synthetic_bundle())
        return 0
    if not a.out or not a.bsp:
        ap.error("need at least one --bsp NAME=PATH and the output file")
    entries = []
    for item in a.bsp:
        name, _, path = item.partition("=")
        name = name.lower()
        if not safe_name(name) or not path:
            ap.error(f"bad --bsp {item!r}")
        entries.append((name, path))
    log = (lambda *_a: None) if a.quiet else print
    data, report = build_bundle(entries, a.game_root, a.mod, min(a.tris, HARD_TRIS), a.max_bytes, log)
    tmp = a.out + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, a.out)
    log(f"wrote {a.out}: {len(report)} maps, {len(data)} bytes, sha1 {hashlib.sha1(data).hexdigest()[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
