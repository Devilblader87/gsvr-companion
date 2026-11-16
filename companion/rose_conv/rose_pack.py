"""ROSE Online -> RPG1 pack converter (prototype). Reads the player's own client files READ-ONLY, writes one pack for a
5x5 block window of one zone. Stdlib only. Layout: docs/RPG1_PACK.md.

  python3 -I tools/rose_pack.py <client dir> -o out/birth_island.rpg1 [--zone 20] [--center 32_35] [--size 5]
  python3 -I tools/rose_pack.py <client dir> --zone 22 --all -o out/adventurers_plain.rpg1      

COORDINATE DECISION (the one place where it is written down):
  file space  : right-handed, Z up, centimetres; X east, Y north (zone centre = IFO (0,0) = block (32,32) centre).
  pack / Unity: left-handed, Y up, METRES.  (x, y, z)_file -> (x, z, y)_unity / 100, i.e. X east, Z north, Y up.
                (a swap of two axes is a reflection, so triangle winding is REVERSED for objects; terrain is built clockwise).
  local origin: south-west corner of the block window (x grow east, z grow north). Unity absolute = local + AREA.world_off.
                absolute = (x_file/100 + 5200, z_file/100, y_file/100 + 5200); block (bx,by) spans x 160*bx..+160, z 160*(64-by)..+160
                (by grows SOUTH, so the north-most block row has the smallest by).
  quaternions : file (x,y,z,w) -> Unity (-x,-z,-y,w) (reflection conjugation).
  UV          : kept as stored in the ROSE files (v=0 = first DDS row). Texture data is stored in DDS row order, so
                Texture2D.LoadRawTextureData + the UV as is gives the right picture in Unity (no flip).
"""
import sys, os, re, math, json, hashlib, struct, argparse, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosevfs, rosefmt as F, rosedds as D, rpg1 as P, rosechar as C, rose_chars as RC, rose_env, rose_audio as AU, rose_warp, rose_lobby, rose_revive, rose_text as TX

TOOL_VERSION = 4
# every section a full game zone writes with all options (docs/RPG1_PACK.md section 19); --no-chars writes ACTJ instead of the character sections, --lobby adds LOBY
FULL_ZONE_TAGS = (b'AREA', b'CELL', b'HGHT', b'TERR', b'TATL', b'TTIL', b'OTEX', b'OMAT', b'OMSH', b'SPWN', b'SPWE',
                  b'SKEL', b'CMSH', b'CANM', b'ACTR', b'AVAT',
                  b'SPWR', b'NBEH', b'EVNT', b'REVV', b'WARP', b'WATR', b'SKYB', b'OCOL', b'OCLD',
                  b'DROP', b'SKIL', b'ITEM', b'ITEX', b'SKDB', b'SHOP', b'QUST', b'SNDS', b'MMAP', b'UIAT', b'UIDL')
LOBBY_NAME = 'login'          # --lobby output: out/login.rpg1, the world asks the companion for rose__login.rpg1
BUDGET_BYTES = 48 * 1024 * 1024     # whole zone with characters, avatar, sky, water, music (hard design ceiling 100 MB); Birth Island 5x5 = 22.6 MB + 6 MB audio, Adventurer's Plain = 33.1 MB + 6 MB
BLOCK_M = 160.0
CELL_HIM = 2.5
CENTER_OFF = 5200.0           # metres: IFO (0,0) in Unity absolute coordinates


# ------------------------------------------------------------------ small 3x3 / quaternion helpers
def quat_wxyz_to_mat(w, x, y, z):
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return (1, 0, 0, 0, 1, 0, 0, 0, 1)
    w, x, y, z = w / n, x / n, y / n, z / n
    return (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y),
            2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x),
            2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y))


def mat_mul(a, b):
    return tuple(sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3))


def mat_vec(a, v):
    return (a[0] * v[0] + a[1] * v[1] + a[2] * v[2], a[3] * v[0] + a[4] * v[1] + a[5] * v[2], a[6] * v[0] + a[7] * v[1] + a[8] * v[2])


def mat_scale_cols(m, s):
    return (m[0] * s[0], m[1] * s[1], m[2] * s[2], m[3] * s[0], m[4] * s[1], m[5] * s[2], m[6] * s[0], m[7] * s[1], m[8] * s[2])


def mat_det(a):
    return (a[0] * (a[4] * a[8] - a[5] * a[7]) - a[1] * (a[3] * a[8] - a[5] * a[6]) + a[2] * (a[3] * a[7] - a[4] * a[6]))


def mat_cof(a):
    return (a[4] * a[8] - a[5] * a[7], a[5] * a[6] - a[3] * a[8], a[3] * a[7] - a[4] * a[6],
            a[2] * a[7] - a[1] * a[8], a[0] * a[8] - a[2] * a[6], a[1] * a[6] - a[0] * a[7],
            a[1] * a[5] - a[2] * a[4], a[2] * a[3] - a[0] * a[5], a[0] * a[4] - a[1] * a[3])


def norm3(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 1.0, 0.0)


def q8(v):
    return max(-127, min(127, int(round(v * 127))))


def file_quat_to_unity(q):
    x, y, z, w = q
    return (-x, -z, -y, w)


def unity_yaw_deg(q_file_xyzw):
    """Heading (degrees, about Unity +Y) of the Unity +Z axis after the file rotation (file +Y rotated)."""
    x, y, z, w = file_quat_to_unity(q_file_xyzw)
    fx = 2 * (x * z + w * y); fz = 1 - 2 * (x * x + y * y)
    return math.degrees(math.atan2(fx, fz))


def facing_yaw_deg(q_file_xyzw):
    """SPWN.yawDeg: heading of the model's FRONT after the IFO rotation. The retail characters face file -Y at rest (docs/RPG1_PACK.md 8.1),
    i.e. opposite to the rotated +Z of unity_yaw_deg; the packed characters are baked to face +Z, so `Quaternion.Euler(0, yawDeg, 0)` places them."""
    y = unity_yaw_deg(q_file_xyzw) + 180.0
    return (y + 180.0) % 360.0 - 180.0


# ------------------------------------------------------------------ zone / window
class Window:
    """Rectangular block window: nx blocks east x ny blocks south, first block (bx0, by0) = north-west. `Window(bx0, by0, n)` is the square case."""

    def __init__(self, bx0, by0, nx, ny=None):
        ny = nx if ny is None else ny
        self.bx0, self.by0, self.nx, self.ny = bx0, by0, nx, ny
        self.n = max(nx, ny)
        self.x0 = BLOCK_M * bx0                      # absolute Unity X of local x = 0
        self.z0 = BLOCK_M * (65 - by0 - ny)          # absolute Unity Z of local z = 0
        self.size_x = BLOCK_M * nx
        self.size_z = BLOCK_M * ny
        self.size_m = max(self.size_x, self.size_z)  # legacy (square windows)

    def blocks(self):
        return [(self.bx0 + i, self.by0 + j) for j in range(self.ny) for i in range(self.nx)]

    def to_local(self, x_cm, y_cm, z_cm):
        """file-space centimetres (x east, y north, z up) -> local Unity (x, y, z) metres."""
        return (x_cm / 100.0 + CENTER_OFF - self.x0, z_cm / 100.0, y_cm / 100.0 + CENTER_OFF - self.z0)

    def block_of_file(self, x_cm, y_cm):
        ax = x_cm / 100.0 + CENTER_OFF; az = y_cm / 100.0 + CENTER_OFF
        return int(math.floor(ax / BLOCK_M)), int(math.floor(65 - az / BLOCK_M))


def zone_row(vfs, zone_id):
    zl = F.stb(vfs.read('3DDATA/STB/LIST_ZONE.STB'))
    c = zl['cells'][zone_id]
    def col(i):
        return c[i].replace('\\\\', '\\') if i < len(c) else ''
    name = col(0)
    try:
        _, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_ZONE_S.STL'))
        sid = col(26).strip()
        if sid in keys:
            name = TX.stl_text((_, keys, langs), sid, 0, TX.EN) or name       # English, else the other languages, Korean last (rose_text.pick)
    except Exception:
        pass
    # LIST_ZONE col 4 (지하지형, "0: outdoor, 1: underground"): the dungeon floors (Goblin Cave 31 - 33) set it; their walk grid is sealed to the cave (seal_interior)
    return dict(id=zone_id, name=name, zon=col(1), start=col(2), deco_zsc=col(11), cnst_zsc=col(12), cells=list(c), underground=col(4).strip() == '1')


def existing_blocks(vfs, zone_dir):
    out = set()
    pre = zone_dir
    for k in vfs.files:
        if k.startswith(pre) and k.endswith('.HIM'):
            nm = k[len(pre):-4]
            if '/' in nm:
                continue
            try:
                bx, by = nm.split('_'); out.add((int(bx), int(by)))
            except ValueError:
                pass
    return out


def choose_window(have, center, n):
    """n x n window around `center`, shifted/shrunk to stay inside the bounding box of the existing blocks."""
    xs = [b[0] for b in have]; ys = [b[1] for b in have]
    n = min(n, max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    bx0 = center[0] - n // 2; by0 = center[1] - n // 2
    bx0 = max(min(xs), min(bx0, max(xs) - n + 1)); by0 = max(min(ys), min(by0, max(ys) - n + 1))
    return bx0, by0, n


def bounding_window(have):
    """smallest rectangle (bx0, by0, nx, ny) holding every existing block."""
    xs = [b[0] for b in have]; ys = [b[1] for b in have]
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def parse_blocks(spec):
    """'31:37,30:33' -> (bx0, by0, nx, ny) (inclusive ranges, x then y); '32_35' -> a single block."""
    if '_' in spec and ':' not in spec:
        bx, by = (int(v) for v in spec.split('_'))
        return bx, by, 1, 1
    xs, ys = spec.split(',')
    x0, x1 = (int(v) for v in xs.split(':')); y0, y1 = (int(v) for v in ys.split(':'))
    if x1 < x0 or y1 < y0:
        raise ValueError('empty block range %r' % spec)
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


# ------------------------------------------------------------------ terrain
class Terrain:
    """Global height / lightmap data of the window, north-to-south rows like the HIM files."""

    def __init__(self, vfs, zone_dir, win):
        self.win = win
        self.dimx = 64 * win.nx + 1
        self.dimy = 64 * win.ny + 1
        self.H = [[None] * self.dimx for _ in range(self.dimy)]     # H[row from north][col] metres
        self.have = {}
        self.til = {}
        self.light = {}
        for (bx, by) in win.blocks():
            base = '%s%d_%d' % (zone_dir, bx, by)
            if rosevfs.norm(base + '.HIM') not in vfs.files:
                continue
            w, h, extra, hs, ok = F.him(vfs.read(base + '.HIM'))
            if (w, h) != (65, 65):
                raise ValueError('%s: HIM is %dx%d, expected 65x65' % (base, w, h))
            self.have[(bx, by)] = True
            i, j = bx - win.bx0, by - win.by0
            for r in range(65):
                row = self.H[j * 64 + r]
                for c in range(65):
                    row[i * 64 + c] = hs[r * 65 + c] / 100.0
            tp = rosevfs.norm(base + '.TIL')
            if tp in vfs.files:
                tw, th, t, _ = F.til(vfs.read(tp))
                if (tw, th) != (16, 16):
                    raise ValueError('%s: TIL is %dx%d' % (base, tw, th))
                self.til[(bx, by)] = t
            lp = rosevfs.norm('%s%d_%d/%d_%d_PLANELIGHTINGMAP.DDS' % (zone_dir, bx, by, bx, by))
            if lp in vfs.files:
                try:
                    dd = D.parse_dds(vfs.read(lp))
                    if dd['fmt'] == 'DXT1' or dd['fmt'] == 'DXT5' or dd['fmt'] == 'DXT3':
                        self.light[(bx, by)] = (dd['w'], dd['h'], D.decode_dxt(dd['fmt'], dd['levels'][0], dd['w'], dd['h']))
                    else:
                        self.light[(bx, by)] = (dd['w'], dd['h'], dd['levels'][0])
                except Exception as e:
                    print('  warn: lightmap %s: %s' % (lp, e), file=sys.stderr)

    def h_rc(self, r, c):
        r = max(0, min(self.dimy - 1, r)); c = max(0, min(self.dimx - 1, c))
        v = self.H[r][c]
        if v is None:                                   # missing neighbour block: nearest known value
            for rr, cc in ((r, c - 1), (r, c + 1), (r - 1, c), (r + 1, c)):
                if 0 <= rr < self.dimy and 0 <= cc < self.dimx and self.H[rr][cc] is not None:
                    return self.H[rr][cc]
            return 0.0
        return v

    def known(self, r, c):
        return 0 <= r < self.dimy and 0 <= c < self.dimx and self.H[r][c] is not None

    def height_at(self, x, z):
        """bilinear height (m) at local Unity x, z (z north). None outside / over missing blocks."""
        fx = x / CELL_HIM; fr = (self.dimy - 1) - z / CELL_HIM
        if fx < 0 or fr < 0 or fx > self.dimx - 1 or fr > self.dimy - 1:
            return None
        c0 = min(int(fx), self.dimx - 2); r0 = min(int(fr), self.dimy - 2)
        tx = fx - c0; tr = fr - r0
        if not all(self.known(r0 + a, c0 + b) for a in (0, 1) for b in (0, 1)):
            return None
        h00 = self.H[r0][c0]; h01 = self.H[r0][c0 + 1]; h10 = self.H[r0 + 1][c0]; h11 = self.H[r0 + 1][c0 + 1]
        return (h00 * (1 - tx) + h01 * tx) * (1 - tr) + (h10 * (1 - tx) + h11 * tx) * tr

    def normal(self, r, c):
        dx = (self.h_rc(r, c + 1) - self.h_rc(r, c - 1)) / (2 * CELL_HIM)
        dr = (self.h_rc(r + 1, c) - self.h_rc(r - 1, c)) / (2 * CELL_HIM)
        return norm3((-dx, 1.0, dr))                    # +row = south = -z

    def light_rgb(self, bx, by, cx, cy):
        """vertex colour (0..255) = lightmap * 2 at block cell (cx, cy) (0..64)."""
        lm = self.light.get((bx, by))
        if not lm:
            return (255, 255, 255)
        w, h, px = lm
        x = min(w - 1, cx * w // 64); y = min(h - 1, cy * h // 64)
        o = (y * w + x) * 4
        return (min(255, px[o] * 2), min(255, px[o + 1] * 2), min(255, px[o + 2] * 2))


def tile_uv(s, t, rot):
    """tile-local (s east, t south) in 0..1 -> rotated/flipped lookup (ZON rotation enum)."""
    if rot == 2:
        return 1 - s, t
    if rot == 3:
        return s, 1 - t
    if rot == 4:
        return 1 - s, 1 - t
    if rot == 5:
        return t, 1 - s
    if rot == 6:
        return 1 - t, s
    return s, t


SLOT_PX, GUTTER_PX, INSET_PX = 320, 32, 1.0          # atlas slot = 256 content + 2 x 32 gutter (level 0); the gutter halves per mip level and stays block aligned down to level 3
ATLAS_MIPS = 4                                        # mip levels in TATL (1.1 packs had 2 with 272 px slots and 8 px gutters)
LEGACY_SLOT_PX = 272                                  # --atlas-mips 2 --atlas-slot 272 reproduces the 1.1 atlas


def build_atlas(vfs, zon, used_tex, cols=8, mips=ATLAS_MIPS, slot_px=SLOT_PX):
    """Atlas of the used ZON tile textures, DXT5, `mips` levels, 4-px-aligned gutters of replicated edge blocks.
    Slot pitch `slot_px` = 256 + 2 * gutter; every mip level keeps the gutter block aligned (gutter >> level is a multiple of 4), which bounds `mips` (32 px gutter: 4 levels).
    -> (atlas dict, slot_of: zon_tex_idx -> slot)"""
    gutter = (slot_px - 256) // 2
    if slot_px < 256 or (slot_px - 256) % 2 or gutter % (4 << (mips - 1)) or mips < 1:
        raise ValueError('atlas slot %d with %d mips: gutter %d is not block aligned at the last level' % (slot_px, mips, gutter))
    used = sorted(used_tex)
    slot_of = {t: i for i, t in enumerate(used)}
    n = len(used)
    cols = max(1, min(cols, n))
    rows = (n + cols - 1) // cols
    W, H = cols * slot_px, rows * slot_px
    # per slot and level: list of block rows (each row = bytes of (bw + 2 gb) blocks)
    store = [[None] * mips for _ in range(n)]
    flags = [0] * n
    for si, t in enumerate(used):
        path = zon['textures'][t]
        dd = D.parse_dds(vfs.read(path))
        if dd['w'] != 256 or dd['h'] != 256:
            raise ValueError('tile texture %s is %dx%d (expected 256x256)' % (path, dd['w'], dd['h']))
        fmt, w, h, lv = D.to_pack_texture(dd)
        if fmt == D.FMT_RGBA32:
            lv = [D.encode_dxt5(x, w, h) for x in lv]
        elif fmt == D.FMT_DXT1:
            lv = [D.dxt1_to_dxt5(x, w, h) for x in lv]
        lv = list(lv)
        if len(lv) < mips:                                # generate the missing levels: box filter from the last decoded one, re-encode
            lw = max(1, 256 >> (len(lv) - 1))
            rgba = D.decode_dxt('DXT5', lv[-1], lw, lw)
            while len(lv) < mips:
                rgba, lw, _lh = D.downsample2(rgba, lw, lw)
                lv.append(D.encode_dxt5(rgba, lw, lw))
                flags[si] |= 1 if len(lv) == 2 else 2
        for lvl in range(mips):
            bw = 64 >> lvl; gb = (gutter >> lvl) // 4
            data = lv[lvl]
            rowsb = []
            for j in range(bw + 2 * gb):
                cj = min(bw - 1, max(0, j - gb))
                rb = data[cj * bw * 16:(cj + 1) * bw * 16]
                rowsb.append(rb[:16] * gb + rb + rb[-16:] * gb)
            store[si][lvl] = rowsb
    out = []
    for lvl in range(mips):
        bw = 64 >> lvl; gb = (gutter >> lvl) // 4; tot = bw + 2 * gb
        buf = bytearray()
        for r in range(rows):
            for j in range(tot):
                for c in range(cols):
                    si = r * cols + c
                    buf += store[si][lvl][j] if si < n else b'\0' * (tot * 16)
        out.append(bytes(buf))
    slots = []
    for si, t in enumerate(used):
        c, r = si % cols, si // cols
        x0 = c * slot_px + gutter + INSET_PX; x1 = c * slot_px + gutter + 256 - INSET_PX
        y0 = r * slot_px + gutter + INSET_PX; y1 = r * slot_px + gutter + 256 - INSET_PX
        slots.append((t, x0 / W, y0 / H, x1 / W, y1 / H, flags[si]))
    return dict(fmt=D.FMT_DXT5, w=W, h=H, levels=out, slots=slots, slot_px=slot_px, gutter=gutter, mips=mips), slot_of


def build_terrain(terr, zon, slot_of, atlas, step, win):
    blocks = []; ttil = []
    n_cells = 4 // step
    for (bx, by) in win.blocks():
        if (bx, by) not in terr.have:
            continue
        til = terr.til.get((bx, by)) or [0] * 256
        ttil.append((bx, by, til))
        i, j = bx - win.bx0, by - win.by0
        verts = []; tris = []
        for ty in range(16):
            for tx in range(16):
                tile = zon['tiles'][til[ty * 16 + tx]] if til[ty * 16 + tx] < len(zon['tiles']) else zon['tiles'][0]
                l1, l2, o1, o2, blend, rot, _u = tile
                s1 = atlas['slots'][slot_of[l1 + o1]]; s2 = atlas['slots'][slot_of[l2 + o2]]
                base = len(verts)
                for b in range(n_cells + 1):
                    for a in range(n_cells + 1):
                        cx = tx * 4 + a * step; cy = ty * 4 + b * step          # HIM cell inside the block
                        C = i * 64 + cx; R = j * 64 + cy
                        x = C * CELL_HIM; z = (terr.dimy - 1 - R) * CELL_HIM
                        y = terr.h_rc(R, C)
                        nx, ny, nz = terr.normal(R, C)
                        cr, cg, cb = terr.light_rgb(bx, by, cx, cy)
                        s = a / n_cells; t = b / n_cells
                        u1 = s1[1] + (s1[3] - s1[1]) * s; v1 = s1[2] + (s1[4] - s1[2]) * t
                        rs, rt = tile_uv(s, t, rot)
                        u2 = s2[1] + (s2[3] - s2[1]) * rs; v2 = s2[2] + (s2[4] - s2[2]) * rt
                        verts.append((x, y, z, q8(nx), q8(ny), q8(nz), cr, cg, cb, 255, u1, v1, u2, v2))
                row = n_cells + 1
                for b in range(n_cells):
                    for a in range(n_cells):
                        nw = base + b * row + a; ne = nw + 1; sw = nw + row; se = sw + 1
                        tris.append((nw, ne, se)); tris.append((nw, se, sw))   # clockwise seen from above (Unity front face)
        blocks.append(dict(bx=bx, by=by, ox=i * BLOCK_M, oz=(win.ny - 1 - j) * BLOCK_M, verts=verts, tris=tris))
    return blocks, ttil


# ------------------------------------------------------------------ object lightmaps (LIT) -> vertex colours
class LightPages:
    """Decoded object lightmap pages (DXT1 DDS next to the block's LIT files), cached per (block, file)."""

    def __init__(self, vfs, zone_dir):
        self.vfs, self.dir = vfs, zone_dir
        self.pages = {}
        self.lit = {}
        self.warn = collections.Counter()

    def lit_for(self, bx, by, kind):
        """kind 'deco' -> OBJECTLIGHTMAPDATA.LIT, 'cnst' -> BUILDINGLIGHTMAPDATA.LIT -> {object_id: [part dicts]} (empty when the block has none)"""
        key = (bx, by, kind)
        if key not in self.lit:
            fn = 'OBJECTLIGHTMAPDATA.LIT' if kind == 'deco' else 'BUILDINGLIGHTMAPDATA.LIT'
            path = '%s%d_%d/LIGHTMAP/%s' % (self.dir, bx, by, fn)
            res = {}
            if rosevfs.norm(path) in self.vfs.files:
                try:
                    res = F.lit(self.vfs.read(path))
                except Exception as e:
                    self.warn['LIT: %s' % e.__class__.__name__] += 1
            self.lit[key] = res
        return self.lit[key]

    def page(self, bx, by, name):
        key = (bx, by, name.upper())
        if key not in self.pages:
            path = '%s%d_%d/LIGHTMAP/%s' % (self.dir, bx, by, name)
            pg = None
            if rosevfs.norm(path) in self.vfs.files:
                try:
                    dd = D.parse_dds(self.vfs.read(path))
                    px = D.decode_dxt(dd['fmt'], dd['levels'][0], dd['w'], dd['h']) if dd['fmt'].startswith('DXT') else dd['levels'][0]
                    pg = (dd['w'], dd['h'], px)
                except Exception as e:
                    self.warn['lightmap page: %s' % e.__class__.__name__] += 1
            else:
                self.warn['lightmap page missing'] += 1
            self.pages[key] = pg
        return self.pages[key]


def sample_lightmap(pg, ppr, slot, u, v):
    """bilinear sample of the cell `slot` (grid of ppr x ppr cells) at part-local uv (clamped into the cell) -> (r, g, b) 0..255, already x 2 (modulate2x)"""
    w, h, px = pg
    u = min(1.0, max(0.0, u)); v = min(1.0, max(0.0, v))
    cw = w / ppr; ch = h / ppr
    fx = (slot % ppr) * cw + u * cw - 0.5; fy = (slot // ppr) * ch + v * ch - 0.5
    x0 = int(math.floor(fx)); y0 = int(math.floor(fy)); tx = fx - x0; ty = fy - y0
    cx0 = (slot % ppr) * cw; cy0 = (slot // ppr) * ch
    def at(x, y):
        x = int(min(max(x, cx0), cx0 + cw - 1)); y = int(min(max(y, cy0), cy0 + ch - 1))
        o = (min(h - 1, y) * w + min(w - 1, x)) * 4
        return px[o], px[o + 1], px[o + 2]
    a = at(x0, y0); b = at(x0 + 1, y0); c = at(x0, y0 + 1); d = at(x0 + 1, y0 + 1)
    out = []
    for k in range(3):
        val = (a[k] * (1 - tx) + b[k] * tx) * (1 - ty) + (c[k] * (1 - tx) + d[k] * tx) * ty
        out.append(min(255, int(round(val * 2))))
    return tuple(out)


# ------------------------------------------------------------------ objects
class ObjBuilder:
    def __init__(self, vfs, win, grid):
        self.vfs, self.win, self.grid = vfs, win, grid
        self.zms = {}
        self.tex_index = {}
        self.textures = []
        self.mats = []
        self.mat_index = {}
        self.chunks = {}                 # (mat, bx, by) -> list of chunk dicts
        self.warn = collections.Counter()
        self.part_cache = {}
        self.stats = collections.Counter()
        self.compress_raw = False        # character textures: raw RGB -> DXT (+ mips)

    def get_zms(self, path):
        k = rosevfs.norm(path)
        if k not in self.zms:
            try:
                z = F.zms(self.vfs.read(path))
                if 'pos' not in z or 'idx' not in z:
                    raise ValueError('mesh without positions')
                self.zms[k] = z
            except Exception as e:
                self.warn['mesh: %s' % e.__class__.__name__] += 1
                print('  warn: mesh %s: %s' % (path, e), file=sys.stderr)
                self.zms[k] = None
        return self.zms[k]

    def get_tex(self, path):
        k = rosevfs.norm(path)
        ck = (k, self.compress_raw)
        if ck in self.tex_index:
            return self.tex_index[ck]
        idx = P.NO_TEX
        try:
            if not k.endswith('.DDS'):
                raise ValueError('only DDS textures are converted (%s)' % k.rsplit('.', 1)[-1])
            dds = D.parse_dds(self.vfs.read(path))
            fmt, w, h, lv = D.compress_raw(dds) if self.compress_raw else D.to_pack_texture(dds)
            self.textures.append(dict(name=k, fmt=fmt, w=w, h=h, levels=lv))
            idx = len(self.textures) - 1
        except Exception as e:
            self.warn['texture: %s' % e] += 1
            print('  warn: texture %s: %s' % (path, e), file=sys.stderr)
        self.tex_index[ck] = idx
        return idx

    def get_mat(self, zsc, mi):
        path, f, alpha, glow = zsc['mats'][mi]
        key = (rosevfs.norm(path), tuple(f[1:5]), f[7], f[8], round(alpha, 3), self.compress_raw)
        if key in self.mat_index:
            return self.mat_index[key]
        flags = 0
        if f[1]: flags |= P.MF_BLEND
        if f[2]: flags |= P.MF_TWOSIDED
        if f[3]: flags |= P.MF_ALPHATEST
        if f[7]: flags |= P.MF_ADDITIVE
        if f[8]: flags |= P.MF_SPECULAR
        self.mats.append(dict(tex=self.get_tex(path), flags=flags, alpha_ref=f[4] / 256.0, alpha=alpha if alpha else 1.0, path=rosevfs.norm(path)))
        self.mat_index[key] = len(self.mats) - 1
        return self.mat_index[key]

    def object_parts(self, zsc_id, zsc, oid):
        """-> [(zms path, material id in zsc, A 3x3, t (m, file space), collision bits)] with parent chains resolved."""
        key = (zsc_id, oid)
        if key in self.part_cache:
            return self.part_cache[key]
        parts, _eff = zsc['objs'][oid]
        local = []
        for mi, ma, pr in parts:
            rot = pr.get('rot', (1, 0, 0, 0))
            m = quat_wxyz_to_mat(*rot) if any(rot) else (1, 0, 0, 0, 1, 0, 0, 0, 1)
            m = mat_scale_cols(m, pr.get('scale', (1, 1, 1)))
            t = tuple(c / 100.0 for c in pr.get('pos', (0, 0, 0)))
            local.append((m, t, pr.get('parent', 0) - 1))
        out = []
        for idx, (mi, ma, pr) in enumerate(parts):
            A, t = local[idx][0], local[idx][1]
            p = local[idx][2]; guard = 0
            while p >= 0 and guard < 16:
                pm, pt, pp = local[p]
                t = tuple(a + b for a, b in zip(mat_vec(pm, t), pt)); A = mat_mul(pm, A); p = pp; guard += 1
            out.append((zsc['meshes'][mi], ma, A, t, pr.get('col', 0)))
        self.part_cache[key] = out
        return out

    def add_instance(self, zsc_id, zsc, obj, bxy, lm=None):
        """lm: {zsc part index: (page, ppr, slot)} lightmap cells of this instance (None = no lightmap)."""
        oid = obj['id']
        if oid >= len(zsc['objs']):
            self.warn['object id out of range'] += 1
            return
        R = quat_wxyz_to_mat(obj['q'][3], obj['q'][0], obj['q'][1], obj['q'][2])     # IFO quaternion is x,y,z,w
        Ri = mat_scale_cols(R, obj['scale'])
        pos_m = tuple(c / 100.0 for c in obj['pos'])
        self.stats['instances'] += 1
        for part_idx, (mesh_path, ma, Ap, tp, colbits) in enumerate(self.object_parts(zsc_id, zsc, oid)):
            z = self.get_zms(mesh_path)
            if z is None or ma >= len(zsc['mats']):
                continue
            A = mat_mul(Ri, Ap)
            t = tuple(a + b for a, b in zip(mat_vec(Ri, tp), pos_m))
            cof = mat_cof(A); sgn = 1.0 if mat_det(A) >= 0 else -1.0
            pos = z['pos']; nrm = z.get('nrm'); uv = z.get('uv1')
            vs = []
            wx, wy, wz = self.win.x0, 0.0, self.win.z0
            for k, p in enumerate(pos):
                fx, fy, fz = mat_vec(A, p)
                fx += t[0]; fy += t[1]; fz += t[2]
                ux, uy, uz = fx + CENTER_OFF - wx, fz, fy + CENTER_OFF - wz
                if nrm:
                    nx, ny, nz = mat_vec(cof, nrm[k]); nx, ny, nz = norm3((nx * sgn, ny * sgn, nz * sgn))
                else:
                    nx, ny, nz = 0.0, 0.0, 1.0
                u, v = uv[k] if uv else (0.0, 0.0)
                vs.append((ux, uy, uz, q8(nx), q8(nz), q8(ny), u, v))          # normal (x,y,z)_file -> (x,z,y)_unity
            idx = z['idx']
            tri = [(idx[i], idx[i + 1], idx[i + 2]) for i in range(0, len(idx) - 2, 3)]
            if sgn > 0:
                tri = [(a, c, b) for a, b, c in tri]                          # reflection (x,z,y) flips the winding
            shape = colbits & 7
            if shape and not (colbits & 32):
                self.grid.rasterize(vs, tri)
                self.stats['collision parts'] += 1
            if shape:
                self.grid.collect(vs, tri, colbits)
            cols = None
            lmi = lm.get(part_idx) if lm else None
            if lmi and z.get('uv2'):
                pg, ppr, slot = lmi
                cols = bytearray()
                for u2, v2 in z['uv2']:
                    r, g, b = sample_lightmap(pg, ppr, slot, u2, v2)
                    cols += bytes((r, g, b, 255))
                self.stats['lightmapped parts'] += 1
            mat = self.get_mat(zsc, ma)
            cf = collide_flags(colbits)
            if cf:
                self.stats['collider parts'] += 1
            self._emit(mat, bxy, vs, tri, cols, cf)
        self.stats['parts'] += 0

    def _emit(self, mat, bxy, vs, tri, cols=None, cflags=0):
        """cflags != 0: the part is a physics collider (OCLD): its index range in the chunk is remembered with these flags (collide_flags)."""
        lst = self.chunks.setdefault((mat, bxy[0], bxy[1]), [])
        if not lst or len(lst[-1]['verts']) + len(vs) > 65535:
            lst.append(dict(mat=mat, bx=bxy[0], by=bxy[1], verts=[], idx=[], cols=bytearray(), lit=False, col=[]))
        c = lst[-1]
        if cflags and tri:
            c['col'].append((len(c['idx']), 3 * len(tri), cflags))
        base = len(c['verts'])
        c['verts'].extend(vs)
        if cols is not None:
            c['lit'] = True
            c['cols'] += cols
        else:
            c['cols'] += b'\xff' * (4 * len(vs))
        for a, b, d in tri:
            c['idx'].extend((a + base, b + base, d + base))

    def chunk_list(self):
        out = []
        for key in sorted(self.chunks):
            out.extend(c for c in self.chunks[key] if c['idx'])
        return out


# ------------------------------------------------------------------ physics colliders (OCLD)
COL_NOT_MOVEABLE, COL_HEIGHT_ONLY = 8, 32          # ZSC collision flags (bit 0..2 = shape: 0 none, 1 sphere, 2 AABB, 3 OBB, 4 polygon)
OCLD_FLOOR, OCLD_WALL = 1, 2
OCLD_MAX_TRIS = 4096                               # triangles per collider piece: one PhysX cook per frame stays at a few ms


def collide_flags(colbits):
    """OCLD flags of a ZSC object part (0 = no collider). The original (rose-offline-client zone_loader + collision_system) uses the part mesh as a trimesh:
    a part WITH a collision shape is a floor for the down ray unless NOT_MOVEABLE (bit 3) and a wall for the 0.4 m ball cast unless HEIGHT_ONLY (bit 5);
    a part without a shape collides with nothing. VRChat has one player collider for both, so every part that is a floor or a wall becomes a collider."""
    if not (colbits & 7):
        return 0
    f = (0 if colbits & COL_NOT_MOVEABLE else OCLD_FLOOR) | (0 if colbits & COL_HEIGHT_ONLY else OCLD_WALL)
    return f


def collider_pieces(chunks, max_tris=OCLD_MAX_TRIS):
    """chunks (chunk_list order, each with 'col' = [(firstIndex, indexCount, flags)]) -> (pieces, ranges): ranges (firstIndex, indexCount) index the chunk's
    OMSH index list; a piece is one MeshCollider = up to max_tris triangles of ONE chunk (chunk, firstRange, rangeCount, flags, tris), pieces in chunk order.
    Adjacent ranges merge; a range longer than the room left in a piece is split at a triangle boundary."""
    pieces = []; ranges = []
    for ci, c in enumerate(chunks):
        merged = []
        for first, cnt, fl in sorted(c.get('col', [])):
            if merged and merged[-1][0] + merged[-1][1] == first:
                merged[-1] = (merged[-1][0], merged[-1][1] + cnt, merged[-1][2] | fl)
            else:
                merged.append((first, cnt, fl))
        cur = None
        for first, cnt, fl in merged:
            while cnt > 0:
                if cur is None or cur[4] >= max_tris:
                    cur = [ci, len(ranges), 0, 0, 0]
                    pieces.append(cur)
                take = min(cnt, 3 * (max_tris - cur[4]))
                ranges.append((first, take))
                cur[2] += 1; cur[3] |= fl; cur[4] += take // 3
                first += take; cnt -= take
    return [tuple(p) for p in pieces], ranges


# ------------------------------------------------------------------ walkability grid
class Grid:
    def __init__(self, win, cs, terr=None, band=(0.5, 2.0)):
        self.win, self.cs = win, cs
        self.terr, self.band = terr, band
        self.per_sample = False                          # DUNGEON: underground zones test the body band per sample point (rasterize)
        self.w = int(round(win.size_x / cs)); self.h = int(round(win.size_z / cs))
        self.cells = bytearray(b'\x01') * (self.w * self.h)
        self.reason = bytearray(self.w * self.h)         # 0 free, 1 outside data, 2 slope, 3 water, 4 object, 5 outside the cave (seal_interior)
        self.obj_cells = 0
        self.floor = {}                                  # cell -> {y in 0.1 m}: walkable object surfaces (flat enough, collision shape without NOT_MOVEABLE)
        self.floor_c = {}                                # PIER FLOOR: cell -> {y in 0.1 m} of the same surfaces exactly under the cell CENTRE (the height the avatar stands at there)
        self.wall = {}                                   # cell -> {(steep, lo, hi, inner)}: collision geometry y-ranges in 0.1 m (inner = sample near the cell centre)
        self.slope = None                                # per cell terrain slope (finish_grid)
        self.floor_cells = 0; self.carved_cells = 0; self.void_cells = 0

    def block(self, i, reason):
        if self.cells[i]:
            self.cells[i] = 0; self.reason[i] = reason

    def rasterize(self, vs, tris):
        """mark every cell the XZ footprint of the triangles touches as blocked by an object."""
        cs = self.cs; w, h = self.w, self.h
        mark = self._mark_obj
        if self.per_sample and self.terr is not None:
            self._rasterize_samples(vs, tris)
            return
        for a, b, c in tris:
            pa, pb, pc = vs[a], vs[b], vs[c]
            if self.terr is not None:                    # only geometry at body height blocks: skip canopies / overhangs / ground-hugging parts
                g = self.terr.height_at((pa[0] + pb[0] + pc[0]) / 3, (pa[2] + pb[2] + pc[2]) / 3)
                if g is not None and (max(pa[1], pb[1], pc[1]) < g + self.band[0] or min(pa[1], pb[1], pc[1]) > g + self.band[1]):
                    continue
            e = max(math.hypot(pa[0] - pb[0], pa[2] - pb[2]), math.hypot(pb[0] - pc[0], pb[2] - pc[2]), math.hypot(pc[0] - pa[0], pc[2] - pa[2]))
            n = max(1, int(math.ceil(e / (cs * 0.5))))
            for i in range(n + 1):
                for j in range(n + 1 - i):
                    u = i / n; v = j / n; t = 1 - u - v
                    mark(pa[0] * t + pb[0] * u + pc[0] * v, pa[2] * t + pb[2] * u + pc[2] * v)

    def collect(self, vs, tris, colbits):
        """remember, per cell, the walkable surfaces (triangles flatter than FLOOR_NY; NOT_MOVEABLE parts excluded: the original's floor ray ignores them) and the
        y-range of every collision triangle, so add_floors can tell a bridge / ramp / stair from a wall."""
        cs = self.cs; w, h = self.w, self.h
        want_floor = not (colbits & 8)
        for a, b, c in tris:
            pa, pb, pc = vs[a], vs[b], vs[c]
            ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]; vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz)
            if ln < 1e-9:
                continue
            flat = abs(ny) / ln >= FLOOR_NY
            lo = int(round(min(pa[1], pb[1], pc[1]) * 10)); hi = int(round(max(pa[1], pb[1], pc[1]) * 10))
            e = max(math.hypot(pa[0] - pb[0], pa[2] - pb[2]), math.hypot(pb[0] - pc[0], pb[2] - pc[2]), math.hypot(pc[0] - pa[0], pc[2] - pa[2]))
            n = max(1, int(math.ceil(e / (cs * 0.5))))
            for i in range(n + 1):
                for j in range(n + 1 - i):
                    u = i / n; v = j / n; t = 1 - u - v
                    x = pa[0] * t + pb[0] * u + pc[0] * v; z = pa[2] * t + pb[2] * u + pc[2] * v
                    gx = int(x // cs); gz = int(z // cs)
                    if not (0 <= gx < w and 0 <= gz < h):
                        continue
                    ci = gz * w + gx
                    if flat and want_floor:
                        self.floor.setdefault(ci, set()).add(int(round((pa[1] * t + pb[1] * u + pc[1] * v) * 10)))
                    if not (colbits & 32):
                        inner = abs(x / cs - gx - 0.5) < INNER and abs(z / cs - gz - 0.5) < INNER
                        self.wall.setdefault(ci, set()).add((not flat, lo, hi, inner))
            if flat and want_floor:
                self._centres(pa, pb, pc)

    def _centres(self, pa, pb, pc):
        """PIER FLOOR: every cell centre inside the XZ footprint of a walkable triangle gets the exact surface height there (floor_c). The edge samples of collect
        spread over the whole cell, so one cell of a ramp / stair holds 3 m of heights and the median is no surface at all."""
        cs = self.cs; w, h = self.w, self.h
        d = (pb[2] - pc[2]) * (pa[0] - pc[0]) + (pc[0] - pb[0]) * (pa[2] - pc[2])
        if abs(d) < 1e-9:
            return
        gx0 = max(0, int(math.floor(min(pa[0], pb[0], pc[0]) / cs - 0.5))); gx1 = min(w - 1, int(math.ceil(max(pa[0], pb[0], pc[0]) / cs - 0.5)))
        gz0 = max(0, int(math.floor(min(pa[2], pb[2], pc[2]) / cs - 0.5))); gz1 = min(h - 1, int(math.ceil(max(pa[2], pb[2], pc[2]) / cs - 0.5)))
        for gz in range(gz0, gz1 + 1):
            z = (gz + 0.5) * cs
            for gx in range(gx0, gx1 + 1):
                x = (gx + 0.5) * cs
                l1 = ((pb[2] - pc[2]) * (x - pc[0]) + (pc[0] - pb[0]) * (z - pc[2])) / d
                l2 = ((pc[2] - pa[2]) * (x - pc[0]) + (pa[0] - pc[0]) * (z - pc[2])) / d
                l3 = 1.0 - l1 - l2
                if l1 < -1e-6 or l2 < -1e-6 or l3 < -1e-6:
                    continue
                self.floor_c.setdefault(gz * w + gx, set()).add(int(round((l1 * pa[1] + l2 * pb[1] + l3 * pc[1]) * 10)))

    def _rasterize_samples(self, vs, tris):
        """DUNGEON: the body band per sample point against the terrain under that point. In a cave the terrain drops by ~100 m into pits right next to the
        floors, so the terrain under a triangle's centroid says nothing about its edges (bridge supports below a room's floor closed the room's doorway).
        The sample spacing also follows the triangle's height, so a tall wall always has samples inside the band."""
        cs = self.cs; b0, b1 = self.band
        terr = self.terr; mark = self._mark_obj
        for a, b, c in tris:
            pa, pb, pc = vs[a], vs[b], vs[c]
            gs = [g for g in (terr.height_at(pa[0], pa[2]), terr.height_at(pb[0], pb[2]), terr.height_at(pc[0], pc[2])) if g is not None]
            ylo = min(pa[1], pb[1], pc[1]); yhi = max(pa[1], pb[1], pc[1])
            if gs and (yhi < min(gs) + b0 or ylo > max(gs) + b1) and max(gs) - min(gs) < b1 - b0:
                continue                                 # flat terrain under the triangle and the triangle wholly above / below the band
            e = max(math.hypot(pa[0] - pb[0], pa[2] - pb[2]), math.hypot(pb[0] - pc[0], pb[2] - pc[2]), math.hypot(pc[0] - pa[0], pc[2] - pa[2]))
            n = max(1, int(math.ceil(e / (cs * 0.5))), min(64, int(math.ceil((yhi - ylo) / (0.5 * (b1 - b0))))))
            for i in range(n + 1):
                for j in range(n + 1 - i):
                    u = i / n; v = j / n; t = 1 - u - v
                    x = pa[0] * t + pb[0] * u + pc[0] * v; z = pa[2] * t + pb[2] * u + pc[2] * v
                    g = terr.height_at(x, z)
                    if g is None:
                        continue
                    y = pa[1] * t + pb[1] * u + pc[1] * v
                    if g + b0 <= y <= g + b1:
                        mark(x, z)

    def _mark_obj(self, x, z):
        gx = int(x // self.cs); gz = int(z // self.cs)
        if 0 <= gx < self.w and 0 <= gz < self.h:
            i = gz * self.w + gx
            if self.cells[i]:
                self.cells[i] = 0; self.reason[i] = 4; self.obj_cells += 1


FLOOR_NY = 0.6              # |normal.y| of a walkable surface (up to ~53 degrees)
INNER = 0.3                 # a collision sample blocks a floor cell only within this fraction of the cell around its centre (rails at the edge of a bridge do not)
FLOOR_STEP = 1.4            # floor levels of neighbouring cells connect up to this many cell sizes of height difference (a ramp at FLOOR_NY)
FLOOR_SINK = 2.0            # BRIDGE PATH: a floor level up to this far below the terrain of an object-blocked cell is a slab sunk into the ground (bridge ends): the terrain is the floor there
FLOOR_CANOPY_MAX = 4.0      # ... but only up to this height: more is another storey of a building (Luxem Tower floors 8 m apart), not a canopy
FLOOR_CANOPY = 1.0          # PIER FLOOR: the highest floor level of a cell floating more than this above the floor cells around it (a sail / canopy top over a deck) is not what the avatar stands on
FLOOR_REACH = 80.0          # floors higher than this above the terrain are ignored (ZONES MORE: was 15; a bridge over a deep gorge floats 50 m above the ground, the chain rule below already drops roofs and towers)
INTERIOR_FLOOR_REACH = 200.0   # DUNGEON: underground zones; the Goblin Cave bridges cross a pit whose terrain lies ~100 m below them (seal_interior already drops everything outside the cave)
PASS_SLOPE = 3.0            # a required point may be linked through slope-blocked cells up to this rise / run (the original has no slope limit at all)
PASS_CELLS = 12             # ... through at most this many cells


def water_top(water, x, z):
    top = None
    for (x0, z0, x1, z1, wh) in water:
        if x0 <= x <= x1 and z0 <= z <= z1 and (top is None or wh > top):
            top = wh
    return top


def floor_levels(ys):
    """sorted floor samples (0.1 m units) -> one height (m) per storey (a gap above 0.8 m starts another one)"""
    out = []; cur = [ys[0]]
    for y in ys[1:]:
        if y - cur[-1] > 8:
            out.append(cur); cur = []
        cur.append(y)
    out.append(cur)
    return [c[len(c) // 2] / 10.0 for c in out]


def floor_levels_c(ys):
    """PIER FLOOR: sorted heights (0.1 m units) of the surfaces exactly under a cell centre -> one height (m) per storey (a gap above 0.8 m starts another one);
    the top surface of a storey (the underside of a plank is a walkable-looking triangle 0.6 m lower)."""
    out = []; cur = [ys[0]]
    for y in ys[1:]:
        if y - cur[-1] > 8:
            out.append(cur[-1]); cur = []
        cur.append(y)
    out.append(cur[-1])
    return [y / 10.0 for y in out]


def pick_floor_level(cand, best, heights, grid):
    """PIER FLOOR: cells with several connected floor levels (deck + canopy / mast / upper storey over it) keep the highest level (the old rule: the floor ray of
    the original hits the top surface) unless it floats between FLOOR_CANOPY and FLOOR_CANOPY_MAX above what the floor cells around stand on: then the level nearest to that
    (median of the decided floor neighbours, 8-neighbourhood) wins. The avatar stood 2 m over a pier deck where a sail part had a flat top. A cell without a decided
    floor neighbour waits for the next pass; the last resort is the highest level. best: cell -> level (in / out)."""
    w, h = grid.w, grid.h
    todo = sorted(ci for ci in cand if ci not in best)
    for _pass in range(8):
        if not todo:
            break
        rest = []
        for ci in todo:
            gx, gz = ci % w, ci // w
            fl = []
            for dz in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    a, b = gx + dx, gz + dz
                    if (dx or dz) and 0 <= a < w and 0 <= b < h and (b * w + a) in best:
                        fl.append(best[b * w + a])
            if not fl:
                rest.append(ci); continue
            ref = sorted(fl)[len(fl) // 2]
            top = max(cand[ci])
            best[ci] = top if not (FLOOR_CANOPY < top - ref <= FLOOR_CANOPY_MAX) else min(cand[ci], key=lambda y: (abs(y - ref), -y))
        todo = rest
    for ci in todo:
        best[ci] = max(cand[ci])


def add_floors(grid, heights, water, max_wade, reach=FLOOR_REACH):
    """Walkable object surfaces (bridges, ramps, stairs, platforms): the original lets the player stand on any collision surface its floor ray hits, the terrain-only
    grid blocked the water / steep ground under them or the rails on them. A floor level of a cell becomes walkable when it is not deep under water, has no collision
    geometry at body height around the cell centre and is reachable from open ground through floor levels of neighbouring cells without a wall spanning the height
    step (a boulder or a roof is not). The cell then gets the floor height in HGHT. Returns the number of cells opened."""
    cs = grid.cs; w, h = grid.w, grid.h
    nodes = []; by_cell = {}; ctag = []
    for ci in sorted(grid.floor):
        if heights[ci] == -32768:
            continue
        hc = heights[ci] / 100.0
        wt = water_top(water, (ci % w + 0.5) * cs, (ci // w + 0.5) * cs)
        walls = grid.wall.get(ci, ())
        fcs = getattr(grid, 'floor_c', None)
        cen = floor_levels_c(sorted(fcs[ci])) if fcs and ci in fcs else []
        # PIER FLOOR: the levels exactly under the cell centre are the heights the avatar stands at; the old edge-sample levels stay as connection nodes only
        # (a ramp foot or a bridge end that does not reach the centre) and decide the height of a cell without a centre level
        for y, isc in [(v, True) for v in cen] + [(v, False) for v in floor_levels(sorted(grid.floor[ci])) if not any(abs(v - c) < 0.05 for c in cen)]:
            if hc - FLOOR_SINK <= y < hc - 0.3 and not grid.cells[ci] and grid.reason[ci] == 4 and not any(c >= hc - 0.3 for c in cen):
                y = hc                                   # BRIDGE PATH: the end of a deck / ramp slab sunk into the ground: the object block was the slab itself, the ground over it is the floor
            if y < hc - 0.3 or y > hc + reach:
                continue
            if abs(y - hc) <= 0.3 and grid.cells[ci]:
                continue                                 # the terrain itself is walkable here
            if wt is not None and y < wt - max_wade:
                continue
            if any(inner and steep and wh - wl >= 6 and wl / 10.0 < y + 2.0 and wh / 10.0 > y + 0.5 for steep, wl, wh, inner in walls):      # risers of stairs (< 0.6 m) are no walls
                continue
            by_cell.setdefault(ci, []).append(len(nodes)); nodes.append((ci, y)); ctag.append(isc)
    parent = list(range(len(nodes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    def spans(ci, lo, hi):
        for steep, wl, wh, _inner in grid.wall.get(ci, ()):
            if steep and wl / 10.0 <= lo + 0.3 and wh / 10.0 >= hi - 0.3:
                return True
        return False

    def compat(ci, a, cj, b):
        d = abs(a - b)
        if d > FLOOR_STEP * cs:
            return False
        if d > 0.5 and (spans(ci, min(a, b), max(a, b)) or spans(cj, min(a, b), max(a, b))):
            return False
        return True
    touch = [False] * len(nodes)
    for k, (ci, y) in enumerate(nodes):
        gx, gz = ci % w, ci // w
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = gx + dx, gz + dz
            if not (0 <= a < w and 0 <= b < h):
                continue
            cj = b * w + a
            if grid.cells[cj] and heights[cj] != -32768 and compat(ci, y, cj, heights[cj] / 100.0):
                touch[k] = True
            for m in by_cell.get(cj, ()):
                if compat(ci, y, cj, nodes[m][1]):
                    ra, rb = find(k), find(m)
                    if ra != rb:
                        parent[ra] = rb
    good = set(find(k) for k in range(len(nodes)) if touch[k])
    opened = 0; best = {}
    cand = {}; cand_e = {}
    for k, (ci, y) in enumerate(nodes):
        if find(k) in good:
            (cand if ctag[k] else cand_e).setdefault(ci, []).append(y)
    for ci in cand_e:
        if ci not in cand:
            cand[ci] = cand_e[ci]
    for ci in cand:
        if len(cand[ci]) == 1:
            best[ci] = cand[ci][0]
    pick_floor_level(cand, best, heights, grid)
    for ci, y in best.items():
        if not grid.cells[ci]:
            grid.cells[ci] = 1; grid.reason[ci] = 0; opened += 1
        heights[ci] = int(round(y * 100))
    # PIER FLOOR: a blocked cell inside a deck (barrel, mast post: a wall at body height) keeps the deck height in HGHT. The height field is interpolated between cell
    # centres, so the terrain 6 m below the pier in that cell pulled the avatar down (or, with a canopy level, up) around it.
    fcs = getattr(grid, 'floor_c', None) or {}
    for ci in sorted(fcs):
        if grid.cells[ci] or ci in best or grid.reason[ci] not in (3, 4) or heights[ci] == -32768:
            continue
        gx, gz = ci % w, ci // w
        ys = sorted(best[cj] for cj in ((gz * w + gx + 1) if gx + 1 < w else -1, (gz * w + gx - 1) if gx > 0 else -1, ci + w if gz + 1 < h else -1, ci - w if gz > 0 else -1) if cj in best)
        if len(ys) < 2:
            continue
        ref = ys[len(ys) // 2]
        y = min(floor_levels_c(sorted(fcs[ci])), key=lambda v: abs(v - ref))
        if abs(y - ref) <= 1.0:
            heights[ci] = int(round(y * 100))
    grid.floor_cells = opened
    return opened


def seal_interior(grid):
    """DUNGEON: walk grid of an underground zone (LIST_ZONE col 4 = 1). A cave is a terrain floor inside rock meshes; outside the rock the heightmap goes on as
    flat ground that no geometry covers and no light reaches (the original's walls keep the player in; our rasterised walls have gaps at body height and the
    corridor ends behind the gates are open). Every cell that holds no collision geometry and no object floor and is connected to the window border through
    such cells is that outer void: it is blocked (reason 5). Uncovered cells enclosed by the cave (open room centres) stay as they are.
    Returns the number of walkable cells closed."""
    w, h = grid.w, grid.h
    cov = bytearray(w * h)
    for i in grid.wall:
        cov[i] = 1
    for i in grid.floor:
        cov[i] = 1
    void = bytearray(w * h); st = []
    for i in range(w * h):
        x, z = i % w, i // w
        if (x == 0 or z == 0 or x == w - 1 or z == h - 1) and not cov[i]:
            void[i] = 1; st.append(i)
    while st:
        c = st.pop(); x, z = c % w, c // w
        for d, ok in ((1, x < w - 1), (-1, x > 0), (w, z < h - 1), (-w, z > 0)):
            n = c + d
            if ok and not cov[n] and not void[n]:
                void[n] = 1; st.append(n)
    closed = 0
    for i in range(w * h):
        if void[i]:
            if grid.cells[i]:
                closed += 1
            grid.cells[i] = 0; grid.reason[i] = 5
    grid.void_cells = closed
    return closed


def grid_components(grid):
    w, h = grid.w, grid.h
    comp = [-1] * (w * h); n = 0
    for s in range(w * h):
        if not grid.cells[s] or comp[s] >= 0:
            continue
        comp[s] = n; st = [s]
        while st:
            c = st.pop(); x, z = c % w, c // w
            for d, ok in ((1, x < w - 1), (-1, x > 0), (w, z < h - 1), (-w, z > 0)):
                if ok and grid.cells[c + d] and comp[c + d] < 0:
                    comp[c + d] = n; st.append(c + d)
        n += 1
    return comp


def link_points(grid, start, points):
    """Required places (start, events, gates; local metres) that are not connected to the start region: open the cheapest chain of steep-slope cells (slope up to
    PASS_SLOPE, at most PASS_CELLS of them; water, object and outside cells never) between the point and the region. The original has no slope limit; the cliff
    rule stays everywhere else. Returns (linked, unlinked) point counts."""
    import heapq
    w, h = grid.w, grid.h; cs = grid.cs
    linked = unlinked = 0

    def nearest(x, z):
        gx, gz = int(x // cs), int(z // cs)
        if not (0 <= gx < w and 0 <= gz < h):
            return -1
        best = None
        for dz in range(-12, 13):
            for dx in range(-12, 13):
                a, b = gx + dx, gz + dz
                if 0 <= a < w and 0 <= b < h and grid.cells[b * w + a]:
                    d = dx * dx + dz * dz
                    if best is None or d < best[0]:
                        best = (d, b * w + a)
        return best[1] if best else -1
    sc = nearest(*start)
    if sc < 0:
        return 0, len(points)
    for (px, pz) in points:
        comp = grid_components(grid); main = comp[sc]
        c0 = nearest(px, pz)
        if c0 < 0 or comp[c0] == main:
            continue
        dist = {c0: 0.0}; prev = {}; pq = [(0.0, 0, c0)]; goal = -1
        while pq:
            d, nb, c = heapq.heappop(pq)
            if d > dist.get(c, 1e18):
                continue
            if grid.cells[c] and comp[c] == main:
                goal = c; break
            x, z = c % w, c // w
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                a, b = x + dx, z + dz
                if not (0 <= a < w and 0 <= b < h):
                    continue
                n = b * w + a
                if grid.cells[n]:
                    step = 0.0; nn = nb
                elif grid.reason[n] == 2 and grid.slope[n] <= PASS_SLOPE and nb < PASS_CELLS:
                    step = 1.0 + grid.slope[n]; nn = nb + 1
                else:
                    continue
                if d + step < dist.get(n, 1e18):
                    dist[n] = d + step; prev[n] = c; heapq.heappush(pq, (d + step, nn, n))
        if goal < 0:
            unlinked += 1
            continue
        c = goal
        while c != c0:
            if not grid.cells[c]:
                grid.cells[c] = 1; grid.reason[c] = 0; grid.carved_cells += 1
            c = prev[c]
        linked += 1
    return linked, unlinked


def finish_grid(grid, terr, water, max_slope, max_wade):
    """terrain-derived blocking: missing data, steep slope, deep water. Objects were rasterised earlier (reason 4 stays)."""
    cs = grid.cs; w, h = grid.w, grid.h
    heights = []
    for gz in range(h):
        z = (gz + 0.5) * cs
        for gx in range(w):
            x = (gx + 0.5) * cs
            hc = terr.height_at(x, z)
            i = gz * w + gx
            if hc is None:
                heights.append(-32768)
                grid.block(i, 1)
                continue
            heights.append(int(round(hc * 100)))
            hx1 = terr.height_at(min(x + cs / 2, grid.win.size_x - 1e-3), z); hx0 = terr.height_at(max(x - cs / 2, 0), z)
            hz1 = terr.height_at(x, min(z + cs / 2, grid.win.size_z - 1e-3)); hz0 = terr.height_at(x, max(z - cs / 2, 0))
            sl = 0.0
            if None not in (hx0, hx1):
                sl = max(sl, abs(hx1 - hx0) / cs)
            if None not in (hz0, hz1):
                sl = max(sl, abs(hz1 - hz0) / cs)
            if grid.slope is None:
                grid.slope = [0.0] * (w * h)
            grid.slope[i] = sl
            if sl > max_slope:
                grid.block(i, 2)
            for (x0, z0, x1, z1, wh) in water:
                if x0 <= x <= x1 and z0 <= z <= z1 and hc < wh - max_wade:
                    grid.block(i, 3)
    return heights


def reachable_count(grid, sx, sz):
    w, h = grid.w, grid.h
    gx, gz = int(sx // grid.cs), int(sz // grid.cs)
    if not (0 <= gx < w and 0 <= gz < h):
        return 0, False
    if not grid.cells[gz * w + gx]:                     # start cell blocked: nearest walkable
        best = None
        for r in range(1, 12):
            for dz in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    x, z = gx + dx, gz + dz
                    if 0 <= x < w and 0 <= z < h and grid.cells[z * w + x]:
                        best = (x, z); break
                if best: break
            if best: break
        if not best:
            return 0, False
        gx, gz = best
    seen = bytearray(w * h); seen[gz * w + gx] = 1
    stack = [(gx, gz)]; n = 0
    while stack:
        x, z = stack.pop(); n += 1
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = x + dx, z + dz
            if 0 <= a < w and 0 <= b < h and grid.cells[b * w + a] and not seen[b * w + a]:
                seen[b * w + a] = 1; stack.append((a, b))
    return n, True


# ------------------------------------------------------------------ gameplay tables
NPC_COLS = {2: 'walk_speed', 3: 'run_speed', 4: 'scale_pct', 5: 'weapon_r', 6: 'weapon_l', 7: 'level', 8: 'hp', 9: 'attack', 10: 'hit',
            11: 'defence', 12: 'resistance', 13: 'avoid', 14: 'attack_speed', 15: 'magic_dmg', 16: 'ai', 17: 'exp', 18: 'drop_row',
            19: 'money_drop_pct', 20: 'item_drop_pct', 25: 'untargetable', 26: 'attack_range', 27: 'npc_type', 42: 'height'}


def _num(s):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def npc_sidecar(vfs, ids):
    """STB/CHR/PART_NPC rows of the monster/NPC ids used in the window -> JSON-able dict (names in English, language index 1)."""
    out = dict(actors={}, notes='cols named after docs/ROSE_SPEC.md section 6; speeds in cm/s? (raw table values), scale in percent')
    try:
        stb = F.stb(vfs.read('3DDATA/STB/LIST_NPC.STB'))
    except Exception as e:
        out['error'] = 'LIST_NPC.STB: %s' % e
        return out
    keys = langs = None
    try:
        _, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_NPC_S.STL'))
    except Exception:
        pass
    chr_ = None
    try:
        chr_ = F.chr_(vfs.read('3DDATA/NPC/LIST_NPC.CHR'))
    except Exception:
        pass
    part_zsc = None
    try:
        part_zsc = F.zsc(vfs.read('3DDATA/NPC/PART_NPC.ZSC'))
    except Exception:
        pass
    for nid in sorted(ids):
        if nid >= stb['rows']:
            continue
        c = stb['cells'][nid]
        row = dict(id=nid, name_kr=c[0], mon_file=c[1] if len(c) > 1 else '')
        for ci, nm in NPC_COLS.items():
            if ci < len(c):
                row[nm] = _num(c[ci]) if c[ci] != '' else None
        sid = c[40].strip() if len(c) > 40 else ''
        row['name'] = row['name_kr']
        if keys and sid in keys:
            cells = TX.stl_cells((None, keys, langs), sid, 0)
            row['name'] = TX.pick(cells, TX.EN) or row['name_kr']
            row['name_ja'] = TX.pick(cells, TX.JA) or row['name']
            TX.note(TX.D_NPC, nid, 0, row['name'], row['name_ja'])
        if chr_:
            sk, mo, ef, npcs, _ok = chr_
            ch = npcs.get(nid)
            if ch:
                model = dict(skeleton=sk[ch['skel']], chr_name=ch['name'], model_ids=ch['models'],
                             motions=[dict(id=mid, file=mo[fi]) for mid, fi in ch['motions'] if fi < len(mo)],
                             effects=ch['effects'])
                if part_zsc:
                    model['parts'] = []
                    for oid in ch['models']:
                        if oid < len(part_zsc['objs']):
                            for mi, ma, pr in part_zsc['objs'][oid][0]:
                                model['parts'].append(dict(mesh=part_zsc['meshes'][mi], texture=part_zsc['mats'][ma][0],
                                                           skinned=bool(part_zsc['mats'][ma][1][0]), bone=pr.get('bone'), dummy=pr.get('dummy')))
                row['model'] = model
        out['actors'][str(nid)] = row
    return out


# ------------------------------------------------------------------ item / NPC summary (names via LIST_*_S.STL, language 1)
ITEM_TABLES = {1: 'LIST_FACEITEM', 2: 'LIST_CAP', 3: 'LIST_BODY', 4: 'LIST_ARMS', 5: 'LIST_FOOT', 6: 'LIST_BACK', 7: 'LIST_JEWEL', 8: 'LIST_WEAPON',
               9: 'LIST_SUBWPN', 10: 'LIST_USEITEM', 11: 'LIST_JEMITEM', 12: 'LIST_NATURAL', 13: 'LIST_QUESTITEM', 14: 'LIST_PAT'}
ITEM_TYPE_NAMES = {1: 'face', 2: 'cap', 3: 'body', 4: 'hands', 5: 'feet', 6: 'back', 7: 'jewel', 8: 'weapon', 9: 'subweapon', 10: 'consumable', 11: 'gem', 12: 'material', 13: 'quest', 14: 'vehicle'}


class ItemNames:
    def __init__(self, vfs):
        self.vfs = vfs
        self.cache = {}

    def name(self, code):
        t, n = divmod(code, 1000)
        tb = ITEM_TABLES.get(t)
        if not tb:
            return None
        if tb not in self.cache:
            try:
                stb = F.stb(self.vfs.read('3DDATA/STB/%s.STB' % tb))
            except KeyError:
                self.cache[tb] = None
                return None
            try:
                stl = F.stl(self.vfs.read('3DDATA/STB/%s_S.STL' % tb))
            except (KeyError, ValueError):
                stl = None
            self.cache[tb] = (stb, stl)
        c = self.cache[tb]
        if not c or n >= c[0]['rows']:
            return None
        row = c[0]['cells'][n]
        sid = row[-1] if row else ''
        if c[1] and sid in c[1][1]:
            cells = TX.stl_cells(c[1], sid, 0)
            en = TX.pick(cells, TX.EN)
            TX.note(TX.D_ITEM, code, 0, en, TX.pick(cells, TX.JA))
            return en
        return row[0] or None

    def info(self, code):
        """name / icon / price / weight / quality of an item code (ROSE_SPEC 5.1 columns 9 / 5 / 7 / 8), None when the row does not exist"""
        nm = self.name(code)
        t, n = divmod(code, 1000)
        c = self.cache.get(ITEM_TABLES.get(t))
        if not c or n >= c[0]['rows']:
            return None
        row = c[0]['cells'][n]
        g = lambda i: int(row[i]) if i < len(row) and row[i].strip().lstrip('-').isdigit() else 0
        return dict(name=(nm or '').strip(), icon=g(9), price=g(5), weight=g(7), quality=g(8))


def drop_table(vfs, items, row):
    """ITEM_DROP.STB row -> candidate list [{code, type, number, name, weight}]; weight = number of slots (of the non-empty ones) holding that item.
    Cells >= 1000 are base1000 item codes (type * 1000 + number); the small values 1..4 in the trailing columns are the table's redirects (ROSE_SPEC 6) and are not items."""
    try:
        stb = F.stb(vfs.read('3DDATA/STB/ITEM_DROP.STB'))
    except KeyError:
        return []
    if not (0 <= row < stb['rows']):
        return []
    cnt = collections.Counter()
    for c in stb['cells'][row][1:]:
        if c.strip().isdigit() and int(c) >= 1000:
            cnt[int(c)] += 1
    return [dict(code=k, type=ITEM_TYPE_NAMES.get(k // 1000, str(k // 1000)), number=k % 1000, name=items.name(k), weight=v) for k, v in sorted(cnt.items())]


def behaviour_rows(vfs, side_actors):
    """AIP summaries for the npc ids of `side_actors` (npc_sidecar output) -> (rows for NBEH, {id: summary dict}, warnings)"""
    import rose_ai
    try:
        ai = F.stb(vfs.read('3DDATA/STB/FILE_AI.STB'))
    except KeyError:
        return [], {}, ['FILE_AI.STB missing']
    rows = []; summ = {}; warn = []
    for key, a in sorted(side_actors.items(), key=lambda kv: int(kv[0])):
        nid = int(key)
        aid = a.get('ai') or 0
        path = ai['cells'][aid][0] if isinstance(aid, int) and 0 < aid < ai['rows'] else ''
        sm = None
        if path:
            try:
                sm = rose_ai.summarize(vfs.read(path))
            except (KeyError, rose_ai.AipError, struct.error) as e:
                warn.append('AIP %s: %s' % (path, e))
        ws = a.get('walk_speed') or 0
        fl = 0
        if sm:
            fl |= P.NB_HAS_AI
            fl |= P.NB_AGGRESSIVE if sm['aggressive'] else 0
            fl |= P.NB_APPROACHES if sm['approach_m'] else 0
            fl |= P.NB_WANDERS if sm['wander_m'] else 0
            fl |= P.NB_WANDER_RUN if sm['wander_run'] else 0
            fl |= P.NB_LEASH if sm['leash_m'] else 0
            fl |= P.NB_FLEES if sm['flee_hp_pct'] else 0
            fl |= P.NB_CALLS_ALLIES if sm['call_allies_m'] else 0
            fl |= P.NB_SKILLS if sm['uses_skills'] else 0
        if not ws:
            fl |= P.NB_STATIONARY
        sm = sm or dict(idle_interval_s=0, new_target_pct=0, aggro_m=0, approach_m=0, wander_m=0, leash_m=0, giveup_m=0, flee_hp_pct=0, call_allies_m=0)
        rows.append(dict(npc=nid, ai=aid if isinstance(aid, int) else 0, flags=fl, idle_interval=min(65535, sm['idle_interval_s']), new_target_pct=min(255, sm['new_target_pct']),
                         flee_hp_pct=min(255, sm['flee_hp_pct']), aggro_m=float(sm['aggro_m']), approach_m=float(sm['approach_m']), wander_m=float(sm['wander_m']),
                         leash_m=float(sm['leash_m']), giveup_m=float(sm['giveup_m']), call_allies_m=float(sm['call_allies_m'])))
        summ[nid] = {k: v for k, v in sm.items() if k != 'triggers'}
        summ[nid]['ai_file'] = path
    return rows, summ, warn


def npc_summary(vfs, side, spawns, ifos_npcs, behaviour):
    """Runtime-friendly JSON: one entry per monster / NPC type of the pack (names in English from the STL, drops resolved, behaviour, spawn counts)."""
    items = ItemNames(vfs)
    kinds = {}
    for sp in spawns:
        if sp['kind'] == P.KIND_SPAWN:
            for nid, c in sp['basic'] + sp['tactic']:
                e = kinds.setdefault(nid, dict(monster=True, spawn_points=set(), slots=0)); e['spawn_points'].add((sp['bx'], sp['by'], round(sp['x']), round(sp['z'])))
        else:
            kinds.setdefault(sp['npc'], dict(monster=False, spawn_points=set(), slots=0))['spawn_points'].add((sp['bx'], sp['by'], round(sp['x']), round(sp['z'])))
    out = dict(format='rose-npcs 1', language='English (STL language 1)', monsters={}, npcs={})
    for key, a in sorted(side['actors'].items(), key=lambda kv: int(kv[0])):
        nid = int(key)
        k = kinds.get(nid, dict(monster=True, spawn_points=set()))
        row = dict(id=nid, name=a.get('name'), name_ja=a.get('name_ja'), name_kr=a.get('name_kr'), level=a.get('level'), hp=a.get('hp'), attack=a.get('attack'), hit=a.get('hit'), defence=a.get('defence'),
                   resistance=a.get('resistance'), avoid=a.get('avoid'), attack_speed=a.get('attack_speed'), attack_range_cm=a.get('attack_range'),
                   walk_speed_cm_s=a.get('walk_speed'), run_speed_cm_s=a.get('run_speed'), scale_pct=a.get('scale_pct'), exp=a.get('exp'),
                   money_drop_pct=a.get('money_drop_pct'), item_drop_pct=a.get('item_drop_pct'), drop_row=a.get('drop_row'), npc_type=a.get('npc_type'),
                   magic_damage=bool(a.get('magic_dmg')), untargetable=bool(a.get('untargetable')), ai=a.get('ai'), spawn_points=len(k['spawn_points']),
                   behaviour=behaviour.get(nid))
        if k['monster'] and isinstance(a.get('drop_row'), int) and a.get('drop_row'):
            row['drops'] = drop_table(vfs, items, a['drop_row'])
        conv = ifos_npcs.get(nid)
        if conv:
            row['conversations'] = sorted(conv)
        (out['monsters'] if k['monster'] else out['npcs'])[str(nid)] = row
    return out


# ------------------------------------------------------------------ spawn scatter
class _Lcg:
    """tiny deterministic generator (same numbers on every Python version)"""

    def __init__(self, seed):
        self.s = (seed * 2654435761 + 12345) & 0xffffffff

    def next(self):
        self.s = (self.s * 1664525 + 1013904223) & 0xffffffff
        return (self.s >> 8) / float(1 << 24)


def scatter_slots(grid, x, z, half, n, seed, min_sep_cap=4.0):
    """n positions inside the square [x - half, x + half] x [z - half, z + half] on walkable grid cells, spread with a minimum separation (dart throwing, deterministic).
    -> (slots [(x, z)], flags, walkable cells in the square, min separation used)"""
    cs = grid.cs; w, h = grid.w, grid.h
    x0, x1 = max(0.0, x - half), min(grid.win.size_x - 1e-3, x + half)
    z0, z1 = max(0.0, z - half), min(grid.win.size_z - 1e-3, z + half)
    gx0, gx1 = int(x0 // cs), int(x1 // cs); gz0, gz1 = int(z0 // cs), int(z1 // cs)
    cand = [(gx, gz) for gz in range(gz0, gz1 + 1) for gx in range(gx0, gx1 + 1) if 0 <= gx < w and 0 <= gz < h and grid.cells[gz * w + gx]]
    flags = P.SPWR_SQUARE
    if not cand:
        return [(x, z)], flags | P.SPWR_NO_WALKABLE, 0, 0.0
    flags |= P.SPWR_ON_WALKABLE
    area = len(cand) * cs * cs
    sep = max(1.0, min(min_sep_cap, 0.8 * math.sqrt(area / max(1, n))))
    rng = _Lcg(seed)
    slots = []
    tries = 0
    cur = sep
    while len(slots) < n and tries < n * 60:
        tries += 1
        if tries % (n * 15) == 0:
            cur *= 0.7
        gx, gz = cand[int(rng.next() * len(cand)) % len(cand)]
        px = (gx + rng.next()) * cs; pz = (gz + rng.next()) * cs
        if not (x0 <= px <= x1 and z0 <= pz <= z1):
            continue
        if any((px - sx) ** 2 + (pz - sz) ** 2 < cur * cur for sx, sz in slots):
            continue
        slots.append((px, pz))
    while len(slots) < n:                                  # crowded: fall back to plain cell centres
        gx, gz = cand[int(rng.next() * len(cand)) % len(cand)]
        slots.append(((gx + 0.5) * cs, (gz + 0.5) * cs))
    return slots, flags, len(cand), sep


def spawn_rules(spawns, grid):
    """SPWN-ordered rule dicts for rpg1.enc_spwr. Monster spawn points: IFO range (metres, half side of a square - the server rolls +-range on both axes),
    interval (s), limit (max alive), tactic points; the first wave is basic[0].count (what the server spawns on its first tick). NPCs get one slot (their point)."""
    rules = []
    for i, sp in enumerate(spawns):
        if sp['kind'] != P.KIND_SPAWN:
            rules.append(dict(range=0.0, respawn=0.0, limit=1, tactic_points=0, first_wave=1, flags=P.SPWR_ON_WALKABLE, slots=[(sp['x'], sp['z'])], walk_cells=0, min_sep=0.0))
            continue
        n = max(1, min(64, sp['limit']))
        slots, fl, wc, sep = scatter_slots(grid, sp['x'], sp['z'], float(max(1, sp['range'])), n, i * 7919 + sp['bx'] * 131 + sp['by'])
        rules.append(dict(range=float(sp['range']), respawn=float(sp['interval']), limit=min(65535, sp['limit']), tactic_points=min(65535, sp['tp']),
                          first_wave=min(65535, sp['basic'][0][1]) if sp['basic'] else 0, flags=fl, slots=slots, walk_cells=wc, min_sep=sep))
    return rules


# ------------------------------------------------------------------ top level
def convert(vfs, zone, center=None, size=5, cell=2.5, terrain_step=2, max_slope=1.0, max_wade=1.0, tile_cols=8, log=print,
            chars=True, avatar=True, anim_fps=C.FPS_OUT, with_actj=False, window=None, atlas_mips=ATLAS_MIPS, atlas_slot=SLOT_PX,
            env=True, lightmaps=True, sky_px=1024, spawn_scatter=True, ui=False, audio=True, audio_opts=None, lobby=None):
    """zone: dict(id, name, zon, start, deco_zsc, cnst_zsc[, cells]). window = (bx0, by0, nx, ny) or 'all' (bounding box of every block) overrides center / size.
    -> (pack bytes, manifest dict, sidecar dict). The sidecar holds the actor table ('actors') and the runtime NPC summary ('summary')."""
    TX.reset()          # the language overlay (TXJA / TXEN, docs/RPG1_PACK.md section 26) collects the texts the sections below read
    zon_path = rosevfs.norm(zone['zon'])
    zone_dir = zon_path.rsplit('/', 1)[0] + '/'
    zon = F.zon(vfs.read(zon_path))
    have = existing_blocks(vfs, zone_dir)
    if not have:
        raise ValueError('no HIM blocks under %s' % zone_dir)
    if window == 'all':
        window = bounding_window(have)
    if window is not None:
        bx0, by0, nx, ny = window
        win = Window(bx0, by0, nx, ny)
    else:
        bx0, by0, n = choose_window(have, center, size)
        win = Window(bx0, by0, n)
    log('zone %s "%s": window blocks x %d..%d, y %d..%d (%d x %d)' % (zone['id'], zone['name'], win.bx0, win.bx0 + win.nx - 1, win.by0, win.by0 + win.ny - 1, win.nx, win.ny))

    terr = Terrain(vfs, zone_dir, win)
    if not terr.have:
        raise ValueError('window has no terrain')
    # --- tiles used
    used_tex = set()
    for b, til in terr.til.items():
        for ti in set(til):
            t = zon['tiles'][ti]
            used_tex.add(t[0] + t[2]); used_tex.add(t[1] + t[3])
    if not terr.til:
        t = zon['tiles'][0]; used_tex.add(t[0] + t[2]); used_tex.add(t[1] + t[3])
    atlas, slot_of = build_atlas(vfs, zon, used_tex, tile_cols, atlas_mips, atlas_slot)
    log('atlas: %d textures, %dx%d DXT5, %d mips (%d bytes)' % (len(slot_of), atlas['w'], atlas['h'], atlas['mips'], sum(len(l) for l in atlas['levels'])))
    tblocks, ttil_blocks = build_terrain(terr, zon, slot_of, atlas, terrain_step, win)

    # --- ifo
    ifos = {}
    for b in win.blocks():
        p = '%s%d_%d.IFO' % (zone_dir, b[0], b[1])
        if rosevfs.norm(p) in vfs.files:
            ifos[b] = F.ifo(vfs.read(p))
    grid = Grid(win, cell, terr)
    grid.per_sample = bool(zone.get('underground'))
    ob = ObjBuilder(vfs, win, grid)
    zsc_d = F.zsc(vfs.read(zone['deco_zsc'])) if zone.get('deco_zsc') and rosevfs.norm(zone['deco_zsc']) in vfs.files else None
    zsc_c = F.zsc(vfs.read(zone['cnst_zsc'])) if zone.get('cnst_zsc') and rosevfs.norm(zone['cnst_zsc']) in vfs.files else None
    lp = LightPages(vfs, zone_dir) if lightmaps else None
    skipped = collections.Counter()
    for b, o in sorted(ifos.items()):
        for t, zsc, zid in ((1, zsc_d, 'deco'), (3, zsc_c, 'cnst')):
            lit = lp.lit_for(b[0], b[1], zid) if lp else {}
            for li, obj in enumerate(o.get(t, [])):
                if zsc is None:
                    skipped['no zsc for type %d' % t] += 1
                    continue
                lm = None
                parts = lit.get(li + 1)
                if parts:
                    lm = {}
                    for pt in parts:
                        pg = lp.page(b[0], b[1], pt['file'])
                        if pg and pt['ppr'] > 0:
                            lm[pt['part']] = (pg, pt['ppr'], pt['slot'])
                ob.add_instance(zid, zsc, obj, b, lm)
        for t, nm in ((6, 'animated objects'), (4, 'sound objects'), (5, 'effect objects'), (10, 'warps'), (11, 'collision objects'), (12, 'event objects')):
            skipped[nm] += len(o.get(t, []))
    log('objects: %d instances, %d chunks, %d materials, %d textures, %d lightmapped parts' % (ob.stats['instances'], len(ob.chunk_list()), len(ob.mats), len(ob.textures),
                                                                                               ob.stats['lightmapped parts']))

    # --- water + grid
    wplanes, water_tile = rose_env.water_planes(ifos, win)
    water = [(p['x0'], p['z0'], p['x1'], p['z1'], p['y']) for p in wplanes]
    heights = finish_grid(grid, terr, water, max_slope, max_wade)
    under = bool(zone.get('underground'))
    if under:                                    # DUNGEON: before the floors, so no rock top chains to the outer void
        seal_interior(grid)
        log('walk grid: underground zone, %d cells outside the cave closed' % grid.void_cells)
    add_floors(grid, heights, water, max_wade, INTERIOR_FLOOR_REACH if under else FLOOR_REACH)
    reasons = collections.Counter(grid.reason)

    # --- start + spawns
    start = None
    for nm, p in zon['events']:
        if nm == zone['start']:
            start = p
    if start is None and zon['events']:
        start = zon['events'][0][1]
    sx, sy, sz = win.to_local(start[0], start[2], start[1]) if start else (win.size_x / 2, 0, win.size_z / 2)
    # ZON events are (x, height, y): to_local takes (x, y, z)
    start_note = 'zone start event'
    if not (0.0 <= sx < win.size_x and 0.0 <= sz < win.size_z):          # window without the start event: first revive point inside, else the window centre
        start_note = 'window centre (start event outside the window)'
        sx, sy, sz = win.size_x / 2, 0.0, win.size_z / 2
        for nm, p in zon['events']:
            if rose_env.classify_event(nm) == P.EV_RESTORE:
                x, y, z = win.to_local(p[0], p[2], p[1])
                if 0.0 <= x < win.size_x and 0.0 <= z < win.size_z:
                    sx, sy, sz = x, y, z; start_note = 'revive point %s (start event outside the window)' % nm
                    break
        h0 = terr.height_at(sx, sz)
        if h0 is not None:
            sy = h0
    spawns = []; npc_ids = set(); npc_conv = collections.defaultdict(set)
    for b, o in sorted(ifos.items()):
        for obj in o.get(8, []):
            x, y, z = win.to_local(*obj['pos'])
            basic = [(nid, cnt) for _n, nid, cnt in obj['basic']]; tactic = [(nid, cnt) for _n, nid, cnt in obj['tactic']]
            npc_ids.update(i for i, _ in basic + tactic)
            spawns.append(dict(kind=P.KIND_SPAWN, npc=0, x=x, y=y, z=z, yaw=facing_yaw_deg(obj['q']), bx=b[0], by=b[1], basic=basic, tactic=tactic,
                               interval=obj['interval'], limit=obj['limit'], range=obj['range'], tp=obj['tp']))
        for obj in o.get(2, []):
            x, y, z = win.to_local(*obj['pos'])
            npc_ids.add(obj['id'])
            if obj.get('quest'):
                npc_conv[obj['id']].add(obj['quest'])
            spawns.append(dict(kind=P.KIND_NPC, npc=obj['id'], x=x, y=y, z=z, yaw=facing_yaw_deg(obj['q']), bx=b[0], by=b[1], basic=[], tactic=[],
                               interval=0, limit=0, range=0, tp=0))
    import rose_skills      # lane SUMMONS: the npcs of the summon skills / summon capsules are actors of every zone (a summon needs its model; skeleton budget in rose_skills)
    try:
        summon_ids, summon_warn = rose_skills.zone_summon_ids(vfs, npc_ids)
    except Exception as e:
        summon_ids, summon_warn = [], ['SKDB summons: %s' % e]
    for w_ in summon_warn:
        log('summons: ' + w_)
    if summon_ids:
        log('summons: %d summon npcs added as actors' % len(summon_ids))
        npc_ids.update(summon_ids)
    side = npc_sidecar(vfs, npc_ids)
    cb = None
    if chars:
        cb = RC.CharBuilder(vfs, ob, out_fps=anim_fps)
        try:
            chr_t = F.chr_(vfs.read('3DDATA/NPC/LIST_NPC.CHR'))
            pz = F.zsc(vfs.read('3DDATA/NPC/PART_NPC.ZSC'))
            wz = F.zsc(vfs.read('3DDATA/WEAPON/LIST_WEAPON.ZSC')) if rosevfs.norm('3DDATA/WEAPON/LIST_WEAPON.ZSC') in vfs.files else None
            swz = F.zsc(vfs.read('3DDATA/WEAPON/LIST_SUBWPN.ZSC')) if rosevfs.norm('3DDATA/WEAPON/LIST_SUBWPN.ZSC') in vfs.files else None
            cb.add_actors(side['actors'], chr_t, pz, wz, swz)
        except KeyError as e:
            cb.warn['no NPC tables: %s' % e] += 1
        if avatar:
            try:
                cb.add_avatar()
            except KeyError as e:
                cb.warn['no avatar tables: %s' % e] += 1
        log('characters: %d skeletons, %d meshes, %d clips, %d actors%s' % (len(cb.skels), len(cb.meshes), len(cb.clips), len(cb.actors),
                                                                           ', avatar %d items' % len(cb.avatar['items']) if cb.avatar else ''))
    # --- events + gates (also needed by the grid: every place the original lets the player reach must be connected to the start region)
    warns = collections.Counter()
    evs = []; warps = []
    if env:
        evs = rose_env.zone_events(zon, win)
        warps = rose_warp.zone_warps(vfs, zone['id'], ifos, win, ob, spawns, zone_row, (quat_wxyz_to_mat, mat_scale_cols, mat_mul, mat_vec), warns)
    req = [(e['x'], e['z']) for e in evs if e['inside']] + [(w_['cx'], w_['cz']) for w_ in warps if w_['inside']]
    linked, unlinked = link_points(grid, (sx, sz), req) if req else (0, 0)
    reasons = collections.Counter(grid.reason)
    log('walk grid: %d cells opened on object floors, %d steep cells opened for %d far places (%d still apart)' % (grid.floor_cells, grid.carved_cells, linked, unlinked))
    reach, _ok = reachable_count(grid, sx, sz)
    walk = sum(1 for c in grid.cells if c)

    # --- 1.2 sections: spawn rules, behaviour, events, water, sky, object colours
    extra = []; info = {}
    rules = spawn_rules(spawns, grid) if spawn_scatter else None
    if rules is not None:
        extra.append((b'SPWR', 1, len(rules), P.enc_spwr(rules)))
    beh_rows, beh_summary, bw = behaviour_rows(vfs, side['actors'])
    for w_ in bw:
        warns[w_] += 1
    if beh_rows:
        extra.append((b'NBEH', 1, len(beh_rows), P.enc_nbeh(beh_rows)))
    import rose_aips        # AIPS (rose_aips.py): the decoded AI scripts for the runtime's 1:1 monster behaviour; optional, never fails the conversion, no version stamp
    try:
        ai_sec, ai_warn, ai_info = rose_aips.build_section(vfs, side['actors'], F.stb)
    except Exception as e:
        ai_sec, ai_warn, ai_info = None, ['AIPS: %s' % e], {}
    for w_ in ai_warn:
        warns[w_] += 1
    if ai_sec is not None:
        extra.append(ai_sec)
        info['aips'] = ai_info
    if env:
        if evs:
            extra.append((b'EVNT', 1, len(evs), P.enc_evnt(evs)))
        revv = rose_revive.zone_revive(zon, win, zone.get('cells', []), zone['id'])          # lane death: REVV = revive points by the LIST_ZONE names
        if revv:
            extra.append((b'REVV', 1, len(revv), rose_revive.enc_revv(revv)))
        if warps:
            extra.append((b'WARP', 1, len(warps), P.enc_warp(warps)))
            info['warps'] = [dict(kind=w['kind'], warp_id=w['warp_id'], target_zone=w['target_zone'], target_event=w['target_event'], inside=w['inside'],
                                  centre=[round(w['cx'], 2), round(w['cz'], 2)], half=[round(w['hx'], 2), round(w['hz'], 2)], yaw=round(w['yaw'], 1)) for w in warps]
        if wplanes:
            try:
                fmt, fw, fh, frames = rose_env.water_frames(vfs)
                extra.append((b'WATR', 1, len(wplanes), P.enc_watr(wplanes, frames, fmt, fw, fh, rose_env.WATER_FPS, water_tile)))
            except (KeyError, ValueError) as e:
                warns['water frames: %s' % e] += 1
                extra.append((b'WATR', 1, len(wplanes), P.enc_watr(wplanes, [], D.FMT_DXT1, 0, 0, rose_env.WATER_FPS, water_tile)))
        if zone.get('cells') is not None:
            try:
                sky, sky_info = rose_env.read_sky(vfs, zone['cells'], sky_px)
                extra.append((b'SKYB', 1, 1, P.enc_skyb(sky)))
                info['sky'] = sky_info
            except (KeyError, ValueError) as e:
                warns['sky: %s' % e] += 1
    if lobby is not None:       # LOBY (rose_lobby.py): character select positions + camera path of the login scene; written with or without --no-env
        lb, linfo = rose_lobby.lobby_section(vfs, win, lobby.get('positions'), lobby.get('camera'), warns)
        if lb is not None:
            extra.append((b'LOBY', 1, linfo['positions'], lb))
        info['lobby'] = linfo
    chunks = ob.chunk_list()
    if lightmaps and any(c['lit'] for c in chunks):
        extra.append((b'OCOL', 1, len(chunks), P.enc_ocol([bytes(c['cols']) if c['lit'] else None for c in chunks])))
    col_pieces, col_ranges = collider_pieces(chunks)
    if col_pieces:          # OCLD: physics colliders of the collision parts (docs/RPG1_PACK.md section 21), written with or without --no-env
        extra.append((b'OCLD', 1, len(col_pieces), P.enc_ocld(col_pieces, col_ranges, OCLD_MAX_TRIS)))

    ys = [v[1] for blk in tblocks for v in blk['verts']]
    lvl = collections.Counter(round(p['y'], 2) for p in wplanes)
    area = dict(zone_id=zone['id'], blocks_x=win.nx, blocks_y=win.ny, first_bx=win.bx0, first_by=win.by0, block_size=BLOCK_M, grid_w=grid.w, grid_h=grid.h,
                cell_size=cell, origin_x=0.0, origin_z=0.0, world_off_x=win.x0, world_off_z=win.z0, start_x=sx, start_y=sy, start_z=sz,
                bmin_x=0.0, bmin_y=min(ys), bmin_z=0.0, bmax_x=win.size_x, bmax_y=max(ys), bmax_z=win.size_z,
                height_unit=0.01, water_level=lvl.most_common(1)[0][0] if lvl else 0.0, atlas_gutter_px=atlas['gutter'], terrain_step=terrain_step)
    recs, ents, nsp, nent = P.enc_spawns(spawns)
    actj = json.dumps(side, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    sections = [
        (b'AREA', 1, 1, P.enc_area(area)),
        (b'CELL', 1, grid.w * grid.h, bytes(grid.cells)),
        (b'HGHT', 1, grid.w * grid.h, P.enc_hght(heights)),
        (b'TERR', 1, len(tblocks), P.enc_terr(tblocks)),
        (b'TATL', 1, len(atlas['slots']), P.enc_tatl(atlas['fmt'], atlas['w'], atlas['h'], atlas['levels'], atlas['slot_px'], atlas['gutter'], atlas['slots'])),
        (b'TTIL', 1, len(zon['tiles']), P.enc_ttil([(slot_of[t[0] + t[2]], slot_of[t[1] + t[3]], t[5], t[4]) if (t[0] + t[2]) in slot_of and (t[1] + t[3]) in slot_of else (0, 0, 1, 0) for t in zon['tiles']], ttil_blocks)),
        (b'OTEX', 1, len(ob.textures), P.enc_otex(ob.textures)),
        (b'OMAT', 1, len(ob.mats), P.enc_omat(ob.mats)),
        (b'OMSH', 1, len(chunks), P.enc_omsh(chunks)),
        (b'SPWN', 1, nsp, recs),
        (b'SPWE', 1, nent, ents),
    ]
    if cb:
        sections += cb.sections()
    if with_actj or not cb or not cb.actors:
        sections.append((b'ACTJ', 1, len(side['actors']), actj))
    sections += extra
    import rose_gameplay
    gp_sections, gp_warn = rose_gameplay.build_sections(vfs, side['actors'], items=ItemNames(vfs), monsters=set(
        nid for sp in spawns if sp['kind'] == P.KIND_SPAWN for nid, _c in sp['basic'] + sp['tactic']))
    sections += gp_sections
    for w_ in gp_warn:
        log('gameplay tables: ' + w_)
    import rose_items       # ITEM (rose_items.py): item database + start records for the runtime's stats / equip / potions; optional, never fails the conversion
    try:
        it_sections, it_warn = rose_items.build_section(vfs, ItemNames(vfs))
    except Exception as e:
        it_sections, it_warn = [], ['ITEM: %s' % e]
    sections += it_sections
    for w_ in it_warn:
        log('item tables: ' + w_)
    import rose_skills      # SKDB (rose_skills.py): skill database + LIST_STATUS for the runtime's skill window / learning / effects (lane SKILLS); optional, never fails the conversion
    try:
        sk_sections, sk_warn = rose_skills.build_section(vfs)
    except Exception as e:
        sk_sections, sk_warn = [], ['SKDB: %s' % e]
    sections += sk_sections
    for w_ in sk_warn:
        log('skill tables: ' + w_)
    import rose_shop       # SHOP (rose_shop.py): NPC shop tabs + price inputs for the runtime shop window; optional, never fails the conversion
    try:
        sh_sections, sh_warn = rose_shop.build_section(vfs, set(sp['npc'] for sp in spawns if sp['kind'] == P.KIND_NPC))
    except Exception as e:
        sh_sections, sh_warn = [], ['SHOP: %s' % e]
    sections += sh_sections
    for w_ in sh_warn:
        log('shop tables: ' + w_)
    import rose_itemx       # ITEX (rose_itemx.py): item extras (grade bonuses, rare type, gem abilities, names + descriptions, tooltip words) for the items-complete lane; optional, never fails the conversion
    try:
        ix_sections, ix_warn = rose_itemx.build_section(vfs)
    except Exception as e:
        ix_sections, ix_warn = [], ['ITEX: %s' % e]
    sections += ix_sections
    for w_ in ix_warn:
        log('item extras: ' + w_)
    import rose_quest       # QUST (rose_quest.py): quests + NPC dialogs of the zone (reachable QSD triggers, translated CON functions, texts); optional, never fails the conversion
    try:
        q_sections, q_info = rose_quest.build_section(vfs, ifos, set(nid for sp in spawns if sp['kind'] == P.KIND_SPAWN for nid, _c in sp['basic'] + sp['tactic']))
    except Exception as e:
        q_sections, q_info = [], dict(warnings={'QUST: %s' % e: 1})
    sections += q_sections
    info['quests'] = {k: v for k, v in q_info.items() if k != 'warnings'}
    for w_, n_ in sorted(q_info.get('warnings', {}).items()):
        log('quests: %s (x%d)' % (w_, n_))
    audio_info = None
    if audio:       # RPG1 1.5 SNDS (rose_audio.py): zone music + core sound effects from the client's Sound folder; never fails the conversion
        import rose_audio
        a_sections, audio_info, a_warn = rose_audio.build_sections(vfs, zone, monsters=set(
            nid for sp in spawns if sp['kind'] == P.KIND_SPAWN for nid, _c in sp['basic'] + sp['tactic']), side_actors=side['actors'], **(audio_opts or {}))
        sections += a_sections
        for w_ in a_warn:
            log('audio: ' + w_)
    import rose_minimap     # MMAP (rose_minimap.py): the zone's minimap image + world-to-map transform + arrow + NPC markers for the DlgMinimap map; optional, never fails the conversion
    try:
        mm_sections, mm_info, mm_warn = rose_minimap.build_section(vfs, zone, zon, spawns, win)
    except Exception as e:
        mm_sections, mm_info, mm_warn = [], {}, ['MMAP: %s' % e]
    sections += mm_sections
    if mm_info:
        info['minimap'] = mm_info
    for w_ in mm_warn:
        log('minimap: ' + w_)
    if ui:      # RPG1 1.4 UIAT + UIDL (rose_ui.py): zone independent, about 6.5 MB; normally shipped as its own pack (rose_ui.py -o)
        import rose_ui
        ui_sections, ui_info = rose_ui.build_ui(vfs, vfs.root)
        sections += ui_sections
        for w_ in ui_info['warnings'][:5]:
            log('ui tables: ' + w_)
    try:                    # TXJA / TXEN: Japanese + the English texts without a legacy home; optional, never fails the conversion (old runtimes ignore the tags and read English)
        TX.global_texts(vfs, lambda nm: _stl_or_none(vfs, nm))
        sections += TX.current().sections()
    except Exception as e:
        log('language overlay: %s' % e)
    pack = P.write_pack(sections)
    pinfo = P.read_pack(pack)
    char_bytes = None
    if cb:
        char_bytes = {t.decode(): pinfo['sections'][t]['size'] for t in (b'SKEL', b'CMSH', b'CANM', b'ACTR', b'AVAT') if t in pinfo['sections']}
        char_bytes['textures (in OTEX/OMAT)'] = cb.texture_bytes()
        char_bytes['total'] = sum(char_bytes.values())
    if len(pack) > BUDGET_BYTES:
        log('warning: pack is %.1f MB, above the %d MB budget' % (len(pack) / 2 ** 20, BUDGET_BYTES // 2 ** 20))
    side['summary'] = npc_summary(vfs, side, spawns, npc_conv, beh_summary)
    man = dict(
        format='RPG1', version='%d.%d' % (pinfo['version'] >> 16, pinfo['version'] & 0xffff), tool='rose_pack.py', tool_version=TOOL_VERSION,
        zone=dict(id=zone['id'], name=zone['name'], zon=zon_path),
        window=dict(first_bx=win.bx0, first_by=win.by0, size=win.n, blocks_x=win.nx, blocks_y=win.ny, blocks_present=len(terr.have),
                    start_block='%d_%d' % win.block_of_file(start[0], start[2]) if start else None, start_source=start_note),
        options=dict(cell=cell, terrain_step=terrain_step, max_slope=max_slope, max_wade=max_wade, atlas_mips=atlas['mips'], atlas_slot=atlas['slot_px'],
                     env=env, lightmaps=lightmaps, sky_px=sky_px),
        pack=dict(bytes=len(pack), sha256=hashlib.sha256(pack).hexdigest()),
        sections=[dict(tag=t.decode(), offset=pinfo['sections'][t]['off'], bytes=pinfo['sections'][t]['size'], count=pinfo['sections'][t]['count'],
                       crc32='%08x' % pinfo['sections'][t]['crc'], sha256=hashlib.sha256(pinfo['sections'][t]['data']).hexdigest()) for t in pinfo['order']],
        stats=dict(terrain_blocks=len(tblocks), terrain_verts=sum(len(b['verts']) for b in tblocks), terrain_tris=sum(len(b['tris']) for b in tblocks),
                   atlas_textures=len(slot_of), atlas_bytes=sum(len(l) for l in atlas['levels']), object_instances=ob.stats['instances'], object_chunks=len(chunks),
                   object_verts=sum(len(c['verts']) for c in chunks), object_tris=sum(len(c['idx']) // 3 for c in chunks), object_textures=len(ob.textures),
                   object_materials=len(ob.mats), lightmapped_parts=ob.stats['lightmapped parts'], lightmapped_chunks=sum(1 for c in chunks if c['lit']),
                   collision_parts=ob.stats['collision parts'], collider_parts=ob.stats['collider parts'], collider_pieces=len(col_pieces),
                   collider_tris=sum(p[4] for p in col_pieces), collider_ranges=len(col_ranges), grid=[grid.w, grid.h], walkable_cells=walk,
                   blocked_by=dict(outside=reasons[1], slope=reasons[2], water=reasons[3], object=reasons[4], interior=reasons[5]), underground=bool(zone.get('underground')), floor_cells=grid.floor_cells, carved_cells=grid.carved_cells,
                   unlinked_places=unlinked, reachable_from_start=reach,
                   spawns=sum(1 for s in spawns if s['kind'] == 0), npcs=sum(1 for s in spawns if s['kind'] == 1), actors=len(side['actors']),
                   spawn_slots=sum(len(r['slots']) for r in rules) if rules else 0, water_planes=len(wplanes), water_levels=dict((str(k), v) for k, v in lvl.items()),
                   characters=None if not cb else dict(skeletons=len(cb.skels), meshes=len(cb.meshes), mesh_verts=sum(len(m['verts']) for m in cb.meshes),
                                                       mesh_tris=sum(len(m['idx']) // 3 for m in cb.meshes), clips=len(cb.clips),
                                                       clip_frames=sum(c['frames'] for c in cb.clips), anim_fps=anim_fps, actors=len(cb.actors),
                                                       avatar_items=len(cb.avatar['items']) if cb.avatar else 0)),
        env=info, audio=audio_info,
        character_bytes=char_bytes, budget=dict(limit_bytes=BUDGET_BYTES, ok=len(pack) <= BUDGET_BYTES),
        skipped=dict(skipped), warnings=dict(ob.warn, **{('chars: ' + k): v for k, v in (cb.warn if cb else {}).items()}, **{('lit: ' + k): v for k, v in (lp.warn if lp else {}).items()}, **warns),
    )
    return pack, man, side


def _stl_or_none(vfs, name):
    try:
        return F.stl(vfs.read('3DDATA/STB/%s.STL' % name))
    except (KeyError, ValueError, struct.error, IndexError):
        return None


def slug(name):
    return re.sub(r'[^a-z0-9]+', '', name.lower()) or 'zone'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('client', help='ROSE client directory (data.idx + *.VFS), read only')
    ap.add_argument('-o', '--out', default=None, help='output .rpg1 (default out/<zone name>.rpg1)')
    ap.add_argument('--zone', type=int, default=None, help='LIST_ZONE row (default 20 = Birth Island; 22 = Adventurer\'s Plain; --lobby: from the lobby config)')
    ap.add_argument('--center', default=None, help='block bx_by to centre a square --size window on (default: block of the zone start event)')
    ap.add_argument('--size', type=int, default=None, help='square window size in blocks (when neither --blocks nor --all is given; default 5, --lobby 2)')
    ap.add_argument('--blocks', default=None, help='rectangular window x0:x1,y0:y1 (inclusive block coordinates, e.g. 31:37,30:33) or a single block bx_by')
    ap.add_argument('--all', action='store_true', help='every block of the zone (bounding rectangle of the existing blocks)')
    ap.add_argument('--cell', type=float, default=2.5, help='walkability cell size in metres')
    ap.add_argument('--terrain-step', type=int, default=2, choices=(1, 2, 4), help='HIM cells per terrain quad (2 = 5 m)')
    ap.add_argument('--max-slope', type=float, default=1.0, help='blocked above this rise/run')
    ap.add_argument('--max-wade', type=float, default=1.0, help='blocked where water is deeper than this (m)')
    ap.add_argument('--atlas-mips', type=int, default=ATLAS_MIPS, help='mip levels of the terrain atlas (1.1 packs: 2)')
    ap.add_argument('--atlas-slot', type=int, default=SLOT_PX, help='atlas slot pitch in px (256 + 2 x gutter; 320 for 4 mips, 272 for the 1.1 layout with 2 mips)')
    ap.add_argument('--sky-px', type=int, default=1024, help='sky texture size cap (px)')
    ap.add_argument('--no-env', action='store_true', help='skip WATR / SKYB / EVNT / WARP / REVV')
    ap.add_argument('--no-lightmaps', action='store_true', help='skip the OCOL object lightmap vertex colours')
    ap.add_argument('--no-chars', action='store_true', help='skip the character sections (SKEL CMSH CANM ACTR AVAT); writes the ACTJ JSON instead')
    ap.add_argument('--no-avatar', action='store_true', help='characters of the window only, no player avatar')
    ap.add_argument('--anim-fps', type=int, default=C.FPS_OUT, help='fixed animation sample rate (retail ZMO files are 30 fps)')
    ap.add_argument('--no-audio', action='store_true', help='skip the SNDS section (zone music + sound effects, RPG1 1.5)')
    ap.add_argument('--music-seconds', type=float, default=None, help='length of the looped zone music in seconds (default %g)' % AU.MUSIC_SECONDS)
    ap.add_argument('--music-rate', type=int, default=None, help='music sample rate in Hz, mono (default %d)' % AU.MUSIC_RATE)
    ap.add_argument('--night-music', action='store_true', help='also pack the night track when it differs from the day track')
    ap.add_argument('--audio-budget', type=float, default=None, help='size cap of the SNDS section in MB (default %g)' % (AU.AUDIO_BUDGET / 2 ** 20))
    ap.add_argument('--ui', action='store_true', help='also embed the UI sections UIAT + UIDL (RPG1 1.4, +6.5 MB; see tools/rose_ui.py for the standalone UI pack)')
    ap.add_argument('--actj', action='store_true', help='also write the legacy ACTJ JSON section')
    ap.add_argument('--lobby', action='store_true', help='character select pack: the login zone of the lobby config, a small window around the select camera, the LOBY section, out/%s.rpg1' % LOBBY_NAME)
    ap.add_argument('--lobby-config', default=None, help='lobby config JSON outside the repo (zone, camera, positions_file; default: $%s)' % rose_lobby.CONFIG_ENV)
    ap.add_argument('--lobby-positions', default=None, help='JSON file with the slot positions from your own client (rose_lobby.load_positions); without it they are derived from the camera')
    ap.add_argument('--lobby-camera', default=None, help='select camera ZMO inside the client (overrides the config)')
    a = ap.parse_args(argv)
    if a.lobby:
        try:
            lcfg = rose_lobby.resolve(a.zone, a.lobby_camera, a.lobby_positions, a.lobby_config)
        except ValueError as e:
            raise SystemExit(str(e))
        a.zone, a.lobby_camera, a.lobby_positions = lcfg['zone'], lcfg['camera'], lcfg['positions']
        if not a.lobby_camera:
            print('warning: no select camera configured ("camera" in the lobby config): no camera path, positions only from the positions file')
    if a.zone is None:
        a.zone = 20
    vfs = rosevfs.Vfs(a.client)
    if a.size is None:
        a.size = 2 if a.lobby else 5
    zone = zone_row(vfs, a.zone)
    zon = F.zon(vfs.read(zone['zon']))
    window = None; center = None
    if a.all:
        window = 'all'
    elif a.blocks:
        window = parse_blocks(a.blocks)
    elif a.center:
        center = tuple(int(x) for x in a.center.split('_'))
    elif a.lobby and rose_lobby.lobby_center_block(vfs, a.lobby_positions, a.lobby_camera) is not None:
        center = rose_lobby.lobby_center_block(vfs, a.lobby_positions, a.lobby_camera)
    else:
        st = [p for nm, p in zon['events'] if nm == zone['start']]
        if not st:
            raise SystemExit('zone has no start event; give --center, --blocks or --all')
        center = Window(0, 0, 1).block_of_file(st[0][0], st[0][2])   # ZON event = (x, height, y)
    out = a.out or os.path.join('out', (LOBBY_NAME if a.lobby else slug(zone['name'])) + '.rpg1')
    pack, man, side = convert(vfs, zone, center, a.size, a.cell, a.terrain_step, a.max_slope, a.max_wade,
                              chars=not a.no_chars, avatar=not a.no_avatar, anim_fps=a.anim_fps, with_actj=a.actj, window=window,
                              atlas_mips=a.atlas_mips, atlas_slot=a.atlas_slot, env=not a.no_env, lightmaps=not a.no_lightmaps, sky_px=a.sky_px, ui=a.ui,
                              audio=not a.no_audio, audio_opts={k: v for k, v in dict(music_seconds=a.music_seconds, music_rate=a.music_rate, night_music=a.night_music or None,
                                                                                         budget=None if a.audio_budget is None else int(a.audio_budget * 2 ** 20)).items() if v is not None},
                              lobby=dict(positions=a.lobby_positions, camera=a.lobby_camera) if a.lobby else None)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'wb') as f:
        f.write(pack)
    base = os.path.splitext(out)[0]
    summary = side.pop('summary', None)
    with open(base + '.manifest.json', 'w') as f:
        json.dump(man, f, indent=1, sort_keys=True)
    with open(base + '.actors.json', 'w') as f:
        json.dump(side, f, indent=1, sort_keys=True, ensure_ascii=False)
    if summary:
        with open(base + '.npcs.json', 'w') as f:
            json.dump(summary, f, indent=1, sort_keys=True, ensure_ascii=False)
    print('wrote %s (%.2f MB, RPG1 %s)' % (out, len(pack) / 1e6, man['version']))
    for s in man['sections']:
        print('  %s %9d bytes  count %d' % (s['tag'], s['bytes'], s['count']))
    print(json.dumps(man['stats'], indent=1))


if __name__ == '__main__':
    main()
