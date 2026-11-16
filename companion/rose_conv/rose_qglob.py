"""GLOBAL QUEST TEXTS (ROSE w14), companion-owned (not a synced converter file): section QGLB for the always-loaded avatar pack.

QUST (rose_quest) only holds the quests / quest items the triggers of ONE zone name, so a quest taken in another zone showed only its name in the log (no description,
quest items as "#code"). QGLB has the QUST layout with just two filled tables: quest (every LIST_QUEST row: name, description, start, end, time limit) and qitem (every
LIST_QUESTITEM row: code 13000 + row, icon, name). Japanese texts: TXJA of the avatar pack, domain QSTR sub 1, keyed by the offset in the QGLB blob (a zone's own QUST uses sub 0).
Runtime: GsvrRoseQuest.QuestTextG / QItemIconG / QItemNameG (zone table first, this one when the zone has no row), GsvrRpg1Actors.TakeGlobalQuest.
"""
import collections
import struct

import rosefmt as F
import rose_text as TX
import rose_quest as Q

TAG_QGLB = b'QGLB'
GLOB_SUB = 1


class GlobBlob(Q.StrBlob):
    """StrBlob whose Japanese overlay entries use sub GLOB_SUB"""

    def add(s, text, ja=None):
        if text is None:
            return -1
        text = text[:4000]
        ja = ja[:4000] if ja and ja != text else None
        key = (text, ja)
        if key in s.index:
            return s.index[key]
        off = len(s.buf)
        u = text.encode('utf-16-le')
        s.buf += struct.pack('<H', len(u) // 2) + u
        s.buf += b'\0' * (-len(s.buf) % 4)
        s.index[key] = off
        if ja:
            TX.note(TX.D_QSTR, off, GLOB_SUB, text, ja)
        return off


def build(vfs, warn=None):
    """-> (sections, info, txja sections). sections is empty when the client has no quest table. Call TX.reset() first; the caller appends the TXJA sections."""
    warn = collections.Counter() if warn is None else warn
    sb = GlobBlob()
    qt = Q.quest_table(vfs, warn)
    qj = Q.quest_table(vfs, collections.Counter(), TX.JA)
    quests = []
    for qid in sorted(qt):
        nm, de, st_, en, tl = qt[qid]
        j = qj.get(qid, (None,) * 5)
        quests.append((qid, sb.add(nm, j[0]), sb.add(de, j[1]), sb.add(st_, j[2]), sb.add(en, j[3]), tl))
    items = []
    stb = Q._stb(vfs, '3DDATA/STB/LIST_QUESTITEM.STB')
    if stb is not None:
        keys = langs = None
        try:
            _t, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_QUESTITEM_S.STL'))
        except (KeyError, ValueError, struct.error, IndexError):
            pass
        for n in range(1, min(stb['rows'], 1000)):
            c = stb['cells'][n]
            if not c or not keys:
                continue
            try:
                icon = int(c[9].strip() or '0') if len(c) > 9 else 0
            except ValueError:
                icon = 0
            sid = c[-1].strip()
            if sid not in keys or len(langs) <= Q.STL_LANG:
                continue
            cells = TX.stl_cells((_t, keys, langs), sid, 0)
            nm, nj = TX.pick(cells, TX.EN), TX.pick(cells, TX.JA)
            if nm is None:
                continue
            items.append((13000 + n, max(0, min(icon, 0xFFFF)), sb.add(nm, nj)))
    info = dict(quests=len(quests), quest_items=len(items), bytes=0)
    if not quests and not items:
        return [], info, []
    data = Q.enc_qust(dict(quest=[Q.QUEST.pack(*x) for x in quests], qitem=[Q.QITEM.pack(c, ic, 0, s_) for c, ic, s_ in items]), bytes(sb.buf))
    info['bytes'] = len(data)
    return [(TAG_QGLB, 1, len(quests), data)], info, [x for x in TX.current().sections() if x[0] == TX.TAG_TXJA]
