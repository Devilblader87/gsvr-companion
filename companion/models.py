"""Weapon model + sound pack (GSM1, tools/modelpack.py) for the companion: fixed route /pack/models.bin.

Log command:  [GSVR-REQ] models=<mod> req=<id>      e.g.  models=cstrike req=3x81721
status.json:  "models": {state: idle|building|ready|error, mod, req, size, key, error, seconds}
The world fetches /pack/models.bin once status.models.state == "ready" and status.models.req == its id
(same 5 s string-load budget as the maps; the pack is built once per install and then cached).

Like the maps, the pack is converted from the player's OWN install (models/{p,v,w}_*.mdl, sound/weapons/*.wav,
sound/items/kevlar.wav of an ALLOWED mod); the world contains no Valve assets. Mods with a table in modelpack.py:
cstrike (weapons + CS player models), valve (HL weapons + items, HL multiplayer models, monsters in the GSX1 MON1 chunk),
svencoop (the valve tables, read from svencoop/ which ships its own copies of those files) and gearbox (Opposing Force: the valve
tables + the OF weapons / monsters from gearbox/, missing files fall back to valve/). The mod name is only
looked up in the ownership index, never joined into a path. The converter runs as a subprocess (-X utf8 -I, stdin closed,
timeout); its output is validated (validate_models) before it is served, and re-validated on every request in
--prebuilt-dir mode (<prebuilt-dir>/<mod>__models.gsm).

Cache key = models-<mod>-<sha1 over (name, size, mtime) of the input files>[:16]-<converter version OF THAT MOD>.
The per-mod version is `modelpack.py --version --mod <mod>` = "<GSM version>-<mod>-r<PACK_REV[mod]>" (hashed to "v-<12 hex>"),
NOT a hash over the converter sources: a companion update that only touches another mod's profile (0.9.23: Opposing Force
added) does not rebuild the packs of the other mods. The maintainer bumps PACK_REV[mod] when that mod's output changes;
tools/modelpack_rev_check.py (tools/usharp_offline/check.sh) fails when an output changed without the bump.
Migration from <= 0.9.23: those caches were keyed by the old global version (sha1 of the converter sources). LEGACY_ADOPT lists,
per mod, the per-mod version whose output is byte-identical to that old global version's output (golden
tools/modelpack_rev.json) plus the old key tails; while the mod's version is still that one, an existing old-style cache file for the
same mod + input hash is validated and renamed to the new key instead of rebuilding. After the first bump of that mod's
PACK_REV nothing is adopted any more.
"""
import hashlib, os, shlex, struct, subprocess, sys, time

import buildprog, runtime, steam

# mod -> (per-mod converter version whose output equals the old global-version output, old key tails "v-<12 hex>" of 0.9.23).
# gsm1-2.7-9032733190c4 (the 0.9.23 sources) -> sha1 -> v-6838689b2e54. Frozen after 0.9.23: never extend for later releases
# (a later global version has no byte-equal guarantee); new mods / bumps simply build once.
LEGACY_ADOPT = {m: (f"gsm1-2.7-{m}-r1", ("v-6838689b2e54",)) for m in ("cstrike", "valve", "svencoop", "gearbox")}

MAX_PACK = 100 * 1024 * 1024
HEADER = 64          # v1 header; v2 = 80 (4 more counts: players Q, player anims A, player poses H, reserved)


def validate_models(path):
    """GSM1 sanity check (same arithmetic as GsvrModelLoader.cs) → size. Raises RuntimeError with the reason."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(80)
    except OSError as e:
        raise RuntimeError(f"model pack unreadable: {e}")
    if size == 0:
        raise RuntimeError("model pack is empty")
    if head[:4] != b"GSM1":
        raise RuntimeError(f"bad magic {head[:4]!r} (expected GSM1)")
    if size > MAX_PACK:
        raise RuntimeError(f"model pack too large for VRChat ({size} > {MAX_PACK} bytes)")
    if len(head) < HEADER:
        raise RuntimeError("GSM1 header truncated")
    hdr, ver, w, m, s, e, u, i, p, n, aw, ah, mips, nb, _flags = struct.unpack_from("<15i", head, 4)
    q = a = h = 0
    if ver == 2 and len(head) >= 80:
        q, a, h, _r = struct.unpack_from("<4i", head, 64)
    if ((ver, hdr) not in ((1, 64), (2, 80)) or not 0 <= q <= 64 or not 0 <= a <= 65536 or not 0 <= h <= 1 << 20
            or not 0 < w <= 256 or not 0 < m <= 4096 or not 0 <= s <= 4096 or not 0 <= e <= 65536
            or u <= 0 or i <= 0 or i % 3 or p < u or n < 0 or not 8 <= aw <= 8192 or not 8 <= ah <= 8192
            or aw % 8 or ah % 8 or not 1 <= mips <= 4 or nb < 0 or nb % 4):
        raise RuntimeError("GSM1 header values out of range")
    atlas = sum((aw >> l) * (ah >> l) * 4 for l in range(mips))
    need = hdr + 4 * (w * 32 + m * 8 + s * 6 + e * 4 + i + u * 2 + p * 6 + n) + atlas + nb
    if ver == 2:
        need += 4 * (q * 8 + a * 8 + h * 8 + w * 4)
    if need != size:
        raise RuntimeError(f"GSM1 pack size mismatch: header needs {need} bytes, file has {size}")
    return size


def input_files(game_root, mod):
    """Files the converter reads (for the cache key), sorted relative paths → (path, size, mtime_ns)."""
    out = []
    for d in (mod, mod + "_addon", "valve"):
        md = os.path.join(game_root, d, "models")
        try:
            names = sorted(os.listdir(md))
        except OSError:
            names = []
        for fn in names:
            # every top-level model: p_/v_/w_ weapons, and for valve/svencoop the monsters (scientist.mdl + its
            # T / NN sequence-group files …) of the GSX1 MON1 chunk; stat only, so the few hundred files are cheap
            if fn.lower().endswith(".mdl"):
                out.append(os.path.join(md, fn))
        pd = os.path.join(md, "player")            # GSM1 v2: player models (player/<name>/<name>[T].mdl)
        try:
            pnames = sorted(os.listdir(pd))
        except OSError:
            pnames = []
        for pn in pnames:
            try:
                files = sorted(os.listdir(os.path.join(pd, pn)))
            except OSError:
                continue
            out += [os.path.join(pd, pn, fn) for fn in files if fn.lower().endswith(".mdl")]
        for sub in ("weapons", "items", "player", "squeek", "hornet", "agrunt", "ctf"):     # HL: snark / hornet gun sounds; ctf: OF powerup pickup sounds
            sd = os.path.join(game_root, d, "sound", sub)
            try:
                names = sorted(os.listdir(sd))
            except OSError:
                names = []
            out += [os.path.join(sd, fn) for fn in names if fn.lower().endswith(".wav")]
    res = []
    for p in out:
        try:
            st = os.stat(p)
        except OSError:
            continue
        res.append((os.path.relpath(p, game_root).replace("\\", "/"), st.st_size, st.st_mtime_ns))
    return res


class ModelBuilder:
    """Runs tools/modelpack.py as a subprocess and caches the pack (one per mod + input state + converter version)."""

    def __init__(self, converter, template=None, python=None, timeout=600, frozen=None):
        self.converter = os.path.abspath(converter)
        self.python = python or sys.executable          # frozen build: sys.executable is gsvr-cli(.exe)
        self.frozen = runtime.FROZEN if frozen is None else frozen
        self.timeout = timeout
        self.template = template or (runtime.script_prefix(self.frozen) + " --game-root {game_root} --mod {mod} {out}")
        self.version = self._version()          # global (source-hash) version: log line only
        self._mod_versions = {}

    def _version(self, mod=None):
        """-> "v-<12 hex>" of `--version [--mod <mod>]`; "s-..." (converter file + template hash) when the converter cannot be run."""
        args = ("--version",) if mod is None else ("--version", "--mod", mod)
        try:
            r = subprocess.run(runtime.script_argv(self.python, self.converter, *args, frozen=self.frozen),
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

    def mod_version(self, mod):
        """Cache version of this mod's pack (asked from the converter once per mod). An old converter that ignores --mod
        prints its global version, which is the old behaviour (everything rebuilds on any change)."""
        v = self._mod_versions.get(mod)
        if v is None:
            v = self._mod_versions[mod] = self._version(mod)
        return v

    def _adopt_legacy(self, cache_dir, mod, inhash, final, pver, log):
        """Old-style cache file (global version key of 0.9.23) of the same mod + input state -> renamed to `final` when this
        mod's output is unchanged since then. True if adopted."""
        ent = LEGACY_ADOPT.get(mod)
        if not ent or pver != "v-" + hashlib.sha1(ent[0].encode()).hexdigest()[:12]:
            return False
        for tail in ent[1]:
            old = os.path.join(cache_dir, f"models-{mod}-{inhash}-{tail}.gsm")
            if not os.path.isfile(old):
                continue
            try:
                validate_models(old)
                os.replace(old, final)
            except (RuntimeError, OSError) as e:
                log(f"models: old cache {os.path.basename(old)} not adopted ({e})")
                continue
            log(f"models: adopted {os.path.basename(old)} -> {os.path.basename(final)} (pack output unchanged)")
            return True
        return False

    def argv(self, **kw):
        out = [tok.format(python=self.python, converter=self.converter, **kw) for tok in shlex.split(self.template, posix=True)]
        # -X utf8 (as builder.Converter): converter stdout must not depend on the console code page; -I ignores PYTHONUTF8
        if out and out[0] == self.python and "-X" not in out[1:3] and not self.frozen:
            out[1:1] = ["-X", "utf8"]
        return out

    def build(self, cache_dir, mod, game_root, log=print):
        t0 = time.monotonic()
        os.makedirs(cache_dir, exist_ok=True)
        h = hashlib.sha1()
        files = input_files(game_root, mod)
        for rel, size, mtime in files:
            h.update(f"{rel}\0{size}\0{mtime}\n".encode())
        if not files:
            raise RuntimeError(f"no weapon models/sounds found for '{mod}'")
        pver = self.mod_version(mod)
        key = f"models-{mod}-{h.hexdigest()[:16]}-{pver}"
        final = os.path.join(cache_dir, key + ".gsm")
        if not os.path.isfile(final):
            self._adopt_legacy(cache_dir, mod, h.hexdigest()[:16], final, pver, log)
        if os.path.isfile(final):
            try:
                size = validate_models(final)
                return dict(path=final, size=size, key=key, cached=True, seconds=round(time.monotonic() - t0, 3))
            except RuntimeError as e:
                log(f"models: cached pack rejected ({e}), rebuilding")
                os.remove(final)
        tmp = os.path.join(cache_dir, key + ".tmp")
        argv = self.argv(game_root=game_root, mod=mod, out=tmp)
        log("models build: " + " ".join(shlex.quote(a) for a in argv))
        try:
            with buildprog.TRACKER.job("models", mod, mod) as job:
                r = buildprog.run(argv, self.timeout, on_line=job.feed, **runtime.low_priority_kwargs(),
                                  cwd=cache_dir, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"model converter timeout after {self.timeout}s")
        except OSError as e:
            raise RuntimeError(f"model converter failed to start: {e}")
        for line in (r.stdout + r.stderr).strip().splitlines()[-4:]:
            log("  modelpack: " + line)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            raise RuntimeError(f"model converter exit {r.returncode}: {tail[0][:200]}")
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError("model converter produced no output")
        try:
            size = validate_models(tmp)
        except RuntimeError as e:
            os.remove(tmp)
            raise RuntimeError(f"model pack rejected: {e}")
        os.replace(tmp, final)
        return dict(path=final, size=size, key=key, cached=False, seconds=round(time.monotonic() - t0, 3))


class PrebuiltModels:
    """--prebuilt-dir: serve <dir>/<mod>__models.gsm as-is (validated on every request)."""

    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        self.converter, self.template, self.version = self.dir, "(prebuilt <mod>__models.gsm)", "prebuilt"

    def mod_version(self, mod):
        return self.version

    def build(self, cache_dir, mod, game_root, log=print):
        t0 = time.monotonic()
        p = os.path.join(self.dir, f"{mod}__models.gsm")       # mod already matched SAFE_NAME + the index
        if not os.path.isfile(p):
            raise RuntimeError(f"no prebuilt {mod}__models.gsm")
        size = validate_models(p)
        h = hashlib.sha1()
        with open(p, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
        return dict(path=p, size=size, key=f"prebuilt-models-{mod}-{h.hexdigest()[:16]}", cached=True,
                    seconds=round(time.monotonic() - t0, 3))


def new_slot():
    return {"state": "idle", "mod": None, "req": None, "size": 0, "key": None, "error": None, "path": None,
            "seconds": None}


def public(slot):
    return {k: v for k, v in slot.items() if k != "path"}


PREWARM_MODS = ("cstrike", "valve", "svencoop", "gearbox", "bshift", "ricochet", "dmc")      # mods with a modelpack.py profile


def prewarm(state, mconv, mod, log):
    """Background cache fill (no status change): the same build a request would do, so a later request is a hit."""
    with state.lock:
        entry = state.index.get(mod)
    if entry is None:
        return
    try:
        r = mconv.build(state.args.cache_dir, mod, entry["game_root"], log=log)
        log(f"models: prewarm {mod} {'already cached' if r['cached'] else 'built in ' + str(r['seconds']) + ' s'}")
    except Exception as ex:                     # noqa: BLE001
        log(f"models: prewarm {mod} failed: {ex}")


def run_job(state, mconv, job, log):
    """Worker step for one `models=` request (called from State.worker, outside the state lock)."""
    mod, req = job
    with state.lock:
        state.models.update(state="building", mod=mod, req=req, error=None)
        state.seq += 1
    try:
        mod = (mod or "").lower()
        if not steam.SAFE_NAME.match(mod):
            raise RuntimeError("bad mod name")
        with state.lock:
            entry = state.index.get(mod)
            own = state.ownership.get(mod)
        if entry is None:
            raise RuntimeError(f"mod '{mod}' not allowed ({own['reason'] if own else 'unknown mod'})")
        r = mconv.build(state.args.cache_dir, mod, entry["game_root"], log=log)
        with state.lock:
            state.models.update(state="ready", mod=mod, size=r["size"], key=r["key"], path=r["path"], seconds=r["seconds"])
            state.seq += 1
        log(f"models: ready {mod} {r['size']} bytes ({'cache hit' if r['cached'] else 'built'} in {r['seconds']} s)")
    except Exception as ex:                     # noqa: BLE001 — surface any failure in status
        with state.lock:
            state.models.update(state="error", error=str(ex)[:300], size=0, key=None, path=None)
            state.seq += 1
        log(f"models: ERROR {mod}: {ex}")
