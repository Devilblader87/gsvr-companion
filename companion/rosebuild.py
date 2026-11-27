"""ROSE pack build manager (BYOG): builds the RPG1 packs rose__<name>.rpg1 from the player's OWN ROSE install with the vendored
converter `rose_conv` (lane B, companion/rose_conv/), caches them in the rose folder and tells the companion's "rose" slot about
progress. Stdlib only. Design + lane contract: docs/design/rose_companion_build.md.

Cache:  <rose dir>/rose__<name>.rpg1  +  rose__<name>.rpg1.json  {"conv": VERSION, "install": fingerprint, "bytes", "built", "seconds"}
        fingerprint = sha256 over (relative path, size, mtime) of the install's VFS files (rose_conv.install_files(install):
        data.idx + the .VFS stores; a converter without it: the *.idx files at the top of the install), plus the lines of
        rose_conv.install_extra(install) when the converter has it (3Ddata/Control/Xml + Sound folder stats, ogg decoder).
Per pack (with a usable install):  missing = no .rpg1;  stale = no sidecar / other conv version (conv_version: VERSION + the pack's
PACK_REV) / other fingerprint;  fresh = else.
Without a usable install (or without rose_conv) every file in the folder is served as is (hand-dropped dev / test packs).

Builds: at most PARALLEL_MAX = 2 at once (one worker thread each), never two zone builds at once (LOAD PACKAGE 5: a first join
builds the login and the avatar pack side by side, the likely zone starts as soon as one of them is done). Memory (Mac M4, peak RSS
of one child, 0.9.44 converter): zone Adventurer's Plain 0.94 GB, login 0.55 GB, avatar 0.57 GB, so two children stay under
~1.6 GB (earlier notes: zone builds 1-3 GB in-process). In-process builds (a converter module given to the Manager without
parallel=, GSVR_ROSE_INPROC=1) stay one at a time: rose_conv keeps module state (rose_text) and holds the GIL. The companion runs each build in a CHILD PROCESS
(isolate=True, the default for an auto-loaded converter): rose_conv is pure Python CPU work, and in a thread it held the GIL so
long that the HTTP server starved (status.json 2+ s, the GUI flipped to "Starting", the world's polls slowed; maintainer
2026-11-14). The child is `gsvr-cli --run-rose-build NAME INSTALL OUT` (frozen) / `python -I gsvr_main.py --run-rose-build ...`
(source), below normal priority (Windows BELOW_NORMAL_PRIORITY_CLASS + CREATE_NO_WINDOW, POSIX nice +5), and prints
`@progress <frac> <stage>` lines and one final `@result {json}` / `@error {json}` line on stdout (child_main / parse_child_line).
Cancel / pre-emption = terminate the child (+ the .part cleanup). The child exits when its stdin closes (the companion died).
A Manager given an explicit converter module (tests) builds in-process in the worker thread unless isolate=True. On-demand (request) first; the latest on-demand request wins
and pre-empts a background build or an on-demand build below PREEMPT_KEEP_FRAC (cancel -> requeued at the front of the background
queue) when no worker is free for it (or it is a zone and another zone builds); an on-demand build past it is finished first and
the new request runs next. While an on-demand pack waits, no background build starts. Background prebuild of the other PACKS starts
after the first successful on-demand build, or when an install becomes known (set_install). A second request for the pack that is
building joins it. A failed pack is not rebuilt on every request: the error is answered again until FAIL_RETRY_S passed, the
install fingerprint changed or "Build all" was pressed. The HTTP server never waits for any of this: request() only looks at the
disk and the queue, and no disk IO runs with the manager lock held.

Hooks (module level, after init()):  set_install(path), state(), build_all(force=False), request(name), cancel(), stop().
status.json carries public_state(state(cached=True)) (no user folder names); the full state is GUI only (GET /rose/state).
state(cached=True) never touches the disk: the per pack rows come from a cache that a "rosestate" thread refreshes off the
request path (the real converter's install_extra() walks the Sound folder, far too slow for every status poll).
Log lines:  [rose] build <name> started / NN% <stage> / done in N s / failed: ... / cancelled
Converter selection: `import rose_conv`; GSVR_ROSE_CONV=stub selects companion/rose_conv_stub.py (tests / dev only).
"""
import collections, datetime, glob, hashlib, importlib, json, os, re, subprocess, sys, threading, time

import rose, ro
import runtime

NO_INSTALL = "ROSE install not found: set the ROSE folder in the companion"
NO_CONV = "ROSE converter not bundled with this companion"
NOTIFY_MIN_S = 1.0          # building progress pushed to the slot at least this often (and on every stage change / +1 %)
STATE_CACHE_S = 2.0         # state(): per pack fresh/stale/missing re-checked at most this often (status.json polls)
FAIL_RETRY_S = 300.0        # a failed pack is built again after this long (or after the install changed / Build all)
PREEMPT_KEEP_FRAC = 0.5     # a running on-demand build past this progress is finished before the next on-demand pack
CHILD_FLAG = "--run-rose-build"   # gsvr_main.py: run one pack build in this (child) process
CHILD_NICE = 5              # POSIX: the child lowers its own priority by this much
CREATE_NO_WINDOW = 0x08000000
KILL_WAIT_S = 5.0           # terminate() -> kill() after this long
PARALLEL_MAX = 2            # LOAD PACKAGE 5: child builds at once (never two zones); in-process builds: 1
LAST_ZONE_FILE = "rosebuild_last.json"      # in the rose folder: {"zone": name}, the last zone pack the world asked for (likely zone of the next first load)


class Flavor:
    """What differs between the game flavours that share this build manager: ROSE (rose__<name>.rpg1, converter rose_conv) and
    Ragnarok Online (ro__<name>.pack, converter ro_conv, companion/ro.py). The manager itself is the same code."""
    def __init__(self, tag, label, prefix, ext, child_flag, env, conv_module, stub_module, mod):
        self.tag, self.label, self.prefix, self.ext = tag, label, prefix, ext
        self.child_flag, self.env = child_flag, env
        self.conv_module, self.stub_module, self.mod = conv_module, stub_module, mod
        self.no_install = f"{label} install not found: set the {label} folder in the companion"
        self.no_conv = f"{label} converter not bundled with this companion"

    def pack_file(self, name):
        return f"{self.prefix}__{name}{self.ext}"


ROSE = Flavor("rose", "ROSE", "rose", ".rpg1", CHILD_FLAG, "GSVR_ROSE_CONV", "rose_conv", "rose_conv_stub", rose)
RO = Flavor("ro", "Ragnarok", "ro", ".pack", "--run-ro-build", "GSVR_RO_CONV", "ro_conv", "ro_conv_stub", ro)


def load_conv(log=None, fl=None):
    """-> the converter module (rose_conv, or rose_conv_stub when GSVR_ROSE_CONV=stub; fl=RO: ro_conv / GSVR_RO_CONV) or None when it
    is not bundled."""
    fl = fl or ROSE
    try:
        m = importlib.import_module(fl.stub_module if os.environ.get(fl.env, "") == "stub" else fl.conv_module)
        return m
    except ImportError:
        return None
    except Exception as e:                       # noqa: BLE001 - a broken converter must not take the companion down
        if log:
            log(f"[{fl.tag}] converter failed to load: {e}")
        return None


class _AudioMerged:
    """the RO converter as the build manager sees it: PACKS = the map packs + AUDIO_PACKS (snd_common, snd_<map>), everything else delegated.
    A view, not a mutation: ro_conv.PACKS stays the map list the world loads (tests + converter CLI read it); the audio packs are built after the maps
    (they are not in PREBUILD_ORDER, so prebuild_order appends them) and served by name like any pack."""

    def __init__(self, conv):
        self._conv = conv
        self.PACKS = dict(conv.PACKS)
        ui = getattr(conv, "UI_PACKS", None)          # w42 ui-a: the 'roui' interface pack (ro_conv.UI_PACKS), served by name like the audio packs
        if isinstance(ui, dict):
            self.PACKS.update(ui)
        npc = getattr(conv, "NPC_PACKS", None)          # w43 npc lane: the 'npc_<town>' NPC sprite packs (ro_conv.NPC_PACKS), served by name like the ui pack
        if isinstance(npc, dict):
            self.PACKS.update(npc)
        self.PACKS.update(conv.AUDIO_PACKS)

    def __getattr__(self, name):
        return getattr(self._conv, name)


def merge_audio(conv):
    """PACKS.update(AUDIO_PACKS) for a converter that has AUDIO_PACKS (ro_conv); any other converter (ROSE, stubs) is returned unchanged"""
    if conv is None or isinstance(conv, _AudioMerged) or not isinstance(getattr(conv, "AUDIO_PACKS", None), dict) or not isinstance(getattr(conv, "PACKS", None), dict):
        return conv
    return _AudioMerged(conv)


def conv_version(conv, name):
    """the converter version a pack of `name` is stamped / compared with: conv.pack_version(name) (VERSION + the per-pack revision of
    rose_conv.PACK_REV, so bumping one entry rebuilds only that pack), conv.VERSION for a converter without it, None without a converter."""
    pv = getattr(conv, "pack_version", None)
    if callable(pv):
        return pv(name)
    return getattr(conv, "VERSION", None)


# W10 FIRST LOAD (first join of a new player): the world asks for the lobby pack, then the avatar pack, then (after the pick) the start zone.
LOBBY_PACK, AVATAR_PACK = "login", "avatar"


def start_zone(conv):
    """the start zone pack (a new character's first zone): the first PACKS entry of kind "zone" (birthisland); None without one"""
    for n, spec in (getattr(conv, "PACKS", None) or {}).items():
        if isinstance(spec, dict) and spec.get("kind") == "zone":
            return n
    return None


def is_zone(conv, name):
    spec = (getattr(conv, "PACKS", None) or {}).get(name)
    return isinstance(spec, dict) and spec.get("kind") == "zone"


def likely_zone(conv, last=None):
    """LOAD PACKAGE 5: the zone a first load most likely ends in: the last zone pack the world asked for (rosebuild_last.json; the companion
    cannot read the player's save slots) while PACKS still has it, else the start zone (a new player's first character starts there)"""
    if last and is_zone(conv, last):
        return last
    return start_zone(conv)


def first_contact(conv, zone=None):
    """the packs a first join needs, in the order the world asks for them: lobby, avatar, then `zone` (likely_zone) or the start zone (only those PACKS has)"""
    packs = getattr(conv, "PACKS", None) or {}
    z = zone if zone and is_zone(conv, zone) else start_zone(conv)
    return [n for n in (LOBBY_PACK, AVATAR_PACK, z) if n and n in packs]


def prebuild_order(conv, names, zone=None):
    """background prebuild order: the first-contact packs first (lobby, avatar, likely zone), then the rest in PACKS order.
    A converter with PREBUILD_ORDER (ro_conv: the RO world loads every map at once, start map first) keeps that fixed order instead."""
    names = [n for n in names if n not in getattr(conv, "NO_PREBUILD", ())]       # ro_conv NO_PREBUILD: on-demand packs (the dungeons lane), built only when the world asks
    fixed = getattr(conv, "PREBUILD_ORDER", None)
    if fixed:
        return [n for n in fixed if n in names] + [n for n in names if n not in fixed]
    fc = first_contact(conv, zone)
    return [n for n in fc if n in names] + [n for n in names if n not in fc]


def warm_after(conv, name, zone=None):
    """W10 warm-up: when the world asks for `name`, the packs it will ask for next (they go to the front of the background queue, so their
    build overlaps the lobby instead of starting at the pick): login -> avatar + likely zone, avatar -> likely zone, anything else -> none.
    A converter with warm_after(name) (ro_conv: the maps the RO world asks for after `name`) answers itself."""
    wa = getattr(conv, "warm_after", None)
    if callable(wa):
        return list(wa(name))
    fc = first_contact(conv, zone)
    return fc[fc.index(name) + 1:] if name in fc else []


def read_last_zone(rose_dir):
    """the zone name rosebuild_last.json holds (a-z 0-9 _ only) or None"""
    try:
        with open(os.path.join(rose_dir, LAST_ZONE_FILE), encoding="utf-8") as fh:
            d = json.load(fh)
        z = d.get("zone") if isinstance(d, dict) else None
        return z if isinstance(z, str) and re.fullmatch(r"[a-z0-9_]{1,64}", z) else None
    except (OSError, ValueError, TypeError):
        return None


def write_last_zone(rose_dir, name):
    p = os.path.join(rose_dir, LAST_ZONE_FILE)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"zone": name}, fh)
        os.replace(tmp, p)
    except OSError:
        _rm(tmp)


def conv_env_value(conv):
    """GSVR_ROSE_CONV / GSVR_RO_CONV for a child build that must load the same converter as this process ("stub" / None = the real one)."""
    return "stub" if getattr(conv, "__name__", "").endswith("_conv_stub") else None


def child_argv(name, install, out, fl=None):
    """argv of the child process that builds one pack (frozen: gsvr-cli, source: python -I gsvr_main.py)."""
    flag = (fl or ROSE).child_flag
    if runtime.FROZEN:
        return [runtime.cli_exe(), flag, name, install, out]
    return [runtime.console_python(), "-X", "utf8", "-I", "-u", os.path.join(runtime.HERE, "gsvr_main.py"), flag,
            name, install, out]


def child_popen_kwargs():
    """below normal priority + no console window (Windows); POSIX: the child calls os.nice itself (no preexec_fn: the
    companion has threads)."""
    if sys.platform == "win32":
        return {"creationflags": runtime.BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW}
    return {}


def parse_child_line(line):
    """one stdout line of the child -> ("progress", frac, stage) | ("result", dict) | ("error", dict) | None (other output)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.rstrip("\r\n")
    if line.startswith("@progress "):
        parts = line.split(" ", 2)
        try:
            frac = float(parts[1])
        except (IndexError, ValueError):
            return None
        if frac != frac:                                 # NaN
            return None
        return ("progress", max(0.0, min(1.0, frac)), parts[2] if len(parts) > 2 else "")
    for tag in ("result", "error"):
        if line.startswith("@%s " % tag):
            try:
                d = json.loads(line[len(tag) + 2:])
            except ValueError:
                return None
            return (tag, d) if isinstance(d, dict) else None
    return None


def _watch_stdin():
    """child: the companion holds our stdin open; EOF = it died (or was killed) -> stop building at once."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError, AttributeError):
        pass
    os._exit(3)


def child_main(argv, fl=None):
    """`<exe> --run-rose-build NAME INSTALL OUT`: build one pack with the converter (GSVR_ROSE_CONV=stub: the stub) in this
    process (fl=RO: the Ragnarok converter) and report on stdout: `@progress <frac> <stage>` lines, then `@result {"conv", "result"}` (exit 0) or
    `@error {"error", "cancelled"}` (exit 1). Converter output goes to stderr. -> exit code."""
    if len(argv) != 3:
        print("usage: %s NAME INSTALL OUT" % (fl or ROSE).child_flag, file=sys.stderr)
        return 2
    name, install, out = argv
    if sys.platform != "win32":
        try:
            os.nice(CHILD_NICE)
        except (AttributeError, OSError):
            pass
    proto = sys.stdout
    sys.stdout = sys.stderr                              # stray prints never mix into the protocol
    lock = threading.Lock()

    def emit(tag, text):
        with lock:
            try:
                proto.write("@%s %s\n" % (tag, text))
                proto.flush()
            except (OSError, ValueError):
                os._exit(3)                              # the companion is gone
    if os.environ.get("GSVR_ROSE_CHILD_STDIN_WATCH", "1") != "1":
        return _child_build(name, install, out, emit, lock, fl)
    threading.Thread(target=_watch_stdin, name="rose-stdin", daemon=True).start()
    rc = _child_build(name, install, out, emit, lock, fl)
    for s in (proto, sys.stderr):                        # the stdin reader blocks in read(): a normal interpreter shutdown
        try:                                             # would abort on its buffer lock -> leave at once
            s.flush()
        except (OSError, ValueError):
            pass
    os._exit(rc)


def _child_build(name, install, out, emit, lock, fl=None):
    fl = fl or ROSE
    conv = load_conv(lambda m: print(m, file=sys.stderr), fl)
    if conv is None:
        emit("error", json.dumps({"error": fl.no_conv, "cancelled": False}))
        return 1
    last = {"f": -1.0, "stage": None, "t": 0.0}

    def progress(stage, frac):
        try:
            f = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            return
        stage = " ".join(str(stage).split())[:40]
        now = time.monotonic()
        with lock:
            if stage == last["stage"] and f - last["f"] < 0.002 and now - last["t"] < 0.25:
                return
            last.update(f=f, stage=stage, t=now)
        emit("progress", "%.4f %s" % (f, stage))
    try:
        res = conv.build(name, install, out, progress=progress, cancel=None)
    except (Exception, SystemExit) as e:                 # noqa: BLE001 - reported to the companion
        canc = getattr(conv, "Cancelled", None)
        emit("error", json.dumps({"error": str(e) or type(e).__name__,
                                  "cancelled": isinstance(canc, type) and isinstance(e, canc)}))
        return 1
    emit("result", json.dumps({"conv": conv_version(conv, name), "result": res if isinstance(res, dict) else None},
                              default=str))
    return 0


class ChildCancelled(Exception):
    pass


def pack_path(rose_dir, name, fl=None):
    return os.path.join(rose_dir, (fl or ROSE).pack_file(name))


def sidecar_path(rose_dir, name, fl=None):
    return pack_path(rose_dir, name, fl) + ".json"


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


def read_sidecar(rose_dir, name, fl=None):
    try:
        with open(sidecar_path(rose_dir, name, fl), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_sidecar(rose_dir, name, data, fl=None):
    p = sidecar_path(rose_dir, name, fl)
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
    def __init__(self, rose_dir, conv=None, log=print, on_update=None, autoload=True, isolate=None, child_env=None, parallel=None,
                 flavor=None):
        self.fl = flavor or ROSE
        self._fk = {} if self.fl is ROSE else {"fl": self.fl}     # ROSE keeps the plain call shape (tests patch these helpers)
        self.rose_dir = rose_dir
        self.log = log or (lambda m: None)
        self.conv = conv if conv is not None else (load_conv(self.log, self.fl) if autoload else None)
        if self.fl is not ROSE:
            self.conv = merge_audio(self.conv)            # RO: PACKS.update(AUDIO_PACKS) - the sound lane's snd_* packs are built + served too
        # builds in a child process: the default for the companion (auto-loaded converter); GSVR_ROSE_INPROC=1 = in-process
        if isolate is None:
            isolate = conv is None and os.environ.get("GSVR_ROSE_INPROC", "") != "1" and os.environ.get("GSVR_RO_INPROC", "") != "1"
        self.isolate = bool(isolate)
        # LOAD PACKAGE 5: builds at once (child processes: PARALLEL_MAX; in-process: 1 unless a test asks for more with the stub)
        self.parallel = max(1, int(parallel if parallel is not None else (PARALLEL_MAX if self.isolate else 1)))
        self.child_env = dict(child_env or {})          # extra environment of the child (tests: stub knobs)
        self.on_update = on_update              # on_update(name, slot_fields): called WITHOUT the manager lock held
        self.cv = threading.Condition()
        self.install, self.install_ok, self.install_reason = None, False, "not set"
        self.gen = 0                            # install generation (a build of an older install is never requeued)
        self.demand = None                      # pending on-demand pack (latest request wins)
        self.queue = []                         # background prebuild queue
        self.forced = set()                     # build_all(force=True): queued packs built even when fresh
        self.running = []                       # running builds {name, kind, stage, progress, t0, hist_s, cancel, requeue, ...}, at most self.parallel
        self.errors = {}                        # name -> last build error text (GUI view)
        self.failed = {}                        # name -> {t, fp, conv, err}: no rebuild before FAIL_RETRY_S / other install
        self.prebuild_started = False
        self.stopped = False
        self.finishing = 0                      # a finished build whose slot update is still being delivered (wait_idle)
        self.threads = []
        self.last_zone = read_last_zone(rose_dir) if rose_dir else None      # LOAD PACKAGE 5: the likely zone of the next first load
        self._state_cache = (0.0, None)
        self._state_rev = 0                     # bumped by _invalidate(): a state() computed outside the lock is cached only when unchanged
        self._refresh = threading.Event()       # wakes the "rosestate" thread (state(cached=True) found the rows old / invalid)
        self._refresher = None
        self.clean_parts()

    # ---- helpers -----------------------------------------------------------------------------
    @property
    def cur(self):
        """the build the GUI / status shows first: the first running on-demand build, else the first running one, else None"""
        run = list(self.running)
        for c in run:
            if c["kind"] == "demand":
                return c
        return run[0] if run else None

    def _run_of(self, name, live=True):
        """caller holds the lock: the running build of name (live: not cancelled) or None"""
        for c in self.running:
            if c["name"] == name and (not live or not c["cancel"].is_set()):
                return c
        return None

    def _zone(self):
        """the likely zone (likely_zone) of this manager"""
        return likely_zone(self.conv, self.last_zone)

    def _can_start(self, name):
        """caller holds the lock: a free worker, name not running (also not a cancelled build of it still winding down), never two zones"""
        if len(self.running) >= self.parallel or any(c["name"] == name for c in self.running):
            return False
        return not (is_zone(self.conv, name) and any(is_zone(self.conv, c["name"]) for c in self.running))

    def _blockers(self, name):
        """caller holds the lock: the live running builds that keep name from starting (cancelled ones end on their own)"""
        live = [c for c in self.running if not c["cancel"].is_set()]
        if is_zone(self.conv, name) and any(is_zone(self.conv, c["name"]) for c in self.running):
            return [c for c in live if is_zone(self.conv, c["name"])]
        return live if len(self.running) >= self.parallel else []

    def _pick(self):
        """caller holds the lock: -> (name, kind) for a free worker, or None. The on-demand pack first; while it has to wait, no background
        build starts (it would take the worker the on-demand pack waits for)."""
        if self.demand is not None:
            if self._can_start(self.demand):
                n, self.demand = self.demand, None
                return n, "demand"
            return None
        for i, n in enumerate(self.queue):
            if self._can_start(n):
                self.queue.pop(i)
                return n, "background"
        return None

    def _remember_zone(self, name):
        """LOAD PACKAGE 5: the world asked for zone pack name -> the likely zone of the next first load (rosebuild_last.json). No lock held."""
        if not self.rose_dir or not is_zone(self.conv, name):
            return
        with self.cv:
            if self.last_zone == name:
                return
            self.last_zone = name
        write_last_zone(self.rose_dir, name)

    def packs(self):
        p = getattr(self.conv, "PACKS", None) if self.conv and self.rose_dir else None
        return list(p) if p else []

    def usable(self):
        return self.conv is not None and self.install_ok and self.install is not None and bool(self.rose_dir)

    def _invalidate(self):
        """caller holds the lock: the per pack view is recomputed on the next state() (the cached view keeps the old rows
        until the refresher has new ones)"""
        self._state_cache = (0.0, self._state_cache[1])
        self._state_rev += 1
        if self._refresher is not None:
            self._refresh.set()

    def clean_parts(self):
        """Leftovers of a crash / kill: rose__*.rpg1.part and sidecar .tmp files."""
        if not self.rose_dir or not os.path.isdir(self.rose_dir):
            return
        for p in glob.glob(os.path.join(self.rose_dir, f"{self.fl.prefix}__*{self.fl.ext}.part")) + \
                glob.glob(os.path.join(self.rose_dir, f"{self.fl.prefix}__*{self.fl.ext}.json.tmp")):
            _rm(p)
            self.log(f"[{self.fl.tag}] removed leftover {os.path.basename(p)}")

    def pack_state(self, name, fp=None):
        """-> "fresh" | "stale" | "missing" (see the module doc). fp: the install fingerprint (computed when None).
        Disk IO: the manager never calls it with its lock held."""
        if not os.path.isfile(pack_path(self.rose_dir, name, **self._fk)):
            return "missing"
        if not self.usable():
            return "fresh"                       # nothing to compare with: a hand-dropped / older pack is served as is
        sc = read_sidecar(self.rose_dir, name, **self._fk)
        if not sc or sc.get("conv") != conv_version(self.conv, name):
            return "stale"
        if sc.get("install") != (fp if fp is not None else fingerprint(self.conv, self.install)):
            return "stale"
        return "fresh"

    def _no_install_text(self):
        if self.conv is None:
            return self.fl.no_conv
        if self.install and not self.install_ok:
            return f"{self.fl.label} install not usable ({self.install_reason}): set the {self.fl.label} folder in the companion"
        return self.fl.no_install

    def _eta(self, cur, now=None):
        el = (now or time.monotonic()) - cur["t0"]
        f = cur["progress"]
        if f >= 0.05 and el >= 1:
            return int(round(el * (1 - f) / f))
        if cur.get("hist_s"):
            return int(max(0, round(cur["hist_s"] * (1 - f))))
        return -1

    def _building(self, name):
        cur = self._run_of(name, live=False)
        return {"state": "building", "size": 0, "error": None, "path": None,
                "stage": cur["stage"] if cur else "queued", "progress": round(cur["progress"], 3) if cur else 0.0,
                "eta_s": self._eta(cur) if cur else -1}

    def _notify(self, name, fields):
        if self.on_update:
            try:
                self.on_update(name, fields)
            except Exception as e:               # noqa: BLE001
                self.log(f"[{self.fl.tag}] slot update failed: {e}")

    def _ensure_thread(self):
        self.threads = [t for t in self.threads if t.is_alive()]
        while len(self.threads) < self.parallel:
            t = threading.Thread(target=self._run, name="rosebuild" if not self.threads else "rosebuild%d" % len(self.threads), daemon=True)
            self.threads.append(t)
            t.start()

    @staticmethod
    def _preempt(cur, by):
        """cancel a running build (requeued at the front of the background queue)"""
        if cur and not cur["cancel"].is_set():
            cur["requeue"] = True
            cur["preempted_by"] = by
            cur["cancel"].set()

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
        if f["fp"] == fp and f["conv"] == conv_version(self.conv, name) and now - f["t"] < FAIL_RETRY_S:
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
            if usable and known:
                self._remember_zone(name)
            if not usable or not known:
                r = self.fl.mod.prepare(self.rose_dir, name)
                if r["state"] == "error" and f"no {self.fl.prefix}__" in (r["error"] or "") and known:
                    r["error"] = f"{self._no_install_text()} (no {self.fl.pack_file(name)} in the {self.fl.tag} folder)"
                elif r["state"] == "error" and f"no {self.fl.prefix}__" in (r["error"] or "") and self.conv is not None \
                        and not usable:
                    r["error"] = f"{r['error']}; {self._no_install_text()}"
                return r
            fp = fingerprint(self.conv, install)
            if self.pack_state(name, fp) == "fresh":
                r = self.fl.mod.prepare(self.rose_dir, name)
                if r["state"] == "ready":
                    return r
                self.log(f"[{self.fl.tag}] {name}: cached pack unusable ({r['error']}), rebuilding")
            now = time.monotonic()
            with self.cv:
                if gen != self.gen:
                    continue                                 # the install changed meanwhile: decide again with the new one
                f = self._fail_block(name, fp, now)
                if f:
                    left = max(1, int(FAIL_RETRY_S - (now - f["t"])))
                    return {"state": "error", "size": 0, "path": None,
                            "error": f"{self.fl.label} build of {name} failed: {f['err']} (next try in {left} s, or after the {self.fl.label} "
                                     f"install changed / Build all now in the companion)"}
                self.errors.pop(name, None)
                cur = self._run_of(name)
                if cur:
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
                    if not self._can_start(name):
                        # no free worker (or another zone builds): pre-empt a background build first (least progress first), then an
                        # on-demand one below PREEMPT_KEEP_FRAC; one past it is finished first
                        bl = sorted(self._blockers(name), key=lambda c: (c["kind"] == "demand", c["progress"]))
                        victim = next((c for c in bl if not self._keep_running(c)), None)
                        if victim is not None:
                            self._preempt(victim, name)
                        elif bl:
                            k = bl[0]
                            self.log(f"[{self.fl.tag}] {name} runs next: {k['name']} is {int(k['progress'] * 100)}% done (not pre-empted)")
                    self._ensure_thread()
                    self.cv.notify_all()
                self._warm_next(name, fp, now)
                return self._building(name)
        return {"state": "error", "size": 0, "path": None, "error": f"{self.fl.label} install changed while asking for {name}: ask again"}

    def _warm_next(self, name, fp, now):
        """caller holds the lock. W10 FIRST LOAD: the packs the world asks for after `name` (warm_after) go to the front of the background
        queue (behind nothing but the on-demand pack): a first join builds lobby -> avatar -> start zone back to back while the player is in
        the lobby. Fresh packs are skipped by the worker (no disk IO here); packs in their failure cool-down stay where they are."""
        nxt = [n for n in warm_after(self.conv, name, self._zone()) if n != self.demand and n in self.packs()
               and self._run_of(n) is None
               and self._fail_block(n, fp, now) is None]
        if not nxt or self.queue[:len(nxt)] == nxt:
            return
        for n in reversed(nxt):
            self._requeue_front(n)
        self.log(f"[{self.fl.tag}] warm-up after {name}: {', '.join(nxt)} next")
        self._invalidate()
        self._ensure_thread()
        self.cv.notify_all()

    def _requeue_front(self, name):
        if name in self.queue:
            self.queue.remove(name)
        self.queue.insert(0, name)

    def set_install(self, path, prebuild=True):
        """The player's ROSE install (GUI field / --rose-install / auto find). None clears it. A change cancels the running
        build; a pending on-demand pack is rebuilt from the new install. -> (ok, reason)."""
        path = os.path.abspath(path) if path else None
        if self.conv is None:
            ok, reason = False, self.fl.no_conv
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
            run_d = [c["name"] for c in self.running if c["kind"] == "demand" and not c["cancel"].is_set()]
            resume = self.demand or (run_d[0] if run_d else None)
            for c in self.running:
                c["requeue"] = False
                c["cancel"].set()
            self.install, self.install_ok, self.install_reason = path, bool(ok), reason
            self.gen += 1
            self.demand, self.queue, self.errors, self.failed, self.forced = None, [], {}, {}, set()
            self.prebuild_started = False
            self._invalidate()
            self.log(f"[{self.fl.tag}] install: {path or '-'} ({'ok' if ok else reason})")
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
            busy = {self.demand} | {c["name"] for c in self.running if not c["cancel"].is_set()}
            add = [n for n in prebuild_order(self.conv, self.packs(), self._zone()) if n not in busy and n not in self.queue and states.get(n) != "fresh"
                   and (force or self._fail_block(n, fp, now) is None)]
            if force:
                self.forced.update(add)
            self.queue.extend(add)
            if add:
                self.log(f"[{self.fl.tag}] background prebuild: " + ", ".join(add))
                self._invalidate()
                self._ensure_thread()
                self.cv.notify_all()
        return add

    def ensure_fresh(self):
        """watchdog tick (companion, every few seconds): when nothing is building or queued but a pack is stale / missing (the
        game was patched while the helper ran, or the first prebuild was dropped), queue it in the background - packs in their
        failure cool-down stay out. Idle + all fresh = one cheap disk check. -> queued names."""
        with self.cv:
            if self.stopped or not self.usable() or self.demand or self.queue or self.running:
                return []
            gen = self.gen
        return self._prebuild(gen)

    def build_all(self, force=False):
        """GUI "Build all ROSE zones now": queue every pack that is not fresh (force=True: every pack). An explicit retry:
        clears the failure cool-downs. -> queued names."""
        with self.cv:
            if not self.usable():
                return []
            self.errors, self.failed = {}, {}
            gen = self.gen
        return self._prebuild(gen, force=force)

    def cancel(self, batch_only=False):
        """Stop the running build and drop the queue (no requeue). The on-demand slot gets an error.
        batch_only (first-run Cancel): only the background batch (running non-demand builds + queue); an on-demand build the
        world waits for keeps running."""
        with self.cv:
            if batch_only:
                names = []
                for c in self.running:
                    if c["kind"] != "demand":
                        c["requeue"] = False
                        c["cancel"].set()
                self.queue, self.forced = [], set()
            else:
                names = [c["name"] for c in self.running if c["kind"] == "demand"] + ([self.demand] if self.demand else [])
                for c in self.running:
                    c["requeue"] = False
                    c["cancel"].set()
                self.demand, self.queue, self.forced = None, [], set()
        for name in dict.fromkeys(names):
            self._notify(name, {"state": "error", "size": 0, "path": None, "error": f"{self.fl.label} build of {name} cancelled"})

    def stop(self):
        with self.cv:
            self.stopped = True
            for c in self.running:
                c["requeue"] = False
                c["cancel"].set()
            self.cv.notify_all()
        self._refresh.set()

    def wait_idle(self, timeout=None):
        """tests / CLI: wait until nothing runs or is queued -> True when idle"""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cv:
            while self.running or self.demand or self.queue or self.finishing:
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
            sc = read_sidecar(self.rose_dir, n, **self._fk) or {}
            try:
                size = os.path.getsize(pack_path(self.rose_dir, n, **self._fk))
            except OSError:
                size = 0
            spec = self.conv.PACKS.get(n) if isinstance(self.conv.PACKS, dict) else None
            label = spec.get("label") if isinstance(spec, dict) else None
            packs[n] = {"state": self.pack_state(n, fp), "bytes": size, "built": sc.get("built"), "label": label or n}
        return packs

    def _refresh_loop(self):
        """"rosestate" thread: recomputes the per pack rows (disk IO) whenever state(cached=True) found them old / invalid."""
        while True:
            self._refresh.wait()
            self._refresh.clear()
            with self.cv:
                if self.stopped:
                    return
                rev = self._state_rev
            t = time.monotonic()
            try:
                packs = self._pack_rows()
            except Exception as e:                       # noqa: BLE001 - keep the old rows
                self.log(f"[{self.fl.tag}] state refresh failed: {e}")
                time.sleep(STATE_CACHE_S)
                continue
            with self.cv:
                if rev == self._state_rev:
                    self._state_cache = (t, packs)
                else:                                    # changed meanwhile: keep these rows, scan again
                    self._state_cache = (0.0, packs)
                    self._refresh.set()
            time.sleep(0.05)                             # at most ~20 scans/s even when invalidated all the time

    def _kick_refresh(self):
        """caller holds the lock: (start and) wake the refresher"""
        if self._refresher is None or not self._refresher.is_alive():
            self._refresher = threading.Thread(target=self._refresh_loop, name="rosestate", daemon=True)
            self._refresher.start()
        self._refresh.set()

    def state(self, cached=False):
        """GUI view: {install, install_ok, install_reason, conv, current: {name, kind, stage, progress, eta_s, elapsed_s} | None,
        queue: [names], packs: {name: {state: fresh|stale|missing, bytes, built, error}}}. JSON-safe; cheap (cached 2 s).
        cached=True (the HTTP request path): no disk IO at all; the per pack rows come from the cache, which the "rosestate"
        thread refreshes when they are older than STATE_CACHE_S or invalidated ({} until its first scan).
        Full detail incl. the install path: GUI only (GET /rose/state); status.json carries public_state() of it."""
        now = time.monotonic()
        with self.cv:
            at, packs = self._state_cache
            rev = self._state_rev
            if cached and (packs is None or now - at > STATE_CACHE_S) and not self.stopped:
                self._kick_refresh()
        if cached:
            packs = packs or {}
        elif packs is None or now - at > STATE_CACHE_S:
            packs = self._pack_rows()
            with self.cv:
                if rev == self._state_rev:
                    self._state_cache = (now, packs)
        with self.cv:
            packs = {n: dict(v, error=self.errors.get(n)) for n, v in packs.items()}
            cur = self.cur

            def row(c):
                return {"name": c["name"], "kind": c["kind"], "stage": c["stage"], "progress": round(c["progress"], 3),
                        "eta_s": self._eta(c, now), "elapsed_s": int(now - c["t0"])}
            return {"install": self.install, "install_ok": self.install_ok, "install_reason": self.install_reason,
                    "conv": getattr(self.conv, "VERSION", None) if self.conv else None,
                    "current": None if not cur else row(cur),
                    "running": [row(c) for c in self.running],          # LOAD PACKAGE 5: every running build (at most `parallel`)
                    "likely_zone": self._zone() if self.conv else None,
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
            self.log(f"[{self.fl.tag}] build {cur['name']} {pct}% {stage}")
        if fields:
            self._notify(cur["name"], fields)

    def _run(self):
        while True:
            with self.cv:
                while True:
                    if self.stopped:
                        return
                    pick = self._pick()
                    if pick is not None:
                        break
                    self.cv.wait()
                name, kind = pick
                if not self.usable():
                    self.cv.notify_all()
                    continue
                # claimed before the disk checks (which run outside the lock): a request meanwhile joins this build
                cur = {"name": name, "kind": kind, "stage": "starting", "progress": 0.0, "t0": time.monotonic(),
                       "hist_s": None, "cancel": threading.Event(), "requeue": False, "preempted_by": None,
                       "pushed": 0.0, "pushed_at": 0.0, "logged": -10}
                self.running.append(cur)
                install, gen = self.install, self.gen
                forced = name in self.forced
                self._invalidate()
            fp = fingerprint(self.conv, install)
            fresh = not forced and self.pack_state(name, fp) == "fresh"
            sc = read_sidecar(self.rose_dir, name, **self._fk) or {}
            with self.cv:
                if fresh and cur["kind"] == "background" and not cur["cancel"].is_set():
                    self.running.remove(cur)                 # built meanwhile (or never stale): nothing to do
                    self._invalidate()
                    self.cv.notify_all()
                    continue
                cur["hist_s"] = sc.get("seconds") if isinstance(sc.get("seconds"), (int, float)) else None
                kind = cur["kind"]
                fields = self._building(name) if kind == "demand" else None
            self.log(f"[{self.fl.tag}] build {name} started ({kind}, conv {getattr(self.conv, 'VERSION', '?')})")
            if fields:
                self._notify(name, fields)
            self._build_one(cur, install, fp, gen)

    def _child_env(self):
        env = dict(os.environ)
        v = conv_env_value(self.conv)
        if v:
            env[self.fl.env] = v
        else:
            env.pop(self.fl.env, None)
        env["PYTHONUTF8"] = "1"
        env.update(self.child_env)
        return env

    @staticmethod
    def _kill(proc):
        try:
            proc.terminate()
            proc.wait(KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(KILL_WAIT_S)
        except OSError:
            pass

    def _build_child(self, cur, install, out):
        """one pack build in a child process (module doc). -> the converter's result dict. Raises ChildCancelled
        (cur["cancel"] was set: the child was terminated) or RuntimeError (the build failed)."""
        name = cur["name"]
        try:
            proc = subprocess.Popen(child_argv(name, install, out, **self._fk), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self._child_env(), cwd=self.rose_dir, **child_popen_kwargs())
        except OSError as e:
            raise RuntimeError(f"could not start the {self.fl.label} build process: {e}")
        cur["pid"] = proc.pid
        got = {"result": None, "error": None}
        tail = collections.deque(maxlen=20)

        def reader():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    p = parse_child_line(raw)
                    if p is None:
                        txt = raw.decode("utf-8", "replace").rstrip()
                        if txt:
                            tail.append(txt)
                    elif p[0] == "progress":
                        self._progress(cur, p[2], p[1])
                    else:
                        got[p[0]] = p[1]
            except (OSError, ValueError):
                pass
        rt = threading.Thread(target=reader, name="rosebuild-out", daemon=True)
        rt.start()
        killed = False
        try:
            while proc.poll() is None:
                if cur["cancel"].wait(0.1):
                    killed = True
                    self._kill(proc)
                    break
        finally:
            if proc.poll() is None:                     # an exception in this thread: never leave the child running
                killed = True
                self._kill(proc)
            rt.join(10)
            for fh in (proc.stdin, proc.stdout):
                try:
                    fh.close()
                except (OSError, ValueError):
                    pass
        rc = proc.returncode
        if (killed or cur["cancel"].is_set()) and not (rc == 0 and got["result"] is not None):
            raise ChildCancelled(name)            # a child that already finished the pack keeps its result (0.9.40 audit)
        if got["error"] is not None:
            if got["error"].get("cancelled"):
                raise ChildCancelled(name)
            raise RuntimeError(str(got["error"].get("error") or "unknown error"))
        if rc != 0 or got["result"] is None:
            more = (": " + " | ".join(list(tail)[-3:])) if tail else ""
            raise RuntimeError(f"{self.fl.label} build process exited with code {rc}{more}"[:500])
        want = conv_version(self.conv, name)
        if got["result"].get("conv") != want:
            raise RuntimeError(f"{self.fl.label} converter version mismatch (build process {got['result'].get('conv')}, companion "
                               f"{want}): restart the companion")
        return got["result"].get("result") or {}

    def _build_one(self, cur, install, fp, gen):
        name = cur["name"]
        out = pack_path(self.rose_dir, name, **self._fk)
        result, err, cancelled = None, None, False
        try:
            os.makedirs(self.rose_dir, exist_ok=True)
            if self.isolate:
                res = self._build_child(cur, install, out)
            else:
                res = self.conv.build(name, install, out, progress=lambda s, f: self._progress(cur, s, f),
                                      cancel=cur["cancel"].is_set)
            size = self.fl.mod.validate(out)
            secs = round(time.monotonic() - cur["t0"], 1)
            write_sidecar(self.rose_dir, name, **self._fk, data={"conv": conv_version(self.conv, name), "install": fp, "bytes": size,
                                                "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                                "seconds": secs,
                                                "warnings": list((res or {}).get("warnings") or [])[:20]
                                                if isinstance(res, dict) else []})
            result = secs
        except Exception as e:                             # noqa: BLE001 - incl. rose_conv.Cancelled / BuildError
            canc_cls = getattr(self.conv, "Cancelled", None)
            if cur["cancel"].is_set() or isinstance(e, ChildCancelled) or \
                    (isinstance(canc_cls, type) and isinstance(e, canc_cls)):
                cancelled = True
            else:
                err = str(e) or type(e).__name__
        finally:
            _rm(out + ".part")
        pb_gen = None
        with self.cv:
            if cur in self.running:
                self.running.remove(cur)
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
                    self.failed[name] = {"t": time.monotonic(), "fp": fp, "conv": conv_version(self.conv, name), "err": err}
                if cur["kind"] == "demand":
                    notify = {"state": "error", "size": 0, "path": None, "error": f"{self.fl.label} build of {name} failed: {err}"}
            else:
                self.forced.discard(name)
                self.failed.pop(name, None)
                if cur["kind"] == "demand" and not self.prebuild_started and gen == self.gen:
                    pb_gen = gen
                notify = "ready" if cur["kind"] == "demand" else None
            self.cv.notify_all()
        if cancelled:
            by = cur["preempted_by"]
            self.log(f"[{self.fl.tag}] build {name} cancelled" + (f" (pre-empted by {by}, requeued)" if by and cur["requeue"] else ""))
        elif err is not None:
            self.log(f"[{self.fl.tag}] build {name} failed: {err}")
        else:
            self.log(f"[{self.fl.tag}] build {name} done in {result:.0f} s")
        try:
            if pb_gen is not None:
                self._prebuild(pb_gen)
            if notify == "ready":
                self._notify(name, self.fl.mod.prepare(self.rose_dir, name))
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


def state(cached=False):
    return _m().state(cached=cached)


def build_all(force=False):
    return _m().build_all(force=force)


def request(name):
    return _m().request(name)


def cancel(batch_only=False):
    return _m().cancel(batch_only=batch_only)


def stop():
    if MANAGER is not None:
        MANAGER.stop()
