"""Ragnarok Online IMF reader (draw order of the character layers; stdlib only). Re-implemented from the public format notes (Ragnarok Research Lab "IMF"),
no code copied; checked against the owner's client (the layer table of every job is 2 layers: 0 body, 1 head).

Layout (little endian): f32 version (1.01), i32 checksum, i32 max layer, then for every layer 0..max: i32 action count; per action: i32 frame count; per frame:
i32 priority, i32 cx, i32 cy. Actions = ACT actions (anim * 8 + dir). A LOWER priority value is drawn nearer to the viewer."""
import struct

LAYER_BODY, LAYER_HEAD = 0, 1
MAX_LAYERS = 8
MAX_ACTIONS = 4096
MAX_FRAMES = 4096


def parse(buf):
    """-> {'version', 'layers': [[[(priority, cx, cy) per frame] per action] per layer]}. ValueError on a malformed file."""
    if buf is None or len(buf) < 12:
        raise ValueError('imf too short')
    ver, _chk, maxl = struct.unpack_from('<fii', buf, 0)
    if maxl < 0 or maxl >= MAX_LAYERS:
        raise ValueError('imf layer count %d' % maxl)
    p = 12
    layers = []
    for _l in range(maxl + 1):
        if p + 4 > len(buf):
            raise ValueError('imf truncated')
        na, = struct.unpack_from('<i', buf, p)
        p += 4
        if na < 0 or na > MAX_ACTIONS:
            raise ValueError('imf action count %d' % na)
        acts = []
        for _a in range(na):
            if p + 4 > len(buf):
                raise ValueError('imf truncated')
            nf, = struct.unpack_from('<i', buf, p)
            p += 4
            if nf < 0 or nf > MAX_FRAMES or p + nf * 12 > len(buf):
                raise ValueError('imf frame count %d' % nf)
            fr = []
            for _f in range(nf):
                fr.append(struct.unpack_from('<iii', buf, p))
                p += 12
            acts.append(fr)
        layers.append(acts)
    return dict(version=ver, layers=layers)


def priority(im, layer, action, frame):
    """priority of a layer in an action frame; None when the file does not have it."""
    try:
        return im['layers'][layer][action][frame][0]
    except (IndexError, KeyError):
        return None


def head_behind(im, action, frame):
    """True when the head layer has a strictly HIGHER priority value than the body layer in that frame (drawn behind it). Equal / missing = in front."""
    b = priority(im, LAYER_BODY, action, frame)
    h = priority(im, LAYER_HEAD, action, frame)
    return b is not None and h is not None and h > b


def write(layers, version=1.01):
    """layers [[[(priority, cx, cy)] per action] per layer] -> IMF bytes (SYNTHETIC fixtures for the tests)."""
    out = bytearray(struct.pack('<fii', version, 0, len(layers) - 1))
    for acts in layers:
        out += struct.pack('<i', len(acts))
        for fr in acts:
            out += struct.pack('<i', len(fr))
            for pr, cx, cy in fr:
                out += struct.pack('<iii', pr, cx, cy)
    return bytes(out)
