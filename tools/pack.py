# Builds a GSV3 (default) or GSV2 map pack for the VRChat loader (unity/Assets/GoldSrcVR/Scripts/GsvrPackLoader.cs).
# usage: python3 -I pack.py --synthetic out.bin            (no Valve data: procedural terrain + checker texture)
#        python3 -I pack.py --bsp <map.bsp> --game-root input/Half-Life --mod <valve|cstrike|…> out.bin
#        optional: --max-tex 256 (cap texture edge before atlasing), --no-entities-brushes (world model only),
#                  --format 2 (old single-mesh GSV2 layout; the loader reads both)
#
# ── GSV2 layout ── little endian; every section starts 4-byte aligned (all section sizes are multiples of 4)
#   header (64 bytes, int32 unless noted)
#    0  char[4] "GSV2"
#    4  headerBytes        = 64 (sections start here)
#    8  vertexCount  V
#   12  indexCount   I     (triangle list, I % 3 == 0)
#   16  solidIndexCount S  (indices [0,S) are solid → MeshCollider; [S,I) render-only, e.g. func_illusionary)
#   20  atlasW   AW        diffuse atlas, multiple of 8
#   24  atlasH   AH        multiple of 8
#   28  atlasMips M        mip levels stored (4 → levels 0..3, level l is (AW>>l)x(AH>>l))
#   32  lightW   LW        lightmap atlas
#   36  lightH   LH
#   40  entityBytes E      length of the entity text (latin-1, without padding)
#   44  flags              bit0 = map has real lightmap data (else lightmap atlas is neutral grey); bit1 = the lightmap
#                          atlas holds 2 pages (LW = 2048 wide, page 0 rows [0, LW), page 1 below it, no face block
#                          across the boundary; the largest blocks may be at half resolution) — docs/sven_world.md §2
#   48  textureCount       distinct diffuse textures in the atlas (info only)
#   52  reserved[3] = 0
#   sections, in this order:
#   float32 pos [V*3]   Unity space, metres: (x, z, y) * 0.0254 from GoldSrc (x, y, z); triangles (0,i,i+1) per face
#   float32 uv0 [V*2]   texture space in *tiles*: (s/texW, t/texH) — unbounded, the shader takes frac()
#   float32 uv1 [V*2]   lightmap atlas UV in [0,1] (u = column / LW, v = row / LH, row 0 = first row in the data)
#   float32 rect[V*4]   atlas rect of the face's texture (x, y, w, h) normalised to [0,1]; final diffuse UV =
#                       rect.xy + frac(uv0) * rect.zw.  Each texture has an 8-px wrapped gutter (1 px at mip 3)
#   int32   idx [I]
#   uint8   atlas RGBA32, mip levels 0..M-1 concatenated (= Unity's raw layout for Texture2D(AW,AH,RGBA32,M,…))
#                       row 0 of each level is v = 0. Alpha 0 = cut-out ('{' textures, palette index 255)
#   uint8   lightmap RGBA32 [LW*LH*4], no mips, A = 255. Value 128 = neutral (×2 overbright in the shader = 1.0)
#   uint8   entities [E padded up to a multiple of 4]
#
# ── GSV3 layout ── = GSV2 core with these changes (full spec + rationale: docs/goldsrc_base_layer.md):
#   header: magic "GSV3"; 8 V and 12 I now count ALL vertices/indices (world model first, then every brush-entity
#     model); 16 S = solid indices of the WORLD; 52 worldVertexCount WV; 56 worldIndexCount WI (world render
#     triangles are idx[0,WI), world collision idx[0,S)); 60 dirOffset = byte offset of the section directory.
#   core sections unchanged in order/encoding. World indices are absolute (= relative to vertex 0). Brush-model
#     vertices are MODEL-LOCAL (raw BSP coordinates, i.e. relative to the entity origin — the origin brush centre when
#     the model has one) converted like world vertices; their indices are relative to the model's firstVertex.
#   directory (at dirOffset, after the padded entity text): int32 count N, then N × 16 bytes
#     { char[4] tag, int32 byteOffset (from file start), int32 byteSize, int32 elementCount }; sections follow.
#   "MODL" count R, 96 bytes per record (24 × 4-byte fields; r = 0 is the world model):
#     i0 bspModel  i1 entityIndex (-1 none)  i2 firstVertex  i3 vertexCount  i4 firstIndex
#     i5 renderStart i6 renderCount  i7 collideStart i8 collideCount   (index ranges relative to firstIndex)
#     i9 flags: 1 render tris, 2 collide tris, 4 volume class (trigger_/func_ladder/zones: no tris, logic only),
#               8 static-mergeable (func_wall/func_illusionary without targetname, opaque), 16 non-solid class,
#               32 translucent (rendermode 1/2/3/5 with renderamt > 0: drawn blended, never merged/flag 8; see REND)
#     i10..i13 headnode[4] for hulls 0..3 as HNOD indices (< 0 = the hull is a single contents leaf)
#     f14..f16 origin (GoldSrc units, from the entity's "origin" key)   f17..f19 mins  f20..f22 maxs (dmodel bounds,
#     model-local GoldSrc units, already 1 unit larger than the brushes on every side)   i23 reserved 0
#   "PLAN" count P: float32 × 4 per plane (nx, ny, nz, dist) in GoldSrc space (Z up, units)
#   "HNOD" count H: int32 × 3 per node (plane, front child, back child). child >= 0 → HNOD index; child < 0 →
#     contents (-1 empty, -2 solid, -3 water, -4 slime, -5 lava, -6 sky, -16 ladder, …). Nodes [0,H0) are hull 0
#     (BSP nodes, leaf children replaced by the leaf's contents → point contents incl. water), nodes [H0,H) are the
#     clipnodes of hulls 1-3 (shared by all models), child indices already offset by H0.
#   "ENTS" count = entities: int32 × 4 (firstKv, kvCount, modelRecord (-1), reserved 0)
#   "EKVS" count = keyvalues: int32 × 4 (keyOffset, keyLength, valueOffset, valueLength) — byte offsets into the
#     entity text, in file order (duplicate keys kept, e.g. multi_manager "door#1")
#   optional bot navigation mesh (tools/nav5.py, from <mod>/maps/<map>.nav or czero/maps/<map>.nav; older loaders skip
#   unknown tags). Areas are referenced by INDEX (not the file's ids); GoldSrc units:
#   "NAVA" count A: 14 × 4 bytes: f32 lo.x lo.y hi.x hi.y, f32 z at NW (lo.x,lo.y) NE (hi.x,lo.y) SE (hi.x,hi.y)
#     SW (lo.x,hi.y), i32 flags (1 crouch 2 jump 4 precise 8 no-jump), i32 place (0 none, else NAVP index + 1),
#     i32 firstLink, i32 linkCount, i32 firstSpot, i32 spotCount
#   "NAVL" count L: i32 (target area index | direction << 24), direction 0 N 1 E 2 S 3 W
#   "NAVS" count S: f32 x y z, i32 flags (1 cover 2 good sniper 4 ideal sniper)
#   "NAVP" count = places: NUL-separated ASCII names (place n = name n-1), padded to 4 bytes
#   "NAVN" count = A·A: uint16 next hop, entry [target·A + from] = the neighbour of `from` on the cheapest path to
#     `target` (0xFFFF = unreachable, target itself = target); padded to 4 bytes. Cost = 3D centre distance, ×1.5 into
#     crouch areas, +50 u into jump areas (precomputed so Udon never runs a path search)
#   optional AI node graph (tools/nodegraph.py; older loaders skip it, a loader without it falls back to the nav mesh):
#   "NODG" count = N nodes: the HL CGraph of the map's info_node / info_node_air entities — the game's own
#     maps/graphs/<map>.nod when it matches the map (FLoadGraph), else the offline CTestHull build. i32 version 16,
#     i32 N, i32 L links, i32 T route tables, i32 slot[8] (hull × 2 + capability variant → table), N × 48 bytes
#     (f32 origin xyz, f32 peek xyz, i32 info (1 land 2 air 4 water), i32 firstLink, i32 linkCount, i32 hintType,
#     i32 hintActivity, f32 hintYaw), L × 20 bytes (i32 src, i32 dst, i32 hull bits (1 small 2 human 4 large 8 fly),
#     i32 link entity index (−1 none), f32 weight = 2-D length), T × N·N uint16 next node [from·N + to] (from = no
#     path); padded to 4. Layout spec: tools/nodegraph.py:section
#   optional surface materials (older loaders skip it):
#   "TMAT" count = I/3: uint8 per PACK triangle t (= idx[3t..3t+2], i.e. the index buffer above: world render
#     triangles (solid first), then every model's own index range in MODL order), padded to 4 bytes. Value = the CS
#     material letter (ASCII 'C' concrete, 'M' metal, 'D' dirt, 'V' vent, 'G' grate, 'T' tile, 'S' slosh, 'W' wood,
#     'P' computer, 'Y' glass, 'N' snow, 'X' grass, ...) of the face's texture in <mod>/sound/materials.txt (fallback
#     valve/sound/materials.txt), game rule (cs.so UTIL_TextureHit + TEXTURETYPE_Find, client.so
#     PM_CatagorizeTextureType): strip a leading '-'/'+' plus the next char, then one of '{' '~' '!' ' '; compare the
#     first 16 chars case-insensitively (CBTEXTURENAMEMAX 17 in this build, NOT the 12 the file's comment claims);
#     first matching line wins; not found -> 'C'. Per pack triangle (not per collider triangle) because the loader
#     decides at runtime which brush models are merged into the world collider (pool capacity); it composes
#     GsvrPackLoader.triMaterial (one letter per map-collider triangle) from this table while merging.
#   optional clip-only floors (older loaders skip it; they then fall through invisible walkable clip brushes, e.g. de_aztec's
#   suspension bridge): "CLIP" count = T collision-only triangles: float32 pos [3 V] (Unity space, metres) then int32 idx [3 T]
#   (indices into those V vertices). Walkable (normal.y >= 0.7) surfaces of the world's hull 1 that no hull-0 brush backs,
#   recovered by tools/pack.py clip_floors(); the loader appends them to the map collider (never rendered).
#   optional render modes (older loaders skip it; they then draw every brush model opaque):
#   "REND" count = R (one per MODL record, same order): int32 × 2 { mode | renderamt << 8 | renderfx << 16,
#     rendercolor r | g << 8 | b << 16 } from the entity keys rendermode (0 normal, 1 color, 2 texture, 3 glow,
#     4 solid = alpha test, 5 additive), renderamt (0..255), renderfx, rendercolor; a missing key is 0 like in the
#     engine (so mode 2 without renderamt is invisible). Record 0 (world) = 0, 0. docs/goldsrc_base_layer.md §1.
#   optional entity sounds (tools/sounds.py; older loaders skip it): "SNDS" count = clips, the wavs the map's
#     ambient_generic / env_message / movers / chargers can play, mono float32 with cue loop points
#   optional env_message texts (tools/titles.py): "TITL" count = messages, the titles.txt entries the map names
#   optional speech sentences (tools/sentences.py; valve / svencoop only): "SENT" count = sentences, the
#     sentences.txt lines the map's scripted_sentence / "!NAME" ambients / talk monsters can say as word records
#     (clip = index into SNDS, whose clip list then also holds the word wavs) + the sentence groups
#
# Design choice (task item 3, option a): ONE mesh / ONE material / ONE draw call. GoldSrc textures tile, so UVs stay
# in texture space and every vertex carries its atlas rect; the shader does frac() tiling inside the rect and
# samples with tex2Dgrad(…, ddx/ddy of the *untiled* UV) so mip selection has no seams at tile borders. The mip
# chain is built here per texture (wrap-aware), so no Unity-side mip generation is needed (SetPixelData is generic
# and not usable from UdonSharp; LoadRawTextureData with a full mip chain is).
import argparse, math, os, re, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

INCH = 0.0254          # GoldSrc unit ≈ 1 inch (player 72 units ≈ 1.83 m)
GUTTER = 8             # px around each atlas texture at mip 0 (→ 1 px at mip 3)
MIPS = 4
NEUTRAL = 128          # lightmap value meaning "1.0" after the shader's ×2 overbright
HEADER = 64

# brush entities that are never drawn in GoldSrc (or are pure volumes)
INVISIBLE_CLASSES = ("trigger_", "func_ladder", "func_buyzone", "func_bomb_target", "func_hostage_rescue",
                     "func_vip_safetyzone", "func_escapezone", "func_tankcontrols", "func_traincontrols",
                     "func_vehiclecontrols",
                     "func_monsterclip", "func_friction", "func_mortar_field", "game_zone_player", "env_bubbles",
                     "func_weaponcheck", "info_", "env_snow", "env_rain", "player_respawn_zone")
NONSOLID_CLASSES = ("func_illusionary", "func_water")
# classes the player addresses with +use: an all-aaatrigger model is their only hit box (collision-only record)
USE_HITBOX_CLASSES = ("func_button", "func_rot_button", "momentary_rot_button")
# corner n = (x0|x1, y0|y1, z0|z1) as bits 4 / 2 / 1; a box is 12 triangles, emitted in both windings (the use ray hits
# whichever side faces it, and a Unity MeshCollider ignores back faces)
BOX_TRIS = ((0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4),
            (1, 5, 7), (1, 7, 3))


# ───────────────────────── texture helpers (RGBA bytes, row-major, w*h*4) ─────────────────────────

def checker_rgba(w=64, h=64, cell=8, a=(230, 160, 40, 255), b=(60, 70, 90, 255)):
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes(a if ((x // cell) + (y // cell)) % 2 else b)
    return bytes(px)


def half(rgba, w, h, masked):
    """2x2 box filter. For cut-out textures colour is averaged over opaque texels only (no dark fringes)."""
    nw, nh = max(1, w // 2), max(1, h // 2)
    out = bytearray(nw * nh * 4)
    row = w * 4
    o = 0
    for y in range(nh):
        r0 = (2 * y) * row
        r1 = r0 + row
        for x in range(nw):
            i0 = r0 + 8 * x; i1 = r1 + 8 * x
            a0, a1, a2, a3 = rgba[i0 + 3], rgba[i0 + 7], rgba[i1 + 3], rgba[i1 + 7]
            if masked:
                s = a0 + a1 + a2 + a3
                if s == 0:
                    out[o:o + 4] = b"\0\0\0\0"
                else:
                    for c in range(3):
                        out[o + c] = (rgba[i0 + c] * a0 + rgba[i0 + 4 + c] * a1 + rgba[i1 + c] * a2
                                      + rgba[i1 + 4 + c] * a3) // s
                    out[o + 3] = s // 4
            else:
                for c in range(4):
                    out[o + c] = (rgba[i0 + c] + rgba[i0 + 4 + c] + rgba[i1 + c] + rgba[i1 + 4 + c] + 2) >> 2
            o += 4
    return bytes(out), nw, nh


def resize_nearest(rgba, w, h, nw, nh):
    out = bytearray()
    for y in range(nh):
        sy = y * h // nh
        for x in range(nw):
            sx = x * w // nw
            i = (sy * w + sx) * 4
            out += rgba[i:i + 4]
    return bytes(out)


def shelf_pack(sizes, width):
    """sizes: list of (w, h) → (positions list, used height). Tallest first, left-to-right shelves."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    pos = [None] * len(sizes)
    x = y = shelf_h = 0
    for i in order:
        w, h = sizes[i]
        if w > width:
            return None, 1 << 30
        if x + w > width:
            y += shelf_h; x = 0; shelf_h = 0
        pos[i] = (x, y)
        x += w
        shelf_h = max(shelf_h, h)
    return pos, y + shelf_h


def pick_atlas(sizes, max_side, align):
    best = None
    w = 256
    while w <= max_side:
        pos, used = shelf_pack(sizes, w)
        if pos is not None and used <= max_side:
            hgt = -(-used // align) * align
            cand = (max(w, hgt), w * hgt, w, hgt, pos)       # prefer square-ish, then smaller area
            if best is None or cand[:2] < best[:2]:
                best = cand
        w *= 2
    return None if best is None else best[2:]


# ───────────────────────── map conversion ─────────────────────────

def load_textures(bsp, used, game_root, mod, log):
    from wad3 import Wad, decode_miptex, find_wads
    ws = bsp.entity_list()[0] if bsp.entity_list() else {}
    found, missing, fallback = find_wads(ws.get("wad", ""), game_root, mod) if game_root else ([], [], [])
    log(f"wad key → found {[os.path.basename(p) for p in found]}, missing {missing}, fallback {len(fallback)} WADs")
    wads, fb_wads = [], None
    for p in found:
        try:
            wads.append(Wad(p))
        except Exception as e:  # noqa: BLE001
            log(f"  cannot read {p}: {e}")
    out, src = {}, {"bsp": 0, "wad": 0, "fallback": 0, "missing": 0}
    for mi in sorted(used):
        name, w, h, embedded = bsp.textures[mi]
        tex = None
        if embedded:
            tex = decode_miptex(bsp.data, bsp.miptex_offset(mi)); kind = "bsp"
        if tex is None:
            kind = "wad"
            for wd in wads:
                tex = wd.get(name)
                if tex:
                    break
        if tex is None:
            kind = "fallback"
            if fb_wads is None:
                fb_wads = []
                for p in fallback:
                    try:
                        fb_wads.append(Wad(p))
                    except Exception:  # noqa: BLE001
                        pass
            for wd in fb_wads:
                tex = wd.get(name)
                if tex:
                    log(f"  {name}: found in fallback {os.path.basename(wd.path)}")
                    break
        if tex is None:
            kind = "missing"
            log(f"  {name}: MISSING → checker")
            tex = (name, 64, 64, checker_rgba(64, 64, 8, (255, 0, 255, 255), (20, 20, 20, 255)))
        src[kind] += 1
        out[mi] = tex
    return out, src


def build_atlas(texs, max_tex, log):
    """texs: {key: (name, w, h, rgba)} → (AW, AH, mip-chain bytes, {key: rect(x,y,w,h) normalised})."""
    max_side = 4096
    cap = max_tex
    while True:
        items = {}
        for k, (name, w, h, rgba) in texs.items():
            masked = name.startswith("{")
            if w % 16 or h % 16:                      # keep mip 3 + gutter alignment exact
                nw, nh = max(16, -(-w // 16) * 16), max(16, -(-h // 16) * 16)
                rgba, w, h = resize_nearest(rgba, w, h, nw, nh), nw, nh
            while max(w, h) > cap and min(w, h) >= 32:
                rgba, w, h = half(rgba, w, h, masked)
            items[k] = (w, h, rgba, masked)
        keys = list(items)
        sizes = [(items[k][0] + 2 * GUTTER, items[k][1] + 2 * GUTTER) for k in keys]
        res = pick_atlas(sizes, max_side, 8)
        if res is not None:
            break
        cap //= 2
        log(f"atlas does not fit {max_side}², capping textures at {cap}px")
    AW, AH, pos = res
    levels = [bytearray((AW >> l) * (AH >> l) * 4) for l in range(MIPS)]
    rects = {}
    for k, (x, y) in zip(keys, pos):
        w, h, rgba, masked = items[k]
        rects[k] = ((x + GUTTER) / AW, (y + GUTTER) / AH, w / AW, h / AH)
        mw, mh, m = w, h, rgba
        for l in range(MIPS):
            g = GUTTER >> l
            lw = AW >> l
            row = mw * 4
            for r in range(-g, mh + g):
                sr = (r % mh) * row
                src = m[sr:sr + row]
                line = src[(mw - g) * 4:] + src + src[:g * 4]          # wrapped gutter left/right
                dst = (((y >> l) + g + r) * lw + (x >> l)) * 4
                levels[l][dst:dst + len(line)] = line
            if l + 1 < MIPS:
                m, mw, mh = half(m, mw, mh, masked)
    log(f"diffuse atlas {AW}x{AH}, {len(keys)} textures, {MIPS} mips, max edge {cap}")
    return AW, AH, b"".join(bytes(b) for b in levels), rects


LM_PAGE = 2048          # lightmap page edge (texels); the atlas holds one page or two stacked pages (flags bit 1)
LM_PAGES = 2
FLAG_LIGHT, FLAG_LM_PAGES2 = 1, 2


def shelf_pack_pages(sizes, width, page_h, pages):
    """Like shelf_pack, but no block crosses a page boundary (row multiple of page_h): a shelf that does not fit
    the rest of a page starts the next one. → (positions, used height) or (None, 1 << 30)."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    pos = [None] * len(sizes)
    x = y = shelf_h = 0
    page = 0
    for i in order:
        w, h = sizes[i]
        if w > width or h > page_h:
            return None, 1 << 30
        if x + w > width:
            y += shelf_h; x = 0; shelf_h = 0
        if y + h > (page + 1) * page_h:                # shelf would cross into the next page
            page += 1
            if page >= pages:
                return None, 1 << 30
            y = page * page_h; x = 0; shelf_h = 0
        pos[i] = (x, y)
        x += w
        shelf_h = max(shelf_h, h)
    return pos, y + shelf_h


def halve_block(w, h, rgb):
    """2×2 box filter of a w×h RGB luxel block → (ceil(w/2), ceil(h/2), rgb); odd edges average what exists."""
    nw, nh = (w + 1) // 2, (h + 1) // 2
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        for x in range(nw):
            acc = [0, 0, 0]
            n = 0
            for yy in (2 * y, 2 * y + 1):
                if yy >= h:
                    continue
                for xx in (2 * x, 2 * x + 1):
                    if xx >= w:
                        continue
                    o = (yy * w + xx) * 3
                    acc[0] += rgb[o]; acc[1] += rgb[o + 1]; acc[2] += rgb[o + 2]
                    n += 1
            d = (y * nw + x) * 3
            out[d:d + 3] = bytes(((acc[0] + n // 2) // n, (acc[1] + n // 2) // n, (acc[2] + n // 2) // n))
    return nw, nh, bytes(out)


def lightmap_layout(blocks, log):
    """→ (LW, LH, positions, step per block, pages). Tries, in order: one page (square-ish, the old layout), two
    stacked pages of LM_PAGE² at full resolution, then two pages with the biggest blocks at half resolution (step 2,
    as many as needed, largest first). The last entry of blocks is the neutral block (never halved)."""
    sizes = [(w + 2, h + 2) for (w, h, _rgb) in blocks]
    res = pick_atlas(sizes, LM_PAGE, 4)
    if res is not None:
        LW, LH, pos = res
        return LW, LH, pos, [1] * len(blocks), 1
    steps = [1] * len(blocks)

    def attempt(n_half, order):
        st = [1] * len(blocks)
        for i in order[:n_half]:
            st[i] = 2
        sz = [((w + 1) // 2 + 2, (h + 1) // 2 + 2) if st[i] == 2 else (w + 2, h + 2)
              for i, (w, h, _rgb) in enumerate(blocks)]
        pos, used = shelf_pack_pages(sz, LM_PAGE, LM_PAGE, LM_PAGES)
        return (pos, used, st) if pos is not None else None

    order = sorted((i for i in range(len(blocks) - 1) if blocks[i][2] is not None and (blocks[i][0] > 1 or blocks[i][1] > 1)),
                   key=lambda i: -(blocks[i][0] * blocks[i][1]))
    got = attempt(0, order)
    if got is None:
        lo, hi = 1, len(order)                         # smallest n_half that fits (more halving always packs smaller)
        if attempt(hi, order) is None:
            raise SystemExit(f"lightmap needs more than {LM_PAGES} pages of {LM_PAGE}² even at half resolution")
        while lo < hi:
            mid = (lo + hi) // 2
            if attempt(mid, order) is None:
                lo = mid + 1
            else:
                hi = mid
        got = attempt(lo, order)
        log(f"lightmap: {lo} of {len(order)} face blocks at half resolution (largest first) to fit {LM_PAGES} pages")
    pos, used, steps = got
    LH = -(-used // 4) * 4
    return LM_PAGE, max(LH, LM_PAGE + 4), pos, steps, 2


def build_lightmap(blocks, log):
    """blocks: list of (w, h, rgb|None) → (LW, LH, rgba bytes, list of (x, y) of luxel 0 per block, neutral (x, y),
    luxel step per block (1 full, 2 half resolution), pages). Pages = 2: the atlas is LM_PAGE wide and holds page 0
    in rows [0, LM_PAGE) and page 1 below it (header flags bit 1); no block crosses the page boundary."""
    blocks = list(blocks) + [(2, 2, None)]                                  # last = neutral block (4×4 with border)
    LW, LH, pos, steps, pages = lightmap_layout(blocks, log)
    img = bytearray(bytes((NEUTRAL, NEUTRAL, NEUTRAL, 255)) * (LW * LH))
    origins = []
    for (w, h, rgb), (x, y), st in zip(blocks, pos, steps):
        origins.append((x + 1, y + 1))
        if rgb is None:
            continue
        if st == 2:
            w, h, rgb = halve_block(w, h, rgb)
        rows = []
        for r in range(h):
            src = rgb[r * w * 3:(r + 1) * w * 3]
            line = bytearray()
            for c in range(w):
                line += src[c * 3:c * 3 + 3] + b"\xff"
            line = line[:4] + line + line[-4:]                          # clamp-extend padding
            rows.append(line)
        rows = [rows[0]] + rows + [rows[-1]]
        for r, line in enumerate(rows):
            d = ((y + r) * LW + x) * 4
            img[d:d + len(line)] = line
    nx, ny = pos[-1]
    origins.pop()
    real = blocks[:-1]
    eff = [((w + st - 1) // st, (h + st - 1) // st) for (w, h, _), st in zip(real, steps)]     # texels as stored
    log(f"lightmap atlas {LW}x{LH} ({pages} page{'s' if pages > 1 else ''}), {len(real)} face blocks, "
        f"{sum(w * h for w, h, _ in real)} luxels ({100.0 * sum((w + 2) * (h + 2) for w, h in eff) / (LW * LH):.0f}% fill"
        f"{', ' + str(sum(1 for s_ in steps if s_ == 2)) + ' halved' if pages > 1 else ''})")
    return LW, LH, bytes(img), origins, (nx + 2, ny + 2), steps[:-1], pages


def lightmap_uv(ox, oy, step, ss, tt, ms, mt, LW, LH):
    """Lightmap atlas UV of texture-space point (ss, tt) on a face whose luxel 0 sits at texel (ox, oy). Luxel i of
    the face covers texture space [16 i − 8, 16 i + 8) around ms·16 (centre i); a step-2 (halved) block's texel j
    averages luxels 2j and 2j + 1, so its centre is luxel 2j + 0.5 → texel = (luxel + 0.5) / step."""
    if step == 1:                                      # the original expression (packs stay byte-identical)
        return (ox + ss / 16.0 - ms + 0.5) / LW, (oy + tt / 16.0 - mt + 0.5) / LH
    return (ox + (ss / 16.0 - ms + 0.5) / step) / LW, (oy + (tt / 16.0 - mt + 0.5) / step) / LH


def from_bsp(path, game_root, mod, max_tex, brushes, log):
    from bsp30 import Bsp
    b = Bsp(path)
    ents = b.entity_list()
    # (model index, origin offset, solid?)
    models = [(0, (0.0, 0.0, 0.0), True)]
    if brushes:
        for e in ents:
            m = e.get("model", "")
            cls = e.get("classname", "")
            if not m.startswith("*") or cls.startswith(INVISIBLE_CLASSES):
                continue
            rmode, ramt, _fx, _col = render_keys(e)
            if rmode in BLENDED_MODES and ramt <= 0:
                continue
            o = tuple(float(v) for v in (e.get("origin", "0 0 0").split() + ["0", "0", "0"])[:3])
            models.append((int(m[1:]), o, not cls.startswith(NONSOLID_CLASSES)))
    faces = []                                    # (face, origin, solid)
    for mi, o, solid in models:
        if mi >= len(b.models):
            continue
        mdl = b.models[mi]
        for f in b.faces[mdl[14]:mdl[14] + mdl[15]]:
            name = b.textures[b.texinfo[f[4]][2]][0]
            from wad3 import is_skipped
            if is_skipped(name):
                continue
            faces.append((f, o, solid))
    used = {b.texinfo[f[4]][2] for f, _, _ in faces}
    texs, src = load_textures(b, used, game_root, mod, log)
    AW, AH, atlas, rects = build_atlas(texs, max_tex, log)

    blocks, lm = [], []
    has_light = len(b.lighting) > 0
    for f, _, _ in faces:
        ms, mt, w, h, rgb = b.face_lightmap(f)
        lm.append((ms, mt, rgb is not None))
        blocks.append((w, h, rgb) if rgb is not None else (1, 1, None))
    LW, LH, light, origins, neutral, steps, pages = build_lightmap(blocks, log)

    pos, uv0, uv1, rect, solid_idx, other_idx = [], [], [], [], [], []
    for (f, o, solid), (ms, mt, lit), (ox, oy), stp in zip(faces, lm, origins, steps):
        s, t, miptex, _ = b.texinfo[f[4]]
        _n, tw, th, _e = b.textures[miptex]
        r = rects[miptex]
        base = len(pos) // 3
        poly = b.face_polygon(f)
        for x, y, z in poly:
            ss = x * s[0] + y * s[1] + z * s[2] + s[3]
            tt = x * t[0] + y * t[1] + z * t[2] + t[3]
            wx, wy, wz = x + o[0], y + o[1], z + o[2]
            pos += [wx * INCH, wz * INCH, wy * INCH]    # GoldSrc Z-up → Unity Y-up (x, z, y; verified in client)
            uv0 += [ss / tw, tt / th]
            if lit:
                uv1 += lightmap_uv(ox, oy, stp, ss, tt, ms, mt, LW, LH)
            else:
                uv1 += [(neutral[0] + 0.5) / LW, (neutral[1] + 0.5) / LH]
            rect += r
        dst = solid_idx if solid else other_idx
        for i in range(1, len(poly) - 1):
            # GoldSrc polygons are clockwise seen from the front in its right-handed Z-up space; the (x,z,y) swap
            # mirrors that into counter-clockwise → but Unity's cross(b-a,c-a) of (0,i,i+1) then points along the
            # plane normal, i.e. it IS Unity front-facing (clockwise on screen). Checked against the BSP plane
            # normals (1704/1705 faces) and in pack_preview.py; the GSV1 (0,i+1,i) order rendered back faces.
            dst += [base, base + i, base + i + 1]
    stats = dict(faces=len(faces), models=len(models), tex_src=src, has_light=has_light)
    return dict(pos=pos, uv0=uv0, uv1=uv1, rect=rect, idx=solid_idx + other_idx, solid=len(solid_idx),
                atlas=(AW, AH, atlas), light=(LW, LH, light), ents=b.entities.encode("latin-1"),
                ntex=len(texs), flags=(FLAG_LIGHT if has_light else 0) | (FLAG_LM_PAGES2 if pages > 1 else 0), stats=stats)


# ───────────────────────── surface materials (sound/materials.txt) ─────────────────────────
# Evidence: cs.so TEXTURETYPE_Init 0xdaaa0 / TEXTURETYPE_Find 0xdad30 (objdump: linear search, strncasecmp(.., 16),
# default 'C', at most 1024 entries), UTIL_TextureHit 0xfdd00 (prefix strip, name cut at 16); client.so
# PM_InitTextureTypes 0x10ff40 (same parser + case-insensitive exchange sort) / PM_FindTextureType 0x110300 (binary
# search) / PM_CatagorizeTextureType 0x1110a0 (same prefix strip). Parser: skip leading whitespace; a line whose first
# non-blank char is '/' or not a letter is ignored; letter = toupper(that char); skip blanks; the name runs to the
# next whitespace (a name that reaches the end of the buffer without whitespace after it is dropped), cut at 16.
MAT_NAME = 16
MAT_MAX = 1024


def load_materials(paths):
    """First existing materials.txt -> (list of (LETTER, name <= 16 chars) in file order, path used)."""
    for p in paths:
        if p and os.path.isfile(p):
            data = open(p, "rb").read().decode("latin-1")
            out = []
            for ln in data.splitlines(keepends=True):
                if len(out) >= MAT_MAX:
                    break
                ln = ln[:0x1ff]
                i = 0
                while i < len(ln) and ln[i].isspace():
                    i += 1
                if i >= len(ln) or ln[i] == "/" or not ln[i].isalpha():
                    continue
                letter = ln[i].upper()
                j = i + 1
                while j < len(ln) and ln[j].isspace():
                    j += 1
                k = j
                while k < len(ln) and not ln[k].isspace():
                    k += 1
                if j >= len(ln) or k >= len(ln):           # no name / no whitespace after it -> line skipped
                    continue
                out.append((letter, ln[j:k][:MAT_NAME]))
            return out, p
    return [], None


def strip_texture_prefix(name):
    """UTIL_TextureHit / PM_CatagorizeTextureType: '-0'..'+9' animation/tiling prefix (2 chars), then one of
    '{' '~' '!' ' ' (1 char); the result is cut at 16 chars."""
    if name[:1] in ("-", "+"):
        name = name[2:]
    if name[:1] in ("{", "~", "!", " "):
        name = name[1:]
    return name[:MAT_NAME]


def material_of(name, mats, cache):
    """Server rule (TEXTURETYPE_Find): first entry equal in the first 16 chars, case-insensitive; default 'C'."""
    key = strip_texture_prefix(name).lower()
    if key not in cache:
        cache[key] = next((c for c, n in mats if n.lower() == key), "C")
    return cache[key]


def client_sorted(mats):
    """PM_InitTextureTypes' exchange sort (i < j, swap when strcasecmp(i, j) > 0), reproduced instead of sorted() so
    duplicates end up in the game's order."""
    t = list(mats)
    for i in range(len(t)):
        for j in range(i + 1, len(t)):
            if t[i][1].lower() > t[j][1].lower():
                t[i], t[j] = t[j], t[i]
    return t


def material_of_client(key, srt):
    """Client pm_shared rule (footsteps): binary search in the sorted table; differs from the server rule only for a
    texture listed twice with different letters."""
    lo, hi = 0, len(srt) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        n = srt[mid][1].lower()
        if key == n:
            return srt[mid][0]
        if key > n:
            lo = mid + 1
        else:
            hi = mid - 1
    return "C"


def materials_paths(bsp_path, game_root, mod):
    from gamedirs import content_dirs, root_of_bsp
    roots = [game_root] if game_root else []
    roots.append(root_of_bsp(bsp_path))                    # <root>/<mod or mod_addon …>/maps/x.bsp
    out = []
    for r in roots:
        out += [os.path.join(d, "sound", "materials.txt") for d in content_dirs(r, mod)]
    return out


# ───────────────────────── Sven Co-op map config (maps/<map>.cfg) → worldspawn keys ─────────────────────────
# Sven Co-op reads <mod>/maps/<map>.cfg at map start (on top of <mod>/default_map_settings.cfg): cvars ("name value"),
# start items ("weapon_x", "ammo_x n"), "nosuit" / "nomedkit" and `map_script <file>` (scripts/maps/<file>.as). The
# runtime has no file access, so pack.py writes what it needs as keys of the worldspawn entity (entity 0), all
# prefixed "gsvr_" (no GoldSrc key starts with it); docs/sven_world.md §7. Survival mode is supported when the cfg
# says mp_survival_supported 1 or a map script calls g_SurvivalMode.EnableMapSupport() (survival_generic, snd,
# polar_rescue) [ext: the game's cfg / .as files]. Maps without a cfg (Half-Life, Counter-Strike) are unchanged.
CFG_CVARS = ("mp_survival_supported", "mp_survival_starton", "mp_survival_startdelay", "mp_survival_nextmap",
             "mp_hevsuit_voice", "nextmap", "starthealth", "startarmor", "maxhealth", "maxarmor", "mp_respawndelay",
             "mp_falldamage", "mp_forcerespawn", "sv_gravity", "mp_npckill")
CFG_KEY = {"mp_survival_supported": "gsvr_survival", "mp_survival_starton": "gsvr_survival_starton",
           "mp_survival_startdelay": "gsvr_survival_startdelay", "mp_survival_nextmap": "gsvr_survival_nextmap",
           "mp_hevsuit_voice": "gsvr_hevsuit_voice", "nextmap": "gsvr_nextmap"}
ITEM_PREFIXES = ("weapon_", "ammo_", "item_")


def parse_map_cfg(text):
    """cfg text → (cvars {name: value}, items [(classname, count)], map_scripts [file], flags {nosuit, nomedkit}).
    '#' / '//' lines are comments; values may be quoted; the last value of a cvar wins (cvars are set in order)."""
    cvars, items, scripts, flags = {}, [], [], set()
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        word = parts[0]
        name = word.lower()
        val = parts[1].strip().strip('"') if len(parts) > 1 else ""
        if name == "map_script":
            if val:
                scripts.append(val)
        elif name in ("nosuit", "nomedkit"):
            flags.add(name)
        elif name.startswith(ITEM_PREFIXES):
            try:
                n = int(float(val)) if val else 1
            except ValueError:
                n = 1
            items.append((word, n))                  # classname as written (weapon_9mmAR)
        else:
            cvars[name] = val
    return cvars, items, scripts, flags


def map_cfg_files(bsp_path, game_root, mod):
    """→ (default_map_settings.cfg or None, maps/<map>.cfg or None): the first one in the content-folder order, then
    next to the BSP."""
    from gamedirs import content_dirs, root_of_bsp
    name = os.path.splitext(os.path.basename(bsp_path))[0]
    dirs = content_dirs(game_root, mod) if game_root else []
    dirs += [d for d in content_dirs(root_of_bsp(bsp_path), mod) if d not in dirs]
    dflt = next((os.path.join(d, "default_map_settings.cfg") for d in dirs
                 if os.path.isfile(os.path.join(d, "default_map_settings.cfg"))), None)
    own = [os.path.join(d, "maps", name + ".cfg") for d in dirs] + [os.path.splitext(bsp_path)[0] + ".cfg"]
    return dflt, next((p for p in own if os.path.isfile(p)), None)


def script_text(script, cfg_path):
    """scripts/maps/<script>.as next to the cfg's content folder ("" when missing); comments stripped."""
    if not cfg_path:
        return ""
    root = os.path.dirname(os.path.dirname(cfg_path))
    p = os.path.join(root, "scripts", "maps", script.replace("\\", "/") + ".as")
    if not os.path.isfile(p):
        return ""
    t = open(p, encoding="latin-1").read()
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    return re.sub(r"//[^\n]*", "", t)


SCRIPT_CVAR_RE = re.compile(r'CVarSetFloat\s*\(\s*"([A-Za-z0-9_]+)"\s*,\s*(-?[0-9.]+)\s*\)')


def map_cfg_keys(bsp_path, game_root, mod):
    """→ [(key, value)] for worldspawn, [] when the map has no cfg."""
    dflt, own = map_cfg_files(bsp_path, game_root, mod)
    if not own:
        return []
    cv, _i, _s, _f = parse_map_cfg(open(dflt, encoding="latin-1").read()) if dflt else ({}, [], [], set())
    mc, items, scripts, flags = parse_map_cfg(open(own, encoding="latin-1").read())
    cv.update(mc)
    survival = cv.get("mp_survival_supported", "0") not in ("", "0")
    for s in scripts:
        t = script_text(s, own)
        if "g_SurvivalMode.EnableMapSupport" in t:
            survival = True
        for m in SCRIPT_CVAR_RE.finditer(t):           # MapInit cvars (HLSP: mp_hevsuit_voice 1)
            if m.group(1) in CFG_CVARS:
                cv[m.group(1)] = m.group(2)
    keys = [("gsvr_map_cfg", os.path.basename(own))]
    if scripts:
        keys.append(("gsvr_map_script", " ".join(scripts)))
    keys.append(("gsvr_survival", "1" if survival else "0"))
    for k in CFG_CVARS:
        if k == "mp_survival_supported" or k not in cv:
            continue
        keys.append((CFG_KEY.get(k, "gsvr_" + k), cv[k]))
    if items:
        keys.append(("gsvr_equip", " ".join(f"{c} {n}" for c, n in items)))
    for f in sorted(flags):
        keys.append(("gsvr_" + f, "1"))
    return [(k, v.replace('"', "")) for k, v in keys]


def with_map_cfg(text, bsp_path, game_root, mod, log):
    """Entity text with the map cfg keys appended to the worldspawn block (unchanged without a cfg)."""
    keys = map_cfg_keys(bsp_path, game_root, mod)
    end = text.find("}")
    if not keys or not text.lstrip().startswith("{") or end < 0 or '"worldspawn"' not in text[:end]:
        return text
    log("map cfg: " + ", ".join(f"{k}={v}" for k, v in keys))
    add = "".join(f'"{k}" "{v}"\n' for k, v in keys)
    return text[:end] + add + text[end:]


# ───────────────────────── GSV3: per-model sections + collision trees ─────────────────────────

MERGE_CLASSES = ("func_wall", "func_illusionary")      # static when nothing can address them (no targetname)
F_RENDER, F_COLLIDE, F_VOLUME, F_MERGE, F_NONSOLID, F_TRANSLUCENT = 1, 2, 4, 8, 16, 32
# GoldSrc render modes (const.h kRender*): 0 normal, 1 color, 2 texture, 3 glow, 4 solid (alpha test), 5 additive.
# Blended = drawn in the transparent pass (never merged into the opaque world mesh); 4 stays opaque (alpha test).
BLENDED_MODES = (1, 2, 3, 5)


def render_keys(e):
    """Entity → (rendermode, renderamt, renderfx, (r, g, b)), ints, as the engine sees them: a missing key is 0
    (edicts are zero-filled before the keyvalues are applied [inf: Quake ED_Alloc/ED_ClearEdict lineage, HLSDK
    CBaseEntity::KeyValue only parses keys that are present]) — so rendermode 2 without renderamt is invisible."""
    def num(k):
        try:
            return int(float(e.get(k, "0") or 0))
        except ValueError:
            return 0
    col = []
    for v in (e.get("rendercolor", "") or "").split()[:3]:
        try:
            col.append(max(0, min(255, int(float(v)))))
        except ValueError:
            col.append(0)
    col += [0] * (3 - len(col))
    return max(0, min(5, num("rendermode"))), max(0, min(255, num("renderamt"))), max(0, min(255, num("renderfx"))), tuple(col)


def rend_words(e):
    """REND record: (mode | amt << 8 | fx << 16, r | g << 8 | b << 16)."""
    if e is None:
        return (0, 0)
    m, a, fx, (r, g, b) = render_keys(e)
    return (m | a << 8 | fx << 16, r | g << 8 | b << 16)
KV_RE = re.compile(r'\{|\}|"([^"]*)"\s*"([^"]*)"')


def entity_table(text):
    """Entity text → (ents [(firstKv, kvCount)], kvs [(keyOff, keyLen, valOff, valLen)]) in file order.
    Same tokenisation as Bsp.entity_list(), so entity i here is entity i there."""
    ents, kvs, cur = [], [], None
    for m in KV_RE.finditer(text):
        tok = m.group(0)
        if tok == "{":
            cur = [len(kvs), 0]
        elif tok == "}":
            if cur is not None:
                ents.append(tuple(cur))
            cur = None
        elif cur is not None:
            kvs.append((m.start(1), len(m.group(1)), m.start(2), len(m.group(2))))
            cur[1] += 1
    return ents, kvs


def entity_dicts(text):
    """Entity text → list of dicts (first occurrence of a key wins, like the loader's EntValue)."""
    out, cur = [], None
    for m in KV_RE.finditer(text):
        tok = m.group(0)
        if tok == "{":
            cur = {}
        elif tok == "}":
            if cur is not None:
                out.append(cur)
            cur = None
        elif cur is not None:
            cur.setdefault(m.group(1), m.group(2))
    return out


def render_targets(ents):
    """Targetnames an env_render can change (its `target`): their brush models keep a render mesh even when they start
    invisible (docs/sven_world.md §7)."""
    return {e.get("target", "") for e in ents if e.get("classname") == "env_render" and e.get("target")}


def model_groups(e, rendered=()):
    """Brush entity → (render-only?, render+collide?, collide-only?, flags) by class/render keys (the runtime may
    still turn collision off, e.g. SF_DOOR_PASSABLE). rendered = render_targets(): a named model an env_render can
    make visible keeps its faces when it starts invisible (translucent, renderamt 0 → the runtime hides the renderer)."""
    cls = e.get("classname", "")
    if cls.startswith(INVISIBLE_CLASSES):
        return None, F_VOLUME
    rmode, ramt, _fx, _col = render_keys(e)
    nonsolid = cls.startswith(NONSOLID_CLASSES)
    flags = F_NONSOLID if nonsolid else 0
    if rmode in BLENDED_MODES and ramt <= 0 and e.get("targetname") and e.get("targetname") in rendered:
        return ("B" if nonsolid else "A"), flags | F_TRANSLUCENT
    if rmode in BLENDED_MODES and ramt <= 0:          # never drawn (CL_FxBlend 0)
        if cls in MERGE_CLASSES and not e.get("targetname"):
            flags |= F_MERGE
        return (None if nonsolid else "C"), flags
    if rmode in BLENDED_MODES:
        flags |= F_TRANSLUCENT                        # own pooled renderer with the translucent material, never merged
    elif cls in MERGE_CLASSES and not e.get("targetname"):
        flags |= F_MERGE
    return ("B" if nonsolid else "A"), flags


# ───────────────────────── clip-only floors (CLIP section) ─────────────────────────
# GoldSrc moves the player through the clip hulls (hull 1 = 32x32x72 box, expanded brushes), not through the visible
# faces. A "clip" / invisible brush therefore makes a walkable surface that has NO faces in the BSP: de_aztec's
# suspension bridge is drawn by nine func_illusionary models (trrm_wood, non-solid in the engine too) and walked on
# because of a clip slab in the world's hull 1. The runtime colliders are built from the faces, so the player fell
# through. We recover the up-facing surfaces of hull-1 solid that no hull-0 brush backs and emit them as
# collision-only triangles.
CLIP_CELL = 8.0            # raster cell (GoldSrc units) of the recovered floor
CLIP_COARSE = 32.0         # prefilter cell: skipped when hull 0 is solid under all nine sample points
CLIP_MIN_CELLS = 32         # smallest recovered patch (8 u cells; 2048 u^2): smaller bits are fringe slivers, not walkways
CLIP_MIN_WIDTH = 3          # and at least this many cells across (24 u): a 1-2 cell strip is a fringe along a wall, not a floor
CLIP_MIN_NZ = 0.7          # walkable slope limit (GoldSrc: 0.7)
HULL1_HALF = (16.0, 16.0, 36.0)


def _clip_poly(poly, n, d, keep_front):
    """Sutherland-Hodgman: keep the part of the convex polygon on the front (n.p - d >= 0) or back side."""
    out = []
    sgn = 1.0 if keep_front else -1.0
    m = len(poly)
    for i in range(m):
        a, b = poly[i], poly[(i + 1) % m]
        da = sgn * (n[0] * a[0] + n[1] * a[1] + n[2] * a[2] - d)
        db = sgn * (n[0] * b[0] + n[1] * b[1] + n[2] * b[2] - d)
        if da >= 0:
            out.append(a)
        if (da >= 0) != (db >= 0):
            t = da / (da - db)
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t))
    return out


def clip_floors(b, log=None):
    """Walkable hull-1 surfaces without hull-0 faces under them → (vertices GoldSrc units [(x, y, z)], triangles
    [(i, j, k)] indexing them, counter-clockwise seen from above in GoldSrc space)."""
    if not b.clipnodes or not b.models:
        return [], []
    head = b.models[0][10]
    if head < 0:
        return [], []
    planes, cn, nodes = b.planes, b.clipnodes, b.nodes
    leafc = b.leaf_contents
    SOLID = -2

    def h1(x, y, z):
        n = head
        while n >= 0:
            pl, a, c = cn[n]
            nx, ny, nz, d, _t = planes[pl]
            n = a if nx * x + ny * y + nz * z - d >= 0 else c
        return n

    def h0solid(x, y, z):
        n = 0
        while n >= 0:
            pl, a, c = nodes[n]
            nx, ny, nz, d, _t = planes[pl]
            n = a if nx * x + ny * y + nz * z - d >= 0 else c
        return leafc[-(n + 1)] in (SOLID, -6)         # solid or sky (the engine's hull 1 treats sky as solid too)

    m0 = b.models[0]
    lo = (m0[0] - 64, m0[1] - 64, m0[2] - 64)
    hi = (m0[3] + 64, m0[4] + 64, m0[5] + 64)
    cells = {}                                   # (i, j, layer z key) -> (z at the four corners) to avoid duplicates
    quads = []                                   # (x0, y0, x1, y1, plane index) in GoldSrc units, true (shifted) surface
    stack = [(head, ())]
    while stack:
        n, path = stack.pop()
        if n >= 0:
            pl, a, c = cn[n]
            stack.append((a, path + ((pl, 0),)))
            stack.append((c, path + ((pl, 1),)))
            continue
        if n != SOLID:
            continue
        for k, (pl, side) in enumerate(path):
            nx, ny, nz, d, _t = planes[pl]
            if side != 1 or nz < CLIP_MIN_NZ:
                continue                         # leaf on the back side of an up-facing plane = that plane is its top
            # polygon on the plane (x, y extent = model bounds), clipped by the rest of the path
            poly = [(lo[0], lo[1], 0.0), (hi[0], lo[1], 0.0), (hi[0], hi[1], 0.0), (lo[0], hi[1], 0.0)]
            poly = [(x, y, (d - nx * x - ny * y) / nz) for x, y, _z in poly]
            for j, (p2, s2) in enumerate(path):
                if j == k:
                    continue
                q = planes[p2]
                poly = _clip_poly(poly, q[0:3], q[3], s2 == 0)
                if len(poly) < 3:
                    break
            if len(poly) < 3:
                continue
            quads.append((poly, pl))
    sup = {}
    tris_v, tris_i = [], []
    seen = set()
    for poly, pl in quads:
        nx, ny, nz, d, _t = planes[pl]
        s = HULL1_HALF[0] * abs(nx) + HULL1_HALF[1] * abs(ny) + HULL1_HALF[2] * abs(nz)
        dt = d - s                               # the true brush surface: plane offset minus the box support
        xs = [v[0] for v in poly]
        ys = [v[1] for v in poly]
        i0, i1 = int(math.floor(min(xs) / CLIP_CELL)), int(math.ceil(max(xs) / CLIP_CELL))
        j0, j1 = int(math.floor(min(ys) / CLIP_CELL)), int(math.ceil(max(ys) / CLIP_CELL))

        def inside(x, y):
            sg = 0
            for q in range(len(poly)):
                ax, ay = poly[q][0], poly[q][1]
                bx, by = poly[(q + 1) % len(poly)][0], poly[(q + 1) % len(poly)][1]
                cr = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
                if cr > 1e-6:
                    if sg < 0:
                        return False
                    sg = 1
                elif cr < -1e-6:
                    if sg > 0:
                        return False
                    sg = -1
            return True

        def zt(x, y):                            # true surface height
            return (dt - nx * x - ny * y) / nz

        def z1(x, y):                            # expanded (hull-1) surface height
            return (d - nx * x - ny * y) / nz

        def cell_ok(x, y):
            if not inside(x, y):
                return False
            if h1(x, y, z1(x, y) + 1.0) == SOLID:
                return False                     # covered: interior face between two solid leaves
            if h0solid(x, y, zt(x, y) - 1.0) or h0solid(x, y, zt(x, y) + 0.0):
                return False                     # a visible brush backs it: the face collider already has this floor
            if h0solid(x, y, zt(x, y) + 2.0):
                return False                     # sealed in (above a map roof / sky): nobody stands there
            for ex in (-15.0, 15.0):
                for ey in (-15.0, 15.0):
                    if h1(x + ex, y + ey, z1(x + ex, y + ey) - 1.0) != SOLID:
                        return False             # fringe of the box expansion: the centre is not on the real brush
            return True

        ci0, ci1 = int(math.floor(i0 * CLIP_CELL / CLIP_COARSE)), int(math.ceil(i1 * CLIP_CELL / CLIP_COARSE))
        cj0, cj1 = int(math.floor(j0 * CLIP_CELL / CLIP_COARSE)), int(math.ceil(j1 * CLIP_CELL / CLIP_COARSE))
        per = int(CLIP_COARSE / CLIP_CELL)
        for ci in range(ci0, ci1):
            for cj in range(cj0, cj1):
                x0, y0 = ci * CLIP_COARSE, cj * CLIP_COARSE
                allsolid = True
                for fx in (0.0, 0.5, 1.0):
                    for fy in (0.0, 0.5, 1.0):
                        x, y = x0 + fx * CLIP_COARSE, y0 + fy * CLIP_COARSE
                        if not h0solid(x, y, zt(x, y) - 1.0):
                            allsolid = False
                            break
                    if not allsolid:
                        break
                if allsolid:
                    continue
                for di in range(per):
                    for dj in range(per):
                        i, j = ci * per + di, cj * per + dj
                        x, y = (i + 0.5) * CLIP_CELL, (j + 0.5) * CLIP_CELL
                        if not cell_ok(x, y):
                            continue
                        key = (i, j, round(zt(x, y) * 4))
                        if key in seen:
                            continue
                        seen.add(key)
                        sup.setdefault(pl, []).append((i, j))
    # merge cells of one plane into row runs, then identical consecutive runs into rectangles
    for pl, lst in sup.items():
        nx, ny, nz, d, _t = planes[pl]
        s = HULL1_HALF[0] * abs(nx) + HULL1_HALF[1] * abs(ny) + HULL1_HALF[2] * abs(nz)
        dt = d - s
        # drop specks: 4-connected components smaller than CLIP_MIN_CELLS cells (edge slivers of the fringe tests)
        cellset = set(lst)
        keep = []
        done = set()
        for c0 in lst:
            if c0 in done:
                continue
            comp, todo = [], [c0]
            done.add(c0)
            while todo:
                ci_, cj_ = todo.pop()
                comp.append((ci_, cj_))
                for nb in ((ci_ + 1, cj_), (ci_ - 1, cj_), (ci_, cj_ + 1), (ci_, cj_ - 1)):
                    if nb in cellset and nb not in done:
                        done.add(nb)
                        todo.append(nb)
            wi = max(c_[0] for c_ in comp) - min(c_[0] for c_ in comp) + 1
            wj = max(c_[1] for c_ in comp) - min(c_[1] for c_ in comp) + 1
            if len(comp) >= CLIP_MIN_CELLS and min(wi, wj) >= CLIP_MIN_WIDTH:
                keep += comp
        rows = {}
        for i, j in keep:
            rows.setdefault(j, []).append(i)
        out_rects = []
        open_rects = {}                          # (i0, i1) -> [j_start, j_last]
        for j in sorted(rows):
            xs_ = sorted(rows[j])
            cur = []
            st = pv = None
            for i in xs_:
                if st is None:
                    st = pv = i
                elif i == pv + 1:
                    pv = i
                else:
                    cur.append((st, pv + 1)); st = pv = i
            if st is not None:
                cur.append((st, pv + 1))
            nxt = {}
            for r in cur:
                if r in open_rects and open_rects[r][1] == j - 1:
                    nxt[r] = [open_rects[r][0], j]
                else:
                    nxt[r] = [j, j]
            for r, v in open_rects.items():
                if r not in nxt:
                    out_rects.append((r, v))
            open_rects = nxt
        out_rects += list(open_rects.items())
        for (ia, ib), (ja, jb) in out_rects:
            x0, x1 = ia * CLIP_CELL, ib * CLIP_CELL
            y0, y1 = ja * CLIP_CELL, (jb + 1) * CLIP_CELL
            base = len(tris_v)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                tris_v.append((x, y, (dt - nx * x - ny * y) / nz))
            tris_i += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    if log:
        log(f"clip floors: {len(tris_i)} collision-only triangles from {len(quads)} hull-1 top faces")
    return tris_v, tris_i


def from_bsp_v3(path, game_root, mod, max_tex, brushes, log):
    from bsp30 import Bsp
    from wad3 import is_skipped
    b = Bsp(path)
    b.entities = with_map_cfg(b.entities, path, game_root, mod, log)
    ents = b.entity_list()
    ent_tab, kv_tab = entity_table(b.entities)
    assert len(ent_tab) == len(ents), (len(ent_tab), len(ents))
    rendered = render_targets(ents)

    hitbox_models = set()      # use-class models made only of skipped textures (aaatrigger): kept as collision-only
    box_models = set()         # use-class models without any face (the compiler dropped a NULL-texture button, Sven's retinal
                               # scanner "denied" buttons): a collision box from the model bounds is their hit box

    def faces_of(mi):
        mdl = b.models[mi]
        allf = b.faces[mdl[14]:mdl[14] + mdl[15]]
        if mi in hitbox_models:
            return list(allf)
        return [f for f in allf if not is_skipped(b.textures[b.texinfo[f[4]][2]][0])]

    # records: (bsp model, entity index, group, flags, origin)
    recs = [(0, 0 if ents else -1, "W", F_RENDER | F_COLLIDE, (0.0, 0.0, 0.0))]
    if brushes:
        for ei, e in enumerate(ents):
            m = e.get("model", "")
            if not m.startswith("*"):
                continue
            try:
                mi = int(m[1:])
            except ValueError:
                continue
            if mi <= 0 or mi >= len(b.models):
                continue
            grp, flags = model_groups(e, rendered)
            if grp is not None and e.get("classname", "") in USE_HITBOX_CLASSES and not faces_of(mi) \
                    and b.models[mi][15] > 0 and not (e.get("spawnflags", "0").isdigit() and int(e.get("spawnflags", "0")) & 256):
                # c1a0a's elevator button: the visible button is part of the car model, the func_button itself is an
                # invisible "aaatrigger" box. Without triangles it had no collider, so the use ray never found it.
                hitbox_models.add(mi)
                grp, flags = "C", flags & ~F_MERGE
            elif grp is not None and e.get("classname", "") in USE_HITBOX_CLASSES and b.models[mi][15] == 0:
                # +use in GoldSrc finds a button by its bounding box (a view cone, no ray), so a button with no faces still
                # works there; the VR laser / desktop eye ray needs a collider
                box_models.add(mi)
                grp, flags = "C", flags & ~(F_MERGE | F_TRANSLUCENT)
            try:
                o = tuple(float(v) for v in (e.get("origin", "0 0 0").split() + ["0", "0", "0"])[:3])
            except ValueError:
                o = (0.0, 0.0, 0.0)
            recs.append((mi, ei, grp, flags, o))

    # face lists per record, in emission order: world A (solid) then B; models B, A, C
    rec_faces = []
    for mi, ei, grp, flags, o in recs:
        fl = faces_of(mi) if grp is not None else []
        if grp == "W":
            cls_of = {}
            rec_faces.append([("A", f) for f in fl])
        else:
            rec_faces.append([(grp, f) for f in fl])
    all_faces = [f for lst in rec_faces for _g, f in lst]
    used = {b.texinfo[f[4]][2] for f in all_faces}
    texs, src = load_textures(b, used, game_root, mod, log)
    AW, AH, atlas, rects = build_atlas(texs, max_tex, log)

    blocks, lm = [], []
    has_light = len(b.lighting) > 0
    for f in all_faces:
        ms, mt, w, h, rgb = b.face_lightmap(f)
        lm.append((ms, mt, rgb is not None))
        blocks.append((w, h, rgb) if rgb is not None else (1, 1, None))
    LW, LH, light, origins, neutral, steps, pages = build_lightmap(blocks, log)

    mats, mat_path = load_materials(materials_paths(path, game_root, mod))
    mat_cache = {}
    log(f"materials: {len(mats)} entries from {mat_path}" if mat_path else "materials: no sound/materials.txt -> no TMAT")
    pos, uv0, uv1, rect, idx, tmat = [], [], [], [], [], bytearray()
    face_no = 0
    out_recs = []
    for (mi, ei, grp, flags, o), lst in zip(recs, rec_faces):
        first_v, first_i = len(pos) // 3, len(idx)
        groups = {"A": [], "B": [], "C": []}
        gmat = {"A": bytearray(), "B": bytearray(), "C": bytearray()}
        for g, f in lst:
            ms, mt, lit = lm[face_no]
            ox, oy = origins[face_no]
            stp = steps[face_no]
            face_no += 1
            s_, t_, miptex, _ = b.texinfo[f[4]]
            _n, tw, th, _e = b.textures[miptex]
            r = rects[miptex]
            base = len(pos) // 3 - first_v
            poly = b.face_polygon(f)
            for x, y, z in poly:
                ss = x * s_[0] + y * s_[1] + z * s_[2] + s_[3]
                tt = x * t_[0] + y * t_[1] + z * t_[2] + t_[3]
                pos += [x * INCH, z * INCH, y * INCH]          # model-local (world: absolute)
                uv0 += [ss / tw, tt / th]
                if lit:
                    uv1 += lightmap_uv(ox, oy, stp, ss, tt, ms, mt, LW, LH)
                else:
                    uv1 += [(neutral[0] + 0.5) / LW, (neutral[1] + 0.5) / LH]
                rect += r
            letter = ord(material_of(b.textures[miptex][0], mats, mat_cache))
            for i in range(1, len(poly) - 1):
                groups[g] += [base, base + i, base + i + 1]
                gmat[g].append(letter)
        if mi in box_models and grp == "C":
            m_ = b.models[mi]
            xs, ys, zs = (m_[0], m_[3]), (m_[1], m_[4]), (m_[2], m_[5])
            base = len(pos) // 3 - first_v
            for n in range(8):
                x, y, z = xs[(n >> 2) & 1], ys[(n >> 1) & 1], zs[n & 1]
                pos += [x * INCH, z * INCH, y * INCH]
                uv0 += [0.0, 0.0]
                uv1 += [(neutral[0] + 0.5) / LW, (neutral[1] + 0.5) / LH]
                rect += [0.0, 0.0, 0.0, 0.0]
            for a_, b_, c_ in BOX_TRIS:
                groups["C"] += [base + a_, base + b_, base + c_, base + a_, base + c_, base + b_]
                gmat["C"] += bytes([ord("C"), ord("C")])
        if grp == "W":
            local = groups["A"] + groups["B"]
            tmat += gmat["A"] + gmat["B"]
            rs, rc, cs, cc = 0, len(local), 0, len(groups["A"])
        else:
            local = groups["B"] + groups["A"] + groups["C"]
            tmat += gmat["B"] + gmat["A"] + gmat["C"]
            nb, na, nc = len(groups["B"]), len(groups["A"]), len(groups["C"])
            rs, rc, cs, cc = 0, nb + na, nb, na + nc
        idx += local
        if rc:
            flags |= F_RENDER
        if cc:
            flags |= F_COLLIDE
        mdl = b.models[mi]
        out_recs.append(dict(model=mi, ent=ei, fv=first_v, vc=len(pos) // 3 - first_v, fi=first_i, rs=rs, rc=rc,
                             cs=cs, cc=cc, flags=flags, head=mdl[9:13], origin=o, mins=mdl[0:3], maxs=mdl[3:6]))

    # collision trees: hull 0 nodes (leaf → contents) then clipnodes (offset by H0)
    H0 = len(b.nodes)
    def c0(c):
        return c if c >= 0 else b.leaf_contents[-(c + 1)]
    def c1(c):
        return c + H0 if c >= 0 else c
    hnod = []
    for pl, a, bb in b.nodes:
        hnod += [pl, c0(a), c0(bb)]
    for pl, a, bb in b.clipnodes:
        hnod += [pl, c1(a), c1(bb)]
    for r in out_recs:
        h = r["head"]
        r["head"] = (c0(h[0]) if h[0] < 0 else h[0], c1(h[1]), c1(h[2]), c1(h[3]))
    plan = []
    for nx, ny, nz, d, _t in b.planes:
        plan += [nx, ny, nz, d]
    ents_out = []
    rec_of_ent = {r["ent"]: k for k, r in enumerate(out_recs) if k > 0}
    for ei, (fk, kc) in enumerate(ent_tab):
        ents_out += [fk, kc, 0 if ei == 0 else rec_of_ent.get(ei, -1), 0]
    ekvs = [v for kv in kv_tab for v in kv]

    assert len(tmat) * 3 == len(idx)
    if mats:
        srt = client_sorted(mats)
        diff = sorted(k for k in mat_cache if material_of_client(k, srt) != mat_cache[k])
        hist = {}
        for c in tmat:
            hist[chr(c)] = hist.get(chr(c), 0) + 1
        log(f"materials: {len(mat_cache)} distinct textures, triangles per letter "
            + " ".join(f"{k}={v}" for k, v in sorted(hist.items(), key=lambda kv: -kv[1]))
            + (f"; client pm_shared lookup differs for {diff}" if diff else ""))
    rend = [rend_words(ents[r["ent"]] if k > 0 and 0 <= r["ent"] < len(ents) else None) for k, r in enumerate(out_recs)]
    nt = sum(1 for r in out_recs if r["flags"] & F_TRANSLUCENT)
    if nt:
        modes = {}
        for k, r in enumerate(out_recs):
            if r["flags"] & F_TRANSLUCENT:
                key = f"{rend[k][0] & 255}/{(rend[k][0] >> 8) & 255}"
                modes[key] = modes.get(key, 0) + 1
        log(f"render modes: {nt} translucent brush models (mode/amt: "
            + " ".join(f"{k}x{v}" for k, v in sorted(modes.items())) + ")")
    world = out_recs[0]
    solid = world["cc"]
    stats = dict(faces=len(all_faces), records=len(out_recs), tex_src=src, has_light=has_light,
                 merge=sum(1 for r in out_recs if r["flags"] & F_MERGE), volumes=sum(1 for r in out_recs if r["flags"] & F_VOLUME),
                 planes=len(b.planes), hull0_nodes=H0, clipnodes=len(b.clipnodes), entities=len(ent_tab), kvs=len(kv_tab))
    cv, ct = clip_floors(b, log)
    clip = None
    if ct:
        # Unity space, metres; triangles wound so the up-facing side is the front face (a MeshCollider ignores back faces)
        cpos = []
        for x, y, z in cv:
            cpos += [x * INCH, z * INCH, y * INCH]
        cidx = []
        for t in ct:
            a, b_, c = (cpos[3 * i:3 * i + 3] for i in t)
            ux, uy, uz = b_[0] - a[0], b_[1] - a[1], b_[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            up = uz * vx - ux * vz                  # y of cross(b - a, c - a)
            cidx += list(t) if up > 0 else [t[0], t[2], t[1]]
        clip = (cpos, cidx)
        stats_clip = len(ct)
    else:
        stats_clip = 0
    stats["clip_tris"] = stats_clip
    return dict(clip=clip, pos=pos, uv0=uv0, uv1=uv1, rect=rect, idx=idx, solid=solid, world_v=world["vc"], world_i=world["rc"],
                atlas=(AW, AH, atlas), light=(LW, LH, light), ents=b.entities.encode("latin-1"),
                ntex=len(texs), flags=(FLAG_LIGHT if has_light else 0) | (FLAG_LM_PAGES2 if pages > 1 else 0), stats=stats,
                v3=dict(recs=out_recs, plan=plan, hnod=hnod, ents=ents_out, ekvs=ekvs), tmat=bytes(tmat) if mats else None,
                rend=rend)


def write_v3(out, p):
    vc, ic = len(p["pos"]) // 3, len(p["idx"])
    AW, AH, atlas = p["atlas"]
    LW, LH, light = p["light"]
    ents = p["ents"]
    v3 = p.get("v3") or dict(recs=[dict(model=0, ent=-1, fv=0, vc=vc, fi=0, rs=0, rc=ic, cs=0, cc=p["solid"],
                                        flags=F_RENDER | (F_COLLIDE if p["solid"] else 0), head=(-1, -1, -1, -1),
                                        origin=(0, 0, 0), mins=(0, 0, 0), maxs=(0, 0, 0))],
                             plan=[], hnod=[], ents=[], ekvs=[])
    world_v, world_i = p.get("world_v", vc), p.get("world_i", ic)
    assert len(atlas) == sum((AW >> l) * (AH >> l) * 4 for l in range(MIPS))
    assert len(light) == LW * LH * 4
    core = HEADER + (vc * 11 + ic) * 4 + len(atlas) + len(light) + len(ents) + (-len(ents) % 4)
    modl = bytearray()
    for r in v3["recs"]:
        modl += struct.pack("<14i9fi", r["model"], r["ent"], r["fv"], r["vc"], r["fi"], r["rs"], r["rc"], r["cs"], r["cc"],
                            r["flags"], *r["head"], *r["origin"], *r["mins"], *r["maxs"], 0)
    secs = [(b"MODL", bytes(modl), len(v3["recs"])),
            (b"PLAN", struct.pack(f"<{len(v3['plan'])}f", *v3["plan"]), len(v3["plan"]) // 4),
            (b"HNOD", struct.pack(f"<{len(v3['hnod'])}i", *v3["hnod"]), len(v3["hnod"]) // 3),
            (b"ENTS", struct.pack(f"<{len(v3['ents'])}i", *v3["ents"]), len(v3["ents"]) // 4),
            (b"EKVS", struct.pack(f"<{len(v3['ekvs'])}i", *v3["ekvs"]), len(v3["ekvs"]) // 4)]
    if p.get("nav"):
        secs += nav_sections(p["nav"])
    if p.get("nodg"):
        blob, n = p["nodg"]
        secs.append((b"NODG", blob, n))
    if p.get("tmat"):
        assert len(p["tmat"]) * 3 == ic
        secs.append((b"TMAT", p["tmat"] + b"\0" * (-len(p["tmat"]) % 4), len(p["tmat"])))
    if p.get("sky"):
        import sky
        secs.append((b"SKY6", sky.section(p["sky"]), 6))
    if p.get("clip"):
        cpos, cidx = p["clip"]
        secs.append((b"CLIP", struct.pack(f"<{len(cpos)}f", *cpos) + struct.pack(f"<{len(cidx)}i", *cidx), len(cidx) // 3))
    if p.get("rend"):
        assert len(p["rend"]) == len(v3["recs"])
        secs.append((b"REND", b"".join(struct.pack("<2i", *w) for w in p["rend"]), len(p["rend"])))
    if p.get("snds"):
        import sounds
        secs.append((b"SNDS", sounds.section(p["snds"]), len(p["snds"])))
    if p.get("titl"):
        import titles
        secs.append((b"TITL", titles.section(p["titl"]), len(p["titl"])))
    if p.get("sent"):
        import sentences
        secs.append((b"SENT", sentences.section(p["sent"]), len(p["sent"])))
    dir_off = core
    off = dir_off + 4 + 16 * len(secs)
    directory = struct.pack("<i", len(secs))
    for tag, blob, cnt in secs:
        assert len(blob) % 4 == 0
        directory += tag + struct.pack("<3i", off, len(blob), cnt)
        off += len(blob)
    with open(out, "wb") as fh:
        fh.write(b"GSV3" + struct.pack("<15i", HEADER, vc, ic, p["solid"], AW, AH, MIPS, LW, LH, len(ents),
                                       p["flags"], p["ntex"], world_v, world_i, dir_off))
        for k in ("pos", "uv0", "uv1", "rect"):
            fh.write(struct.pack(f"<{len(p[k])}f", *p[k]))
        fh.write(struct.pack(f"<{ic}i", *p["idx"]))
        fh.write(atlas)
        fh.write(light)
        fh.write(ents + b"\0" * (-len(ents) % 4))
        assert fh.tell() == dir_off
        fh.write(directory)
        for _tag, blob, _cnt in secs:
            fh.write(blob)
    size = os.path.getsize(out)
    assert size == off
    nr = len(v3["recs"])
    print(f"{out}: GSV3 {vc} vertices ({world_v} world), {ic // 3} triangles ({world_i // 3} world, {p['solid'] // 3} world solid), "
          f"{nr} model records, {len(v3['plan']) // 4} planes, {len(v3['hnod']) // 3} hull nodes, {len(v3['ents']) // 4} entities / "
          f"{len(v3['ekvs']) // 4} keyvalues, atlas {AW}x{AH}x{MIPS}mips ({p['ntex']} textures), lightmap {LW}x{LH}, "
          f"{size} bytes ({size / 1e6:.2f} MB)")
    if p["stats"]:
        print("stats:", p["stats"])



def node_graph(bsp, game_root, mod, nod_path, max_mb, log):
    """AI node graph → (NODG section bytes, node count) or None: the game's .nod for this map when it matches the
    map's node entities (CGraph::FLoadGraph), else the offline CTestHull build (tools/nodegraph.py)."""
    import navgen, nodegraph
    try:
        C, _sz = navgen.load(bsp)
        if not any(e.get("classname") in ("info_node", "info_node_air") for e in C.ents):
            log("node graph: no info_node entities")
            return None
        g = None
        nod = nod_path or nodegraph.find_nod(bsp, game_root, mod)
        if nod:
            g = nodegraph.load_nod(nod, C.ents, log=log)
            if g:
                log(f"node graph: file {nod} ({len(g['nodes'])} nodes, {len(g['links'])} links, {len(g['tables'])} route tables)")
        if g is None:
            g = nodegraph.build(C=C, log=log)
            if g:
                log(f"node graph: built from {len(g['nodes'])} info_node entities ({len(g['links'])} links, "
                    f"{len(g['tables'])} route tables)")
        if g is None:
            return None
        blob = nodegraph.section(g)
        if len(blob) > max_mb * 1024 * 1024:
            print(f"node graph left out: {len(blob) / 1e6:.1f} MB > --node-max-mb {max_mb:g}")
            return None
        return blob, len(g["nodes"])
    except Exception as e:                       # noqa: BLE001 — a broken graph must not cost the map
        log(f"node graph: skipped ({e})")
        return None


def find_nav(bsp, game_root, mod):
    """<mod>/maps/<map>.nav (written by the game's bot after bot_analyze), else Condition Zero's czero/maps/<map>.nav."""
    name = os.path.splitext(os.path.basename(bsp))[0]
    cands = [os.path.join(os.path.dirname(os.path.abspath(bsp)), name + ".nav")]
    # <root>/<mod>/maps/<map>.bsp → <root> (the companion passes only --bsp)
    from gamedirs import content_dirs, root_of_bsp
    roots = [game_root] if game_root else []
    roots.append(root_of_bsp(bsp))
    for r in roots:
        cands += [os.path.join(d, "maps", name + ".nav") for d in content_dirs(r, mod, fallback=None)]
        cands.append(os.path.join(r, "czero", "maps", name + ".nav"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def next_hop_table(areas, index):
    """Dijkstra from every target over the reversed area graph → next hop toward the target for every area."""
    import heapq
    A = len(areas)
    cen = [((a["lo"][0] + a["hi"][0]) / 2, (a["lo"][1] + a["hi"][1]) / 2, (a["lo"][2] + a["hi"][2]) / 2) for a in areas]
    rev = [[] for _ in range(A)]          # rev[v] = [(u, cost u→v)]
    for u, a in enumerate(areas):
        for d in a["connect"]:
            for t in d:
                v = index.get(t)
                if v is None or v == u:
                    continue
                c = math.dist(cen[u], cen[v])
                fl = areas[v]["flags"]
                if fl & 1: c *= 1.5
                if fl & 2: c += 50.0
                rev[v].append((u, c))
    out = [0xFFFF] * (A * A)
    for t in range(A):
        dist = [math.inf] * A
        dist[t] = 0.0
        base = t * A
        out[base + t] = t
        h = [(0.0, t)]
        while h:
            dv, v = heapq.heappop(h)
            if dv > dist[v]:
                continue
            for u, c in rev[v]:
                nd = dv + c
                if nd < dist[u]:
                    dist[u] = nd
                    out[base + u] = v
                    heapq.heappush(h, (nd, u))
    return out


def nav_sections(nav):
    import nav5
    areas = nav["areas"]
    index = {a["id"]: i for i, a in enumerate(areas)}
    nava, navl, navs = bytearray(), [], bytearray()
    for a in areas:
        lo, hi = a["lo"], a["hi"]
        links = [index[t] | (d << 24) for d, tgt in enumerate(a["connect"]) for t in tgt if t in index]
        nava += struct.pack("<8f6i", lo[0], lo[1], hi[0], hi[1], lo[2], a["neZ"], hi[2], a["swZ"], a["flags"], a["place"],
                            len(navl), len(links), len(navs) // 16, len(a["spots"]))
        navl += links
        for sp in a["spots"]:
            navs += struct.pack("<3fi", *sp["pos"], sp["flags"])
    names = b"\0".join(n.encode("ascii", "replace") for n in nav["places"]) + b"\0"
    names += b"\0" * (-len(names) % 4)
    nxt = next_hop_table(areas, index)
    nxt_b = struct.pack(f"<{len(nxt)}H", *nxt)
    nxt_b += b"\0" * (-len(nxt_b) % 4)
    return [(b"NAVA", bytes(nava), len(areas)), (b"NAVN", nxt_b, len(nxt)), (b"NAVL", struct.pack(f"<{len(navl)}i", *navl), len(navl)),
            (b"NAVS", bytes(navs), len(navs) // 16), (b"NAVP", names, len(nav["places"]))]


def synthetic(n=128, size=60.0):
    pos, uv0, uv1, rect, idx = [], [], [], [], []
    AW = AH = 64 + 2 * GUTTER
    tex = {0: ("checker", 64, 64, checker_rgba())}
    AW, AH, atlas, rects = build_atlas(tex, 64, lambda m: None)
    LW = LH = 64
    light = bytearray()
    for y in range(LH):
        for x in range(LW):
            v = int(80 + 100 * (0.5 + 0.5 * math.sin(x * 0.2) * math.cos(y * 0.15)))
            light += bytes((v, v, v, 255))
    for z in range(n + 1):
        for x in range(n + 1):
            px, pz = (x / n - 0.5) * size, (z / n - 0.5) * size
            py = 1.5 * math.sin(px * 0.25) * math.cos(pz * 0.2)
            pos += [px, py, pz]; uv0 += [x / 8.0, z / 8.0]; uv1 += [x / n, z / n]; rect += rects[0]
    for z in range(n):
        for x in range(n):
            a = z * (n + 1) + x; bb = a + 1; c = a + n + 1; d = c + 1
            idx += [a, c, bb, bb, c, d]
    return dict(pos=pos, uv0=uv0, uv1=uv1, rect=rect, idx=idx, solid=len(idx), atlas=(AW, AH, atlas),
                light=(LW, LH, bytes(light)), ents=b"", ntex=1, flags=1, stats={})


def write(out, p):
    vc, ic = len(p["pos"]) // 3, len(p["idx"])
    AW, AH, atlas = p["atlas"]
    LW, LH, light = p["light"]
    ents = p["ents"]
    assert len(atlas) == sum((AW >> l) * (AH >> l) * 4 for l in range(MIPS))
    assert len(light) == LW * LH * 4
    with open(out, "wb") as fh:
        fh.write(b"GSV2" + struct.pack("<15i", HEADER, vc, ic, p["solid"], AW, AH, MIPS, LW, LH, len(ents),
                                       p["flags"], p["ntex"], 0, 0, 0))
        for k in ("pos", "uv0", "uv1", "rect"):
            fh.write(struct.pack(f"<{len(p[k])}f", *p[k]))
        fh.write(struct.pack(f"<{ic}i", *p["idx"]))
        fh.write(atlas)
        fh.write(light)
        fh.write(ents + b"\0" * (-len(ents) % 4))
    size = os.path.getsize(out)
    print(f"{out}: {vc} vertices, {ic // 3} triangles ({p['solid'] // 3} solid), atlas {AW}x{AH}x{MIPS}mips "
          f"({p['ntex']} textures), lightmap {LW}x{LH}, entities {len(ents)} B, {size} bytes ({size / 1e6:.2f} MB)")
    if p["stats"]:
        print("stats:", p["stats"])


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):     # never crash on a console code page (Windows cp1252)
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--synthetic", action="store_true")
    g.add_argument("--bsp")
    ap.add_argument("--game-root", help="e.g. input/Half-Life (WAD search root)")
    ap.add_argument("--mod", default="valve")
    ap.add_argument("--max-tex", type=int, default=512, help="cap texture edge length before atlasing")
    ap.add_argument("--no-entities-brushes", action="store_true", help="world model only")
    ap.add_argument("--format", type=int, choices=(2, 3), default=3, help="pack version (default 3)")
    ap.add_argument("--nav", help="bot navigation mesh (.nav); default: auto-detect next to the BSP / <mod>/maps / czero/maps")
    ap.add_argument("--no-nav", action="store_true")
    ap.add_argument("--gen-nav", action=argparse.BooleanOptionalAction, default=True,
                    help="no .nav found: generate one from the BSP with tools/navgen.py (default on)")
    ap.add_argument("--max-bytes", type=int, default=95 * 1024 * 1024,
                    help="pack too big (VRChat download cap 100 MB): rebuild with half the texture cap until it fits")
    ap.add_argument("--nav-max-mb", type=float, default=24.0,
                    help="leave the nav mesh out when its next-hop table (areas² × 2 bytes) is bigger than this; big "
                         "generated meshes of open Sven Co-op maps would otherwise fill the 100 MB download cap")
    ap.add_argument("--no-nodes", action="store_true", help="leave out the NODG AI node graph (info_node entities)")
    ap.add_argument("--nod", help="the game's node graph file (.nod); default: <mod>/maps/graphs/<map>.nod, else built "
                                  "from the map's info_node entities with tools/nodegraph.py")
    ap.add_argument("--node-max-mb", type=float, default=16.0,
                    help="leave the node graph out when its section (route tables N² × 2 bytes each) is bigger than this")
    ap.add_argument("--no-sky", action="store_true", help="leave out the SKY6 skybox faces")
    ap.add_argument("--no-sounds", action="store_true", help="leave out the SNDS entity sounds (ambient_generic, movers …)")
    ap.add_argument("--sound-mb", type=float, default=24.0, help="SNDS budget in MB of float32 samples (ambient first)")
    ap.add_argument("--no-titles", action="store_true", help="leave out the TITL env_message texts (titles.txt)")
    ap.add_argument("--no-sentences", action="store_true", help="leave out the SENT speech sentences (and their word wavs)")
    ap.add_argument("--sentence-mb", type=float, default=8.0,
                    help="budget in MB of float32 samples for the sentence word wavs (scripted_sentence first, then talk groups)")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("out")
    a = ap.parse_args()
    t0 = time.time()
    import bprog
    bprog.plan(convert=55, nav=20, sky=2, sounds=15, write=8)
    _marks = (("diffuse atlas", 1), ("lightmap atlas", 2), ("materials:", 3))        # progress of the silent convert stage
    _log = (lambda m: None) if a.quiet else (lambda m: print("  " + m))

    def log(m):
        _log(m)
        for needle, k in _marks:
            if needle in m:
                bprog.progress("convert", k, 4)
    bprog.progress("convert", 0, 4)
    conv = from_bsp_v3 if a.format == 3 else from_bsp
    data = synthetic() if a.synthetic else conv(a.bsp, a.game_root, a.mod, a.max_tex, not a.no_entities_brushes, log)
    bprog.progress("convert", 4, 4)
    if a.format == 3 and a.bsp and not a.no_nav:
        import nav5
        np_ = a.nav or find_nav(a.bsp, a.game_root, a.mod)
        if not np_ and a.gen_nav:
            import navgen
            bprog.progress("nav", 0, 1)
            data["nav"] = navgen.generate(a.bsp, log=log)
            log(f"nav source: generated by navgen ({len(data['nav']['areas'])} areas, {len(data['nav']['places'])} places)")
        elif not np_:
            log("nav source: none (no .nav found, --no-gen-nav)")
        if np_:
            log(f"nav source: file {np_}")
            nav = nav5.parse(open(np_, "rb").read())
            if nav5.check(nav) == 0:
                data["nav"] = nav
                bs = os.path.getsize(a.bsp)
                log(f"nav {np_}: {len(nav['areas'])} areas, {len(nav['places'])} places"
                    + ("" if nav["bsp_size"] in (None, bs) else f" (WARNING: made for a different map version, bsp {nav['bsp_size']} != {bs})"))
            else:
                log(f"nav {np_}: invalid references, skipped")
        if data.get("nav"):
            na = len(data["nav"]["areas"])
            if na * na * 2 > a.nav_max_mb * 1024 * 1024:
                print(f"nav left out: {na} areas need a {na * na * 2 / 1e6:.1f} MB next-hop table (> --nav-max-mb {a.nav_max_mb:g})")
                data["nav"] = None
    bprog.progress("nav", 1, 1)
    if a.format == 3 and a.bsp and not a.no_nodes:
        data["nodg"] = node_graph(a.bsp, a.game_root, a.mod, a.nod, a.node_max_mb, log)
    if a.format == 3 and a.bsp and not a.no_sky:
        import sky
        try:
            data["sky"] = sky.load(data["ents"].decode("latin-1", "replace"), a.game_root, a.mod, log=log)
        except Exception as e:                       # noqa: BLE001 — a broken sky image must not cost the map
            log(f"sky: skipped ({e})")
    bprog.progress("sky", 1, 1)
    if a.format == 3 and a.bsp and (not a.no_sounds or not a.no_titles):
        bprog.progress("sounds", 0, 1)
        ent_list = entity_dicts(data["ents"].decode("latin-1", "replace"))
        if not a.no_sounds:
            import sounds
            try:
                data["snds"] = sounds.load(ent_list, a.game_root, a.mod, max_bytes=int(a.sound_mb * 1024 * 1024), log=log)
            except Exception as e:                   # noqa: BLE001 — a broken wav must not cost the map
                log(f"sounds: skipped ({e})")
            if not a.no_sentences:
                import sentences
                try:
                    have = [c[0] for c in data.get("snds") or []]
                    mname = os.path.splitext(os.path.basename(a.bsp))[0].lower()
                    sent, extra = sentences.load(ent_list, a.game_root, a.mod, mname, have,
                                                 max_bytes=int(a.sentence_mb * 1024 * 1024), log=log)
                    if sent:
                        data["snds"] = (data.get("snds") or []) + extra
                        data["sent"] = sent
                except Exception as e:               # noqa: BLE001 — a broken sentences.txt must not cost the map
                    log(f"sentences: skipped ({e})")
        if not a.no_titles:
            import titles
            try:
                data["titl"] = titles.load(ent_list, a.game_root, a.mod, log=log)
            except Exception as e:                   # noqa: BLE001
                log(f"titles: skipped ({e})")
    bprog.progress("sounds", 1, 1)
    bprog.progress("write", 0, 1)
    (write_v3 if a.format == 3 else write)(a.out, data)
    bprog.progress("write", 1, 1)
    # VRChat downloads at most 100 MB: big HD maps (Sven Co-op) get smaller textures until the pack fits
    mt = a.max_tex
    while a.format == 3 and a.bsp and os.path.getsize(a.out) > a.max_bytes and mt > 64:
        mt //= 2
        print(f"pack {os.path.getsize(a.out) / 1e6:.1f} MB > {a.max_bytes / 1e6:.0f} MB: rebuilding with --max-tex {mt}")
        small = conv(a.bsp, a.game_root, a.mod, mt, not a.no_entities_brushes, log)
        for k in ("nav", "nodg", "sky", "snds", "titl", "sent"):
            if data.get(k):
                small[k] = data[k]
        data = small
        write_v3(a.out, data)
    print(f"built in {time.time() - t0:.1f} s")
