"""ROSE w27 CART ANIMATION (companion-owned, like rose_skillvis.py: the vendored converter files stay byte-identical to rose-vrchat). Owner MC: cart attacks + cart animation.
Design: docs/design/rose_carts.md section 7. Runtime twin: GsvrRpg1Actors.SetVehicle / VehClip, GsvrRpg1Play CartVisual / NetRemoteCart; offline rules tools/usharp_offline/rosew27cartrules.cs,
converter test companion/test_rose_cartvis.py (synthetic data only).

What the original does (rose-offline-client model_loader.rs spawn_vehicle_model, systems/vehicle_model_system.rs; rose-data character_motion_database.rs get_vehicle_action_motion):
  * the vehicle is its own skinned model on CART01.ZMD (cart) / CASTLEGEAR02.ZMD (castle gear); the four worn parts are skinned to it;
  * the driver's ROOT JOINT (skinned_mesh.joints[0]) is re-parented to the vehicle's dummy 0 (p_cart01 / P_01): the driver moves with the seat;
  * vehicle clip = TYPE_MOTION[(body base motion (LIST_PAT cell 40) + action) * columns + arms motion type (the ARMS row's cell 40, 0 without arms)] -> FILE_MOTION column 0;
    actions stop 0, move 1, attack 1..3 = 2..4 ("Carts only have a single attack animation": attack 2 / 3 play attack 1 on a cart);
  * driver clip = the same with the body's base AVATAR motion (cell 41) and motion type 0, FILE_MOTION column 0 (the male column for both genders, as the reference does).

How it is packed (no new section kind, no new runtime table):
  ZONE PACKS (every pack with the avatar, WRAPPER_REV 9): per gender and vehicle kind one EXTENDED skeleton = the avatar skeleton's joints (real + dummies, unchanged indices) followed by
  the vehicle skeleton's joints (parents shifted). The driver's re-parenting is baked into the clips: the merged clip's avatar root track = dummy 0 of the vehicle pose at that frame
  composed with the driver clip's root (pack space: dummy * FLIP^-1 * root, both roots carry the faces-+Z turn). Every other avatar joint plays the driver clip (looped), every vehicle
  joint the vehicle clip; the vehicle clip's frame events (the attack hit frame) are kept. Each merged clip is one SKMO row (rose_skillvis.py layout) keyed by its TYPE_MOTION row
  (base + action), gender, arms motion type and loop: stop / move loop, attacks once. A weapon-type row whose file is the type-0 file is not written (the runtime falls back like the
  original). SKMO-only clips are lazy unless BONT pre-bakes them (rose_skillvis SKILL_BONT_MAX), so they ride the existing budget.
  AVATAR PACK: the part meshes are SKINNED to the extended skeleton (palette = vehicle joint + avatar joint count; the extended skeletons are added to its SKEL so every palette entry is
  inside its mesh's skeleton) and the item table is written as section ACRS instead of ACAR. A runtime without this lane ignores ACRS (no vehicle look rather than a torn one); the new
  runtime draws ACRS parts only while the slot plays an extended-skeleton clip (GsvrRpg1Actors.VehicleOn) and still draws an old rigid ACAR as before.

No number or text of the game is committed: everything is read from the player's own client at conversion time.
"""
import functools
import os
import struct
import sys

import rosefmt as F
import rosechar as C
import rpg1 as P
import rose_chars as RC
import rose_carts as CARTS
import rose_skillvis as SV
import rose_bont

TAG_ACAR, TAG_ACRS = b'ACAR', b'ACRS'
BODY_CLASSES = {511: False, 512: True}          # cart body / castle gear body (rose_carts.CLASS_PART)
ARMS_CLASSES = {551: False, 552: True}
ACT_STOP, ACT_MOVE, ACT_ATTACK1 = 0, 1, 2
CART_ACTIONS = (ACT_STOP, ACT_MOVE, ACT_ATTACK1)             # the client maps a cart's attack 2 / 3 to attack 1
# BUDGET (measured on the 1.31 client, w27): the full castle gear set (5 actions x 5 arms motion types x 2 genders = 48 clips with the cart's) costs 1.1 MB CANM + 5.7 MB BONT in EVERY
# zone pack; the cart's 6 clips 95 KB + 0.44 MB. The castle gear (2 body rows, level-gated) therefore gets stop / walk / attack 1 of arms motion type 0 only (+ ~0.2 MB CANM, 0.8 MB BONT):
# attack 2 / 3 and the screw / wheel / cannon / spear arm variants fall back to those clips at runtime (SkmoClip: the weapon type's row, else type 0; VehClip: attack 2 / 3 -> attack 1).
GEAR_ACTIONS = (ACT_STOP, ACT_MOVE, ACT_ATTACK1)
GEAR_ALL_ARMS = False                            # True = every arms motion type of the table (the budget above)
LOOP_ACTIONS = (ACT_STOP, ACT_MOVE)
MAX_WT = 19                                      # TYPE_MOTION has 20 weapon columns
# SKELETON BUDGET (w27 zonefix, round r127): the runtime refuses a pack's WHOLE character set when SKEL holds more than GsvrRpg1Chars.MaxSkeletons (64) skeletons (ValidateSkel ->
# "characters rejected: bad section structure (SKEL)" -> the zone cannot load, the travel goes back). The summon lane fills a zone up to rose_skills.MAX_SUMMON_SKELETONS (60) npc
# skeletons before the avatar's 2 are added; the extended skeletons (2 genders x cart + castle gear = 4) came on top: Zant 59 + 2 + 4 = 65. So (1) the summon picker reserves the
# extended skeletons (install: rose_skills.pick_summon_actors capped at RUNTIME_MAX_SKELETONS - AVATAR_SKELETONS - EXT_SKELETONS) and (2) add_vehicle_clips / skinned_cart_items add
# extended skeletons only while SKEL stays <= RUNTIME_MAX_SKELETONS (else no vehicle clips in that pack: the cart parts stay off there, the zone still loads).
RUNTIME_MAX_SKELETONS = 64                       # GsvrRpg1Chars.MaxSkeletons (test_rose_cartvis pins the twin)
AVATAR_SKELETONS = 2                             # MALE.ZMD + FEMALE.ZMD (add_avatar)
EXT_SKELETONS = 4                                # genders (2) x vehicle kinds (cart, castle gear)
CELL_BASE, CELL_AVBASE = 40, 41


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
    except IndexError:
        return default
    return int(v) if v.lstrip('-').isdigit() else default


def vehicle_bases(pat):
    """LIST_PAT -> {gear?: (base motion, base avatar motion)} of the first named body row of each kind with a base motion; warnings for bodies of one kind that disagree"""
    out, warn = {}, []
    if pat is None:
        return out, warn
    for r in range(1, pat['rows']):
        c = pat['cells'][r]
        if not (c and str(c[-1]).strip()):
            continue
        cls = _int(c, 4)
        if cls not in BODY_CLASSES:
            continue
        b, a = _int(c, CELL_BASE), _int(c, CELL_AVBASE)
        if b <= 0:
            continue
        g = BODY_CLASSES[cls]
        if g not in out:
            out[g] = (b, a)
        elif out[g] != (b, a):
            warn.append('vehicle body row %d: base motion %d / %d differs from %d / %d' % (r, b, a, out[g][0], out[g][1]))
    return out, warn


def arms_types(pat):
    """LIST_PAT -> {gear?: sorted arms motion types (the arms rows' cell 40), always with 0}"""
    out = {False: {0}, True: {0}}
    if pat is None:
        return {k: sorted(v) for k, v in out.items()}
    for r in range(1, pat['rows']):
        c = pat['cells'][r]
        if not (c and str(c[-1]).strip()):
            continue
        cls = _int(c, 4)
        if cls in ARMS_CLASSES:
            t = _int(c, CELL_BASE)
            if 0 <= t <= MAX_WT:
                out[ARMS_CLASSES[cls]].add(t)
    return {k: sorted(v) for k, v in out.items()}


def ext_skeleton(av, veh, name):
    """avatar skeleton + vehicle skeleton -> one skeleton: the avatar's joints first (same indices, so every avatar mesh skins as before), then the vehicle's (parents + avatar count)"""
    na = len(av['joints'])
    joints = [dict(j) for j in av['joints']]
    for j in veh['joints']:
        k = dict(j)
        k['parent'] = j['parent'] + na if j['parent'] >= 0 else -1
        joints.append(k)
    C.recompute_globals(dict(joints=joints))
    return dict(name=name, nreal=av['nreal'], joints=joints, flags=av.get('flags', P.SKF_FACES_PLUS_Z), base=na)


def _compose(a, b):
    """(pos, rot) a then b: a * b"""
    r = C.qrot(a[1], b[0])
    return (a[0][0] + r[0], a[0][1] + r[1], a[0][2] + r[2]), C.qnorm(C.qmul(a[1], b[1]))


INV_FLIP = (C.qinv(C.FLIP_Q))


def seat_root(dummy, root):
    """the driver's root joint in pack space when it hangs under the vehicle dummy: dummy * FLIP^-1 * root (both are pack space; the root carries the faces-+Z turn once)"""
    return _compose(dummy, _compose(((0.0, 0.0, 0.0), INV_FLIP), root))


def merged_clip(av, veh, ext, ext_idx, vclip, dclip, name):
    """vehicle clip (on veh) + driver clip (on av) -> one clip dict on the extended skeleton (frames = the vehicle clip's, the driver clip loops)"""
    na = ext['base']
    n = vclip['frames']
    dn = max(1, dclip['frames'])
    dummy_j = veh['nreal']                       # dummy 0 = the driver seat (model_loader.rs: joints[dummy_bone_offset])
    if dummy_j >= len(veh['joints']):
        raise ValueError('vehicle skeleton has no driver dummy')
    dtracks = {t['joint'] for t in dclip['tracks']}
    roots = [i for i, j in enumerate(av['joints']) if j['parent'] < 0]
    # w27 review: a LOOPING row wraps after the vehicle clip's n frames; the driver clip (dn frames) is stretched to a whole number of its loops over n (nearest frame),
    # so the driver pose does not jump at the wrap when n is no multiple of dn (castle gear stop 81 / walk 53 against car_sit 21 frames). Play-once rows keep the driver's own rate.
    cyc = max(1, int(round(n / float(dn)))) if vclip['loop'] else 0
    tr = {}
    for k in range(n):
        vg = C.global_pose(veh, C.clip_local_pose(veh, vclip, k))
        dl = C.clip_local_pose(av, dclip, (int(round(k * cyc * dn / float(n))) if cyc else k) % dn)
        for i in sorted(dtracks | set(roots)):
            p, q, _s = dl[i]
            if i in roots:
                p, q = seat_root(vg[dummy_j], (p, q))
            e = tr.setdefault(i, dict(joint=i, pos=[], rot=[], scale=None))
            e['pos'].append(tuple(p)); e['rot'].append(tuple(q))
    for i, e in tr.items():
        rs = e['rot']
        for k in range(1, len(rs)):
            if sum(rs[k - 1][x] * rs[k][x] for x in range(4)) < 0:
                rs[k] = (-rs[k][0], -rs[k][1], -rs[k][2], -rs[k][3])
        if i not in roots and not any(t['joint'] == i and t['pos'] is not None for t in dclip['tracks']):
            e['pos'] = None
    tracks = [tr[i] for i in sorted(tr)]
    for t in vclip['tracks']:
        tracks.append(dict(joint=t['joint'] + na, pos=t['pos'], rot=t['rot'], scale=t['scale']))
    return dict(name=name, frames=n, fps=vclip['fps'], src_fps=vclip.get('src_fps', vclip['fps']), loop=vclip['loop'], events=list(vclip['events']), dropped=0,
                tracks=tracks, skeleton=ext_idx)


def _same(a, b):
    return a is not None and b is not None and RC._norm(a) == RC._norm(b)


def _pat(vfs):
    try:
        return F.stb(vfs.read('3DDATA/STB/%s.STB' % CARTS.PAT_STB))
    except KeyError:
        return None


def _veh_skeleton(vfs, gear):
    return C.build_skeleton(F.zmd(vfs.read(CARTS.GEAR_ZMD if gear else CARTS.CART_ZMD)), name=os.path.basename(CARTS.GEAR_ZMD if gear else CARTS.CART_ZMD))


def ext_index(cb, gi_skel, gear, vskel):
    """index of the extended skeleton (avatar skeleton gi_skel + the cart / castle gear skeleton) in cb.skels, appended once"""
    key = ('vehicle', gi_skel, bool(gear))
    if key in cb.skel_idx:
        return cb.skel_idx[key]
    if len(cb.skels) >= RUNTIME_MAX_SKELETONS:
        raise ValueError('skeleton budget: SKEL already holds %d skeletons (runtime max %d)' % (len(cb.skels), RUNTIME_MAX_SKELETONS))
    av = cb.skels[gi_skel]
    sk = ext_skeleton(av, vskel, '%s+%s' % (av.get('name', 'AVATAR'), vskel['name']))
    cb.skels.append(sk)
    cb.skel_idx[key] = len(cb.skels) - 1
    return cb.skel_idx[key]


def ext_needed(cb, askels, gears):
    """extended skeletons ext_index would still append for these avatar skeletons x vehicle kinds"""
    have = getattr(cb, 'skel_idx', {}) or {}
    return len({(a, bool(g)) for a in askels for g in gears if ('vehicle', a, bool(g)) not in have})


def ext_fit(cb, askels, gears):
    """True when every extended skeleton fits in the runtime's SKEL budget (else none is added: never a pack the runtime refuses)"""
    return len(cb.skels) + ext_needed(cb, askels, gears) <= RUNTIME_MAX_SKELETONS


def summon_cap(max_skel):
    """the summon picker's npc skeleton cap with the avatar + extended skeletons reserved"""
    return min(max_skel, RUNTIME_MAX_SKELETONS - AVATAR_SKELETONS - EXT_SKELETONS)


def add_vehicle_clips(cb, genders, tm, fm, pat):
    """after add_avatar (the avatar skeletons exist): the merged driver + vehicle clips -> SKMO rows [(motion, gender, wt, clip, loop)]"""
    rows = []
    bases, warn = vehicle_bases(pat)
    for w in warn:
        cb.warn['cart motions: body base differs'] += 1
    if not bases or not cb.avatar:
        return rows
    wts = arms_types(pat)
    vsk = {}
    for gear in bases:
        try:
            vsk[gear] = _veh_skeleton(cb.vfs, gear)
        except KeyError:
            cb.warn['cart motions: vehicle skeleton missing'] += 1
    askels = [cb.avatar['genders'][gi]['skeleton'] for gi in range(min(len(genders), len(cb.avatar['genders'])))]
    if not ext_fit(cb, askels, vsk):
        cb.warn['cart motions: skeleton budget (%d + %d > %d skeletons): no vehicle clips in this pack' % (len(cb.skels), ext_needed(cb, askels, vsk), RUNTIME_MAX_SKELETONS)] += 1
        return rows
    cache = {}
    for gi, g in enumerate(genders):
        if gi >= len(cb.avatar['genders']):
            break
        askel = cb.avatar['genders'][gi]['skeleton']
        for gear, (base, avbase) in sorted(bases.items()):
            if gear not in vsk:
                continue
            ext = ext_index(cb, askel, gear, vsk[gear])
            for act in (GEAR_ACTIONS if gear else CART_ACTIONS):
                loop = act in LOOP_ACTIONS
                dpath = cb._motion_path(tm, fm, avbase + act, 0, 0) if avbase > 0 else None
                v0 = cb._motion_path(tm, fm, base + act, 0, 0)
                for wt in (wts[gear] if (GEAR_ALL_ARMS or not gear) else [0]):
                    vp = cb._motion_path(tm, fm, base + act, wt, 0)
                    if not vp or not dpath or (wt != 0 and _same(vp, v0)):
                        continue
                    k = (RC._norm(vp), RC._norm(dpath), ext, loop)
                    if k not in cache:
                        cache[k] = -1
                        try:
                            vclip = C.bake_clip(C.parse_zmo(cb.vfs.read(vp)), vsk[gear], cb.fps, loop, name=os.path.basename(RC._norm(vp)))
                            dclip = C.bake_clip(C.parse_zmo(cb.vfs.read(dpath)), cb.skels[askel], cb.fps, True, name=os.path.basename(RC._norm(dpath)))
                            c = merged_clip(cb.skels[askel], vsk[gear], cb.skels[ext], ext, vclip, dclip, '%s+%s' % (vclip['name'], dclip['name']))
                            cb.clips.append(c)
                            cache[k] = len(cb.clips) - 1
                        except (KeyError, ValueError, IndexError, struct.error) as e:
                            cb.warn['cart motions: %s' % e.__class__.__name__] += 1
                            print('  warn: cart motion %s: %s' % (vp, e), file=sys.stderr)
                    if 0 <= cache[k] <= 65535 and 0 < base + act <= 65535:
                        rows.append((base + act, g, wt, cache[k], loop))
    return rows


def skinned_cart_items(cb, genders=(0, 1)):
    """rose_carts.add_cart_items with SKINNED meshes on the extended skeletons (added to the avatar pack's SKEL) -> AVAT-format items of slots 10..13 (section ACRS)"""
    vfs = cb.vfs
    rows = CARTS.part_rows(vfs)
    zsc = cb.zsc(CARTS.PAT_ZSC) if rows else None
    if not rows or zsc is None:
        return []
    try:
        vskel = {False: _veh_skeleton(vfs, False), True: _veh_skeleton(vfs, True)}
    except KeyError:
        cb.warn['cart skeleton missing'] += 1
        return []
    askels = [cb.skeleton('3DDATA/AVATAR/%s.ZMD' % ('MALE' if g == 0 else 'FEMALE')) for g in genders]
    if not ext_fit(cb, askels, {gear for _r, _p, gear in rows}):
        cb.warn['cart parts: skeleton budget (%d skeletons): no skinned parts' % len(cb.skels)] += 1
        return []
    items, cache = [], {}
    for g in genders:
        askel = cb.skeleton('3DDATA/AVATAR/%s.ZMD' % ('MALE' if g == 0 else 'FEMALE'))
        for row, part, gear in rows:
            if row >= len(zsc['objs']):
                cb.warn['cart row outside the ZSC'] += 1
                continue
            ext = ext_index(cb, askel, gear, vskel[gear])
            na = cb.skels[ext]['base']
            ids = []
            for mi, ma, _pr in zsc['objs'][row][0]:
                path = zsc['meshes'][mi]
                z = cb.ob.get_zms(path)
                if z is None or ma >= len(zsc['mats']) or 'skin' not in z:
                    cb.warn['cart part without skinned mesh'] += 1
                    continue
                key = (path.replace('\\', '/').upper(), ext, zsc['mats'][ma][0])
                if key in cache:
                    ids.append(cache[key])
                    continue
                cb.ob.compress_raw = True
                try:
                    mat = cb.ob.get_mat(zsc, ma)
                finally:
                    cb.ob.compress_raw = False
                try:
                    m = C.convert_mesh(z, vskel[gear])
                except Exception as e:
                    cb.warn['cart mesh: %s' % e.__class__.__name__] += 1
                    print('  warn: cart mesh %s: %s' % (path, e), file=sys.stderr)
                    continue
                m['palette'] = [j + na for j in m['palette']]
                m.update(skeleton=ext, flags=0, material=mat, name=os.path.basename(path.replace('\\', '/')))
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


def rename_cart_section(sections):
    """the vendored avatar build writes the part items as ACAR; with skinned parts they go out as ACRS (an old runtime would draw them torn on the avatar skeleton)"""
    return [((TAG_ACRS if t == TAG_ACAR else t), v, c, d) for t, v, c, d in sections]


# ------------------------------------------------------------------ BONT budget (w27 review)
# rose_skillvis decides per pack whether the skill clips are pre-baked (the pack with every clip baked <= SKILL_BONT_MAX). The cart clips are SKMO rows too, so they would count in
# that decision and flip a pack near the limit from pre-baked to lazy skill clips (measured: Adventurer's Plain 93.61 MB with the cart clips, ~92.12 MB without, limit 92.27 MB).
# So the decision is taken WITHOUT the cart's cost (clips, their CANM bytes, the extended skeletons, the SKMO rows): exactly as before this lane. The cart clips are pre-baked only
# while the pack with them still fits; otherwise only they stay lazy (the runtime bakes them on the first mount: SetVehicle waits, GsvrRpg1Chars.BontCheck accepts unbaked lazy clips).

def cart_clip_ids(skmo_rows, motions, clip_count):
    """clips only the cart's SKMO rows use (motion in `motions`, no other SKMO row) -> sorted list"""
    cart = {c for m, _g, _w, c, _l in skmo_rows if m in motions and 0 <= c < clip_count}
    other = {c for m, _g, _w, c, _l in skmo_rows if m not in motions}
    return sorted(cart - other)


def cart_cost(skel, canm, skmo, cart):
    """bytes the cart clips add to the pack when every clip is pre-baked: their BONT frames + CANM records / tracks / events / data / names, the extended skeletons only they use
    (SKEL share + bind texels) and their SKMO rows"""
    clips = rose_bont._clips(canm)
    n = len(clips)
    cs = set(cart)
    flags = [c not in cs for c in range(n)]
    cost = SV.bont_bytes_estimate(skel, canm, [True] * n) - SV.bont_bytes_estimate(skel, canm, flags)
    _n, _fps, co, to, eo, do, no, _z = P.CANM_HDR.unpack_from(canm, 0)
    for c in cart:
        sk, fr, _fl, ft, tc, _fe, ec, _du, _sf, noff = P.CANM_CLIP.unpack_from(canm, co + c * P.CANM_CLIP.size)
        cost += P.CANM_CLIP.size + tc * P.CANM_TRACK.size + ec * P.CANM_EVENT.size
        for k in range(tc):
            cost += P.CANM_TRACK.unpack_from(canm, to + (ft + k) * P.CANM_TRACK.size)[4]
        end = canm.find(b'\0', no + noff)
        cost += (end - no - noff + 1) if end >= 0 else 0
    skels = rose_bont._skeletons(skel)
    only = {clips[c][0] for c in cart} - {clips[c][0] for c in range(n) if c not in cs}
    jt = sum(len(s) for s in skels) or 1
    jo = sum(len(skels[s]) for s in only if 0 <= s < len(skels))
    cost += 16 * 2 * jo + len(skel) * jo // jt
    rows = SV.dec_skmo(skmo)
    if rows:
        cost += (len(skmo) - SV.HDR.size) * sum(1 for r in rows if r[3] in cs) // len(rows)
    return cost


_bstate = dict(motions=set(), only=None)


def _patched_mark_lazy(avat, actr, clip_count):
    if _bstate['only'] is not None:          # the pack keeps its skill clips pre-baked but the cart clips do not fit: only they are left out (lazy_too=False bakes the rest)
        return [c in _bstate['only'] for c in range(clip_count)]
    return _orig['mark_lazy'](avat, actr, clip_count)


def _patched_from_sections(sections, max_pack=rose_bont.MAX_PACK_BYTES, lazy_too=True):
    inner = _orig['from_sections']
    by = {t: d for t, _v, _c, d in sections}
    skmo, skel, canm = by.get(SV.TAG_SKMO), by.get(b'SKEL'), by.get(b'CANM')
    if not (skmo and skel and canm and _bstate['motions']):
        return inner(sections, max_pack, lazy_too)
    try:
        rows = SV.dec_skmo(skmo)
        n = len(rose_bont._clips(canm))
        cart = cart_clip_ids(rows, _bstate['motions'], n)
        if not cart:
            return inner(sections, max_pack, lazy_too)
        rest = 32 + sum((len(d) + 3) & ~3 for _t, _v, _c, d in sections) + 24 * (len(sections) + 1)
        full = rest + SV.bont_bytes_estimate(skel, canm, [True] * n)
        cap = min(max_pack, SV.SKILL_BONT_MAX)
        without = full - cart_cost(skel, canm, skmo, cart)
    except (ValueError, IndexError, struct.error):
        return inner(sections, max_pack, lazy_too)
    if full <= cap or without > cap:          # everything fits / the skill clips are lazy anyway (then the cart clips are too: they are SKMO-only): rose_skillvis decides as before
        return inner(sections, max_pack, lazy_too)
    _bstate['only'] = set(cart)               # without the cart the skill clips would be pre-baked: keep that, leave only the cart clips lazy
    try:
        return SV._orig['from_sections'](sections, max_pack, False)
    finally:
        _bstate['only'] = None


# ------------------------------------------------------------------ hooks
_orig = {}


def _wrap_add_avatar(orig):
    def add_avatar(self, genders=(0, 1), *a, **kw):
        res = orig(self, genders, *a, **kw)
        _bstate['motions'] = set()
        try:
            tm, fm = SV._tm_fm(self.vfs)
            pat = _pat(self.vfs)
            if tm is not None and pat is not None:
                rows = add_vehicle_clips(self, genders, tm, fm, pat)
                have = {SV.key(m, lp, g, w) for m, g, w, _c, lp in (getattr(self, 'skill_motions', None) or [])}
                keep = [r for r in rows if SV.key(r[0], r[4], r[1], r[2]) not in have]
                if len(keep) != len(rows):
                    self.warn['cart motions: row taken by a skill motion'] += len(rows) - len(keep)
                self.skill_motions = list(getattr(self, 'skill_motions', None) or []) + keep
                _bstate['motions'] = {r[0] for r in keep}
        except (KeyError, ValueError, IndexError, struct.error) as e:          # never fails the conversion: a pack without the rows draws the old rigid cart / none
            self.warn['cart motions: %s' % e.__class__.__name__] += 1
        return res
    add_avatar.__doc__ = orig.__doc__
    return add_avatar


def _wrap_avatar_build(orig):
    @functools.wraps(orig)          # __wrapped__: inspect.getsource still shows the vendored build (test_rose_conv pins its effect hook)
    def build(*a, **kw):
        sections, info = orig(*a, **kw)
        return rename_cart_section(sections), info
    return build


def install():
    """wraps CharBuilder.add_avatar (after rose_skillvis: its SKMO rows exist), rose_carts.add_cart_items, rose_avatar_pack.build and (around rose_skillvis') rose_bont.mark_lazy /
    from_sections and rose_skills.pick_summon_actors (the skeleton budget reserve) once (idempotent)"""
    if _orig:
        return
    import rose_avatar_pack as RA
    _orig['add_avatar'] = RC.CharBuilder.add_avatar
    _orig['add_cart_items'] = CARTS.add_cart_items
    _orig['avatar_build'] = RA.build
    _orig['mark_lazy'] = rose_bont.mark_lazy
    _orig['from_sections'] = rose_bont.from_sections
    RC.CharBuilder.add_avatar = _wrap_add_avatar(_orig['add_avatar'])
    CARTS.add_cart_items = skinned_cart_items
    RA.build = _wrap_avatar_build(_orig['avatar_build'])
    rose_bont.mark_lazy = _patched_mark_lazy
    rose_bont.from_sections = _patched_from_sections
    import rose_skills as SK
    _orig['pick_summon_actors'] = SK.pick_summon_actors

    def pick_summon_actors(base_ids, summon_ids, skel_of, max_skel=SK.MAX_SUMMON_SKELETONS):
        return _orig['pick_summon_actors'](base_ids, summon_ids, skel_of, summon_cap(max_skel))
    pick_summon_actors.__doc__ = _orig['pick_summon_actors'].__doc__
    SK.pick_summon_actors = pick_summon_actors          # zone_summon_ids looks the name up at call time
