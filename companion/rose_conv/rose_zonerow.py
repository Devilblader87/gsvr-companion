"""ROSE w25 DROPS (companion-owned, like rose_npcskills.py / rose_music.py: the vendored converter files stay byte-identical to rose-vrchat): the per-ZONE rows of two
client tables that the vendored converter never reads, docs/design/rose_w25_drops.md.

1. ZONE DROP ROW. The original server (rose-offline rose-game-irose/src/data/drop_table.rs get_drop) rolls an item drop in the NPC's ITEM_DROP row only when a second
   1..100 roll is <= the NPC's item drop rate (LIST_NPC col 20); otherwise it rolls the ZONE's row (ITEM_DROP row = zone id). The vendored DROP section
   (rose_gameplay.py) carries the NPC rows only. This module appends ONE more record for the zone row:
       record with npcId 0 (no LIST_NPC row 0 is a monster, old runtimes never look it up) and dropRow = the zone id, same slots / pools layout;
       its item codes join the section's item table (rose_gameplay.item_records runs over every record).
   No zone row (empty row, zone id outside the table, the lobby pack) = the section is exactly the vendored one.
2. ZONE TRIGGERS. LIST_ZONE cells 22 / 23 / 24 name the zone's join / kill / dead QSD triggers (rose-offline zone_database.rs get_zone_join_trigger /
   get_zone_kill_trigger / get_zone_dead_trigger; the reference server reads but never runs them). The Forgotten Temple (56) uses them for its party entry
   2051-10 .. 2053-12 (team number + revive point of the party, the way out without a key), the PvP kill trophy 2051-13 and the key loss on death 2051-14.
   This module adds them to the zone's QUST section as EVENT table entries with fixed ids (EV_JOIN / EV_KILL / EV_DEAD below; IFO event object indices are small,
   so an old runtime never runs them) and the vendored closure pulls in their chains (next on failure, trigger rewards).

`install()` wraps rose_gameplay.drop_rows and rose_quest.build_section (rose_pack calls both through the module attribute). `ZONE` is set by rose_conv._zone_bytes before
the conversion (None for the lobby pack: no zone rows there). No number or text of the game is committed: everything is read from the player's own client.
"""
import collections

import rose_gameplay as G
import rose_quest as RQ
import rosefmt as F

ZONE = None                 # zone id of the pack being built (rose_conv._zone_bytes); None = no zone rows
ZONE_DROP_NPC = 0           # npcId of the zone row record (GsvrRpgRose.ZoneDropNpc)
EV_JOIN, EV_KILL, EV_DEAD = 1000001, 1000002, 1000003       # QUST event ids of the zone triggers (GsvrRoseQuest.EvZoneJoin / EvZoneKill / EvZoneDead)
ZONE_TRIGGER_CELLS = ((22, EV_JOIN), (23, EV_KILL), (24, EV_DEAD))
_orig_rows = None
_orig_quest = None


# ------------------------------------------------------------------ 1. zone drop row
def zone_drop_record(stb, zone):
    """ITEM_DROP row `zone` -> the DROP record dict of rose_gameplay (npc = ZONE_DROP_NPC), None when the zone has no row or the row yields nothing"""
    if stb is None or zone is None or not (0 < zone < stb['rows']):
        return None
    slots, pools = G.drop_layout(stb, zone)
    if not G.drop_codes(slots, pools):
        return None
    return dict(npc=ZONE_DROP_NPC, drop_row=zone, slots=slots, pools=pools)


def drop_rows_with_zone(orig, vfs, side_actors, monsters=None):
    """rose_gameplay.drop_rows + the zone row record behind the NPC records (only when the zone has monsters with drops or a zone row; the lobby: ZONE None)"""
    rows, stb = orig(vfs, side_actors, monsters)
    rec = zone_drop_record(stb, ZONE)
    if rec is not None and (rows or monsters):
        rows = list(rows) + [rec]
    return rows, stb


# ------------------------------------------------------------------ 2. zone triggers
def zone_trigger_names(vfs, zone):
    """LIST_ZONE row `zone` -> [(event id, trigger name)] for the non-empty join / kill / dead cells"""
    if zone is None:
        return []
    try:
        s = F.stb(vfs.read('3DDATA/STB/LIST_ZONE.STB'))
    except KeyError:
        return []
    if not (0 <= zone < s['rows']):
        return []
    c = s['cells'][zone]
    out = []
    for cell, ev in ZONE_TRIGGER_CELLS:
        nm = c[cell].strip() if len(c) > cell else ''
        if nm:
            out.append((ev, nm))
    return out


def _retag_events(sec, n_ifo_events, extra):
    """QUST section bytes: event entries at index n_ifo_events + k (the appended zone trigger names) get the fixed id of extra[k]. Every other byte is kept."""
    dec = RQ.dec_qust(sec)
    ev = []
    for e, t in dec['tables']['event']:
        k = e - n_ifo_events
        ev.append((extra[k][0], t) if 0 <= k < len(extra) else (e, t))
    dec['tables']['event'] = ev
    tables = {name: [RQ.STRUCTS[name].pack(*rec) for rec in recs] for name, recs in dec['tables'].items()}
    return RQ.enc_qust(tables, dec['strings'])


def build_section_with_zone(orig, vfs, ifos, monster_ids, warn=None):
    """rose_quest.build_section + the zone's join / kill / dead triggers as event entries (EV_JOIN / EV_KILL / EV_DEAD)"""
    extra = zone_trigger_names(vfs, ZONE)
    if not extra:
        return orig(vfs, ifos, monster_ids, warn)
    zn = RQ._zone_npcs
    n_ifo = len(zn(ifos)[1])

    def zone_npcs(i):
        npcs, events = zn(i)
        return npcs, list(events) + [nm for _ev, nm in extra]
    RQ._zone_npcs = zone_npcs
    try:
        secs, info = orig(vfs, ifos, monster_ids, warn if warn is not None else collections.Counter())
    finally:
        RQ._zone_npcs = zn
    out = []
    for tag, ver, cnt, data in secs:
        if tag == RQ.TAG_QUST:
            data = _retag_events(data, n_ifo, extra)
        out.append((tag, ver, cnt, data))
    info = dict(info, zone_triggers=[nm for _ev, nm in extra])
    return out, info


def install():
    """wraps rose_gameplay.drop_rows and rose_quest.build_section once (idempotent)."""
    global _orig_rows, _orig_quest
    if _orig_rows is None:
        _orig_rows = G.drop_rows
        o = _orig_rows

        def drop_rows(vfs, side_actors, monsters=None):
            return drop_rows_with_zone(o, vfs, side_actors, monsters)
        drop_rows.__doc__ = drop_rows_with_zone.__doc__
        G.drop_rows = drop_rows
    if _orig_quest is None:
        _orig_quest = RQ.build_section
        q = _orig_quest

        def build_section(vfs, ifos, monster_ids, warn=None):
            return build_section_with_zone(q, vfs, ifos, monster_ids, warn)
        build_section.__doc__ = build_section_with_zone.__doc__
        RQ.build_section = build_section
