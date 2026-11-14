"""Character select scene for an RPG1 pack (section LOBY, docs/RPG1_PACK.md section 15): the slot positions and the select camera path of the login zone.
Read-only, stdlib only. The world (GsvrRoseLobby) stands the saved characters on the positions and puts the player where the select camera looks from.

What the owner configures (nothing of it is in this repo): a JSON file OUTSIDE the repo (`--lobby-config FILE` or the environment variable ROSE_LOBBY_CONFIG), CLI options override it:
  {"zone": <LIST_ZONE row of the login / select scene>, "camera": "<path of the select camera ZMO inside the client>", "positions_file": "<slot positions JSON>"}
How the reference client uses them (rose-offline-client, an open-source re-implementation; what the retail executable does is not verified):
* the login / select scene is a normal zone (a constant in its login system) -> `rose_pack.py --lobby` converts it like any zone (small window);
* the select camera is a bone-less ZMO played once when the select screen opens: channel 0 = eye, 1 = target, 2 = up (ZMO position channels, file centimetres, z up),
  channel 3 = fov / near / far (not used here);
* the slot positions are not read from a game file there: that client hard codes them in its source. So the converter never carries them: the owner passes them
  (`positions_file`, see `load_positions`); without them they are derived (an arc in front of the camera's last eye point, flag LF_POS_DERIVED).
"""
import os, json, math

import rosefmt as F, rpg1 as P
from rosechar import _ZMO_SZ

CONFIG_ENV = 'ROSE_LOBBY_CONFIG'  # owner's lobby config (JSON outside the repo): zone, camera, positions_file
SLOTS = 5                         # the original select screen has five character slots
ARC_DIST = 12.0                   # derived layout: the middle slot stands this far in front of the camera's last eye point (the target can be far behind the scene)
ARC_SPACING = 2.5                 # derived layout: metres between neighbouring slots along the arc
ARC_BEND = 0.3                    # derived layout: each step away from the middle comes this many metres x step^2 closer to the camera
BLOCK_M = 160.0                   # = rose_pack.BLOCK_M / CENTER_OFF (not imported: rose_pack imports this module)
CENTER_OFF = 5200.0


def parse_camera_zmo(d):
    """ZMO0002 camera -> dict(fps, nf, chans=[[(x, y, z) file cm, ...] per POSITION channel in file order]). Non-position channels are skipped by size."""
    r = F.R(d)
    if r.nul() != 'ZMO0002':
        raise ValueError('not a ZMO0002')
    fps = r.u32(); nf = r.u32(); nc = r.u32()
    if nc > 64 or nf > 100000:
        raise ValueError('ZMO header out of range')
    ch = [(r.u32(), r.u32()) for _ in range(nc)]
    for t, _b in ch:
        if t not in _ZMO_SZ:
            raise ValueError('unknown ZMO channel type %d' % t)
    pos = [[] for t, _b in ch if t == 2]
    for _f in range(nf):
        k = 0
        for t, _b in ch:
            if t == 2:
                pos[k].append(r.vec(3)); k += 1
            else:
                r.p += _ZMO_SZ[t]
    return dict(fps=fps, nf=nf, chans=pos)


def _norm(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (0.0, 1.0, 0.0) if n < 1e-9 else (v[0] / n, v[1] / n, v[2] / n)


def camera_frames(z, win):
    """parse_camera_zmo result -> [(eye, target, up)] in local Unity metres (file (x, y, z) cm -> window.to_local; up = (x, z, y) normalised). [] when the file has < 3 position channels."""
    if len(z['chans']) < 3:
        return []
    out = []
    for e, t, u in zip(z['chans'][0], z['chans'][1], z['chans'][2]):
        out.append((tuple(win.to_local(e[0], e[1], e[2])), tuple(win.to_local(t[0], t[1], t[2])), _norm((u[0], u[2], u[1]))))
    return out


def yaw_to(px, pz, tx, tz):
    """Unity yaw in degrees (0 = +Z, 90 = +X) from (px, pz) towards (tx, tz); 0 when the points coincide."""
    dx, dz = tx - px, tz - pz
    if abs(dx) < 1e-9 and abs(dz) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(dx, dz))


def derived_positions(eye, target, n=SLOTS, spacing=ARC_SPACING, bend=ARC_BEND, dist=ARC_DIST):
    """n slots on an arc `dist` metres in front of the eye along the view direction (horizontal), across it, the outer ones bent towards the camera; each faces
    the eye. y = the eye height (the world puts them on the ground)."""
    fx, fz = target[0] - eye[0], target[2] - eye[2]
    m = math.hypot(fx, fz)
    fx, fz = (0.0, 1.0) if m < 1e-6 else (fx / m, fz / m)
    rx, rz = fz, -fx                         # right of the view direction (Unity, y up)
    out = []
    for i in range(n):
        k = i - (n - 1) / 2.0
        x = eye[0] + fx * dist + rx * k * spacing - fx * bend * k * k
        z = eye[2] + fz * dist + rz * k * spacing - fz * bend * k * k
        out.append(dict(x=x, y=eye[1], z=z, yaw=yaw_to(x, z, eye[0], eye[2]), scale=1.0))
    return out


def load_positions(path, win):
    """Owner's positions file -> list of dict(x, y, z, scale) in local metres. JSON:
    {"frame": "bevy" | "unity", "positions": [[x, y, z], ...] (1..16), "scale": 1.0 (optional)}
    bevy = the rose-offline-client frame (metres, y up, z = -north); unity = Unity absolute metres (y up, z = north). Raises ValueError on a bad file."""
    with open(path, 'r') as f:
        j = json.load(f)
    frame = j.get('frame', 'bevy')
    if frame not in ('bevy', 'unity'):
        raise ValueError('positions file: frame must be "bevy" or "unity"')
    pts = j.get('positions')
    if not isinstance(pts, list) or not (1 <= len(pts) <= P.LOBY_MAX_POS):
        raise ValueError('positions file: 1..%d positions expected' % P.LOBY_MAX_POS)
    sc = float(j.get('scale', 1.0))
    if not (0.1 <= sc <= 10.0):
        raise ValueError('positions file: scale out of range')
    out = []
    for p in pts:
        if not isinstance(p, list) or len(p) != 3:
            raise ValueError('positions file: every position is [x, y, z]')
        x, y, z = (float(v) for v in p)
        if not all(math.isfinite(v) for v in (x, y, z)):
            raise ValueError('positions file: non-finite value')
        ax, ay, az = (x, y, -z) if frame == 'bevy' else (x, y, z)
        out.append(dict(x=ax - win.x0, y=ay, z=az - win.z0, scale=sc))
    return out


def load_config(path):
    """Owner's lobby config -> dict(zone=int or None, camera=str or None, positions=str or None). Missing keys = None; a config that carries "positions" itself is its own
    positions file. Raises ValueError on a bad file."""
    with open(path, 'r') as f:
        j = json.load(f)
    if not isinstance(j, dict):
        raise ValueError('lobby config: a JSON object expected')
    z = j.get('zone')
    if z is not None and (not isinstance(z, int) or isinstance(z, bool) or not (0 < z < 10000)):
        raise ValueError('lobby config: zone must be a LIST_ZONE row number')
    cam = j.get('camera')
    if cam is not None and (not isinstance(cam, str) or not cam):
        raise ValueError('lobby config: camera must be a path inside the client')
    pos = j.get('positions_file')
    if pos is None and 'positions' in j:
        pos = path
    return dict(zone=z, camera=cam, positions=pos)


def resolve(zone=None, camera=None, positions=None, config_path=None, env=None):
    """CLI values override the config (config_path, else env[CONFIG_ENV]). -> dict(zone, camera, positions). Raises ValueError when the zone row is not known."""
    env = os.environ if env is None else env
    cfg = dict(zone=None, camera=None, positions=None)
    path = config_path or env.get(CONFIG_ENV)
    if path:
        cfg = load_config(path)
    out = dict(zone=zone if zone is not None else cfg['zone'], camera=camera or cfg['camera'], positions=positions or cfg['positions'])
    if out['zone'] is None:
        raise ValueError('--lobby needs the LIST_ZONE row of the login / select scene: --zone N, or "zone" in the lobby config (--lobby-config FILE or %s, a JSON file outside the repo)' % CONFIG_ENV)
    return out


def lobby_section(vfs, win, positions_file=None, camera=None, warns=None):
    """-> (LOBY bytes, info dict). Camera missing / unreadable = no camera (a warning); no positions file = derived positions (needs the camera; without it the section is not written: None)."""
    cam = []; fps = 30
    try:
        if not camera:
            raise KeyError('no camera configured')
        z = parse_camera_zmo(vfs.read(camera))
        fps = z['fps'] or 30
        cam = camera_frames(z, win)
        if not cam and warns is not None:
            warns['lobby camera: fewer than 3 position channels'] += 1
    except (KeyError, ValueError, IndexError) as e:
        if warns is not None:
            warns['lobby camera: %s' % e] += 1
    flags = P.LF_CAMERA if cam else 0
    if positions_file:
        pos = load_positions(positions_file, win)
        flags |= P.LF_POS_EXACT
    elif cam:
        pos = derived_positions(cam[-1][0], cam[-1][1])
        flags |= P.LF_POS_DERIVED
    else:
        return None, dict(positions=0, camera_frames=0, note='no camera and no positions file: no LOBY section')
    eye = cam[-1][0] if cam else None
    for p in pos:
        if 'yaw' not in p:
            p['yaw'] = yaw_to(p['x'], p['z'], eye[0], eye[2]) if eye else 180.0
    inside = sum(1 for p in pos if 0.0 <= p['x'] <= win.size_x and 0.0 <= p['z'] <= win.size_z)
    info = dict(positions=len(pos), inside=inside, camera_frames=len(cam), camera_fps=fps,
                source='file' if flags & P.LF_POS_EXACT else 'derived')
    return P.enc_loby(pos, cam, fps, flags), info


def lobby_center_block(vfs, positions_file=None, camera=None):
    """Block (bx, by) to centre the lobby window on: the first position of the owner's file, else the camera's last eye point (file cm); None when neither is readable."""
    if positions_file:
        with open(positions_file, 'r') as f:
            j = json.load(f)
        x, y, zz = j['positions'][0]
        ax, az = (x, -zz) if j.get('frame', 'bevy') == 'bevy' else (x, zz)
        return int(math.floor(ax / BLOCK_M)), int(math.floor(65 - az / BLOCK_M))
    try:
        if not camera:
            return None
        z = parse_camera_zmo(vfs.read(camera))
        if z['chans'] and z['chans'][0]:
            t = z['chans'][0][-1]
            ax = t[0] / 100.0 + CENTER_OFF; az = t[1] / 100.0 + CENTER_OFF
            return int(math.floor(ax / BLOCK_M)), int(math.floor(65 - az / BLOCK_M))
    except (KeyError, ValueError, IndexError):
        pass
    return None
