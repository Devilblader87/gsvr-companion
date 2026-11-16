"""Character builder for the RPG1 converter: monsters / NPCs (CHR + PART_NPC.ZSC + ZMD + ZMO) and the player avatar
(MALE/FEMALE.ZMD + LIST_*.ZSC + TYPE_MOTION/FILE_MOTION). Produces the sections SKEL, CMSH, CANM, ACTR, AVAT (docs/RPG1_PACK.md section 8).

Materials and textures go into the shared OTEX / OMAT tables through the ObjBuilder passed in (`ob.get_mat`); character textures are
always DXT compressed (`ob.compress_raw`). Stdlib only; the client files are only read.
"""
import os, sys, math, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosevfs, rosefmt as F, rosechar as C, rpg1 as P

NPC_LOOP_MOTIONS = (0, 1, 5)                  # stop, move, run loop; attack / hit / die / cast are one shot
NPC_MOTION_IDS = tuple(range(6))              # 0 stop 1 move 2 attack 3 hit 4 die 5 run
# ROSE character action ids (TYPE_MOTION rows) and which loop
AVATAR_ACTIONS = (0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13)
AVATAR_LOOP_ACTIONS = (0, 1, 2, 3, 5)
ACTION_NAMES = {0: 'stop1', 1: 'stop2', 2: 'walk', 3: 'run', 4: 'sitting', 5: 'sit', 6: 'standup', 7: 'stop3', 8: 'attack', 9: 'attack2',
                10: 'attack3', 11: 'hit', 12: 'fall', 13: 'die', 14: 'raise', 15: 'jump1', 16: 'jump2', 17: 'pickitem'}
AVATAR_WEAPON_TYPES = (1, 0)                  # 1 = one-hand sword (LIST_WEAPON col 34), 0 = unarmed (the old pair; zone packs before WRAPPER_REV 5)
# W13 CONTENT: weapon motion type (LIST_WEAPON column 34 = TYPE_MOTION column) per weapon item class (LIST_WEAPON column 4). The runtime table is GsvrRoseItems.WeaponMotionType; test_rose_conv pins both.
WEAPON_CLASS_MOTION = {211: 1, 212: 3, 221: 5, 222: 6, 223: 8, 231: 10, 232: 12, 233: 9, 241: 6, 242: 14, 251: 16, 252: 7, 253: 12, 271: 11}
WEAPON_MOTION_TYPES = tuple(sorted({0} | set(WEAPON_CLASS_MOTION.values())))          # 0 unarmed, 1 sword, 3 blunt, 5 two-hand sword, 6 spear / staff, 7 dual, 8 axe, 9 launcher, 10 bow, 11 crossbow, 12 gun, 14 wand, 16 katar
# default item catalogue per slot (ZSC object index = item number); the starter items from INIT_AVATAR are added when missing
# character creation (charedit lane): the open-source client's create screen offers 7 faces (numbers 1 + 7 i) and 5 hairs (numbers 5 i, i.e. 0 .. 20); they must be in the catalogue
# so the lobby's create board and the saved characters can show them. A number the client's ZSC does not hold is skipped (mesh_object returns nothing), never an error.
CREATE_FACES = tuple(1 + 7 * i for i in range(7))
CREATE_HAIRS = tuple(5 * i for i in range(5))
AVATAR_CATALOGUE = {P.SLOT_FACE: tuple(sorted({1, 2, 3} | set(CREATE_FACES))), P.SLOT_HAIR: tuple(sorted({1, 2, 3, 4} | set(CREATE_HAIRS))), P.SLOT_CAP: (2,), P.SLOT_BODY: (1, 30), P.SLOT_HANDS: (1,),
                    P.SLOT_FEET: (1,), P.SLOT_WEAPON: (1,)}
AVATAR_ZSC = {P.SLOT_FACE: 'FACE', P.SLOT_HAIR: 'HAIR', P.SLOT_CAP: 'CAP', P.SLOT_BODY: 'BODY', P.SLOT_HANDS: 'ARMS', P.SLOT_FEET: 'FOOT'}
# rigid-part fallback attachment when a ZSC part names neither bone nor dummy (model_loader.rs): face/hair -> bone 4, cap -> dummy 6
AVATAR_FALLBACK = {P.SLOT_FACE: ('bone', 4), P.SLOT_HAIR: ('bone', 4), P.SLOT_CAP: ('dummy', 6), P.SLOT_FACEITEM: ('dummy', 4),
                   P.SLOT_BACK: ('dummy', 3), P.SLOT_WEAPON: ('dummy', 0)}
AVATAR_FALLBACK[P.SLOT_SUBWPN] = ('dummy', 1)    # shields / sub weapons: left hand dummy when the ZSC part names none (the client spawns it without a default bone)
AVATAR_RUN_BASE = 20.0                        # rose-offline calculate_run_speed: (20 + feet speed) * (dex + 500) / 100


def _norm(p):
    return rosevfs.norm(p)


def _exists(vfs, path):
    return _norm(path) in vfs.files


# ---- full equipment catalogue (rose_avatar_pack.py, the shared avatar pack). slot -> (item STB, ZSC path with %s = M / W or '' when unisex, ZSC dir)
# item number = STB row = ZSC object index (model_loader.rs get_model_part_index: id = item_number); LIST_FACEIEM.ZSC is spelled that way in the client.
EQUIP_TABLES = (
    (P.SLOT_CAP, 'LIST_CAP', '3DDATA/AVATAR/LIST_%sCAP.ZSC', True),
    (P.SLOT_BODY, 'LIST_BODY', '3DDATA/AVATAR/LIST_%sBODY.ZSC', True),
    (P.SLOT_HANDS, 'LIST_ARMS', '3DDATA/AVATAR/LIST_%sARMS.ZSC', True),
    (P.SLOT_FEET, 'LIST_FOOT', '3DDATA/AVATAR/LIST_%sFOOT.ZSC', True),
    (P.SLOT_BACK, 'LIST_BACK', '3DDATA/AVATAR/LIST_BACK.ZSC', False),
    (P.SLOT_FACEITEM, 'LIST_FACEITEM', '3DDATA/AVATAR/LIST_FACEIEM.ZSC', False),
    (P.SLOT_WEAPON, 'LIST_WEAPON', '3DDATA/WEAPON/LIST_WEAPON.ZSC', False),
    (P.SLOT_SUBWPN, 'LIST_SUBWPN', '3DDATA/WEAPON/LIST_SUBWPN.ZSC', False),
)
# character look parts (no item rows: every ZSC object with a mesh): hair variants 0..34 matter because a cap swaps the hair for hair + capHairType
LOOK_TABLES = ((P.SLOT_FACE, '3DDATA/AVATAR/LIST_%sFACE.ZSC'), (P.SLOT_HAIR, '3DDATA/AVATAR/LIST_%sHAIR.ZSC'))
FIELD_ZSC = '3DDATA/ITEM/LIST_FIELDITEM.ZSC'
FIELD_ANIM = '3DDATA/MOTION/ITEM_ANI.ZMO'
FIELD_SKEL = (([(-1, 'field', (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))], [], True), 'FIELD')


class CharBuilder:
    def __init__(self, vfs, ob, out_fps=C.FPS_OUT, log=print):
        self.vfs, self.ob, self.fps, self.log = vfs, ob, out_fps, log
        self.skels = []; self.skel_idx = {}
        self.meshes = []; self.mesh_idx = {}
        self.clips = []; self.clip_idx = {}
        self.actors = []
        self.avatar = None
        self.warn = collections.Counter()
        self._zsc = {}
        self.mat_ids = set()

    # ------------------------------------------------------------ caches
    def zsc(self, path):
        k = _norm(path)
        if k not in self._zsc:
            try:
                self._zsc[k] = F.zsc(self.vfs.read(path))
            except Exception as e:
                self.warn['zsc: %s' % e.__class__.__name__] += 1
                print('  warn: zsc %s: %s' % (path, e), file=sys.stderr)
                self._zsc[k] = None
        return self._zsc[k]

    def skeleton(self, path):
        k = _norm(path)
        if k in self.skel_idx:
            return self.skel_idx[k]
        zmd = F.zmd(self.vfs.read(path))
        sk = C.build_skeleton(zmd, name=os.path.basename(k.replace('\\', '/')))
        sk['flags'] = P.SKF_FACES_PLUS_Z
        self.skels.append(sk)
        self.skel_idx[k] = len(self.skels) - 1
        return self.skel_idx[k]

    def clip(self, path, skel, loop, label=''):
        """-> clip id or -1"""
        if not path:
            return -1
        k = (_norm(path), skel, bool(loop))
        if k in self.clip_idx:
            return self.clip_idx[k]
        cid = -1
        try:
            z = C.parse_zmo(self.vfs.read(path))
            c = C.bake_clip(z, self.skels[skel], self.fps, loop, name=os.path.basename(_norm(path)))
            c['skeleton'] = skel
            if c['dropped']:
                self.warn['clip channels outside the skeleton'] += c['dropped']
            self.clips.append(c)
            cid = len(self.clips) - 1
        except Exception as e:
            self.warn['clip: %s' % e.__class__.__name__] += 1
            print('  warn: clip %s: %s' % (path, e), file=sys.stderr)
        self.clip_idx[k] = cid
        return cid

    # ------------------------------------------------------------ meshes
    def mesh_object(self, zsc, oid, skel, default_attach=None, open_eyes=False):
        """all parts of ZSC object `oid` -> list of CMSH ids. default_attach = joint index for rigid parts without bone / dummy property."""
        if zsc is None or oid >= len(zsc['objs']):
            self.warn['object id out of range'] += 1
            return []
        parts = zsc['objs'][oid][0]
        sk = self.skels[skel]
        nreal = sk['nreal']
        ids = []
        for mi, ma, pr in parts:
            path = zsc['meshes'][mi]
            z = self.ob.get_zms(path)
            if z is None or ma >= len(zsc['mats']):
                continue
            joint = None
            if 'bone' in pr:
                joint = pr['bone']
            elif 'dummy' in pr:
                joint = nreal + pr['dummy']
            is_skin = bool(zsc['mats'][ma][1][0])
            has_skin = 'skin' in z
            if has_skin and (is_skin or joint is None and default_attach is None):
                rigid = None
            else:
                rigid = joint if joint is not None else default_attach
                if rigid is None:
                    rigid = 0; self.warn['rigid part without attachment -> root joint'] += 1
                if rigid >= len(sk['joints']):
                    self.warn['attachment joint out of range'] += 1; rigid = 0
            # The original client (model_loader.rs spawn_model_parts) spawns every part with an identity transform under its bone / dummy:
            # the ZSC part pos / rot / scale are NOT applied to avatar, weapon and NPC parts (they only matter for zone objects).
            clip = 0
            if open_eyes and z.get('mat'):
                clip = z['mat'][-1]              # face meshes: last material group = eyes-open faces drawn when the first group is skipped
            key = (_norm(path), skel, zsc['mats'][ma][0], tuple(zsc['mats'][ma][1]), rigid, clip)
            if key in self.mesh_idx:
                ids.append(self.mesh_idx[key]); continue
            self.ob.compress_raw = True
            try:
                mat = self.ob.get_mat(zsc, ma)
            finally:
                self.ob.compress_raw = False
            try:
                zz = z
                if clip:
                    zz = dict(z); zz['idx'] = z['idx'][clip * 3:]          # blink: eyes open = skip the first `clip` faces
                m = C.convert_mesh(zz, sk, rigid, None)
            except Exception as e:
                self.warn['mesh: %s' % e.__class__.__name__] += 1
                print('  warn: character mesh %s: %s' % (path, e), file=sys.stderr)
                continue
            m.update(skeleton=skel, material=mat, flags=P.CMF_RIGID if rigid is not None else 0, name=os.path.basename(_norm(path)))
            self.mat_ids.add(mat)
            self.meshes.append(m)
            self.mesh_idx[key] = len(self.meshes) - 1
            ids.append(self.mesh_idx[key])
        return ids

    # ------------------------------------------------------------ monsters / NPCs
    def add_actors(self, rows, chr_tables, part_zsc, weapon_zsc=None, subweapon_zsc=None):
        """rows: npc_sidecar rows (dict by npc id with 'model'); chr_tables = rosefmt.chr_ result."""
        sk_paths, mo_paths, ef_paths, npcs, _ok = chr_tables
        for nid in sorted(rows):
            row = rows[nid]
            ch = npcs.get(int(nid))
            a = dict(npc=int(nid), name=row.get('name') or row.get('name_kr') or '', skeleton=P.NO_ID16, meshes=[], clips=[-1] * 12)
            if ch is None or ch['skel'] >= len(sk_paths) or not _exists(self.vfs, sk_paths[ch['skel']]):
                self.warn['actor without skeleton'] += 1
                self.actors.append(self._actor_rec(a, row))
                continue
            skel = self.skeleton(sk_paths[ch['skel']])
            a['skeleton'] = skel
            for oid in ch['models']:
                a['meshes'] += self.mesh_object(part_zsc, oid, skel)
            for key, zs, dummy in (('weapon_r', weapon_zsc, 0), ('weapon_l', subweapon_zsc, 1)):
                wid = row.get(key)
                if isinstance(wid, int) and wid > 0 and zs is not None:
                    a['meshes'] += self.mesh_object(zs, wid, skel, default_attach=self.skels[skel]['nreal'] + dummy)
            for mid, fi in ch['motions']:
                if mid in NPC_MOTION_IDS and fi < len(mo_paths) and _exists(self.vfs, mo_paths[fi]):
                    a['clips'][mid] = self.clip(mo_paths[fi], skel, mid in NPC_LOOP_MOTIONS)
            self.actors.append(self._actor_rec(a, row))

    def _bbox(self, mesh_ids):
        lo = [1e9] * 3; hi = [-1e9] * 3
        for i in mesh_ids:
            m = self.meshes[i]
            if m['flags'] & P.CMF_RIGID:
                continue
            for k in range(3):
                lo[k] = min(lo[k], m['bbmin'][k]); hi[k] = max(hi[k], m['bbmax'][k])
        return (lo, hi) if lo[0] < 1e8 else ([0, 0, 0], [0.5, 1.0, 0.5])

    def _actor_rec(self, a, row):
        def iv(k, d=0):
            v = row.get(k)
            return int(v) if isinstance(v, (int, float)) else d
        scale = (iv('scale_pct', 100) or 100) / 100.0
        lo, hi = self._bbox(a['meshes'])
        ex, ez = hi[0] - lo[0], hi[2] - lo[2]
        walk = iv('walk_speed') / 100.0; run = iv('run_speed') / 100.0
        a.update(
            flags=(P.ACF_MAGIC if iv('magic_dmg') else 0) | (P.ACF_UNTARGETABLE if iv('untargetable') else 0),
            level=iv('level'), hp=iv('hp'), attack=iv('attack'), hit=iv('hit'), defence=iv('defence'), resistance=iv('resistance'), avoid=iv('avoid'),
            attack_speed=iv('attack_speed'), attack_range=iv('attack_range'), npc_type=iv('npc_type'), ai=iv('ai'), exp=iv('exp'),
            drop_row=iv('drop_row'), money_drop_pct=iv('money_drop_pct'), item_drop_pct=iv('item_drop_pct'),
            walk_speed=walk, run_speed=run, scale=scale,
            radius=round(max(0.2, 0.5 * math.sqrt(max(ex, 0.01) * max(ez, 0.01))) * scale, 3), height=round((hi[1]) * scale, 3),
            walk_anim_speed=(walk * 100 + 180) / 600.0, run_anim_speed=(run * 100 + 180) / 600.0,
            attack_anim_speed=max(iv('attack_speed'), 30) / 100.0,
            weapon_r=iv('weapon_r'), weapon_l=iv('weapon_l'))
        return a

    # ------------------------------------------------------------ player avatar
    def add_avatar(self, genders=(0, 1), catalogue=None, weapon_types=WEAPON_MOTION_TYPES, actions=AVATAR_ACTIONS):
        v = self.vfs
        cat = catalogue or AVATAR_CATALOGUE
        tm = F.stb(v.read('3DDATA/STB/TYPE_MOTION.STB'))
        fm = F.stb(v.read('3DDATA/STB/FILE_MOTION.STB'))
        init = F.stb(v.read('3DDATA/STB/INIT_AVATAR.STB'))
        wstb = F.stb(v.read('3DDATA/STB/LIST_WEAPON.STB'))
        feet = F.stb(v.read('3DDATA/STB/LIST_FOOT.STB'))
        wzsc = self.zsc('3DDATA/WEAPON/LIST_WEAPON.ZSC')
        out_genders = []; items = []; clipmap = []
        for g in genders:
            skel_path = '3DDATA/AVATAR/%s.ZMD' % ('MALE' if g == 0 else 'FEMALE')
            skel = self.skeleton(skel_path)
            sk = self.skels[skel]
            nreal = sk['nreal']
            # starter outfit: INIT_AVATAR row g, columns 6..13 = faceitem cap body hands feet back right left (base1000 codes)
            row = init['cells'][g] if g < init['rows'] else []
            def code(ci):
                try:
                    return int(row[ci])
                except (ValueError, IndexError):
                    return 0
            starter_no = {P.SLOT_CAP: code(7) % 1000, P.SLOT_BODY: code(8) % 1000 or 1, P.SLOT_HANDS: code(9) % 1000 or 1, P.SLOT_FEET: code(10) % 1000 or 1,
                          P.SLOT_FACE: 1, P.SLOT_HAIR: 1, P.SLOT_WEAPON: code(12) % 1000 or 1}
            starter_no[P.SLOT_CAP] = 0                                   # visitors start bareheaded; the catalogue still carries a cap
            gitems = {}
            for slot, nums in cat.items():
                lst = list(nums)
                if starter_no.get(slot) and starter_no[slot] not in lst:
                    lst.insert(0, starter_no[slot])
                for num in lst:
                    if slot == P.SLOT_WEAPON:
                        zsc = wzsc; fb = nreal + 0
                    else:
                        zsc = self.zsc('3DDATA/AVATAR/LIST_%s%s.ZSC' % ('M' if g == 0 else 'W', AVATAR_ZSC[slot]))
                        kind, idx = AVATAR_FALLBACK.get(slot, (None, None))
                        fb = None if kind is None else (idx if kind == 'bone' else nreal + idx)
                    ids = self.mesh_object(zsc, num, skel, default_attach=fb, open_eyes=slot == P.SLOT_FACE)
                    if not ids:
                        continue
                    attach = None
                    if slot == P.SLOT_WEAPON:
                        attach = nreal + 0
                    gitems[(slot, num)] = len(items)
                    items.append(dict(gender=g, slot=slot, number=num, meshes=ids, attach=attach, starter=starter_no.get(slot) == num))
            starter = [-1] * 10
            for slot in range(7):
                starter[slot] = gitems.get((slot, starter_no.get(slot)), -1) if starter_no.get(slot) else -1
            # clips: TYPE_MOTION[action][weapon type] -> FILE_MOTION row -> gender column (fallback male, fallback unarmed)
            for wt in weapon_types:
                for act in actions:
                    path = self._motion_path(tm, fm, act, wt, g)
                    if not path:
                        continue
                    cid = self.clip(path, skel, act in AVATAR_LOOP_ACTIONS)
                    if cid >= 0:
                        clipmap.append((g, act, wt, cid))
            # body bounds -> capsule
            body = [it for it in items if it['gender'] == g and it['slot'] in (P.SLOT_BODY, P.SLOT_FEET, P.SLOT_FACE, P.SLOT_HAIR) and it['starter']]
            ids = [m for it in body for m in it['meshes']]
            lo, hi = self._bbox([m for it in body for m in it['meshes'] if not (self.meshes[m]['flags'] & P.CMF_RIGID)] or [])
            top = max([self.meshes[m]['bbmax'][1] for m in ids] or [1.7])
            feet_speed = 65.0
            try:
                feet_speed = float(feet['cells'][starter_no[P.SLOT_FEET]][33] or 65)
            except (ValueError, IndexError):
                pass
            dex = 15
            run = (AVATAR_RUN_BASE + feet_speed) * (dex + 500) / 100.0
            out_genders.append(dict(name='male' if g == 0 else 'female', skeleton=skel, height=round(top, 3), radius=0.3, walk_speed=2.0, run_speed=round(run / 100.0, 3),
                                    walk_anim_speed=(200 + 180) / 600.0, run_anim_speed=(run + 180) / 600.0,
                                    joint_r=nreal + 0, joint_l=nreal + 1, starter=starter))
        self.avatar = dict(genders=out_genders, items=items, clipmap=clipmap)
        return self.avatar

    # ------------------------------------------------------------ the full equipment catalogue (shared avatar pack)
    def add_equipment(self, genders=(0, 1)):
        """Every equipment row of the item tables that has avatar meshes, per gender, plus all faces / hairs -> AVAT (items sorted by gender, slot, number; the starter flag is never set).
        An item whose ZSC object has no mesh (the client draws nothing for it either) gets no AVAT item: the runtime keeps the look it has. -> the avatar dict like add_avatar."""
        v = self.vfs
        out_genders = []; items = []
        for g in genders:
            skel = self.skeleton('3DDATA/AVATAR/%s.ZMD' % ('MALE' if g == 0 else 'FEMALE'))
            nreal = self.skels[skel]['nreal']
            gl = 'M' if g == 0 else 'W'
            for slot, zpath in LOOK_TABLES:
                zsc = self.zsc(zpath % gl)
                if zsc is None:
                    continue
                for num in range(len(zsc['objs'])):
                    kind, idx = AVATAR_FALLBACK[slot]
                    ids = self.mesh_object(zsc, num, skel, default_attach=idx, open_eyes=slot == P.SLOT_FACE)
                    if ids:
                        items.append(dict(gender=g, slot=slot, number=num, meshes=ids, attach=None, starter=False))
            for slot, stb_name, zpath, split in EQUIP_TABLES:
                try:
                    stb = F.stb(v.read('3DDATA/STB/%s.STB' % stb_name))
                except KeyError:
                    self.warn['no table %s' % stb_name] += 1
                    continue
                zsc = self.zsc(zpath % gl if split else zpath)
                if zsc is None:
                    continue
                kind, idx = AVATAR_FALLBACK.get(slot, (None, None))
                fb = None if kind is None else (idx if kind == 'bone' else nreal + idx)
                for num in range(1, min(stb['rows'], 1000)):
                    c = stb['cells'][num]
                    if not (c and c[-1].strip()):
                        continue                    # no string id = no item (same filter as rose_items.build_section)
                    ids = self.mesh_object(zsc, num, skel, default_attach=fb)
                    if not ids:
                        self.warn['item without avatar mesh (slot %s)' % P.SLOT_NAMES[slot]] += 1
                        continue
                    items.append(dict(gender=g, slot=slot, number=num, meshes=ids, attach=fb if slot in (P.SLOT_WEAPON, P.SLOT_SUBWPN) else None, starter=False))
            out_genders.append(dict(name='male' if g == 0 else 'female', skeleton=skel, height=0.0, radius=0.0, walk_speed=0.0, run_speed=0.0, walk_anim_speed=0.0, run_anim_speed=0.0,
                                    joint_r=nreal, joint_l=nreal + 1, starter=[-1] * 10))
        items.sort(key=lambda it: (it['gender'], it['slot'], it['number']))
        self.avatar = dict(genders=out_genders, items=items, clipmap=[])
        return self.avatar

    # ------------------------------------------------------------ ground models of dropped items
    def field_models(self, indices):
        """LIST_FIELDITEM.ZSC objects `indices` (item STB cell 10; 0 = the money model) -> [(index, [CMSH ids])] on a one-joint identity skeleton (pack axes, no facing bake), objects without a mesh skipped.
        The client draws a field model with every part under the drop's root at an identity transform (model_loader.rs spawn_item_drop_model)."""
        zsc = self.zsc(FIELD_ZSC)
        if zsc is None:
            return []
        zmd, nm = FIELD_SKEL
        sk = C.build_skeleton(zmd, name=nm, flip=False)
        sk['flags'] = 0
        self.skels.append(sk)
        skel = len(self.skels) - 1
        out = []
        for i in sorted(set(indices)):
            ids = self.mesh_object(zsc, i, skel, default_attach=0)
            if ids:
                out.append((i, ids))
        return out

    def field_animation(self):
        """3DDATA/MOTION/ITEM_ANI.ZMO (the drop toss, played once on the drop's root by the client) -> [(px, py, pz, qx, qy, qz, qw)] per frame in pack axes (metres), [] when unreadable"""
        try:
            z = C.parse_zmo(self.vfs.read(FIELD_ANIM))
            pos = z['pos'].get(0, []); rot = z['rot'].get(0, [])
            n = max(len(pos), len(rot))
            keys = []
            for f in range(n):
                p = C.file_p(pos[min(f, len(pos) - 1)]) if pos else (0.0, 0.0, 0.0)
                q = C.file_q(rot[min(f, len(rot) - 1)]) if rot else C.IDENT_Q
                keys.append(tuple(p) + tuple(q))
            return keys
        except Exception as e:
            self.warn['field animation: %s' % e.__class__.__name__] += 1
            return []

    def _motion_path(self, tm, fm, action, wtype, gender):
        try:
            idx = int(tm['cells'][action][wtype] or 0)
        except (ValueError, IndexError):
            idx = 0
        if idx == 0 and wtype != 0:
            return self._motion_path(tm, fm, action, 0, gender)
        if idx <= 0 or idx >= fm['rows']:
            return None
        cells = fm['cells'][idx]
        for col in (gender, 0):
            if col < len(cells) and cells[col] and _exists(self.vfs, cells[col]):
                return cells[col]
        return None if wtype == 0 else self._motion_path(tm, fm, action, 0, gender)

    def texture_bytes(self):
        """bytes of OTEX texture data (+ OMAT rows) used by the character materials (they live in the shared OTEX / OMAT tables)"""
        texs = {self.ob.mats[m]['tex'] for m in self.mat_ids if self.ob.mats[m]['tex'] != P.NO_TEX}
        return sum(sum(len(l) for l in self.ob.textures[t]['levels']) + 32 for t in texs) + 24 * len(self.mat_ids)

    # ------------------------------------------------------------ sections
    def sections(self):
        """-> list of (tag, section_version, count, bytes) for SKEL CMSH CANM ACTR AVAT (those that have content)"""
        out = []
        if self.skels:
            out.append((b'SKEL', 1, len(self.skels), P.enc_skel(self.skels)))
        if self.meshes:
            out.append((b'CMSH', 1, len(self.meshes), P.enc_cmsh(self.meshes)))
        if self.clips:
            out.append((b'CANM', 1, len(self.clips), P.enc_canm(self.clips, self.fps)))
        if self.actors:
            out.append((b'ACTR', 1, len(self.actors), P.enc_actr(self.actors)))
        if self.avatar:
            a = self.avatar
            out.append((b'AVAT', 1, len(a['items']), P.enc_avat(a['genders'], a['items'], a['clipmap'])))
        return out
