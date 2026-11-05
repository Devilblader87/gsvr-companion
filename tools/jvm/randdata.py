"""W9R2-1: the random neutral picks and the small per-widget fields (docs/NOTES_units.md "ChooseRandomCreep").
Udon twin: W3NatRand.cs (impl ids 1340-1349). Clean room: behaviour from the audited maps' scripts + the base data;
tags [obs] / [inf] / [ext] as in the other NOTES.

  ChooseRandomCreep(level)  candidates = the run's unit types (install data + the map's own object data) with UnitData
                            race creeps (8), UnitBalance isbldg 0 and level == arg (-1 = every level), in table order;
                            one integer below(len) on game stream 0x23 (the stream ChooseRandomItem uses) [inf: the
                            exact filter and the stream need the oracle probe / the game.dll body]. Empty set: 0, no draw.
  ChooseRandomNPBuilding()  candidates = unit types with UnitBalance nbrandom == 1 (29 types in the base data) [obs data].
  Set/GetItemUserData       an integer per item, default 0 (Item.udata).
  Set/IsItemInvulnerable    a bool per item, default false; no damage path to items exists in the engine [inf].
  GetHeroProperName(hero)   the type's Propernames list (UnitStrings.txt; the map's w3u 'upro' wins), entry = the hero's
                            per-owner / per-type creation counter, wrapping at the end of the list [inf: the first hero
                            of a type gets entry 0]; '' when the type has no names.
"""
import re

RACE_CREEPS = 8


def _raw(tid):
    """the integer type id of a unit-types key (rawcode ints)."""
    return tid if tid < 0x80000000 else tid - 0x100000000


def creep_pool(unit_types, level):
    """ChooseRandomCreep candidates (type ids) in table order."""
    out = []
    for tid, r in unit_types.items():
        if int(r.get('race', 0) or 0) != RACE_CREEPS or int(r.get('bldg', 0) or 0) != 0:
            continue
        lv = int(r.get('level', 0) or 0)
        if lv != level and level != -1:
            continue
        out.append(tid)
    return out


def np_pool(unit_types):
    """ChooseRandomNPBuilding candidates in table order."""
    return [tid for tid, r in unit_types.items() if int(r.get('nbrandom', 0) or 0) == 1]


def _pick(N, pool):
    if not pool:
        return 0
    i = N.streams.game[0x23].below(len(pool))
    return pool[i]


def choose_random_creep(N, level):
    return _pick(N, creep_pool(N.unit_types, level))


def choose_random_np_building(N):
    return _pick(N, np_pool(N.unit_types))


def set_item_user_data(it, v):
    if it is not None:
        it.udata = v


def get_item_user_data(it):
    return getattr(it, 'udata', 0) if it is not None else 0


def set_item_invulnerable(it, flag):
    if it is not None:
        it.invul = bool(flag)


def is_item_invulnerable(it):
    return bool(getattr(it, 'invul', False)) if it is not None else False


def proper_names(rec):
    """the type record's Propernames as a list ('' / missing -> [])."""
    s = rec.get('propernames') or ''
    return [p.strip() for p in s.split(',') if p.strip()] if isinstance(s, str) else []


def assign_name(counters, owner_id, tid, rec):
    """a hero is created: its entry index in the type's list (counter per owner and type), None without names."""
    names = proper_names(rec)
    if not names:
        return None
    k = (owner_id, tid)
    n = counters.get(k, 0)
    counters[k] = n + 1
    return n % len(names)


def hero_proper_name(u):
    idx = getattr(u, 'pname', None) if u is not None else None
    if idx is None:
        return ''
    names = proper_names(u.data)
    return names[idx] if idx < len(names) else ''
