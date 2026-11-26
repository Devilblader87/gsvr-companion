"""RO atmosphere of a map (docs/design/ro_atmo.md): fog, BGM track number and the RSW light, read from the player's OWN client by the converter and written into the map pack
as ONE small section RATM (64 bytes). Nothing of the game is in git: the numbers come from `data\\fogparametertable.txt`, `data\\mp3nametable.txt` and the map's RSW.

The original client has NO sky: the 3D map is drawn over a black clear colour, the ground fades into the fog colour of fogparametertable.txt towards the far plane
(`<map>.rsw#near#far#0xAARRGGBB#factor#`, near / far = fractions of the 240 unit far clip, 5 units = 1 GAT cell). The world therefore shows a black sky + that fog.

RATM (little endian u32 x 16): version, flags (bit 0 fog, bit 1 bgm), fogNear, fogFar (permille of the far clip), fog colour 0x00RRGGBB, fog alpha 0..255, fog factor (permille),
ambient r g b (permille), diffuse r g b (permille), bgm track number (-1 none; BGM\\NN.mp3), reserved.
"""
import struct

TAG_RATM = b'RATM'
RATM_VERSION = 1
RATM = struct.Struct('<16I')
FLAG_FOG, FLAG_BGM = 1, 2
FAR_CLIP_UNITS = 240.0          # roBrowser / client convention: fog near / far are fractions of this (RO world units, 5 units = 1 GAT cell)
UNITS_PER_CELL = 5.0


def parse_fog(text):
    """fogparametertable.txt -> {map (lower, no .rsw): dict(near, far, argb, factor)}. Format: `name.rsw#` then four values each ending in `#` (own lines, // comments, blank lines
    anywhere); a later entry of the same map wins. Malformed entries are skipped."""
    toks = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('//'):
            continue
        toks.append(ln)
    out = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.lower().endswith('.rsw#') and i + 4 < len(toks):
            try:
                v = [x.rstrip('#').strip() for x in toks[i + 1:i + 5]]
                near, far, argb, fac = float(v[0]), float(v[1]), int(v[2], 16), float(v[3])
            except ValueError:
                i += 1
                continue
            if 0.0 <= near <= far <= 2.0:
                out[t[:-5].lower()] = dict(near=near, far=far, argb=argb & 0xFFFFFFFF, factor=fac)
            i += 5
        else:
            i += 1
    return out


def bgm_number(rel):
    """'bgm\\08.mp3' -> 8 (the track number), -1 when the name is not NN.mp3"""
    if not rel:
        return -1
    base = rel.replace('/', '\\').split('\\')[-1].lower()
    if not base.endswith('.mp3'):
        return -1
    n = base[:-4]
    return int(n) if n.isdigit() else -1


def _pm(x):
    return max(0, min(4000, int(round(float(x) * 1000.0))))


def enc_ratm(fog, bgm_no, light):
    """fog: parse_fog entry or None; light: rofmt.rsw light dict (ambient / diffuse tuples) -> 64 bytes"""
    flags = 0
    fn = ff = fc = fa = fm = 0
    if fog:
        flags |= FLAG_FOG
        fn, ff, fc, fa, fm = _pm(fog['near']), _pm(fog['far']), fog['argb'] & 0xFFFFFF, (fog['argb'] >> 24) & 255, _pm(fog['factor'])
    if bgm_no >= 0:
        flags |= FLAG_BGM
    a = light.get('ambient', (0.3, 0.3, 0.3)) if light else (0.3, 0.3, 0.3)
    d = light.get('diffuse', (1.0, 1.0, 1.0)) if light else (1.0, 1.0, 1.0)
    return RATM.pack(RATM_VERSION, flags, fn, ff, fc, fa, fm, _pm(a[0]), _pm(a[1]), _pm(a[2]), _pm(d[0]), _pm(d[1]), _pm(d[2]), bgm_no & 0xFFFFFFFF, 0, 0)


def dec_ratm(d):
    v = RATM.unpack_from(d, 0)
    if v[0] != RATM_VERSION or len(d) < RATM.size:
        raise ValueError('RATM version / size')
    return dict(flags=v[1], fog=bool(v[1] & FLAG_FOG), near=v[2], far=v[3], rgb=v[4], alpha=v[5], factor=v[6], ambient=v[7:10], diffuse=v[10:13],
                bgm=(v[13] if v[13] < 0x80000000 else v[13] - 0x100000000))


def fog_metres(near_pm, far_pm, cell_m):
    """fog start / end in metres for a map with GAT cell size cell_m (the same rule as GsvrRoWalk.LFogMetres)"""
    k = FAR_CLIP_UNITS / UNITS_PER_CELL * cell_m / 1000.0
    return near_pm * k, far_pm * k


def build_section(vfs, mapname, light, warn=None):
    """-> (TAG, version, count, bytes) for the map pack; a client without the tables gives a section with no fog / no bgm (never raises)."""
    mp = mapname.lower()
    fog = None
    try:
        fog = parse_fog(vfs.read('data\\fogparametertable.txt').decode('cp949', 'replace')).get(mp)
    except KeyError:
        if warn is not None:
            warn.append('atmo: fogparametertable.txt not in the client (no fog)')
    no = -1
    try:
        from . import audio
        no = bgm_number(audio.bgm_for(audio.parse_nametable(vfs.read('data\\mp3nametable.txt').decode('cp949', 'replace')), mp))
    except KeyError:
        if warn is not None:
            warn.append('atmo: mp3nametable.txt not in the client (no bgm number)')
    return (TAG_RATM, RATM_VERSION, 1, enc_ratm(fog, no, light))
