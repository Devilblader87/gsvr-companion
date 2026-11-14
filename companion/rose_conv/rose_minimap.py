"""Zone minimap for an RPG1 pack (section MMAP, docs/RPG1_PACK.md, section MMAP): the zone's minimap image, its world <-> map transform, the minimap arrow and the fixed NPC
markers, so the runtime can draw the DlgMinimap map like the original client. Read-only on the client files, stdlib only.

The transform is the original's (rose-offline-client ui_minimap_system.rs): LIST_ZONE col 8 = minimap image (a DDS under 3DDATA/MAPS/..), cols 9 / 10 = start block x / y,
block = 16 * grid_per_patch * grid_size of the ZON (160 m), the image is 64 px per block with a one block (64 px) outline on every side:
    pixel x = 64 + (X - startX * block) / (block / 64)         X = absolute (server) metres, east
    pixel y = 64 + ((65 - startY) * block - Z) / (block / 64)  Z = absolute (server) metres, north; y grows SOUTH (image row 0 = top)
The section stores the same as an affine map from absolute metres to image pixels:  px = (X - ox) / mpp,  py = (oz - Z) / mpp  with
ox = startX * block - 64 * mpp, oz = (65 - startY) * block + 64 * mpp, mpp = block / 64.

MMAP layout (little endian, section version 1, count = number of NPC markers):
    u32 flags (bit 0: the OTEX block has the arrow as texture 1)   u32 npcCount   u32 npcOff   u32 otexOff   u32 otexSize
    f32 ox   f32 oz   f32 mpp   f32 blockM   f32 outlinePx   i32 startX   i32 startY            (48 bytes header)
    npcCount x { f32 x, f32 z (absolute metres), u16 icon (STATEICON sprite index), u16 npcId, u32 0 }   (16 bytes)
    OTEX block (rpg1.enc_otex): texture 0 = the map (DXT1 / DXT5, no mips), texture 1 = MINIMAP_ARROW (RGBA32, 16 x 16, optional)
"""
import os, sys, struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosevfs, rosefmt as F, rosedds as D, rpg1 as P

TAG_MMAP = b'MMAP'
HDR = struct.Struct('<5I5f2i')            # 48 bytes
NPC = struct.Struct('<2f2HI')             # 16 bytes
OUTLINE_PX = 64.0
BLOCK_PX = 64.0
ARROW_PATH = '3DDATA/CONTROL/RES/MINIMAP_ARROW.TGA'
FLAG_ARROW = 1
MAX_NPC = 512
KIND_NPC = 1                              # rose_pack spawn kind of a fixed NPC (P.KIND_NPC)


def map_transform(start_x, start_y, block_m):
    """-> (ox, oz, mpp): absolute metres of image pixel (0, 0) and metres per pixel (see the module doc)."""
    mpp = block_m / BLOCK_PX
    return start_x * block_m - OUTLINE_PX * mpp, (65 - start_y) * block_m + OUTLINE_PX * mpp, mpp


def world_to_px(ox, oz, mpp, x, z):
    """absolute metres -> image pixels (x right, y down)"""
    return (x - ox) / mpp, (oz - z) / mpp


def parse_tga(d):
    """uncompressed true-colour TGA (type 2, 24 / 32 bit) -> (w, h, rgba bytes, top row first)."""
    if len(d) < 18:
        raise ValueError('tga too short')
    idlen, cmap, typ = d[0], d[1], d[2]
    w, h = struct.unpack_from('<HH', d, 12)
    bpp, desc = d[16], d[17]
    if typ != 2 or cmap != 0 or bpp not in (24, 32) or w == 0 or h == 0:
        raise ValueError('unsupported tga (type %d, %d bpp)' % (typ, bpp))
    o = 18 + idlen
    n = bpp // 8
    if o + w * h * n > len(d):
        raise ValueError('tga data cut off')
    out = bytearray(w * h * 4)
    top_first = bool(desc & 0x20)
    for y in range(h):
        sy = y if top_first else h - 1 - y
        for x in range(w):
            s = o + (sy * w + x) * n
            q = (y * w + x) * 4
            out[q] = d[s + 2]; out[q + 1] = d[s + 1]; out[q + 2] = d[s]
            out[q + 3] = d[s + 3] if n == 4 else 255
    return w, h, bytes(out)


def map_texture(name, dds_bytes):
    """DDS bytes -> (pack texture dict without mips, (image w, h)): DXT1 when opaque else DXT5 for raw images, compressed images keep their format. Dimensions are padded to
    whole 4 x 4 blocks by edge replication (the transform is unchanged: the padding sits right / below the image)."""
    dds = D.parse_dds(dds_bytes)
    w, h = dds['w'], dds['h']
    if dds['fmt'] != 'RGBA':
        fmt, tw, th, lv = D.to_pack_texture(dds)
        return dict(name=name, fmt=fmt, w=tw, h=th, levels=[lv[0]]), (w, h)
    rgba = dds['levels'][0]
    nw, nh = (w + 3) // 4 * 4, (h + 3) // 4 * 4
    if (nw, nh) != (w, h):
        out = bytearray(nw * nh * 4)
        for y in range(nh):
            for x in range(nw):
                o = (min(y, h - 1) * w + min(x, w - 1)) * 4
                out[(y * nw + x) * 4:(y * nw + x) * 4 + 4] = rgba[o:o + 4]
        rgba = bytes(out)
    opaque = all(rgba[i] == 255 for i in range(3, len(rgba), 4))
    enc = D.encode_dxt1 if opaque else D.encode_dxt5
    return dict(name=name, fmt=D.FMT_DXT1 if opaque else D.FMT_DXT5, w=nw, h=nh, levels=[enc(rgba, nw, nh)]), (w, h)


def _num(s):
    try:
        return int(float(str(s).strip()))
    except ValueError:
        return 0


def npc_markers(vfs, spawns, win, warn):
    """fixed NPC spawns (kind NPC) with their LIST_NPC col 18 minimap icon -> [(x, z, icon, npc_id)] absolute metres. NPCs without an icon are skipped (like the original)."""
    try:
        stb = F.stb(vfs.read('3DDATA/STB/LIST_NPC.STB'))
    except KeyError:
        warn.append('minimap: no LIST_NPC.STB, no NPC markers')
        return []
    rows = stb['cells']
    out = []
    skipped = 0
    for sp in spawns:
        if sp['kind'] != KIND_NPC:
            continue
        nid = sp['npc']
        icon = _num(rows[nid][18]) if 0 <= nid < len(rows) and len(rows[nid]) > 18 else 0
        if icon <= 0 or nid > 0xFFFF or icon > 0xFFFF:
            skipped += 1
            continue
        out.append((sp['x'] + win.x0, sp['z'] + win.z0, icon, nid))
    if skipped:
        warn.append('minimap: %d NPCs without a minimap icon skipped' % skipped)
    if len(out) > MAX_NPC:
        warn.append('minimap: %d NPC markers, kept %d' % (len(out), MAX_NPC))
        out = out[:MAX_NPC]
    return out


def enc_mmap(tex_map, tex_arrow, ox, oz, mpp, block_m, start_x, start_y, npcs):
    flags = FLAG_ARROW if tex_arrow is not None else 0
    otex = P.enc_otex([tex_map] + ([tex_arrow] if tex_arrow is not None else []))
    npc_off = HDR.size
    otex_off = npc_off + len(npcs) * NPC.size
    body = b''.join(NPC.pack(x, z, ic, nid, 0) for x, z, ic, nid in npcs)
    d = HDR.pack(flags, len(npcs), npc_off, otex_off, len(otex), ox, oz, mpp, block_m, OUTLINE_PX, start_x, start_y) + body + otex
    return d + b'\0' * (-len(d) % 4)


def dec_mmap(d):
    """-> dict(flags, ox, oz, mpp, block, outline, start_x, start_y, npcs=[(x, z, icon, id)], textures=[tex dicts]); ValueError when malformed."""
    if len(d) < HDR.size:
        raise ValueError('MMAP too short')
    flags, nn, no, oo, osz, ox, oz, mpp, blk, outl, sx, sy = HDR.unpack_from(d, 0)
    if no < HDR.size or no + nn * NPC.size > len(d) or oo < no or oo + osz > len(d) or not mpp > 0:
        raise ValueError('MMAP layout out of bounds')
    npcs = []
    for i in range(nn):
        x, z, ic, nid, _r = NPC.unpack_from(d, no + i * NPC.size)
        npcs.append((x, z, ic, nid))
    texs = P.dec_otex(d[oo:oo + osz])
    if not texs or (flags & FLAG_ARROW and len(texs) < 2):
        raise ValueError('MMAP textures missing')
    return dict(flags=flags, ox=ox, oz=oz, mpp=mpp, block=blk, outline=outl, start_x=sx, start_y=sy, npcs=npcs, textures=texs)


def build_section(vfs, zone, zon, spawns, win):
    """-> (sections [(tag, version, count, data)], info dict, warnings [str]). Never raises for missing client files (no section then)."""
    warn = []
    cells = zone.get('cells') or []
    path = cells[8].replace('\\\\', '\\').strip() if len(cells) > 8 else ''
    sx = _num(cells[9]) if len(cells) > 9 else 0
    sy = _num(cells[10]) if len(cells) > 10 else 0
    if not path:
        return [], {}, ['minimap: LIST_ZONE col 8 is empty, no MMAP']
    if rosevfs.norm(path) not in vfs.files:
        return [], {}, ['minimap: %s not found, no MMAP' % path]
    try:
        tex, (iw, ih) = map_texture('minimap', vfs.read(path))
    except (ValueError, KeyError) as e:
        return [], {}, ['minimap: %s unusable (%s)' % (path, e)]
    if 'gpp' not in zon or 'gsz' not in zon:
        return [], {}, ['minimap: ZON has no grid block (grid per patch / size)']
    block_m = 16.0 * zon['gpp'] * zon['gsz'] / 100.0
    ox, oz, mpp = map_transform(sx, sy, block_m)
    arrow = None
    try:
        aw, ah, argba = parse_tga(vfs.read(ARROW_PATH))
        arrow = dict(name='minimap_arrow', fmt=D.FMT_RGBA32, w=aw, h=ah, levels=[argba])
    except (ValueError, KeyError) as e:
        warn.append('minimap: arrow unavailable (%s)' % e)
    npcs = npc_markers(vfs, spawns, win, warn)
    data = enc_mmap(tex, arrow, ox, oz, mpp, block_m, sx, sy, npcs)
    info = dict(path=path, image=[iw, ih], texture=[tex['w'], tex['h']], bytes=len(data), block_m=block_m, start=[sx, sy], origin=[ox, oz], mpp=mpp,
                npcs=len(npcs), arrow=arrow is not None,
                interior_blocks=[(iw - 2 * OUTLINE_PX) / BLOCK_PX, (ih - 2 * OUTLINE_PX) / BLOCK_PX])
    return [(TAG_MMAP, 1, len(npcs), data)], info, warn
