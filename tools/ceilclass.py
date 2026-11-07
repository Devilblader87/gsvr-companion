"""Duck-scheme ceiling classification of a triangle list: a Python port of GsvrPackLoader.ClassifyCeilings.

The VRChat runtime classifies every collision triangle of a map (world + merged models) and of every pooled brush model at load time
(rules 1-5, see GsvrPackLoader.cs "ceiling split"): kind 1 = ceiling (down-facing, hanging lintel / head-band wall piece, top of a thin
slab), 0 = floor / wall. In Udon that took 18 s on big maps, so tools/pack.py computes the same kinds offline and stores them in the
pack's CEIL section (docs/goldsrc_base_layer.md); the runtime then only builds the two colliders. This module is that offline
computation. It must give the C# result triangle for triangle: tools/usharp_offline/ceil_pack_check.py compares both on real maps
(the C# side is the compiled runtime, duckscan_kinds.exe), and check.sh fails on a single differing triangle.

Same stages in the same order as the C# state machine (CeilStep): 0 normals, 1-2 grid of the down-facing triangles, 3-4 of the up-facing
ones, 10-11 of the vertical ones, 5 hanging-wall seeds (rule 2), 8 slab tops (rules 3 / 5), 12-13 head-band wall pieces (rule 4, two
passes), 6 coplanar runs, 7 final. The lookup grids use the same hashed 1 m xz buckets (so the same hash collisions), the C# height
prefilters are left out (they only skip entries that cannot hit). Python floats are doubles, the runtime's are float32: the inputs are
rounded to float32 by the callers (vertices come from the pack), the arithmetic is not - tools/usharp_offline/ceil_pack_check.py is the proof
that this makes no difference on the real maps.
"""
import math, struct
from array import array

CEIL_NORMAL_Y = -0.5
CELL = 1.0
TOL_XZ = 0.06
TOL_Y = 0.04
MAX_CELLS = 400
MIN_CLEAR = 0.88
SLAB_THICK = 0.20
SLAB_NARROW = 0.50
MAX_HANG = 1.83
SLAB_THICK_CRAWL = 0.95
SIDE_PROBE = 0.08
BOTTOM_SAMPLES = 4
HANG_FREE = 0.10
CRAWL_STEP = 0.5
CRAWL_GRID = 8
FACE_GAP = 0.03
SLIVER_W = 0.01
NONE_LOW = -1e9
_sqrt = math.sqrt
_floor = math.floor


def _hash(cx, cz, mask):
    return ((cx * 7919) ^ (cz * 104729)) & mask


class Grid:
    """CSR-like hashed xz grid: buckets[h] = triangle indices (ascending), big = triangles covering more than MAX_CELLS cells."""

    def __init__(self, count):
        nb = 64
        while nb < count * 2:
            nb *= 2
        self.mask = nb - 1
        self.b = {}
        self.big = []

    def add(self, t, x0, x1, z0, z1):
        cx0, cx1 = int(_floor(x0 / CELL)), int(_floor(x1 / CELL))
        cz0, cz1 = int(_floor(z0 / CELL)), int(_floor(z1 / CELL))
        if (cx1 - cx0 + 1) * (cz1 - cz0 + 1) > MAX_CELLS:
            self.big.append(t)
            return
        b, mask = self.b, self.mask
        for cx in range(cx0, cx1 + 1):
            for cz in range(cz0, cz1 + 1):
                h = ((cx * 7919) ^ (cz * 104729)) & mask
                lst = b.get(h)
                if lst is None:
                    b[h] = [t]
                else:
                    lst.append(t)

    def at(self, px, pz):
        return self.b.get(_hash(int(_floor(px / CELL)), int(_floor(pz / CELL)), self.mask), ())


_cell = array("f", [0.0])


def r(x):
    """round a Python float (double) to float32 (round to nearest even), the way every operation of the C# float arithmetic does."""
    _cell[0] = x
    return _cell[0]


_r = r

# the C# float constants
TOLXZ = r(0.06)
TOLY = r(0.04)
MINCLEAR = r(0.88)
SLABTHICK = r(0.20)
SLABNARROW = r(0.50)
MAXHANG = r(1.83)
SLABCRAWL = r(0.95)
SIDEPROBE = r(0.08)
HANGFREE = r(0.10)
FACEGAP = r(0.03)
SLIVERW = r(0.01)
C001 = r(0.01)
C003 = r(0.03)
C0005 = r(0.005)
C002 = r(0.002)
C00005 = r(0.0005)
C002b = r(0.02)
C08 = r(0.8)
C033 = r(0.33)
C098 = r(0.98)
C005 = r(0.05)
C0999 = r(0.999)
C1E6 = r(1e-6)
C1E5 = r(1e-5)
C1E4 = r(1e-4)
C1E9 = r(1e-9)
GUARD = 1e-3          # margin of the double prefilters below: a decision this far from its threshold is the same in float32


def classify(X, Y, Z, tris, ref_y=None):
    """X / Y / Z = vertex coordinates (Unity metres, float32 values), tris = flat index list (a, b, c per triangle, winding as in the runtime:
    normal = cross(b - a, c - a)). ref_y = None for the world, else the ground height used under brush-model pieces (1e9 = no ground).
    Every float operation of the C# is rounded to float32 in the same order, so the result is the runtime's, triangle for triangle.
    -> bytearray kind[t] (1 = ceiling)."""
    n = len(tris) // 3
    K = bytearray(n)
    if n == 0:
        return K
    ref_on = ref_y is not None
    REF = r(ref_y) if ref_on else 0.0
    NRM = [None] * n          # unit normal, None = degenerate (cross product < 1e-9)
    PD = [0.0] * n
    P = [None] * n            # per up / down-facing triangle (len > 1e-5): corners, normal, edges, winding sign
    down, up, wall = [], [], []
    for t in range(n):
        ia, ib, ic = tris[3 * t], tris[3 * t + 1], tris[3 * t + 2]
        ax, ay, az = X[ia], Y[ia], Z[ia]
        bx, by, bz = X[ib], Y[ib], Z[ib]
        cx, cy, cz = X[ic], Y[ic], Z[ic]
        ux, uy, uz = r(bx - ax), r(by - ay), r(bz - az)
        vx, vy, vz = r(cx - ax), r(cy - ay), r(cz - az)
        nx, ny, nz = r(r(uy * vz) - r(uz * vy)), r(r(uz * vx) - r(ux * vz)), r(r(ux * vy) - r(uy * vx))
        ln = r(_sqrt(r(r(r(nx * nx) + r(ny * ny)) + r(nz * nz))))
        if ln < C1E9:
            continue
        nx, ny, nz = r(nx / ln), r(ny / ln), r(nz / ln)
        NRM[t] = (nx, ny, nz)
        PD[t] = r(r(r(nx * ax) + r(ny * ay)) + r(nz * az))
        if ny < CEIL_NORMAL_Y:
            K[t] = 1
            down.append(t)
        elif ny > 0.5:
            up.append(t)
        else:
            wall.append(t)
            continue
        if not (ln > C1E5):
            continue
        dx1, dz1 = r(bx - ax), r(bz - az)
        dx2, dz2 = r(cx - bx), r(cz - bz)
        dx3, dz3 = r(ax - cx), r(az - cz)
        area = r(r(r(bx - ax) * r(cz - az)) - r(r(bz - az) * r(cx - ax)))
        P[t] = (ax, ay, az, bx, by, bz, cx, cy, cz, nx, ny, nz,
                dx1, dz1, r(_sqrt(r(r(dx1 * dx1) + r(dz1 * dz1)))), dx2, dz2, r(_sqrt(r(r(dx2 * dx2) + r(dz2 * dz2)))),
                dx3, dz3, r(_sqrt(r(r(dx3 * dx3) + r(dz3 * dz3)))), 1.0 if area >= 0.0 else -1.0)
    if not down and not up:
        return K

    def tri_box(t):
        ia, ib, ic = tris[3 * t], tris[3 * t + 1], tris[3 * t + 2]
        xa, xb, xc, za, zb, zc = X[ia], X[ib], X[ic], Z[ia], Z[ib], Z[ic]
        return min(xa, xb, xc), max(xa, xb, xc), min(za, zb, zc), max(za, zb, zc)

    def build(lst, count):
        g = Grid(count)
        for t in lst:
            x0, x1, z0, z1 = tri_box(t)
            g.add(t, x0, x1, z0, z1)
        return g

    # (the bucket count follows the per-class triangle counts of stage 0, the vertical grid wants strictly -0.5 < ny < 0.5 and a horizontal normal)
    gdown, gup = build(down, len(down)), build(up, len(up))
    gwall = build([t for t in wall if -0.5 < NRM[t][1] < 0.5 and (NRM[t][0] != 0.0 or NRM[t][2] != 0.0)], len(wall))

    def plane_y(p, px, pz):
        """_pV.y - (n.x * (px - a.x) + n.z * (pz - a.z)) / n.y in float32."""
        return r(p[1] - r(r(r(p[9] * r(px - p[0])) + r(p[11] * r(pz - p[2]))) / p[10]))

    def plane_y_fast(p, px, pz):
        return p[1] - (p[9] * (px - p[0]) + p[11] * (pz - p[2])) / p[10]

    def inside(p, px, pz):
        """InsideXz: the three xz edge tests (the triangle grown by CeilTolXz)."""
        sg = p[21]
        pxa = px - p[0]
        # double prefilter: clearly inside every edge / clearly outside one
        fast = []
        for lx, lz, dx, dz, ln in ((p[0], p[2], p[12], p[13], p[14]), (p[3], p[5], p[15], p[16], p[17]), (p[6], p[8], p[18], p[19], p[20])):
            fast.append(1.0 if ln < 1e-6 else sg * (dx * (pz - lz) - dz * (px - lx)) / ln)
        if fast[0] < -0.06 - GUARD or fast[1] < -0.06 - GUARD or fast[2] < -0.06 - GUARD:
            return False
        if fast[0] > -0.06 + GUARD and fast[1] > -0.06 + GUARD and fast[2] > -0.06 + GUARD:
            return True
        for lx, lz, dx, dz, ln in ((p[0], p[2], p[12], p[13], p[14]), (p[3], p[5], p[15], p[16], p[17]), (p[6], p[8], p[18], p[19], p[20])):
            d = 1.0 if ln < C1E6 else r(r(r(dx * r(pz - lz)) - r(dz * r(px - lx))) / ln)
            if r(sg * d) < -TOLXZ:
                return False
        return True

    skip_ceil = [False]
    wib_dep = [False]

    def floor_below(px, py, pz):
        best = NONE_LOW
        top = r(py + TOLY)
        for lst in (gup.at(px, pz), gup.big):
            for t in lst:
                p = P[t]
                if p is None:
                    continue
                if p[10] < C001:
                    continue
                if skip_ceil[0] and K[t] != 0:
                    continue
                yd = plane_y_fast(p, px, pz)
                if yd > top + GUARD or yd <= best - GUARD:
                    continue
                y = plane_y(p, px, pz)
                if y > top or y <= best:
                    continue
                if not inside(p, px, pz):
                    continue
                best = y
        return best

    def on_ceiling(px, py, pz):
        for t in gdown.at(px, pz):
            p = P[t]
            if p is None:
                continue
            if p[10] > -C001:
                continue
            if abs(plane_y_fast(p, px, pz) - py) > 0.04 + GUARD:
                continue
            y = plane_y(p, px, pz)
            if abs(r(y - py)) > TOLY:
                continue
            if inside(p, px, pz):
                return True
        return False

    def slab_under(px, py, pz):
        best = 1e9
        for t in gdown.at(px, pz):
            p = P[t]
            if p is None:
                continue
            if p[10] > -C001:
                continue
            dd = py - plane_y_fast(p, px, pz)
            if dd < -0.04 - GUARD or dd > 0.20 + GUARD:
                continue
            y = plane_y(p, px, pz)
            d = r(py - y)
            if d < -TOLY or d > SLABTHICK:
                continue
            if not inside(p, px, pz):
                continue
            if y < best:
                best = y
        return best

    def slab_under_near(px, py, pz, max_thick):
        best = NONE_LOW
        for t in gdown.at(px, pz):
            p = P[t]
            if p is None:
                continue
            if p[10] > -C001:
                continue
            dd = py - plane_y_fast(p, px, pz)
            if dd < 0.005 - GUARD or dd > max_thick + GUARD:
                continue
            y = plane_y(p, px, pz)
            d = r(py - y)
            if d < C0005 or d > max_thick:
                continue
            if not inside(p, px, pz):
                continue
            if y > best:
                best = y
        return 1e9 if best < -1e8 else best

    def corners(t):
        ia, ib, ic = tris[3 * t], tris[3 * t + 1], tris[3 * t + 2]
        return (X[ia], Y[ia], Z[ia]), (X[ib], Y[ib], Z[ib]), (X[ic], Y[ic], Z[ic])

    def wall_hangs(t):
        a, b, c = corners(t)
        p0, p1, p2 = a, b, c
        if p1[1] < p0[1]:
            p0, p1 = p1, p0
        if p2[1] < p0[1]:
            p0, p2 = p2, p0
        if p2[1] < p1[1]:
            p1, p2 = p2, p1
        if r(p1[1] - p0[1]) > C003:
            return False
        if r(p2[1] - p1[1]) < C003:
            return False
        if not on_ceiling(*p0):
            return False
        if not on_ceiling(*p1):
            return False
        half = r(0.5)
        mx, my, mz = r(r(p0[0] + p1[0]) * half), r(r(p0[1] + p1[1]) * half), r(r(p0[2] + p1[2]) * half)
        if not on_ceiling(mx, my, mz):
            return False
        fl = floor_below(mx, my, mz)
        if fl < -1e8 and ref_on:
            fl = REF
        return r(my - fl) >= MINCLEAR

    def sqr(a, b):
        dx, dy, dz = r(a[0] - b[0]), r(a[1] - b[1]), r(a[2] - b[2])
        return r(r(r(dx * dx) + r(dy * dy)) + r(dz * dz))

    def sliver_hangs(a, b, c):
        p, q, o = a, b, c
        lab, lbc, lca = sqr(b, a), sqr(c, b), sqr(a, c)
        if lbc > lab and lbc >= lca:
            p, q, o = b, c, a
        elif lca > lab and lca > lbc:
            p, q, o = c, a, b
        ex, ey, ez = r(q[0] - p[0]), r(q[1] - p[1]), r(q[2] - p[2])
        ln = r(_sqrt(r(r(r(ex * ex) + r(ey * ey)) + r(ez * ez))))
        if ln < C005:
            return False
        wx, wy, wz = r(o[0] - p[0]), r(o[1] - p[1]), r(o[2] - p[2])
        cx_, cy_, cz_ = r(r(ey * wz) - r(ez * wy)), r(r(ez * wx) - r(ex * wz)), r(r(ex * wy) - r(ey * wx))
        cm = r(_sqrt(r(r(r(cx_ * cx_) + r(cy_ * cy_)) + r(cz_ * cz_))))
        if r(cm / ln) > SLIVERW:
            return False
        band = False
        for k in range(BOTTOM_SAMPLES):
            f = r(r(k + 0.5) / BOTTOM_SAMPLES)
            sx, sy, sz = r(p[0] + r(ex * f)), r(p[1] + r(ey * f)), r(p[2] + r(ez * f))
            fl = floor_below(sx, sy, sz)
            if fl < -1e8:
                if not ref_on:
                    return False
                fl = REF
            clear = r(sy - fl)
            if clear <= HANGFREE:
                return False
            if clear >= MINCLEAR and clear < MAXHANG:
                band = True
        return band

    def crawl_slab_top(t):
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = corners(t)
        e1x, e1y, e1z = r(bx - ax), r(by - ay), r(bz - az)
        e2x, e2y, e2z = r(cx - bx), r(cy - by), r(cz - bz)
        e3x, e3y, e3z = r(ax - cx), r(ay - cy), r(az - cz)
        edge = max(r(_sqrt(r(r(r(e1x * e1x) + r(e1y * e1y)) + r(e1z * e1z)))),
                   r(_sqrt(r(r(r(e2x * e2x) + r(e2y * e2y)) + r(e2z * e2z)))),
                   r(_sqrt(r(r(r(e3x * e3x) + r(e3y * e3y)) + r(e3z * e3z)))))
        ng = min(max(int(math.ceil(r(edge / r(CRAWL_STEP)))), 1), CRAWL_GRID)
        acx, acy, acz = r(cx - ax), r(cy - ay), r(cz - az)
        for k in range((ng + 1) * (ng + 1)):
            gi, gj = k // (ng + 1), k % (ng + 1)
            fu, fv = r(r(gi + C033) / (ng + 1)), r(r(gj + C033) / (ng + 1))
            if r(fu + fv) > C098:
                continue
            mx = r(r(ax + r(e1x * fu)) + r(acx * fv))
            my = r(r(ay + r(e1y * fu)) + r(acy * fv))
            mz = r(r(az + r(e1z * fu)) + r(acz * fv))
            under = slab_under_near(mx, my, mz, SLABCRAWL)
            if under > 1e8:
                continue
            fl = floor_below(mx, under, mz)
            if fl < -1e8:
                if not ref_on:
                    continue
                fl = REF
            clear = r(under - fl)
            if clear >= MINCLEAR and clear < MAXHANG and r(my - fl) < MAXHANG:
                return True
        return False

    def slab_top(t):
        a, b, c = corners(t)
        w = r(max(a[0], b[0], c[0]) - min(a[0], b[0], c[0]))
        d = r(max(a[2], b[2], c[2]) - min(a[2], b[2], c[2]))
        if min(w, d) > SLABNARROW:
            return False
        three = r(3.0)
        m = tuple(r(r(r(a[i] + b[i]) + c[i]) / three) for i in range(3))
        under = slab_under(*m)
        if under > 1e8:
            return False
        for v in (a, b, c):
            if slab_under(*[r(m[i] + r(r(v[i] - m[i]) * C08)) for i in range(3)]) > 1e8:
                return False
        fl = floor_below(m[0], under, m[2])
        if fl < -1e8 and ref_on:
            fl = REF
        return r(under - fl) >= MINCLEAR

    def in_face_below(t, px, py, pz, tx, tz):
        nx, ny, nz = NRM[t]
        pdt = PD[t]
        pu = r(r(px * tx) + r(pz * tz))
        for lst in (gwall.at(px, pz), gwall.big):
            for u in lst:
                if u == t or K[u] != 0:
                    continue
                if abs(PD[u] - pdt) > 0.01 + GUARD:
                    continue
                if abs(r(PD[u] - pdt)) > C001:
                    continue
                m = NRM[u]
                if m[0] * nx + m[1] * ny + m[2] * nz < 0.999 - GUARD:
                    continue
                if r(r(r(m[0] * nx) + r(m[1] * ny)) + r(m[2] * nz)) < C0999:
                    continue
                a, b, c = corners(u)
                # double prefilter: every d clearly away from the +-1e-6 thresholds decides without the float32 chain
                fa, fb, fc_ = a[0] * tx + a[2] * tz, b[0] * tx + b[2] * tz, c[0] * tx + c[2] * tz
                f1 = (fb - fa) * (py - a[1]) - (b[1] - a[1]) * (pu - fa)
                f2 = (fc_ - fb) * (py - b[1]) - (c[1] - b[1]) * (pu - fb)
                f3 = (fa - fc_) * (py - c[1]) - (a[1] - c[1]) * (pu - fc_)
                if abs(f1) > 1e-4 and abs(f2) > 1e-4 and abs(f3) > 1e-4:
                    if not ((f1 < 0 or f2 < 0 or f3 < 0) and (f1 > 0 or f2 > 0 or f3 > 0)):
                        return True
                    continue
                au, bu, cu = r(r(a[0] * tx) + r(a[2] * tz)), r(r(b[0] * tx) + r(b[2] * tz)), r(r(c[0] * tx) + r(c[2] * tz))
                d1 = r(r(r(bu - au) * r(py - a[1])) - r(r(b[1] - a[1]) * r(pu - au)))
                d2 = r(r(r(cu - bu) * r(py - b[1])) - r(r(c[1] - b[1]) * r(pu - bu)))
                d3 = r(r(r(au - cu) * r(py - c[1])) - r(r(a[1] - c[1]) * r(pu - cu)))
                neg = d1 < -C1E6 or d2 < -C1E6 or d3 < -C1E6
                pos = d1 > C1E6 or d2 > C1E6 or d3 > C1E6
                if not (neg and pos):
                    return True
        return False

    def wall_in_head_band(t):
        skip_ceil[0] = True
        wib_dep[0] = False
        res = _wall_in_head_band(t)
        skip_ceil[0] = False
        return res

    def _wall_in_head_band(t):
        a, b, c = corners(t)
        ymin, ymax = min(a[1], b[1], c[1]), max(a[1], b[1], c[1])
        if r(ymax - ymin) < C001:
            return False
        if sliver_hangs(a, b, c):
            return True
        three = r(3.0)
        cen = tuple(r(r(r(a[i] + b[i]) + c[i]) / three) for i in range(3))
        nx, nz = NRM[t][0], NRM[t][2]
        hl = r(_sqrt(r(r(nx * nx) + r(nz * nz))))
        if hl < C1E4:
            return False
        tx, tz = r(-nz / hl), r(nx / hl)
        nx = r(r(nx / hl) * SIDEPROBE)
        nz = r(r(nz / hl) * SIDEPROBE)
        ifc = [0] * (3 * BOTTOM_SAMPLES)
        for side in (-1, 1):
            band = False
            counted = resting = 0
            for e in range(3):
                p = a if e == 0 else (b if e == 1 else c)
                q = b if e == 0 else (c if e == 1 else a)
                o = c if e == 0 else (a if e == 1 else b)
                up_, uq, uo = r(r(p[0] * tx) + r(p[2] * tz)), r(r(q[0] * tx) + r(q[2] * tz)), r(r(o[0] * tx) + r(o[2] * tz))
                if abs(r(uq - up_)) < C002:
                    continue
                yl = r(p[1] + r(r(r(q[1] - p[1]) * r(uo - up_)) / r(uq - up_)))
                if o[1] <= r(yl + C00005):
                    continue
                for k in range(BOTTOM_SAMPLES):
                    f = r(r(k + 0.5) / BOTTOM_SAMPLES)
                    sx, sy, sz = r(p[0] + r(r(q[0] - p[0]) * f)), r(p[1] + r(r(q[1] - p[1]) * f)), r(p[2] + r(r(q[2] - p[2]) * f))
                    fc = ifc[e * BOTTOM_SAMPLES + k]
                    if fc == 0:
                        fc = 2 if in_face_below(t, sx, r(sy - FACEGAP), sz, tx, tz) else 1
                        ifc[e * BOTTOM_SAMPLES + k] = fc
                    if fc == 2:
                        wib_dep[0] = True
                        continue
                    sx = r(sx + r(r(cen[0] - sx) * C002b))
                    sz = r(sz + r(r(cen[2] - sz) * C002b))
                    fl = floor_below(r(sx + r(side * nx)), sy, r(sz + r(side * nz)))
                    counted += 1
                    if fl < -1e8:
                        if not ref_on:
                            resting += 1
                            continue
                        fl = REF
                    clear = r(sy - fl)
                    if clear <= HANGFREE:
                        resting += 1
                        continue
                    if clear >= MINCLEAR and clear < MAXHANG:
                        band = True
            if band and resting * 2 <= counted:
                return True
        return False

    seeds = 0
    # stage 5: wall pieces hanging from a ceiling (kind 2)
    for t in range(n):
        if K[t] != 0:
            continue
        m = NRM[t]
        if m is not None and abs(m[1]) >= 0.5:
            a, b, c = corners(t)
            if sliver_hangs(a, b, c):
                K[t] = 2
                seeds += 1
            continue
        if m is None:
            continue
        if wall_hangs(t):
            K[t] = 2
            seeds += 1
    # stage 8: tops of thin hanging slabs / slabs over crawl spaces (kind 3)
    for t in up:
        if K[t] != 0:
            continue
        if slab_top(t) or crawl_slab_top(t):
            K[t] = 3
    # stages 12 / 13: rule 4
    wall_cand = [t for t in range(n) if NRM[t] is not None and abs(NRM[t][1]) < 0.5]
    re = bytearray(n)
    for stage in (12, 13):
        for t in wall_cand:
            if K[t] != 0:
                continue
            if stage == 13 and re[t] == 0:
                continue
            if wall_in_head_band(t):
                K[t] = 2
                seeds += 1
            elif stage == 12 and wib_dep[0]:
                re[t] = 1
    # stage 6: coplanar runs of the same face (kind 3)
    if seeds > 0:
        zero = (0.0, 0.0, 0.0)
        for t in range(n):
            if K[t] != 2:
                continue
            miny = min(Y[tris[3 * t]], Y[tris[3 * t + 1]], Y[tris[3 * t + 2]])
            nt = NRM[t] or zero
            for d in (-1, 1):
                for s in range(1, 65):
                    u = t + d * s
                    if u < 0 or u >= n:
                        break
                    if K[u] == 1:
                        break
                    if K[u] == 2 or K[u] == 3:
                        continue
                    nu = NRM[u] or zero
                    dot = r(r(r(nu[0] * nt[0]) + r(nu[1] * nt[1])) + r(nu[2] * nt[2]))
                    if dot < C0999 or abs(r(PD[u] - PD[t])) > C002b:
                        break
                    umin = min(Y[tris[3 * u]], Y[tris[3 * u + 1]], Y[tris[3 * u + 2]])
                    if umin < r(miny - C003):
                        break
                    K[u] = 3
    for t in range(n):
        if K[t] != 0:
            K[t] = 1
    return K


# ───────────────────────── the CEIL pack section ─────────────────────────
# Layout (little endian int32 unless noted), docs/goldsrc_base_layer.md:
#   i0 version (SECTION_VERSION)  i1 R = MODL record count  i2 M = merged model count  i3 C = clip-only collision triangles
#   i4 W = triangles of the world collider (world solid + merged models + clip floors)  i5 ceiling triangles among them  i6, i7 reserved 0
#   i32 modelTris[R]  triangles of the collision list of record r that have kinds below (0 = none: record 0, merged, volume, > 3000)
#   i32 modelCeil[R]  ceiling triangles among them
#   i32 merged[M]     the records the loader merges into the world collider when no pool overflow happens (modelRoute 0)
#   uint8 kind[W]     1 = ceiling, 0 = floor / wall, per triangle of the world collider in the order GsvrPackLoader.BuildMesh builds it
#   uint8 kind[...]   for r = 1..R-1 with modelTris[r] > 0: modelTris[r] bytes = the kinds of its collision triangles
#   (padded to 4 bytes)
# A model's kinds assume the ground a ray down finds under the model at its record origin (GsvrEntSystem.DuckSplitModel).
SECTION_VERSION = 1
MAX_MODEL_TRIS = 3000          # GsvrPackLoader.ModelSplitMaxTris
F_VOLUME, F_MERGE, F_TRANSLUCENT = 4, 8, 32


def _f32s(vals):
    from array import array
    return array("f", vals).tolist()


def _f32(x):
    from array import array
    return array("f", [x])[0]


def merged_records(recs, rend):
    """Records ComputeRoutes routes to the world collider without a pool overflow (pass 0): static-mergeable, not visibly translucent."""
    out = []
    for r in range(1, len(recs)):
        rec = recs[r]
        fl = rec["flags"]
        if (rec["rc"] <= 0 and rec["cc"] <= 0) or fl & F_VOLUME:
            continue
        vis = bool(fl & F_TRANSLUCENT) and ((rend[r][0] >> 8) & 255) > 0
        if not vis and fl & F_MERGE:
            out.append(r)
    return out


def _ground_grid(X, Y, Z, tris, kinds):
    g = {}
    for t in range(len(tris) // 3):
        if kinds[t]:
            continue
        ia, ib, ic = tris[3 * t], tris[3 * t + 1], tris[3 * t + 2]
        ax, ay, az, bx, by, bz, cx, cy, cz = X[ia], Y[ia], Z[ia], X[ib], Y[ib], Z[ib], X[ic], Y[ic], Z[ic]
        ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)       # y of cross(b - a, c - a)
        if ny <= 1e-12:
            continue                                              # back face / vertical: a Unity raycast ignores it
        x0, x1 = int(math.floor(min(ax, bx, cx))), int(math.floor(max(ax, bx, cx)))
        z0, z1 = int(math.floor(min(az, bz, cz))), int(math.floor(max(az, bz, cz)))
        tri = (ax, ay, az, bx, by, bz, cx, cy, cz, ny)
        for i in range(x0, x1 + 1):
            for j in range(z0, z1 + 1):
                g.setdefault((i, j), []).append(tri)
    return g


def ground_below(grid, x, y, z, maxd=20.0):
    """Highest front-facing floor under (x, y, z) within maxd (the first hit of a ray down); None without."""
    best = None
    for ax, ay, az, bx, by, bz, cx, cy, cz, ny in grid.get((int(math.floor(x)), int(math.floor(z))), ()):
        # barycentric in xz (the triangle is up-facing: ny = twice its signed xz area, positive)
        w0 = (bz - z) * (cx - x) - (bx - x) * (cz - z)           # area weights of A, B, C: f(P, B, C), f(P, C, A), f(P, A, B)
        w1 = (cz - z) * (ax - x) - (cx - x) * (az - z)
        w2 = (az - z) * (bx - x) - (ax - x) * (bz - z)
        e = -1e-9 * ny
        if w0 < e or w1 < e or w2 < e:
            continue
        yy = (w0 * ay + w1 * by + w2 * cy) / ny
        if yy > y or yy < y - maxd:
            continue
        if best is None or yy > best:
            best = yy
    return best


def build_section(p, log=None):
    """p = the dict tools/pack.py builds (GSV3). -> (blob, count) or None when the pack has no collision at all."""
    v3 = p.get("v3")
    if not v3:
        return None
    recs = v3["recs"]
    rend = p.get("rend") or [(0, 0)] * len(recs)
    R = len(recs)
    inch = _f32(0.0254)
    pos = _f32s(p["pos"])
    idx = p["idx"]
    nv_all = len(pos) // 3
    world = recs[0]
    merged = merged_records(recs, rend)
    # BuildMesh: nv == _vertexCount && no clip floors -> the pack's vertex array is used as is (no origin added to merged models)
    clip = p.get("clip")
    clip_v = len(clip[0]) // 3 if clip else 0
    clip_i = clip[1] if clip else []
    wv = p.get("world_v", world["vc"])
    shortcut = wv + sum(recs[r]["vc"] for r in merged) == nv_all and clip_v == 0
    X, Y, Z = pos[0:3 * wv:3], pos[1:3 * wv:3], pos[2:3 * wv:3]
    tris = list(idx[:p["solid"]])
    vb = wv
    for r in merged:
        rec = recs[r]
        fv, vc, fi, cs, cc = rec["fv"], rec["vc"], rec["fi"], rec["cs"], rec["cc"]
        if shortcut:
            ox = oy = oz = 0.0
        else:
            o = [_f32(c) for c in rec["origin"]]
            ox, oy, oz = _f32(o[0] * inch), _f32(o[2] * inch), _f32(o[1] * inch)
        seg = pos[3 * fv:3 * (fv + vc)]
        X += _f32s([c + ox for c in seg[0::3]])
        Y += _f32s([c + oy for c in seg[1::3]])
        Z += _f32s([c + oz for c in seg[2::3]])
        tris += [i + vb for i in idx[fi + cs:fi + cs + cc]]
        vb += vc
    if clip_v:
        cp = _f32s(clip[0])
        X += cp[0::3]
        Y += cp[1::3]
        Z += cp[2::3]
        tris += [i + vb for i in clip_i]
    wk = classify(X, Y, Z, tris)
    nW = len(tris) // 3
    wkind = bytes(wk)
    grid = None
    mtris = [0] * R
    mceil = [0] * R
    mkinds = []
    done = set(merged)
    for r in range(1, R):
        rec = recs[r]
        fl = rec["flags"]
        cc = rec["cc"]
        if r in done or cc <= 0 or fl & F_VOLUME or cc // 3 > MAX_MODEL_TRIS:
            continue
        if grid is None:
            grid = _ground_grid(X, Y, Z, tris, wk)
        fv, vc, fi, cs = rec["fv"], rec["vc"], rec["fi"], rec["cs"]
        seg = pos[3 * fv:3 * (fv + vc)]
        o = [_f32(c) for c in rec["origin"]]
        mn, mx = [_f32(c) for c in rec["mins"]], [_f32(c) for c in rec["maxs"]]
        # GsvrEntSystem.DuckSplitModel: a ray down from the middle of the underside, 3 cm below it (Unity: x, z-up, y)
        lox, loy, loz = _r(o[0] + mn[0]), _r(o[1] + mn[1]), _r(o[2] + mn[2])
        hix, hiy = _r(o[0] + mx[0]), _r(o[1] + mx[1])
        half = _r(0.5)
        ux, uy, uz = _r(_r(_r(lox + hix) * half) * inch), _r(_r(loz * inch) + _r(-0.03)), _r(_r(_r(loy + hiy) * half) * inch)
        hit = ground_below(grid, ux, uy, uz)
        ref = 1e9 if hit is None else _r(_r(hit) - _r(o[2] * inch))
        k = classify(seg[0::3], seg[1::3], seg[2::3], list(idx[fi + cs:fi + cs + cc]), ref)
        mtris[r] = cc // 3
        mceil[r] = sum(k)
        mkinds.append(bytes(k))
    body = wkind + b"".join(mkinds)
    blob = struct.pack("<8i", SECTION_VERSION, R, len(merged), len(clip_i) // 3, nW, sum(wk), 0, 0)
    blob += struct.pack("<%di" % R, *mtris) + struct.pack("<%di" % R, *mceil) + struct.pack("<%di" % len(merged), *merged) + body
    blob += b"\0" * (-len(blob) % 4)
    if log:
        log(f"ceilings: {sum(wk)} of {nW} world collider triangles, {sum(1 for r in mtris if r)} brush models "
            f"({sum(sum(k) for k in map(bytearray, mkinds))} of {sum(mtris)} triangles), {len(blob)} bytes")
    return blob, nW + sum(mtris)
