"""Original texts of the Ragnarok world (w43 lane txt, docs/design/ro_rotxt.md): the 'ROTX' section of the 'roui' pack.

The owner's client (English) keeps the words the world shows in loose files of its System folder and in data.grf:
  System/iteminfo.lub              item names (identified / unidentified), the long description with the ^RRGGBB colour codes, card slots
  data/msgstringtable.txt (grf)    every client message by line number ('You got %s (%d).', 'You got %s Base EXP.', 'Not Enough SP', ...)
  System/Towninfo.lub              the town markers of the minimap (name, cell x / y, type) per map
Only what the world uses is packed (the bag / drop / shop item ids of ui.icon_ids(), MSG_IDS, the markers of api.MAP_ORDER); nothing of it is in git, the
companion reads it from the owner's install (read only) and the world gets it inside the interface pack, so no extra download round.

  ROTX (section version 1), all little endian, ASCII only (anything else becomes '?'):
    header 8 x u32: version 1, item count, message count, marker count, pool bytes, item table offset, message table offset, marker table offset (offsets from the section start)
    items   8 x i32: item id, name off, name len, unidentified name off / len, description off / len (lines joined by LF), slot count        (sorted by id)
    msgs    3 x i32: message id, off, len                                                                                                   (sorted by id)
    marks   7 x i32: map name off / len, marker name off / len, type, cell x, cell y                                                         (file order)
    pool: the strings back to back (no terminator)

    build(folder, vfs, item_ids, map_names, warn) -> bytes of the section (b'' when the client has none of the files)
"""
import os
import struct

from . import lub

TAG_ROTX = b'ROTX'
SECTION_VERSION = 1
HDR = struct.Struct('<8I')
ITEM = struct.Struct('<8i')
MSG = struct.Struct('<3i')
MARK = struct.Struct('<7i')
PATH_MSG = 'data/msgstringtable.txt'
MAX_STR = 4000

# client message numbers (line index in msgstringtable.txt, 0 based) the world shows: loot + exp lines, refusals of the shops / skills / weight rules, window words
MSG_IDS = (
    52, 53, 55, 56, 105, 153, 168, 185, 202, 203, 214, 220, 233, 239, 242, 243, 244, 245, 259, 279, 284, 327, 372, 487, 488, 580, 603, 663, 1317, 1334, 1722,
)
M_OVERWEIGHT, M_NOZENY, M_GOT_ITEM, M_NO_SP, M_NO_HP, M_GOT_ZENY, M_GOT_BASE, M_GOT_JOB, M_SKILL_POINT, M_STATUS_POINTS = 52, 55, 153, 202, 203, 327, 487, 488, 284, 259


def _ascii(s):
    return str(s).encode('ascii', 'replace').decode('ascii')[:MAX_STR]


def _system_file(folder, name):
    """folder/System/<name> case-insensitive -> bytes | None."""
    if not folder:
        return None
    for sysn in os.listdir(folder) if os.path.isdir(folder) else ():
        if sysn.lower() == 'system' and os.path.isdir(os.path.join(folder, sysn)):
            base = os.path.join(folder, sysn)
            for fn in os.listdir(base):
                if fn.lower() == name.lower():
                    with open(os.path.join(base, fn), 'rb') as f:
                        return f.read()
    return None


def read_items(data, ids):
    """iteminfo.lub bytes -> {id: (name, unidentified name, description, slots)} for the ids the client has."""
    tbl = lub.tables(data).get('tbl') or {}
    out = {}
    for iid in ids:
        r = tbl.get(iid)
        if not isinstance(r, dict):
            continue
        desc = r.get('identifiedDescriptionName') or {}
        lines = [desc[k] for k in sorted(k for k in desc if isinstance(k, int)) if isinstance(desc[k], str)]
        name = r.get('identifiedDisplayName')
        if not isinstance(name, str) or not name:
            continue
        un = r.get('unidentifiedDisplayName')
        slots = r.get('slotCount')
        out[iid] = (name, un if isinstance(un, str) and un else name, '\n'.join(lines), slots if isinstance(slots, int) else 0)
    return out


def read_messages(data, ids):
    """msgstringtable.txt bytes (cp949, one message per line, a trailing '#') -> {line number: text}."""
    lines = data.decode('cp949', 'replace').split('\n')
    out = {}
    for i in ids:
        if 0 <= i < len(lines):
            t = lines[i].rstrip('\r')
            if t.endswith('#'):
                t = t[:-1]
            if t:
                out[i] = t
    return out


def read_markers(data, map_names):
    """Towninfo.lub bytes -> [(map, marker name, type, x, y)] of the maps in map_names (file order of the maps = the table order)."""
    tbl = lub.tables(data).get('mapNPCInfoTable') or {}
    want = set(map_names)
    out = []
    for m, rows in tbl.items():
        if m not in want or not isinstance(rows, dict):
            continue
        for k in sorted(k for k in rows if isinstance(k, int)):
            r = rows[k]
            if isinstance(r, dict) and isinstance(r.get('name'), str) and all(isinstance(r.get(a), int) for a in ('X', 'Y', 'TYPE')):
                out.append((m, r['name'], r['TYPE'], r['X'], r['Y']))
    return out


def encode(items, msgs, marks):
    """items {id: (name, unid, desc, slots)}, msgs {id: text}, marks [(map, name, type, x, y)] -> the ROTX section bytes."""
    pool = bytearray()

    def put(s):
        b = _ascii(s).encode('ascii')
        off = len(pool)
        pool.extend(b)
        return off, len(b)
    irec = b''
    for iid in sorted(items):
        n, u, d, s = items[iid]
        no, nl = put(n)
        uo, ul = put(u)
        do, dl = put(d)
        irec += ITEM.pack(iid, no, nl, uo, ul, do, dl, s)
    mrec = b''
    for mid in sorted(msgs):
        o, l = put(msgs[mid])
        mrec += MSG.pack(mid, o, l)
    krec = b''
    for m, n, t, x, y in marks:
        mo, ml = put(m)
        no, nl = put(n)
        krec += MARK.pack(mo, ml, no, nl, t, x, y)
    io = HDR.size
    mo = io + len(irec)
    ko = mo + len(mrec)
    head = HDR.pack(SECTION_VERSION, len(items), len(msgs), len(marks), len(pool), io, mo, ko)
    return head + irec + mrec + krec + bytes(pool)


def decode(d):
    """-> (items {id: (name, unid, desc, slots)}, msgs {id: text}, marks [(map, name, type, x, y)]) - tests / tools."""
    ver, ni, nm, nk, npool, io, mo, ko = HDR.unpack_from(d, 0)
    if ver != SECTION_VERSION:
        raise ValueError('ROTX version')
    po = ko + nk * MARK.size

    def s(o, l):
        return d[po + o:po + o + l].decode('ascii')
    items, msgs, marks = {}, {}, []
    for i in range(ni):
        iid, no, nl, uo, ul, do, dl, sl = ITEM.unpack_from(d, io + i * ITEM.size)
        items[iid] = (s(no, nl), s(uo, ul), s(do, dl), sl)
    for i in range(nm):
        mid, o, l = MSG.unpack_from(d, mo + i * MSG.size)
        msgs[mid] = s(o, l)
    for i in range(nk):
        a, al, b, bl, t, x, y = MARK.unpack_from(d, ko + i * MARK.size)
        marks.append((s(a, al), s(b, bl), t, x, y))
    return items, msgs, marks


def section(folder, vfs, item_ids, map_names, warn=None):
    """-> (tag, version, count, data) or None when the client has no text file at all. A missing / unreadable file only drops its part (warning)."""
    items, msgs, marks = {}, {}, []
    raw = _system_file(folder, 'iteminfo.lub')
    if raw is None:
        if warn is not None:
            warn.append('rotxt: System/iteminfo.lub not found (no original item texts)')
    else:
        try:
            items = read_items(raw, item_ids)
        except (lub.LubError, ValueError, IndexError, KeyError, struct.error) as e:
            if warn is not None:
                warn.append('rotxt: iteminfo.lub unreadable (%s)' % e)
    try:
        msgs = read_messages(vfs.read(PATH_MSG), MSG_IDS)
    except KeyError:
        if warn is not None:
            warn.append('rotxt: data/msgstringtable.txt not in the client data (no original messages)')
    raw = _system_file(folder, 'towninfo.lub')
    if raw is None:
        if warn is not None:
            warn.append('rotxt: System/Towninfo.lub not found (no town markers)')
    else:
        try:
            marks = read_markers(raw, map_names)
        except (lub.LubError, ValueError, IndexError, KeyError, struct.error) as e:
            if warn is not None:
                warn.append('rotxt: Towninfo.lub unreadable (%s)' % e)
    if not items and not msgs and not marks:
        return None
    if warn is not None and item_ids and len(items) < len(item_ids):
        warn.append('rotxt: %d of %d world items have no iteminfo entry' % (len(item_ids) - len(items), len(item_ids)))
    return (TAG_ROTX, SECTION_VERSION, len(items) + len(msgs) + len(marks), encode(items, msgs, marks))
