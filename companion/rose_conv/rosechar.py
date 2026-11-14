"""ROSE character data -> pack-space structures: skeletons (ZMD), skinned meshes (ZMS), animation clips (ZMO), poses.

Stdlib only; pure functions on already-read bytes / rosefmt results. Used by rose_pack.py (converter), rose_pack_check.py
(previews) and the tests. Byte layouts of the pack sections live in rpg1.py / docs/RPG1_PACK.md.

AXES (see docs/RPG1_PACK.md section 2 and 8): file space is right-handed, Z up, cm (X east, Y north). Pack space is Unity axes,
metres: p_pack = (x, z, y) / 100. The retail skeletons FACE FILE -Y (south: toes are at -Y of the ankles, the "p_04" face-item
dummy is at -Y of the head, the back dummy "p_03" at +Y), so the converter bakes a 180 degree turn about the vertical axis into
every character: ROOT bone = F * root, F = rotation (0, 1, 0, 0) (x, y, z, w). With it all pack characters face +Z (Unity
forward), `Quaternion.Euler(0, yaw, 0)` with yaw = heading of the wanted facing direction turns them correctly.

CONVENTIONS
  quaternion tuples are (x, y, z, w). file quaternion (w, x, y, z) -> pack (-x, -z, -y, w) (axis swap is a reflection: the vector
  part of a rotation changes sign).
  A joint's pose is local (parent relative) translation + rotation; ROSE ZMO channels REPLACE the bind local value of that joint
  (a joint without channel keeps its bind pose). Skin transform of joint j = G_anim[j] * inverse(G_bind[j]).
  Joints = real bones followed by the ZMD dummy bones ("dummy index + bone count" in ZSC / ZMO channel space).
"""
import math, struct, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosefmt as F

FPS_OUT = 30
FLIP_Q = (0.0, 1.0, 0.0, 0.0)           # 180 deg about Unity Y (bakes "faces +Z")
JF_DUMMY = 1                            # SKEL joint flag: ZMD dummy bone
IDENT_Q = (0.0, 0.0, 0.0, 1.0)


# ------------------------------------------------------------------ small math
def qmul(a, b):
    x1, y1, z1, w1 = a; x2, y2, z2, w2 = b
    return (w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2)


def qnorm(q):
    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n) if n > 1e-12 else IDENT_Q


def qinv(q):
    return (-q[0], -q[1], -q[2], q[3])


def qrot(q, v):
    x, y, z, w = q
    tx = 2 * (y * v[2] - z * v[1]); ty = 2 * (z * v[0] - x * v[2]); tz = 2 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + (y * tz - z * ty), v[1] + w * ty + (z * tx - x * tz), v[2] + w * tz + (x * ty - y * tx))


def qslerp(a, b, t):
    d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    if d < 0:
        b = (-b[0], -b[1], -b[2], -b[3]); d = -d
    if d > 0.9995:
        return qnorm(tuple(a[i] + (b[i] - a[i]) * t for i in range(4)))
    th = math.acos(min(1.0, d)); s = math.sin(th)
    wa = math.sin((1 - t) * th) / s; wb = math.sin(t * th) / s
    return tuple(a[i] * wa + b[i] * wb for i in range(4))


def qmat(q):
    """unit quaternion -> 3x3 row-major tuple"""
    x, y, z, w = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def file_q(q_wxyz):
    """file-space quaternion (w, x, y, z) -> pack-space (x, y, z, w)"""
    w, x, y, z = q_wxyz
    return qnorm((-x, -z, -y, w))


def file_p(p, scale=0.01):
    """file-space vector -> pack-space (x, z, y) * scale"""
    return (p[0] * scale, p[2] * scale, p[1] * scale)


# ------------------------------------------------------------------ skeleton
def build_skeleton(zmd, name='', flip=True):
    """zmd = rosefmt.zmd() result (bones, dummies, ok) -> dict(name, nreal, joints=[dict(parent, flags, name, lpos, lrot, gpos, grot)]).
    Joints: real bones then dummies. Root = bone 0 (or any bone whose parent is itself / invalid); `flip` bakes the faces-+Z turn into roots."""
    bones, dummies = zmd[0], zmd[1]
    n = len(bones)
    joints = []
    for i, (par, nm, pos, q) in enumerate(bones):
        root = i == 0 or par == i or par < 0 or par >= n
        lp = file_p(pos); lr = file_q(q)
        if root and flip:
            lp = qrot(FLIP_Q, lp); lr = qnorm(qmul(FLIP_Q, lr))
        joints.append(dict(parent=-1 if root else par, flags=0, name=nm, lpos=lp, lrot=lr))
    for par, nm, pos, q in dummies:
        par = par if 0 <= par < n else 0
        joints.append(dict(parent=par, flags=JF_DUMMY, name=nm, lpos=file_p(pos), lrot=file_q(q)))
    _globals(joints)
    return dict(name=name, nreal=n, joints=joints)


def _globals(joints):
    done = [False] * len(joints)

    def rec(i, depth=0):
        if done[i]:
            return
        j = joints[i]
        p = j['parent']
        if p < 0 or depth > len(joints):
            j['gpos'], j['grot'] = j['lpos'], j['lrot']
        else:
            rec(p, depth + 1)
            pj = joints[p]
            t = qrot(pj['grot'], j['lpos'])
            j['gpos'] = (pj['gpos'][0] + t[0], pj['gpos'][1] + t[1], pj['gpos'][2] + t[2])
            j['grot'] = qnorm(qmul(pj['grot'], j['lrot']))
        done[i] = True

    for i in range(len(joints)):
        rec(i)


def recompute_globals(skel):
    _globals(skel['joints'])


# ------------------------------------------------------------------ meshes
def quant_weights(pairs):
    """[(joint, weight)] -> up to 4 (joint, u8) with the u8 summing to 255 (largest remainder), strongest first."""
    acc = {}
    for b, w in pairs:
        if w > 0:
            acc[b] = acc.get(b, 0.0) + w
    items = sorted(((b, w) for b, w in acc.items() if w > 0.002), key=lambda p: (-p[1], p[0]))[:4]
    if not items:
        return [(pairs[0][0] if pairs else 0, 255)]
    s = sum(w for _, w in items)
    raw = [w / s * 255.0 for _, w in items]
    q = [int(x) for x in raw]
    for k in sorted(range(len(q)), key=lambda k: -(raw[k] - q[k]))[:255 - sum(q)]:
        q[k] += 1
    return [(b, qq) for (b, _), qq in zip(items, q) if qq > 0]


def zms_influences(z, njoints):
    """per vertex [(joint, weight)] from a rosefmt.zms result (None without skin data). ZMS v7/8 store palette slots, v5/6 are mapped by rosefmt."""
    skin = z.get('skin')
    if not skin:
        return None
    bones = z.get('bones') or []
    out = []
    for w, ix in skin:
        row = []
        for wi, ii in zip(w, ix):
            j = bones[ii] if z['ver'] >= 7 and ii < len(bones) else (ii if z['ver'] < 7 else 0)
            row.append((j if j < njoints else 0, wi))
        out.append(row)
    return out


def _mat_mul_vec(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2], m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def _cofactor(m):
    a = m
    return ((a[1][1] * a[2][2] - a[1][2] * a[2][1], a[1][2] * a[2][0] - a[1][0] * a[2][2], a[1][0] * a[2][1] - a[1][1] * a[2][0]),
            (a[0][2] * a[2][1] - a[0][1] * a[2][2], a[0][0] * a[2][2] - a[0][2] * a[2][0], a[0][1] * a[2][0] - a[0][0] * a[2][1]),
            (a[0][1] * a[1][2] - a[0][2] * a[1][1], a[0][2] * a[1][0] - a[0][0] * a[1][2], a[0][0] * a[1][1] - a[0][1] * a[1][0]))


def _n3(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 1.0, 0.0)


def _q8(v):
    return max(-127, min(127, int(round(v * 127))))


def convert_mesh(z, skel, rigid_joint=None, xf=None, flip=True):
    """rosefmt.zms result -> CMSH mesh dict (bind pose, pack space, model space of `skel`).

    rigid_joint=None: skinned mesh, vertices are already in model space (file axes), weights from the ZMS (<= 4 influences).
    rigid_joint=j:    the mesh lives in the local space of joint j (face, hair, cap, weapon): `xf` = optional (A 3x3 rows, t metres)
                      part transform in that joint space (file axes) applied first; the vertices are moved into model space with the
                      bind pose of j and get weight 255 on j.
    Returns dict(verts=[(x,y,z,nx,ny,nz,u,v)], bi=[(4 palette slots)], bw=[(4 u8)], idx=[...] clockwise, palette=[joint ids], bbmin, bbmax)."""
    pos = z['pos']; nrm = z.get('nrm'); uv = z.get('uv1'); nv = len(pos)
    joints = skel['joints']
    infl = None
    if rigid_joint is None:
        infl = zms_influences(z, len(joints))
        if infl is None:
            raise ValueError('skinned mesh needs bone weights + indices')
        P = (lambda p: (-p[0], p[2], -p[1])) if flip else (lambda p: (p[0], p[2], p[1]))
        verts = [P(p) for p in pos]
        norms = [P(n) for n in nrm] if nrm else None
    else:
        j = joints[rigid_joint]
        A, t = xf if xf else (((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0.0, 0.0, 0.0))
        cof = _cofactor(A)
        verts = []; norms = [] if nrm else None
        for k, p in enumerate(pos):
            a = _mat_mul_vec(A, p)
            lp = (a[0] + t[0], a[2] + t[2], a[1] + t[1])                  # file -> pack axes (joint local)
            w = qrot(j['grot'], lp)
            verts.append((w[0] + j['gpos'][0], w[1] + j['gpos'][1], w[2] + j['gpos'][2]))
            if nrm:
                n = _mat_mul_vec(cof, nrm[k])
                n = qrot(j['grot'], (n[0], n[2], n[1]))
                norms.append(n)
    idx = z['idx']
    tri = [(idx[i], idx[i + 2], idx[i + 1]) for i in range(0, len(idx) - 2, 3)]       # reflection -> reverse winding
    if rigid_joint is not None:
        det = A[0][0] * cof[0][0] + A[0][1] * cof[0][1] + A[0][2] * cof[0][2]
        if det < 0:
            tri = [(a, c, b) for a, b, c in tri]                                          # mirrored part: flip back
    if norms is None:
        norms = _face_normals(verts, tri)
    palette = []
    rows = []
    if infl is not None:
        rows = [quant_weights(r) for r in infl]
        palette = sorted({b for r in rows for b, _ in r})
    else:
        palette = [rigid_joint]
        rows = [[(rigid_joint, 255)]] * nv
    if len(palette) > 255:
        raise ValueError('palette of %d joints does not fit u8' % len(palette))
    slot = {b: i for i, b in enumerate(palette)}
    out_v = []; bis = []; bws = []
    for k in range(nv):
        n = _n3(norms[k])
        u, v = uv[k] if uv else (0.0, 0.0)
        out_v.append((verts[k][0], verts[k][1], verts[k][2], _q8(n[0]), _q8(n[1]), _q8(n[2]), u, v))
        r = rows[k]
        bis.append(tuple([slot[b] for b, _ in r] + [0] * (4 - len(r))))
        bws.append(tuple([w for _, w in r] + [0] * (4 - len(r))))
    lo = tuple(min(v[i] for v in verts) for i in range(3)); hi = tuple(max(v[i] for v in verts) for i in range(3))
    return dict(verts=out_v, bi=bis, bw=bws, idx=[i for t in tri for i in t], palette=palette, bbmin=lo, bbmax=hi)


def _face_normals(verts, tri):
    acc = [[0.0, 0.0, 0.0] for _ in verts]
    for a, b, c in tri:
        pa, pb, pc = verts[a], verts[b], verts[c]
        e1 = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]); e2 = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        # triangles are clockwise (left handed): the outward normal is the cross product e1 x e2 for a left handed system
        for i in (a, b, c):
            acc[i][0] += n[0]; acc[i][1] += n[1]; acc[i][2] += n[2]
    return [_n3(a) for a in acc]


# ------------------------------------------------------------------ ZMO
_ZMO_SZ = {1: 0, 2: 12, 4: 16, 8: 12, 16: 4, 32: 8, 64: 8, 128: 8, 256: 8, 512: 4, 1024: 4}


def parse_zmo(d):
    """full ZMO0002 parse -> dict(fps, nf, pos={joint: [(x,y,z) file cm]}, rot={joint: [(w,x,y,z)]}, scale={joint: [s]}, events=[(frame, id)], interp_ms)"""
    r = F.R(d)
    if r.nul() != 'ZMO0002':
        raise ValueError('not a ZMO0002')
    fps = r.u32(); nf = r.u32(); nc = r.u32()
    ch = [(r.u32(), r.u32()) for _ in range(nc)]
    pos = {}; rot = {}; scale = {}
    for t, b in ch:
        if t == 2: pos[b] = []
        elif t == 4: rot[b] = []
        elif t == 1024: scale[b] = []
    for _f in range(nf):
        for t, b in ch:
            if t == 2: pos[b].append(r.vec(3))
            elif t == 4: rot[b].append(r.vec(4))
            elif t == 1024: scale[b].append(r.f32())
            else:
                r.p += _ZMO_SZ[t]
    events = []; interp = None
    if d[-4:] in (b'EZMO', b'3ZMO'):
        off, = struct.unpack_from('<I', d, len(d) - 8)
        rr = F.R(d, off); n = rr.u16()
        ev = [rr.u16() for _ in range(n)]
        events = [(i, e) for i, e in enumerate(ev) if e]
        if d[-4:] == b'3ZMO':
            interp = rr.u32()
    return dict(fps=fps, nf=nf, pos=pos, rot=rot, scale=scale, events=events, interp_ms=interp)


def bake_clip(z, skel, out_fps=FPS_OUT, loop=False, flip=True, name=''):
    """parse_zmo result -> clip dict(name, frames, fps, loop, tracks=[dict(joint, pos|None, rot|None, scale|None)], events, src_fps, dropped).
    Values are in pack space (local, parent relative; roots carry the faces-+Z turn). Resampled to out_fps with lerp / slerp
    (identity when the source already has out_fps). Looping clips wrap (frame n-1 blends toward frame 0, period = n / fps)."""
    joints = skel['joints']; nj = len(joints)
    src_fps = z['fps'] or out_fps
    nf = max(1, z['nf'])
    if src_fps == out_fps:
        n_out = nf
    else:
        n_out = max(1, int(round(nf * out_fps / float(src_fps))))
    dropped = 0
    tracks = {}

    def src_pos(i, p):
        v = file_p(p)
        return v

    def sample(seq, lerp_fn):
        out = []
        for k in range(n_out):
            if src_fps == out_fps:
                out.append(seq[min(k, len(seq) - 1)]); continue
            s = k * src_fps / float(out_fps)
            i0 = int(math.floor(s)); t = s - i0
            i1 = i0 + 1
            if i1 >= nf:
                i1 = 0 if loop else nf - 1
            if i0 >= nf:
                i0 = nf - 1 if not loop else i0 % nf
            out.append(lerp_fn(seq[i0], seq[i1], t) if t > 1e-9 else seq[i0])
        return out

    for b, seq in z['pos'].items():
        if b >= nj:
            dropped += 1; continue
        root = joints[b]['parent'] < 0
        vals = [file_p(p) for p in seq]
        if root and flip:
            vals = [qrot(FLIP_Q, p) for p in vals]
        tracks.setdefault(b, {})['pos'] = sample(vals, lambda a, c, t: tuple(a[i] + (c[i] - a[i]) * t for i in range(3)))
    for b, seq in z['rot'].items():
        if b >= nj:
            dropped += 1; continue
        root = joints[b]['parent'] < 0
        vals = [file_q(q) for q in seq]
        if root and flip:
            vals = [qnorm(qmul(FLIP_Q, q)) for q in vals]
        rs = sample(vals, qslerp)
        for k in range(1, len(rs)):                                      # keep the hemisphere continuous (compact + lerp friendly)
            if sum(rs[k - 1][i] * rs[k][i] for i in range(4)) < 0:
                rs[k] = (-rs[k][0], -rs[k][1], -rs[k][2], -rs[k][3])
        tracks.setdefault(b, {})['rot'] = rs
    for b, seq in z['scale'].items():
        if b >= nj:
            dropped += 1; continue
        tracks.setdefault(b, {})['scale'] = sample(list(seq), lambda a, c, t: a + (c - a) * t)
    ev = []
    for f, e in z['events']:
        ev.append((min(n_out - 1, int(round(f * out_fps / float(src_fps)))), e))
    return dict(name=name, frames=n_out, fps=out_fps, src_fps=src_fps, loop=bool(loop), events=ev, dropped=dropped,
                tracks=[dict(joint=b, pos=t.get('pos'), rot=t.get('rot'), scale=t.get('scale')) for b, t in sorted(tracks.items())])


# ------------------------------------------------------------------ poses (checker, tests, reference for the Unity port)
def clip_local_pose(skel, clip, t):
    """local pose of every joint at time t (in frames of clip['fps']); loop wraps, else clamps to the last frame.
    Returns [(pos, rot, scale)]. Joints without a track keep their bind pose."""
    nf = clip['frames']
    if clip['loop']:
        t = t % nf
    else:
        t = max(0.0, min(nf - 1, t))
    i0 = int(math.floor(t)); f = t - i0
    i1 = i0 + 1
    if i1 >= nf:
        i1 = 0 if clip['loop'] else nf - 1
    out = [(j['lpos'], j['lrot'], 1.0) for j in skel['joints']]
    for tr in clip['tracks']:
        k = tr['joint']
        p, q, s = out[k]
        if tr['pos'] is not None:
            a, b = tr['pos'][i0], tr['pos'][i1]
            p = tuple(a[i] + (b[i] - a[i]) * f for i in range(3))
        if tr['rot'] is not None:
            q = qslerp(tr['rot'][i0], tr['rot'][i1], f) if f > 1e-9 else tr['rot'][i0]
        if tr['scale'] is not None:
            a, b = tr['scale'][i0], tr['scale'][i1]
            s = a + (b - a) * f
        out[k] = (p, q, s)
    return out


def global_pose(skel, local):
    """local [(pos, rot, scale)] -> global [(pos, rot)] (scale of a joint multiplies its local translation and children's offsets)"""
    joints = skel['joints']
    g = [None] * len(joints)

    def rec(i, depth=0):
        if g[i] is not None:
            return g[i]
        p, q, s = local[i]
        par = joints[i]['parent']
        if par < 0 or depth > len(joints):
            g[i] = (p, q, s)
        else:
            pp, pq, ps = rec(par, depth + 1)
            t = qrot(pq, (p[0] * ps, p[1] * ps, p[2] * ps))
            g[i] = ((pp[0] + t[0], pp[1] + t[1], pp[2] + t[2]), qnorm(qmul(pq, q)), ps * s)
        return g[i]

    for i in range(len(joints)):
        rec(i)
    return [(a, b) for a, b, _ in g]


def bind_pose(skel):
    return [(j['gpos'], j['grot']) for j in skel['joints']]


def skin_transforms(skel, pose):
    """global pose [(pos, rot)] -> skin transforms [(pos, rot)] = G_anim * inverse(G_bind): v' = rot * v + pos"""
    out = []
    for (gp, gq), j in zip(pose, skel['joints']):
        iq = qinv(j['grot'])
        ip = qrot(iq, (-j['gpos'][0], -j['gpos'][1], -j['gpos'][2]))
        r = qrot(gq, ip)
        out.append(((gp[0] + r[0], gp[1] + r[1], gp[2] + r[2]), qnorm(qmul(gq, iq))))
    return out


def skin_vertices(mesh, skin):
    """posed positions + normals of a CMSH mesh dict (palette slots, u8 weights) given skin transforms; -> ([pos], [normal])"""
    pal = mesh['palette']
    P = []; N = []
    for (x, y, z, nx, ny, nz, u, v), bi, bw in zip(mesh['verts'], mesh['bi'], mesh['bw']):
        ax = ay = az = 0.0; bx = by = bz = 0.0
        for s in range(4):
            w = bw[s] / 255.0
            if w <= 0:
                continue
            sp, sq = skin[pal[bi[s]]]
            r = qrot(sq, (x, y, z)); n = qrot(sq, (nx / 127.0, ny / 127.0, nz / 127.0))
            ax += w * (r[0] + sp[0]); ay += w * (r[1] + sp[1]); az += w * (r[2] + sp[2])
            bx += w * n[0]; by += w * n[1]; bz += w * n[2]
        P.append((ax, ay, az)); N.append(_n3((bx, by, bz)))
    return P, N
