"""RO original interface atlas (w42 lane ui-a, docs/design/ro_ui.md): the 'roui' pack = ONE RPG1 section ROUI with the basic_interface
window pieces and the item icons of the PLAYER's own client (data.grf), read only, nothing of the game in the repo or the world.

  ROUI (section version 1):
    header 8 x u32: version 1, atlas width, atlas height, format 0 (RGBA32, rows TOP first: texture v 0 = the image top, the GoldSrcVR/RoseUi
                    shader's _Uv convention), sprite count, name bytes, pixel offset (from the section start), 0
    sprites 8 x i32: name offset, name length, x, y (from the top), w, h, kind (0 interface piece, 1 item icon, 2 generated, 3 skill icon), item / skill id (icons, else 0)
    names (ASCII, no terminator), padded to 4, then the pixels (w * h * 4)
  Magenta (255, 0, 255) texels are transparent (img.key_magenta), like every RO interface bitmap.

The world (GsvrRoWin.BuildFromBytes) looks sprites up by NAME: interface pieces by their file stem (UI_FILES), icons as 'ic_<item id>' / 'sk_<skill id>', plus the
generated 'white' (solid 4 x 4, for fills / the cursor). A missing file only drops that sprite (warning); the window code draws without it.
"""
import os
import struct
import sys

from . import img, items, mobsets, rotxt, sprite
from .grf import Vfs

_ROSE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rose_conv')
if _ROSE not in sys.path:
    sys.path.append(_ROSE)
import rpg1 as P            # noqa: E402

UI_PACK = 'roui'
UI_REV = 4                                          # 2 = w42 integrate: + the minimap pictures map_<name> (ro_conv.uimap, 64 px); 3 = w43 txt: + window art pieces + the ROTX text section (w43 lobby, on its own branch: + the ROLG login pieces lg_<stem>); 4 = w44 integrate: ROUI + ROLG + ROTX together (one rebuild)
TAG_ROUI = b'ROUI'
TAG_ROLG = b'ROLG'                                  # w43 lobby: the login pieces in their OWN atlas section (same layout as ROUI; the 1024 page of ROUI is full with icons + minimaps)
SECTION_VERSION = 1
FMT_RGBA32 = 0
KIND_UI, KIND_ICON, KIND_GEN, KIND_SKILL, KIND_MAP = 0, 1, 2, 3, 4
MAP_PIC = 64                                        # minimap pictures in the atlas (the window stretches them to 96 / 128 px; 50 maps x 64^2 fit next to the icons)
HDR = struct.Struct('<8I')
REC = struct.Struct('<8i')
MAX_PAGE = 1024
PATH_UI = 'data/texture/유저인터페이스/basic_interface/%s.bmp'
# the pieces the w42 windows draw (GsvrRoWin): Basic Info (expanded / compact + gauges + menu buttons), title bars, inventory (cells, tabs, footer),
# equipment, hotkey bar, window buttons. Order is irrelevant (looked up by name).
UI_FILES = (
    'basewin_bg', 'basewin_mini', 'gze_bg', 'gze_bg1', 'gzeblue_left', 'gzeblue_mid', 'gzeblue_right', 'gzered_left', 'gzered_mid', 'gzered_right',
    'titlebar_left', 'titlebar_mid', 'titlebar_right', 'itemwin_mid', 'tab_itm_01', 'tab_itm_02', 'tab_itm_03', 'btnbar_left', 'btnbar_mid', 'btnbar_right',
    'equipwin_bg', 'shortitem_bg', 'sys_close_off', 'sys_close_on', 'sys_mini_off', 'sys_mini_on', 'sys_base_off', 'sys_base_on',
    'btn_items_off', 'btn_items_on', 'btn_equip_off', 'btn_equip_on', 'btn_skill_off', 'btn_skill_on', 'btn_status_off', 'btn_status_on',
    'btn_option_off', 'btn_option_on', 'btn_map_off', 'btn_map_on',
    # w43 txt: the original art of the ui-b windows (GsvrRoUiB.LArt): status face, skill head / foot bars + level-up arrows, tabs, buttons, rounded frame corners
    'statwin0_bg', 'skill_upbar_left', 'skill_upbar_mid', 'skill_upbar_right', 'skill_botbar_left', 'skill_botbar_mid', 'skill_botbar_right', 'skill_up_a', 'skill_up_b',
    'tab_l', 'tab_m', 'tab_r', 'tab_a_l', 'tab_a_m', 'tab_a_r', 'btn_out_left', 'btn_out_mid', 'btn_out_right', 'btn_over_mid',
    'sysboxs_lu', 'sysboxs_ru', 'sysboxs_ld', 'sysboxs_rd',
)

# w43 lobby (docs/design/ro_lobby.md): the classic login flow pieces, data/texture/<ui>/login_interface/<stem>.bmp, drawn by the lobby panels (GsvrRoLobby) as 'lg_<stem>':
# win_select = the character select window, win_make = "Make Your Characters", arw-<stat>0 / 1 = the six stat hexagon corner arrows (off / on), name-edit = the name field,
# btn_ok / btn_cancel / btn_make / btn_back / btn_next / btn_close = buttons, etc_empty = the empty slot, box_select = the selection frame, chk_* = check boxes.
# (Delete / Charge / Login of the original are drawn from the pieces above + our own text; the owner's reference client is a custom one: missing pieces only drop that sprite.)
PATH_LOGIN = 'data/texture/유저인터페이스/login_interface/%s.bmp'
LOGIN_PREFIX = 'lg_'
LOGIN_FILES = (
    'win_select', 'win_make', 'name-edit', 'box_select', 'etc_empty',
    'arw-str0', 'arw-str1', 'arw-agi0', 'arw-agi1', 'arw-vit0', 'arw-vit1', 'arw-int0', 'arw-int1', 'arw-dex0', 'arw-dex1', 'arw-luk0', 'arw-luk1',
    'btn_ok', 'btn_cancel', 'btn_make', 'btn_back', 'btn_next', 'btn_close', 'chk_saveon', 'chk_saveoff',
)


def collect_login(vfs, warn=None):
    """-> [(name 'lg_<stem>', KIND_UI, 0, w, h, rgba)]: the login / select / make-character pieces the client has (missing ones are a warning)."""
    out, miss = [], []
    for stem in LOGIN_FILES:
        try:
            w, h, px = img.decode(vfs.read(PATH_LOGIN % stem), stem + '.bmp')
            px, _a = img.key_magenta(px, w, h)
        except (KeyError, img.ImageError, ValueError, IndexError, struct.error):
            miss.append(stem)
            continue
        out.append((login_name(stem), KIND_UI, 0, w, h, px))
    if miss and warn is not None:
        warn.append('ui: %d login / character select pieces not in the client data (%s)' % (len(miss), ' '.join(miss[:8])))
    return out


def login_name(stem):
    """Atlas name of a login piece, == GsvrRoLobby.LTex."""
    return LOGIN_PREFIX + stem


# the skill icons of the skills the world knows (rAthena ids + AEGIS names of unity/Assets/GoldSrcVR/Editor/ro_skills.txt, GENERATED there by tools/ro_skills_gen.py;
# copied here because the companion ships without the Unity tree): the client keeps them as item\\<aegis lower>.bmp; the world draws 'sk_<id>'.
SKILL_ICONS = (
    (1, 'nv_basic'), (142, 'nv_firstaid'), (2, 'sm_sword'), (3, 'sm_twohand'), (4, 'sm_recovery'), (5, 'sm_bash'), (6, 'sm_provoke'),
    (7, 'sm_magnum'), (8, 'sm_endure'), (144, 'sm_movingrecovery'), (145, 'sm_fatalblow'), (146, 'sm_autoberserk'), (9, 'mg_srecovery'),
    (10, 'mg_sight'), (11, 'mg_napalmbeat'), (12, 'mg_safetywall'), (13, 'mg_soulstrike'), (14, 'mg_coldbolt'), (15, 'mg_frostdiver'),
    (16, 'mg_stonecurse'), (17, 'mg_fireball'), (18, 'mg_firewall'), (19, 'mg_firebolt'), (20, 'mg_lightningbolt'), (21, 'mg_thunderstorm'),
    (157, 'mg_energycoat'), (43, 'ac_owl'), (44, 'ac_vulture'), (45, 'ac_concentration'), (46, 'ac_double'), (47, 'ac_shower'),
    (147, 'ac_makingarrow'), (148, 'ac_chargearrow'), (22, 'al_dp'), (23, 'al_demonbane'), (24, 'al_ruwach'), (25, 'al_pneuma'), (26, 'al_teleport'),
    (27, 'al_warp'), (28, 'al_heal'), (29, 'al_incagi'), (30, 'al_decagi'), (31, 'al_holywater'), (32, 'al_crucis'), (33, 'al_angelus'),
    (34, 'al_blessing'), (35, 'al_cure'), (156, 'al_holylight'), (36, 'mc_inccarry'), (37, 'mc_discount'), (38, 'mc_overcharge'),
    (39, 'mc_pushcart'), (40, 'mc_identify'), (41, 'mc_vending'), (42, 'mc_mammonite'), (153, 'mc_cartrevolution'), (154, 'mc_changecart'),
    (155, 'mc_loud'), (48, 'tf_double'), (49, 'tf_miss'), (50, 'tf_steal'), (51, 'tf_hiding'), (52, 'tf_poison'), (53, 'tf_detoxify'),
    (149, 'tf_sprinklesand'), (150, 'tf_backsliding'), (151, 'tf_pickstone'), (152, 'tf_throwstone'),
)


def icon_ids():
    """every item id the world can put into a bag: the item lane's icons + every monster map's drops (sorted, unique)."""
    s = set(items.ICON_ITEMS)
    for v in mobsets.DROP_ITEMS.values():
        s.update(v)
    return tuple(sorted(s))


def _piece(vfs, stem):
    w, h, px = img.decode(vfs.read(PATH_UI % stem), stem + '.bmp')
    px, _a = img.key_magenta(px, w, h)
    return w, h, px


def collect(vfs, warn=None, icons=None, maps=None):
    """-> [(name, kind, item id, w, h, rgba)]: the interface pieces the client has, the icons it has, 'white'."""
    out, miss = [], []
    for stem in UI_FILES:
        try:
            w, h, px = _piece(vfs, stem)
        except (KeyError, img.ImageError, ValueError, IndexError, struct.error):
            miss.append(stem)
            continue
        out.append((stem, KIND_UI, 0, w, h, px))
    if miss and warn is not None:
        warn.append('ui: %d interface pieces not in the client data (%s)' % (len(miss), ' '.join(miss[:8])))
    ids = icon_ids() if icons is None else tuple(icons)
    imiss = []
    try:
        res = items.item_res(vfs)
    except KeyError:
        res = {}
        if warn is not None:
            warn.append('ui: item resource table not in the client data (no item icons)')
    for iid in ids:
        r = res.get(iid)
        try:
            if not r:
                raise KeyError(iid)
            w, h, px = img.decode(vfs.read(items.PATH_ICON % r), r + '.bmp')
            if w > 64 or h > 64:
                raise ValueError('icon size')
            px, _a = img.key_magenta(px, w, h)
        except (KeyError, img.ImageError, ValueError, IndexError, struct.error):
            imiss.append(iid)
            continue
        out.append(('ic_%d' % iid, KIND_ICON, iid, w, h, px))
    if imiss and res and warn is not None:
        warn.append('ui: %d item icons not in the client data (%s)' % (len(imiss), ' '.join(str(i) for i in imiss[:12])))
    smiss = []
    for sid, aegis in SKILL_ICONS:
        try:
            w, h, px = img.decode(vfs.read(items.PATH_ICON % aegis), aegis + '.bmp')
            if w > 64 or h > 64:
                raise ValueError('icon size')
            px, _a = img.key_magenta(px, w, h)
        except (KeyError, img.ImageError, ValueError, IndexError, struct.error):
            smiss.append(aegis)
            continue
        out.append(('sk_%d' % sid, KIND_SKILL, sid, w, h, px))
    if smiss and warn is not None:
        warn.append('ui: %d skill icons not in the client data (%s)' % (len(smiss), ' '.join(smiss[:8])))
    if maps:
        from . import uimap
        pics, mmiss = uimap.load(vfs, maps, MAP_PIC)
        for tex, w, h, px in pics:
            out.append((tex, KIND_MAP, 0, w, h, px))
        if mmiss and warn is not None:
            warn.append('ui: %d minimap pictures not in the client data (%s)' % (len(mmiss), ' '.join(mmiss[:8])))
    out.append(('white', KIND_GEN, 0, 4, 4, b'\xff' * 64))
    return out


def build_atlas(pieces, max_page=MAX_PAGE, warn=None):
    """-> (W, H, rgba top-first, [(name, kind, id, x, y, w, h)]). Everything on ONE page: icons that do not fit are dropped (last first, warning)."""
    pieces = list(pieces)
    while True:
        pages, places = sprite.pack_atlas([(p[3], p[4]) for p in pieces], max_page=max_page, pad=1)
        if len(pages) <= 1:
            break
        drop = max((i for i, p in enumerate(pieces) if p[1] == KIND_MAP), default=-1)          # minimap pictures go first, then icons, never the pieces
        if drop < 0:
            drop = max((i for i, p in enumerate(pieces) if p[1] in (KIND_ICON, KIND_SKILL)), default=-1)
        if drop < 0:
            raise sprite.FormatError('ui pieces do not fit one %d page' % max_page)
        if warn is not None and not any(w.startswith('ui: atlas full') for w in warn):
            warn.append('ui: atlas full, sprites dropped from %s on (minimap pictures first, then icons)' % pieces[drop][0])
        del pieces[drop]
    W, H = pages[0] if pages else (64, 64)
    buf = bytearray(W * H * 4)
    table = []
    for (name, kind, iid, w, h, px), (_pg, x, y) in zip(pieces, places):
        for r in range(h):
            d = ((y + r) * W + x) * 4
            buf[d:d + w * 4] = px[r * w * 4:(r + 1) * w * 4]
        table.append((name, kind, iid, x, y, w, h))
    return W, H, bytes(buf), table


def enc_roui(W, H, rgba, table):
    names = b''
    recs = b''
    for name, kind, iid, x, y, w, h in table:
        nb = name.encode('ascii')
        recs += REC.pack(len(names), len(nb), x, y, w, h, kind, iid)
        names += nb
    names += b'\x00' * (-len(names) % 4)
    pix_off = HDR.size + len(recs) + len(names)
    return HDR.pack(SECTION_VERSION, W, H, FMT_RGBA32, len(table), len(names), pix_off, 0) + recs + names + rgba


def dec_roui(d):
    """-> (W, H, {name: (kind, id, x, y, w, h)}, rgba) - tests / tools."""
    ver, W, H, fmt, n, nb, po, _z = HDR.unpack_from(d, 0)
    if ver != SECTION_VERSION or fmt != FMT_RGBA32:
        raise ValueError('ROUI version / format')
    out = {}
    no = HDR.size + n * REC.size
    for i in range(n):
        off, ln, x, y, w, h, kind, iid = REC.unpack_from(d, HDR.size + i * REC.size)
        out[d[no + off:no + off + ln].decode('ascii')] = (kind, iid, x, y, w, h)
    return W, H, out, d[po:po + W * H * 4]


def ui_sections(vfs, warn=None, icons=None, maps=None, folder=None):
    """-> [(tag, version, count, data)] = [ROUI] (+ [ROTX] when folder (the client folder with its System/*.lub) is given and has texts; an empty list when the client has no
    interface piece at all). maps = world map names for the minimap pictures and the town markers."""
    pieces = collect(vfs, warn, icons, maps)
    if not any(p[1] == KIND_UI for p in pieces):
        if warn is not None:
            warn.append('ui: no interface pieces in the client data (the world keeps its plain windows)')
        return []
    W, H, rgba, table = build_atlas(pieces, warn=warn)
    out = [(TAG_ROUI, SECTION_VERSION, len(table), enc_roui(W, H, rgba, table))]
    lg = collect_login(vfs, warn)
    if lg:
        W, H, rgba, table = build_atlas(lg, warn=warn)
        out.append((TAG_ROLG, SECTION_VERSION, len(table), enc_roui(W, H, rgba, table)))
    if folder:
        tx = rotxt.section(folder, vfs, tuple(icon_ids() if icons is None else icons), tuple(maps or ()), warn)
        if tx is not None:
            out.append(tx)
    return out


def build(name, folder, out_path, progress=None, cancel=None, log=None, icons=None):
    """the 'roui' pack -> out_path (RPG1 container with one ROUI section). -> dict(name, bytes, sprites, warnings)."""
    from .api import check_install, BuildError, Cancelled
    if name != UI_PACK:
        raise BuildError('not the ui pack: %r' % name)
    ok, why = check_install(folder)
    if not ok:
        raise BuildError('Ragnarok folder not usable: %s' % why)

    def tick(stage, f):
        if cancel is not None and cancel():
            raise Cancelled(name)
        if progress is not None:
            progress(stage, f)
    tick('grf', 0.0)
    vfs = Vfs(folder)
    warns = list(vfs.errors)
    tick('ui', 0.2)
    from .api import MAP_ORDER
    secs = ui_sections(vfs, warns, icons, MAP_ORDER, folder)
    if not secs:
        raise BuildError('no interface pieces in the client data')
    data = P.write_pack(secs)
    tick('write', 0.9)
    part = out_path + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    try:
        with open(part, 'wb') as f:
            f.write(data)
        os.replace(part, out_path)
    finally:
        if os.path.exists(part):
            os.remove(part)
    if log is not None:
        log('ui: %d sprites, %d section(s), %d bytes' % (secs[0][2], len(secs), len(data)))
    tick('write', 1.0)
    return {'name': name, 'bytes': len(data), 'sprites': secs[0][2], 'warnings': warns}
