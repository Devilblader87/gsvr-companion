"""Zone exits for an RPG1 pack (section WARP, docs/RPG1_PACK.md section 13): the original warp gates of a zone and its NPC / quest teleports. Read-only, stdlib only.

Sources (all from the player's client data, nothing hand-typed except the NPC <-> quest trigger link below):
* Warp gates = IFO block type 10 objects. `warp_id` is a WARP.STB row: col 0 name, col 1 target zone (LIST_ZONE row), col 2 target event name (a ZON event of the target
  zone). The trigger shape is the mesh of object 1 of 3DDATA/SPECIAL/LIST_DECO_SPECIAL.ZSC under the IFO transform; the retail client tests a 1 m ball 1 m above the feet
  against it and sends the warp request at most every 5 s (rose-offline-client collision_system.rs, zone_loader.rs). Here the mesh becomes an oriented box (yaw + half
  extents in local metres).
* NPC teleports = QSD reward 7 (teleport zone, x, y in server centimetres = Unity absolute metres x 100). The conversation (CON) files that call the quest trigger are
  encrypted, so QUEST_TELEPORTS names the NPC id, the QSD file and the trigger per zone (a zone without gates whose guide NPC sends the player on); the target comes from the QSD.
"""
import math, struct

import rosevfs, rosefmt as F, rpg1 as P

CENTER_OFF = 5200.0
WARP_STB = '3DDATA/STB/WARP.STB'
SPECIAL_ZSC = '3DDATA/SPECIAL/LIST_DECO_SPECIAL.ZSC'
GATE_OBJECT = 1
NPC_RADIUS = 2.5          # metres around the NPC: "talk" = stand next to it
NPC_HALF_H = 3.0
GATE_MIN_HALF = 1.0
QUEST_TELEPORTS = {20: [dict(npc=1030, qsd='3DDATA/QUESTDATA/TUTORIAL.QSD', trigger='move3_22')]}


def warp_table(vfs):
    """WARP.STB -> {row: (name, target zone or 0, target event name)} for rows with a target zone."""
    out = {}
    try:
        t = F.stb(vfs.read(WARP_STB))
    except KeyError:
        return out
    for i, c in enumerate(t['cells']):
        if len(c) < 3:
            continue
        try:
            z = int(c[1].strip() or '0')
        except ValueError:
            continue
        if z > 0:
            out[i] = (c[0], z, c[2].strip())
    return out


def qsd_teleports(data):
    """QSD file bytes -> {trigger name: (zone, x_cm, y_cm)} of the first teleport reward (opcode 7) of each trigger (rose-offline qsd.rs layout, size-prefixed records)."""
    out = {}
    p = 0
    _ver, ng = struct.unpack_from('<II', data, p); p += 8
    n, = struct.unpack_from('<H', data, p); p += 2 + n
    for _g in range(ng):
        nt, = struct.unpack_from('<I', data, p); p += 4
        n, = struct.unpack_from('<H', data, p); p += 2 + n
        for _t in range(nt):
            _cn, nc, nr = struct.unpack_from('<BII', data, p); p += 9
            n, = struct.unpack_from('<H', data, p); name = data[p + 2:p + 2 + n].decode('cp949', 'replace').strip(' \0'); p += 2 + n
            for _ in range(nc):
                sz, = struct.unpack_from('<I', data, p)
                if sz < 8:
                    raise ValueError('QSD condition size %d' % sz)
                p += sz
            for _ in range(nr):
                sz, op = struct.unpack_from('<II', data, p)
                if sz < 8:
                    raise ValueError('QSD reward size %d' % sz)
                if op & 0xffff == 7 and sz >= 20 and name not in out:
                    out[name] = struct.unpack_from('<III', data, p + 8)
                p += sz
    return out


def zone_event_abs(vfs, zone_id, event, zone_row):
    """Unity absolute (x, z) metres of ZON event `event` of zone `zone_id`, None if missing. zone_row = rose_pack.zone_row."""
    try:
        z = zone_row(vfs, zone_id)
        zon = F.zon(vfs.read(z['zon']))
    except (KeyError, IndexError, ValueError):
        return None
    for nm, p in zon['events']:
        if nm == event:
            return (p[0] / 100.0 + CENTER_OFF, p[2] / 100.0 + CENTER_OFF)    # ZON event = (x, height, y) cm
    return None


def gate_box(verts):
    """Local Unity vertices of the transformed gate mesh -> (cx, cy, cz, hx, hy, hz, yaw deg). Box axes: forward = the mesh's longest horizontal principal direction."""
    n = len(verts)
    mx = sum(v[0] for v in verts) / n; mz = sum(v[2] for v in verts) / n
    sxx = sum((v[0] - mx) ** 2 for v in verts); szz = sum((v[2] - mz) ** 2 for v in verts); sxz = sum((v[0] - mx) * (v[2] - mz) for v in verts)
    ang = 0.5 * math.atan2(2 * sxz, sxx - szz)            # principal axis angle from +x towards +z
    fx, fz = math.cos(ang), math.sin(ang)                   # forward
    yaw = math.degrees(math.atan2(fx, fz))                  # Quaternion.Euler(0, yaw, 0) * +Z = forward
    rx, rz = fz, -fx                                        # right = Euler(0, yaw, 0) * +X
    us = [v[0] * rx + v[2] * rz for v in verts]; ws = [v[0] * fx + v[2] * fz for v in verts]; ys = [v[1] for v in verts]
    cu, cw = (min(us) + max(us)) / 2, (min(ws) + max(ws)) / 2
    cx, cz = cu * rx + cw * fx, cu * rz + cw * fz
    hx = max(GATE_MIN_HALF, (max(us) - min(us)) / 2); hz = max(GATE_MIN_HALF, (max(ws) - min(ws)) / 2); hy = max(GATE_MIN_HALF, (max(ys) - min(ys)) / 2)
    yaw = (yaw + 180.0) % 360.0 - 180.0
    return cx, (min(ys) + max(ys)) / 2, cz, hx, hy, hz, yaw


def gate_vertices(ob, zsc, obj, win, quat_to_mat, mat_scale_cols, mat_mul, mat_vec):
    """Vertices of IFO warp object `obj` (LIST_DECO_SPECIAL object 1) in local Unity metres, same transform as rose_pack.ObjBuilder.add_instance."""
    R = quat_to_mat(obj['q'][3], obj['q'][0], obj['q'][1], obj['q'][2])
    Ri = mat_scale_cols(R, obj['scale'])
    pos_m = tuple(c / 100.0 for c in obj['pos'])
    out = []
    for mesh_path, _ma, Ap, tp, _col in ob.object_parts('special', zsc, GATE_OBJECT):
        z = ob.get_zms(mesh_path)
        if z is None:
            continue
        A = mat_mul(Ri, Ap)
        t = tuple(a + b for a, b in zip(mat_vec(Ri, tp), pos_m))
        for p in z['pos']:
            fx, fy, fz = mat_vec(A, p)
            out.append((fx + t[0] + CENTER_OFF - win.x0, fz + t[2], fy + t[1] + CENTER_OFF - win.z0))
    return out


def zone_warps(vfs, zone_id, ifos, win, ob, spawns, zone_row, mats, warns):
    """-> list of WARP records (rpg1.enc_warp dicts). ifos: {block: parsed IFO}; spawns: rose_pack spawn dicts (kind 1 = NPC, local x/y/z);
    mats = (quat_wxyz_to_mat, mat_scale_cols, mat_mul, mat_vec) of rose_pack; warns: collections.Counter."""
    table = warp_table(vfs)
    known = set()
    def zone_known(z):
        if z in known:
            return True
        try:
            if zone_row(vfs, z)['zon']:
                known.add(z); return True
        except (KeyError, IndexError):
            pass
        return False
    zsc = None
    if rosevfs.norm(SPECIAL_ZSC) in vfs.files:
        zsc = F.zsc(vfs.read(SPECIAL_ZSC))
    out = []
    for b, o in sorted(ifos.items()):
        for obj in o.get(10, []):
            row = table.get(obj['warp'])
            if row is None:
                warns['warp %d: no WARP.STB target' % obj['warp']] += 1
                continue
            name, tzone, tev = row
            verts = gate_vertices(ob, zsc, obj, win, *mats) if zsc is not None and GATE_OBJECT < len(zsc['objs']) else []
            if verts:
                cx, cy, cz, hx, hy, hz, yaw = gate_box(verts)
            else:
                cx, cy, cz = win.to_local(*obj['pos']); hx = hz = hy = 4.0; yaw = 0.0
                warns['warp %d: no gate mesh, 4 m box' % obj['warp']] += 1
            flags = 0
            ta = zone_event_abs(vfs, tzone, tev, zone_row) if tev else None
            if ta is not None:
                flags |= P.WF_TARGET_ABS
            if zone_known(tzone):
                flags |= P.WF_TARGET_KNOWN
            out.append(dict(warp_id=obj['warp'], target_zone=tzone, kind=P.WK_GATE, inside=0.0 <= cx < win.size_x and 0.0 <= cz < win.size_z, flags=flags,
                            cx=cx, cy=cy, cz=cz, hx=hx, hy=hy, hz=hz, yaw=yaw, tx=ta[0] if ta else 0.0, tz=ta[1] if ta else 0.0, target_event=tev, name=name, npc=0))
    for q in QUEST_TELEPORTS.get(zone_id, []):
        try:
            tele = qsd_teleports(vfs.read(q['qsd'])).get(q['trigger'])
        except (KeyError, ValueError, struct.error) as e:
            warns['quest teleport %s: %s' % (q['trigger'], e.__class__.__name__)] += 1
            continue
        if tele is None:
            warns['quest teleport %s: trigger not found' % q['trigger']] += 1
            continue
        tzone, x_cm, y_cm = tele
        flags = P.WF_TARGET_ABS | P.WF_DWELL | (P.WF_TARGET_KNOWN if zone_known(tzone) else 0)
        for s in spawns:
            if s['kind'] != P.KIND_NPC or s['npc'] != q['npc']:
                continue
            out.append(dict(warp_id=0, target_zone=tzone, kind=P.WK_NPC, inside=0.0 <= s['x'] < win.size_x and 0.0 <= s['z'] < win.size_z, flags=flags,
                            cx=s['x'], cy=s['y'] + 1.0, cz=s['z'], hx=NPC_RADIUS, hy=NPC_HALF_H, hz=NPC_RADIUS, yaw=0.0, tx=x_cm / 100.0, tz=y_cm / 100.0,
                            target_event='', name=q['trigger'], npc=q['npc']))
    return out
