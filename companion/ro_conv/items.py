"""Item look of the Ragnarok world (docs/design/ro_items.md): headgear / weapon / shield sprites and item icons from the player's own client.

Everything is read from the client (read only) and appended to the player sprite set of the start pack (RSAT / RSAN, sprite.py) under new
names; nothing of the game is in git. Names (ASCII, the world looks them up with GsvrRoSpriteSet.FindSprite):

  hg_<view>_<m|f>   headgear, view = the client's accessory id (rAthena item_db "View"); one sprite for Head_Top / Mid / Low alike
  wp_<type>_<m|f>   weapon by client weapon type (1 dagger, 2 sword, ... = ro_itemdb.SUBS), wp_i<item id>_<m|f> when the client has a sprite of
                    that very item (e.g. Main Gauche); sh_<view>_<m|f> shield (view 1 Guard, 2 Buckler, 3 Shield, 4 Mirror Shield)
  ic_<item id>              the 24 x 24 inventory icon (8 identical one-frame clips, the world uses the frame's atlas rectangle)

All gear sprites share the body's action layout (anim * 8 + dir) and the body's origin: a weapon / shield is drawn at the body's feet origin,
a headgear exactly like the head (offset = body anchor - its own anchor). Weapons / shields are EMPTY in idle and walk (as in the original: the
weapon is only drawn in standby / attack), so the converter keeps PLAYER_KEEP = idle, walk, standby, attack2, attack3 for body, head and gear.
How the tables are found: accessory view -> client name through AccessoryId.lub + AccName.lub (lub.py), weapon type -> name through
WeaponTable.lub, item id -> icon through idnum2itemresnametable.txt; the shield names are the fixed classic four.
"""
import re
import zlib

from . import img, lub, sprite
from .itemview import ITEM_VIEW

# anims kept for the character: idle walk standby attack2 attack3 (sprite.PLAYER_ANIMS indices 0 1 4 10 11); weapons are visible in 4 / 10 / 11 only
ANIM_STANDBY, ANIM_ATTACK2, ANIM_ATTACK3 = 4, 10, 11
PLAYER_KEEP = (sprite.ANIM_IDLE, sprite.ANIM_WALK, ANIM_STANDBY, ANIM_ATTACK2, ANIM_ATTACK3)
STILL = (sprite.ANIM_IDLE, sprite.ANIM_SIT)     # idle frames after the first are head-turn poses

# Which looks the start pack carries (item ids; the look is found through ITEM_VIEW): the shops' weapons + shield, the common headgear,
# the starting gear. A new item only needs its id here (and its icon in ICON_EXTRA when it is not a gear item).
GEAR_ITEMS = (1201, 1204, 1207, 1101, 1104, 1107, 1501, 1601, 2101, 2102,                   # Knife Cutter Main Gauche, Sword Falchion Blade, Club, Rod, Guard
              2201, 2203, 2205, 2206, 2207, 2208, 2210, 2211, 2212, 2213, 2214, 2216, 2220, 2223, 5001, 5005)   # headgear
ICON_EXTRA = (501, 502, 503, 504, 505, 506, 507, 512, 515, 601, 602, 611, 645, 656, 1065, 1750, 1751, 2239, 2301, 4001, 4002, 4003, 4004, 4006,
              619, 620, 622, 705, 713, 909, 915, 935, 938, 949, 1002, 1010, 1202, 1602, 2102, 2262)
ICON_ITEMS = tuple(sorted(set(GEAR_ITEMS) | set(ICON_EXTRA)))

SHIELD_NAMES = {1: '가드', 2: '버클러', 3: '쉴드', 4: '미러쉴드'}
JOB_NOVICE = '초보자'
SEX_WORD = {'m': '남', 'f': '여'}
PATH_ACC = 'data/sprite/악세사리/%s/%s%s'               # sex word, sex word, suffix (suffix starts with '_')
PATH_WEAPON = 'data/sprite/인간족/%s/%s_%s%s'            # job, job, sex word, suffix
PATH_SHIELD = 'data/sprite/방패/%s/%s_%s_%s'            # job, job, sex word, name
PATH_ICON = 'data/texture/유저인터페이스/item/%s.bmp'
LUB_DIRS = ('data/luafiles514/lua files/datainfo/', 'data/lua files/datainfo/')
RES_TABLE = 'data/idnum2itemresnametable.txt'
_E = (sprite.FormatError, ValueError, IndexError, KeyError, lub.LubError, img.ImageError)


def _read_first(vfs, name):
    for d in LUB_DIRS:
        if vfs.exists(d + name):
            return vfs.read(d + name)
    raise KeyError(name)


def item_res(vfs):
    """{item id: client resource name} from idnum2itemresnametable.txt ('501#redpotion#' per line, CP949)."""
    out = {}
    txt = vfs.read(RES_TABLE).decode('cp949', 'replace')
    for ln in txt.replace('\r', '').split('\n'):
        p = ln.split('#')
        if len(p) >= 2 and p[0].strip().isdigit() and p[1].strip():
            out[int(p[0])] = p[1].strip()
    return out


def acc_names(vfs):
    """{accessory view id: client file suffix, e.g. 12: '_고글'} from AccessoryId.lub (name -> id) + AccName.lub (name -> suffix)."""
    ids = dict((k, v) for s, k, v in lub.parse(_read_first(vfs, 'accessoryid.lub')) if s == 'ACCESSORY_IDs' and isinstance(v, int))
    out = {}
    for s, k, v in lub.parse(_read_first(vfs, 'accname.lub')):
        if s == 'AccNameTable' and isinstance(k, tuple) and k[0] == 'gt' and isinstance(v, str) and k[2] in ids:
            out[ids[k[2]]] = v
    return out


def weapon_names(vfs):
    """{weapon type index: client file suffix, e.g. 1: '_단검'} from WeaponTable.lub."""
    return dict((k, v) for s, k, v in lub.parse(_read_first(vfs, 'weapontable.lub'))
                if s == 'WeaponNameTable' and isinstance(k, int) and isinstance(v, str) and v)


def plan(item_ids, view=None):
    """item ids -> ordered [(sprite name, kind, key)]: kind 'h' key = view, 'w' key = (type, item id), 's' key = view. Duplicates are merged, ids
    without a look (not in ITEM_VIEW, or no view) are skipped."""
    view = ITEM_VIEW if view is None else view
    seen, out = set(), []
    for iid in item_ids:
        v = view.get(iid)
        if v is None:
            continue
        kind, sub, vw = v
        if kind == 'h':
            nm, key = 'hg_%d' % vw, vw
        elif kind == 'w':
            nm, key = 'wp_%d' % sub, (sub, iid)
        elif kind == 's':
            nm, key = 'sh_%d' % vw, vw
        else:
            continue
        if nm not in seen:
            seen.add(nm)
            out.append((nm, kind, key))
        if kind == 'w':                                      # the item's own sprite (wp_i<id>), when the client has one
            nm2 = 'wp_i%d' % iid
            if nm2 not in seen:
                seen.add(nm2)
                out.append((nm2, 'wi', (sub, iid)))
    return out


def _load(vfs, name, base):
    return sprite.convert_sprite(name, sprite.KIND_OTHER, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=PLAYER_KEEP, still_anims=STILL)


def gear_sprites(vfs, sex, warn=None, item_ids=GEAR_ITEMS, view=None):
    """-> [sprite dict] for `sex` ('m' / 'f'); the name ends in '_m' / '_f'. A look the client does not have is skipped with a
    'gear: ...' warning (the world then shows the body without it). Never raises for missing files."""
    sw = SEX_WORD[sex]
    suffix = '_' + sex
    out = []
    try:
        acc = acc_names(vfs)
    except _E:
        acc = {}
        if warn is not None:
            warn.append('gear: accessory name tables not in the client data (no headgear)')
    try:
        wn = weapon_names(vfs)
    except _E:
        wn = {}
        if warn is not None:
            warn.append('gear: weapon table not in the client data (no weapons)')
    for nm, kind, key in plan(item_ids, view):
        path = None
        if kind == 'h':
            sfx = acc.get(key)
            if sfx:
                path = PATH_ACC % (sw, sw, sfx)
        elif kind == 'w':
            sfx = wn.get(key[0])
            if sfx:
                path = PATH_WEAPON % (JOB_NOVICE, JOB_NOVICE, sw, sfx)
        elif kind == 'wi':
            p = PATH_WEAPON % (JOB_NOVICE, JOB_NOVICE, sw, '_%d' % key[1])
            if vfs.exists(p + '.spr') and vfs.exists(p + '.act'):
                path = p
            else:
                continue                                      # no own sprite: the generic type sprite is used (no warning)
        elif kind == 's':
            sfx = SHIELD_NAMES.get(key)
            if sfx:
                path = PATH_SHIELD % (JOB_NOVICE, JOB_NOVICE, sw, sfx)
        if path is None:
            if warn is not None:
                warn.append('gear: %s has no client name (skipped)' % nm)
            continue
        try:
            out.append(_load(vfs, nm + suffix, path))
        except KeyError:
            if warn is not None:
                warn.append('gear: %s not in the client data (skipped)' % nm)
        except _E as e:
            if warn is not None:
                warn.append('gear: %s: %s' % (nm, e))
    return out


def icon_sprite(item_id, rgba, w, h):
    """one icon as a sprite: 8 identical one-frame clips (the clip table must be a multiple of 8), image keyed like convert_sprite's."""
    key = (w, h, zlib.crc32(rgba), len(rgba))
    fr = dict(img_key=key, left=-(w // 2), top=-h, w=w, h=h, anchor=None)
    return dict(name='ic_%d' % item_id, kind=sprite.KIND_OTHER, clips=[dict(delay_ms=100, frames=[dict(fr)]) for _ in range(8)], images={key: rgba})


def icon_sprites(vfs, warn=None, item_ids=ICON_ITEMS):
    """-> [icon sprite dicts] (bmp, magenta key -> alpha); items the client has no icon for are skipped with one summary warning."""
    try:
        res = item_res(vfs)
    except KeyError:
        if warn is not None:
            warn.append('gear: item resource table not in the client data (no icons)')
        return []
    out, miss = [], []
    for iid in item_ids:
        r = res.get(iid)
        try:
            if not r:
                raise KeyError(iid)
            w, h, px = img.decode(vfs.read(PATH_ICON % r), r + '.bmp')
            px, _a = img.key_magenta(px, w, h)
            if w > 64 or h > 64:
                raise ValueError('icon size')
            out.append(icon_sprite(iid, px, w, h))
        except _E:
            miss.append(iid)
    if miss and warn is not None:
        warn.append('gear: %d item icons not in the client data (%s)' % (len(miss), ' '.join(str(i) for i in miss[:12])))
    return out


def extra_sprites(vfs, sex, warn=None, item_ids=GEAR_ITEMS, icon_ids=ICON_ITEMS):
    """what api.sprite_sections appends after the player / monster / NPC sprites: gear of both sexes, then the icons."""
    out = gear_sprites(vfs, sex, warn, item_ids)
    other = {'m': 'f', 'f': 'm'}.get(sex)
    if other:
        out += gear_sprites(vfs, other, None, item_ids)
    return out + icon_sprites(vfs, warn, icon_ids)
