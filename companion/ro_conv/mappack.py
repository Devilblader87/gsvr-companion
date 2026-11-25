"""RO map (RSW + GND + GAT + RSM) -> RPG1 zone pack (the container + sections the ROSE loader GsvrRpg1Loader already reads).

Coordinate decision (docs/design/ro_pack.md is the reference):
  RO file space : Y DOWN, GND tile (tx, ty) spans x tx*zoom..+zoom, z ty*zoom..+zoom (zoom = 10 units), GAT cell = zoom / 2 units,
                  +x = east, +z (= GAT y) = north. RSW model positions are relative to the map centre.
  pack / Unity  : X = x * s, Y = -y * s, Z = z * s with s = cell_m / (zoom / 2) metres per unit (default cell_m = 1.0: one GAT cell
                  = 1 m, one GND tile = 2 m, Prontera 312 x 392 cells = 312 m x 392 m). Origin = south-west map corner, AREA.world_off = 0.
                  Flipping one axis is a reflection: triangle winding is set explicitly (terrain clockwise from above = Unity front
                  face, walls written double sided, every object material is two sided).
  GAT cell (cx, cy) = pack grid cell index cy * grid_w + cx (CELL: 1 walkable / 0 blocked; HGHT: mean corner height, unit 0.01 m).
"""
import collections
import json
import math
import os
import struct
import sys

from . import rofmt, img
from .grf import norm

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROSE = os.path.join(os.path.dirname(_HERE), 'rose_conv')
if _ROSE not in sys.path:
    sys.path.insert(0, _ROSE)
import rpg1 as P          # noqa: E402  (shared RPG1 container + section codecs, never edited here)
import rosedds as D       # noqa: E402

TOOL_VERSION = 4               # 4: GAT walk surface (GWLK) + interior shells; 3: ground lightmap atlas (TLMP) instead of subdivided tiles, DXT1 cut-outs, deduped OTEX; 2: subdivided ground (rebuilds old packs)
ZONE_ID = 900               # RO zones live at 900+ so they never collide with ROSE zone ids (1..200)
SLOT_PX, GUTTER, ATLAS_MIPS = 320, 32, 4
POOL_TERRAIN, POOL_CHUNKS = 80, 800          # GsvrSceneBuilder.Rose RoseTerrainPool / RoseObjectPool
MAX_VERTS = 65535
TAG_TLMP = b'TLMP'                           # ground lightmap atlas (RO only, docs/design/ro_pack.md; GsvrRpg1Pack.ValidateTlmp)
TLMP_HDR = struct.Struct('<8I')             # format, width, height, mips, slots, slot_px, columns, reserved
LM_SLOT, LM_ATLAS_W, LM_MIPS = 8, 1024, 2   # 8x8 GND lightmap per slot (6x6 interior + 1 px border); mip 1 = 4x4 per slot (still inside the slot)
# GWLK (RO only, docs/design/ro_pack.md "Walk surface"): the player's walk surface from the GAT where the GND ground is not it (bridges, stairs and ramps
# into buildings are RSM models without colliders: the GAT carries their height, the GND lies below), + the interior flag of the map.
TAG_GWLK = b'GWLK'
GWLK_VERSION = 1
GWLK_HDR = struct.Struct('<8I')             # version, flags, pieces, raised, cell_mm, wall_cm, reserved, reserved
GWLK_PIECE = struct.Struct('<2I')           # vertex count, triangle count; then vertices (u16 corner x, u16 corner z, i16 y cm), padded to 4, then u16 indices, padded to 4
GWLK_RAISED = struct.Struct('<2H2h')        # cell x, cell y, GAT height cm (corner mean), GND height cm at the cell centre
GW_INTERIOR = 1                             # flags bit 0: interior map (rooms over a void: black sky + shells)
WALK_RAISE_M = 0.1                          # a walkable cell whose GAT corner lies this far above the GND (or that has no GND top) gets the GAT surface
WALK_WALL_M = 1.5                           # walls of the GAT surface toward non-walkable cells (no stepping off a bridge)
RAISED_LIST_M = 0.5                         # GAT >= GND + this at the cell centre: listed in GWLK (offline rules, the autotest's bridge leg)
MAX_RAISED = 4096
WALK_PIECE_CELLS = 64                       # one collider piece per 64 x 64 cells (<= 65 x 65 corners + walls: far below 65535 vertices)
SHELL_M = 6.0                               # interior shells: height of the dark wall over a room edge toward the void
SHELL_GAP_M = 0.3                           # interior shells: an open GND edge between two tops this far apart in height is closed
SHELL_OFF_M = 1.0                           # interior shells stand this far OUTSIDE the room edge (RO interiors often have RSM wall models standing just outside the
                                            # GND floor edge: a shell on the edge itself would hide them; a GND tile is 2 m, so 1 m stays inside the void / wall-top tile)
SHELL_GAP_OFF_M = 0.5                       # a gap fill stands this far inside the higher top (its wall-top ring is >= 1 tile = 2 m)
INTERIOR_VOID = 0.5                         # a map with at least this fraction of GND tiles without a top is an interior (prt_in 0.74, prt_church 0.87, towns / fields ~0)


class BuildError(Exception):
    pass


# ------------------------------------------------------------------ small math
def _mm(a, b):
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]


def _ident():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _T(x, y, z):
    return [1.0, 0, 0, x, 0, 1.0, 0, y, 0, 0, 1.0, z, 0, 0, 0, 1.0]


def _S(x, y, z):
    return [x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1.0]


def _axis_angle(ang, ax):
    x, y, z = ax
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-9 or abs(ang) < 1e-12:
        return _ident()
    x /= n; y /= n; z /= n
    c, s = math.cos(ang), math.sin(ang); t = 1 - c
    return [t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0,
            t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0,
            t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0,
            0, 0, 0, 1.0]


def _quat(qx, qy, qz, qw):
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw) or 1.0
    x, y, z, w = qx / n, qy / n, qz / n, qw / n
    return [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0,
            2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0,
            2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0,
            0, 0, 0, 1.0]


def _rx(deg):
    return _axis_angle(math.radians(deg), (1, 0, 0))


def _ry(deg):
    return _axis_angle(math.radians(deg), (0, 1, 0))


def _rz(deg):
    return _axis_angle(math.radians(deg), (0, 0, 1))


def _ap(m, v):
    x, y, z = v
    return (m[0] * x + m[1] * y + m[2] * z + m[3], m[4] * x + m[5] * y + m[6] * z + m[7], m[8] * x + m[9] * y + m[10] * z + m[11])


def _mat3(m):
    """RSM 3x3 (9 floats, column after column) as a 4x4: x' = m0*x + m3*y + m6*z, ..."""
    return [m[0], m[3], m[6], 0, m[1], m[4], m[7], 0, m[2], m[5], m[8], 0, 0, 0, 0, 1.0]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _nrm(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 1.0, 0.0)


def _q8(v):
    return max(-127, min(127, int(round(v * 127))))


# ------------------------------------------------------------------ RSM -> centred model space geometry
def model_parts(m):
    """RSM dict -> {model texture index: (verts [(x, y, z, u, v)], tris [(a, b, c)])} in RO model space (Y down), centred like the
    client: x / z on the bounding box centre, the lowest point (max y) at y = 0. The first rotation key frame replaces the static
    rotation (we build static meshes). Vertices are shared per smoothing group (shade 2) or not at all (shade 0 / 1: flat)."""
    nodes = m['nodes']
    if not nodes:
        return {}
    names = {n['name']: i for i, n in enumerate(nodes)}
    root = names.get(m['main'])
    kids = collections.defaultdict(list)
    roots = []
    for i, n in enumerate(nodes):
        p = names.get(n['parent'])
        if p is None or p == i or n['name'] == m['main']:
            roots.append(i)
        else:
            kids[p].append(i)
    if root is None:
        root = roots[0]
    only = len(nodes) == 1
    world = {}

    def local(i, is_root):
        n = nodes[i]
        mt = _ident() if is_root else _T(*n['pos'])
        if n['rotkeys']:
            mt = _mm(mt, _quat(*n['rotkeys'][0][1:5]))
        else:
            mt = _mm(mt, _axis_angle(n['rotangle'], n['rotaxis']))
        return _mm(mt, _S(*n['scale']))

    stack = [(r, _ident(), True) for r in roots]
    seen = set()
    while stack:
        i, parent, is_root = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        m1 = _mm(parent, local(i, is_root))
        n = nodes[i]
        m2 = _mat3(n['mat3']) if only else _mm(_T(*n['offset']), _mat3(n['mat3']))
        world[i] = (m1, _mm(m1, m2))
        for k in kids.get(i, ()):
            stack.append((k, m1, False))
    parts = {}
    lo = [1e30] * 3; hi = [-1e30] * 3
    for i, n in enumerate(nodes):
        if i not in world:
            continue
        vm = world[i][1]
        pv = [_ap(vm, v) for v in n['verts']]
        for p in pv:
            for k in range(3):
                if p[k] < lo[k]: lo[k] = p[k]
                if p[k] > hi[k]: hi[k] = p[k]
        keymap = {}
        smooth = m.get('shade', 2) == 2
        for fi, f in enumerate(n['faces']):
            v0, v1, v2, t0, t1, t2, ts, _two = f[:8]
            # vertex sharing decides the normals build() accumulates: smooth models share a vertex only inside one smoothing group
            # (hard edges between groups, like the client), flat / unshaded models get one vertex set per face (face normals)
            grp = (f[8] if len(f) > 8 else 0) if smooth else ('f', fi)
            if max(v0, v1, v2) >= len(pv) or max(t0, t1, t2) >= len(n['tverts']) or ts >= len(n['tex']):
                continue
            tex = n['tex'][ts]
            if tex < 0 or tex >= len(m['textures']):
                continue
            verts, tris = parts.setdefault(tex, ([], []))
            idx = []
            for vv, tt in ((v0, t0), (v1, t1), (v2, t2)):
                k = (tex, vv, tt, grp)
                j = keymap.get(k)
                if j is None:
                    j = len(verts); keymap[k] = j
                    u, v = n['tverts'][tt]
                    verts.append((pv[vv][0], pv[vv][1], pv[vv][2], u, v))
                idx.append(j)
            tris.append(tuple(idx))
    if lo[0] > hi[0]:
        return {}
    cx, cz, my = (lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2, hi[1]
    out = {}
    for t, (vs, ts) in parts.items():
        out[t] = ([(x - cx, y - my, z - cz, u, v) for x, y, z, u, v in vs], ts)
    return out


# ------------------------------------------------------------------ textures
def _q5(v):
    return max(0, min(31, int(v * 31 / 255.0 + 0.5)))


def _q6(v):
    return max(0, min(63, int(v * 63 / 255.0 + 0.5)))


def _enc_color_round(px, three=False, transparent=()):
    """16 (r, g, b) -> DXT1 colour block with ROUNDED 565 endpoints (rosedds._enc_color truncates: dark lightmap colours of 1..7 became 0).
    three = 3-colour mode (c0 <= c1, index 3 = transparent black: DXT1 punch-through alpha); transparent = texel indices that get index 3."""
    op = [p for i, p in enumerate(px) if i not in transparent] or [(0, 0, 0)]
    hi = tuple(max(p[k] for p in op) for k in range(3)); lo = tuple(min(p[k] for p in op) for k in range(3))
    c0 = (_q5(hi[0]) << 11) | (_q6(hi[1]) << 5) | _q5(hi[2]); c1 = (_q5(lo[0]) << 11) | (_q6(lo[1]) << 5) | _q5(lo[2])
    if three:
        if c0 > c1:
            c0, c1 = c1, c0
        p0, p1 = D._rgb565(c0), D._rgb565(c1)
        pal = [p0, p1, tuple((a + b) // 2 for a, b in zip(p0, p1))]
    else:
        if c0 == c1:
            return struct.pack('<HHI', c0, c1, 0)
        if c0 < c1:
            c0, c1 = c1, c0
        p0, p1 = D._rgb565(c0), D._rgb565(c1)
        pal = [p0, p1, tuple((2 * a + b) // 3 for a, b in zip(p0, p1)), tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))]
    bits = 0
    for i, p in enumerate(px):
        if i in transparent:
            best = 3
        else:
            best = min(range(len(pal)), key=lambda j: (pal[j][0] - p[0]) ** 2 + (pal[j][1] - p[1]) ** 2 + (pal[j][2] - p[2]) ** 2)
        bits |= best << (2 * i)
    return struct.pack('<HHI', c0, c1, bits)


def _enc_blocks(rgba, w, h, alpha, cutout=False):
    """DXT1 / DXT5 with a per-block cache (RO textures have many flat / repeated blocks; the encoder is pure Python).
    cutout = DXT1 with punch-through alpha (alpha < 128 -> index 3 of the 3-colour mode = transparent): half the bytes of DXT5 for the
    magenta-keyed textures, whose alpha is only 0 / 255 (the object shader alpha-tests at 0.5 either way)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    out = bytearray(); cache = {}
    for by in range(bh):
        for bx in range(bw):
            blk = bytearray()
            for py in range(4):
                y = min(by * 4 + py, h - 1)
                for px in range(4):
                    x = min(bx * 4 + px, w - 1)
                    o = (y * w + x) * 4
                    blk += rgba[o:o + 4]
            key = bytes(blk)
            e = cache.get(key)
            if e is None:
                px = [(key[i], key[i + 1], key[i + 2]) for i in range(0, 64, 4)]
                if cutout:
                    tr = frozenset(i // 4 for i in range(0, 64, 4) if key[i + 3] < 128)
                    e = _enc_color_round(px, True, tr) if tr else D._enc_color(px)
                    cache[key] = e
                    out += e
                    continue
                e = D._enc_color(px)
                if alpha:
                    e = D._enc_alpha([key[i + 3] for i in range(0, 64, 4)]) + e
                cache[key] = e
            out += e
    return bytes(out)


def _mips(rgba, w, h):
    lv = [(rgba, w, h)]
    while w > 1 or h > 1:
        rgba, w, h = D.downsample2(rgba, w, h)
        lv.append((rgba, w, h))
    return lv


def load_texture(vfs, rel, warn, cap=256, magenta=True):
    """data\\texture\\<rel> -> (w, h, rgba, has_alpha), power-of-two, at most cap. Missing / unsupported -> grey placeholder + warning."""
    try:
        data = vfs.read('data\\texture\\' + rel)
        w, h, px = img.decode(data, rel)
    except KeyError:
        warn['texture missing'] += 1
        w, h, px = img.placeholder()
    except img.ImageError as e:
        warn['texture %s' % e] += 1
        w, h, px = img.placeholder()
    alpha = False
    if magenta:
        px, alpha = img.key_magenta(px, w, h)
    nw, nh = img.pow2_at_most(w, cap), img.pow2_at_most(h, cap)
    if (nw, nh) != (w, h):
        px = img.resize(px, w, h, nw, nh)
        w, h = nw, nh
    return w, h, px, alpha


def binary_alpha(px):
    """True when every alpha byte is 0 or 255 (magenta key, 1-bit TGA): DXT1 punch-through keeps it exactly."""
    a = px[3::4]
    return a.count(0) + a.count(255) == len(a)


def tex_key(w, h, px, alpha):
    """content key for the OTEX dedupe (different paths, same image -> one texture)."""
    import hashlib
    return (w, h, alpha, hashlib.sha1(px).digest())


def build_lightmap_atlas(g, lms):
    """GND lightmaps -> the TLMP atlas: slot 0 = lit (shadow 255, no colour; tiles without a lightmap), slot 1.. = the lightmaps in `lms`
    (deduped by content). One 8x8 slot per lightmap incl. its 1 px border (the border repeats the neighbour, so bilinear filtering stays
    inside the slot), RGB = lightmap colour, A = shadow intensity. DXT5 (alpha = 8-level block interpolation: hard shadow edges stay),
    2 mips (mip 1 = 4x4 per slot = one DXT block). -> dict(fmt, w, h, levels, slots, cols, slot_of={lm: slot})."""
    L = g['lightmaps']; cw, ch = L['cw'], L['ch']; sz = cw * ch * 4; d = L['data']
    texels = [bytes([0, 0, 0, 255]) * (LM_SLOT * LM_SLOT)]
    seen = {}; slot_of = {}
    for lm in lms:
        raw = d[lm * sz:(lm + 1) * sz]
        s_ = seen.get(raw)
        if s_ is None:
            px = bytearray(LM_SLOT * LM_SLOT * 4)
            for y in range(LM_SLOT):
                for x in range(LM_SLOT):
                    sh, lc = rofmt.lightmap_px(g, lm, x * cw // LM_SLOT, y * ch // LM_SLOT)
                    o = (y * LM_SLOT + x) * 4
                    px[o:o + 4] = bytes((lc[0], lc[1], lc[2], sh))
            s_ = seen[raw] = len(texels)
            texels.append(bytes(px))
        slot_of[lm] = s_
    n = len(texels)
    W = LM_ATLAS_W
    while W > LM_SLOT and (W // 2) // LM_SLOT >= n:
        W //= 2
    cols = W // LM_SLOT
    rows = (n + cols - 1) // cols
    H = rows * LM_SLOT
    if H > 4096:
        raise BuildError('%d lightmaps do not fit one %dx4096 lightmap atlas' % (n, W))
    rowsb = [bytearray(W * 4) for _ in range(H)]
    for si, px in enumerate(texels):
        col, row = si % cols, si // cols
        for yy in range(LM_SLOT):
            rowsb[row * LM_SLOT + yy][col * LM_SLOT * 4:(col + 1) * LM_SLOT * 4] = px[yy * LM_SLOT * 4:(yy + 1) * LM_SLOT * 4]
    rgba = b''.join(bytes(r) for r in rowsb)
    levels = []; lw, lh = W, H
    for L_ in range(LM_MIPS):
        bw, bh = (lw + 3) // 4, (lh + 3) // 4
        out = bytearray(); cache = {}
        for by in range(bh):
            for bx in range(bw):
                blk = bytearray()
                for py in range(4):
                    y = min(by * 4 + py, lh - 1)
                    o = (y * lw + bx * 4) * 4
                    blk += rgba[o:o + 16]
                key = bytes(blk)
                e = cache.get(key)
                if e is None:
                    e = D._enc_alpha([key[i + 3] for i in range(0, 64, 4)]) + _enc_color_round([(key[i], key[i + 1], key[i + 2]) for i in range(0, 64, 4)])
                    cache[key] = e
                out += e
        levels.append(bytes(out))
        if L_ + 1 < LM_MIPS:
            rgba, lw, lh = D.downsample2(rgba, lw, lh)
    return dict(fmt=D.FMT_DXT5, w=W, h=H, levels=levels, slots=n, cols=cols, slot_of=slot_of)


def lm_uv(atlas, slot, x, y):
    """atlas UV of lightmap pixel coordinates (x, y) (texel centres at k + 0.5, the tile interior spans 1..7) in `slot`."""
    col, row = slot % atlas['cols'], slot // atlas['cols']
    return ((col * LM_SLOT + x) / float(atlas['w']), (row * LM_SLOT + y) / float(atlas['h']))


def enc_tlmp(atlas):
    return TLMP_HDR.pack(atlas['fmt'], atlas['w'], atlas['h'], len(atlas['levels']), atlas['slots'], LM_SLOT, atlas['cols'], 0) + b''.join(atlas['levels'])


def dec_tlmp(d):
    fmt, w, h, mips, slots, spx, cols, _r = TLMP_HDR.unpack_from(d, 0)
    levels = []; pos = TLMP_HDR.size; lw, lh = w, h
    for _ in range(mips):
        n = D.level_size(fmt, lw, lh)
        levels.append(bytes(d[pos:pos + n])); pos += n
        lw = max(1, lw // 2); lh = max(1, lh // 2)
    if pos > len(d):
        raise ValueError('TLMP truncated')
    return dict(fmt=fmt, w=w, h=h, mips=mips, slots=slots, slot_px=spx, cols=cols, levels=levels)


def slot_rect(slot, spx, gut, aw, ah, inset=1.0):
    """GsvrRpg1Pack.SlotRect (same float formula; ValidateTatl compares the stored rectangle with it)."""
    cols = aw // spx
    col, row = slot % cols, slot // cols
    content = spx - 2 * gut
    x0 = col * spx + gut + inset; y0 = row * spx + gut + inset
    return (x0 / aw, y0 / ah, (col * spx + gut + content - inset) / aw, (row * spx + gut + content - inset) / ah)


def build_atlas(images, mips=ATLAS_MIPS, spx=SLOT_PX, gut=GUTTER):
    """images: list of 256x256 RGBA (index = slot) -> dict(fmt, w, h, levels, slots=[rect]). DXT1, gutters = replicated edges."""
    n = max(1, len(images))
    cols = max(1, int(math.ceil(math.sqrt(n))))
    rows = (n + cols - 1) // cols
    W, H = cols * spx, rows * spx
    if W > 8192 or H > 8192:
        raise BuildError('%d ground textures do not fit one 8192 px atlas' % n)
    pyr = [_mips(im, 256, 256) for im in images]
    levels = []
    for L in range(mips):
        sp, g, c = spx >> L, gut >> L, 256 >> L
        aw, ah = W >> L, H >> L
        rowsb = [bytearray(aw * 4) for _ in range(ah)]
        for si, p in enumerate(pyr):
            px = p[L][0]
            col, row = si % cols, si // cols
            for yy in range(sp):
                sy = min(c - 1, max(0, yy - g))
                src = px[sy * c * 4:(sy + 1) * c * 4]
                line = src[:4] * g + src + src[-4:] * g
                dst = rowsb[row * sp + yy]
                dst[col * sp * 4:(col + 1) * sp * 4] = line
        rgba = b''.join(bytes(r) for r in rowsb)
        levels.append(_enc_blocks(rgba, aw, ah, False))
    slots = [slot_rect(i, spx, gut, W, H) for i in range(len(images))]
    return dict(fmt=D.FMT_DXT1, w=W, h=H, levels=levels, slots=slots, slot_px=spx, gutter=gut, mips=mips)


# ------------------------------------------------------------------ the map
def _light_dir(light):
    lon, lat = math.radians(light['longitude']), math.radians(light['latitude'])
    return _nrm((math.cos(lat) * math.sin(lon), math.sin(lat), math.cos(lat) * math.cos(lon)))     # towards the sun, Unity axes


OBJ_SHADER_LIGHT = 0.73        # mean brightness of the loader's object shader: ambient 0.55 + 0.45 x mean lambert 0.4 (RoseObject, GsvrRpg1Loader.ambient)


def object_light_scale(light):
    """Dungeon lighting (docs/design/ro_culvert.md): objects are NOT lit by the RSW light (the loader's object shader has one fixed light, ~0.73 mean), while the ground is
    (vertex colours + lightmaps). A DARK map (Prontera Culvert B1: ambient 0.2 / diffuse 0.4) would show bright iron frames over dark ground. The client lights an RSM with
    ambient + diffuse x lambert: the mean of that (lambert 0.4) relative to the shader's mean is the scale of the object textures, clamped to 0.25 .. 1 (a map that is
    brighter than the shader keeps its textures: towns and the pre-existing packs are byte-identical)."""
    a, d = light['ambient'], light['diffuse']
    mean = sum(a[i] + 0.4 * d[i] for i in range(3)) / 3.0
    return max(0.25, min(1.0, mean / OBJ_SHADER_LIGHT))


def scale_rgb(px, f):
    """RGBA bytes with RGB x f (alpha kept); f >= 1 returns px itself."""
    if f >= 0.999:
        return px
    tbl = bytes(min(255, int(round(i * f))) for i in range(256))
    out = bytearray(px.translate(tbl))
    out[3::4] = px[3::4]
    return bytes(out)


def _shade(n, light, L):
    k = max(0.0, n[0] * L[0] + n[1] * L[1] + n[2] * L[2])
    a, d = light['ambient'], light['diffuse']
    return [min(1.5, a[i] + d[i] * k) for i in range(3)]


# ------------------------------------------------------------------ GAT walk surface (GWLK) + interior shells
def _gnd_top(g, tx, ty):
    """the GND cube of tile (tx, ty) when it has a top surface, else None (void / outside)."""
    if 0 <= tx < g['w'] and 0 <= ty < g['h']:
        c = g['cubes'][ty * g['w'] + tx]
        if 0 <= c[4] < len(g['surfaces']):
            return c
    return None


def _bil(c, fx, fz):
    """GND tile height (RO units, Y down) at the tile fraction (fx, fz) of cube c (corners h1 (0,0), h2 (1,0), h3 (0,1), h4 (1,1))."""
    return c[0] * (1 - fx) * (1 - fz) + c[1] * fx * (1 - fz) + c[2] * (1 - fx) * fz + c[3] * fx * fz


def _cm(y):
    return max(-32767, min(32767, int(round(y * 100.0))))


def interior_of(g):
    """-> (interior, void fraction): RO interiors (prt_in, prt_church, every *_in) are rooms over a void, GND tiles without a top between them."""
    W, H = g['w'], g['h']
    void = sum(1 for ty in range(H) for tx in range(W) if _gnd_top(g, tx, ty) is None)
    f = void / float(max(1, W * H))
    return f >= INTERIOR_VOID, f


def gnd_cell_y(g, s, cx, cy, fx=0.5, fz=0.5):
    """GND ground height (metres, Unity Y up) inside GAT cell (cx, cy) at the cell fraction (fx, fz); None without a top (void)."""
    c = _gnd_top(g, cx // 2, cy // 2)
    if c is None:
        return None
    return -_bil(c, ((cx & 1) + fx) / 2.0, ((cy & 1) + fz) / 2.0) * s


def walk_surface(t, g, s, cell_m=1.0):
    """The player's walk surface from the GAT (RO walks on the GAT, not on the GND or the RSM models: bridges, stairs and ramps into buildings are
    models without colliders whose height only the GAT carries). Cells: every walkable cell whose GAT corner lies more than WALK_RAISE_M above the
    GND ground of its tile (or that has no GND top at all: walkable over the void), plus their walkable 4-neighbours (the surface starts flush with
    the ground). Corners are welded (mean of the GAT corners of the surface cells sharing them: one continuous surface, steps become ramps).
    Every edge toward a non-walkable cell gets a WALK_WALL_M wall (no stepping off a bridge). The GND colliders stay: elsewhere GAT == GND.
    -> dict(pieces=[(verts [(corner x, corner z, y cm)], tris [(a, b, c)])], raised=[(cx, cy, gat cm, gnd cm)], cells, raised_cells, walls)."""
    gw, gh = t['w'], t['h']
    hs = t['heights']
    walk = bytearray(1 if ty_ in rofmt.GAT_WALKABLE else 0 for ty_ in t['types'])

    def wk(x, y):
        return 0 <= x < gw and 0 <= y < gh and walk[y * gw + x] == 1

    R = bytearray(gw * gh)
    lst = []
    for cy in range(gh):
        for cx in range(gw):
            i = cy * gw + cx
            if not walk[i]:
                continue
            gy = [-h * s for h in hs[i]]
            c = _gnd_top(g, cx // 2, cy // 2)
            if c is None:
                R[i] = 1
                continue
            up = 0.0
            for k in range(4):
                gn = -_bil(c, ((cx & 1) + (k & 1)) / 2.0, ((cy & 1) + (k >> 1)) / 2.0) * s
                up = max(up, gy[k] - gn)
            if up > WALK_RAISE_M:
                R[i] = 1
            gm = sum(gy) / 4.0
            gc = -_bil(c, ((cx & 1) + 0.5) / 2.0, ((cy & 1) + 0.5) / 2.0) * s
            if gm - gc >= RAISED_LIST_M and len(lst) < MAX_RAISED:
                lst.append((cx, cy, _cm(gm), _cm(gc)))
    raised_n = sum(R)
    S_ = bytearray(R)
    for cy in range(gh):
        for cx in range(gw):
            if R[cy * gw + cx]:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if wk(cx + dx, cy + dy):
                        S_[(cy + dy) * gw + cx + dx] = 1
    acc = {}
    for cy in range(gh):
        for cx in range(gw):
            i = cy * gw + cx
            if not S_[i]:
                continue
            for k in range(4):
                key = (cx + (k & 1), cy + (k >> 1))
                a = acc.get(key)
                y = -hs[i][k] * s
                if a is None:
                    acc[key] = [y, 1]
                else:
                    a[0] += y; a[1] += 1
    yc = {k: v[0] / v[1] for k, v in acc.items()}
    pieces = {}
    walls = 0
    wall_cm = _cm(WALK_WALL_M)
    for cy in range(gh):
        for cx in range(gw):
            if not S_[cy * gw + cx]:
                continue
            pk = (cx // WALK_PIECE_CELLS, cy // WALK_PIECE_CELLS)
            pc = pieces.get(pk)
            if pc is None:
                pc = pieces[pk] = ({}, [], [])
            vidx, verts, tris = pc

            def V(x, z, up=0):
                key = (x, z, up)
                j = vidx.get(key)
                if j is None:
                    j = vidx[key] = len(verts)
                    verts.append((x, z, _cm(yc[(x, z)]) + (wall_cm if up else 0)))
                return j
            q = [V(cx, cy), V(cx + 1, cy), V(cx, cy + 1), V(cx + 1, cy + 1)]
            tris.append((q[2], q[3], q[1])); tris.append((q[2], q[1], q[0]))          # clockwise from above = up-facing in Unity (like the terrain tops)
            for dx, dy, e0, e1 in ((1, 0, (cx + 1, cy), (cx + 1, cy + 1)), (-1, 0, (cx, cy), (cx, cy + 1)),
                                   (0, 1, (cx, cy + 1), (cx + 1, cy + 1)), (0, -1, (cx, cy), (cx + 1, cy))):
                if wk(cx + dx, cy + dy):
                    continue
                a0, a1, b0, b1 = V(*e0), V(*e1), V(e0[0], e0[1], 1), V(e1[0], e1[1], 1)
                tris.append((a0, a1, b1)); tris.append((a0, b1, b0))          # double sided: both windings
                tris.append((a0, b1, a1)); tris.append((a0, b0, b1))
                walls += 1
    out = [(pc[1], pc[2]) for _k, pc in sorted(pieces.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    return dict(pieces=out, raised=lst, cells=sum(S_), raised_cells=raised_n, walls=walls, cell_mm=int(round(cell_m * 1000)), wall_cm=wall_cm)


def enc_gwlk(ws, flags):
    out = bytearray(GWLK_HDR.pack(GWLK_VERSION, flags, len(ws['pieces']), len(ws['raised']), ws['cell_mm'], ws['wall_cm'], 0, 0))
    for verts, tris in ws['pieces']:
        if len(verts) > MAX_VERTS:
            raise BuildError('walk surface piece has %d vertices (> 65535)' % len(verts))
        out += GWLK_PIECE.pack(len(verts), len(tris))
        for x, z, y in verts:
            out += struct.pack('<HHh', x, z, y)
        out += b'\0' * (-len(out) % 4)
        for tri in tris:
            out += struct.pack('<3H', *tri)
        out += b'\0' * (-len(out) % 4)
    for r in ws['raised']:
        out += GWLK_RAISED.pack(*r)
    return bytes(out)


def dec_gwlk(d):
    """-> dict(version, flags, cell_mm, wall_cm, pieces=[(verts, tris)], raised=[...]) (tests / tools; the world reads it in GsvrRoWalk)."""
    ver, flags, npc, nr, cell_mm, wall_cm, _a, _b = GWLK_HDR.unpack_from(d, 0)
    p = GWLK_HDR.size
    pieces = []
    for _ in range(npc):
        nv, nt = GWLK_PIECE.unpack_from(d, p); p += GWLK_PIECE.size
        verts = [struct.unpack_from('<HHh', d, p + 6 * i) for i in range(nv)]
        p += 6 * nv; p += -p % 4
        tris = [struct.unpack_from('<3H', d, p + 6 * i) for i in range(nt)]
        p += 6 * nt; p += -p % 4
        pieces.append((verts, tris))
    raised = [GWLK_RAISED.unpack_from(d, p + GWLK_RAISED.size * i) for i in range(nr)]
    if p + GWLK_RAISED.size * nr > len(d):
        raise ValueError('GWLK truncated')
    return dict(version=ver, flags=flags, cell_mm=cell_mm, wall_cm=wall_cm, pieces=pieces, raised=raised)


def walk_height(ws, cell_m, x, z):
    """Height of the walk surface (floor triangles only, walls skipped) under the pack point (x, z), None if no surface there (tests / tools)."""
    best = None
    for verts, tris in ws['pieces']:
        for a, b, c in tris:
            pa, pb, pc = verts[a], verts[b], verts[c]
            if not ((pa[0], pa[1]) != (pb[0], pb[1]) and (pa[0], pa[1]) != (pc[0], pc[1]) and (pb[0], pb[1]) != (pc[0], pc[1])):
                continue          # a wall triangle (two corners share x / z)
            ax, az, bx, bz, cx_, cz = pa[0] * cell_m, pa[1] * cell_m, pb[0] * cell_m, pb[1] * cell_m, pc[0] * cell_m, pc[1] * cell_m
            den = (bz - cz) * (ax - cx_) + (cx_ - bx) * (az - cz)
            if abs(den) < 1e-12:
                continue
            l1 = ((bz - cz) * (x - cx_) + (cx_ - bx) * (z - cz)) / den
            l2 = ((cz - az) * (x - cx_) + (ax - cx_) * (z - cz)) / den
            l3 = 1.0 - l1 - l2
            if l1 < -1e-6 or l2 < -1e-6 or l3 < -1e-6:
                continue
            y = (l1 * pa[2] + l2 * pb[2] + l3 * pc[2]) / 100.0
            if best is None or y > best:
                best = y
    return best


def shell_quads(g, t, s):
    """Interior maps: RO models the room walls for its top-down camera only (the GND side surfaces exist on the north / east walls the camera sees;
    south / west walls and every room edge toward the void are open). At eye height in VR the neighbouring rooms floating in the void show through.
    -> quads [(tile x, tile y, [a0, a1, b0, b1] metres, kind)] that close them, per GAT cell edge (1 m):
      - an OPEN GND edge (no side surface) between two tops more than SHELL_GAP_M apart: the missing wall between both heights;
      - a top's edge toward the void: a SHELL_M wall up from the edge.
    An edge between two tops whose GAT cells on both sides are walkable stays open (stairs); an edge toward a walkable GAT cell over the void (a door
    or warp corridor) gets kind 'door' (the world draws it as a black curtain without a collider: the player walks through into the warp), every
    other quad kind 'wall' (terrain: drawn + collider). a0 / a1 = this tile's edge, b0 / b1 = the other end (like the terrain walls)."""
    W, H = g['w'], g['h']
    gw, gh = t['w'], t['h']
    walk = bytearray(1 if ty_ in rofmt.GAT_WALKABLE else 0 for ty_ in t['types'])
    ns = len(g['surfaces'])
    zoom = g['zoom'] if g['zoom'] > 0 else 10.0
    tm = zoom * s          # tile size in metres

    def wk(x, y):
        return 0 <= x < gw and 0 <= y < gh and walk[y * gw + x] == 1
    out = []
    for ty in range(H):
        for tx in range(W):
            c = _gnd_top(g, tx, ty)
            if c is None:
                continue
            # (neighbour, this tile's edge heights h0 h1 (RO units), the neighbour's edge heights, edge start (x, z) tiles, edge direction, existing side surface,
            #  inside cells, beyond cells)
            edges = (
                ((tx + 1, ty), (c[1], c[3]), (0, 2), (tx + 1, ty), (0, 1), c[6], [(2 * tx + 1, 2 * ty), (2 * tx + 1, 2 * ty + 1)], [(2 * tx + 2, 2 * ty), (2 * tx + 2, 2 * ty + 1)], (1, 0)),
                ((tx, ty + 1), (c[2], c[3]), (0, 1), (tx, ty + 1), (1, 0), c[5], [(2 * tx, 2 * ty + 1), (2 * tx + 1, 2 * ty + 1)], [(2 * tx, 2 * ty + 2), (2 * tx + 1, 2 * ty + 2)], (0, 1)),
                ((tx - 1, ty), (c[0], c[2]), (1, 3), (tx, ty), (0, 1), None, [(2 * tx, 2 * ty), (2 * tx, 2 * ty + 1)], [(2 * tx - 1, 2 * ty), (2 * tx - 1, 2 * ty + 1)], (-1, 0)),
                ((tx, ty - 1), (c[0], c[1]), (2, 3), (tx, ty), (1, 0), None, [(2 * tx, 2 * ty), (2 * tx + 1, 2 * ty)], [(2 * tx, 2 * ty - 1), (2 * tx + 1, 2 * ty - 1)], (0, -1)),
            )
            for k, (nb, hh, ni, e0, ed, side, ins, bey, ow) in enumerate(edges):
                n = _gnd_top(g, nb[0], nb[1])
                if n is not None and k >= 2:
                    continue          # an edge between two tops: handled once, from the tile west / south of it
                if n is not None:
                    if 0 <= side < ns:
                        continue          # the GND has the wall
                    nh = (n[ni[0]], n[ni[1]])
                    if max(abs(hh[0] - nh[0]), abs(hh[1] - nh[1])) * s <= SHELL_GAP_M:
                        continue
                # the toward-the-void shell stands SHELL_OFF_M outside the edge (behind RSM wall models at the floor edge); a gap fill stands SHELL_GAP_OFF_M
                # inside the HIGHER of the two tops (the wall-top ring), behind a model wall standing on the lower floor's edge
                if n is None:
                    off = SHELL_OFF_M
                else:
                    off = SHELL_GAP_OFF_M if (nh[0] + nh[1]) < (hh[0] + hh[1]) else -SHELL_GAP_OFF_M          # Y down: the smaller value is the higher top
                for half in range(2):
                    f0, f1 = half * 0.5, half * 0.5 + 0.5
                    ya = [-(hh[0] + (hh[1] - hh[0]) * f) * s for f in (f0, f1)]
                    kind = 'wall'
                    if wk(*bey[half]) and (n is None or wk(*ins[half])):
                        if n is not None:
                            continue          # open in RO: a walkable passage across this edge (stairs between two tops)
                        kind = 'door'          # a passage toward the void (door / warp corridor): a black curtain WITHOUT collider (object chunk), the player walks through
                        yb = [y + SHELL_M for y in ya]
                    elif n is None:
                        yb = [y + SHELL_M for y in ya]
                    else:
                        yb = [-(nh[0] + (nh[1] - nh[0]) * f) * s for f in (f0, f1)]
                    # an offset void shell is stretched by the offset past the tile's corners, so two shells around a room corner meet (no slit to look through)
                    st = off / tm if (n is None and kind == 'wall') else 0.0          # a door curtain keeps its exact cell edge
                    g0 = f0 - (st if half == 0 else 0.0)
                    g1 = f1 + (st if half == 1 else 0.0)
                    p = []
                    for ys in (ya, yb):
                        for j, f in enumerate((g0, g1)):
                            p.append(((e0[0] + ed[0] * f) * tm + ow[0] * off, ys[j], (e0[1] + ed[1] * f) * tm + ow[1] * off))
                    out.append((tx, ty, p, kind))
    return out


def build(vfs, mapname, cell_m=1.0, tiles_per_block=None, max_chunks=700, model_z='auto', start=None, zone_id=ZONE_ID, tex_cap=256,
          models=True, log=print, extra_sections=(), tick=None, interior=None):
    """-> (pack bytes, manifest dict). vfs: grf.Vfs (or anything with read / exists).
    extra_sections: more (tag, version, count, data) RPG1 sections appended to the pack (api.build adds the sprite set RSAT / RSAN).
    tick(stage, 0..1): progress + cancel hook (api.build raises Cancelled from it), called per terrain row and per model instance."""
    if tick is None:
        def tick(_stage, _f):
            return None
    warn = collections.Counter()
    mp = mapname.lower()
    for ext in ('.rsw', '.gnd', '.gat'):
        if mp.endswith(ext):
            mp = mp[:-4]
    try:
        rw = rofmt.rsw(vfs.read('data\\%s.rsw' % mp))
    except KeyError:
        raise BuildError('map %s: data\\%s.rsw not found in the client data' % (mapname, mp))
    for w_ in rw['warnings']:
        warn[w_] += 1
    gname = rw['gnd'] or mp + '.gnd'
    tname = rw['gat'] or mp + '.gat'
    try:
        g = rofmt.gnd(vfs.read('data\\' + gname))
        t = rofmt.gat(vfs.read('data\\' + tname))
    except KeyError as e:
        raise BuildError('map %s: %s not found' % (mapname, e))
    zoom = g['zoom'] if g['zoom'] > 0 else 10.0
    s = cell_m / (zoom / 2.0)
    W, H = g['w'], g['h']
    log('map %s: rsw %d.%d, gnd %d.%d %dx%d tiles, gat %dx%d cells, %d models, %d textures' % (
        mp, rw['version'] >> 8, rw['version'] & 255, g['version'] >> 8, g['version'] & 255, W, H, t['w'], t['h'], len(rw['models']), len(g['textures'])))
    if (t['w'], t['h']) != (2 * W, 2 * H):
        warn['gat size %dx%d is not 2 x gnd %dx%d' % (t['w'], t['h'], W, H)] += 1

    # ---------------- grid (GAT)
    gw, gh = t['w'], t['h']
    cells = bytearray(gw * gh); hts = []
    walk = 0
    for i in range(gw * gh):
        ok = t['types'][i] in rofmt.GAT_WALKABLE
        cells[i] = 1 if ok else 0
        walk += ok
        hts.append(-sum(t['heights'][i]) / 4.0 * s)
    if not walk:
        raise BuildError('GAT has no walkable cell')

    def gat_y(x_ro, z_ro):
        cx = int(x_ro / (zoom / 2)); cy = int(z_ro / (zoom / 2))
        if 0 <= cx < gw and 0 <= cy < gh:
            return -hts[cy * gw + cx] / s
        return None

    # ---------------- blocks
    bt = tiles_per_block
    if not bt:
        bt = 32
        while ((W + bt - 1) // bt) * ((H + bt - 1) // bt) > POOL_TERRAIN or (W + bt - 1) // bt > 64 or (H + bt - 1) // bt > 64:
            bt += 8
    nbx, nby = (W + bt - 1) // bt, (H + bt - 1) // bt
    if nbx * nby > POOL_TERRAIN:
        warn['%d terrain blocks > scene pool %d' % (nbx * nby, POOL_TERRAIN)] += 1

    # ---------------- ground atlas
    used = sorted(set(sf[2] for sf in g['surfaces'] if 0 <= sf[2] < len(g['textures'])))
    slot_of = {tx: i for i, tx in enumerate(used)}
    images = []
    for tx in used:
        w_, h_, px, _a = load_texture(vfs, g['textures'][tx], warn, cap=4096, magenta=False)
        images.append(img.resize(px, w_, h_, 256, 256))
    if not images:
        images.append(img.placeholder(256, 256)[2]); slot_of[-1] = 0
    log('ground atlas: %d textures' % len(images))
    atlas = build_atlas(images)

    # ---------------- terrain
    # Ground light like the client: texture x (lightmap shadow x tile colour + lightmap colour). The baked intensity already holds sun +
    # ambient + the building shadows (Prontera's shadows are ~140 / 255 = the RSW ambient), so there is no extra sun term on lightmapped
    # ground. The lightmaps reach Unity as the TLMP atlas (8x8 per GND lightmap, RGB = colour, A = shadow) addressed by uv1, the tile colour
    # as the vertex colour; the RO terrain shader (GoldSrcVR/RoTerrain) computes tex(uv0) * (colour * lm.a + lm.rgb). The GPU filters the
    # 6x6 lightmap interior per 2 m tile bilinearly like the client, so the shadow edges stay sharp with 4 vertices per tile (pack 2 had to
    # subdivide 12k Prontera tiles into 3x3 quads to hold them in vertex colours: TERR 4.2 -> 10.4 MB).
    light = rw['light']; Ld = _light_dir(light); obj_light = object_light_scale(light)
    L = g['lightmaps']
    lm_used = sorted(set(sf[3] for sf in g['surfaces'] if 0 <= sf[3] < L['n']))
    lmat = build_lightmap_atlas(g, lm_used)
    log('lightmap atlas: %d lightmaps -> %d slots, %dx%d DXT5, %d mips, %d bytes' % (
        len(lm_used), lmat['slots'], lmat['w'], lmat['h'], len(lmat['levels']), sum(len(x) for x in lmat['levels'])))

    def colour(sf, n):
        """vertex colour of surface sf: the tile colour (lightmapped; the shader multiplies the lightmap shadow and adds its colour) or
        the sun-shaded tile colour for a surface without a lightmap (its lightmap slot is 0 = lit, no colour)."""
        col = sf[4]
        if not (0 <= sf[3] < L['n']):
            k = _shade(n, light, Ld)
            return tuple(max(0, min(255, int(round(k[i] * col[i])))) for i in range(3))
        return (col[0], col[1], col[2])

    def luv(sf, a, b):
        """lightmap atlas UV of surface sf at the tile point (a, b) in 0..1 (the 6x6 interior: pixel coordinates 1 + 6a, 1 + 6b)."""
        slot = lmat['slot_of'].get(sf[3], 0) if 0 <= sf[3] < L['n'] else 0
        return lm_uv(lmat, slot, 1.0 + 6.0 * a, 1.0 + 6.0 * b)

    def auv(sf, a, b):
        """atlas UV of surface sf at the tile corner (a, b) in 0 / 1 (corner j = (j & 1, j >> 1))."""
        r = atlas['slots'][slot_of.get(sf[2], 0)]
        j = int(a) + 2 * int(b)
        u = min(1.0, max(0.0, sf[0][j])); v = min(1.0, max(0.0, sf[1][j]))
        return (r[0] + (r[2] - r[0]) * u, r[1] + (r[3] - r[1]) * v)

    def P3(x, z, h):
        return (x * zoom * s, -h * s, z * zoom * s)

    blocks = {}
    cubes = g['cubes']; surfs = g['surfaces']; ns = len(surfs)
    for ty in range(H):
        tick('terrain', ty / float(max(1, H)))
        for tx in range(W):
            c = cubes[ty * W + tx]
            key = (tx // bt, ty // bt)
            b = blocks.setdefault(key, ([], []))
            verts, tris = b
            h1, h2, h3, h4, up, front, right = c
            if 0 <= up < ns:
                sf = surfs[up]
                p = [P3(tx, ty, h1), P3(tx + 1, ty, h2), P3(tx, ty + 1, h3), P3(tx + 1, ty + 1, h4)]
                nA = _cross(_sub(p[3], p[2]), _sub(p[1], p[2])); nB = _cross(_sub(p[1], p[2]), _sub(p[0], p[2]))
                n = _nrm((nA[0] + nB[0], nA[1] + nB[1], nA[2] + nB[2]))
                cr = colour(sf, n)
                base = len(verts)
                for j in range(4):
                    a_, b_ = j & 1, j >> 1
                    uv = auv(sf, a_, b_); lv = luv(sf, a_, b_)
                    verts.append((p[j][0], p[j][1], p[j][2], _q8(n[0]), _q8(n[1]), _q8(n[2]), cr[0], cr[1], cr[2], 255, uv[0], uv[1], lv[0], lv[1]))
                tris.append((base + 2, base + 3, base + 1)); tris.append((base + 2, base + 1, base))
            walls = []
            if 0 <= front < ns and ty + 1 < H:
                nb = cubes[(ty + 1) * W + tx]
                walls.append((front, [P3(tx, ty + 1, h3), P3(tx + 1, ty + 1, h4), P3(tx, ty + 1, nb[0]), P3(tx + 1, ty + 1, nb[1])], (h3, h4, nb[0], nb[1])))
            if 0 <= right < ns and tx + 1 < W:
                nb = cubes[ty * W + tx + 1]
                walls.append((right, [P3(tx + 1, ty + 1, h4), P3(tx + 1, ty, h2), P3(tx + 1, ty + 1, nb[2]), P3(tx + 1, ty, nb[0])], (h4, h2, nb[2], nb[0])))
            for si, p, hh in walls:
                if abs(hh[0] - hh[2]) < 1e-4 and abs(hh[1] - hh[3]) < 1e-4:
                    continue
                sf = surfs[si]
                n0 = _nrm(_cross(_sub(p[1], p[0]), _sub(p[3], p[0])))
                for side, n in ((0, n0), (1, (-n0[0], -n0[1], -n0[2]))):
                    base = len(verts)
                    cr = colour(sf, n)
                    for j in range(4):
                        a_, b_ = j & 1, j >> 1
                        uv = auv(sf, a_, b_); lv = luv(sf, a_, b_)
                        verts.append((p[j][0], p[j][1], p[j][2], _q8(n[0]), _q8(n[1]), _q8(n[2]), cr[0], cr[1], cr[2], 255, uv[0], uv[1], lv[0], lv[1]))
                    if side == 0:
                        tris.append((base, base + 1, base + 3)); tris.append((base, base + 3, base + 2))
                    else:
                        tris.append((base, base + 3, base + 1)); tris.append((base, base + 2, base + 3))
    # interior shells (docs/design/ro_pack.md "Interiors"): black, double sided terrain walls that close the open room edges. Black = vertex colour 0 with
    # lightmap slot 0 (no colour): the RoTerrain shader computes tex * (colour * lm.a + lm.rgb) = 0 whatever the atlas texel. They are terrain, so they collide too.
    auto_int, void_f = interior_of(g)
    is_int = auto_int if interior is None else bool(interior)
    nshell = 0
    doors = []
    if is_int:
        r0 = atlas['slots'][0]
        suv = ((r0[0] + r0[2]) / 2.0, (r0[1] + r0[3]) / 2.0)
        slv = lm_uv(lmat, 0, LM_SLOT / 2.0, LM_SLOT / 2.0)
        for tx, ty, p, kind in shell_quads(g, t, s):
            if kind == 'door':
                doors.append((tx, ty, p))
                continue
            verts, tris = blocks.setdefault((tx // bt, ty // bt), ([], []))
            n0 = _nrm(_cross(_sub(p[1], p[0]), _sub(p[3], p[0])))
            for side, n in ((0, n0), (1, (-n0[0], -n0[1], -n0[2]))):
                base = len(verts)
                for j in range(4):
                    verts.append((p[j][0], p[j][1], p[j][2], _q8(n[0]), _q8(n[1]), _q8(n[2]), 0, 0, 0, 255, suv[0], suv[1], slv[0], slv[1]))
                if side == 0:
                    tris.append((base, base + 1, base + 3)); tris.append((base, base + 3, base + 2))
                else:
                    tris.append((base, base + 3, base + 1)); tris.append((base, base + 2, base + 3))
            nshell += 1
    log('interior: %s (%.0f %% of the GND tiles are void%s), %d shell walls, %d door curtains' % (
        'yes' if is_int else 'no', void_f * 100.0, '' if interior is None else ', forced', nshell, len(doors)))
    tblocks = []
    for (bx, by), (verts, tris) in sorted(blocks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if not verts:
            continue
        if len(verts) > MAX_VERTS:
            raise BuildError('terrain block %d_%d has %d vertices (> 65535): use a smaller --block' % (bx, by, len(verts)))
        tblocks.append(dict(bx=bx, by=by, ox=bx * bt * zoom * s, oz=by * bt * zoom * s, verts=verts, tris=tris))
    tys = [v[1] for b in tblocks for v in b['verts']] or [0.0]

    # ---------------- objects
    texs = []; mats = []; mat_of = {}; tex_of = {}
    chunks = []; ostats = collections.Counter()
    zhyp = 'a'
    insts = rw['models'] if models else []
    if insts:
        def fit(flip):
            ok = 0
            for m in insts:
                x = m['pos'][0] + W * zoom / 2.0
                z = (H * zoom / 2.0 - m['pos'][2]) if flip else (m['pos'][2] + H * zoom / 2.0)
                gy = gat_y(x, z)
                if gy is not None and abs(gy - m['pos'][1]) < zoom / 2.0:
                    ok += 1
            return ok
        fa, fb = fit(False), fit(True)
        zhyp = model_z if model_z in ('a', 'b') else ('b' if fb > fa else 'a')
        log('model placement: %d / %d instances on the ground as stored, %d mirrored -> using %s' % (
            fa, len(insts), fb, 'stored z' if zhyp == 'a' else 'mirrored z'))
        cache = {}
        geo = []
        for ii, m in enumerate(insts):
            tick('objects', ii / float(len(insts)))
            fn = m['file']
            if fn not in cache:
                try:
                    rs = rofmt.rsm(vfs.read('data\\model\\' + fn))
                    cache[fn] = (rs, model_parts(rs))
                except KeyError:
                    cache[fn] = None; warn['model missing'] += 1
                except rofmt.FormatError as e:
                    cache[fn] = None; warn['model: %s' % e] += 1
            if cache[fn] is None:
                ostats['skipped instances'] += 1
                continue
            rs, parts = cache[fn]
            if not parts:
                continue
            px, py, pz = m['pos']
            M = _mm(_T(px + W * zoom / 2.0, py, pz + H * zoom / 2.0), _rz(m['rot'][2]))
            M = _mm(M, _rx(m['rot'][0])); M = _mm(M, _ry(m['rot'][1])); M = _mm(M, _S(*m['scale']))
            cx = px + W * zoom / 2.0
            cz = (H * zoom / 2.0 - pz) if zhyp == 'b' else (pz + H * zoom / 2.0)
            mids = []
            for ti in parts:
                path = norm(rs['textures'][ti])
                if path not in mat_of:
                    w_, h_, px_, alpha = load_texture(vfs, rs['textures'][ti], warn, cap=tex_cap)
                    px_ = scale_rgb(px_, obj_light)          # dark dungeon: the RSW light the object shader does not know (1.0 for every town / field)
                    k_ = tex_key(w_, h_, px_, alpha)
                    if k_ in tex_of:                 # same image under another path: one texture + one material
                        mat_of[path] = tex_of[k_]
                        ostats['deduped textures'] += 1
                    else:
                        mat_of[path] = tex_of[k_] = len(mats)
                        lv = _mips(px_, w_, h_)
                        cut = alpha and binary_alpha(px_)          # magenta key: 1-bit alpha -> DXT1 punch-through (half of DXT5)
                        ostats['dxt1 cut-out' if cut else ('dxt5' if alpha else 'dxt1')] += 1
                        texs.append(dict(name=path, fmt=D.FMT_DXT5 if alpha and not cut else D.FMT_DXT1, w=w_, h=h_,
                                         levels=[_enc_blocks(r_, a_, b_, alpha and not cut, cut) for r_, a_, b_ in lv]))
                        fl = P.MF_TWOSIDED | (P.MF_ALPHATEST if alpha else 0)
                        mats.append(dict(tex=len(texs) - 1, flags=fl, alpha_ref=0.5 if alpha else 0.0, alpha=1.0))
                mids.append((mat_of[path], ti))
            geo.append((M, parts, mids, cx, cz))
            ostats['instances'] += 1
        # chunk grouping: (block group, material); coarsen the groups until the chunk count fits the scene pool
        f = 1
        while True:
            keys = set()
            for M, parts, mids, cx, cz in geo:
                gx = int(cx / (zoom * bt * f)); gz = int(cz / (zoom * bt * f))
                for mi, _ti in mids:
                    keys.add((gx, gz, mi))
            if len(keys) <= max_chunks or f >= 64:
                break
            f *= 2
        if len(keys) > max_chunks:
            warn['%d object chunks > budget %d even with one group' % (len(keys), max_chunks)] += 1
        open_ = {}
        for M, parts, mids, cx, cz in geo:
            gx = int(cx / (zoom * bt * f)); gz = int(cz / (zoom * bt * f))
            for mi, ti in mids:
                vs, ts = parts[ti]
                if len(vs) > MAX_VERTS:
                    warn['model part > 65535 vertices skipped'] += 1
                    continue
                k = (gx, gz, mi)
                c = open_.get(k)
                if c is None or len(c['verts']) + len(vs) > MAX_VERTS:
                    c = dict(mat=mi, bx=min(nbx - 1, gx * f), by=min(nby - 1, gz * f), verts=[], idx=[], _n=[])
                    open_[k] = c; chunks.append(c)
                base = len(c['verts'])
                pts = []
                for x, y, z, u, v in vs:
                    q = _ap(M, (x, y, z))
                    zz = (H * zoom - q[2]) if zhyp == 'b' else q[2]
                    pts.append((q[0] * s, -q[1] * s, zz * s))
                acc = [[0.0, 0.0, 0.0] for _ in pts]
                for a, b, cc in ts:
                    n = _cross(_sub(pts[b], pts[a]), _sub(pts[cc], pts[a]))
                    for j in (a, b, cc):
                        acc[j][0] += n[0]; acc[j][1] += n[1]; acc[j][2] += n[2]
                for j, p in enumerate(pts):
                    n = _nrm(acc[j])
                    c['verts'].append((p[0], p[1], p[2], _q8(n[0]), _q8(n[1]), _q8(n[2]), vs[j][3], vs[j][4]))
                for a, b, cc in ts:
                    c['idx'] += (base + a, base + b, base + cc)
        chunks = [c for c in chunks if c['idx']]
        for c in chunks:
            c.pop('_n', None)
        log('objects: %d instances, %d models, %d chunks (group x%d), %d materials (textures: %d DXT1, %d DXT1 cut-out, %d DXT5, %d deduped)' % (
            ostats['instances'], len([v for v in cache.values() if v]), len(chunks), f, len(mats),
            ostats['dxt1'], ostats['dxt1 cut-out'], ostats['dxt5'], ostats['deduped textures']))
    if doors:             # interior door curtains: black, two sided object chunks (no collider in an RO pack), one per terrain block
        texs.append(dict(name='ro_shell_black', fmt=D.FMT_DXT1, w=4, h=4, levels=[_enc_blocks(bytes([0, 0, 0, 255]) * 16, 4, 4, False)]))
        mats.append(dict(tex=len(texs) - 1, flags=P.MF_TWOSIDED, alpha_ref=0.0, alpha=1.0))
        dmat = len(mats) - 1
        dch = {}
        for tx, ty, p in doors:
            k = (tx // bt, ty // bt)
            c = dch.get(k)
            if c is None:
                c = dch[k] = dict(mat=dmat, bx=k[0], by=k[1], verts=[], idx=[])
                chunks.append(c)
            n0 = _nrm(_cross(_sub(p[1], p[0]), _sub(p[3], p[0])))
            base = len(c['verts'])
            for j in range(4):
                c['verts'].append((p[j][0], p[j][1], p[j][2], _q8(n0[0]), _q8(n0[1]), _q8(n0[2]), 0.5, 0.5))
            c['idx'] += (base, base + 1, base + 3, base, base + 3, base + 2)
        log('door curtains: %d quads in %d object chunks' % (len(doors), len(dch)))
    if not mats:          # OMAT / OTEX must exist; one unused material keeps the sections well formed
        texs.append(dict(name='none', fmt=D.FMT_RGBA32, w=4, h=4, levels=[bytes([128, 128, 128, 255]) * 16]))
        mats.append(dict(tex=0, flags=P.MF_TWOSIDED, alpha_ref=0.0, alpha=1.0))

    # ---------------- water
    wsrc = g['water'] or rw['water']
    planes = []
    wy = None
    if wsrc is not None:
        wy = -wsrc['level'] * s
        if any(h < wy for h in hts):
            planes.append(dict(x0=0.0, z0=0.0, x1=W * zoom * s, z1=H * zoom * s, y=wy, bx=0, by=0, flags=0))
    # ---------------- start + area
    if start is not None and not (0 <= start[0] < gw and 0 <= start[1] < gh):
        warn['start cell %d,%d outside the %dx%d grid: map centre used' % (start[0], start[1], gw, gh)] += 1
        start = None
    if start is None:
        start = (gw // 2, gh // 2)
    sx, sy = start
    best = None
    for r in range(0, max(gw, gh) + 1):
        for yy in range(max(0, sy - r), min(gh, sy + r + 1)):
            for xx in range(max(0, sx - r), min(gw, sx + r + 1)):
                if cells[yy * gw + xx] and (best is None):
                    best = (xx, yy)
        if best:
            break
    bxs, bys = best
    st = ((bxs + 0.5) * cell_m, hts[bys * gw + bxs], (bys + 0.5) * cell_m)
    ymin = min(min(tys), min(hts)); ymax = max(max(tys), max(hts))
    area = dict(zone_id=zone_id, blocks_x=nbx, blocks_y=nby, first_bx=0, first_by=0, block_size=bt * zoom * s, grid_w=gw, grid_h=gh,
                cell_size=cell_m, origin_x=0.0, origin_z=0.0, world_off_x=0.0, world_off_z=0.0, start_x=st[0], start_y=st[1], start_z=st[2],
                bmin_x=0.0, bmin_y=ymin, bmin_z=0.0, bmax_x=W * zoom * s, bmax_y=ymax, bmax_z=H * zoom * s,
                height_unit=0.01, water_level=wy if planes else ymin - 50.0, atlas_gutter_px=atlas['gutter'], terrain_step=1)
    hq = []
    for h in hts:
        v = int(round(h / 0.01))
        if v <= -32768 or v > 32767:
            warn['height out of the i16 range (clamped)'] += 1
            v = max(-32767, min(32767, v))
        hq.append(v)
    sections = [
        (b'AREA', 1, 1, P.enc_area(area)),
        (b'CELL', 1, gw * gh, bytes(cells)),
        (b'HGHT', 1, gw * gh, P.enc_hght(hq)),
        (b'TERR', 1, len(tblocks), P.enc_terr(tblocks)),
        (b'TATL', 1, len(atlas['slots']), P.enc_tatl(atlas['fmt'], atlas['w'], atlas['h'], atlas['levels'], atlas['slot_px'], atlas['gutter'],
                                                     [(used[i] if i < len(used) else 0,) + tuple(r) + (0,) for i, r in enumerate(atlas['slots'])])),
        (b'TTIL', 1, 1, P.enc_ttil([(0, 0, 1, 0)], [])),
        (TAG_TLMP, 1, lmat['slots'], enc_tlmp(lmat)),
        (b'OTEX', 1, len(texs), P.enc_otex(texs)),
        (b'OMAT', 1, len(mats), P.enc_omat(mats)),
        (b'OMSH', 1, len(chunks), P.enc_omsh(chunks)),
        (b'SPWN', 1, 0, b''),
        (b'SPWE', 1, 0, b''),
    ]
    if planes:
        sections.append((b'WATR', 1, len(planes), P.enc_watr(planes, [], D.FMT_DXT1, 0, 0, 10, 4.0)))
    ws = walk_surface(t, g, s, cell_m)
    sections.append((TAG_GWLK, GWLK_VERSION, len(ws['pieces']), enc_gwlk(ws, GW_INTERIOR if is_int else 0)))
    log('walk surface (GWLK): %d cells (%d raised / over the void), %d walls, %d pieces, %d vertices, %d triangles, %d listed bridge / stair cells' % (
        ws['cells'], ws['raised_cells'], ws['walls'], len(ws['pieces']), sum(len(v) for v, _t in ws['pieces']), sum(len(t_) for _v, t_ in ws['pieces']), len(ws['raised'])))
    sections += list(extra_sections)
    tick('write', 0.0)
    pack = P.write_pack(sections)
    man = dict(tool='ro_conv', tool_version=TOOL_VERSION, map=mp, cell_m=cell_m, metres_per_unit=s, tiles_per_block=bt,
               blocks=[nbx, nby], terrain_blocks=len(tblocks), grid=[gw, gh], walkable_cells=walk, start_cell=[bxs, bys],
               start=list(st), ground_textures=len(images), object_textures=len(texs), materials=len(mats), chunks=len(chunks),
               lightmap=dict(lightmaps=len(lm_used), slots=lmat['slots'], w=lmat['w'], h=lmat['h'], mips=len(lmat['levels'])),
               texture_formats=dict(dxt1=ostats['dxt1'], dxt1_cutout=ostats['dxt1 cut-out'], dxt5=ostats['dxt5'], deduped=ostats['deduped textures']),
               sections={k.decode(): len(v) for k, _sv, _c, v in sections}, instances=ostats['instances'], skipped_instances=ostats['skipped instances'], model_z=zhyp,
               water=dict(y=wy, plane=bool(planes)), interior=is_int, void_fraction=round(void_f, 3), shell_walls=nshell, door_curtains=len(doors),
               walk=dict(cells=ws['cells'], raised_cells=ws['raised_cells'], walls=ws['walls'], pieces=len(ws['pieces']),
                         verts=sum(len(v) for v, _t in ws['pieces']), tris=sum(len(t_) for _v, t_ in ws['pieces']), listed=len(ws['raised'])),
               bytes=len(pack), warnings=dict(warn))
    for k, v in sorted(warn.items()):
        log('  warn: %s x%d' % (k, v))
    log('pack: %d bytes (%.1f MiB), %d sections' % (len(pack), len(pack) / 1048576.0, len(sections)))
    return pack, man
