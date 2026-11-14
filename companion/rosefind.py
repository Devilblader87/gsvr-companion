"""ROSE Online install detection (BYOG: the companion builds the RPG1 packs from the player's own install).

find() -> ordered candidate folders, each validated by rose_conv.check_install (lane B); until that module exists a local
check is used: the folder holds the VFS index data.idx (what ~/dev/rose-vrchat/tools/rosevfs.py opens) and a *.vfs store.
Stdlib only; winreg / drive letters go through steam._registry / steam.fixed_drives so tests can inject fakes.
Config: the chosen folder is the key CONFIG_KEY ("rose_install") in the GUI settings file gui.json, next to hl_root.
"""
import glob, json, os, sys, time

import steam

CONFIG_KEY = "rose_install"
SCAN_BUDGET_S = 3.0
SCAN_DEPTH = 2
INDEX_FILE = "data.idx"


def local_check(path):
    """Fallback validation: data.idx plus at least one *.vfs next to it -> (ok, reason)."""
    if not path or not os.path.isdir(path):
        return False, "not a folder"
    try:
        names = {n.lower() for n in os.listdir(path)}
    except OSError as e:
        return False, "cannot read the folder (%s)" % e
    if INDEX_FILE not in names:
        return False, "no %s in the folder" % INDEX_FILE
    if not any(n.endswith(".vfs") for n in names):
        return False, "no .vfs data files in the folder"
    return True, "ROSE VFS index found"


def check(path):
    """-> (ok, reason) via rose_conv.check_install when available, else local_check."""
    try:
        import rose_conv
        fn = getattr(rose_conv, "check_install", None)
    except Exception:                                            # not there yet / broken import: fall back
        fn = None
    if fn is None:
        return local_check(path)
    try:
        r = fn(path)
        return (bool(r[0]), str(r[1])) if isinstance(r, tuple) else (bool(r), "")
    except Exception as e:
        return False, "check failed: %s" % e


def _registry_dirs(reg):
    out = []
    for hive in ("HKLM", "HKCU"):
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for sub in reg.subkeys(hive, key):
                k = key + "\\" + sub
                name = reg.read(hive, k, "DisplayName") or ""
                if "rose" not in name.lower():
                    continue
                for val in ("InstallLocation", "InstallDir", "DisplayIcon"):
                    v = (reg.read(hive, k, val) or "").strip().strip('"')
                    if "," in v and v.rsplit(",", 1)[0].lower().endswith(".exe"):
                        v = v.rsplit(",", 1)[0].strip('"')
                    if v.lower().endswith(".exe"):
                        v = os.path.dirname(v)
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
                    if "rose" in n.lower():
                        out.append(os.path.join(common, n))
            except OSError:
                pass
    return out


def _common_dirs(env):
    sd = env.get("SystemDrive", "C:") + os.sep
    pats = []
    for pf in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("ProgramW6432")):
        if pf:
            pats.append(os.path.join(pf, "*ROSE*"))
    pats += [os.path.join(sd, "Program Files*", "*ROSE*"), os.path.join(sd, "Games", "*ROSE*")]
    if env.get("LOCALAPPDATA"):
        pats.append(os.path.join(env["LOCALAPPDATA"], "*ROSE*"))
    home = env.get("USERPROFILE") or os.path.expanduser("~")
    pats.append(os.path.join(home, "Games", "*ROSE*"))
    out = []
    for p in pats:
        out += sorted(glob.glob(p))
    return [p for p in out if os.path.isdir(p)]


def _drive_scan(drives, deadline, max_depth, now):
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
            if n.startswith("$") or n.lower() in ("windows", "system volume information"):
                continue
            if not os.path.isdir(p):
                continue
            if "rose" in n.lower():
                out.append(p)
            elif depth < max_depth:
                walk(p, depth + 1)
    for dr in drives:
        if now() > deadline:
            break
        walk(dr, 1)
    return out


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


def find(configured=None, with_reasons=False, **kw):
    """Ordered, validated ROSE install folders (strings); with_reasons=True -> [(path, how, reason)]."""
    res = []
    for p, how in candidates(configured=configured, **kw):
        ok, reason = check(p)
        if ok:
            res.append((p, how, reason))
    return res if with_reasons else [p for p, _h, _r in res]


# ---- persisted config (gui.json, same file as hl_root) ----
def settings_file():
    """the GUI settings file gui.json (gui.settings_path uses this): %APPDATA%\\GSVR\\gui.json, else ~/.config/gsvr/gui.json"""
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GSVR", "gui.json")
    return os.path.expanduser("~/.config/gsvr/gui.json")


def load_config(settings_file):
    """-> the saved ROSE folder (str, "" when not set / unreadable)"""
    try:
        with open(settings_file, encoding="utf-8") as fh:
            d = json.load(fh)
        return str(d.get(CONFIG_KEY, "") or "").strip() if isinstance(d, dict) else ""
    except (OSError, ValueError):
        return ""


def save_config(settings_file, path):
    """Set CONFIG_KEY in the settings JSON, keeping all other keys."""
    try:
        with open(settings_file, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    d[CONFIG_KEY] = (path or "").strip()
    os.makedirs(os.path.dirname(os.path.abspath(settings_file)), exist_ok=True)
    tmp = settings_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, settings_file)
