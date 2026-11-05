#!/usr/bin/env python3
"""Glue screens (main menu, LAN, create game, lobby, loading) of the player's install for the world's start menu
(maintainer 2026-10-21: "Lobby 1 zu 1 wie im Original, direkt beim Laden der Welt"; docs/ui_plan.md L1-L6).

FDF (tools/fdf.py) + layout (tools/w3ui.Layout) -> per screen a draw list in screen units (0.8 x 0.6, origin bottom
left): textured quads (backdrops: background stretched or tiled, inset; the 8-cell border strip as corners + edges)
and texts (GlobalStrings resolved), plus the buttons as hotspots. Frame types: FRAME / DIALOG containers, BACKDROP,
GLUETEXTBUTTON / GLUEBUTTON / TEXTBUTTON (ControlBackdrop = a child backdrop), TEXT; SPRITE (3D models) recorded only.

  python3 tools/w3glue.py [--screen MainMenu] [--install input/war3]   -> work/ui/glue_<screen>.png (preview)

Nothing of the install goes into the repo (work/ is git-ignored).
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import w3fdf as fdf                     # noqa: E402
import w3ui                    # noqa: E402

GLUE = 'UI\\FrameDef\\Glue\\'
SCREENS = {                    # screen -> (fdf file, root frame)
    'MainMenu': ('MainMenu.fdf', 'MainMenuFrame'),
    'LocalMultiplayerJoin': ('LocalMultiplayerJoin.fdf', 'LocalMultiplayerJoin'),
    'LocalMultiplayerCreate': ('LocalMultiplayerCreate.fdf', 'LocalMultiplayerCreate'),
    'GameChatroom': ('GameChatroom.fdf', 'GameChatroom'),
    'Loading': ('Loading.fdf', 'Loading'),
}
# containers the game fills with another FDF at run time (container -> file, root); the root fills the container
# (NOTES_glue.md: every fill is SetAllPoints)
COMPOSE = {
    'TeamSetupContainer': ('TeamSetup.fdf', 'TeamSetup'),
    'MapInfoPaneContainer': ('MapInfoPane.fdf', 'MapInfoPane'),
    'MapListContainer': ('MapListBox.fdf', 'MapListBox'),
    'GameListContainer': ('ListBoxWar3.fdf', 'ListBoxWar3'),
    'AdvancedOptionsContainer': ('AdvancedOptionsDisplay.fdf', 'AdvancedOptionsDisplay'),
    'AdvancedOptionsPaneContainer': ('AdvancedOptionsPane.fdf', 'AdvancedOptionsPane'),
}
# SAVE-MENU (maintainer 2026-11-02): the game menu's own panels, drawn like the glue screens. The in-game menu (F10) is built from
# UI\\FrameDef\\UI\\EscMenu*.fdf; the game positions the panels in code, centred on the screen [inf], and their background /
# border are the skin keys of the Alliance dialog's AllianceBackdrop (EscMenuBackground / EscMenuBorder, corner 0.048,
# insets 0.01 [obs work/ui/fdf_ingame.json]). key -> (file, root frame, panel (W, H), frames of the root the screen hides)
ESC = 'UI\\FrameDef\\UI\\'
ESC_SCREENS = {
    'EscMenuMain': ('EscMenuMainPanel.fdf', 'EscMenuMainPanel', (0.288, 0.384), ('EndGamePanel', 'ConfirmQuitPanel', 'HelpPanel',
                                                                                'TipsPanel', 'ConfirmRestartPanel', 'QuitPanel')),
    'EscMenuSave': ('EscMenuSaveGamePanel.fdf', 'EscMenuSaveGamePanel', (0.432, 0.288),
                    ('EscMenuOverwriteContainer', 'EscMenuDeleteContainer', 'LoadOnly')),
    'EscMenuLoad': ('EscMenuSaveGamePanel.fdf', 'EscMenuSaveGamePanel', (0.432, 0.288),
                    ('EscMenuOverwriteContainer', 'EscMenuDeleteContainer', 'SaveOnly')),
}
ESC_TITLES = ('WouldTheRealOptionsTitleTextPleaseStandUp', 'LoadGameTitleText', 'SaveGameTitleText')
ESC_VALUES = ('SaveGameFileEditBoxText', 'OverwriteMessageText', 'DeleteMessageText')   # texts the world fills (T lines)
HIDDEN = ('RealmSelect', 'AdvancedOptionsPanel', 'DebugTestFrame', 'Test')   # NOTES_glue start visibility      # frames the game hides at start (the Battle.net gateway picker)
# MapInfoPane frames the game places in code (NOTES_glue.md "MapInfoPane layout", stacked mode, the one Create /
# Join / GameChatroom use): frame -> (point, relative frame, relative point, x, y); label rows are W wide, 0.013 high
# [inf text height]; each *Value keeps its FDF TOPLEFT-to-label anchor, gets the label's size and is right-justified
MAPINFO_ROWS = (('SuggestedPlayersLabel', 0x10, ('TOP', 'MinimapImageBackdrop', 'BOTTOM', 0.0, 0.0115)),
                ('MapSizeLabel', 0x20, ('TOP', None, 'BOTTOM', 0.0, -0.002)),
                ('MapTilesetLabel', 0x40, ('TOP', None, 'BOTTOM', 0.0, -0.002)),
                ('MapDescLabel', 0x80, ('TOP', None, 'BOTTOM', 0.0, -0.008)))
MAPINFO_BITS = {'LocalMultiplayerCreate': 0xffff, 'LocalMultiplayerJoin': 0xff7f, 'GameChatroom': 0xff7f}
MAPINFO_UNUSED = ('NumPlayersLabel', 'NumPlayersValue', 'ElapsedTimeLabel', 'ElapsedTimeValue')   # not in 1.21's code
MAPINFO_NOMAP = ('MaxPlayersIcon', 'AuthIcon')      # hidden while no map is set [obs 0x6f24d0c0]
TEXT_ROW_H = 0.013                                  # [inf] a label's height (font size, the FDF gives none)
MAPDESC_H = 0.10625                                 # Create: description height limit [obs 0x6f245b6a]
LOADING_SLOTS = ('LoadingTitleText', 'LoadingSubtitleText', 'LoadingText', 'LoadingMeleeMapName')   # the map's
# loading texts (custom map: title / subtitle / text, wrapped, max height 0.16875; melee: the map name [obs 0x6f24325d])
LISTS = ('MapListBox', 'ListBoxWar3', 'TeamSetupContainer', 'FileListFrame')   # filled by the world at run time (L lines): lists,
# and the lobby's player slots (TeamSetup is laid out in code, NOTES_glue.md "TeamSetup")
LIST_ICONS = {'melee': 'UI\\Widgets\\Glues\\icon-file-melee.blp', 'ums': 'UI\\Widgets\\Glues\\icon-file-ums.blp',
              'folder': 'UI\\Widgets\\Glues\\icon-folder.blp', 'up': 'UI\\Widgets\\Glues\\icon-folder-up.blp'}
BUTTONS = ('GLUETEXTBUTTON', 'GLUEBUTTON', 'TEXTBUTTON', 'BUTTON', 'GLUECHECKBOX', 'GLUEPOPUPMENU', 'POPUPMENU')
# the border strip (BackdropEdgeFile): 8 square cells left to right (WC3 modding convention [ext], checked against
# the previews): left, right, top, bottom, top-left, top-right, bottom-left, bottom-right
EDGE_CELLS = ('L', 'R', 'T', 'B', 'UL', 'UR', 'BL', 'BR')


class Glue(w3ui.Layout):
    def __init__(self, install, race='Human'):
        w3ui.Layout.__init__(self, install, race)
        self.strings = {}
        self.cur, self.place, self.justify, self.hide = None, {}, {}, set()
        for f in ('UI\\FrameDef\\GlobalStrings.fdf', 'UI\\FrameDef\\NetworkStrings.fdf'):
            raw = install.read(f, missing_ok=True)
            if raw:
                self.strings.update(fdf.parse(raw.decode('utf-8', 'replace'))['strings'])   # string tables are UTF-8

    def text(self, key):
        if key is None:
            return ''
        return self.strings.get(key, key)

    def child(self, children, name):
        for c in children:
            if c.get('name') == name:
                return c
        return None

    def sk(self, v):
        """a skin key (DecorateFileNames: EscMenuBackground) -> its texture path; a path stays."""
        if v and '\\' not in v and '/' not in v:
            return self.skin(v) or v
        return v

    def backdrop(self, props, r, out, frame):
        pd = {}
        for k, v in props:
            pd[k] = v
        x0, y0, x1, y1 = r
        bg = self.sk(pd.get('BackdropBackground', [None])[0])
        ins = [float(x) for x in pd.get('BackdropBackgroundInsets', [0, 0, 0, 0])][:4]
        if bg:
            bx0, bx1, by0, by1 = x0 + ins[0], x1 - ins[1], y0 + ins[3], y1 - ins[2]
            if 'BackdropTileBackground' in pd and pd.get('BackdropBackgroundSize'):
                ts = float(pd['BackdropBackgroundSize'][0])
                y = by1
                while y > by0 + 1e-6:
                    x = bx0
                    th = min(ts, y - by0)
                    while x < bx1 - 1e-6:
                        tw = min(ts, bx1 - x)
                        out.append({'el': 'quad', 'frame': frame, 'file': bg, 'rect': (x, y - th, x + tw, y),
                                    'uv': (0.0, tw / ts, 0.0, th / ts), 'rot': 0})
                        x += tw
                    y -= th
            else:
                out.append({'el': 'quad', 'frame': frame, 'file': bg, 'rect': (bx0, by0, bx1, by1),
                            'uv': (0.0, 1.0, 0.0, 1.0), 'rot': 0, 'mirror': 'BackdropMirrored' in pd})
        edge = self.sk(pd.get('BackdropEdgeFile', [None])[0])
        if edge and pd.get('BackdropCornerSize'):
            c = float(pd['BackdropCornerSize'][0])
            flags = str(pd.get('BackdropCornerFlags', ['UL|UR|BL|BR|T|L|B|R'])[0]).split('|')
            cell = lambda k: (EDGE_CELLS.index(k) / 8.0, (EDGE_CELLS.index(k) + 1) / 8.0)   # noqa: E731
            pieces = {'UL': (x0, y1 - c, x0 + c, y1), 'UR': (x1 - c, y1 - c, x1, y1),
                      'BL': (x0, y0, x0 + c, y0 + c), 'BR': (x1 - c, y0, x1, y0 + c),
                      'L': (x0, y0 + c, x0 + c, y1 - c), 'R': (x1 - c, y0 + c, x1, y1 - c),
                      'T': (x0 + c, y1 - c, x1 - c, y1), 'B': (x0 + c, y0, x1 - c, y0 + c)}
            for k, pr in pieces.items():
                if k not in flags or pr[2] - pr[0] <= 1e-6 or pr[3] - pr[1] <= 1e-6:
                    continue
                u0, u1 = cell(k)
                rot = 1 if k in ('T', 'B') else 0           # the edge cells are vertical strips; T / B drawn rotated
                rep = ((pr[2] - pr[0]) if rot else (pr[3] - pr[1])) / c if k in ('L', 'R', 'T', 'B') else 1.0
                out.append({'el': 'quad', 'frame': frame, 'file': edge, 'rect': pr, 'uv': (u0, u1, 0.0, rep),
                            'rot': rot, 'cell': k})

    def walk_glue(self, n, parent, out, named, hot):
        props, children = self.resolved(n)
        typ = n.get('type')
        name = n.get('name')
        if typ == 'DIALOG' or name in HIDDEN or name in self.hide:
            return                                          # modal dialogs / hidden panels until opened
        if name in self.place:                              # code placement replaces the FDF's (sizes only if given)
            mine = set(k for k, _v in self.place[name])
            drop = set(('Anchor', 'SetAllPoints')) | (mine & set(('Width', 'Height')))
            if 'SetPoint' in mine:
                drop.add('SetPoint')
            props = [kv for kv in props if kv[0] not in drop] + self.place[name]
        pts = [str(v[0]) for k, v in props if k == 'SetPoint' and v]
        if typ == 'TEXT' and pts and all(p.startswith('TOP') for p in pts) and not any(
                k in ('Height', 'SetAllPoints') for k, _v in props):
            ff = [v for k, v in props if k == 'FrameFont']   # [inf] a text frame without a height is one line high
            props = list(props) + [('Height', [float(ff[-1][1]) if ff and len(ff[-1]) > 1 else TEXT_ROW_H])]
        r = self.rect(props, parent, named)
        if name == 'MapInfoPane':
            self.map_info(r)
        if name in LISTS:
            if name == 'FileListFrame':              # SAVE-MENU: the file list is built in code with the edit box's look
                cb = self.templates.get('EscMenuControlBackdropTemplate')
                if cb is not None:
                    self.backdrop(self.resolved(cb)[0], r, out, name)
            out.append({'el': 'list', 'frame': name, 'rect': r})
        if n.get('name'):
            named[n['name']] = r
        pd = {}
        for k, v in props:
            pd[k] = v
        skip = set()
        if n.get('name') in COMPOSE:
            f, root = COMPOSE[n['name']]
            self.load(GLUE + f)
            sub = self.templates.get(root)
            if sub is not None:
                sp, _sc = self.resolved(sub)
                if not any(k in ('SetPoint', 'Anchor', 'SetAllPoints') for k, _v in sp):
                    sub = dict(sub, props=list(sub['props']) + [('SetAllPoints', [])])
                self.walk_glue(sub, r, out, named, hot)
        if typ not in ('BACKDROP',) + BUTTONS and pd.get('ControlBackdrop'):
            cb = pd['ControlBackdrop'][0]                   # list boxes, text areas, edit boxes, sliders, scroll bars
            skip.add(cb)
            node = self.child(children, cb) or self.templates.get(cb)
            if node is not None:
                bp, _bc = self.resolved(node)
                self.backdrop(bp, r, out, n.get('name'))
        if typ == 'BACKDROP':
            self.backdrop(props, r, out, n.get('name'))
        elif typ in BUTTONS:
            cb = pd.get('ControlBackdrop', [None])[0]
            for k in ('ControlBackdrop', 'ControlPushedBackdrop', 'ControlDisabledBackdrop', 'ControlDisabledPushedBackdrop',
                      'ControlMouseOverHighlight', 'CheckBoxCheckHighlight', 'CheckBoxDisabledCheckHighlight'):
                if pd.get(k):
                    skip.add(pd[k][0])
            node = self.child(children, cb) if cb else None
            if node is None and cb:
                node = self.templates.get(cb)
            if node is not None:
                bp, _bc = self.resolved(node)
                self.backdrop(bp, r, out, n.get('name'))
            hot.append({'name': n.get('name'), 'type': typ, 'rect': r, 'key': pd.get('ControlShortcutKey', [''])[0]})
        elif typ == 'TEXT':
            ff = pd.get('FrameFont', [None, 0.013])
            col = [float(x) for x in pd.get('FontColor', [1, 1, 1, 1])][:4]
            col = col + [1.0] * (4 - len(col))               # an RGB FontColor (the edit box template): opaque
            if name in self.justify:
                pd['FontJustificationH'] = [self.justify[name]]
            out.append({'el': 'text', 'frame': n.get('name'), 'text': self.text(pd.get('Text', [None])[0]),
                        'rect': r, 'size': float(ff[1]) if len(ff) > 1 else 0.013, 'color': col,
                        'justh': pd.get('FontJustificationH', ['JUSTIFYLEFT'])[0],
                        'justv': pd.get('FontJustificationV', ['JUSTIFYTOP'])[0]})
        elif typ == 'SPRITE':
            out.append({'el': 'sprite', 'frame': n.get('name'), 'rect': r,
                        'model': pd.get('BackgroundArt', [None])[0]})
        for c in children:
            if c.get('name') in skip or c['kind'] != 'Frame':
                continue
            self.walk_glue(c, r, out, named, hot)

    def map_info(self, r):
        """code placement of the MapInfoPane children (stacked mode) for the pane rect r, into self.place"""
        W = r[2] - r[0]
        bits = MAPINFO_BITS.get(self.cur, 0xffff)
        self.place['MinimapImage'] = [('SetPoint', ['TOP', 'MapInfoPane', 'TOP', 0.0, -0.0325])]
        prev = None
        for lab, bit, (pt, rel, rp, x, y) in MAPINFO_ROWS:
            val = lab.replace('Label', 'Value')
            if not bits & bit:
                self.hide.update((lab, val))
                continue
            self.place[lab] = [('SetPoint', [pt, rel or prev, rp, x, y]), ('Width', [W]), ('Height', [TEXT_ROW_H])]
            if lab == 'MapDescLabel':           # the description wraps below its label, W wide [obs 0x6f24c7a0]
                self.place[val] = [('Width', [W]), ('Height', [MAPDESC_H])]
                self.justify[val] = 'JUSTIFYLEFT'
            else:
                self.place[val] = [('Width', [W]), ('Height', [TEXT_ROW_H])]
                self.justify[val] = 'JUSTIFYRIGHT'
            prev = lab
        # header row: icon + name centred on the pane top [obs 0x6f24ce0b]; the world centres the name itself, so
        # the name gets the full row
        self.place['MapNameValue'] = [('SetPoint', ['TOP', 'MapInfoPane', 'TOP', 0.0, -0.002]), ('Width', [W]),
                                      ('Height', [0.01875])]
        self.justify['MapNameValue'] = 'JUSTIFYCENTER'

    def esc_screen(self, key):
        f, root, (pw, ph), hide = ESC_SCREENS[key]
        self.cur = key
        self.place, self.justify = {}, {}
        self.hide = set(hide)
        self.load(ESC + 'EscMenuTemplates.fdf')
        self.load(ESC + f)
        rootn = self.templates.get(root)
        if rootn is None:
            raise KeyError('%s not in %s' % (root, f))
        for t in ESC_TITLES:                                # a title has a TOP anchor and no width: centred over the panel [inf]
            self.place[t] = [('Width', [pw])]
            self.justify[t] = 'JUSTIFYCENTER'
        cx, cy = w3ui.SCREEN_W / 2, w3ui.SCREEN_H / 2
        panel = (cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2)
        named = {root: panel}
        out, hot = [], []
        self.backdrop([('BackdropBackground', ['EscMenuBackground']), ('BackdropTileBackground', []),
                       ('BackdropBackgroundSize', [0.128]), ('BackdropBackgroundInsets', [0.01, 0.01, 0.01, 0.01]),
                       ('BackdropEdgeFile', ['EscMenuBorder']), ('BackdropCornerSize', [0.048]),
                       ('BackdropCornerFlags', ['UL|UR|BL|BR|T|L|B|R'])], panel, out, root)
        for _pass in range(3):
            body, hot = [], []
            self.walk_glue(rootn, panel, body, named, hot)
        return out + body, hot

    def screen(self, key):
        if key in ESC_SCREENS:
            return self.esc_screen(key)
        f, root = SCREENS[key]
        self.cur = key
        self.place, self.justify = {}, {}
        self.hide = set(MAPINFO_UNUSED) | set(MAPINFO_NOMAP)
        self.load(GLUE + 'StandardTemplates.fdf')
        tree = self.load(GLUE + f)
        named = {}
        rootn = self.templates.get(root)
        if rootn is None:
            raise KeyError('%s not in %s (top level: %s)' % (root, f, [n.get('name') for n in tree['nodes']][:8]))
        screen = (0.0, 0.0, w3ui.SCREEN_W, w3ui.SCREEN_H)
        for _pass in range(3):                              # SetPoint may name a frame defined later: repeat with
            out, hot = [], []                               # the rects of the previous pass
            self.walk_glue(rootn, screen, out, named, hot)
        return out, hot


def glue_sections(install, log=print):
    """-> [(b'JGL1', text, n), (b'UIAT', atlas, 1)]: every screen of SCREENS as lines (screen units, origin bottom
    left; atlas rects normalised with v up):
      SCR <screen>
      Q <x0> <y0> <x1> <y1> <au0> <av0> <au1> <av1> <rot>        (rot 1: the texture's v runs along x)
      X <x0> <y0> <x1> <y1> <size> <r> <g> <b> <a> <justh> <justv> <text>   (|c colour markup kept, \\n escaped)
      H <name> <type> <x0> <y0> <x1> <y1> <key>
      M <frame> <x0> <y0> <x1> <y1> <model>                       (SPRITE: a model, drawn later; MinimapImage =
                                                                   the map preview)
      T <frame> <x0> <y0> <x1> <y1> <size> <r> <g> <b> <a> <justh> <justv>   (a value text the world fills)
      L <frame> <x0> <y0> <x1> <y1>                               (a list box the world fills: maps, games)
      I <melee|ums|folder|up> <au0> <av0> <au1> <av1>             (list row icons)
    plus F / G glyph lines like w3ui (the install's font)."""
    from PIL import Image, ImageFont, ImageDraw
    import io
    import struct
    import w3pack
    g = Glue(install)
    crops, keys, lines = [], {}, []
    cache = {}

    def tex(f):
        if f not in cache:
            raw = install.read(f.replace('/', '\\'), missing_ok=True)
            try:
                cache[f] = w3pack.blp_rgba(raw) if raw else None
            except Exception:                            # noqa: BLE001
                cache[f] = None
        return cache[f]

    def entry(f, cell):
        """atlas entry of texture f (cell = (u0, u1) column range of a border strip, or None = whole)"""
        k = (f, cell)
        if k not in keys:
            t = tex(f)
            if t is None:
                return None
            if cell:
                tw = t.size[0]
                t = t.crop((int(round(cell[0] * tw)), 0, max(int(round(cell[0] * tw)) + 1, int(round(cell[1] * tw))), t.size[1]))
            keys[k] = len(crops)
            crops.append(t)
        return keys[k]

    for scr in list(SCREENS) + list(ESC_SCREENS):
        try:
            els, hot = g.screen(scr)
        except KeyError as e:
            log('  glue: %s skipped: %s' % (scr, e))
            continue
        lines.append(('SCR', scr))
        for e in els:
            if e['el'] == 'quad':
                u0, u1, v0, v1 = e['uv']
                cell = (u0, u1) if 'cell' in e else None
                k = entry(e['file'], cell)
                if k is None:
                    continue
                x0, y0, x1, y1 = e['rect']
                if cell:                                 # border piece: repeat the cell along its length
                    rep = max(v1, 1e-6)
                    n = int(rep) + (1 if rep - int(rep) > 1e-3 else 0)
                    for i in range(n):
                        part = min(1.0, rep - i)
                        if e.get('rot'):
                            L = (x1 - x0) / rep
                            r = (x0 + i * L, y0, x0 + i * L + part * L, y1)
                        else:
                            L = (y1 - y0) / rep
                            r = (x0, y1 - i * L - part * L, x1, y1 - i * L)
                        lines.append(('Q', r, k, (0.0, 1.0, 0.0, part), e.get('rot', 0), False))
                else:
                    lines.append(('Q', e['rect'], k, (u0, u1, v0, v1), 0, bool(e.get('mirror'))))
            elif e['el'] == 'text' and e['text']:
                lines.append(('X', e))
            elif e['el'] == 'text' and ((e['frame'] or '').endswith('Value') or e['frame'] in LOADING_SLOTS or e['frame'] in ESC_VALUES):
                lines.append(('T', e))                   # a value the world fills (map name, size, ...)
            elif e['el'] == 'list':
                lines.append(('L', e))
            elif e['el'] == 'sprite':
                lines.append(('M', e))
        for h in hot:
            lines.append(('H', h))
    icons = [(k, entry(f, None)) for k, f in sorted(LIST_ICONS.items())]
    white = len(crops)                                   # a white texel block (hover highlight, solid fills)
    crops.append(Image.new('RGBA', (4, 4), (255, 255, 255, 255)))
    ttf = install.read('Fonts\\FRIZQT__.TTF', missing_ok=True)
    glyphs = []
    if ttf:
        font = ImageFont.truetype(io.BytesIO(ttf), w3ui.FONT_PX)
        asc, desc = font.getmetrics()
        for c in w3ui.FONT_CHARS:
            l, t, r, b = font.getbbox(chr(c))
            w, h = max(1, r - l), max(1, b - t)
            im = Image.new('RGBA', (w, h), (255, 255, 255, 0))
            ImageDraw.Draw(im).text((-l, -t), chr(c), font=font, fill=(255, 255, 255, 255))
            glyphs.append((c, len(crops), w, h, l, t, font.getlength(chr(c))))
            crops.append(im)
    order = sorted(range(len(crops)), key=lambda k: (-crops[k].size[1], -crops[k].size[0]))
    spos, height = w3ui._shelf([(crops[k].size[0] + 4, crops[k].size[1] + 4) for k in order], w3ui.ATLAS_W)
    pos = [None] * len(crops)
    for k, p in zip(order, spos):
        pos[k] = p
    H = -(-height // 256) * 256
    page = Image.new('RGBA', (w3ui.ATLAS_W, H), (0, 0, 0, 0))
    rects = []
    for im, (x, y) in zip(crops, pos):
        page.paste(im, (x + 2, y + 2))
        w, h = im.size
        rects.append(((x + 2) / w3ui.ATLAS_W, (H - (y + 2 + h)) / H, (x + 2 + w) / w3ui.ATLAS_W, (H - (y + 2)) / H))
    out = []
    for ln in lines:
        t = ln[0]
        if t == 'SCR':
            out.append('SCR ' + ln[1])
        elif t == 'Q':
            _t, r, k, uv, rot, mirror = ln
            au0, av0, au1, av1 = rects[k]
            u0, u1, v0, v1 = uv                          # texture space, v down from the top
            a0 = au0 + (au1 - au0) * u0
            a1 = au0 + (au1 - au0) * u1
            if mirror:
                a0, a1 = a1, a0
            b1 = av1 - (av1 - av0) * v0
            b0 = av1 - (av1 - av0) * v1
            out.append('Q %.6f %.6f %.6f %.6f %.6f %.6f %.6f %.6f %d' % (tuple(r) + (a0, b0, a1, b1, rot)))
        elif t == 'X':
            e = ln[1]
            out.append('X %.6f %.6f %.6f %.6f %.5f %.3f %.3f %.3f %.3f %s %s %s' % (
                tuple(e['rect']) + (e['size'],) + tuple(e['color']) + (e['justh'], e['justv'],
                                                                      e['text'].replace('\n', '\\n'))))
        elif t == 'T':
            e = ln[1]
            out.append('T %s %.6f %.6f %.6f %.6f %.5f %.3f %.3f %.3f %.3f %s %s' % (
                (e['frame'],) + tuple(e['rect']) + (e['size'],) + tuple(e['color']) + (e['justh'], e['justv'])))
        elif t == 'L':
            e = ln[1]
            out.append('L %s %.6f %.6f %.6f %.6f' % ((e['frame'],) + tuple(e['rect'])))
        elif t == 'H':
            h = ln[1]
            out.append('H %s %s %.6f %.6f %.6f %.6f %s' % ((h['name'], h['type']) + tuple(h['rect']) + (h['key'] or '-',)))
        elif t == 'M':
            e = ln[1]
            out.append('M %s %.6f %.6f %.6f %.6f %s' % ((e['frame'] or '-',) + tuple(e['rect']) + (e['model'] or '-',)))
    for k, i in icons:                                   # list row icons (map type, folder, up one level)
        if i is not None:
            out.append('I %s %.6f %.6f %.6f %.6f' % ((k,) + rects[i]))
    wr = rects[white]
    out.append('W %.6f %.6f %.6f %.6f' % ((wr[0] + wr[2]) / 2, (wr[1] + wr[3]) / 2, (wr[0] + wr[2]) / 2, (wr[1] + wr[3]) / 2))
    if ttf:
        out.append('F %d %d %d' % (w3ui.FONT_PX, asc, desc))
        for c, k, w, h, l, t, adv in glyphs:
            out.append('G %d %.6f %.6f %.6f %.6f %d %d %d %d %.2f' % ((c,) + rects[k] + (w, h, l, t, adv)))
    text = ('\n'.join(out) + '\n').encode('utf-8')
    atlas = struct.pack('<3i', w3ui.ATLAS_W, H, 1) + page.transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    log('  glue: %d screens, %d textures + %d glyphs, atlas %dx%d (%.1f MB), %d lines' % (
        sum(1 for x in lines if x[0] == 'SCR'), len(keys), len(glyphs), w3ui.ATLAS_W, H, len(atlas) / 1048576.0, len(out)))
    return [(b'JGL1', text, len(out)), (b'UIAT', atlas, 1)]


def runs(text, base):
    """WC3 colour markup |cAARRGGBB ... |r (|n newline) -> [(text, (r, g, b, a))]."""
    out, col, i, buf = [], tuple(base), 0, ''
    while i < len(text):
        if text[i] == '|' and i + 1 < len(text):
            c = text[i + 1].lower()
            if c == 'c' and i + 10 <= len(text):
                if buf:
                    out.append((buf, col))
                    buf = ''
                h = text[i + 2:i + 10]
                try:
                    col = (int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, int(h[6:8], 16) / 255.0, int(h[0:2], 16) / 255.0)
                except ValueError:
                    pass
                i += 10
                continue
            if c == 'r':
                if buf:
                    out.append((buf, col))
                    buf = ''
                col = tuple(base)
                i += 2
                continue
            if c == 'n':
                buf += '\n'
                i += 2
                continue
        buf += text[i]
        i += 1
    if buf:
        out.append((buf, col))
    return out


def preview(glue, els, hot, path, scale=1600):
    from PIL import Image, ImageDraw, ImageFont
    import io
    import w3pack
    W, H = int(w3ui.SCREEN_W * scale), int(w3ui.SCREEN_H * scale)
    img = Image.new('RGBA', (W, H), (10, 10, 20, 255))
    cache = {}
    ttf = glue.inst.read('Fonts\\FRIZQT__.TTF', missing_ok=True)

    def tex(f):
        if f not in cache:
            raw = glue.inst.read(f.replace('/', '\\'), missing_ok=True)
            cache[f] = w3pack.blp_rgba(raw) if raw and f.lower().endswith('.blp') else None
        return cache[f]
    for e in els:
        x0, y0, x1, y1 = e['rect']
        pw, ph = max(1, int(round((x1 - x0) * scale))), max(1, int(round((y1 - y0) * scale)))
        px, py = int(round(x0 * scale)), int(round(H - y1 * scale))
        if e['el'] == 'quad':
            t = tex(e['file'])
            if t is None:
                continue
            tw, th = t.size
            u0, u1, v0, v1 = e['uv']
            if e.get('rot'):
                t = t.rotate(90, expand=True)
                tw, th = t.size
                # rotated strip: the cell's u range becomes v; repeat along x
                c0, c1 = int(u0 * th), int(u1 * th)
                cellim = t.crop((0, th - c1, tw, th - c0))
                rep = max(1, int(round(v1)))
                strip = Image.new('RGBA', (tw * rep, cellim.size[1]))
                for i in range(rep):
                    strip.paste(cellim, (i * tw, 0))
                piece = strip.resize((pw, ph))
            else:
                c0, c1 = int(u0 * tw), max(int(u0 * tw) + 1, int(u1 * tw))
                cellim = t.crop((c0, 0, c1, th))
                if v1 > 1.001 or u1 - u0 > 1.001:
                    rx, ry = max(1, int(round(u1 - u0))), max(1, int(round(v1)))
                    strip = Image.new('RGBA', (cellim.size[0] * rx, cellim.size[1] * ry))
                    for i in range(rx):
                        for j in range(ry):
                            strip.paste(cellim, (i * cellim.size[0], j * cellim.size[1]))
                    cellim = strip
                elif v1 < 0.999 or (u1 - u0) < 0.999 and 'cell' not in e:
                    cellim = t.crop((int(u0 * tw), int(v0 * th), max(1, int(u1 * tw)), max(1, int(v1 * th))))
                piece = cellim.resize((pw, ph))
                if e.get('mirror'):
                    piece = piece.transpose(Image.FLIP_LEFT_RIGHT)
            img.alpha_composite(piece, (px, py))
        elif e['el'] == 'text' and ttf and e['text']:
            font = ImageFont.truetype(io.BytesIO(ttf), max(6, int(e['size'] * scale)))
            d = ImageDraw.Draw(img)
            rr = runs(e['text'], e['color'])
            w = sum(d.textlength(t, font=font) for t, _c in rr)
            tx = px
            if e['justh'] == 'JUSTIFYCENTER':
                tx = px + (pw - w) / 2
            elif e['justh'] == 'JUSTIFYRIGHT':
                tx = px + pw - w
            ty = py + (ph - e['size'] * scale) / 2 if e['justv'] == 'JUSTIFYMIDDLE' else py
            for t, c in rr:
                d.text((tx, ty), t, font=font, fill=tuple(int(255 * x) for x in c))
                tx += d.textlength(t, font=font)
        elif e['el'] == 'sprite':
            ImageDraw.Draw(img).rectangle((px, py, px + pw, py + ph), outline=(80, 80, 200, 255))
    d = ImageDraw.Draw(img)
    for h in hot:
        x0, y0, x1, y1 = h['rect']
        d.rectangle((int(x0 * scale), int(H - y1 * scale), int(x1 * scale), int(H - y0 * scale)), outline=(0, 255, 0, 120))
    img.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--screen', default='MainMenu')
    ap.add_argument('--install', default=os.path.join(ROOT, 'input', 'war3'))
    a = ap.parse_args()
    import war3fs
    g = Glue(war3fs.open_install(a.install))
    els, hot = g.screen(a.screen)
    out = os.path.join(ROOT, 'work', 'ui')
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, 'glue_%s.png' % a.screen)
    preview(g, els, hot, p)
    print('%s: %d draw elements, %d buttons: %s -> %s' % (a.screen, len(els), len(hot),
                                                          [h['name'] for h in hot][:12], p))


if __name__ == '__main__':
    main()
