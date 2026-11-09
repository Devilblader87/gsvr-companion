"""--prebuilt-dir mode: serve packs that were built elsewhere, no game files needed.

File names: <mod>__<map>.bin (e.g. cstrike__de_dust2.bin). With --prebuilt-mod MOD, bare <map>.bin files are
also accepted and count as MOD (so `--prebuilt-dir work/cs --prebuilt-mod cstrike` serves work/cs/de_dust2.bin
as cstrike/de_dust2). Mods must match steam.SAFE_NAME, maps steam.safe_map_name; anything else in the folder is ignored. Paths come only
from the directory listing, never from request input.

Meant for test clients (e.g. a VRChat test VM without the game installed) — the ownership gate is replaced by
"the operator put these packs here". Every pack is re-validated (builder.validate_pack) each time it is served.
"""
import hashlib, json, os, time

import builder, steam


def scan(directory, default_mod=None):
    """-> (ownership, index) in the same shapes as steam.ownership_* / steam.build_index."""
    directory = os.path.abspath(directory)
    maps_by_mod = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for fn in names:
        base, ext = os.path.splitext(fn)
        if ext.lower() != ".bin":
            continue
        if "__" in base:
            mod, _, name = base.partition("__")
        elif default_mod:
            mod, name = default_mod, base
        else:
            continue
        mod, name = mod.lower(), name.lower()
        if mod == "war3" and name == "previews":
            continue                       # war3__previews.bin = the lobby's preview atlas, not a map (BUGS L-6)
        if not (steam.SAFE_NAME.match(mod) and steam.safe_map_name(name)):
            continue
        p = os.path.join(directory, fn)
        if not os.path.isfile(p):
            continue
        maps = maps_by_mod.setdefault(mod, {})
        if name in maps and "__" not in base:
            continue                       # explicit <mod>__<map>.bin wins over a bare duplicate
        maps[name] = {"path": p, "size": os.path.getsize(p), "dir": "prebuilt"}
        side = os.path.splitext(p)[0] + ".info.json"     # optional lobby info next to a prebuilt pack (WC3)
        if os.path.isfile(side):
            try:
                with open(side, encoding="utf-8") as fh:
                    inf = json.load(fh)
                if isinstance(inf, dict):
                    maps[name]["info"] = inf
                    if inf.get("title"):
                        maps[name]["title"] = str(inf["title"])
            except (OSError, ValueError):
                pass
    own, idx = {}, {}
    for appid, (title, mod) in steam.KNOWN_APPS.items():
        own[mod] = {"appid": appid, "name": title, "allowed": False, "source": "prebuilt-dir",
                    "game_root": None, "reason": "no prebuilt packs for this mod"}
    for mod, maps in maps_by_mod.items():
        appid = steam.MOD_TO_APP.get(mod)
        title = steam.KNOWN_APPS[appid][0] if appid in steam.KNOWN_APPS else mod
        own[mod] = {"appid": appid, "name": title, "allowed": True, "source": "prebuilt-dir",
                    "game_root": directory, "reason": f"--prebuilt-dir ({len(maps)} packs)"}
        idx[mod] = {"appid": appid, "name": title, "game_root": directory, "maps": maps}
    return own, idx


class Prebuilt:
    """Drop-in for builder.Converter: 'building' = validating the prebuilt file and serving it in place."""

    def __init__(self, directory):
        self.converter = os.path.abspath(directory)
        self.template = "(prebuilt packs, no converter)"
        self.version = "prebuilt"

    def build(self, cache_dir, mod, mapname, bsp, game_root, mod_dir, log=print, extra=()):
        t0 = time.monotonic()
        size = builder.validate_pack(bsp)            # raises RuntimeError with the reason
        h = hashlib.sha1()
        with open(bsp, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
        return dict(path=bsp, size=size, key=f"prebuilt-{mod}-{mapname}-{h.hexdigest()[:16]}", cached=True,
                    seconds=round(time.monotonic() - t0, 3))
