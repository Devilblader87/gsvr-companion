#!/usr/bin/env python3
"""Item type records for the W8 engine (docs/NOTES_items.md §1; clean-room: values only, read at run time).

Sources (bring your own game: the player's install / the map, never stored in the repo):
  Units\\ItemData.slk        base rows in file order (TFT maps: every version, NOTES_items §1.1)
  Units\\ItemStrings.txt     display names (command UI)
  war3map.w3t               modified base items (values change, position stays) + custom items appended in file order

A record: {'id4', 'Level', 'oldLevel', 'cls' (item type 0…7, NOTES_items §1.2 incl. the 'miscellanous' quirk),
'pickRandom', 'powerup', 'usable', 'perishable', 'droppable', 'drop', 'sellable', 'pawnable', 'uses' (0 = no
charges), 'HP', 'goldcost', 'lumbercost', 'ignoreCD', 'stockMax', 'stockRegen', 'stockStart', 'cooldownID' (4-char
ability id or ''), 'abilList' (comma string), 'name'}.
"""
NUM = ('Level', 'oldLevel', 'pickRandom', 'powerup', 'usable', 'perishable', 'droppable', 'drop', 'sellable',
       'pawnable', 'uses', 'HP', 'goldcost', 'lumbercost', 'ignoreCD', 'stockMax', 'stockRegen', 'stockStart')

# The item's own flag word (CItem_p1 §4: `+0x20`, game mask 0x3fc8). The port keeps the same bits as the Udon
# `itFlags` word (W3Items.cs); every value below is set from the type record at creation and can be overridden per
# item through the SetItem* natives. INVUL (0x8), WAS_OWNED (0x100) and NO_CHARGES (0x1000) are represented by the
# item's own fields (`Item.life` / `Item.owner` / `Item.uncharged`) instead.
FLAG_POWERUP = 1
FLAG_SELLABLE = 2
FLAG_PAWNABLE = 4
FLAG_USABLE = 8
FLAG_PERISHABLE = 16
FLAG_DROPPABLE = 32
FLAG_DROP = 64
FLAG_IGNORECD = 128
# PORT8-Widget: the game's *hidden* bit is +0x20 bit 0x1 (CItem_p1 §4 / Uncl. 0022 §7), outside the 0x3fc8 gameplay
# mask. The port's word is its own encoding (bit 0 above is "powerup"), so the visibility state gets its own bit here.
FLAG_HIDDEN = 256
FLAG_KEYS = (('powerup', FLAG_POWERUP), ('sellable', FLAG_SELLABLE), ('pawnable', FLAG_PAWNABLE),
             ('usable', FLAG_USABLE), ('perishable', FLAG_PERISHABLE), ('droppable', FLAG_DROPPABLE),
             ('drop', FLAG_DROP), ('ignoreCD', FLAG_IGNORECD))


def record_flags(rec):
    """-> the item flag word from a type record (the initial value of the instance's word, CItem_p1 §4)."""
    f = 0
    for k, b in FLAG_KEYS:
        if rec.get(k):
            f |= b
    return f


# class string -> item type; the game compares with 'miscellanous' (sic) so "Miscellaneous" items are type 7 [obs]
CLASSES = {'permanent': 0, 'charged': 1, 'powerup': 2, 'artifact': 3, 'purchasable': 4, 'campaign': 5,
           'miscellanous': 6}
ITEM_TYPE_UNKNOWN = 7
SCHEMA = 2                                          # bump when a record field is added (world.item_base cache)


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip('"'))
    except (TypeError, ValueError):
        return 0.0                                  # '-', '_', '' (uses '-' = no charges)


def _cls(v):
    return CLASSES.get(str(v or '').strip('"').lower(), ITEM_TYPE_UNKNOWN)


def norm_abil_list(v):
    """CAbilityItemRandomItem_p1 §abilities [obs 0x6f1dc090 / 0x6f2b91d0]: the item's ability list is tokenised on space, comma, hyphen,
    underscore and tab; empty tokens are skipped. -> the tokens joined with ',' (the form every reader splits)."""
    import re
    return ','.join(t for t in re.split(r'[ ,\-_\t]', str(v or '').strip('"')) if t)


def base_items(read):
    """read(name) -> bytes. -> {'items': [record] in SLK order, 'meta': {w3t field id: column}} (JSON-able)."""
    import w3slk as slk
    from .unitdata import parse_profile
    _h, rows = slk.parse(read('Units\\ItemData.slk').decode('latin-1'))
    strs = {}
    try:
        raw = read('Units\\ItemStrings.txt')
    except KeyError:
        raw = None
    if raw:
        parse_profile(raw.decode('latin-1'), strs)
    items = []
    seen = set()
    for r in rows:
        iid = r.get('itemID')
        if not isinstance(iid, str) or len(iid) != 4 or iid in seen:
            continue                                # a duplicate id is not added twice [obs]
        seen.add(iid)
        rec = {'id4': iid, 'cls': _cls(r.get('class')), 'abilList': norm_abil_list(r.get('abilList')),
               'cooldownID': _cd(r.get('cooldownID')),
               'name': str(strs.get(iid, {}).get('Name', '')).strip('"')}
        for k in NUM:
            rec[k] = _num(r.get(k))
        items.append(rec)
    meta = {}
    _h, mrows = slk.parse(read('Units\\UnitMetaData.slk').decode('latin-1'))
    for r in mrows:
        if r.get('slk') == 'ItemData' and isinstance(r.get('ID'), str):
            meta[r['ID']] = r.get('field')
    # the item name is a Profile field of the shared meta ('unam', useItem) [ext data]
    for r in mrows:
        if r.get('field') == 'Name' and r.get('slk') == 'Profile' and r.get('useItem') in (1, '1'):
            meta[r['ID']] = 'Name'
    return {'items': items, 'meta': meta, 'schema': SCHEMA}


def _cd(v):
    v = str(v or '').strip('"')
    return v if len(v) == 4 else ''                 # the item cooldown group (an ability id; '' = none) [ext data]


def _apply(rec, field, val, wts):
    if field == 'cooldownID':
        rec['cooldownID'] = _cd(val)
    elif field == 'class':
        rec['cls'] = _cls(val)
    elif field == 'abilList':
        rec['abilList'] = norm_abil_list(val)
    elif field == 'Name':
        import w3map
        rec['name'] = w3map.resolve(val, wts) if isinstance(val, str) else rec.get('name', '')
    elif field in NUM:
        rec[field] = _num(val)


def merge(base, w3t, wts=None):
    """base_items() + war3map.w3t bytes -> [record]: base rows (modified in place), then custom items in file order."""
    out = [dict(r) for r in base['items']]
    by_id = {r['id4']: r for r in out}
    std = {r['id4']: dict(r) for r in out}         # BUGS OD-1: custom items start from the standard record
    if w3t:
        import w3map
        d = w3map.parse_objects(w3t, '.w3t')
        meta = base['meta']
        for o in d['original_modified']:
            rec = by_id.get(o['orig'])
            if rec is None:
                continue
            for mid, _lvl, val in o['mods']:
                f = meta.get(mid)
                if f:
                    _apply(rec, f, val, wts)
        for o in d['custom']:
            src = std.get(o['orig'])
            if src is None or o['new'] in by_id:
                continue
            rec = dict(src)
            rec['id4'] = o['new']
            for mid, _lvl, val in o['mods']:
                f = meta.get(mid)
                if f:
                    _apply(rec, f, val, wts)
            out.append(rec)
            by_id[rec['id4']] = rec
    return out


def random_pool(items, itype, level):
    """ChooseRandomItem(Ex) candidates in list order (NOTES_items §2): type 7 → any class + oldLevel; type 8 → any
    class + Level; else class == type + Level; level −1 = any level; pickRandom ≠ 0."""
    out = []
    for r in items:
        if not r['pickRandom']:
            continue
        if itype not in (7, 8) and r['cls'] != itype:
            continue
        if level != -1:
            lv = r['oldLevel'] if itype == 7 else r['Level']
            if int(lv) != level:
                continue
        out.append(r['id4'])
    return out
