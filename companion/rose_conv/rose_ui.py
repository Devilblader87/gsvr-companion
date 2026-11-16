"""ROSE client UI -> RPG1 1.4 sections UIAT (sprite atlas set) and UIDL (dialog layouts). Reads the player's own client READ-ONLY.

  python3 -I tools/rose_ui.py <client dir> -o out/rose_ui.rpg1 [--base 800x600] [--sheets UI,ITEM1,SKILLICON,STATEICON,TARGETMARK,CLANBACK,CLANCENTER] [--dialogs DlgInfo,DlgQuickBar]

Inputs (docs/ROSE_UI.md): VFS `3DDATA/CONTROL/RES/<sheet>.TSI` + the DDS files they name, loose `3Ddata/Control/Xml/*.xml` (+ `UI_strID.id`, `EXUI_strID.id`),
VFS `3DDATA/EFFECT/SPECIAL/DIGITNUMBER*.DDS`. Output: standalone UI pack (UIAT + UIDL only) and/or the two sections for `rose_pack.py --ui`.
Layouts: docs/RPG1_PACK.md section 11. Nothing of the game is stored in the repository.
"""
import os
import re
import struct
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosedds as D
import rpg1 as P

TAG_UIAT = b'UIAT'
TAG_UIDL = b'UIDL'
RES = '3DDATA/CONTROL/RES/'
NO_SPRITE = 0xFFFFFFFF
NO_TEX = 0xFFFF

# TSI sheets: (name, MODULEID used by the XML files). Module 2 and 10+ are unused by the client XML.
SHEETS = (('UI', 0), ('ITEM1', 1), ('EXUI', 3), ('SKILLICON', 4), ('STATEICON', 5), ('SOKETJAM', 6), ('CLANBACK', 7), ('CLANCENTER', 8), ('TARGETMARK', 9))
DEFAULT_SHEETS = ('UI', 'ITEM1', 'SKILLICON', 'STATEICON', 'TARGETMARK', 'CLANBACK', 'CLANCENTER')   # CLANBACK / CLANCENTER (modules 7 / 8): the clan mark sprites, id = sprite index (background 1..16, foreground 1..19)
MODULE_DIGITS = 100                      # code-defined damage digit strips (not a TSI)
DIGIT_FILES = (('DIGITNUMBER01', 10), ('DIGITNUMBER02', 10), ('DIGITNUMBERMISS', 4))
DIGIT_DIR = '3DDATA/EFFECT/SPECIAL/'
# first priority dialogs (combat prototype) first, the rest alphabetical
PRIORITY = ('DlgInfo', 'DlgQuickBar', 'DlgChat', 'DlgMinimap', 'DlgMenu', 'DlgItem', 'DlgSkill')

# widget type codes (UIDL)
WT = {'IMAGE': 1, 'BUTTON': 2, 'GUAGE': 3, 'CAPTION': 4, 'RADIOBUTTON': 5, 'CHECKBOX': 6, 'EDITBOX': 7, 'LISTBOX': 8, 'ZLISTBOX': 9, 'SCROLLBAR': 10,
      'SCROLLBOX': 11, 'PANE': 12, 'TABBEDPANE': 13, 'TAB': 14, 'TABBUTTON': 15, 'SKILL': 16, 'COMBOBOX': 17, 'TABLE': 18, 'RADIOBOX': 19, 'PUSHBUTTON': 20,
      'TOPIMAGE': 21, 'MIDDLEIMAGE': 22, 'BOTTOMIMAGE': 23, 'OTHER': 24}
WT_NAME = {v: k for k, v in WT.items()}
ALIAS = {'JLISTBOX': 'LISTBOX', 'JCOMBOBOX': 'COMBOBOX', 'BUTTON_OLD': 'BUTTON', 'BUTTONOK': 'BUTTON', 'BUTTONCANCEL': 'BUTTON',
         'IMAGETOP': 'TOPIMAGE', 'IMAGEMIDDLE': 'MIDDLEIMAGE', 'IMAGEBOTTOM': 'BOTTOMIMAGE'}
CONTAINERS = ('PANE', 'TABBEDPANE', 'TAB')
WF_NOIMAGE = 1          # invisible hit area (NOIMAGE="1")
WF_HIDDEN = 2           # not shown in the default state (alternate layout / not-first tab / stacked list), see section 11.3
SF_CLIPPED = 1          # sprite rect was cut at the texture border (icon cells of 40 px overhang a 512 px texture)
DF_VISIBLE = 1          # dialog flags: DEFAULT_VISIBLE
DF_MODAL = 2

SHEET_REC = struct.Struct('<HHIII')                  # moduleId, nameLen, nameOff, firstSprite, spriteCount
SPRITE_REC = struct.Struct('<BBH4HIHH')              # sheet, flags, tex, x, y, w, h, nameOff, nameLen, localIndex
UIAT_HDR = struct.Struct('<12I')
DLG_REC = struct.Struct('<IHHHHBBhhhhHHHHII10x')     # nameOff nameLen flags w h ax ay adjX adjY posX posY showSid hideSid pad pad firstWidget widgetCount
WID_REC = struct.Struct('<BBHhhHH4IiIHHffHHHH')      # type flags id x y w h spr[4] parent nameOff nameLen clickSid scaleW scaleH alpha module attrA attrB
UIDL_HDR = struct.Struct('<12I')
assert SHEET_REC.size == 16 and SPRITE_REC.size == 20 and UIAT_HDR.size == 48
assert DLG_REC.size == 48 and WID_REC.size == 56 and UIDL_HDR.size == 48


# ---------------------------------------------------------------- source parsers
def parse_tsi(d):
    """-> (textures [name], sprites [dict(tex, l, t, r, b, name)]). Layout: docs/ROSE_UI.md 3.3; the whole file must be consumed."""
    p = 0
    n, = struct.unpack_from('<H', d, p); p += 2
    tex = []
    for _ in range(n):
        ln, = struct.unpack_from('<H', d, p); p += 2
        tex.append(bytes(d[p:p + ln]).split(b'\0')[0].decode('cp949')); p += ln + 4
    total, = struct.unpack_from('<H', d, p); p += 2
    sprites = []
    for _ in range(n):
        c, = struct.unpack_from('<H', d, p); p += 2
        for _ in range(c):
            ti, l, t, r, b, _u = struct.unpack_from('<H4iI', d, p); p += 22
            nm = bytes(d[p:p + 32]).split(b'\0')[0].decode('cp949'); p += 32
            sprites.append(dict(tex=ti, l=l, t=t, r=r, b=b, name=nm))
    if p != len(d):
        raise ValueError('TSI: %d trailing bytes' % (len(d) - p))
    if total != len(sprites):
        raise ValueError('TSI: header says %d sprites, found %d' % (total, len(sprites)))
    return tex, sprites


def parse_id(text):
    """`NAME<ws>INDEX` lines (CR/LF mixture) -> {name_lower: index}"""
    out = {}
    for line in re.split(r'[\r\n]+', text):
        parts = line.split()
        if len(parts) >= 2:
            try:
                out.setdefault(parts[0].lower(), int(parts[1]))
            except ValueError:
                pass
    return out


_TAG = re.compile(r'<(/?)([A-Za-z_][A-Za-z_0-9]*)((?:\s+[A-Za-z_0-9]+\s*=\s*"[^"]*")*)\s*(/?)>')
_ATTR = re.compile(r'([A-Za-z_0-9]+)\s*=\s*"([^"]*)"')


def parse_xml(text):
    """Tolerant parser for the client's window XML (upper-case attributes, text between tags = developer name). -> node dict(tag, attrs, text, children)."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    root = dict(tag='ROOT', attrs={}, text='', children=[])
    stack = [root]
    pos = 0
    ms = list(_TAG.finditer(text))
    for i, m in enumerate(ms):
        closing, tag, attrs, selfc = m.group(1), m.group(2).upper(), m.group(3), m.group(4)
        if closing:
            if len(stack) > 1:
                stack.pop()
            continue
        node = dict(tag=tag, attrs={k.upper(): v for k, v in _ATTR.findall(attrs)}, text='', children=[])
        stack[-1]['children'].append(node)
        if not selfc:
            nxt = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            node['text'] = re.sub(r'\s+', ' ', text[m.end():nxt]).strip()
            stack.append(node)
    if len(root['children']) != 1:
        raise ValueError('XML: expected one root element, found %d' % len(root['children']))
    return root['children'][0]


def _int(s, default=0):
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(round(float(s)))
        except ValueError:
            return default


def _flt(s, default=1.0):
    try:
        return float(str(s).strip())
    except ValueError:
        return default


def find_ci(directory, name):
    """case-insensitive file lookup in one directory (client folders mix 3Ddata / 3DDATA, .xml / .XML)"""
    low = name.lower()
    for f in os.listdir(directory):
        if f.lower() == low:
            return os.path.join(directory, f)
    return None


def find_xml_dir(client):
    d = client
    for part in ('3ddata', 'control', 'xml'):
        nxt = find_ci(d, part)
        if not nxt:
            return None
        d = nxt
    return d


def resolve_anchor(anchor, adjust, size, screen):
    """DEFAULT_X/Y anchor (0 start, 1 centre, 2 end) + ADJUST pixel offset -> top-left coordinate for a screen extent. [derived from the 54 files, unverified in TRose.exe]"""
    if anchor == 1:
        return (screen - size) // 2 + adjust
    if anchor == 2:
        return screen - size + adjust
    return adjust


# ---------------------------------------------------------------- textures
def ui_texture(name, dds_bytes, rgba32=False, warn=None):
    """DDS bytes -> dict(name, fmt, w, h, levels=[level 0 only]) in a Unity-loadable format. UI textures have no mip chain (sprite bleed); DXT1 when opaque else DXT5;
    sizes below 4 px are padded to 4 x 4 (DXT needs whole blocks)."""
    dds = D.parse_dds(dds_bytes)
    w, h = dds['w'], dds['h']
    if dds['fmt'] == 'RGBA':
        rgba = dds['levels'][0]
        if w < 4 or h < 4:
            nw, nh = max(4, w), max(4, h)
            out = bytearray(nw * nh * 4)
            for y in range(nh):
                for x in range(nw):
                    o = (min(y, h - 1) * w + min(x, w - 1)) * 4
                    out[(y * nw + x) * 4:(y * nw + x) * 4 + 4] = rgba[o:o + 4]
            rgba, w, h = bytes(out), nw, nh
            if warn is not None:
                warn.append('%s padded to %dx%d' % (name, w, h))
        if rgba32:
            return dict(name=name, fmt=D.FMT_RGBA32, w=w, h=h, levels=[bytes(rgba)])
        opaque = all(rgba[i] == 255 for i in range(3, len(rgba), 4))
        enc = D.encode_dxt1 if opaque else D.encode_dxt5
        return dict(name=name, fmt=D.FMT_DXT1 if opaque else D.FMT_DXT5, w=w, h=h, levels=[enc(rgba, w, h)])
    fmt, w, h, lv = D.to_pack_texture(dds)
    if w < 4 or h < 4:
        raise ValueError('%s: compressed texture below 4x4' % name)
    if rgba32:
        fmt2 = {D.FMT_DXT1: 'DXT1', D.FMT_DXT5: 'DXT5'}[fmt]
        return dict(name=name, fmt=D.FMT_RGBA32, w=w, h=h, levels=[D.decode_dxt(fmt2, lv[0], w, h)])
    return dict(name=name, fmt=fmt, w=w, h=h, levels=[lv[0]])


# ---------------------------------------------------------------- UIAT codec
def enc_uiat(textures, sheets, sprites):
    """textures: OTEX style dicts (name, fmt, w, h, levels). sheets: [dict(module, name, first, count)]. sprites: [dict(sheet, flags, tex, x, y, w, h, name, local)]."""
    names = bytearray()
    cache = {}

    def nm(s):
        b = s.encode('utf-8')
        if b not in cache:
            cache[b] = len(names); names.extend(b)
        return cache[b], len(b)
    shb = bytearray()
    for s in sheets:
        o, l = nm(s['name'])
        shb += SHEET_REC.pack(s['module'], l, o, s['first'], s['count'])
    spb = bytearray()
    for s in sprites:
        o, l = nm(s['name'])
        spb += SPRITE_REC.pack(s['sheet'], s.get('flags', 0), s['tex'], s['x'], s['y'], s['w'], s['h'], o, l, s['local'])
    tex = P.enc_otex(textures)
    names = P._pad4(bytes(names))
    pos = UIAT_HDR.size
    tex_off = pos; pos += len(tex); pos += -pos % 4
    sh_off = pos; pos += len(shb)
    sp_off = pos; pos += len(spb)
    nm_off = pos
    hdr = UIAT_HDR.pack(len(textures), tex_off, len(tex), len(sheets), sh_off, SHEET_REC.size, len(sprites), sp_off, SPRITE_REC.size, nm_off, len(names), 0)
    out = bytearray(hdr)
    out += tex
    out += b'\0' * (sh_off - len(out))
    out += shb + spb + names
    return bytes(out)


def dec_uiat(d):
    (tcount, toff, tbytes, shc, sho, shs, spc, spo, sps, nmo, nmb, _r) = UIAT_HDR.unpack_from(d, 0)
    if shs < SHEET_REC.size or sps < SPRITE_REC.size:
        raise ValueError('UIAT strides too small')
    if toff + tbytes > len(d) or sho + shc * shs > len(d) or spo + spc * sps > len(d) or nmo + nmb > len(d):
        raise ValueError('UIAT block out of bounds')
    textures = P.dec_otex(d[toff:toff + tbytes])
    if len(textures) != tcount:
        raise ValueError('UIAT texture count %d != %d' % (tcount, len(textures)))
    names = bytes(d[nmo:nmo + nmb])

    def s(o, l):
        if o + l > len(names):
            raise ValueError('UIAT name out of range')
        return names[o:o + l].decode('utf-8')
    sheets = []
    for i in range(shc):
        mod, nl, no, first, cnt = SHEET_REC.unpack_from(d, sho + i * shs)
        sheets.append(dict(module=mod, name=s(no, nl), first=first, count=cnt))
    sprites = []
    for i in range(spc):
        sh, fl, tx, x, y, w, h, no, nl, loc = SPRITE_REC.unpack_from(d, spo + i * sps)
        sprites.append(dict(sheet=sh, flags=fl, tex=tx, x=x, y=y, w=w, h=h, name=s(no, nl), local=loc))
    return dict(textures=textures, sheets=sheets, sprites=sprites)


# ---------------------------------------------------------------- UIDL codec
def enc_uidl(dialogs, base):
    """dialogs: [dict(name, flags, w, h, ax, ay, adj_x, adj_y, pos_x, pos_y, show_sid, hide_sid, widgets=[...])]; widget dicts as built by build_dialog. base = (w, h)."""
    names = bytearray()
    cache = {}

    def nm(s):
        b = s.encode('utf-8')
        if b not in cache:
            cache[b] = len(names); names.extend(b)
        return cache[b], len(b)
    dl = bytearray(); wd = bytearray()
    nwid = 0
    for d in dialogs:
        o, l = nm(d['name'])
        dl += DLG_REC.pack(o, l, d['flags'], d['w'], d['h'], d['ax'], d['ay'], d['adj_x'], d['adj_y'], d['pos_x'], d['pos_y'],
                           d['show_sid'], d['hide_sid'], 0, 0, nwid, len(d['widgets']))
        for w in d['widgets']:
            o, l = nm(w['name'])
            sp = list(w['spr']) + [NO_SPRITE] * (4 - len(w['spr']))
            wd += WID_REC.pack(w['type'], w['flags'], w['id'], w['x'], w['y'], w['w'], w['h'], sp[0], sp[1], sp[2], sp[3], w['parent'],
                               o, l, w['click_sid'], w['scale_w'], w['scale_h'], w['alpha'], w['module'], w['attr_a'], w['attr_b'])
        nwid += len(d['widgets'])
    names = P._pad4(bytes(names))
    dlg_off = UIDL_HDR.size
    wid_off = dlg_off + len(dl)
    nm_off = wid_off + len(wd)
    hdr = UIDL_HDR.pack(len(dialogs), dlg_off, DLG_REC.size, nwid, wid_off, WID_REC.size, nm_off, len(names), base[0], base[1], 0, 0)
    return hdr + bytes(dl) + bytes(wd) + names


def dec_uidl(d):
    (dc, dof, dst, wc, wof, wst, nmo, nmb, bw, bh, _a, _b) = UIDL_HDR.unpack_from(d, 0)
    if dst < DLG_REC.size or wst < WID_REC.size:
        raise ValueError('UIDL strides too small')
    if dof + dc * dst > len(d) or wof + wc * wst > len(d) or nmo + nmb > len(d):
        raise ValueError('UIDL block out of bounds')
    names = bytes(d[nmo:nmo + nmb])

    def s(o, l):
        if o + l > len(names):
            raise ValueError('UIDL name out of range')
        return names[o:o + l].decode('utf-8')
    allw = []
    for i in range(wc):
        (t, fl, wid, x, y, w, h, s0, s1, s2, s3, par, no, nl, cs, sw, sh, al, mod, aa, ab) = WID_REC.unpack_from(d, wof + i * wst)
        allw.append(dict(type=t, flags=fl, id=wid, x=x, y=y, w=w, h=h, spr=[s0, s1, s2, s3], parent=par, name=s(no, nl), click_sid=cs,
                         scale_w=sw, scale_h=sh, alpha=al, module=mod, attr_a=aa, attr_b=ab))
    dialogs = []
    for i in range(dc):
        (no, nl, fl, w, h, ax, ay, adx, ady, px, py, shs, his, _p1, _p2, first, cnt) = DLG_REC.unpack_from(d, dof + i * dst)
        if first + cnt > wc:
            raise ValueError('UIDL dialog %d widget range out of bounds' % i)
        dialogs.append(dict(name=s(no, nl), flags=fl, w=w, h=h, ax=ax, ay=ay, adj_x=adx, adj_y=ady, pos_x=px, pos_y=py, show_sid=shs, hide_sid=his,
                            first=first, widgets=allw[first:first + cnt]))
    return dict(base=(bw, bh), dialogs=dialogs)


# ---------------------------------------------------------------- build: sprites
class SpriteSet:
    """Everything the UIAT section needs: textures (deduplicated by file name), sheets, sprites; plus name lookup for the dialog builder."""

    def __init__(self):
        self.textures = []
        self.tex_index = {}
        self.sheets = []
        self.sprites = []
        self.module_sheet = {}          # MODULEID -> index into self.sheets
        self.by_name = {}               # module -> {name_lower: global sprite index}
        self.warn = []

    def add_texture(self, key, tex):
        if key not in self.tex_index:
            self.tex_index[key] = len(self.textures)
            self.textures.append(tex)
        return self.tex_index[key]

    def add_sheet(self, name, module, rects):
        """rects: [(tex_index or NO_TEX, x, y, w, h, name)] -> registers the sheet and its sprites"""
        si = len(self.sheets)
        first = len(self.sprites)
        names = self.by_name.setdefault(module, {})
        clipped = 0
        for local, (ti, x, y, w, h, nm) in enumerate(rects):
            fl = 0
            if ti != NO_TEX:
                t = self.textures[ti]
                if x < 0 or y < 0 or w <= 0 or h <= 0 or x >= t['w'] or y >= t['h']:
                    self.warn.append('%s sprite %d (%s) rect %d,%d %dx%d outside texture %s %dx%d' % (name, local, nm, x, y, w, h, t['name'], t['w'], t['h']))
                elif x + w > t['w'] or y + h > t['h']:
                    w = min(w, t['w'] - x); h = min(h, t['h'] - y)      # icon grids of 40 px cells overhang a 512 px texture by 8 px
                    fl = SF_CLIPPED; clipped += 1
            self.sprites.append(dict(sheet=si, flags=fl, tex=ti, x=max(0, x), y=max(0, y), w=max(0, w), h=max(0, h), name=nm, local=local))
            names.setdefault(nm.lower(), first + local)
        if clipped:
            self.warn.append('%s: %d sprites overhang their texture and were clipped to it (flag 1)' % (name, clipped))
        self.sheets.append(dict(module=module, name=name, first=first, count=len(rects)))
        self.module_sheet[module] = si

    def lookup(self, module, gid):
        """sprite name (or numeric local index for the icon sheets) -> global sprite index or NO_SPRITE"""
        if module not in self.module_sheet or not gid:
            return NO_SPRITE
        g = self.by_name.get(module, {}).get(gid.lower())
        if g is not None:
            return g
        return NO_SPRITE


def load_sprites(vfs, sheets=DEFAULT_SHEETS, rgba32=False, digits=True, id_names=None):
    ss = SpriteSet()
    id_names = id_names or {}
    for sname, module in SHEETS:
        if sname not in sheets:
            continue
        try:
            tex, sprites = parse_tsi(vfs.read(RES + sname + '.TSI'))
        except (KeyError, ValueError, struct.error) as e:
            ss.warn.append('sheet %s skipped: %s' % (sname, e))
            continue
        used = sorted({s['tex'] for s in sprites})
        tmap = {}
        for ti in used:
            if ti >= len(tex):
                ss.warn.append('%s: sprite texture slot %d out of range' % (sname, ti))
                tmap[ti] = NO_TEX
                continue
            key = tex[ti].lower()
            if key in ss.tex_index:
                tmap[ti] = ss.tex_index[key]; continue
            try:
                t = ui_texture(key, vfs.read(RES + tex[ti]), rgba32, ss.warn)
            except (KeyError, ValueError) as e:
                ss.warn.append('%s: texture %s unavailable (%s)' % (sname, tex[ti], e))
                tmap[ti] = NO_TEX
                continue
            tmap[ti] = ss.add_texture(key, t)
        rects = [(tmap.get(s['tex'], NO_TEX), s['l'], s['t'], s['r'] - s['l'] + 1, s['b'] - s['t'] + 1, s['name']) for s in sprites]
        ss.add_sheet(sname, module, rects)
        # cross-check the .id name table against the sprite names (UI / EXUI)
        idt = id_names.get(sname)
        if idt:
            names = ss.by_name[module]
            bad = [n for n, i in idt.items() if names.get(n) != ss.sheets[-1]['first'] + i]
            if bad:
                ss.warn.append('%s_strID.id: %d of %d names differ from the TSI sprite order (e.g. %s)' % (sname, len(bad), len(idt), bad[0]))
    if digits:
        rects = []
        for fname, parts in DIGIT_FILES:
            try:
                t = ui_texture(fname.lower() + '.dds', vfs.read(DIGIT_DIR + fname + '.DDS'), rgba32, ss.warn)
            except (KeyError, ValueError) as e:
                ss.warn.append('digits: %s unavailable (%s)' % (fname, e))
                continue
            ti = ss.add_texture('digit/' + fname.lower(), t)
            gw = t['w'] // parts
            for i in range(parts):
                rects.append((ti, i * gw, 0, gw, t['h'], '%s_%d' % (fname, i)))
        if rects:
            ss.add_sheet('DIGITS', MODULE_DIGITS, rects)
    return ss


# ---------------------------------------------------------------- build: dialogs
def build_dialog(name, root, ss, base):
    """XML root node -> UIDL dialog dict. Coordinates are dialog-local pixels (container offsets already added)."""
    a = root['attrs']
    w = _int(a.get('WIDTH')); h = _int(a.get('HEIGHT'))
    ax, ay = _int(a.get('DEFAULT_X')), _int(a.get('DEFAULT_Y'))
    adx, ady = _int(a.get('ADJUST_X')), _int(a.get('ADJUST_Y'))
    flags = (DF_VISIBLE if _int(a.get('DEFAULT_VISIBLE', a.get('DEFAULTVISIBLE'))) else 0) | (DF_MODAL if _int(a.get('MODAL')) else 0)
    widgets = []
    warn = []

    def sprites_for(tag, at, module):
        g = lambda k: ss.lookup(module, at.get(k, ''))
        if tag in ('BUTTON', 'RADIOBUTTON', 'TABBUTTON', 'PUSHBUTTON'):
            return [g('NORMALGID'), g('OVERGID'), g('DOWNGID'), g('DISABLEGID')]
        if tag == 'CHECKBOX':
            return [g('UNCHECKGID'), NO_SPRITE, g('CHECKGID'), NO_SPRITE]
        return [g('GID'), NO_SPRITE, NO_SPRITE, NO_SPRITE]

    def walk(node, ox, oy, parent, hidden):
        tabs = 0
        seen_rects = {}
        for ch in node['children']:
            tag = ALIAS.get(ch['tag'], ch['tag'])
            at = ch['attrs']
            if tag not in WT:
                tag_code = WT['OTHER']
            else:
                tag_code = WT[tag]
            x = _int(at.get('X', at.get('OFFSETX')))
            y = _int(at.get('Y', at.get('OFFSETY')))
            if 'OFFSETX' in at and 'X' not in at:
                x = _int(at['OFFSETX'])
            if 'OFFSETY' in at and 'Y' not in at:
                y = _int(at['OFFSETY'])
            if tag == 'SKILL':
                ax_, ay_ = x, y                      # skill-tree nodes carry absolute offsets inside the dialog
            else:
                ax_, ay_ = ox + x, oy + y
            cw, chh = _int(at.get('WIDTH')), _int(at.get('HEIGHT'))
            module = _int(at.get('MODULEID'))
            hid = hidden
            if tag == 'TAB':
                hid = hid or tabs > 0
                tabs += 1
            if tag == 'PANE' and node is root and w and cw > w:
                hid = True                           # bigger alternate layout of the root (minimap big map)
            text = ch['text']
            if text.upper().startswith('VERTICAL') and node is root:
                hid = True                           # quickbar rotated layout
            if tag == 'LISTBOX':
                key = (ax_, ay_, cw, chh)
                if key in seen_rects:
                    hid = True                       # stacked list boxes: one per chat channel, only the first one is shown
                seen_rects[key] = True
            fl = (WF_NOIMAGE if _int(at.get('NOIMAGE')) else 0) | (WF_HIDDEN if hid else 0)
            spr = sprites_for(tag, at, module)
            for k in ('GID', 'NORMALGID', 'CHECKGID'):
                if at.get(k) and spr[2 if k == 'CHECKGID' else 0] == NO_SPRITE and not fl & WF_NOIMAGE and module in ss.module_sheet:
                    warn.append('%s widget %s: sprite %r (module %d) not found' % (name, at.get('ID', '?'), at.get(k), module))
                    break
            wid = _int(at.get('INDEX')) if tag == 'SKILL' else _int(at.get('ID'))
            attr_a = attr_b = 0
            if tag == 'SCROLLBAR':
                attr_a = _int(at.get('LISTBOXID'))
            elif tag == 'RADIOBUTTON':
                attr_a = _int(at.get('RADIOBOXID'))
            elif tag == 'EDITBOX':
                attr_a = _int(at.get('LIMITTEXT'))
            elif tag in ('LISTBOX', 'ZLISTBOX'):
                attr_a = _int(at.get('FONT'))
                attr_b = _int(at.get('LINESPACE'))
            elif tag == 'SKILL':
                attr_a = _int(at.get('LEVEL'))
                attr_b = _int(at.get('LIMITLEVEL'))
            label = text
            if tag == 'SKILL' and at.get('IMAGE'):
                label = at['IMAGE']
            idx = len(widgets)
            widgets.append(dict(type=tag_code, flags=fl, id=wid & 0xffff, x=ax_, y=ay_, w=cw, h=chh, spr=spr, parent=parent, name=label,
                                click_sid=_int(at.get('CLICKSID')), scale_w=_flt(at.get('SCALEWIDTH'), 1.0), scale_h=_flt(at.get('SCALEHEIGHT'), 1.0),
                                alpha=_int(at.get('ALPHAVALUE'), 255) & 0xffff, module=module & 0xffff, attr_a=attr_a & 0xffff, attr_b=attr_b & 0xffff))
            if ch['children']:
                walk(ch, ax_ if tag != 'SKILL' else ox, ay_ if tag != 'SKILL' else oy, idx, hid)
    walk(root, 0, 0, -1, False)
    return dict(name=name, flags=flags, w=w, h=h, ax=ax, ay=ay, adj_x=adx, adj_y=ady,
                pos_x=resolve_anchor(ax, adx, w, base[0]), pos_y=resolve_anchor(ay, ady, h, base[1]),
                show_sid=_int(a.get('SHOWSID')), hide_sid=_int(a.get('HIDESID')), widgets=widgets), warn


def order_dialogs(names):
    pri = [p for p in PRIORITY if p.lower() in {n.lower() for n in names}]
    low = {p.lower() for p in pri}
    rest = sorted((n for n in names if n.lower() not in low), key=str.lower)
    by = {n.lower(): n for n in names}
    return [by[p.lower()] for p in pri] + rest


def build_ui(vfs, client, sheets=DEFAULT_SHEETS, dialogs=None, base=(800, 600), rgba32=False, digits=True):
    """-> (sections [(tag, section_version, count, data)], info dict). `client` = client folder (loose XML), `vfs` = rosevfs.Vfs (TSI + DDS)."""
    xml_dir = find_xml_dir(client)
    if not xml_dir:
        raise SystemExit('3Ddata/Control/Xml not found under %s (the window XML files are loose files)' % client)
    id_names = {}
    for sname, fname in (('UI', 'UI_strID.id'), ('EXUI', 'EXUI_strID.id')):
        p = find_ci(xml_dir, fname)
        if p:
            with open(p, 'rb') as f:
                id_names[sname] = parse_id(f.read().decode('cp949', 'replace'))
    ss = load_sprites(vfs, sheets, rgba32, digits, id_names)
    files = {os.path.splitext(f)[0]: f for f in os.listdir(xml_dir) if f.lower().endswith('.xml')}
    want = order_dialogs(list(files)) if not dialogs else [next((k for k in files if k.lower() == d.lower()), None) for d in dialogs]
    out = []
    warns = list(ss.warn)
    for nm in want:
        if nm is None:
            warns.append('requested dialog not found')
            continue
        try:
            with open(os.path.join(xml_dir, files[nm]), 'rb') as f:
                root = parse_xml(f.read().decode('cp949', 'replace'))
            dlg, w = build_dialog(nm, root, ss, base)
        except ValueError as e:
            warns.append('%s skipped: %s' % (nm, e))
            continue
        out.append(dlg); warns += w
    uiat = enc_uiat(ss.textures, ss.sheets, ss.sprites)
    uidl = enc_uidl(out, base)
    sections = [(TAG_UIAT, 1, len(ss.sprites), uiat), (TAG_UIDL, 1, len(out), uidl)]
    info = dict(base=list(base), rgba32=rgba32, textures=len(ss.textures), texture_bytes=sum(len(l) for t in ss.textures for l in t['levels']),
                sheets=[dict(s) for s in ss.sheets], sprites=len(ss.sprites), dialogs=len(out), widgets=sum(len(d['widgets']) for d in out),
                section_bytes=dict(UIAT=len(uiat), UIDL=len(uidl)), warnings=warns)
    return sections, info


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('client', help='ROSE client directory (data.idx + *.VFS + loose 3Ddata/Control/Xml), read only')
    ap.add_argument('-o', '--out', default='out/rose_ui.rpg1', help='standalone UI pack (UIAT + UIDL, RPG1 1.4)')
    ap.add_argument('--base', default='800x600', help='base resolution the DEFAULT/ADJUST anchors are resolved for (default 800x600)')
    ap.add_argument('--sheets', default=','.join(DEFAULT_SHEETS), help='TSI sheets to include (%s)' % ','.join(n for n, _ in SHEETS))
    ap.add_argument('--dialogs', default=None, help='comma list of dialogs (default: all, priority dialogs first)')
    ap.add_argument('--rgba32', action='store_true', help='store the textures uncompressed (RGBA32) instead of DXT1/DXT5')
    ap.add_argument('--no-digits', action='store_true', help='skip the damage digit strips')
    a = ap.parse_args(argv)
    import rosevfs
    bw, bh = (int(x) for x in a.base.lower().split('x'))
    vfs = rosevfs.Vfs(a.client)
    sections, info = build_ui(vfs, a.client, tuple(x.strip().upper() for x in a.sheets.split(',') if x.strip()),
                              [x.strip() for x in a.dialogs.split(',')] if a.dialogs else None, (bw, bh), a.rgba32, not a.no_digits)
    pack = P.write_pack(sections)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'wb') as f:
        f.write(pack)
    with open(os.path.splitext(a.out)[0] + '.manifest.json', 'w') as f:
        json.dump(info, f, indent=1, sort_keys=True)
    print('wrote %s (%.2f MB, RPG1 %d.%d)' % (a.out, len(pack) / 1e6, P.read_pack(pack)['version'] >> 16, P.read_pack(pack)['version'] & 0xffff))
    print('  UIAT %d bytes (%d textures, %d sprites)   UIDL %d bytes (%d dialogs, %d widgets)' % (
        info['section_bytes']['UIAT'], info['textures'], info['sprites'], info['section_bytes']['UIDL'], info['dialogs'], info['widgets']))
    for w in info['warnings'][:30]:
        print('  warning:', w)
    if len(info['warnings']) > 30:
        print('  ... %d more warnings (manifest)' % (len(info['warnings']) - 30))


if __name__ == '__main__':
    main()
