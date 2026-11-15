"""Environment data for an RPG1 pack: zone events, water planes + flip-book frames, sky / fog / light / time of day. Read-only, stdlib only.
Everything here is looked up by the original client's own files (LIST_ZONE, LIST_SKY, SCRIPTS/LOAD_FOG.LUA, LOAD_LIGHT.LUA, OCEAN01_xx.DDS);
what the retail client hard codes in its executable is taken from the open-source rose-offline-client (cited in docs/ROSE_SPEC.md 13).
Output is plain dicts; rpg1.enc_* writes them (layouts in docs/RPG1_PACK.md section 8).
"""
import re, math

import rosevfs, rosefmt as F, rosedds as D, rpg1 as P

CENTER_OFF = 5200.0
WATER_FRAMES = 25
WATER_FPS = 10                 # rose-offline-client water_material.rs: frame = (elapsed seconds * 10) % 25, cross-faded with the next frame
WATER_TILE_DEFAULT_M = 20.0
TICK_SECONDS = 10.0            # rose-data world.rs WORLD_TICK_DURATION

# values the retail client does not keep in a file (rose-offline-client systems/zone_time_system.rs); only used as an alternative palette
FOG_ALT_COLORS = [(100, 100, 100), (200, 200, 200), (100, 100, 100), (10, 10, 10)]
FOG_ALT_DENSITY = [0.0022, 0.0018, 0.0022, 0.0020]


# ------------------------------------------------------------------ events
def classify_event(name):
    n = name.lower()
    if n == 'start':
        return P.EV_START
    if n.startswith('restore'):
        return P.EV_RESTORE
    if n.startswith('warp'):
        return P.EV_WARP
    return P.EV_OTHER


def zone_events(zon, win):
    """ZON event positions (x, height, y) file cm -> list of dict(name, kind, inside, x, y, z) in local metres."""
    out = []
    for nm, p in zon['events']:
        x, y, z = win.to_local(p[0], p[2], p[1])
        inside = 0.0 <= x <= win.size_x and 0.0 <= z <= win.size_z
        out.append(dict(name=nm, kind=classify_event(nm), inside=inside, x=x, y=y, z=z))
    return out


# ------------------------------------------------------------------ water
def water_planes(ifos, win):
    """IFO type 9 planes -> list of dict(x0, z0, x1, z1, y, bx, by) in local metres + the water tile length in metres (from `water_size`).
    The planes use file axes (x east, height, y north) like Vec3 fields (start / end); level = the middle component (equal for start and end)."""
    planes = []; tile = None
    for (bx, by), o in sorted(ifos.items()):
        w = o.get(9)
        if not w:
            continue
        size, lst = w
        if size > 0:
            tile = size / 100.0 if tile is None else tile
        for s, e in lst:
            ax, az = s[0] / 100.0 + CENTER_OFF - win.x0, s[2] / 100.0 + CENTER_OFF - win.z0
            bx_, bz_ = e[0] / 100.0 + CENTER_OFF - win.x0, e[2] / 100.0 + CENTER_OFF - win.z0
            planes.append(dict(x0=min(ax, bx_), z0=min(az, bz_), x1=max(ax, bx_), z1=max(az, bz_), y=(s[1] + e[1]) / 200.0, bx=bx, by=by, flags=0))
    return planes, (tile or WATER_TILE_DEFAULT_M)


def water_frames(vfs, mips=5):
    """OCEAN01_01..25.DDS (64 x 64 DXT1, no mips in the file) -> (fmt, w, h, [[level bytes] per frame]); the mip chain is generated (box filter) down to 4 x 4."""
    frames = []; w = h = None
    for i in range(1, WATER_FRAMES + 1):
        dd = D.parse_dds(vfs.read('3DDATA/JUNON/WATER/OCEAN01_%02d.DDS' % i))
        f, w, h, lv = D.to_pack_texture(dd)
        if f != D.FMT_DXT1:
            raise ValueError('water frame %d is not DXT1' % i)
        lv = list(lv)
        lw, lh = max(1, w >> (len(lv) - 1)), max(1, h >> (len(lv) - 1))
        rgba = D.decode_dxt('DXT1', lv[-1], lw, lh)
        while len(lv) < mips and min(lw, lh) > 4:
            rgba, lw, lh = D.downsample2(rgba, lw, lh)
            lv.append(D.encode_dxt1(rgba, lw, lh))
        frames.append(lv[:mips])
    return D.FMT_DXT1, w, h, frames


# ------------------------------------------------------------------ colours / lua
def decode_color(s):
    """LIST_SKY colours are decimal RRRGGGBBB (one retail cell has an extra digit: 10 digits = RRR GGG 0 BBB). -> (r, g, b) 0..255 or None"""
    s = (s or '').strip()
    if not s.isdigit():
        return None
    if len(s) == 10:
        s = s[:6] + s[7:]
    s = s.zfill(9)
    return (min(255, int(s[0:3])), min(255, int(s[3:6])), min(255, int(s[6:9])))


def _lua_nums(txt, pat):
    m = re.search(pat, txt)
    return [float(x) for x in m.groups()] if m else None


def read_lua_light(vfs):
    """SCRIPTS/LOAD_LIGHT.LUA 'light_01' (the map light): active (non-commented) setLight calls. -> dict(ambient, diffuse, specular, direction)"""
    txt = vfs.read('SCRIPTS/LOAD_LIGHT.LUA').decode('cp949', 'replace')
    live = '\n'.join(l for l in txt.splitlines() if not l.lstrip().startswith('--'))
    light = live.split('char_light')[0]
    out = {}
    for what in ('specular', 'ambient', 'diffuse', 'direction'):
        v = _lua_nums(light, r'setLight\(\s*light\s*,\s*"%s"\s*,\s*([-.\d]+)\s*,\s*([-.\d]+)\s*,\s*([-.\d]+)\s*\)' % what)
        if v:
            out[what] = tuple(v)
    return out


def read_lua_fog(vfs):
    """SCRIPTS/LOAD_FOG.LUA -> dict(use, color, range (cm), alpha_range (cm))"""
    txt = vfs.read('SCRIPTS/LOAD_FOG.LUA').decode('cp949', 'replace')
    live = '\n'.join(l for l in txt.splitlines() if not l.lstrip().startswith('--'))
    out = {}
    m = re.search(r'useFog\(\s*(\d+)\s*\)', live)
    out['use'] = bool(int(m.group(1))) if m else False
    for key, pat in (('color', r'setFogColor\(\s*([-.\d]+)\s*,\s*([-.\d]+)\s*,\s*([-.\d]+)\s*\)'), ('range', r'setFogRange\(\s*([-.\d]+)\s*,\s*([-.\d]+)\s*\)'),
                     ('alpha_range', r'setAlphaFogRange\(\s*([-.\d]+)\s*,\s*([-.\d]+)\s*\)')):
        v = _lua_nums(live, pat)
        if v:
            out[key] = tuple(v)
    return out


# ------------------------------------------------------------------ sky
def _rgb01(c):
    return tuple(v / 255.0 for v in c)


def file_to_pack_dir(v):
    """a direction / position in file axes (x east, y north, z up) -> pack axes (x, z, y)"""
    return (v[0], v[2], v[1])


def sky_dome(vfs, mesh_path):
    """sky ZMS (metres, Z up) -> (verts [(x, y, z, u, v)] pack axes, indices) ; winding flipped like every other reflected mesh."""
    z = F.zms(vfs.read(mesh_path))
    uv = z.get('uv1') or [(0.0, 0.0)] * len(z['pos'])
    verts = [(p[0], p[2], p[1], uv[i][0], uv[i][1]) for i, p in enumerate(z['pos'])]
    idx = z['idx']
    tri = []
    for i in range(0, len(idx) - 2, 3):
        a, b, c = idx[i], idx[i + 1], idx[i + 2]
        tri += [a, c, b]
    return verts, tri


def read_sky(vfs, zone_cells, sky_px=1024):
    """zone_cells = LIST_ZONE row (API cells). -> dict for rpg1.enc_skyb, plus an info dict (what came from where)."""
    info = {}
    def cell(i):
        return zone_cells[i] if i < len(zone_cells) else ''
    def to_int(s, d=0):
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return d
    sky_id = to_int(cell(7))
    cycle, morning, day, evening, night = (to_int(cell(i)) for i in (13, 14, 15, 16, 17))
    stb = F.stb(vfs.read('3DDATA/STB/LIST_SKY.STB'))
    row = stb['cells'][sky_id] if 0 <= sky_id < stb['rows'] else None
    pal_bg = [(255, 255, 255)] * 4; pal_amb = [(255, 255, 255)] * 4; pal_ca = [(255, 255, 255)] * 4; pal_cd = [(255, 255, 255)] * 4
    dome_verts = dome_idx = None; textures = []; flags = 0; blend = 5
    if row:
        def col(i):
            return row[i] if i < len(row) else ''
        for s in range(4):
            c = decode_color(col(6 + s)) or (255, 255, 255)
            pal_bg[s] = c; pal_amb[s] = c
            pal_ca[s] = decode_color(col(10 + 2 * s)) or (255, 255, 255)
            pal_cd[s] = decode_color(col(11 + 2 * s)) or (255, 255, 255)
        blend = to_int(col(5), 5)
        mesh = col(0).replace('\\\\', '\\')
        if mesh and rosevfs.norm(mesh) in vfs.files:
            dome_verts, dome_idx = sky_dome(vfs, mesh); flags |= P.SK_DOME
            info['dome'] = rosevfs.norm(mesh)
        for slot, key in ((1, 'day'), (2, 'night')):
            tp = col(slot).replace('\\\\', '\\')
            if tp and rosevfs.norm(tp) in vfs.files:
                dd = D.parse_dds(vfs.read(tp))
                fmt, w, h, lv = D.compress_raw(dd) if dd['fmt'] == 'RGBA' else D.to_pack_texture(dd)
                if sky_px and w > sky_px:                      # shrink: box filter from the decoded RGBA, then re-encode with a fresh mip chain
                    rgba = D.decode_dxt('DXT1' if fmt == D.FMT_DXT1 else 'DXT5', lv[0], w, h) if fmt in (D.FMT_DXT1, D.FMT_DXT5) else lv[0]
                    while w > sky_px:
                        rgba, w, h = D.downsample2(rgba, w, h)
                    dd2 = dict(fmt='RGBA', w=w, h=h, levels=[bytes(rgba)])
                    fmt, w, h, lv = D.compress_raw(dd2)
                textures.append(dict(name=rosevfs.norm(tp), fmt=fmt, w=w, h=h, levels=lv))
                info[key + '_texture'] = rosevfs.norm(tp)
        if len(textures) == 2:
            flags |= P.SK_NIGHT_TEX
    if cell(4).strip() == '1':
        # DUNGEON: an underground zone (LIST_ZONE col 4) keeps the time of day at night (morning = day = evening = night = 0: rose-offline-client zone_time_system
        # takes the night branch for every tick) and its sky row has white background colours; the fog colour (pal_bg, what the runtime fogs to) follows the
        # client's fixed fog colours instead (night = 10 / 255: the cave fades to black). The map ambient stays the sky row's.
        pal_bg = list(FOG_ALT_COLORS)
        info['fog_palette'] = 'underground: fixed fog colours'
    lf = read_lua_fog(vfs); ll = read_lua_light(vfs)
    fog = (0.0, 0.0, 0.0, 0.0)
    if lf.get('use') and 'range' in lf:
        ar = lf.get('alpha_range', lf['range'])
        fog = (lf['range'][0] / 100.0, lf['range'][1] / 100.0, ar[0] / 100.0, ar[1] / 100.0)
        flags |= P.SK_FOG
    ld = ll.get('direction', (0.5, 0.5, 0.5))
    n = math.sqrt(sum(v * v for v in ld)) or 1.0
    # the Lua direction (.5, .5, .5) is the vector TOWARDS the light in file axes (same octant as rose-offline-client's fixed sun, which shines west/south/down); light_dir = direction the light travels
    light_dir = file_to_pack_dir(tuple(-v / n for v in ld))
    flags |= P.SK_LIGHT
    info.update(fog_lua=lf, light_lua=ll, sky_id=sky_id)
    return dict(sky_id=sky_id, flags=flags, cycle=cycle, morning=morning, day=day, evening=evening, night=night, tick_s=TICK_SECONDS, fog=fog,
                dome_scale=10.0, blend=blend, light_dir=light_dir, light_ambient=ll.get('ambient', (1.0, 1.0, 1.0)), light_diffuse=ll.get('diffuse', (1.0, 1.0, 1.0)),
                light_specular=ll.get('specular', (0.5, 0.5, 0.5)),
                pal_bg=[_rgb01(c) for c in pal_bg], pal_ambient=[_rgb01(c) for c in pal_amb], pal_char_ambient=[_rgb01(c) for c in pal_ca],
                pal_char_diffuse=[_rgb01(c) for c in pal_cd], pal_fog_alt=[_rgb01(c) for c in FOG_ALT_COLORS], fog_density_alt=FOG_ALT_DENSITY,
                dome_verts=dome_verts, dome_idx=dome_idx, textures=textures), info
