"""RO dungeons lane (docs/design/ro_dungeons.md, w37): Payon Cave, Ant Hell, the Orc Dungeon and the three maps that lead into them.

One table (WORLD) = the lane's maps. Map ids of the lane are LOCAL (0..11 = index in WORLD); the world id = len(towns.NAMES) + len(culvert.NAMES) + local id (31..42, w39:
the culvert floors sit at 27..30; GsvrSceneBuilder.RoDungeonBase). GsvrRoMaps holds 64 maps in two-int masks (towns.MAX_MAPS), the lane is wired into the world in w39.

Classic PRE-RENEWAL rAthena (GPL-3.0) numbers only; labels are our own English names. Every map is an ordinary ro_conv map pack (RSM 1.4 models only:
2026-11-25 survey of the owner's iRO client, 0 RSM2 in all 12 maps - so no RSM2 parser is needed for this lane).

Entrances in rAthena (npc/warps): Payon Cave <- pay_arche 36,131 (payonwarp048, a town warp: this lane owns that row), Ant Hell <- cmd_fild08, the Orc Dungeon <-
in_orcs01 (the orc village, itself reached from gef_fild10). cmd_fild08 and gef_fild10 are far fields outside the towns lane's 27 maps, so two synthetic
`bridge#` warps (BRIDGES) join them to the nearest loaded towns maps (gef_fild00, moc_fild20); a later lane that owns the real roads replaces them.

Loading plan (the same on both sides: LDunResident in GsvrRoDungeon.cs == resident()): dungeon maps are NEVER fetched in the background. A map is resident
while the player is on `cur` when it is `cur` itself or one warp away from `cur` (+ map 0 = prontera, owned by the towns plan). Everything else is fetched ON DEMAND
by its warp behind the loading curtain (GsvrRoMaps WantStep, unchanged) and kept while the lane's RAM estimate is <= DUNGEON_BUDGET_MB, otherwise the least
recently used non-resident lane map is unloaded first (LDunEvictPick).
"""
import re

# (map, label, cluster, kind)
WORLD = (
    ('pay_dun00', 'Payon Cave 1', 'payoncave', 'dungeon'),
    ('pay_dun01', 'Payon Cave 2', 'payoncave', 'dungeon'),
    ('pay_dun02', 'Payon Cave 3', 'payoncave', 'dungeon'),
    ('pay_dun03', 'Payon Cave 4', 'payoncave', 'dungeon'),
    ('pay_dun04', 'Payon Cave 5', 'payoncave', 'dungeon'),
    ('cmd_fild08', 'Ant Hell approach', 'anthell', 'field'),
    ('anthell01', 'Ant Hell 1', 'anthell', 'dungeon'),
    ('anthell02', 'Ant Hell 2', 'anthell', 'dungeon'),
    ('gef_fild10', 'Orc Field', 'orcs', 'field'),
    ('in_orcs01', 'Orc Village', 'orcs', 'town'),
    ('orcsdun01', 'Orc Dungeon 1', 'orcs', 'dungeon'),
    ('orcsdun02', 'Orc Dungeon 2', 'orcs', 'dungeon'),
)
CLUSTERS = ('payoncave', 'anthell', 'orcs')
NAMES = tuple(r[0] for r in WORLD)
LABEL = dict((r[0], r[1]) for r in WORLD)
CLUSTER = dict((r[0], r[2]) for r in WORLD)
KIND = dict((r[0], r[3]) for r in WORLD)
# towns lane maps this lane's warps touch (the towns lane owns them; read-only here)
TOWN_ENDS = ('pay_arche', 'gef_fild00', 'moc_fild20')
DUNGEON_BUDGET_MB = 80                                  # RAM estimate (GsvrRpg1Loader.LZoneMB) the lane may keep loaded; peak resident set is 60.7 MB (on cmd_fild08: itself + both Ant Hell floors)
RAM_MB = {                                              # LZoneMB of the converted packs (owner's iRO client, ro_conv 3, 2026-11-25), x10 = tenths of a MB (int in Udon)
    'pay_dun00': 54, 'pay_dun01': 113, 'pay_dun02': 121, 'pay_dun03': 192, 'pay_dun04': 133,
    'cmd_fild08': 306, 'anthell01': 133, 'anthell02': 168,
    'gef_fild10': 287, 'in_orcs01': 35, 'orcsdun01': 78, 'orcsdun02': 66,
}

# synthetic joins (see module doc): (map, x, y, sx, sy, target, tx, ty, name); cells are walkable, outside every other span (tools/ro_dungeons_data.py check)
BRIDGES = (                                             # arrival = 3 cells beside the way back (outside its span 1,1: no ping-pong)
    ('gef_fild00', 366, 170, 1, 1, 'gef_fild10', 60, 203, 'bridge#orc1'),
    ('gef_fild10', 60, 200, 1, 1, 'gef_fild00', 366, 173, 'bridge#orc2'),
    ('moc_fild20', 200, 380, 1, 1, 'cmd_fild08', 196, 46, 'bridge#ant1'),
    ('cmd_fild08', 196, 43, 1, 1, 'moc_fild20', 200, 383, 'bridge#ant2'),
)
# the way into Payon Cave: a TOWN warp of rAthena (npc/warps/cities/payon.txt payonwarp048); the towns lane's table has no end in the lane so this lane's table owns it.
# ENTRANCES is the expected row (the generator reads it from payon.txt; test_ro_dungeons.py pins both)
ENTRANCES = (
    ('pay_arche', 36, 131, 2, 2, 'pay_dun00', 21, 183, 'payonwarp048'),
)
# pay_dun03 -> pay_dun04 is a random script in rAthena (npc/warps/dungeons/pay_dun.txt payd04r: 45 trigger at 127,62 area 2,2, rand(4) over four arrivals).
# The warp table holds the first arrival; the world picks one of PAYD04_ARRIVALS with a shared roll (GsvrRoDungeon.LPayd04Arrival).
PAYD04_TRIGGER = ('pay_dun03', 127, 62, 2, 2)
PAYD04_ARRIVALS = ((201, 204), (189, 43), (43, 40), (34, 202))


def bridge_rows():
    return BRIDGES


def payd04_rows():
    m, x, y, sx, sy = PAYD04_TRIGGER
    t = PAYD04_ARRIVALS[0]
    return ((m, x, y, sx, sy, 'pay_dun04', t[0], t[1], 'payd04r#0'),)


def parse_rows(text, names=None):
    """ro_warps.txt rows -> [(map, x, y, sx, sy, target, tx, ty, name)] (both ends in the lane maps or TOWN_ENDS when names is None)"""
    ok = set(NAMES) | set(TOWN_ENDS) if names is None else set(names)
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        f = ln.split()
        if len(f) < 8 or f[0] not in ok or f[5] not in ok:
            continue
        try:
            x, y, sx, sy, tx, ty = (int(f[i]) for i in (1, 2, 3, 4, 6, 7))
        except ValueError:
            continue
        out.append((f[0], x, y, sx, sy, f[5], tx, ty, f[8] if len(f) > 8 else ''))
    return out


def links(rows):
    g = {}
    for r in rows:
        if r[0] != r[5]:
            g.setdefault(r[0], set()).add(r[5])
    return g


# map -> the lane / town maps its warps lead to (== links(parse_rows(ro_dungeon_warps.txt)); test_ro_dungeons.py keeps the two equal)
LINKS = {
    'pay_dun00': ('pay_dun01', 'pay_arche'),
    'pay_dun01': ('pay_dun00', 'pay_dun02'),
    'pay_dun02': ('pay_dun01', 'pay_dun03'),
    'pay_dun03': ('pay_dun02', 'pay_dun04'),
    'pay_dun04': ('pay_dun03',),
    'cmd_fild08': ('anthell01', 'anthell02', 'moc_fild20'),
    'anthell01': ('cmd_fild08', 'anthell02'),
    'anthell02': ('cmd_fild08', 'anthell01'),
    'gef_fild10': ('in_orcs01', 'gef_fild00'),
    'in_orcs01': ('gef_fild10', 'orcsdun01', 'orcsdun02'),
    'orcsdun01': ('in_orcs01', 'orcsdun02'),
    'orcsdun02': ('in_orcs01', 'orcsdun01'),
    'pay_arche': ('pay_dun00',),                        # the lane's entrance only (the towns lane owns the rest of pay_arche's warps)
    'gef_fild00': ('gef_fild10',),
    'moc_fild20': ('cmd_fild08',),
}


def resident(cur, names=NAMES):
    """the lane maps kept loaded while the player is on `cur` (module doc): `cur` itself + every lane map one warp away from it (== GsvrRoDungeon.LDunResident)"""
    out = []
    if cur not in names:
        return out
    for m in names:
        if m == cur or m in LINKS.get(cur, ()):
            out.append(m)
    return out


def resident_mb10(cur):
    return sum(RAM_MB[m] for m in resident(cur))


def evict_pick(loaded, cur, lru, names=NAMES):
    """the lane map to unload first when the loaded set is over DUNGEON_BUDGET_MB: the least recently used (smallest lru stamp) loaded map that is not resident
    around `cur`; -1 when nothing may go (== GsvrRoDungeon.LDunEvictPick). loaded: set of map names, lru: {map: stamp}"""
    keep = set(resident(cur, names))
    best = None
    for m in names:
        if m in loaded and m not in keep and (best is None or lru.get(m, 0) < lru.get(best, 0)):
            best = m
    return best


def over_budget(loaded):
    return sum(RAM_MB[m] for m in loaded) > DUNGEON_BUDGET_MB * 10


def warm_after(name):
    """companion prebuild hook: the lane never prebuilds in the background (on demand only). Building the packs once is the companion's `ro=<map>` request path."""
    return []


# ───────────── MVP table (the bosses OF THESE dungeons only) ─────────────
# (mob id, name, map, x, y, delay1 ms, delay2 ms, label of the map, kind)  - npc/pre-re/mobs/dungeons/*.txt `boss_monster` rows (x,y 0,0 = anywhere on the map)
#   Moonlight Flower pay_dun04: 1 h + 0..10 min    Maya anthell02: 2 h + 0..10 min (rAthena: delay1 + rnd()%delay2 = the window)
# The Orc Dungeon has NO MVP in rAthena pre-re: Orc Hero (1087) is a boss of gef_fild02, Orc Lord (1190) of gef_fild10 (a FIELD of this lane's orcs cluster:
# MVP_FIELD below keeps its row, classic delay 2 h + 10 min, mvp flag 0 = it is a MINI-boss row there: `boss_monster ... ,0` = not an MVP kill reward).
MVPS = (
    (1150, 'Moonlight Flower', 'pay_dun04', 120, 115, 3600000, 600000),
    (1147, 'Maya', 'anthell02', 0, 0, 7200000, 600000),
)
# the field boss rows of this lane's maps that rAthena lists WITHOUT the MVP rewards (last field 0): kept as data, off by default
FIELD_BOSSES = (
    (1190, 'Orc Lord', 'gef_fild10', 0, 0, 7200000, 600000),
)
MVP_SLOTS = 4                                            # GsvrRoDungeon state arrays
