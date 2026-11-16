"""Skill / spell / hit effects for the Unity runtime (GoldSrcVR GsvrRoseFx, lane EFFECTS, docs/design/rose_effects.md): section EFCT in the AVATAR pack.

The original client plays an effect file (.EFT) at the cast, on the flying bullet and at the hit of a skill. An EFT lists particle systems (.PTL: emitters with a
texture, blend, emit rate, life, emit radius, gravity and keyframes for size / colour / alpha / velocity / texture frame / rotation) and animated effect meshes.
This module reads them from the player's own client and writes one compact, fixed-stride table the runtime simulates with a pooled quad buffer.

Source tables (read only, rosefmt.stb cell indices = rose-offline column numbers):
  FILE_EFFECT.STB  row -> cell 1 the EFT path (an "effect file id")
  LIST_EFFECT.STB  row = an "effect id": cells 9 hit normal (file id), 10 hit critical, 11 bullet (file id), 13 bullet move type, 15 bullet speed (cm/s)
  LIST_SKILL.STB   cells 56 / 59 / 62 / 65 casting effect file ids (57 / 60 / 63 / 66 dummy bone, 999 = none), 71 bullet effect id (LIST_EFFECT), 74 hit effect file id,
                   77 / 80 hit dummy effect file ids             [rose-offline rose-data-irose skill_database.rs]
  LIST_NPC.STB     cell 33 hand hit effect id (LIST_EFFECT), 34 die effect file id       [npc_database.rs]
  LIST_WEAPON.STB  cell 38 bullet effect id, 39 hit effect id (both LIST_EFFECT)          [item_database.rs]
  EFT / PTL layouts: rose-offline rose-file-readers eft.rs / ptl.rs (ROSE_SPEC 3.13); the simulation rules: rose-offline-client particle_sequence_system.rs.

Units in the section: Unity metres, effect-local axes with +Z = the actor's front (file (x, y, z) cm -> (-x, z, -y) / 100: the file -> Unity swap of rose_pack
plus the 180 degree turn the packed characters are baked with), real seconds. rose-offline ages particles with 4.8 x dt (and integrates velocity with it); that factor
is folded in here: times / 4.8, velocities x 4.8 / 100, gravity x 4.8 / 100 (x 4.8 again for world-space emitters, which add 4.8 x gravity). Keyframe start times are
the middle of their (min, max) range (the original draws one per emitter instance), sorted, and each keyframe carries the index of the next keyframe of the same type
with the fade flag (the interpolation target) - so the runtime only walks the list.

Layout (little endian, 4-byte aligned):
  header 96 = 24 x u32: 0 version (1) ; 1 fileCount ; 2 fileOff ; 3 emitCount ; 4 emitOff ; 5 keyCount ; 6 keyOff ; 7 skillCount ; 8 skillOff ; 9 npcCount ; 10 npcOff ;
                        11 weaponCount ; 12 weaponOff ; 13 atlasFmt (2 = DXT5) ; 14 atlasW ; 15 atlasH ; 16 atlasLevels ; 17 atlasOff ; 18 atlasBytes ;
                        19 cellPx ; 20 cellCols ; 21 cellCount ; 22 0 ; 23 0
  file 16 (sorted by id): u16 fileId ; u16 emitFirst ; u16 emitCount ; u16 flags (1 an emitter loops forever, 2 mesh parts approximated) ; f32 duration (s, 0 = endless) ; u32 0
  emitter 128: 0 u16 kind (0 particles, 1 mesh part as one textured quad) ; 2 u16 mode (0 additive, 1 alpha) ; 4 u16 cell (0xFFFF = none) ; 6 u8 atlasCols ; 7 u8 atlasRows ;
               8 u16 keyFirst ; 10 u16 keyCount ; 12 u16 maxParticles ; 14 u16 coords (0 world, 1 local position, 2 local) ; 16 i32 loops (<= 0 endless) ;
               20 f32 delay ; 24 lifeLo ; 28 lifeHi ; 32 emitLo ; 36 emitHi (particles / s) ; 40 radiusLo xyz ; 52 radiusHi xyz ; 64 gravLo xyz ; 76 gravHi xyz (m / s2) ;
               88 pos xyz ; 100 yaw (deg) ; 104 meshSize (m, half edge) ; 108 meshDur (s) ; 112 meshRepeat ; 116 align (0 camera facing, 1 flat on the ground, 2 upright about Y) ; 120 0 ; 124 0
  keyframe 48: u16 type ; u8 fade ; u8 0 ; i16 nextFade (absolute key index, -1 none) ; u16 0 ; f32 t (s) ; f32 v[9] as (lo, hi) pairs:
               1 size (half edge x, y m) ; 2 timer (s) ; 3 r ; 4 g ; 5 b ; 6 a ; 7 rgba ; 8 vel x ; 9 vel y (up) ; 10 vel z (front) ; 11 vel xyz (m / s) ; 12 texture frame ; 13 rotation (deg)
  skill 16 (sorted by id): u16 skillId ; u16 cast[2] (file ids) ; u16 bullet (file id) ; u16 hit (file id) ; u16 hitDummy (file id) ; u16 bulletSpeed (cm / s) ; u16 0
  npc 8 (sorted): u16 npcId ; u16 hit (file id) ; u16 die (file id) ; u16 0
  weapon 8 (sorted): u16 weaponId ; u16 hit (file id) ; u16 bullet (file id) ; u16 bulletSpeed (cm / s)
  atlas: DXT5 levels, cells of cellPx (128) in rows of cellCols (16), cell i at (i % cols, i / cols), row 0 = top (Texture2D.LoadRawTextureData as is)

Only file ids that resolve to a parsed EFT with at least one drawable part are written; references to anything else are 0 (the runtime then shows nothing).
No game data is committed; everything is read at conversion time on the player's machine.
"""
import math
import struct

import rosefmt as F
import rosedds as D

TAG_EFCT = b'EFCT'
VERSION = 1
HDR = struct.Struct('<24I')
FILE = struct.Struct('<HHHHfI')
EMIT = struct.Struct('<HHHBBHHHHi 25f 2I')
KEY = struct.Struct('<HBBhHf9f')
SKILL = struct.Struct('<8H')
NPC = struct.Struct('<4H')
WEAPON = struct.Struct('<4H')
assert HDR.size == 96 and FILE.size == 16 and EMIT.size == 128 and KEY.size == 48 and SKILL.size == 16 and NPC.size == 8

TIME_SCALE = 4.8            # rose-offline particle_sequence_system: particles age 4.8 x dt
CELL_PX = 128
CELL_COLS = 16
MAX_CELLS = 256             # atlas 2048 x 2048 at most (5.3 MB DXT5 incl. mips; the retail skills + npcs + weapons need 156 cells = 2048 x 1280, 3.5 MB)
ATLAS_LEVELS = 4            # 128 -> 16 px per cell; cells stay separate in every level
MAX_FILES = 1024
MAX_EMITTERS_PER_FILE = 16
MAX_EMITTERS = 4096
MAX_KEYS = 32767
MAX_PARTICLES = 128         # per emitter (the runtime's whole pool is smaller; the budget clips further)
FMT_DXT5 = 2
K_SIZE, K_TIMER, K_R, K_G, K_B, K_A, K_RGBA, K_VX, K_VY, K_VZ, K_VXYZ, K_TEX, K_ROT = range(1, 14)
KEY_FLOATS = {1: 4, 2: 2, 3: 2, 4: 2, 5: 2, 6: 2, 7: 8, 8: 2, 9: 2, 10: 2, 11: 6, 12: 2, 13: 2}
MODE_ADD, MODE_ALPHA = 0, 1


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
        return int(v) if v.lstrip('-').isdigit() else default
    except (IndexError, ValueError, TypeError, OverflowError):
        return default


F_LIMIT = 1.0e6


def fin(x, lim=F_LIMIT):
    """a float for the section: NaN / inf -> 0, clamped to +-lim (a broken PTL never reaches the runtime as NaN)"""
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    return max(-lim, min(lim, x))


def _u16(v):
    return max(0, min(65535, int(v)))


def _norm(p):
    return p.replace('\\', '/').strip()


def _null(p):
    return not p or p.upper() == 'NULL'


# ------------------------------------------------------------------ EFT / PTL readers
class _Rd(F.R):
    def str32(self):
        n = self.u32()
        if n > 4096 or self.p + n > len(self.d):
            raise ValueError('string past the end')
        return F.dec(self.raw(n))

    def skip_str(self):
        n = self.u32()
        if n > 1 << 20 or self.p + n > len(self.d):
            raise ValueError('block past the end')
        self.p += n


def parse_eft(d):
    """EFT bytes -> dict(particles=[...], meshes=[...]) (rose-file-readers eft.rs); raises ValueError / struct.error on a bad file"""
    r = _Rd(d)
    r.skip_str()                                  # name
    r.u32()                                         # use sound
    r.str32()                                       # sound file (no retail EFT uses it, rose-offline effect_loader)
    r.u32()
    parts, meshes = [], []
    n = r.u32()
    if n > 256:
        raise ValueError('too many particle parts')
    for _ in range(n):
        r.skip_str(); r.skip_str(); r.p += 4
        ptl = r.str32()
        r.u32(); r.str32(); r.u32(); r.p += 4       # use animation, animation file, repeat (transform animation: not used)
        pos = r.vec(3); pitch, yaw, roll = r.vec(3); r.p += 4
        delay = r.u32(); linked = r.u32()
        parts.append(dict(ptl=_norm(ptl), pos=pos, pitch=pitch, yaw=yaw, roll=roll, delay=delay, linked=linked))
    n = r.u32()
    if n > 256:
        raise ValueError('too many mesh parts')
    for _ in range(n):
        r.skip_str(); r.skip_str(); r.p += 4
        mesh = r.str32(); manim = r.str32(); tex = r.str32()
        alpha, two, atest, ztest, zwrite, src, dst, op = struct.unpack_from('<8I', d, r.p); r.p += 32
        r.u32(); r.str32(); r.u32(); r.p += 4
        pos = r.vec(3); pitch, yaw, roll = r.vec(3); r.p += 4
        delay = r.u32(); repeat = r.u32(); linked = r.u32()
        meshes.append(dict(mesh=_norm(mesh), anim='' if _null(manim) else _norm(manim), tex=_norm(tex), src=src, dst=dst, op=op, alpha=alpha,
                           pos=pos, pitch=pitch, yaw=yaw, roll=roll, delay=delay, repeat=repeat, linked=linked))
    if r.p > len(d):
        raise ValueError('EFT past the end')
    return dict(particles=parts, meshes=meshes)


def parse_ptl(d):
    """PTL bytes -> list of sequence dicts (rose-file-readers ptl.rs); raises ValueError on an unknown keyframe type"""
    r = _Rd(d)
    n = r.u32()
    if n > 64:
        raise ValueError('too many sequences')
    out = []
    for _ in range(n):
        name = r.str32()
        life = r.vec(2); emit = r.vec(2); loops, = struct.unpack_from('<i', d, r.p); r.p += 4
        r.vec(6)                                    # spawn direction (unused by the original)
        rad = r.vec(6); grav = r.vec(6)
        tex = r.str32()
        num, align, coords, cols, rows, sprite, dst, src, op = struct.unpack_from('<iIIIIIIII', d, r.p); r.p += 36
        nk = r.u32()
        if nk > 512:
            raise ValueError('too many keyframes')
        keys = []
        for _ in range(nk):
            typ = r.u32(); t0, t1 = r.vec(2); fade = r.u8()
            if typ not in KEY_FLOATS:
                raise ValueError('bad keyframe type %d' % typ)
            keys.append(dict(type=typ, t=(t0, max(t0, t1)), fade=1 if fade else 0, v=r.vec(KEY_FLOATS[typ])))
        out.append(dict(name=name, life=life, emit=emit, loops=loops, rad=rad, grav=grav, tex=_norm(tex), num=num, align=align,
                        coords=coords if coords in (0, 1, 2) else 2, cols=max(1, cols), rows=max(1, rows), src=src, dst=dst, op=op, keys=keys))
    if r.p > len(d):
        raise ValueError('PTL past the end')
    return out


# ------------------------------------------------------------------ conversion rules (pure, unit tested)
def rng(lo, hi):
    """the original's random range: frandom * |hi - lo| + lo -> the (lo, hi) the runtime lerps between"""
    return (float(lo), float(lo) + abs(float(hi) - float(lo)))


def neg(r):
    return (-r[1], -r[0])


def axes(lo3, hi3, scale):
    """file (x, y, z) ranges -> Unity (-x, z, -y) ranges, each scaled; -> (lo xyz, hi xyz)"""
    x = rng(lo3[0] * scale, hi3[0] * scale); y = rng(lo3[1] * scale, hi3[1] * scale); z = rng(lo3[2] * scale, hi3[2] * scale)
    x, uy, uz = neg(x), z, neg(y)
    return (x[0], uy[0], uz[0]), (x[1], uy[1], uz[1])


def pos_unity(p):
    return (-p[0] / 100.0, p[2] / 100.0, -p[1] / 100.0)


def blend_mode(src, dst):
    """PTL / EFT blend factors (1 zero, 2 one, 5 src alpha, 6 inv src alpha ...) -> MODE_ADD / MODE_ALPHA (the runtime's premultiplied quad shader does both)"""
    if dst in (2, 4, 9) or (src == 2 and dst == 2):
        return MODE_ADD
    return MODE_ALPHA


def convert_key(k):
    """one PTL keyframe -> (unity type, fade, t seconds, [9 floats as (lo, hi) pairs]); velocity axes swapped like positions"""
    typ, v = k['type'], k['v']
    t = (k['t'][0] + k['t'][1]) * 0.5 / TIME_SCALE
    vs = TIME_SCALE / 100.0
    if typ == K_SIZE:
        out = list(rng(v[0] / 100.0, v[2] / 100.0)) + list(rng(v[1] / 100.0, v[3] / 100.0))
    elif typ == K_TIMER:
        out = [x / TIME_SCALE for x in rng(v[0], v[1])]
    elif typ == K_RGBA:
        out = []
        for c in range(4):
            out += list(rng(v[c], v[4 + c]))
    elif typ == K_VX:
        out = list(neg(rng(v[0] * vs, v[1] * vs)))
    elif typ == K_VY:            # file y (north) -> Unity -z
        typ, out = K_VZ, list(neg(rng(v[0] * vs, v[1] * vs)))
    elif typ == K_VZ:            # file z (up) -> Unity y
        typ, out = K_VY, list(rng(v[0] * vs, v[1] * vs))
    elif typ == K_VXYZ:
        x = neg(rng(v[0] * vs, v[3] * vs)); y = rng(v[1] * vs, v[4] * vs); z = rng(v[2] * vs, v[5] * vs)
        out = list(x) + list(z) + list(neg(y))
    else:
        out = list(rng(v[0], v[1]))
    return typ, k['fade'], t, (out + [0.0] * 9)[:9]


def order_keys(keys):
    """converted keys [(type, fade, t, v)] -> sorted by t (stable) with nextFade: index of the first later key of the same type with fade, else -1
    (rose-offline ParticleSequence::from)"""
    ks = sorted(keys, key=lambda k: k[2])
    out = []
    for i, k in enumerate(ks):
        nxt = -1
        for j in range(i + 1, len(ks)):
            if ks[j][1] and ks[j][0] == k[0]:
                nxt = j
                break
        out.append((k[0], k[1], k[2], k[3], nxt))
    return out


def emitter_duration(seq_life_hi, emit_lo, loops, num, delay):
    """seconds until a finite emitter's last particle is gone: delay + emit time of loops x num particles at the slowest rate + the longest life; -1 = endless (loops <= 0
    with particles). An emitter without particles (num 0) is finite: it ends at its delay."""
    if num <= 0:
        return max(0.0, delay)
    if loops <= 0:
        return -1.0
    rate = max(emit_lo, 0.01)
    return delay + loops * num / rate + seq_life_hi


# ------------------------------------------------------------------ texture atlas
class Atlas:
    """particle / mesh textures -> cells of CELL_PX in one DXT5 atlas (row 0 = top). add() -> cell index or None (missing / bad / full)"""

    def __init__(self, read, max_cells=MAX_CELLS):
        self.read = read
        self.cells = {}           # path upper -> index
        self.rgba = []            # RGBA8 CELL_PX x CELL_PX per cell
        self.max = max_cells
        self.missing = 0

    def add(self, path):
        k = path.upper()
        if k in self.cells:
            return self.cells[k]
        if len(self.rgba) >= self.max:
            return None
        try:
            px = cell_rgba(D.parse_dds(self.read(path)))
        except Exception:          # one bad / missing texture: that part is skipped
            self.missing += 1
            self.cells[k] = None
            return None
        self.cells[k] = len(self.rgba)
        self.rgba.append(px)
        return self.cells[k]

    def encode(self):
        """-> (w, h, [DXT5 level bytes]) or None without cells"""
        n = len(self.rgba)
        if n == 0:
            return None
        cols = min(CELL_COLS, n)
        rows = (n + CELL_COLS - 1) // CELL_COLS
        w, h = cols * CELL_PX, rows * CELL_PX
        img = bytearray(w * h * 4)
        for i, px in enumerate(self.rgba):
            cx, cy = (i % CELL_COLS) * CELL_PX, (i // CELL_COLS) * CELL_PX
            for y in range(CELL_PX):
                o = ((cy + y) * w + cx) * 4
                img[o:o + CELL_PX * 4] = px[y * CELL_PX * 4:(y + 1) * CELL_PX * 4]
        levels = []
        lw, lh, cur = w, h, bytes(img)
        for lv in range(ATLAS_LEVELS):
            levels.append(D.encode_dxt5(cur, lw, lh))
            if lv + 1 < ATLAS_LEVELS:
                cur, lw, lh = D.downsample2(cur, lw, lh)
        return w, h, levels


def resize_nearest(px, w, h, nw, nh):
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        sy = min(h - 1, y * h // nh)
        for x in range(nw):
            sx = min(w - 1, x * w // nw)
            o = (sy * w + sx) * 4
            out[(y * nw + x) * 4:(y * nw + x) * 4 + 4] = px[o:o + 4]
    return bytes(out)


def cell_rgba(dds):
    """parse_dds result -> RGBA8 CELL_PX x CELL_PX: the mip level closest to (not below) the cell size, then box halving / nearest scaling"""
    w, h = dds['w'], dds['h']
    lv = 0
    while lv + 1 < len(dds['levels']) and max(1, w >> (lv + 1)) >= CELL_PX and max(1, h >> (lv + 1)) >= CELL_PX:
        lv += 1
    lw, lh = max(1, w >> lv), max(1, h >> lv)
    data = dds['levels'][lv]
    px = data if dds['fmt'] == 'RGBA' else D.decode_dxt(dds['fmt'], data, lw, lh)
    while lw >= 2 * CELL_PX and lh >= 2 * CELL_PX:
        px, lw, lh = D.downsample2(px, lw, lh)
    if (lw, lh) != (CELL_PX, CELL_PX):
        px = resize_nearest(px, lw, lh, CELL_PX, CELL_PX)
    return bytes(px)


# ------------------------------------------------------------------ builder
class Builder:
    def __init__(self, read, atlas=None):
        self.read = read
        self.atlas = atlas or Atlas(read)
        self.files = {}           # file id -> record dict or None
        self.emitters = []
        self.keys = []
        self.ptl_cache = {}
        self.key_cache = {}
        self.warn = {}
        self.paths = {}

    def _w(self, k):
        self.warn[k] = self.warn.get(k, 0) + 1

    def _ptl(self, path):
        k = path.upper()
        if k not in self.ptl_cache:
            try:
                self.ptl_cache[k] = parse_ptl(self.read(path))
            except Exception:          # one bad / missing PTL: its parts are skipped
                self.ptl_cache[k] = None
                self._w('PTL missing or unreadable')
        return self.ptl_cache[k]

    def _mesh_info(self, m):
        """-> (size m, flat, duration s) of an effect mesh: horizontal extent of the ZMS, flat when it is much wider than tall, ZMO frames / fps"""
        size, flat, dur = 1.0, 0, 1.0
        try:
            z = F.zms(self.read(m['mesh']))
            pts = z.get('pos') or []
            if pts:
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
                k = 1.0 if z['ver'] < 7 else 0.01          # rosefmt.zms gives metres for old versions, centimetres for ZMS0007/8
                ex, ey, ez = (max(xs) - min(xs)) * k, (max(ys) - min(ys)) * k, (max(zs) - min(zs)) * k
                size = max(0.2, min(20.0, max(ex, ey, ez)))
                flat = 1 if ez < 0.25 * max(ex, ey, 1e-3) else 0
        except Exception:
            self._w('effect mesh missing or unreadable')
        if m['anim']:
            try:
                a = F.zmo(self.read(m['anim']))
                if a['fps'] > 0 and a['nf'] > 0:
                    dur = a['nf'] / float(a['fps'])
            except Exception:
                pass
        return size, flat, max(0.1, min(10.0, dur))

    def add_file(self, fid, path):
        """FILE_EFFECT row -> True when the effect has a drawable part (then it is in the table)"""
        if fid in self.files:
            return self.files[fid] is not None
        self.files[fid] = None
        if len([f for f in self.files.values() if f]) >= MAX_FILES:
            return False
        try:
            eft = parse_eft(self.read(path))
        except Exception:          # one bad / missing EFT: that effect is not in the table
            self._w('EFT missing or unreadable')
            return False
        first = len(self.emitters)
        dur, endless, flags = 0.0, False, 0
        for p in eft['particles']:
            seqs = self._ptl(p['ptl']) if p['ptl'] else None
            for si, s in enumerate(seqs or ()):
                if len(self.emitters) - first >= MAX_EMITTERS_PER_FILE or len(self.emitters) >= MAX_EMITTERS:
                    self._w('emitters above the per-effect budget')
                    break
                cell = self.atlas.add(s['tex']) if s['tex'] else None
                if cell is None:
                    self._w('particle texture missing')
                    continue
                ck = (p['ptl'].upper(), si)                 # emitters of the same PTL sequence share its keyframes
                if ck in self.key_cache:
                    kf, nk = self.key_cache[ck]
                else:
                    ks = order_keys([convert_key(k) for k in s['keys']])
                    if len(self.keys) + len(ks) > MAX_KEYS:
                        self._w('keyframes above the budget')
                        continue
                    kf, nk = len(self.keys), len(ks)
                    self.keys += [(k[0], k[1], k[2], k[3], k[4] + kf if k[4] >= 0 else -1) for k in ks]
                    self.key_cache[ck] = (kf, nk)
                delay = p['delay'] / 1000.0
                life = rng(s['life'][0] / TIME_SCALE, s['life'][1] / TIME_SCALE)
                emit = rng(s['emit'][0], s['emit'][1])
                gs = TIME_SCALE / 100.0 * (TIME_SCALE if s['coords'] == 0 else 1.0)
                rlo, rhi = axes(s['rad'][0:3], s['rad'][3:6], 0.01)
                glo, ghi = axes(s['grav'][0:3], s['grav'][3:6], gs)
                num = max(0, min(MAX_PARTICLES, s['num']))
                d = emitter_duration(life[1], emit[0], s['loops'], num, delay)
                if d < 0:
                    endless = True
                dur = max(dur, d)
                self.emitters.append(dict(kind=0, mode=blend_mode(s['src'], s['dst']), cell=cell, cols=min(255, s['cols']), rows=min(255, s['rows']),
                                          key_first=kf, key_count=nk, num=num, coords=s['coords'], loops=s['loops'], delay=delay, life=life, emit=emit,
                                          rlo=rlo, rhi=rhi, glo=glo, ghi=ghi, pos=pos_unity(p['pos']), yaw=-p['yaw'], msize=0.0, mdur=0.0, mrep=0.0, align=s['align'] if s['align'] in (0, 1, 2) else 0))
        for m in eft['meshes']:
            if len(self.emitters) - first >= MAX_EMITTERS_PER_FILE or len(self.emitters) >= MAX_EMITTERS:
                break
            cell = self.atlas.add(m['tex']) if m['tex'] else None
            if cell is None:
                self._w('mesh texture missing')
                continue
            size, flat, mdur = self._mesh_info(m)
            rep = max(1, min(8, m['repeat'] or 1))
            delay = m['delay'] / 1000.0
            dur = max(dur, delay + mdur * rep)
            flags |= 2
            self.emitters.append(dict(kind=1, mode=blend_mode(m['src'], m['dst']), cell=cell, cols=1, rows=1, key_first=0, key_count=0, num=1, coords=2, loops=1,
                                      delay=delay, life=(mdur, mdur), emit=(0.0, 0.0), rlo=(0, 0, 0), rhi=(0, 0, 0), glo=(0, 0, 0), ghi=(0, 0, 0),
                                      pos=pos_unity(m['pos']), yaw=-m['yaw'], msize=size * 0.5, mdur=mdur, mrep=float(rep), align=1 if flat else 0))
        n = len(self.emitters) - first
        if n == 0:
            return False
        if endless:
            flags |= 1
            dur = 0.0
        self.files[fid] = dict(id=fid, first=first, count=n, flags=flags, dur=dur)
        return True


def resolve_file(b, fe_paths, fid):
    """file id -> fid when it resolves to a drawable effect, else 0"""
    if fid <= 0 or fid > 65535 or fid not in fe_paths:
        return 0
    return fid if b.add_file(fid, fe_paths[fid]) else 0


def effect_parts(le, eid):
    """LIST_EFFECT row -> (hit normal file, hit critical file, bullet file, bullet speed cm/s); zeros for a bad row"""
    if le is None or eid <= 0 or eid >= le['rows']:
        return 0, 0, 0, 0
    c = le['cells'][eid]
    return _int(c, 9), _int(c, 10), _int(c, 11), _int(c, 15)


def skill_effects(c):
    """LIST_SKILL row cells -> dict(cast=[up to 4 file ids], bullet_effect (LIST_EFFECT row), hit (file id), hit_dummy=[2 file ids])"""
    return dict(cast=[_int(c, 56 + 3 * k) for k in range(4)], bullet_effect=_int(c, 71), hit=_int(c, 74), hit_dummy=[_int(c, 77), _int(c, 80)])


def build_tables(read, file_stb, list_stb, skill_stb, npc_stb=None, weapon_stb=None, atlas=None):
    """-> (skills, npcs, weapons, builder). Pure apart from read(path) -> bytes (KeyError when missing)."""
    fe = {}
    for r in range(file_stb['rows']):
        c = file_stb['cells'][r]
        if len(c) > 1 and str(c[1]).strip():
            fe[r] = _norm(str(c[1]))
    b = Builder(read, atlas)
    skills = []
    for sid in range(1, min(skill_stb['rows'], 65536)):
        se = skill_effects(skill_stb['cells'][sid])
        casts = [x for x in (resolve_file(b, fe, f) for f in se['cast']) if x][:2]
        hn, _hc, bul, speed = effect_parts(list_stb, se['bullet_effect'])
        bullet = resolve_file(b, fe, bul)
        hit = resolve_file(b, fe, se['hit']) or resolve_file(b, fe, hn)
        hd = [x for x in (resolve_file(b, fe, f) for f in se['hit_dummy']) if x]
        if casts or bullet or hit or hd:
            skills.append(dict(id=sid, cast=(casts + [0, 0])[:2], bullet=bullet, hit=hit, hit_dummy=hd[0] if hd else 0, speed=_u16(speed)))
    npcs = []
    if npc_stb is not None:
        for n in range(1, min(npc_stb['rows'], 65536)):
            c = npc_stb['cells'][n]
            hit = resolve_file(b, fe, effect_parts(list_stb, _int(c, 33))[0])
            die = resolve_file(b, fe, _int(c, 34))
            if hit or die:
                npcs.append(dict(id=n, hit=hit, die=die))
    weapons = []
    if weapon_stb is not None:
        for n in range(1, min(weapon_stb['rows'], 65536)):
            c = weapon_stb['cells'][n]
            hit = resolve_file(b, fe, effect_parts(list_stb, _int(c, 39))[0])
            _h, _c, bul, speed = effect_parts(list_stb, _int(c, 38))
            bullet = resolve_file(b, fe, bul)
            if hit or bullet:
                weapons.append(dict(id=n, hit=hit, bullet=bullet, speed=_u16(speed)))
    return skills, npcs, weapons, b


def enc_efct(b, skills, npcs=(), weapons=()):
    files = sorted((f for f in b.files.values() if f), key=lambda f: f['id'])
    fb = b''.join(FILE.pack(_u16(f['id']), f['first'], f['count'], f['flags'], fin(f['dur']), 0) for f in files)
    eb = bytearray()
    for e in b.emitters:
        fl = [e['delay'], e['life'][0], e['life'][1], e['emit'][0], e['emit'][1], *e['rlo'], *e['rhi'], *e['glo'], *e['ghi'], *e['pos'], e['yaw'], e['msize'], e['mdur'], e['mrep'], float(e['align'])]
        eb += EMIT.pack(e['kind'], e['mode'], 0xFFFF if e['cell'] is None else e['cell'], e['cols'], e['rows'], e['key_first'], e['key_count'], e['num'], e['coords'],
                        max(-2 ** 31, min(2 ** 31 - 1, int(e['loops']))), *[fin(x) for x in fl], 0, 0)
    kb = b''.join(KEY.pack(k[0], k[1], 0, k[4], 0, fin(k[2]), *[fin(x) for x in k[3]]) for k in b.keys)
    sb = b''.join(SKILL.pack(s['id'], s['cast'][0], s['cast'][1], s['bullet'], s['hit'], s['hit_dummy'], s['speed'], 0) for s in sorted(skills, key=lambda s: s['id']))
    nb = b''.join(NPC.pack(n['id'], n['hit'], n['die'], 0) for n in sorted(npcs, key=lambda n: n['id']))
    wb = b''.join(WEAPON.pack(w['id'], w['hit'], w['bullet'], w['speed']) for w in sorted(weapons, key=lambda w: w['id']))
    at = b.atlas.encode()
    ab = b''.join(at[2]) if at else b''
    off = HDR.size
    fo = off; off += len(fb)
    eo = off; off += len(eb)
    ko = off; off += len(kb)
    so = off; off += len(sb)
    no = off; off += len(nb)
    wo = off; off += len(wb)
    ao = off
    hdr = HDR.pack(VERSION, len(files), fo, len(b.emitters), eo, len(b.keys), ko, len(skills), so, len(npcs), no, len(weapons), wo,
                   FMT_DXT5 if at else 0, at[0] if at else 0, at[1] if at else 0, len(at[2]) if at else 0, ao, len(ab), CELL_PX, CELL_COLS, len(b.atlas.rgba), 0, 0)
    return hdr + fb + bytes(eb) + kb + sb + nb + wb + ab


def dec_efct(d):
    """-> dict(files, emitters, keys, skills, npcs, weapons, atlas=(fmt, w, h, levels, bytes, cell, cols, cells)); same bounds as GsvrRoseFx.Valid. ValueError on a bad layout."""
    if len(d) < HDR.size:
        raise ValueError('EFCT shorter than its header')
    h = HDR.unpack_from(d, 0)
    if h[0] != VERSION:
        raise ValueError('EFCT version %d' % h[0])

    def tab(n, o, st, mx):
        if n > mx or (n and (o < HDR.size or o + n * st.size > len(d))):
            raise ValueError('bad EFCT table')
        return [st.unpack_from(d, o + i * st.size) for i in range(n)]
    files = tab(h[1], h[2], FILE, MAX_FILES)
    emit = tab(h[3], h[4], EMIT, MAX_EMITTERS)
    keys = tab(h[5], h[6], KEY, MAX_KEYS)
    skills = tab(h[7], h[8], SKILL, 65535)
    npcs = tab(h[9], h[10], NPC, 65535)
    weapons = tab(h[11], h[12], WEAPON, 65535)
    for t in (files, skills, npcs, weapons):
        ids = [r[0] for r in t]
        if ids != sorted(set(ids)):
            raise ValueError('EFCT table not sorted')
    for f in files:
        if f[1] + f[2] > len(emit):
            raise ValueError('EFCT file emitters out of range')
    for e in emit:
        if e[5] + e[6] > len(keys):
            raise ValueError('EFCT emitter keys out of range')
        if e[2] >= h[21]:
            raise ValueError('EFCT emitter cell outside the atlas')
        for k in range(e[5], e[5] + e[6]):
            if not 1 <= keys[k][0] <= 13 or (keys[k][3] != -1 and not k < keys[k][3] < e[5] + e[6]):
                raise ValueError('EFCT keyframe type / fade target out of range')
    if h[17] + h[18] > len(d):
        raise ValueError('EFCT atlas past the end')
    return dict(files=files, emitters=emit, keys=keys, skills=skills, npcs=npcs, weapons=weapons, atlas=(h[13], h[14], h[15], h[16], h[18], h[19], h[20], h[21]))


def _stb(vfs, name):
    """a table of the client; None when the client has none (no section / that part skipped). A table that is there but does not parse raises (the avatar build fails)."""
    try:
        d = vfs.read('3DDATA/STB/%s.STB' % name)
    except KeyError:
        return None
    return F.stb(d)


def coverage(skill_stb, skills, player_rule=None):
    """-> (player skills, player skills with an effect, all rows with an effect). player_rule(cells) -> bool (default: rose_skills' player row rule)"""
    have = set(s['id'] for s in skills)
    if player_rule is None:
        def player_rule(c):
            return _int(c, 5) > 0 and _int(c, 51) > 0 and len(c) > 4 and str(c[4]).strip().isdigit()
    pl = [sid for sid in range(1, skill_stb['rows']) if player_rule(skill_stb['cells'][sid])]
    return len(pl), len([s for s in pl if s in have]), len(have)


def build_section(vfs, log=print):
    """-> (sections, info). Missing tables: no FILE_EFFECT / LIST_SKILL = no section (warning). A present table that does not parse raises (rose_avatar_pack fails the build)."""
    fs, ls, sk = _stb(vfs, 'FILE_EFFECT'), _stb(vfs, 'LIST_EFFECT'), _stb(vfs, 'LIST_SKILL')
    if fs is None or sk is None:
        return [], dict(warnings={'EFCT: FILE_EFFECT / LIST_SKILL missing': 1})
    skills, npcs, weapons, b = build_tables(vfs.read, fs, ls, sk, _stb(vfs, 'LIST_NPC'), _stb(vfs, 'LIST_WEAPON'))
    if not b.emitters:
        return [], dict(warnings={'EFCT: no drawable effect': 1})
    data = enc_efct(b, skills, npcs, weapons)
    np_, npfx, nall = coverage(sk, skills)
    info = dict(effects=len([f for f in b.files.values() if f]), emitters=len(b.emitters), keys=len(b.keys), cells=len(b.atlas.rgba), skills=nall,
                player_skills=np_, player_skills_with_effect=npfx, npcs=len(npcs), weapons=len(weapons), bytes=len(data), warnings=dict(b.warn))
    log('effects: %d files, %d emitters, %d atlas cells, %d of %d player skills with an effect (%d skill rows), %d npcs, %d weapons, %.2f MB'
        % (info['effects'], info['emitters'], info['cells'], npfx, np_, nall, len(npcs), len(weapons), len(data) / 1e6))
    return [(TAG_EFCT, 1, len(skills), data)], info
