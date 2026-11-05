#!/usr/bin/env python3
"""Attack missile art + selection circles of the map's unit types (BUGS P-MISSILE, P-SELRING; docs/w3p_models.md
UMIS / USEL). Presentation only: the engine's missiles keep their simulation, this only says what to draw.

Missile art per unit type and weapon index (0, 1) [obs data: Units\\UnitMetaData.slk — Missileart ua1m / ua2m and
Missilearc uma1 / uma2 are Profile fields with index 0 / 1 (the *UnitFunc.txt value is a comma list, one per weapon),
launchZ ulpz / impactZ uimz are UnitWeapons fields]. Only weapons with a missile weapon type (missile, artillery,
msplash, mbounce, mline, aline) get a row.

Glow colour of an art (for missiles drawn as a glow quad, e.g. a particle-only model): the model's PRE2 particle
emitters' segment colours, alpha-weighted, times the emitter texture's mean colour; else the mean colour of the
model's own textures; else none (the world's default glow). [inf: a sensible tint, not a game rule.]

Selection circles [obs data: UI\\MiscData.txt [SelectionCircle]]: the circle size is the UnitUI `scale` (w3u ussc)
times ScaleFactor (72); Size00 / Size01 pick the small / medium / large texture; ColorFriend / ColorNeutral /
ColorEnemy; the height offset is UnitUI `selZ` (w3u uslz).
"""
import struct

MISSILE_TYPES = (2, 3, 5, 6, 7, 8)          # jvm.unitdata.WEAP_TYPES: missile artillery msplash mbounce mline aline
SEL_DEFAULT = {'NumSizes': '3', 'Size00': '100', 'Size01': '300', 'ScaleFactor': '72',
               'Texture00': 'ReplaceableTextures\\Selection\\SelectionCircleSmall',
               'Texture01': 'ReplaceableTextures\\Selection\\SelectionCircleMed',
               'Texture02': 'ReplaceableTextures\\Selection\\SelectionCircleLarge',
               'ColorFriend': '255,0,255,0', 'ColorNeutral': '255,255,255,0', 'ColorEnemy': '255,255,0,0'}


def _model(v):
    v = str(v or '').strip().strip('"').strip()
    if not v.lower().endswith(('.mdl', '.mdx')):
        return ''
    return v[:-4] + '.mdx'


def _num(v, d=0.0):
    try:
        return float(str(v).strip().strip('"'))
    except (TypeError, ValueError):
        return d


def _split2(v):
    parts = [p.strip() for p in str(v or '').strip('"').split(',')]
    return [parts[i] if len(parts) > i else parts[-1] for i in (0, 1)]


def missile_art(files, uids, log=print):
    """-> {uid: [(weapon 0/1, model path, '' (no art known) or '-' (blanked by the map: the game shows no missile
    [obs Azure: hgtw ua1m = '']), arc, weapon type, launch z, impact z), ...]} for the weapons with a missile weapon
    type. files.read / files.map.read as in w3pack.Files."""
    import w3slk as slk
    import w3map
    from jvm import unitdata

    def rd(n):
        return files.read(n, missing_ok=True)

    def need(n):
        b = rd(n)
        if b is None:
            raise KeyError(n)
        return b
    w3u = files.map.read('war3map.w3u', missing_ok=True)
    types = unitdata.merge(unitdata.base_records(need), w3u)
    prof = {}
    for race in unitdata.PROFILE_FILES:
        raw = rd('Units\\%sUnitFunc.txt' % race)
        if raw:
            unitdata.parse_profile(raw.decode('latin-1'), prof)
    art = {k: _split2(v.get('Missileart')) for k, v in prof.items() if v.get('Missileart')}
    raw = rd('Units\\UnitWeapons.slk')
    wp = slk.table(raw.decode('latin1'), 'unitWeapID') if raw else {}
    zs = {k: [_num(r.get('launchZ')), _num(r.get('impactZ'))] for k, r in wp.items()}
    if w3u:
        try:
            d = w3map.parse_objects(w3u, '.w3u')
            art0, zs0 = dict(art), dict(zs)    # custom types start from the base's standard values [ext]
            for table, is_custom in ((d.get('original_modified', []), False), (d.get('custom', []), True)):
                for o in table:
                    tid = o['new'] if is_custom else o['orig']
                    if is_custom:
                        art[tid] = list(art0.get(o['orig'], ['', '']))
                        zs[tid] = list(zs0.get(o['orig'], [0.0, 0.0]))
                    for mid, _lvl, val in o['mods']:
                        if mid in ('ua1m', 'ua2m') and isinstance(val, str):
                            k = 0 if mid == 'ua1m' else 1
                            row = art.setdefault(tid, ['', ''])
                            art[tid] = row = list(row)
                            row[k] = val if val.strip() else '-'      # '-': the map blanked it (the game draws none)
                        elif mid in ('ulpz', 'uimz') and isinstance(val, (int, float)):
                            zs.setdefault(tid, [0.0, 0.0])[0 if mid == 'ulpz' else 1] = float(val)
        except Exception as e:                 # noqa: BLE001
            log('  missile art: war3map.w3u unreadable (%s)' % e)
    out = {}
    for u in sorted(set(uids)):
        rec = types.get(u)
        if not rec:
            continue
        rows = []
        on = int(_num(rec.get('weapsOn'), 0))
        for i in (0, 1):
            tp = int(_num(rec.get('weapTp%d' % (i + 1)), 0))
            if not (on >> i) & 1 or tp not in MISSILE_TYPES:
                continue
            raw = (art.get(u) or ['', ''])[i]
            path = '-' if raw == '-' else _model(raw)
            z = zs.get(u, [0.0, 0.0])
            rows.append((i, path, _num(rec.get('Missilearc%d' % (i + 1)), 0.0), tp, z[0], z[1]))
        if rows:
            out[u] = rows
    return out


def _mean_rgb(im):
    """alpha-weighted mean colour of a PIL RGBA image -> (r, g, b) 0..1 or None."""
    im = im.convert('RGBA').resize((16, 16))
    sr = sg = sb = sa = 0.0
    for r, g, b, a in im.getdata():
        w = a / 255.0
        sr += r * w
        sg += g * w
        sb += b * w
        sa += w
    if sa <= 0.01:
        return None
    return sr / sa / 255.0, sg / sa / 255.0, sb / sa / 255.0


def particle_colours(data):
    """PRE2 emitters of an MDX -> [(rgb weighted by the segment alphas, texture index)] [ext MDX PRE2 layout: node,
    then 8 f32 (speed .. length), 4 u32 (filter, rows, columns, head/tail), 2 f32 (tail length, time), 3 × 3 f32
    segment colours (r, g, b [obs data: FrostWyrmMissile blue, Mortar orange]), 3 u8 segment alphas, 3 f32 scaling, 12 u32 intervals, u32 texture id]."""
    import mdx
    out = []
    p = 4
    n = len(data)
    while p + 8 <= n:
        tag = data[p:p + 4]
        size = struct.unpack_from('<I', data, p + 4)[0]
        c0, c1 = p + 8, p + 8 + size
        if tag == b'PRE2':
            q = c0
            while q + 4 <= c1:
                isz = struct.unpack_from('<I', data, q)[0]
                if isz <= 0:
                    break
                _nd, r = mdx._node(data, q + 4)
                cols = struct.unpack_from('<9f', data, r + 56)
                alph = struct.unpack_from('<3B', data, r + 92)
                tex = struct.unpack_from('<I', data, r + 95 + 12 + 48)[0]
                wsum = float(sum(alph)) or 3.0
                rgb = [0.0, 0.0, 0.0]
                for k in range(3):
                    w = (alph[k] or (0 if sum(alph) else 1)) / wsum
                    r_, g_, b_ = cols[k * 3:k * 3 + 3]
                    rgb[0] += r_ * w
                    rgb[1] += g_ * w
                    rgb[2] += b_ * w
                out.append((tuple(rgb), tex))
                q += isz
        p = c1
    return out


def art_colour(files, path, blp_rgba):
    """-> RGBA8 packed as r | g << 8 | b << 16 | a << 24 (a = 255), or 0 when the art gives no colour."""
    import mdx
    data = files.read(path, missing_ok=True)
    if data is None:
        return 0
    try:
        m = mdx.parse(data)
    except Exception:                          # noqa: BLE001
        return 0

    def tex_rgb(ti):
        if not (0 <= ti < len(m.textures)):
            return None
        rid, tp, _fl = m.textures[ti]
        if rid or not tp:
            return None
        b = files.read(tp, missing_ok=True)
        if b is None:
            return None
        try:
            return _mean_rgb(blp_rgba(b))
        except Exception:                      # noqa: BLE001
            return None
    cols = []
    try:
        for rgb, ti in particle_colours(data):
            t = tex_rgb(ti) or (1.0, 1.0, 1.0)
            cols.append(tuple(max(0.0, min(1.0, rgb[k])) * t[k] for k in range(3)))
    except Exception:                          # noqa: BLE001
        cols = []
    if not cols:
        for ti in range(len(m.textures)):
            t = tex_rgb(ti)
            if t:
                cols.append(t)
    if not cols:
        return 0
    c = [sum(x[k] for x in cols) / len(cols) for k in range(3)]
    mx = max(c)
    if mx <= 0.02:
        return 0
    c = [int(round(255 * v / mx)) for v in c]          # a glow: normalised to full brightness
    return c[0] | c[1] << 8 | c[2] << 16 | 255 << 24


def selection_info(files):
    """UI\\MiscData.txt [SelectionCircle] (later definitions win) -> dict of raw strings."""
    out = dict(SEL_DEFAULT)
    for n in ('UI\\MiscData.txt', 'Units\\MiscData.txt'):
        raw = files.read(n, missing_ok=True)
        if not raw:
            continue
        sec = None
        for line in raw.decode('latin-1').splitlines():
            line = line.split('//', 1)[0].strip()
            if line.startswith('[') and line.endswith(']'):
                sec = line[1:-1]
            elif sec == 'SelectionCircle' and '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
        break
    return out


def argb_to_rgba8(v):
    """'a,r,g,b' -> r | g << 8 | b << 16 | a << 24."""
    try:
        a, r, g, b = [int(x) for x in v.split(',')[:4]]
    except ValueError:
        return 0
    return (r & 255) | (g & 255) << 8 | (b & 255) << 16 | (a & 255) << 24


def ubersplat_info(files):
    """Splats\\UberSplatData.slk -> {Name: (texture path, Scale, middle colour RGBA8, BlendMode)} for the rows whose
    texture exists: the ground decal under a structure (UnitUI `uberSplat`, w3u uubs) [obs data]. Presentation only."""
    import w3slk as slk
    raw = files.read('Splats\\UberSplatData.slk', missing_ok=True)
    if not raw:
        return {}
    out = {}
    for name, r in slk.table(raw.decode('latin-1'), 'Name').items():
        d, fn = r.get('Dir') or '', r.get('file') or ''
        if not fn:
            continue
        path = (d + '\\' if d else '') + str(fn) + '.blp'
        if files.read(path, missing_ok=True) is None:
            continue

        def c(k):
            v = r.get(k)
            return max(0, min(255, int(v))) if isinstance(v, (int, float)) else 255
        col = c('MiddleR') | c('MiddleG') << 8 | c('MiddleB') << 16 | c('MiddleA') << 24
        sc = r.get('Scale')
        bm = r.get('BlendMode')
        out[name] = (path, float(sc) if isinstance(sc, (int, float)) else 100.0, col, int(bm) if isinstance(bm, int) else 0)
    return out
