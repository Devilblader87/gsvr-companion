"""Ragnarok Online map file parsers (GAT, GND, RSW, RSM v1). Stdlib only, read only.

Written from the publicly documented layouts (community format notes as used by roBrowser / BrowEdit / rAthena tooling, read as
documentation; no code copied). All values are kept in RO file space: Y points DOWN (heights are negative upwards), one GAT cell
= 5 units, one GND tile = `zoom` units (10). docs/design/ro_pack.md has the conversion to Unity.
"""
import struct
from .grf import decode_name


class FormatError(ValueError):
    pass


class _R:
    __slots__ = ('d', 'p')

    def __init__(self, d, p=0):
        self.d = d; self.p = p

    def need(self, n):
        if self.p + n > len(self.d):
            raise FormatError('truncated at %d (+%d of %d)' % (self.p, n, len(self.d)))

    def u8(self):
        self.need(1); v = self.d[self.p]; self.p += 1; return v

    def i32(self):
        self.need(4); v = struct.unpack_from('<i', self.d, self.p)[0]; self.p += 4; return v

    def u16(self):
        self.need(2); v = struct.unpack_from('<H', self.d, self.p)[0]; self.p += 2; return v

    def f32(self):
        self.need(4); v = struct.unpack_from('<f', self.d, self.p)[0]; self.p += 4; return v

    def fs(self, n):
        self.need(4 * n); v = struct.unpack_from('<%df' % n, self.d, self.p); self.p += 4 * n; return v

    def s(self, n):
        self.need(n); v = decode_name(bytes(self.d[self.p:self.p + n])); self.p += n; return v

    def raw(self, n):
        self.need(n); v = self.d[self.p:self.p + n]; self.p += n; return v


def _ver(r, magic):
    if bytes(r.raw(4)) != magic:
        raise FormatError('not a %s file' % magic.decode())
    major = r.u8(); minor = r.u8()
    return (major << 8) | minor


# ------------------------------------------------------------------ GAT (walkability + heights)
GAT_WALKABLE = (0, 2, 3, 4, 6)        # 1 = blocked, 5 = gap / cliff (shootable, not walkable); unknown types count as blocked


def gat(d):
    """-> dict(version, w, h, heights=[(h1,h2,h3,h4)...] row-major from cell (0,0) = south-west, types=[...]).
    h1 = (x, y) corner, h2 = (x+1, y), h3 = (x, y+1), h4 = (x+1, y+1); RO units, Y down."""
    r = _R(d)
    ver = _ver(r, b'GRAT')
    w = r.i32(); h = r.i32()
    if w <= 0 or h <= 0 or w > 4096 or h > 4096:
        raise FormatError('GAT size %dx%d' % (w, h))
    r.need(w * h * 20)
    hs = []; ts = []
    st = struct.Struct('<4fI')
    p = r.p
    for i in range(w * h):
        a, b, c, e, t = st.unpack_from(d, p + i * 20)
        hs.append((a, b, c, e)); ts.append(t)
    return dict(version=ver, w=w, h=h, heights=hs, types=ts)


# ------------------------------------------------------------------ GND (ground mesh)
def gnd(d):
    """-> dict(version, w, h, zoom, textures=[path relative to data\\texture\\], lightmaps=dict(n, cw, ch, data),
    surfaces=[(u1..u4, v1..v4, tex, lm, (r,g,b,a))], cubes=[(h1,h2,h3,h4, up, front, right)], water=None|dict)."""
    r = _R(d)
    ver = _ver(r, b'GRGN')
    if ver < 0x106:
        raise FormatError('GND version %d.%d is not supported (needs >= 1.6)' % (ver >> 8, ver & 255))
    w = r.i32(); h = r.i32(); zoom = r.f32()
    if w <= 0 or h <= 0 or w > 2048 or h > 2048:
        raise FormatError('GND size %dx%d' % (w, h))
    nt = r.i32(); tl = r.i32()
    if nt < 0 or nt > 4096 or tl <= 0 or tl > 1024:
        raise FormatError('GND texture table %d x %d' % (nt, tl))
    textures = [r.s(tl) for _ in range(nt)]
    nl = r.i32(); cw = r.i32(); ch = r.i32(); _gs = r.i32()
    if nl < 0 or cw <= 0 or ch <= 0 or cw * ch > 4096:
        raise FormatError('GND lightmaps %d %dx%d' % (nl, cw, ch))
    lsz = cw * ch * 4
    lmdata = r.raw(nl * lsz)
    ns = r.i32()
    if ns < 0:
        raise FormatError('GND surface count')
    r.need(ns * 40)
    surfs = []
    st = struct.Struct('<8fhH4B')          # 40 bytes
    for i in range(ns):
        v = st.unpack_from(d, r.p + i * 40)
        surfs.append((v[0:4], v[4:8], v[8], v[9], (v[12], v[11], v[10], v[13])))     # file colour is B G R A
    r.p += ns * 40
    r.need(w * h * 28)
    cubes = []
    cs = struct.Struct('<4f3i')
    for i in range(w * h):
        cubes.append(cs.unpack_from(d, r.p + i * 28))
    r.p += w * h * 28
    water = None
    if ver >= 0x108 and r.p + 24 <= len(d):
        lvl = r.f32(); typ = r.i32(); wh = r.f32(); ws = r.f32(); wp = r.f32(); an = r.i32()
        water = dict(level=lvl, type=typ, wave_height=wh, wave_speed=ws, wave_pitch=wp, anim_speed=an)
        if ver >= 0x109 and r.p + 8 <= len(d):
            su = r.i32(); sv = r.i32()
            levels = []
            if 0 < su * sv <= 65536 and r.p + su * sv * 24 <= len(d):
                for _ in range(su * sv):
                    levels.append(r.f32()); r.raw(20)
            water['split'] = (su, sv); water['levels'] = levels
    return dict(version=ver, w=w, h=h, zoom=zoom, textures=textures,
                lightmaps=dict(n=nl, cw=cw, ch=ch, data=bytes(lmdata)), surfaces=surfs, cubes=cubes, water=water)


def lightmap_px(g, lm, x, y):
    """(shadow 0..255, (r, g, b)) of lightmap `lm` pixel (x, y). Layout per lightmap: cw*ch shadow bytes, then cw*ch RGB."""
    L = g['lightmaps']
    if lm < 0 or lm >= L['n']:
        return 255, (0, 0, 0)
    cw, ch = L['cw'], L['ch']
    base = lm * cw * ch * 4
    x = min(cw - 1, max(0, x)); y = min(ch - 1, max(0, y))
    i = y * cw + x
    d = L['data']
    o = base + cw * ch + i * 3
    return d[base + i], (d[o], d[o + 1], d[o + 2])


def lightmap_sample(g, lm, x, y):
    """Bilinear sample of lightmap `lm` at continuous pixel coordinates (texel centres at k + 0.5, like the client's filtered
    lightmap texture) -> (shadow 0..255 float, (r, g, b) floats). A tile's 6x6 interior spans x, y = 1..7; the one pixel border
    repeats the neighbour, so x = 1 / 7 is the shared tile edge. lm < 0 (no lightmap) -> fully lit, no colour."""
    L = g['lightmaps']
    if lm < 0 or lm >= L['n']:
        return 255.0, (0.0, 0.0, 0.0)
    cw, ch = L['cw'], L['ch']
    fx = min(cw - 1.0, max(0.0, x - 0.5)); fy = min(ch - 1.0, max(0.0, y - 0.5))
    x0 = min(cw - 2, int(fx)) if cw > 1 else 0; y0 = min(ch - 2, int(fy)) if ch > 1 else 0
    ax = fx - x0; ay = fy - y0
    x1 = min(cw - 1, x0 + 1); y1 = min(ch - 1, y0 + 1)
    d = L['data']; base = lm * cw * ch * 4; cb = base + cw * ch
    out = [0.0, 0.0, 0.0, 0.0]
    for xx, yy, wgt in ((x0, y0, (1 - ax) * (1 - ay)), (x1, y0, ax * (1 - ay)), (x0, y1, (1 - ax) * ay), (x1, y1, ax * ay)):
        if wgt <= 0.0:
            continue
        i = yy * cw + xx
        out[0] += d[base + i] * wgt
        o = cb + i * 3
        out[1] += d[o] * wgt; out[2] += d[o + 1] * wgt; out[3] += d[o + 2] * wgt
    return out[0], (out[1], out[2], out[3])


# ------------------------------------------------------------------ RSW (world: files, water, light, objects)
def _looks_name(b, ext):
    s = bytes(b).split(b'\0', 1)[0].lower()
    return s.endswith(ext) and len(s) > len(ext)


def rsw(d):
    """-> dict(version, build, ini, gnd, gat, water=None|dict, light=dict, models=[dict(name, file, node, pos, rot, scale)],
    lights, sounds, effects counts, warnings)."""
    r = _R(d)
    ver = _ver(r, b'GRSW')
    warn = []
    build = 0
    # the bytes between the version and the file names grew over the years (2.2: u8, 2.5: i32 build + u8); pick the layout whose
    # GND name field (second 40-byte name) really holds a .gnd file name
    start = r.p
    skip = None
    for k in ((0, 1, 5, 4, 8) if ver >= 0x202 else (0,)):
        p = start + k + 40
        if p + 40 <= len(d) and _looks_name(d[p:p + 40], b'.gnd'):
            skip = k; break
    if skip is None:
        skip = 0; warn.append('RSW: gnd name not found, assuming no extra header bytes')
    if skip >= 4:
        build = struct.unpack_from('<i', d, start)[0]
    elif skip == 1:
        build = d[start]
    r.p = start + skip
    ini = r.s(40); gndf = r.s(40)
    gatf = r.s(40) if ver >= 0x104 else ''
    _src = r.s(40)
    water = None
    if 0x103 <= ver < 0x206:
        water = dict(level=r.f32())
        if ver >= 0x108:
            water.update(type=r.i32(), wave_height=r.f32(), wave_speed=r.f32(), wave_pitch=r.f32())
        if ver >= 0x109:
            water['anim_speed'] = r.i32()
    light = dict(longitude=45, latitude=45, diffuse=(1.0, 1.0, 1.0), ambient=(0.3, 0.3, 0.3), opacity=1.0)
    if ver >= 0x105:
        light['longitude'] = r.i32(); light['latitude'] = r.i32()
        light['diffuse'] = r.fs(3); light['ambient'] = r.fs(3)
        if ver >= 0x107:
            light['opacity'] = r.f32()
    if ver >= 0x106:
        r.raw(16)                     # ground top / bottom / left / right
    n = r.i32()
    models = []; cnt = dict(lights=0, sounds=0, effects=0)
    for i in range(max(0, n)):
        if r.p + 4 > len(d):
            warn.append('RSW: object table truncated at %d of %d' % (i, n)); break
        t = r.i32()
        try:
            if t == 1:
                m = dict()
                if ver >= 0x103:
                    m['name'] = r.s(40); m['anim_type'] = r.i32(); m['anim_speed'] = r.f32(); m['block_type'] = r.i32()
                    if ver >= 0x206 and r.p + 81 <= len(d) and not _looks_name(d[r.p:r.p + 80], b'.rsm') and not _looks_name(d[r.p:r.p + 80], b'.rsm2'):
                        r.u8()                # 2.6 builds >= 186: one extra byte
                m['file'] = r.s(80); m['node'] = r.s(80)
                m['pos'] = r.fs(3); m['rot'] = r.fs(3); m['scale'] = r.fs(3)
                models.append(m)
            elif t == 2:
                r.raw(80 + 12 + 12 + 4); cnt['lights'] += 1
            elif t == 3:
                r.raw(80 + 80 + 12 + 4 + 4 + 4 + 4 + (4 if ver >= 0x200 else 0)); cnt['sounds'] += 1
            elif t == 4:
                r.raw(80 + 12 + 4 + 4 + 16); cnt['effects'] += 1
            else:
                warn.append('RSW: unknown object type %d at %d, rest of the table skipped' % (t, i)); break
        except FormatError:
            warn.append('RSW: object %d truncated' % i); break
    return dict(version=ver, build=build, ini=ini, gnd=gndf, gat=gatf, water=water, light=light, models=models, counts=cnt, warnings=warn)


# ------------------------------------------------------------------ RSM v1 (static models)
def rsm(d):
    """-> dict(version, shade, alpha, textures=[path rel. to data\\texture\\], main, nodes=[dict(name, parent, tex=[model texture idx], mat3,
    offset, pos, rotangle, rotaxis, scale, verts, tverts=[(u, v)], faces=[(v0,v1,v2, t0,t1,t2, texslot, twoside, smooth group)],
    rotkeys=[(frame, qx,qy,qz,qw)])]). shade = the client's shade type: 0 none, 1 flat (face normals), 2 smooth (per smoothing group;
    the group is stored per face from version 1.2, older files count as one group).
    RSM2 (version >= 2.0) raises FormatError (not supported yet)."""
    r = _R(d)
    ver = _ver(r, b'GRSM')
    if ver >= 0x200:
        raise FormatError('RSM2 (version %d.%d) is not supported yet' % (ver >> 8, ver & 255))
    _anim = r.i32(); shade = r.i32()
    alpha = r.u8() / 255.0 if ver >= 0x104 else 1.0
    r.raw(16)
    nt = r.i32()
    if nt < 0 or nt > 1024:
        raise FormatError('RSM texture count %d' % nt)
    textures = [r.s(40) for _ in range(nt)]
    main = r.s(40)
    nn = r.i32()
    if nn < 0 or nn > 1024:
        raise FormatError('RSM node count %d' % nn)
    nodes = []
    for _ in range(nn):
        nd = dict(name=r.s(40), parent=r.s(40))
        k = r.i32()
        if k < 0 or k > 1024:
            raise FormatError('RSM node texture count %d' % k)
        nd['tex'] = [r.i32() for _ in range(k)]
        nd['mat3'] = r.fs(9); nd['offset'] = r.fs(3); nd['pos'] = r.fs(3)
        nd['rotangle'] = r.f32(); nd['rotaxis'] = r.fs(3); nd['scale'] = r.fs(3)
        nv = r.i32()
        if nv < 0 or nv > 1000000:
            raise FormatError('RSM vertex count %d' % nv)
        r.need(nv * 12)
        nd['verts'] = [struct.unpack_from('<3f', d, r.p + i * 12) for i in range(nv)]; r.p += nv * 12
        ntv = r.i32()
        tvs = 12 if ver >= 0x102 else 8
        if ntv < 0 or ntv > 1000000:
            raise FormatError('RSM uv count %d' % ntv)
        r.need(ntv * tvs)
        o = tvs - 8
        nd['tverts'] = [struct.unpack_from('<2f', d, r.p + i * tvs + o) for i in range(ntv)]; r.p += ntv * tvs
        nf = r.i32()
        fs = 24 if ver >= 0x102 else 20
        if nf < 0 or nf > 1000000:
            raise FormatError('RSM face count %d' % nf)
        r.need(nf * fs)
        faces = []
        for i in range(nf):
            v0, v1, v2, t0, t1, t2, ts, _pad, two = struct.unpack_from('<6HHHi', d, r.p + i * fs)
            sg = struct.unpack_from('<i', d, r.p + i * fs + 20)[0] if fs == 24 else 0
            faces.append((v0, v1, v2, t0, t1, t2, ts, two, sg))
        r.p += nf * fs
        nd['faces'] = faces
        if ver >= 0x105:
            npk = r.i32()
            if npk < 0 or npk > 100000:
                raise FormatError('RSM position key count')
            r.raw(npk * 16)
        nrk = r.i32()
        if nrk < 0 or nrk > 100000:
            raise FormatError('RSM rotation key count')
        r.need(nrk * 20)
        nd['rotkeys'] = [struct.unpack_from('<i4f', d, r.p + i * 20) for i in range(nrk)]; r.p += nrk * 20
        nodes.append(nd)
    return dict(version=ver, shade=shade, alpha=alpha, textures=textures, main=main, nodes=nodes)
