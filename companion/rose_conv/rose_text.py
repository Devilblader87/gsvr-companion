"""Language overlay of the RPG1 pack (docs/RPG1_PACK.md section 26): the player's own client texts in a second language, optional and additive.

Why an overlay: every text section of the pack (ITEM, DROP, ITEX, QUST, SKDB, SHOP, the NPC names) stores ONE language (English, STL language 1) in its own
fixed layout, and the runtime reads those sections as before. The Japanese (STL language 2, LTB column 3) is written once, in one extra section, keyed by the same
numbers the runtime already looks the English text up with. An old runtime ignores the unknown tag and shows English, a new runtime picks the overlay for Japanese players.

Sections (same layout, tag = language):
  TXJA  Japanese (STL language index 2) : entries whose Japanese text exists and differs from the English text of the legacy section
  TXEN  English (STL language index 1)  : only the domains that have NO legacy English home (zone names, the UI string tables), see EN_HOME
Layout (little endian, 4-byte aligned, section version 1):
  header 32: u32 version (1) ; u32 stlLanguage (2 JA, 1 EN) ; u32 count ; u32 recOff (32) ; u32 recStride (16) ; u32 textOff ; u32 textBytes ; u32 0
  record 16 at recOff + i * stride, sorted by (domain, sub, key) ascending: u16 domain ; u16 sub ; u32 key ; u32 off ; u32 len   (UTF-8 bytes inside the text blob)
  text blob at textOff: UTF-8, equal strings stored once.
Readers: binary search on (domain, sub, key); a missing entry = use the legacy (English) text. Unknown domains are ignored.

Fallback rules (Stefan 2026-11-15, `pick`): the STL has 5 language columns 0 Korean, 1 English, 2 Japanese, 3 traditional Chinese, 4 simplified Chinese.
  English text  = English, else Japanese, traditional Chinese, simplified Chinese, and Korean only when nothing else exists.
  Japanese text = Japanese, else the English text above.
No text of the game is committed; everything is read at conversion time on the player's machine.
"""
import struct

TAG_TXJA = b'TXJA'
TAG_TXEN = b'TXEN'
VERSION = 1
HDR = struct.Struct('<8I')
REC = struct.Struct('<HHIII')
assert HDR.size == 32 and REC.size == 16

KO, EN, JA, ZHT, ZHS = 0, 1, 2, 3, 4
LTB_EN, LTB_JA = 2, 3                         # ULNGTB_CON.LTB columns (0 id, 1 Korean, 2 English, 3 Japanese, 4 / 5 Chinese)

# domains: the key and the sub numbers are the ones the runtime already looks the English text up with
D_ITEM = 1        # key = item code (type * 1000 + number); sub 0 name, 1 description
D_NPC = 2         # key = LIST_NPC row (npc / monster id); sub 0 name
D_QSTR = 3        # key = byte offset of the English string inside the QUST string blob (dialog lines, quest name / description / start / end, quest item names); sub 0
D_SKILL = 4       # key = skill id (LIST_SKILL row); sub 0 name
D_JOB = 5         # key = job id (STR_JOB.STL key); sub 0 name
D_ZONE = 6        # key = zone id (LIST_ZONE row); sub 0 name                                  [EN home: TXEN]
D_SHOPTAB = 7     # key = index of the tab in the SHOP section; sub 0 name
D_LABEL = 8       # key = index in the ITEX label table (rose_itemx.LABELS order); sub 0
D_ABIL = 9        # key = ability type number (STR_ABILITY.STL); sub 0
D_ITEMTYPE = 10   # key = item class number (STR_ITEMTYPE.STL); sub 0
D_JOBCLASS = 11   # key = LIST_CLASS row (job class name); sub 0
D_USTR = 12       # key = LIST_STRING.STL key (the client's UI words); sub 0                   [EN home: TXEN]
D_SKTYPE = 13     # key = STR_SKILLTYPE.STL key                                                [EN home: TXEN]
D_SKTARGET = 14   # key = STR_SKILLTARGET.STL key                                              [EN home: TXEN]
D_STATUS = 15     # key = LIST_STATUS_S.STL key; sub 0 name, 1 description                     [EN home: TXEN]
D_CLAN = 16       # key = STR_CLAN.STL key                                                     [EN home: TXEN]
D_PLANET = 17     # key = STR_PLANET.STL key                                                   [EN home: TXEN]
D_UNION = 18      # key = LIST_UNION_S.STL key                                                 [EN home: TXEN]
D_PREFIX = 19     # key = STR_ITEMPREFIX.STL key                                               [EN home: TXEN]
EN_HOME = (D_ZONE, D_USTR, D_SKTYPE, D_SKTARGET, D_STATUS, D_CLAN, D_PLANET, D_UNION, D_PREFIX)

# global tables: (domain, stl file, subs)  (item / NPC / quest / skill / shop tables are written where the converter reads them)
GLOBAL_STLS = ((D_USTR, 'LIST_STRING', 1), (D_SKTYPE, 'STR_SKILLTYPE', 1), (D_SKTARGET, 'STR_SKILLTARGET', 1), (D_STATUS, 'LIST_STATUS_S', 2),
               (D_CLAN, 'STR_CLAN', 1), (D_PLANET, 'STR_PLANET', 1), (D_UNION, 'LIST_UNION_S', 1), (D_PREFIX, 'STR_ITEMPREFIX', 1))
MAX_TEXT = 4000


def clean(s):
    """a cell as stored (NUL padding removed); a whitespace-only cell counts as empty in `pick`"""
    return s.replace('\x00', '') if isinstance(s, str) else ''


def pick(cells, lang):
    """cells = the texts of one STL entry (or LTB row) per language column [ko, en, ja, zh-trad, zh-simp] (missing / empty allowed) -> text of `lang` (EN or JA)
    by the fallback rules of the module docstring, the chosen cell unchanged; '' when no language has a text."""
    c = [clean(cells[i]) if i < len(cells) else '' for i in range(5)]
    c = [x if x.strip() else '' for x in c]
    en = c[EN] or c[JA] or c[ZHT] or c[ZHS] or c[KO]
    if lang == JA:
        return c[JA] or en
    return en


def stl_cells(stl, key, k):
    """STL (typ, keys, langs) -> [text k of language 0..n] for string id `key`; [] when the key is missing"""
    if not stl or key not in stl[1]:
        return []
    i = stl[1][key][0]
    out = []
    for L in stl[2]:
        e = L[i] if i < len(L) else []
        out.append(e[k] if k < len(e) else '')
    return out


def stl_text(stl, key, k, lang):
    return pick(stl_cells(stl, key, k), lang)


class Collector:
    """texts noted while the converter builds the sections: {(domain, sub, key): (english, japanese)}; written by sections()"""

    def __init__(self):
        self.d = {}

    def note(self, domain, key, sub, en, ja):
        en, ja = clean(en), clean(ja)
        if not ja.strip():
            ja = en
        k = (domain, sub, key)
        if k in self.d:
            return
        self.d[k] = (en[:MAX_TEXT], ja[:MAX_TEXT])

    def note_cells(self, domain, key, sub, cells):
        """one STL entry: both languages by `pick`"""
        if cells:
            self.note(domain, key, sub, pick(cells, EN), pick(cells, JA))

    def sections(self):
        """-> [(tag, 1, count, bytes)] for the non-empty overlays"""
        ja = [(k, v[1]) for k, v in self.d.items() if v[1] and v[1] != v[0]]
        en = [(k, v[0]) for k, v in self.d.items() if k[0] in EN_HOME and v[0]]
        out = []
        if ja:
            out.append((TAG_TXJA, 1, len(ja), enc(ja, JA)))
        if en:
            out.append((TAG_TXEN, 1, len(en), enc(en, EN)))
        return out


_cur = Collector()


def reset():
    global _cur
    _cur = Collector()
    return _cur


def current():
    return _cur


def note(domain, key, sub, en, ja):
    _cur.note(domain, key, sub, en, ja)


def note_cells(domain, key, sub, cells):
    _cur.note_cells(domain, key, sub, cells)


def enc(entries, stl_lang):
    """entries: [((domain, sub, key), text)] -> section bytes (sorted, strings stored once)"""
    ent = sorted((k, t) for k, t in entries if t)
    blob = bytearray()
    seen = {}
    recs = bytearray()
    for (dom, sub, key), t in ent:
        b = t.encode('utf-8')[:MAX_TEXT]
        if b in seen:
            off = seen[b]
        else:
            off = len(blob)
            blob += b
            seen[b] = off
        recs += REC.pack(dom, sub, key, off, len(b))
    blob += b'\0' * (-len(blob) % 4)
    text_off = HDR.size + len(recs)
    return HDR.pack(VERSION, stl_lang, len(ent), HDR.size, REC.size, text_off, len(blob), 0) + bytes(recs) + bytes(blob)


def dec(d):
    """section bytes -> (stl_lang, {(domain, sub, key): text}); ValueError on bad bounds / unsorted records (same bounds as GsvrRoseText.Valid)"""
    if len(d) < HDR.size:
        raise ValueError('text overlay shorter than its header')
    ver, lang, n, ro, stride, to, tb, _z = HDR.unpack_from(d, 0)
    if ver != VERSION or stride < REC.size or ro < HDR.size or n > 1 << 20 or ro + n * stride > len(d) or to < 0 or to + tb > len(d):
        raise ValueError('bad text overlay header')
    out = {}
    prev = None
    for i in range(n):
        dom, sub, key, off, ln = REC.unpack_from(d, ro + i * stride)
        k = (dom, sub, key)
        if prev is not None and k <= prev:
            raise ValueError('text overlay records not sorted')
        prev = k
        if off + ln > tb:
            raise ValueError('text overlay string outside the blob')
        out[k] = bytes(d[to + off: to + off + ln]).decode('utf-8')
    return lang, out


def lookup(table, domain, key, sub=0):
    return table.get((domain, sub, key), '')


def global_texts(vfs, read_stl, zone_ids=None):
    """notes the zone-independent texts: zone names (every LIST_ZONE row with a string id) and the UI string tables. read_stl(name) -> rosefmt.stl tuple or None."""
    import rosefmt as F
    try:
        zl = F.stb(vfs.read('3DDATA/STB/LIST_ZONE.STB'))
        zs = read_stl('LIST_ZONE_S')
    except (KeyError, ValueError, struct.error, IndexError):
        zl = zs = None
    if zl and zs:
        for zid in range(zl['rows']):
            c = zl['cells'][zid]
            sid = c[26].strip() if len(c) > 26 else ''
            if sid:
                note_cells(D_ZONE, zid, 0, stl_cells(zs, sid, 0))
    for dom, name, subs in GLOBAL_STLS:
        stl = read_stl(name)
        if not stl:
            continue
        for k in stl[1]:
            ks = str(k).strip()
            if not ks.isdigit():
                continue
            for s in range(subs):
                note_cells(dom, int(ks), s, stl_cells(stl, k, s))
