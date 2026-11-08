"""Self-update from the release feed (stdlib only): GitHub (github.com/<owner>/<repo>, the public default) or a Gitea
server (LAN / own host).

Flow (GUI: automatic check on start + "Install" button; CLI: `gsvr-cli --update-check` prints the result):
 1. check   GET {base}/api/v1/repos/{owner}/{repo}/releases?limit=30  (+ /releases/latest)  → newest non-draft
            release (pre-releases included unless disabled — all v0.x releases are pre-releases), compare its tag
            with version.VERSION.
 2. asset   the release must carry the platform asset AND its checksum file:
              frozen Windows:  gsvr-companion-windows-x64.zip     + gsvr-companion-windows-x64.zip.sha256
              frozen Linux:    gsvr-companion-linux-x64.tar.gz    + ….tar.gz.sha256
              source install:  gsvr-companion-source.zip          + ….zip.sha256
            Missing asset or missing .sha256 → no update offered. SHA-256 mismatch → refused, nothing touched.
 3. stage   download + unpack into <install>/.gsvr-update/ (same volume → renames are atomic). Archive members are
            checked (no absolute paths, no "..", no links/devices, size limits). The archive must contain
            gsvr-manifest.txt whose header names the same version and platform.
 4. swap    for every file in the new manifest: rename the existing file to <name>.gsvr-old (Windows allows
            renaming a running .exe / loaded .dll, not overwriting it), then move the new file in. Files listed in
            the OLD manifest but not in the new one are renamed away too. Any error → everything rolled back.
            Source installs only ever touch companion/ and tools/ and refuse to run inside a git checkout.
 5. restart the GUI relaunches itself (new exe / new gui.py) and exits; the next start deletes *.gsvr-old.
User settings (%APPDATA%\\GSVR\\gui.json) and the pack cache (default %LOCALAPPDATA%\\GSVR\\cache, user-selectable) live outside the
install folder and are never touched.

Auth: requests go out anonymously first; if the server answers 401/403/404 and a read token is configured, the
request is retried with `Authorization: token …`. The token is only ever sent to the configured server origin
(also stripped on cross-origin redirects) and never logged or put into an error message.
"""
import hashlib, json, os, platform, re, shutil, stat, subprocess, sys, tarfile, time, urllib.error, urllib.parse
import urllib.request, zipfile

import runtime
from version import DEFAULT_UPDATE_BASE, REPO_NAME, REPO_OWNER, VERSION

MANIFEST = "gsvr-manifest.txt"
OLD_SUFFIX = ".gsvr-old"
STAGING = ".gsvr-update"
SOURCE_DIRS = ("companion", "tools")      # the only folders a source-install update may write to
MAX_ASSET = 512 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
MAX_FILES = 20000
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- versions
def parse_version(s):
    """'v1.2.3', '1.2', '0.4.0-rc1' → comparable tuple. A pre-release sorts below its release. ValueError if bad."""
    s = str(s).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    core, _, pre = s.partition("-")
    parts = core.split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a version: {s!r}")
    nums = tuple(int(p) for p in parts) + (0,) * (4 - len(parts))
    return nums, (0 if pre else 1), pre


def compare_versions(a, b):
    """-1 if a < b, 0 if equal, 1 if a > b."""
    va, vb = parse_version(a), parse_version(b)
    return (va > vb) - (va < vb)


# ---------------------------------------------------------------------------------------------- platform
def arch():
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m or "unknown"


def platform_key(frozen=None):
    """'windows-x64' / 'linux-x64' / 'macos-arm64' for a frozen build, 'source' for a python install."""
    frozen = runtime.FROZEN if frozen is None else frozen
    if not frozen:
        return "source"
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)
    return f"{osname}-{arch()}"


def asset_name(key):
    ext = ".zip" if key == "source" or key.startswith("windows") else ".tar.gz"
    return f"gsvr-companion-{key}{ext}"


# ---------------------------------------------------------------------------------------------- HTTP
def _origin(url):
    p = urllib.parse.urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return p.scheme.lower(), (p.hostname or "").lower(), port


class _SameOriginAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the Authorization header to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            new.remove_header("Authorization")
        return new


def _redact(text, token):
    return text.replace(token, "***") if token else text


class Client:
    def __init__(self, base=None, token=None, owner=REPO_OWNER, repo=REPO_NAME, timeout=15):
        base = (base or DEFAULT_UPDATE_BASE).strip().rstrip("/")
        if urllib.parse.urlsplit(base).scheme not in ("http", "https") or not urllib.parse.urlsplit(base).hostname:
            raise UpdateError(f"update server URL must be http(s)://host[:port], got {base!r}")
        self.base, self.token, self.owner, self.repo, self.timeout = base, (token or "").strip() or None, owner, repo, timeout
        self._auth_needed = False
        self._opener = urllib.request.build_opener(_SameOriginAuthRedirect)

    def _request(self, url, use_token):
        h = {"User-Agent": f"gsvr-companion/{VERSION}", "Accept": "*/*"}
        if "api.github.com" in url:
            h["Accept"] = "application/vnd.github+json"
            if use_token and self.token:
                h["Authorization"] = "Bearer " + self.token
                return self._opener.open(urllib.request.Request(url, headers=h), timeout=self.timeout)
        if use_token and self.token and _origin(url) == _origin(self.base):
            h["Authorization"] = "token " + self.token
        return self._opener.open(urllib.request.Request(url, headers=h), timeout=self.timeout)

    def open(self, url):
        """Anonymous first; on 401/403/404 retry with the read token (if one is configured)."""
        if not self._auth_needed or not self.token:
            try:
                return self._request(url, False)
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403, 404) or not self.token:
                    raise
                e.close()
                self._auth_needed = True
        return self._request(url, True)

    def read(self, url, max_bytes):
        try:
            with self.open(url) as r:
                data = r.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            raise UpdateError(f"HTTP {e.code} for {url}") from None
        except (OSError, ValueError) as e:
            raise UpdateError(_redact(f"cannot reach {url}: {e}", self.token)) from None
        if len(data) > max_bytes:
            raise UpdateError(f"{url}: response larger than {max_bytes} bytes")
        return data

    def download(self, url, path, max_bytes=MAX_ASSET, progress=None):
        """Stream url → path, return sha256 hex."""
        h = hashlib.sha256()
        n = 0
        try:
            with self.open(url) as r, open(path, "wb") as fh:
                total = int(r.headers.get("Content-Length") or 0)
                if total > max_bytes:
                    raise UpdateError(f"asset too large ({total} bytes)")
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    n += len(b)
                    if n > max_bytes:
                        raise UpdateError(f"asset larger than {max_bytes} bytes")
                    h.update(b)
                    fh.write(b)
                    if progress:
                        progress(n, total)
        except urllib.error.HTTPError as e:
            raise UpdateError(f"HTTP {e.code} downloading {url}") from None
        except UpdateError:
            raise
        except (OSError, ValueError) as e:
            raise UpdateError(_redact(f"download failed ({url}): {e}", self.token)) from None
        return h.hexdigest()

    def github(self):
        """github.com (public releases, no home IP involved): REST API on api.github.com, same release JSON fields."""
        return urllib.parse.urlsplit(self.base).hostname in ("github.com", "www.github.com", "api.github.com")

    def api(self, path):
        if self.github():
            url = f"https://api.github.com/repos/{self.owner}/{self.repo}{path.replace('?limit=', '?per_page=')}"
        else:
            url = f"{self.base}/api/v1/repos/{self.owner}/{self.repo}{path}"
        try:
            with self.open(url) as r:
                return json.loads(r.read(4 << 20).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            raise UpdateError(f"update server answered HTTP {e.code} for {path}") from None
        except (OSError, ValueError) as e:
            raise UpdateError(_redact(f"cannot reach update server {self.base}: {e}", self.token)) from None

    def releases(self, prereleases=True):
        """→ list of release dicts (non-draft; pre-releases only if allowed), newest version first."""
        try:
            lst = self.api("/releases?limit=30")
        except urllib.error.HTTPError:
            raise UpdateError(f"repository {self.owner}/{self.repo} not found on {self.base}"
                              + ("" if self.token else " (private repo? set a read token in Settings)")) from None
        try:
            lst = list(lst) + [self.api("/releases/latest")]
        except urllib.error.HTTPError:
            pass                               # 404 = no stable release yet (all pre-releases)
        out, seen = [], set()
        for r in lst:
            if not isinstance(r, dict) or r.get("draft") or r.get("id") in seen:
                continue
            if r.get("prerelease") and not prereleases:
                continue
            try:
                parse_version(r.get("tag_name", ""))
            except ValueError:
                continue
            seen.add(r.get("id"))
            out.append(r)
        out.sort(key=lambda r: parse_version(r["tag_name"]), reverse=True)
        return out


# ---------------------------------------------------------------------------------------------- check
def check_for_update(base=None, token=None, prereleases=True, current=VERSION, key=None, client=None):
    """→ dict(status, current, latest, version, key, asset, asset_url, sha_url, name, notes, message)
    status: "available" (newer release with asset + checksum), "current" (nothing newer), "no-asset" (newer
    release without this platform's asset or checksum — not installable), "none" (no releases at all)."""
    key = key or platform_key()
    client = client or Client(base, token)
    rels = client.releases(prereleases)
    res = dict(status="none", current=current, latest=None, version=None, key=key, asset=asset_name(key),
               asset_url=None, sha_url=None, name=None, notes="", message="no releases on the update server")
    if not rels:
        return res
    r = rels[0]
    tag = r["tag_name"]
    res.update(latest=tag, version=tag[1:] if tag[:1] in "vV" else tag, name=r.get("name") or tag,
               notes=(r.get("body") or "")[:4000])
    if compare_versions(tag, current) <= 0:
        res.update(status="current", message=f"up to date (installed {current}, latest {tag})")
        return res
    assets = {a.get("name"): a.get("browser_download_url") for a in r.get("assets") or [] if isinstance(a, dict)}
    res.update(asset_url=assets.get(res["asset"]), sha_url=assets.get(res["asset"] + ".sha256"))
    if not res["asset_url"]:
        res.update(status="no-asset", message=f"{tag} is out, but has no {res['asset']} for this platform")
    elif not res["sha_url"]:
        res.update(status="no-asset", message=f"{tag} is out, but {res['asset']}.sha256 is missing — not installing unverified files")
    else:
        res.update(status="available", message=f"update available: {current} → {tag}")
    return res


def parse_sha256_file(text, expect_name):
    """'<hex>  <name>' (sha256sum format) or just '<hex>' → hex. The name, if present, must match."""
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if not SHA_RE.match(parts[0]):
            break
        if len(parts) > 1 and parts[-1].lstrip("*") != expect_name:
            raise UpdateError(f"checksum file is for {parts[-1]!r}, not {expect_name!r}")
        return parts[0].lower()
    raise UpdateError("checksum file has no SHA-256 value")


# ---------------------------------------------------------------------------------------------- archives
def _safe_rel(name):
    """Archive member name → normalized relative path, or raise."""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or "\0" in n:
        raise UpdateError(f"unsafe path in archive: {name!r}")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UpdateError(f"unsafe path in archive: {name!r}")
    return "/".join(parts)


def extract(archive, dest):
    """Unpack zip / tar.gz into dest with member checks → folder that holds the release (single top dir stripped).
    The archive is closed on every path (a refused member must not leave it open: Windows then can't delete it)."""
    os.makedirs(dest, exist_ok=True)
    entries = []                      # (rel, mode, reader)
    total = 0
    zf = None
    try:
        if archive.endswith(".zip"):
            zf = zipfile.ZipFile(archive)
            for zi in zf.infolist():
                mode = (zi.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise UpdateError(f"symlink in archive: {zi.filename!r}")
                rel = _safe_rel(zi.filename)
                if zi.is_dir() or not rel:
                    continue
                total += zi.file_size
                entries.append((rel, mode & 0o777, lambda zi=zi: zf.open(zi)))
        elif archive.endswith((".tar.gz", ".tgz")):
            zf = tarfile.open(archive, "r:gz")
            for ti in zf.getmembers():
                rel = _safe_rel(ti.name)
                if ti.isdir() or not rel:
                    continue
                if not ti.isreg():
                    raise UpdateError(f"link or special file in archive: {ti.name!r}")
                total += ti.size
                entries.append((rel, ti.mode & 0o777, lambda ti=ti: zf.extractfile(ti)))
        else:
            raise UpdateError(f"unknown archive type: {os.path.basename(archive)}")
        if len(entries) > MAX_FILES or total > MAX_UNPACKED:
            raise UpdateError("archive too large")
        for rel, mode, reader in entries:
            out = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with reader() as src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh, 1 << 20)
            if mode and not runtime.WIN:
                os.chmod(out, (mode & 0o755) | 0o600)
    finally:
        if zf is not None:
            zf.close()
    tops = {rel.split("/", 1)[0] for rel, _m, _r in entries}
    if len(tops) == 1 and all("/" in rel for rel, _m, _r in entries):
        return os.path.join(dest, tops.pop())
    return dest


def write_manifest(root, version, key, files):
    """Writes <root>/gsvr-manifest.txt: header line + one relative path (forward slashes) per shipped file."""
    files = sorted({f.replace("\\", "/") for f in files if f.replace("\\", "/") != MANIFEST})
    with open(os.path.join(root, MANIFEST), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# gsvr-companion {version} {key}\n")
        fh.writelines(f + "\n" for f in files)
    return files


def read_manifest(root):
    """→ (version, key, [relative paths]) or None if there is no manifest."""
    p = os.path.join(root, MANIFEST)
    try:
        with open(p, encoding="utf-8") as fh:
            lines = [l.rstrip("\r\n") for l in fh]
    except OSError:
        return None
    head = lines[0].split() if lines else []
    if len(head) != 4 or head[:2] != ["#", "gsvr-companion"]:
        raise UpdateError(f"{MANIFEST}: bad header")
    files = [_safe_rel(l) for l in lines[1:] if l.strip() and not l.startswith("#")]
    return head[2], head[3], [f for f in files if f and f != MANIFEST]


def _allowed(rel, kind):
    if kind == "source":
        return rel.split("/", 1)[0] in SOURCE_DIRS
    return True


def validate_stage(stage, version, key, kind):
    m = read_manifest(stage)
    if m is None:
        raise UpdateError(f"archive has no {MANIFEST} — not a GSVR release archive")
    mver, mkey, files = m
    if version is not None and compare_versions(mver, version) != 0:
        raise UpdateError(f"archive says version {mver}, release tag says {version}")
    if mkey != key:
        raise UpdateError(f"archive is for {mkey}, this install needs {key}")
    if not files:
        raise UpdateError("archive manifest lists no files")
    for rel in files:
        if not _allowed(rel, kind):
            raise UpdateError(f"source update may only touch {'/, '.join(SOURCE_DIRS)}/ — archive has {rel}")
        if not os.path.isfile(os.path.join(stage, *rel.split("/"))):
            raise UpdateError(f"archive manifest lists {rel}, but the file is missing")
    if kind == "frozen" and not any(f in ("GSVR.exe", "GSVR") for f in files):
        raise UpdateError("archive has no GSVR executable")
    return mver, files


# ---------------------------------------------------------------------------------------------- swap
def _backup_name(dst):
    b = dst + OLD_SUFFIX
    n = 1
    while os.path.lexists(b):
        try:
            os.remove(b)                       # left over from an earlier update and no longer locked
        except OSError:
            b = f"{dst}.{n}{OLD_SUFFIX}"       # still locked (old exe running) → pick another name
            n += 1
    return b


def _retry(fn, *args, tries=12, delay=0.25):
    """Windows: a virus scanner (Defender) briefly locks freshly written / opened files (WinError 32, 5) — retry
    the rename for ~3 s before giving up (then the caller rolls back)."""
    for k in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(delay)


def apply(stage, root, kind, files, log=print, _fail_after=None):
    """Swap the staged files into root (see module doc, step 4). Rolls back completely on any error.
    _fail_after: tests only — raise after that many files were moved."""
    root = os.path.abspath(root)
    if kind == "source" and os.path.exists(os.path.join(root, ".git")):
        raise UpdateError(f"{root} is a git checkout — update it with git pull, not the self-updater")
    old = read_manifest(root)
    old_files = [f for f in (old[2] if old else []) if _allowed(f, kind)]
    done = []                                  # (dst, backup or None, moved_new_in)
    try:
        for i, rel in enumerate(files):
            if _fail_after is not None and i >= _fail_after:
                raise OSError("simulated failure (test)")
            src = os.path.join(stage, *rel.split("/"))
            dst = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            backup = None
            if os.path.lexists(dst):
                if os.path.isdir(dst) and not os.path.islink(dst):
                    raise UpdateError(f"{rel} is a folder in the install, a file in the update")
                backup = _backup_name(dst)
                _retry(os.rename, dst, backup)  # works for a running exe / loaded dll on Windows
            done.append((dst, backup, False))
            try:
                _retry(os.replace, src, dst)
            except OSError:
                shutil.copy2(src, dst)         # staging on another volume (should not happen)
            done[-1] = (dst, backup, True)
        keep = set(files)
        for rel in old_files:                  # shipped by the old version, gone in the new one
            dst = os.path.join(root, *rel.split("/"))
            if rel not in keep and os.path.isfile(dst):
                backup = _backup_name(dst)
                _retry(os.rename, dst, backup)
                done.append((dst, backup, False))
        mp = os.path.join(root, MANIFEST)
        mback = None
        if os.path.lexists(mp):
            mback = _backup_name(mp)
            _retry(os.rename, mp, mback)
        done.append((mp, mback, False))
        os.replace(os.path.join(stage, MANIFEST), mp)
        done[-1] = (mp, mback, True)
    except Exception as e:                    # noqa: BLE001 — roll back whatever happened
        for dst, backup, moved in reversed(done):
            try:
                if moved and os.path.lexists(dst):
                    os.remove(dst)
                if backup and os.path.lexists(backup):
                    os.rename(backup, dst)
            except OSError as re_:
                log(f"[update] rollback problem at {dst}: {re_}")
        if isinstance(e, UpdateError):
            raise
        raise UpdateError(f"could not swap files ({type(e).__name__}: {e}) — old version restored") from None
    log(f"[update] {len(files)} files installed into {root}")
    return [d for d, _b, _m in done]


def cleanup_old(root=None, kind=None, retries=1, delay=0.5):
    """Delete *.gsvr-old leftovers of the last update and a stale staging dir. Returns the number removed."""
    root = os.path.abspath(root or runtime.install_root())
    kind = kind or runtime.install_kind()
    shutil.rmtree(os.path.join(root, STAGING), ignore_errors=True)
    if kind == "source":                       # never walk anything but our own folders
        dirs = [(root, False)] + [(os.path.join(root, d), True) for d in SOURCE_DIRS]
    else:
        dirs = [(root, True)]
    removed = 0
    for _attempt in range(max(1, retries)):
        left = 0
        for top, recursive in dirs:
            if not os.path.isdir(top):
                continue
            walker = os.walk(top) if recursive else [(top, [], os.listdir(top))]
            for d, _dirs, names in walker:
                for fn in names:
                    p = os.path.join(d, fn)
                    if fn.endswith(OLD_SUFFIX) and os.path.isfile(p):
                        try:
                            os.remove(p)
                            removed += 1
                        except OSError:
                            left += 1
        if not left:
            break
        time.sleep(delay)                      # the old exe may still be exiting
    return removed


# ---------------------------------------------------------------------------------------------- install
def install(check, base=None, token=None, root=None, kind=None, client=None, log=print, progress=None):
    """Download + verify + stage + swap the update described by `check` (from check_for_update).
    → argv that relaunches the new version. Raises UpdateError; on error the install is unchanged."""
    if check.get("status") != "available":
        raise UpdateError(check.get("message") or "no installable update")
    root = os.path.abspath(root or runtime.install_root())
    kind = kind or runtime.install_kind()
    if kind == "source" and os.path.exists(os.path.join(root, ".git")):
        raise UpdateError(f"{root} is a git checkout — update it with git pull, not the self-updater")
    client = client or Client(base, token)
    staging = os.path.join(root, STAGING)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        want = parse_sha256_file(client.read(check["sha_url"], 4096).decode("utf-8", "replace"), check["asset"])
        archive = os.path.join(staging, check["asset"])
        log(f"[update] downloading {check['asset']} ({check['latest']})")
        got = client.download(check["asset_url"], archive, progress=progress)
        if got != want:
            raise UpdateError(f"SHA-256 mismatch for {check['asset']} (expected {want[:16]}…, got {got[:16]}…) — update refused")
        log(f"[update] SHA-256 ok ({got[:16]}…)")
        stage = extract(archive, os.path.join(staging, "new"))
        _ver, files = validate_stage(stage, check["version"], check["key"], kind)
        apply(stage, root, kind, files, log=log)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return relaunch_argv(root, kind)


def relaunch_argv(root=None, kind=None):
    root = os.path.abspath(root or runtime.install_root())
    kind = kind or runtime.install_kind()
    if kind == "frozen":
        return [os.path.join(root, runtime.GUI_EXE)]
    exe = sys.executable
    if runtime.WIN and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.isfile(w) else exe
    return [exe, "-I", os.path.join(root, "companion", "gui.py")]


def spawn_detached(argv, cwd=None):
    kw = {}
    if runtime.WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, **kw)
