#!/usr/bin/env python3
"""World at War XAnimParts decoder + pose baker. Python 3 stdlib only.

BYOG: reads the player's own fastfiles (via tools/waw_zone.py); output goes to scratch, never into the repo.

Decoding follows the loaded (in-memory) XAnimParts layout; the track walk order is the one OpenAssetTools'
FlatXAnimReader uses (GPL-3 reference, re-implemented here). See docs/xanim.md for the format.

  bones in `names` are grouped by quat type: boneCount[0] NO_QUAT, [1] HALF_QUAT, [2] FULL_QUAT,
  [3] HALF_QUAT_NO_SIZE, [4] FULL_QUAT_NO_SIZE; then trans tracks (each names its bone by a dataByte index):
  [5] SMALL_TRANS, [6] TRANS, [7] TRANS_NO_SIZE, [8] NO_TRANS; [9] = total bone count.

decode(walker, name) -> {name, numframes, framerate, loop, delta, bones: [{name, quat, trans}], notes: [(name, t)]}
  quat  = {"type", "idx": [frame numbers], "q": [(x, y, z, w) floats]}    (constant tracks: one key, idx [0])
  trans = {"type", "idx": [...], "t": [(x, y, z)]}
sample(anim, frame) -> {bone: (q, t)} local (parent-relative) transforms, linear key interpolation (nlerp)

Usage:
  waw_xanim.py info <file.ff> <xanim>          tracks, key counts, notetracks, data consumption check
  waw_xanim.py check <file.ff>                 decode every xanim in the zone, report failures
"""
import argparse
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_zone  # noqa: E402

QUAT_TYPES = ["NO_QUAT", "HALF_QUAT", "FULL_QUAT", "HALF_QUAT_NO_SIZE", "FULL_QUAT_NO_SIZE"]
TRANS_TYPES = ["SMALL_TRANS", "TRANS", "TRANS_NO_SIZE", "NO_TRANS"]
Q = 1.0 / 32767.0


class Cursor:
    """sequential consumption of the flat data pools (dataByte, dataShort, dataInt, random*, indices)"""

    def __init__(self, parts):
        def vals(k):
            v = parts.get(k)
            return v.values() if isinstance(v, waw_zone.Blob) else []
        self.pools = {"byte": vals("dataByte"), "short": vals("dataShort"), "int": vals("dataInt"),
                      "rbyte": vals("randomDataByte"), "rshort": vals("randomDataShort"), "rint": vals("randomDataInt")}
        ind = parts.get("indices") or {}
        i2 = ind.get("_2") if parts["numframes"] >= 256 else None
        self.pools["indices"] = i2.values() if isinstance(i2, waw_zone.Blob) else []
        self.pos = {k: 0 for k in self.pools}

    def take(self, pool, n=1):
        p = self.pos[pool]
        arr = self.pools[pool]
        if p + n > len(arr):
            raise ValueError(f"xanim data exhausted: {pool} wants {n} at {p} of {len(arr)}")
        self.pos[pool] = p + n
        return arr[p:p + n]

    def one(self, pool):
        return self.take(pool, 1)[0]

    def remaining(self):
        return {k: len(v) - self.pos[k] for k, v in self.pools.items() if len(v) - self.pos[k]}


def _f32(i):
    return struct.unpack("<f", struct.pack("<i", i))[0]


def _indices(cur, stored, byte_idx):
    n = stored + 1
    if byte_idx:
        return cur.take("byte", n)
    if stored >= 64:
        r = cur.take("indices", n)
        cur.take("short", (n - 2) // 256 + 2)      # checkpoint copies the linker leaves in dataShort
        return r
    return [x & 0xFFFF for x in cur.take("short", n)]


def decode(w, name_or_parts):
    parts = name_or_parts if isinstance(name_or_parts, dict) else w.by_name[("XAnimParts", name_or_parts)]
    bc = parts["boneCount"]
    nf = parts["numframes"]
    byte_idx = nf < 256
    names = [w.script_strings[i] for i in parts["names"].values()] if isinstance(parts.get("names"), waw_zone.Blob) else []
    cur = Cursor(parts)
    bones = [{"name": n, "quat": None, "trans": None} for n in names]
    b = 0
    for _ in range(bc[0]):
        bones[b]["quat"] = {"type": "NO_QUAT", "idx": [0], "q": [(0.0, 0.0, 0.0, 1.0)]}
        b += 1
    for _ in range(bc[1]):
        st = cur.one("short") & 0xFFFF
        idx = _indices(cur, st, byte_idx)
        raw = cur.take("rshort", 2 * (st + 1))
        bones[b]["quat"] = {"type": "HALF_QUAT", "idx": idx,
                            "q": [_qn((0.0, 0.0, raw[2 * i] * Q, raw[2 * i + 1] * Q)) for i in range(st + 1)]}
        b += 1
    for _ in range(bc[2]):
        st = cur.one("short") & 0xFFFF
        idx = _indices(cur, st, byte_idx)
        raw = cur.take("rshort", 4 * (st + 1))
        bones[b]["quat"] = {"type": "FULL_QUAT", "idx": idx,
                            "q": [_qn(tuple(raw[4 * i + k] * Q for k in range(4))) for i in range(st + 1)]}
        b += 1
    for _ in range(bc[3]):
        raw = cur.take("short", 2)
        bones[b]["quat"] = {"type": "HALF_QUAT_NO_SIZE", "idx": [0], "q": [_qn((0.0, 0.0, raw[0] * Q, raw[1] * Q))]}
        b += 1
    for _ in range(bc[4]):
        raw = cur.take("short", 4)
        bones[b]["quat"] = {"type": "FULL_QUAT_NO_SIZE", "idx": [0], "q": [_qn(tuple(r * Q for r in raw))]}
        b += 1
    if b != bc[9] and bc[9] != sum(bc[0:5]):
        raise ValueError("quat bone counts do not add up")
    for _ in range(bc[5]):
        bi = cur.one("byte")
        st = cur.one("short") & 0xFFFF
        mins = [_f32(x) for x in cur.take("int", 3)]
        size = [_f32(x) for x in cur.take("int", 3)]
        idx = _indices(cur, st, byte_idx)
        raw = cur.take("rbyte", 3 * (st + 1))
        bones[bi]["trans"] = {"type": "SMALL_TRANS", "idx": idx, "mins": mins, "size": size,
                              "t": [tuple(mins[k] + size[k] * raw[3 * i + k] for k in range(3)) for i in range(st + 1)]}
    for _ in range(bc[6]):
        bi = cur.one("byte")
        st = cur.one("short") & 0xFFFF
        mins = [_f32(x) for x in cur.take("int", 3)]
        size = [_f32(x) for x in cur.take("int", 3)]
        idx = _indices(cur, st, byte_idx)
        raw = [x & 0xFFFF for x in cur.take("rshort", 3 * (st + 1))]
        bones[bi]["trans"] = {"type": "TRANS", "idx": idx, "mins": mins, "size": size,
                              "t": [tuple(mins[k] + size[k] * raw[3 * i + k] for k in range(3)) for i in range(st + 1)]}
    for _ in range(bc[7]):
        bi = cur.one("byte")
        c = tuple(_f32(x) for x in cur.take("int", 3))
        bones[bi]["trans"] = {"type": "TRANS_NO_SIZE", "idx": [0], "t": [c]}
    for _ in range(bc[8]):
        bi = cur.one("byte")
        bones[bi]["trans"] = {"type": "NO_TRANS", "idx": [0], "t": [(0.0, 0.0, 0.0)]}
    left = cur.remaining()
    if left:
        raise ValueError(f"xanim data not fully consumed: {left}")
    if any(x["trans"] is None or x["quat"] is None for x in bones):
        raise ValueError("bone without track")
    notes = []
    nt = parts.get("notify")
    if isinstance(nt, waw_zone.Blob):
        for x in nt.values():
            notes.append((w.script_strings[x["name"]], x["time"]))
    delta = decode_delta(parts.get("deltaPart"), byte_idx)
    return {"name": parts["name"], "numframes": nf, "framerate": parts["framerate"], "loop": bool(parts["bLoop"]),
            "delta": delta, "assetType": parts["assetType"], "bones": bones, "notes": notes,
            "duration": nf / parts["framerate"] if parts["framerate"] else 0.0}


def _blobvals(x):
    return x.values() if isinstance(x, waw_zone.Blob) else (list(x) if isinstance(x, list) else [])


def decode_delta(dp, byte_idx):
    """root motion track (XAnimDeltaPart: trans + 2D quat about z); None when absent"""
    if not isinstance(dp, dict):
        return None
    out = {}
    tr = dp.get("trans")
    if isinstance(tr, dict):
        if tr["size"] == 0:
            f0 = tr["u"]["frame0"] if "frame0" in tr["u"] else None
            out["trans"] = {"idx": [0], "t": [(f0["x"], f0["y"], f0["z"])] if f0 else [(0.0, 0.0, 0.0)]}
        else:
            fr = tr["u"]["frames"]
            ind = fr["indices"]
            idx = list(_blobvals(ind.get("_1") if byte_idx else ind.get("_2")))
            mins = (fr["mins"]["x"], fr["mins"]["y"], fr["mins"]["z"])
            size = (fr["size"]["x"], fr["size"]["y"], fr["size"]["z"])
            src = fr["frames"].get("_1") if tr["smallTrans"] else fr["frames"].get("_2")
            raw = _blobvals(src)
            n = tr["size"] + 1
            out["trans"] = {"idx": idx, "t": [tuple(mins[k] + size[k] * raw[3 * i + k] for k in range(3)) for i in range(n)]}
    qq = dp.get("quat")
    if isinstance(qq, dict):
        if qq["size"] == 0:
            v = qq["u"]["frame0"]["value"]
            out["quat"] = {"idx": [0], "q": [_qn((0.0, 0.0, v[0] * Q, v[1] * Q))]}
        else:
            fr = qq["u"]["frames"]
            ind = fr["indices"]
            idx = list(_blobvals(ind.get("_1") if byte_idx else ind.get("_2")))
            raw = _blobvals(fr["frames"])
            vals = [x["value"] for x in raw] if raw and isinstance(raw[0], dict) else [raw[i:i + 2] for i in range(0, len(raw), 2)]
            out["quat"] = {"idx": idx, "q": [_qn((0.0, 0.0, v[0] * Q, v[1] * Q)) for v in vals]}
    return out or None


def _qn(q):
    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    if n == 0:
        return (0.0, 0.0, 0.0, 1.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def _seg(idx, f):
    """key segment for frame f: (i0, i1, t)"""
    n = len(idx)
    if n == 1 or f <= idx[0]:
        return 0, 0, 0.0
    if f >= idx[-1]:
        return n - 1, n - 1, 0.0
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if idx[mid] <= f:
            lo = mid
        else:
            hi = mid
    a, b = idx[lo], idx[hi]
    return lo, hi, (f - a) / (b - a) if b > a else 0.0


def sample_quat(tr, f):
    i0, i1, t = _seg(tr["idx"], f)
    a, b = tr["q"][i0], tr["q"][i1]
    if t == 0.0:
        return a
    if a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3] < 0:
        b = (-b[0], -b[1], -b[2], -b[3])
    return _qn(tuple(a[k] + (b[k] - a[k]) * t for k in range(4)))


def sample_trans(tr, f):
    i0, i1, t = _seg(tr["idx"], f)
    a, b = tr["t"][i0], tr["t"][i1]
    if t == 0.0:
        return a
    return tuple(a[k] + (b[k] - a[k]) * t for k in range(3))


def sample(anim, frame):
    return {b["name"]: (sample_quat(b["quat"], frame), sample_trans(b["trans"], frame)) for b in anim["bones"]}


def bake(anim, fps=None):
    """[{bone: (q, t)}] for every frame 0..numframes (inclusive) at the anim's own rate (or resampled to fps)"""
    nf = anim["numframes"]
    if not fps or not anim["framerate"]:
        return [sample(anim, f) for f in range(nf + 1)]
    n = int(round(anim["duration"] * fps))
    return [sample(anim, min(nf, i * anim["framerate"] / fps)) for i in range(n + 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("info"); p.add_argument("file"); p.add_argument("name")
    p = sp.add_parser("check"); p.add_argument("file")
    a = ap.parse_args()
    w = waw_zone.zone(a.file)
    if a.cmd == "info":
        an = decode(w, a.name)
        print(f"{an['name']}: {an['numframes']} frames @ {an['framerate']} fps = {an['duration']:.3f} s, "
              f"loop {an['loop']}, delta {bool(an['delta'])}, {len(an['bones'])} bones")
        types = {}
        for b in an["bones"]:
            types[b["quat"]["type"]] = types.get(b["quat"]["type"], 0) + 1
            types[b["trans"]["type"]] = types.get(b["trans"]["type"], 0) + 1
        print("  track types:", types)
        for b in an["bones"][:80]:
            print(f"  {b['name']:30s} {b['quat']['type']:18s} keys {len(b['quat']['idx']):3d}  "
                  f"{b['trans']['type']:13s} keys {len(b['trans']['idx']):3d}")
        print("  notetracks:", [(n, round(t * an["numframes"]), round(t, 4)) for n, t in an["notes"]])
    else:
        ok, bad = 0, []
        for (tn, nm), parts in sorted(w.by_name.items()):
            if tn != "XAnimParts":
                continue
            try:
                decode(w, parts)
                ok += 1
            except Exception as ex:
                bad.append((nm, str(ex)))
        print(f"{os.path.basename(a.file)}: {ok} xanims decoded (all data consumed), {len(bad)} failed")
        for nm, e in bad[:20]:
            print("  FAIL", nm, e)


if __name__ == "__main__":
    main()
