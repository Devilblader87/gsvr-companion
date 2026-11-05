"""MAP-RELAY: automatic transfer of Warcraft III map FILES from the host to the guests through a relay (relay/ in
this repo). Only the .w3x/.w3m file travels; each guest builds the pack from its own game install.

  * Sharer.maybe_share(): the host's companion uploads a served non-stock map (HEAD, then PUT if the relay lacks it)
    in a background thread. Stock Blizzard maps are never uploaded (see is_stock).
  * fetch_map(): a guest's companion that is asked for 'war3/sha-<16 hex>' it does not have polls the relay (HEAD,
    up to 60 s), downloads, verifies the SHA-256 and stores the file in <install>/Maps/Download/GSVR/<original name> [<sha8>].w3x
    (sha-<sha16>.w3x when the relay has no valid name; or <cache dir>/mapdl when that is not writable). Either way the
    map index keys it by content: 'sha-<sha16>'. Downloaded files are untrusted: size cap, format check,
    and validate_map() (MPQ header sanity + decompression-bomb limits) before the file is used.
"""
import hashlib, http.client, os, re, sys, tempfile, threading, time, urllib.parse

import runtime, war3

MAX_MAP_BYTES = 50 * 1024 * 1024        # the relay's default limit too
FETCH_TIMEOUT_S = 60
POLL_S = 2.0
NAME_MAX = 120
NAME_EXTRA = " ()[]-_.,'!&+"
KEY_RE = re.compile(r"^sha-([0-9a-f]{16})$")
STOCK_FOLDERS = ("frozenthrone", "scenario", "test", "campaigns")     # <install>/Maps/<folder>: Blizzard's own maps
# per-call decompression ceiling of the shimmed zlib / bz2, per MPQ file / whole archive (declared sizes)
MAX_CALL_BYTES = 64 << 20
MAX_FILE_BYTES = 128 << 20
MAX_TOTAL_BYTES = 512 << 20
MAX_RATIO = 2000                        # declared size / stored size of a file above 1 MiB


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------------------- map file names
def sanitize_name(raw):
    """Original map file name (maybe percent-encoded UTF-8) -> a safe base name or None. Same rules as the relay:
    basename only, no control chars / separators / '..', letters (Unicode), digits and ' ()[]-_.,'!&+', at most
    NAME_MAX chars, ends in .w3x / .w3m."""
    if not raw or not isinstance(raw, str) or len(raw) > 1100:
        return None
    s = urllib.parse.unquote(raw, errors="replace") if "%" in raw else raw
    s = re.split(r"[\\/]", s)[-1]
    s = "".join(ch for ch in s if ch.isalpha() or ch.isdigit() or ch in NAME_EXTRA)
    while ".." in s:
        s = s.replace("..", ".")
    s = s.strip(" .")
    if len(s) > NAME_MAX or s.lower()[-4:] not in (".w3x", ".w3m") or len(s) <= 4 or not s[:-4].strip(" ."):
        return None
    return s[:-4] + s[-4:].lower()


def name_from_disposition(value):
    """Content-Disposition -> sanitized file name (filename*=UTF-8'' preferred, else filename="...") or None"""
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, re.I)
    if m:
        return sanitize_name(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]*)"', value, re.I)
    return sanitize_name(m.group(1)) if m else None


def local_name(name, digest):
    """'<name without ext> [<sha8>].<ext>' for a valid name, else sha-<sha16>.w3x (= the index key of the map)"""
    name = sanitize_name(name)
    if not name:
        return "sha-%s.w3x" % digest[:16]
    return "%s [%s]%s" % (name[:-4], digest[:8], name[-4:])


# ----------------------------------------------------------------------------------------- URL + client
def parse_relay_url(url):
    """-> (https?, host, port, base path) or None. https only; plain http just for a loopback test relay."""
    if not url:
        return None
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("https", "http") or not u.hostname:
        return None
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), u.path.rstrip("/")


def _conn(parts, timeout=20):
    scheme, host, port, _b = parts
    return (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port, timeout=timeout)


def relay_head(parts, key, kind="map"):
    """-> HTTP status (200 present, 404 absent, 429 limited ...) or raises OSError. kind: 'map' (Warcraft III) or 'cs'
    (Counter-Strike bundle, csshare.py)."""
    c = _conn(parts)
    try:
        c.request("HEAD", "%s/v1/%s/%s" % (parts[3], kind, key), headers={"User-Agent": "gsvr-companion"})
        r = c.getresponse()
        r.read()
        return r.status
    finally:
        c.close()


def relay_put(parts, sha, path, name=None, kind="map"):
    size = os.path.getsize(path)
    name = sanitize_name(name or os.path.basename(path)) if kind == "map" else None
    c = _conn(parts, timeout=60)
    try:
        with open(path, "rb") as fh:
            c.request("PUT", "%s/v1/%s/%s" % (parts[3], kind, sha), body=fh,
                      headers=dict({"Content-Length": str(size), "User-Agent": "gsvr-companion",
                                    "Content-Type": "application/octet-stream"},
                                   **({"X-Map-Name": urllib.parse.quote(name, safe="()!'")} if name else {})))
            r = c.getresponse()
            r.read()
            return r.status
    finally:
        c.close()


def relay_download(parts, key, tmp, progress=None, cancel=None, meta=None, kind="map"):
    """GET into tmp (hard size cap) -> (status, sha256 hex or None). meta (dict) gets 'name' = the sanitized original
    file name from Content-Disposition when the relay sent one."""
    c = _conn(parts, timeout=30)
    try:
        c.request("GET", "%s/v1/%s/%s" % (parts[3], kind, key), headers={"User-Agent": "gsvr-companion"})
        r = c.getresponse()
        if r.status != 200:
            r.read(1024)
            return r.status, None
        total = int(r.getheader("Content-Length") or 0)
        if meta is not None:
            meta["name"] = name_from_disposition(r.getheader("Content-Disposition"))
            bs = (r.getheader("X-Bundle-Sha256") or "").lower()          # CS bundles: the relay's record of the bundle's own hash
            meta["bundle_sha"] = bs if re.match(r"^[0-9a-f]{64}$", bs) else None
        if total > MAX_MAP_BYTES:
            raise ValueError("map larger than %d MB" % (MAX_MAP_BYTES >> 20))
        h, got = hashlib.sha256(), 0      # (a CS bundle is verified by csshare.validate_bundle: its key is the .bsp's hash)
        with open(tmp, "wb") as out:
            while True:
                if cancel and cancel():
                    raise Cancelled()
                b = r.read(1 << 16)
                if not b:
                    break
                got += len(b)
                if got > MAX_MAP_BYTES:
                    raise ValueError("map larger than %d MB" % (MAX_MAP_BYTES >> 20))
                h.update(b)
                out.write(b)
                if progress and total:
                    progress(min(99, got * 100 // total))
        if total and got != total:
            raise ValueError("truncated download")
        return 200, h.hexdigest()
    finally:
        c.close()


# ----------------------------------------------------------------------------------------- untrusted data
def head_ok(head):
    return head[:4] == b"HM3W" or b"MPQ\x1a" in head[:512]


def harden_mpq():
    """Imports tools/w3mpq.py -> the module. w3mpq bounds its own zlib / bz2 output per block (_bounded,
    zlib_decompress, bz2_decompress) and checks declared sizes / ratios with MPQ(path, limits=True); the old
    process-wide _Limited shim broke on its decompressobj() calls (e2e test 2026-10-29: every guest fetch failed
    with "'_Limited' object has no attribute 'decompressobj'")."""
    t = runtime.tools_dir()
    if t not in sys.path:
        sys.path.insert(0, t)
    import w3mpq
    return w3mpq


def validate_map(path, deadline_s=60):
    """Raises ValueError unless `path` looks like a sane map file: size, HM3W / MPQ magic, MPQ tables readable,
    declared sizes and ratios within limits, and every file inflates inside the per-call limit. Per-file read
    errors (protected / damaged maps exist) are not a reason to refuse; the converter copes with those."""
    size = os.path.getsize(path)
    if size > MAX_MAP_BYTES:
        raise ValueError("map larger than %d MB" % (MAX_MAP_BYTES >> 20))
    with open(path, "rb") as fh:
        if not head_ok(fh.read(512)):
            raise ValueError("not a Warcraft III map (HM3W / MPQ header missing)")
    mpq = harden_mpq()
    try:
        a = mpq.MPQ(path, limits=True)
    except ValueError:
        raise
    except Exception as e:                                   # noqa: BLE001 — a hostile file may raise anything
        raise ValueError("unreadable MPQ (%s)" % str(e)[:80])
    with a:
        total = 0
        for _name, _bidx, blk in a.listing():
            _pos, csize, fsize, _fl = blk
            total += fsize
            if fsize > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("declared content too large (decompression bomb)")
            if fsize > (1 << 20) and fsize > MAX_RATIO * max(csize, 1):
                raise ValueError("implausible compression ratio (decompression bomb)")
        t0 = time.monotonic()
        for _name, bidx, blk in a.listing():
            if time.monotonic() - t0 > deadline_s:
                raise ValueError("validation took too long")
            try:
                a.read_block(bidx, _name)
            except ValueError as e:
                if "bomb" in str(e):
                    raise
            except Exception:                                # noqa: BLE001 — damaged / unsupported file: not our verdict
                pass


# ----------------------------------------------------------------------------------------- stock maps
def stock_dirs(installs):
    out = []
    for inst in installs:
        md = war3._maps_dir(inst["root"])
        for base in ([md] if md else []) + [inst["root"]]:
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for n in names:
                if n.lower() in STOCK_FOLDERS and os.path.isdir(os.path.join(base, n)):
                    out.append(os.path.join(base, n))
    return out


class StockSet:
    """Hashes of the Blizzard maps of the installs (so a stock map copied elsewhere is still never uploaded),
    plus an optional extra list (<cache dir>/war3_stock_sha.txt, one hex per line)."""
    def __init__(self, cache_dir=None):
        self.cache_dir, self.key, self.shas, self.lock = cache_dir, None, set(), threading.Lock()

    def refresh(self, installs):
        dirs = stock_dirs(installs)
        key = tuple(sorted(os.path.normcase(d) for d in dirs))
        with self.lock:
            if key == self.key:
                return self.shas
        shas = {v[0] for v in war3.fan_maps(dirs, self.cache_dir, (), None, ()).values()}
        if self.cache_dir:
            try:
                with open(os.path.join(self.cache_dir, "war3_stock_sha.txt"), encoding="utf-8") as fh:
                    shas |= {l.strip().lower() for l in fh if re.match(r"^[0-9a-fA-F]{64}$", l.strip())}
            except OSError:
                pass
        with self.lock:
            self.key, self.shas = key, shas
            return shas


def is_stock(path, installs, sha, stock_shas=()):
    """True for a Blizzard map: CASC path (not a file), below <install>/Maps/<standard folder> or <install>/<folder>,
    or one of the known stock hashes."""
    if sha in stock_shas:
        return True
    if not os.path.isfile(path):
        return True                                           # CASC-internal map
    return any(war3._under(path, d) for d in stock_dirs(installs))


# ----------------------------------------------------------------------------------------- SHARE
class Sharer:
    def __init__(self, relay_url, cache_dir, log=print):
        self.parts, self.cache_dir, self.log = parse_relay_url(relay_url), cache_dir, log
        self.done, self.busy, self.lock = {}, set(), threading.Lock()   # sha -> time of success / last failure
        self.stock = StockSet(cache_dir)

    def maybe_share(self, sha, path, installs, wait=False):
        """Fire-and-forget (the build never waits for it). Returns the thread (tests join it) or None."""
        if not self.parts or not sha or not path:
            return None
        with self.lock:
            t = self.done.get(sha)
            if sha in self.busy or (t is not None and (t == 0 or time.time() - t < 600)):
                return None
            self.busy.add(sha)
        th = threading.Thread(target=self._run, args=(sha, path, list(installs)), daemon=True)
        th.start()
        return th

    def _run(self, sha, path, installs):
        ok = None
        try:
            if is_stock(path, installs, sha, self.stock.refresh(installs)):
                self.log("map relay: %s is a stock map, not shared" % sha[:16])
                ok = 0
                return
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > MAX_MAP_BYTES or size < 16 or not head_ok(fh.read(512)):
                    self.log("map relay: %s not shared (size or format)" % sha[:16])
                    ok = 0
                    return
            st = relay_head(self.parts, sha)
            if st == 200:
                ok = time.time()
                return
            if st != 404:
                self.log("map relay: HEAD %s -> %s" % (sha[:16], st))
                return
            st = relay_put(self.parts, sha, path)
            self.log("map relay: shared %s (PUT -> %s)" % (sha[:16], st))
            ok = time.time() if st in (200, 201) else None
        except Exception as e:                                # noqa: BLE001 — relay trouble never touches the game
            self.log("map relay: share %s failed: %s" % (sha[:16], str(e)[:100]))
        finally:
            with self.lock:
                self.busy.discard(sha)
                self.done[sha] = ok if ok is not None else time.time() - 540    # failures retry after ~1 min


# ----------------------------------------------------------------------------------------- FETCH
def download_fallback_dir(cache_dir):
    return os.path.join(cache_dir, "mapdl")


def pick_dest_dir(installs, cache_dir):
    """<install>/Maps/Download/GSVR of the first install where it can be created and written, else the data dir."""
    for inst in installs:
        md = war3._maps_dir(inst["root"]) or os.path.join(inst["root"], "Maps")
        d = os.path.join(md, "Download", "GSVR")
        try:
            os.makedirs(d, exist_ok=True)
            fd, p = tempfile.mkstemp(prefix=".w", dir=d)
            os.close(fd)
            os.remove(p)
            return d
        except OSError:
            continue
    d = download_fallback_dir(cache_dir)
    os.makedirs(d, exist_ok=True)
    return d


def wait_on_relay(parts, key, progress=None, cancel=None, timeout=FETCH_TIMEOUT_S, sleep=time.sleep, clock=time.monotonic,
                  poll=POLL_S, kind="map"):
    """HEAD until the host's upload is there (200), at most `timeout` s. Raises LookupError / Cancelled."""
    t0 = clock()
    while True:
        if cancel and cancel():
            raise Cancelled()
        try:
            st = relay_head(parts, key, kind)
        except OSError as e:
            raise LookupError("relay unreachable: %s" % str(e)[:80])
        if st == 200:
            return
        if st not in (404,):
            raise LookupError("relay answered %s" % st)
        if clock() - t0 >= timeout:
            raise LookupError("map not on the relay after %d s" % timeout)
        if progress:
            progress(0, "waiting")
        sleep(poll)


def fetch_map(relay_url, key16, installs, cache_dir, progress=None, cancel=None, log=print, timeout=FETCH_TIMEOUT_S,
              full_sha=None, sleep=time.sleep, clock=time.monotonic, poll=POLL_S):
    """Waits up to `timeout` s for the host's upload, downloads, verifies, validates and stores the map.
    progress(percent, phase) with phase 'waiting' | 'download'. -> path of the stored file.
    Raises LookupError (not on the relay in time), Cancelled, ValueError (bad data)."""
    parts = parse_relay_url(relay_url)
    if not parts or not re.match(r"^[0-9a-f]{16}$", key16):
        raise LookupError("no usable relay")
    wait_on_relay(parts, full_sha or key16, progress, cancel, timeout, sleep, clock, poll)
    dest = pick_dest_dir(installs, cache_dir)
    fd, tmp = tempfile.mkstemp(prefix=".part-", dir=dest)
    os.close(fd)
    try:
        if progress:
            progress(0, "download")
        try:
            meta = {}
            st, digest = relay_download(parts, full_sha or key16, tmp, lambda p: progress and progress(p, "download"), cancel, meta)
        except OSError as e:
            raise LookupError("download failed: %s" % str(e)[:80])
        if st != 200:
            raise LookupError("relay answered %s" % st)
        if not digest.startswith(key16) or (full_sha and digest != full_sha):
            raise ValueError("downloaded map does not match its hash")
        validate_map(tmp)
        # original name + sha8 (space and brackets: never a safe name key, so the MPQ install listing skips the file and the
        # content scan indexes it as sha-<sha16>); without a valid name: sha-<sha16>.w3x, whose name key IS that key
        final = os.path.join(dest, local_name(meta.get("name"), digest))
        os.replace(tmp, final)
        tmp = None
        if progress:
            progress(100, "download")
        log("map relay: fetched %s -> %s" % (key16, final))
        return final
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
