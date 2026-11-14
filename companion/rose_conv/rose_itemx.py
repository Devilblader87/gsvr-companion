"""Item extras for the Unity runtime (docs/RPG1_PACK.md section 15): section ITEX, optional and additive (no pack version bump).

The runtime (GsvrRoseItemX, items complete lane) needs more than ITEM holds: item grade bonuses (refine +1..+9), the quality colour class of an item (rare type), the ability
values of gems (LIST_JEMITEM, item type 11 is not in ITEM), names + descriptions of ALL item types (tooltips; ITEM only names the equipment / consumables) and the client's own
words for the tooltip lines (item class names, ability names, the labels of the tooltip). Texts live only inside the pack, nothing is committed.

Source tables (read only, from the player's own client; column numbers = rosefmt.stb cell indices, the same numbers rose-offline's item_database.rs uses):
  LIST_GRADE: row g (1..9) cells 0..4 = attack, hit, defence, resistance, avoid bonus of grade g, cell 5 = glow colour (rrrgggbbb decimal) ;
  item tables LIST_FACEITEM .. LIST_USEITEM, LIST_JEMITEM, LIST_NATURAL, LIST_QUESTITEM (types 1..13): cell 4 class, 7 weight, 9 icon, 30 rare type (equipment), the last cell = string id
  into LIST_*_S.STL (language 1: [name, description]); LIST_JEMITEM cells 16..19 = two (ability type, value) pairs of a gem ;
  STR_ABILITY.STL (key = ability type number), STR_ITEMTYPE.STL (key = item class number), LIST_CLASS_S.STL (job class names, key = the LIST_CLASS row's last cell),
  LIST_STRING.STL (the tooltip labels, keys 106 107 110 125 170 171 430 433 434 435 436 437).
The refine success table is NOT in the client (the server decides; a newer client's LIST_UPGRADE.STB is not part of this one): nothing about it is written here.

Layout (little endian, 4-byte aligned):
  header 80: u32 version (1) ; u32 recCount ; u32 recStride (32) ; u32 recOff ; u32 textOff ; u32 textBytes ; u32 gradeOff ; u32 gradeCount ; u32 labelOff ; u32 labelCount ;
             u32 abilOff ; u32 abilCount ; u32 clsOff ; u32 clsCount ; u32 jobOff ; u32 jobCount ; u32 0 x4
  record 32 at recOff + i * stride, sorted by code (binary search): u32 code ; u16 rare ; u16 class ; u16 icon ; u16 weight ; u32 nameOff ; u16 nameLen ; u16 descLen ; u32 descOff ;
             u16 add0Type ; i16 add0Value ; u16 add1Type ; i16 add1Value   (the gem abilities; 0 for everything but type 11)
  grade 16 at gradeOff + g * 16 (g = 0 .. gradeCount - 1, row 0 is all zero): i16 attack, hit, defence, resistance, avoid ; u16 0 ; u32 glow RGB (0xRRGGBB)
  label entry 8 (index = LABELS order): u32 off ; u16 len ; u16 0         ability entry 8 (index = ability type number 0 .. abilCount - 1, len 0 = none)
  class entry 12 (sorted by id): u32 id ; u32 off ; u16 len ; u16 0         job class entry 12 (id = LIST_CLASS row, sorted)
  texts: UTF-8 blob at textOff (offsets are relative to it).
No text or table of the game is committed; everything is read at conversion time on the player's machine.
"""
import struct

import rosefmt as F

TAG_ITEX = b'ITEX'
HDR = struct.Struct('<20I')
REC = struct.Struct('<IHHHHIHHIHhHh')
GRADE = struct.Struct('<5hHI')
STR8 = struct.Struct('<IHH')
SPARSE = struct.Struct('<IIHH')
REC_BYTES = 32
MAX_RECS = 1 << 17
MAX_GRADES = 16
MAX_ABIL = 128
TABLES = {1: 'LIST_FACEITEM', 2: 'LIST_CAP', 3: 'LIST_BODY', 4: 'LIST_ARMS', 5: 'LIST_FOOT', 6: 'LIST_BACK', 7: 'LIST_JEWEL', 8: 'LIST_WEAPON', 9: 'LIST_SUBWPN',
          10: 'LIST_USEITEM', 11: 'LIST_JEMITEM', 12: 'LIST_NATURAL', 13: 'LIST_QUESTITEM'}
# tooltip words: LIST_STRING.STL keys, in the order of the runtime's GsvrRoseItemX.L_* indices
LABELS = (106, 107, 110, 125, 170, 171, 430, 433, 434, 435, 436, 437)
assert HDR.size == 80 and REC.size == REC_BYTES and GRADE.size == 16 and STR8.size == 8 and SPARSE.size == 12


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
    except IndexError:
        return default
    return int(v) if v.lstrip('-').isdigit() else default


def _i16(v):
    return max(-32768, min(32767, v))


def _u16(v):
    return max(0, min(65535, v))


def glow_rgb(v):
    """LIST_GRADE cell 5 'rrrgggbbb' decimal -> 0xRRGGBB (each part clamped to 255)"""
    v = max(0, v)
    r, g, b = v // 1000000, (v // 1000) % 1000, v % 1000
    return (min(255, r) << 16) | (min(255, g) << 8) | min(255, b)


def grade_rows(stb):
    """LIST_GRADE -> list of (attack, hit, defence, resistance, avoid, glowRGB) for grades 0..rows-1 (row 0 = no grade, all zero); [] without a table"""
    if stb is None:
        return []
    out = []
    for g in range(min(stb['rows'], MAX_GRADES)):
        c = stb['cells'][g]
        out.append(tuple(_int(c, i) for i in range(5)) + (glow_rgb(_int(c, 5)),) if g > 0 else (0, 0, 0, 0, 0, 0))
    return out


def stl_text(stl, key, k, lang=1):
    """STL (typ, keys, langs) -> text k (0 name, 1 description) of string id `key`, '' when missing"""
    if not stl or key not in stl[1] or lang >= len(stl[2]):
        return ''
    ent = stl[2][lang][stl[1][key][0]]
    return ent[k].replace('\r\n', '\n').strip() if k < len(ent) else ''


def item_rows(tables, stls):
    """tables {type: stb}, stls {type: stl or None} -> record dicts (every row of the 13 tables that has a string id)"""
    out = []
    for t, stb in sorted(tables.items()):
        for n in range(1, min(stb['rows'], 1000)):
            c = stb['cells'][n]
            sid = c[-1].strip() if c else ''
            if not sid:
                continue
            nm = stl_text(stls.get(t), sid, 0)
            ds = stl_text(stls.get(t), sid, 1)
            r = dict(code=t * 1000 + n, rare=_u16(_int(c, 30)) if t <= 9 else 0, cls=_u16(_int(c, 4)), icon=_u16(_int(c, 9)), weight=_u16(_int(c, 7)), name=nm, desc=ds, add=[(0, 0), (0, 0)])
            if t == 11:
                r['add'] = [(_u16(_int(c, 16)), _i16(_int(c, 17))), (_u16(_int(c, 18)), _i16(_int(c, 19)))]
            out.append(r)
    return out


class _Blob:
    def __init__(self):
        self.b = bytearray()
        self.seen = {}

    def add(self, text):
        e = text.encode('utf-8')[:65000]
        if not e:
            return 0, 0
        if e in self.seen:
            return self.seen[e], len(e)
        off = len(self.b)
        self.b += e
        self.seen[e] = off
        return off, len(e)


def enc_itemx(recs, grades, labels, abil, classes, jobs):
    """recs: record dicts; grades: tuples (see grade_rows); labels: list of str (LABELS order); abil: {ability type: str}; classes / jobs: {id: str}"""
    recs = sorted(recs, key=lambda r: r['code'])
    if len(recs) > MAX_RECS:
        raise ValueError('ITEX: too many records')
    blob = _Blob()
    body = bytearray()
    for r in recs:
        no, nl = blob.add(r['name'])
        do, dl = blob.add(r['desc'])
        (a0, v0), (a1, v1) = r['add']
        body += REC.pack(r['code'], r['rare'], r['cls'], r['icon'], r['weight'], no, nl, min(dl, 65535), do, a0, v0, a1, v1)
    gb = b''.join(GRADE.pack(*g[:5], 0, g[5]) for g in grades)
    lb = bytearray()
    for s in labels:
        o, n = blob.add(s)
        lb += STR8.pack(o, n, 0)
    na = (max(abil) + 1) if abil else 0
    if na > MAX_ABIL:
        raise ValueError('ITEX: ability number out of range')
    ab = bytearray()
    for i in range(na):
        o, n = blob.add(abil.get(i, ''))
        ab += STR8.pack(o, n, 0)
    cb = bytearray()
    for k in sorted(classes):
        o, n = blob.add(classes[k])
        cb += SPARSE.pack(k, o, n, 0)
    jb = bytearray()
    for k in sorted(jobs):
        o, n = blob.add(jobs[k])
        jb += SPARSE.pack(k, o, n, 0)
    pos = HDR.size
    rec_off = pos; pos += len(body)
    grade_off = pos; pos += len(gb)
    label_off = pos; pos += len(lb)
    abil_off = pos; pos += len(ab)
    cls_off = pos; pos += len(cb)
    job_off = pos; pos += len(jb)
    text = bytes(blob.b) + b'\0' * (-len(blob.b) % 4)
    hdr = HDR.pack(1, len(recs), REC_BYTES, rec_off, pos, len(text), grade_off, len(grades), label_off, len(labels), abil_off, na, cls_off, len(classes), job_off, len(jobs), 0, 0, 0, 0)
    return hdr + bytes(body) + gb + bytes(lb) + bytes(ab) + bytes(cb) + bytes(jb) + text


def dec_itemx(d):
    """-> dict(recs, grades, labels, abil, classes, jobs); raises ValueError on a bad layout. Same bounds as the Unity reader (GsvrRoseItemX.Valid)."""
    if len(d) < HDR.size:
        raise ValueError('ITEX shorter than its header')
    h = HDR.unpack_from(d, 0)
    ver, n, stride, ro, to, tb, go, gn, lo, ln, ao, an, co, cn, jo, jn = h[:16]
    if ver != 1 or n > MAX_RECS or stride < REC_BYTES or ro < HDR.size or ro + n * stride > len(d) or to + tb > len(d) or gn > MAX_GRADES or an > MAX_ABIL:
        raise ValueError('bad ITEX header')
    for off, cnt, sz in ((go, gn, 16), (lo, ln, 8), (ao, an, 8), (co, cn, 12), (jo, jn, 12)):
        if cnt and (off < HDR.size or off + cnt * sz > len(d)):
            raise ValueError('bad ITEX table')

    def txt(o, l):
        if o + l > tb:
            raise ValueError('ITEX text outside the blob')
        return bytes(d[to + o: to + o + l]).decode('utf-8', 'replace')
    recs = []
    prev = -1
    for i in range(n):
        v = REC.unpack_from(d, ro + i * stride)
        if v[0] <= prev:
            raise ValueError('ITEX records not sorted by code')
        prev = v[0]
        recs.append(dict(code=v[0], rare=v[1], cls=v[2], icon=v[3], weight=v[4], name=txt(v[5], v[6]), desc=txt(v[8], v[7]), add=[(v[9], v[10]), (v[11], v[12])]))
    grades = []
    for g in range(gn):
        v = GRADE.unpack_from(d, go + g * 16)
        grades.append(tuple(v[0:5]) + (v[6],))
    labels = [txt(*STR8.unpack_from(d, lo + i * 8)[:2]) for i in range(ln)]
    abil = {}
    for i in range(an):
        o, l, _ = STR8.unpack_from(d, ao + i * 8)
        if l:
            abil[i] = txt(o, l)
    classes = {}
    for i in range(cn):
        k, o, l, _ = SPARSE.unpack_from(d, co + i * 12)
        classes[k] = txt(o, l)
    jobs = {}
    for i in range(jn):
        k, o, l, _ = SPARSE.unpack_from(d, jo + i * 12)
        jobs[k] = txt(o, l)
    return dict(recs=recs, grades=grades, labels=labels, abil=abil, classes=classes, jobs=jobs)


def _stb(vfs, name):
    try:
        return F.stb(vfs.read('3DDATA/STB/%s.STB' % name))
    except KeyError:
        return None


def _stl(vfs, name):
    try:
        return F.stl(vfs.read('3DDATA/STB/%s.STL' % name))
    except (KeyError, ValueError):
        return None


def _keyed(stl, as_int=True):
    """STL -> {key: first string of language 1}; keys that are not numbers are skipped when as_int"""
    out = {}
    if not stl or len(stl[2]) < 2:
        return out
    for k, (i, _o) in stl[1].items():
        if as_int and not k.strip().isdigit():
            continue
        ent = stl[2][1][i]
        out[int(k) if as_int else k] = ent[0].strip() if ent else ''
    return out


def build_section(vfs):
    """-> (sections, warnings). Never raises on a missing table: nothing readable = no section."""
    warn = []
    tables, stls = {}, {}
    for t, nm in sorted(TABLES.items()):
        stb = _stb(vfs, nm)
        if stb is None:
            warn.append('ITEX: %s.STB missing' % nm)
            continue
        tables[t] = stb
        stls[t] = _stl(vfs, nm + '_S')
        if stls[t] is None:
            warn.append('ITEX: %s_S.STL missing (no names / descriptions)' % nm)
    recs = item_rows(tables, stls)
    if not recs:
        return [], warn + ['ITEX: no item rows']
    grades = grade_rows(_stb(vfs, 'LIST_GRADE'))
    if not grades:
        warn.append('ITEX: LIST_GRADE.STB missing (no grade bonuses)')
    lstr = _keyed(_stl(vfs, 'LIST_STRING'))
    labels = [lstr.get(k, '') for k in LABELS]
    abil = _keyed(_stl(vfs, 'STR_ABILITY'))
    classes = _keyed(_stl(vfs, 'STR_ITEMTYPE'))
    jobs = {}
    cstb, cstl = _stb(vfs, 'LIST_CLASS'), _stl(vfs, 'LIST_CLASS_S')
    if cstb is not None and cstl is not None:
        for i in range(1, cstb['rows']):
            c = cstb['cells'][i]
            sid = c[-1].strip() if c else ''
            if sid:
                nm = stl_text(cstl, sid, 0)
                if nm:
                    jobs[i] = nm
    return [(TAG_ITEX, 1, len(recs), enc_itemx(recs, grades, labels, abil, classes, jobs))], warn
