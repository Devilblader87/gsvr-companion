"""RO classic maps, third wave part 1 (docs/design/ro_maps3.md, w53 lane "dungeons3"): Byalan Island + the Undersea Tunnel, the Sunken Ship and the Geffen Dungeon (levels 20 - 60).

One table (WORLD) = the lane's 13 maps in world id order: world id = MAPS3_BASE + index (62..74, right after the 12 maps of the second wave). The world holds up to 256 maps (w52 mapcap).
Every map is an ordinary ro_conv map pack (no new converter code), ON DEMAND (cluster -2, the w39 rule): never fetched in the background, a warp fetches it behind the loading curtain, resident
while the player is on one of them or one warp away, kept under the on-demand RAM budget. Music + fog per map come from the converter (RATM + snd_<map> for every map of PACKS).

Classic PRE-RENEWAL rAthena (GPL-3.0) numbers only; labels are our own English names.

Entrances in rAthena and what this lane does about them (all three start with an NPC or on a map that is not loaded):
  Byalan Island  <- the Izlude sailor (NPC, 150 Zeny, warps to izlu2dun 107,50); back: Sailor#2izlude (izlu2dun 108,27 -> izlude).   A `bridge#byl` pair replaces both NPCs (no fare).
  Sunken Ship    <- Alberta's Fisk (NPC, 250 Zeny, warps to alb2trea 43,53); back: Fisk#a2t (alb2trea 39,50 -> alberta 192,169).     A `bridge#sun` pair replaces both NPCs.
  Geffen Dungeon <- geffen 120,114 -> gef_tower 52,177, then gef_tower 153,28 -> gef_dun00 104,99 (gef_tower is not loaded): one `bridge#gef` pair joins geffen to gef_dun00 on the real
                    arrival cell of the dungeon side (gef_dun00 104,99; its own way back span 104,103 3,3 is rAthena's).
  gef_dun03 is an EVENT map in rAthena: the warp gef_dun02 -> gef_dun03 is commented out there (npc/pre-re/warps/dungeons/gef_dun.txt "Removed the warp from gef_dun02 to gef_dun03"); only the
  way back (gefd03-1) exists. This lane keeps it exactly so: the map is registered, nobody walks in (only a test / QA jump reaches it).
The bridges are placeholders (tools/ro_maps3_gen.py check: walkable, outside every span); a lane that owns the NPC ferries replaces them.
"""
import re

MAPS3_BASE = 62

# (map, label, area, kind)
WORLD = (
    ('izlu2dun', 'Byalan Island', 'byalan', 'town'),
    ('iz_dun00', 'Undersea Tunnel 1', 'byalan', 'dungeon'),
    ('iz_dun01', 'Undersea Tunnel 2', 'byalan', 'dungeon'),
    ('iz_dun02', 'Undersea Tunnel 3', 'byalan', 'dungeon'),
    ('iz_dun03', 'Undersea Tunnel 4', 'byalan', 'dungeon'),
    ('iz_dun04', 'Undersea Tunnel 5', 'byalan', 'dungeon'),
    ('alb2trea', 'Sunken Ship dock', 'sunken', 'town'),
    ('treasure01', 'Sunken Ship 1', 'sunken', 'dungeon'),
    ('treasure02', 'Sunken Ship 2', 'sunken', 'dungeon'),
    ('gef_dun00', 'Geffen Dungeon 1', 'gefdun', 'dungeon'),
    ('gef_dun01', 'Geffen Dungeon 2', 'gefdun', 'dungeon'),
    ('gef_dun02', 'Geffen Dungeon 3', 'gefdun', 'dungeon'),
    ('gef_dun03', 'Geffen Dungeon 4', 'gefdun', 'dungeon'),
)
AREAS = ('byalan', 'sunken', 'gefdun')
NAMES = tuple(r[0] for r in WORLD)
LABEL = dict((r[0], r[1]) for r in WORLD)
AREA = dict((r[0], r[2]) for r in WORLD)
KIND = dict((r[0], r[3]) for r in WORLD)
# loaded maps of earlier lanes this lane's bridges touch (read only here)
TOWN_ENDS = ('izlude', 'alberta', 'geffen')
# maps without rAthena monsters (the Byalan landing has the Kafra + sailor only, the Sunken Ship dock the Kafra + tool dealer)
NO_MOBS = ('izlu2dun', 'alb2trea')
# the two boss rows of this lane that are MVPs (Drake treasure02, Doppelganger gef_dun02) + Dracula of gef_dun01 (MvpExp > 0 too: the w52 rule books every MVP of a loaded map)
MVPS = ((1112, 'treasure02'), (1046, 'gef_dun02'), (1389, 'gef_dun01'))

# synthetic joins (module doc): (map, x, y, sx, sy, target, tx, ty, name); cells are walkable, arrivals lie outside every span (tools/ro_maps3_gen.py check)
BRIDGES = (
    ('izlude', 197, 207, 1, 1, 'izlu2dun', 107, 50, 'bridge#byl1'),            # the Izlude sailor (renewal Izlude of the owner's client: npc cell 197,205); arrival = rAthena's own cell of the sailor's warp
    ('izlu2dun', 108, 29, 1, 1, 'izlude', 197, 210, 'bridge#byl2'),             # Sailor#2izlude stands at 108,27; the way back lands on the dock (the renewal cell of the same NPC script)
    ('alberta', 189, 153, 1, 1, 'alb2trea', 43, 53, 'bridge#sun1'),             # Fisk (alberta 189,151) -> alb2trea 43,53
    ('alb2trea', 39, 52, 1, 1, 'alberta', 192, 169, 'bridge#sun2'),             # Fisk#a2t (alb2trea 39,50) -> alberta 192,169
    ('geffen', 120, 114, 3, 1, 'gef_dun00', 104, 99, 'bridge#gef1'),            # rAthena gef002 (geffen 120,114 3,1 -> gef_tower 52,177) + gef005 (gef_tower -> gef_dun00 104,99) as one hop
    ('gef_dun00', 104, 103, 3, 3, 'geffen', 120, 110, 'bridge#gef2'),           # rAthena gef024 (the way to gef_tower) lands on the Geffen side of gef002
)
# the three chains of the client scenario RO:romaps3 (GsvrRoTest.LChainArea3): 4 stops, 3 hops each (the way into treasure02 crosses ten same-map warps of the ship: the GAT walker cannot take them)
CHAINS = (('izlude', 'izlu2dun', 'iz_dun00', 'iz_dun01'), ('alberta', 'alb2trea', 'treasure01', 'alb2trea'), ('geffen', 'gef_dun00', 'gef_dun01', 'gef_dun02'))
ONDEMAND_BUDGET_MB = 120          # GsvrRoMaps.onDemandBudgetMB: unchanged by this lane (the largest resident set is checked in test_ro_maps3.py / romaps3rules.cs)

from .maps3_data import LINKS, MEASURED   # noqa: E402  GENERATED by tools/ro_maps3_gen.py: links per map, (terrain blocks, object chunks, LZoneMB x10) of the converted packs


def resident(cur, names=NAMES):
    """the lane maps kept loaded while the player is on `cur`: `cur` itself + every lane map one warp away from it (the w39 rule == GsvrRoMaps.LResident for on-demand maps)"""
    out = []
    if cur not in names:
        return out
    for m in names:
        if m == cur or m in LINKS.get(cur, ()):
            out.append(m)
    return out


def world_id(name):
    return MAPS3_BASE + NAMES.index(name)
