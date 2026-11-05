"""Release helper for the GSVR companion (stdlib only). Builds the archives the self-updater installs and
uploads them to a Gitea release. It never runs implicitly: build_windows.bat / build_linux.sh only call
`package` / `source`; `upload` is a separate manual step.

  python3 -I companion/packaging/release.py package --platform windows-x64 [--dist companion/build/dist/GSVR]
  python3 -I companion/packaging/release.py source
  python3 -I companion/packaging/release.py upload --tag v0.4.0 [--files …] [--stable] [--notes FILE] [--dry-run]

Archives (in companion/build/release/):
  gsvr-companion-<platform>.zip|.tar.gz   one top folder (GSVR/ for builds, gsvr-companion/ for source) with
                                          gsvr-manifest.txt ("# gsvr-companion <version> <platform>" + file list)
  <archive>.sha256                        "<sha256>  <archive name>" — the updater refuses archives without it
upload: token from $GITEA_TOKEN or --token-file (a file with the bare token or a GITEA_TOKEN=… line); the token is
never printed. The tag must already exist on the server and must be "v" + version.VERSION. Existing assets with
the same name are replaced. Creates the release (pre-release unless --stable) if the tag has none yet.
"""
import argparse, hashlib, json, os, shutil, sys, tarfile, tempfile, urllib.error, urllib.parse, urllib.request, uuid, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.dirname(HERE)
ROOT = os.path.dirname(COMP)
sys.path.insert(0, COMP)
import updater                                                         # noqa: E402
from version import DEFAULT_UPDATE_BASE, REPO_NAME, REPO_OWNER, VERSION  # noqa: E402

BUILD = os.path.join(COMP, "build")
OUT = os.path.join(BUILD, "release")
SKIP_DIRS = {"__pycache__", "build", ".git"}
SKIP_EXT = (".pyc", ".pyo", updater.OLD_SUFFIX)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def list_files(top):
    out = []
    for d, dirs, names in os.walk(top):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and x != updater.STAGING)
        for fn in sorted(names):
            if fn.endswith(SKIP_EXT) or fn == ".DS_Store" or fn == updater.MANIFEST:
                continue
            out.append(os.path.relpath(os.path.join(d, fn), top).replace(os.sep, "/"))
    return out


def make_archive(src_root, top_name, key, out_dir=OUT, version=VERSION):
    """Manifest + archive + .sha256 for the tree at src_root → (archive path, sha256)."""
    files = updater.write_manifest(src_root, version, key, list_files(src_root))
    os.makedirs(out_dir, exist_ok=True)
    name = updater.asset_name(key)
    path = os.path.join(out_dir, name)
    if os.path.exists(path):
        os.remove(path)
    members = files + [updater.MANIFEST]
    if name.endswith(".zip"):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for rel in members:
                zf.write(os.path.join(src_root, *rel.split("/")), f"{top_name}/{rel}")
    else:
        with tarfile.open(path, "w:gz") as tf:
            for rel in members:
                ti = tf.gettarinfo(os.path.join(src_root, *rel.split("/")), f"{top_name}/{rel}")
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                with open(os.path.join(src_root, *rel.split("/")), "rb") as fh:
                    tf.addfile(ti, fh)
    digest = sha256_file(path)
    with open(path + ".sha256", "w", encoding="ascii", newline="\n") as fh:
        fh.write(f"{digest}  {name}\n")
    print(f"{name}: {len(members)} files, {os.path.getsize(path)} bytes, sha256 {digest}")
    return path, digest


def cmd_package(a):
    dist = os.path.abspath(a.dist)
    exe = "GSVR.exe" if a.platform.startswith("windows") else "GSVR"
    cli = "gsvr-cli.exe" if a.platform.startswith("windows") else "gsvr-cli"
    for f in (exe, cli):
        if not os.path.isfile(os.path.join(dist, f)):
            sys.exit(f"{dist}/{f} missing — run the PyInstaller build first (build_windows.bat / build_linux.sh)")
    if a.platform == updater.platform_key(frozen=True):              # same platform: the build must report VERSION
        import subprocess
        v = subprocess.run([os.path.join(dist, cli), "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
        if v != VERSION:
            sys.exit(f"{cli} --version says {v!r}, version.py says {VERSION!r} — rebuild")
    make_archive(dist, "GSVR", a.platform, a.out)


def cmd_source(a):
    """Source archive (python install): companion/ + tools/ as in the v0.2/v0.3 zips, plus the manifest."""
    stage = tempfile.mkdtemp(prefix="gsvr-source-")
    for d in updater.SOURCE_DIRS:
        shutil.copytree(os.path.join(ROOT, d), os.path.join(stage, d),
                        ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc", "*.pyo", ".DS_Store", "*" + updater.OLD_SUFFIX,
                                                      updater.STAGING, ".gitignore", "vrctest_*.sh"))      # test-LAN helpers stay out
    make_archive(stage, "gsvr-companion", "source", a.out)
    shutil.rmtree(stage, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- upload
def read_token(token_file):
    t = os.environ.get("GITEA_TOKEN", "").strip()
    if t:
        return t
    if token_file:
        with open(os.path.expanduser(token_file), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip() in ("GITEA_TOKEN", "TOKEN", "token"):
                        return v.strip().strip('"\'')
                    continue
                return line
    sys.exit("no token: set GITEA_TOKEN or pass --token-file")


class Api:
    def __init__(self, base, token, owner=REPO_OWNER, repo=REPO_NAME):
        self.base, self.token, self.owner, self.repo = base.rstrip("/"), token, owner, repo

    def call(self, method, path, body=None, ctype="application/json", ok=(200, 201, 204)):
        url = f"{self.base}/api/v1/repos/{self.owner}/{self.repo}{path}"
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": "token " + self.token, "Accept": "application/json",
                                              "User-Agent": f"gsvr-release/{VERSION}",
                                              **({"Content-Type": ctype} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            msg = e.read(2000).decode("utf-8", "replace").replace(self.token, "***")
            if e.code in ok:
                return e.code, None
            return e.code, {"error": msg}

    def upload(self, rid, path):
        name = os.path.basename(path)
        boundary = "gsvr-" + uuid.uuid4().hex
        with open(path, "rb") as fh:
            payload = fh.read()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        return self.call("POST", f"/releases/{rid}/assets?name={urllib.parse.quote(name)}", body,
                         ctype=f"multipart/form-data; boundary={boundary}")


def cmd_upload(a):
    if a.tag != "v" + VERSION and not a.allow_version_mismatch:
        sys.exit(f"tag {a.tag} != v{VERSION} (version.py) — bump version.py or pass --allow-version-mismatch")
    if a.files:
        files = [os.path.abspath(f) for f in a.files]
    elif os.path.isdir(a.out):
        files = sorted(os.path.join(a.out, f) for f in os.listdir(a.out) if f.startswith("gsvr-companion-"))
    else:
        files = []
    if not files:
        sys.exit(f"nothing to upload in {a.out}")
    for f in files:                                                  # every archive needs its checksum, and it must match
        if f.endswith(".sha256"):
            continue
        if f + ".sha256" not in files:
            sys.exit(f"{os.path.basename(f)} has no .sha256 next to it — refusing (the updater would reject it)")
        want = updater.parse_sha256_file(open(f + ".sha256", encoding="ascii").read(), os.path.basename(f))
        if sha256_file(f) != want:
            sys.exit(f"{os.path.basename(f)}: checksum file does not match — rebuild")
    print(f"release {a.tag} on {a.base}/{a.owner}/{REPO_NAME} ({'stable' if a.stable else 'pre-release'}):")
    for f in files:
        print(f"  {os.path.basename(f)}  {os.path.getsize(f)} bytes")
    if a.dry_run:
        print("dry run — nothing uploaded")
        return
    api = Api(a.base, read_token(a.token_file), owner=a.owner)
    # the release repo (public, binaries only) has no source history: the release creates its tag on main
    code, rel = api.call("GET", f"/releases/tags/{urllib.parse.quote(a.tag)}")
    if code == 404:
        notes = open(a.notes, encoding="utf-8").read() if a.notes else ""
        code, rel = api.call("POST", "/releases", {"tag_name": a.tag, "target_commitish": "main", "name": f"GSVR Companion {a.tag}",
                                                   "body": notes, "draft": False, "prerelease": not a.stable})
        if code not in (200, 201):
            sys.exit(f"could not create release: HTTP {code} {rel}")
        print(f"created release {a.tag} (id {rel['id']})")
    elif code != 200:
        sys.exit(f"release lookup failed: HTTP {code} {rel}")
    existing = {x["name"]: x["id"] for x in rel.get("assets") or []}
    for f in files:
        name = os.path.basename(f)
        if name in existing:
            c, r = api.call("DELETE", f"/releases/{rel['id']}/assets/{existing[name]}")
            if c not in (200, 204):
                sys.exit(f"could not replace {name}: HTTP {c} {r}")
        c, r = api.upload(rel["id"], f)
        if c not in (200, 201):
            sys.exit(f"upload of {name} failed: HTTP {c} {r}")
        print(f"  uploaded {name}" + (" (replaced)" if name in existing else ""))
    print("done")


def main(argv=None):
    ap = argparse.ArgumentParser(description="GSVR companion release helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("package", help="archive a PyInstaller build (companion/build/dist/GSVR)")
    p.add_argument("--platform", required=True, choices=["windows-x64", "linux-x64", "windows-arm64", "linux-arm64"])
    p.add_argument("--dist", default=os.path.join(BUILD, "dist", "GSVR"))
    p.add_argument("--out", default=OUT)
    p = sub.add_parser("source", help="archive companion/ + tools/ for python installs")
    p.add_argument("--out", default=OUT)
    p = sub.add_parser("upload", help="upload archives + .sha256 to the Gitea release <tag>")
    p.add_argument("--tag", required=True)
    p.add_argument("--files", nargs="*", help="default: every gsvr-companion-* file in --out")
    p.add_argument("--out", default=OUT)
    p.add_argument("--base", required=True, help="Gitea server URL (GitHub releases: use gh release create)")
    p.add_argument("--owner", default=REPO_OWNER, help="repo owner on that server (LAN Gitea: Devilblad3r)")
    p.add_argument("--token-file", help="file with the token (or GITEA_TOKEN=…); $GITEA_TOKEN wins")
    p.add_argument("--stable", action="store_true", help="create the release as stable (default: pre-release)")
    p.add_argument("--notes", help="release notes file (only used when the release is created)")
    p.add_argument("--allow-version-mismatch", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    {"package": cmd_package, "source": cmd_source, "upload": cmd_upload}[a.cmd](a)


if __name__ == "__main__":
    main()
