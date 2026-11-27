#!/usr/bin/env python3
"""CoD: World at War multiplayer weapon files (plain text in iw_14.iwd: weapons/mp/<name>). Python 3 stdlib only.

BYOG: reads the player's own main/iw_*.iwd; nothing is copied into the repo.

File format ("WEAPONFILE" text, same family as CoD4 weaponfiles):
    WEAPONFILE\\key\\value\\key\\value ... (backslash separated, values may contain CR/LF = multi line lists)
The key order is fixed. A full MP infantry weapon has 395 keys (thompson_mp: 395), grenades/utility files have 119..418
tokens (fewer keys). Values are numbers or strings; empty value = unset.

Usage:
  waw_weapon.py list  <main dir>                          all weapons/mp names + key counts
  waw_weapon.py show  <main dir> <weapon> [--all]         normalized table (key facts) or every key
  waw_weapon.py table <main dir> --out weapons_mp.json    every MP weapon, normalized + raw
  waw_weapon.py doc   <main dir> --out docs/weapondef.md  regenerate the key reference
"""
import argparse
import glob
import json
import os
import sys
import zipfile

MP_PREFIX = "weapons/mp/"


def _num(s):
    s = s.strip()
    if s == "":
        return ""
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f == int(f) and "." not in s and "e" not in s.lower() else f


def parse_weaponfile(data):
    """bytes -> (ordered dict key->value). Raises ValueError if the header is not WEAPONFILE."""
    t = data.decode("latin1").split("\\")
    if not t or t[0] != "WEAPONFILE":
        raise ValueError("not a WEAPONFILE")
    kv = {}
    for i in range(1, len(t) - 1, 2):
        kv[t[i]] = _num(t[i + 1]) if "\n" not in t[i + 1] else [x for x in t[i + 1].replace("\r", "").split("\n") if x]
    return kv


def open_main(main_dir):
    """Find the iwd that holds weapons/mp (iw_14.iwd in the German retail install)."""
    for f in sorted(glob.glob(os.path.join(main_dir, "*.iwd"))):
        z = zipfile.ZipFile(f)
        if any(n.startswith(MP_PREFIX) for n in z.namelist()):
            return z
    raise FileNotFoundError("no iwd with weapons/mp in " + main_dir)


def load_all(main_dir):
    z = open_main(main_dir)
    out = {}
    for n in z.namelist():
        if n.startswith(MP_PREFIX) and not n.endswith("/"):
            try:
                out[n[len(MP_PREFIX):]] = parse_weaponfile(z.read(n))
            except ValueError:
                pass
    return out


LOC_KEYS = ["locNone", "locHelmet", "locHead", "locNeck", "locTorsoUpper", "locTorsoLower", "locRightArmUpper",
            "locRightArmLower", "locRightHand", "locLeftArmUpper", "locLeftArmLower", "locLeftHand",
            "locRightLegUpper", "locRightLegLower", "locRightFoot", "locLeftLegUpper", "locLeftLegLower",
            "locLeftFoot", "locGun"]


def summary(w):
    """Normalized facts for the Unity weapon controller. Units as in the file: times in seconds, ranges in game units
    (inches), angles in degrees, kick values in the engine's own units (see docs/weapondef.md)."""
    g = w.get
    ft = g("fireTime", 0) or 0
    s = {
        "name": g("displayName"), "type": g("weaponType"), "class": g("weaponClass"), "fireType": g("fireType"),
        "clipSize": g("clipSize"), "maxAmmo": g("maxAmmo"), "startAmmo": g("startAmmo"), "shotCount": g("shotCount"),
        "damage": {"max": g("damage"), "min": g("minDamage"), "maxRange": g("maxDamageRange"),
                   "minRange": g("minDamageRange"), "melee": g("meleeDamage"), "player": g("playerDamage")},
        "hitLocation": {k[3:]: g(k) for k in LOC_KEYS},
        "fireTime": ft, "rpm": round(60.0 / ft, 1) if ft else 0,
        "boltAction": g("boltAction"), "rechamberTime": g("rechamberTime"), "rechamberBoltTime": g("rechamberBoltTime"),
        "reload": {"time": g("reloadTime"), "emptyTime": g("reloadEmptyTime"), "addTime": g("reloadAddTime"),
                   "emptyAddTime": g("reloadEmptyAddTime"), "startTime": g("reloadStartTime"),
                   "endTime": g("reloadEndTime"), "segmented": g("segmentedReload")},
        "raise": {"raise": g("raiseTime"), "first": g("firstRaiseTime"), "drop": g("dropTime"),
                  "quickRaise": g("quickRaiseTime"), "quickDrop": g("quickDropTime"),
                  "emptyRaise": g("emptyRaiseTime"), "emptyDrop": g("emptyDropTime")},
        "ads": {"allowed": g("aimDownSight"), "inTime": g("adsTransInTime"), "outTime": g("adsTransOutTime"),
                "zoomFov": g("adsZoomFov"), "zoomInFrac": g("adsZoomInFrac"), "zoomOutFrac": g("adsZoomOutFrac"),
                "spread": g("adsSpread"), "moveSpeedScale": g("adsMoveSpeedScale"), "overlay": g("adsOverlayShader")},
        "hipSpread": {"standMin": g("hipSpreadStandMin"), "duckedMin": g("hipSpreadDuckedMin"),
                      "proneMin": g("hipSpreadProneMin"), "max": g("hipSpreadMax"),
                      "duckedMax": g("hipSpreadDuckedMax"), "proneMax": g("hipSpreadProneMax"),
                      "fireAdd": g("hipSpreadFireAdd"), "turnAdd": g("hipSpreadTurnAdd"),
                      "moveAdd": g("hipSpreadMoveAdd"), "decay": g("hipSpreadDecayRate"),
                      "duckedDecay": g("hipSpreadDuckedDecay"), "proneDecay": g("hipSpreadProneDecay")},
        "kick": {p + "GunKick" + k: g(p + "GunKick" + k) for p in ("hip", "ads")
                 for k in ("PitchMin", "PitchMax", "YawMin", "YawMax", "Accel", "SpeedMax", "SpeedDecay",
                           "StaticDecay", "ReducedKickBullets", "ReducedKickPercent")},
        "viewKick": {p + "ViewKick" + k: g(p + "ViewKick" + k) for p in ("hip", "ads")
                     for k in ("PitchMin", "PitchMax", "YawMin", "YawMax", "CenterSpeed")},
        "moveSpeedScale": g("moveSpeedScale"), "sprintDurationScale": g("sprintDurationScale"),
        "penetrateType": g("penetrateType"), "impactType": g("impactType"), "rifleBullet": g("rifleBullet"),
        "gunModel": g("gunModel"), "worldModel": g("worldModel"), "handModel": g("handModel"),
        "anims": {k[:-4]: g(k) for k in g_keys(w) if k.endswith("Anim") and g(k)},
        "sounds": {k: g(k) for k in g_keys(w) if (k.endswith("Sound") or k.endswith("SoundPlayer")) and g(k)},
        "notetrackSoundMap": notetrack_map(w),
        "effects": {k: g(k) for k in g_keys(w) if k.endswith("Effect") and g(k)},
        "hudIcon": g("hudIcon"), "killIcon": g("killIcon"),
    }
    return s


def g_keys(w):
    return list(w.keys())


def notetrack_map(w):
    """notetrackSoundMap = lines 'notetrack alias' -> {notetrack: alias} (anim notetrack name -> sound alias)."""
    v = w.get("notetrackSoundMap") or []
    if isinstance(v, str):
        v = [v]
    out = {}
    for line in v:
        p = line.split()
        if len(p) >= 2:
            out[p[0].lower()] = p[1]
    return out


def damage_at(w, dist):
    """Damage of one bullet at 'dist' game units for a torso hit (WaW: full damage up to maxDamageRange, linear fall off to
    minDamage at minDamageRange, constant after; hit location multiplier applied separately)."""
    d0, d1 = w["damage"], w["minDamage"]
    r0, r1 = w["maxDamageRange"], w["minDamageRange"]
    if dist <= r0:
        return float(d0)
    if dist >= r1 or r1 <= r0:
        return float(d1)
    return d0 + (d1 - d0) * (dist - r0) / (r1 - r0)


# ---- key reference ---------------------------------------------------------------------------------------------
# Original meaning (CoD4/CoD5 weapon file editor), grouped. Units: seconds / game units (1 unit = 1 inch) / degrees.
KEYDOC = [
    ("Identity", [
        ("displayName", "localize reference of the weapon name (WEAPON_THOMPSON)"),
        ("modeName", "name of the fire mode shown in the HUD (alt weapons)"),
        ("playerAnimType", "which third person player animation set is used (smg, rifle, pistol, lmg, ...)"),
        ("altWeapon", "weapon file switched to with the alt key (bayonet, grenade launcher, scope, ...)"),
        ("AIOverlayDescription", "SP only"),
        ("weaponType", "bullet / grenade / projectile / gas / melee"),
        ("weaponClass", "rifle / smg / mg / spread (shotgun) / pistol / grenade / rocketlauncher / turret / non player"),
        ("penetrateType", "none / small / medium / large: how much geometry bullets go through (info/bullet_penetration_mp)"),
        ("impactType", "bullet_small / bullet_large / ... selects impact FX and sounds"),
        ("inventoryType", "primary / offhand / item / altmode"),
        ("fireType", "Full Auto / Single Shot / 2-Round Burst / 3-Round Burst / Double Barrel"),
        ("clipType", "bottom / top / left: how the magazine sits (reload anim, shell eject)")]),
    ("Flags", [
        ("twoHanded", "1 = both hands on the weapon"), ("rifleBullet", "1 = rifle round (sound, penetration, tracer class)"),
        ("armorPiercing", "1 = ignores armor"), ("boltAction", "1 = bolt action: rechamber after every shot"),
        ("aimDownSight", "1 = ADS allowed"), ("rechamberWhileAds", "1 = stay in ADS while rechambering"),
        ("noADSAutoReload", "1 = no automatic reload when ADS and empty"),
        ("noPartialReload", "1 = reload only when the clip is empty"),
        ("segmentedReload", "1 = shell by shell reload (reloadStart/reloadAdd/reloadEnd)"),
        ("adsFire", "1 = ADS fire anim plays only in ADS"), ("noAdsWhenMagEmpty", "1 = no ADS with empty magazine"),
        ("avoidDropCleanup", "1 = keep dropped weapon on cleanup"), ("enhanced", "weapon variant flag"),
        ("bayonet", "1 = bayonet attached (melee lunge)"),
        ("cancelAutoHolsterWhenEmpty", "UI/behaviour flag"), ("suppressAmmoReserveDisplay", "HUD"),
        ("laserSightDuringNightvision", "flag"), ("blocksProne", "1 = cannot go prone"),
        ("silenced", "1 = silenced: no world fire sound beyond a small radius"),
        ("mountableWeapon", "1 = deployable on surfaces (bipod)")]),
    ("Aim assist (console, ignore on PC)", [
        ("autoAimRange", "console auto aim range"), ("aimAssistRange", "console aim assist range (hip)"),
        ("aimAssistRangeAds", "console aim assist range (ADS)"),
        ("enemyCrosshairRange", "distance at which the crosshair turns red over an enemy"),
        ("crosshairColorChange", "1 = crosshair colour changes over enemy/friend")]),
    ("Movement", [
        ("moveSpeedScale", "scale of the player's run speed while holding it (1.0 = full)"),
        ("adsMoveSpeedScale", "same while ADS"), ("sprintDurationScale", "scale of sprint time"),
        ("gunMaxPitch", "max pitch the viewmodel lags behind the view (deg)"),
        ("gunMaxYaw", "max yaw the viewmodel lags behind the view (deg)")]),
    ("Ammo", [
        ("lowAmmoWarningThreshold", "fraction of clip at which the low ammo HUD warns"),
        ("ammoName", "ammo type shared between weapons"), ("maxAmmo", "reserve + clip limit"),
        ("startAmmo", "ammo at spawn"), ("clipName", "clip type"), ("clipSize", "rounds per magazine"),
        ("shotCount", "projectiles per shot (shotgun pellets)"), ("dropAmmoMin", "min ammo in the dropped weapon"),
        ("dropAmmoMax", "max ammo in the dropped weapon"), ("reloadAmmoAdd", "rounds added per reload step (0 = fill the clip)"),
        ("reloadStartAdd", "rounds added at the start of a segmented reload")]),
    ("Damage", [
        ("damage", "damage per bullet from 0 to maxDamageRange"),
        ("minDamage", "damage per bullet beyond minDamageRange"),
        ("meleeDamage", "damage of the melee attack"),
        ("maxDamageRange", "distance up to which damage is full (game units = inches)"),
        ("minDamageRange", "distance from which damage is minDamage (linear in between)"),
        ("playerDamage", "damage the weapon deals to the player when fired by script (explosives etc.)"),
        ("locXxx (19 keys)", "hit location multipliers: locNone, locHelmet, locHead, locNeck, locTorsoUpper/Lower, "
                             "loc{Left,Right}Arm{Upper,Lower}, loc{Left,Right}Hand, loc{Left,Right}Leg{Upper,Lower}, "
                             "loc{Left,Right}Foot, locGun. damage_final = damage(dist) * locXxx; the global table "
                             "info/mp_lochit_dmgtable (all 1.0) is multiplied on top")]),
    ("Timing (seconds)", [
        ("fireTime", "time between shots (0.08 = 750 rpm)"), ("fireDelay", "delay between trigger and shot"),
        ("meleeTime", "melee duration"), ("meleeChargeTime", "bayonet lunge duration"),
        ("meleeDelay", "time until the melee hit is applied"), ("meleeChargeDelay", "same for the lunge"),
        ("reloadTime", "tactical reload duration"), ("reloadEmptyTime", "empty reload duration"),
        ("reloadEmptyAddTime", "time after reload start when ammo is added (empty)"),
        ("reloadAddTime", "same for tactical reload"), ("reloadStartTime", "segmented reload: start"),
        ("reloadEndTime", "segmented reload: end"), ("reloadStartAddTime", "segmented: first round add"),
        ("rechamberTime", "cycle time after a shot (bolt action)"), ("rechamberBoltTime", "bolt part of the rechamber"),
        ("dropTime", "put away"), ("raiseTime", "pull out"), ("altDropTime/altRaiseTime", "to/from alt mode"),
        ("quickDropTime/quickRaiseTime", "quick weapon switch times"), ("firstRaiseTime", "pull out after spawn"),
        ("emptyDropTime/emptyRaiseTime", "switch times with empty clip"),
        ("sprintInTime/sprintLoopTime/sprintOutTime", "sprint pose transitions"),
        ("deployTime/breakdownTime", "bipod deploy/pack"), ("nightVision*", "night vision goggles (unused)")]),
    ("Viewmodel pose offsets", [
        ("standMove/Rot*, duckedMove/Rot*, proneMove/Rot*", "viewmodel offset (F,R,U in units) and rotation (P,Y,R in deg) "
                                                           "for the stance while moving, MinSpeed = speed at which it starts"),
        ("posMoveRate/posRotRate/posProne*Rate", "how fast the pose blends"),
        ("sprint*, duckedSprint*", "sprint pose offset/rotation/bob/scale"),
        ("duckedOfs*, proneOfs*", "static stance offsets"),
        ("hipIdleAmount/adsIdleAmount, hipIdleSpeed/adsIdleSpeed", "idle sway amplitude and speed"),
        ("idleCrouchFactor/idleProneFactor", "idle scale per stance")]),
    ("ADS", [
        ("adsSpread", "spread while ADS (0 = perfect)"), ("adsAimPitch", "pitch offset of the aim"),
        ("adsTransInTime/adsTransOutTime", "time to go in/out of ADS"), ("adsTransBlendTime", "anim blend"),
        ("adsReloadTransTime", "ADS exit time when reloading"),
        ("adsCrosshairInFrac/adsCrosshairOutFrac", "ADS fraction at which the crosshair hides/shows"),
        ("adsZoomFov", "field of view while ADS (degrees)"),
        ("adsZoomInFrac/adsZoomOutFrac", "ADS fraction at which the FOV change completes/starts"),
        ("adsBobFactor/adsViewBobMult", "walking bob while ADS"),
        ("adsViewErrorMin/Max", "random aim error while ADS"),
        ("adsDofStart/adsDofEnd", "depth of field distances in ADS"),
        ("adsOverlay*", "sniper scope overlay material, size")]),
    ("Hip spread (degrees)", [
        ("hipSpreadStandMin/DuckedMin/ProneMin", "minimum spread while standing/crouched/prone"),
        ("hipSpreadMax/DuckedMax/ProneMax", "maximum spread"),
        ("hipSpreadFireAdd", "spread added per shot"), ("hipSpreadTurnAdd", "added when turning fast"),
        ("hipSpreadMoveAdd", "added when moving"), ("hipSpreadDecayRate/DuckedDecay/ProneDecay", "recovery per second")]),
    ("Gun kick (viewmodel recoil)", [
        ("hip/adsGunKickPitchMin/Max, YawMin/Max", "random kick impulse range for the viewmodel"),
        ("hip/adsGunKickAccel", "acceleration"), ("hip/adsGunKickSpeedMax", "max kick speed"),
        ("hip/adsGunKickSpeedDecay/StaticDecay", "decay of the speed / of the held offset"),
        ("hip/adsGunKickReducedKickBullets/Percent", "after N bullets the kick is reduced by percent")]),
    ("View kick (camera recoil)", [
        ("hip/adsViewKickPitchMin/Max, YawMin/Max", "random camera kick per shot (units in the engine, see docs)"),
        ("hip/adsViewKickCenterSpeed", "speed at which the view returns to the aim point")]),
    ("Sway", [
        ("swayMaxAngle/LerpSpeed/PitchScale/YawScale/HorizScale/VertScale", "viewmodel lag when turning (hip)"),
        ("adsSway*", "same while ADS"), ("swayShellShockScale", "extra sway under shellshock")]),
    ("AI / reticle / UI", [
        ("fightDist/maxDist", "AI engagement distances"), ("aiVsAiAccuracyGraph/aiVsPlayerAccuracyGraph", "AI only"),
        ("reticleCenter/reticleSide/*Size/reticleMinOfs/hipReticleSidePos", "crosshair material and size"),
        ("hudIcon/killIcon/dpadIcon/ammoCounterIcon(+Ratio)", "HUD materials"), ("ammoCounterClip", "HUD ammo counter style"),
        ("flipKillIcon", "mirror the kill feed icon"), ("fireRumble/meleeImpactRumble", "console rumble")]),
    ("Models", [
        ("gunModel..gunModel16", "viewmodel (+ attachments)"), ("handModel", "arms model (team specific, overridden by character)"),
        ("worldModel..worldModel16", "third person / pickup model"), ("worldClipModel", "collision model of the pickup"),
        ("knifeModel/worldKnifeModel", "melee model"), ("hideTags", "viewmodel tags hidden by default (silencer, aperture)")]),
    ("Animations (xanim names)", [
        ("idleAnim/emptyIdleAnim", "idle"), ("fireAnim/lastShotAnim/adsFireAnim/adsLastShotAnim", "shot"),
        ("rechamberAnim/adsRechamberAnim", "bolt cycle"), ("meleeAnim/meleeChargeAnim", "melee"),
        ("reloadAnim/reloadEmptyAnim/reloadStartAnim/reloadEndAnim", "reload"),
        ("raiseAnim/dropAnim/firstRaiseAnim/quickRaiseAnim/quickDropAnim/emptyRaiseAnim/emptyDropAnim/altRaise/altDrop",
         "pull out / put away"), ("sprintIn/Loop/OutAnim", "sprint"), ("adsUpAnim/adsDownAnim", "ADS transition"),
        ("deployAnim/breakdownAnim", "bipod")]),
    ("Effects / sounds", [
        ("viewFlashEffect/worldFlashEffect", "muzzle flash (first/third person)"),
        ("viewShellEjectEffect/worldShellEjectEffect/viewLastShotEjectEffect/worldLastShotEjectEffect", "shell eject"),
        ("worldClipDropEffect", "dropped magazine"),
        ("*Sound / *SoundPlayer", "sound alias (3rd person / first person '_plr' variant): pickup, ammoPickup, fire, "
                                  "lastShot, emptyFire, melee*, rechamber, reload*, raise, firstRaise, putaway, "
                                  "altSwitch, deploy, breakdown, nightVision*"),
        ("crackSound/whizbySound", "bullet crack / whizby (unset for most weapons)"),
        ("notetrackSoundMap", "lines 'notetrack alias': an xanim notetrack plays the alias (reload clips, bolt). "
                              "Mag in/out, bolt etc. come from here, not from reloadSound")]),
]


def cmd_list(a):
    for n, w in sorted(load_all(a.main).items()):
        print(f"{n:40s} keys {len(w):4d}  {w.get('weaponClass', '')}/{w.get('weaponType', '')}")


def cmd_show(a):
    w = load_all(a.main)[a.weapon]
    if a.all:
        for k, v in w.items():
            print(f"{k} = {v}")
    else:
        print(json.dumps(summary(w), indent=1))


def cmd_table(a):
    allw = load_all(a.main)
    out = {n: {"summary": summary(w), "raw": w} for n, w in allw.items()}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{len(out)} weapon files -> {a.out}")


def cmd_doc(a):
    allw = load_all(a.main)
    ref = {n: allw[n] for n in ("thompson_mp", "kar98k_mp", "m1garand_mp") if n in allw}
    lines = ["# WaW MP weapon files: key reference", "",
             "Source: `main/iw_14.iwd` `weapons/mp/<name>` (139 files, plain text, `WEAPONFILE\\key\\value...`). Parsed by "
             "`tools/waw_weapon.py`. Full infantry weapons have 395 keys. Grenades and utility files have fewer.", "",
             "Units: time seconds, distance game units (1 unit = 1 inch), angles degrees. Values below are the three "
             "prototype weapons.", ""]
    for grp, rows in KEYDOC:
        lines += [f"## {grp}", "", "| key | meaning | " + " | ".join(ref) + " |", "|---|---|" + "---|" * len(ref)]
        for k, d in rows:
            vals = []
            for n, w in ref.items():
                kk = k.split("/")[0].split(",")[0].strip()
                vals.append(str(w.get(kk, "")) if kk in w else "")
            lines.append(f"| `{k}` | {d} | " + " | ".join(v.replace("|", "/") for v in vals) + " |")
        lines.append("")
    lines += ["## Behaviour notes (verified against the three files)", "",
              "- Damage fall off: `damage` up to `maxDamageRange`, linear to `minDamage` at `minDamageRange`, constant after "
              "(`damage_at()` in the tool). Thompson: 40 up to 750, 20 from 1000. Final = that * `locXxx` of the hit "
              "location (Thompson: head/helmet 1.4, everything else 1, gun 0).",
              "- `info/mp_lochit_dmgtable` (rawfile in common_mp.ff) lists the 19 locations with factor 1 each; the weapon "
              "keys are the real multipliers.",
              "- Player health is 100 (`playerDamage` 100 for explosives). Kill needs 3 Thompson torso hits at range < 750.",
              "- Rate of fire = 60 / `fireTime` (Thompson 750 rpm, Kar98k bolt, M1 Garand semi).",
              "- Hip spread min/max in degrees, `hipSpreadFireAdd` per shot, decays with `hipSpreadDecayRate`; "
              "ADS uses `adsSpread` (0 for all three).",
              "- Recoil has two parts: gun kick (viewmodel animation offset) and view kick (camera). WaW mixes both; the "
              "engine integrates the kick with `Accel`/`SpeedMax`/`SpeedDecay`. Exact integrator not documented in the "
              "files, see docs/open_problems in the report.", ""]
    with open(a.out, "w") as f:
        f.write("\n".join(lines))
    print("wrote", a.out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("list"); p.add_argument("main")
    p = sp.add_parser("show"); p.add_argument("main"); p.add_argument("weapon"); p.add_argument("--all", action="store_true")
    p = sp.add_parser("table"); p.add_argument("main"); p.add_argument("--out", required=True)
    p = sp.add_parser("doc"); p.add_argument("main"); p.add_argument("--out", required=True)
    a = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "table": cmd_table, "doc": cmd_doc}[a.cmd](a)


if __name__ == "__main__":
    main()
