"""Ragnarok Online skill effects (.str) -> compact REFX / RFTX sections (stdlib only). Design: docs/design/ro_sound.md.

BYOG: inputs are bytes of the player's own client (data\\texture\\effect\\*.str + the textures they name). Nothing of it is in the repo; tests write their own
synthetic STR files (write_str below).

STR (version 148, "STRM") as documented by the Ragnarok Research Lab (file-formats/str) and roBrowser's StrEffect:
  header 36 bytes: "STRM", u32 version, u32 fps, u32 maxKey (last frame), u32 layerCount, 16 reserved
  layer: u32 texCount, texCount x 128-byte names (relative to data\\texture\\effect\\, NUL padded), u32 keyCount, keyCount x 124-byte keys
  key:   i32 frame, u32 type (0 = base key, 1 = animated key), f32 x, y (centre), f32 uv[8] (the first 4 = u, v, u+us, v+vs; the last 4 repeat them), f32 x[4], y[4] (quad corners, relative to the centre),
         f32 aniframe (texture index, animated keys), u32 anitype, f32 delay, f32 angle (1024 = 360 degrees; stored in degrees), f32 r, g, b, a (0..255),
         u32 srcalpha, u32 destalpha, u32 mtpreset
A layer shows between its first and last key: a type 0 key is a new base state; a type 1 key is the base state + the key's offsets (position, corners,
angle, colour, texture frame) - the client adds the offsets of the animated key to the last base key and eases to them over the frames between.

Output (all little endian):
  REFX section (count = effects)
    header   u32 effectCount, u32 layerCount, u32 keyCount, u32 nameBytes, u32 texCount, u32 reserved x3        = 32
    effect   <6i>: nameOff, nameLen, fps, maxFrame, firstLayer, layerCount                                      = 24 each
    layer    <4i>: firstKey, keyCount, texFirst (index into the texture index table), texCount                  = 16 each
    key      <2h 15f 2h>: frame i16, kind i16 (0 base / 1 animated), x y u v us vs, cx0..3 / cy0..3 as 8 f32 ... (see KEY_FMT)
    texture index table: i32 per layer texture = slot in RFTX (-1 = texture missing), names (ASCII effect names)
  RFTX section: one DXT5 atlas of all textures the effects use (slot 128 px, 3 mips), header 8 x u32 like TLMP (format, width, height, mips, slots, slot_px, columns, 0)
The runtime (GsvrRoFx) draws one camera facing quad per layer with the interpolated key; blend = (srcalpha, destalpha): 5 / 2 = additive-ish (SRCALPHA, ONE), 5 / 6 = alpha.
"""
import math
import os
import struct

from . import mappack
from .grf import norm
import rosedds as D  # noqa: E402  (the shared DXT helpers mappack uses)

MAGIC = b'STRM'
HDR_BYTES = 36
KEY_BYTES = 124
_KEY = struct.Struct('<iI2f8f8ffIff4f3I')     # frame type pos[2] uv[8] xy[8] aniframe anitype delay angle color[4] src dst mtpreset
TAG_REFX = b'REFX'
TAG_RFTX = b'RFTX'
SECTION_VERSION = 1
EFFECT_DIR = 'texture\\effect\\'


class StrError(ValueError):
    pass


def parse_str(d):
    """-> dict(version, fps, max_frame, layers=[dict(textures=[name], keys=[dict(frame, type, pos, uv, xy, aniframe, anitype, delay, angle, color, src, dst)])]).
    Raises StrError when the bytes are not exactly a well formed STR (size must match to the byte)."""
    if len(d) < HDR_BYTES or d[:4] != MAGIC:
        raise StrError('not a STR file')
    ver, fps, mk, lc = struct.unpack_from('<4I', d, 4)
    if lc > 256 or fps < 1 or fps > 1000:
        raise StrError('implausible header (layers %d, fps %d)' % (lc, fps))
    o = HDR_BYTES
    layers = []
    try:
        for _ in range(lc):
            tc, = struct.unpack_from('<I', d, o)
            o += 4
            if tc > 4096 or o + 128 * tc > len(d):
                raise StrError('texture table out of range')
            tex = [d[o + i * 128:o + i * 128 + 128].split(b'\0')[0].decode('cp949', 'replace') for i in range(tc)]
            o += 128 * tc
            kc, = struct.unpack_from('<I', d, o)
            o += 4
            if kc > 65536 or o + KEY_BYTES * kc > len(d):
                raise StrError('key table out of range')
            keys = []
            for _k in range(kc):
                t = _KEY.unpack_from(d, o)
                o += KEY_BYTES
                keys.append(dict(frame=t[0], type=t[1], pos=t[2:4], xy=t[12:20], aniframe=t[20], anitype=t[21], delay=t[22], angle=t[23] * (360.0 / 1024.0),
                                 color=t[24:28], src=t[28], dst=t[29]))
            layers.append(dict(textures=tex, keys=keys))
    except struct.error as e:
        raise StrError('truncated: %s' % e)
    if o != len(d):
        raise StrError('%d trailing bytes' % (len(d) - o))
    return dict(version=ver, fps=fps, max_frame=mk, layers=layers)


def write_str(effect, version=148):
    """test / fixture helper: the inverse of parse_str (same dict shape)."""
    out = bytearray(struct.pack('<4sIIII', MAGIC, version, effect['fps'], effect['max_frame'], len(effect['layers'])) + b'\0' * 16)
    for ly in effect['layers']:
        out += struct.pack('<I', len(ly['textures']))
        for t in ly['textures']:
            out += t.encode('cp949')[:127].ljust(128, b'\0')
        out += struct.pack('<I', len(ly['keys']))
        for k in ly['keys']:
            out += _KEY.pack(k['frame'], k['type'], *k['pos'], 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, *k['xy'], k['aniframe'], k['anitype'], k['delay'], k['angle'] * (1024.0 / 360.0), *k['color'], k['src'], k['dst'], 0)
    return bytes(out)


# ------------------------------------------------------------------ runtime semantics (mirrored 1:1 by GsvrRoFx in Udon)
# Verified on the real client data (h_levelup.str): a type 0 key is an absolute state; a type 1 key (same frame) holds PER-FRAME DELTAS of that state
# (pos, corner offsets, angle, colour): base (320, 81) + anim (0, 32.25) at frame 10 -> the next base key at frame 14 is (320, 210) = 81 + 4 x 32.25.
# uv is the unit rectangle in all 134,275 base keys of the iRO client, so it is not stored (corner order: 0 right-top, 1 left-top, 2 left-bottom, 3 right-bottom
# for x = 32, -32, -32, 32 / y = -32, -32, 32, 32: u = 1, 0, 0, 1, v = 0, 0, 1, 1).
PX_PER_M = 35.0                # STR pixels per world metre (roBrowser's effect scale; 64 px quad = 1.8 m)
ORIGIN_X, ORIGIN_Y = 320.0, 292.0     # the effect's feet point in the 640x480 STR screen space
CORNER_U = (1.0, 0.0, 0.0, 1.0)
CORNER_V = (0.0, 0.0, 1.0, 1.0)
BLEND_ADD, BLEND_ALPHA = 0, 1


def blend_class(src, dst):
    """D3DBLEND pair of the key -> 0 additive (dst ONE / DESTALPHA, the glowing sparkle textures with a black background) or 1 normal alpha blend (dst INVSRCALPHA / ZERO)."""
    return BLEND_ALPHA if dst in (1, 6) else BLEND_ADD


def sample_layer(keys, frame):
    """-> None (layer hidden at `frame`) or dict(pos, xy, angle, color, aniframe) of the state at `frame`.
    Hidden before the first key and after the last key. a = last base key (type 0) at or before the frame; m = last animated key (type 1) with a.frame <= m.frame <= frame;
    state = a + (frame - a.frame) * m  (pos 2, corners 8, angle, colour 4). Without m the state is a (held)."""
    if not keys:
        return None
    if frame < keys[0]['frame'] or frame > keys[-1]['frame']:
        return None
    a = m = None
    for k in keys:
        if k['frame'] > frame:
            break
        if k['type'] == 0:
            a = k
            m = None
        elif a is not None and k['frame'] >= a['frame']:
            m = k
    if a is None:
        return None
    d = float(frame - a['frame'])
    st = dict(pos=list(a['pos']), xy=list(a['xy']), angle=a['angle'], color=list(a['color']), aniframe=a['aniframe'], anitype=a['anitype'], delay=a['delay'],
              src=a['src'], dst=a['dst'], start=a['frame'])
    if m is not None and d > 0:
        st['pos'] = [a['pos'][i] + m['pos'][i] * d for i in range(2)]
        st['xy'] = [a['xy'][i] + m['xy'][i] * d for i in range(8)]
        st['angle'] = a['angle'] + m['angle'] * d
        st['color'] = [a['color'][i] + m['color'][i] * d for i in range(4)]
    return st


def texture_frame(st, frame, tex_count):
    """index into the layer's texture list: anitype 0 = aniframe (static), 1 = + delay per frame (clamped to the last), 2 / 3 / 4 = + delay per frame, looping (3 / 4 approximated as 2)."""
    if tex_count <= 1:
        return 0
    f = st['aniframe']
    if st['anitype'] in (1, 2, 3, 4):
        f += (frame - st['start']) * st['delay']
    i = int(math.floor(f))
    if st['anitype'] == 1:
        return max(0, min(tex_count - 1, i))
    return i % tex_count if st['anitype'] else max(0, min(tex_count - 1, i))


def corner_world(st, c, scale=1.0):
    """corner c (0..3) of a sampled state -> (x right, y up) metres relative to the effect origin: the corner offset is rotated by the layer angle (degrees) about the layer centre,
    1 STR pixel = 1 / PX_PER_M m (GsvrRoFx.LCornerX / LCornerY, same formula)."""
    cx, cy = st['pos'][0] - ORIGIN_X, st['pos'][1] - ORIGIN_Y
    x, y = cx + st['xy'][c], cy + st['xy'][4 + c]
    r = math.radians(st['angle'])
    cs, sn = math.cos(r), math.sin(r)
    return ((cx + (x - cx) * cs - (y - cy) * sn) / PX_PER_M * scale, -(cy + (x - cx) * sn + (y - cy) * cs) / PX_PER_M * scale)


# ------------------------------------------------------------------ REFX / RFTX writer
SLOT_PX = 128
MIPS = 3
KEY_FMT = '<2h2f8f3f4f4h'
KEY_OUT_BYTES = 80


def enc_key(k):
    """one key = 80 bytes: i16 frame, i16 type, f32 pos[2], f32 xy[8], f32 angle (degrees), f32 aniframe, f32 delay, f32 color[4] (0..255, deltas for type 1), i16 src, dst, anitype, 0."""
    return struct.pack(KEY_FMT, max(-32768, min(32767, int(k['frame']))), int(k['type']), *k['pos'], *k['xy'], k['angle'], k['aniframe'], k['delay'], *k['color'],
                       int(k['src']), int(k['dst']), int(k['anitype']), 0)


def dec_key(d, o):
    t = struct.unpack_from(KEY_FMT, d, o)
    return dict(frame=t[0], type=t[1], pos=t[2:4], xy=t[4:12], angle=t[12], aniframe=t[13], delay=t[14], color=t[15:19], src=t[19], dst=t[20], anitype=t[21])


def enc_refx(effects, tex_slots):
    """effects: [(name, parsed STR dict)], tex_slots: {texture name (lower) -> RFTX slot}. -> section bytes."""
    names = bytearray()
    eff = bytearray()
    lay = bytearray()
    keys = bytearray()
    texi = bytearray()
    nlay = nkey = ntex = 0
    for name, e in effects:
        nm = name.encode('ascii')
        eff += struct.pack('<6i', len(names), len(nm), e['fps'], e['max_frame'], nlay, len(e['layers']))
        names += nm
        for ly in e['layers']:
            lay += struct.pack('<4i', nkey, len(ly['keys']), ntex, len(ly['textures']))
            for t in ly['textures']:
                texi += struct.pack('<i', tex_slots.get(norm(t), -1))
            ntex += len(ly['textures'])
            for k in ly['keys']:
                keys += enc_key(k)
            nkey += len(ly['keys'])
            nlay += 1
    while len(names) % 4:
        names += b'\0'
    hdr = struct.pack('<8I', len(effects), nlay, nkey, len(names), ntex, 0, 0, 0)
    return bytes(hdr + eff + lay + keys + texi + names)


def dec_refx(d):
    """-> [dict(name, fps, max_frame, layers=[dict(texslots=[int], keys=[key])])] (test helper / reference decoder for GsvrRoFx.ReadEffects)."""
    ne, nl, nk, nb, nt = struct.unpack_from('<5I', d, 0)
    o = 32
    effs = [struct.unpack_from('<6i', d, o + 24 * i) for i in range(ne)]
    o += 24 * ne
    lays = [struct.unpack_from('<4i', d, o + 16 * i) for i in range(nl)]
    o += 16 * nl
    ko = o
    o += KEY_OUT_BYTES * nk
    texs = list(struct.unpack_from('<%di' % nt, d, o)) if nt else []
    o += 4 * nt
    names = d[o:o + nb]
    if o + nb != len(d):
        raise StrError('REFX size mismatch')
    out = []
    for no, nlen, fps, mf, fl, lc in effs:
        layers = []
        for fk, kc, tf, tc in lays[fl:fl + lc]:
            layers.append(dict(texslots=texs[tf:tf + tc], keys=[dec_key(d, ko + KEY_OUT_BYTES * (fk + j)) for j in range(kc)]))
        out.append(dict(name=names[no:no + nlen].decode('ascii'), fps=fps, max_frame=mf, layers=layers))
    return out


def build_atlas(images, spx=SLOT_PX, mips=MIPS):
    """images: list of spx x spx RGBA -> dict(fmt DXT5, w, h, levels, slots, cols). Slot i = column i % cols, row i // cols. DXT5 (the TGAs carry real alpha)."""
    n = len(images)
    cols = max(1, int(math.ceil(math.sqrt(n))))
    rows = (n + cols - 1) // cols
    W, H = cols * spx, rows * spx
    if W > 4096 or H > 4096:
        raise StrError('%d effect textures do not fit one 4096 atlas' % n)
    rowsb = [bytearray(W * 4) for _ in range(H)]
    for si, px in enumerate(images):
        col, row = si % cols, si // cols
        for yy in range(spx):
            rowsb[row * spx + yy][col * spx * 4:(col + 1) * spx * 4] = px[yy * spx * 4:(yy + 1) * spx * 4]
    rgba = b''.join(bytes(r) for r in rowsb)
    levels = []
    lw, lh = W, H
    for L in range(mips):
        levels.append(mappack._enc_blocks(rgba, lw, lh, True))
        if L + 1 < mips:
            rgba, lw, lh = D.downsample2(rgba, lw, lh)
    return dict(fmt=D.FMT_DXT5, w=W, h=H, levels=levels, slots=n, cols=cols, slot_px=spx)


RFTX_HDR = struct.Struct('<8I')


def enc_rftx(atlas):
    return RFTX_HDR.pack(atlas['fmt'], atlas['w'], atlas['h'], len(atlas['levels']), atlas['slots'], atlas['slot_px'], atlas['cols'], 0) + b''.join(atlas['levels'])


def dec_rftx(d):
    fmt, w, h, mips, slots, spx, cols, _r = RFTX_HDR.unpack_from(d, 0)
    levels = []
    pos = RFTX_HDR.size
    lw, lh = w, h
    for _ in range(mips):
        n = D.level_size(fmt, lw, lh)
        levels.append(bytes(d[pos:pos + n]))
        pos += n
        lw, lh = max(1, lw // 2), max(1, lh // 2)
    if pos != len(d):
        raise StrError('RFTX size mismatch')
    return dict(fmt=fmt, w=w, h=h, mips=mips, slots=slots, slot_px=spx, cols=cols, levels=levels)
