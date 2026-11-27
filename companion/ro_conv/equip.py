"""Equipment look of the Ragnarok world, part 2 (w43 lane "equip", docs/design/ro_equip.md): draw order from the client's IMF files, hair styles + hair /
clothes colours as palette rows, garment (robe) sprites. Everything is read from the player's own client (read only); nothing of the game is in git.

  * IMF (data/imf/<job>_<sex>.imf): per action + frame the draw priority of the body layer (0) and the head layer (1). The converter turns it into ONE flag on
    every body frame (sprite.FF_HEAD_BEHIND: the head and its headgear sit behind the body in that frame, e.g. an arm swung across the face), so the world needs
    no IMF file. Lower priority value = nearer to the viewer (checked against the Novice sprites: idle shows the hair over the back in every direction).
  * Hair: hi_<style>_<m|f> = the client's hair style sprite, INDEXED (the red channel is the palette index); bi_<m|f> = the Novice body, indexed the same way.
    One RPAL section holds the palette rows: hair rows ((sex * HAIR_STYLES + style - 1) * HAIR_COLORS + colour), then the Novice clothes rows
    (base + sex * BODY_COLORS + colour); the shader looks the colour up (_PalRow). A colour the client has no .pal for falls back to the sprite's own palette.
  * Robes: rb_<view>_<m|f> = data/sprite/로브/<robe name>/<sex>/초보자_<sex> for the robe views in ROBE_VIEWS (view = the client's robe id = the item's ClassNum;
    the classic pre-renewal garments have none, so nothing is drawn for them - as in the original client).

RPAL section (RPG1 tag 'RPAL', version 1): u32 version, rows, hair styles, hair colours, body colours, reserved; then rows * 1024 bytes (256 x RGBA, A = 255),
row 0 first (= the bottom row of the texture, Unity LoadRawTextureData order).
"""
import struct

from . import imf as IMF
from . import items, sprite

HAIR_STYLES = 17            # classic styles 1..17 (hair stylist); the creation screen offers 1..HAIR_CREATE
HAIR_CREATE = 9
HAIR_COLORS = 9             # 0..8 (rAthena max_hair_color 8)
BODY_COLORS = 8             # 0..7 (rAthena max_cloth_color 7)
TAG_RPAL = b'RPAL'
RPAL_VERSION = 1
SEXES = ('m', 'f')
SEX_WORD = {'m': '남', 'f': '여'}
PATH_HEAD = 'data/sprite/인간족/머리통/%s/%d_%s'                  # sex word, style, sex word
PATH_BODY = 'data/sprite/인간족/몸통/%s/초보자_%s'
PATH_PAL_HAIR = 'data/palette/머리/머리%d_%s_%d.pal'              # style, sex word, colour
PATH_PAL_BODY = 'data/palette/몸/초보자_%s_%d.pal'                # sex word, colour
PATH_IMF = 'data/imf/%s_%s.imf'                                 # job name, sex word
PATH_ROBE = 'data/sprite/로브/%s/%s/초보자_%s'                    # robe name, sex word, sex word
ROBE_IDS = 'data/luafiles514/lua files/datainfo/spriterobeid.lub'
ROBE_NAMES = 'data/luafiles514/lua files/datainfo/spriterobename.lub'
ROBE_VIEWS = (1,)           # views the start pack carries (a demo robe for the test hook; the classic garments have no robe sprite)
NOVICE = '초보자'


def hair_name(style, sex):
    return 'hi_%d_%s' % (style, sex)


def body_name(sex):
    return 'bi_%s' % sex


def robe_name(view, sex):
    return 'rb_%d_%s' % (view, sex)


def hair_row(sex_idx, style, colour):
    """palette row of a hair style / colour (== GsvrRoSprite.LHairRow); style 1..HAIR_STYLES, colour 0..HAIR_COLORS-1."""
    return (sex_idx * HAIR_STYLES + (style - 1)) * HAIR_COLORS + colour


def body_row(sex_idx, colour):
    """palette row of the Novice clothes colour (== GsvrRoSprite.LBodyRow)."""
    return 2 * HAIR_STYLES * HAIR_COLORS + sex_idx * BODY_COLORS + colour


def row_count():
    return 2 * HAIR_STYLES * HAIR_COLORS + 2 * BODY_COLORS


def _pal_rows(data, own):
    """1024 bytes of a .pal (256 x r g b a) -> 1024 bytes RGBA with alpha 255; `own` = fallback [(r, g, b)] x 256."""
    if data is not None and len(data) >= 1024:
        return b''.join(bytes((data[i * 4], data[i * 4 + 1], data[i * 4 + 2], 255)) for i in range(256))
    return b''.join(bytes((r, g, b, 255)) for (r, g, b) in own)


def _read(vfs, path):
    try:
        return vfs.read(path)
    except KeyError:
        return None


def hair_sprites(vfs, sex, warn=None, styles=HAIR_STYLES):
    """-> [indexed hair sprite dicts hi_<style>_<sex>] for the styles the client has (both sexes are built by the caller), then their own palettes in 'palette'."""
    out = []
    miss = []
    sw = SEX_WORD[sex]
    for st in range(1, styles + 1):
        base = PATH_HEAD % (sw, st, sw)
        try:
            out.append(sprite.convert_sprite(hair_name(st, sex), sprite.KIND_HEAD, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=items.PLAYER_KEEP, indexed=True))
        except KeyError:
            miss.append(st)
        except (sprite.FormatError, ValueError, IndexError, struct.error) as e:
            if warn is not None:
                warn.append('equip: hair style %d: %s' % (st, e))
    if miss and warn is not None:
        warn.append('equip: %d %s hair styles not in the client data (%s)' % (len(miss), sex, ' '.join(str(i) for i in miss[:8])))
    return out


def body_sprite(vfs, sex, warn=None):
    """the indexed Novice body (clothes colours); None when the client lacks it."""
    base = PATH_BODY % (SEX_WORD[sex], SEX_WORD[sex])
    try:
        return sprite.convert_sprite(body_name(sex), sprite.KIND_BODY, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=items.PLAYER_KEEP, indexed=True)
    except KeyError:
        return None
    except (sprite.FormatError, ValueError, IndexError, struct.error) as e:
        if warn is not None:
            warn.append('equip: indexed body %s: %s' % (sex, e))
        return None


def build_rpal(vfs, hair, bodies, warn=None):
    """hair = {(sex, style): sprite dict}, bodies = {sex: sprite dict}  ->  RPAL section bytes (every row filled: the client's .pal, else the sprite's own palette,
    else a grey ramp) and the number of rows read from the client."""
    rows = [None] * row_count()
    got = 0
    for si, sx in enumerate(SEXES):
        sw = SEX_WORD[sx]
        for st in range(1, HAIR_STYLES + 1):
            sp = hair.get((sx, st))
            own = sp['palette'] if sp is not None else [(i, i, i) for i in range(256)]
            for c in range(HAIR_COLORS):
                d = _read(vfs, PATH_PAL_HAIR % (st, sw, c))
                if d is not None and len(d) >= 1024:
                    got += 1
                rows[hair_row(si, st, c)] = _pal_rows(d, own)
        bs = bodies.get(sx)
        own = bs['palette'] if bs is not None else [(i, i, i) for i in range(256)]
        for c in range(BODY_COLORS):
            d = _read(vfs, PATH_PAL_BODY % (sw, c))
            if d is not None and len(d) >= 1024:
                got += 1
            rows[body_row(si, c)] = _pal_rows(d, own)
    if warn is not None and got == 0:
        warn.append('equip: no palette files in the client data (hair / clothes colours show the default)')
    head = struct.pack('<6I', RPAL_VERSION, len(rows), HAIR_STYLES, HAIR_COLORS, BODY_COLORS, 0)
    return head + b''.join(rows), got


def dec_rpal(d):
    """RPAL bytes -> dict(rows, hair_styles, hair_colors, body_colors, data) (tests / the offline e2e)."""
    v, n, hs, hc, bc, _r = struct.unpack_from('<6I', d, 0)
    if v != RPAL_VERSION or len(d) != 24 + n * 1024 or n != 2 * hs * hc + 2 * bc:
        raise ValueError('RPAL layout')
    return dict(rows=n, hair_styles=hs, hair_colors=hc, body_colors=bc, data=d[24:])


# ------------------------------------------------------------------ draw order (IMF)
def imf_path(name):
    """sprite name -> IMF path or None: body_<sex> / bi_<sex> = Novice, body_j<id>_<sex> = jobs.JOB_BODY."""
    from . import jobs
    parts = name.split('_')
    if name in ('body_m', 'body_f', 'bi_m', 'bi_f'):
        return PATH_IMF % (NOVICE, SEX_WORD[parts[-1]])
    if len(parts) == 3 and parts[0] == 'body' and parts[1][:1] == 'j' and parts[1][1:].isdigit() and parts[2] in SEX_WORD:
        job = int(parts[1][1:])
        if job in jobs.JOB_BODY:
            return PATH_IMF % (jobs.JOB_BODY[job], SEX_WORD[parts[2]])
    return None


def apply_imf(sp, im):
    """sets the 'order' flag on every frame of body sprite `sp` whose head layer has a HIGHER priority value than the body layer (= is drawn behind it). Returns the
    number of flagged frames. clip index = ACT action (anim * 8 + dir), frame i = ACT frame i."""
    n = 0
    for a, c in enumerate(sp['clips']):
        for i, fr in enumerate(c['frames']):
            if IMF.head_behind(im, a, i):
                fr['order'] = sprite.FF_HEAD_BEHIND
                n += 1
            else:
                fr['order'] = 0
    return n


def apply_imf_all(vfs, sprites, warn=None):
    """IMF flags on every body sprite of `sprites` the client has an IMF for; -> {sprite name: flagged frames}."""
    done = {}
    for sp in sprites:
        if sp['kind'] != sprite.KIND_BODY:
            continue
        p = imf_path(sp['name'])
        if p is None:
            continue
        d = _read(vfs, p)
        if d is None:
            continue
        try:
            done[sp['name']] = apply_imf(sp, IMF.parse(d))
        except (ValueError, struct.error) as e:
            if warn is not None:
                warn.append('equip: imf of %s: %s' % (sp['name'], e))
    return done


# ------------------------------------------------------------------ robes
def robe_table(vfs):
    """{robe view id: client folder name} from spriterobeid.lub + spriterobename.lub ({} when the client has none)."""
    from . import lub
    try:
        ids = dict((k, v) for s, k, v in lub.parse(vfs.read(ROBE_IDS)) if s == 'SPRITE_ROBE_IDs' and isinstance(v, int))
        out = {}
        for s, k, v in lub.parse(vfs.read(ROBE_NAMES)):
            if s == 'RobeNameTable' and isinstance(k, tuple) and k[0] == 'gt' and isinstance(v, str) and k[2] in ids:
                out[ids[k[2]]] = v
        return out
    except (KeyError, ValueError, IndexError, lub.LubError):
        return {}


def robe_sprites(vfs, sex, warn=None, views=ROBE_VIEWS):
    """-> [rb_<view>_<sex> sprite dicts] for the robe views the client has a Novice sprite for (the world draws the robe on the Novice body)."""
    names = robe_table(vfs)
    out = []
    sw = SEX_WORD[sex]
    for v in views:
        nm = names.get(v)
        if not nm:
            if warn is not None and sex == 'm':
                warn.append('equip: robe view %d has no client name (skipped)' % v)
            continue
        base = PATH_ROBE % (nm, sw, sw)
        try:
            out.append(sprite.convert_sprite(robe_name(v, sex), sprite.KIND_OTHER, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=items.PLAYER_KEEP, still_anims=(sprite.ANIM_IDLE, sprite.ANIM_SIT)))
        except KeyError:
            if warn is not None and sex == 'm':
                warn.append('equip: robe %d not in the client data (skipped)' % v)
        except (sprite.FormatError, ValueError, IndexError, struct.error) as e:
            if warn is not None:
                warn.append('equip: robe %d: %s' % (v, e))
    return out


def equip_sprites(vfs, warn=None):
    """what api.sprite_sections appends: indexed hair + body (both sexes) and robes; -> (sprites, rpal bytes or None)."""
    hair, bodies, sp = {}, {}, []
    for sx in SEXES:
        for h in hair_sprites(vfs, sx, warn):
            st = int(h['name'].split('_')[1])
            hair[(sx, st)] = h
            sp.append(h)
        b = body_sprite(vfs, sx, warn)
        if b is not None:
            bodies[sx] = b
            sp.append(b)
    if not hair and not bodies:
        return [], None
    for sx in SEXES:
        sp += robe_sprites(vfs, sx, warn)
    rpal, _got = build_rpal(vfs, hair, bodies, warn)
    return sp, rpal
