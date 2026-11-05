"""GAME-FOLDERS: per game, where the companion found it and how (status.json "games", GUI table).

games_section() is the single source of truth: the running companion calls it with its own ownership (status.json),
the GUI calls local_view() (same detection functions) only while the companion is not running.
Entry: {"id","name","found","root","source","mods","setting","hint"[,"version","maps_root"]}
source: steam-manifest | manual | registry | install-folder | non-steam-scan | none
"""
import os

import steam
import war3

SETTING_HL, SETTING_W3 = "hl_root", "war3_root"
HINT_HL = "Nicht gefunden - Steam-Version installieren oder mit 'Aendern...' den Ordner mit hl.exe waehlen."
HINT_W3 = "Nicht gefunden - mit 'Aendern...' den Warcraft-III-Ordner waehlen."


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _as_list(v):
    return [v] if isinstance(v, str) else list(v or [])


def games_section(own, war3_manual=None, war3_maps=None, nonsteam=None):
    """own: ownership dict (steam.ownership_* + war3.ownership). nonsteam: steam.detect_non_steam() candidates."""
    out = []
    for appid, (name, mod) in steam.KNOWN_APPS.items():
        e = own.get(mod) or {}
        g = {"id": mod, "name": name, "found": bool(e.get("allowed")), "root": "", "source": "none", "mods": [],
             "setting": SETTING_HL, "hint": ""}
        if g["found"]:
            g["root"] = e.get("game_root") or ""
            g["source"] = "manual" if e.get("source") == "manual-override" else "steam-manifest"
            g["mods"] = steam.mods_in_root(g["root"]) if g["root"] else []
        else:
            cand = next((c for c in (nonsteam or []) if mod in c.get("mods", [])), None)
            if cand:
                g.update(root=cand["root"], source="non-steam-scan", mods=list(cand.get("mods", [])),
                         hint="Ordner gefunden, Besitz nicht pruefbar - mit 'Aendern...' bestaetigen.")
            else:
                g["hint"] = HINT_HL
        out.append(g)
    e = own.get(war3.MOD)
    if e is not None:
        g = {"id": war3.MOD, "name": "Warcraft III", "found": bool(e.get("allowed")), "root": "", "source": "none",
             "mods": [], "setting": SETTING_W3, "hint": ""}
        if g["found"]:
            g["root"] = e.get("game_root") or ""
            inst = (e.get("installs") or [{}])[0]
            g["version"] = str(inst.get("version", ""))
            if any(_same(m, g["root"]) for m in _as_list(war3_manual) if m):
                g["source"] = "manual"
            elif any(_same(p, g["root"]) for p in war3._registry_paths() if p):
                g["source"] = "registry"
            else:
                g["source"] = "install-folder"
            maps = _as_list(war3_maps)
            if maps:
                g["maps_root"] = maps[0]
        else:
            g["hint"] = HINT_W3
        out.append(g)
    return out


def local_view(game_root=None, war3_root=None, war3_maps=None, scan=True):
    """Same detection as the companion's rescan(), for the GUI while the companion is not running."""
    if game_root:
        own = steam.ownership_manual(game_root)
    else:
        own = steam.ownership_from_steam(steam.find_steam_root())
    try:
        own.update(war3.ownership(war3_root or None))
    except Exception:                                         # noqa: BLE001 - a broken install must not break the GUI
        pass
    ns = []
    if scan and not own.get("cstrike", {}).get("allowed"):
        try:
            ns = steam.detect_non_steam()
        except Exception:                                     # noqa: BLE001
            ns = []
    return games_section(own, war3_root or None, [war3_maps] if war3_maps else None, ns)
