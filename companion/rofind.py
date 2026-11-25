"""Ragnarok Online (iRO) install detection (BYOG: the companion builds the RO packs from the player's own client data).

find() -> ordered candidate folders, each validated by ro_conv.check_install (the converter lane) when that module is bundled, else by
local_check: the folder holds data.grf (the client's main archive; GRF header "Master of Magic" or, GRF 0x300, "Event Horizon"). Search order: the configured folder
(gui.json "ro_install"), registry uninstall entries of Ragnarok / Gravity / WarpPortal clients, Steam libraries, the usual default folders
(C:\\Gravity\\Ragnarok, C:\\WarpPortal\\..., Program Files), then a bounded drive scan. A manual folder pick (the GUI's "Change...") feeds
`configured`; a picked parent folder works too (child folders are tried, like rosefind). Stdlib only; registry / drives / env are injectable
(same fakes as test_rosefind). Nothing is read from the GRF here except its 16 byte magic.
"""
import glob, json, os, sys, time

import steam
import rosefind

CONFIG_KEY = "ro_install"
SCAN_BUDGET_S = 3.0
SCAN_DEPTH = 2
DATA_FILE = "data.grf"
GRF_MAGIC = b"Master of Magic"
GRF_MAGICS = (GRF_MAGIC, b"Event Horizon")          # GRF 0x200 / 0x300 (current iRO data.grf)
# program / folder names of the iRO clients (Gravity's launcher, WarpPortal, "Ragnarok Online" / "RagnarokOnline"): lower-case substrings
NAME_HINTS = ("ragnarok", "gravity", "warpportal", "warp portal", "iro ", "ラグナロク")


def name_matches(name):
    n = (name or "").lower()
    return any(h in n for h in NAME_HINTS)


def _data_grf(path):
    """the real file name of data.grf inside path (any case) or None"""
    try:
        for n in os.listdir(path):
            if n.lower() == DATA_FILE and os.path.isfile(os.path.join(path, n)):
                return os.path.join(path, n)
    except OSError:
        pass
    return None


def local_check(path):
    """Fallback validation: data.grf in the folder with a GRF header -> (ok, reason). Reasons never contain the folder name."""
    if not path or not os.path.isdir(path):
        return False, "not a folder"
    g = _data_grf(path)
    if g is None:
        return False, "no data.grf in the folder"
    try:
        with open(g, "rb") as fh:
            head = fh.read(16)
    except OSError as e:
        return False, "cannot read data.grf (%s)" % (e.strerror or "error")
    if not head.startswith(GRF_MAGICS):
        return False, "data.grf is not a GRF archive"
    return True, "data.grf found"


def check(path):
    """-> (ok, reason) via ro_conv.check_install when available, else local_check."""
    try:
        import ro_conv
        fn = getattr(ro_conv, "check_install", None)
    except Exception:                                            # not bundled / broken import: fall back
        fn = None
    if fn is None:
        return local_check(path)
    try:
        r = fn(path)
        return (bool(r[0]), str(r[1])) if isinstance(r, tuple) else (bool(r), "")
    except Exception as e:
        return False, "check failed: %s" % e


# registry: Uninstall entries (DisplayName matches) + the vendors' own keys (best effort; unknown keys simply do not exist)
VENDOR_KEYS = (r"SOFTWARE\Gravity Co., Ltd.\Ragnarok", r"SOFTWARE\Gravity\Ragnarok", r"SOFTWARE\WarpPortal\Ragnarok Online",
               r"SOFTWARE\Gravity Soft\Ragnarok Online")
VENDOR_VALUES = ("InstallDir", "InstallPath", "InstallLocation", "Path")


def _fix_exe(v):
    v = (v or "").strip().strip('"')
    if "," in v and v.rsplit(",", 1)[0].lower().endswith(".exe"):
        v = v.rsplit(",", 1)[0].strip('"')
    if v.lower().endswith(".exe"):
        v = os.path.dirname(v)
    return v


def _registry_dirs(reg):
    out = []
    for hive in ("HKLM", "HKCU"):
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for sub in reg.subkeys(hive, key):
                k = key + "\\" + sub
                if not name_matches(reg.read(hive, k, "DisplayName") or ""):
                    continue
                for val in ("InstallLocation", "InstallDir", "DisplayIcon"):
                    v = _fix_exe(reg.read(hive, k, val))
                    if v:
                        out.append(v)
        for key in VENDOR_KEYS + tuple(k.replace("SOFTWARE\\", "SOFTWARE\\WOW6432Node\\", 1) for k in VENDOR_KEYS):
            for val in VENDOR_VALUES:
                v = _fix_exe(reg.read(hive, key, val))
                if v:
                    out.append(v)
    return out


def _steam_dirs(steam_roots):
    out = []
    for root in steam_roots:
        for lib in steam.library_folders(root):
            common = os.path.join(lib, "steamapps", "common")
            try:
                for n in sorted(os.listdir(common)):
                    if "ragnarok" in n.lower():
                        out.append(os.path.join(common, n))
            except OSError:
                pass
    return out


def _common_dirs(env):
    sd = env.get("SystemDrive", "C:") + os.sep
    pats = [os.path.join(sd, "Gravity", "Ragnarok*"), os.path.join(sd, "Gravity", "*"), os.path.join(sd, "WarpPortal", "*"),
            os.path.join(sd, "WarpPortal", "*", "*"), os.path.join(sd, "Games", "*Ragnarok*"), os.path.join(sd, "*Ragnarok*")]
    for pf in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("ProgramW6432")):
        if pf:
            pats += [os.path.join(pf, "Gravity", "*"), os.path.join(pf, "*Ragnarok*"), os.path.join(pf, "WarpPortal", "*"),
                     os.path.join(pf, "Gravity", "*", "*")]
    if env.get("LOCALAPPDATA"):
        pats.append(os.path.join(env["LOCALAPPDATA"], "*Ragnarok*"))
    home = env.get("USERPROFILE") or os.path.expanduser("~")
    pats += [os.path.join(home, "Games", "*Ragnarok*"), os.path.join(home, "Desktop", "*Ragnarok*")]
    out = []
    for p in pats:
        out += sorted(glob.glob(p))
    return [p for p in out if os.path.isdir(p)]


def candidates(configured=None, registry=None, steam_roots=None, env=None, drives=None,
               budget=SCAN_BUDGET_S, max_depth=SCAN_DEPTH, now=time.monotonic):
    """-> [(path, how)] unvalidated, in search order, de-duplicated. Without injection only the configured path off Windows."""
    out, seen = [], set()

    def add(p, how):
        if not p:
            return
        p = os.path.normpath(p)
        k = os.path.normcase(p)
        if k not in seen:
            seen.add(k)
            out.append((p, how))
    add(configured, "configured")
    win = sys.platform == "win32"
    reg = registry if registry is not None else (steam._registry if win else None)
    if reg is not None:
        for p in _registry_dirs(reg):
            add(p, "registry")
    roots = steam_roots if steam_roots is not None else (steam.find_steam_roots() if win else [])
    for p in _steam_dirs(roots):
        add(p, "steam")
    e = env if env is not None else (os.environ if win else {})
    if e:
        for p in _common_dirs(e):
            add(p, "common folder")
    drv = drives if drives is not None else steam.fixed_drives()
    if drv:
        for p in _drive_scan(drv, now() + budget, max_depth, now):
            add(p, "drive scan")
    return out


def _drive_scan(drives, deadline, max_depth, now):
    """bounded breadth-limited walk of the fixed drives for folders whose name looks like an RO client"""
    out = []

    def walk(d, depth):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return
        for n in names:
            if now() > deadline:
                return
            p = os.path.join(d, n)
            if n.startswith("$") or n.lower() in ("windows", "system volume information") or not os.path.isdir(p):
                continue
            if name_matches(n):
                out.append(p)
            elif depth < max_depth:
                walk(p, depth + 1)
    for dr in drives:
        if now() > deadline:
            break
        walk(dr, 1)
    return out


def find(configured=None, with_reasons=False, **kw):
    """Ordered, validated RO install folders (strings); with_reasons=True -> [(path, how, reason)]. A candidate's direct sub-folders are
    tried too ("C:\\Gravity" holds the client one folder deeper)."""
    res, seen = [], set()
    for p, how in candidates(configured=configured, **kw):
        for q in [p] + rosefind.child_dirs(p):
            k = os.path.normcase(os.path.normpath(q))
            if k in seen:
                continue
            seen.add(k)
            ok, reason = check(q)
            if ok:
                res.append((q, how, reason))
                break
    return res if with_reasons else [p for p, _h, _r in res]


def settings_file():
    return rosefind.settings_file()


def load_config(settings_file):
    """-> the saved RO folder (str, "" when not set / unreadable)"""
    try:
        with open(settings_file, encoding="utf-8") as fh:
            d = json.load(fh)
        return str(d.get(CONFIG_KEY, "") or "").strip() if isinstance(d, dict) else ""
    except (OSError, ValueError):
        return ""
