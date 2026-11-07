# Half-Life client HUD art for the GSM1 model pack (GSX1 chunk "HUD1"). Stdlib only (used by tools/modelpack.py and the tests).
#
# The HUD sprites are the player's own files: sprites/hud.txt (name, resolution, sprite sheet, x y w h) + sprites/weapon_*.txt (the
# same format per weapon: weapon / weapon_s / ammo / ammo2 icons) + the 640 sprite sheets they name (640hud1 ... 640hud9, the Opposing
# Force 640hudof*), sprites/640_pain.spr (4 pain arrows) and sprites/640_train.spr (5 train speed frames). Nothing of it is in the
# repo; the pack is built on the player's machine (tools/modelpack.py, companion).
#
# Chunk "HUD1" (all int32 little endian, payload padded to 4 bytes):
#   [8]   entryCount E, atlasW, atlasH, 0, 0, 0, 0, 0
#   [E*8] nameOff, nameLen (into the GSM1 names table), x, y, w, h (pixels in the atlas, top-left origin, rows top -> bottom), 0, 0
#   uint8 atlas[atlasW * atlasH]: one byte per pixel = the sprite intensity (the sheets are additive and grey: max of r, g, b). The
#         runtime tints it (HUD colour 255,160,0 ...), so the same art serves every colour and fade.
# Entry names (lower case): the hud.txt names ("number_0", "cross", "suit_full", "dmg_rad", "item_battery", "bucket1", ...),
# "<weapon file stem>:<role>" for the weapon scripts ("weapon_9mmar:weapon", "weapon_9mmar:ammo"), "pain_0".."pain_3" (front, right,
# rear, left) and "train_0".."train_4". Only the 640 resolution set is taken (VR draws it scaled; no 320 / 1280 / 2560 sets).
import os, struct

import spr
import gamedirs

RES = 640
ATLAS_W = 1024
WEAPON_ROLES = ("weapon", "weapon_s", "ammo", "ammo2")      # crosshair / autoaim / zoom roles are the 2D crosshair: n/a VR
HUD_MODS = ("valve", "gearbox", "bshift", "dmc", "ricochet")  # mods whose OWN folder ships a hud.txt (svencoop / cstrike: own HUD, not packed)
MAX_ENTRIES = 512


def parse_sprite_list(text, res=RES):
    """hud.txt / weapon_*.txt text -> [(name, sheet, x, y, w, h)] of resolution `res` in file order. Format: first token = entry
    count (ignored, the real list ends at the end of file), then `name res sheet x y w h` per line; `//` starts a comment; fields
    are separated by any run of tabs / spaces. Malformed lines are skipped (the engine reads them with sscanf and stops)."""
    out = []
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        t = line.split()
        if len(t) != 7:
            continue
        try:
            r, x, y, w, h = int(t[1]), int(t[3]), int(t[4]), int(t[5]), int(t[6])
        except ValueError:
            continue
        if r != res or w <= 0 or h <= 0 or x < 0 or y < 0:
            continue
        out.append((t[0].lower(), t[2].replace("\\", "/").lower(), x, y, w, h))
    return out


def select_rect(entries, name):
    """First entry of that name (the engine's lookup returns the first match for the wanted resolution)."""
    for e in entries:
        if e[0] == name:
            return e
    return None


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("latin-1")


def _find(dirs, sub, name):
    for d in dirs:
        p = os.path.join(d, sub, name)
        if os.path.isfile(p):
            return p
    low = name.lower()
    for d in dirs:
        try:
            for e in os.listdir(os.path.join(d, sub)):
                if e.lower() == low:
                    return os.path.join(d, sub, e)
        except OSError:
            pass
    return None


def intensity(frame):
    """spr.Frame (RGBA) -> w*h bytes, max(r, g, b) (additive sprites: black = no contribution)."""
    rgba = frame.rgba
    out = bytearray(frame.w * frame.h)
    for i in range(frame.w * frame.h):
        o = i * 4
        out[i] = max(rgba[o], rgba[o + 1], rgba[o + 2])
    return bytes(out)


class HudData:
    """sheets: [(key, w, h, L8 bytes)]; entries: [(name, sheet index, x, y, w, h)] with x / y inside the sheet."""

    def __init__(self):
        self.sheets = []
        self.entries = []
        self._ix = {}
        self.missing = []

    def sheet(self, key, w, h, data):
        if key not in self._ix:
            self._ix[key] = len(self.sheets)
            self.sheets.append((key, w, h, data))
        return self._ix[key]

    def has_sheet(self, key):
        return key in self._ix

    def add(self, name, key, x, y, w, h):
        if len(self.entries) >= MAX_ENTRIES or any(e[0] == name for e in self.entries):
            return
        i = self._ix[key]
        sw, sh = self.sheets[i][1], self.sheets[i][2]
        if x + w > sw or y + h > sh:
            self.missing.append(f"{name}: rect {x},{y},{w},{h} outside {key} {sw}x{sh}")
            return
        self.entries.append((name, i, x, y, w, h))


def load(game_root, mod):
    """-> HudData or None (mod without its own hud.txt)."""
    if mod not in HUD_MODS or not game_root:
        return None
    dirs = gamedirs.content_dirs(game_root, mod, fallback="valve")
    own = [d for d in dirs if os.path.basename(d).startswith(mod)]
    hp = _find(own, "sprites", "hud.txt")
    if not hp:
        return None
    hd = HudData()

    def sheet_of(name):
        key = name.lower()
        if hd.has_sheet(key):
            return key
        p = _find(dirs, "sprites", key + ".spr")
        if not p:
            hd.missing.append(key)
            return None
        try:
            s = spr.load(p)
        except (ValueError, struct.error, OSError):
            hd.missing.append(key)
            return None
        f = s.frames[0]
        hd.sheet(key, f.w, f.h, intensity(f))
        return key

    def feed(prefix, lst):
        for name, sheet, x, y, w, h in lst:
            key = sheet_of(sheet)
            if key:
                hd.add(prefix + name, key, x, y, w, h)

    feed("", parse_sprite_list(_read(hp)))
    stems = {}
    for d in reversed(dirs):                     # the mod's own script wins over valve's
        try:
            for e in sorted(os.listdir(os.path.join(d, "sprites"))):
                if e.lower().startswith("weapon_") and e.lower().endswith(".txt"):
                    stems[e[:-4].lower()] = os.path.join(d, "sprites", e)
        except OSError:
            pass
    for stem in sorted(stems):
        lst = [e for e in parse_sprite_list(_read(stems[stem])) if e[0] in WEAPON_ROLES]
        feed(stem + ":", lst)
    for base, count in (("pain", 4), ("train", 5)):
        p = _find(dirs, "sprites", f"{RES}_{base}.spr")
        if not p:
            continue
        try:
            s = spr.load(p)
        except (ValueError, struct.error, OSError):
            continue
        for i, f in enumerate(s.frames[:count]):
            key = f"{base}_{i}"
            hd.sheet(key, f.w, f.h, intensity(f))
            hd.add(key, key, 0, 0, f.w, f.h)
    return hd if hd.entries else None


def layout(sheets, atlas_w=ATLAS_W):
    """Shelf packing, tallest first: [(key, w, h, data)] -> ([(x, y)] in input order, atlas height rounded up to 4)."""
    order = sorted(range(len(sheets)), key=lambda i: (-sheets[i][2], -sheets[i][1], i))
    pos = [None] * len(sheets)
    x = y = shelf = 0
    for i in order:
        w, h = sheets[i][1], sheets[i][2]
        if w > atlas_w:
            raise ValueError("HUD sheet wider than the atlas: " + sheets[i][0])
        if x + w > atlas_w:
            x, y, shelf = 0, y + shelf, 0
        if shelf == 0:
            shelf = h
        pos[i] = (x, y)
        x += w
    return pos, -(-(y + shelf) // 4) * 4


def hud1_chunk(hd, name):
    """HudData, name(str) -> (off, len) in the GSM1 names table. Returns (chunk bytes, (atlasW, atlasH, entries))."""
    pos, ah = layout(hd.sheets)
    aw = ATLAS_W
    img = bytearray(aw * ah)
    for (key, w, h, data), (sx, sy) in zip(hd.sheets, pos):
        for r in range(h):
            o = (sy + r) * aw + sx
            img[o:o + w] = data[r * w:(r + 1) * w]
    tab = b""
    for nm, si, x, y, w, h in hd.entries:
        no, nl = name(nm)
        tab += struct.pack("<8i", no, nl, pos[si][0] + x, pos[si][1] + y, w, h, 0, 0)
    payload = struct.pack("<8i", len(hd.entries), aw, ah, 0, 0, 0, 0, 0) + tab + bytes(img)
    payload += b"\0" * (-len(payload) % 4)
    return b"HUD1" + struct.pack("<i", len(payload)) + payload, (aw, ah, len(hd.entries))


def parse_hud1(payload, names):
    """Reader used by the tests and tools/hudpack.py check: -> (aw, ah, {name: (x, y, w, h)}, atlas bytes)."""
    n, aw, ah = struct.unpack_from("<3i", payload, 0)
    if n < 0 or n > MAX_ENTRIES or aw <= 0 or ah <= 0 or 32 + n * 32 + aw * ah > len(payload):
        raise ValueError("HUD1 header invalid")
    ents = {}
    for i in range(n):
        no, nl, x, y, w, h = struct.unpack_from("<6i", payload, 32 + i * 32)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > aw or y + h > ah:
            raise ValueError("HUD1 entry out of range")
        ents[names[no:no + nl].decode("ascii")] = (x, y, w, h)
    return aw, ah, ents, payload[32 + n * 32:32 + n * 32 + aw * ah]
