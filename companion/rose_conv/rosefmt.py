"""Independent parsers written from ROSE_SPEC.md (sections 3.x)."""
import struct

def dec(b):
    b = b.split(b'\0', 1)[0]
    try: return b.decode('utf-8')
    except UnicodeDecodeError: return b.decode('cp949', 'replace')

class R:
    def __init__(s, d, p=0): s.d, s.p = d, p
    def u8(s): v = s.d[s.p]; s.p += 1; return v
    def u16(s): v, = struct.unpack_from('<H', s.d, s.p); s.p += 2; return v
    def u32(s): v, = struct.unpack_from('<I', s.d, s.p); s.p += 4; return v
    def f32(s): v, = struct.unpack_from('<f', s.d, s.p); s.p += 4; return v
    def vec(s, n): v = struct.unpack_from('<%df' % n, s.d, s.p); s.p += 4*n; return v
    def raw(s, n): v = s.d[s.p:s.p+n]; s.p += n; return v
    def nul(s):
        e = s.d.index(b'\0', s.p); v = dec(s.d[s.p:e]); s.p = e+1; return v
    def s8(s): return dec(s.raw(s.u8()))
    def s16(s): return dec(s.raw(s.u16()))
    def s32(s): return dec(s.raw(s.u32()))

def stb(d):
    r = R(d); assert r.raw(3) == b'STB'; ver = chr(r.u8())
    dpos = r.u32(); rows = r.u32(); cols = r.u32(); r.u32()
    r.p += 4 if ver == '0' else 2*(cols+1)
    names = [r.s16() for _ in range(cols)]
    r.s16()
    keys = [r.s16() for _ in range(rows-1)]
    r.p = dpos
    cells = [[r.s16() for _ in range(cols-1)] for _ in range(rows-1)]
    return dict(rows=rows-1, cols=cols-1, names=names, keys=keys, cells=cells, end=r.p, ver=ver)

def zon(d):
    r = R(d); n = r.u32(); blocks = [(r.u32(), r.u32()) for _ in range(n)]
    out = dict(events=[], textures=[], tiles=[])
    for t, off in blocks:
        r.p = off
        if t == 0:
            r.p += 12; out['gpp'] = r.u32(); out['gsz'] = r.f32()
        elif t == 1:
            for _ in range(r.u32()):
                p = r.vec(3); out['events'].append((r.s8(), p))
        elif t == 2:
            for _ in range(r.u32()): out['textures'].append(r.s8())
        elif t == 3:
            for _ in range(r.u32()): out['tiles'].append(struct.unpack('<7I', r.raw(28)))
    return out

def him(d):
    r = R(d); w = r.u32(); h = r.u32(); extra = r.raw(8)
    hs = struct.unpack_from('<%df' % (w*h), d, r.p)
    return w, h, extra, hs, len(d) == 16 + 4*w*h

def til(d):
    r = R(d); w = r.u32(); h = r.u32()
    t = []
    for _ in range(w*h): r.p += 3; t.append(r.u32())
    return w, h, t, r.p == len(d)

def zms(d):
    r = R(d); m = r.nul(); ver = int(m[3:])
    fl = r.u32(); r.vec(6)
    o = dict(ver=ver, flags=fl)
    if ver >= 7:
        bc = r.u16(); bones = [r.u16() for _ in range(bc)]; vc = r.u16()
        def arr(n): 
            v = [r.vec(n) for _ in range(vc)]; return v
        if fl & 2: o['pos'] = arr(3)
        if fl & 4: o['nrm'] = arr(3)
        if fl & 8: o['col'] = arr(4)
        if fl & 48 == 48: o['skin'] = [(r.vec(4), struct.unpack_from('<4H', r.raw(8))) for _ in range(vc)]
        if fl & 64: o['tan'] = arr(3)
        for i, b in enumerate((128, 256, 512, 1024)):
            if fl & b: o['uv%d' % (i+1)] = arr(2)
        tc = r.u16(); o['idx'] = struct.unpack_from('<%dH' % (tc*3), r.raw(tc*6))
        mc = r.u16(); o['mat'] = [r.u16() for _ in range(mc)]
        sc = r.u16(); r.p += 2*sc
        if ver >= 8: r.u16()
        o['bones'] = bones
    else:
        bc = r.u32(); bones = []
        for _ in range(bc): r.u32(); bones.append(r.u32())
        vc = r.u32()
        def arr(n): return [(r.u32(), r.vec(n))[1] for _ in range(vc)]
        if fl & 2: o['pos'] = [tuple(x/100 for x in p) for p in arr(3)]
        if fl & 4: o['nrm'] = arr(3)
        if fl & 8: o['col'] = arr(4)
        if fl & 48 == 48:
            o['skin'] = []
            for _ in range(vc):
                r.u32(); w = r.vec(4); ix = struct.unpack_from('<4I', r.raw(16)); o['skin'].append((w, tuple(bones[i] for i in ix)))
        if fl & 64: o['tan'] = arr(3)
        for i, b in enumerate((128, 256, 512, 1024)):
            if fl & b: o['uv%d' % (i+1)] = arr(2)
        tc = r.u32(); idx = []
        for _ in range(tc): r.u32(); idx += [r.u32(), r.u32(), r.u32()]
        o['idx'] = idx
        if ver >= 6:
            mc = r.u32(); o['mat'] = [(r.u32(), r.u32())[1] for _ in range(mc)]
    o['vc'] = vc; o['end'] = r.p; o['len'] = len(d)
    return o

def zmd(d):
    r = R(d); m = r.raw(7).decode(); ver = int(m[3:])
    nb = r.u32(); bones = []
    for _ in range(nb):
        par = r.u32(); name = r.nul(); pos = r.vec(3); q = r.vec(4); bones.append((par, name, pos, q))
    nd = r.u32(); dummies = []
    for _ in range(nd):
        name = r.nul(); par = r.u32(); pos = r.vec(3); q = r.vec(4) if ver == 3 else (1, 0, 0, 0); dummies.append((par, name, pos, q))
    return bones, dummies, r.p == len(d)

def zmo(d):
    r = R(d); assert r.nul() == 'ZMO0002'
    fps = r.u32(); nf = r.u32(); nc = r.u32()
    ch = [(r.u32(), r.u32()) for _ in range(nc)]
    sz = {1: 0, 2: 12, 4: 16, 8: 12, 16: 4, 32: 8, 64: 8, 128: 8, 256: 8, 512: 4, 1024: 4}
    body = nf * sum(sz[t] for t, _ in ch)
    start = r.p; r.p += body
    out = dict(fps=fps, nf=nf, nc=nc, ch=ch, body_end=r.p, len=len(d), tail=d[-4:])
    if d[-4:] in (b'EZMO', b'3ZMO'):
        off, = struct.unpack_from('<I', d, len(d)-8)
        rr = R(d, off); n = rr.u16(); ev = [rr.u16() for _ in range(n)]
        out['events_n'] = n; out['events_nonzero'] = sum(1 for e in ev if e); out['ext_off'] = off
        if d[-4:] == b'3ZMO': out['interp'] = rr.u32()
        out['ext_end'] = rr.p
    return out

def ifo(d):
    r = R(d); n = r.u32(); blocks = [(r.u32(), r.u32()) for _ in range(n)]
    out = {}
    def obj():
        name = r.s8(); warp = r.u16(); ev = r.u16(); ot = r.u32(); oid = r.u32(); mx = r.u32(); my = r.u32()
        q = r.vec(4); p = r.vec(3); s = r.vec(3)
        return dict(name=name, warp=warp, ev=ev, type=ot, id=oid, q=q, pos=p, scale=s)
    for t, off in blocks:
        r.p = off; L = []
        if t in (1, 3, 6, 10, 11):
            for _ in range(r.u32()): L.append(obj())
        elif t == 2:
            for _ in range(r.u32()): o = obj(); o['ai'] = r.u32(); o['quest'] = r.s8(); L.append(o)
        elif t == 4:
            for _ in range(r.u32()): o = obj(); o['path'] = r.s8(); o['range'] = r.u32(); o['interval'] = r.u32(); L.append(o)
        elif t == 5:
            for _ in range(r.u32()): o = obj(); o['path'] = r.s8(); L.append(o)
        elif t == 12:
            for _ in range(r.u32()): o = obj(); o['trig'] = r.s8(); o['fn'] = r.s8(); L.append(o)
        elif t == 8:
            for _ in range(r.u32()):
                o = obj(); r.s8()
                o['basic'] = [(r.s8(), r.u32(), r.u32()) for _ in range(r.u32())]
                o['tactic'] = [(r.s8(), r.u32(), r.u32()) for _ in range(r.u32())]
                o['interval'], o['limit'], o['range'], o['tp'] = r.u32(), r.u32(), r.u32(), r.u32(); L.append(o)
        elif t == 9:
            L = dict(size=r.f32(), planes=[(r.vec(3), r.vec(3)) for _ in range(r.u32())]) if False else None
            sz = r.f32(); L = (sz, [(r.vec(3), r.vec(3)) for _ in range(r.u32())])
        else:
            continue
        out[t] = L
    return out

def zsc(d):
    r = R(d)
    meshes = [r.nul() for _ in range(r.u16())]
    mats = []
    for _ in range(r.u16()):
        p = r.nul(); f = [r.u16() for _ in range(9)]; a = r.f32(); g = r.u16(); gc = r.vec(3)
        mats.append((p, f, a, g))
    effs = [r.nul() for _ in range(r.u16())]
    objs = []
    for _ in range(r.u16()):
        r.p += 12; pc = r.u16()
        if pc == 0: objs.append(([], [])); continue
        parts = []
        for _ in range(pc):
            mi = r.u16(); ma = r.u16(); prop = {}
            while True:
                pid = r.u8()
                if pid == 0: break
                sz = r.u8()
                if pid == 1: prop['pos'] = r.vec(3)
                elif pid == 2: prop['rot'] = r.vec(4)
                elif pid == 3: prop['scale'] = r.vec(3)
                elif pid == 4: r.p += 16
                elif pid == 5: prop['bone'] = r.u16()
                elif pid == 6: prop['dummy'] = r.u16()
                elif pid == 7: prop['parent'] = r.u16()
                elif pid == 29: prop['col'] = r.u16()
                elif pid == 30: prop['anim'] = dec(r.raw(sz))
                elif pid in (31, 32): r.p += 2
                else: raise ValueError('prop %d' % pid)
            parts.append((mi, ma, prop))
        es = []
        for _ in range(r.u16()):
            eid = r.u16(); et = r.u16(); prop = {}
            while True:
                pid = r.u8()
                if pid == 0: break
                r.u8()
                if pid == 1: prop['pos'] = r.vec(3)
                elif pid == 2: prop['rot'] = r.vec(4)
                elif pid == 3: prop['scale'] = r.vec(3)
                elif pid == 7: prop['parent'] = r.u16()
                else: raise ValueError('eprop %d' % pid)
            es.append((eid, et, prop))
        r.p += 24
        objs.append((parts, es))
    return dict(meshes=meshes, mats=mats, effs=effs, objs=objs, end=r.p, len=len(d))

def vstr(r):
    b = r.u8()
    n = b if not b & 0x80 else (r.u8() << 7) | (b & 0x7f)
    return dec(r.raw(n))

def stl(d):
    r = R(d); typ = vstr(r); kc = r.u32(); keys = {}
    for i in range(kc):
        k = vstr(r); keys[k] = (i, r.u32())
    lc = r.u32(); offs = [r.u32() for _ in range(lc)]
    langs = []
    n = {'ITST01': 2, 'NRST01': 1, 'QEST01': 4}[typ]
    for lo in offs:
        r.p = lo; ent = [r.u32() for _ in range(kc)]; L = []
        for eo in ent:
            r.p = eo; L.append([vstr(r) for _ in range(n)])
        langs.append(L)
    return typ, keys, langs

def chr_(d):
    r = R(d)
    sk = [r.nul() for _ in range(r.u16())]; mo = [r.nul() for _ in range(r.u16())]; ef = [r.nul() for _ in range(r.u16())]
    n = r.u16(); npcs = {}
    for i in range(n):
        if r.u8() == 0: continue
        s = r.u16(); name = r.nul()
        models = [r.u16() for _ in range(r.u16())]
        motions = [(r.u16(), r.u16()) for _ in range(r.u16())]
        effects = [(r.u16(), r.u16()) for _ in range(r.u16())]
        npcs[i] = dict(skel=s, name=name, models=models, motions=motions, effects=effects)
    return sk, mo, ef, npcs, r.p == len(d)


def lit(d):
    """LIT lightmap assignment (ROSE_SPEC 3.5) -> {object_id: [dict(part, name, file, extra, ppr, slot)]}; object_id = IFO list index + 1."""
    r = R(d); n = r.u32(); out = {}
    for _ in range(n):
        pc = r.u32(); oid = r.u32(); L = []
        for _ in range(pc):
            nm = dec(r.raw(r.u8())); part = r.u32(); fn = r.s8(); extra = r.vec(2) if False else r.raw(8); ppr = r.u32(); slot = r.u32()
            L.append(dict(part=part, name=nm, file=fn, extra=bytes(extra), ppr=ppr, slot=slot))
        out.setdefault(oid, []).extend(L)
    return out
