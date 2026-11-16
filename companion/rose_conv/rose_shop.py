"""NPC shops for the Unity runtime (docs/RPG1_PACK.md section 14): section SHOP, optional and additive.

The runtime (GsvrRoseShop) shows the shop window of an NPC (tabs, item grid) and prices buying / selling with the original formulas (rose-offline ability_values.rs and
npc_store_system.rs, re-implemented). Everything it needs is in this section, so it does not depend on ITEM (materials, which ITEM does not hold, can be priced too).

Source tables (read only, from the player's own client; column numbers = rosefmt.stb cell indices, the same numbers rose-offline uses):
  LIST_NPC cell 20 (store union number, 0 = none), cells 21..24 (store tab ids, 0 = no tab) ; LIST_SELL row = tab id: cell 1 tab name key (LIST_SELL_S.STL), cells 2.. item codes
  (base1000, 0 = empty slot, the column still counts as a slot) ; item tables LIST_FACEITEM .. LIST_USEITEM, LIST_JEMITEM, LIST_NATURAL, LIST_QUESTITEM (types 1..13):
  cell 4 class, 5 base price, 6 price rate, 8 quality.

Layout (little endian, 4-byte aligned):
  header 64: u32 npcCount ; u32 npcStride (24) ; u32 npcOff ; u32 tabCount ; u32 tabStride (16) ; u32 tabOff ; u32 slotCount ; u32 slotOff ; u32 priceCount ; u32 priceOff ;
             u32 namesOff ; u32 namesBytes ; u32 worldRate ; u32 itemRate ; u32 townRate ; u32 0
  npc record 24 (sorted by npc id): u32 npcId ; u32 unionNumber ; i32 tab[4] (index into the tab table, -1 = none)
  tab record 16: u32 slotStart ; u32 slotN ; u32 nameOff ; u16 nameLen (UTF-8 in the names blob) ; u16 0
  slots: u32 item code per column (0 = empty slot)
  price record 16 (sorted by code): u32 code ; u32 basePrice ; u16 priceRate ; u16 class ; u16 quality ; u16 0
  rates: world / item / town price rate of the server defaults (rose-offline world_rates.rs: 100 / 50 / 100).
No text or table of the game is committed; everything is read at conversion time on the player's machine.
"""
import struct

import rosefmt as F
import rose_text as TX

TAG_SHOP = b'SHOP'
HDR = struct.Struct('<16I')
NPC_REC = struct.Struct('<II4i')
TAB_REC = struct.Struct('<IIIHH')
PRICE_REC = struct.Struct('<IIHHHH')
MAX_NPCS = 4096
MAX_TABS = 65536
MAX_SLOTS = 1 << 20
MAX_PRICES = 1 << 17
DEFAULT_RATES = (100, 50, 100)      # world, item, town
TABLES = {1: 'LIST_FACEITEM', 2: 'LIST_CAP', 3: 'LIST_BODY', 4: 'LIST_ARMS', 5: 'LIST_FOOT', 6: 'LIST_BACK', 7: 'LIST_JEWEL', 8: 'LIST_WEAPON', 9: 'LIST_SUBWPN',
          10: 'LIST_USEITEM', 11: 'LIST_JEMITEM', 12: 'LIST_NATURAL', 13: 'LIST_QUESTITEM'}
# item classes priced with the item rate instead of the town rate (rose-offline ItemClass: medicine, food, metals, materials, chemicals); the runtime has the same list
ITEM_RATE_CLASSES = (311, 312, 421, 422, 423, 424, 425, 426, 427, 428)
assert HDR.size == 64 and NPC_REC.size == 24 and TAB_REC.size == 16 and PRICE_REC.size == 16


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
    except IndexError:
        return default
    return int(v) if v.lstrip('-').isdigit() else default


def _u16(v):
    return max(0, min(65535, v))


def enc_shop(npcs, tabs, prices, rates=DEFAULT_RATES):
    """npcs: [dict(npc, union, tabs[4])] (tabs = indices into `tabs`, -1 none) ; tabs: [dict(name, slots[codes])] ; prices: [dict(code, base, rate, cls, quality)]"""
    npcs = sorted(npcs, key=lambda n: n['npc'])
    prices = sorted(prices, key=lambda p: p['code'])
    names = bytearray()
    slots = []
    trecs = []
    for t in tabs:
        nm = t['name'].encode('utf-8')[:65535]
        trecs.append(TAB_REC.pack(len(slots), len(t['slots']), len(names), len(nm), 0))
        names += nm
        slots += list(t['slots'])
    body_npc = b''.join(NPC_REC.pack(n['npc'], n['union'], *n['tabs']) for n in npcs)
    body_tab = b''.join(trecs)
    body_slot = b''.join(struct.pack('<I', s) for s in slots)
    body_price = b''.join(PRICE_REC.pack(p['code'], p['base'], _u16(p['rate']), _u16(p['cls']), _u16(p['quality']), 0) for p in prices)
    pad = b'\0' * (-len(names) % 4)
    o = HDR.size
    o_tab = o + len(body_npc)
    o_slot = o_tab + len(body_tab)
    o_price = o_slot + len(body_slot)
    o_names = o_price + len(body_price)
    hdr = HDR.pack(len(npcs), NPC_REC.size, o, len(tabs), TAB_REC.size, o_tab, len(slots), o_slot, len(prices), o_price, o_names, len(names), rates[0], rates[1], rates[2], 0)
    return hdr + body_npc + body_tab + body_slot + body_price + bytes(names) + pad


def dec_shop(d):
    """-> dict(npcs, tabs, slots, prices, rates, names); raises ValueError when the bounds do not hold (same checks as GsvrRoseShop.Valid)"""
    if len(d) < HDR.size:
        raise ValueError('short')
    nc, ns, no, tc, ts, to, sc, so, pc, po, nmo, nmb, wr, ir, tr, _z = HDR.unpack_from(d, 0)
    if ns != NPC_REC.size or ts != TAB_REC.size or nc > MAX_NPCS or tc > MAX_TABS or sc > MAX_SLOTS or pc > MAX_PRICES:
        raise ValueError('bad header')
    if no + nc * ns > len(d) or to + tc * ts > len(d) or so + sc * 4 > len(d) or po + pc * 16 > len(d) or nmo + nmb > len(d):
        raise ValueError('out of bounds')
    npcs = []
    for i in range(nc):
        v = NPC_REC.unpack_from(d, no + i * ns)
        npcs.append(dict(npc=v[0], union=v[1], tabs=list(v[2:])))
    tabs = []
    for i in range(tc):
        v = TAB_REC.unpack_from(d, to + i * ts)
        if v[0] + v[1] > sc or v[2] + v[3] > nmb:
            raise ValueError('tab out of bounds')
        tabs.append(dict(start=v[0], n=v[1], name=bytes(d[nmo + v[2]:nmo + v[2] + v[3]]).decode('utf-8')))
    slots = list(struct.unpack_from('<%dI' % sc, d, so))
    prices = []
    for i in range(pc):
        v = PRICE_REC.unpack_from(d, po + i * 16)
        prices.append(dict(code=v[0], base=v[1], rate=v[2], cls=v[3], quality=v[4]))
    for n in npcs:
        for t in n['tabs']:
            if t < -1 or t >= tc:
                raise ValueError('npc tab index')
    return dict(npcs=npcs, tabs=tabs, slots=slots, prices=prices, rates=(wr, ir, tr), names=nmb)


def _stb(vfs, name):
    try:
        return F.stb(vfs.read('3DDATA/STB/%s.STB' % name))
    except KeyError:
        return None


def tab_name(sell_s, sid, lang=TX.EN):
    """tab name from LIST_SELL_S.STL (STL language `lang`: 1 English, 2 Japanese), '' when unknown"""
    if not sell_s or not sid:
        return ''
    _t, keys, langs = sell_s
    if sid not in keys or len(langs) < 2:
        return ''
    try:
        return TX.stl_text(sell_s, sid, 0, lang).strip()
    except IndexError:
        return ''


def price_rows(tables):
    """tables: {type: stb} -> price dicts (item number < 1000; rows with no base price and no content are skipped)"""
    out = []
    for t, stb in sorted(tables.items()):
        for n in range(1, min(stb['rows'], 1000)):
            c = stb['cells'][n]
            base = _int(c, 5)
            if base <= 0 and not any(str(x).strip() for x in c[:12]):
                continue
            out.append(dict(code=t * 1000 + n, base=max(0, base), rate=_int(c, 6), cls=_int(c, 4), quality=_int(c, 8)))
    return out


def build_shops(npc_stb, sell_stb, sell_s, npc_ids):
    """-> (npcs, tabs): shops of the NPC ids that have at least one non-empty tab"""
    npcs, tabs, tab_of = [], [], {}
    for nid in sorted(npc_ids):
        if not (0 < nid < npc_stb['rows']):
            continue
        c = npc_stb['cells'][nid]
        refs = []
        for k in range(4):
            tid = _int(c, 21 + k)
            if not (0 < tid < sell_stb['rows']):
                refs.append(-1)
                continue
            if tid not in tab_of:
                row = sell_stb['cells'][tid]
                codes = [max(0, _int(row, i)) for i in range(2, len(row))]
                while codes and codes[-1] == 0:
                    codes.pop()
                if not any(codes):
                    tab_of[tid] = -1
                else:
                    tab_of[tid] = len(tabs)
                    sid_ = str(row[1]).strip() if len(row) > 1 else ''
                    tabs.append(dict(name=tab_name(sell_s, sid_), slots=codes))
                    TX.note(TX.D_SHOPTAB, len(tabs) - 1, 0, tabs[-1]['name'], tab_name(sell_s, sid_, TX.JA))      # key = the tab's index in the SHOP section
            refs.append(tab_of[tid])
        if any(r >= 0 for r in refs):
            npcs.append(dict(npc=nid, union=max(0, _int(c, 20)), tabs=refs))
    return npcs, tabs


def build_section(vfs, npc_ids):
    """-> (sections, warnings). Never raises on a missing table: no LIST_NPC / LIST_SELL or no shop NPC = no section."""
    warn = []
    npc_stb = _stb(vfs, 'LIST_NPC')
    sell = _stb(vfs, 'LIST_SELL')
    if npc_stb is None or sell is None:
        return [], ['SHOP: LIST_NPC.STB or LIST_SELL.STB missing']
    sell_s = None
    try:
        sell_s = F.stl(vfs.read('3DDATA/STB/LIST_SELL_S.STL'))
    except Exception:
        warn.append('SHOP: LIST_SELL_S.STL missing (tabs have no names)')
    npcs, tabs = build_shops(npc_stb, sell, sell_s, npc_ids)
    if not npcs:
        return [], warn + ['SHOP: no shop NPC in the window']
    tables = {}
    for t, nm in sorted(TABLES.items()):
        stb = _stb(vfs, nm)
        if stb is None:
            warn.append('SHOP: %s.STB missing' % nm)
        else:
            tables[t] = stb
    prices = price_rows(tables)
    seen = set(p['code'] for p in prices)
    miss = set(s for t in tabs for s in t['slots'] if s and s not in seen)
    if miss:
        warn.append('SHOP: %d shop items without a price row (not buyable)' % len(miss))
    return [(TAG_SHOP, 1, len(npcs), enc_shop(npcs, tabs, prices))], warn
