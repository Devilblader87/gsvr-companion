"""Revive points for an RPG1 pack (section REVV, docs/RPG1_PACK.md section 14): where a dead player comes back. Read-only, stdlib only.

Sources (the zone row of LIST_ZONE.STB and the ZON event list of the zone):
* col 3 = the NAME of the revive event position; every ZON event with exactly that name is a revive point of the zone (the original server picks the one nearest to the place of death).
* col 2 = the NAME of the start event; used when a zone has no revive point at all.
* cols 31 / 32 / 33 (zone, x, y in server centimetres) = the revive target stored in the zone row. Builds without these columns (30 data columns) write no such record.
Unlike EVNT (which only keeps what the heuristic calls a restore point) every record keeps the `inside` flag, so the runtime can tell that the truly nearest point is outside the converted window.
"""
import struct

import rpg1 as P

KIND_REVIVE, KIND_START, KIND_ZONE_ROW = 0, 1, 2
HDR = struct.Struct('<4I')                     # count, stride (32), recOff, namesOff
REC = struct.Struct('<BBH3f2fI4x')             # kind, inside, reviveZone, x, y, z (local m), absX, absZ (Unity absolute m), nameOff, pad = 32
CENTER_OFF = 5200.0


def _col(cells, i):
    return cells[i].strip() if i < len(cells) else ''


def zone_revive(zon, win, cells, zone_id):
    """-> list of dict(kind, inside, zone, x, y, z, ax, az, name). `zon` = rosefmt.zon result, `win` has to_local / x0 / z0 / size_x / size_z, `cells` = the LIST_ZONE row."""
    rname = _col(cells, 3).lower()
    sname = _col(cells, 2).lower()
    out = []
    starts = []
    for nm, p in zon['events']:
        x, y, z = win.to_local(p[0], p[2], p[1])
        inside = 0.0 <= x <= win.size_x and 0.0 <= z <= win.size_z
        rec = dict(inside=inside, zone=zone_id, x=x, y=y, z=z, ax=x + win.x0, az=z + win.z0, name=nm)
        if rname and nm.lower() == rname:
            rec['kind'] = KIND_REVIVE; out.append(rec)
        elif sname and nm.lower() == sname:
            rec['kind'] = KIND_START; starts.append(rec)
    out += starts
    try:
        rz, px, py = int(_col(cells, 31) or '0'), int(_col(cells, 32) or '0'), int(_col(cells, 33) or '0')
    except ValueError:
        rz = px = py = 0
    if rz > 0:
        ax, az = px / 100.0, py / 100.0
        inside = rz == zone_id and 0.0 <= ax - win.x0 <= win.size_x and 0.0 <= az - win.z0 <= win.size_z
        out.append(dict(kind=KIND_ZONE_ROW, inside=inside, zone=rz, x=ax - win.x0, y=0.0, z=az - win.z0, ax=ax, az=az, name=''))
    return out


def enc_revv(recs):
    names = bytearray(); offs = []
    for r in recs:
        offs.append(len(names)); names += r['name'].encode('utf-8') + b'\0'
    body = b''.join(REC.pack(r['kind'], 1 if r['inside'] else 0, r['zone'], r['x'], r['y'], r['z'], r['ax'], r['az'], o) for r, o in zip(recs, offs))
    ro = HDR.size
    d = HDR.pack(len(recs), REC.size, ro, ro + len(body)) + body + bytes(names)
    return d + b'\0' * (-len(d) % 4)


def dec_revv(d):
    n, stride, ro, no = HDR.unpack_from(d, 0)
    if stride < REC.size or ro + n * stride > len(d) or no > len(d):
        raise ValueError('REVV layout out of bounds')
    out = []
    for i in range(n):
        k, ins, z, x, y, zz, ax, az, nm = REC.unpack_from(d, ro + i * stride)
        e = d.find(b'\0', no + nm)
        if e < 0:
            raise ValueError('REVV name unterminated')
        out.append(dict(kind=k, inside=bool(ins), zone=z, x=x, y=y, z=zz, ax=ax, az=az, name=d[no + nm:e].decode('utf-8')))
    return out
