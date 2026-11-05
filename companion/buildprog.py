"""Build progress of the converter subprocesses -> status.json "build" (and the GUI progress bars).

Converters (tools/bprog.py) print machine-readable lines on stdout:
    @plan <stage>=<weight> ...            optional: the stages of the run and their rough share of the time
    @progress <stage> <done> <total>      stage <stage> is <done>/<total> finished
Every other line is ordinary log output. A running build is a Job in the process-wide TRACKER; run() starts the subprocess
with its stdout streamed line by line into Job.feed. Jobs exist only while a converter runs (a cache hit never creates one).

status.json "build": [{"kind": "map"|"models"|"hologram"|"war3", "mod": "cstrike", "name": "de_dust2", "pct": 0-100,
                       "eta_s": seconds left (-1 = unknown), "state": "building"}, ...]   (empty list = idle; at most MAX_JOBS)
pct comes from the stage weights; a converter without progress lines gets a time based estimate from the previous build of the
same key (kind|mod|name, kept in <cache-dir>/build_times.json); eta from the rate so far, or from that previous duration.
"""
import json, os, subprocess, threading, time

MAX_JOBS = 4
DEFAULT_WEIGHT = 10
KINDS = ("map", "models", "hologram", "war3")


class Job:
    def __init__(self, tracker, kind, mod, name, hist_s=None):
        self.tracker, self.kind, self.mod, self.name = tracker, kind, mod, name
        self.t0 = time.monotonic()
        self.hist_s = hist_s
        self.plan = {}                 # stage -> weight
        self.stages = {}               # stage -> (done, total)
        self.order = []
        self.best = 0.0                # pct never goes down

    def feed(self, line):
        """One stdout line of the converter -> True when it was a progress line (@plan / @progress)."""
        if not line.startswith("@"):
            return False
        parts = line.split()
        try:
            if parts[0] == "@plan":
                plan = {}
                for item in parts[1:]:
                    k, _, v = item.partition("=")
                    if k and v:
                        plan[k[:24]] = max(1, min(int(v), 1000))
                if plan:
                    self.plan = plan
                return True
            if parts[0] == "@progress" and len(parts) >= 4:
                stage, done, total = parts[1][:24], int(parts[2]), int(parts[3])
                if total <= 0:
                    return True
                if stage not in self.stages:
                    self.order.append(stage)
                self.stages[stage] = (max(0, min(done, total)), total)
                return True
        except ValueError:
            return True
        return False

    def _pct_stages(self):
        if not self.stages:
            return None
        names = list(self.plan) + [s for s in self.order if s not in self.plan]
        tot = sum(self.plan.get(s, DEFAULT_WEIGHT) for s in names)
        got = 0.0
        for s in names:
            if s in self.stages:
                d, t = self.stages[s]
                got += self.plan.get(s, DEFAULT_WEIGHT) * d / t
        return 100.0 * got / tot if tot else None

    def snapshot(self, now=None):
        el = (now if now is not None else time.monotonic()) - self.t0
        pct = self._pct_stages()
        eta = -1.0
        hist_eta = max(0.0, self.hist_s - el) if self.hist_s else None
        if pct is None:
            if self.hist_s:                                   # no progress lines: time based guess, never 100 before the end
                pct = min(95.0, 100.0 * el / self.hist_s)
                eta = hist_eta
            else:
                pct = 0.0
        elif pct >= 100.0:
            eta = 0.0
        elif pct >= 8.0 and el >= 2.0:
            eta = el * (100.0 - pct) / pct                    # rate so far
        elif hist_eta is not None:
            eta = hist_eta
        pct = max(self.best, min(pct, 99.0))                 # 100 is never shown: the job disappears when the converter exits
        self.best = pct
        return {"kind": self.kind, "mod": self.mod, "name": self.name, "pct": int(pct),
                "eta_s": int(round(eta)) if eta >= 0 else -1, "state": "building"}

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.tracker.finish(self, ok=et is None)
        return False


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = []
        self.history = {}
        self.path = None

    def set_history_file(self, path):
        with self.lock:
            self.path = path
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                self.history = {str(k): float(v) for k, v in d.items() if isinstance(v, (int, float)) and v > 0}
            except (OSError, ValueError, AttributeError):
                self.history = {}

    @staticmethod
    def hkey(kind, mod, name):
        return f"{kind}|{mod}|{name}"

    def job(self, kind, mod, name):
        """Context manager: with TRACKER.job("map", "cstrike", "de_dust2") as j: run(argv, on_line=j.feed)"""
        name = (name or "").split("@")[0][:48]
        j = Job(self, kind, mod or "", name, self.history.get(self.hkey(kind, mod or "", name)))
        with self.lock:
            self.jobs.append(j)
        return j

    def finish(self, job, ok=True):
        path, data = None, None
        with self.lock:
            if job in self.jobs:
                self.jobs.remove(job)
            if ok:
                self.history[self.hkey(job.kind, job.mod, job.name)] = max(1.0, time.monotonic() - job.t0)
                if len(self.history) > 200:
                    for k in list(self.history)[:50]:
                        del self.history[k]
                path, data = self.path, dict(self.history)
        if path:
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, path)
            except OSError:
                pass

    def public(self):
        now = time.monotonic()
        with self.lock:
            return [j.snapshot(now) for j in self.jobs[:MAX_JOBS]]


TRACKER = Tracker()


def run(argv, timeout, on_line=None, **popen_kw):
    """subprocess.run(capture_output, text) replacement that streams stdout lines into on_line while the child runs.
    Progress lines (@plan / @progress, when on_line returns True) are not part of the returned stdout.
    -> CompletedProcess; raises subprocess.TimeoutExpired / OSError like subprocess.run."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", **popen_kw)
    out, err = [], []

    def pump_out():
        for line in p.stdout:
            line = line.rstrip("\r\n")
            if on_line is not None and on_line(line):
                continue
            out.append(line)

    ts = [threading.Thread(target=pump_out, daemon=True),
          threading.Thread(target=lambda: err.extend(ln.rstrip("\r\n") for ln in p.stderr), daemon=True)]
    for t in ts:
        t.start()
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        raise
    finally:
        for t in ts:
            t.join(5)
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass
    return subprocess.CompletedProcess(argv, p.returncode, "\n".join(out) + ("\n" if out else ""),
                                       "\n".join(err) + ("\n" if err else ""))
