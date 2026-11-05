"""W3P1 map pack converter: a Warcraft III map (.w3x/.w3m) + the player's own install -> one binary pack for the
VRChat world (loader: unity/Assets/W3VR/Scripts/W3PackLoader.cs). Bring your own game: everything is read from the
player's files at conversion time; nothing of it is part of the world.

usage: python3 tools/w3pack.py --map MAP.w3x --install DIR OUT.w3p [--no-models] [--jobs N]
       (--version prints the converter version)

Pack layout (little-endian, every section 4-byte aligned):
  header  'W3P1'  i32 format version (1)  i32 section count  i32 reserved
  directory: count x (char[4] tag, i32 offset, i32 bytes, i32 count)
  sections:
    INFO  UTF-8 "key=value" lines (name, tileset, tiles_w/h, origin, playable bounds, camera bounds, water colour)
    TATL  terrain atlas: i32 w, i32 h, i32 mips, i32 slots, then RGBA32 mip chain, rows bottom-up (Unity order).
          One 512x256 slot per ground texture (TATL slot k = w3e ground k); 256-wide textures fill the left half.
    TVTX  terrain vertices: count x (f32 x, f32 y, f32 z, f32 u, f32 v)   -- WC3 world units, z = height;
          grouped by layer (all layer-0 quads first, then layer 1 ...), 4 vertices per quad (TL, TR, BL, BR)
    TIDX  terrain indices: i32 per index, layers concatenated, each layer's indices relative to its first vertex
    TLAY  i32 per layer: index count of layer 0..3 (layer 0 opaque base, 1..3 alpha-blended in order); a layer has
          index count / 6 * 4 vertices
    WVTX  water vertices: count x (f32 x, f32 y, f32 z, f32 depth = water - ground, >= 0);  WIDX  water indices (i32)
    REGN  regions: count x (f32 left, bottom, right, top, i32 name offset into the NAME blob); NAME = 0-terminated names
    PATH  pathing grid: i32 w, i32 h, then w*h bytes (wpm flags, 32 world units per cell, row 0 = south)
    DTYP  doodad type ids: count x char[4]
    DOOD  doodads: count x (i32 type index, f32 x, y, z, angle, sx, sy, sz, i32 variation, i32 flags | life << 8)
    UNIT  preplaced units: count x (char[4] id, f32 x, y, z, angle, i32 player)
    MINI  minimap: i32 w, i32 h, RGBA32 rows bottom-up (war3mapMap.blp, for checks and the UI)
    MLST MNAM GEOS MVTX MIDX SEQS SNAM MVAT MATX TEXS DTYM UTYM  models (W2): see docs/w3p_models.md and
          tools/w3pack_models.py (skipped with --no-models)
    JMET JCOD JCST JREA JSTR JLOC JFUN JFAX JGLB JINI JNAT JSIG JUTY JNAM  map script compiled for the Udon JASS VM (W4):
          see docs/jvm_bytecode.md "Pack sections" and tools/w3pack_jvm.py (skipped with --no-jvm)

Terrain rules (format behaviour; tags as in the NOTES files):
  * tilepoint height = (ground - 0x2000 + (layer - 2) * 0x200) / 4, water = (level - 0x2000) / 4 - 89.6 [ext format
    doc, tools/w3map.py]; tile = 128 world units; tilepoint (i, j) at origin + 128 * (i, j).
  * ground texture BLP: 4x4 cells of 64 px; cell index = corner mask, bit 1 = bottom-right, 2 = bottom-left,
    4 = top-right, 8 = top-left (measured on the textures' alpha, this file's --probe); 512-wide textures carry 16
    full-cell variations in the right half [inf]. Per tile the corner textures are drawn in ascending texture index:
    the lowest one as a full cell (variation), every higher one with the mask of the corners that use it [ext].
  * BLP1 JPEG: header + mip data form one CMYK JPEG; the four stored channels are B, G, R, A, each inverted
    (value = 255 - stored) [obs: decoded textures].
  * Cliffs: v1 draws the cliff layer into the heights (slopes); cliff models come with W2.
"""
import argparse, io, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import w3mpq as mpq, w3slk as slk, w3map, w3pack_jvm, w3pack_models, war3fs  # noqa: E402

VERSION = "w3p1-85"  # 8: MATX as DXT5, 9: WSND sounds, 10: DXT5 checked (BUGS L-10), WSLB, effect models, 11: WUAK unit voices + fog natives, 12: race column, shops (JITS JSHP), WSPA spell art, music INFO (BUGS L-11), 13: AB_ALIAS Arep = Arst (fingerprint covers AB_ALIAS), 14: AB_BASES + Asud, 15: + AIde, 16: + ANca Aabr, 17: + AIva, 18: + Aliq, 19: + Afbk, 20: + AIta, 21: + Ainf Auhf, 22: + ANab, 23: + ANfl, 24: + ANic, 25: + Aphx, 26: + AHfs, 27: WUCS unit combat / death sounds, 28: + ANmo, 29: AB_ALIAS AOsh ANbf = AUcs, 30: + AOac = AEar, 31: + Asds, 32: + Aweb, 33: + ANst, 34: + AHbz ANrf, 35: GEOS flag 256 billboard (VAT pivot + MVTX normal offset), 36: UMIS missile art + USEL selection circles (BUGS P-MISSILE, P-SELRING), 37: + Awrp waygate (A-AB22), 38: + Arej rejuvenation (A-SP80), 39: + Arpl Arpm replenish (A-SP81), 40: + ANvc volcano (A-SP82), 41: USPL structure ubersplats (NOTES_units §4.2), 42: + AUls locust swarm (A-SP83), 43: SetGameSpeed / SetPlayerOnScoreScreen / RemoveGuardPosition / RemoveAllGuardPositions implemented (native kinds), 44: missile art bakes Stand + Death (BUGS P-IMPACT), 45: MPEM / MPSQ particle emitters, particle-only effect / missile models kept (BUGS P-PART), 46: base tables of the map's balance set (Custom_V1 for custom maps, BUGS DS-1), 47: JDST destructable types + placed destructables (pathing, BUGS DS-2), 48: unit / item tooltip overrides of the map (BUGS UI-TIP), 49: JERR = why the JVM sections are missing (BUGS L-NOJVM), 50: Reforged w3i 31 / doo 8.11 / junk w3r read, 1.32 skin natives declared + implemented (BUGS LTD-W3I, LTD-BLZ), 51: JUT5 + Revive (rally ability of a Revive=1 building), JMET slot_order (team maps), the map's own Units\\*.slk / *.txt before the install's (BUGS FF-RALLY, FF-SLOTS, FF-SLK), 52: UTYM flags carry the type tint + forced team colour (BUGS ETD-LOOK), Q lines = per-level research tooltips (BUGS ETD-TIP), 53: effect / spell-art / missile sequences at 30 fps, never budget-dropped (BUGS FX-ANIM, FX-HEADTAIL, FX-BLEND), 54: JUQR per-level research requirements (BUGS ETD-ORA3), 55: + AIms AImm AIrm AIrg item bases + the MoveSpeedBonusesStack key (BUGS W9-4), 56: AB_ALIAS AHtc = ACtc (BUGS W9-3-GEM1), 57: JRND per unit type nbrandom + Propernames (BUGS W9R2-1), 58: + Arel Aamk AIsb AIob AIfb AIpb item bases (BUGS W9R2-3), 59: + Apo2 (the AIpb poison sibling, BUGS W9R2-3); 60: JVER by the slot layout (GetPlayerNeutralPassive), not InitHashtable: a 1.26 install is '121' (BUGS LTW-SHOPS), 61: + Adef Acrs ANht Acan ability bases (NOTES_abilities §5.57 .. §5.60, BUGS W9R3-2), 62: JMSC + the hero revive keys (ReviveBaseFactor ... ReviveRefundRate, BUGS W9R3-1), 63: + AIrc ability base (Ankh of Reincarnation, BUGS BOSS-ANKH), 64: JDTG destructable targType flags (units attack destructables, BUGS W9-PYR-DEST); 65: JLOC localized GetLocalizedString keys (BUGS GAMEOVER-DIALOG); 66: JUT5 + sight / nsight day / night sight radius (fog of war, BUGS FOG); 67: JUPE research effect slots (effect id, base, mod x4) + JUUP per unit type Upgrades Used list + defUp (research effects on units, BUGS FF-UPG); 68: + ACrn ability base (unit Reincarnation, BUGS ETD-REINC); 69: + Ahar Ahrl Awha Aaha Agld Agmb Abgm Aeat harvest bases (PORT-Harvest); 70: melee start units (htow hpea ogre opeo unpl uaco etol ewsp) seed models / icons / voices when the script runs MeleeStartingUnits (BUGS MELEE-SEEDS), 71: + Aent Aegm Aenc entangle bases (PORT-Entangle), 72: the JASS compiler short-circuits and (PORT2, pack code changes), 73: JAIP real melee AI programs from the install (off by default, docs/ai_plan.md S1); 74: unit animations (BUGS UNIT-ANIM): Attack / Spell / Stand Work baked at 30 fps for units, only the first of each category + the 'Attack - 2' variant (Stand/Walk Gold/Lumber variants gone), Birth/Morph dropped before Attack/Death by the budget; WUCS T rows + a work label (the Stand Work SND event) and the harvesters' weapType+Wood chop label; old packs read as before; + JABL header 32 bytes (the ability's Animnames text) and the bake keeps the Spell / Stand Channel / Attack Slam variants the unit's abilities name (BUGS SPELL-ANIM); + UHGT per unit type move kind + Movement - Height (flyers are drawn at their height, hover / float units on the water surface, BUGS UNIT-HEIGHT); 75: JAIP (the race AI programs of the install) is packed by default for melee maps (W3_PACK_AI=0 / --no-ai-programs leaves it out), the lobby Computer slots play with it (docs/ai_plan.md S7); also + Aroo ability base (CAbilityRoot: Ancients root / uproot, enum 167, PORT4-Build) and the JUPE slot's code column (the ability a rlev research levels, PORT4-Upgrade); 76: PORT5-NativesB native kinds (UnitSuspendDecay, the sleep natives, UnitDropItemPoint / Target / Slot implemented); 77: PORT5-NativesB round 2 native kinds (UnitHasBuffsEx / UnitCountBuffsEx / UnitRemoveBuffs(Ex), IsUnitDetected / IsUnitInvisible, UnitShareVision, DestroyEffect, the minimap filter states implemented); 78: PORT6-Inventory ability bases AIre AIra AImr AIrv AIlu (enum 168 .. 172), the item class order ids (AIhe 0xd0130 AIha 0xd0131 AImr 0xd0135 AIre 0xd0139 AIra 0xd013a AIrv 0xd0284) in JABL, AB_ALIAS AIwb = Aweb, item abilList tokenised on space / comma / hyphen / underscore / tab; 79: PORT7-RestGameplay ability base AIfs (figurine items AIfs AIfd AIfh AIff AIfr AIfu, enum 173; the DataC / DataD unit types ride in the JABL C / D reals like Arai); 80: PORT8-NativesB round 3 native kinds (UnitDamagePoint, GetPlayerScore implemented); 81: the glue pack carries the game menu screens EscMenuMain / EscMenuSave / EscMenuLoad (SAVE-MENU) + SAVE-2 natives SaveGameCache / ReloadGameCachesFromDisk (impl 1640 / 1641); 82: + Arbr ability base (rart armor-type ability, enum 174, PORT11-Upgrade); 83: + the EffectDeathTime misc key (the dead item's 5 s lifetime, PORT13-Inventory), 84: + Aave ability base (the ConvertUnits morph record, enum 175, PORT13-ConvertFix); 85: the glue pack carries HTEX / HINF (hall textures + crests, docs/w3_hub_plan.md H2)


def source_digest():
    """sha1 (12 hex) over the converter sources next to this file and in jvm/ (tests excluded): the companion's pack
    cache key changes with every engine-table / compiler change, not only with VERSION bumps."""
    import hashlib
    h = hashlib.sha1()
    here = os.path.dirname(os.path.abspath(__file__))
    for sub in ('', 'jvm'):
        d = os.path.join(here, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith('.py') and not fn.startswith('test_'):
                with open(os.path.join(d, fn), 'rb') as fh:
                    h.update((sub + '/' + fn).encode() + b'\0' + fh.read())
    return h.hexdigest()[:12]
SLOT_W, SLOT_H, CELL = 512, 256, 64
MIPS = 4                         # 64 -> 32 -> 16 -> 8 px per cell (no bleeding between cells: mips built per cell)
CORNER_BR, CORNER_BL, CORNER_TR, CORNER_TL = 1, 2, 4, 8


def blp_rgba(data):
    """BLP1 (JPEG or paletted) -> PIL RGBA image of mip 0."""
    from PIL import Image, ImageChops
    magic, comp, abits, w, h, _pt, _ps = struct.unpack_from('<4s6I', data, 0)
    if magic != b'BLP1':
        raise ValueError('not BLP1: %r' % magic)
    offs = struct.unpack_from('<16I', data, 28)
    sizes = struct.unpack_from('<16I', data, 92)
    if comp == 0:
        hs = struct.unpack_from('<I', data, 156)[0]
        jpg = data[160:160 + hs] + data[offs[0]:offs[0] + sizes[0]]
        im = Image.open(io.BytesIO(jpg))
        if im.mode != 'CMYK':
            im = im.convert('CMYK')
        b, g, r, a = [ImageChops.invert(ch) for ch in im.split()]
        return Image.merge('RGBA', (r, g, b, a))
    pal = data[156:156 + 1024]
    px = data[offs[0]:offs[0] + w * h]
    alpha = data[offs[0] + w * h:offs[0] + w * h + (w * h * abits + 7) // 8] if abits else b''
    out = bytearray(w * h * 4)
    for i, c in enumerate(px):
        out[i * 4:i * 4 + 3] = bytes((pal[c * 4 + 2], pal[c * 4 + 1], pal[c * 4]))
        if abits == 8:
            a = alpha[i]
        elif abits == 1:
            a = 255 if alpha[i >> 3] >> (i & 7) & 1 else 0
        elif abits == 4:
            a = (alpha[i >> 1] >> ((i & 1) * 4) & 15) * 17
        else:
            a = 255
        out[i * 4 + 3] = a
    return Image.frombytes('RGBA', (w, h), bytes(out))


class Files:
    """Map archive first (imports override), then the install's core files (any version: war3fs, docs/versions.md).
    map_path: a file, or a map key / CASC path resolved inside the install."""

    def __init__(self, map_path, install):
        self.core = war3fs.open_install(install)
        if os.path.isfile(map_path):
            self.map = mpq.MPQ(map_path, tables=self.core.tables, limits=True)
            self.map.check_limits()          # untrusted map: a decompression bomb stops here (mpq.Bomb)
        else:
            maps = self.core.maps()
            hit = maps.get(map_path.lower())
            if hit is None:
                hit = next((v for v in maps.values() if v[1] == map_path), None)
            if hit is None:
                raise SystemExit('map %r not found in %s (%d maps)' % (map_path, install, len(maps)))
            self.map = mpq.MPQ(self.core.read_map(hit[1]), tables=self.core.tables)
        self.version = self.core.version
        # BUGS DS-1: base tables of the map's balance set (custom maps: Custom_V1) for every reader of the install
        self.data_set = war3fs.data_set(self.map.read('war3map.w3i', missing_ok=True))
        self.core.set_data_set(self.data_set)

    def read(self, name, missing_ok=False, limit=None):
        d = self.map.read(name, missing_ok=True, limit=limit)
        if d is not None:
            return d
        return self.core.read(name, missing_ok=missing_ok, limit=limit)


def cell_rect(cell):
    """(x0, y0) in a slot (top-left origin, pixels) of cell 0..31 (16..31 = right half)."""
    half, c = divmod(cell, 16)
    return half * 256 + (c % 4) * CELL, (c // 4) * CELL


def build_atlas(files, ground, tiles):
    """-> (atlas bytes (mip chain, bottom-up rows), w, h, extended flags per slot)."""
    from PIL import Image
    n = max(1, len(ground))
    levels = []
    ext = []
    slots = []
    for gid in ground:
        row = tiles.get(gid)
        im = None
        if row:
            data = files.read('%s\\%s.blp' % (row['dir'], row['file']), missing_ok=True)
            if data:
                im = blp_rgba(data)
        if im is None:
            im = Image.new('RGBA', (256, 256), (255, 0, 255, 255))
        if im.size[1] != 256:
            im = im.resize((im.size[0] * 256 // im.size[1], 256), Image.BILINEAR)
        ext.append(im.size[0] >= 512)
        slots.append(im)
    for lvl in range(MIPS):
        cw = CELL >> lvl
        lw, lh = SLOT_W >> lvl, SLOT_H * n >> lvl
        page = Image.new('RGBA', (lw, lh), (0, 0, 0, 0))
        for k, im in enumerate(slots):
            for cell in range(32 if ext[k] else 16):
                x0, y0 = cell_rect(cell)
                c = im.crop((x0, y0, x0 + CELL, y0 + CELL))
                if lvl:
                    c = c.resize((cw, cw), Image.BOX)
                page.paste(c, ((x0 >> lvl), (k * SLOT_H + y0) >> lvl))
        levels.append(page.transpose(Image.FLIP_TOP_BOTTOM).tobytes())
    return b''.join(levels), SLOT_W, SLOT_H * n, ext


def terrain_mesh(t, ext, n_slots):
    """-> (vertex floats grouped by layer, index lists per layer, relative to the layer's first vertex)."""
    lv = [[], [], [], []]
    layers = [[], [], [], []]
    W, H = t.w, t.h
    aw, ah = float(SLOT_W), float(SLOT_H * n_slots)
    inset = 0.5

    def uv(slot, cell, cx, cy):
        x0, y0 = cell_rect(cell)
        px = x0 + (inset if cx == 0 else CELL - inset)
        py = slot * SLOT_H + y0 + (inset if cy == 0 else CELL - inset)        # top-left origin
        return px / aw, 1.0 - py / ah                                          # Unity: bottom-up

    for j in range(H - 1):
        for i in range(W - 1):
            bl, br = j * W + i, j * W + i + 1
            tl, tr = (j + 1) * W + i, (j + 1) * W + i + 1
            corners = {CORNER_BL: bl, CORNER_BR: br, CORNER_TL: tl, CORNER_TR: tr}
            texs = sorted(set(t.tex[c] for c in corners.values()))
            pos = {}
            for bit, c in corners.items():
                pos[bit] = (t.ox + 128.0 * (c % W), t.oy + 128.0 * (c // W), t.height[c])
            for li, tx in enumerate(texs[:4]):
                slot = min(tx, n_slots - 1)
                if li == 0:
                    v = t.variation[bl]
                    cell = 16 + (v % 16) if ext[slot] and v < 16 else 15
                else:
                    cell = sum(bit for bit, c in corners.items() if t.tex[c] == tx)
                vtx = lv[li]
                base = len(vtx) // 5
                # image space: top = +y (north); quad corners TL, TR, BL, BR
                for bit, cx, cy in ((CORNER_TL, 0, 0), (CORNER_TR, 1, 0), (CORNER_BL, 0, 1), (CORNER_BR, 1, 1)):
                    x, y, z = pos[bit]
                    u, w = uv(slot, cell, cx, cy)
                    vtx += [x, y, z, u, w]
                # two triangles, counter-clockwise seen from above (+z) in WC3 space
                layers[li] += [base + 2, base + 3, base + 1, base + 2, base + 1, base + 0]
    return lv[0] + lv[1] + lv[2] + lv[3], layers


def water_mesh(t):
    vtx, idx = [], []
    W, H = t.w, t.h
    for j in range(H - 1):
        for i in range(W - 1):
            cs = (j * W + i, j * W + i + 1, (j + 1) * W + i, (j + 1) * W + i + 1)
            if not any(t.flags[c] & 0x40 for c in cs):
                continue
            base = len(vtx) // 4
            for c in cs:
                vtx += [t.ox + 128.0 * (c % W), t.oy + 128.0 * (c // W), t.water[c], max(0.0, t.water[c] - t.height[c])]
            idx += [base + 0, base + 1, base + 3, base + 0, base + 3, base + 2]
    return vtx, idx


PACK_SOFT_MAX = 100 * 1024 * 1024 - 2 * 1024 * 1024     # companion MAX_PACK (VRChat's limit) minus a margin

# BUGS PACK-64: the world loader and the companion up to 0.9.5 refuse a pack with more than 64 sections. Optional
# sections go first when a big map would exceed it (JLOC: GetLocalizedString keys stay raw).
MAX_SECTIONS = 128                # world + companion >= 0.9.6 take 128 (uploaded 2026-10-29); 0.9.5 and older 64
OPTIONAL_SECTIONS = (b'JLOC', b'JAIP')


def cap_sections(secs, log=print):
    for tag in OPTIONAL_SECTIONS:
        if len(secs) <= MAX_SECTIONS:
            break
        keep = [x for x in secs if x[0] != tag]
        if len(keep) < len(secs):
            log('w3pack: %d sections > %d: %s left out' % (len(secs), MAX_SECTIONS, tag.decode()))
            secs[:] = keep
    if len(secs) > MAX_SECTIONS:
        raise RuntimeError('pack has %d sections, the loader takes at most %d' % (len(secs), MAX_SECTIONS))
    return secs



def write_sections(secs, out_path):
    cap_sections(secs)
    head = 16 + 16 * len(secs)
    off = head
    directory = bytearray()
    body = bytearray()
    for tag, data, count in secs:
        pad = (-len(data)) % 4
        directory += struct.pack('<4s3i', tag, off, len(data), count)
        body += data + b'\0' * pad
        off += len(data) + pad
    with open(out_path, 'wb') as fh:
        fh.write(struct.pack('<4s3i', b'W3P1', 1, len(secs), 0) + bytes(directory) + bytes(body))


def glue_pack(install, out_path, log=print):
    """the world's start menu + lobby (docs/ui_plan.md L3): no map, the install's glue screens."""
    import w3glue
    core = war3fs.open_install(install)
    import w3sound
    il = ['name=__glue'] + w3sound.music_lines(lambda n: core.read(n, missing_ok=True))
    secs = [(b'INFO', ('\n'.join(il) + '\n').encode('utf-8'), len(il))] + w3glue.glue_sections(core, log)
    try:                                # H2 (docs/w3_hub_plan.md 3.2): the hall's wood / stone / ground tiles + banner crests; never fatal
        import w3hub
        secs += w3hub.hub_sections(core, log)
    except Exception as e:              # noqa: BLE001
        log('w3pack: hub textures skipped: %s: %s' % (type(e).__name__, e))
    write_sections(secs, out_path)
    log('w3pack: glue -> %s  %d bytes' % (out_path, os.path.getsize(out_path)))


def pack(map_path, install, out_path, log=print, models=True, jobs=None, jvm=True, ai_programs=True):
    import time
    from PIL import Image
    t0 = time.time()
    phases = [[t0]]                     # docs/load_times.md: seconds per converter phase (log line at the end)

    def phase(name):
        now = time.time()
        phases.append((name, now - phases[0][0]))
        phases[0][0] = now
    files = Files(map_path, install)
    phase('open')
    t = w3map.parse_w3e(files.read('war3map.w3e'))
    files.core.set_tileset(t.tileset)
    wts_raw = files.read('war3map.wts', missing_ok=True)
    wts = w3map.parse_wts(wts_raw) if wts_raw else {}
    info = w3map.parse_w3i(files.read('war3map.w3i'), wts)
    tiles = slk.table(files.read('TerrainArt\\Terrain.slk').decode('latin1'), 'tileID')
    atlas, aw, ah, ext = build_atlas(files, t.ground, tiles)
    vtx, layers = terrain_mesh(t, ext, max(1, len(t.ground)))
    wv, wi = water_mesh(t)
    regions = []
    raw = files.read('war3map.w3r', missing_ok=True)
    if raw:
        regions = w3map.parse_w3r(raw)
    wpm = w3map.parse_wpm(files.read('war3map.wpm'))
    doo = w3map.parse_doo(files.read('war3map.doo'))['doodads']
    raw = files.read('war3mapUnits.doo', missing_ok=True)
    units = w3map.parse_units_doo(raw)['units'] if raw else []
    mini = None
    raw = files.read('war3mapMap.blp', missing_ok=True)
    if raw:
        try:
            mini = blp_rgba(raw)
        except Exception as e:          # minimap is optional
            log('  minimap skipped: %s' % e)
    phase('terrain')

    secs = []
    cb = info.get('camera_bounds', [0] * 8)
    wr = info.get('water_rgba', [0, 0, 255, 255])
    lines = ['name=' + info.get('name', ''), 'tileset=' + t.tileset, 'tiles_w=%d' % (t.w - 1), 'tiles_h=%d' % (t.h - 1),
             'origin=%g,%g' % (t.ox, t.oy), 'playable=%d,%d' % (info.get('playable_w', 0), info.get('playable_h', 0)),
             'camera=' + ','.join('%g' % v for v in cb), 'water_rgba=' + ','.join(str(v) for v in wr),
             'players=%d' % len(info.get('players', [])), 'converter=' + VERSION, 'version=' + files.version]
    try:                                # S5: the race playlists (W3Music; the mp3 come from the companion)
        import w3sound
        lines += w3sound.music_lines(lambda n: files.read(n, missing_ok=True))
    except Exception as e:              # noqa: BLE001
        log('w3pack: music lines skipped: %s: %s' % (type(e).__name__, e))
    secs.append((b'INFO', ('\n'.join(lines) + '\n').encode('utf-8'), len(lines)))
    secs.append((b'TATL', struct.pack('<4i', aw, ah, MIPS, len(t.ground)) + atlas, len(t.ground)))
    secs.append((b'TVTX', struct.pack('<%df' % len(vtx), *vtx), len(vtx) // 5))
    allidx = layers[0] + layers[1] + layers[2] + layers[3]
    secs.append((b'TIDX', struct.pack('<%di' % len(allidx), *allidx), len(allidx)))
    secs.append((b'TLAY', struct.pack('<4i', *(len(l) for l in layers)), 4))
    secs.append((b'WVTX', struct.pack('<%df' % len(wv), *wv), len(wv) // 4))
    secs.append((b'WIDX', struct.pack('<%di' % len(wi), *wi), len(wi)))
    names = bytearray()
    reg = bytearray()
    for r in regions:
        reg += struct.pack('<4fi', r['left'], r['bottom'], r['right'], r['top'], len(names))
        names += r['name'].encode('utf-8', 'replace') + b'\0'
    secs.append((b'REGN', bytes(reg), len(regions)))
    secs.append((b'NAME', bytes(names), len(regions)))
    secs.append((b'PATH', struct.pack('<2i', wpm['w'], wpm['h']) + bytes(wpm['cells']), wpm['w'] * wpm['h']))
    types = sorted(set(d['id'] for d in doo))
    tix = {k: n for n, k in enumerate(types)}
    secs.append((b'DTYP', b''.join(k.encode('latin1')[:4].ljust(4, b'\0') for k in types), len(types)))
    dd = bytearray()
    for d in doo:
        dd += struct.pack('<i7fii', tix[d['id']], d['x'], d['y'], d['z'], d['angle'], *d['scale'], d['var'],
                          d['flags'] | d['life'] << 8)
    secs.append((b'DOOD', bytes(dd), len(doo)))
    uu = bytearray()
    for u in units:
        uu += struct.pack('<4s4fi', u['id'].encode('latin1')[:4].ljust(4, b'\0'), u['x'], u['y'], u['z'], u['angle'],
                          u['player'])
    secs.append((b'UNIT', bytes(uu), len(units)))
    if mini is not None:
        secs.append((b'MINI', struct.pack('<2i', *mini.size) + mini.transpose(Image.FLIP_TOP_BOTTOM).tobytes(), 1))
    if jvm:
        try:
            secs += w3pack_jvm.build_from_files(files, log, ai_programs=ai_programs)
        except Exception as e:          # a map whose script does not compile still gets its terrain/model pack
            log('w3pack: jvm sections skipped: %s: %s' % (type(e).__name__, e))
            why = ('%s: %s' % (type(e).__name__, e))[:300]
            secs.append((b'JERR', why.encode('utf-8'), 1))   # BUGS L-NOJVM: the world shows why the map has no logic
    phase('jvm')
    msum = None
    if models:
        tm = time.time()
        art_models, art_sec = [], None
        try:                            # td_score: the spell art (CasterArt / TargetArt / EffectArt / SpecialArt)
            import w3spellart
            sj = files.map.read('war3map.j', missing_ok=True) or files.map.read('scripts\\war3map.j', missing_ok=True) or b''
            art_models, art_sec = w3spellart.spell_art(files, units, sj, log)
        except Exception as e:          # noqa: BLE001
            log('w3pack: spell art skipped: %s: %s' % (type(e).__name__, e))
        msecs, msum = w3pack_models.build(files, doo, units, log, blp_rgba, jobs=jobs, extra_effects=art_models)
        if art_sec:
            msecs.append(art_sec)
        secs += msecs
        msum += ', %.1f s' % (time.time() - tm)
        phase('models')
    try:                                # UI: the install's HUD layout + textures + font (docs/ui_plan.md U3)
        import w3ui
        secs += w3ui.ui_sections(files.core, log, lambda n: files.map.read(n, missing_ok=True), getattr(files, 'ui_objs', None))
    except Exception as e:
        log('w3pack: ui sections skipped: %s: %s' % (type(e).__name__, e))
    phase('ui')
    try:                                # the map's sounds (docs/sound_plan.md S1)
        import w3sound
        from jvm.run import strip_bom
        js = files.map.read('war3map.j', missing_ok=True) or files.map.read('scripts\\war3map.j', missing_ok=True) or b''
        js = strip_bom(js)                                         # JASS-BOM
        bj = files.read('Scripts\\Blizzard.j', missing_ok=True) or b''
        rd = lambda n, limit=None: files.read(n, missing_ok=True, limit=limit)            # noqa: E731
        table = w3sound.sound_labels(rd)
        labels = w3sound.script_labels(bj.decode('latin1')) + [l for l in w3sound.script_labels(js.decode('latin1'))]
        labels = list(dict.fromkeys(labels))
        try:                            # S4: the commanded units' acknowledgement voices
            import re
            uids = set(u['id'] for u in units) | set(re.findall(r"'([\x21-\x7e]{4})'", js.decode('latin1')))
            import w3ui                  # BUGS MELEE-SEEDS
            uids |= set(w3ui.melee_seeds(js))
            ack_files, ack = w3sound.unit_ack(files, uids, units, log)
        except Exception as e:          # noqa: BLE001
            log('w3pack: unit voices skipped: %s: %s' % (type(e).__name__, e))
            ack_files, ack = [], None
        try:                            # S4b: death / weapon impact sounds of the map's unit types
            cmb_files, cmb = w3sound.unit_combat(files, uids, log)
        except Exception as e:          # noqa: BLE001
            log('w3pack: combat sounds skipped: %s: %s' % (type(e).__name__, e))
            cmb_files, cmb = [], None
        base_files = list(w3sound.UI_SOUNDS) + w3sound.script_sounds(js.decode('latin1')) + [table[l][0] for l in labels if l in table]
        caps = {f: w3sound.COMBAT_SECONDS for f in cmb_files if f not in ack_files}
        snd = w3sound.wsnd_section(rd, list(dict.fromkeys(base_files + ack_files + cmb_files)), log, caps=caps)
        # the VRChat download limit (companion builder MAX_PACK): close to it, the combat clips go first, then the
        # voices (their tables stay; a label without a clip is not played) — Skibi 1.31 was 101.3 MB with all clips
        rest = sum(len(d) + 32 for _t, d, _c in secs)
        if snd and rest + len(snd[1]) > PACK_SOFT_MAX:
            log('w3pack: pack near the download limit (%.1f MB): combat sound clips dropped' % ((rest + len(snd[1])) / 1e6))
            snd = w3sound.wsnd_section(rd, list(dict.fromkeys(base_files + ack_files)), log)
        if snd and rest + len(snd[1]) > PACK_SOFT_MAX:
            log('w3pack: still near the download limit: voice clips dropped')
            snd = w3sound.wsnd_section(rd, list(dict.fromkeys(base_files)), log)
        if ack:
            secs.append(ack)
        if cmb:
            secs.append(cmb)
        if snd:
            secs.append(snd)
        lab = w3sound.wslb_section(labels, table)     # docs/sound_plan.md: CreateSoundFromLabel's files
        if lab:
            secs.append(lab)
    except Exception as e:
        log('w3pack: sound section skipped: %s: %s' % (type(e).__name__, e))
    phase('sound')

    cap_sections(secs, log)
    head = 16 + 16 * len(secs)
    off = head
    directory = bytearray()
    body = bytearray()
    for tag, data, count in secs:
        pad = (-len(data)) % 4
        directory += struct.pack('<4s3i', tag, off, len(data), count)
        body += data + b'\0' * pad
        off += len(data) + pad
    blob = struct.pack('<4s3i', b'W3P1', 1, len(secs), 0) + bytes(directory) + bytes(body)
    tmp = out_path + '.part'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, out_path)
    phase('write')
    log('w3pack: phases ' + ', '.join('%s %.1f s' % p for p in phases[1:]))
    log('w3pack: %s -> %s  %d bytes | terrain %d verts, layers %s | water %d verts | %d regions, %d doodads (%d types), '
        '%d units | atlas %dx%d (%d slots, ext %s)' % (os.path.basename(map_path), out_path, len(blob), len(vtx) // 5,
                                                      [len(l) // 6 for l in layers], len(wv) // 4, len(regions),
                                                      len(doo), len(types), len(units), aw, ah, len(t.ground),
                                                      ''.join('1' if e else '0' for e in ext)))
    if msum:
        log('w3pack: %s %s | total %.1f s' % (os.path.basename(map_path), msum, time.time() - t0))
    return len(blob)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--version', action='store_true')
    ap.add_argument('--map', help='map file, or a map key / CASC path inside --install (1.31: maps live in CASC)')
    ap.add_argument('--install')
    ap.add_argument('out', nargs='?')
    ap.add_argument('--no-models', action='store_true', help='terrain + tables only (no W2 model sections)')
    ap.add_argument('--ai-programs', action='store_true', help='(default since w3p1-75) pack the race AI programs of the install (JAIP, melee maps, local packs only)')
    ap.add_argument('--no-ai-programs', action='store_true', help='leave the JAIP section out (or W3_PACK_AI=0): the lobby Computer slots play with the simple bot')
    ap.add_argument('--no-jvm', action='store_true', help='no JVM sections (compiled map script)')
    ap.add_argument('--glue', action='store_true', help='the start-menu / lobby pack of the install (no map): JGL1 + UIAT')
    ap.add_argument('--jobs', type=int, default=None, help='model baking processes (default: a quarter of the logical CPUs, 1..4, see w3pack_models.bake_jobs)')
    a = ap.parse_args()
    if a.version:
        print(VERSION + '+' + source_digest())
        return 0
    if a.glue or a.map == '__glue':                 # the companion asks for war3/__glue like a map
        if not (a.install and a.out):
            ap.error('--glue needs --install and OUT')
        glue_pack(a.install, a.out)
        return 0
    if not (a.map and a.install and a.out):
        ap.error('--map, --install and OUT are required')
    pack(a.map, a.install, a.out, models=not a.no_models, jobs=a.jobs, jvm=not a.no_jvm, ai_programs=not a.no_ai_programs)
    return 0


if __name__ == '__main__':
    sys.exit(main())
