#!/usr/bin/env python3
"""Export World at War weapons (view + world models, arms, anims) to a neutral intermediate for a GSM1-style pack
builder. Python 3 stdlib only (pypy3 recommended). BYOG: reads the player's own fastfiles; everything is written to
the scratch dir, never into the repo. Format: docs/xmodel.md ("Intermediate export") and docs/xanim.md.

  waw_export.py --ff common_mp.ff --arms-ff localized_mp_dome.ff --arms viewmodel_rus_guard_arms
                --weapons thompson_mp,kar98k_mp,m1garand_mp --out /tmp/codwaw_scratch/export

Writes:
  models/<xmodel>.json + .bin     bones + surfaces (pos/nrm/uv/col/bone idx/weights/tris), WaW units + axes
  anims/<xanim>.json + .bin       per frame, per anim bone: rotation (absolute local) + translation delta
  rigs/<weapon>/<arms>/rig.json   combined bone list (arms + gun under tag_weapon)
  rigs/<weapon>/<arms>/<xanim>.bin   per frame, per rig bone: model-space (q, t) = the pose to skin with
  weapons/<weapon>.json           models, hideTags, anim slots, notetracks -> sound aliases, sounds, timings
  images.json                     every image name the exported materials use (+ semantic)
"""
import argparse
import json
import os
import struct
import sys
import time
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_zone  # noqa: E402
import waw_xmodel as X  # noqa: E402
import waw_xanim  # noqa: E402
import waw_pose  # noqa: E402
import t4_schema as S  # noqa: E402

FORMAT_VERSION = 1
WEAP_ANIMS = sorted(((v, k) for k, v in S.ENUMS.items() if k.startswith("WEAP_ANIM_") and k != "WEAP_ANIM_ROOT"
                     and v < S.ENUMS["NUM_WEAP_ANIMS"]))
SOUND_FIELDS = ["fireSound", "fireSoundPlayer", "fireLastSound", "fireLastSoundPlayer", "emptyFireSound",
                "emptyFireSoundPlayer", "rechamberSound", "rechamberSoundPlayer", "reloadSound", "reloadSoundPlayer",
                "reloadEmptySound", "reloadEmptySoundPlayer", "raiseSound", "raiseSoundPlayer", "firstRaiseSound",
                "firstRaiseSoundPlayer", "putawaySound", "putawaySoundPlayer", "pickupSound", "pickupSoundPlayer",
                "meleeSwipeSound", "meleeSwipeSoundPlayer", "meleeHitSound", "crackSound", "whizbySound"]
NUMBER_FIELDS = ["weapType", "weapClass", "fireType", "iClipSize", "iMaxAmmo", "iStartAmmo", "shotCount", "damage",
                 "playerDamage", "iMeleeDamage", "iFireDelay", "iFireTime", "iRechamberTime", "iRechamberBoltTime",
                 "iHoldFireTime", "iMeleeTime", "iMeleeDelay", "iReloadTime", "iReloadEmptyTime", "iReloadAddTime",
                 "reloadEmptyAddTime", "iDropTime", "iRaiseTime", "iFirstRaiseTime", "iEmptyRaiseTime",
                 "iEmptyDropTime", "quickDropTime", "quickRaiseTime", "iAdsTransInTime", "iAdsTransOutTime",
                 "fAdsZoomFov", "bBoltAction", "bRifleBullet", "aimDownSight", "moveSpeedScale", "adsMoveSpeedScale"]


def _name(x):
    return x.get("name") if isinstance(x, dict) else None


def _alias(snd):
    if not isinstance(snd, dict):
        return None
    n = snd.get("name")
    if isinstance(n, list) and n and isinstance(n[0], dict):
        return n[0].get("soundName")
    if isinstance(n, dict):
        return n.get("soundName")
    return None


# ---- model export --------------------------------------------------------------------------------------------------
def export_model(model, out_dir, lod_limit=None):
    os.makedirs(out_dir, exist_ok=True)
    bin_parts, off = [], 0
    meta_lods = []

    def put(arr):
        nonlocal off
        b = arr.tobytes() if hasattr(arr, "tobytes") else bytes(arr)
        pad = (-len(b)) % 4
        bin_parts.append(b + b"\0" * pad)
        o = off
        off += len(b) + pad
        return o, len(b)

    for li, L in enumerate(model["lods"]):
        if lod_limit is not None and li >= lod_limit:
            break
        surfs = []
        for s in L["surfs"]:
            V, T = len(s["pos"]), len(s["tris"])
            pos = array("f", [c for p in s["pos"] for c in p])
            nrm = array("f", [c for p in s["nrm"] for c in p])
            uv = array("f", [c for p in s["uv"] for c in p])
            col = bytes(c for p in s["col"] for c in p)
            bi = bytearray(4 * V)
            bw = array("f", [0.0] * (4 * V))
            for v, wl in enumerate(s["weights"]):
                for k, (b, wgt) in enumerate(wl[:4]):
                    bi[4 * v + k] = b
                    bw[4 * v + k] = wgt
            tri = array("H", [i for t in s["tris"] for i in t])
            ent = {"material": s["material"], "images": s["images"], "vertCount": V, "triCount": T,
                   "deformed": s["deformed"],
                   "bones": sorted({b for wl in s["weights"] for b, _ in wl})}
            for k, arr in (("pos", pos), ("nrm", nrm), ("uv", uv), ("col", col), ("boneIdx", bi),
                           ("boneWeight", bw), ("tris", tri)):
                o, n = put(arr)
                ent[k] = [o, n]
            surfs.append(ent)
        meta_lods.append({"dist": L["dist"], "surfs": surfs})
    meta = {"format": "codwaw-xmodel", "version": FORMAT_VERSION, "name": model["name"],
            "units": "inch (WaW units)", "axes": "x forward, y left, z up (right handed)",
            "radius": model["radius"], "mins": model["mins"], "maxs": model["maxs"],
            "bones": [{"name": b["name"], "parent": b["parent"], "local_q": b["local_q"], "local_t": b["local_t"],
                       "model_t": b["model_t"], "base_q": b["base_q"], "base_t": b["base_t"]} for b in model["bones"]],
            "lods": meta_lods, "materials": model["materials"],
            "bin": model["name"] + ".bin",
            "layout": "per surface: pos f32x3, nrm f32x3, uv f32x2 (top-left origin), col u8x4 RGBA, boneIdx u8x4, "
                      "boneWeight f32x4, tris u16x3; [offset, bytes] into the .bin"}
    with open(os.path.join(out_dir, model["name"] + ".bin"), "wb") as f:
        for b in bin_parts:
            f.write(b)
    with open(os.path.join(out_dir, model["name"] + ".json"), "w") as f:
        json.dump(meta, f, indent=1)
    return meta


# ---- anim export ---------------------------------------------------------------------------------------------------
def export_anim(anim, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    frames = waw_xanim.bake(anim)
    names = [b["name"] for b in anim["bones"]]
    data = array("f")
    for fr in frames:
        for n in names:
            q, t = fr[n]
            data.extend(q)
            data.extend(t)
    meta = {"format": "codwaw-xanim", "version": FORMAT_VERSION, "name": anim["name"],
            "numframes": anim["numframes"], "framerate": anim["framerate"], "duration": anim["duration"],
            "loop": anim["loop"], "assetType": anim["assetType"], "frameCount": len(frames),
            "bones": names,
            "trackTypes": {b["name"]: [b["quat"]["type"], b["trans"]["type"]] for b in anim["bones"]},
            "notes": [{"name": n, "time": t, "frame": t * anim["numframes"], "seconds": t * anim["duration"]}
                      for n, t in anim["notes"]],
            "delta": anim["delta"],
            "bin": anim["name"] + ".bin",
            "layout": "frameCount x bones x (q f32x4 xyzw, t f32x3); q = absolute parent-relative rotation, "
                      "t = translation DELTA: local translation = model bone model_t + t"}
    with open(os.path.join(out_dir, anim["name"] + ".bin"), "wb") as f:
        f.write(data.tobytes())
    with open(os.path.join(out_dir, anim["name"] + ".json"), "w") as f:
        json.dump(meta, f, indent=1)
    return meta


def export_rig_anim(rig, anim, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    data = array("f")
    frames = waw_xanim.bake(anim)
    for fr in frames:
        for q, t in waw_pose.pose(rig, fr):
            data.extend(q)
            data.extend(t)
    with open(os.path.join(out_dir, anim["name"] + ".bin"), "wb") as f:
        f.write(data.tobytes())
    return len(frames)


# ---- weapon --------------------------------------------------------------------------------------------------------
def anim_lookup(w, name):
    if not name:
        return None
    k = ("XAnimParts", name)
    if k in w.by_name:
        return name
    low = name.lower()
    for (tn, nm) in w.by_name:
        if tn == "XAnimParts" and nm.lower() == low:
            return nm
    return None


def weapon_info(w, wd):
    ss = w.script_strings
    slots = {}
    for v, k in WEAP_ANIMS:
        nm = wd["szXAnims"][v] if v < len(wd["szXAnims"]) else None
        if nm:
            slots[k[len("WEAP_ANIM_"):].lower()] = anim_lookup(w, nm) or nm
    nmap = {}
    for a, b in zip(wd["notetrackSoundMapKeys"], wd["notetrackSoundMapValues"]):
        if a:
            nmap[ss[a]] = ss[b] if b else None
    return {
        "name": wd["szInternalName"], "displayName": wd["szDisplayName"],
        "viewModel": _name(wd["gunXModel"][0]) if wd["gunXModel"] and wd["gunXModel"][0] else None,
        "viewModelAlts": [_name(x) for x in wd["gunXModel"][1:] if x],
        "handModel": _name(wd["handXModel"]),
        "worldModel": _name(wd["worldModel"][0]) if wd["worldModel"] and wd["worldModel"][0] else None,
        "hideTags": [ss[i] for i in wd["hideTags"] if i],
        "anims": slots,
        "notetrackSoundMap": nmap,
        "sounds": {k: _alias(wd.get(k)) for k in SOUND_FIELDS if _alias(wd.get(k))},
        "numbers": {k: wd.get(k) for k in NUMBER_FIELDS},
        "effects": {k: _name(wd.get(k)) for k in ("viewFlashEffect", "worldFlashEffect", "viewShellEjectEffect",
                                                     "worldShellEjectEffect") if _name(wd.get(k))},
    }


def hidden_surfaces(model, hide):
    """indices (LOD0) of surfaces whose bones are all hidden tags or below one (the game hides them by default)"""
    hidden_bones = set()
    for i, b in enumerate(model["bones"]):
        p, chain = i, []
        while p >= 0:
            chain.append(model["bones"][p]["name"])
            p = model["bones"][p]["parent"]
        if any(c in hide for c in chain):
            hidden_bones.add(i)
    out = []
    for si, s in enumerate(model["lods"][0]["surfs"]):
        bs = {b for wl in s["weights"] for b, _ in wl}
        if bs and bs <= hidden_bones:
            out.append(si)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ff", required=True)
    ap.add_argument("--arms-ff")
    ap.add_argument("--arms", default="viewmodel_rus_guard_arms")
    ap.add_argument("--weapons", default="thompson_mp,kar98k_mp,m1garand_mp")
    ap.add_argument("--out", default="/tmp/codwaw_scratch/export")
    ap.add_argument("--no-images", action="store_true", help="do not copy the referenced .iwi files")
    a = ap.parse_args()
    t0 = time.time()
    w = waw_zone.zone(a.ff)
    wa = waw_zone.zone(a.arms_ff) if a.arms_ff else w
    arms = X.model_from_zone(wa, a.arms)
    export_model(arms, os.path.join(a.out, "models"))
    images = {}

    def note_images(model):
        for L in model["lods"]:
            for s in L["surfs"]:
                for sem, im in s["images"].items():
                    images.setdefault(im, set()).add(sem)

    note_images(arms)
    summary = {"arms": a.arms, "weapons": {}}
    for wn in a.weapons.split(","):
        wd = w.by_name[("WeaponDef", wn)]
        info = weapon_info(w, wd)
        gun = X.model_from_zone(w, info["viewModel"])
        world = X.model_from_zone(w, info["worldModel"]) if info["worldModel"] else None
        export_model(gun, os.path.join(a.out, "models"))
        note_images(gun)
        if world:
            export_model(world, os.path.join(a.out, "models"))
            note_images(world)
        info["viewModelHiddenSurfaces"] = hidden_surfaces(gun, set(info["hideTags"]))
        if world:
            info["worldModelHiddenSurfaces"] = hidden_surfaces(world, set(info["hideTags"]))
        rig = waw_pose.build_rig([(arms, None), (gun, "tag_weapon")])
        rdir = os.path.join(a.out, "rigs", wn, a.arms)
        os.makedirs(rdir, exist_ok=True)
        json.dump({"format": "codwaw-rig", "version": FORMAT_VERSION, "weapon": wn,
                   "models": [{"name": m["name"], "firstBone": off, "boneCount": len(m["bones"])}
                              for m, off in rig["models"]],
                   "bones": [{"name": b["name"], "parent": b["parent"], "model": b["model"]} for b in rig["bones"]],
                   "layout": "<xanim>.bin = frameCount x rig bones x (q f32x4 xyzw, t f32x3), model space; skin "
                             "with the bone's base_q/base_t (bind) from models/<model>.json"},
                  open(os.path.join(rdir, "rig.json"), "w"), indent=1)
        done = {}
        for slot, an in info["anims"].items():
            if an in done or ("XAnimParts", an) not in w.by_name:
                continue
            anim = waw_xanim.decode(w, an)
            m = export_anim(anim, os.path.join(a.out, "anims"))
            export_rig_anim(rig, anim, rdir)
            missing = [b for b in m["bones"] if b not in rig["byname"]]
            done[an] = {"frames": m["frameCount"], "seconds": round(anim["duration"], 3),
                        "notes": [n["name"] for n in m["notes"]], "boneNotInRig": missing}
        info["exportedAnims"] = done
        os.makedirs(os.path.join(a.out, "weapons"), exist_ok=True)
        with open(os.path.join(a.out, "weapons", wn + ".json"), "w") as f:
            json.dump(info, f, indent=1)
        summary["weapons"][wn] = {"view": info["viewModel"], "world": info["worldModel"], "anims": len(done),
                                  "hidden": info["viewModelHiddenSurfaces"]}
        print(f"{wn}: view {info['viewModel']} ({sum(len(s['pos']) for s in gun['lods'][0]['surfs'])} v), "
              f"world {info['worldModel']}, {len(done)} anims, hideTags {info['hideTags']}")
    found = {}
    if not a.no_images:
        idir = os.path.join(a.out, "images")
        os.makedirs(idir, exist_ok=True)
        for im in sorted(images):
            if im.startswith("$"):               # engine built-ins ($identitynormalmap ...)
                continue
            data = waw_pose.iwi_bytes(im)
            if data:
                fn = im.replace("/", "_").replace("~", "_").replace("&", "_") + ".iwi"
                with open(os.path.join(idir, fn), "wb") as f:
                    f.write(data)
                found[im] = fn
    with open(os.path.join(a.out, "images.json"), "w") as f:
        json.dump({k: {"semantics": sorted(v), "file": found.get(k)} for k, v in sorted(images.items())}, f, indent=1)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(f"exported to {a.out} in {time.time() - t0:.1f}s; {len(images)} images referenced, {len(found)} .iwi copied")


if __name__ == "__main__":
    main()
