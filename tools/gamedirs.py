# Content folders of a GoldSrc mod, in the order the tools search them (WADs, sounds, titles.txt, sky, materials.txt,
# .nav). One place so pack.py, wad3.py, sounds.py, titles.py and sky.py agree with the companion's map index
# (companion/steam.py MOD_DIR_SUFFIXES).
#
# Order: <mod>_addon, <mod>, <mod>_downloads, then valve. The addon folder is the player's own content and wins; the
# downloads folder holds what game servers pushed and comes after the mod's own files, so a server download never
# shadows a file the game ships [inf: Steampipe GoldSrc search path, the same order the companion has always used for
# map names]. Sven Co-op 5 ships no valve folder; it is skipped when it does not exist. _hd / _<language> folders are
# not searched (they hold models and localised briefings, no BSP / WAD / map sounds the tools need).
import os

CONTENT_SUFFIXES = ("_addon", "", "_downloads")


def content_dirs(game_root, mod, fallback="valve"):
    """→ existing <game_root>/<dir> paths in search order (no duplicates)."""
    if not game_root:
        return []
    names = [mod + s for s in CONTENT_SUFFIXES]
    if fallback and fallback not in names:
        names.append(fallback)
    out = []
    for n in names:
        p = os.path.join(game_root, n)
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def content_names(game_root, mod, fallback="valve"):
    """→ folder names (e.g. ["svencoop_addon", "svencoop"]) of content_dirs."""
    return [os.path.basename(p) for p in content_dirs(game_root, mod, fallback)]


def root_of_bsp(bsp_path):
    """<root>/<dir>/maps/<map>.bsp → <root> (the companion passes the BSP of any content folder)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(bsp_path))))
