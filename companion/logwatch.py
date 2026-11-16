"""Tails the newest VRChat output log and yields GSVR commands.

VRChat writes one log per session: output_log_YYYY-MM-DD_HH-MM-SS.txt (older builds: output_log_HH-MM-SS.txt).
Udon Debug.Log lines look like
    2026.10.18 20:01:02 Debug      -  [GSVR-REQ] map=cstrike/de_dust2 req=7
The watcher starts at the END of the current newest log (old requests are not replayed), switches to a
newer log when one appears (read from its start), and handles truncation and partial lines.
At startup the last "Joining wrld_…" line of the existing log is replayed once, so a companion started
while VRChat is already inside a world still knows that world (--world-allow gate).
"""
import glob, os, re, sys, time

REQ = re.compile(r"\[GSVR-REQ\]\s+(.*?)\s*$")
WORLD = re.compile(r"Joining (wrld_[0-9A-Za-z\-]+)")


VRC_APPID = "438100"
PROTON_REL = ("steamapps", "compatdata", VRC_APPID, "pfx", "drive_c", "users", "steamuser",
              "AppData", "LocalLow", "VRChat", "VRChat")
LINUX_STEAM_ROOTS = (".steam/steam", ".steam/debian-installation", ".local/share/Steam",
                     ".var/app/com.valvesoftware.Steam/.local/share/Steam")
STALE_S = 600                  # no output_log written for 10 min while the world polls us = wrong folder
LOG_WARNING = ("VRChat log not found / not updating - set the VRChat log folder "
               "(Linux/Proton: steamapps/compatdata/438100/pfx/...)")


def is_wine():
    """Windows Python under Wine/Proton: WINE env vars, HKCU\\Software\\Wine, or Z:\\ = the host's /."""
    if sys.platform != "win32":
        return False
    if os.environ.get("WINEPREFIX") or os.environ.get("WINELOADER") or os.environ.get("WINEDLLPATH"):
        return True
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Wine"))
        return True
    except (ImportError, OSError):
        pass
    return os.path.isdir("Z:\\home") and os.path.isdir("Z:\\etc")


def host_root():
    """Where the host (Linux) file system is visible: Z:\\ under Wine, / natively."""
    return "Z:\\" if sys.platform == "win32" else "/"


def _host(zroot, posix):
    """POSIX absolute path -> path this process can open (through the Z: drive under Wine)."""
    return os.path.join(zroot, *[x for x in posix.replace("\\", "/").split("/") if x])


def _vdf_libraries(steam_root, zroot):
    out = []
    try:
        import vdf
        data = vdf.load(os.path.join(steam_root, "steamapps", "libraryfolders.vdf"))
    except (OSError, ValueError, ImportError):
        return out
    for k, v in (data.get("libraryfolders") or {}).items():
        if not k.isdigit():
            continue
        p = v.get("path") if isinstance(v, dict) else v
        if p:
            p = str(p)
            out.append(_host(zroot, p) if p.startswith("/") else p)     # POSIX path in the VDF -> via Z:
    return out


def _home_dirs(zroot, home_env):
    homes = []
    if home_env:
        homes.append(_host(zroot, home_env) if home_env.startswith("/") else home_env)
    try:
        homes += [os.path.join(zroot, "home", n) for n in sorted(os.listdir(os.path.join(zroot, "home")))]
    except OSError:
        pass
    seen, out = set(), []
    for h in homes:
        k = os.path.normcase(os.path.normpath(h))
        if k not in seen and os.path.isdir(h):
            seen.add(k)
            out.append(h)
    return out


def proton_candidates(zroot=None, home_env=None, extra_roots=()):
    """VRChat log dirs a Linux/Proton install could use, in search order: the Steam roots under the home dirs
    (~/.steam/steam, ~/.steam/debian-installation, ~/.local/share/Steam, Flatpak), extra_roots, and every library
    of their libraryfolders.vdf. Only Steam roots/libraries that exist (have steamapps/) yield a candidate."""
    zroot = host_root() if zroot is None else zroot
    if home_env is None:
        home_env = os.environ.get("HOME", "")
    queue = []
    for h in _home_dirs(zroot, home_env):
        queue += [os.path.join(h, *r.split("/")) for r in LINUX_STEAM_ROOTS]
    queue += list(extra_roots)
    out, seen = [], set()
    while queue:
        r = queue.pop(0)
        k = os.path.normcase(os.path.normpath(r))
        if k in seen:
            continue
        seen.add(k)
        if os.path.isdir(os.path.join(r, "steamapps")):
            out.append(os.path.join(r, *PROTON_REL))
            queue += _vdf_libraries(r, zroot)
    return out


def _newest_mtime(d):
    try:
        f = newest_log(d)
        return os.path.getmtime(f) if f else None
    except OSError:
        return None


def pick_best(cands):
    """The candidate whose newest output_log is most recent; else the first existing dir; else None."""
    best, best_t = None, None
    for d in cands:
        t = _newest_mtime(d)
        if t is not None and (best_t is None or t > best_t):
            best, best_t = d, t
    if best:
        return best
    return next((d for d in cands if os.path.isdir(d)), None)


def default_log_dir(zroot=None, home_env=None, wine=None, extra_roots=(), override=None):
    """override (GUI field / --vrchat-log-dir) wins. Plain Windows: %USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat.
    Under Wine and on native Linux: pick_best over proton_candidates (+ the Wine prefix's own folder)."""
    if override:
        return override
    wine = is_wine() if wine is None else wine
    own = None
    if sys.platform == "win32":
        own = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "AppData", "LocalLow", "VRChat", "VRChat")
        if not wine and zroot is None:
            return own
    elif zroot is None and sys.platform != "linux":
        return None      # macOS: no VRChat PC; pass --vrchat-log-dir
    cands = proton_candidates(zroot, home_env, extra_roots) + ([own] if own else [])
    return pick_best(cands) or own


def log_health(log_dir, now=None):
    """-> (ok, reason): ok when an output_log was written within STALE_S"""
    if not log_dir:
        return False, "no folder"
    t = _newest_mtime(log_dir)
    if t is None:
        return False, "no output_log"
    if (time.time() if now is None else now) - t > STALE_S:
        return False, "output_log not updating"
    return True, ""


def newest_log(log_dir):
    files = glob.glob(os.path.join(log_dir, "output_log_*.txt"))
    if not files:
        return None
    return max(files, key=lambda f: (os.path.getmtime(f), f))


def parse_line(line):
    """-> ("world", wrld_id) | ("cmd", {k: v}) | None"""
    m = REQ.search(line)
    if m:
        cmd = {}
        for tok in m.group(1).split()[:8]:
            k, eq, v = tok.partition("=")
            if k.isalpha() and len(k) <= 16:
                cmd[k.lower()] = v if eq else "1"
        return ("cmd", cmd)
    m = WORLD.search(line)
    if m:
        return ("world", m.group(1))
    return None


def last_world_line(path, end):
    """Last 'Joining wrld_…' line in path[:end] (decoded), or None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(end)
    except OSError:
        return None
    last = None
    for raw in data.splitlines():
        if b"Joining wrld_" in raw:
            last = raw
    return last.decode("utf-8", "replace") if last is not None else None


class LogTailer:
    def __init__(self, log_dir, poll=0.5):
        self.dir, self.poll = log_dir, poll
        self.path, self.pos, self.buf = None, 0, b""
        self.startup_world_line = None

    def _check_rotation(self, first):
        cur = newest_log(self.dir)
        if cur and cur != self.path:
            self.path, self.buf = cur, b""
            # first file at startup: skip history; a newly appeared file: read from the start
            self.pos = os.path.getsize(cur) if first else 0
            if first:
                self.startup_world_line = last_world_line(cur, self.pos)
            return True
        return False

    def lines(self, stop):
        """Generator of decoded complete lines; runs until stop.is_set()."""
        first = True
        last_rot = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if first or now - last_rot > 2.0:
                if self._check_rotation(first) and not first:
                    yield f"__ROTATED__ {self.path}"
                if first and self.startup_world_line:
                    yield self.startup_world_line          # world only — old [GSVR-REQ] lines are NOT replayed
                first, last_rot = False, now
            if self.path:
                try:
                    size = os.path.getsize(self.path)
                    if size < self.pos:          # truncated
                        self.pos, self.buf = 0, b""
                    if size > self.pos:
                        # Python's open() on Windows shares read+write, so VRChat can keep writing.
                        with open(self.path, "rb") as fh:
                            fh.seek(self.pos)
                            chunk = fh.read(size - self.pos)
                        self.pos += len(chunk)
                        self.buf += chunk
                        *done, self.buf = self.buf.split(b"\n")
                        for raw in done:
                            yield raw.decode("utf-8", "replace").rstrip("\r")
                        continue
                except OSError:
                    self.path = None
            stop.wait(self.poll)


class MultiTailer:
    """Tests only (--follow-all-logs, two VRChat clients on one machine): tails EVERY output log that is written to after the
    start, each from its own position (files present at the start: from their end, newer ones: from their start). No rotation
    marker - the other session is still running; each file's last 'Joining wrld_' line at the start is replayed once."""

    def __init__(self, log_dir, poll=0.5):
        self.dir, self.poll = log_dir, poll
        self.files = {}          # path -> [pos, buf]
        self.skip = set()        # logs already stale at the start (not polled)

    def _scan(self, first, out):
        for f in glob.glob(os.path.join(self.dir, "output_log_*.txt")):
            if f in self.files or f in self.skip:
                continue
            try:
                size, mt = os.path.getsize(f), os.path.getmtime(f)
            except OSError:
                continue
            if first and time.time() - mt > 6 * 3600:
                self.skip.add(f)
                continue
            if first:
                self.files[f] = [size, b""]
                w = last_world_line(f, size)
                if w:
                    out.append(w)
            else:
                self.files[f] = [0, b""]
                out.append(f"__OPENED__ {f}")

    def lines(self, stop):
        first, last_scan = True, 0.0
        while not stop.is_set():
            now = time.monotonic()
            pending = []
            if first or now - last_scan > 2.0:
                self._scan(first, pending)
                first, last_scan = False, now
            for f, st in list(self.files.items()):
                try:
                    size = os.path.getsize(f)
                except OSError:
                    del self.files[f]
                    continue
                if size < st[0]:
                    st[0], st[1] = 0, b""
                if size > st[0]:
                    try:
                        with open(f, "rb") as fh:
                            fh.seek(st[0])
                            chunk = fh.read(size - st[0])
                    except OSError:
                        continue
                    st[0] += len(chunk)
                    st[1] += chunk
                    *done, st[1] = st[1].split(b"\n")
                    pending.extend(raw.decode("utf-8", "replace").rstrip("\r") for raw in done)
            if pending:
                yield from pending
                continue
            stop.wait(self.poll)
