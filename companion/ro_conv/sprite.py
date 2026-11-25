"""Ragnarok Online SPR + ACT -> composed sprite atlases + animation table for Udon (stdlib only). Design: docs/design/ro_sprites.md.

BYOG: the inputs are bytes from the player's own client (data.grf, read by the GRF lane); this module never ships game data. The tests use
synthetic SPR / ACT files written by write_spr / write_act below.

Format references (public documentation, re-implemented here, no code copied):
  - SPR / ACT layout as documented by the Ragnarok Research Lab (https://ragnarokresearchlab.github.io/file-formats/spr/ and .../act/)
  - cross-checked against the field order described in roBrowser (GPL-3.0, src/Loaders/Sprite.js, Action.js) and zrenderer (MIT)

Output: two RPG1 sections (container = companion/rose_conv/rpg1.py write_pack, layout docs/design/ro_sprites.md "Pack layout"):
  RSAT  atlas pages, RGBA32, rows BOTTOM first (Unity LoadRawTextureData order)
  RSAN  animation table: sprites -> clips (anim * 8 + dir) -> frames (atlas rect + offset from the feet + anchor + delay)

Every ACT frame (all its layers, with mirror / scale / rotation / tint) is pre-composed into ONE image, identical images are stored once, so the
runtime draws one quad per sprite part (body, head) and needs no palette and no layer maths. Head placement: head.offset + body.anchor - head.anchor.

  usage: python3 -I sprite.py OUT.rpg1 name:kind:file.spr:file.act [...]     kind = body | head | monster
"""
import math
import struct
import sys
import os
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROSE = os.path.join(os.path.dirname(_HERE), 'rose_conv')
if _ROSE not in sys.path:
    sys.path.append(_ROSE)
import rpg1 as P  # noqa: E402  (RPG1 container: write_pack / read_pack)

# ---------------------------------------------------------------- constants
KIND_BODY, KIND_HEAD, KIND_MONSTER, KIND_OTHER = 0, 1, 2, 3
KINDS = {'body': KIND_BODY, 'head': KIND_HEAD, 'monster': KIND_MONSTER, 'other': KIND_OTHER}

# RO direction index (ACT action = anim * 8 + dir): 0 S (facing the camera), then clockwise seen from above
DIR_NAMES = ('S', 'SW', 'W', 'NW', 'N', 'NE', 'E', 'SE')
# player actions (body / head ACT): 13 anims
PLAYER_ANIMS = ('idle', 'walk', 'sit', 'pickup', 'standby', 'attack1', 'hurt', 'freeze1', 'dead', 'freeze2', 'attack2', 'attack3', 'cast')
# monster actions: 5 anims
MONSTER_ANIMS = ('idle', 'walk', 'attack', 'hurt', 'dead')
ANIM_IDLE, ANIM_WALK, ANIM_SIT = 0, 1, 2

# client paths (unicode; the GRF stores them CP949-encoded: path.encode('cp949')). Hair style 1 = the default Novice head.
PATH_BODY = {'m': 'data/sprite/인간족/몸통/남/초보자_남', 'f': 'data/sprite/인간족/몸통/여/초보자_여'}
PATH_HEAD = {'m': 'data/sprite/인간족/머리통/남/1_남', 'f': 'data/sprite/인간족/머리통/여/1_여'}
PATH_PORING = 'data/sprite/몬스터/poring'

RSAN_VERSION = 1
RSAN_HDR = struct.Struct('<5If3I')     # version, sprites, clips, frames, pages, ref_height_px, names_off, reserved x2  (36 bytes)
RSAN_SPRITE = struct.Struct('<6I')     # name_off, name_len, kind, first_clip, clip_count, frame_count
RSAN_CLIP = struct.Struct('<I2H')      # first_frame, frame_count, delay_ms
RSAN_FRAME = struct.Struct('<6H4h')    # page, flags, x, y(from TOP), w, h, left, top (px from the feet, y down), anchor x, anchor y  (20 bytes)
RSAT_PAGE = struct.Struct('<5I')       # w, h, fmt (3 = RGBA32), data_off (from section start), data_size
FMT_RGBA32 = 3
FF_ANCHOR = 1                          # frame flag: the anchor fields are valid
FF_EMPTY = 2                           # frame flag: no pixels (w = h = 0)
# w34 monsters: a frame composed at 1 / 2^shift of its size (a death anim's 3.6x-scaled ghost layer would be a 370 px composite per frame); bits 8..11 of the
# flags = shift, the runtime (GsvrRoSpriteSet) multiplies left / top / w / h by 2^shift, so the quad keeps the original size and only the texels are coarser.
FF_SHIFT_POS = 8
FF_SHIFT_MASK = 0x0F00
MAX_SHIFT = 4
TAG_RSAT, TAG_RSAN = b'RSAT', b'RSAN'
MAX_PAGE = 2048
DEFAULT_DELAY_TICKS = 4.0              # ACT before 2.2 has no delays: 4 ticks
TICK_MS = 25.0                         # ACT delay unit (ms per tick)


class FormatError(ValueError):
    pass


def _need(buf, pos, n, what):
    if pos + n > len(buf):
        raise FormatError('%s: truncated at %d (need %d bytes, have %d)' % (what, pos, n, len(buf) - pos))


# ---------------------------------------------------------------- SPR
def parse_spr(buf):
    """-> dict(version, pal=[(w, h, idx bytes)], rgba=[(w, h, RGBA bytes top row first)], palette=[(r, g, b)] * 256 or None).
    Version word = buf[2] minor, buf[3] major (0x0201 = 2.1). 2.1+: indexed frames are RLE (0x00 n = n zero bytes). 2.0+: RGBA frames
    (stored A B G R, rows bottom-up; returned as R G B A, top row first). 1.1+: 256 * RGBA palette in the last 1024 bytes."""
    buf = bytes(buf)
    _need(buf, 0, 6, 'SPR header')
    if buf[:2] != b'SP':
        raise FormatError('not an SPR file')
    ver = buf[2] | (buf[3] << 8)
    if ver < 0x0100 or ver > 0x0201:
        raise FormatError('unsupported SPR version %d.%d' % (ver >> 8, ver & 0xFF))
    n_pal = struct.unpack_from('<H', buf, 4)[0]
    pos = 6
    n_rgba = 0
    if ver >= 0x0200:
        _need(buf, pos, 2, 'SPR header')
        n_rgba = struct.unpack_from('<H', buf, pos)[0]
        pos += 2
    end = len(buf) - 1024 if ver >= 0x0101 else len(buf)
    if end < pos:
        raise FormatError('SPR too short for its palette')
    pal = []
    for i in range(n_pal):
        _need(buf[:end], pos, 4, 'SPR indexed frame %d' % i)
        w, h = struct.unpack_from('<2H', buf, pos)
        pos += 4
        if ver >= 0x0201:
            _need(buf[:end], pos, 2, 'SPR frame %d size' % i)
            sz = struct.unpack_from('<H', buf, pos)[0]
            pos += 2
            _need(buf[:end], pos, sz, 'SPR frame %d data' % i)
            idx = rle_decode(buf[pos:pos + sz], w * h)
            pos += sz
        else:
            _need(buf[:end], pos, w * h, 'SPR frame %d data' % i)
            idx = buf[pos:pos + w * h]
            pos += w * h
        pal.append((w, h, bytes(idx)))
    rgba = []
    for i in range(n_rgba):
        _need(buf[:end], pos, 4, 'SPR rgba frame %d' % i)
        w, h = struct.unpack_from('<2H', buf, pos)
        pos += 4
        n = w * h * 4
        _need(buf[:end], pos, n, 'SPR rgba frame %d data' % i)
        src = buf[pos:pos + n]
        pos += n
        out = bytearray(n)
        for y in range(h):
            s = (h - 1 - y) * w * 4           # stored bottom-up
            d = y * w * 4
            for x in range(w):
                a, b, g, r = src[s + x * 4:s + x * 4 + 4]
                out[d + x * 4:d + x * 4 + 4] = bytes((r, g, b, a))
        rgba.append((w, h, bytes(out)))
    palette = None
    if ver >= 0x0101:
        p = buf[len(buf) - 1024:]
        palette = [(p[i * 4], p[i * 4 + 1], p[i * 4 + 2]) for i in range(256)]
    return dict(version=ver, pal=pal, rgba=rgba, palette=palette)


def rle_decode(data, n):
    """SPR 2.1 RLE: a 0x00 byte is followed by a run length of zero bytes (a length of 0 is read as 1); output padded / cut to n."""
    out = bytearray()
    i, L = 0, len(data)
    while i < L and len(out) < n:
        b = data[i]
        i += 1
        if b == 0:
            c = data[i] if i < L else 1
            i += 1
            out += b'\0' * (c if c else 1)
        else:
            out.append(b)
    if len(out) < n:
        out += b'\0' * (n - len(out))
    return bytes(out[:n])


def rle_encode(idx):
    out = bytearray()
    i, n = 0, len(idx)
    while i < n:
        if idx[i] == 0:
            j = i
            while j < n and idx[j] == 0 and j - i < 255:
                j += 1
            out += bytes((0, j - i))
            i = j
        else:
            out.append(idx[i])
            i += 1
    return bytes(out)


def spr_image(spr, index, sprite_type):
    """-> (w, h, RGBA top row first) of SPR frame `index` (sprite_type 0 indexed, 1 rgba) or None when out of range. Palette index 0 = transparent."""
    if sprite_type == 1:
        if index < 0 or index >= len(spr['rgba']):
            return None
        return spr['rgba'][index]
    if index < 0 or index >= len(spr['pal']):
        return None
    w, h, idx = spr['pal'][index]
    pal = spr['palette'] or [(i, i, i) for i in range(256)]
    lut = [bytes((r, g, b, 255)) for (r, g, b) in pal]
    lut[0] = b'\0\0\0\0'
    return (w, h, b''.join(lut[c] for c in idx))


# ---------------------------------------------------------------- ACT
def parse_act(buf):
    """-> dict(version, actions=[[frame, ...]], events=[bytes], delays=[ms per frame, one per action]).
    frame = dict(layers=[dict(x, y, spr, mirror, rgba, sx, sy, angle, type)], event, anchors=[(x, y)])."""
    buf = bytes(buf)
    _need(buf, 0, 16, 'ACT header')
    if buf[:2] != b'AC':
        raise FormatError('not an ACT file')
    ver = buf[2] | (buf[3] << 8)
    if ver < 0x0100 or ver > 0x0205:
        raise FormatError('unsupported ACT version %d.%d' % (ver >> 8, ver & 0xFF))
    n_act = struct.unpack_from('<H', buf, 4)[0]
    pos = 16                                   # 10 reserved bytes after the count
    actions = []
    for a in range(n_act):
        _need(buf, pos, 4, 'ACT action %d' % a)
        nf = struct.unpack_from('<I', buf, pos)[0]
        pos += 4
        if nf > 10000:
            raise FormatError('ACT action %d: %d frames' % (a, nf))
        frames = []
        for f in range(nf):
            _need(buf, pos, 36, 'ACT frame %d/%d' % (a, f))
            pos += 32                          # two unused rectangles
            nl = struct.unpack_from('<I', buf, pos)[0]
            pos += 4
            if nl > 1000:
                raise FormatError('ACT frame %d/%d: %d layers' % (a, f, nl))
            layers = []
            for _ in range(nl):
                _need(buf, pos, 16, 'ACT layer')
                x, y, si, flags = struct.unpack_from('<3iI', buf, pos)
                pos += 16
                rgba, sx, sy, ang, typ = (255, 255, 255, 255), 1.0, 1.0, 0, 0
                if ver >= 0x0200:
                    _need(buf, pos, 8, 'ACT layer colour')
                    rgba = tuple(buf[pos:pos + 4])
                    sx = sy = struct.unpack_from('<f', buf, pos + 4)[0]
                    pos += 8
                    if ver >= 0x0204:
                        _need(buf, pos, 4, 'ACT layer scale y')
                        sy = struct.unpack_from('<f', buf, pos)[0]
                        pos += 4
                    _need(buf, pos, 8, 'ACT layer angle')
                    ang, typ = struct.unpack_from('<2i', buf, pos)
                    pos += 8
                    if ver >= 0x0205:
                        _need(buf, pos, 8, 'ACT layer size')
                        pos += 8                   # width, height (redundant with the SPR frame)
                layers.append(dict(x=x, y=y, spr=si, mirror=bool(flags & 1), rgba=rgba, sx=sx, sy=sy, angle=ang, type=typ))
            ev = -1
            if ver >= 0x0200:
                _need(buf, pos, 4, 'ACT frame event')
                ev = struct.unpack_from('<i', buf, pos)[0]
                pos += 4
            anchors = []
            if ver >= 0x0203:
                _need(buf, pos, 4, 'ACT anchors')
                na = struct.unpack_from('<I', buf, pos)[0]
                pos += 4
                if na > 64:
                    raise FormatError('ACT frame %d/%d: %d anchors' % (a, f, na))
                for _ in range(na):
                    _need(buf, pos, 16, 'ACT anchor')
                    _u, ax, ay, _attr = struct.unpack_from('<4i', buf, pos)
                    pos += 16
                    anchors.append((ax, ay))
            frames.append(dict(layers=layers, event=ev, anchors=anchors))
        actions.append(frames)
    events = []
    if ver >= 0x0201 and pos + 4 <= len(buf):
        ne = struct.unpack_from('<I', buf, pos)[0]
        pos += 4
        for _ in range(ne):
            _need(buf, pos, 40, 'ACT event name')
            events.append(buf[pos:pos + 40].split(b'\0', 1)[0])
            pos += 40
    delays = [DEFAULT_DELAY_TICKS * TICK_MS] * n_act
    if ver >= 0x0202 and pos + 4 * n_act <= len(buf):
        delays = [struct.unpack_from('<f', buf, pos + 4 * i)[0] * TICK_MS for i in range(n_act)]
    return dict(version=ver, actions=actions, events=events, delays=delays)


# ---------------------------------------------------------------- composing
def compose_frame(spr, frame, scale=1.0):
    """All layers of one ACT frame -> (left, top, w, h, RGBA top row first); left / top = pixel position of the image's top-left corner relative
    to the feet origin (y down, RO screen space). A layer image is centred on (x, y), scaled by (sx, sy), mirrored (flip x) and rotated by `angle`
    degrees clockwise (screen space), colour multiplied by its rgba; later layers are drawn over earlier ones ("over" blend). (0, 0, 0, 0, b'') when empty.
    scale != 1: the whole frame is composed at that size (positions and layer scales multiplied; w34 FF_SHIFT frames: scale = 1 / 2^shift)."""
    items = []
    for L in frame['layers']:
        if L['spr'] < 0:
            continue
        img = spr_image(spr, L['spr'], L['type'])
        if img is None or img[0] == 0 or img[1] == 0:
            continue
        if scale != 1.0:
            L = dict(L, x=L['x'] * scale, y=L['y'] * scale, sx=(abs(L['sx']) or 1.0) * scale, sy=(abs(L['sy']) or 1.0) * scale)
        items.append((L, img))
    if not items:
        return (0, 0, 0, 0, b'')
    boxes = []
    for L, (w, h, _px) in items:
        sx, sy = abs(L['sx']) or 1.0, abs(L['sy']) or 1.0
        if L['angle'] % 360 == 0 and sx == 1.0 and sy == 1.0 and L['x'] == int(L['x']) and L['y'] == int(L['y']):
            l0 = int(L['x']) - w // 2
            t0 = int(L['y']) - h // 2
            boxes.append((l0, t0, l0 + w, t0 + h))
        else:
            ca, sa = math.cos(math.radians(L['angle'])), math.sin(math.radians(L['angle']))
            xs, ys = [], []
            for cx, cy in ((-w / 2, -h / 2), (w / 2, -h / 2), (-w / 2, h / 2), (w / 2, h / 2)):
                px, py = cx * sx, cy * sy
                xs.append(L['x'] + px * ca - py * sa)
                ys.append(L['y'] + px * sa + py * ca)
            # pixel centres inside the transformed rectangle (rounded first: cos 90 is 6e-17, not 0)
            boxes.append(tuple(int(math.floor(round(v, 6) + 0.5)) for v in (min(xs), min(ys), max(xs), max(ys))))
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    W = max(b[2] for b in boxes) - left
    H = max(b[3] for b in boxes) - top
    out = bytearray(W * H * 4)
    for (L, (w, h, px)), box in zip(items, boxes):
        tr, tg, tb, ta = L['rgba']
        tint = (tr, tg, tb, ta) != (255, 255, 255, 255)
        sx, sy = abs(L['sx']) or 1.0, abs(L['sy']) or 1.0
        fast = L['angle'] % 360 == 0 and sx == 1.0 and sy == 1.0 and L['x'] == int(L['x']) and L['y'] == int(L['y'])
        ca, sa = math.cos(math.radians(L['angle'])), math.sin(math.radians(L['angle']))
        for oy in range(box[1], box[3]):
            for ox in range(box[0], box[2]):
                if fast:
                    ix, iy = ox - box[0], oy - box[1]
                else:
                    # inverse map the output pixel centre into the layer image
                    dx, dy = ox + 0.5 - L['x'], oy + 0.5 - L['y']
                    ux = (dx * ca + dy * sa) / sx
                    uy = (-dx * sa + dy * ca) / sy
                    ix, iy = int(math.floor(ux + w / 2)), int(math.floor(uy + h / 2))
                    if ix < 0 or iy < 0 or ix >= w or iy >= h:
                        continue
                if L['mirror']:
                    ix = w - 1 - ix
                s = (iy * w + ix) * 4
                r, g, b, a = px[s], px[s + 1], px[s + 2], px[s + 3]
                if tint:
                    r, g, b, a = r * tr // 255, g * tg // 255, b * tb // 255, a * ta // 255
                if a == 0:
                    continue
                d = ((oy - top) * W + (ox - left)) * 4
                if a == 255 or out[d + 3] == 0:
                    out[d:d + 4] = bytes((r, g, b, a))
                else:
                    da = out[d + 3]
                    oa = a + da * (255 - a) // 255
                    for k, c in enumerate((r, g, b)):
                        out[d + k] = (c * a + out[d + k] * da * (255 - a) // 255) // max(1, oa)
                    out[d + 3] = oa
    return (left, top, W, H, bytes(out))


# ---------------------------------------------------------------- atlas packing
def pack_atlas(sizes, max_page=MAX_PAGE, pad=1):
    """Shelf packer. sizes: [(w, h)] -> (pages=[(W, H)], places=[(page, x, y)]) with y from the TOP. Pages are power-of-two squares, the last page
    is shrunk to the smallest power of two that fits, then its height halved while everything still fits (W x W/2, W/4 ... down to 64; power-of-two sides). A `pad` px transparent gap keeps point / bilinear sampling from bleeding."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0], i))
    places = [None] * len(sizes)
    for i, (w, h) in enumerate(sizes):
        if w + pad > max_page or h + pad > max_page:
            raise FormatError('sprite frame %dx%d larger than a %d page' % (w, h, max_page))
    pages = []
    todo = [i for i in order if sizes[i][0] > 0 and sizes[i][1] > 0]
    for i in order:
        if sizes[i][0] == 0 or sizes[i][1] == 0:
            places[i] = (0, 0, 0)
    page = 0
    while todo:
        # try growing sizes for this page; the first that fits everything left wins, else fill a max page
        for size in (64, 128, 256, 512, 1024, 2048, 4096):
            if size > max_page:
                size = max_page
            placed, rest = _shelf(todo, sizes, size, pad)
            if not rest or size >= max_page:
                break
        if not placed:
            raise FormatError('atlas packing made no progress')
        H = size
        if not rest:
            # last page: halve the height while everything still fits (a 2048 x 1024 page is half the RGBA32 bytes of a square)
            while H > 64:
                p2, r2 = _shelf(todo, sizes, size, pad, H // 2)
                if r2:
                    break
                placed, H = p2, H // 2
        for i, x, y in placed:
            places[i] = (page, x, y)
        pages.append((size, H))
        todo = rest
        page += 1
    return pages, places


def _shelf(todo, sizes, size, pad, height=None):
    height = size if height is None else height
    placed, rest = [], []
    x = y = shelf_h = 0
    for i in todo:
        w, h = sizes[i]
        if w + pad > size:                     # wider than this page size: never place it here (it would run past the right edge)
            rest.append(i)
            continue
        if x + w + pad > size:
            x, y, shelf_h = 0, y + shelf_h, 0
        if y + h + pad > height:
            rest.append(i)
            continue
        placed.append((i, x, y))
        x += w + pad
        shelf_h = max(shelf_h, h + pad)
    return placed, rest


# ---------------------------------------------------------------- building a sprite set
def shift_for(w, h, max_side):
    """w34: the FF_SHIFT of a composite of w x h px under a max side (0 = keep; at most MAX_SHIFT)."""
    if not max_side:
        return 0
    s, m = 0, max(w, h)
    while (m >> s) > max_side and s < MAX_SHIFT:
        s += 1
    return s


def convert_sprite(name, kind, spr_bytes, act_bytes, anims=None, still_anims=None, max_side=None):
    """One sprite (SPR + ACT) -> dict(name, kind, clips=[dict(delay_ms, frames=[dict(img_key, left, top, w, h, anchor, shift)])], images={key: rgba}).
    anims: anim ids to keep (default all); the clip table always spans 8 * (max kept anim + 1) clips, skipped anims are empty clips.
    still_anims: anims reduced to their first frame (player idle / sit: the extra frames are the head-turn poses, not an animation).
    max_side (w34): a composite with a side above it is composed again at 1 / 2^shift (FF_SHIFT; left / top / w / h then in shifted px)."""
    spr = parse_spr(spr_bytes)
    act = parse_act(act_bytes)
    n_anim = (len(act['actions']) + 7) // 8
    keep = set(range(n_anim)) if anims is None else set(a for a in anims if a < n_anim)
    if still_anims is None:
        still_anims = (ANIM_IDLE, ANIM_SIT) if kind in (KIND_BODY, KIND_HEAD) else ()
    top_anim = max(keep) + 1 if keep else 0
    images = {}
    cache = {}
    clips = []
    for a in range(top_anim * 8):
        anim = a // 8
        frames_out = []
        delay = act['delays'][a] if a < len(act['delays']) else DEFAULT_DELAY_TICKS * TICK_MS
        if anim in keep and a < len(act['actions']):
            frames = act['actions'][a]
            if anim in still_anims:
                frames = frames[:1]
            for fr in frames:
                left, top, w, h, px = compose_frame(spr, fr)
                sh = shift_for(w, h, max_side)
                if sh:
                    left, top, w, h, px = compose_frame(spr, fr, 1.0 / (1 << sh))
                key = None
                if w and h:
                    key = (w, h, zlib.crc32(px), len(px))
                    if key not in cache:
                        cache[key] = px
                        images[key] = px
                anchor = fr['anchors'][0] if fr['anchors'] else None
                frames_out.append(dict(img_key=key, left=left, top=top, w=w, h=h, anchor=anchor, shift=sh))
        clips.append(dict(delay_ms=max(1, min(65535, int(round(delay)))) if frames_out else 0, frames=frames_out))
    if kind == KIND_OTHER:          # w34: an item on the ground has ONE direction in its ACT: every empty direction shows direction 0 (the runtime picks the clip by viewer angle)
        for a in range(0, len(clips), 8):
            for d in range(1, 8):
                if a + d < len(clips) and not clips[a + d]['frames'] and clips[a]['frames']:
                    clips[a + d] = dict(delay_ms=clips[a]['delay_ms'], frames=list(clips[a]['frames']))
    return dict(name=name, kind=kind, clips=clips, images=images)


def ref_height_px(body, head):
    """Pixel height of the standing character (feet origin to the top of the head) from idle-south frame 0 of body + head; 0 when unknown."""
    try:
        b = body['clips'][ANIM_IDLE * 8]['frames'][0]
    except (IndexError, KeyError):
        return 0.0
    top = b['top']
    if head is not None:
        try:
            h = head['clips'][ANIM_IDLE * 8]['frames'][0]
            dx = dy = 0
            if b['anchor'] is not None and h['anchor'] is not None:
                dy = b['anchor'][1] - h['anchor'][1]
            top = min(top, h['top'] + dy)
        except (IndexError, KeyError):
            pass
    return float(max(0, -top))


def build_set(sprites, max_page=MAX_PAGE, ref=None, ref_px=None):
    """sprites: [convert_sprite(...)] -> (rsat bytes, rsan bytes). ref = (body, head) sprite dicts to measure the character height with
    (default: the first body + the first head). ref_px (w34): the reference height in px given directly (a monster-only set of a field pack carries the
    Novice height of the same client, so the world scales its monsters exactly like the player sprites)."""
    keys = []
    seen = {}
    for s in sprites:
        for k in s['images']:
            if k not in seen:
                seen[k] = len(keys)
                keys.append((k, s['images'][k]))
    pages, places = pack_atlas([(k[0], k[1]) for k, _ in keys], max_page=max_page)
    bufs = [bytearray(W * H * 4) for (W, H) in pages]
    for (k, px), (pg, x, y) in zip(keys, places):
        w, h = k[0], k[1]
        W, H = pages[pg]
        for r in range(h):
            row = H - 1 - (y + r)                 # rows bottom first (Unity raw order)
            bufs[pg][(row * W + x) * 4:(row * W + x + w) * 4] = px[r * w * 4:(r + 1) * w * 4]
    # RSAT
    head = struct.pack('<I', len(pages))
    recs = bytearray()
    data = bytearray()
    base = 4 + RSAT_PAGE.size * len(pages)
    for (W, H), b in zip(pages, bufs):
        recs += RSAT_PAGE.pack(W, H, FMT_RGBA32, base + len(data), len(b))
        data += b
    rsat = head + bytes(recs) + bytes(data)
    # RSAN
    if ref is None:
        body = next((s for s in sprites if s['kind'] == KIND_BODY), None)
        hd = next((s for s in sprites if s['kind'] == KIND_HEAD), None)
    else:
        body, hd = ref
    refh = ref_height_px(body, hd) if body is not None else 0.0
    if ref_px is not None:
        refh = float(ref_px)
    srecs, crecs, frecs, names = bytearray(), bytearray(), bytearray(), bytearray()
    nclip = nframe = 0
    for s in sprites:
        nm = s['name'].encode('utf-8')
        sf = sum(len(c['frames']) for c in s['clips'])
        srecs += RSAN_SPRITE.pack(len(names), len(nm), s['kind'], nclip, len(s['clips']), sf)
        names += nm
        for c in s['clips']:
            crecs += RSAN_CLIP.pack(nframe, len(c['frames']), c['delay_ms'])
            nclip += 1
            for f in c['frames']:
                flags = 0
                if f['img_key'] is None:
                    pg = x = y = 0
                    flags |= FF_EMPTY
                else:
                    pg, x, y = places[seen[f['img_key']]]
                ax = ay = 0
                if f['anchor'] is not None:
                    flags |= FF_ANCHOR
                    ax, ay = f['anchor']
                flags |= (f.get('shift', 0) & 15) << FF_SHIFT_POS
                frecs += RSAN_FRAME.pack(pg, flags, x, y, f['w'], f['h'], _i16(f['left']), _i16(f['top']), _i16(ax), _i16(ay))
                nframe += 1
    off_s = RSAN_HDR.size
    names_off = off_s + len(srecs) + len(crecs) + len(frecs)
    rsan = RSAN_HDR.pack(RSAN_VERSION, len(sprites), nclip, nframe, len(pages), refh, names_off, 0, 0) + bytes(srecs) + bytes(crecs) + bytes(frecs) + P._pad4(bytes(names))
    return rsat, rsan


def _i16(v):
    if v < -32768 or v > 32767:
        raise FormatError('sprite offset %d outside int16' % v)
    return v


def write_set_pack(sprites, **kw):
    rsat, rsan = build_set(sprites, **kw)
    return P.write_pack([(TAG_RSAT, 1, 1, rsat), (TAG_RSAN, RSAN_VERSION, len(sprites), rsan)])


# ---------------------------------------------------------------- reading back (checker / tests)
def dec_rsan(d):
    v, ns, nc, nf, npg, refh, names_off, _r0, _r1 = RSAN_HDR.unpack_from(d, 0)
    if v != RSAN_VERSION:
        raise FormatError('RSAN version %d' % v)
    o = RSAN_HDR.size
    sprites = []
    for i in range(ns):
        no, nl, kind, fc, cc, fcount = RSAN_SPRITE.unpack_from(d, o + i * RSAN_SPRITE.size)
        sprites.append(dict(name=bytes(d[names_off + no:names_off + no + nl]).decode('utf-8'), kind=kind, first_clip=fc, clips=cc, frames=fcount))
    o += ns * RSAN_SPRITE.size
    clips = [RSAN_CLIP.unpack_from(d, o + i * RSAN_CLIP.size) for i in range(nc)]
    o += nc * RSAN_CLIP.size
    frames = [RSAN_FRAME.unpack_from(d, o + i * RSAN_FRAME.size) for i in range(nf)]
    return dict(ref_height_px=refh, pages=npg, sprites=sprites, clips=clips, frames=frames)


def dec_rsat(d):
    n = struct.unpack_from('<I', d, 0)[0]
    pages = []
    for i in range(n):
        w, h, fmt, off, size = RSAT_PAGE.unpack_from(d, 4 + i * RSAT_PAGE.size)
        if fmt != FMT_RGBA32 or size != w * h * 4 or off + size > len(d):
            raise FormatError('bad RSAT page %d' % i)
        pages.append((w, h, bytes(d[off:off + size])))
    return pages


# ---------------------------------------------------------------- synthetic writers (test fixtures only)
def write_spr(pal_frames, rgba_frames=(), palette=None, version=0x0201):
    """pal_frames [(w, h, idx bytes)], rgba_frames [(w, h, RGBA top first)] -> SPR bytes (used to make SYNTHETIC fixtures)."""
    out = bytearray(b'SP' + bytes((version & 0xFF, version >> 8)))
    out += struct.pack('<H', len(pal_frames))
    if version >= 0x0200:
        out += struct.pack('<H', len(rgba_frames))
    for w, h, idx in pal_frames:
        out += struct.pack('<2H', w, h)
        if version >= 0x0201:
            enc = rle_encode(idx)
            out += struct.pack('<H', len(enc)) + enc
        else:
            out += bytes(idx)
    if version >= 0x0200:
        for w, h, px in rgba_frames:
            out += struct.pack('<2H', w, h)
            for y in range(h - 1, -1, -1):
                for x in range(w):
                    r, g, b, a = px[(y * w + x) * 4:(y * w + x) * 4 + 4]
                    out += bytes((a, b, g, r))
    if version >= 0x0101:
        pal = palette or [(i, i, i) for i in range(256)]
        for r, g, b in pal:
            out += bytes((r, g, b, 0))
    return bytes(out)


def write_act(actions, delays=None, events=(), version=0x0205):
    """actions [[dict(layers=[dict(x, y, spr, mirror, rgba, sx, sy, angle, type)], event, anchors=[(x, y)])]] -> ACT bytes (SYNTHETIC fixtures)."""
    out = bytearray(b'AC' + bytes((version & 0xFF, version >> 8)))
    out += struct.pack('<H', len(actions)) + b'\0' * 10
    for frames in actions:
        out += struct.pack('<I', len(frames))
        for fr in frames:
            out += b'\0' * 32
            out += struct.pack('<I', len(fr['layers']))
            for L in fr['layers']:
                out += struct.pack('<3iI', L['x'], L['y'], L['spr'], 1 if L.get('mirror') else 0)
                if version >= 0x0200:
                    out += bytes(L.get('rgba', (255, 255, 255, 255)))
                    out += struct.pack('<f', L.get('sx', 1.0))
                    if version >= 0x0204:
                        out += struct.pack('<f', L.get('sy', L.get('sx', 1.0)))
                    out += struct.pack('<2i', L.get('angle', 0), L.get('type', 0))
                    if version >= 0x0205:
                        out += struct.pack('<2i', 0, 0)
            if version >= 0x0200:
                out += struct.pack('<i', fr.get('event', -1))
            if version >= 0x0203:
                anc = fr.get('anchors', [])
                out += struct.pack('<I', len(anc))
                for ax, ay in anc:
                    out += struct.pack('<4i', 0, ax, ay, 0)
    if version >= 0x0201:
        out += struct.pack('<I', len(events))
        for e in events:
            out += e[:39].ljust(40, b'\0')
    if version >= 0x0202:
        ds = delays or [DEFAULT_DELAY_TICKS] * len(actions)
        for d in ds:
            out += struct.pack('<f', d)
    return bytes(out)


# ---------------------------------------------------------------- companion entry
def build_player_set(read, sex='m', monsters=(('poring', PATH_PORING),)):
    """read(unicode path) -> bytes (the GRF lane's reader, it encodes the name as CP949). Builds body + head (anims idle / walk) and the monsters
    (idle / walk / attack / hurt / dead) -> RPG1 pack bytes."""
    sp = [convert_sprite('body_' + sex, KIND_BODY, read(PATH_BODY[sex] + '.spr'), read(PATH_BODY[sex] + '.act'), anims=(ANIM_IDLE, ANIM_WALK)),
          convert_sprite('head_' + sex, KIND_HEAD, read(PATH_HEAD[sex] + '.spr'), read(PATH_HEAD[sex] + '.act'), anims=(ANIM_IDLE, ANIM_WALK))]
    for nm, path in monsters:
        sp.append(convert_sprite(nm, KIND_MONSTER, read(path + '.spr'), read(path + '.act')))
    return write_set_pack(sp)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    sprites = []
    for spec in argv[1:]:
        name, kind, sprp, actp = spec.split(':', 3)
        with open(sprp, 'rb') as f:
            sb = f.read()
        with open(actp, 'rb') as f:
            ab = f.read()
        anims = (ANIM_IDLE, ANIM_WALK) if kind in ('body', 'head') else None
        sprites.append(convert_sprite(name, KINDS[kind], sb, ab, anims=anims))
    pack = write_set_pack(sprites)
    with open(argv[0], 'wb') as f:
        f.write(pack)
    rs = dec_rsan(P.read_pack(pack)['sections'][TAG_RSAN]['data'])
    print('%s: %d bytes, %d sprites, %d clips, %d frames, %d pages, ref height %.0f px' % (argv[0], len(pack), len(rs['sprites']), len(rs['clips']),
                                                                                     len(rs['frames']), rs['pages'], rs['ref_height_px']))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
