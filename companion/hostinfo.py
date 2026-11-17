"""status.json "host": coarse memory / CPU numbers of the player's PC for the ROSE world's ADAPTIVE PRELOAD memory guard
(docs/design/rose_preload_adaptive.md, 0.9.46). The world decides from them how many neighbour zones it may keep hidden.

  "host": {"ram_total_mb": int, "ram_free_mb": int, "cpu_pct": int}     every value -1 = unknown on this system

Privacy: status.json is readable by any world, so only three ROUNDED numbers go out (total RAM to 1024 MB, free RAM to
256 MB, CPU load to 5 %): no names, paths, model strings or ids. Cheap: one OS call per value, cached for CACHE_S seconds
(the world polls status.json at most once per 5 s), the CPU load is the delta of two system-time samples (no sleeping in a request).
The shared HOST samples the CPU times on a small daemon thread every CPU_SAMPLE_S, so the load is that of the last few seconds even when
the world asks only every 45 s (0.9.46 audit: a delta between two rare polls averaged the load over minutes). NaN / inf readings = unknown.
No third-party module (psutil is not bundled): Windows GlobalMemoryStatusEx / GetSystemTimes via ctypes, Linux /proc,
macOS sysctl hw.memsize (free / CPU unknown there: -1, the world then sizes by the total alone).
"""
import math, os, sys, threading, time

CACHE_S = 5.0
CPU_SAMPLE_S = 5.0
TOTAL_STEP_MB, FREE_STEP_MB, CPU_STEP_PCT = 1024, 256, 5
UNKNOWN = -1


def _round(v, step):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return UNKNOWN
    if not math.isfinite(v) or v < 0:                  # NaN / inf from an odd OS reading: unknown, never a broken status.json
        return UNKNOWN
    return int(round(v / float(step))) * step


def public(total_mb, free_mb, cpu_pct):
    """The status.json "host" dict from raw numbers (None / negative = unknown): rounded, ints only, free <= total."""
    t = _round(total_mb, TOTAL_STEP_MB)
    f = _round(free_mb, FREE_STEP_MB)
    if t > 0 and f > t:
        f = t
    c = _round(cpu_pct, CPU_STEP_PCT)
    if c > 100:
        c = 100
    return {"ram_total_mb": t, "ram_free_mb": f, "cpu_pct": c}


# ── readers: each returns raw numbers or None (never raises) ──

def _win_mem():
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return m.ullTotalPhys / 1048576.0, m.ullAvailPhys / 1048576.0


def _win_cpu_times():
    import ctypes
    i, k, u = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
        return None
    return float(i.value), float(k.value + u.value)          # kernel time includes idle time


def parse_meminfo(text):
    """/proc/meminfo -> (total_mb, available_mb) or None (MemAvailable missing: MemFree + Cached as the old kernels' estimate)."""
    kv = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0].endswith(":"):
            try:
                kv[p[0][:-1]] = int(p[1])               # kB
            except ValueError:
                pass
    if "MemTotal" not in kv:
        return None
    av = kv.get("MemAvailable")
    if av is None:
        av = kv.get("MemFree", 0) + kv.get("Cached", 0)
    return kv["MemTotal"] / 1024.0, av / 1024.0


def parse_proc_stat(text):
    """/proc/stat first line -> (idle, total) jiffies or None."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [float(x) for x in line.split()[1:] if x.isdigit()]
            if len(v) < 4:
                return None
            idle = v[3] + (v[4] if len(v) > 4 else 0.0)  # idle + iowait
            return idle, sum(v)
    return None


def _read(path):
    with open(path, "r") as fh:
        return fh.read()


def _linux_mem():
    return parse_meminfo(_read("/proc/meminfo"))


def _linux_cpu_times():
    return parse_proc_stat(_read("/proc/stat"))


def _mac_mem():
    import ctypes, ctypes.util
    lib = ctypes.CDLL(ctypes.util.find_library("c"))
    v = ctypes.c_uint64(0)
    n = ctypes.c_size_t(8)
    if lib.sysctlbyname(b"hw.memsize", ctypes.byref(v), ctypes.byref(n), None, ctypes.c_size_t(0)) != 0:
        return None
    return v.value / 1048576.0, None


def cpu_pct(prev, cur):
    """CPU load in percent between two (idle, total) samples; None when unknown / no time passed."""
    if not prev or not cur:
        return None
    di, dt = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))


class HostInfo:
    """Cached sampler. mem / cpu = callables returning (total_mb, free_mb|None) / (idle, total) or None; clock = time source (tests)."""

    def __init__(self, mem=None, cpu=None, clock=time.monotonic, cpu_thread=False):
        if mem is None or cpu is None:
            dm, dc = _defaults()
            mem = mem or dm
            cpu = cpu or dc
        self._mem, self._cpu, self._clock = mem, cpu, clock
        self._lock = threading.Lock()
        self._at = None
        self._val = public(None, None, None)
        self._cpu_prev = None
        self._cpu_cur = None
        self._cpu_thread = cpu_thread                   # True: a daemon thread samples the CPU times every CPU_SAMPLE_S (started at the first public())
        self._thread = None

    def _safe(self, f):
        try:
            return f() if f else None
        except Exception:                               # an OS call that fails = unknown, never a broken status.json
            return None

    def cpu_tick(self):
        """One CPU sample of the sampler thread (also callable from tests): the load is the delta of the last two samples."""
        c = self._safe(self._cpu)
        if c:
            with self._lock:
                self._cpu_prev, self._cpu_cur = self._cpu_cur, c

    def _cpu_loop(self):
        while True:
            time.sleep(CPU_SAMPLE_S)
            self.cpu_tick()

    def _start_thread(self):
        if not self._cpu_thread or self._thread is not None or self._cpu is None:
            return
        try:
            self.cpu_tick()
            self._thread = threading.Thread(target=self._cpu_loop, name="gsvr-hostinfo-cpu", daemon=True)
            self._thread.start()
        except Exception:                               # no thread = the old two-poll delta
            self._cpu_thread = False

    def public(self):
        self._start_thread()
        with self._lock:
            now = self._clock()
            if self._at is not None and now - self._at < CACHE_S:
                return dict(self._val)
            self._at = now
            m = self._safe(self._mem)
            if self._cpu_thread:
                load = cpu_pct(self._cpu_prev, self._cpu_cur)          # the last CPU_SAMPLE_S, not the time since the previous poll
            else:
                c = self._safe(self._cpu)
                load = cpu_pct(self._cpu_prev, c)
                if c:
                    self._cpu_prev = c
            total, free = (m if m else (None, None))
            try:
                self._val = public(total, free, load)
            except Exception:
                self._val = public(None, None, None)
            return dict(self._val)


def _defaults():
    if sys.platform.startswith("win"):
        return _win_mem, _win_cpu_times
    if sys.platform.startswith("linux") or os.path.exists("/proc/meminfo"):
        return _linux_mem, _linux_cpu_times
    if sys.platform == "darwin":
        return _mac_mem, None
    return None, None


HOST = HostInfo(cpu_thread=True)
