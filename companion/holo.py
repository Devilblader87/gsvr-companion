"""Map hologram preview bundle (GSPV, tools/previewpack.py) for the companion: fixed route /pack/hologram.bin.

Log command:  [GSVR-REQ] holo=<mod> maps=<name,name,...> req=<id>     e.g.  holo=svencoop maps=svencoop1,svencoop2 req=h3x81721
status.json:  "holo": {state: idle|queued|building|ready|error, mod, req, size, key, error, seconds, maps}
The mod hub (GsvrHubHologram) asks once per series when a player enters a mod room; the answer is ONE small bundle with a
decimated, lightmap-free world-faces mesh of every listed map (about 0.2-0.6 MB per map, at most ~10 MB), so browsing the map
list needs no further downloads. Same 5 s string-load budget as the other routes (poll status, then GET the bundle).

Like the maps, the bundle is converted from the player's OWN install (maps/<name>.bsp + the WADs of the mod) of an ALLOWED mod;
the hub world contains no Valve / Gearbox / Sven assets. The mod name and the map names are only looked up in the ownership
index (never joined into a path from request input). Maps that are not installed are left out of the bundle (the world shows its
own placeholder for them). The converter runs as a subprocess (-X utf8 -I, stdin closed, timeout); its output is validated
(validate_hologram) before it is served, and re-validated on every request in --prebuilt-dir mode
(<prebuilt-dir>/<mod>__hologram.gspv, the bundle of the whole mod; the world looks the map up by name inside it).

Cache key = holo-<mod>-<sha1 over (map name, bsp size, bsp mtime) of the listed maps>[:16]-<converter --version hash>.
"""
import hashlib, os, shlex, struct, subprocess, sys, time

import buildprog, runtime, steam

MAX_BUNDLE = 24 * 1024 * 1024
MAX_MAPS = 128
HEADER, ENTRY = 32, 80
HARD_TRIS = 40000
BYTES_PER_VERT, BYTES_PER_TRI = 16, 12


def validate_hologram(path):
    """GSPV sanity check (same arithmetic as GsvrHubHologram.cs ParseBundle and tools/previewpack.parse_bundle, without
    reading the floats) -> size. Raises RuntimeError with the reason."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            data = fh.read(HEADER + ENTRY * 256)
    except OSError as e:
        raise RuntimeError(f"hologram bundle unreadable: {e}")
    if size == 0:
        raise RuntimeError("hologram bundle is empty")
    if data[:4] != b"GSPV":
        raise RuntimeError(f"bad magic {data[:4]!r} (expected GSPV)")
    if size > MAX_BUNDLE:
        raise RuntimeError(f"hologram bundle too large ({size} > {MAX_BUNDLE} bytes)")
    if len(data) < HEADER:
        raise RuntimeError("GSPV header truncated")
    ver, n, total, _fl, _bud, _r0, _r1 = struct.unpack_from("<7i", data, 4)
    if ver != 1 or not 1 <= n <= 256 or total != size or len(data) < HEADER + ENTRY * n:
        raise RuntimeError("GSPV header values out of range")
    expect = HEADER + ENTRY * n
    for i in range(n):
        o = HEADER + ENTRY * i
        v, t, m, off = struct.unpack_from("<4i", data, o + 32)
        if v < 3 or t < 1 or t > HARD_TRIS or v > 3 * t or not 0 <= m <= 6 or off != expect or off % 4:
            raise RuntimeError(f"GSPV directory entry {i} out of range")
        expect = off + v * BYTES_PER_VERT + t * BYTES_PER_TRI
    if expect != size:
        raise RuntimeError(f"GSPV size mismatch: directory needs {expect} bytes, file has {size}")
    return size


def parse_maps(value):
    """'a,b,c' -> ['a','b','c'] (safe names only, no duplicates, at most MAX_MAPS); [] when nothing usable."""
    out = []
    for n in (value or "").lower().split(","):
        if n.endswith(".bsp"):
            n = n[:-4]
        if n and n not in out and steam.safe_map_name(n) and len(n) <= 31:
            out.append(n)
        if len(out) >= MAX_MAPS:
            break
    return out


class HoloBuilder:
    """Runs tools/previewpack.py as a subprocess and caches the bundle (one per mod + map list + input state + converter)."""

    def __init__(self, converter, template=None, python=None, timeout=600, frozen=None):
        self.converter = os.path.abspath(converter)
        self.python = python or sys.executable
        self.frozen = runtime.FROZEN if frozen is None else frozen
        self.timeout = timeout
        self.template = template or (runtime.script_prefix(self.frozen) + " --game-root {game_root} --mod {mod} {bsps} {out}")
        self.version = self._version()

    def _version(self):
        try:
            r = subprocess.run(runtime.script_argv(self.python, self.converter, "--version", frozen=self.frozen),
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=30, stdin=subprocess.DEVNULL)
            v = (r.stdout or "").strip()
            if r.returncode == 0 and v:
                return "v-" + hashlib.sha1(v.encode()).hexdigest()[:12]
        except (OSError, subprocess.SubprocessError):
            pass
        h = hashlib.sha1()
        try:
            with open(self.converter, "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
        h.update(self.template.encode())
        return "s-" + h.hexdigest()[:12]

    def argv(self, bsps, **kw):
        out = []
        for tok in shlex.split(self.template, posix=True):
            if tok == "{bsps}":
                for name, path in bsps:
                    out += ["--bsp", f"{name}={path}"]
            else:
                out.append(tok.format(python=self.python, converter=self.converter, **kw))
        if out and out[0] == self.python and "-X" not in out[1:3] and not self.frozen:
            out[1:1] = ["-X", "utf8"]
        return out

    def build(self, cache_dir, mod, maps, game_root, log=print):
        """maps: [(name, bsp path)] (already resolved through the index). -> dict(path, size, key, cached, seconds, maps)."""
        t0 = time.monotonic()
        os.makedirs(cache_dir, exist_ok=True)
        h = hashlib.sha1()
        have = []
        for name, path in maps:
            try:
                st = os.stat(path)
            except OSError:
                continue
            have.append((name, path))
            h.update(f"{name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        if not have:
            raise RuntimeError(f"none of the {len(maps)} maps of the series is installed for '{mod}'")
        key = f"holo-{mod}-{h.hexdigest()[:16]}-{self.version}"
        final = os.path.join(cache_dir, key + ".gspv")
        if os.path.isfile(final):
            try:
                size = validate_hologram(final)
                return dict(path=final, size=size, key=key, cached=True, seconds=round(time.monotonic() - t0, 3), maps=len(have))
            except RuntimeError as e:
                log(f"hologram: cached bundle rejected ({e}), rebuilding")
                os.remove(final)
        tmp = os.path.join(cache_dir, key + ".tmp")
        argv = self.argv(have, game_root=game_root, mod=mod, out=tmp)
        log(f"hologram build: {len(have)} maps of {mod}")
        try:
            with buildprog.TRACKER.job("hologram", mod, mod) as job:
                r = buildprog.run(argv, self.timeout, on_line=job.feed, **runtime.low_priority_kwargs(),
                                  cwd=cache_dir, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"hologram converter timeout after {self.timeout}s")
        except OSError as e:
            raise RuntimeError(f"hologram converter failed to start: {e}")
        for line in (r.stdout + r.stderr).strip().splitlines()[-4:]:
            log("  previewpack: " + line)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            raise RuntimeError(f"hologram converter exit {r.returncode}: {tail[0][:200]}")
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError("hologram converter produced no output")
        try:
            size = validate_hologram(tmp)
        except RuntimeError as e:
            os.remove(tmp)
            raise RuntimeError(f"hologram bundle rejected: {e}")
        os.replace(tmp, final)
        return dict(path=final, size=size, key=key, cached=False, seconds=round(time.monotonic() - t0, 3), maps=len(have))


class PrebuiltHolo:
    """--prebuilt-dir: serve <dir>/<mod>__hologram.gspv as-is (validated on every request; the world picks the maps by name)."""

    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        self.converter, self.template, self.version = self.dir, "(prebuilt <mod>__hologram.gspv)", "prebuilt"

    def build(self, cache_dir, mod, maps, game_root, log=print):
        t0 = time.monotonic()
        p = os.path.join(self.dir, f"{mod}__hologram.gspv")        # mod already matched SAFE_NAME + the index
        if not os.path.isfile(p):
            raise RuntimeError(f"no prebuilt {mod}__hologram.gspv")
        size = validate_hologram(p)
        h = hashlib.sha1()
        with open(p, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
        return dict(path=p, size=size, key=f"prebuilt-holo-{mod}-{h.hexdigest()[:16]}", cached=True,
                    seconds=round(time.monotonic() - t0, 3), maps=0)


def new_slot():
    return {"state": "idle", "mod": None, "req": None, "size": 0, "key": None, "error": None, "path": None,
            "seconds": None, "maps": 0}


def public(slot):
    return {k: v for k, v in slot.items() if k != "path"}


def run_job(state, hconv, job, log):
    """Worker step for one `holo=` request (called from State.holo_worker, outside the state lock)."""
    mod, maps, req = job
    with state.lock:
        state.holo.update(state="building", mod=mod, req=req, error=None)
        state.seq += 1
    try:
        mod = (mod or "").lower()
        if not steam.SAFE_NAME.match(mod):
            raise RuntimeError("bad mod name")
        names = parse_maps(maps)
        if not names:
            raise RuntimeError("no usable map names in the request")
        with state.lock:
            entry = state.index.get(mod)
            own = state.ownership.get(mod)
        if entry is None:
            raise RuntimeError(f"mod '{mod}' not allowed ({own['reason'] if own else 'unknown mod'})")
        resolved = []
        for n in names:
            m = entry["maps"].get(n)
            if m is not None:
                resolved.append((n, m["path"]))
        r = hconv.build(state.args.cache_dir, mod, resolved, entry["game_root"], log=log)
        with state.lock:
            state.holo.update(state="ready", mod=mod, size=r["size"], key=r["key"], path=r["path"], seconds=r["seconds"],
                              maps=r["maps"])
            state.seq += 1
        log(f"hologram: ready {mod} {r['size']} bytes, {r['maps']} maps ({'cache hit' if r['cached'] else 'built'} in {r['seconds']} s)")
    except Exception as ex:                     # noqa: BLE001 - surface any failure in status
        with state.lock:
            state.holo.update(state="error", error=str(ex)[:300], size=0, key=None, path=None, maps=0)
            state.seq += 1
        log(f"hologram: ERROR {mod}: {ex}")
