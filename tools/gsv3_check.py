# Independent GSV3 checker (shares NO code with pack.py / bsp30.py — it only follows the layout documented in the
# pack.py header and docs/goldsrc_base_layer.md). Verifies a pack offline before a VRChat test:
#   1. structure: header, 4-byte alignment, directory, every index/offset/child in range, model ranges disjoint
#   2. entities: keyvalue table vs. the entity text, record <-> entity back-links, class counts (--expect)
#   3. brush models: model-local vertices lie inside the record's mins/maxs, render/collide ranges
#   4. Hull 1 (player hull 32x32x72): point contents at every spawn (must be empty), trace down vs. a ray cast
#      against the render/collision triangles (floor must be 36 u below the hull hit), horizontal traces vs. the
#      render walls expanded by the box (16·|nx| + 16·|ny| + 36·|nz| along the wall normal)
#   5. volumes: func_ladder / trigger_* centres are inside their own model hull; hull-0 contents histogram (water …)
#   6. optional --probe FILE: writes hull probes (point contents + traces) for the Mono harness, which runs the same
#      queries through GsvrEntHull.cs and must get identical answers
#   7. optional TMAT (surface materials): one letter per pack triangle (count == I/3), every value an upper-case
#      ASCII letter; the loader's map-collider triangle list (world solid + merged models, GsvrPackLoader.BuildMesh
#      routing re-implemented here for pool capacity 0 and 64) gets exactly one letter per collider triangle;
#      letter histogram of the world collider; --expect-mat LETTERS requires each letter on the collider
#      (--require-tmat fails a pack without TMAT)
#   8. optional REND (render modes): one entry per MODL record, each equal to the entity's rendermode / renderamt /
#      renderfx / rendercolor keys parsed here independently (missing key = 0, as in the engine); MODL flag 32 set
#      exactly on rendermode 1/2/3/5 models with renderamt > 0 and render triangles, never together with flag 8
#      (static-mergeable); the loader routing (translucent records first) re-implemented for pool 64 pools every
#      translucent record (none merged into the opaque world); --expect-translucent N, --require-rend
# usage: python3 -I tools/gsv3_check.py pack.bin [--expect func_door=16 --expect func_ladder=4 …] [--probe out.txt]
#        [--expect-mat CMT] [--require-tmat] [--expect-translucent N] [--require-rend]
import math, os, re, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

INCH = 0.0254
DIST_EPS = 0.03125
EMPTY, SOLID, WATER, SLIME, LAVA, SKY, LADDER = -1, -2, -3, -4, -5, -6, -16
FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)
    return cond


# ───────────────────────── decode ─────────────────────────

def decode(path):
    d = open(path, "rb").read()
    check(d[:4] == b"GSV3", f"magic GSV3 (got {d[:4]!r})")
    hb, V, I, S, AW, AH, M, LW, LH, E, flags, ntex, WV, WI, DIR = struct.unpack_from("<15i", d, 4)
    check(hb == 64 and DIR % 4 == 0 and len(d) % 4 == 0, f"header 64 B, directory at {DIR} aligned, size {len(d)} aligned")
    o = hb
    def take(n, fmt):
        nonlocal o
        v = struct.unpack_from(f"<{n}{fmt}", d, o); o += 4 * n; return v
    P = dict(V=V, I=I, S=S, WV=WV, WI=WI)
    P["pos"] = take(3 * V, "f"); take(2 * V, "f"); take(2 * V, "f"); take(4 * V, "f")
    P["idx"] = take(I, "i")
    o += sum((AW >> l) * (AH >> l) * 4 for l in range(M)) + LW * LH * 4
    P["text"] = d[o:o + E].decode("latin-1"); o += E + (-E % 4)
    check(o == DIR, f"core sections end exactly at the directory ({o} == {DIR})")
    n, = struct.unpack_from("<i", d, DIR)
    secs = {}
    end = DIR + 4 + 16 * n
    for k in range(n):
        tag, off, size, cnt = struct.unpack_from("<4s3i", d, DIR + 4 + 16 * k)
        secs[tag.decode()] = (off, size, cnt)
        check(off == end and off % 4 == 0 and size % 4 == 0, f"section {tag.decode()} @{off} {size} B ×{cnt} contiguous + aligned")
        end = off + size
    check(end == len(d), f"directory covers the file exactly ({end} == {len(d)})")
    def sec(tag, fmt, per):
        off, size, cnt = secs[tag]
        check(size == cnt * per * 4, f"{tag}: {cnt} × {per} fields")
        return struct.unpack_from(f"<{cnt * per}{fmt}", d, off), cnt
    raw, R = sec("MODL", "i", 24)
    fl = struct.unpack_from(f"<{R * 24}f", d, secs["MODL"][0])
    P["recs"] = []
    for r in range(R):
        a, f = raw[r * 24:(r + 1) * 24], fl[r * 24:(r + 1) * 24]
        P["recs"].append(dict(model=a[0], ent=a[1], fv=a[2], vc=a[3], fi=a[4], rs=a[5], rc=a[6], cs=a[7], cc=a[8],
                              flags=a[9], head=a[10:14], origin=f[14:17], mins=f[17:20], maxs=f[20:23]))
    pl, NP = sec("PLAN", "f", 4)
    P["planes"] = [pl[i * 4:i * 4 + 4] for i in range(NP)]
    hn, NH = sec("HNOD", "i", 3)
    P["nodes"] = [hn[i * 3:i * 3 + 3] for i in range(NH)]
    en, NE = sec("ENTS", "i", 4)
    P["ents"] = [en[i * 4:i * 4 + 4] for i in range(NE)]
    kv, NK = sec("EKVS", "i", 4)
    P["kvs"] = [kv[i * 4:i * 4 + 4] for i in range(NK)]
    P["rend"] = None
    if "REND" in secs:
        P["rend"] = [tuple(v) for v in zip(*[iter(sec("REND", "i", 2)[0])] * 2)]
    P["clip"] = None
    if "CLIP" in secs:                 # collision-only clip-hull floors: float32 pos [3 V] (Unity metres) + int32 idx [3 T]
        off, size, cnt = secs["CLIP"]
        nv = (size - cnt * 12) // 12
        check(nv > 0 and size == nv * 12 + cnt * 12, f"CLIP: {cnt} triangles over {nv} vertices")
        P["clip"] = (struct.unpack_from(f"<{nv * 3}f", d, off), struct.unpack_from(f"<{cnt * 3}i", d, off + nv * 12))
        check(all(0 <= i < nv for i in P["clip"][1]), "CLIP indices inside the CLIP vertex list")
    P["nodg"] = None
    if "NODG" in secs:                 # AI node graph (tools/nodegraph.py:section)
        import nodegraph
        off, size, cnt = secs["NODG"]
        G = nodegraph.parse_section(d[off:off + size])
        N, L = len(G["nodes"]), len(G["links"])
        check(G["version"] == nodegraph.VERSION and N == cnt and 0 < N <= nodegraph.MAX_NODES,
              f"NODG: graph version {G['version']}, {N} nodes (directory count {cnt})")
        check(all(0 <= n["first"] and n["first"] + n["count"] <= L for n in G["nodes"])
              and all(0 <= k["src"] < N and 0 <= k["dst"] < N and k["ent"] < NE for k in G["links"]),
              f"NODG: {L} links inside the node / entity arrays")
        check(all(0 <= s_ < len(G["tables"]) for s_ in G["slots"]) and all(max(t) < N for t in G["tables"]),
              f"NODG: {len(G['tables'])} route tables, every next hop a node index")
        P["nodg"] = G
    P["tmat"] = None
    if "TMAT" in secs:
        off, size, cnt = secs["TMAT"]
        check(size == cnt + (-cnt % 4), f"TMAT: {cnt} bytes padded to {size}")
        P["tmat"] = d[off:off + cnt]
    print(f"{path}: V={V} (world {WV}) I={I} (world {WI}, solid {S}) records={R} planes={NP} nodes={NH} "
          f"entities={NE} kvs={NK} size={len(d)}")
    return P


def g_of(P, vi):
    """vertex → GoldSrc units (Unity (x, y, z) metres = GoldSrc (x, z, y) inches)."""
    x, y, z = P["pos"][3 * vi:3 * vi + 3]
    return (x / INCH, z / INCH, y / INCH)


# ───────────────────────── structure ─────────────────────────

def structure(P):
    recs, V, I = P["recs"], P["V"], P["I"]
    w = recs[0]
    check(w["model"] == 0 and w["fv"] == 0 and w["vc"] == P["WV"] and w["fi"] == 0 and w["rc"] == P["WI"]
          and w["cc"] == P["S"], "record 0 = world model, matches header WV/WI/S")
    nv = ni = 0
    bad = 0
    for k, r in enumerate(recs):
        if r["fv"] != nv or r["fi"] != ni:
            bad += 1
        nv += r["vc"]; ni = r["fi"] + max(r["rs"] + r["rc"], r["cs"] + r["cc"], 0)
        if not (r["rs"] >= 0 and r["cs"] >= 0 and r["rc"] % 3 == 0 and r["cc"] % 3 == 0):
            bad += 1
        loc = P["idx"][r["fi"]:ni]
        if loc and (min(loc) < 0 or max(loc) >= r["vc"]):
            bad += 1
        if (r["rc"] > 0) != bool(r["flags"] & 1) or (r["cc"] > 0) != bool(r["flags"] & 2):
            bad += 1
    check(bad == 0 and nv == V and ni == I, f"records tile the vertex/index arrays, local indices in range ({bad} bad)")
    NH, NP = len(P["nodes"]), len(P["planes"])
    okc = all(0 <= n[0] < NP and all((0 <= c < NH) or -16 <= c <= -1 for c in n[1:]) for n in P["nodes"])
    check(okc, "HNOD: plane indices and children in range (child < 0 = contents -1..-16)")
    okh = all(all((0 <= h < NH) or -16 <= h <= -1 for h in r["head"]) for r in recs)
    check(okh, "MODL headnodes in range")
    unit = sum(1 for p in P["planes"] if abs(math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2) - 1) > 1e-3)
    check(unit == 0, f"plane normals are unit length ({unit} bad)")
    # model-local vertices inside the dmodel bounds (proves the converter did NOT add the entity origin)
    out = 0; tot = 0
    for r in recs[1:]:
        for vi in range(r["fv"], r["fv"] + r["vc"]):
            p = g_of(P, vi); tot += 1
            if any(p[a] < r["mins"][a] - 0.5 or p[a] > r["maxs"][a] + 0.5 for a in range(3)):
                out += 1
    check(out == 0, f"brush-model vertices are model-local and inside mins/maxs ({tot} vertices, {out} outside)")


# ───────────────────────── entities ─────────────────────────

def entities(P, expect):
    text, kvs = P["text"], P["kvs"]
    ents = []
    for fk, kc, rec, _ in P["ents"]:
        kv = []
        for k in range(fk, fk + kc):
            ko, kl, vo, vl = kvs[k]
            kv.append((text[ko:ko + kl], text[vo:vo + vl]))
        ents.append(dict(kv=kv, d=dict(kv), rec=rec))
    # reference: the entity text parsed independently with a different method (character scanner; braces inside
    # quoted values such as "{C1A3_ORG1" are data)
    ref, cur, toks, i = [], None, [], 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            j = text.index('"', i + 1)
            toks.append(text[i + 1:j]); i = j + 1
            if cur is not None and len(toks) == 2:
                cur.append((toks[0], toks[1])); toks = []
            continue
        if ch == "{":
            cur, toks = [], []
        elif ch == "}" and cur is not None:
            ref.append(cur); cur = None
        i += 1
    check(len(ref) == len(ents) and all(ref[i] == ents[i]["kv"] for i in range(len(ents))),
          f"EKVS table == independent parse of the entity text ({len(ents)} entities)")
    okb = all(P["recs"][e["rec"]]["ent"] == i for i, e in enumerate(ents) if e["rec"] >= 1)
    okm = all(r["ent"] < 0 or (ents[r["ent"]]["rec"] == k or k == 0) for k, r in enumerate(P["recs"]))
    mdl = all(ents[r["ent"]]["d"].get("model") == f"*{r['model']}" for r in P["recs"][1:])
    check(okb and okm and mdl, "MODL.entityIndex <-> ENTS.modelRecord consistent, entity 'model' key = *bspModel")
    cls = {}
    for r in P["recs"][1:]:
        c = ents[r["ent"]]["d"].get("classname", "?")
        cls[c] = cls.get(c, 0) + 1
    print("  brush entity classes:", ", ".join(f"{k}×{v}" for k, v in sorted(cls.items())))
    for e in expect:
        c, n = e.split("=")
        check(cls.get(c, 0) == int(n), f"expected {n}× {c} brush records (got {cls.get(c, 0)})")
    return ents


# ───────────────────────── hull ─────────────────────────

class Hull:
    def __init__(self, P, sky_solid=True):
        self.planes, self.nodes = P["planes"], P["nodes"]
        self.sky_solid = sky_solid

    def solid(self, c):
        return c == SOLID or (self.sky_solid and c == SKY)

    def contents(self, head, p):
        n = head
        while n >= 0:
            pl, a, b = self.nodes[n]
            nx, ny, nz, dist = self.planes[pl]
            n = a if nx * p[0] + ny * p[1] + nz * p[2] - dist >= 0 else b
        return n

    def trace(self, head, p1, p2):
        """Line p1→p2 through the hull tree → (fraction, endpos, normal, startsolid, allsolid). Depth-first, near
        side first, so the first solid leaf reached is the first impact; the impact plane is the split that started
        that leaf's segment. End position backed off DIST_EPS from the plane (as the engine keeps it off planes)."""
        d = (p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2])
        stack = [(head, 0.0, 1.0, None)]
        startsolid, allsolid = False, True
        while stack:
            n, fa, fb, enter = stack.pop()
            if n < 0:
                if not self.solid(n):
                    allsolid = False
                    continue
                if enter is None:            # segment begins at the trace start: start point is in solid
                    startsolid = True
                    continue
                pl, side, t1, t2, pa, pb = enter
                nx, ny, nz, dist = self.planes[pl]
                f = (t1 - DIST_EPS) / (t1 - t2) if side == 0 else (t1 + DIST_EPS) / (t1 - t2)
                f = min(max(f, 0.0), 1.0)
                frac = pa + (pb - pa) * f
                normal = (nx, ny, nz) if side == 0 else (-nx, -ny, -nz)
                end = (p1[0] + d[0] * frac, p1[1] + d[1] * frac, p1[2] + d[2] * frac)
                return frac, end, normal, startsolid, False
            pl, a, b = self.nodes[n]
            nx, ny, nz, dist = self.planes[pl]
            t1 = nx * (p1[0] + d[0] * fa) + ny * (p1[1] + d[1] * fa) + nz * (p1[2] + d[2] * fa) - dist
            t2 = nx * (p1[0] + d[0] * fb) + ny * (p1[1] + d[1] * fb) + nz * (p1[2] + d[2] * fb) - dist
            if t1 >= 0 and t2 >= 0:
                stack.append((a, fa, fb, enter))
            elif t1 < 0 and t2 < 0:
                stack.append((b, fa, fb, enter))
            else:
                fm = fa + (fb - fa) * (t1 / (t1 - t2))
                side = 0 if t1 >= 0 else 1
                near, far = (a, b) if side == 0 else (b, a)
                stack.append((far, fm, fb, (pl, side, t1, t2, fa, fb)))
                stack.append((near, fa, fm, enter))
        return 1.0, tuple(p2), None, startsolid, allsolid and startsolid


# ───────────────────────── render-triangle raycast (GoldSrc space) ─────────────────────────

class Tris:
    CELL = 64.0

    def __init__(self, P, ents):
        self.t = []
        for k, r in enumerate(P["recs"]):
            if r["cc"] == 0:
                continue
            o = r["origin"] if k else (0.0, 0.0, 0.0)
            loc = P["idx"][r["fi"] + r["cs"]:r["fi"] + r["cs"] + r["cc"]]
            for j in range(0, len(loc), 3):
                tri = []
                for q in loc[j:j + 3]:
                    g = g_of(P, r["fv"] + q)
                    tri.append((g[0] + o[0], g[1] + o[1], g[2] + o[2]))
                self.t.append(tri)
        self.grid = {}
        for ti, (a, b, c) in enumerate(self.t):
            x0, x1 = min(a[0], b[0], c[0]), max(a[0], b[0], c[0])
            y0, y1 = min(a[1], b[1], c[1]), max(a[1], b[1], c[1])
            for gx in range(int(math.floor(x0 / self.CELL)), int(math.floor(x1 / self.CELL)) + 1):
                for gy in range(int(math.floor(y0 / self.CELL)), int(math.floor(y1 / self.CELL)) + 1):
                    self.grid.setdefault((gx, gy), []).append(ti)

    def cast(self, o, dirn, maxd):
        cells = set()
        steps = max(1, int(maxd * math.hypot(dirn[0], dirn[1]) / self.CELL) + 1)
        for s in range(steps + 1):
            for t in (s / steps * maxd,):
                x, y = o[0] + dirn[0] * t, o[1] + dirn[1] * t
                for ex in (-1, 0, 1):
                    for ey in (-1, 0, 1):
                        cells.add((int(math.floor(x / self.CELL)) + ex, int(math.floor(y / self.CELL)) + ey))
        best, bn = maxd, None
        seen = set()
        for c in cells:
            for ti in self.grid.get(c, ()):
                if ti in seen:
                    continue
                seen.add(ti)
                a, b, cc = self.t[ti]
                e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2]); e2 = (cc[0] - a[0], cc[1] - a[1], cc[2] - a[2])
                p = (dirn[1] * e2[2] - dirn[2] * e2[1], dirn[2] * e2[0] - dirn[0] * e2[2], dirn[0] * e2[1] - dirn[1] * e2[0])
                det = e1[0] * p[0] + e1[1] * p[1] + e1[2] * p[2]
                if abs(det) < 1e-9:
                    continue
                inv = 1.0 / det
                s_ = (o[0] - a[0], o[1] - a[1], o[2] - a[2])
                u = (s_[0] * p[0] + s_[1] * p[1] + s_[2] * p[2]) * inv
                if u < -1e-6 or u > 1 + 1e-6:
                    continue
                q = (s_[1] * e1[2] - s_[2] * e1[1], s_[2] * e1[0] - s_[0] * e1[2], s_[0] * e1[1] - s_[1] * e1[0])
                v = (dirn[0] * q[0] + dirn[1] * q[1] + dirn[2] * q[2]) * inv
                if v < -1e-6 or u + v > 1 + 1e-6:
                    continue
                t = (e2[0] * q[0] + e2[1] * q[1] + e2[2] * q[2]) * inv
                if 1e-4 < t < best:
                    n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
                    ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1
                    best, bn = t, (n[0] / ln, n[1] / ln, n[2] / ln)
        return best, bn


# ───────────────────────── surface materials ─────────────────────────

def collider_tris(P, capacity, merge_static=True):
    """Pack triangle numbers of the loader's map collider, in its order (GsvrPackLoader.RouteModels + BuildMesh):
    world solid [0, S/3), then every record r >= 1 routed 0 (merged), in record order, its collide range."""
    out = list(range(P["S"] // 3))
    pooled = 0
    trans_pooled = set()
    for k, r in enumerate(P["recs"]):                  # translucent records (flag 32) take pool slots first
        if k > 0 and r["flags"] & 32 and not r["flags"] & 4 and (r["rc"] > 0 or r["cc"] > 0) and pooled < capacity:
            pooled += 1
            trans_pooled.add(k)
    for k, r in enumerate(P["recs"]):
        if k == 0 or k in trans_pooled:
            continue
        if (r["rc"] <= 0 and r["cc"] <= 0) or r["flags"] & 4:
            continue                                   # route 2: data only
        if r["flags"] & 32:
            pass                                       # translucent, pool full: merged (drawn opaque)
        elif not (merge_static and r["flags"] & 8):
            if pooled < capacity:
                pooled += 1                            # route 1: own pooled mesh
                continue
        t0 = (r["fi"] + r["cs"]) // 3
        out += range(t0, t0 + r["cc"] // 3)
    return out


def materials(P, expect_mat, require):
    tm = P["tmat"]
    if tm is None:
        check(not require, "TMAT section present" if require else "no TMAT section (optional)")
        return
    check(len(tm) * 3 == P["I"], f"TMAT: one letter per pack triangle ({len(tm)} == I/3 = {P['I'] // 3})")
    bad = sum(1 for c in tm if not 65 <= c <= 90)
    check(bad == 0, f"TMAT: every value is an upper-case ASCII letter ({bad} bad)")
    for cap in (0, 64):
        col = collider_tris(P, cap)
        expect_n = P["S"] // 3 + sum(r["cc"] // 3 for r in P["recs"][1:] if r["cc"] > 0 and not r["flags"] & 4
                                     and (r["flags"] & 8 or cap == 0))
        ok = all(0 <= t < len(tm) for t in col) and len(col) == len(set(col))
        check(ok and (cap != 0 or len(col) == expect_n),
              f"TMAT: loader collider (pool {cap}) = {len(col)} triangles, each with one letter")
    col = collider_tris(P, 64)
    hist = {}
    for t in col:
        c = chr(tm[t])
        hist[c] = hist.get(c, 0) + 1
    n = max(1, len(col))
    print("  materials (map collider, pool 64): " + " ".join(f"{k}={v} ({100.0 * v / n:.1f}%)"
                                                            for k, v in sorted(hist.items(), key=lambda kv: -kv[1])))
    check(hist.get("C", 0) > 0, "TMAT: concrete (the CS default) occurs")
    for c in expect_mat:
        check(hist.get(c, 0) > 0, f"TMAT: expected material '{c}' on the collider ({hist.get(c, 0)} triangles)")


def render_modes(P, ents, expect_n, require):
    rend = P["rend"]
    if rend is None:
        check(not require, "REND section present" if require else "no REND section (optional)")
        return
    recs = P["recs"]
    check(len(rend) == len(recs), f"REND: one entry per MODL record ({len(rend)} == {len(recs)})")

    def key_int(d, k):
        v = d.get(k, "").strip()
        m = re.match(r"[-+]?\d+(\.\d*)?", v)
        return int(float(m.group(0))) if m else 0
    bad_kv = bad_flag = 0
    trans = []
    for k, r in enumerate(recs):
        w0, w1 = rend[k] if k < len(rend) else (0, 0)
        if k == 0:
            bad_kv += (w0, w1) != (0, 0)
            continue
        d = ents[r["ent"]]["d"]
        mode = min(5, max(0, key_int(d, "rendermode")))
        amt = min(255, max(0, key_int(d, "renderamt")))
        fx = min(255, max(0, key_int(d, "renderfx")))
        cols = [min(255, max(0, int(float(x)))) for x in d.get("rendercolor", "").split()[:3]] + [0, 0, 0]
        want = (mode | amt << 8 | fx << 16, cols[0] | cols[1] << 8 | cols[2] << 16)
        bad_kv += (w0, w1) != want
        t = mode in (1, 2, 3, 5) and amt > 0 and r["rc"] > 0
        if bool(r["flags"] & 32) != t or (r["flags"] & 32 and r["flags"] & 8):
            bad_flag += 1
        if t:
            trans.append(k)
    check(bad_kv == 0, f"REND entries == entity render keys (missing key = 0) ({bad_kv} bad)")
    check(bad_flag == 0, f"MODL flag 32 exactly on blended models with renderamt > 0, never with flag 8 ({bad_flag} bad)")
    col = set(collider_tris(P, 64))
    merged = [k for k in trans if (recs[k]["fi"] + recs[k]["cs"]) // 3 in col and recs[k]["cc"] > 0]
    check(not merged, f"pool 64: every translucent record pooled, none merged into the opaque world ({len(merged)} merged)")
    modes = {}
    for k in trans:
        cls = ents[recs[k]["ent"]]["d"].get("classname", "?")
        key = f"{cls} mode {rend[k][0] & 255} amt {(rend[k][0] >> 8) & 255}"
        modes[key] = modes.get(key, 0) + 1
    print("  translucent brush models: " + (", ".join(f"{v}x {k}" for k, v in sorted(modes.items())) or "none"))
    if expect_n is not None:
        check(len(trans) == expect_n, f"expected {expect_n} translucent brush models (got {len(trans)})")


def num3(s):
    v = [float(x) for x in s.split()[:3]]
    return tuple(v + [0.0] * (3 - len(v)))


def hull_checks(P, ents, probe_path):
    H = Hull(P)
    W = P["recs"][0]
    head1 = W["head"][1]
    T = Tris(P, ents)
    solids = [r for r in P["recs"][1:] if r["cc"] > 0 and not r["flags"] & (4 | 16)]

    def trace_all(a, b):
        """World hull 1 plus every solid brush model at its spawn position (doors closed), like the engine's
        move clipping against all SOLID_BSP entities: the earliest impact wins."""
        best = H.trace(head1, a, b)
        for r in solids:
            o = r["origin"]
            lo = [min(a[q], b[q]) - o[q] for q in range(3)]; hi = [max(a[q], b[q]) - o[q] for q in range(3)]
            if any(hi[q] < r["mins"][q] - 17 or lo[q] > r["maxs"][q] + 17 for q in range(2)) or \
               hi[2] < r["mins"][2] - 37 or lo[2] > r["maxs"][2] + 37:
                continue
            t = H.trace(r["head"][1], (a[0] - o[0], a[1] - o[1], a[2] - o[2]), (b[0] - o[0], b[1] - o[1], b[2] - o[2]))
            if t[0] < best[0]:
                best = (t[0], tuple(a[q] + (b[q] - a[q]) * t[0] for q in range(3)), t[2], best[3] or t[3], False)
        return best
    spawns = [num3(e["d"]["origin"]) for e in ents
              if e["d"].get("classname") in ("info_player_start", "info_player_deathmatch") and "origin" in e["d"]]
    probes = []
    # spawns in empty hull-1 space (the player's origin = box centre must be outside the expanded solid)
    empt = sum(1 for s in spawns if not H.solid(H.contents(head1, s)))
    check(len(spawns) > 0 and empt >= 0.9 * len(spawns), f"hull 1: {empt}/{len(spawns)} spawn origins in empty space")
    # floor: hull-1 trace down vs. render ray down → hull end should be 36 u above the render floor
    good = n = 0
    worst = 0.0
    for s in spawns:
        fr, end, nrm, ss, als = trace_all(s, (s[0], s[1], s[2] - 512))
        probes.append(("T", head1, s, (s[0], s[1], s[2] - 512), fr))
        dr, _nr = T.cast(s, (0.0, 0.0, -1.0), 600)
        if fr >= 1 or dr >= 600:
            continue
        n += 1
        diff = (s[2] - end[2]) + 36 - dr          # hull floor (end − 36) vs. render floor (s − dr)
        worst = max(worst, abs(diff))
        if abs(diff) <= 1.0:
            good += 1
    check(n > 0 and good >= 0.8 * n, f"hull 1 floor = render floor + 36 u at {good}/{n} spawns (|diff| ≤ 1 u, worst {worst:.2f})")
    # horizontal: hull-1 distance vs. render distance minus the box expansion along the hit normal
    agree = early = late = tot = box = offs = 0
    for s in spawns:
        for k in range(8):
            a = k * math.pi / 4
            dirn = (math.cos(a), math.sin(a), 0.0)
            p2 = (s[0] + dirn[0] * 1024, s[1] + dirn[1] * 1024, s[2])
            probes.append(("T", head1, s, p2, H.trace(head1, s, p2)[0]))
            fr, end, nrm, ss, als = trace_all(s, p2)
            if ss or fr >= 1:
                continue
            dh = fr * 1024
            best = bests = None
            side = (-dirn[1], dirn[0])
            for dz in (0.0,):          # centre ray only: the expansion formulas already contain the box extents
                for dl in (0.0,):
                    o = (s[0] + side[0] * dl, s[1] + side[1] * dl, s[2] + dz)
                    dr, nr = T.cast(o, dirn, 1100)
                    if nr is None:
                        continue
                    cosv = abs(dirn[0] * nr[0] + dirn[1] * nr[1])
                    if cosv < 0.2 or abs(nr[2]) > 0.05:     # vertical walls only (bounded slopes are not plane offsets)
                        continue
                    # two expansion rules exist in the compile tools: box (Minkowski: 16|nx|+16|ny|+36|nz|) and
                    # "plane offset" (16·|n_xy| + 36·|nz| along the normal, corners closed by bevel planes)
                    exp = dr - (16 * abs(nr[0]) + 16 * abs(nr[1]) + 36 * abs(nr[2])) / cosv
                    exps = dr - (16 * math.hypot(nr[0], nr[1]) + 36 * abs(nr[2])) / cosv
                    if best is None or exp < best:
                        best = exp
                    if bests is None or exps < bests:
                        bests = exps
            if best is None:
                continue
            tot += 1
            if abs(dh - best) <= 2.0:
                agree += 1; box += 1
            elif abs(dh - bests) <= 2.0:
                agree += 1; offs += 1
            elif dh < best:
                early += 1
            else:
                late += 1
    check(tot > 0 and agree >= 0.5 * tot and late <= 0.05 * tot, f"hull 1 walls = render walls expanded by the box: {agree}/{tot} rays within 2 u "
          f"({box} box rule, {offs} plane-offset rule; {early} hull earlier: box corners/clip brushes/ledges; {late} hull later)")
    # volumes
    hist = {}
    for nd in P["nodes"][:]:
        for c in nd[1:]:
            if c < 0:
                hist[c] = hist.get(c, 0) + 1
    print("  contents histogram (node children):", ", ".join(f"{k}:{v}" for k, v in sorted(hist.items())))
    vols = {}
    for k, r in enumerate(P["recs"][1:], 1):
        cls = ents[r["ent"]]["d"].get("classname", "?")
        if not (r["flags"] & 4 or cls.startswith(("func_ladder", "trigger_", "func_water"))):
            continue
        c = tuple((r["mins"][a] + r["maxs"][a]) * 0.5 for a in range(3))
        in1 = H.contents(r["head"][1], c) != EMPTY
        if not in1:                       # non-convex volume (several brushes): any point of a 5³ grid inside
            g5 = [tuple(r["mins"][a] + (r["maxs"][a] - r["mins"][a]) * (q[a] + 0.5) / 5 for a in range(3))
                  for q in ((i, j, k) for i in range(5) for j in range(5) for k in range(5))]
            hit = [p for p in g5 if H.contents(r["head"][1], p) != EMPTY]
            if hit:
                in1, c = True, hit[0]
        in0 = H.contents(r["head"][0], c) != EMPTY
        probes.append(("C", r["head"][1], c, c, H.contents(r["head"][1], c)))
        probes.append(("C", r["head"][0], c, c, H.contents(r["head"][0], c)))
        out = (r["maxs"][0] + 64, c[1], c[2])          # just outside the model's bounds
        ok_out = H.contents(r["head"][1], out) == EMPTY
        v = vols.setdefault(cls, [0, 0, 0])
        v[0] += 1; v[1] += in1 and ok_out; v[2] += in0
    for cls, (cnt, ok1, ok0) in sorted(vols.items()):
        check(ok1 == cnt, f"{cls}: {ok1}/{cnt} model volumes inside own hull 1 (and empty just outside the bounds); hull 0: {ok0}/{cnt}")
    # random world point probes for the harness
    import random
    rnd = random.Random(7)
    xs = [g_of(P, i) for i in range(0, P["WV"], max(1, P["WV"] // 400))]
    for i in range(300):
        a, b = rnd.choice(xs), rnd.choice(xs)
        a = (a[0] + rnd.uniform(-40, 40), a[1] + rnd.uniform(-40, 40), a[2] + rnd.uniform(-10, 60))
        b = (b[0] + rnd.uniform(-40, 40), b[1] + rnd.uniform(-40, 40), b[2] + rnd.uniform(-10, 60))
        for h in (W["head"][0], head1):
            probes.append(("C", h, a, a, H.contents(h, a)))
            probes.append(("T", h, a, b, H.trace(h, a, b)[0]))
    movers = mover_expectations(P, ents)
    if probe_path:
        with open(probe_path, "w") as fh:
            for kind, h, a, b, v in probes:
                fh.write(f"{kind} {h} {a[0]!r} {a[1]!r} {a[2]!r} {b[0]!r} {b[1]!r} {b[2]!r} {v!r}\n")
            for line in movers:
                fh.write(line + "\n")
        print(f"  wrote {len(probes)} hull probes + {len(movers)} mover endpoints to {probe_path}")


def mover_expectations(P, ents):
    """HL door/button endpoints (doors.cpp / buttons.cpp behaviour, re-derived here independently of the C#):
    movedir from angles (0 -1 0 = up, 0 -2 0 = down, else forward), travel = Σ|dir_i·(size_i − 2)| − lip."""
    out = []
    for k, r in enumerate(P["recs"][1:], 1):
        d = ents[r["ent"]]["d"]
        cls = d.get("classname")
        sf = int(float(d.get("spawnflags", "0") or 0))
        if "angles" in d:
            ang = num3(d["angles"])
        else:
            ang = (0.0, float(d.get("angle", "0") or 0), 0.0)
        o = r["origin"]
        if cls in ("func_door", "func_button"):
            if ang == (0.0, -1.0, 0.0):
                mv = (0.0, 0.0, 1.0)
            elif ang == (0.0, -2.0, 0.0):
                mv = (0.0, 0.0, -1.0)
            else:
                p_, y_ = math.radians(ang[0]), math.radians(ang[1])
                mv = (math.cos(p_) * math.cos(y_), math.cos(p_) * math.sin(y_), -math.sin(p_))
            lip = float(d.get("lip", "0") or 0)
            if cls == "func_button" and lip == 0:
                lip = 4.0
            size = [r["maxs"][q] - r["mins"][q] for q in range(3)]
            travel = sum(abs(mv[q] * (size[q] - 2)) for q in range(3)) - lip
            p1 = tuple(o); p2 = tuple(o[q] + mv[q] * travel for q in range(3))
            if cls == "func_button" and (math.dist(p1, p2) < 1 or sf & 1):
                p2 = p1
            if cls == "func_door" and sf & 1:
                p1, p2 = p2, p1
            out.append("D %d %r %r %r %r %r %r" % (r["ent"], *p1, *p2))
        elif cls in ("func_door_rotating", "func_rot_button"):
            axis = (0.0, 0.0, 1.0) if sf & 64 else ((1.0, 0.0, 0.0) if sf & 128 else (0.0, 1.0, 0.0))
            if sf & 2:
                axis = tuple(-a for a in axis)
            dist = float(d.get("distance", "0") or 0)
            a1 = ang; a2 = tuple(ang[q] + axis[q] * dist for q in range(3))
            if cls == "func_door_rotating" and sf & 1:
                a1, a2 = a2, a1
            out.append("R %d %r %r %r %r %r %r" % (r["ent"], *a1, *a2))
    return out


def main():
    args = sys.argv[1:]
    expect, probe = [], None
    while "--expect" in args:
        k = args.index("--expect"); expect.append(args[k + 1]); del args[k:k + 2]
    if "--probe" in args:
        k = args.index("--probe"); probe = args[k + 1]; del args[k:k + 2]
    expect_mat, require_tmat = "", False
    if "--expect-mat" in args:
        k = args.index("--expect-mat"); expect_mat = args[k + 1]; del args[k:k + 2]
    if "--require-tmat" in args:
        args.remove("--require-tmat"); require_tmat = True
    expect_trans, require_rend = None, False
    if "--expect-translucent" in args:
        k = args.index("--expect-translucent"); expect_trans = int(args[k + 1]); del args[k:k + 2]
    if "--require-rend" in args:
        args.remove("--require-rend"); require_rend = True
    P = decode(args[0])
    structure(P)
    materials(P, expect_mat, require_tmat)
    ents = entities(P, expect)
    render_modes(P, ents, expect_trans, require_rend)
    if P["planes"]:
        hull_checks(P, ents, probe)
    print("GSV3 CHECK " + ("PASS" if not FAILS else f"FAIL ({len(FAILS)})"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
