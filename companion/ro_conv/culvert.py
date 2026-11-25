"""RO Prontera Culvert (w35 lane culvert, rebased in w39 onto the towns layout; docs/design/ro_culvert.md): the four floors prt_sewb1..prt_sewb4.

World ids 27..30 = right after the towns maps (`towns.NAMES`, ids 0..26) and before the dungeons lane (ids 31..42): api.MAP_ORDER = towns + culvert + dungeons.
Loading = the towns residency of GsvrRoMaps (docs/design/ro_towns.md) with cluster ON_DEMAND (GsvrRoMaps.ClusterOnDemand = -2): a floor is resident only on itself or next to another
on-demand map one warp away; the entrance from prt_fild05 is a town warp, so B1 is fetched behind the loading curtain (or earlier by the registry's hint, GsvrRoMaps.LHintMap).
The companion still prebuilds the floors in the background after the town maps (1.4 - 11 s each, cached on disk).
"""
WORLD = (
    ('prt_sewb1', 'Culvert B1', 'culvert', 'dungeon'),
    ('prt_sewb2', 'Culvert B2', 'culvert', 'dungeon'),
    ('prt_sewb3', 'Culvert B3', 'culvert', 'dungeon'),
    ('prt_sewb4', 'Culvert B4', 'culvert', 'dungeon'),
)
NAMES = tuple(r[0] for r in WORLD)
LABEL = dict((r[0], r[1]) for r in WORLD)
CLUSTER = dict((r[0], r[2]) for r in WORLD)
KIND = dict((r[0], r[3]) for r in WORLD)
ON_DEMAND = -2                              # == GsvrRoMaps.ClusterOnDemand: the world's mapCluster value of every culvert / dungeon map
TOWN_ENDS = ('prt_fild05',)                 # the town-lane map the entrance warp leaves from (rAthena's own entrance: prt_fild05 274,208)

# map -> maps its warps lead to (== links(parse_rows(ro_culvert_warps.txt)); test_ro_culvert.py keeps the two equal)
LINKS = {
    'prt_sewb1': ('prt_fild05', 'prt_sewb2'),
    'prt_sewb2': ('prt_sewb1', 'prt_sewb3'),
    'prt_sewb3': ('prt_sewb2', 'prt_sewb4'),
    'prt_sewb4': ('prt_sewb3',),
    'prt_fild05': ('prt_sewb1',),               # the entrance only (the towns lane owns the rest of prt_fild05's warps)
}


def parse_rows(text, names=None):
    """ro_culvert_warps.txt rows -> [(map, x, y, sx, sy, target, tx, ty, name)] (both ends in the floors or TOWN_ENDS when names is None)"""
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


def resident(cur, names=NAMES):
    """the floors kept loaded while the player is on `cur` (== GsvrRoMaps.LResident for on-demand maps): `cur` itself + every floor one warp away from a floor"""
    out = []
    if cur not in names:
        return out
    for m in names:
        if m == cur or m in LINKS.get(cur, ()):
            out.append(m)
    return out


def warm_after(name):
    """companion prebuild hook: the floors after `name` in order (the floors are built in the background like every pack; the world still fetches them on demand)"""
    if name not in NAMES:
        return []
    return list(NAMES[NAMES.index(name) + 1:])
