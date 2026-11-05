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


def default_log_dir():
    if sys.platform == "win32":
        base = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        return os.path.join(base, "AppData", "LocalLow", "VRChat", "VRChat")
    return None      # VRChat (PC) only exists on Windows; elsewhere pass --vrchat-log-dir


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
