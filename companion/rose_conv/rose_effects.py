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
with the fade flag (the interpolation target) - so the runtime only walks the list. Part rotations (EFT pitch / yaw / roll; rose-offline Y * X * Z) become one
quaternion per emitter in the effect frame: qy(-yaw) * qx(pitch) * qz(-roll) (the frame is the original's mirrored in x).

VERSION 2 (fx11, PACK_REV avatar 3): real effect meshes (ZMS geometry + ZMO vertex animation as a half-float vertex animation texture), the attachment dummy bones of
the skill table, all four casting effects, status (buff) effects, item use effects, the level-up effect, up to 64 emitters per effect, the part rotation.

VERSION 3 (w17-fx, PACK_REV avatar 4): textures at their ORIGINAL resolution (variable-size cells in one DXT5 atlas: the client's DXT blocks are copied, not re-encoded,
a cell rectangle table), the original's blend factors / blend op / z test / z write / two-sided / alpha / lighting per part (the runtime maps them onto a small fixed set of
shared materials), the EFT part transform animations (ZMO position / rotation / scale tracks of bone 0, rose-offline TransformAnimation), the cast effects' motion frame
events (the skill's casting / repeat / action motion, rose-offline animation_effect_system EFFECT_SKILL_CASTING_0..3) and the party level-up effect.

Layout (little endian, 4-byte aligned):
  header 192 = 48 x u32: 0 version (3) ; 1 fileCount ; 2 fileOff ; 3 emitCount ; 4 emitOff ; 5 keyCount ; 6 keyOff ; 7 skillCount ; 8 skillOff ; 9 npcCount ; 10 npcOff ;
                        11 weaponCount ; 12 weaponOff ; 13 atlasFmt (2 = DXT5) ; 14 atlasW ; 15 atlasH ; 16 atlasLevels ; 17 atlasOff ; 18 atlasBytes ;
                        19 0 ; 20 0 ; 21 cellCount ; 22 meshCount ; 23 meshOff ; 24 vertCount ; 25 vertOff ; 26 idxCount ; 27 idxOff ;
                        28 vatW ; 29 vatH ; 30 vatOff ; 31 vatBytes (RGBAHalf texels) ; 32 statusCount ; 33 statusOff ; 34 itemCount ; 35 itemOff ;
                        36 levelUpFile (file id, 0 = none) ; 37 trackCount ; 38 trackOff ; 39 trackDataOff ; 40 trackDataFloats ; 41 cellRectOff ;
                        42 partyLevelUpFile (file id, 0 = none) ; 43..47 0
  file 16 (sorted by id): u16 fileId ; u16 emitFirst ; u16 emitCount ; u16 flags (1 an emitter / mesh loops forever, 2 has mesh parts) ; f32 duration (s, 0 = endless) ; u32 0
  emitter 144: 0 u16 kind (0 particles, 1 effect mesh) ; 2 u16 mode (blend factors: srcCode x 3 + dstCode, see blend_mode) ; 4 u16 cell (0xFFFF = none) ; 6 u8 atlasCols ;
               7 u8 atlasRows ; 8 u16 keyFirst ; 10 u16 keyCount ; 12 u16 maxParticles ; 14 u16 coords (0 world, 1 local position, 2 local) ; 16 i32 loops (<= 0 endless) ;
               20 f32 delay ; 24 lifeLo ; 28 lifeHi ; 32 emitLo ; 36 emitHi (particles / s) ; 40 radiusLo xyz ; 52 radiusHi xyz ; 64 gravLo xyz ; 76 gravHi xyz (m / s2) ;
               88 pos xyz ; 100 yaw (deg) ; 104 meshSize (m, half edge) ; 108 meshDur (s, one animation pass) ; 112 meshRepeat (0 = endless) ;
               116 align (0 camera facing, 1 flat on the ground, 2 upright about Y) ; 120 u16 mesh (0xFFFF none) ; 122 u16 flags (1 z test, 2 z write, 4 alpha test,
               8 two sided, 16 lit (zone map ambient), 32 alpha disabled, 64 blend op reverse subtract, 128 blend op max) ; 124 u16 track (0xFFFF none) ;
               126 u16 trackRepeat (0 = endless) ; 128 rotation quaternion xyzw (effect frame)
  keyframe 48: u16 type ; u8 fade ; u8 0 ; i16 nextFade (absolute key index, -1 none) ; u16 0 ; f32 t (s) ; f32 v[9] as (lo, hi) pairs:
               1 size (half edge x, y m) ; 2 timer (s) ; 3 r ; 4 g ; 5 b ; 6 a ; 7 rgba ; 8 vel x ; 9 vel y (up) ; 10 vel z (front) ; 11 vel xyz (m / s) ; 12 texture frame ; 13 rotation (deg)
  skill 48 (sorted by id): u16 skillId ; u16 cast[4] (file ids) ; u16 castDummy[4] (0xFFFF = the actor root) ; u16 bullet (file id) ; u16 bulletDummy ;
               u16 hit (file id) ; u16 hitDummy (0xFFFF root) ; u16 dummyHit[2] (file ids, on the target root) ; u16 bulletSpeed (cm / s) ; u16 status[2] (LIST_STATUS rows) ;
               u16 areaHit (file id) ; u16 0 ; u16 castAt[4] (when cast k starts: ms after the cast start, | 0x8000 = ms after the fire (action motion), 0xFFFF = never: its
               motion has no frame event for it)
  npc 8 (sorted): u16 npcId ; u16 hit (file id) ; u16 die (file id) ; u16 0
  weapon 8 (sorted): u16 weaponId ; u16 hit (file id) ; u16 bullet (file id) ; u16 bulletSpeed (cm / s)
  status 8 (sorted): u16 statusId ; u16 step (file id, shown while the status runs) ; u16 end (file id, at its end) ; u16 0
  item 8 (sorted): u16 itemNumber (LIST_USEITEM row) ; u16 use (file id) ; u16 statusFile (file id, held while the potion runs) ; u16 0
  mesh 32: u32 vertFirst ; u16 vertCount ; u16 flags (1 position animation, 2 uv animation, 4 alpha animation, 8 the ZMS has normals = lit) ; u32 idxFirst ; u32 idxCount ;
           u32 vatPos (texel, frame f vertex v at vatPos + f * vertCount + v: x y z alpha) ; u32 vatUv (texel, same order: u v 0 0) ; u16 frames ; u16 fps ; u32 0
  vertex 20: f32 x y z (effect-part frame, m) ; f32 u v      index: u16 (relative to the mesh), the table padded to 4 bytes
  track 16: u32 dataFirst (float index into the track data) ; u16 frames ; u16 fps ; u32 vatTexel (the same frames as 2 RGBAHalf texels each for the mesh shader) ;
            u16 flags (1 position, 2 rotation, 4 scale) ; u16 0
  track data: f32 x 8 per frame: position xyz (m, effect-part frame) ; scale ; rotation quaternion xyzw (effect-part frame); a channel the ZMO lacks keeps the identity
  cell rect 8 (cellCount): u16 x ; u16 y ; u16 w ; u16 h (atlas pixels, row 0 = top)
  atlas: DXT5 levels (Texture2D.LoadRawTextureData as is); every cell block aligned in every level (cells >= 32 px, power-of-two edges)
  vat: RGBAHalf texels, vatW per row (Texture2D.LoadRawTextureData as is)

Only file ids that resolve to a parsed EFT with at least one drawable part are written; references to anything else are 0 (the runtime then shows nothing).
No game data is committed; everything is read at conversion time on the player's machine.
"""
import math
import struct

import rosefmt as F
import rosedds as D

TAG_EFCT = b'EFCT'
VERSION = 3
HDR = struct.Struct('<48I')
FILE = struct.Struct('<HHHHfI')
EMIT = struct.Struct('<HHHBBHHHHi 25f HHHH 4f')
KEY = struct.Struct('<HBBhHf9f')
SKILL = struct.Struct('<24H')
NPC = struct.Struct('<4H')
WEAPON = struct.Struct('<4H')
STATUS = struct.Struct('<4H')
ITEM = struct.Struct('<4H')
MESH = struct.Struct('<IHHIIIIHHI')
VERT = struct.Struct('<5f')
TRACK = struct.Struct('<IHHIHH')
CELL = struct.Struct('<4H')
assert HDR.size == 192 and FILE.size == 16 and EMIT.size == 144 and KEY.size == 48 and SKILL.size == 48 and NPC.size == 8 and MESH.size == 32 and VERT.size == 20
assert TRACK.size == 16 and CELL.size == 8

TIME_SCALE = 4.8            # rose-offline particle_sequence_system: particles age 4.8 x dt
MAX_CELLS = 1024
ATLAS_LEVELS = 4            # every cell is >= TEX_MIN px, so it stays block aligned (>= 4 px) in every level
ATLAS_W = 2048              # atlas width (the retail effects at full resolution: 2048 x 2432 = 6.6 MB DXT5 incl. mips; w14 / fx11: 128 px cells, 2048 x 1408)
ATLAS_MAX_H = 4096          # 2048 x 4096 at most (11.2 MB incl. mips); above it the largest textures are halved until the atlas fits
TEX_MAX = 512               # per texture edge (the retail maximum); a larger texture uses its first mip level that fits
TEX_MIN = 32                # smaller textures are scaled up (nearest) so every cell stays block aligned in all ATLAS_LEVELS (32 >> 3 = 4 px)
MAX_TRACKS = 1024
MAX_TRACK_FRAMES = 4096
CAST_EVENTS = (44, 64, 74, 84)          # rose-data-irose animation_event_flags: EFFECT_SKILL_CASTING_0..3
CAST_NEVER = 0xFFFF
CAST_FIRE = 0x8000
PARTY_UP_EFT = '3DDATA/EFFECT/PARTY_UP_01.EFT'      # the party level-up effect (FILE_EFFECT row 297 on the retail client)
PARTY_UP_ID = 65001
MAX_FILES = 1024
MAX_EMITTERS_PER_FILE = 64  # fx11: the retail maximum (3 effects have 45..64; w14 kept only 16)
MAX_EMITTERS = 8192
MAX_KEYS = 32767
MAX_PARTICLES = 128         # per emitter (the runtime's whole pool is smaller; the budget clips further)
MAX_MESH_VERTS = 2048       # per effect mesh (retail max 1794)
MAX_MESHES = 1024
MAX_VAT_TEXELS = 1 << 22    # 32 MB of RGBAHalf at most (retail ~0.3 M texels = 2.3 MB)
VAT_W = 1024
NO_DUMMY = 0xFFFF
LEVELUP_EFT = '3DDATA/EFFECT/LEVELUP_01.EFT'   # rose-offline-client client_entity_event_system: the level-up effect is a fixed path
LEVELUP_ID = 65000          # its file id when FILE_EFFECT has no row for that path
FMT_DXT5 = 2
K_SIZE, K_TIMER, K_R, K_G, K_B, K_A, K_RGBA, K_VX, K_VY, K_VZ, K_VXYZ, K_TEX, K_ROT = range(1, 14)
KEY_FLOATS = {1: 4, 2: 2, 3: 2, 4: 2, 5: 2, 6: 2, 7: 8, 8: 2, 9: 2, 10: 2, 11: 6, 12: 2, 13: 2}
# blend factors (rose-offline effect_loader decode_blend_factor: 1 zero, 2 one, 3 src colour, 4 inv src colour, 5 src alpha, 6 inv src alpha, 7 dst alpha, 8 inv dst alpha,
# 9 dst colour, 10 inv dst colour) -> mode = srcCode x 3 + dstCode. The runtime shaders blend premultiplied (One, OneMinusSrcAlpha): colour out = rgb x srcFactor, alpha out
# = 0 (dst One), a (dst InvSrcAlpha) or 1 (dst Zero) - exact for every retail combination with those three destinations.
SRC_ONE, SRC_COLOR, SRC_ALPHA, SRC_ZERO, SRC_INVALPHA = 0, 1, 2, 3, 4
DST_ONE, DST_INVALPHA, DST_ZERO = 0, 1, 2
MODE_ADD, MODE_ALPHA, MODE_OPAQUE = SRC_ALPHA * 3 + DST_ONE, SRC_ALPHA * 3 + DST_INVALPHA, SRC_ONE * 3 + DST_ZERO
MF_ZTEST, MF_ZWRITE, MF_ATEST, MF_TWOSIDED, MF_LIT, MF_NOALPHA, MF_REVSUB, MF_MAX = 1, 2, 4, 8, 16, 32, 64, 128
MESH_NORMALS = 8
ZMO_SIZE = {1: 0, 2: 12, 4: 16, 8: 12, 16: 4, 32: 8, 64: 8, 128: 8, 256: 8, 512: 4, 1024: 4}


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


def _tanim(use, path):
    """an EFT part's transform animation path, '' when unused (rose-file-readers eft.rs: the use flag (a u32, != 0) and a path that is not empty / NULL; retail files carry
    0xCDCDCD00 / 0xCDCDCD01 there - uninitialised bytes of the original tool - and every 0xCDCDCD00 part has an empty / NULL path, so `!= 0` is enough)"""
    if not use or _null(path.strip() if path else ''):
        return ''
    return _norm(path)


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
        ua = r.u32(); af = r.str32(); arep = r.u32(); r.p += 4          # transform animation (rose-file-readers eft.rs: use flag != 0, the file not empty / NULL)
        pos = r.vec(3); pitch, yaw, roll = r.vec(3); r.p += 4
        delay = r.u32(); linked = r.u32()
        parts.append(dict(ptl=_norm(ptl), pos=pos, pitch=pitch, yaw=yaw, roll=roll, delay=delay, linked=linked, tanim=_tanim(ua, af), trep=arep))
    n = r.u32()
    if n > 256:
        raise ValueError('too many mesh parts')
    for _ in range(n):
        r.skip_str(); r.skip_str(); r.p += 4
        mesh = r.str32(); manim = r.str32(); tex = r.str32()
        alpha, two, atest, ztest, zwrite, src, dst, op = struct.unpack_from('<8I', d, r.p); r.p += 32
        ua = r.u32(); af = r.str32(); arep = r.u32(); r.p += 4
        pos = r.vec(3); pitch, yaw, roll = r.vec(3); r.p += 4
        delay = r.u32(); repeat = r.u32(); linked = r.u32()
        meshes.append(dict(mesh=_norm(mesh), anim='' if _null(manim) else _norm(manim), tex=_norm(tex), src=src, dst=dst, op=op, alpha=alpha,
                           two=two, atest=atest, ztest=ztest, zwrite=zwrite, pos=pos, pitch=pitch, yaw=yaw, roll=roll, delay=delay, repeat=repeat, linked=linked,
                           tanim=_tanim(ua, af), trep=arep))
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


def _qmul(a, b):
    ax, ay, az, aw = a; bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


def _qaxis(ax, deg):
    h = math.radians(fin(deg, 1e5)) * 0.5
    s = math.sin(h)
    return (ax[0] * s, ax[1] * s, ax[2] * s, math.cos(h))


def part_quat(pitch, yaw, roll):
    """EFT part rotation (rose-offline spawn_particle / spawn_mesh: Y(yaw) * X(pitch) * Z(roll) in the original's frame) -> quaternion (x, y, z, w) in the effect
    frame, which is the original's mirrored in x: qy(-yaw) * qx(pitch) * qz(-roll)"""
    q = _qmul(_qmul(_qaxis((0, 1, 0), -yaw), _qaxis((1, 0, 0), pitch)), _qaxis((0, 0, 1), -roll))
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(fin(c / n) for c in q)


def parse_zmo_vertex(d):
    """ZMO0002 of an effect mesh (vertex animation, rose-offline zmo.rs + zmo_texture_asset_loader.rs) -> dict(fps, nf, pos={vertex: [xyz cm per frame]},
    uv={vertex: [(u, v)]}, alpha={vertex: [a]}). Raises ValueError on a bad file."""
    r = _Rd(d)
    if F.dec(r.raw(8)).rstrip('\0') != 'ZMO0002':
        raise ValueError('not a ZMO0002')
    fps, nf, nc = r.u32(), r.u32(), r.u32()
    if nf <= 0 or nf > 4096 or nc > 65536 or fps <= 0 or fps > 1000:
        raise ValueError('bad ZMO header')
    ch = [(r.u32(), r.u32()) for _ in range(nc)]
    for t, _i in ch:
        if t not in ZMO_SIZE:
            raise ValueError('bad ZMO channel %d' % t)
    if r.p + nf * sum(ZMO_SIZE[t] for t, _ in ch) > len(d):
        raise ValueError('ZMO past the end')
    pos, uv, al = {}, {}, {}
    for t, i in ch:
        if t == 2:
            pos[i] = []
        elif t == 32:
            uv[i] = []
        elif t == 16:
            al[i] = []
    for _f in range(nf):
        for t, i in ch:
            if t == 2:
                pos[i].append(r.vec(3))
            elif t == 32:
                uv[i].append(r.vec(2))
            elif t == 16:
                al[i].append(r.f32())
            else:
                r.p += ZMO_SIZE[t]
    return dict(fps=fps, nf=nf, pos=pos, uv=uv, alpha=al)


def mesh_vert_unity(p):
    """ZMS vertex (metres after rosefmt.zms, file axes) -> effect-part frame (-x, z, -y)"""
    return (-p[0], p[2], -p[1])


def half4(x, y, z, w):
    return struct.pack('<4e', *[max(-65000.0, min(65000.0, fin(v))) for v in (x, y, z, w)])


_SRC = {2: SRC_ONE, 3: SRC_COLOR, 5: SRC_ALPHA, 1: SRC_ZERO, 6: SRC_INVALPHA}
_DST = {2: DST_ONE, 6: DST_INVALPHA, 1: DST_ZERO}


def blend_mode(src, dst):
    """PTL / EFT blend factors -> mode = srcCode x 3 + dstCode (the runtime's premultiplied shaders: colour = rgb x the source factor, alpha out 0 / a / 1 for the
    destination One / InvSrcAlpha / Zero). Exact for every retail combination except: a destination InvSrcColor ("screen", 12 retail parts) or a destination colour /
    alpha factor is drawn as One (additive); other source factors as One."""
    return _SRC.get(src, SRC_ONE) * 3 + _DST.get(dst, DST_ONE)


def blend_op_flags(op):
    """blend op (rose-offline decode_blend_op: 1 add, 2 subtract, 3 reverse subtract, 4 min, 5 max) -> MF_REVSUB / MF_MAX (their own shared materials); add, subtract
    (src - dst, unused by retail) and min (unused) blend like add"""
    return MF_REVSUB if op == 3 else (MF_MAX if op == 5 else 0)


def parse_zmo_transform(d):
    """ZMO0002 transform animation of an EFT part (rose-offline ZmoAssetLoader + transform_animation_system: bone 0's position (cm), rotation (w x y z) and uniform scale;
    a ZMO whose channels all name bone 0 and has more than 2 channels assigns them by channel index, so only the first channel is bone 0).
    -> dict(fps, nf, pos=[xyz] or None, rot=[(x, y, z, w)] or None, scale=[s] or None) in FILE axes. Raises ValueError on a bad file."""
    r = _Rd(d)
    if F.dec(r.raw(8)).rstrip('\0') != 'ZMO0002':
        raise ValueError('not a ZMO0002')
    fps, nf, nc = r.u32(), r.u32(), r.u32()
    if nf <= 0 or nf > MAX_TRACK_FRAMES or nc > 4096 or fps <= 0 or fps > 1000:
        raise ValueError('bad ZMO header')
    ch = [(r.u32(), r.u32()) for _ in range(nc)]
    for t, _b in ch:
        if t not in ZMO_SIZE:
            raise ValueError('bad ZMO channel %d' % t)
    if r.p + nf * sum(ZMO_SIZE[t] for t, _ in ch) > len(d):
        raise ValueError('ZMO past the end')
    by_index = nc > 2 and max([b for _t, b in ch] or [0]) == 0
    want = [(ci == 0) if by_index else (b == 0) for ci, (_t, b) in enumerate(ch)]
    pos, rot, sc = None, None, None
    for ci, (t, _b) in enumerate(ch):
        if want[ci]:
            if t == 2 and pos is None:
                pos = []
            elif t == 4 and rot is None:
                rot = []
            elif t == 1024 and sc is None:
                sc = []
    taken = {}
    for ci, (t, _b) in enumerate(ch):
        if want[ci] and t in (2, 4, 1024) and t not in taken:
            taken[t] = ci
    for _f in range(nf):
        for ci, (t, _b) in enumerate(ch):
            if t == 2:
                v = r.vec(3)
                if taken.get(2) == ci:
                    pos.append(v)
            elif t == 4:
                w, x, y, z = r.vec(4)
                if taken.get(4) == ci:
                    rot.append((x, y, z, w))
            elif t == 1024:
                v = r.f32()
                if taken.get(1024) == ci:
                    sc.append(v)
            else:
                r.p += ZMO_SIZE[t]
    return dict(fps=fps, nf=nf, pos=pos, rot=rot, scale=sc)


def track_frames(z):
    """parse_zmo_transform result -> [8 floats per frame] in the effect-part frame: position (-x, z, -y) / 100, scale, rotation quaternion mirrored like part_quat
    (file (x, y, z, w) -> rose-offline (x, z, -y, w) -> the effect frame mirrored in x: (x, -z, y, w)); a missing channel = identity (the child transform's default)"""
    out = []
    for f in range(z['nf']):
        p = z['pos'][f] if z['pos'] else (0.0, 0.0, 0.0)
        q = z['rot'][f] if z['rot'] else (0.0, 0.0, 0.0, 1.0)
        s = z['scale'][f] if z['scale'] else 1.0
        x, y, zz, w = q[0], -q[2], q[1], q[3]
        n = math.sqrt(x * x + y * y + zz * zz + w * w) or 1.0
        px, py, pz = pos_unity(p)
        out.append([fin(px), fin(py), fin(pz), fin(s), fin(x / n), fin(y / n), fin(zz / n), fin(w / n)])
    return out


def _motion_path(tm, fm, action, wtype):
    """TYPE_MOTION[action][weapon type] -> FILE_MOTION row -> its male column (rose_chars._motion_path without the fallbacks); None when there is none"""
    if tm is None or fm is None or action <= 0:
        return None
    try:
        idx = int(tm['cells'][action][wtype] or 0)
    except (ValueError, IndexError, TypeError):
        return None
    if idx <= 0 or idx >= fm['rows']:
        return None
    c = fm['cells'][idx]
    return _norm(str(c[0])) if c and str(c[0]).strip() else None


def motion_events(read, path, cache):
    """-> (frames, fps, {event id: first frame}) of a character motion ZMO (its EZMO / 3ZMO trailer: one u16 event id per frame), None when unreadable"""
    k = path.upper()
    if k in cache:
        return cache[k]
    cache[k] = None
    try:
        d = read(path)
        r = _Rd(d)
        if F.dec(r.raw(8)).rstrip('\0') != 'ZMO0002':
            return None
        fps, nf = r.u32(), r.u32()
        ev = {}
        if d[-4:] in (b'EZMO', b'3ZMO') and fps > 0:
            off, = struct.unpack_from('<I', d, len(d) - 8)
            n, = struct.unpack_from('<H', d, off)
            ids = struct.unpack_from('<%dH' % n, d, off + 2)
            for f, e in enumerate(ids):
                if e and e not in ev:
                    ev[e] = f
        cache[k] = (nf, fps, ev) if fps > 0 else None
    except Exception:
        cache[k] = None
    return cache[k]


def cast_timing(c, read, tm, fm, cache):
    """LIST_SKILL row -> castAt[4] (rose-offline animation_effect_system: EFFECT_SKILL_CASTING_k (frame events 44 / 64 / 74 / 84) spawns casting effect k while the skill's
    casting motion (52, speed 53 %), then its repeat motion (54) and its action motion (68, speed 69 %) play): ms after the cast start for an event in the casting motion
    (or the repeat motion, after one pass of the casting motion), CAST_FIRE | ms after the fire for one in the action motion, CAST_NEVER when the motions have no such
    event (the original never shows it). The weapon-type column whose motion shows it earliest is used. No motion tables / no motion = 0 (with the cast)."""
    if tm is None or fm is None:
        return [0] * 4
    cm, cspd, rm, am, aspd = _int(c, 52), _int(c, 53), _int(c, 54), _int(c, 68), _int(c, 69)
    cspd = cspd / 100.0 if cspd > 0 else 1.0
    aspd = aspd / 100.0 if aspd > 0 else 1.0
    best = [None] * 4
    found = False
    ncol = tm.get('cols') or max([len(x) for x in tm['cells']] or [0])

    def ev(action, wt):
        p = _motion_path(tm, fm, action, wt)
        return motion_events(read, p, cache) if p else None
    for wt in range(min(64, ncol)):
        mc, mr, ma = ev(cm, wt), ev(rm, wt), ev(am, wt)
        if mc is None and ma is None:
            continue
        found = True
        for k in range(4):
            e = CAST_EVENTS[k]
            cand = None
            if mc is not None and e in mc[2]:
                cand = (0, mc[2][e] / float(mc[1]) / cspd)
            elif mr is not None and e in mr[2]:
                cand = (0, ((mc[0] / float(mc[1])) if mc else 0.0) / cspd + mr[2][e] / float(mr[1]) / cspd)
            elif ma is not None and e in ma[2]:
                cand = (1, ma[2][e] / float(ma[1]) / aspd)
            if cand is not None and (best[k] is None or cand < best[k]):
                best[k] = cand
    if not found:
        return [0] * 4
    out = []
    for b in best:
        if b is None:
            out.append(CAST_NEVER)
        else:
            ms = max(0, min(0x7FFF, int(round(b[1] * 1000.0))))
            out.append(ms | (CAST_FIRE if b[0] else 0))
    return out


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
    """particle / mesh textures at their ORIGINAL resolution (edges up to TEX_MAX, at least TEX_MIN, powers of two) -> one DXT5 atlas of variable-size cells (row 0 = top).
    The client's DXT blocks are copied into the atlas (DXT5 as is, DXT1 colour blocks + an opaque alpha block; only DXT1 3-colour blocks with transparency and DXT3 /
    raw textures are re-encoded), its own mip levels too when it has them. add() -> cell index or None (missing / bad / full)."""

    def __init__(self, read, max_cells=MAX_CELLS, tex_max=TEX_MAX, width=ATLAS_W, max_h=ATLAS_MAX_H):
        self.read = read
        self.cells = {}           # path upper -> index
        self.tex = []             # per cell: dict(w, h, levels=[DXT5 bytes of level k, block rows of (w >> k) / 4])
        self.max = max_cells
        self.tex_max = tex_max
        self.width = width
        self.max_h = max_h
        self.missing = 0
        self.halved = 0
        self.copied = 0           # levels taken from the client's blocks (not re-encoded)
        self._enc = None

    @property
    def rgba(self):               # fx11 name: the cell list (len = cell count)
        return self.tex

    def add(self, path):
        k = path.upper()
        if k in self.cells:
            return self.cells[k]
        if len(self.tex) >= self.max:
            return None
        try:
            t = cell_tex(D.parse_dds(self.read(path)), self.tex_max, self)
        except Exception:          # one bad / missing texture: that part is skipped
            self.missing += 1
            self.cells[k] = None
            return None
        self.cells[k] = len(self.tex)
        self.tex.append(t)
        self._enc = None
        return self.cells[k]

    def layout(self):
        """-> (atlas width, height, [(x, y) per cell]); halves the largest textures (their next mip level) until the atlas fits max_h"""
        while True:
            w, h, pos = pack_cells([(t['w'], t['h']) for t in self.tex], self.width)
            if h <= self.max_h:
                return w, h, pos
            big = max(range(len(self.tex)), key=lambda i: (self.tex[i]['w'] * self.tex[i]['h'], -i))
            if not halve_tex(self.tex[big]):
                return w, h, pos          # nothing left to halve (cannot happen below MAX_CELLS x TEX_MIN^2)
            self.halved += 1

    def encode(self):
        """-> (w, h, [DXT5 level bytes], [(x, y, w, h) per cell]) or None without cells"""
        if not self.tex:
            return None
        if self._enc is not None:
            return self._enc
        aw, ah, pos = self.layout()
        levels = []
        for lv in range(ATLAS_LEVELS):
            lw, lh = aw >> lv, ah >> lv
            bw = lw // 4
            out = bytearray((lw // 4) * (lh // 4) * 16)
            for t, (x, y) in zip(self.tex, pos):
                tb = (t['w'] >> lv) // 4
                th = (t['h'] >> lv) // 4
                src = t['levels'][lv]
                bx, by = (x >> lv) // 4, (y >> lv) // 4
                for row in range(th):
                    o = ((by + row) * bw + bx) * 16
                    out[o:o + tb * 16] = src[row * tb * 16:(row + 1) * tb * 16]
            levels.append(bytes(out))
        self._enc = (aw, ah, levels, [(x, y, t['w'], t['h']) for t, (x, y) in zip(self.tex, pos)])
        return self._enc


def pack_cells(sizes, width=ATLAS_W):
    """power-of-two rectangles -> (atlas width, height, [(x, y)]): guillotine packing, the largest first (height, then width), each into the free rectangle lowest / leftmost
    where it fits. Every origin is a sum of edges >= TEX_MIN, so the cells stay block aligned in every mip level. The width shrinks to the smallest power of two that is
    still >= the widest cell and >= the square root of the total area (small test atlases); the height is rounded up to a multiple of 4 x 2^(ATLAS_LEVELS - 1)."""
    if not sizes:
        return 0, 0, []
    area = sum(w * h for w, h in sizes)
    w = TEX_MIN
    while w < width and (w < max(s[0] for s in sizes) or w * w < area):
        w *= 2
    w = min(w, width)
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0], i))
    INF = 1 << 30
    free = [(0, 0, w, INF)]
    pos = [None] * len(sizes)
    top = 0
    for i in order:
        sw, sh = sizes[i]
        best = None
        for fi, (fx, fy, fw, fh) in enumerate(free):
            if sw <= fw and sh <= fh and (best is None or (fy, fx) < (free[best][1], free[best][0])):
                best = fi
        fx, fy, fw, fh = free.pop(best)
        pos[i] = (fx, fy)
        top = max(top, fy + sh)
        if fw - sw > 0:
            free.append((fx + sw, fy, fw - sw, sh))
        if fh - sh > 0:
            free.append((fx, fy + sh, fw, fh - sh if fh < INF else INF))
    al = 4 << (ATLAS_LEVELS - 1)
    return w, (top + al - 1) // al * al, pos


def _pow2_floor(n):
    p = 1
    while p * 2 <= n:
        p *= 2
    return p


def _dxt1_block_to_dxt5(blk):
    """one DXT1 block -> DXT5: a 4-colour block (c0 > c1, or a 3-colour block that uses only indices 0 / 1) keeps its colour block exactly with an opaque alpha block;
    a 3-colour block using the mid colour / transparency is re-encoded from its pixels (DXT5 decodes the colour block in 4-colour mode only)"""
    c0, c1, bits = struct.unpack_from('<HHI', blk, 0)
    if c0 > c1:
        return b'\xff\xff\x00\x00\x00\x00\x00\x00' + bytes(blk)
    used2 = any(((bits >> (2 * i)) & 3) >= 2 for i in range(16))
    if not used2:
        return b'\xff\xff\x00\x00\x00\x00\x00\x00' + bytes(blk)
    return D.encode_dxt5(D.decode_dxt('DXT1', bytes(blk), 4, 4), 4, 4)


def to_dxt5_level(fmt, data, w, h):
    """one DDS level (fmt 'DXT1' / 'DXT3' / 'DXT5' / 'RGBA', w and h multiples of 4) -> DXT5 blocks"""
    if fmt == 'DXT5':
        return bytes(data)
    if fmt == 'DXT1':
        return b''.join(_dxt1_block_to_dxt5(data[i:i + 8]) for i in range(0, (w // 4) * (h // 4) * 8, 8))
    if fmt == 'DXT3':
        return D.dxt3_to_dxt5(data)
    return D.encode_dxt5(data, w, h)


def _levels_from_rgba(px, w, h):
    out = []
    for lv in range(ATLAS_LEVELS):
        out.append(D.encode_dxt5(px, w, h))
        if lv + 1 < ATLAS_LEVELS:
            px, w, h = D.downsample2(px, w, h)
    return out


def cell_tex(dds, tex_max=TEX_MAX, stats=None):
    """parse_dds result -> dict(w, h, levels): the first mip level whose edges fit tex_max; power-of-two edges >= TEX_MIN keep the client's blocks (and its own mip levels),
    anything else is decoded, scaled to power-of-two edges in [TEX_MIN, tex_max] (box halving / nearest) and encoded with box-filtered mips."""
    w, h, fmt = dds['w'], dds['h'], dds['fmt']
    lv = 0
    while lv + 1 < len(dds['levels']) and (max(1, w >> lv) > tex_max or max(1, h >> lv) > tex_max):
        lv += 1
    lw, lh = max(1, w >> lv), max(1, h >> lv)
    p2 = lw == _pow2_floor(lw) and lh == _pow2_floor(lh)
    if p2 and TEX_MIN <= lw <= tex_max and TEX_MIN <= lh <= tex_max and fmt != 'RGBA':
        have = min(ATLAS_LEVELS, len(dds['levels']) - lv)
        levels = [to_dxt5_level(fmt, dds['levels'][lv + k], lw >> k, lh >> k) for k in range(have)]
        if have < ATLAS_LEVELS:          # the client's file has fewer mip levels: the rest box-filtered from its smallest one
            k = have - 1
            px, mw, mh = D.decode_dxt(fmt, dds['levels'][lv + k], lw >> k, lh >> k), lw >> k, lh >> k
            while len(levels) < ATLAS_LEVELS:
                px, mw, mh = D.downsample2(px, mw, mh)
                levels.append(D.encode_dxt5(px, mw, mh))
        if stats is not None:
            stats.copied += 1
        return dict(w=lw, h=lh, levels=levels)
    data = dds['levels'][lv]
    px = data if fmt == 'RGBA' else D.decode_dxt(fmt, data, lw, lh)
    tw = max(TEX_MIN, min(tex_max, _pow2_floor(lw)))
    th = max(TEX_MIN, min(tex_max, _pow2_floor(lh)))
    while lw >= 2 * tw and lh >= 2 * th:
        px, lw, lh = D.downsample2(px, lw, lh)
    if (lw, lh) != (tw, th):
        px = resize_nearest(px, lw, lh, tw, th)
    return dict(w=tw, h=th, levels=_levels_from_rgba(px, tw, th))


def halve_tex(t):
    """a cell texture one mip level down (its level 1 becomes level 0, one more level from box filtering) -> False when it is already TEX_MIN"""
    if t['w'] // 2 < TEX_MIN or t['h'] // 2 < TEX_MIN:
        return False
    w, h = t['w'] // 2, t['h'] // 2
    last_w, last_h = w >> (ATLAS_LEVELS - 2), h >> (ATLAS_LEVELS - 2)
    px = D.decode_dxt('DXT5', t['levels'][-1], last_w, last_h)
    px, nw, nh = D.downsample2(px, last_w, last_h)
    t['levels'] = t['levels'][1:] + [D.encode_dxt5(px, nw, nh)]
    t['w'], t['h'] = w, h
    return True


def resize_nearest(px, w, h, nw, nh):
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        sy = min(h - 1, y * h // nh)
        for x in range(nw):
            sx = min(w - 1, x * w // nw)
            o = (sy * w + sx) * 4
            out[(y * nw + x) * 4:(y * nw + x) * 4 + 4] = px[o:o + 4]
    return bytes(out)


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
        self.meshes = []          # mesh records (dicts)
        self.verts = []           # (x, y, z, u, v)
        self.idx = []             # u16 relative to the mesh
        self.vat = bytearray()    # RGBAHalf texels
        self.vat_texels = 0
        self.mesh_cache = {}
        self.tracks = []          # dict(first, frames, fps, vat, flags)
        self.track_data = []      # floats, 8 per frame
        self.track_cache = {}

    def _w(self, k):
        self.warn[k] = self.warn.get(k, 0) + 1

    def track(self, path):
        """an EFT part's transform animation (ZMO) -> (track index, seconds of one pass) or (None, 0) when missing / unusable (the part then stands still, like
        rose-offline when the asset fails to load). Cached per path; the frames go to the track data (f32, the particles' CPU side) and as 2 RGBAHalf texels per frame to the
        animation texture (the mesh shader)."""
        k = path.upper()
        if k in self.track_cache:
            return self.track_cache[k]
        self.track_cache[k] = (None, 0.0)
        if len(self.tracks) >= MAX_TRACKS:
            self._w('transform animations above the budget')
            return self.track_cache[k]
        try:
            z = parse_zmo_transform(self.read(path))
        except Exception:
            self._w('transform animation missing or unreadable')
            return self.track_cache[k]
        if not (z['pos'] or z['rot'] or z['scale']):
            self._w('transform animation without bone 0 channels')
            return self.track_cache[k]
        if self.vat_texels + 2 * z['nf'] > MAX_VAT_TEXELS:
            self._w('vertex animation above the budget')
            return self.track_cache[k]
        fr = track_frames(z)
        first = len(self.track_data)
        vat = self.vat_texels
        for f in fr:
            self.track_data += f
            self.vat += half4(f[0], f[1], f[2], f[3]) + half4(f[4], f[5], f[6], f[7])
        self.vat_texels += 2 * len(fr)
        flags = (1 if z['pos'] else 0) | (2 if z['rot'] else 0) | (4 if z['scale'] else 0)
        self.tracks.append(dict(first=first, frames=z['nf'], fps=z['fps'], vat=vat, flags=flags))
        self.track_cache[k] = (len(self.tracks) - 1, z['nf'] / float(z['fps']))
        return self.track_cache[k]

    def _ptl(self, path):
        k = path.upper()
        if k not in self.ptl_cache:
            try:
                self.ptl_cache[k] = parse_ptl(self.read(path))
            except Exception:          # one bad / missing PTL: its parts are skipped
                self.ptl_cache[k] = None
                self._w('PTL missing or unreadable')
        return self.ptl_cache[k]

    def mesh(self, mpath, apath):
        """effect mesh (ZMS) + its vertex animation (ZMO, may be '') -> (mesh index, seconds of one pass) or (None, 0) when the mesh is missing / unusable.
        The ZMO replaces positions / uvs / alpha of the vertices it has channels for (rose-offline effect_mesh_material.wgsl: position, uv; alpha per vertex here,
        rose-offline keeps one alpha track). Cached per (mesh, animation)."""
        k = (mpath.upper(), apath.upper())
        if k in self.mesh_cache:
            return self.mesh_cache[k]
        self.mesh_cache[k] = (None, 0.0)
        if len(self.meshes) >= MAX_MESHES:
            self._w('effect meshes above the budget')
            return self.mesh_cache[k]
        try:
            z = F.zms(self.read(mpath))
        except Exception:
            self._w('effect mesh missing or unreadable')
            return self.mesh_cache[k]
        pts = z.get('pos') or []
        uvs = z.get('uv1') or [(0.0, 0.0)] * len(pts)
        idx = list(z.get('idx') or ())
        nv = len(pts)
        if nv == 0 or nv > MAX_MESH_VERTS or len(idx) < 3 or len(uvs) != nv or any(i >= nv for i in idx):
            self._w('effect mesh unusable (no / too many vertices, bad indices)')
            return self.mesh_cache[k]
        idx = idx[:len(idx) - len(idx) % 3]
        idx = [v for i in range(0, len(idx), 3) for v in (idx[i], idx[i + 2], idx[i + 1])]          # w17-fx: (-x, z, -y) is a reflection -> reversed winding (like rosechar), so a one-sided part culls its back faces
        an = None
        if apath:
            try:
                an = parse_zmo_vertex(self.read(apath))
            except Exception:
                self._w('effect mesh animation missing or unreadable')
                an = None
        flags, vpos, vuv, frames, fps, dur = (MESH_NORMALS if z.get('nrm') else 0), 0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 0.0
        if an is not None and (an['pos'] or an['uv'] or an['alpha']):
            nf = an['nf']
            need = nf * nv * ((1 if (an['pos'] or an['alpha']) else 0) + (1 if an['uv'] else 0))
            if self.vat_texels + need > MAX_VAT_TEXELS:
                self._w('vertex animation above the budget')
            else:
                frames, fps, dur = nf, an['fps'], nf / float(an['fps'])
                if an['pos'] or an['alpha']:
                    flags |= (1 if an['pos'] else 0) | (4 if an['alpha'] else 0)
                    vpos = self.vat_texels
                    out = bytearray()
                    for f in range(nf):
                        for v in range(nv):
                            pv = an['pos'].get(v)
                            x, y, zz = mesh_vert_unity((pv[f][0] / 100.0, pv[f][1] / 100.0, pv[f][2] / 100.0)) if pv else mesh_vert_unity(pts[v])
                            av = an['alpha'].get(v)
                            out += half4(x, y, zz, av[f] if av else 1.0)
                    self.vat += out
                    self.vat_texels += nf * nv
                if an['uv']:
                    flags |= 2
                    vuv = self.vat_texels
                    out = bytearray()
                    for f in range(nf):
                        for v in range(nv):
                            uv = an['uv'].get(v)
                            u, w = uv[f] if uv else uvs[v]
                            out += half4(u, w, 0.0, 0.0)
                    self.vat += out
                    self.vat_texels += nf * nv
        first = len(self.verts)
        for v in range(nv):
            x, y, zz = mesh_vert_unity(pts[v])
            self.verts.append((fin(x), fin(y), fin(zz), fin(uvs[v][0]), fin(uvs[v][1])))
        ifirst = len(self.idx)
        self.idx += idx
        self.meshes.append(dict(vfirst=first, vcount=nv, flags=flags, ifirst=ifirst, icount=len(idx), vpos=vpos, vuv=vuv, frames=frames, fps=fps))
        self.mesh_cache[k] = (len(self.meshes) - 1, dur)
        return self.mesh_cache[k]

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
                ti, tdur = self.track(p['tanim']) if p.get('tanim') else (None, 0.0)
                trep = max(0, min(65535, p.get('trep', 0)))
                if ti is not None and trep > 0 and tdur > 0.0:
                    d = tdur * trep          # rose-offline effect_system: a sequence whose transform animation completed counts as finished (its particles end with the effect)
                if d < 0:
                    endless = True
                dur = max(dur, d)
                self.emitters.append(dict(kind=0, mode=blend_mode(s['src'], s['dst']), cell=cell, cols=min(255, s['cols']), rows=min(255, s['rows']),
                                          key_first=kf, key_count=nk, num=num, coords=s['coords'], loops=s['loops'], delay=delay, life=life, emit=emit,
                                          rlo=rlo, rhi=rhi, glo=glo, ghi=ghi, pos=pos_unity(p['pos']), yaw=-p['yaw'], msize=0.0, mdur=0.0, mrep=0.0, align=s['align'] if s['align'] in (0, 1, 2) else 0,
                                          mesh=NO_DUMMY, mflags=blend_op_flags(s['op']), quat=part_quat(p['pitch'], p['yaw'], p['roll']),
                                          track=NO_DUMMY if ti is None else ti, trep=trep))
        for m in eft['meshes']:
            if len(self.emitters) - first >= MAX_EMITTERS_PER_FILE or len(self.emitters) >= MAX_EMITTERS:
                self._w('emitters above the per-effect budget')
                break
            cell = self.atlas.add(m['tex']) if m['tex'] else None
            if cell is None:
                self._w('mesh texture missing')
                continue
            mi, mdur = self.mesh(m['mesh'], m['anim'])
            if mi is None:
                continue
            rep = max(0, min(255, m['repeat']))
            delay = m['delay'] / 1000.0
            if mdur <= 0.0 or rep == 0:
                endless = True          # a static mesh lives as long as the effect / an endless repeat (rose-offline MeshAnimation::repeat(None)) until the hold time
            else:
                dur = max(dur, delay + mdur * rep)
            flags |= 2
            mf = (MF_ZTEST if m['ztest'] else 0) | (MF_ZWRITE if m['zwrite'] else 0) | (MF_ATEST if m['atest'] else 0) | (MF_TWOSIDED if m['two'] else 0)
            mf |= (MF_LIT if self.meshes[mi]['flags'] & MESH_NORMALS else 0) | (0 if m['alpha'] else MF_NOALPHA) | blend_op_flags(m['op'])
            ti, _td = self.track(m['tanim']) if m.get('tanim') else (None, 0.0)
            self.emitters.append(dict(kind=1, mode=blend_mode(m['src'], m['dst']), cell=cell, cols=1, rows=1, key_first=0, key_count=0, num=1, coords=2, loops=1,
                                      delay=delay, life=(mdur, mdur), emit=(0.0, 0.0), rlo=(0, 0, 0), rhi=(0, 0, 0), glo=(0, 0, 0), ghi=(0, 0, 0),
                                      pos=pos_unity(m['pos']), yaw=-m['yaw'], msize=0.0, mdur=mdur, mrep=float(rep), align=0,
                                      mesh=mi, mflags=mf, quat=part_quat(m['pitch'], m['yaw'], m['roll']),
                                      track=NO_DUMMY if ti is None else ti, trep=max(0, min(65535, m.get('trep', 0)))))
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


def _dummy(c, i):
    """a dummy bone column: 999 / empty / negative = the actor root (NO_DUMMY), else the dummy index (rose-offline: joints[dummy offset + id])"""
    v = _int(c, i, -1)
    return NO_DUMMY if v < 0 or v >= 999 else min(v, 254)


def skill_effects(c):
    """LIST_SKILL row cells -> dict(cast=[4 file ids], cast_dummy=[4], bullet_effect (LIST_EFFECT row), bullet_dummy, hit (file id), hit_dummy_bone, hit_dummy=[2 file ids],
    status=[2 LIST_STATUS rows], area_hit (file id))   [skill_database.rs columns 56..67, 71..83, 11..12]"""
    return dict(cast=[_int(c, 56 + 3 * k) for k in range(4)], cast_dummy=[_dummy(c, 57 + 3 * k) for k in range(4)], bullet_effect=_int(c, 71),
                bullet_dummy=_dummy(c, 72), hit=_int(c, 74), hit_dummy_bone=_dummy(c, 75), hit_dummy=[_int(c, 77), _int(c, 80)],
                status=[_int(c, 11), _int(c, 12)], area_hit=_int(c, 83))


def build_tables(read, file_stb, list_stb, skill_stb, npc_stb=None, weapon_stb=None, atlas=None, status_stb=None, useitem_stb=None, type_motion=None, file_motion=None):
    """-> (skills, npcs, weapons, builder); builder.statuses / builder.items / builder.levelup / builder.partyup hold the fx11 / w17 tables. type_motion / file_motion
    (TYPE_MOTION / FILE_MOTION) give the cast effects' motion frame events (cast_timing; without them every cast starts with the cast).
    Pure apart from read(path) -> bytes (KeyError when missing)."""
    mcache = {}
    fe = {}
    for r in range(file_stb['rows']):
        c = file_stb['cells'][r]
        if len(c) > 1 and str(c[1]).strip():
            fe[r] = _norm(str(c[1]))
    b = Builder(read, atlas)
    st_rows = {}
    if status_stb is not None:
        for sid in range(1, min(status_stb['rows'], 65536)):
            c = status_stb['cells'][sid]
            step = resolve_file(b, fe, _int(c, 10))
            end = resolve_file(b, fe, _int(c, 15))
            st_rows[sid] = (step, end, [_int(c, 5), _int(c, 7)])
    skills = []
    for sid in range(1, min(skill_stb['rows'], 65536)):
        se = skill_effects(skill_stb['cells'][sid])
        casts, cdum, cat = [], [], []
        timing = None
        for k, (f, dm) in enumerate(zip(se['cast'], se['cast_dummy'])):
            x = resolve_file(b, fe, f)
            if x:
                if timing is None:
                    timing = cast_timing(skill_stb['cells'][sid], read, type_motion, file_motion, mcache)
                casts.append(x); cdum.append(dm); cat.append(timing[k])
        hn, _hc, bul, speed = effect_parts(list_stb, se['bullet_effect'])
        bullet = resolve_file(b, fe, bul)
        hit = resolve_file(b, fe, se['hit']) or resolve_file(b, fe, hn)
        hd = [resolve_file(b, fe, f) for f in se['hit_dummy']]
        area = resolve_file(b, fe, se['area_hit'])
        sts = [x if x in st_rows and (st_rows[x][0] or st_rows[x][1]) else 0 for x in se['status']]
        if casts or bullet or hit or any(hd) or area or any(sts):
            skills.append(dict(id=sid, cast=(casts + [0] * 4)[:4], cast_dummy=(cdum + [NO_DUMMY] * 4)[:4], bullet=bullet, bullet_dummy=se['bullet_dummy'], hit=hit,
                               hit_bone=se['hit_dummy_bone'], hit_dummy=hd, speed=_u16(speed), status=sts, area=area,
                               hit_dummy0=hd[0], cast_at=(cat + [CAST_NEVER] * 4)[:4]))
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
    b.statuses = [dict(id=k, step=v[0], end=v[1]) for k, v in sorted(st_rows.items()) if v[0] or v[1]]
    b.items = []
    if useitem_stb is not None:
        for n in range(1, min(useitem_stb['rows'], 65536)):
            c = useitem_stb['cells'][n]
            use = resolve_file(b, fe, _int(c, 22))
            sid = _int(c, 24)
            sf = 0
            if sid in st_rows:          # the potion's status: its own step effect, else the step effect of the status it applies (rose-offline use_item_event_system)
                sf = st_rows[sid][0]
                for a in st_rows[sid][2]:
                    if not sf and a in st_rows:
                        sf = st_rows[a][0]
            if use or sf:
                b.items.append(dict(id=n, use=use, status=sf))
    b.levelup = fixed_effect(b, fe, LEVELUP_EFT, LEVELUP_ID)
    b.partyup = fixed_effect(b, fe, PARTY_UP_EFT, PARTY_UP_ID)
    return skills, npcs, weapons, b


def fixed_effect(b, fe, path, reserved_id):
    """an effect the original plays by its path (the level-up, the party level-up): its FILE_EFFECT row when there is one, else the reserved file id; 0 = not drawable"""
    for fid, p in fe.items():
        if p.upper() == path:
            return resolve_file(b, fe, fid)
    if fid_free(fe, reserved_id):
        try:
            b.read(path)
            return reserved_id if b.add_file(reserved_id, path) else 0
        except Exception:
            return 0
    return 0


def fid_free(fe, fid):
    return fid not in fe


def _pad4(b):
    return b + bytes((-len(b)) % 4)


def enc_efct(b, skills, npcs=(), weapons=()):
    files = sorted((f for f in b.files.values() if f), key=lambda f: f['id'])
    fb = b''.join(FILE.pack(_u16(f['id']), f['first'], f['count'], f['flags'], fin(f['dur']), 0) for f in files)
    eb = bytearray()
    for e in b.emitters:
        fl = [e['delay'], e['life'][0], e['life'][1], e['emit'][0], e['emit'][1], *e['rlo'], *e['rhi'], *e['glo'], *e['ghi'], *e['pos'], e['yaw'], e['msize'], e['mdur'], e['mrep'], float(e['align'])]
        q = e.get('quat', (0.0, 0.0, 0.0, 1.0))
        eb += EMIT.pack(e['kind'], e['mode'], 0xFFFF if e['cell'] is None else e['cell'], e['cols'], e['rows'], e['key_first'], e['key_count'], e['num'], e['coords'],
                        max(-2 ** 31, min(2 ** 31 - 1, int(e['loops']))), *[fin(x) for x in fl], e.get('mesh', NO_DUMMY), e.get('mflags', 0),
                        e.get('track', NO_DUMMY), e.get('trep', 0), *[fin(x) for x in q])
    kb = b''.join(KEY.pack(k[0], k[1], 0, k[4], 0, fin(k[2]), *[fin(x) for x in k[3]]) for k in b.keys)
    sb = b''
    for s in sorted(skills, key=lambda s: s['id']):
        c = (list(s['cast']) + [0] * 4)[:4]
        cd = (list(s.get('cast_dummy', [NO_DUMMY] * 4)) + [NO_DUMMY] * 4)[:4]
        hd = (list(s.get('hit_dummy') if isinstance(s.get('hit_dummy'), list) else [s.get('hit_dummy', 0)]) + [0, 0])[:2]
        st = (list(s.get('status', [])) + [0, 0])[:2]
        ca = (list(s.get('cast_at', [0] * 4)) + [CAST_NEVER] * 4)[:4]
        sb += SKILL.pack(s['id'], *c, *cd, s['bullet'], s.get('bullet_dummy', NO_DUMMY), s['hit'], s.get('hit_bone', NO_DUMMY), *hd, s['speed'], *st, s.get('area', 0), 0, *ca)
    nb = b''.join(NPC.pack(n['id'], n['hit'], n['die'], 0) for n in sorted(npcs, key=lambda n: n['id']))
    wb = b''.join(WEAPON.pack(w['id'], w['hit'], w['bullet'], w['speed']) for w in sorted(weapons, key=lambda w: w['id']))
    stb_ = b''.join(STATUS.pack(x['id'], x['step'], x['end'], 0) for x in sorted(getattr(b, 'statuses', []), key=lambda x: x['id']))
    itb = b''.join(ITEM.pack(x['id'], x['use'], x['status'], 0) for x in sorted(getattr(b, 'items', []), key=lambda x: x['id']))
    mb = b''.join(MESH.pack(m['vfirst'], m['vcount'], m['flags'], m['ifirst'], m['icount'], m['vpos'], m['vuv'], m['frames'], m['fps'], 0) for m in b.meshes)
    vb = b''.join(VERT.pack(*v) for v in b.verts)
    ib = _pad4(struct.pack('<%dH' % len(b.idx), *b.idx)) if b.idx else b''
    tracks = getattr(b, 'tracks', [])
    tb = b''.join(TRACK.pack(t['first'], t['frames'], t['fps'], t['vat'], t['flags'], 0) for t in tracks)
    tdb = struct.pack('<%df' % len(b.track_data), *[fin(x) for x in b.track_data]) if getattr(b, 'track_data', None) else b''
    vat = bytes(b.vat)
    vat_h = (b.vat_texels + VAT_W - 1) // VAT_W if b.vat_texels else 0
    vat += bytes(vat_h * VAT_W * 8 - len(vat))
    at = b.atlas.encode()
    ab = b''.join(at[2]) if at else b''
    cb = b''.join(CELL.pack(*r) for r in at[3]) if at else b''
    parts = [fb, bytes(eb), kb, sb, nb, wb, stb_, itb, mb, vb, ib, tb, tdb, cb, vat, ab]
    offs = []
    off = HDR.size
    for p in parts:
        offs.append(off)
        off += len(p)
    fo, eo, ko, so, no, wo, sto, ito, mo, vo, io, to, tdo, co, vao, ao = offs
    ncell = len(b.atlas.tex)
    hdr = HDR.pack(VERSION, len(files), fo, len(b.emitters), eo, len(b.keys), ko, len(skills), so, len(npcs), no, len(weapons), wo,
                   FMT_DXT5 if at else 0, at[0] if at else 0, at[1] if at else 0, len(at[2]) if at else 0, ao, len(ab), 0, 0, ncell,
                   len(b.meshes), mo, len(b.verts), vo, len(b.idx), io, VAT_W if vat_h else 0, vat_h, vao, len(vat),
                   len(getattr(b, 'statuses', [])), sto, len(getattr(b, 'items', [])), ito, getattr(b, 'levelup', 0),
                   len(tracks), to, tdo, len(getattr(b, 'track_data', [])), co, getattr(b, 'partyup', 0), 0, 0, 0, 0, 0)
    return hdr + b''.join(parts)


def dec_efct(d):
    """-> dict(files, emitters, keys, skills, npcs, weapons, statuses, items, meshes, verts, idx, tracks, track_data, cells, vat=(w, h, off, bytes), levelup, partyup,
    atlas=(fmt, w, h, levels, bytes, 0, 0, cells)); same bounds as GsvrRoseFx.Valid. ValueError on a bad layout."""
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
    statuses = tab(h[32], h[33], STATUS, 65535)
    items = tab(h[34], h[35], ITEM, 65535)
    meshes = tab(h[22], h[23], MESH, MAX_MESHES)
    verts = tab(h[24], h[25], VERT, 1 << 22)
    tracks = tab(h[37], h[38], TRACK, MAX_TRACKS)
    cells = tab(h[21], h[41], CELL, MAX_CELLS)
    if h[40] > (1 << 24) or (h[40] and (h[39] < HDR.size or h[39] + 4 * h[40] > len(d))):
        raise ValueError('bad EFCT track data')
    tdata = struct.unpack_from('<%df' % h[40], d, h[39]) if h[40] else ()
    if h[26] and (h[27] < HDR.size or h[27] + 2 * h[26] > len(d)):
        raise ValueError('bad EFCT index table')
    idx = struct.unpack_from('<%dH' % h[26], d, h[27]) if h[26] else ()
    for t in (files, skills, npcs, weapons, statuses, items):
        ids = [r[0] for r in t]
        if ids != sorted(set(ids)):
            raise ValueError('EFCT table not sorted')
    for f in files:
        if f[1] + f[2] > len(emit) or not 1 <= f[2] <= MAX_EMITTERS_PER_FILE:
            raise ValueError('EFCT file emitters out of range')
    vat_tex = h[28] * h[29]
    if h[31] != vat_tex * 8 or (h[31] and h[30] + h[31] > len(d)):
        raise ValueError('EFCT vertex animation past the end')
    for m in meshes:
        if m[0] + m[1] > len(verts) or m[1] < 1 or m[1] > MAX_MESH_VERTS or m[3] + m[4] > len(idx) or m[4] % 3:
            raise ValueError('EFCT mesh out of range')
        if any(i >= m[1] for i in idx[m[3]:m[3] + m[4]]):
            raise ValueError('EFCT mesh index out of range')
        for base, bit in ((m[5], 5), (m[6], 2)):
            if (m[2] & bit) and (base == 0xFFFFFFFF or base + m[7] * m[1] > vat_tex or m[7] < 1 or m[8] < 1):
                raise ValueError('EFCT mesh animation out of range')
    for t in tracks:
        if t[1] < 1 or t[2] < 1 or t[0] + 8 * t[1] > len(tdata) or t[3] + 2 * t[1] > vat_tex:
            raise ValueError('EFCT track out of range')
    aw, ah = h[14], h[15]
    for c in cells:
        if c[2] < 4 or c[3] < 4 or c[0] + c[2] > aw or c[1] + c[3] > ah:
            raise ValueError('EFCT cell outside the atlas')
    for e in emit:
        if e[5] + e[6] > len(keys):
            raise ValueError('EFCT emitter keys out of range')
        if e[2] >= h[21]:
            raise ValueError('EFCT emitter cell outside the atlas')
        if e[0] == 1 and e[35] >= len(meshes):
            raise ValueError('EFCT emitter mesh out of range')
        if e[37] != NO_DUMMY and e[37] >= len(tracks):
            raise ValueError('EFCT emitter track out of range')
        for k in range(e[5], e[5] + e[6]):
            if not 1 <= keys[k][0] <= 13 or (keys[k][3] != -1 and not k < keys[k][3] < e[5] + e[6]):
                raise ValueError('EFCT keyframe type / fade target out of range')
    if h[17] + h[18] > len(d):
        raise ValueError('EFCT atlas past the end')
    return dict(files=files, emitters=emit, keys=keys, skills=skills, npcs=npcs, weapons=weapons, statuses=statuses, items=items, meshes=meshes, verts=verts, idx=idx,
                tracks=tracks, track_data=tdata, cells=cells, vat=(h[28], h[29], h[30], h[31]), levelup=h[36], partyup=h[42],
                atlas=(h[13], h[14], h[15], h[16], h[18], h[19], h[20], h[21]))


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
    skills, npcs, weapons, b = build_tables(vfs.read, fs, ls, sk, _stb(vfs, 'LIST_NPC'), _stb(vfs, 'LIST_WEAPON'), status_stb=_stb(vfs, 'LIST_STATUS'),
                                            useitem_stb=_stb(vfs, 'LIST_USEITEM'), type_motion=_stb(vfs, 'TYPE_MOTION'), file_motion=_stb(vfs, 'FILE_MOTION'))
    if not b.emitters:
        return [], dict(warnings={'EFCT: no drawable effect': 1})
    data = enc_efct(b, skills, npcs, weapons)
    np_, npfx, nall = coverage(sk, skills)
    at = b.atlas.encode()
    ca = [a for s in skills for a, c in zip(s['cast_at'], s['cast']) if c]
    info = dict(effects=len([f for f in b.files.values() if f]), emitters=len(b.emitters), keys=len(b.keys), cells=len(b.atlas.rgba), skills=nall,
                player_skills=np_, player_skills_with_effect=npfx, npcs=len(npcs), weapons=len(weapons), bytes=len(data), warnings=dict(b.warn),
                meshes=len(b.meshes), mesh_verts=len(b.verts), vat_texels=b.vat_texels, statuses=len(b.statuses), items=len(b.items), levelup=b.levelup,
                partyup=b.partyup, tracks=len(b.tracks), atlas=(at[0], at[1]) if at else (0, 0), atlas_copied=b.atlas.copied, atlas_halved=b.atlas.halved,
                casts_at_cast=len([a for a in ca if a != CAST_NEVER and not a & CAST_FIRE]), casts_at_fire=len([a for a in ca if a != CAST_NEVER and a & CAST_FIRE]),
                casts_never=len([a for a in ca if a == CAST_NEVER]))
    log('effects: %d files, %d emitters, %d atlas cells (%d x %d, %d copied, %d halved), %d meshes (%d vertices, %d animation texels), %d transform tracks, '
        '%d of %d player skills with an effect (%d skill rows; casts %d at the cast, %d at the fire, %d never), %d npcs, %d weapons, %d statuses, %d items, '
        'level-up %d, party level-up %d, %.2f MB'
        % (info['effects'], info['emitters'], info['cells'], info['atlas'][0], info['atlas'][1], b.atlas.copied, b.atlas.halved, len(b.meshes), len(b.verts),
           b.vat_texels, len(b.tracks), npfx, np_, nall, info['casts_at_cast'], info['casts_at_fire'], info['casts_never'], len(npcs), len(weapons),
           len(b.statuses), len(b.items), b.levelup, b.partyup, len(data) / 1e6))
    return [(TAG_EFCT, 1, len(skills), data)], info
