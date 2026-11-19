"""The official GSVR worlds the companion can open (one row per world in the app) + the vrchat:// launch link.

WORLDS mirrors unity/Assets/GoldSrcVR/Editor/gsvr_worlds.json (worldId + name); test_firstrun.py compares the two when the repo is present.
Game id -> world: Half-Life, Opposing Force and the other GoldSrc singleplayer games share the Half-Life hub world.
Nothing here touches the network; opening the link hands it to VRChat through the OS.
"""
import os
import re
import sys

DISCORD_URL = "https://discord.gg/nc2rw6Snx"
WORLD_ID_RE = re.compile(r"^wrld_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# key -> (world id, display name, game ids that unlock it)
WORLDS = {
    "rose": ("wrld_3a03fa73-b6c8-4116-8785-40fbe353b9b2", "GSVR - ROSE Online ローズオンライン", ("rose",)),
    "hl": ("wrld_b0402df7-7bfb-44b2-9406-7aafd8e67e1a", "GSVR - Half-Life", ("valve", "gearbox", "bshift", "svencoop")),   # Sven Co-op is part of the hub, no own row
    "war3": ("wrld_71563253-4115-40d3-b165-7cdba23bff06", "Warcraft III VR (bring your own game)", ("war3",)),   # W3VR (own unity project, ~/dev/decomp/warcraft3)
    "cs": ("wrld_5ab8566b-7e6a-4cfb-a0ac-b6d099b94ffb", "GSVR - Counter-Strike", ("cstrike",)),
}
WORLD_ORDER = ("rose", "hl", "cs", "war3")
SVEN_SCENE_WORLD_ID = "wrld_19508790-4711-46f7-bf73-e5b96f944db9"   # old separate Sven scene: no row/button, id kept for reference


def world_for_game(game_id):
    """game id ('valve', 'rose', ...) -> world key, or None (no world for this game)"""
    for k, (_wid, _name, gids) in WORLDS.items():
        if game_id in gids:
            return k
    return None


def launch_url(world_id):
    """vrchat://launch link for a world id; ValueError for anything that is not a wrld_ uuid"""
    if not WORLD_ID_RE.match(world_id or ""):
        raise ValueError("not a world id: %r" % (world_id,))
    return "vrchat://launch?ref=vrchat.com&id=%s" % world_id


def open_link(url):
    """hand the link to the OS (VRChat registers vrchat://). -> True when the launcher was started"""
    if not url.startswith(("vrchat://", "https://")):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(url)                                   # noqa: S606 (fixed scheme, checked above)
        else:
            import webbrowser
            return bool(webbrowser.open(url))
        return True
    except OSError:
        return False
