"""GSVR companion v0 — serves GoldSrc map packs built from the player's OWN game install to a VRChat
world over http://127.0.0.1 only. Stdlib only.

usage: python3 -I companion.py [--game-root DIR [--mods a,b] | --prebuilt-dir DIR [--prebuilt-mod MOD]]
                                [--steam-root DIR] [--vrchat-log-dir DIR]
                                [--port auto|N] [--cache-dir DIR] [--converter tools/pack.py]
                                [--pack-cmd TEMPLATE] [--world-allow wrld_x,...] [--world-list URL] [--http-log FILE]

Port: `--port auto` (default) binds the first free port of PORT_CANDIDATES on 127.0.0.1; the world bakes one URL
set per candidate (unity/.../Editor/GsvrSceneBuilder.cs CompanionPorts — keep both lists equal) and accepts a
status.json only if app == "gsvr-companion". `--port N` forces one port. No free port → message + exit code 3.

Fixed URL set (VRCUrls can't be built at runtime, so the world bakes these in, per candidate port):
  GET /status.json        small JSON, poll ≤ 1 per 5 s
  GET /index.json         mods + maps + sizes (fetch once per session / after status.index_rev changes)
  GET /pack/current.bin   pack for the last `map=` request
  GET /pack/wait.bin      long poll (W3VR, BUGS L-SLOT): held until the current build is ready (<= --wait-max s), then
                          the pack + trailer [status JSON][u32 LE len]["GSVW"]; otherwise the status JSON itself.
                          status.json "wait" = the hold limit in seconds (0/missing = old companion: poll instead)
                          For war3/__glue the trailer also carries "index_json" (= /index.json as a string).
  GET /pack/next.bin      pack for the last `next=` (prefetch) request
  GET /pack/models.bin    weapon model + sound pack (GSM1) for the last `models=` request (companion/models.py)
  GET /pack/hologram.bin  mod hub map-hologram bundle (GSPV) for the last `holo=` request (companion/holo.py)
status.json "build": [{kind: map|models|hologram|war3, mod, name, pct 0-100, eta_s (-1 unknown), state}] = the converter runs in progress
(empty list when idle; companion/buildprog.py parses the converters' "@progress" stdout lines; old worlds ignore the field).
Requests from the world arrive as VRChat log lines:
  [GSVR-REQ] map=<mod>/<map> [req=<id>] [share=<16 hex>]   build+serve as current; share = the map's relay key (CS-SHARE:
                                            a cstrike map the player lacks is fetched from the relay by that key)
  [GSVR-REQ] next=<mod>/<map> [req=<id>]    prefetch into next slot
  [GSVR-REQ] ping=<nonce>                   echoed in status.json "ping" (liveness + log latency check)
  [GSVR-REQ] rescan                         re-run ownership + map index
  [GSVR-REQ] models=<mod> [req=<id>]        build/serve the weapon model+sound pack (status.json "models")
  [GSVR-REQ] holo=<mod> maps=<a,b,c> [req=<id>]  build/serve the hub's hologram bundle of those maps (status.json "holo")
  [GSVR-REQ] music=<Sound\\...mp3> slot=a|b  W3VR: serve that mp3 of the war3 install as GET /music/<slot>.mp3
                                            (status.json "music": {slot: path}); the world's video player decodes it
"""
import argparse, errno, http.server, json, os, queue, re, socket, struct, sys, threading, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # -I drops the script dir from sys.path
import builder, buildprog, csshare, games as games_mod, holo, logwatch, mapshare, models, prebuilt, runtime, steam, war3   # noqa: E402
from version import VERSION                                        # noqa: E402  (single source of truth)

APP = "gsvr-companion"          # status.json "app": the world + GUI accept a status only with this value
PROTO = 1                       # status.json "proto"
# Single source of truth for the port list (mirrored in GsvrSceneBuilder.CompanionPorts, checked by selftest.py).
# Outside 27000-27100 (Steam, FlexLM/Autodesk lmgrd uses 27080) and below 49152 (Windows dynamic range).
PORT_CANDIDATES = (38427, 41213, 44391)
EXIT_NO_PORT = 3
HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
# /pack/wait.bin (BUGS L-SLOT): VRChat allows one string load per 5 s per world; a status poll that only says "ready"
# wastes one slot, so the world may ask for the pack directly and the companion holds the answer until the build is
# done. WAIT_MAX_S stays well under the client's tolerance (warcraft3 docs/load_times.md, measured in the test
# client) and under the world's own no-answer watchdog.
WAIT_MAX_S = 20
WAIT_FRESH_S = 5.0      # a map request seen this recently (or during the hold) is what the waiting world asked for
WAIT_GRACE_S = 3.0      # no fresh request: give the log tailer this long to read the world's new request line
WAIT_MAGIC = b"GSVW"


def changelevel_targets(bsp_path):
    """Map names of the trigger_changelevel entities in a GoldSrc BSP (v30 entity lump, lump 0): the levels a player
    can walk into from this map. Used to pre-convert them into the cache (map prefetch, no build wait at a change)."""
    import re
    try:
        with open(bsp_path, "rb") as fh:
            data = fh.read(64 << 20)
            if len(data) < 4 + 15 * 8:
                return []
            off, ln = csshare.bsp_lump_dir(data)[0]        # Blue Shift stores entities/planes swapped
            if off <= 0 or ln <= 0 or ln > 16 << 20:
                return []
            txt = data[off:off + ln].decode("latin-1", "replace")
    except (OSError, struct.error):
        return []
    out = []
    for blk in re.findall(r"\{([^{}]*)\}", txt):
        kv = dict(re.findall(r'"([^"]*)"\s*"([^"]*)"', blk))
        if kv.get("classname") == "trigger_changelevel" and kv.get("map"):
            m = kv["map"].strip().lower()
            if m and m not in out:
                out.append(m)
    return out


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def fetch_world_list(url, timeout=5):
    """wrld_… ids of a world list (one per line, # comments; worlds.txt of the release repo). Raises OSError/ValueError."""
    req = urllib.request.Request(url, headers={"User-Agent": "gsvr-companion/" + VERSION})
    ids = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r.read(64 * 1024).decode("utf-8", "replace").splitlines():
            w = line.split("#", 1)[0].strip()
            if w.startswith("wrld_") and 20 < len(w) < 64 and all(c.isalnum() or c in "_-" for c in w):
                ids.append(w)
    return ids


MAP_WATCH_S = 10                # the player's map folders are checked this often for new / changed maps
RESCAN_ON_MISS_S = 30           # a requested map that is not in the index triggers a rescan at most this often
WORLD_LIST_REFETCH_S = 30       # an unknown world re-reads --world-list at most this often (new official worlds)


def default_cache_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "GSVR", "cache")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/GSVR")
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "gsvr")


def _folder_sorted(maps):
    """Index order: by name; folder-annotated maps (war3) by folder path, then title, so the world meets its folders sorted."""
    if not any("folder" in i for i in maps.values()):
        return sorted(maps.items())
    low = lambda i, n: ((i.get("folder") or {"classic": "Classic", "fun": "Fun Maps"}.get(i.get("cat"), "")).lower(),  # noqa: E731
                        (i.get("title") or n).lower(), n)
    return sorted(maps.items(), key=lambda kv: low(kv[1], kv[0]))


class State:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)    # notified on every slot / job change (wait_current)
        self.req_seen = None                              # monotonic time of the last map= request (wait_current)
        self.wait_delay = getattr(args, "wait_debug_delay", 0) or 0   # measurement only: first hold >= this long
        self.jobs = {"current": None, "next": None}      # latest pending request per slot (older ones dropped)
        self.wake = threading.Event()
        self.slots = {s: {"state": "idle", "map": None, "req": None, "size": 0, "key": None, "error": None,
                          "path": None, "seconds": None, "percent": None, "phase": None} for s in ("current", "next")}
        self.seq, self.ping, self.world, self.world_ok = 0, None, None, not args.world_allow
        self.world_list_at = None                         # last --world-list fetch (monotonic; None = never)
        self.index_rev = 0
        self.port = None                                  # bound port (set by main before serving)
        self.ownership, self.index, self.games = {}, {}, []
        self.models, self.models_job, self.mconv = models.new_slot(), None, None   # /pack/models.bin (models.py)
        self.models_wake = threading.Event()
        self.holo, self.holo_job, self.hconv = holo.new_slot(), None, None       # /pack/hologram.bin (holo.py)
        self.holo_wake = threading.Event()
        self.prewarm = []                       # mods whose model pack is built in the background (models_worker)
        self.prewarm_done = False
        self.neighbors = []                     # (mod, map) levels next to the current map, pre-converted when idle
        self.w3conv = None                      # Warcraft III converter (tools/w3pack.py), set by main
        self.preview_lock = threading.Lock()    # one preview atlas build at a time
        self.last_miss_rescan = 0.0             # resolve(): rescan when a requested map is missing (throttled)
        self.last_watch = 0.0                   # maps_changed(): last folder check
        self.watch_sig = None
        self.music = {"a": (None, None), "b": (None, None)}   # W3VR S5: slot -> (path, mp3 bytes)
        # MAP-RELAY (mapshare.py): host -> guests map file transfer through a relay; off while relay_url is empty
        self.relay_url = (getattr(args, "relay_url", "") or "").strip()
        self.autodownload = getattr(args, "map_autodownload", "on")
        self.sharer = mapshare.Sharer(self.relay_url, args.cache_dir, log) if self.relay_url else None
        # CS-SHARE (csshare.py): the same relay for custom Counter-Strike maps (+ their custom files)
        self.cs_sharer = csshare.CsSharer(self.relay_url, args.cache_dir, log) if self.relay_url else None
        self.cs_keys = csshare.ShareKeys(args.cache_dir, csshare.load_extra_stock(args.cache_dir)) if self.relay_url else None
        self.share_thread = None                           # background hasher of the CS share keys (add_share_keys)
        self.ask = {}                                      # key16 -> None (pending) | True | False (the player's answer)
        self.fetch_failed = {}                             # key16 -> monotonic time of the last failed fetch
        self.rescan()

    # ---- ownership + index -------------------------------------------------------------------
    def rescan(self):
        a = self.args
        if a.prebuilt_dir:
            own, idx = prebuilt.scan(a.prebuilt_dir, a.prebuilt_mod)
            log(f"prebuilt dir: {os.path.abspath(a.prebuilt_dir)} (no game files used, ownership = packs present)")
            with self.lock:
                self.ownership, self.index = own, idx
                self.index_rev += 1
                self.seq += 1
            for mod, e in sorted(own.items()):
                if e["allowed"]:
                    log(f"  {mod:9s} ALLOWED ({e['reason']}): " + ", ".join(sorted(idx[mod]["maps"])))
            if not idx:
                log("WARNING: no <mod>__<map>.bin packs found (use --prebuilt-mod MOD for bare <map>.bin names)")
            return
        for line in steam.discovery_report(a.steam_root, a.game_root):
            log(line)
        if a.game_root:
            own = steam.ownership_manual(a.game_root, set(a.mods.split(",")) if a.mods else None)
        else:
            root = a.steam_root or steam.find_steam_root()
            if not root:
                log("WARNING: Steam not found - nothing will be served (use --steam-root or --game-root)")
            own = steam.ownership_from_steam(root)
        cs = own.get("cstrike", {})
        nonsteam = []
        if not cs.get("allowed"):
            for line in steam.non_steam_hint_lines(False):
                log(line)
            try:
                nonsteam = steam.detect_non_steam()          # cached by the call above
            except Exception:                                # noqa: BLE001
                nonsteam = []
        idx = steam.build_index(own)
        if not a.no_war3:                               # Warcraft III (not a Steam app: own install check, war3.py)
            w_own = war3.ownership(a.war3_root)
            own.update(w_own)
            fans = list(a.war3_maps or [])
            if getattr(self, "relay_url", ""):           # fallback folder for fetched maps (Maps folder not writable)
                fans.append(mapshare.download_fallback_dir(a.cache_dir))
            idx.update(war3.build_index(w_own, fans, a.cache_dir))
            self.watch_sig = war3.LAST_SCAN.get("pre_sig")   # baseline = taken BEFORE the scan: files that land while it hashes trigger one more rescan
        missing_keys = self.add_share_keys(idx)
        games = games_mod.games_section(own, a.war3_root, a.war3_maps, nonsteam)
        with self.lock:
            self.ownership, self.index, self.games = own, idx, games
            self.index_rev += 1
            self.seq += 1
        self.start_share_hasher(missing_keys)          # only now: the hasher writes into self.index
        for mod, e in own.items():
            log(f"  {mod:9s} {'ALLOWED' if e['allowed'] else 'denied '} ({e['reason']})"
                + (f" — {len(idx[mod]['maps'])} maps" if mod in idx else "")
                + (f", {idx[mod]['skipped']} skipped (unsafe names)" if idx.get(mod, {}).get("skipped") else ""))

    def add_share_keys(self, idx):
        """CS-SHARE: every non-stock cstrike map gets "share" = the 16-hex relay key (SHA-256 prefix of its .bsp) in the index, so
        the world (map browser) can hand it to every player's companion. Only while a relay is configured. The scan itself never
        hashes: keys come from the cache; the maps without one are hashed one by one in a background thread (index_rev moves
        when keys arrive), and a map that is about to be served is hashed at once (serve_share_key). Stale cache entries are pruned."""
        if not self.cs_keys:
            return []
        live, missing = set(), []
        for mod in csshare.MODS:
            for name, m in idx.get(mod, {}).get("maps", {}).items():
                ck = self.cs_keys.cache_key(m["path"])
                if ck is not None and not csshare.is_stock_map(name, self.cs_keys.extra):
                    live.add(ck[0])
                k = self.cs_keys.key(name, m["path"], compute=False)
                if k:
                    m["share"] = k
                elif ck is not None and not csshare.is_stock_map(name, self.cs_keys.extra):
                    missing.append((mod, name, m["path"]))
        self.cs_keys.prune(live)
        self.cs_keys.save()
        return missing

    def start_share_hasher(self, missing):
        if missing and not (self.share_thread and self.share_thread.is_alive()):
            self.share_thread = threading.Thread(target=self._hash_share_keys, args=(missing,), daemon=True)
            self.share_thread.start()

    def _hash_share_keys(self, missing):
        got = 0
        for mod, name, path in missing:
            k = self.cs_keys.key(name, path)
            if not k:
                continue
            with self.lock:
                m = self.index.get(mod, {}).get("maps", {}).get(name)
                if m is not None and m.get("path") == path and m.get("share") != k:
                    m["share"] = k
                    got += 1
        if got:
            with self.changed:
                self.index_rev += 1
                self.seq += 1
                self.changed.notify_all()
        self.cs_keys.save()

    def serve_share_key(self, mod, name, m):
        """a map is being served: make sure its key is hashed and in the index now (index_rev moves when it is new)"""
        if not self.cs_keys or mod not in csshare.MODS or m.get("share"):
            return
        k = self.cs_keys.key(name, m["path"])
        if k:
            with self.changed:
                m["share"] = k
                self.index_rev += 1
                self.seq += 1
                self.changed.notify_all()
            self.cs_keys.save()

    def index_json(self):
        """Index format: "v": 1 always; "fv": 2 = maps carry "folder" / "cat" (MAP-FOLDERS, docs/NOTES_glue.md of the
        WC3 repo). A world that does not know them uses "dir" as before; a companion without them sends no "fv"."""
        with self.lock:
            out = {"v": 1, "rev": self.index_rev,
                   "mods": {m: {"appid": e["appid"], "name": e["name"],
                                "maps": [dict({"name": n, "size": i["size"], "dir": i["dir"], "title": i.get("title", n)},
                                              **({"folder": i["folder"]} if i.get("folder") else {}),
                                              **({"cat": i["cat"]} if i.get("cat") else {}),
                                              **({"info": i["info"]} if i.get("info") else {}),
                                              **({"share": i["share"]} if i.get("share") else {}))
                                         for n, i in _folder_sorted(e["maps"])]}
                            for m, e in sorted(self.index.items())}}
            if any("folder" in i for e in self.index.values() for i in e["maps"].values()):
                out["fv"] = 2
            return out

    def status_json(self):
        with self.lock:
            return self._status_locked()

    def _status_locked(self):
        pub = {s: {k: v for k, v in d.items() if k != "path"} for s, d in self.slots.items()}
        st = pub["current"]
        return {"app": APP, "wait": getattr(self.args, "wait_max", 0), "proto": PROTO, "port": self.port, "v": 1, "companion": VERSION, "seq": self.seq, "time": int(time.time()),
                "state": st["state"], "map": st["map"], "req": st["req"], "size": st["size"],
                "key": st["key"], "error": st["error"], "seconds": st["seconds"], "next": pub["next"],
                "percent": st["percent"], "phase": st["phase"],
                "ask": [k for k, v in self.ask.items() if v is None][:1],
                "ping": self.ping, "world": self.world, "world_ok": self.world_ok,
                "index_rev": self.index_rev, "log": self.args.vrchat_log_dir is not None,
                "models": models.public(self.models),
                "holo": holo.public(self.holo),
                "build": buildprog.TRACKER.public(),
                "music": {s: p for s, (p, _d) in self.music.items()},
                "ownership": {m: e["allowed"] for m, e in sorted(self.ownership.items())},
                "games": self.games,
                "war3_versions": self.index.get(war3.MOD, {}).get("versions", [])}

    def _set(self, slot, **kw):
        with self.lock:
            self.slots[slot].update(kw)
            self.seq += 1
            self.changed.notify_all()

    def wait_current(self, max_s=None):
        """/pack/wait.bin: block (this request's thread only) until the current slot has an answer for the world's
        latest request, at most max_s seconds -> (slot copy, status dict) taken under one lock. Busy (queued /
        building / a job pending) -> wait; idle/ready/error with a fresh request -> at once; no fresh request -> up
        to WAIT_GRACE_S for the log tailer to read one. The world can't put its req into the URL (VRCUrls are
        baked), so it checks the trailer's req/map itself."""
        max_s = getattr(self.args, "wait_max", 0) if max_s is None else max_s
        t0 = time.monotonic()
        with self.changed:
            delay, self.wait_delay = self.wait_delay, 0
            while True:
                now = time.monotonic()
                s = self.slots["current"]
                busy = self.jobs["current"] is not None or s["state"] in ("building", "queued", "fetching")
                fresh = self.req_seen is not None and self.req_seen > t0 - WAIT_FRESH_S
                if not self.world_ok:
                    end = t0                                      # nothing will be served: answer the status now
                elif busy:
                    end = t0 + max_s
                elif fresh:
                    end = t0
                else:
                    end = t0 + min(WAIT_GRACE_S, max_s)
                end = max(end, t0 + min(delay, 600))             # --wait-debug-delay (measurement runs only)
                if now >= end:
                    return dict(s), self._status_locked()
                self.changed.wait(end - now)

    def refresh_world_list(self):
        """A world outside --world-allow: re-read --world-list (an official world published after the start must
        not need a companion restart). True when new ids were added."""
        url = self.args.world_list
        if not url or (self.world_list_at is not None and time.monotonic() - self.world_list_at < WORLD_LIST_REFETCH_S):
            return False
        self.world_list_at = time.monotonic()
        try:
            new = set(fetch_world_list(url)) - self.args.world_allow
        except (OSError, ValueError) as e:
            log(f"world list not reachable ({e})")
            return False
        if new:
            self.args.world_allow |= new
            log(f"world list: +{len(new)} official world(s) from {url}")
        return bool(new)

    # ---- commands from the log -----------------------------------------------------------------
    def handle(self, kind, val):
        if kind == "world":
            ok = not self.args.world_allow or val in self.args.world_allow
            if not ok and self.refresh_world_list():
                ok = val in self.args.world_allow
            with self.lock:
                self.world, self.world_ok = val, ok
                self.seq += 1
            log(f"world: {val} ({'allowed' if ok else 'NOT in --world-allow, requests ignored'})")
            return
        cmd = val
        log(f"request: {cmd}")
        if not self.world_ok:
            log("  ignored: current world not in --world-allow")
            return
        if "ping" in cmd:
            with self.lock:
                self.ping, self.seq = cmd["ping"][:64], self.seq + 1
        if "rescan" in cmd:
            self.rescan()
        if "music" in cmd and cmd.get("slot") in ("a", "b"):
            with self.lock:
                ent = self.index.get(war3.MOD)
            data = war3.music_bytes(ent, cmd["music"], log=log)
            with self.lock:
                self.music[cmd["slot"]] = (cmd["music"] if data else None, data)
                self.seq += 1
            log(f"  music {cmd['slot']}: {cmd['music']} ({len(data) if data else 'not found'} bytes)")
        if "models" in cmd:
            with self.lock:
                self.models_job = (cmd["models"][:64], cmd.get("req", "")[:32] or None)
                # answer at once with the new req: the world sees "building" for ITS request (not a stale req of
                # a previous visit / a prewarm build that is still running) and keeps waiting instead of re-asking
                if self.models.get("req") != self.models_job[1] and self.models.get("state") != "ready":
                    self.models.update(state="queued", req=self.models_job[1], error=None)
                    self.seq += 1
            self.models_wake.set()
        if "holo" in cmd:
            with self.lock:
                self.holo_job = (cmd["holo"][:64], cmd.get("maps", "")[:4000], cmd.get("req", "")[:32] or None)
                if self.holo.get("req") != self.holo_job[2]:        # answer at once with the new req (never a stale "ready")
                    self.holo.update(state="queued", req=self.holo_job[2], error=None, path=None, size=0, key=None)
                    self.seq += 1
            self.holo_wake.set()
        for slot, k in (("current", "map"), ("next", "next")):
            if k in cmd:
                with self.lock:
                    ver = cmd.get("ver", "")[:8]
                    share = cmd.get("share", "")
                    self.jobs[slot] = (cmd[k], cmd.get("req", "")[:32] or None,
                                       ver if ver.isalnum() else None,
                                       share if csshare.KEY16.match(share) else None)
                    # BUGS C-QUEUE: a build of another request still runs (a big Warcraft III map takes minutes on a
                    # slow PC): answer at once with the new req as "queued", else the world sees its request
                    # unanswered and reports "companion can't see VRChat"
                    if self.slots[slot]["state"] == "building" and self.slots[slot]["req"] != self.jobs[slot][1]:
                        self.slots[slot].update(state="queued", map=cmd[k][:96], req=self.jobs[slot][1], error=None,
                                                size=0, key=None, path=None, percent=None, phase=None)
                        self.seq += 1
                    if slot == "current":
                        self.req_seen = time.monotonic()
                    self.changed.notify_all()
                self.wake.set()

    # ---- worker ----------------------------------------------------------------------------------
    def resolve(self, spec):
        """'<mod>/<map>' -> (mod, map, entry) using ONLY the index (no path joins on request input)."""
        mod, _, name = spec.partition("/")
        mod, name = mod.lower(), name.lower()
        if name.endswith(".bsp"):
            name = name[:-4]
        if not (steam.SAFE_NAME.match(mod) and steam.safe_map_name(name)):
            raise RuntimeError("bad map spec")
        for attempt in (0, 1):
            with self.lock:
                if mod in self.index and name in self.index[mod]["maps"]:
                    e = self.index[mod]
                    return mod, name, e, e["maps"][name]
            # a map / pack added after the start (fan-map folder, new prebuilt pack): one rescan, at most every 30 s
            if attempt == 0 and time.time() - self.last_miss_rescan >= RESCAN_ON_MISS_S:
                self.last_miss_rescan = time.time()
                log(f"map '{mod}/{name}' not in the index: rescanning")
                self.rescan()
                continue
            break
        with self.lock:
            if mod not in self.index:
                own = self.ownership.get(mod)
                raise RuntimeError(f"mod '{mod}' not allowed ({own['reason'] if own else 'unknown mod'})")
            raise RuntimeError(f"map '{mod}/{name}' not found")

    # ---- MAP-RELAY fetch -----------------------------------------------------------------------------
    ASK_WAIT_S = 120
    FETCH_RETRY_S = 15

    def answer_ask(self, key, yes):
        with self.changed:
            if key in self.ask and self.ask[key] is None:
                self.ask[key] = bool(yes)
                self.seq += 1
                self.changed.notify_all()
                return True
        return False

    def fetch_from_relay(self, slot, spec, miss, share=None):
        """A requested 'war3/sha-<16 hex>' (or, with `share`, a 'cstrike/<name>' the player lacks) is not in the index: take it from
        the relay (progress goes to the slot's status: state 'fetching', percent, phase). True = the file(s) are stored and
        the index rescanned."""
        mod, _, name = spec.lower().partition("/")
        cs = bool(share) and mod in csshare.MODS and csshare.KEY16.match(share) and mod in self.ownership \
            and self.ownership[mod].get("allowed") and steam.safe_map_name(name)
        m = mapshare.KEY_RE.match(name)
        if not (self.sharer and self.autodownload != "off" and "not found" in str(miss)
                and ((mod == war3.MOD and m and war3.MOD in self.index) or cs)):
            return False
        key = share if cs else m.group(1)
        if time.monotonic() - self.fetch_failed.get(key, -1e9) < self.FETCH_RETRY_S:
            return False
        installs = self.ownership.get(war3.MOD, {}).get("installs", [])
        if not installs and not cs:
            return False
        cancel = lambda: self.jobs[slot] is not None                      # noqa: E731 — a newer request replaces this one
        if self.autodownload == "ask":
            self._set(slot, state="fetching", percent=0, phase="ask")
            with self.changed:
                known = key in self.ask
                if not known:
                    self.ask[key] = None
                    self.seq += 1
            if not known:
                log(f"map relay: asking the player whether to download {key}")
            t0 = time.monotonic()
            with self.changed:
                while self.ask[key] is None and time.monotonic() - t0 < self.ASK_WAIT_S and not cancel():
                    self.changed.wait(1.0)
                yes = self.ask[key]
            if cancel():
                raise RuntimeError("fetch superseded")
            if not yes:
                if yes is None:
                    with self.lock:
                        self.ask.pop(key, None)                           # nobody answered: ask again next time
                return False
        last = [None]

        def progress(pct, phase):
            if (pct, phase) != last[0]:
                last[0] = (pct, phase)
                self._set(slot, state="fetching", percent=pct, phase=phase)
        progress(0, "waiting")
        log(f"map relay: {spec} not found locally, asking the relay")
        try:
            if cs:
                csshare.fetch_cs(self.relay_url, key, name, self.ownership[mod]["game_root"], mod, self.args.cache_dir,
                                 progress, cancel, log, extra=self.cs_extra())
            else:
                mapshare.fetch_map(self.relay_url, key, installs, self.args.cache_dir, progress, cancel, log)
        except mapshare.Cancelled:
            raise RuntimeError("fetch superseded")
        except (LookupError, ValueError, OSError) as ex:
            self.fetch_failed[key] = time.monotonic()
            log(f"map relay: {key} not fetched: {ex}")
            return False
        self._set(slot, state="building", percent=None, phase=None)
        self.last_miss_rescan = time.time()
        self.rescan()
        return True

    def cs_extra(self):
        return self.cs_sharer.extra if self.cs_sharer else frozenset()

    def maps_changed(self):
        """the player's map folders changed since the last index (maintainer 2026-10-24: copied maps never showed up
        until a restart): checked every MAP_WATCH_S seconds, then one rescan (the world re-reads index.json when
        status.index_rev moves)."""
        now = time.time()
        if now - self.last_watch < MAP_WATCH_S:
            return False
        self.last_watch = now
        sig = war3.watch_signature()
        if self.watch_sig is None:
            self.watch_sig = sig
            return False
        if sig == self.watch_sig:
            return False
        self.watch_sig = sig
        return True

    def worker(self, conv, stop):
        while not stop.is_set():
            self.wake.wait(1.0)
            self.wake.clear()
            if not self.args.no_war3 and self.maps_changed():
                log("map folders changed: rescanning")
                self.rescan()
            for slot in ("current", "next"):
                with self.lock:
                    job, self.jobs[slot] = self.jobs[slot], None
                if not job:
                    continue
                spec, req, ver, share = job
                self._set(slot, state="building", map=spec, req=req, error=None, percent=None, phase=None)
                try:
                    try:
                        mod, name, e, m = self.resolve(spec)
                    except RuntimeError as miss:
                        if not self.fetch_from_relay(slot, spec, miss, share):
                            raise
                        mod, name, e, m = self.resolve(spec)
                    if mod == war3.MOD and "versions" in m:
                        # Warcraft III: the requested (or newest) version's install; the cache key carries it
                        path, root, v = war3.pick(m, ver)
                        r = (self.w3conv or conv).build(self.args.cache_dir, mod, name + "@" + v, path, root, root, log=log)
                        if self.sharer and m.get("sha256"):          # MAP-RELAY: offer the map file to the guests
                            self.sharer.maybe_share(m["sha256"], m["path"], self.ownership.get(war3.MOD, {}).get("installs", []))
                    else:
                        mod_dir = os.path.join(e["game_root"], m["dir"])
                        r = conv.build(self.args.cache_dir, mod, name, m["path"], e["game_root"], mod_dir, log=log)
                        if self.cs_sharer and mod in csshare.MODS:     # CS-SHARE: offer a custom map (+ its custom files) to the guests
                            self.serve_share_key(mod, name, m)
                            self.cs_sharer.maybe_share(m["path"], e["game_root"], mod)
                    with self.lock:
                        superseded = self.jobs[slot] is not None
                    if not superseded:              # superseded: the status stays on the queued request (C-QUEUE)
                        self._set(slot, state="ready", map=f"{mod}/{name}", size=r["size"], key=r["key"],
                                  path=r["path"], seconds=r["seconds"])
                    log(f"{slot}: ready {mod}/{name} {r['size']} bytes ({'cache hit' if r['cached'] else 'built'}"
                        f" in {r['seconds']} s){' — superseded, rebuilding' if superseded else ''}")
                    if slot == "current" and mod != war3.MOD and not superseded:
                        nb = [(mod, n) for n in changelevel_targets(m["path"]) if n != name]
                        with self.lock:
                            self.neighbors = nb
                        if nb:
                            log(f"  neighbours to pre-convert: {', '.join(n for _m, n in nb)}")
                except Exception as ex:                     # noqa: BLE001 — surface any failure in status
                    with self.lock:
                        superseded = self.jobs[slot] is not None
                    if not superseded:
                        self._set(slot, state="error", error=str(ex)[:300], size=0, key=None, path=None)
                    log(f"{slot}: ERROR {spec}: {ex}")
                if any(self.jobs.values()):
                    self.wake.set()
            # idle: pre-convert one neighbouring level into the cache (a later map= / next= request is a cache hit)
            with self.lock:
                nb = self.neighbors.pop(0) if self.neighbors and not any(self.jobs.values()) else None
            if nb:
                try:
                    mod, name, e, m = self.resolve(nb[0] + "/" + nb[1])
                    r = conv.build(self.args.cache_dir, mod, name, m["path"], e["game_root"],
                                   os.path.join(e["game_root"], m["dir"]), log=log)
                    log(f"pre-converted {mod}/{name} ({'already cached' if r['cached'] else 'built in ' + str(r['seconds']) + ' s'})")
                except Exception as ex:                     # noqa: BLE001 — best effort
                    log(f"pre-convert {nb[0]}/{nb[1]} skipped: {ex}")
                with self.lock:
                    if self.neighbors:
                        self.wake.set()

    def models_worker(self, stop):
        """Own thread for the model packs (a first build takes about a minute): map requests never wait behind it.
        After the first index scan it prewarms the pack of every installed mod that has a model profile, so the
        first visit after an update finds it in the cache; a real request always goes first."""
        while not stop.is_set():
            self.models_wake.wait(2.0)
            self.models_wake.clear()
            with self.lock:
                mjob, self.models_job = self.models_job, None
                warm = self.prewarm.pop(0) if (mjob is None and self.prewarm) else None
            if mjob and self.mconv is not None:
                models.run_job(self, self.mconv, mjob, log)
            elif warm and self.mconv is not None:
                models.prewarm(self, self.mconv, warm, log)
            with self.lock:
                more = self.models_job is not None or bool(self.prewarm)
            if more:
                self.models_wake.set()

    def holo_worker(self, stop):
        """Own thread for the hologram bundles (a first build of a big series takes some seconds to a minute): map requests
        and model packs never wait behind it."""
        while not stop.is_set():
            self.holo_wake.wait(2.0)
            self.holo_wake.clear()
            with self.lock:
                job, self.holo_job = self.holo_job, None
            if job and self.hconv is not None:
                holo.run_job(self, self.hconv, job, log)
            with self.lock:
                more = self.holo_job is not None
            if more:
                self.holo_wake.set()

    def log_loop(self, stop):
        d = self.args.vrchat_log_dir
        if not d:
            log("WARNING: no VRChat log dir (use --vrchat-log-dir); map requests from the world disabled")
            return
        log(f"watching VRChat logs in {d}")
        for line in logwatch.LogTailer(d).lines(stop):
            if line.startswith("__ROTATED__"):
                log("log rotated → " + line.split(" ", 1)[1])
                with self.lock:                             # new VRChat session: forget world
                    self.world, self.world_ok = None, not self.args.world_allow
                continue
            p = logwatch.parse_line(line)
            if not p:
                continue
            self.handle(*p)


def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "GSVR"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _route(self):
            ua = self.headers.get("User-Agent", "-")
            host = (self.headers.get("Host") or "").strip().lower()
            hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host
            path = self.path.split("?", 1)[0]
            client = self.client_address[0]
            # status polls (the GUI every 2 s, the world's wait loop) are logged once per client/agent, not per request
            if path not in QUIET_PATHS or _first_poll(client, ua, path):
                log(f"HTTP {self.command} {self.path} from {client} Host={host or '-'} UA={ua}"
                    + (" (further polls of this path are not logged)" if path in QUIET_PATHS else ""))
            if state.args.http_log:
                with open(state.args.http_log, "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{client}\t{self.command}\t{self.path}\t{host}\t{ua}\n")
            if client not in ("127.0.0.1", "::1"):
                return self._send(403, "forbidden\n")
            if hostname not in ALLOWED_HOSTS:                 # DNS-rebinding defence
                return self._send(421, "misdirected request\n")
            if path not in ("/status.json", "/pack/wait.bin") and not state.world_ok:  # --world-allow: only serve data inside allowed worlds
                return self._send(403, "world not allowed\n")
            if path == "/status.json":
                return self._send(200, json.dumps(state.status_json(), separators=(",", ":")), "application/json")
            if path == "/index.json":
                return self._send(200, json.dumps(state.index_json(), separators=(",", ":")), "application/json")
            if path == "/pack/wait.bin":
                return self._wait_pack()
            if path in ("/music/a.mp3", "/music/b.mp3"):     # W3VR S5: the requested mp3, unchanged
                with state.lock:
                    _p, data = state.music[path[7]]
                if not data:
                    return self._send(404, "no music in this slot\n")
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            if path in ("/pack/current.bin", "/pack/next.bin", "/pack/models.bin", "/pack/previews.bin", "/pack/hologram.bin"):
                s = None
                if path == "/pack/previews.bin":              # the WC3 lobby's map previews (built on first use,
                    with state.lock:                          # outside the lock: ~3 s for 150 maps)
                        ent = state.index.get(war3.MOD)
                    s = {"state": "missing", "path": None}
                    if ent:
                        try:
                            with state.preview_lock:
                                pv = war3.previews(ent, state.args.cache_dir, log=log)
                                s = {"state": "ready", "path": pv, "key": os.path.basename(pv)[:-4]}
                        except Exception as e:          # noqa: BLE001 — e.g. no Pillow in a source install
                            log(f"previews failed: {e}")
                            s = {"state": "failed", "path": None}
                with state.lock:
                    if s is not None:
                        pass
                    elif path == "/pack/models.bin":
                        s = dict(state.models)
                    elif path == "/pack/hologram.bin":
                        s = dict(state.holo)
                    else:
                        s = dict(state.slots["current" if path == "/pack/current.bin" else "next"])
                if s["state"] != "ready" or not s["path"]:
                    return self._send(503, f"pack not ready ({s['state']})\n")
                try:
                    fh = open(s["path"], "rb")
                except OSError:
                    return self._send(503, "pack vanished\n")
                with fh:
                    size = os.fstat(fh.fileno()).st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-GSVR-Key", s["key"] or "")
                    self.end_headers()
                    if self.command != "HEAD":
                        while True:
                            b = fh.read(1 << 20)
                            if not b:
                                break
                            self.wfile.write(b)
                return
            return self._send(404, "not found\n")

        def _wait_pack(self):
            """BUGS L-SLOT: held until the build is ready -> pack + trailer, else the status JSON (200 both)."""
            t0 = time.monotonic()
            s, st = state.wait_current()
            held = time.monotonic() - t0
            meta = json.dumps(st, separators=(",", ":")).encode()
            fh = None
            if st["world_ok"] and s["state"] == "ready" and s["path"]:
                try:
                    fh = open(s["path"], "rb")
                except OSError:
                    fh = None
            if fh is None:
                log(f"wait.bin: status after {held:.1f} s (state={s['state']} req={s['req']})")
                return self._send(200, meta, "application/json")
            if s["map"] == war3.MOD + "/__glue":              # W3VR menu: the map list rides along (one slot less)
                st = dict(st, index_json=json.dumps(state.index_json(), separators=(",", ":")))
                meta = json.dumps(st, separators=(",", ":")).encode()
            trailer = meta + struct.pack("<I", len(meta)) + WAIT_MAGIC
            with fh:
                size = os.fstat(fh.fileno()).st_size
                log(f"wait.bin: pack {s['map']} req={s['req']} {size} bytes after {held:.1f} s")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size + len(trailer)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-GSVR-Key", s["key"] or "")
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        b = fh.read(1 << 20)
                        if not b:
                            break
                        self.wfile.write(b)
                    self.wfile.write(trailer)

        do_GET = do_HEAD = _route

        def _reject(self):
            self._send(405, "method not allowed\n")

        def do_POST(self):
            """POST /ask?sha=<16 hex>&answer=yes|no — the GUI's answer to the 'ask' prompt (map_autodownload=ask)."""
            host = (self.headers.get("Host") or "").strip().lower()
            hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host
            q = urllib.parse.urlsplit(self.path)
            if (self.client_address[0] not in ("127.0.0.1", "::1") or hostname not in ALLOWED_HOSTS
                    or q.path != "/ask" or self.headers.get("X-GSVR-Ask") != "1"):
                return self._reject()
            qs = urllib.parse.parse_qs(q.query)
            key, ans = (qs.get("sha") or [""])[0], (qs.get("answer") or [""])[0]
            if not re.match(r"^[0-9a-f]{16}$", key) or ans not in ("yes", "no"):
                return self._send(400, "bad request\n")
            return self._send(200 if state.answer_ask(key, ans == "yes") else 404, "ok\n")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket silently share a port that another program listens on →
    # use SO_EXCLUSIVEADDRUSE there instead, so a taken port fails loudly and we move to the next candidate.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _NoHandler(http.server.BaseHTTPRequestHandler):      # placeholder until the state exists
    pass


def bind_reason(e):
    """OSError from bind() → one short human-readable reason."""
    w = getattr(e, "winerror", None)
    if w in (10013, 10048) or (sys.platform == "win32" and e.errno in (errno.EACCES, errno.EADDRINUSE)):
        return f"port in use or reserved by Windows (WinError {w or e.errno})"
    if e.errno == errno.EADDRINUSE:
        return "port in use by another program"
    if e.errno == errno.EACCES:
        return "permission denied (port reserved?)"
    return f"{type(e).__name__}: {e}"


QUIET_PATHS = {"/status.json", "/pack/wait.bin"}
_polls_seen = set()


def _first_poll(client, ua, path):
    key = (client, ua, path)
    if key in _polls_seen:
        return False
    _polls_seen.add(key)
    return True


def probe(port, timeout=0.5):
    """Something listening on 127.0.0.1:port? → None (free), "gsvr" (a GSVR companion) or "foreign"."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
    except OSError:
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status.json", headers={"Host": "127.0.0.1"})
        with urllib.request.urlopen(req, timeout=1.0) as r:
            d = json.loads(r.read(65536).decode("utf-8"))
        if isinstance(d, dict) and d.get("app") == APP:
            return "gsvr"
    except (OSError, ValueError):
        pass
    return "foreign"


def no_port_help(ports):
    p = ports[0]
    return [f"ERROR: could not open any companion port on 127.0.0.1 (tried {', '.join(map(str, ports))}).",
            "  The VRChat world only looks on these ports, so the companion can't run until one is free.",
            f"  Find the program holding a port (cmd):  netstat -ano | findstr :{p}",
            "                                     then:  tasklist /fi \"PID eq <PID from the last column>\"",
            "  Close that program, or reserve the port for the companion (cmd as Administrator):",
            f"    netsh int ipv4 add excludedportrange protocol=tcp startport={p} numberofports=1",
            "  (if that says the port is in use by Hyper-V/WSL: net stop winnat, run the line above, net start winnat)",
            "  Windows-reserved ranges:  netsh int ipv4 show excludedportrange protocol=tcp"]


def bind_server(ports):
    """Bind the first usable port → (server, port), or (None, message lines). One log line per failed port."""
    for p in ports:
        found = probe(p)
        if found == "gsvr":
            return None, [f"ERROR: another GSVR companion is already running on 127.0.0.1:{p} — "
                          "close its window (or the GUI's Stop) first; the world would talk to that one."]
        if found == "foreign":
            log(f"port {p}: in use — another program answers there (not the GSVR companion), trying the next one")
            continue
        try:
            return Server(("127.0.0.1", p), _NoHandler), p              # loopback ONLY
        except OSError as e:
            log(f"port {p}: {bind_reason(e)}, trying the next one" if p != ports[-1] else f"port {p}: {bind_reason(e)}")
    return None, no_port_help(list(ports))


def parse_ports(port_arg, cand_arg):
    cands = tuple(int(x) for x in cand_arg.split(",") if x.strip()) if cand_arg else PORT_CANDIDATES
    if port_arg in (None, "", "auto"):
        return cands, True
    return (int(port_arg),), False


def main(argv=None):
    ap = argparse.ArgumentParser(description="GSVR companion v0")
    ap.add_argument("--game-root", help="manual override: GoldSrc game dir containing valve/, cstrike/ … (skips Steam ownership check)")
    ap.add_argument("--prebuilt-dir", help="serve pre-built packs <mod>__<map>.bin from this dir (no game files, no converter)")
    ap.add_argument("--no-prewarm", action="store_true", help="don't build the model packs of installed mods in the background at start")
    ap.add_argument("--prebuilt-mod", help="with --prebuilt-dir: treat bare <map>.bin files as this mod (e.g. cstrike)")
    ap.add_argument("--mods", help="with --game-root: comma list of mods to allow (default: all present)")
    ap.add_argument("--steam-root", help="Steam install dir (default: auto-detect)")
    ap.add_argument("--vrchat-log-dir", default=logwatch.default_log_dir())
    ap.add_argument("--port", default="auto",
                    help="'auto' (default): first free of " + ",".join(map(str, PORT_CANDIDATES)) + "; or a fixed port")
    ap.add_argument("--port-candidates", help=argparse.SUPPRESS)       # tests only: replaces PORT_CANDIDATES
    ap.add_argument("--cache-dir", default=default_cache_dir())
    ap.add_argument("--converter", default=os.path.join(runtime.tools_dir(), "pack.py"))
    ap.add_argument("--pack-cmd", help="converter command template, e.g. '{python} -I {converter} --bsp {bsp} {out}'")
    ap.add_argument("--model-converter", default=os.path.join(runtime.tools_dir(), "modelpack.py"))
    ap.add_argument("--models-cmd", help="model converter template, placeholders {python} {converter} {game_root} {mod} {out}")
    ap.add_argument("--holo-converter", default=os.path.join(runtime.tools_dir(), "previewpack.py"))
    ap.add_argument("--holo-cmd", help="hologram converter template, placeholders {python} {converter} {game_root} {mod} {bsps} {out}")
    ap.add_argument("--build-timeout", type=int, default=600)
    ap.add_argument("--wait-max", type=float, default=WAIT_MAX_S,
                    help="longest hold of /pack/wait.bin in seconds (status.json 'wait'; 0 = the world polls as before)")
    ap.add_argument("--wait-debug-delay", type=float, default=0, help=argparse.SUPPRESS)  # measurement: first hold
    ap.add_argument("--world-allow", help="comma list of wrld_ ids; requests from other worlds are ignored")
    ap.add_argument("--war3-root", action="append", help="Warcraft III install folder; repeat for several versions "
                    "(added to the registry / usual folders)")
    ap.add_argument("--no-war3", action="store_true", help="do not look for Warcraft III")
    ap.add_argument("--war3-maps", action="append", help="fan-map folder (maps you got yourself, matched by SHA-256: "
                    "key sha-<16 hex>); repeatable")
    ap.add_argument("--war3-converter", default=os.path.join(runtime.tools_dir(), "w3pack.py"))
    ap.add_argument("--relay-url", default="", help="MAP-RELAY: base URL of the map relay (https://...; empty = off): "
                    "share the maps you host and fetch missing ones the host shared")
    ap.add_argument("--map-autodownload", choices=("on", "ask", "off"), default="on",
                    help="with --relay-url: fetch a missing map from the relay automatically (on), after asking in the "
                    "GUI (ask) or never (off)")
    ap.add_argument("--world-list", help="URL of the official world list; re-read when a world outside --world-allow joins")
    ap.add_argument("--http-log", help="append every HTTP request (TSV, incl. User-Agent) to this file")
    a = ap.parse_args(argv)
    a.world_allow = set(a.world_allow.split(",")) if a.world_allow else None
    a.cache_dir = os.path.abspath(a.cache_dir)
    if a.prebuilt_dir and a.game_root:
        ap.error("--prebuilt-dir and --game-root are mutually exclusive")
    if a.prebuilt_mod and not a.prebuilt_dir:
        ap.error("--prebuilt-mod needs --prebuilt-dir")
    if a.prebuilt_mod and not steam.SAFE_NAME.match(a.prebuilt_mod):
        ap.error("bad --prebuilt-mod")
    if a.prebuilt_mod:
        a.prebuilt_mod = a.prebuilt_mod.lower()
    try:
        ports, auto = parse_ports(a.port, a.port_candidates)
    except ValueError:
        ap.error("--port must be 'auto' or a number")
    if any(not 0 < p < 65536 for p in ports):
        ap.error("port out of range")

    log(f"GSVR companion {VERSION} (python {sys.version.split()[0]}, {sys.platform}, {runtime.install_kind()})")
    buildprog.TRACKER.set_history_file(os.path.join(a.cache_dir, "build_times.json"))     # status.json "build": eta of the last build
    srv, port = bind_server(ports)           # bind first: a port problem shows up at once, before the Steam scan
    if srv is None:
        for line in port:
            log(line)
        return EXIT_NO_PORT
    if not auto and port not in PORT_CANDIDATES:
        log(f"WARNING: --port {port} is not one of {PORT_CANDIDATES} — the VRChat world will NOT find this companion")

    if a.prebuilt_dir:
        conv = prebuilt.Prebuilt(a.prebuilt_dir)
    else:
        conv = builder.Converter(a.converter, a.pack_cmd, timeout=a.build_timeout)
    log(f"converter: {conv.converter} version={conv.version}")
    log(f"command:   {conv.template}")
    log(f"cache:     {a.cache_dir}")
    st = State(a)
    st.port = port
    st.mconv = (models.PrebuiltModels(a.prebuilt_dir) if a.prebuilt_dir
                else models.ModelBuilder(a.model_converter, a.models_cmd, timeout=a.build_timeout))
    log(f"models:    {st.mconv.converter} version={st.mconv.version}")
    st.hconv = (holo.PrebuiltHolo(a.prebuilt_dir) if a.prebuilt_dir
                else holo.HoloBuilder(a.holo_converter, a.holo_cmd, timeout=a.build_timeout))
    log(f"hologram: {st.hconv.converter} version={st.hconv.version}")
    if not a.prebuilt_dir and war3.MOD in st.index and os.path.isfile(a.war3_converter):
        st.w3conv = builder.Converter(a.war3_converter, runtime.script_prefix() + " --map {bsp} --install {game_root} {out}",
                                      timeout=a.build_timeout)
        log(f"war3:      {st.w3conv.converter} version={st.w3conv.version}")
    if not a.prebuilt_dir:                          # packs of older converter versions are dead weight now
        builder.prune_cache(a.cache_dir, [conv.version, st.mconv.version,
                                          st.w3conv.version if st.w3conv else None], log=log,
                           spare=() if st.w3conv else (war3.MOD + "-",))
    stop = threading.Event()
    threading.Thread(target=st.worker, args=(conv, stop), daemon=True).start()
    if not a.prebuilt_dir and not getattr(a, "no_prewarm", False):
        with st.lock:
            st.prewarm = [m for m in models.PREWARM_MODS if m in st.index]
        if st.prewarm:
            log("models: prewarming " + ", ".join(st.prewarm) + " in the background")
    threading.Thread(target=st.models_worker, args=(stop,), daemon=True).start()
    threading.Thread(target=st.holo_worker, args=(stop,), daemon=True).start()
    threading.Thread(target=st.log_loop, args=(stop,), daemon=True).start()
    srv.RequestHandlerClass = make_handler(st)
    log(f"listening on http://127.0.0.1:{port}/  (status.json, index.json, pack/current.bin, pack/next.bin, pack/models.bin, pack/hologram.bin)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
