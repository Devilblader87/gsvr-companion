"""titles.txt → the GSV3 "TITL" section (tools/pack.py): the client text messages a map's env_message entities name.

titles.txt (<mod>/titles.txt, fallback valve/titles.txt) [ext: client TextMessageParse, as the file's own header
documents]: `$position x y` (0..1, −1 = centred), `$effect n` (0 fade, 1 flicker/credits, 2 write-out), `$color r g b`,
`$color2 r g b`, `$fadein s` (per character with effect 2), `$fadeout s`, `$holdtime s`, `$fxtime s`; a directive
holds for every message after it until changed. A message is a name line followed by `{`, the text lines and `}`.
`//` lines are comments. Names compare case-insensitively [inf].

TITL layout (count = N messages):
  int32 N, int32 textBytes (padded), int32 0, int32 0
  N × 16 four-byte fields: 0 nameOff, 1 nameLen, 2 textOff, 3 textLen (into the text blob), 4 effect,
    5 color (r | g << 8 | b << 16 | 255 << 24), 6 color2 (same), 7 x (f32), 8 y (f32), 9 fadein (f32),
    10 fadeout (f32), 11 holdtime (f32), 12 fxtime (f32), 13–15 0
  text blob: ASCII (other bytes → '?'), names upper-cased as in the file, lines joined with "\n", padded to 4
"""
import os, re, struct


def parse(text):
    """→ {NAME_UPPER: dict(name, text, effect, color, color2, x, y, fadein, fadeout, holdtime, fxtime)}."""
    cur = dict(effect=0, color=(255, 255, 255), color2=(255, 255, 255), x=-1.0, y=-1.0, fadein=0.0, fadeout=0.0,
               holdtime=0.0, fxtime=0.0)
    out = {}
    lines = text.replace("\r", "").split("\n")
    i = 0
    name = None
    while i < len(lines):
        ln = lines[i].strip()
        i += 1
        if not ln or ln.startswith("//"):
            continue
        if ln.startswith("$"):
            w = ln.split()
            k = w[0][1:].lower()
            nums = []
            for t in w[1:]:
                try:
                    nums.append(float(t))
                except ValueError:
                    break
            if k == "position" and len(nums) >= 2:
                cur["x"], cur["y"] = nums[0], nums[1]
            elif k == "effect" and nums:
                cur["effect"] = int(nums[0])
            elif k in ("color", "color2") and len(nums) >= 3:
                cur[k] = tuple(int(max(0, min(255, v))) for v in nums[:3])
            elif k in ("fadein", "fadeout", "holdtime", "fxtime") and nums:
                cur[k] = nums[0]
            continue
        if ln == "{" and name:
            body = []
            while i < len(lines) and lines[i].strip() != "}":
                body.append(lines[i].rstrip())
                i += 1
            i += 1
            out[name.upper()] = dict(cur, name=name, text="\n".join(body))
            name = None
            continue
        name = ln.split()[0]
    return out


def find(game_root, mod):
    from gamedirs import content_dirs
    for root in content_dirs(game_root, mod):
        p = os.path.join(root, "titles.txt")
        if os.path.isfile(p):
            return p
    return None


def wanted(ents):
    names = []
    for e in ents:
        if e.get("classname") == "env_message" and e.get("message"):
            n = e["message"].strip().upper()
            if n not in names:
                names.append(n)
    return names


def load(ents, game_root, mod, log=print):
    """→ list of message dicts referenced by the map (missing names logged)."""
    need = wanted(ents)
    if not need or not game_root:
        return []
    p = find(game_root, mod)
    if not p:
        log(f"titles: no titles.txt for {len(need)} env_message names")
        return []
    with open(p, "rb") as fh:
        table = parse(fh.read().decode("latin-1"))
    got = [table[n] for n in need if n in table]
    miss = [n for n in need if n not in table]
    log(f"titles: {len(got)} of {len(need)} env_message texts from {p}" + (f" (missing {', '.join(miss[:8])})" if miss else ""))
    return got


def _rgba(c):
    return c[0] | c[1] << 8 | c[2] << 16 | 255 << 24


def section(msgs):
    """TITL blob (see module doc)."""
    blob = b""
    recs = b""
    for m in msgs:
        nb = m["name"].upper().encode("ascii", "replace")
        tb = m["text"].encode("ascii", "replace")
        no = len(blob); blob += nb
        to = len(blob); blob += tb
        c1 = _rgba(m["color"]); c2 = _rgba(m["color2"])
        recs += struct.pack("<5i2I6f3i", no, len(nb), to, len(tb), m["effect"], c1, c2, m["x"], m["y"], m["fadein"],
                            m["fadeout"], m["holdtime"], m["fxtime"], 0, 0, 0)
    blob += b"\0" * (-len(blob) % 4)
    return struct.pack("<4i", len(msgs), len(blob), 0, 0) + recs + blob
