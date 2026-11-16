"""RPG1 pack container + section codecs (stdlib only). Byte layout: docs/RPG1_PACK.md.

Everything is little endian. `write_pack` / `read_pack` handle the container; `enc_*` / `dec_*` are the section codecs
shared by the converter (rose_pack.py), the checker (rose_pack_check.py) and the tests.
"""
import struct, zlib, sys, array, math

MAGIC = b'RPG1'
VERSION = 0x00010005          # newest minor this module writes. 1.5: SNDS (zone music + sound effects). 1.4: UIAT UIDL (UI atlas + dialogs). 1.1: characters (SKEL CMSH CANM ACTR AVAT); 1.2: SPWR NBEH WATR SKYB EVNT OCOL; 1.3: DROP SKIL. Older readers ignore new tags
VERSION_1_1 = 0x00010001
VERSION_1_2 = 0x00010002
VERSION_1_3 = 0x00010003
VERSION_1_4 = 0x00010004
TAGS_1_5 = (b'SNDS', b'FLDM')
TAGS_1_4 = (b'UIAT', b'UIDL')
TAGS_1_3 = (b'DROP', b'SKIL')
TAGS_1_2 = (b'SPWR', b'NBEH', b'WATR', b'SKYB', b'EVNT', b'OCOL')
TAGS_1_1 = (b'SKEL', b'CMSH', b'CANM', b'ACTR', b'AVAT')
HEADER_BYTES = 32
DIR_ENTRY = 24

TERR_STRIDE = 36
OMSH_STRIDE = 24


def _nopad(v):
    """drop the pad byte after the three i8 normal components"""
    return v[:6] + v[7:]


def _pad4(b):
    return b + b'\0' * (-len(b) % 4)


def pack_version(tags):
    """minor version a pack with these section tags needs: 1.5 if SNDS is present, else 1.4 if any 1.4 section (UIAT UIDL) is present, else 1.3 if any 1.3 section (DROP SKIL) is present, else 1.2 if any 1.2 section is present, else 1.1
    (the file format never writes 1.0 any more). A minor is stamped only when its sections are there, so a pack without them stays readable by the older loaders."""
    if any(t in TAGS_1_5 for t in tags):
        return VERSION
    if any(t in TAGS_1_4 for t in tags):
        return VERSION_1_4
    if any(t in TAGS_1_3 for t in tags):
        return VERSION_1_3
    return VERSION_1_2 if any(t in TAGS_1_2 for t in tags) else VERSION_1_1


MAX_SECTIONS = 48      # directory limit (GoldSrcVR GsvrRpg1Pack.MaxSections); a full zone has 33 sections since the wave 2 merge (docs/RPG1_PACK.md section 19)


def write_pack(sections, version=None):
    """sections: list of (tag: 4 bytes, section_version: int, count: int, data: bytes) -> pack bytes. `version` defaults to pack_version(tags)."""
    if len(sections) > MAX_SECTIONS:
        raise ValueError('%d sections, the directory holds at most %d' % (len(sections), MAX_SECTIONS))
    out = bytearray(HEADER_BYTES)
    ents = []
    for tag, sver, count, data in sections:
        if len(tag) != 4:
            raise ValueError('tag must be 4 bytes')
        data = bytes(data)
        off = len(out)
        out += data
        out += b'\0' * (-len(out) % 4)
        ents.append((tag, off, len(data), count, zlib.crc32(data) & 0xffffffff, sver))
    dir_off = len(out)
    for e in ents:
        out += struct.pack('<4s5I', *e)
    total = len(out)
    ver = pack_version([e[0] for e in ents]) if version is None else version
    out[0:HEADER_BYTES] = struct.pack('<4s7I', MAGIC, ver, HEADER_BYTES, dir_off, len(ents), total, 0, 0)
    return bytes(out)


def read_pack(buf, verify=True):
    """-> dict(version, sections={tag: dict(off,size,count,crc,ver,data)}, order=[tags]). Raises ValueError on a bad container."""
    if len(buf) < HEADER_BYTES or buf[:4] != MAGIC:
        raise ValueError('not an RPG1 pack')
    magic, ver, hb, dir_off, n, total, flags, _r = struct.unpack_from('<4s7I', buf, 0)
    if (ver >> 16) != (VERSION >> 16):
        raise ValueError('unsupported major version %d' % (ver >> 16))
    if total != len(buf):
        raise ValueError('file size %d != header %d' % (len(buf), total))
    if hb < HEADER_BYTES or dir_off < hb or dir_off + n * DIR_ENTRY > len(buf):
        raise ValueError('bad directory')
    secs = {}; order = []
    for i in range(n):
        tag, off, size, count, crc, sver = struct.unpack_from('<4s5I', buf, dir_off + i * DIR_ENTRY)
        if off < hb or off + size > dir_off or off % 4:
            raise ValueError('section %r out of bounds' % tag)
        data = buf[off:off + size]
        if verify and (zlib.crc32(data) & 0xffffffff) != crc:
            raise ValueError('section %r crc mismatch' % tag)
        if tag in secs:
            raise ValueError('duplicate section %r' % tag)
        secs[tag] = dict(off=off, size=size, count=count, crc=crc, ver=sver, data=data)
        order.append(tag)
    return dict(version=ver, flags=flags, sections=secs, order=order)


# ---------------------------------------------------------------- AREA (meta, 128 bytes)
AREA_FIELDS = ('zone_id', 'blocks_x', 'blocks_y', 'first_bx', 'first_by', 'block_size', 'grid_w', 'grid_h', 'cell_size',
               'origin_x', 'origin_z', 'world_off_x', 'world_off_z', 'start_x', 'start_y', 'start_z',
               'bmin_x', 'bmin_y', 'bmin_z', 'bmax_x', 'bmax_y', 'bmax_z', 'height_unit', 'water_level',
               'atlas_gutter_px', 'terrain_step')
_AREA_FMT = '<5if2if' + 'f' * 2 + 'f' * 2 + '3f' + '6f' + '2f' + '2i'
AREA_BYTES = 128


def enc_area(a):
    vals = [a[k] for k in AREA_FIELDS]
    b = struct.pack(_AREA_FMT, *vals)
    return b + b'\0' * (AREA_BYTES - len(b))


def dec_area(d):
    if len(d) < AREA_BYTES:
        raise ValueError('AREA too short')
    return dict(zip(AREA_FIELDS, struct.unpack_from(_AREA_FMT, d, 0)))


# ---------------------------------------------------------------- CELL / HGHT
def enc_hght(vals):
    arr = array.array('h', vals)
    if sys.byteorder == 'big':
        arr.byteswap()
    return arr.tobytes()


def dec_hght(d):
    arr = array.array('h'); arr.frombytes(bytes(d))
    if sys.byteorder == 'big':
        arr.byteswap()
    return list(arr)


# ---------------------------------------------------------------- TERR
TERR_HDR = struct.Struct('<4I')
TERR_REC = struct.Struct('<2i4I2f2f')          # bx,by,vc,tc,vo,io,ox,oz,miny,maxy = 40 bytes
VTX_TERR = struct.Struct('<3f4b4B4f')          # pos, normal(i8 x3 + pad), rgba, uv0, uv1 = 36 bytes


def enc_terr(blocks):
    """blocks: list of dict(bx,by,ox,oz,verts=[(x,y,z,nx,ny,nz,r,g,b,a,u0,v0,u1,v1)...], tris=[(i,j,k)...]) (normals as -127..127)."""
    recs = bytearray(); vb = bytearray(); ib = bytearray()
    for b in blocks:
        ys = [v[1] for v in b['verts']] or [0.0]
        recs += TERR_REC.pack(b['bx'], b['by'], len(b['verts']), len(b['tris']), len(vb), len(ib), b['ox'], b['oz'], min(ys), max(ys))
        for v in b['verts']:
            vb += VTX_TERR.pack(v[0], v[1], v[2], v[3], v[4], v[5], 0, v[6], v[7], v[8], v[9], v[10], v[11], v[12], v[13])
        for t in b['tris']:
            ib += struct.pack('<3H', *t)
    vb = _pad4(vb)
    vo = TERR_HDR.size + len(recs)
    io = vo + len(vb)
    return TERR_HDR.pack(len(blocks), TERR_STRIDE, vo, io) + bytes(recs) + bytes(vb) + bytes(ib)


def dec_terr(d):
    n, stride, vo, io = TERR_HDR.unpack_from(d, 0)
    if stride != TERR_STRIDE:
        raise ValueError('terrain stride %d' % stride)
    out = []
    for i in range(n):
        bx, by, vc, tc, voff, ioff, ox, oz, miny, maxy = TERR_REC.unpack_from(d, TERR_HDR.size + i * TERR_REC.size)
        if vo + voff + vc * stride > io or io + ioff + tc * 6 > len(d):
            raise ValueError('terrain block %d_%d out of bounds' % (bx, by))
        verts = [_nopad(VTX_TERR.unpack_from(d, vo + voff + k * stride)) for k in range(vc)]
        idx = struct.unpack_from('<%dH' % (tc * 3), d, io + ioff)
        out.append(dict(bx=bx, by=by, ox=ox, oz=oz, miny=miny, maxy=maxy, verts=verts, tris=[idx[k:k + 3] for k in range(0, len(idx), 3)]))
    return out


# ---------------------------------------------------------------- TATL (terrain atlas)
TATL_HDR = struct.Struct('<8I')     # format,width,height,mips,slots,slot_px,gutter_px,reserved
TATL_SLOT = struct.Struct('<I4fI')  # zon texture index, u0,v0,u1,v1 (inset content rect), flags


def enc_tatl(fmt, w, h, levels, slot_px, gutter_px, slots):
    """slots: list of (zon_tex_index, u0, v0, u1, v1, flags). levels: list of bytes (mip 0 first)."""
    out = TATL_HDR.pack(fmt, w, h, len(levels), len(slots), slot_px, gutter_px, 0)
    for s in slots:
        out += TATL_SLOT.pack(*s)
    for lv in levels:
        out += lv
    return out


def dec_tatl(d):
    fmt, w, h, nm, ns, slot_px, gut, _ = TATL_HDR.unpack_from(d, 0)
    slots = [TATL_SLOT.unpack_from(d, TATL_HDR.size + i * TATL_SLOT.size) for i in range(ns)]
    pos = TATL_HDR.size + ns * TATL_SLOT.size
    from rosedds import level_size
    levels = []; lw, lh = w, h
    for _ in range(nm):
        n = level_size(fmt, lw, lh)
        levels.append(bytes(d[pos:pos + n])); pos += n
        lw = max(1, lw // 2); lh = max(1, lh // 2)
    if pos > len(d):
        raise ValueError('atlas data truncated')
    return dict(fmt=fmt, w=w, h=h, slot_px=slot_px, gutter_px=gut, slots=slots, levels=levels)


# ---------------------------------------------------------------- TTIL (tile indices + ZON tile table)
def enc_ttil(tiles, blocks):
    """tiles: list of (slot1, slot2, rotation, blend); blocks: list of (bx, by, [256 tile indices])."""
    out = struct.pack('<2I', len(tiles), len(blocks))
    for s1, s2, rot, bl in tiles:
        out += struct.pack('<2H2BH', s1, s2, rot, bl, 0)
    for bx, by, idx in blocks:
        out += struct.pack('<2i', bx, by) + struct.pack('<256H', *idx)
    return out


def dec_ttil(d):
    nt, nb = struct.unpack_from('<2I', d, 0)
    pos = 8
    tiles = []
    for _ in range(nt):
        s1, s2, rot, bl, _p = struct.unpack_from('<2H2BH', d, pos); pos += 8
        tiles.append((s1, s2, rot, bl))
    blocks = []
    for _ in range(nb):
        bx, by = struct.unpack_from('<2i', d, pos); pos += 8
        blocks.append((bx, by, list(struct.unpack_from('<256H', d, pos)))); pos += 512
    return tiles, blocks


# ---------------------------------------------------------------- OTEX (object textures)
OTEX_REC = struct.Struct('<8I')     # format,w,h,mips,data_off,data_size,name_off,name_len


def enc_otex(texs):
    """texs: list of dict(name, fmt, w, h, levels). Section: u32 count, u32 names_off, u32 data_off, records, names, data."""
    names = bytearray(); data = bytearray(); recs = bytearray()
    for t in texs:
        nm = t['name'].encode('utf-8')
        blob = b''.join(t['levels'])
        recs += OTEX_REC.pack(t['fmt'], t['w'], t['h'], len(t['levels']), len(data), len(blob), len(names), len(nm))
        names += nm
        data += blob
        data += b'\0' * (-len(data) % 4)
    names = _pad4(names)
    return struct.pack('<3I', len(texs), 12 + len(recs), 12 + len(recs) + len(names)) + bytes(recs) + bytes(names) + bytes(data)


def dec_otex(d):
    from rosedds import level_size
    n, no, do = struct.unpack_from('<3I', d, 0)
    out = []
    for i in range(n):
        fmt, w, h, nm, doff, dsize, noff, nlen = OTEX_REC.unpack_from(d, 12 + i * OTEX_REC.size)
        name = bytes(d[no + noff:no + noff + nlen]).decode('utf-8')
        blob = d[do + doff:do + doff + dsize]
        levels = []; pos = 0; lw, lh = w, h
        for _ in range(nm):
            sz = level_size(fmt, lw, lh)
            levels.append(bytes(blob[pos:pos + sz])); pos += sz
            lw = max(1, lw // 2); lh = max(1, lh // 2)
        if pos != dsize:
            raise ValueError('texture %s size mismatch (%d vs %d)' % (name, pos, dsize))
        out.append(dict(name=name, fmt=fmt, w=w, h=h, levels=levels))
    return out


# ---------------------------------------------------------------- OMAT (object materials)
OMAT_REC = struct.Struct('<2I2f2I')   # texture index (0xFFFFFFFF none), flags, alpha_ref, alpha, reserved x2
MF_BLEND, MF_TWOSIDED, MF_ALPHATEST, MF_ADDITIVE, MF_SPECULAR = 1, 2, 4, 8, 16
NO_TEX = 0xFFFFFFFF


def enc_omat(mats):
    return b''.join(OMAT_REC.pack(m['tex'], m['flags'], m['alpha_ref'], m['alpha'], 0, 0) for m in mats)


def dec_omat(d):
    return [dict(zip(('tex', 'flags', 'alpha_ref', 'alpha'), OMAT_REC.unpack_from(d, i * OMAT_REC.size)[:4])) for i in range(len(d) // OMAT_REC.size)]


# ---------------------------------------------------------------- OMSH (merged object mesh chunks)
OMSH_HDR = struct.Struct('<4I')                # count, stride, vert_blob_off, idx_blob_off
OMSH_REC = struct.Struct('<I2i4I6f')           # material,bx,by,vc,ic,vo,io,bbmin xyz,bbmax xyz = 52
VTX_OBJ = struct.Struct('<3f4b2f')


def enc_omsh(chunks):
    """chunks: list of dict(mat,bx,by,verts=[(x,y,z,nx,ny,nz,u,v)],idx=[i...])."""
    recs = bytearray(); vb = bytearray(); ib = bytearray()
    for c in chunks:
        vs = c['verts']
        lo = [min(v[k] for v in vs) for k in range(3)]; hi = [max(v[k] for v in vs) for k in range(3)]
        recs += OMSH_REC.pack(c['mat'], c['bx'], c['by'], len(vs), len(c['idx']), len(vb), len(ib), *lo, *hi)
        for v in vs:
            vb += VTX_OBJ.pack(v[0], v[1], v[2], v[3], v[4], v[5], 0, v[6], v[7])
        ib += struct.pack('<%dH' % len(c['idx']), *c['idx'])
        ib += b'\0' * (-len(ib) % 4)
    vo = OMSH_HDR.size + len(recs)
    io = vo + len(vb)
    return OMSH_HDR.pack(len(chunks), OMSH_STRIDE, vo, io) + bytes(recs) + bytes(vb) + bytes(ib)


def dec_omsh(d):
    n, stride, vo, io = OMSH_HDR.unpack_from(d, 0)
    if stride != OMSH_STRIDE:
        raise ValueError('object stride %d' % stride)
    out = []
    for i in range(n):
        mat, bx, by, vc, ic, voff, ioff, *bb = OMSH_REC.unpack_from(d, OMSH_HDR.size + i * OMSH_REC.size)
        if vo + voff + vc * stride > io or io + ioff + ic * 2 > len(d):
            raise ValueError('object chunk %d out of bounds' % i)
        verts = [_nopad(VTX_OBJ.unpack_from(d, vo + voff + k * stride)) for k in range(vc)]
        idx = list(struct.unpack_from('<%dH' % ic, d, io + ioff))
        out.append(dict(mat=mat, bx=bx, by=by, bbmin=tuple(bb[:3]), bbmax=tuple(bb[3:]), verts=verts, idx=idx))
    return out


# ---------------------------------------------------------------- SPWN / SPWE
SPWN_REC = struct.Struct('<BBH4f2h2HI4i')      # kind,pad,npc,x,y,z,yaw,bx,by,nbasic,ntactic,first,interval,limit,range,tp = 48
SPWE_REC = struct.Struct('<2H')
KIND_SPAWN, KIND_NPC = 0, 1


def enc_spawns(spawns):
    """spawns: list of dict(kind,npc,x,y,z,yaw,bx,by,basic=[(npc,count)],tactic=[(npc,count)],interval,limit,range,tp)."""
    recs = bytearray(); ents = bytearray(); n = 0
    for s in spawns:
        basic, tactic = s.get('basic', []), s.get('tactic', [])
        recs += SPWN_REC.pack(s['kind'], 0, s.get('npc', 0), s['x'], s['y'], s['z'], s['yaw'], s['bx'], s['by'],
                              len(basic), len(tactic), n, s.get('interval', 0), s.get('limit', 0), s.get('range', 0), s.get('tp', 0))
        for e in basic + tactic:
            ents += SPWE_REC.pack(*e)
        n += len(basic) + len(tactic)
    return bytes(recs), bytes(ents), len(spawns), n


def dec_spawns(recs, ents):
    out = []
    for i in range(len(recs) // SPWN_REC.size):
        k, _p, npc, x, y, z, yaw, bx, by, nb, nt, first, iv, lim, rng, tp = SPWN_REC.unpack_from(recs, i * SPWN_REC.size)
        es = [SPWE_REC.unpack_from(ents, (first + j) * SPWE_REC.size) for j in range(nb + nt)]
        out.append(dict(kind=k, npc=npc, x=x, y=y, z=z, yaw=yaw, bx=bx, by=by, basic=es[:nb], tactic=es[nb:], interval=iv, limit=lim, range=rng, tp=tp, first=first))
    return out


# ================================================================ characters (version 1.1): SKEL CMSH CANM ACTR AVAT
def _cstr_blob():
    """tiny NUL-terminated string table builder: add(s) -> offset"""
    class B:
        def __init__(self): self.b = bytearray(); self.d = {}
        def add(self, s):
            s = s or ''
            if s not in self.d:
                self.d[s] = len(self.b); self.b += s.encode('utf-8') + b'\0'
            return self.d[s]
        def bytes(self): return _pad4(bytes(self.b))
    return B()


def _cstr(blob, off):
    e = blob.index(b'\0', off)
    return bytes(blob[off:e]).decode('utf-8')


# ---------------------------------------------------------------- SKEL
SKEL_HDR = struct.Struct('<4I')                 # skelCount, jointStride (64), jointsOff, namesOff
SKEL_REC = struct.Struct('<8I')                 # firstJoint, jointCount, realBones, nameOff, flags, 0,0,0       = 32
JOINT_REC = struct.Struct('<hH3f4f3f4fI')       # parent, flags, lpos, lrot, gpos, grot, nameOff                  = 64
SKF_FACES_PLUS_Z = 1


def enc_skel(skels):
    """skels: list of dict(name, nreal, flags, joints=[dict(parent, flags, name, lpos, lrot, gpos, grot)]). Joint order: real bones then dummies."""
    names = _cstr_blob(); recs = bytearray(); jb = bytearray(); first = 0
    for sk in skels:
        recs += SKEL_REC.pack(first, len(sk['joints']), sk['nreal'], names.add(sk['name']), sk.get('flags', SKF_FACES_PLUS_Z), 0, 0, 0)
        for j in sk['joints']:
            jb += JOINT_REC.pack(j['parent'], j['flags'], *j['lpos'], *j['lrot'], *j['gpos'], *j['grot'], names.add(j['name']))
        first += len(sk['joints'])
    jo = SKEL_HDR.size + len(recs)
    nb = names.bytes()
    return SKEL_HDR.pack(len(skels), JOINT_REC.size, jo, jo + len(jb)) + bytes(recs) + bytes(jb) + nb


def dec_skel(d):
    n, stride, jo, no = SKEL_HDR.unpack_from(d, 0)
    if stride < JOINT_REC.size:
        raise ValueError('joint stride %d' % stride)
    names = d[no:]
    out = []
    for i in range(n):
        first, cnt, nreal, noff, flags, *_ = SKEL_REC.unpack_from(d, SKEL_HDR.size + i * SKEL_REC.size)
        if jo + (first + cnt) * stride > no:
            raise ValueError('skeleton %d joints out of bounds' % i)
        joints = []
        for k in range(cnt):
            v = JOINT_REC.unpack_from(d, jo + (first + k) * stride)
            joints.append(dict(parent=v[0], flags=v[1], lpos=v[2:5], lrot=v[5:9], gpos=v[9:12], grot=v[12:16], name=_cstr(names, v[16])))
        out.append(dict(name=_cstr(names, noff), nreal=nreal, flags=flags, joints=joints))
    return out


# ---------------------------------------------------------------- CMSH
CMSH_HDR = struct.Struct('<8I')                 # meshCount, vertStride (32), vertOff, idxOff, palOff, namesOff, 0, 0
CMSH_REC = struct.Struct('<10I6f')              # skeleton, material, vc, ic, vo, io, palOff(entries), palCount, flags, nameOff, bbmin, bbmax = 64
VTX_CMSH = struct.Struct('<3f4b2f4B4B')         # pos, normal(i8 x3 + pad), uv, bone slots u8 x4, weights u8 x4 = 32
CMF_RIGID = 1


def enc_cmsh(meshes):
    """meshes: list of dict(name, skeleton, material, flags, verts=[(x,y,z,nx,ny,nz,u,v)], bi=[4 slots], bw=[4 u8], idx=[...], palette=[joint ids], bbmin, bbmax)."""
    names = _cstr_blob(); recs = bytearray(); vb = bytearray(); ib = bytearray(); pb = bytearray(); pal_n = 0
    for m in meshes:
        recs += CMSH_REC.pack(m['skeleton'], m['material'], len(m['verts']), len(m['idx']), len(vb), len(ib), pal_n, len(m['palette']),
                              m.get('flags', 0), names.add(m.get('name', '')), *m['bbmin'], *m['bbmax'])
        for v, bi, bw in zip(m['verts'], m['bi'], m['bw']):
            vb += VTX_CMSH.pack(v[0], v[1], v[2], v[3], v[4], v[5], 0, v[6], v[7], *bi, *bw)
        ib += struct.pack('<%dH' % len(m['idx']), *m['idx'])
        ib += b'\0' * (-len(ib) % 4)
        pb += struct.pack('<%dH' % len(m['palette']), *m['palette'])
        pal_n += len(m['palette'])
    pb = _pad4(pb)
    nb = names.bytes()
    vo = CMSH_HDR.size + len(recs); io = vo + len(vb); po = io + len(ib); no = po + len(pb)
    return CMSH_HDR.pack(len(meshes), VTX_CMSH.size, vo, io, po, no, 0, 0) + bytes(recs) + bytes(vb) + bytes(ib) + bytes(pb) + nb


def dec_cmsh(d):
    n, stride, vo, io, po, no, _a, _b = CMSH_HDR.unpack_from(d, 0)
    if stride != VTX_CMSH.size:
        raise ValueError('cmsh stride %d' % stride)
    names = d[no:]
    out = []
    for i in range(n):
        sk, mat, vc, ic, voff, ioff, poff, pc, flags, noff, *bb = CMSH_REC.unpack_from(d, CMSH_HDR.size + i * CMSH_REC.size)
        if vo + voff + vc * stride > io or io + ioff + ic * 2 > po or po + (poff + pc) * 2 > no:
            raise ValueError('skinned mesh %d out of bounds' % i)
        verts = []; bis = []; bws = []
        for k in range(vc):
            v = VTX_CMSH.unpack_from(d, vo + voff + k * stride)
            verts.append((v[0], v[1], v[2], v[3], v[4], v[5], v[7], v[8])); bis.append(v[9:13]); bws.append(v[13:17])
        out.append(dict(skeleton=sk, material=mat, flags=flags, name=_cstr(names, noff), verts=verts, bi=bis, bw=bws,
                        idx=list(struct.unpack_from('<%dH' % ic, d, io + ioff)),
                        palette=list(struct.unpack_from('<%dH' % pc, d, po + poff * 2)), bbmin=tuple(bb[:3]), bbmax=tuple(bb[3:])))
    return out


# ---------------------------------------------------------------- CANM
CANM_HDR = struct.Struct('<8I')                 # clipCount, fps, clipsOff, tracksOff, eventsOff, dataOff, namesOff, 0
CANM_CLIP = struct.Struct('<7I2fI')             # skeleton, frames, flags, firstTrack, trackCount, firstEvent, eventCount, duration(f), srcFps(f), nameOff       = 40
CANM_TRACK = struct.Struct('<HBBIII')           # joint, mask, 0, dataOff, dataSize, 0                                                                              = 16
CANM_EVENT = struct.Struct('<2H')
CAF_LOOP = 1
TM_POS, TM_ROT, TM_SCALE = 1, 2, 4


def _snorm16(v):
    return max(-32767, min(32767, int(round(v * 32767.0))))


def enc_canm(clips, fps):
    """clips: list of dict(name, skeleton, frames, loop, src_fps, tracks=[dict(joint, pos|None, rot|None, scale|None)], events=[(frame, id)]).
    pos is quantised to u16 inside a per-track min/range box, rot to 4 x snorm16 (xyzw, renormalised on decode), scale stays f32."""
    names = _cstr_blob(); cb = bytearray(); tb = bytearray(); eb = bytearray(); db = bytearray(); ntr = 0; nev = 0
    for c in clips:
        n = c['frames']
        for tr in c['tracks']:
            mask = (TM_POS if tr['pos'] is not None else 0) | (TM_ROT if tr['rot'] is not None else 0) | (TM_SCALE if tr['scale'] is not None else 0)
            start = len(db)
            if tr['pos'] is not None:
                lo = [min(p[i] for p in tr['pos']) for i in range(3)]; hi = [max(p[i] for p in tr['pos']) for i in range(3)]
                rg = [h - l for l, h in zip(lo, hi)]
                db += struct.pack('<6f', *lo, *rg)
                for p in tr['pos']:
                    db += struct.pack('<3H', *[int(round((p[i] - lo[i]) / rg[i] * 65535)) if rg[i] > 0 else 0 for i in range(3)])
                db += b'\0' * (-len(db) % 4)
            if tr['rot'] is not None:
                for q in tr['rot']:
                    db += struct.pack('<4h', *[_snorm16(x) for x in q])
            if tr['scale'] is not None:
                db += struct.pack('<%df' % n, *tr['scale'])
            tb += CANM_TRACK.pack(tr['joint'], mask, 0, start, len(db) - start, 0)
        for f, e in c['events']:
            eb += CANM_EVENT.pack(f, e)
        cb += CANM_CLIP.pack(c['skeleton'], n, CAF_LOOP if c['loop'] else 0, ntr, len(c['tracks']), nev, len(c['events']),
                             n / float(fps), float(c.get('src_fps', fps)), names.add(c['name']))
        ntr += len(c['tracks']); nev += len(c['events'])
    co = CANM_HDR.size; to = co + len(cb); eo = to + len(tb); do = eo + len(eb); no = do + len(db)
    return CANM_HDR.pack(len(clips), fps, co, to, eo, do, no, 0) + bytes(cb) + bytes(tb) + bytes(eb) + bytes(db) + names.bytes()


def dec_canm(d):
    n, fps, co, to, eo, do, no, _ = CANM_HDR.unpack_from(d, 0)
    names = d[no:]
    clips = []
    for i in range(n):
        sk, fr, flags, ft, tc, fe, ec, dur, sfps, noff = CANM_CLIP.unpack_from(d, co + i * CANM_CLIP.size)
        tracks = []
        for k in range(tc):
            joint, mask, _p, doff, dsize, _r = CANM_TRACK.unpack_from(d, to + (ft + k) * CANM_TRACK.size)
            pos = rot = scale = None
            p = do + doff
            if mask & TM_POS:
                v = struct.unpack_from('<6f', d, p)
                raw = struct.unpack_from('<%dH' % (3 * fr), d, p + 24)
                pos = [tuple(v[a] + raw[f * 3 + a] / 65535.0 * v[3 + a] for a in range(3)) for f in range(fr)]
                p += 24 + ((6 * fr + 3) // 4) * 4
            if mask & TM_ROT:
                raw = struct.unpack_from('<%dh' % (4 * fr), d, p); p += 8 * fr
                rot = []
                for f in range(fr):
                    q = [x / 32767.0 for x in raw[f * 4:f * 4 + 4]]
                    ln = math.sqrt(sum(x * x for x in q)) or 1.0
                    rot.append(tuple(x / ln for x in q))
            if mask & TM_SCALE:
                scale = list(struct.unpack_from('<%df' % fr, d, p)); p += 4 * fr
            if p - (do + doff) != dsize:
                raise ValueError('clip %d track %d size mismatch' % (i, k))
            tracks.append(dict(joint=joint, pos=pos, rot=rot, scale=scale))
        events = [CANM_EVENT.unpack_from(d, eo + (fe + k) * CANM_EVENT.size) for k in range(ec)]
        clips.append(dict(name=_cstr(names, noff), skeleton=sk, frames=fr, loop=bool(flags & CAF_LOOP), fps=fps, src_fps=sfps, duration=dur, tracks=tracks, events=events))
    return clips, fps


# ---------------------------------------------------------------- ACTR
ACTR_HDR = struct.Struct('<6I')                 # actorCount, stride (128), recsOff, stringsOff, listsOff, 0
ACTR_REC = struct.Struct('<HHI HH II 6H HH IIHH 8f IHH 12h HH')
ACTR_BYTES = 128
ACF_NPC, ACF_MAGIC, ACF_UNTARGETABLE = 1, 2, 4
NO_ID16 = 0xFFFF


def enc_actr(actors):
    """actors: list of dict (see ACTR_FIELDS) ; 'meshes' = list of CMSH ids ; 'clips' = 12 clip ids (-1 none)."""
    st = _cstr_blob(); recs = bytearray(); lists = bytearray(); lcount = 0
    for a in actors:
        clips = list(a['clips']) + [-1] * (12 - len(a['clips']))
        rec = ACTR_REC.pack(a['npc'], a['flags'], st.add(a['name']), a['level'], a['skeleton'],
                            a['hp'], a['attack'], a['hit'], a['defence'], a['resistance'], a['avoid'], a['attack_speed'], a['attack_range'],
                            a['npc_type'], a['ai'], a['exp'], a['drop_row'], a['money_drop_pct'], a['item_drop_pct'],
                            a['walk_speed'], a['run_speed'], a['scale'], a['radius'], a['height'], a['walk_anim_speed'], a['run_anim_speed'], a['attack_anim_speed'],
                            lcount, len(a['meshes']), 0, *clips[:12], a['weapon_r'], a['weapon_l'])
        recs += rec + b'\0' * (ACTR_BYTES - len(rec))
        lists += struct.pack('<%dH' % len(a['meshes']), *a['meshes']); lcount += len(a['meshes'])
    lists = _pad4(lists)
    ro = ACTR_HDR.size; so = ro + len(recs); lo = so + len(st.bytes())
    return ACTR_HDR.pack(len(actors), ACTR_BYTES, ro, so, lo, 0) + bytes(recs) + st.bytes() + bytes(lists)


ACTR_KEYS = ('npc', 'flags', 'nameOff', 'level', 'skeleton', 'hp', 'attack', 'hit', 'defence', 'resistance', 'avoid', 'attack_speed', 'attack_range',
             'npc_type', 'ai', 'exp', 'drop_row', 'money_drop_pct', 'item_drop_pct', 'walk_speed', 'run_speed', 'scale', 'radius', 'height',
             'walk_anim_speed', 'run_anim_speed', 'attack_anim_speed', 'meshFirst', 'meshCount', 'pad')


def dec_actr(d):
    n, stride, ro, so, lo, _ = ACTR_HDR.unpack_from(d, 0)
    if stride < ACTR_BYTES:
        raise ValueError('actor stride %d' % stride)
    out = []
    nk = len(ACTR_KEYS)
    for i in range(n):
        v = ACTR_REC.unpack_from(d, ro + i * stride)
        a = dict(zip(ACTR_KEYS, v[:nk]))
        a['name'] = _cstr(d[so:lo], a.pop('nameOff'))
        a['clips'] = list(v[nk:nk + 12]); a['weapon_r'], a['weapon_l'] = v[nk + 12], v[nk + 13]
        a['meshes'] = list(struct.unpack_from('<%dH' % a['meshCount'], d, lo + a['meshFirst'] * 2))
        del a['meshFirst'], a['meshCount'], a['pad']
        out.append(a)
    return out


# ---------------------------------------------------------------- AVAT
AVAT_HDR = struct.Struct('<10I')                # genders, items, meshRefs, clipMaps, gendersOff, itemsOff, refsOff, clipMapOff, namesOff, 0
AVAT_GENDER = struct.Struct('<2I6f2H10h')       # skeleton, nameOff, height, radius, walk, run, walkAnim, runAnim, jointR, jointL, starter[10] = 64 (padded)
AVAT_GENDER_BYTES = 64
AVAT_ITEM = struct.Struct('<BBHIHHI')           # gender, slot, itemNumber, firstRef, meshCount, attachJoint, flags = 16
AVAT_CLIPMAP = struct.Struct('<4BHH')           # gender, action, weaponType, 0, clip, 0 = 8
AVF_STARTER = 1
SLOT_FACE, SLOT_HAIR, SLOT_CAP, SLOT_BODY, SLOT_HANDS, SLOT_FEET, SLOT_WEAPON, SLOT_BACK, SLOT_FACEITEM, SLOT_SUBWPN = range(10)
SLOT_CART_BODY, SLOT_CART_ENGINE, SLOT_CART_LEG, SLOT_CART_ARMS = 10, 11, 12, 13           # vehicle parts (rose_carts.py): item number = LIST_PAT row
SLOT_NAMES = ('face', 'hair', 'cap', 'body', 'hands', 'feet', 'weapon', 'back', 'faceitem', 'subweapon', 'cart body', 'cart engine', 'cart leg', 'cart arms')
NO_JOINT = 0xFFFF


def enc_avat(genders, items, clipmap):
    """genders: list of dict(name, skeleton, height, radius, walk_speed, run_speed, walk_anim_speed, run_anim_speed, joint_r, joint_l, starter=[10 item indices or -1]).
    items: list of dict(gender, slot, number, meshes=[CMSH ids], attach=joint|None, starter=bool). clipmap: list of (gender, action, weaponType, clip)."""
    names = _cstr_blob(); gb = bytearray(); ib = bytearray(); rb = bytearray(); cb = bytearray(); nref = 0
    for g in genders:
        st = list(g['starter']) + [-1] * (10 - len(g['starter']))
        rec = AVAT_GENDER.pack(g['skeleton'], names.add(g['name']), g['height'], g['radius'], g['walk_speed'], g['run_speed'],
                               g['walk_anim_speed'], g['run_anim_speed'], g['joint_r'], g['joint_l'], *st[:10])
        gb += rec + b'\0' * (AVAT_GENDER_BYTES - len(rec))
    for it in items:
        ib += AVAT_ITEM.pack(it['gender'], it['slot'], it['number'], nref, len(it['meshes']), NO_JOINT if it.get('attach') is None else it['attach'],
                             AVF_STARTER if it.get('starter') else 0)
        rb += struct.pack('<%dH' % len(it['meshes']), *it['meshes']); nref += len(it['meshes'])
    rb = _pad4(rb)
    for g, a, w, c in clipmap:
        cb += AVAT_CLIPMAP.pack(g, a, w, 0, c, 0)
    go = AVAT_HDR.size; io = go + len(gb); ro = io + len(ib); co = ro + len(rb); no = co + len(cb)
    return AVAT_HDR.pack(len(genders), len(items), nref, len(clipmap), go, io, ro, co, no, 0) + bytes(gb) + bytes(ib) + bytes(rb) + bytes(cb) + names.bytes()


# ---------------------------------------------------------------- FLDM (field item models, the shared avatar pack; docs/RPG1_PACK.md section 23)
FLDM_HDR = struct.Struct('<8I')                 # modelCount, refCount, animFrames, animFps, modelsOff, refsOff, animOff, 0
FLDM_MODEL = struct.Struct('<HHI')              # field model index (the item STB cell 10 = the ZSC object), meshCount, firstRef = 8
FLDM_KEY = struct.Struct('<7f')                 # drop animation key: position x y z (m, pack axes), rotation x y z w


def enc_fldm(models, anim, fps=30):
    """models: list of (index, [CMSH ids]) sorted by index; anim: list of (px, py, pz, qx, qy, qz, qw) keys at `fps` (the client's ITEM_ANI.ZMO, played once from the drop)"""
    models = sorted(models)
    mb = bytearray(); rb = bytearray(); nref = 0
    for idx, ids in models:
        mb += FLDM_MODEL.pack(idx, len(ids), nref)
        rb += struct.pack('<%dH' % len(ids), *ids); nref += len(ids)
    rb = _pad4(rb)
    ab = b''.join(FLDM_KEY.pack(*k) for k in anim)
    mo = FLDM_HDR.size; ro = mo + len(mb); ao = ro + len(rb)
    return FLDM_HDR.pack(len(models), nref, len(anim), fps, mo, ro, ao, 0) + bytes(mb) + bytes(rb) + ab


def dec_fldm(d):
    nm, nr, na, fps, mo, ro, ao, _ = FLDM_HDR.unpack_from(d, 0)
    if mo + nm * FLDM_MODEL.size > len(d) or ro + nr * 2 > len(d) or ao + na * FLDM_KEY.size > len(d):
        raise ValueError('FLDM out of bounds')
    refs = struct.unpack_from('<%dH' % nr, d, ro)
    models = []
    for i in range(nm):
        idx, cnt, first = FLDM_MODEL.unpack_from(d, mo + i * FLDM_MODEL.size)
        if first + cnt > nr:
            raise ValueError('FLDM refs out of bounds')
        models.append((idx, list(refs[first:first + cnt])))
    anim = [FLDM_KEY.unpack_from(d, ao + i * FLDM_KEY.size) for i in range(na)]
    return models, anim, fps


def dec_avat(d):
    ng, ni, nr, nc, go, io, ro, co, no, _ = AVAT_HDR.unpack_from(d, 0)
    names = d[no:]
    genders = []
    for i in range(ng):
        v = AVAT_GENDER.unpack_from(d, go + i * AVAT_GENDER_BYTES)
        genders.append(dict(skeleton=v[0], name=_cstr(names, v[1]), height=v[2], radius=v[3], walk_speed=v[4], run_speed=v[5], walk_anim_speed=v[6],
                            run_anim_speed=v[7], joint_r=v[8], joint_l=v[9], starter=list(v[10:20])))
    refs = struct.unpack_from('<%dH' % nr, d, ro)
    items = []
    for i in range(ni):
        g, slot, num, first, cnt, att, fl = AVAT_ITEM.unpack_from(d, io + i * AVAT_ITEM.size)
        if first + cnt > nr:
            raise ValueError('avatar item %d refs out of bounds' % i)
        items.append(dict(gender=g, slot=slot, number=num, meshes=list(refs[first:first + cnt]), attach=None if att == NO_JOINT else att, starter=bool(fl & AVF_STARTER)))
    clipmap = [AVAT_CLIPMAP.unpack_from(d, co + i * AVAT_CLIPMAP.size) for i in range(nc)]
    return genders, items, [(g, a, w, c) for g, a, w, _z, c, _y in clipmap]


# ================================================================ version 1.2: SPWR NBEH WATR SKYB EVNT OCOL
# ---------------------------------------------------------------- SPWR (spawn rules + scatter slots, one record per SPWN record)
SPWR_HDR = struct.Struct('<4I')                 # count, stride (32), recOff, slotOff
SPWR_REC = struct.Struct('<ffHHHHIHHfI')        # range, respawn, limit, tacticPts, firstWave, flags, firstSlot, slotCount, walkCells, minSep, 0 = 32
SPWR_SQUARE, SPWR_ON_WALKABLE, SPWR_NO_WALKABLE = 1, 2, 4


def enc_spwr(rules):
    """rules: list of dict(range, respawn, limit, tactic_points, first_wave, flags, slots=[(x, z)], walk_cells, min_sep) in SPWN order."""
    recs = bytearray(); slots = bytearray(); n = 0
    for r in rules:
        recs += SPWR_REC.pack(r['range'], r['respawn'], r['limit'], r['tactic_points'], r['first_wave'], r['flags'], n, len(r['slots']),
                              min(65535, r['walk_cells']), r['min_sep'], 0)
        for x, z in r['slots']:
            slots += struct.pack('<2f', x, z)
        n += len(r['slots'])
    ro = SPWR_HDR.size
    return SPWR_HDR.pack(len(rules), SPWR_REC.size, ro, ro + len(recs)) + bytes(recs) + bytes(slots)


def dec_spwr(d):
    n, stride, ro, so = SPWR_HDR.unpack_from(d, 0)
    if stride < SPWR_REC.size or ro + n * stride > len(d):
        raise ValueError('SPWR stride/count out of bounds')
    out = []
    for i in range(n):
        rg, rs, lim, tp, fw, fl, first, cnt, wc, ms, _z = SPWR_REC.unpack_from(d, ro + i * stride)
        if so + (first + cnt) * 8 > len(d):
            raise ValueError('SPWR slots out of bounds')
        out.append(dict(range=rg, respawn=rs, limit=lim, tactic_points=tp, first_wave=fw, flags=fl, walk_cells=wc, min_sep=ms,
                        slots=[struct.unpack_from('<2f', d, so + (first + k) * 8) for k in range(cnt)]))
    return out


# ---------------------------------------------------------------- NBEH (monster / NPC behaviour from the AIP scripts, one record per npc id)
NBEH_HDR = struct.Struct('<4I')                 # count, stride (40), recOff, 0
NBEH_REC = struct.Struct('<4H2BH6fI')           # npcId, aiId, flags, idleIntervalS, newTargetPct, fleeHpPct, 0, aggro, approach, wander, leash, giveup, callAllies, 0 = 40
NB_AGGRESSIVE, NB_APPROACHES, NB_WANDERS, NB_WANDER_RUN, NB_LEASH, NB_FLEES, NB_CALLS_ALLIES, NB_SKILLS, NB_STATIONARY, NB_HAS_AI = (1 << i for i in range(10))


def enc_nbeh(rows):
    recs = b''.join(NBEH_REC.pack(r['npc'], r['ai'], r['flags'], r['idle_interval'], r['new_target_pct'], r['flee_hp_pct'], 0, r['aggro_m'], r['approach_m'],
                                  r['wander_m'], r['leash_m'], r['giveup_m'], r['call_allies_m'], 0) for r in rows)
    return NBEH_HDR.pack(len(rows), NBEH_REC.size, NBEH_HDR.size, 0) + recs


def dec_nbeh(d):
    n, stride, ro, _ = NBEH_HDR.unpack_from(d, 0)
    if stride < NBEH_REC.size or ro + n * stride > len(d):
        raise ValueError('NBEH stride/count out of bounds')
    keys = ('npc', 'ai', 'flags', 'idle_interval', 'new_target_pct', 'flee_hp_pct', '_p', 'aggro_m', 'approach_m', 'wander_m', 'leash_m', 'giveup_m', 'call_allies_m', '_z')
    out = []
    for i in range(n):
        r = dict(zip(keys, NBEH_REC.unpack_from(d, ro + i * stride))); del r['_p'], r['_z']; out.append(r)
    return out


# ---------------------------------------------------------------- WATR (water planes + flip-book frames)
WATR_HDR = struct.Struct('<10IfI')              # planeCount, planeStride (32), planesOff, frameCount, frameW, frameH, frameFormat, frameMips, framesOff, fps, tileMetres, blend = 48
WATR_PLANE = struct.Struct('<5f2hII')           # minX, minZ, maxX, maxZ, y, bx, by, flags, 0 = 32
WATR_BLEND_ALPHA_ADD = 1                        # rgb = tex.rgb * tex.a + framebuffer (what the retail client does)


def enc_watr(planes, frames, fmt, fw, fh, fps, tile_m, blend=WATR_BLEND_ALPHA_ADD):
    """planes: list of dict(x0,z0,x1,z1,y,bx,by,flags); frames: list of [level bytes ...] (all frames the same size and mip count)."""
    recs = b''.join(WATR_PLANE.pack(p['x0'], p['z0'], p['x1'], p['z1'], p['y'], p['bx'], p['by'], p.get('flags', 0), 0) for p in planes)
    mips = len(frames[0]) if frames else 0
    fb = b''.join(b''.join(f) for f in frames)
    po = WATR_HDR.size
    return WATR_HDR.pack(len(planes), WATR_PLANE.size, po, len(frames), fw, fh, fmt, mips, po + len(recs), fps, tile_m, blend) + recs + fb


def dec_watr(d):
    from rosedds import level_size
    (n, stride, po, nf, fw, fh, fmt, mips, fo, fps, tile, blend) = WATR_HDR.unpack_from(d, 0)
    if stride < WATR_PLANE.size or po + n * stride > len(d):
        raise ValueError('WATR planes out of bounds')
    planes = []
    for i in range(n):
        x0, z0, x1, z1, y, bx, by, fl, _z = WATR_PLANE.unpack_from(d, po + i * stride)
        planes.append(dict(x0=x0, z0=z0, x1=x1, z1=z1, y=y, bx=bx, by=by, flags=fl))
    frames = []; pos = fo
    for _ in range(nf):
        lv = []; lw, lh = fw, fh
        for _m in range(mips):
            sz = level_size(fmt, lw, lh)
            if pos + sz > len(d):
                raise ValueError('WATR frames truncated')
            lv.append(bytes(d[pos:pos + sz])); pos += sz
            lw = max(1, lw // 2); lh = max(1, lh // 2)
        frames.append(lv)
    return dict(planes=planes, frames=frames, fmt=fmt, w=fw, h=fh, fps=fps, tile=tile, blend=blend, mips=mips)


# ---------------------------------------------------------------- SKYB (sky dome, fog, light, time of day)
STATE_NAMES = ('morning', 'day', 'evening', 'night')
SK_DOME, SK_NIGHT_TEX, SK_FOG, SK_LIGHT = 1, 2, 4, 8
_SKYB_FIXED = struct.Struct('<7If4ffI12f')       # skyId, flags, cycle, morning, day, evening, night, tickSeconds, fog x4, domeScale, blend, lightDir, ambient, diffuse, specular = 104 bytes
_SKYB_PAL = struct.Struct('<' + '3f' * 20)             # 5 palettes x 4 states


def enc_skyb(sky):
    """sky: dict(sky_id, flags, cycle, morning, day, evening, night, tick_s, fog=(near, far, a_near, a_far), dome_scale, blend, light_dir, light_ambient, light_diffuse,
    light_specular, pal_bg[4], pal_ambient[4], pal_char_ambient[4], pal_char_diffuse[4], pal_fog_alt[4], fog_density_alt[4], dome_verts [(x,y,z,u,v)], dome_idx [i], textures [dict]).
    Layout: fixed block (_SKYB_FIXED) + palettes (5 x 4 x rgb) + 4 fog densities + dome (u32 vc, u32 ic, verts 20 B, u16 idx padded to 4) + OTEX-style texture table (day, night)."""
    out = bytearray()
    out += _SKYB_FIXED.pack(sky['sky_id'], sky['flags'], sky['cycle'], sky['morning'], sky['day'], sky['evening'], sky['night'], sky['tick_s'],
                            *sky['fog'], sky['dome_scale'], sky['blend'], *sky['light_dir'], *sky['light_ambient'], *sky['light_diffuse'], *sky['light_specular'])
    flat = []
    for name in ('pal_bg', 'pal_ambient', 'pal_char_ambient', 'pal_char_diffuse', 'pal_fog_alt'):
        for c in sky[name]:
            flat += list(c)
    out += _SKYB_PAL.pack(*flat)
    out += struct.pack('<4f', *sky['fog_density_alt'])
    dv = sky.get('dome_verts') or []; di = sky.get('dome_idx') or []
    out += struct.pack('<2I', len(dv), len(di))
    for v in dv:
        out += struct.pack('<5f', *v)
    ib = struct.pack('<%dH' % len(di), *di)
    out += _pad4(ib)
    out += enc_otex(sky.get('textures') or [])
    return bytes(out)


def dec_skyb(d):
    f = _SKYB_FIXED.unpack_from(d, 0)
    pos = _SKYB_FIXED.size
    sky = dict(sky_id=f[0], flags=f[1], cycle=f[2], morning=f[3], day=f[4], evening=f[5], night=f[6], tick_s=f[7], fog=tuple(f[8:12]), dome_scale=f[12], blend=f[13],
               light_dir=tuple(f[14:17]), light_ambient=tuple(f[17:20]), light_diffuse=tuple(f[20:23]), light_specular=tuple(f[23:26]))
    pal = _SKYB_PAL.unpack_from(d, pos); pos += _SKYB_PAL.size
    for k, name in enumerate(('pal_bg', 'pal_ambient', 'pal_char_ambient', 'pal_char_diffuse', 'pal_fog_alt')):
        sky[name] = [tuple(pal[(k * 4 + s) * 3:(k * 4 + s) * 3 + 3]) for s in range(4)]
    sky['fog_density_alt'] = struct.unpack_from('<4f', d, pos); pos += 16
    vc, ic = struct.unpack_from('<2I', d, pos); pos += 8
    if pos + vc * 20 + ic * 2 > len(d):
        raise ValueError('SKYB dome out of bounds')
    sky['dome_verts'] = [struct.unpack_from('<5f', d, pos + i * 20) for i in range(vc)]; pos += vc * 20
    sky['dome_idx'] = list(struct.unpack_from('<%dH' % ic, d, pos)); pos += ic * 2 + (-ic * 2 % 4)
    sky['textures'] = dec_otex(d[pos:])
    return sky


# ---------------------------------------------------------------- EVNT (zone event positions: start, revive, warps)
EVNT_HDR = struct.Struct('<4I')                 # count, stride (24), recOff, namesOff
EVNT_REC = struct.Struct('<IBBH3fI')            # nameOff, kind, inWindow, 0, x, y, z, 0 = 24
EV_START, EV_RESTORE, EV_WARP, EV_OTHER = 0, 1, 2, 3


def enc_evnt(events):
    """events: list of dict(name, kind, inside, x, y, z) (local metres)."""
    st = _cstr_blob(); recs = bytearray()
    for e in events:
        recs += EVNT_REC.pack(st.add(e['name']), e['kind'], 1 if e['inside'] else 0, 0, e['x'], e['y'], e['z'], 0)
    ro = EVNT_HDR.size
    return EVNT_HDR.pack(len(events), EVNT_REC.size, ro, ro + len(recs)) + bytes(recs) + st.bytes()


def dec_evnt(d):
    n, stride, ro, no = EVNT_HDR.unpack_from(d, 0)
    if stride < EVNT_REC.size or ro + n * stride > len(d) or no > len(d):
        raise ValueError('EVNT out of bounds')
    out = []
    for i in range(n):
        nm, kind, inside, _p, x, y, z, _z = EVNT_REC.unpack_from(d, ro + i * stride)
        out.append(dict(name=_cstr(d[no:], nm), kind=kind, inside=bool(inside), x=x, y=y, z=z))
    return out


# ---------------------------------------------------------------- WARP (zone exits: warp gates + NPC / quest teleports), added 2026-11-13
# Not yet in any minor's tag list (stamped into 1.6 at merge); readers treat a missing WARP as "this zone has no exits".
TAGS_WARP = (b'WARP',)
WARP_HDR = struct.Struct('<4I')                 # count, stride (64), recOff, namesOff
WARP_REC = struct.Struct('<HHBBH3f3ffffIIHH8x')  # warpId, targetZone, kind, inWindow, flags, cx, cy, cz, hx, hy, hz, yawDeg, targetAbsX, targetAbsZ, targetEventOff, nameOff, npcId, 0, pad = 64
WK_GATE, WK_NPC = 0, 1                          # IFO type 10 warp object (WARP.STB row) / NPC teleport (QSD reward "teleport"; talk = stand next to the NPC)
WF_TARGET_ABS = 1                               # targetAbsX / Z valid (Unity absolute metres of the target zone; local = abs - AREA.world_off)
WF_DWELL = 2                                    # trigger only after standing inside for a moment (NPC teleports: no accidental trips)
WF_TARGET_KNOWN = 4                             # the target zone row exists in LIST_ZONE


def enc_warp(warps):
    """warps: list of dict(warp_id, target_zone, kind, inside, flags, cx, cy, cz, hx, hy, hz, yaw, tx, tz, target_event, name, npc) (local metres, yaw in degrees)."""
    st = _cstr_blob(); recs = bytearray()
    for w in warps:
        recs += WARP_REC.pack(w['warp_id'], w['target_zone'], w['kind'], 1 if w['inside'] else 0, w['flags'], w['cx'], w['cy'], w['cz'], w['hx'], w['hy'], w['hz'], w['yaw'],
                              w['tx'], w['tz'], st.add(w['target_event']), st.add(w['name']), w['npc'], 0)
    ro = WARP_HDR.size
    return WARP_HDR.pack(len(warps), WARP_REC.size, ro, ro + len(recs)) + bytes(recs) + st.bytes()


def dec_warp(d):
    if len(d) < WARP_HDR.size:
        raise ValueError('WARP too short')
    n, stride, ro, no = WARP_HDR.unpack_from(d, 0)
    if stride < WARP_REC.size or ro + n * stride > len(d) or no > len(d):
        raise ValueError('WARP out of bounds')
    out = []
    for i in range(n):
        (wid, tzone, kind, inside, flags, cx, cy, cz, hx, hy, hz, yaw, tx, tz, teo, nmo, npc, _z) = WARP_REC.unpack_from(d, ro + i * stride)
        out.append(dict(warp_id=wid, target_zone=tzone, kind=kind, inside=bool(inside), flags=flags, cx=cx, cy=cy, cz=cz, hx=hx, hy=hy, hz=hz, yaw=yaw, tx=tx, tz=tz,
                        target_event=_cstr(d[no:], teo), name=_cstr(d[no:], nmo), npc=npc))
    return out


# ---------------------------------------------------------------- LOBY (character select scene: slot positions + camera path), added 2026-11-13
# Not yet in any minor's tag list (stamped at merge, like WARP); readers treat a missing LOBY as "no character select data" (the world keeps its shell layout).
TAGS_LOBY = (b'LOBY',)
LOBY_HDR = struct.Struct('<6IfI')               # posCount, posStride (32), posOff, camCount, camStride (36), camOff, camFps, flags            = 32
LOBY_POS = struct.Struct('<5f12x')              # x, y, z (local metres), yawDeg (Unity, 0 = +Z; faces the camera), scale, pad               = 32
LOBY_CAM = struct.Struct('<9f')                 # eye xyz, target xyz (local metres), up xyz (unit direction)                                    = 36
LF_POS_EXACT = 1                                # positions came from the owner's positions file (the original slots)
LF_POS_DERIVED = 2                              # derived positions (an arc in front of the camera's last target): no positions file was given
LF_CAMERA = 4                                   # the camera path is present (camCount >= 1)
LOBY_MAX_POS = 16
LOBY_MAX_CAM = 4096


def enc_loby(positions, cam, cam_fps, flags):
    """positions: list of dict(x, y, z, yaw, scale); cam: list of (eye, target, up) 3-tuples (local metres); flags LF_*."""
    if len(positions) > LOBY_MAX_POS or len(cam) > LOBY_MAX_CAM:
        raise ValueError('LOBY too many records')
    po = LOBY_HDR.size
    co = po + len(positions) * LOBY_POS.size
    out = bytearray(LOBY_HDR.pack(len(positions), LOBY_POS.size, po, len(cam), LOBY_CAM.size, co, float(cam_fps), flags))
    for p in positions:
        out += LOBY_POS.pack(p['x'], p['y'], p['z'], p['yaw'], p['scale'])
    for e, t, u in cam:
        out += LOBY_CAM.pack(e[0], e[1], e[2], t[0], t[1], t[2], u[0], u[1], u[2])
    return bytes(out)


def dec_loby(d):
    if len(d) < LOBY_HDR.size:
        raise ValueError('LOBY too short')
    n, ps, po, nc, cs, co, fps, flags = LOBY_HDR.unpack_from(d, 0)
    if n > LOBY_MAX_POS or nc > LOBY_MAX_CAM or ps < LOBY_POS.size or cs < LOBY_CAM.size or po < LOBY_HDR.size or co < LOBY_HDR.size \
            or po + n * ps > len(d) or co + nc * cs > len(d):
        raise ValueError('LOBY out of bounds')
    pos = []
    for i in range(n):
        x, y, z, yaw, sc = LOBY_POS.unpack_from(d, po + i * ps)
        pos.append(dict(x=x, y=y, z=z, yaw=yaw, scale=sc))
    cam = []
    for i in range(nc):
        v = LOBY_CAM.unpack_from(d, co + i * cs)
        cam.append((tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9])))
    return dict(positions=pos, cam=cam, fps=fps, flags=flags)


# ---------------------------------------------------------------- OCOL (baked object lightmap -> vertex colours, parallel to OMSH vertices)
OCOL_HDR = struct.Struct('<4I')                 # chunkCount (== OMSH count), stride (4), recOff, dataOff
OCOL_REC = struct.Struct('<2I')                 # byte offset into the data blob (0xFFFFFFFF = chunk has no lightmap: white), vertCount
OCOL_NONE = 0xFFFFFFFF


def enc_ocol(chunk_colors):
    """chunk_colors: per OMSH chunk either None (all white) or a bytes-like of vertCount * 4 (r, g, b, a = 255)."""
    recs = bytearray(); data = bytearray()
    for c in chunk_colors:
        if c is None:
            recs += OCOL_REC.pack(OCOL_NONE, 0)
        else:
            recs += OCOL_REC.pack(len(data), len(c) // 4); data += c
    ro = OCOL_HDR.size
    return OCOL_HDR.pack(len(chunk_colors), 4, ro, ro + len(recs)) + bytes(recs) + bytes(data)


def dec_ocol(d):
    n, stride, ro, do = OCOL_HDR.unpack_from(d, 0)
    if stride != 4 or ro + n * OCOL_REC.size > len(d):
        raise ValueError('OCOL header out of bounds')
    out = []
    for i in range(n):
        off, vc = OCOL_REC.unpack_from(d, ro + i * OCOL_REC.size)
        if off == OCOL_NONE:
            out.append(None)
        else:
            if do + off + vc * 4 > len(d):
                raise ValueError('OCOL chunk %d out of bounds' % i)
            out.append(bytes(d[do + off:do + off + vc * 4]))
    return out


# ---------------------------------------------------------------- OCLD (physics colliders of the object collision parts; appended 2026-11-14, additive, no version stamp)
# A reader without OCLD behaves as before (objects without colliders). Layout: docs/RPG1_PACK.md section 21.
OCLD_HDR = struct.Struct('<8I')                 # pieceCount, pieceStride (16), piecesOff, rangeCount, rangeStride (8), rangesOff, maxTris, totalTris = 32
OCLD_PIECE = struct.Struct('<2I2HI')            # chunk (OMSH index), firstRange, rangeCount, flags (1 floor, 2 wall), tris = 16
OCLD_RANGE = struct.Struct('<2I')               # firstIndex, indexCount into the chunk's OMSH index list (both multiples of 3) = 8


def enc_ocld(pieces, ranges, max_tris):
    """pieces: [(chunk, firstRange, rangeCount, flags, tris)] sorted by chunk; ranges: [(firstIndex, indexCount)]."""
    po = OCLD_HDR.size; ro = po + len(pieces) * OCLD_PIECE.size
    out = bytearray(OCLD_HDR.pack(len(pieces), OCLD_PIECE.size, po, len(ranges), OCLD_RANGE.size, ro, max_tris, sum(p[4] for p in pieces)))
    for p in pieces:
        out += OCLD_PIECE.pack(*p)
    for r in ranges:
        out += OCLD_RANGE.pack(*r)
    return bytes(out)


def dec_ocld(d):
    if len(d) < OCLD_HDR.size:
        raise ValueError('OCLD too short')
    n, ps, po, nr, rs, ro, max_tris, total = OCLD_HDR.unpack_from(d, 0)
    if ps < OCLD_PIECE.size or rs < OCLD_RANGE.size or po + n * ps > len(d) or ro + nr * rs > len(d):
        raise ValueError('OCLD out of bounds')
    pieces = [OCLD_PIECE.unpack_from(d, po + i * ps) for i in range(n)]
    ranges = [OCLD_RANGE.unpack_from(d, ro + i * rs) for i in range(nr)]
    return dict(pieces=pieces, ranges=ranges, max_tris=max_tris, total_tris=total)
