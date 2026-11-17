"""ROSE w15 SKILLTIPS (companion-owned, like rose_qglob.py: the vendored converter files stay byte-identical to rose-vrchat): the skill DESCRIPTIONS of the skill tooltip.

The runtime shows the original's skill tooltip on hover (skill window rows, skill tree rows, quickbar slots): name + level, the description text of LIST_SKILL_S.STL, cost, power,
range, requirements. The numbers come from the SKDB records; only the description text is new data. It is appended to the SKDB section (rose_skills.py layout, header words 10 / 11
were reserved zero: packs without the table read as "no description"):

  header word 10 = descCount ; word 11 = descOff (4-byte aligned, after the job name table)
  description record 8 at descOff + i * 8, sorted by skill id: u16 skill id ; u16 len ; u32 off (UTF-8, off relative to descOff + descCount * 8; equal texts stored once)
  text blob right after the records

Source: LIST_SKILL_S.STL text 1 of the string id in the last cell of the LIST_SKILL row (the same id the names use), English by the rose_text fallbacks; `<br>` = line break.
The client's texts have no placeholders (checked on the real client: 228 texts, one `<br>`); the tooltip numbers are filled from the record by GsvrRoseSkill.TipText.
Japanese: TXJA domain D_SKILL, sub 1, key = skill id (the overlay of the names, sub 0). German has no client text: it falls back to English in the runtime.
`install()` wraps rose_skills.build_section (called by the vendored rose_pack through the module attribute), so the vendored files are not edited. No text of the game is committed.
"""
import struct

import rosefmt as F
import rose_text as TX
import rose_skills

HDR_DESC_COUNT, HDR_DESC_OFF = 10, 11          # header word indices of rose_skills.HDR
DESC = struct.Struct('<HHI')
MAX_DESC = 400
MAX_SKILLS = rose_skills.MAX_SKILLS
_BR = ('<br>', '<br/>', '<br />')


def clean_desc(t):
    """a STL description as the tooltip shows it: NULs removed, `<br>` (any case, with or without `/`) = line break, lines trimmed, empty lines dropped"""
    t = str(t).replace('\x00', '').replace('\r', '')
    low = t.lower()
    for br in _BR:
        while br in low:
            i = low.index(br)
            t = t[:i] + '\n' + t[i + len(br):]
            low = t.lower()
    return '\n'.join(x.strip() for x in t.split('\n') if x.strip())


def cut_utf8(b, n):
    """b cut to at most n bytes without splitting a UTF-8 character"""
    if len(b) <= n:
        return b
    b = b[:n]
    while b and (b[-1] & 0xC0) == 0x80:
        b = b[:-1]
    if b and b[-1] >= 0xC0:
        b = b[:-1]
    return b


def append_table(skdb, descs):
    """skdb = SKDB section bytes, descs = [(skill id, text)] -> SKDB bytes with the description table (unchanged without a usable text). Header words 10 / 11 are rewritten."""
    if len(skdb) < rose_skills.HDR.size:
        return skdb
    items = sorted((int(i), clean_desc(t)) for i, t in descs if 0 < int(i) <= 65535)
    items = [(i, t) for i, t in items if t][:MAX_SKILLS]
    if not items:
        return skdb
    h = list(rose_skills.HDR.unpack_from(skdb, 0))
    out = bytearray(skdb)
    out += b'\0' * (-len(out) % 4)
    off = len(out)
    recs = bytearray()
    blob = bytearray()
    seen = {}
    for i, t in items:
        b = cut_utf8(t.encode('utf-8'), MAX_DESC)
        if b not in seen:
            seen[b] = len(blob)
            blob += b
        recs += DESC.pack(i, len(b), seen[b])
    out += recs + blob
    out += b'\0' * (-len(out) % 4)
    h[HDR_DESC_COUNT], h[HDR_DESC_OFF] = len(items), off
    rose_skills.HDR.pack_into(out, 0, *h)
    return bytes(out)


def dec_descs(d):
    """-> {skill id: description}; {} when the section has no table or a bad one. Same bounds as GsvrRoseSkill.DescCount / DescText."""
    if len(d) < rose_skills.HDR.size:
        return {}
    h = rose_skills.HDR.unpack_from(d, 0)
    n, o = h[HDR_DESC_COUNT], h[HDR_DESC_OFF]
    if n <= 0 or n > MAX_SKILLS or o < rose_skills.HDR.size or o + n * DESC.size > len(d):
        return {}
    base = o + n * DESC.size
    out = {}
    for i in range(n):
        sid, ln, off = DESC.unpack_from(d, o + i * DESC.size)
        if base + off + ln <= len(d):
            out[sid] = bytes(d[base + off: base + off + ln]).decode('utf-8', 'replace')
    return out


def stl_descs(vfs, lang=TX.EN):
    """string id -> cleaned description (STL text 1) in STL language `lang`; {} without the file"""
    try:
        typ, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_SKILL_S.STL'))
    except (KeyError, ValueError, struct.error, IndexError):
        return {}
    out = {}
    for k in keys:
        t = clean_desc(TX.stl_text((typ, keys, langs), k, 1, lang))
        if t:
            out[k] = t
    return out


def rows_of(skdb_ids, skill_stb, en, ja):
    """-> [(skill id, english, japanese)] for the skill ids of the SKDB: the description of the string id in the last cell of the LIST_SKILL row"""
    out = []
    for sid in skdb_ids:
        if sid >= skill_stb['rows']:
            continue
        c = skill_stb['cells'][sid]
        key = str(c[-1]).strip() if c else ''
        if en.get(key):
            out.append((sid, en[key], ja.get(key, '')))
    return out


def build_with_descs(orig, vfs):
    """the wrapper body: orig(vfs) -> (sections, warnings); the SKDB section gets the description table, the Japanese texts go to the TXJA collector"""
    sections, warn = orig(vfs)
    if not sections:
        return sections, warn
    tag, ver, count, data = sections[0]
    sk = rose_skills._stb(vfs, 'LIST_SKILL')
    en = stl_descs(vfs)
    if sk is None or not en:
        return sections, warn
    ja = stl_descs(vfs, TX.JA)
    ids = [r['id'] for r in rose_skills.dec_skdb(data)[0]]
    rows = rows_of(ids, sk, en, ja)
    for sid, e, j in rows:
        TX.note(TX.D_SKILL, sid, 1, e, j)
    return [(tag, ver, count, append_table(data, [(s, e) for s, e, _j in rows]))] + list(sections[1:]), warn


_orig = None


def install():
    """wraps rose_skills.build_section once (idempotent); the vendored rose_pack calls it as rose_skills.build_section(vfs)"""
    global _orig
    if _orig is not None:
        return
    _orig = rose_skills.build_section
    orig = _orig

    def build_section(vfs):
        return build_with_descs(orig, vfs)
    rose_skills.build_section = build_section
