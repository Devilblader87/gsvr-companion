"""Minimap pictures of the Ragnarok world (docs/design/ro_ui_b.md): the client's own map images for the minimap window.

The client keeps one picture per map at data/texture/유저인터페이스/map/<map name>.bmp (usually square, 128 x 128 .. 512 x 512). The world stretches it over the
square minimap window (GsvrRoUiB.LMiniX / LMiniY map a GAT cell to a pixel, north up). Everything is read from the player's own client (read only); nothing of the game
is in git. The UI atlas packer of the ui-a lane (GsvrRoWin) takes the pictures under the names GsvrRoUiB.LMiniTex gives: "map_<name>".

  plan(map_names)            -> [(texture name, client path)]  (pure, no IO)
  load(vfs, map_names, size) -> ([(texture name, size, size, rgba)], [missing map names])
"""
from . import img

PATH_MAP = 'data/texture/유저인터페이스/map/%s.bmp'
TEX_PREFIX = 'map_'
DEFAULT_SIZE = 128                                  # the largest minimap window is 192 px; the atlas keeps 128 and the shader stretches (classic client: 128)
_E = (img.ImageError, ValueError, IndexError, KeyError)


def tex_name(map_name):
    """Texture name of the UI atlas, == GsvrRoUiB.LMiniTex."""
    return TEX_PREFIX + map_name


def plan(map_names):
    """Map names (GsvrRoMaps order) -> [(texture name, client path)]; duplicates merged, empty names skipped."""
    out, seen = [], set()
    for n in map_names:
        n = (n or '').strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append((tex_name(n), PATH_MAP % n))
    return out


def load(vfs, map_names, size=DEFAULT_SIZE):
    """Reads and scales the pictures: ([(texture name, size, size, rgba)], [map names the client has no picture for]). A picture that is not a BMP / TGA counts as missing."""
    out, miss = [], []
    for name, path in plan(map_names):
        try:
            if not vfs.exists(path):
                raise KeyError(path)
            w, h, px = img.decode(vfs.read(path), path)
            out.append((name, size, size, img.resize(px, w, h, size, size)))
        except _E:
            miss.append(name[len(TEX_PREFIX):])
    return out, miss
