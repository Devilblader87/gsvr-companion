"""Zone ambient effects (lane w20 OPTICS): the world objects the original client animates / lights and the converter used to skip.

The IFO of every zone block has two lists that rose_pack counted as "skipped":
  type 6  animated "morph" objects (LIST_MORPH_OBJECT.STB row = id): sea waves, shore foam, pool ripples - a small ZMS mesh with a ZMO vertex animation (position, uv, alpha),
          its own texture and blend state, looped for ever (Birth Island 304, Luxem Tower 336, Dolphin Island 384, Kenji Beach 235, Breezy Hills 438, Junon 86 instances)
  type 5  effect objects: an EFT effect file placed in the world (fires, fireflies, ...; 68 instances, 5 distinct files in the retail client)
  ZSC objects with effects: deco / cnst (IFO type 1 / 3) objects whose ZSC object lists an effect (torches, braziers, street lights, house window lights, the Goblin Cave fires,
          Eldeon fires, ...): the EFT of every normal (type 0) and day / night (type 1 = shown at night) effect entry, placed by object x parent part x entry transform
          (Goblin Cave B3 125 + 28, Junon 33 street lights, the clan field 360 fires, Eldeon 26)
They are drawn by the runtime's effect player (GsvrRoseFx, section EFCT of the AVATAR pack): rose_effects gives every morph row used by a zone and every effect object file a
file id of its own (AMB_MORPH_BASE + row, FILE_EFFECT row / AMB_PATH_BASE + crc), and this module writes the zone's instance list, section ZFXO (version 1).

ZFXO layout (little endian):
  header 16: u32 count ; u32 stride (36) ; u32 recOff (16) ; u32 0
  record 36: u16 fileId (EFCT file id) ; u16 kind (1 morph mesh, 2 effect object / object effect, 3 object effect shown at night only) ; f32 pos[3] (zone local metres, pack axes x east, y up, z north) ;
             f32 rot[4] xyzw = the anchor rotation (effect frame -> zone local axes, see anchor_rot) ; f32 scale (uniform)

Frames: the pack's local axes are S(x, y, z) = (x, z, y) of the file axes (a reflection, rose_pack add_instance); the effect frame of EFCT is (-x, z, -y) = Ry180 . S. A world
object placed by the IFO rotation R_obj (file axes) therefore needs the anchor rotation  S R_obj S^-1 . Ry180  - the proper rotation with quaternion (-qx, -qz, -qy, qw) . (0, 1, 0, 0)
(a reflection maps a rotation about axis a to the rotation about -S a). Verified numerically in companion/test_rose_conv.py (class Ambient).
Pure stdlib; no game data here.
"""
import math
import struct
import zlib

TAG_ZFXO = b'ZFXO'
HDR = struct.Struct('<4I')
REC = struct.Struct('<HH3f4ff')
assert HDR.size == 16 and REC.size == 36
KIND_MORPH, KIND_EFFECT, KIND_NIGHT = 1, 2, 3
ZSC_EFFECT_NORMAL, ZSC_EFFECT_DAYNIGHT = 0, 1      # ZSC effect entry types (rose-offline ZscEffectType; 2 = a light container, no EFT)
AMB_MORPH_BASE = 40000          # EFCT file id of LIST_MORPH_OBJECT row r = 40000 + r (rows < 1000)
AMB_PATH_BASE = 41000           # effect object paths without a FILE_EFFECT row: 41000 + crc32(path) % 20000
AMB_PATH_SPAN = 20000
MAX_RECORDS = 4096
MAX_MORPH_ROW = 999
AMB_MAX_VERTS = 128            # a morph mesh with more vertices is left out (retail: waves 6..108; rows 36 / 37 = 288 / 1386 vertices of the Junon arena pools would take 5 / 22 vertex blocks each)
CENTER_OFF = 5200.0             # rose_pack.CENTER_OFF
F_LIMIT = 1.0e6


def norm_path(p):
    return str(p).replace('\\', '/').replace('//', '/').upper().strip()


def morph_file_id(row):
    return AMB_MORPH_BASE + row if 0 < row <= MAX_MORPH_ROW else 0


def path_file_id(fe_ids, path):
    """effect object path -> EFCT file id: its FILE_EFFECT row (fe_ids: normalised path -> row) else a stable id from the path (so the zone pack and the avatar pack agree
    without sharing a table)."""
    k = norm_path(path)
    if k in fe_ids:
        return fe_ids[k]
    return AMB_PATH_BASE + zlib.crc32(k.encode('utf-8', 'replace')) % AMB_PATH_SPAN


def fin(x, lim=F_LIMIT):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x != x or x in (float('inf'), float('-inf')):
        return 0.0
    return max(-lim, min(lim, x))


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by + ay * bw + az * bx - ax * bz,
            aw * bz + az * bw + ax * by - ay * bx, aw * bw - ax * bx - ay * by - az * bz)


def anchor_rot(q):
    """IFO quaternion (x, y, z, w, file axes) -> the anchor rotation (xyzw) of an effect placed there, in the pack's local axes (see the module docstring)"""
    n = math.sqrt(sum(float(c) * float(c) for c in q)) or 1.0
    x, y, z, w = (float(c) / n for c in q)
    r = qmul((-x, -z, -y, w), (0.0, 1.0, 0.0, 0.0))
    return tuple(fin(c, 1.0) for c in r)


def local_pos(pos_cm, win):
    """IFO position (x east, y north, height; centimetres) -> local metres (rose_pack LocalWindow.to_local)"""
    return win.to_local(pos_cm[0], pos_cm[1], pos_cm[2])


def scale_of(s):
    try:
        v = (abs(float(s[0])) + abs(float(s[1])) + abs(float(s[2]))) / 3.0
    except (TypeError, ValueError, IndexError):
        return 1.0
    return fin(v, 100.0) if v > 1e-3 else 1.0


def _qm(rot):
    """ZSC quaternion (w, x, y, z) -> 3x3 row-major tuple (rose_pack.quat_wxyz_to_mat)"""
    w, x, y, z = (float(c) for c in rot)
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    w, x, y, z = w / n, x / n, y / n, z / n
    return (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y),
            2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x),
            2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y))


def _mm(a, b):
    return tuple(sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3))


def _mv(a, v):
    return (a[0] * v[0] + a[1] * v[1] + a[2] * v[2], a[3] * v[0] + a[4] * v[1] + a[5] * v[2], a[6] * v[0] + a[7] * v[1] + a[8] * v[2])


def _scol(m, s):
    return (m[0] * s[0], m[1] * s[1], m[2] * s[2], m[3] * s[0], m[4] * s[1], m[5] * s[2], m[6] * s[0], m[7] * s[1], m[8] * s[2])


def mat_quat(m):
    """rotation part of a 3x3 (columns normalised first) -> quaternion (x, y, z, w)"""
    cols = []
    for c in range(3):
        n = math.sqrt(m[c] ** 2 + m[3 + c] ** 2 + m[6 + c] ** 2) or 1.0
        cols.append((m[c] / n, m[3 + c] / n, m[6 + c] / n))
    r = (cols[0][0], cols[1][0], cols[2][0], cols[0][1], cols[1][1], cols[2][1], cols[0][2], cols[1][2], cols[2][2])
    t = r[0] + r[4] + r[8]
    if t > 0:
        k = math.sqrt(t + 1.0) * 2
        return ((r[7] - r[5]) / k, (r[2] - r[6]) / k, (r[3] - r[1]) / k, 0.25 * k)
    if r[0] > r[4] and r[0] > r[8]:
        k = math.sqrt(1.0 + r[0] - r[4] - r[8]) * 2
        return (0.25 * k, (r[1] + r[3]) / k, (r[2] + r[6]) / k, (r[7] - r[5]) / k)
    if r[4] > r[8]:
        k = math.sqrt(1.0 + r[4] - r[0] - r[8]) * 2
        return ((r[1] + r[3]) / k, 0.25 * k, (r[5] + r[7]) / k, (r[2] - r[6]) / k)
    k = math.sqrt(1.0 + r[8] - r[0] - r[4]) * 2
    return ((r[2] + r[6]) / k, (r[5] + r[7]) / k, 0.25 * k, (r[3] - r[1]) / k)


def object_effects(zsc, oid):
    """the effect entries of ZSC object `oid` -> [(path, type, 3x3 with scale, position cm)] in the object's space: parent part chain x the entry's own transform (like
    rose_pack object_parts). Only EFT files of type 0 (normal) / 1 (day / night); light containers (type 2) are no effects."""
    try:
        parts, effs = zsc['objs'][oid]
    except (IndexError, KeyError, TypeError):
        return []
    local = []
    for _mi, _ma, pr in parts:
        rot = pr.get('rot', (1, 0, 0, 0))
        m = _qm(rot) if any(rot) else (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        local.append((_scol(m, pr.get('scale', (1, 1, 1))), tuple(pr.get('pos', (0, 0, 0))), pr.get('parent', 0) - 1))
    out = []
    for eid, et, pr in effs:
        if et not in (ZSC_EFFECT_NORMAL, ZSC_EFFECT_DAYNIGHT) or eid >= len(zsc.get('effs', ())):
            continue
        path = str(zsc['effs'][eid])
        if not path.upper().endswith('.EFT'):
            continue
        rot = pr.get('rot', (1, 0, 0, 0))
        m = _scol(_qm(rot) if any(rot) else (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), pr.get('scale', (1, 1, 1)))
        t = tuple(pr.get('pos', (0, 0, 0)))
        p = pr.get('parent', 0) - 1
        guard = 0
        while 0 <= p < len(local) and guard < 16:
            pm, pt, pp = local[p]
            t = tuple(a + b for a, b in zip(_mv(pm, t), pt))
            m = _mm(pm, m)
            p = pp
            guard += 1
        out.append((path, et, m, t))
    return out


def zone_records(ifos, win, morph_rows, fe_ids, zscs=()):
    """the zone's ZFXO records: the morph objects + effect objects of the IFO blocks (zone_records_base) and, with zscs = ((IFO type, parsed ZSC), ...), the effects of the placed
    deco / cnst objects (object_effects)"""
    out = zone_records_base(ifos, win, morph_rows, fe_ids)
    if zscs and len(out) < MAX_RECORDS:
        cache = {}
        for _bxy, o in sorted(ifos.items()):
            for t, zsc in zscs:
                if not zsc:
                    continue
                for ob in o.get(t, []) or []:
                    key = (id(zsc), ob['id'])
                    if key not in cache:
                        cache[key] = object_effects(zsc, ob['id'])
                    for path, et, m, tl in cache[key]:
                        ro = _qm((ob['q'][3], ob['q'][0], ob['q'][1], ob['q'][2]))          # IFO quaternion is x, y, z, w
                        a = _mm(_scol(ro, ob['scale']), m)
                        tl2 = _mv(_scol(ro, ob['scale']), tl)
                        pos = (ob['pos'][0] + tl2[0], ob['pos'][1] + tl2[1], ob['pos'][2] + tl2[2])
                        x, y, z = local_pos(pos, win)
                        if not (-1.0 <= x <= win.size_x + 1.0 and -1.0 <= z <= win.size_z + 1.0):
                            continue
                        sc = scale_of((math.sqrt(a[0] ** 2 + a[3] ** 2 + a[6] ** 2), math.sqrt(a[1] ** 2 + a[4] ** 2 + a[7] ** 2), math.sqrt(a[2] ** 2 + a[5] ** 2 + a[8] ** 2)))
                        out.append(dict(file=path_file_id(fe_ids, path), kind=KIND_NIGHT if et == ZSC_EFFECT_DAYNIGHT else KIND_EFFECT, pos=(fin(x), fin(y), fin(z)),
                                        rot=anchor_rot(mat_quat(a)), scale=sc))
                        if len(out) >= MAX_RECORDS:
                            return out
    return out


def zone_records_base(ifos, win, morph_rows, fe_ids):
    """IFO blocks (rosefmt.ifo per block) -> list of dict(file, kind, pos, rot, scale). morph_rows = LIST_MORPH_OBJECT rows with a mesh (set); fe_ids = FILE_EFFECT path -> row.
    Objects outside the window (a partial zone) and morph ids without a row are dropped."""
    out = []
    for _bxy, o in sorted(ifos.items()):
        for t, kind in ((6, KIND_MORPH), (5, KIND_EFFECT)):
            for ob in o.get(t, []) or []:
                if kind == KIND_MORPH:
                    fid = morph_file_id(ob['id']) if ob['id'] in morph_rows else 0
                else:
                    fid = path_file_id(fe_ids, ob.get('path', '')) if ob.get('path') else 0
                if not fid:
                    continue
                x, y, z = local_pos(ob['pos'], win)
                if not (-1.0 <= x <= win.size_x + 1.0 and -1.0 <= z <= win.size_z + 1.0):
                    continue
                out.append(dict(file=fid, kind=kind, pos=(fin(x), fin(y), fin(z)), rot=anchor_rot(ob['q']), scale=scale_of(ob['scale'])))
                if len(out) >= MAX_RECORDS:
                    return out
    return out


def enc_zfxo(recs):
    recs = list(recs)[:MAX_RECORDS]
    body = b''.join(REC.pack(int(r['file']) & 0xFFFF, int(r['kind']), *[fin(c) for c in r['pos']], *[fin(c, 1.0) for c in r['rot']], fin(r['scale'], 100.0)) for r in recs)
    return HDR.pack(len(recs), REC.size, HDR.size, 0) + body


def dec_zfxo(d):
    n, stride, ro, _z = HDR.unpack_from(d, 0)
    if stride != REC.size or ro < HDR.size or n > MAX_RECORDS or ro + n * stride > len(d):
        raise ValueError('ZFXO out of bounds')
    out = []
    for i in range(n):
        f = REC.unpack_from(d, ro + i * stride)
        out.append(dict(file=f[0], kind=f[1], pos=f[2:5], rot=f[5:9], scale=f[9]))
    return out


# ------------------------------------------------------------------ avatar side: which effects the zones need
def scan_ifos(vfs, read_ifo, maps_prefix='3DDATA/MAPS/'):
    """every zone IFO of the client -> (morph rows used (set), effect object paths (sorted list)). A file that does not parse is skipped. vfs.files = the file index."""
    rows, paths = set(), set()
    for k in sorted(getattr(vfs, 'files', {}) or {}):
        if not (k.endswith('.IFO') and k.startswith(maps_prefix)):
            continue
        try:
            o = read_ifo(vfs.read(k))
        except Exception:
            continue
        for ob in o.get(6, []) or []:
            rows.add(ob['id'])
        for ob in o.get(5, []) or []:
            if ob.get('path'):
                paths.add(norm_path(ob['path']))
    return rows, sorted(paths)


def morph_eft(cells):
    """LIST_MORPH_OBJECT row cells (name, ZMS, ZMO, DDS, alpha, two sided, alpha test, z test, z write, src, dst, op) -> an EFT-like dict with ONE mesh part that loops for
    ever (repeat 0). The part is drawn two sided: the original's morph meshes can be mirrored by placement and the effect frame is a reflection of the file frame, so the
    one-sided culling of a single flat wave quad would flip with it. None when the row has no mesh."""
    def c(i):
        try:
            return str(cells[i]).strip()
        except IndexError:
            return ''
    def n(i, d=0):
        v = c(i)
        return int(v) if v.lstrip('-').isdigit() else d
    if not c(1) or not c(3):
        return None
    anim = c(2)
    return dict(particles=[], meshes=[dict(mesh=norm_path(c(1)), anim=norm_path(anim) if anim else '', tex=norm_path(c(3)), src=n(9, 5), dst=n(10, 2), op=n(11, 1),
                                           alpha=n(4, 1), two=1, atest=n(6), ztest=n(7, 1), zwrite=n(8), pos=(0.0, 0.0, 0.0), pitch=0.0, yaw=0.0, roll=0.0,
                                           delay=0, repeat=0, linked=0, tanim=None, trep=0)])


def zone_ambient(vfs, ifos, win, zscs=()):
    """the zone's ZFXO records from the client tables (LIST_MORPH_OBJECT, FILE_EFFECT); a client without LIST_MORPH_OBJECT only has the effect objects. KeyError / ValueError
    when a table is there but unreadable (rose_pack warns, the zone builds without)."""
    import rosefmt as F
    morph_rows = set()
    try:
        m = F.stb(vfs.read('3DDATA/STB/LIST_MORPH_OBJECT.STB'))
        for r in range(1, min(m['rows'], MAX_MORPH_ROW + 1)):
            c = m['cells'][r]
            if len(c) > 3 and str(c[1]).strip() and str(c[3]).strip() and morph_mesh_ok(vfs.read, c):
                morph_rows.add(r)
    except KeyError:
        pass
    fe_ids = {}
    try:
        fe = F.stb(vfs.read('3DDATA/STB/FILE_EFFECT.STB'))
        for r in range(fe['rows']):
            c = fe['cells'][r]
            if len(c) > 1 and str(c[1]).strip():
                fe_ids[norm_path(c[1])] = r
    except KeyError:
        pass
    return zone_records(ifos, win, morph_rows, fe_ids, zscs)


def morph_mesh_ok(read, cells):
    """a LIST_MORPH_OBJECT row whose ZMS reads and has at most AMB_MAX_VERTS vertices (the zone side and the avatar side use the same rule, so ZFXO never names a missing effect)"""
    import rosefmt as F
    try:
        z = F.zms(read(norm_path(cells[1])))
    except Exception:
        return False
    n = len(z.get('pos') or [])
    return 0 < n <= AMB_MAX_VERTS


def scan_zsc(vfs, read_zsc):
    """every deco / cnst ZSC of the client (3DDATA/**/LIST_DECO*.ZSC, LIST_CNST*.ZSC) -> the sorted normalised EFT paths of their normal / day-night effect entries (the avatar
    EFCT must hold them: the zones place them next to the IFO effect objects)"""
    paths = set()
    for k in sorted(getattr(vfs, 'files', {}) or {}):
        base = k.rsplit('/', 1)[-1]
        if not (k.startswith('3DDATA/') and k.endswith('.ZSC') and (base.startswith('LIST_DECO') or base.startswith('LIST_CNST'))):
            continue
        try:
            z = read_zsc(vfs.read(k))
        except Exception:
            continue
        for oid in range(len(z.get('objs', ()))):
            for path, _et, _m, _t in object_effects(z, oid):
                paths.add(norm_path(path))
    return sorted(paths)
