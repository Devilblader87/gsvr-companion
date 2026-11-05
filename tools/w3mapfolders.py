"""MAP-FOLDERS: folder path + category of the maps in the companion's index.json (format "fv": 2).

Source of truth is this file in the warcraft3 repo; the companion gets a copy through tools/sync_war3.sh.
Pure functions, no I/O, so the indexing rules are unit-tested here (test_w3mapfolders.py).

Index entry fields added by `annotate` (the old "dir" stays as it was, so an old world keeps working):
  "folder"  path of the map's folder below the install's Maps folder, "/" separated, "" = top level
            (Maps/FrozenThrone/x.w3x -> "FrozenThrone"; Maps/Mine/TD/y.w3x -> "Mine/TD"; CASC listing: same, below maps\\)
  "cat"     "classic" | "fun", only for maps whose folder is "" (unknown or flat): the world shows them in the virtual
            folders "Classic" (Blizzard melee / scenario maps) and "Fun Maps" (everything not shipped by Blizzard)
An old companion sends neither field: the world derives the folder from "dir" (docs/NOTES_glue.md, MAP-FOLDERS).
"""
import re

INDEX_FORMAT = 2                                      # index.json "fv"; absent = 1 (no folder / cat)
CLASSIC, FUN = "classic", "fun"
TITLES = {CLASSIC: "Classic", FUN: "Fun Maps"}

# Folders Blizzard ships maps in (the game's list): anything below them is a Blizzard map.
BLIZZARD_FOLDERS = ("frozenthrone", "scenario", "test", "campaign", "campaigns", "battlenet", "blizzard", "pregame")

# Blizzard's own melee / scenario maps (RoC + TFT + patches), normalised (see norm()). Only used for maps that sit
# loose in a Maps folder; the CASC listing and the Blizzard folders count as Blizzard without this table.
_BLIZZARD_NAMES = """
 lost temple|turtle rock|twisted meadows|gnoll wood|echo isles|terenas stand|tidewater glades|northern isles|amazonia
 concealed hill|last refuge|secret valley|avalanche|murgul oasis|plunder isle|hammerfall|battleground|ancient isles
 booty bay|cherryville|dark forest|deathrose|scorched basin|rainbow valley|rolling hills|sungrove|timbermaw hold
 divide and conquer|the crossing|marketplace|market square|stranglethorn vale|goldshire|hillsbrad
"""


def norm(name):
    """File / display name -> comparable key: no extension, no "(2)" player count prefix, no version suffix, no
    spaces / punctuation / case ("(2)LostTemple v1.2.w3x" == "Lost Temple")."""
    n = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    n = re.sub(r"\.(w3x|w3m)$", "", n, flags=re.I)
    n = re.sub(r"^\(\d+\)\s*", "", n)
    n = re.sub(r"[\s_]+v?\d+(\.\d+)*[a-z]?$", "", n, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", n.lower())


BLIZZARD_MAPS = frozenset(norm(x) for x in _BLIZZARD_NAMES.replace("\n", "|").split("|") if x.strip())


def folder_of(d):
    """Index "dir" -> folder path below Maps ("" = top / unknown).
    "Maps" / "Maps/FrozenThrone" (MPQ installs and the player's folders), "casc" / "casc/Scenario" (1.31 listing),
    "fan" (--war3-maps DIR) -> "Download", "glue" / "prebuilt" / "" -> ""."""
    if not d:
        return ""
    p = d.replace("\\", "/").strip("/")
    low = p.lower()
    if low in ("maps", "casc", "glue", "prebuilt", "."):
        return ""
    if low == "fan":
        return "Download"
    for pre in ("maps/", "casc/"):
        if low.startswith(pre):
            p = p[len(pre):]
            break
    return "/".join(x for x in p.split("/") if x and x != ".")


def is_blizzard(folder, d, title):
    """True for a map Blizzard ships: from the CASC listing, below a Blizzard folder, or by its well-known name."""
    if (d or "").replace("\\", "/").lower().split("/")[0] == "casc":
        return True
    first = folder.split("/")[0].lower() if folder else ""
    if first in BLIZZARD_FOLDERS:
        return True
    return bool(title) and norm(title) in BLIZZARD_MAPS


def classify(entry):
    """Index entry (dir, title, sha256 ...) -> (folder, cat); cat is None when the folder is known."""
    folder = folder_of(entry.get("dir", ""))
    if folder:
        return folder, None
    blizz = is_blizzard(folder, entry.get("dir", ""), entry.get("title") or entry.get("name") or "")
    return "", CLASSIC if blizz else FUN


def annotate(maps):
    """Adds "folder" / "cat" to every map entry of one mod's index (the "__glue" pseudo map is skipped)."""
    for key, e in maps.items():
        if key == "__glue":
            continue
        e["folder"], cat = classify(e)
        if cat:
            e["cat"] = cat
        else:
            e.pop("cat", None)
    return maps


def effective_folder(e):
    """The folder the world shows the map in (virtual Classic / Fun Maps for top-level maps)."""
    return e.get("folder") or TITLES.get(e.get("cat"), "")


def sort_key(name, e):
    """Index order: folder path, then title - the world lists folders in the order it meets them."""
    return (effective_folder(e).lower(), (e.get("title") or name).lower(), name)
