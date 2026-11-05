"""Warcraft III discovery, ownership gate and map index (mod key "war3"), several versions side by side.

Bring your own game: the companion serves converted WC3 maps only from installs that hold the game's own data:
  * 1.2x (MPQ): war3.mpq AND War3x.mpq (The Frozen Throne; the TD maps are TFT .w3x) in the folder;
  * 1.30/1.31 (CASC): .build.info + Data/ (data, indices, config) in the folder.
WC3 1.x is not a Steam app. Where installs are found (all of them are used, see docs/versions.md of the WC3 repo):
  1. --war3-root DIR (repeatable; GUI setting "Warcraft III folder"), plus
  2. Windows registry HKCU\\Software\\Blizzard Entertainment\\Warcraft III  InstallPathX / InstallPath, plus
  3. the usual folders (Program Files (x86)\\Warcraft III, C:\\Games\\Warcraft III ...).
Map index: one entry per map key (file base name lower-case) with every version that has it:
  maps[key] = {"path", "size", "dir", "ver", "versions": {ver: {"path", "root", "kind"}}}
The top-level fields describe the newest version (default when a request names none). MPQ maps are files below
<install>/Maps; 1.31 maps live inside CASC (path = the CASC path, read by the converter through war3fs).

Fan maps (maintainer 2026-10-21): --war3-maps DIR folders hold maps the player got themselves (never hosted by us). They
are matched by content, not by name: key "sha-<first 16 hex of the SHA-256>" — the world names a fan map only by its
hash (+ display name + a link to the original page). The hashes are cached (path, size, mtime) in
<cache dir>/war3_fan_sha.json; a fan map is converted with the newest install as the game data.
"""
import hashlib, json, os, sys

import runtime, steam

MOD = "war3"
NAME = "Warcraft III: The Frozen Throne"
CORE = ("war3.mpq", "War3x.mpq")
MAP_EXT = (".w3x", ".w3m")


def _registry_paths():
    if sys.platform != "win32":
        return []
    out = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Blizzard Entertainment\Warcraft III") as k:
            for name in ("InstallPathX", "InstallPath"):
                try:
                    out.append(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    pass
    except OSError:
        pass
    return out


def candidates(manual=None):
    """manual: None, one folder, or a list of folders (--war3-root may repeat). Manual folders come first."""
    c = []
    if manual:
        c += [manual] if isinstance(manual, str) else list(manual)
    c += _registry_paths()
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), "C:\\Games", "D:\\Games"):
            if base:
                c.append(os.path.join(base, "Warcraft III"))
        for d in "CDEFGH":                           # a drive's root (the maintainer's 1.31: D:\\Warcraft III)
            c.append(d + ":\\Warcraft III")
    seen, out = set(), []
    for p in c:
        n = os.path.normcase(os.path.normpath(p)) if p else ""
        if n and n not in seen:
            seen.add(n)
            out.append(p)
    return out


def _find_ci(root, fn):
    """Case-insensitive file lookup in one folder (installs differ: War3x.mpq / war3x.mpq)."""
    try:
        for n in os.listdir(root):
            if n.lower() == fn.lower():
                return os.path.join(root, n)
    except OSError:
        pass
    return None


def _kind(root):
    """-> ('mpq'|'casc', reason) or (None, reason)."""
    if os.path.isfile(os.path.join(root, ".build.info")) and os.path.isdir(os.path.join(root, "Data", "data")):
        return "casc", "CASC storage (.build.info + Data)"
    for fn in CORE:
        p = _find_ci(root, fn)
        if not p:
            return None, f"{fn} missing"
        try:
            with open(p, "rb") as fh:
                if len(fh.read(4)) < 4:
                    return None, f"{fn} unreadable"
        except OSError as e:
            return None, f"{fn} unreadable ({e.strerror})"
    return "mpq", "core archives present"


def _fs(root):
    """war3fs view of an install (tools/war3fs.py; tools dir on sys.path like the converter itself)."""
    t = runtime.tools_dir()
    if t not in sys.path:
        sys.path.insert(0, t)
    import war3fs
    return war3fs.open_install(root)


def ownership(manual=None):
    """-> {"war3": {...}} in the same shape as steam.ownership_* entries (appid 0 = not a Steam app), plus
    "installs": [{"root", "kind", "version"}] for every usable install."""
    ent = {"appid": 0, "name": NAME, "allowed": False, "source": "manual-override" if manual else "install",
           "game_root": None, "reason": "no Warcraft III install found", "installs": []}
    why = []
    for root in candidates(manual):
        if not root or not os.path.isdir(root):
            continue
        kind, reason = _kind(root)
        if not kind:
            why.append(f"{root}: {reason}")
            continue
        try:
            ver = _fs(root).version
        except Exception as e:                       # noqa: BLE001 — a broken install must not stop the others
            why.append(f"{root}: {kind} unreadable ({str(e)[:80]})")
            continue
        ent["installs"].append({"root": os.path.normpath(root), "kind": kind, "version": ver})
    if ent["installs"]:
        ent["installs"].sort(key=lambda i: i["version"], reverse=True)
        ent.update(allowed=True, game_root=ent["installs"][0]["root"],
                   reason=", ".join(f"{i['version']} ({i['kind']}) in {i['root']}" for i in ent["installs"]))
    elif why:
        ent["reason"] = "; ".join(why)
    return {MOD: ent}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fan_key(sha):
    return "sha-" + sha[:16]


def _maps_dir(root):
    """<root>/Maps, whatever its case (installs differ: Maps / maps), or None."""
    p = _find_ci(root, "Maps")
    return p if p and os.path.isdir(p) else None


def _norm(p):
    """comparable path: absolute, normalised separators / case, no trailing separator."""
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _under(path, d):
    """path is d itself or below it (both through _norm)."""
    n, dn = _norm(path), _norm(d)
    return n == dn or n.startswith(dn.rstrip(os.sep) + os.sep)


def user_map_dirs(installs):
    """Folders the player copies maps into (maintainer 2026-10-24: new maps in his Maps folder never showed up):
    <install>\\Maps of every install - a 1.30/1.31 CASC install's own listing only covers the maps inside CASC, and
    a 1.2x listing keys by file name (two maps of one name in two folders collide, a name with a space is no key) -
    and the 1.3x documents folder <Documents>\\Warcraft III\\Maps (downloaded / user maps) [ext]. Their maps are
    indexed by content (sha key) like the fan-map folders, unless the install's own listing already has the file
    (MAP-FOLDERS-2). Scanning them also makes the auto rescan (MAP_WATCH_S) see maps copied in later."""
    out = []
    for inst in installs:
        d = _maps_dir(inst["root"])
        if d:
            out.append(d)
    docs = [os.path.join(os.path.expanduser("~"), "Documents")]
    if os.environ.get("OneDrive"):
        docs.append(os.path.join(os.environ["OneDrive"], "Documents"))
    for d in docs:
        out.append(os.path.join(d, "Warcraft III", "Maps"))
    seen, res = set(), []
    for d in out:
        n = _norm(d)
        if os.path.isdir(d) and n not in seen:
            seen.add(n)
            res.append(d)
    return res


def dirs_signature(dirs, files=()):
    """cheap change test for the auto rescan: (path, size, mtime) of every map file below dirs + the given files."""
    sig = []
    for d in dirs or ():
        if not d or not os.path.isdir(d):
            continue
        for dp, dns, fns in os.walk(d):
            for fn in fns:
                if fn.lower().endswith(MAP_EXT):
                    try:
                        st = os.stat(os.path.join(dp, fn))
                    except OSError:
                        continue
                    sig.append((os.path.join(dp, fn), st.st_size, int(st.st_mtime)))
    for p in files:
        try:
            st = os.stat(p)
            sig.append((p, st.st_size, int(st.st_mtime)))
        except OSError:
            pass
    return hash(tuple(sorted(set(sig))))     # set: a file listed twice (in a dir and in files) counts once


def fan_maps(dirs, cache_dir=None, files=(), skip=None, own_dirs=()):
    """-> {key: (sha256, path, size)} for every .w3x / .w3m below the fan-map folders and the extra files (first path
    wins per hash, so the order of `dirs` is a priority). skip(path, scan_dir) -> True leaves a file out (it is
    not even hashed). Files below own_dirs (the player's own folders) are deduplicated by path, not by content: a
    same-bytes copy in a second folder gets its own key (fan_key + path hash)."""
    out = {}
    if not dirs and not files:
        return out
    cpath = os.path.join(cache_dir, "war3_fan_sha.json") if cache_dir else None
    cache = {}
    if cpath and os.path.isfile(cpath):
        try:
            with open(cpath, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
    new = {}
    paths = []
    for d in dirs or ():
        if not d or not os.path.isdir(d):
            continue
        for dp, dns, fns in os.walk(d):
            dns.sort()
            for fn in sorted(fns):
                if fn.lower().endswith(MAP_EXT) and not (skip and skip(os.path.join(dp, fn), d)):
                    paths.append(os.path.join(dp, fn))
    paths += list(files)
    seen = set()                                     # MAP-DUP (maintainer 2026-10-29): one file reached through two spellings of a
    uniq = []                                        # folder (fan folder "D:/WC3/Maps" = the install's "D:\\WC3\\Maps") is one map
    for p in paths:
        n = _norm(p)
        if n not in seen:
            seen.add(n)
            uniq.append(p)
    for p in uniq:
        try:
            st = os.stat(p)
        except OSError:
            continue
        tag = "%d:%d" % (st.st_size, int(st.st_mtime))
        c = cache.get(p)
        sha = c[1] if c and c[0] == tag else None
        if sha is None:
            try:
                sha = _sha256(p)
            except OSError:
                continue
        new[p] = [tag, sha]
        k = fan_key(sha)
        if k in out and _norm(out[k][1]) != _norm(p) and any(_under(p, u) for u in own_dirs):
            k += "-" + hashlib.sha256(_norm(p).encode("utf-8", "replace")).hexdigest()[:8]
        out.setdefault(k, (sha, p, st.st_size))
    if cpath and new != cache:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as fh:
                json.dump(new, fh, sort_keys=True)
        except OSError:
            pass
    return out


# CASC lists its map folders in lower case; the game's list shows them like this [ext: 1.31 map list]
CASC_FOLDERS = {"frozenthrone": "FrozenThrone", "scenario": "Scenario", "download": "Download", "test": "Test"}


LAST_SCAN = {"dirs": [], "files": [], "watch": [], "pre_sig": None}   # what the last build_index scanned / listed (companion auto rescan)


def watch_dirs(installs, fan_dirs=None):
    """every folder whose map files feed the index, for the companion's change watch (INDEX-REV, maintainer 2026-10-27): the
    install's own Maps tree too (an MPQ install lists it directly, it is not a fan folder), and the player's folders
    even when they do not exist yet (a created folder with maps in it changes the signature)."""
    out = list(fan_dirs or [])
    for inst in installs:
        if inst.get("kind") == "casc":
            out.append(os.path.join(inst["root"], "Maps"))
        else:
            out += [os.path.join(inst["root"], "Maps"), os.path.join(inst["root"], "maps")]
    docs = [os.path.join(os.path.expanduser("~"), "Documents")]
    if os.environ.get("OneDrive"):
        docs.append(os.path.join(os.environ["OneDrive"], "Documents"))
    out += [os.path.join(d, "Warcraft III", "Maps") for d in docs]
    seen, res = set(), []
    for d in out:
        k = os.path.normcase(os.path.abspath(d))
        if d and k not in seen:
            seen.add(k)
            res.append(d)
    return res


def watch_signature():
    """signature of everything the last build_index looked at (changes when a map is added / removed / changed)"""
    return dirs_signature(LAST_SCAN["dirs"] + LAST_SCAN.get("watch", []), LAST_SCAN["files"])


def build_index(own, fan_dirs=None, cache_dir=None):
    """-> {"war3": {"appid","name","game_root","maps","skipped","versions"}} when allowed (see module doc)."""
    ent = own.get(MOD)
    installs = ent["installs"] if ent and ent.get("allowed") else []
    # watch list + signature BEFORE the (slow) scan: a map copied while we hash is not in the baseline -> one more rescan;
    # also set when war3 is denied / not installed, so a later install is noticed (INDEX-REV review)
    pre_dirs = list(fan_dirs or []) + user_map_dirs(installs)
    LAST_SCAN["dirs"], LAST_SCAN["files"] = pre_dirs, []
    LAST_SCAN["watch"] = [w for w in watch_dirs(installs, fan_dirs)
                          if not any(os.path.normcase(os.path.abspath(w)) == os.path.normcase(os.path.abspath(p)) or
                                     os.path.normcase(os.path.abspath(w)).startswith(os.path.normcase(os.path.abspath(p)) + os.sep)
                                     for p in pre_dirs)]   # no tree walked twice
    LAST_SCAN["pre_sig"] = watch_signature()
    if not ent or not ent["allowed"]:
        return {}
    maps, skipped = {}, 0
    claimed = set()                                  # files the install's own listing already indexed (by name key)
    for inst in ent["installs"]:                     # newest first: its entry becomes the default
        try:
            listing = _fs(inst["root"]).maps()
        except Exception:                            # noqa: BLE001
            continue
        for key, (display, path) in sorted(listing.items()):
            if not steam.safe_map_name(key):
                if inst["kind"] != "mpq":
                    skipped += 1                     # CASC name without a safe key: not servable
                continue                             # MPQ ("Wintermaul V1.51.w3x"): the Maps scan indexes it by content
            if inst["kind"] == "mpq":
                size = os.path.getsize(path) if os.path.isfile(path) else 0
                d = os.path.relpath(os.path.dirname(path), inst["root"]).replace("\\", "/")
                claimed.add(_norm(path))
            else:                                    # CASC: the folder below maps\ like the game's list
                size = 0
                sub = path.split(":", 1)[-1].replace("\\", "/").split("/")[1:-1]
                d = "casc/" + "/".join(CASC_FOLDERS.get(x.lower(), x) for x in sub) if sub else "casc"
            e = maps.get(key)
            if e is None:
                e = maps[key] = {"path": path, "size": size, "dir": d, "ver": inst["version"], "root": inst["root"],
                                 "title": os.path.splitext(display)[0], "versions": {}}
            e["versions"].setdefault(inst["version"], {"path": path, "root": inst["root"], "kind": inst["kind"]})
    if ent["installs"]:                              # the world's start menu + lobby (glue screens, no map)
        newest = ent["installs"][0]
        maps["__glue"] = {"path": "__glue", "size": 0, "dir": "glue", "ver": newest["version"], "root": newest["root"],
                          "versions": {i["version"]: {"path": "__glue", "root": i["root"], "kind": i["kind"]}
                                       for i in ent["installs"]}}
    # MAP-FOLDERS-2 (maintainer 2026-10-27, --war3-maps = his install root listed all 267 maps again under "Download"):
    # the player's own folders (every install's Maps, Documents) come BEFORE the fan folders, so a file reached by
    # both keeps its real folder below Maps; one entry per map file / content, the install entry wins.
    user = user_map_dirs(ent["installs"])
    fans = [d for d in (fan_dirs or []) if d]
    roots = [i["root"] for i in ent["installs"]]

    mdirs = {r: _maps_dir(r) for r in roots}         # listed once, not per scanned file

    def skip(path, scan_dir):
        if _norm(path) in claimed:                   # already an install entry
            return True
        for r in roots:                              # reached through a folder that contains the install (root, drive):
            if _under(path, r) and _under(r, scan_dir) and not (mdirs[r] and _under(path, mdirs[r])):
                return True                          # only <install>/Maps is the game's map list (Campaigns, ghost/maps ...)
        return False

    dirs = user + fans
    LAST_SCAN["dirs"], LAST_SCAN["files"] = dirs, []
    own_sha = {}                                     # install file -> content hash, only for files of a fan copy's size

    def same_as_install(sha, size):
        for k, e in maps.items():                    # a fan copy of an install map (same bytes) is no second entry
            p = e["path"]
            if k != "__glue" and e["size"] == size and os.path.isfile(p):
                if p not in own_sha:
                    own_sha[p] = _sha256(p)
                if own_sha[p] == sha:
                    return True
        return False

    for key, (sha, path, size) in sorted(fan_maps(dirs, cache_dir, (), skip, user).items()):
        if key in maps or (not any(_under(path, u) for u in user) and same_as_install(sha, size)):
            continue
        newest = ent["installs"][0]
        d = "fan"                                    # directly in a fan folder (or outside every scanned folder): "Download"
        for u in dirs:                               # first match = priority order
            if not _under(path, u):
                continue
            rel = os.path.relpath(os.path.dirname(path), u).replace("\\", "/")
            if u in user:                            # the game's list: Maps/<sub folders>
                d = "Maps" if rel == "." else "Maps/" + rel
            elif rel != ".":                         # fan folder sub-folder: <fan folder name>/<sub folders>
                d = "Maps/" + os.path.basename(os.path.normpath(u)) + "/" + rel
            break
        maps[key] = {"path": path, "size": size, "dir": d, "ver": newest["version"], "root": newest["root"],
                     "title": os.path.splitext(os.path.basename(path))[0], "sha256": sha, "versions": {i["version"]: {"path": path, "root": i["root"], "kind": i["kind"]}
                                                 for i in ent["installs"]}}
    add_map_info(maps, [i["root"] for i in ent["installs"]], cache_dir)
    annotate_folders(maps)
    return {MOD: {"appid": 0, "name": NAME, "game_root": ent["game_root"], "maps": maps, "skipped": skipped,
                  "versions": [i["version"] for i in ent["installs"]]}}


def _info_mod():
    t = runtime.tools_dir()
    if t not in sys.path:
        sys.path.insert(0, t)
    import w3info
    return w3info


def annotate_folders(maps):
    """MAP-FOLDERS (index format 2): every map gets "folder" (path below the Maps folder, "" = flat / unknown) and, for
    the flat ones, "cat" classic / fun (tools/w3mapfolders.py). Without the module the index stays format 1."""
    try:
        t = runtime.tools_dir()
        if t not in sys.path:
            sys.path.insert(0, t)
        import w3mapfolders
    except ImportError:
        return False
    w3mapfolders.annotate(maps)
    return True


def add_map_info(maps, roots, cache_dir=None, log=None):
    """The lobby's map info (tools/w3info.py: title, players, size, tileset, melee, description) for every map,
    cached by path + size + mtime in <cache dir>/war3_info.json (CASC maps: path + the install's .build.info)."""
    cpath = os.path.join(cache_dir, "war3_info.json") if cache_dir else None
    cache = {}
    if cpath and os.path.isfile(cpath):
        try:
            with open(cpath, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
    new, mod, fss, names = {}, None, {}, None
    for key, m in maps.items():
        if key == "__glue":
            continue
        p = m["path"]
        if os.path.isfile(p):
            st = os.stat(p)
            tag = "i2|%s|%d|%d" % (p, st.st_size, int(st.st_mtime))
        else:
            bi = os.path.join(m["root"], ".build.info")
            tag = "i2|%s|casc|%d" % (p, int(os.path.getmtime(bi)) if os.path.isfile(bi) else 0)
        info = cache.get(tag)
        if info is None:
            try:
                mod = mod or _info_mod()
                fs = fss.get(m["root"]) or fss.setdefault(m["root"], _fs(m["root"]))
                if names is None and roots:              # the game's size / tileset words (newest install)
                    names = mod.Names(fss.get(roots[0]) or fss.setdefault(roots[0], _fs(roots[0])))
                if os.path.isfile(p):
                    info = mod.map_info(p, names=names)
                else:
                    info = mod.map_info(data=fs.read_map(p), names=names)
            except Exception:                        # noqa: BLE001 — no info is fine, the map still loads
                info = None
            info = info or {}
        new[tag] = info
        if info:
            m["info"] = info
            if info.get("title") and m.get("dir") != "fan":
                m["title"] = info["title"]
    if cpath and new != cache:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as fh:
                json.dump(new, fh, sort_keys=True, ensure_ascii=False)
        except OSError:
            pass


MUSIC_RE = None


def music_bytes(index_entry, path, log=print):
    """W3VR music (docs/sound_plan.md S5): the mp3 at `path` in the newest war3 install of the index, unchanged —
    the world plays it through VRChat's video player, which decodes mp3 itself (no decoder here). Only
    'Sound\\…\\*.mp3' paths of plain characters are served (the request comes from a log line). -> bytes or None."""
    import re
    global MUSIC_RE
    if MUSIC_RE is None:
        MUSIC_RE = re.compile(r"^sound\\[a-z0-9_ ()\\.-]{1,120}\.mp3$", re.I)
    if not isinstance(path, str) or not MUSIC_RE.match(path) or ".." in path:
        return None
    root = (index_entry or {}).get("game_root")
    if not root:
        return None
    pre = os.path.join(root, "music", *path.lower().split("\\"))   # prebuilt mode (no install): copied mp3 files
    if os.path.isfile(pre):
        with open(pre, "rb") as fh:
            return fh.read()
    try:
        return _fs(root).read(path, missing_ok=True)
    except Exception as e:                           # noqa: BLE001
        log(f"music: {path} unreadable: {e}")
        return None


def previews(index_entry, cache_dir=None, log=print):
    """-> path of the lobby's preview atlas (tools/w3info.previews_pack) for the war3 maps of the index, built once
    per map set (file name = hash of the map list) in the cache dir; a prebuilt war3__previews.bin is used as is."""
    maps = index_entry.get("maps", {})
    pre = os.path.join(index_entry.get("game_root") or "", "war3__previews.bin")
    if all(m.get("dir") == "prebuilt" for k, m in maps.items() if k != "__glue") and os.path.isfile(pre):
        return pre
    h = hashlib.sha1()
    for k in sorted(maps):
        h.update(("%s|%s|%s\n" % (k, maps[k].get("path"), maps[k].get("size"))).encode("utf-8"))
    out = os.path.join(cache_dir or ".", "war3-previews-%s.bin" % h.hexdigest()[:16])
    if os.path.isfile(out):
        return out
    mod = _info_mod()
    ent_roots = {m.get("root") for m in maps.values() if m.get("root")}
    fss = {r: _fs(r) for r in ent_roots}
    newest = index_entry.get("game_root")
    art = mod.Art(fss.get(newest) or _fs(newest))
    items = []
    for k, m in sorted(maps.items()):
        if k == "__glue":
            continue
        try:
            p = m["path"]
            arc = mod.mpq.MPQ(p) if os.path.isfile(p) else mod.mpq.MPQ(fss[m["root"]].read_map(p))
            items.append((k, arc))
        except Exception:                            # noqa: BLE001 — no preview for a broken map
            continue
    data = mod.previews_pack(items, art, log=log)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    for fn in os.listdir(os.path.dirname(out)):      # an older map set's atlas
        if fn.startswith("war3-previews-") and fn.endswith(".bin"):
            try:
                os.remove(os.path.join(os.path.dirname(out), fn))
            except OSError:
                pass
    with open(out + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(out + ".tmp", out)
    return out


def pick(map_entry, ver=None):
    """(path, root, version) of the requested version, else the default (newest) one."""
    v = map_entry["versions"].get(ver) if ver else None
    if v:
        return v["path"], v["root"], ver
    return map_entry["path"], map_entry["root"], map_entry["ver"]
