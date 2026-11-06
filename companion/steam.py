"""Steam discovery, v0 ownership gate and map index.

Ownership v0: a mod is allowed only if Steam's appmanifest_<appid>.acf for the app that ships it
exists in one of the Steam library folders AND its StateFlags has bit 4 (StateFullyInstalled) set,
AND the mod folder exists on disk. A manual --game-root override bypasses the manifest check
(source = "manual-override") — meant for testing / non-standard installs.

Steam AppState StateFlags (bit field, from Steam's EAppState):
  1 Uninstalled, 2 UpdateRequired, 4 FullyInstalled, 8 Encrypted, 16 Locked, 32 FilesMissing,
  64 AppRunning, 128 FilesCorrupt, 256 UpdateRunning, 512 UpdatePaused, 1024 UpdateStarted,
  2048 Uninstalling, 4096 BackupRunning, 65536 Reconfiguring, 131072 Validating, 262144 AddingFiles,
  524288 Preallocating, 1048576 Downloading, 2097152 Staging, 4194304 Committing, 8388608 UpdateStopping
Typical values: 4 = installed & up to date, 6 = installed but update pending, 1026 = updating.
v0 accepts any value with bit 4 set and bit 1 (Uninstalled) clear.
"""
import os, re, sys, time

import vdf

STATE_UNINSTALLED = 1
STATE_FULLY_INSTALLED = 4

# appid -> (display name, mod folder). The game dir comes from the manifest's "installdir".
KNOWN_APPS = {
    70: ("Half-Life", "valve"),
    10: ("Counter-Strike", "cstrike"),
    80: ("Counter-Strike: Condition Zero", "czero"),
    225840: ("Sven Co-op", "svencoop"),
    20: ("Team Fortress Classic", "tfc"),
    30: ("Day of Defeat", "dod"),
    40: ("Deathmatch Classic", "dmc"),
    50: ("Half-Life: Opposing Force", "gearbox"),
    60: ("Ricochet", "ricochet"),
    130: ("Half-Life: Blue Shift", "bshift"),
}
MOD_TO_APP = {mod: appid for appid, (_, mod) in KNOWN_APPS.items()}

# Content folders scanned per mod, in GoldSrc precedence order (first hit wins on duplicate map names).
# _hd / _<language> folders are skipped (they don't add maps; language dirs only hold .txt briefings).
MOD_DIR_SUFFIXES = ("_addon", "", "_downloads")

SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
# Map names (BSP base names) are wider than mod names: Sven Co-op ships "-sp_campaign_portal", community maps use
# "+", "!", "(", "[" … They come only from directory listings and requests are only matched against the index (never
# joined into a path), so this is defence in depth: no path separator (/ \), no ':' (drive / NTFS stream), no
# Windows-reserved character (< > " | ? *), no whitespace or control character (the VRChat log request is split on
# whitespace), no leading '.' (no "." / ".." / hidden names) and no trailing '.' (Windows drops it).
MAP_NAME = re.compile(r"^(?!\.)[A-Za-z0-9_.\-+!#$%&'()\[\]{}@^~,;=]{0,63}[A-Za-z0-9_\-+!#$%&'()\[\]{}@^~,;=]$")
# Windows device names are not usable as file names, whatever the extension (cache files are <mod>-<map>-….bin,
# prebuilt packs <mod>__<map>.bin, so only a bare device name would collide; refuse it anyway).
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.I)


def safe_map_name(name):
    """True if name may be a map name in the index / a request (see MAP_NAME)."""
    return bool(MAP_NAME.match(name)) and not _RESERVED.match(name)


# ------------------------------------------------------------------------------------ registry / drives layer
# Everything that touches winreg or drive letters goes through these two objects so tests can replace them
# (steam._registry = FakeRegistry(...), steam.fixed_drives = lambda: [tmpdir]) and run on macOS / Linux.
class _WinRegistry:
    """read(hive, key, value) -> str|None; subkeys(hive, key) -> [names]. hive is "HKCU" or "HKLM"."""
    def _open(self, hive, key):
        import winreg
        h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        return winreg.OpenKey(h, key)

    def read(self, hive, key, value):
        if sys.platform != "win32":
            return None
        try:
            import winreg
            with self._open(hive, key) as k:
                v = winreg.QueryValueEx(k, value)[0]
            return v if isinstance(v, str) else None
        except (OSError, ImportError):
            return None

    def subkeys(self, hive, key):
        if sys.platform != "win32":
            return []
        out = []
        try:
            import winreg
            with self._open(hive, key) as k:
                i = 0
                while i < 5000:
                    try:
                        out.append(winreg.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except (OSError, ImportError):
            pass
        return out


_registry = _WinRegistry()


def fixed_drives():
    """Roots of all fixed drives ("C:\\", "D:\\" ...); [] off Windows."""
    if sys.platform != "win32":
        return []
    out = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                root = "%s:\\" % chr(65 + i)
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:      # DRIVE_FIXED
                    out.append(root)
    except (OSError, AttributeError):
        pass
    return out


STEAM_REG = (("HKCU", r"Software\Valve\Steam", "SteamPath"),
             ("HKCU", r"Software\Valve\Steam", "SteamExe"),
             ("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
             ("HKLM", r"SOFTWARE\Valve\Steam", "InstallPath"))
STEAM_DRIVE_DIRS = ("Steam", os.path.join("Program Files (x86)", "Steam"), os.path.join("Program Files", "Steam"),
                    os.path.join("Games", "Steam"), "SteamLibrary", os.path.join("Games", "SteamLibrary"))


def steam_candidates():
    """-> [(path, how)] every place a Steam install may be (not yet checked for existence)."""
    cands = []
    for hive, key, val in STEAM_REG:                       # no-op off Windows unless a fake registry is installed
        v = _registry.read(hive, key, val)
        if v:
            v = v.strip().strip('"')
            if val == "SteamExe" or v.lower().endswith(".exe"):
                v = os.path.dirname(v)
            cands.append((os.path.normpath(v), "registry %s\\%s\\%s" % (hive, key, val)))
    if sys.platform == "win32":
        cands.append((os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"),
                      "default Program Files (x86)"))
    elif sys.platform == "darwin":
        cands.append((os.path.expanduser("~/Library/Application Support/Steam"), "macOS default"))
    else:
        cands += [(os.path.expanduser(p), "linux default") for p in
                  ("~/.steam/steam", "~/.local/share/Steam", "~/.var/app/com.valvesoftware.Steam/data/Steam")]
    for d in fixed_drives():
        for sub in STEAM_DRIVE_DIRS:
            cands.append((os.path.join(d, sub), "fixed drive scan"))
    return cands


def find_steam_roots(report=None):
    """All distinct Steam installs found (dirs containing steamapps/), in priority order. report: optional list
    that receives one line per place that was tried."""
    out, seen = [], set()
    for path, how in steam_candidates():
        ok = bool(path) and os.path.isdir(os.path.join(path, "steamapps"))
        if report is not None:
            report.append("  %s %s (%s)" % ("FOUND  " if ok else "no     ", path, how))
        key = os.path.normcase(os.path.normpath(path)) if path else None
        if ok and key not in seen:
            seen.add(key)
            out.append(os.path.normpath(path))
    return out


def find_steam_root():
    """Best-effort Steam install path for this OS, or None."""
    r = find_steam_roots()
    return r[0] if r else None


# ------------------------------------------------------------------------------------ non-Steam GoldSrc installs
NON_STEAM_SOURCE = "detected-non-steam"
SCAN_BUDGET_S = 3.0
SCAN_MAX_DEPTH = 3
_BASE_DIRS = ("Program Files (x86)", "Program Files", "Games", "Spiele")
_NAME_HINTS = re.compile(r"^(counter[- _]?strike|half[- _]?life|cs[ _.\-]?1|valve|hlds|goldsrc)", re.I)
_SKIP_DIRS = {"windows", "$recycle.bin", "system volume information", "steamapps", "node_modules", "appdata",
              "programdata", "recovery", "perflogs", "$windows.~bt", "windowsapps", ".git"}
_scan_cache = {}


def _ci_names(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def is_goldsrc_root(d):
    """A GoldSrc install root: hl.exe (client) or hlds.exe (server) next to a valve/ folder."""
    names = _ci_names(d)
    low = {n.lower() for n in names}
    if not ({"hl.exe", "hlds.exe"} & low):
        return False
    return any(n.lower() == "valve" and os.path.isdir(os.path.join(d, n)) for n in names)


def mods_in_root(game_root):
    """-> sorted mod folder names present: known GoldSrc apps (valve, cstrike, ...) plus any folder with liblist.gam."""
    mods = set()
    for n in _ci_names(game_root):
        p = os.path.join(game_root, n)
        if not os.path.isdir(p):
            continue
        low = n.lower()
        if low in MOD_TO_APP:
            mods.add(low)
        elif SAFE_NAME.match(n) and os.path.isfile(os.path.join(p, "liblist.gam")):
            mods.add(low)
    return sorted(mods)


def describe_root(game_root):
    """-> {"root","valid","mods","reason"} for the GUI validation. valid = at least one known mod folder exists."""
    root = os.path.normpath(game_root) if game_root else ""
    if not root or not os.path.isdir(root):
        return {"root": root, "valid": False, "mods": [], "reason": "Ordner nicht gefunden"}
    mods = mods_in_root(root)
    if not [m for m in mods if m in MOD_TO_APP]:
        return {"root": root, "valid": False, "mods": mods,
                "reason": "kein valve/ oder cstrike/ Ordner darin - bitte den Ordner mit hl.exe waehlen"}
    return {"root": root, "valid": True, "mods": mods, "reason": "gefunden: " + ", ".join(mods)}


def scan_drive(drive, deadline, max_depth=SCAN_MAX_DEPTH, now=time.monotonic):
    """Candidate roots under one drive root. Depth is counted from each base (0 = the base itself). At the drive
    root only folders whose name looks like a GoldSrc game (or Games / Spiele / Program Files) are entered; below
    Program Files / Games / Spiele every folder up to max_depth is. Stops at the deadline."""
    found, seen = [], set()

    def walk(d, depth, free):
        if now() > deadline:
            return
        if is_goldsrc_root(d):
            key = os.path.normcase(os.path.normpath(d))
            if key not in seen:
                seen.add(key)
                found.append(os.path.normpath(d))
            return
        if depth >= max_depth:
            return
        try:
            with os.scandir(d) as it:
                subs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            return
        for n in subs:
            if now() > deadline:
                return
            if n.lower() in _SKIP_DIRS:
                continue
            if not free and not _NAME_HINTS.match(n) and n.lower() not in {b.lower() for b in _BASE_DIRS}:
                continue
            walk(os.path.join(d, n), depth + 1, free or n.lower() in {b.lower() for b in _BASE_DIRS})

    walk(drive, 0, False)
    return found


def _registry_uninstall_roots():
    out = []
    for hive in ("HKLM", "HKCU"):
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for sub in _registry.subkeys(hive, key):
                for val in ("InstallLocation", "DisplayIcon"):
                    v = _registry.read(hive, key + "\\" + sub, val)
                    if not v:
                        continue
                    v = v.strip().strip('"')
                    if "," in v and v.rsplit(",", 1)[0].lower().endswith(".exe"):
                        v = v.rsplit(",", 1)[0].strip('"')              # DisplayIcon "C:\x\hl.exe,0"
                    if v.lower().endswith(".exe"):
                        v = os.path.dirname(v)
                    if v:
                        out.append(os.path.normpath(v))
    return out


def detect_non_steam(budget=SCAN_BUDGET_S, max_depth=SCAN_MAX_DEPTH, force=False, now=time.monotonic):
    """-> [{"root","mods","source":"detected-non-steam","via"}] GoldSrc installs that are not Steam's. Suggestions only:
    nothing here is allowed automatically. Cached per process (force=True rescans). One hard time budget across
    registry + all drives."""
    drives = tuple(fixed_drives())
    ck = (drives, max_depth)
    if not force and ck in _scan_cache:
        return list(_scan_cache[ck])
    deadline = now() + budget
    res, seen = [], set()

    def add(root, via):
        if not root or not is_goldsrc_root(root):
            return
        key = os.path.normcase(os.path.normpath(root))
        if key in seen or "steamapps" in key.replace("\\", "/").split("/"):
            return
        seen.add(key)
        res.append({"root": os.path.normpath(root), "mods": mods_in_root(root), "source": NON_STEAM_SOURCE, "via": via})

    for r in _registry_uninstall_roots():
        add(r, "registry uninstall entry")
        add(os.path.dirname(r), "registry uninstall entry")
    for d in drives:
        for r in scan_drive(d, deadline, max_depth, now):
            add(r, "folder scan")
    _scan_cache[ck] = list(res)
    return res


def discovery_report(steam_root=None, game_root=None):
    """-> [lines] for the companion log: what was searched and found."""
    if game_root:
        d = describe_root(game_root)
        return ["game root (manual, ownership not checked): %s -> %s" % (d["root"], d["reason"])]
    lines, rep = [], []
    roots = [steam_root] if steam_root else find_steam_roots(rep)
    lines.append("Steam search:" if not steam_root else "Steam root given: %s" % steam_root)
    lines += rep
    if not roots:
        lines.append("  no Steam installation found")
    for r in roots:
        libs = library_folders(r)
        lines.append("Steam %s; libraries: %s" % (r, libs))
        for lib in libs:
            try:
                mf = sorted(n for n in os.listdir(os.path.join(lib, "steamapps")) if n.startswith("appmanifest_"))
            except OSError:
                mf = []
            ours = [n for n in mf if n[len("appmanifest_"):-4].isdigit() and int(n[len("appmanifest_"):-4]) in KNOWN_APPS]
            lines.append("  manifests in %s: %s" % (lib, ", ".join(ours) or "none of ours"))
    return lines


def non_steam_hint_lines(cstrike_allowed):
    """If cstrike is not allowed: scan for non-Steam installs and return log lines incl. the GUI hint."""
    if cstrike_allowed:
        return []
    lines = []
    try:
        cands = detect_non_steam()
    except Exception as e:                                   # a scan problem must never stop the companion
        cands = []
        lines.append("non-Steam scan failed: %s" % e)
    for c in cands:
        lines.append("possible non-Steam GoldSrc install (NOT served until confirmed in the GSVR window): %s [%s]"
                     % (c["root"], ", ".join(c["mods"]) or "no mods"))
    lines.append("HINT: Counter-Strike (cstrike) not found. Ordner in der GSVR-Oberflaeche unter "
                 "'Half-Life / Counter-Strike Ordner' angeben (oder --game-root DIR).")
    return lines


def library_folders(steam_root):
    """All Steam library roots (each containing steamapps/). Supports new and legacy libraryfolders.vdf."""
    libs = [os.path.normpath(steam_root)]
    path = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(path):
        try:
            data = vdf.load(path)
        except (OSError, ValueError):
            data = {}
        lf = data.get("libraryfolders") or {}
        for k, v in lf.items():
            if not k.isdigit():
                continue
            p = v.get("path") if isinstance(v, dict) else v      # new: {"path": ...}; legacy: "1" "D:\\Lib"
            if p:
                p = os.path.normpath(p)
                if p not in libs:
                    libs.append(p)
    return [p for p in libs if os.path.isdir(os.path.join(p, "steamapps"))]


def read_manifest(lib, appid):
    path = os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf")
    if not os.path.isfile(path):
        return None
    try:
        st = vdf.load(path).get("appstate", {})
    except (OSError, ValueError):
        return None
    try:
        flags = int(st.get("stateflags", "0"))
    except ValueError:
        flags = 0
    return {"appid": appid, "path": path, "flags": flags, "installdir": st.get("installdir", ""),
            "name": st.get("name", ""),
            "installed": bool(flags & STATE_FULLY_INSTALLED) and not flags & STATE_UNINSTALLED}


def mod_dirs(game_root, mod):
    return [d for d in (os.path.join(game_root, mod + s) for s in MOD_DIR_SUFFIXES) if os.path.isdir(d)]


def ownership_from_steam(steam_root):
    """-> {mod: {...}} for every known app; allowed only when manifest says installed and folder exists."""
    libs = library_folders(steam_root) if steam_root else []
    out = {}
    for appid, (name, mod) in KNOWN_APPS.items():
        ent = {"appid": appid, "name": name, "allowed": False, "source": "steam", "game_root": None,
               "reason": "no appmanifest"}
        for lib in libs:
            m = read_manifest(lib, appid)
            if not m:
                continue
            if not m["installed"]:
                ent["reason"] = f"StateFlags={m['flags']} (not fully installed)"
                continue
            root = os.path.join(lib, "steamapps", "common", m["installdir"])
            if not os.path.isdir(os.path.join(root, mod)):
                ent["reason"] = f"manifest ok but {mod}/ missing"
                continue
            ent.update(allowed=True, game_root=root, reason=f"appmanifest_{appid}.acf StateFlags={m['flags']}")
            break
        out[mod] = ent
    return out


def ownership_manual(game_root, only_mods=None):
    """Mods present in game_root (GUI-confirmed folder or --game-root): source "manual-override", no ownership check."""
    out = {}
    for appid, (name, mod) in KNOWN_APPS.items():
        present = os.path.isdir(os.path.join(game_root, mod))
        allowed = present and (not only_mods or mod in only_mods)
        out[mod] = {"appid": appid, "name": name, "allowed": allowed, "source": "manual-override",
                    "game_root": os.path.normpath(game_root) if present else None,
                    "reason": "manual --game-root" if allowed else ("filtered by --mods" if present else "folder missing")}
    return out


def build_index(ownership):
    """-> {mod: {"appid","name","game_root","maps": {mapname: {"path","size","dir"}}}} for allowed mods.
    Paths come only from directory listings, never from request input."""
    idx = {}
    for mod, ent in ownership.items():
        if not ent["allowed"]:
            continue
        maps = {}
        for d in mod_dirs(ent["game_root"], mod):
            md = os.path.join(d, "maps")
            try:
                names = sorted(os.listdir(md))
            except OSError:
                continue
            for fn in names:
                base, ext = os.path.splitext(fn)
                if ext.lower() != ".bsp" or not safe_map_name(base):
                    continue
                key = base.lower()
                if key in maps:
                    continue
                p = os.path.join(md, fn)
                if os.path.isfile(p):
                    maps[key] = {"path": p, "size": os.path.getsize(p), "dir": os.path.basename(d)}
        idx[mod] = {"appid": ent["appid"], "name": ent["name"], "game_root": ent["game_root"], "maps": maps}
    return idx
