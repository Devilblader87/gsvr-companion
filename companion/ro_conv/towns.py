"""RO towns lane (docs/design/ro_towns.md): the classic PRE-RENEWAL towns beyond the Prontera prototype and the fields that link them.

One table (WORLD) = the map list of the RO world in map id order (== GsvrSceneBuilder.Ragnarok RoMapNames / RoMapCluster, == api.MAP_ORDER).
The first five rows are the prototype maps in their old order (ids 0..4 unchanged). Every map is an ordinary RO map pack (ro_conv
mappack, RSM 1.4 models only - the 2026-11-25 survey of the owner's iRO client found no RSM2 in any of these maps).

Loading plan (GsvrRoMaps residency, the same rule on both sides - LResident in C#, resident() here):
  a map is RESIDENT (loaded in the background, kept loaded) while the player is on map `cur` when it is
    - map 0 (prontera: the start map, the sprite set, the respawn point), or
    - `cur` itself, or
    - in the same town cluster as `cur` (town + its interiors + its border field), or
    - linked to `cur` by a warp (the next map behind every exit is already there when the player walks into it).
  Every other map is loaded ON DEMAND: its warp shows the loading curtain until it is built (GsvrRoMaps WantStep). Maps that are no
  longer resident stay loaded while the RAM estimate (GsvrRpg1Loader.LZoneMB summed over the built maps) is below the budget; above it the
  least recently used non-resident map is unloaded first.
Route fields between two clusters (cluster '') belong to no cluster: they are resident only next to the player.

rAthena (GPL-3.0) map names / numbers only; labels are our own English names.
"""

CLUSTERS = ('prontera', 'geffen', 'payon', 'alberta', 'morocc')

# (map, label, cluster ('' = route field), kind)
WORLD = (
    ('prontera', 'Prontera', 'prontera', 'town'),
    ('prt_in', 'Prontera interiors', 'prontera', 'interior'),
    ('prt_church', 'Prontera church', 'prontera', 'interior'),
    ('prt_fild08', 'Prontera field 08', 'prontera', 'field'),
    ('izlude', 'Izlude', 'prontera', 'town'),
    # west: Prontera -> Geffen
    ('prt_fild05', 'Prontera field 05', 'prontera', 'field'),
    ('prt_fild04', 'Prontera field 04', '', 'field'),
    ('prt_fild00', 'Prontera field 00', '', 'field'),
    ('gef_fild00', 'Geffen field 00', 'geffen', 'field'),
    ('geffen', 'Geffen', 'geffen', 'town'),
    ('geffen_in', 'Geffen interiors', 'geffen', 'interior'),
    # south: Prontera field 08 -> Sograt desert -> Payon
    ('moc_fild01', 'Sograt Desert 01', '', 'field'),
    ('moc_fild02', 'Sograt Desert 02', '', 'field'),
    ('moc_fild03', 'Sograt Desert 03', '', 'field'),
    ('pay_fild01', 'Payon field 01', 'payon', 'field'),
    ('payon', 'Payon', 'payon', 'town'),
    ('payon_in01', 'Payon interiors 01', 'payon', 'interior'),
    ('payon_in02', 'Archer Village interiors', 'payon', 'interior'),
    ('payon_in03', 'Payon interiors 03', 'payon', 'interior'),
    ('pay_arche', 'Archer Village', 'payon', 'town'),
    # Payon -> Alberta
    ('pay_fild02', 'Payon field 02', '', 'field'),
    ('pay_fild03', 'Payon field 03', 'alberta', 'field'),
    ('alberta', 'Alberta', 'alberta', 'town'),
    ('alberta_in', 'Alberta interiors', 'alberta', 'interior'),
    # Sograt desert 01 -> desert crossroads (moc_fild20) -> Morroc
    ('moc_fild20', 'Sograt Desert 20', 'morocc', 'field'),
    ('morocc', 'Morroc', 'morocc', 'town'),
    ('morocc_in', 'Morroc interiors', 'morocc', 'interior'),
)
NAMES = tuple(r[0] for r in WORLD)
PROTO = NAMES[:5]                     # the prototype maps (ids 0..4, unchanged)
LABEL = dict((r[0], r[1]) for r in WORLD)
KIND = dict((r[0], r[3]) for r in WORLD)
CLUSTER = dict((r[0], r[2]) for r in WORLD)
MAX_MAPS = 255                        # w52 mapcap: world map ids 0..254 (GsvrRoMaps.MaxMaps 256 int[] masks; id 255 = the lobby map word, map + 1 in 8 bits on the monster / origin words); was 64 (w39 two-int masks)

# map -> the other maps its warps lead to (== links(parse_warps(ro_warps.txt)); test_ro_towns.py keeps the two equal). The companion has no copy of
# the world's warp table, so its prebuild hook (warm_after) uses this.
LINKS = {
    'prontera': ('prt_in', 'prt_church', 'prt_fild08', 'prt_fild05'),
    'prt_in': ('prontera',),
    'prt_church': ('prontera',),
    'prt_fild08': ('prontera', 'izlude', 'moc_fild01'),
    'izlude': ('prt_fild08',),
    'prt_fild05': ('prontera', 'prt_fild04'),
    'prt_fild04': ('prt_fild05', 'prt_fild00'),
    'prt_fild00': ('prt_fild04', 'gef_fild00'),
    'gef_fild00': ('prt_fild00', 'geffen'),
    'geffen': ('gef_fild00', 'geffen_in'),
    'geffen_in': ('geffen',),
    'moc_fild01': ('prt_fild08', 'moc_fild02', 'moc_fild20'),
    'moc_fild02': ('moc_fild01', 'moc_fild03'),
    'moc_fild03': ('moc_fild02', 'pay_fild01'),
    'pay_fild01': ('moc_fild03', 'payon', 'pay_fild02'),
    'payon': ('pay_fild01', 'payon_in01', 'payon_in03', 'pay_arche'),
    'payon_in01': ('payon',),
    'payon_in02': ('pay_arche',),
    'payon_in03': ('payon',),
    'pay_arche': ('payon', 'payon_in02'),
    'pay_fild02': ('pay_fild01', 'pay_fild03'),
    'pay_fild03': ('pay_fild02', 'alberta'),
    'alberta': ('pay_fild03', 'alberta_in'),
    'alberta_in': ('alberta',),
    'moc_fild20': ('morocc',),                # no way back north: rAthena has none (the guards only lead to Morroc); a respawn = Prontera
    'morocc': ('moc_fild20', 'morocc_in'),
    'morocc_in': ('morocc',),
}


def cluster_id(name):
    """cluster index (CLUSTERS) of a map, -1 for a route field / unknown map (== GsvrRoMaps.mapCluster)."""
    c = CLUSTER.get(name, '')
    return CLUSTERS.index(c) if c in CLUSTERS else -1


def cluster_ids():
    return [cluster_id(n) for n in NAMES]


def parse_warps(text, names=NAMES):
    """ro_warps.txt rows (map x y sx sy target tx ty [name]) whose two maps are both in names -> [(map, x, y, sx, sy, target, tx, ty, name)]
    (== GsvrRoMaps.LParseWarps)."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        f = ln.split()
        if len(f) < 8 or f[0] not in names or f[5] not in names:
            continue
        try:
            x, y, sx, sy, tx, ty = (int(f[i]) for i in (1, 2, 3, 4, 6, 7))
        except ValueError:
            continue
        if sx < 0 or sy < 0:
            continue
        out.append((f[0], x, y, sx, sy, f[5], tx, ty, f[8] if len(f) > 8 else ''))
    return out


def links(warps):
    """{map: set(maps a warp of it leads to)} (other maps only)."""
    g = {}
    for w in warps:
        if w[0] != w[5]:
            g.setdefault(w[0], set()).add(w[5])
    return g


def resident(cur, warps=None, names=NAMES):
    """the maps kept loaded while the player is on `cur` (module doc), in map id order (== GsvrRoMaps.LResident per map). warps None = LINKS."""
    g = LINKS if warps is None else links(warps)
    c = CLUSTER.get(cur, '')
    out = []
    for i, m in enumerate(names):
        if i == 0 or m == cur or (c and CLUSTER.get(m, '') == c) or m in g.get(cur, ()):
            out.append(m)
    return out


def warm_after(name, warps=None, names=NAMES):
    """companion prebuild hook (rosebuild W10): the world asked for `name` -> the maps it asks for next = the resident set of `name` after it
    (the world fetches the resident maps in id order, map 0 first)."""
    if name not in names:
        return []
    i = names.index(name)
    return [m for m in resident(name, warps, names) if names.index(m) > i]
