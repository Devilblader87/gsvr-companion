"""BONT: the pre-baked bone texture of a character pack (docs/RPG1_PACK.md section 27). Stdlib only.

The runtime's actors (GoldSrcVR GsvrRpg1Actors, load stage "bone frames") bake every skin frame of the pack's eager clips into an RGBAFloat
texture: per frame SampleClip + ComposeSkin per joint (GsvrRpg1Chars), in Udon that is the slow part of an actor build. This module does
the same bake from the pack's own SKEL / CANM bytes BIT-EXACT: every float operation of the C# code is done in double and rounded to
float32 at once (array('f')). For + - * / and sqrt that is exactly the IEEE single result (double has more than 2 x 24 + 2 bits), and
Udon executes every operator as its own extern with a System.Single result, so the runtime rounds after every operation too. The
operation order below is the C# expression order, operator by operator (tools/usharp_offline/bont_check.py compares the bytes with
the compiled runtime's own bake).

Which clips are baked: all of them by default. The runtime's eager set (GsvrRpg1Chars.MarkLazyClips) must be in it; the W13 weapon-class
motion sets that only avatars use (lazy: the runtime bakes them on demand when the pack has no BONT) are baked too (lazy_too=True, the default):
they are about 15 % of a 0.9.43 zone's texels, and with them in the section the runtime keeps no CPU copy of the bone texture for later
lazy bakes (_boneRaw, the whole texture) and has no first-equip bake. lazy_too=False writes the eager clips only (the runtime still accepts it).

Section layout (little endian), section version 1:
  0  i32 texels         LayoutBones total (bind frame per skeleton, then every clip's frames)
  4  i32 clipCount      CANM clip count
  8  i32 skelCount      SKEL skeleton count
 12  u32 crcSkel        crc32 of the SKEL section (= its directory CRC)
 16  u32 crcCanm        crc32 of CANM
 20  u32 crcAvat        crc32 of AVAT (0 = the pack has none)
 24  u32 crcActr        crc32 of ACTR (0 = none)
 28  i32 bakedTexels    texels in the data block
 32  i32 riseOff        = 48: f32 rise[clipCount] (root joint lift above its bind height, max over the frames, 0 when not baked)
 36  i32 flagsOff       u8 baked[clipCount] (1 = the clip's frames are in the data block), padded to 4
 40  i32 dataOff        RGBA f32 texels: the bind frames (texels 0 .. 2 * sum(joints) - 1), then the frames of every baked clip in clip order
 44  i32 0
"""
import struct, sys, zlib, math
from array import array

TAG = b'BONT'
SECTION_VERSION = 1
HEADER = struct.Struct('<12i')
EAGER_WEAPON_TYPES = 2              # GsvrRpg1Chars.EagerWeaponTypes
TEX_WIDTH, TEX_MAX_ROWS = 1024, 4096
MAX_PACK_BYTES = 96 * 1024 * 1024  # a pack with BONT stays below this (the companion serves at most 100 MB, companion/rose.py MAX_BYTES); above it the runtime bakes
MOT_COUNT = 12
AR_CLIP = 88
_EPS12 = array('f', [1e-12])[0]     # the C# literal 1e-12f


def _r(xs):
    return array('f', xs).tolist()


def _i16(d, o):
    return struct.unpack_from('<h', d, o)[0]


def _u16(d, o):
    return struct.unpack_from('<H', d, o)[0]


def _i32(d, o):
    return struct.unpack_from('<i', d, o)[0]


# ------------------------------------------------------------------ vector helpers: one C# operator over all frames of a clip
def _add(a, b):
    return _r([x + y for x, y in zip(a, b)])


def _sub(a, b):
    return _r([x - y for x, y in zip(a, b)])


def _mul(a, b):
    return _r([x * y for x, y in zip(a, b)])


def _neg(a):
    return [-x for x in a]


def _qrot(qx, qy, qz, qw, vx, vy, vz):
    """GsvrRpg1Chars.QRotTo: tx = 2f * (qy * vz - qz * vy) ...; r = v + qw * t + (q.xyz x t)"""
    two = [2.0] * len(qx)
    tx = _mul(two, _sub(_mul(qy, vz), _mul(qz, vy)))
    ty = _mul(two, _sub(_mul(qz, vx), _mul(qx, vz)))
    tz = _mul(two, _sub(_mul(qx, vy), _mul(qy, vx)))
    rx = _add(_add(vx, _mul(qw, tx)), _sub(_mul(qy, tz), _mul(qz, ty)))
    ry = _add(_add(vy, _mul(qw, ty)), _sub(_mul(qz, tx), _mul(qx, tz)))
    rz = _add(_add(vz, _mul(qw, tz)), _sub(_mul(qx, ty), _mul(qy, tx)))
    return rx, ry, rz


def _qmul(ax, ay, az, aw, bx, by, bz, bw):
    """GsvrRpg1Chars.QMulTo, left to right"""
    x = _sub(_add(_add(_mul(aw, bx), _mul(ax, bw)), _mul(ay, bz)), _mul(az, by))
    y = _add(_add(_sub(_mul(aw, by), _mul(ax, bz)), _mul(ay, bw)), _mul(az, bx))
    z = _add(_sub(_add(_mul(aw, bz), _mul(ax, by)), _mul(ay, bx)), _mul(az, bw))
    w = _sub(_sub(_sub(_mul(aw, bw), _mul(ax, bx)), _mul(ay, by)), _mul(az, bz))
    return x, y, z, w


def _sqrt(a):
    # Mathf.Sqrt = (float)Math.Sqrt((double)f); NaN / negative -> NaN like Math.Sqrt
    return _r([math.sqrt(x) if x >= 0.0 else float('nan') for x in a])


def _qnorm(x, y, z, w):
    """GsvrRpg1Chars.QNormAt"""
    n = _sqrt(_add(_add(_add(_mul(x, x), _mul(y, y)), _mul(z, z)), _mul(w, w)))
    ox, oy, oz, ow = [], [], [], []
    for i, ni in enumerate(n):
        if ni > _EPS12:
            ox.append(x[i] / ni); oy.append(y[i] / ni); oz.append(z[i] / ni); ow.append(w[i] / ni)
        else:
            ox.append(0.0); oy.append(0.0); oz.append(0.0); ow.append(1.0)
    return _r(ox), _r(oy), _r(oz), _r(ow)


# ------------------------------------------------------------------ section readers (the runtime's accessors)
def _skeletons(skel):
    n, stride, jo = _i32(skel, 0), _i32(skel, 4), _i32(skel, 8)
    out = []
    for s in range(n):
        first, cnt = _i32(skel, 16 + s * 32), _i32(skel, 16 + s * 32 + 4)
        joints = []
        for k in range(cnt):
            p = jo + (first + k) * stride
            joints.append((_i16(skel, p), struct.unpack_from('<3f', skel, p + 4), struct.unpack_from('<4f', skel, p + 16),
                           struct.unpack_from('<3f', skel, p + 32), struct.unpack_from('<4f', skel, p + 44)))
        out.append(joints)
    return out


def _clips(canm):
    n, co = _i32(canm, 0), _i32(canm, 8)
    return [(_i32(canm, co + c * 40), _i32(canm, co + c * 40 + 4), _i32(canm, co + c * 40 + 8), _i32(canm, co + c * 40 + 12), _i32(canm, co + c * 40 + 16))
            for c in range(n)]          # skeleton, frames, flags, firstTrack, trackCount


def mark_lazy(avat, actr, clip_count):
    """GsvrRpg1Chars.MarkLazyClips: clips only the weapon-class sets (AVAT clip map weapon type >= 2) use, and no unarmed / sword row, no ACTR actor"""
    lazy = [False] * clip_count
    if avat is None or clip_count <= 0:
        return lazy
    n, mo = _i32(avat, 12), _i32(avat, 28)
    rows = [(avat[mo + i * 8 + 2], _u16(avat, mo + i * 8 + 4)) for i in range(n)]
    for wt, c in rows:
        if c < clip_count and wt >= EAGER_WEAPON_TYPES:
            lazy[c] = True
    for wt, c in rows:
        if c < clip_count and wt < EAGER_WEAPON_TYPES:
            lazy[c] = False
    if actr is not None:
        na, stride, ro = _i32(actr, 0), _i32(actr, 4), _i32(actr, 8)
        for a in range(na):
            for m in range(MOT_COUNT):
                c = _i16(actr, ro + a * stride + AR_CLIP + m * 2)
                if 0 <= c < clip_count:
                    lazy[c] = False
    return lazy


def layout(skels, clips):
    """GsvrRpg1Chars.LayoutBones -> (bindBase[], clipBase[], total texels)"""
    t, bind, base = 0, [], []
    for sk in skels:
        bind.append(t)
        t += 2 * len(sk)
    for c in clips:
        base.append(t)
        t += c[1] * 2 * len(skels[c[0]])
    return bind, base, t


# ------------------------------------------------------------------ the bake (GsvrRpg1Actors.StepBones for one clip, all frames at once)
def _sample(canm, clip, joints):
    """GsvrRpg1Chars.SampleClip at t = 0, 1, .. frames-1 (integer frames: frac 0, so no slerp; lerps of 0 kept as in C#)"""
    sk, nf, flags, ft, nt = clip
    loop = (flags & 1) != 0
    jc = len(joints)
    i0 = list(range(nf))
    i1 = [i + 1 if i + 1 < nf else (0 if loop else nf - 1) for i in i0]
    zero = [0.0] * nf
    lp = [[[j[1][a]] * nf for a in range(3)] for j in joints]
    lq = [[[j[2][a]] * nf for a in range(4)] for j in joints]
    ls = [[1.0] * nf for _ in joints]
    to, data = _i32(canm, 12), _i32(canm, 20)
    for k in range(nt):
        tp = to + (ft + k) * 16
        j, mask, p = _u16(canm, tp), canm[tp + 2], data + _i32(canm, tp + 4)
        if j >= jc:
            continue
        if mask & 1:
            m = struct.unpack_from('<6f', canm, p)
            q = struct.unpack_from('<%dH' % (3 * nf), canm, p + 24)
            for a in range(3):
                ua = _r([q[i * 3 + a] / 65535.0 for i in i0])
                ub = _r([q[i * 3 + a] / 65535.0 for i in i1])
                va = _r([m[a] + x for x in _r([x * m[3 + a] for x in ua])])
                vb = _r([m[a] + x for x in _r([x * m[3 + a] for x in ub])])
                lp[j][a] = _add(va, _mul(_sub(vb, va), zero))
            p += 24 + ((6 * nf + 3) // 4) * 4
        if mask & 2:
            q = struct.unpack_from('<%dh' % (4 * nf), canm, p)
            c = [_r([q[i * 4 + a] / 32767.0 for i in i0]) for a in range(4)]
            la = _sqrt(_add(_add(_add(_mul(c[0], c[0]), _mul(c[1], c[1])), _mul(c[2], c[2])), _mul(c[3], c[3])))
            out = [[], [], [], []]
            for i, li in enumerate(la):
                for a in range(4):
                    out[a].append(c[a][i] / li if li > 0.0 else c[a][i])
            lq[j] = [_r(o) for o in out]
            p += 8 * nf
        if mask & 4:
            sa = struct.unpack_from('<%df' % nf, canm, p)
            a_ = [sa[i] for i in i0]
            b_ = [sa[i] for i in i1]
            ls[j] = _add(a_, _mul(_sub(b_, a_), zero))
    return lp, lq, ls


def _compose(joints, lp, lq, ls, nf):
    """GsvrRpg1Chars.ComposeSkin over all frames -> per joint (qx, qy, qz, qw, px, py, pz) vectors + the root's global y"""
    jc = len(joints)
    gp, gq, gs = [None] * jc, [None] * jc, [None] * jc
    for k in range(jc):
        par = joints[k][0]
        if par < 0:
            gp[k], gq[k], gs[k] = list(lp[k]), list(lq[k]), ls[k]
        else:
            ps = gs[par]
            v = [_mul(lp[k][a], ps) for a in range(3)]
            rx, ry, rz = _qrot(gq[par][0], gq[par][1], gq[par][2], gq[par][3], v[0], v[1], v[2])
            gp[k] = [_add(rx, gp[par][0]), _add(ry, gp[par][1]), _add(rz, gp[par][2])]
            gq[k] = list(_qnorm(*_qmul(*gq[par], *lq[k])))
            gs[k] = _mul(ps, ls[k])
    out = []
    for k in range(jc):
        bpos, brot = joints[k][3], joints[k][4]
        ix, iy, iz, iw = [-brot[0]] * nf, [-brot[1]] * nf, [-brot[2]] * nf, [brot[3]] * nf
        t = _qrot(ix, iy, iz, iw, [-bpos[0]] * nf, [-bpos[1]] * nf, [-bpos[2]] * nf)
        t = _qrot(gq[k][0], gq[k][1], gq[k][2], gq[k][3], t[0], t[1], t[2])
        px, py, pz = _add(t[0], gp[k][0]), _add(t[1], gp[k][1]), _add(t[2], gp[k][2])
        qx, qy, qz, qw = _qnorm(*_qmul(gq[k][0], gq[k][1], gq[k][2], gq[k][3], ix, iy, iz, iw))
        out.append((qx, qy, qz, qw, px, py, pz))
    return out, gp[0][1]


def bake(skel, canm, avat=None, actr=None, lazy_too=True):
    """-> dict(texels, bind_texels, lazy=[bool] (MarkLazyClips), baked=[bool], rise=[f32], data=array('f') of the bind frames + baked clips, baked_texels)"""
    skels = _skeletons(skel)
    clips = _clips(canm)
    _bind, base, texels = layout(skels, clips)
    lazy = mark_lazy(avat, actr, len(clips))
    bind_texels = 2 * sum(len(s) for s in skels)
    data = array('f')
    for s in skels:                                   # WriteBindFrame: identity quaternion, position 0
        data.extend([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0] * len(s))
    rise = [0.0] * len(clips)
    baked = [lazy_too or not lz for lz in lazy]
    for c, clip in enumerate(clips):
        if not baked[c]:
            continue
        joints = skels[clip[0]]
        nf = clip[1]
        lp, lq, ls = _sample(canm, clip, joints)
        tex, root_y = _compose(joints, lp, lq, ls, nf)
        ry = _r([y - joints[0][3][1] for y in root_y])
        m = 0.0
        for v in ry:
            if v > m:
                m = v
        rise[c] = m
        frame = []
        for f in range(nf):
            for (qx, qy, qz, qw, px, py, pz) in tex:
                frame += (qx[f], qy[f], qz[f], qw[f], px[f], py[f], pz[f], 0.0)
        data.extend(frame)
    return dict(texels=texels, bind_texels=bind_texels, lazy=lazy, baked=baked, rise=rise, data=data, baked_texels=len(data) // 4,
                clips=len(clips), skels=len(skels))


def bont_section(skel, canm, avat=None, actr=None, lazy_too=True):
    """-> (tag, version, count, bytes) for the pack, or None when the pack has no skeleton / clip or its texture would not fit"""
    if not skel or not canm:
        return None
    b = bake(skel, canm, avat, actr, lazy_too)
    if b['clips'] == 0 or b['texels'] < 1 or (b['texels'] + TEX_WIDTH - 1) // TEX_WIDTH > TEX_MAX_ROWS:
        return None
    n = b['clips']
    rise_off = HEADER.size
    flags_off = rise_off + 4 * n
    data_off = flags_off + ((n + 3) & ~3)
    crc = lambda x: (zlib.crc32(x) & 0xffffffff) if x else 0
    u2i = lambda v: v - (1 << 32) if v >= 1 << 31 else v
    hdr = HEADER.pack(b['texels'], n, b['skels'], u2i(crc(skel)), u2i(crc(canm)), u2i(crc(avat)), u2i(crc(actr)), b['baked_texels'],
                      rise_off, flags_off, data_off, 0)
    rise = array('f', b['rise'])
    data = b['data']
    if sys.byteorder != 'little':
        rise.byteswap(); data = array('f', data); data.byteswap()
    flags = bytes(1 if bk else 0 for bk in b['baked'])
    flags += b'\0' * (-len(flags) % 4)
    return (TAG, SECTION_VERSION, n, hdr + rise.tobytes() + flags + data.tobytes())


def from_sections(sections, max_pack=MAX_PACK_BYTES, lazy_too=True):
    """BONT for a section list [(tag, ver, count, bytes)] (rose_pack.convert): uses its SKEL / CANM / AVAT / ACTR; None without characters or when
    the pack with it would pass max_pack bytes (sections + directory, the runtime then bakes the frames itself)"""
    by = {t: d for t, _v, _c, d in sections}
    bt = bont_section(by.get(b'SKEL'), by.get(b'CANM'), by.get(b'AVAT'), by.get(b'ACTR'), lazy_too)
    if bt is None:
        return None
    total = 32 + sum((len(d) + 3) & ~3 for _t, _v, _c, d in sections) + len(bt[3]) + 24 * (len(sections) + 1)
    return bt if total <= max_pack else None
