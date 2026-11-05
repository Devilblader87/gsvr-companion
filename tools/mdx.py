#!/usr/bin/env python3
"""MDX (binary MDLX, version 800 = WC3 1.x) reader + skinning evaluator (clean-room, public MDX format notes [ext]).

  m = mdx.parse(bytes)            -> Model (sequences, global sequences, textures, materials, geosets, geoset
                                     animations, nodes (BONE + HELP), pivots); other chunks skipped by size
  ev = mdx.Evaluator(m)
  ev.pose(seq_index or None, t_ms) -> per geoset: (positions [(x, y, z)], normals [(x, y, z)], alpha)

Chunk layout (all little-endian, 'MDLX' magic, then tag(4) + u32 size + data):
  VERS u32 version.  MODL name[80], anim file[260], extent (radius, min3, max3), u32 blend time.
  SEQS 132 B each: name[80], u32 start, u32 end, f32 move speed, u32 flags (1 = non-looping), f32 rarity,
       u32 sync point, extent.   GLBS u32 durations.   TEXS 268 B each: u32 replaceable id, path[260], u32 flags
       (1 wrap width, 2 wrap height).   PIVT float3 per object id.
  MTLS per material: u32 inclusive size, u32 priority plane, u32 flags, 'LAYS' u32 count, layers (u32 inclusive
       size, u32 filter mode, u32 shading flags, u32 texture id, u32 texture animation id, u32 coord id, f32 alpha,
       optional KMTA / KMTF tracks).
  GEOS per geoset: u32 inclusive size, VRTX/NRMS (u32 n, float3), PTYP (u32 n, u32), PCNT (u32 n, u32),
       PVTX (u32 n, u16), GNDX (u32 n, u8), MTGC (u32 n, u32), MATS (u32 n, u32), u32 material id,
       u32 selection group, u32 selection flags, extent, u32 n + n extents, UVAS u32 n + n x (UVBS u32 n, float2).
  GEOA per animation: u32 inclusive size, f32 alpha, u32 flags, float3 colour, u32 geoset id, KGAO / KGAC tracks.
  Node (BONE: node + u32 geoset id + u32 geoset animation id; HELP: node): u32 inclusive size, name[80],
       u32 object id, u32 parent id (-1 none), u32 flags, KGTR (float3) / KGRT (quat x y z w) / KGSC (float3).
  EVTS event objects: node, then optional 'KEVT' u32 count, i32 global sequence id, u32 frames[count] (no size of
       their own [ext]); the name says the kind — 'SNDx' + a 4-letter AnimLookups key = a sound (docs/sound_plan.md S4b).
  PRE2 per emitter (ParticleEmitter2 [ext public MDX notes]): u32 inclusive size, node, f32 speed, variation,
       latitude (degrees), gravity, lifespan (s), emission rate (/ s), width, length, u32 filter mode (0 blend,
       1 additive, 2 modulate, 3 modulate 2x, 4 alpha key), rows, columns, head-or-tail (0 head, 1 tail, 2 both),
       f32 tail length, time (middle segment point, 0..1), 3 x float3 segment colours (r, g, b [obs data:
       FrostWyrmMissile blue, Mortar orange]), 3 x u8 segment alphas, 3 x f32 segment scaling, 4 x 3 u32 intervals
       (head, head decay, tail, tail decay: start, end, repeat), u32 texture id, u32 squirt, i32 priority plane,
       u32 replaceable id, then tracks KP2S speed, KP2R variation, KP2L latitude, KP2G gravity, KP2E emission
       rate, KP2N length, KP2W width, KP2V visibility (1 float each). Node flags of emitters [ext]: 0x8000
       unshaded, 0x10000 sort far z, 0x20000 line emitter, 0x40000 unfogged, 0x80000 model space, 0x100000 xy quad.
  Track: tag, u32 key count, u32 interpolation (0 none, 1 linear, 2 hermite, 3 bezier), i32 global sequence id,
       keys: u32 frame, value[n] (+ in tangent[n], out tangent[n] when interpolation > 1).

Evaluation rules (format behaviour as implemented by public viewers [ext]; approximations marked [inf]):
  * a track is evaluated inside the sequence interval [start, end] (or [0, duration] of its global sequence, the
    time then being (t - start) mod duration); no key in the interval -> the default (identity / static value);
    before the first / after the last key -> that key's value; interpolation 0 holds the previous key.
  * vectors: linear / Hermite / Bezier with the stored tangents; quaternions: slerp (linear) and squad with the
    stored tangents (Hermite, Bezier) [inf for the squad weighting].
  * node local matrix M = T(pivot) . T(translation) . R(rotation) . S(scale) . T(-pivot), world = parent . M.
  * skinned vertex = average over its matrix group's node world matrices x bind position; normal likewise with
    the 3x3 part, renormalised. Unknown object ids count as identity.
"""
import math, struct, sys

TRI, STRIP, FAN = 4, 5, 6


class Track:
    __slots__ = ('interp', 'gseq', 'frames', 'vals', 'tin', 'tout')

    def __init__(self, interp, gseq):
        self.interp, self.gseq = interp, gseq
        self.frames, self.vals, self.tin, self.tout = [], [], [], []


class Model:
    def __init__(self):
        self.version = 0
        self.name = ''
        self.extent = None
        self.sequences = []      # dict name start end speed flags rarity
        self.global_seqs = []
        self.textures = []       # (replaceable id, path, flags)
        self.materials = []      # [layer dict]
        self.geosets = []        # dict
        self.geoas = []          # dict alpha geoset track
        self.nodes = {}          # object id -> dict name parent flags tr rt sc kind
        self.pivots = []
        self.events = []         # (name, [frames]) per EVTS object
        self.emitters = []       # PRE2 particle emitters (dict, see _pre2)


def _cstr(b):
    return b.split(b'\0', 1)[0].decode('latin1')


def _read_track(d, p, n, ints=False):
    """-> (Track, new pos). n = components per value."""
    cnt, interp, gseq = struct.unpack_from('<IIi', d, p)
    p += 12
    t = Track(interp, gseq)
    vf = ('<%dI' if ints else '<%df') % n
    vs = 4 * n
    for _ in range(cnt):
        t.frames.append(struct.unpack_from('<i', d, p)[0])
        p += 4
        t.vals.append(struct.unpack_from(vf, d, p))
        p += vs
        if interp > 1:
            t.tin.append(struct.unpack_from(vf, d, p))
            t.tout.append(struct.unpack_from(vf, d, p + vs))
            p += 2 * vs
    return t, p


def _tracks(d, p, end, spec):
    """read tracks until end; spec: tag -> (components, ints). -> dict tag -> Track (unknown tags abort)."""
    out = {}
    while p + 16 <= end:
        tag = d[p:p + 4].decode('latin1')
        if tag not in spec:
            break
        n, ints = spec[tag]
        out[tag], p = _read_track(d, p + 4, n, ints)
    return out, p


NODE_TRACKS = {'KGTR': (3, False), 'KGRT': (4, False), 'KGSC': (3, False)}


def _node(d, p):
    size = struct.unpack_from('<I', d, p)[0]
    name = _cstr(d[p + 4:p + 84])
    oid, parent, flags = struct.unpack_from('<iiI', d, p + 84)
    tr, _ = _tracks(d, p + 96, p + size, NODE_TRACKS)
    return {'name': name, 'id': oid, 'parent': parent, 'flags': flags, 'tr': tr.get('KGTR'), 'rt': tr.get('KGRT'),
            'sc': tr.get('KGSC')}, p + size


PRE2_TRACKS = {k: (1, False) for k in ('KP2S', 'KP2R', 'KP2L', 'KP2G', 'KP2E', 'KP2N', 'KP2W', 'KP2V')}
PRE2_FIELDS = ('speed', 'variation', 'latitude', 'gravity', 'lifespan', 'rate', 'width', 'length')


def _pre2(d, q, end):
    """one ParticleEmitter2 (q = after its inclusive size) -> dict: node fields (name id parent flags tr rt sc) +
    the static fields (PRE2_FIELDS, filter rows columns head_tail tail_length time colors alphas scaling intervals
    texture squirt priority replaceable) + 'tracks' {KP2x: Track}."""
    nd, r = _node(d, q)
    f = struct.unpack_from('<8f', d, r)
    for k, name in enumerate(PRE2_FIELDS):
        nd[name] = f[k]
    nd['filter'], nd['rows'], nd['columns'], nd['head_tail'] = struct.unpack_from('<4I', d, r + 32)
    nd['tail_length'], nd['time'] = struct.unpack_from('<2f', d, r + 48)
    c = struct.unpack_from('<9f', d, r + 56)
    nd['colors'] = [c[0:3], c[3:6], c[6:9]]
    nd['alphas'] = list(struct.unpack_from('<3B', d, r + 92))
    nd['scaling'] = list(struct.unpack_from('<3f', d, r + 95))
    nd['intervals'] = list(struct.unpack_from('<12I', d, r + 107))
    nd['texture'], nd['squirt'], nd['priority'], nd['replaceable'] = struct.unpack_from('<IIiI', d, r + 155)
    nd['tracks'], _ = _tracks(d, r + 171, end, PRE2_TRACKS)
    nd['kind'] = 'PRE2'
    return nd


def parse(data):
    if data[:4] != b'MDLX':
        raise ValueError('not MDLX: %r' % data[:4])
    m = Model()
    p = 4
    n = len(data)
    while p + 8 <= n:
        tag = data[p:p + 4].decode('latin1')
        size = struct.unpack_from('<I', data, p + 4)[0]
        c0, c1 = p + 8, p + 8 + size
        if c1 > n:
            raise ValueError('chunk %s overruns file' % tag)
        if tag == 'VERS':
            m.version = struct.unpack_from('<I', data, c0)[0]
        elif tag == 'MODL':
            m.name = _cstr(data[c0:c0 + 80])
            m.extent = struct.unpack_from('<7f', data, c0 + 340)
        elif tag == 'SEQS':
            for q in range(c0, c1, 132):
                st, en, sp, fl, ra, _sy = struct.unpack_from('<IIfIfI', data, q + 80)
                m.sequences.append({'name': _cstr(data[q:q + 80]), 'start': st, 'end': en, 'speed': sp,
                                    'flags': fl, 'rarity': ra, 'extent': struct.unpack_from('<7f', data, q + 104)})
        elif tag == 'GLBS':
            m.global_seqs = list(struct.unpack_from('<%dI' % (size // 4), data, c0))
        elif tag == 'TEXS':
            for q in range(c0, c1, 268):
                rid = struct.unpack_from('<I', data, q)[0]
                fl = struct.unpack_from('<I', data, q + 264)[0]
                m.textures.append((rid, _cstr(data[q + 4:q + 264]), fl))
        elif tag == 'MTLS':
            q = c0
            while q < c1:
                msize, prio, mfl = struct.unpack_from('<III', data, q)
                end = q + msize
                r = q + 12
                layers = []
                if data[r:r + 4] == b'LAYS':
                    cnt = struct.unpack_from('<I', data, r + 4)[0]
                    r += 8
                    for _ in range(cnt):
                        lsize, fm, sh, tex, tanim, coord, alpha = struct.unpack_from('<6If', data, r)
                        tr, _ = _tracks(data, r + 28, r + lsize, {'KMTA': (1, False), 'KMTF': (1, True)})
                        layers.append({'filter': fm, 'shading': sh, 'texture': tex, 'alpha': alpha,
                                       'coord': coord, 'kmta': tr.get('KMTA'), 'kmtf': tr.get('KMTF')})
                        r += lsize
                m.materials.append({'priority': prio, 'flags': mfl, 'layers': layers})
                q = end
        elif tag == 'GEOS':
            q = c0
            while q < c1:
                gsize = struct.unpack_from('<I', data, q)[0]
                m.geosets.append(_geoset(data, q + 4, q + gsize))
                q += gsize
        elif tag == 'GEOA':
            q = c0
            while q < c1:
                asize, alpha, fl = struct.unpack_from('<IfI', data, q)
                gid = struct.unpack_from('<I', data, q + 24)[0]
                tr, _ = _tracks(data, q + 28, q + asize, {'KGAO': (1, False), 'KGAC': (3, False)})
                m.geoas.append({'alpha': alpha, 'flags': fl, 'geoset': gid, 'kgao': tr.get('KGAO')})
                q += asize
        elif tag in ('BONE', 'HELP'):
            q = c0
            while q < c1:
                nd, q = _node(data, q)
                if tag == 'BONE':
                    q += 8                       # geoset id, geoset animation id
                nd['kind'] = tag
                m.nodes[nd['id']] = nd
        elif tag == 'EVTS':
            q = c0
            while q + 96 <= c1:
                nd, q = _node(data, q)
                fr = []
                if data[q:q + 4] == b'KEVT':
                    cnt = struct.unpack_from('<I', data, q + 4)[0]
                    fr = list(struct.unpack_from('<%dI' % cnt, data, q + 12))
                    q += 12 + 4 * cnt
                m.events.append((nd['name'], fr))
        elif tag == 'PRE2':
            q = c0
            while q + 4 <= c1:
                isz = struct.unpack_from('<I', data, q)[0]
                if isz < 175 + 96 or q + isz > c1:
                    break
                m.emitters.append(_pre2(data, q + 4, q + isz))
                q += isz
        elif tag == 'PIVT':
            f = struct.unpack_from('<%df' % (size // 4), data, c0)
            m.pivots = [f[i:i + 3] for i in range(0, len(f) - 2, 3)]
        p = c1
    return m


def _geoset(d, p, end):
    g = {'verts': [], 'normals': [], 'faces': [], 'groups': [], 'mgroups': [], 'material': 0, 'uvs': []}
    ptyp, pcnt, pvtx, mtgc, mats = [], [], [], [], []
    while p < end:
        tag = d[p:p + 4].decode('latin1')
        if tag in ('VRTX', 'NRMS'):
            cnt = struct.unpack_from('<I', d, p + 4)[0]
            f = struct.unpack_from('<%df' % (cnt * 3), d, p + 8)
            g['verts' if tag == 'VRTX' else 'normals'] = [f[i:i + 3] for i in range(0, cnt * 3, 3)]
            p += 8 + cnt * 12
        elif tag in ('PTYP', 'PCNT', 'MTGC', 'MATS'):
            cnt = struct.unpack_from('<I', d, p + 4)[0]
            v = list(struct.unpack_from('<%dI' % cnt, d, p + 8))
            {'PTYP': ptyp, 'PCNT': pcnt, 'MTGC': mtgc, 'MATS': mats}[tag].extend(v)
            p += 8 + cnt * 4
            if tag == 'MATS':
                g['material'], g['selection'], _sf = struct.unpack_from('<3I', d, p)
                p += 12 + 28
                ne = struct.unpack_from('<I', d, p)[0]
                p += 4 + ne * 28
        elif tag == 'PVTX':
            cnt = struct.unpack_from('<I', d, p + 4)[0]
            pvtx = struct.unpack_from('<%dH' % cnt, d, p + 8)
            p += 8 + cnt * 2
        elif tag == 'GNDX':
            cnt = struct.unpack_from('<I', d, p + 4)[0]
            g['groups'] = list(d[p + 8:p + 8 + cnt])
            p += 8 + cnt
        elif tag == 'UVAS':
            nsets = struct.unpack_from('<I', d, p + 4)[0]
            p += 8
            for k in range(nsets):
                if d[p:p + 4] != b'UVBS':
                    break
                cnt = struct.unpack_from('<I', d, p + 4)[0]
                f = struct.unpack_from('<%df' % (cnt * 2), d, p + 8)
                if k == 0:
                    g['uvs'] = [f[i:i + 2] for i in range(0, cnt * 2, 2)]
                p += 8 + cnt * 8
        else:
            break                              # unknown sub-chunk (v800 has none): stop at the geoset end
    # primitives -> triangles
    q = 0
    for typ, cnt in zip(ptyp, pcnt):
        idx = pvtx[q:q + cnt]
        q += cnt
        if typ == TRI:
            g['faces'] += [tuple(idx[i:i + 3]) for i in range(0, cnt - 2, 3)]
        elif typ == STRIP:
            for i in range(cnt - 2):
                a, b, c = idx[i:i + 3]
                g['faces'].append((a, b, c) if i % 2 == 0 else (b, a, c))
        elif typ == FAN:
            for i in range(1, cnt - 1):
                g['faces'].append((idx[0], idx[i], idx[i + 1]))
    r = 0
    for sz in mtgc:
        g['mgroups'].append(mats[r:r + sz])
        r += sz
    nv = len(g['verts'])
    if len(g['normals']) != nv:
        g['normals'] = [(0.0, 0.0, 1.0)] * nv
    if len(g['uvs']) != nv:
        g['uvs'] = [(0.0, 0.0)] * nv
    if len(g['groups']) != nv:
        g['groups'] = [0] * nv
    return g


# ---------------------------------------------------------------- interpolation

def _hermite(a, ao, bi, b, t):
    t2 = t * t
    f1 = t2 * (2 * t - 3) + 1
    f2 = t2 * (t - 2) + t
    f3 = t2 * (t - 1)
    f4 = t2 * (3 - 2 * t)
    return tuple(a[i] * f1 + ao[i] * f2 + bi[i] * f3 + b[i] * f4 for i in range(len(a)))


def _bezier(a, ao, bi, b, t):
    inv = 1 - t
    f1 = inv * inv * inv
    f2 = 3 * t * inv * inv
    f3 = 3 * t * t * inv
    f4 = t * t * t
    return tuple(a[i] * f1 + ao[i] * f2 + bi[i] * f3 + b[i] * f4 for i in range(len(a)))


def _slerp(a, b, t):
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    if dot < 0:
        b = (-b[0], -b[1], -b[2], -b[3])
        dot = -dot
    if dot > 0.9995:
        r = tuple(a[i] + (b[i] - a[i]) * t for i in range(4))
    else:
        th = math.acos(min(1.0, dot))
        s = math.sin(th)
        wa, wb = math.sin((1 - t) * th) / s, math.sin(t * th) / s
        r = tuple(a[i] * wa + b[i] * wb for i in range(4))
    n = math.sqrt(sum(x * x for x in r)) or 1.0
    return tuple(x / n for x in r)


def _squad(a, ao, bi, b, t):
    return _slerp(_slerp(a, b, t), _slerp(ao, bi, t), 2 * t * (1 - t))


def eval_track(tr, t, start, end, global_seqs, default, quat=False):
    if tr is None or not tr.frames:
        return default
    if tr.gseq >= 0 and tr.gseq < len(global_seqs):
        dur = global_seqs[tr.gseq]
        t = (t - start) % dur if dur > 0 else 0
        start, end = 0, dur
    fr = tr.frames
    # keys in [start, end]
    lo = 0
    while lo < len(fr) and fr[lo] < start:
        lo += 1
    hi = lo
    while hi < len(fr) and fr[hi] <= end:
        hi += 1
    if lo == hi:
        return default
    if t <= fr[lo]:
        return tr.vals[lo]
    if t >= fr[hi - 1]:
        return tr.vals[hi - 1]
    k = lo
    while fr[k + 1] < t:
        k += 1
    a, b = tr.vals[k], tr.vals[k + 1]
    if tr.interp == 0:
        return a
    f = (t - fr[k]) / float(fr[k + 1] - fr[k])
    if quat:
        if tr.interp == 1:
            return _slerp(a, b, f)
        return _squad(a, tr.tout[k], tr.tin[k + 1], b, f)
    if tr.interp == 1:
        return tuple(a[i] + (b[i] - a[i]) * f for i in range(len(a)))
    if tr.interp == 2:
        return _hermite(a, tr.tout[k], tr.tin[k + 1], b, f)
    return _bezier(a, tr.tout[k], tr.tin[k + 1], b, f)


# ---------------------------------------------------------------- matrices (3x4 row-major tuples)

IDENT = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def local_matrix(piv, tr, q, s):
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz, wx, wy, wz = x * y, x * z, y * z, w * x, w * y, w * z
    r00, r01, r02 = 1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)
    r10, r11, r12 = 2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)
    r20, r21, r22 = 2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)
    sx, sy, sz = s
    l00, l01, l02 = r00 * sx, r01 * sy, r02 * sz
    l10, l11, l12 = r10 * sx, r11 * sy, r12 * sz
    l20, l21, l22 = r20 * sx, r21 * sy, r22 * sz
    px, py, pz = piv
    tx = tr[0] + px - (l00 * px + l01 * py + l02 * pz)
    ty = tr[1] + py - (l10 * px + l11 * py + l12 * pz)
    tz = tr[2] + pz - (l20 * px + l21 * py + l22 * pz)
    return (l00, l01, l02, tx, l10, l11, l12, ty, l20, l21, l22, tz)


def mul(a, b):
    a00, a01, a02, a03, a10, a11, a12, a13, a20, a21, a22, a23 = a
    b00, b01, b02, b03, b10, b11, b12, b13, b20, b21, b22, b23 = b
    return (a00 * b00 + a01 * b10 + a02 * b20, a00 * b01 + a01 * b11 + a02 * b21, a00 * b02 + a01 * b12 + a02 * b22,
            a00 * b03 + a01 * b13 + a02 * b23 + a03,
            a10 * b00 + a11 * b10 + a12 * b20, a10 * b01 + a11 * b11 + a12 * b21, a10 * b02 + a11 * b12 + a12 * b22,
            a10 * b03 + a11 * b13 + a12 * b23 + a13,
            a20 * b00 + a21 * b10 + a22 * b20, a20 * b01 + a21 * b11 + a22 * b21, a20 * b02 + a21 * b12 + a22 * b22,
            a20 * b03 + a21 * b13 + a22 * b23 + a23)


BILLBOARD_FLAGS = 0x8 | 0x10 | 0x20 | 0x40


class Evaluator:
    def __init__(self, m):
        self.m = m
        self.order = []
        seen = set()

        def visit(oid, depth=0):
            if oid in seen or oid not in m.nodes or depth > 256:
                return
            par = m.nodes[oid]['parent']
            if par in m.nodes and par != oid:
                visit(par, depth + 1)
            seen.add(oid)
            self.order.append(oid)
        for oid in sorted(m.nodes):
            visit(oid)
        # per geoset: vertices grouped by matrix group
        self.ggroups = []
        for g in m.geosets:
            by = {}
            for v, grp in enumerate(g['groups']):
                by.setdefault(grp, []).append(v)
            self.ggroups.append(sorted(by.items()))
        self.alpha_of = {}
        for a in m.geoas:
            self.alpha_of[a['geoset']] = a
        # billboarded geosets: every vertex is skinned only to nodes with a billboard flag (0x8, or the locked
        # variants 0x10 / 0x20 / 0x40 [ext MDX node flags]); the game turns those nodes to face the camera each frame
        # (wisps, glows, sprites). The bake keeps their pivot animated and the vertex offset from it (bb_offsets).
        self.bb_geo = []
        for gi, g in enumerate(m.geosets):
            mg = g['mgroups']
            ok = bool(self.ggroups[gi])
            for grp, _v in self.ggroups[gi]:
                ids = mg[grp] if grp < len(mg) else ()
                if not ids or not all(o in m.nodes and m.nodes[o]['flags'] & BILLBOARD_FLAGS for o in ids):
                    ok = False
                    break
            self.bb_geo.append(ok)

    def _pivot(self, oid):
        return self.m.pivots[oid] if 0 <= oid < len(self.m.pivots) else (0.0, 0.0, 0.0)

    def bb_offsets(self, gi):
        """Billboarded geoset gi: per vertex its bind offset from the (averaged) pivot of its nodes, model axes."""
        g = self.m.geosets[gi]
        mg = g['mgroups']
        out = [None] * len(g['verts'])
        for grp, verts in self.ggroups[gi]:
            ids = mg[grp] if grp < len(mg) else ()
            ps = [self._pivot(o) for o in ids] or [(0.0, 0.0, 0.0)]
            c = tuple(sum(p[i] for p in ps) / len(ps) for i in range(3))
            for v in verts:
                x, y, z = g['verts'][v]
                out[v] = (x - c[0], y - c[1], z - c[2])
        return out

    def seq_range(self, si):
        if si is None or si >= len(self.m.sequences):
            return 0, 0
        s = self.m.sequences[si]
        return s['start'], s['end']

    def node_matrices(self, t, start, end):
        m = self.m
        gs = m.global_seqs
        world = {}
        for oid in self.order:
            nd = m.nodes[oid]
            piv = m.pivots[oid] if 0 <= oid < len(m.pivots) else (0.0, 0.0, 0.0)
            tr = eval_track(nd['tr'], t, start, end, gs, (0.0, 0.0, 0.0))
            q = eval_track(nd['rt'], t, start, end, gs, (0.0, 0.0, 0.0, 1.0), quat=True)
            s = eval_track(nd['sc'], t, start, end, gs, (1.0, 1.0, 1.0))
            loc = local_matrix(piv, tr, q, s)
            par = world.get(nd['parent'])
            world[oid] = mul(par, loc) if par is not None else loc
        return world

    def emitter_matrix(self, em, t, start, end, world=None):
        """world matrix (3x4) of a PRE2 emitter node at model time t: its parent's world matrix (BONE / HELP;
        identity when none / unknown) times its own local matrix from its pivot and tracks."""
        if world is None:
            world = self.node_matrices(t, start, end)
        gs = self.m.global_seqs
        oid = em['id']
        piv = self.m.pivots[oid] if 0 <= oid < len(self.m.pivots) else (0.0, 0.0, 0.0)
        tr = eval_track(em['tr'], t, start, end, gs, (0.0, 0.0, 0.0))
        q = eval_track(em['rt'], t, start, end, gs, (0.0, 0.0, 0.0, 1.0), quat=True)
        sc = eval_track(em['sc'], t, start, end, gs, (1.0, 1.0, 1.0))
        loc = local_matrix(piv, tr, q, sc)
        par = world.get(em['parent'])
        return (mul(par, loc) if par is not None else loc), piv

    def geoset_alpha(self, gi, t, start, end):
        a = self.alpha_of.get(gi)
        if a is None:
            return 1.0
        return eval_track(a['kgao'], t, start, end, self.m.global_seqs, (a['alpha'],))[0]

    def pose(self, si, t, normals=True, billboard=False):
        """-> list per geoset of (positions, normals or None, alpha). t = absolute model time in ms.
        billboard: the vertices of a billboarded geoset get the animated pivot of their nodes instead."""
        start, end = self.seq_range(si)
        world = self.node_matrices(t, start, end)
        out = []
        for gi, g in enumerate(self.m.geosets):
            nv = len(g['verts'])
            pos = [None] * nv
            nrm = [None] * nv if normals else None
            V, N = g['verts'], g['normals']
            mg = g['mgroups']
            for grp, verts in self.ggroups[gi]:
                ids = mg[grp] if grp < len(mg) else ()
                mats = [world.get(o, IDENT) for o in ids] or [IDENT]
                if len(mats) == 1:
                    M = mats[0]
                else:
                    k = 1.0 / len(mats)
                    M = tuple(sum(mm[i] for mm in mats) * k for i in range(12))
                m00, m01, m02, m03, m10, m11, m12, m13, m20, m21, m22, m23 = M
                if billboard and self.bb_geo[gi]:
                    pv = []
                    for o in ids:
                        W = world.get(o, IDENT)
                        x, y, z = self._pivot(o)
                        pv.append((W[0] * x + W[1] * y + W[2] * z + W[3], W[4] * x + W[5] * y + W[6] * z + W[7],
                                   W[8] * x + W[9] * y + W[10] * z + W[11]))
                    pv = pv or [(0.0, 0.0, 0.0)]
                    c = tuple(sum(p[i] for p in pv) / len(pv) for i in range(3))
                    for v in verts:
                        pos[v] = c
                    if normals:
                        for v in verts:
                            nrm[v] = (0.0, 0.0, 1.0)
                    continue
                for v in verts:
                    x, y, z = V[v]
                    pos[v] = (m00 * x + m01 * y + m02 * z + m03, m10 * x + m11 * y + m12 * z + m13,
                              m20 * x + m21 * y + m22 * z + m23)
                if normals:
                    for v in verts:
                        x, y, z = N[v]
                        a, b, c = m00 * x + m01 * y + m02 * z, m10 * x + m11 * y + m12 * z, m20 * x + m21 * y + m22 * z
                        ln = math.sqrt(a * a + b * b + c * c) or 1.0
                        nrm[v] = (a / ln, b / ln, c / ln)
            out.append((pos, nrm, self.geoset_alpha(gi, t, start, end)))
        return out


def summary(m):
    nv = sum(len(g['verts']) for g in m.geosets)
    nf = sum(len(g['faces']) for g in m.geosets)
    return ('v%d %r: %d geosets, %d verts, %d tris, %d seqs, %d gseqs, %d textures, %d materials, %d nodes, %d pivots'
            % (m.version, m.name, len(m.geosets), nv, nf, len(m.sequences), len(m.global_seqs), len(m.textures),
               len(m.materials), len(m.nodes), len(m.pivots)))


# ---------------------------------------------------------------- self-test / preview

def _selftest(install, out_dir, names):
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import w3mpq as mpq
    chain = mpq.war3_chain(install)
    os.makedirs(out_dir, exist_ok=True)
    for name in names:
        data = chain.read(name, missing_ok=True)
        if data is None:
            print('missing', name)
            continue
        m = parse(data)
        print(name, summary(m))
        for s in m.sequences[:40]:
            print('   seq %-20s %6d..%6d %s rarity %g' % (s['name'], s['start'], s['end'],
                                                       'nonloop' if s['flags'] & 1 else 'loop', s['rarity']))
        print('   textures', m.textures)
        ev = Evaluator(m)
        stand = next((i for i, s in enumerate(m.sequences) if s['name'].lower().startswith('stand')), None)
        walk = next((i for i, s in enumerate(m.sequences) if s['name'].lower().startswith('walk')), None)
        t0 = m.sequences[stand]['start'] if stand is not None else 0
        frames = [('frame0', stand, t0)]
        if walk is not None:
            s = m.sequences[walk]
            frames.append(('walk', walk, (s['start'] + s['end']) // 2))
        base = None
        # winding check: triangle normal vs vertex normals at bind pose
        agree = total = 0
        for g in m.geosets:
            V, N = g['verts'], g['normals']
            for a, b, c in g['faces']:
                ux, uy, uz = (V[b][i] - V[a][i] for i in range(3))
                vx, vy, vz = (V[c][i] - V[a][i] for i in range(3))
                n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
                vn = [N[a][i] + N[b][i] + N[c][i] for i in range(3)]
                total += 1
                agree += (n[0] * vn[0] + n[1] * vn[1] + n[2] * vn[2]) > 0
        print('   winding: %d/%d faces agree with vertex normals when read (a, b, c) as CCW' % (agree, total))
        imgs = []
        for label, si, t in frames:
            geo = ev.pose(si, t)
            pts = [p for g in geo for p in g[0]]
            mn = [min(p[i] for p in pts) for i in range(3)]
            mx = [max(p[i] for p in pts) for i in range(3)]
            print('   %-6s t=%d bounds min %s max %s' % (label, t, ['%.1f' % v for v in mn], ['%.1f' % v for v in mx]))
            if base is None:
                base = (mn, mx)
            else:
                ext0 = max(base[1][i] - base[0][i] for i in range(3))
                ext1 = max(mx[i] - mn[i] for i in range(3))
                print('   extent ratio %.2f (%s)' % (ext1 / ext0, 'ok' if ext1 < 2 * ext0 else 'SUSPICIOUS'))
            imgs.append((label, _render(m, geo)))
        from PIL import Image
        W = sum(i.width for _, i in imgs)
        canvas = Image.new('RGB', (W, max(i.height for _, i in imgs)), (40, 40, 40))
        x = 0
        for _, im in imgs:
            canvas.paste(im, (x, 0))
            x += im.width
        fn = os.path.join(out_dir, name.replace('\\', '/').split('/')[-1].replace('.mdx', '.png'))
        canvas.save(fn)
        print('   ->', fn)


def _render(m, geo, size=320):
    """orthographic side view (looking along +y: screen x = model x, screen y = -z), flat shaded."""
    from PIL import Image, ImageDraw
    pts = [p for g in geo for p in g[0]]
    mn = [min(p[i] for p in pts) for i in range(3)]
    mx = [max(p[i] for p in pts) for i in range(3)]
    span = max(mx[0] - mn[0], mx[2] - mn[2], 1e-3)
    sc = (size - 20) / span
    im = Image.new('RGB', (size, size), (40, 40, 40))
    d = ImageDraw.Draw(im)
    tris = []
    for (pos, nrm, alpha), g in zip(geo, m.geosets):
        if alpha < 0.01:
            continue
        for a, b, c in g['faces']:
            pa, pb, pc = pos[a], pos[b], pos[c]
            ux, uy, uz = (pb[i] - pa[i] for i in range(3))
            vx, vy, vz = (pc[i] - pa[i] for i in range(3))
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = math.sqrt(sum(x * x for x in n)) or 1
            light = abs(n[0] * 0.5 - n[1] * 0.7 + n[2] * 0.5) / ln
            depth = (pa[1] + pb[1] + pc[1]) / 3
            tris.append((-depth, light, (pa, pb, pc)))
    tris.sort(key=lambda t: t[0])
    for _, light, ps in tris:
        c = int(60 + 190 * min(1, light))
        d.polygon([(10 + (p[0] - mn[0]) * sc, size - 10 - (p[2] - mn[2]) * sc) for p in ps], fill=(c, c, int(c * 0.9)))
    return im


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='MDX reader self-test')
    ap.add_argument('--install', default='input/war3')
    ap.add_argument('--out', required=True)
    ap.add_argument('models', nargs='+')
    a = ap.parse_args()
    _selftest(a.install, a.out, a.models)
