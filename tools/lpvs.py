# Landmark PVS for the GSV3 map pack: section "LPVS" (T3 level transition stage 4, docs/design/t3_transition4.md).
#
# What the original does: CChangeLevel::ChangeList asks the engine for the entities in the PVS of each landmark
# (pfnEntitiesInPVS(pentLandmark), CChangeLevel.c:765). The engine (ReHLDS pr_cmds.cpp PVSFindEntities, a reverse-engineered
# GoldSrc engine) takes the leaf of the landmark origin (Mod_PointInLeaf), its decompressed vis row (Mod_LeafPVS), marks every
# visible leaf and all its parent nodes (PVSMark), and then keeps an entity when its absmin/absmax box reaches a MARKED,
# NON-SOLID leaf through MARKED nodes (PVSNode: box against the split plane, both sides when the box straddles it).
#
# The runtime has the hull-0 node tree already (HNOD nodes [0, H0) are the BSP nodes 1:1, leaf children replaced by contents),
# so the pack only needs the marks: per landmark two bits per hull-0 node, one per child slot:
#   bit 2n+s set  <=>  child s of node n is a node that PVSMark marks, or a leaf that is visible and not CONTENTS_SOLID.
# PVSNode(root) on that is exactly the engine's walk (the root itself is marked iff any bit is set).
#
# Section layout (all int32, little endian):
#   "LPVS" count L (landmarks), size 16 + L * (16 + 4 W):
#     header  H0 (hull-0 node count the bits refer to), W (words per landmark = ceil(2 H0 / 32)), headnode (world hull 0), 0
#     L records { entityIndex (ENTS index of the info_landmark), flags (1 = all visible: no vis data / solid leaf / visofs -1,
#                 2 = no leaf visible), leaf (BSP leaf index of the landmark origin), visibleLeaves (count), uint32 bits[W] }
# Older loaders skip the unknown tag.
import struct

CONTENTS_SOLID = -2
LPVS_ALL = 1
LPVS_NONE = 2


def _lump(b, name):
    off, ln = b.lumps[name]
    return b.data[off:off + ln]


def bsp_vis(b):
    """-> (leaves [(contents, visofs)], visdata bytes, world visleafs, world headnode0, nodes [(plane, c0, c1)])."""
    leaves = [(l[0], l[1]) for l in struct.iter_unpack("<ii6h2H4B", _lump(b, "leaves"))]
    model0 = b.models[0]
    return leaves, _lump(b, "visibility"), model0[13], model0[9], b.nodes


def decompress_vis(vis, ofs, visleafs):
    """Mod_DecompressVis: row of (visleafs + 7) >> 3 bytes; non-zero bytes copied, a zero byte is followed by a run length of zero bytes.
    ofs < 0 or no vis data -> every leaf visible (0xff row)."""
    row = (visleafs + 7) >> 3
    if ofs < 0 or not vis or ofs >= len(vis):
        return bytes([0xff] * row)
    out = bytearray()
    i = ofs
    while len(out) < row and i < len(vis):
        c = vis[i]
        if c:
            out.append(c)
            i += 1
            continue
        n = vis[i + 1] if i + 1 < len(vis) else 0
        i += 2
        out += b"\0" * n
    out = out[:row]
    out += b"\0" * (row - len(out))
    return bytes(out)


def point_leaf(nodes, planes, head, p):
    """Mod_PointInLeaf: d > 0 -> front child, else back; -> BSP leaf index (0 = the solid leaf)."""
    n = head
    guard = 0
    while n >= 0 and guard < 65536:
        pl, a, bb = nodes[n]
        nx, ny, nz, d = planes[pl][:4]
        n = a if nx * p[0] + ny * p[1] + nz * p[2] - d > 0 else bb
        guard += 1
    return -(n + 1)


def visible_leaves(leaves, vis, visleafs, leaf):
    """Mod_LeafPVS + the PVSMark loop: set of leaf indices j (1..visleafs) whose bit j-1 is set. Leaf 0 (solid) -> all (mod_novis)."""
    ofs = -1 if leaf <= 0 or leaf >= len(leaves) else leaves[leaf][1]
    allvis = leaf <= 0 or ofs < 0 or not vis
    row = bytes([0xff] * ((visleafs + 7) >> 3)) if allvis else decompress_vis(vis, ofs, visleafs)
    out = set()
    for i in range(visleafs):
        if row[i >> 3] & (1 << (i & 7)):
            out.add(i + 1)
    return out, allvis


def slot_bits(nodes, leaves, head, vis_set):
    """The 2-bit-per-node marks (see the header). Returns a list of 2 * len(nodes) booleans."""
    H0 = len(nodes)
    marked = [False] * H0
    # post-order over the tree from head: a node is marked when any leaf below it is visible (PVSMark walks every parent)
    if head < 0:
        return [False] * (2 * H0)
    order, stack, seen = [], [head], set()
    while stack:
        n = stack.pop()
        if n in seen or n < 0 or n >= H0:
            continue
        seen.add(n)
        order.append(n)
        for c in nodes[n][1:3]:
            if c >= 0:
                stack.append(c)
    for n in reversed(order):            # children before parents (a child is always pushed after its parent was appended)
        m = False
        for c in nodes[n][1:3]:
            if c >= 0:
                m = m or marked[c]
            else:
                m = m or (-(c + 1)) in vis_set
        marked[n] = m
    bits = [False] * (2 * H0)
    for n in order:
        for s in (0, 1):
            c = nodes[n][1 + s]
            if c >= 0:
                bits[2 * n + s] = marked[c]
            else:
                lf = -(c + 1)
                bits[2 * n + s] = lf in vis_set and 0 <= lf < len(leaves) and leaves[lf][0] != CONTENTS_SOLID
    return bits


def pack_words(bits):
    w = (len(bits) + 31) // 32
    words = [0] * w
    for i, v in enumerate(bits):
        if v:
            words[i >> 5] |= 1 << (i & 31)
    return [x - (1 << 32) if x >= 1 << 31 else x for x in words]       # signed int32 for struct "i"


def box_in_pvs(nodes, planes, head, bits, mins, maxs):
    """PVSNode on the packed marks (reference for the Udon port GsvrEntHull.PvsBox): True when the box reaches a marked, non-solid leaf."""
    if head < 0 or not any(bits):
        return False
    stack = [head]
    while stack:
        n = stack.pop()
        pl, a, bb = nodes[n]
        nx, ny, nz, d = planes[pl][:4]
        sides = box_sides(nx, ny, nz, d, mins, maxs)
        # the engine visits the front child first and returns on the first hit; the order does not change the boolean
        for s, c in ((1, bb), (0, a)):
            if not sides & (1 << s) or not bits[2 * n + s]:
                continue
            if c < 0:
                return True
            stack.append(c)
    return False


def box_sides(nx, ny, nz, d, mins, maxs):
    """PVSNode's side test: an axial plane (normal = +x / +y / +z) uses the engine's shortcut (dist > mins -> 3 when dist < maxs else 2,
    otherwise 1); any other plane BoxOnPlaneSide (1 = the far corner is on or in front, 2 = the near corner is behind)."""
    k = 0 if (nx == 1.0 and ny == 0.0 and nz == 0.0) else 1 if (nx == 0.0 and ny == 1.0 and nz == 0.0) else 2 if (nx == 0.0 and ny == 0.0 and nz == 1.0) else -1
    if k >= 0:
        if d > mins[k]:
            return 3 if d < maxs[k] else 2
        return 1
    d1 = nx * (maxs[0] if nx >= 0 else mins[0]) + ny * (maxs[1] if ny >= 0 else mins[1]) + nz * (maxs[2] if nz >= 0 else mins[2])
    d2 = nx * (mins[0] if nx >= 0 else maxs[0]) + ny * (mins[1] if ny >= 0 else maxs[1]) + nz * (mins[2] if nz >= 0 else maxs[2])
    return (1 if d1 >= d else 0) | (2 if d2 < d else 0)


def landmarks(ents):
    """[(entity index, origin)] of every info_landmark (ENTS order = entity lump order)."""
    out = []
    for i, e in enumerate(ents):
        if e.get("classname") != "info_landmark":
            continue
        try:
            o = tuple(float(v) for v in e.get("origin", "0 0 0").split()[:3])
        except ValueError:
            o = (0.0, 0.0, 0.0)
        if len(o) != 3:
            o = (0.0, 0.0, 0.0)
        out.append((i, o))
    return out


def build(b, ents, log=print):
    """-> dict(h0, w, head, recs=[(ent, flags, leaf, nvis, words)]) or None when the map has no landmark / no tree."""
    leaves, vis, visleafs, head, nodes = bsp_vis(b)
    lms = landmarks(ents)
    if not lms or head < 0 or not nodes:
        return None
    H0 = len(nodes)
    W = (2 * H0 + 31) // 32
    recs = []
    for ei, o in lms:
        leaf = point_leaf(nodes, b.planes, head, o)
        vset, allvis = visible_leaves(leaves, vis, visleafs, leaf)
        bits = slot_bits(nodes, leaves, head, vset)
        flags = (LPVS_ALL if allvis else 0) | (0 if any(bits) else LPVS_NONE)
        recs.append((ei, flags, leaf, len(vset), pack_words(bits)))
    log(f"lpvs: {len(recs)} landmark(s), {H0} hull-0 nodes, {W * 4 + 16} bytes each, visible leaves "
        + " ".join(f"#{r[0]}:{r[3]}/{visleafs}{'(all)' if r[1] & LPVS_ALL else ''}" for r in recs))
    return dict(h0=H0, w=W, head=head, recs=recs)


def section(lp):
    """-> (blob, count) of the LPVS section."""
    blob = struct.pack("<4i", lp["h0"], lp["w"], lp["head"], 0)
    for ei, flags, leaf, nvis, words in lp["recs"]:
        assert len(words) == lp["w"]
        blob += struct.pack("<4i", ei, flags, leaf, nvis) + struct.pack(f"<{lp['w']}i", *words)
    return blob, len(lp["recs"])


def parse(blob, count):
    """Inverse of section() (tests; the Udon loader is GsvrPackLoader.ReadLandmarkPvs)."""
    h0, w, head, _ = struct.unpack_from("<4i", blob, 0)
    assert len(blob) == 16 + count * (16 + 4 * w), "LPVS size"
    recs, off = [], 16
    for _ in range(count):
        ei, flags, leaf, nvis = struct.unpack_from("<4i", blob, off)
        words = list(struct.unpack_from(f"<{w}i", blob, off + 16))
        recs.append((ei, flags, leaf, nvis, words))
        off += 16 + 4 * w
    return dict(h0=h0, w=w, head=head, recs=recs)


def unpack_bits(words, n):
    return [bool((words[i >> 5] >> (i & 31)) & 1) for i in range(n)]
