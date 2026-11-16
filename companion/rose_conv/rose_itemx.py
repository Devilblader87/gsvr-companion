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
Craft block (optional, header words 16 / 17 = block offset / block bytes, both 0 without it; docs/design/rose_craft.md in the half-life repo, runtime GsvrRoseCraft):
  block header 16: u32 itemCount ; u32 recipeCount ; u32 0 ; u32 0
  item 12 (sorted by code): u32 code ; u16 makeType ; u16 skillLevel ; u16 recipeRow ; u16 difficulty        (item table cells 12 / 13 / 14 / 15: the make skill type, its level, the LIST_PRODUCT row, the difficulty)
  recipe 40 (sorted by row): u32 row ; 4 x { u32 itemCode (0 = any item of the class) ; u16 quantity ; u16 class } ; u32 0
        LIST_PRODUCT.STB row cells: 1 = class of material 1, 2 = item code of material 1 (blank = by class), 3 = its quantity, 4 / 6 / 8 = item codes of materials 2..4, 5 / 7 / 9 = their quantities.
        Rows 1..10 are the weapon refine steps (+1..+10), rows 11..20 the armour refine steps; the rows from 21 on are the make recipes the item cells point at.
  PRODUCT.STB (2003 rows) is a legacy table: the client never loads it, it is not used.
No text or table of the game is committed; everything is read at conversion time on the player's machine.
"""
import struct

import rosefmt as F
import rose_text as TX

TAG_ITEX = b'ITEX'
HDR = struct.Struct('<20I')
REC = struct.Struct('<IHHHHIHHIHhHh')
GRADE = struct.Struct('<5hHI')
STR8 = struct.Struct('<IHH')
SPARSE = struct.Struct('<IIHH')
REC_BYTES = 32
CRAFT_HDR = 16
CRAFT_ITEM = struct.Struct('<IHHHH')
CRAFT_MAT = struct.Struct('<IHH')
CRAFT_REC = struct.Struct('<I')
CRAFT_TYPES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 11)       # item tables with the craft cells (faceitem .. subweapon, gems); use items / materials / quest items have none
MAX_CRAFT_ITEMS = 16384
MAX_CRAFT_RECIPES = 4096
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
    """STL (typ, keys, langs) -> text k (0 name, 1 description) of string id `key` in language `lang` (1 English, 2 Japanese; the fallback rules of rose_text.pick), '' when missing"""
    return TX.stl_text(stl, key, k, lang).replace('\r\n', '\n').strip() if stl and key in stl[1] else ''


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
            TX.note(TX.D_ITEM, t * 1000 + n, 0, nm, stl_text(stls.get(t), sid, 0, TX.JA))          # language overlay (rose_text): Japanese name + description
            TX.note(TX.D_ITEM, t * 1000 + n, 1, ds, stl_text(stls.get(t), sid, 1, TX.JA))
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


def craft_rows(tables, product):
    """-> (items, recipes) of the craft block. tables {type: stb}; product = LIST_PRODUCT stb or None.
    items: [(code, make, level, row, difficulty)] for every item row that names a make skill type, a recipe row that holds a recipe and a level;
    recipes: {row: [(code, qty, class) x 4]} for the rows that have any material (refine rows 1..20 and the make rows)."""
    if product is None:
        return [], {}
    recs = {}
    for n in range(1, min(product['rows'], MAX_CRAFT_RECIPES)):
        c = product['cells'][n]
        mats = []
        cls1 = _u16(_int(c, 1))
        for k in range(4):
            if k == 0:
                code, qty, cls = _int(c, 2), _int(c, 3), cls1
            else:
                code, qty, cls = _int(c, 2 + 2 * k), _int(c, 3 + 2 * k), 0
            if qty <= 0 or (code <= 0 and cls <= 0):
                mats.append((0, 0, 0))
            else:
                mats.append((max(0, code), _u16(qty), cls if code <= 0 else 0))
        if any(m[1] for m in mats):
            recs[n] = mats
    items = []
    for t in CRAFT_TYPES:
        stb = tables.get(t)
        if stb is None:
            continue
        for n in range(1, min(stb['rows'], 1000)):
            c = stb['cells'][n]
            make, lvl, row, dif = _int(c, 12), _int(c, 13), _int(c, 14), _int(c, 15)
            if make <= 0 or row <= 0 or row not in recs:
                continue
            items.append((t * 1000 + n, _u16(make), _u16(lvl), _u16(row), _u16(dif)))
    return items, recs


def enc_craft(items, recs):
    """the craft block bytes (layout in the module docstring); b'' when there is nothing to write"""
    if not items and not recs:
        return b''
    if len(items) > MAX_CRAFT_ITEMS or len(recs) > MAX_CRAFT_RECIPES:
        raise ValueError('ITEX craft block: too many entries')
    out = bytearray(struct.pack('<4I', len(items), len(recs), 0, 0))
    for it in sorted(items):
        out += CRAFT_ITEM.pack(*it)
    for row in sorted(recs):
        out += CRAFT_REC.pack(row)
        for code, qty, cls in recs[row]:
            out += CRAFT_MAT.pack(code, qty, cls)
        out += struct.pack('<I', 0)
    return bytes(out)


def enc_itemx(recs, grades, labels, abil, classes, jobs, craft=b''):
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
    craft_off = pos if craft else 0; pos += len(craft)
    text = bytes(blob.b) + b'\0' * (-len(blob.b) % 4)
    hdr = HDR.pack(1, len(recs), REC_BYTES, rec_off, pos, len(text), grade_off, len(grades), label_off, len(labels), abil_off, na, cls_off, len(classes), job_off, len(jobs), craft_off, len(craft), 0, 0)
    return hdr + bytes(body) + gb + bytes(lb) + bytes(ab) + bytes(cb) + bytes(jb) + bytes(craft) + text


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
    craft = None
    co_, cn_ = h[16], h[17]
    if co_ or cn_:
        if co_ < HDR.size or cn_ < CRAFT_HDR or co_ + cn_ > len(d):
            raise ValueError('bad ITEX craft block')
        ni, nr = struct.unpack_from('<2I', d, co_)
        if ni > MAX_CRAFT_ITEMS or nr > MAX_CRAFT_RECIPES or CRAFT_HDR + ni * 12 + nr * 40 > cn_:
            raise ValueError('bad ITEX craft block counts')
        items = [CRAFT_ITEM.unpack_from(d, co_ + CRAFT_HDR + i * 12) for i in range(ni)]
        rbase = co_ + CRAFT_HDR + ni * 12
        rec_ = {}
        for i in range(nr):
            o = rbase + i * 40
            rec_[struct.unpack_from('<I', d, o)[0]] = [CRAFT_MAT.unpack_from(d, o + 4 + 8 * k) for k in range(4)]
        craft = dict(items=items, recipes=rec_)
    return dict(recs=recs, grades=grades, labels=labels, abil=abil, classes=classes, jobs=jobs, craft=craft)


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


def _keyed(stl, as_int=True, lang=1):
    """STL -> {key: first string of the language}; keys that are not numbers are skipped when as_int"""
    out = {}
    if not stl or len(stl[2]) < 2:
        return out
    for k, (i, _o) in stl[1].items():
        if as_int and not k.strip().isdigit():
            continue
        out[int(k) if as_int else k] = TX.stl_text(stl, k, 0, lang).strip()
    return out


def _note_keyed(domain, en, ja):
    for k, v in en.items():
        TX.note(domain, k, 0, v, ja.get(k, ''))


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
    stl_str, stl_abil, stl_cls = _stl(vfs, 'LIST_STRING'), _stl(vfs, 'STR_ABILITY'), _stl(vfs, 'STR_ITEMTYPE')
    lstr = _keyed(stl_str)
    labels = [lstr.get(k, '') for k in LABELS]
    abil = _keyed(stl_abil)
    classes = _keyed(stl_cls)
    ljs = _keyed(stl_str, lang=TX.JA)
    for i, k in enumerate(LABELS):
        TX.note(TX.D_LABEL, i, 0, labels[i], ljs.get(k, ''))
    _note_keyed(TX.D_ABIL, abil, _keyed(stl_abil, lang=TX.JA))
    _note_keyed(TX.D_ITEMTYPE, classes, _keyed(stl_cls, lang=TX.JA))
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
                    TX.note(TX.D_JOBCLASS, i, 0, nm, stl_text(cstl, sid, 0, TX.JA))
    citems, crecs = craft_rows(tables, _stb(vfs, 'LIST_PRODUCT'))
    if not crecs:
        warn.append('ITEX: LIST_PRODUCT.STB missing or empty (no craft block: making and the original refine materials are off)')
    craft = enc_craft(citems, crecs)
    return [(TAG_ITEX, 1, len(recs), enc_itemx(recs, grades, labels, abil, classes, jobs, craft))], warn
