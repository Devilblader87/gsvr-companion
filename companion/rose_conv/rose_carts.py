"""ROSE carts / castle gear (the player vehicles, LIST_PAT) for the shared avatar pack: the part meshes as ACAR items (optional section next to AVAT).

  rose_avatar_pack.build() calls add_cart_items(cb, genders): for every LIST_PAT row with a string id and a mesh it builds one AVAT-format item per gender in slot 10 + part
  (10 body, 11 engine, 12 leg / wheels, 13 arms; item number = LIST_PAT row = ZSC object index of 3DDATA/PAT/LIST_PAT.ZSC, same rule as every other item).

Why AVAT-style items: the runtime already draws the avatar outfit as a list of rigid / skinned meshes per slot (GsvrRpg1Actors.BuildAvatarParts, the co-op gear sync puts them on remote
avatars with EquipItemOn). A cart part is one more slot of that list, so "the cart" = the four part items of the vehicle worn together.

Geometry (first slice, no cart skeleton in the pack): the part ZMS files are skinned to the vehicle skeleton (CART01.ZMD / CASTLEGEAR02.ZMD) and hold bind-pose model-space positions.
They are converted with the vehicle skeleton (rosechar.convert_mesh), moved so that the vehicle's driver dummy (ZMD dummy 0 = the seat the client parents the driver to, model_loader.rs
spawn_vehicle_model: `driver_dummy_entity: skinned_mesh.joints[dummy_bone_offset]`) sits under the avatar's root joint horizontally (x, z; the height is kept so the wheels touch the ground),
and are then attached RIGIDLY to the avatar skeleton's joint 0 (CMF_RIGID, one palette entry, weight 255). The vehicle does not animate in this slice (wheel spin, castle gear walk
cycle = plan: needs a second skeleton + clips in the pack).

The ITEM stats of the PAT rows (speed, fuel, attack) travel in the ITEM section (rose_items.py, type 14); this module only carries the meshes. Client files are only read.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosefmt as F, rosechar as C, rpg1 as P

PAT_ZSC = '3DDATA/PAT/LIST_PAT.ZSC'
PAT_STB = 'LIST_PAT'
CART_ZMD = '3DDATA/PAT/CART/CART01.ZMD'
GEAR_ZMD = '3DDATA/PAT/CASTLEGEAR/CASTLEGEAR02/CASTLEGEAR02.ZMD'
SLOT_CART_BODY, SLOT_CART_ENGINE, SLOT_CART_LEG, SLOT_CART_ARMS = 10, 11, 12, 13
# LIST_PAT cell 4 (item class) -> (part, castle gear?). 513 = animal body: no part model data in the pack (not supported)
CLASS_PART = {511: (0, False), 512: (0, True), 521: (1, False), 522: (1, True), 531: (2, False), 532: (2, True), 551: (3, False), 552: (3, True)}


def part_rows(vfs):
    """LIST_PAT rows that are real items -> [(row, part 0..3, gear?)], sorted by row; empty when the table is missing"""
    try:
        stb = F.stb(vfs.read('3DDATA/STB/%s.STB' % PAT_STB))
    except KeyError:
        return []
    out = []
    for n in range(1, stb['rows']):
        c = stb['cells'][n]
        if not (c and c[-1].strip()):
            continue
        cls = c[4].strip() if len(c) > 4 else ''
        if cls.isdigit() and int(cls) in CLASS_PART:
            part, gear = CLASS_PART[int(cls)]
            out.append((n, part, gear))
    return out


def _skeleton(vfs, path):
    return C.build_skeleton(F.zmd(vfs.read(path)), name=os.path.basename(path))


def seat_offset(vehicle_skel, avatar_skel):
    """(dx, dz) that moves the vehicle's driver dummy (dummy 0, pack axes) under the avatar's root joint; (0, 0) without a dummy"""
    nreal = vehicle_skel['nreal']
    if len(vehicle_skel['joints']) <= nreal:
        return 0.0, 0.0
    s = vehicle_skel['joints'][nreal]['gpos']
    a = avatar_skel['joints'][0]['gpos']
    return a[0] - s[0], a[2] - s[2]


def bake_mesh(z, vehicle_skel, avatar_skel_idx, shift):
    """one skinned vehicle ZMS -> rigid CMSH mesh dict on the avatar skeleton's joint 0 (bind pose, model space of the avatar skeleton)"""
    m = C.convert_mesh(z, vehicle_skel)                       # skinned in the vehicle's own skeleton, pack axes
    dx, dz = shift
    verts = [(v[0] + dx, v[1], v[2] + dz) + tuple(v[3:]) for v in m['verts']]
    n = len(verts)
    lo = tuple(min(v[i] for v in verts) for i in range(3))
    hi = tuple(max(v[i] for v in verts) for i in range(3))
    return dict(verts=verts, bi=[(0, 0, 0, 0)] * n, bw=[(255, 0, 0, 0)] * n, idx=m['idx'], palette=[0], bbmin=lo, bbmax=hi,
                skeleton=avatar_skel_idx, flags=P.CMF_RIGID)


def add_cart_items(cb, genders=(0, 1)):
    """builds the cart / castle gear part items (AVAT item dicts, slots 10..13, sorted) for the optional ACAR section; the meshes go into the shared CMSH of `cb`. They are NOT added to cb.avatar: a runtime that does not know carts
refuses a shared AVAT with slots beyond its SlotCount and would drop the whole pack, so the items live in their own section that such a runtime ignores. -> list of item dicts (empty without a LIST_PAT table)"""
    vfs = cb.vfs
    rows = part_rows(vfs)
    zsc = cb.zsc(PAT_ZSC) if rows else None
    if not rows or zsc is None:
        return []
    try:
        vskel = {False: _skeleton(vfs, CART_ZMD), True: _skeleton(vfs, GEAR_ZMD)}
    except KeyError:
        cb.warn['cart skeleton missing'] += 1
        return []
    items = []
    cache = {}
    for g in genders:
        askel = cb.skeleton('3DDATA/AVATAR/%s.ZMD' % ('MALE' if g == 0 else 'FEMALE'))
        shifts = dict((gear, seat_offset(vskel[gear], cb.skels[askel])) for gear in (False, True))
        for row, part, gear in rows:
            if row >= len(zsc['objs']):
                cb.warn['cart row outside the ZSC'] += 1
                continue
            ids = []
            for mi, ma, pr in zsc['objs'][row][0]:
                path = zsc['meshes'][mi]
                z = cb.ob.get_zms(path)
                if z is None or ma >= len(zsc['mats']) or 'skin' not in z:
                    cb.warn['cart part without skinned mesh'] += 1
                    continue
                key = (path.replace('\\', '/').upper(), askel, zsc['mats'][ma][0], gear)
                if key in cache:
                    ids.append(cache[key])
                    continue
                cb.ob.compress_raw = True
                try:
                    mat = cb.ob.get_mat(zsc, ma)
                finally:
                    cb.ob.compress_raw = False
                try:
                    m = bake_mesh(z, vskel[gear], askel, shifts[gear])
                except Exception as e:
                    cb.warn['cart mesh: %s' % e.__class__.__name__] += 1
                    print('  warn: cart mesh %s: %s' % (path, e), file=sys.stderr)
                    continue
                m.update(material=mat, name=os.path.basename(path.replace('\\', '/')))
                cb.mat_ids.add(mat)
                cb.meshes.append(m)
                cache[key] = len(cb.meshes) - 1
                ids.append(cache[key])
            if not ids:
                cb.warn['cart item without mesh'] += 1
                continue
            items.append(dict(gender=g, slot=10 + part, number=row, meshes=ids, attach=None, starter=False))
    items.sort(key=lambda it: (it['gender'], it['slot'], it['number']))
    return items
