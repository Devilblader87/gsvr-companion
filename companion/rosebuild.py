"""ROSE pack build manager (BYOG): builds the RPG1 packs rose__<name>.rpg1 from the player's OWN ROSE install with the vendored
converter `rose_conv` (lane B, companion/rose_conv/), caches them in the rose folder and tells the companion's "rose" slot about
progress. Stdlib only. Design + lane contract: docs/design/rose_companion_build.md.

Cache:  <rose dir>/rose__<name>.rpg1  +  rose__<name>.rpg1.json  {"conv": VERSION, "install": fingerprint, "bytes", "built", "seconds"}
        fingerprint = sha256 over (relative path, size, mtime) of the install's VFS files (rose_conv.install_files(install):
        data.idx + the .VFS stores; a converter without it: the *.idx files at the top of the install), plus the lines of
        rose_conv.install_extra(install) when the converter has it (3Ddata/Control/Xml + Sound folder stats, ogg decoder).
Per pack (with a usable install):  missing = no .rpg1;  stale = no sidecar / other conv VERSION / other fingerprint;  fresh = else.
Without a usable install (or without rose_conv) every file in the folder is served as is (hand-dropped dev / test packs).

Builds: one at a time in one worker thread (zone builds use 1-3 GB). On-demand (request) first; the latest on-demand request wins
and pre-empts a background build or an on-demand build below PREEMPT_KEEP_FRAC (cancel -> requeued at the front of the background
queue); an on-demand build past it is finished first and the new request runs next. Background prebuild of the other PACKS starts
after the first successful on-demand build, or when an install becomes known (set_install). A second request for the pack that is
building joins it. A failed pack is not rebuilt on every request: the error is answered again until FAIL_RETRY_S passed, the
install fingerprint changed or "Build all" was pressed. The HTTP server never waits for any of this: request() only looks at the
disk and the queue, and no disk IO runs with the manager lock held.

Hooks (module level, after init()):  set_install(path), state(), build_all(force=False), request(name), cancel(), stop().
status.json carries public_state(state()) (no user folder names); the full state is GUI only (GET /rose/state).
Log lines:  [rose] build <name> started / NN% <stage> / done in N s / failed: ... / cancelled
Converter selection: `import rose_conv`; GSVR_ROSE_CONV=stub selects companion/rose_conv_stub.py (tests / dev only).
"""
import datetime, glob, hashlib, json, os, re, threading, time

import rose

NO_INSTALL = "ROSE install not found: set the ROSE folder in the companion"
NO_CONV = "ROSE converter not bundled with this companion"
NOTIFY_MIN_S = 1.0          # building progress pushed to the slot at least this often (and on every stage change / +1 %)
STATE_CACHE_S = 2.0         # state(): per pack fresh/stale/missing re-checked at most this often (status.json polls)
FAIL_RETRY_S = 300.0        # a failed pack is built again after this long (or after the install changed / Build all)
PREEMPT_KEEP_FRAC = 0.5     # a running on-demand build past this progress is finished before the next on-demand pack


def load_conv(log=None):
    """-> the converter module (rose_conv, or rose_conv_stub when GSVR_ROSE_CONV=stub) or None when it is not bundled."""
    try:
        if os.environ.get("GSVR_ROSE_CONV", "") == "stub":
            import rose_conv_stub as m
        else:
            import rose_conv as m
        return m
    except ImportError:
        return None
    except Exception as e:                       # noqa: BLE001 - a broken converter must not take the companion down
        if log:
            log(f"[rose] converter failed to load: {e}")
        return None


def pack_path(rose_dir, name):
    return os.path.join(rose_dir, f"rose__{name}.rpg1")


def sidecar_path(rose_dir, name):
    return pack_path(rose_dir, name) + ".json"


def index_files(conv, install):
    """-> absolute paths of the install files the fingerprint covers: rose_conv.install_files(install) (relative paths:
    data.idx + the .VFS stores), else the *.idx files at the top of the install."""
    fn = getattr(conv, "install_files", None)
    if callable(fn):
        try:
            return [os.path.join(install, p) for p in fn(install)]
        except Exception:                        # noqa: BLE001
            return []
    return sorted(p for p in glob.glob(os.path.join(install, "*")) if p.lower().endswith(".idx") and os.path.isfile(p))


def fingerprint(conv, install):
    """sha256 (32 hex) over (relative path, size, mtime) of the install's VFS index files + the converter's install_extra()
    lines (loose folders the build reads, decoder availability). Cheap: stat only."""
    h = hashlib.sha256()
    for p in sorted(index_files(conv, install)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        rel = os.path.relpath(p, install).replace("\\", "/")
        h.update(f"{rel}\0{st.st_size}\0{int(st.st_mtime)}\n".encode("utf-8", "replace"))
    extra = getattr(conv, "install_extra", None)
    if callable(extra):
        try:
            for line in extra(install):
                h.update(("x\0" + str(line) + "\n").encode("utf-8", "replace"))
        except Exception:                        # noqa: BLE001 - an unreadable folder = its own fingerprint
            h.update(b"x\0error\n")
    return h.hexdigest()[:32]


def read_sidecar(rose_dir, name):
    try:
        with open(sidecar_path(rose_dir, name), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_sidecar(rose_dir, name, data):
    p = sidecar_path(rose_dir, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---- public status (audit 5): no user folder names in status.json (any world can read it) ----------------------------
_HOME_RES = (re.compile(r"[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/:*?\"<>|\r\n]+", re.I),
             re.compile(r"(?<![\w.])/(?:Users|home)/[^/:\"'\r\n]+"))


def scrub(text):
    """the user's home folder (and any C:\\Users\\<name> / /Users/<name> / /home/<name> prefix) -> "<home>"."""
    if not isinstance(text, str) or not text:
        return text
    homes = {os.path.expanduser("~"), os.environ.get("USERPROFILE") or "", os.environ.get("HOME") or ""}
    for h in sorted((h.rstrip("\\/") for h in homes if h and len(h.rstrip("\\/")) > 3), key=len, reverse=True):
        text = re.sub(re.escape(h), "<home>", text, flags=re.I)
    for r in _HOME_RES:
        text = r.sub("<home>", text)
    return text


def public_state(st):
    """state() for status.json: install / install_reason / per pack errors scrubbed (the full one: GET /rose/state, GUI only)."""
    if not isinstance(st, dict):
        return st
    out = dict(st, install=scrub(st.get("install")), install_reason=scrub(st.get("install_reason")))
    out["packs"] = {n: dict(p, error=scrub(p.get("error"))) for n, p in (st.get("packs") or {}).items()}
    return out


class Manager:
    def __init__(self, rose_dir, conv=None, log=print, on_update=None, autoload=True):
        self.rose_dir = rose_dir
        self.log = log or (lambda m: None)
        self.conv = conv if conv is not None else (load_conv(self.log) if autoload else None)
        self.on_update = on_update              # on_update(name, slot_fields): called WITHOUT the manager lock held
        self.cv = threading.Condition()
        self.install, self.install_ok, self.install_reason = None, False, "not set"
        self.gen = 0                            # install generation (a build of an older install is never requeued)
        self.demand = None                      # pending on-demand pack (latest request wins)
        self.queue = []                         # background prebuild queue
        self.forced = set()                     # build_all(force=True): queued packs built even when fresh
        self.cur = None                         # running build: {name, kind, stage, progress, t0, hist_s, cancel, requeue, ...}
        self.errors = {}                        # name -> last build error text (GUI view)
        self.failed = {}                        # name -> {t, fp, conv, err}: no rebuild before FAIL_RETRY_S / other install
        self.prebuild_started = False
        self.stopped = False
        self.finishing = 0                      # a finished build whose slot update is still being delivered (wait_idle)
        self.thread = None
        self._state_cache = (0.0, None)
        self._state_rev = 0                     # bumped by _invalidate(): a state() computed outside the lock is cached only when unchanged
        self.clean_parts()

    # ---- helpers -----------------------------------------------------------------------------
    def packs(self):
        p = getattr(self.conv, "PACKS", None) if self.conv and self.rose_dir else None
        return list(p) if p else []

    def usable(self):
        return self.conv is not None and self.install_ok and self.install is not None and bool(self.rose_dir)

    def _invalidate(self):
        """caller holds the lock: the per pack view is recomputed on the next state()"""
        self._state_cache = (0.0, None)
        self._state_rev += 1

    def clean_parts(self):
        """Leftovers of a crash / kill: rose__*.rpg1.part and sidecar .tmp files."""
        if not self.rose_dir or not os.path.isdir(self.rose_dir):
            return
        for p in glob.glob(os.path.join(self.rose_dir, "rose__*.rpg1.part")) + \
                glob.glob(os.path.join(self.rose_dir, "rose__*.rpg1.json.tmp")):
            _rm(p)
            self.log(f"[rose] removed leftover {os.path.basename(p)}")

    def pack_state(self, name, fp=None):
        """-> "fresh" | "stale" | "missing" (see the module doc). fp: the install fingerprint (computed when None).
        Disk IO: the manager never calls it with its lock held."""
        if not os.path.isfile(pack_path(self.rose_dir, name)):
            return "missing"
        if not self.usable():
            return "fresh"                       # nothing to compare with: a hand-dropped / older pack is served as is
        sc = read_sidecar(self.rose_dir, name)
        if not sc or sc.get("conv") != getattr(self.conv, "VERSION", None):
            return "stale"
        if sc.get("install") != (fp if fp is not None else fingerprint(self.conv, self.install)):
            return "stale"
        return "fresh"

    def _no_install_text(self):
        if self.conv is None:
            return NO_CONV
        if self.install and not self.install_ok:
            return f"ROSE install not usable ({self.install_reason}): set the ROSE folder in the companion"
        return NO_INSTALL

    def _eta(self, cur, now=None):
        el = (now or time.monotonic()) - cur["t0"]
        f = cur["progress"]
        if f >= 0.05 and el >= 1:
            return int(round(el * (1 - f) / f))
        if cur.get("hist_s"):
            return int(max(0, round(cur["hist_s"] * (1 - f))))
        return -1

    def _building(self, name):
        cur = self.cur if self.cur and self.cur["name"] == name else None
        return {"state": "building", "size": 0, "error": None, "path": None,
                "stage": cur["stage"] if cur else "queued", "progress": round(cur["progress"], 3) if cur else 0.0,
                "eta_s": self._eta(cur) if cur else -1}

    def _notify(self, name, fields):
        if self.on_update:
            try:
                self.on_update(name, fields)
            except Exception as e:               # noqa: BLE001
                self.log(f"[rose] slot update failed: {e}")

    def _ensure_thread(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="rosebuild", daemon=True)
            self.thread.start()

    def _preempt(self, by):
        """cancel the running build (requeued at the front of the background queue)"""
        if self.cur and not self.cur["cancel"].is_set():
            self.cur["requeue"] = True
            self.cur["preempted_by"] = by
            self.cur["cancel"].set()

    @staticmethod
    def _keep_running(cur):
        """an on-demand build past PREEMPT_KEEP_FRAC is finished first (the new request waits as the next demand) instead of
        being cancelled: two zones asked alternately no longer restart each other forever."""
        return bool(cur) and cur["kind"] == "demand" and not cur["cancel"].is_set() and cur["progress"] >= PREEMPT_KEEP_FRAC

    def _fail_block(self, name, fp, now):
        """caller holds the lock. -> the failure record while a failed build of name is in its cool-down (same install
        fingerprint + converter VERSION, less than FAIL_RETRY_S ago), else None (an outdated record is dropped)."""
        f = self.failed.get(name)
        if not f:
            return None
        if f["fp"] == fp and f["conv"] == getattr(self.conv, "VERSION", None) and now - f["t"] < FAIL_RETRY_S:
            return f
        self.failed.pop(name, None)
        return None

    # ---- API ---------------------------------------------------------------------------------
    def request(self, name):
        """[GSVR-REQ] rose=<name> (name already validated) -> slot fields {state: ready|error|building, size, error, path,
        + stage, progress, eta_s while building}. Never blocks on a build; the disk checks run outside the manager lock."""
        for _attempt in range(3):
            with self.cv:
                usable, install, gen, known = self.usable(), self.install, self.gen, name in self.packs()
            if not usable or not known:
                r = rose.prepare(self.rose_dir, name)
                if r["state"] == "error" and "no rose__" in (r["error"] or "") and known:
                    r["error"] = f"{self._no_install_text()} (no rose__{name}.rpg1 in the rose folder)"
                elif r["state"] == "error" and "no rose__" in (r["error"] or "") and self.conv is not None \
                        and not usable:
                    r["error"] = f"{r['error']}; {self._no_install_text()}"
                return r
            fp = fingerprint(self.conv, install)
            if self.pack_state(name, fp) == "fresh":
                r = rose.prepare(self.rose_dir, name)
                if r["state"] == "ready":
                    return r
                self.log(f"[rose] {name}: cached pack unusable ({r['error']}), rebuilding")
            now = time.monotonic()
            with self.cv:
                if gen != self.gen:
                    continue                                 # the install changed meanwhile: decide again with the new one
                f = self._fail_block(name, fp, now)
                if f:
                    left = max(1, int(FAIL_RETRY_S - (now - f["t"])))
                    return {"state": "error", "size": 0, "path": None,
                            "error": f"ROSE build of {name} failed: {f['err']} (next try in {left} s, or after the ROSE "
                                     f"install changed / Build all ROSE zones now in the companion)"}
                self.errors.pop(name, None)
                cur = self.cur
                if cur and cur["name"] == name and not cur["cancel"].is_set():
                    cur["kind"] = "demand"                       # join the running build (background -> on-demand)
                    if self.demand is not None and self.demand != name:
                        self._requeue_front(self.demand)
                    self.demand = None
                elif self.demand == name:
                    pass                                         # join the pending one
                else:
                    if self.demand is not None:
                        self._requeue_front(self.demand)
                    self.demand = name
                    if name in self.queue:
                        self.queue.remove(name)
                    if self._keep_running(cur):
                        self.log(f"[rose] {name} runs next: {cur['name']} is {int(cur['progress'] * 100)}% done (not pre-empted)")
                    else:
                        self._preempt(name)
                    self._ensure_thread()
                    self.cv.notify_all()
                return self._building(name)
        return {"state": "error", "size": 0, "path": None, "error": f"ROSE install changed while asking for {name}: ask again"}

    def _requeue_front(self, name):
        if name in self.queue:
            self.queue.remove(name)
        self.queue.insert(0, name)

    def set_install(self, path, prebuild=True):
        """The player's ROSE install (GUI field / --rose-install / auto find). None clears it. A change cancels the running
        build; a pending on-demand pack is rebuilt from the new install. -> (ok, reason)."""
        path = os.path.abspath(path) if path else None
        if self.conv is None:
            ok, reason = False, NO_CONV
        elif path is None:
            ok, reason = False, "not set"
        else:
            try:
                ok, reason = self.conv.check_install(path)
            except Exception as e:                           # noqa: BLE001
                ok, reason = False, f"check failed: {e}"
        fail, pb_gen = None, None
        with self.cv:
            if path == self.install and ok == self.install_ok:
                return ok, reason
            resume = self.demand or (self.cur["name"] if self.cur and self.cur["kind"] == "demand"
                                     and not self.cur["cancel"].is_set() else None)
            if self.cur:
                self.cur["requeue"] = False
                self.cur["cancel"].set()
            self.install, self.install_ok, self.install_reason = path, bool(ok), reason
            self.gen += 1
            self.demand, self.queue, self.errors, self.failed, self.forced = None, [], {}, {}, set()
            self.prebuild_started = False
            self._invalidate()
            self.log(f"[rose] install: {path or '-'} ({'ok' if ok else reason})")
            if ok:
                if resume:
                    self.demand = resume
                if prebuild:
                    pb_gen = self.gen
                if self.demand:
                    self._ensure_thread()
                self.cv.notify_all()
            elif resume:
                fail = resume
        if fail:
            self._notify(fail, {"state": "error", "size": 0, "path": None, "error": self._no_install_text()})
        if pb_gen is not None:
            self._prebuild(pb_gen)
        return ok, reason

    def _prebuild(self, gen, force=False):
        """queue every PACKS entry that is not fresh (force: all) behind the on-demand one; packs in their failure cool-down
        stay out unless forced. Disk checks outside the lock; gen = the install generation the caller decided for. -> queued names."""
        with self.cv:
            if gen != self.gen or not self.usable() or self.stopped:
                return []
            self.prebuild_started = True
            install = self.install
        fp = fingerprint(self.conv, install)
        states = {n: ("forced" if force else self.pack_state(n, fp)) for n in self.packs()}
        now = time.monotonic()
        with self.cv:
            if gen != self.gen or not self.usable() or self.stopped:
                return []
            busy = {self.demand, self.cur["name"] if self.cur and not self.cur["cancel"].is_set() else None}
            add = [n for n in self.packs() if n not in busy and n not in self.queue and states.get(n) != "fresh"
                   and (force or self._fail_block(n, fp, now) is None)]
            if force:
                self.forced.update(add)
            self.queue.extend(add)
            if add:
                self.log("[rose] background prebuild: " + ", ".join(add))
                self._invalidate()
                self._ensure_thread()
                self.cv.notify_all()
        return add

    def build_all(self, force=False):
        """GUI "Build all ROSE zones now": queue every pack that is not fresh (force=True: every pack). An explicit retry:
        clears the failure cool-downs. -> queued names."""
        with self.cv:
            if not self.usable():
                return []
            self.errors, self.failed = {}, {}
            gen = self.gen
        return self._prebuild(gen, force=force)

    def cancel(self):
        """Stop the running build and drop the queue (no requeue). The on-demand slot gets an error."""
        with self.cv:
            name = self.cur["name"] if self.cur and self.cur["kind"] == "demand" else self.demand
            if self.cur:
                self.cur["requeue"] = False
                self.cur["cancel"].set()
            self.demand, self.queue, self.forced = None, [], set()
        if name:
            self._notify(name, {"state": "error", "size": 0, "path": None, "error": f"ROSE build of {name} cancelled"})

    def stop(self):
        with self.cv:
            self.stopped = True
            if self.cur:
                self.cur["requeue"] = False
                self.cur["cancel"].set()
            self.cv.notify_all()

    def wait_idle(self, timeout=None):
        """tests / CLI: wait until nothing runs or is queued -> True when idle"""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cv:
            while self.cur or self.demand or self.queue or self.finishing:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self.cv.wait(left if left is not None else 1.0)
            return True

    def _pack_rows(self):
        """per pack {state, bytes, built, label} (disk IO: called without the lock)"""
        fp = fingerprint(self.conv, self.install) if self.usable() else None
        packs = {}
        for n in self.packs():
            sc = read_sidecar(self.rose_dir, n) or {}
            try:
                size = os.path.getsize(pack_path(self.rose_dir, n))
            except OSError:
                size = 0
            spec = self.conv.PACKS.get(n) if isinstance(self.conv.PACKS, dict) else None
            label = spec.get("label") if isinstance(spec, dict) else None
            packs[n] = {"state": self.pack_state(n, fp), "bytes": size, "built": sc.get("built"), "label": label or n}
        return packs

    def state(self):
        """GUI view: {install, install_ok, install_reason, conv, current: {name, kind, stage, progress, eta_s, elapsed_s} | None,
        queue: [names], packs: {name: {state: fresh|stale|missing, bytes, built, error}}}. JSON-safe; cheap (cached 2 s).
        Full detail incl. the install path: GUI only (GET /rose/state); status.json carries public_state() of it."""
        now = time.monotonic()
        with self.cv:
            at, packs = self._state_cache
            rev = self._state_rev
        if packs is None or now - at > STATE_CACHE_S:
            packs = self._pack_rows()
            with self.cv:
                if rev == self._state_rev:
                    self._state_cache = (now, packs)
        with self.cv:
            packs = {n: dict(v, error=self.errors.get(n)) for n, v in packs.items()}
            cur = self.cur
            return {"install": self.install, "install_ok": self.install_ok, "install_reason": self.install_reason,
                    "conv": getattr(self.conv, "VERSION", None) if self.conv else None,
                    "current": None if not cur else {"name": cur["name"], "kind": cur["kind"], "stage": cur["stage"],
                                                     "progress": round(cur["progress"], 3), "eta_s": self._eta(cur, now),
                                                     "elapsed_s": int(now - cur["t0"])},
                    "queue": ([self.demand] if self.demand else []) + list(self.queue),
                    "packs": packs}

    # ---- worker ------------------------------------------------------------------------------
    def _progress(self, cur, stage, frac):
        now = time.monotonic()
        try:
            frac = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            frac = cur["progress"]
        stage = str(stage)[:40]
        with self.cv:
            changed = stage != cur["stage"]
            cur["stage"], cur["progress"] = stage, max(cur["progress"], frac)
            pct = int(cur["progress"] * 100)
            push = changed or cur["progress"] - cur["pushed"] >= 0.01 or now - cur["pushed_at"] >= NOTIFY_MIN_S
            logit = changed or pct // 10 != cur["logged"] // 10
            if push:
                cur["pushed"], cur["pushed_at"] = cur["progress"], now
            if logit:
                cur["logged"] = pct
            fields = self._building(cur["name"]) if push and cur["kind"] == "demand" and not cur["cancel"].is_set() else None
        if logit:
            self.log(f"[rose] build {cur['name']} {pct}% {stage}")
        if fields:
            self._notify(cur["name"], fields)

    def _run(self):
        while True:
            with self.cv:
                while not self.stopped and self.demand is None and not self.queue:
                    self.cv.wait()
                if self.stopped:
                    return
                if self.demand is not None:
                    name, kind, self.demand = self.demand, "demand", None
                else:
                    name, kind = self.queue.pop(0), "background"
                if not self.usable():
                    self.cv.notify_all()
                    continue
                # claimed before the disk checks (which run outside the lock): a request meanwhile joins this build
                cur = {"name": name, "kind": kind, "stage": "starting", "progress": 0.0, "t0": time.monotonic(),
                       "hist_s": None, "cancel": threading.Event(), "requeue": False, "preempted_by": None,
                       "pushed": 0.0, "pushed_at": 0.0, "logged": -10}
                self.cur, install, gen = cur, self.install, self.gen
                forced = name in self.forced
                self._invalidate()
            fp = fingerprint(self.conv, install)
            fresh = not forced and self.pack_state(name, fp) == "fresh"
            sc = read_sidecar(self.rose_dir, name) or {}
            with self.cv:
                if fresh and cur["kind"] == "background" and not cur["cancel"].is_set():
                    self.cur = None                          # built meanwhile (or never stale): nothing to do
                    self._invalidate()
                    self.cv.notify_all()
                    continue
                cur["hist_s"] = sc.get("seconds") if isinstance(sc.get("seconds"), (int, float)) else None
                kind = cur["kind"]
                fields = self._building(name) if kind == "demand" else None
            self.log(f"[rose] build {name} started ({kind}, conv {getattr(self.conv, 'VERSION', '?')})")
            if fields:
                self._notify(name, fields)
            self._build_one(cur, install, fp, gen)

    def _build_one(self, cur, install, fp, gen):
        name = cur["name"]
        out = pack_path(self.rose_dir, name)
        result, err, cancelled = None, None, False
        try:
            os.makedirs(self.rose_dir, exist_ok=True)
            res = self.conv.build(name, install, out, progress=lambda s, f: self._progress(cur, s, f),
                                  cancel=cur["cancel"].is_set)
            size = rose.validate(out)
            secs = round(time.monotonic() - cur["t0"], 1)
            write_sidecar(self.rose_dir, name, {"conv": getattr(self.conv, "VERSION", None), "install": fp, "bytes": size,
                                                "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                                "seconds": secs,
                                                "warnings": list((res or {}).get("warnings") or [])[:20]
                                                if isinstance(res, dict) else []})
            result = secs
        except Exception as e:                             # noqa: BLE001 - incl. rose_conv.Cancelled / BuildError
            canc_cls = getattr(self.conv, "Cancelled", None)
            if cur["cancel"].is_set() or (isinstance(canc_cls, type) and isinstance(e, canc_cls)):
                cancelled = True
            else:
                err = str(e) or type(e).__name__
        finally:
            _rm(out + ".part")
        pb_gen = None
        with self.cv:
            self.cur = None
            self.finishing += 1
            self._invalidate()
            notify = None
            if cancelled:
                if cur["requeue"] and gen == self.gen and name != self.demand and name not in self.queue:
                    self.queue.insert(0, name)
            elif err is not None:
                self.forced.discard(name)
                self.errors[name] = err
                if gen == self.gen:
                    self.failed[name] = {"t": time.monotonic(), "fp": fp, "conv": getattr(self.conv, "VERSION", None), "err": err}
                if cur["kind"] == "demand":
                    notify = {"state": "error", "size": 0, "path": None, "error": f"ROSE build of {name} failed: {err}"}
            else:
                self.forced.discard(name)
                self.failed.pop(name, None)
                if cur["kind"] == "demand" and not self.prebuild_started and gen == self.gen:
                    pb_gen = gen
                notify = "ready" if cur["kind"] == "demand" else None
            self.cv.notify_all()
        if cancelled:
            by = cur["preempted_by"]
            self.log(f"[rose] build {name} cancelled" + (f" (pre-empted by {by}, requeued)" if by and cur["requeue"] else ""))
        elif err is not None:
            self.log(f"[rose] build {name} failed: {err}")
        else:
            self.log(f"[rose] build {name} done in {result:.0f} s")
        try:
            if pb_gen is not None:
                self._prebuild(pb_gen)
            if notify == "ready":
                self._notify(name, rose.prepare(self.rose_dir, name))
            elif notify:
                self._notify(name, notify)
        finally:
            with self.cv:
                self.finishing -= 1
                self.cv.notify_all()


# ---- module level hooks (one manager per companion process) --------------------------------------
MANAGER = None


def init(rose_dir, log=print, on_update=None, conv=None, autoload=True):
    global MANAGER
    if MANAGER is not None:
        MANAGER.stop()
    MANAGER = Manager(rose_dir, conv=conv, log=log, on_update=on_update, autoload=autoload)
    return MANAGER


def _m():
    if MANAGER is None:
        raise RuntimeError("rosebuild.init() was not called")
    return MANAGER


def set_install(path, prebuild=True):
    return _m().set_install(path, prebuild=prebuild)


def state():
    return _m().state()


def build_all(force=False):
    return _m().build_all(force=force)


def request(name):
    return _m().request(name)


def cancel():
    return _m().cancel()


def stop():
    if MANAGER is not None:
        MANAGER.stop()
