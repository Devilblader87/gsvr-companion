"""Self-test for the self-updater, the release script and the frozen-build dispatch (stdlib only, headless).

usage: python3 -I companion/selftest_update.py [--game-root input/Half-Life]
Also run by companion/selftest.py (unit part) and gui.py --selftest (controller_test).

Covers: version compare, update check against a local fake Gitea (newer / older / same / pre-release / draft /
private repo + read token / missing asset / missing checksum), install into temp dirs (source + frozen layout,
stale-file removal, user files and settings kept, .gsvr-old cleanup, locked-backup fallback), refusals (bad
checksum, path traversal, wrong manifest version, git checkout, simulated mid-swap failure → rollback), the token
never leaving the configured origin (redirect) or appearing in logs, release.py packaging + upload against the
fake server, and `gsvr_main.py --run-converter` (incl. builder.Converter in frozen mode through a shim exe).
"""
import argparse, contextlib, hashlib, http.server, io, json, os, shutil, stat, subprocess, sys, tarfile
import tempfile, threading, urllib.parse, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for _st in (sys.stdout, sys.stderr):       # -I ignores PYTHONIOENCODING; Windows consoles default to cp1252
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
import runtime, updater                         # noqa: E402
from version import REPO_NAME, REPO_OWNER, VERSION    # noqa: E402

OWNER, NAME = REPO_OWNER, REPO_NAME
SECRET = "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"
FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)
    return bool(cond)


# ---------------------------------------------------------------------------------------------- fake Gitea
class FakeGitea:
    """Minimal Gitea API: releases list/latest/tags, tags, create release, asset upload/delete, downloads."""

    def __init__(self, require_token=None):
        self.releases, self.files, self.tags = [], {}, set()
        self.require_token = require_token
        self.seen = []                                           # (method, path, Authorization header)
        self.redirect_to = None
        self._next = 100
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                auth = self.headers.get("Authorization")
                fake.seen.append((self.command, self.path, auth))
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n) if n else b""
                if fake.require_token and auth != "token " + fake.require_token:
                    return self._send(404, {"message": "not found"})
                u = urllib.parse.urlsplit(self.path)
                p, q = u.path, urllib.parse.parse_qs(u.query)
                api = f"/api/v1/repos/{OWNER}/{NAME}"
                if p.startswith("/dl/") and self.command == "GET":
                    name = urllib.parse.unquote(p[4:])
                    return self._send(200, fake.files[name], "application/octet-stream") if name in fake.files else self._send(404)
                if p.startswith("/redirect/"):
                    return self._send(302, b"", extra={"Location": fake.redirect_to + p[len("/redirect"):]})
                if not p.startswith(api):
                    return self._send(404, {"message": "no route"})
                p = p[len(api):]
                rels = sorted(fake.releases, key=lambda r: r["id"], reverse=True)
                if p == "/releases" and self.command == "GET":
                    return self._send(200, rels[: int(q.get("limit", ["30"])[0])])
                if p == "/releases/latest":
                    st = [r for r in rels if not r["prerelease"] and not r["draft"]]
                    return self._send(200, st[0]) if st else self._send(404, {"message": "not found"})
                if p.startswith("/releases/tags/"):
                    r = [r for r in rels if r["tag_name"] == urllib.parse.unquote(p[len("/releases/tags/"):])]
                    return self._send(200, r[0]) if r else self._send(404, {"message": "not found"})
                if p.startswith("/tags/"):
                    t = urllib.parse.unquote(p[len("/tags/"):])
                    return self._send(200, {"name": t}) if t in fake.tags else self._send(404, {"message": "not found"})
                if p == "/releases" and self.command == "POST":
                    b = json.loads(data)
                    r = fake.add_release(b["tag_name"], prerelease=b.get("prerelease", False), assets={})
                    r["name"], r["body"] = b.get("name"), b.get("body")
                    return self._send(201, r)
                parts = p.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "releases" and parts[2] == "assets":
                    rel = next((r for r in fake.releases if str(r["id"]) == parts[1]), None)
                    if rel is None:
                        return self._send(404)
                    if self.command == "POST":
                        name = q["name"][0]
                        boundary = self.headers["Content-Type"].split("boundary=", 1)[1].encode()
                        body = data.split(b"\r\n\r\n", 1)[1]
                        body = body[: body.rindex(b"\r\n--" + boundary + b"--")]
                        fake.put_asset(rel, name, body)
                        return self._send(201, rel["assets"][-1])
                    if self.command == "DELETE" and len(parts) == 4:
                        rel["assets"] = [a for a in rel["assets"] if str(a["id"]) != parts[3]]
                        return self._send(204)
                return self._send(404, {"message": "no route"})

            do_GET = do_POST = do_DELETE = _handle

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def put_asset(self, rel, name, data):
        self._next += 1
        self.files[name + "@" + rel["tag_name"]] = data
        rel["assets"] = [a for a in rel["assets"] if a["name"] != name] + [
            {"id": self._next, "name": name, "size": len(data),
             "browser_download_url": f"{self.base}/dl/{urllib.parse.quote(name + '@' + rel['tag_name'])}"}]

    def add_release(self, tag, prerelease=True, draft=False, assets=None):
        self._next += 1
        r = {"id": self._next, "tag_name": tag, "name": "GSVR Companion " + tag, "body": "notes", "draft": draft,
             "prerelease": prerelease, "assets": []}
        self.releases.append(r)
        self.tags.add(tag)
        for name, data in (assets or {}).items():
            self.put_asset(r, name, data)
        return r

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------------------------------- helpers
def write_tree(root, files):
    for rel, data in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode())


def snapshot(root):
    out = {}
    for d, _dirs, names in os.walk(root):
        for fn in names:
            p = os.path.join(d, fn)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = hashlib.sha256(fh.read()).hexdigest()
    return out


def build_archive(tmp, key, version, files, top=None, extra_members=None, exec_files=()):
    """Release archive like packaging/release.py builds it → (bytes, sha256 file text)."""
    src = tempfile.mkdtemp(dir=tmp)
    write_tree(src, files)
    for rel in exec_files:
        os.chmod(os.path.join(src, *rel.split("/")), 0o755)
    updater.write_manifest(src, version, key, list(files))
    top = top or ("gsvr-companion" if key == "source" else "GSVR")
    name = updater.asset_name(key)
    buf = io.BytesIO()
    members = list(files) + [updater.MANIFEST]
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in members:
                zf.write(os.path.join(src, *rel.split("/")), f"{top}/{rel}")
            for arcname, data in (extra_members or {}).items():
                zf.writestr(arcname, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for rel in members:
                tf.add(os.path.join(src, *rel.split("/")), f"{top}/{rel}")
    data = buf.getvalue()
    return data, f"{hashlib.sha256(data).hexdigest()}  {name}\n"


def publish(fake, tag, key, data, sha, prerelease=True, sha_override=None, with_sha=True, with_asset=True):
    name = updater.asset_name(key)
    assets = {}
    if with_asset:
        assets[name] = data
    if with_sha:
        assets[name + ".sha256"] = (sha_override if sha_override is not None else sha).encode()
    return fake.add_release(tag, prerelease=prerelease, assets=assets)


def frozen_files(tag, exe_name="GSVR.exe"):
    cli = "gsvr-cli.exe" if exe_name.endswith(".exe") else "gsvr-cli"
    return {exe_name: "gui " + tag, cli: "cli " + tag, "_internal/python3.dll": "dll " + tag,
            "_internal/tools/pack.py": "# pack " + tag}


# ---------------------------------------------------------------------------------------------- tests
def test_versions():
    print("[update] version compare")
    pv, cv = updater.parse_version, updater.compare_versions
    check(pv("v0.4.0") == pv("0.4.0") == pv("0.4") == pv("V0.4.0.0"), "v-prefix and missing parts normalise")
    check(cv("v0.10.0", "v0.9.9") == 1 and cv("0.3.0", "v0.4.0") == -1 and cv("v1.0.0", "1.0") == 0, "numeric compare (0.10 > 0.9)")
    check(cv("v0.4.0-rc1", "v0.4.0") == -1 and cv("v0.4.0-rc2", "v0.4.0-rc1") == 1 and cv("v0.4.1-rc1", "v0.4.0") == 1,
          "pre-release sorts below its release")
    bad = 0
    for s in ("", "latest", "v1.x", "1..2", "v1.2.3.4.5"):
        try:
            pv(s)
        except ValueError:
            bad += 1
    check(bad == 5, "garbage tags rejected")
    check(updater.asset_name("windows-x64") == "gsvr-companion-windows-x64.zip"
          and updater.asset_name("linux-x64") == "gsvr-companion-linux-x64.tar.gz"
          and updater.asset_name("source") == "gsvr-companion-source.zip", "asset names per platform")
    check(updater.platform_key(frozen=False) == "source" and updater.platform_key(frozen=True).count("-") == 1, "platform keys")
    h = "a" * 64
    check(updater.parse_sha256_file(f"{h}  x.zip\n", "x.zip") == h and updater.parse_sha256_file(h.upper(), "x.zip") == h
          and updater.parse_sha256_file(f"{h} *x.zip", "x.zip") == h, "sha256 file formats")
    for txt in (f"{h}  y.zip", "nothex  x.zip", ""):
        try:
            updater.parse_sha256_file(txt, "x.zip")
            check(False, f"bad sha256 file {txt!r} rejected")
        except updater.UpdateError:
            pass


def test_check(tmp):
    print("[update] check against a fake Gitea")
    fake = FakeGitea()
    try:
        cl = updater.Client(fake.base)
        r = updater.check_for_update(client=cl, key="source")
        check(r["status"] == "none", f"no releases → none ({r['message']})")
        data, sha = build_archive(tmp, "source", "0.1.0", {"companion/x.py": "x"})
        publish(fake, "v0.1.0", "source", data, sha)
        r = updater.check_for_update(client=cl, key="source")
        check(r["status"] == "current" and r["latest"] == "v0.1.0", f"older release → current ({r['message']})")
        publish(fake, "v" + VERSION, "source", data, sha)
        r = updater.check_for_update(client=cl, key="source")
        check(r["status"] == "current" and r["latest"] == "v" + VERSION, f"same version → current ({r['message']})")
        publish(fake, "v9.0.0", "source", data, sha)
        r = updater.check_for_update(client=cl, key="source")
        check(r["status"] == "available" and r["latest"] == "v9.0.0" and r["asset_url"] and r["sha_url"],
              f"newer pre-release with asset + sha → available ({r['message']})")
        r = updater.check_for_update(client=cl, key="source", prereleases=False)
        check(r["status"] == "none", f"pre-releases excluded → none ({r['message']})")
        fake.add_release("v9.5.0", draft=True, assets={"gsvr-companion-source.zip": data})
        r = updater.check_for_update(client=cl, key="source")
        check(r["latest"] == "v9.0.0", "drafts ignored")
        r = updater.check_for_update(client=cl, key="windows-x64")
        check(r["status"] == "no-asset" and "gsvr-companion-windows-x64.zip" in r["message"],
              f"newer release without this platform's asset → no-asset ({r['message']})")
        publish(fake, "v9.1.0", "source", data, sha, with_sha=False)
        r = updater.check_for_update(client=cl, key="source")
        check(r["status"] == "no-asset" and ".sha256" in r["message"], f"asset without .sha256 → not offered ({r['message']})")
        publish(fake, "v10.0.0", "source", data, sha, prerelease=False)
        r = updater.check_for_update(client=cl, key="source", prereleases=False)
        check(r["status"] == "available" and r["latest"] == "v10.0.0", "stable release found via /releases/latest + list")
        try:
            updater.check_for_update(base="http://127.0.0.1:1", key="source")
            check(False, "unreachable server → UpdateError")
        except updater.UpdateError as e:
            check("cannot reach" in str(e), f"unreachable server → UpdateError ({e})")
    finally:
        fake.close()

    print("[update] private repo + read token")
    fake = FakeGitea(require_token=SECRET)
    other = FakeGitea()
    fake.redirect_to = other.base
    try:
        data, sha = build_archive(tmp, "source", "9.0.0", {"companion/x.py": "x"})
        publish(fake, "v9.0.0", "source", data, sha)
        try:
            updater.check_for_update(base=fake.base, key="source")
            check(False, "private repo without token → error")
        except updater.UpdateError as e:
            check("token" in str(e), f"private repo without token → asks for a token ({e})")
        cl = updater.Client(fake.base, SECRET)
        r = updater.check_for_update(client=cl, key="source")
        check(r["status"] == "available", "private repo with read token → available")
        check(fake.seen[-1][2] == "token " + SECRET and any(s[2] is None for s in fake.seen),
              "anonymous first, token on retry")
        other.files["x.bin@t"] = b"hello"
        got = cl.read(fake.base + "/redirect/dl/x.bin@t", 100)
        check(got == b"hello" and all(s[2] is None for s in other.seen), "token not sent across a redirect to another origin")
        n = len(other.seen)
        cl.read(other.base + "/dl/x.bin@t", 100)
        check(other.seen[n][2] is None, "token not sent to a URL outside the configured server")
        try:
            updater.Client(fake.base + "/nope", SECRET).read("http://127.0.0.1:1/x", 10)
        except updater.UpdateError as e:
            check(SECRET not in str(e), "token not in error messages")
    finally:
        fake.close()
        other.close()


def test_install(tmp):
    print("[update] install (source layout)")
    fake = FakeGitea()
    try:
        inst = os.path.join(tmp, "inst"); os.makedirs(inst)
        old = {"companion/a.py": "old a", "companion/stale.py": "stale", "tools/pack.py": "old pack"}
        write_tree(inst, old)
        updater.write_manifest(inst, "0.3.0", "source", list(old))
        write_tree(inst, {"companion/user_note.txt": "mine", "other/keep.txt": "not ours"})
        settings = os.path.join(tmp, "appdata", "GSVR", "gui.json")
        write_tree(os.path.dirname(settings), {"gui.json": '{"worlds": "wrld_x"}'})
        new = {"companion/a.py": "new a", "companion/b.py": "new b", "tools/pack.py": "new pack"}
        data, sha = build_archive(tmp, "source", "9.0.0", new)
        publish(fake, "v9.0.0", "source", data, sha)
        cl = updater.Client(fake.base)
        r = updater.check_for_update(client=cl, key="source")
        logs = []
        argv = updater.install(r, root=inst, kind="source", client=cl, log=logs.append)
        rd = lambda rel: open(os.path.join(inst, *rel.split("/")), encoding="utf-8").read()   # noqa: E731
        check(rd("companion/a.py") == "new a" and rd("companion/b.py") == "new b" and rd("tools/pack.py") == "new pack",
              "new files swapped in")
        check(not os.path.exists(os.path.join(inst, "companion", "stale.py")), "file dropped by the new version removed")
        check(rd("companion/user_note.txt") == "mine" and rd("other/keep.txt") == "not ours", "user files / other folders untouched")
        check(open(settings).read() == '{"worlds": "wrld_x"}', "settings untouched")
        check(updater.read_manifest(inst)[0] == "9.0.0", "manifest updated to the new version")
        check(argv[-1] == os.path.join(os.path.abspath(inst), "companion", "gui.py"), f"relaunch argv → new gui.py ({argv[-1]})")
        check(not os.path.exists(os.path.join(inst, updater.STAGING)), "staging dir removed")
        olds = [f for f in snapshot(inst) if f.endswith(updater.OLD_SUFFIX)]
        check(len(olds) == 4, f"backups left as *.gsvr-old until the next start ({len(olds)})")
        check(updater.cleanup_old(inst, "source") == 4 and not [f for f in snapshot(inst) if f.endswith(updater.OLD_SUFFIX)],
              "cleanup_old removes them")

        before = snapshot(inst)
        bad = publish(fake, "v9.1.0", "source", *build_archive(tmp, "source", "9.1.0", {"companion/a.py": "evil"}),
                      sha_override="0" * 64 + "  gsvr-companion-source.zip\n")
        r = updater.check_for_update(client=cl, key="source")
        try:
            updater.install(r, root=inst, kind="source", client=cl, log=logs.append)
            check(False, "bad checksum refused")
        except updater.UpdateError as e:
            check("mismatch" in str(e) and snapshot(inst) == before, f"bad checksum refused, install unchanged ({e})")
        fake.releases.remove(bad)

        tdata, tsha = build_archive(tmp, "source", "9.2.0", {"companion/a.py": "x"},
                                    extra_members={"gsvr-companion/../../evil.py": "pwn"})
        publish(fake, "v9.2.0", "source", tdata, tsha)
        r = updater.check_for_update(client=cl, key="source")
        try:
            updater.install(r, root=inst, kind="source", client=cl, log=logs.append)
            check(False, "path traversal refused")
        except updater.UpdateError as e:
            same, evil = snapshot(inst) == before, os.path.exists(os.path.join(tmp, "evil.py"))
            check("unsafe path" in str(e) and same and not evil,
                  f"path traversal in archive refused ({e}; install unchanged {same}, evil.py written {evil})")
        fake.releases.pop()

        wdata, wsha = build_archive(tmp, "source", "1.0.0", {"companion/a.py": "x"})          # manifest says 1.0.0
        publish(fake, "v9.3.0", "source", wdata, wsha)
        r = updater.check_for_update(client=cl, key="source")
        try:
            updater.install(r, root=inst, kind="source", client=cl, log=logs.append)
            check(False, "manifest version mismatch refused")
        except updater.UpdateError as e:
            check("version" in str(e) and snapshot(inst) == before, f"archive/tag version mismatch refused ({e})")
        fake.releases.pop()

        xdata, xsha = build_archive(tmp, "source", "9.4.0", {"unity/Assets/x.cs": "x"})       # outside companion/tools
        publish(fake, "v9.4.0", "source", xdata, xsha)
        r = updater.check_for_update(client=cl, key="source")
        try:
            updater.install(r, root=inst, kind="source", client=cl, log=logs.append)
            check(False, "source archive writing outside companion/ tools/ refused")
        except updater.UpdateError as e:
            check("may only touch" in str(e) and snapshot(inst) == before, f"source update outside companion/, tools/ refused ({e})")
        fake.releases.pop()

        r = dict(status="available", message="", sha_url=None)
        git = os.path.join(tmp, "gitinst"); os.makedirs(os.path.join(git, ".git"))
        try:
            updater.install(r, root=git, kind="source", client=cl, log=logs.append)
            check(False, "git checkout refused")
        except updater.UpdateError as e:
            check("git" in str(e), "source update inside a git checkout refused")

        stage = tempfile.mkdtemp(dir=tmp)
        write_tree(stage, {"companion/a.py": "AAA", "companion/b.py": "BBB", "tools/pack.py": "PPP"})
        updater.write_manifest(stage, "9.9.0", "source", ["companion/a.py", "companion/b.py", "tools/pack.py"])
        try:
            updater.apply(stage, inst, "source", ["companion/a.py", "companion/b.py", "tools/pack.py"], log=logs.append, _fail_after=2)
            check(False, "mid-swap failure rolls back")
        except updater.UpdateError as e:
            check(snapshot(inst) == before and "restored" in str(e), f"failure after 2 of 3 files → rolled back ({e})")
    finally:
        fake.close()

    print("[update] install (frozen layout: rename running exe, locked backup)")
    fake = FakeGitea()
    try:
        inst = os.path.join(tmp, "frozen"); os.makedirs(inst)
        old = frozen_files("v0.3.0")
        old["_internal/old_only.pyd"] = "gone in new"
        write_tree(inst, old)
        updater.write_manifest(inst, "0.3.0", "windows-x64", list(old))
        os.makedirs(os.path.join(inst, "GSVR.exe" + updater.OLD_SUFFIX))   # a backup that can't be deleted ("still locked")
        data, sha = build_archive(tmp, "windows-x64", "9.0.0", frozen_files("v9.0.0"))
        publish(fake, "v9.0.0", "windows-x64", data, sha)
        cl = updater.Client(fake.base)
        r = updater.check_for_update(client=cl, key="windows-x64")
        # a running exe can be renamed on Windows (its image is mapped with FILE_SHARE_DELETE), but a Python open()
        # handle can't — so the "running exe" is only simulated off Windows; the real case is the frozen smoke test
        held = open(os.path.join(inst, "GSVR.exe"), "rb") if os.name != "nt" else None
        try:
            argv = updater.install(r, root=inst, kind="frozen", client=cl, log=lambda m: None)
        finally:
            if held:
                held.close()
        rd = lambda rel: open(os.path.join(inst, *rel.split("/")), encoding="utf-8").read()   # noqa: E731
        check(rd("GSVR.exe") == "gui v9.0.0" and rd("_internal/python3.dll") == "dll v9.0.0", "exe + dll swapped")
        check(os.path.isfile(os.path.join(inst, "GSVR.exe.1" + updater.OLD_SUFFIX)), "locked backup name → GSVR.exe.1.gsvr-old")
        check(not os.path.exists(os.path.join(inst, "_internal", "old_only.pyd")), "stale file of the old build removed")
        check(argv == [os.path.join(os.path.abspath(inst), runtime.GUI_EXE)], f"relaunch argv → new exe ({argv})")
        os.rmdir(os.path.join(inst, "GSVR.exe" + updater.OLD_SUFFIX))
        updater.cleanup_old(inst, "frozen")
        check(not [f for f in snapshot(inst) if f.endswith(updater.OLD_SUFFIX)], "cleanup_old (frozen) removes backups")

        print("[update] linux tar.gz: exec bits kept")
        lin = os.path.join(tmp, "linux"); os.makedirs(lin)
        files = frozen_files("v9.0.0", "GSVR")
        data, sha = build_archive(tmp, "linux-x64", "9.0.0", files, exec_files=("GSVR", "gsvr-cli"))
        publish(fake, "v9.0.1", "linux-x64", data, sha.replace("9.0.0", "9.0.1"))
        r = updater.check_for_update(client=cl, key="linux-x64")
        r["version"] = "9.0.0"                                              # archive manifest says 9.0.0
        updater.install(r, root=lin, kind="frozen", client=cl, log=lambda m: None)
        if not runtime.WIN:
            check(os.stat(os.path.join(lin, "GSVR")).st_mode & stat.S_IXUSR and os.stat(os.path.join(lin, "gsvr-cli")).st_mode & stat.S_IXUSR,
                  "GSVR + gsvr-cli executable after the update")
        check(open(os.path.join(lin, "GSVR")).read() == "gui v9.0.0", "tar.gz update installed")
    finally:
        fake.close()


def test_release(tmp):
    print("[release] packaging/release.py: archives, checksums, upload to a fake Gitea")
    if runtime.FROZEN:
        print("  skip (frozen build)")
        return
    sys.path.insert(0, os.path.join(HERE, "packaging"))
    import release
    out = os.path.join(tmp, "rel"); os.makedirs(out)
    dist = os.path.join(tmp, "dist"); os.makedirs(dist)
    write_tree(dist, frozen_files("v" + VERSION))
    path, digest = release.make_archive(dist, "GSVR", "windows-x64", out)
    names = zipfile.ZipFile(path).namelist()
    check(all(n.startswith("GSVR/") for n in names) and "GSVR/gsvr-manifest.txt" in names and "GSVR/GSVR.exe" in names,
          "windows archive: GSVR/ top folder + manifest")
    check(updater.parse_sha256_file(open(path + ".sha256").read(), os.path.basename(path)) == digest == release.sha256_file(path),
          ".sha256 matches the archive")
    src = os.path.join(tmp, "src")
    with contextlib.redirect_stdout(io.StringIO()):
        release.cmd_source(argparse.Namespace(out=src))
    sz = zipfile.ZipFile(os.path.join(src, "gsvr-companion-source.zip"))
    names = sz.namelist()
    check("gsvr-companion/companion/companion.py" in names and "gsvr-companion/tools/pack.py" in names
          and "gsvr-companion/companion/version.py" in names and not any("__pycache__" in n or "/build/" in n for n in names),
          f"source archive: companion/ + tools/, no caches/builds ({len(names)} files)")
    man = sz.read("gsvr-companion/gsvr-manifest.txt").decode().splitlines()
    check(man[0] == f"# gsvr-companion {VERSION} source", "source manifest header")

    fake = FakeGitea(require_token=SECRET)
    os.environ["GITEA_TOKEN"] = SECRET
    try:
        fake.tags.add("v" + VERSION)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            release.main(["upload", "--tag", "v" + VERSION, "--out", out, "--base", fake.base])
            release.main(["upload", "--tag", "v" + VERSION, "--out", out, "--base", fake.base])     # again → replace
        rel = fake.releases[0] if fake.releases else {}
        names = sorted(a["name"] for a in rel.get("assets", []))
        check(names == ["gsvr-companion-windows-x64.zip", "gsvr-companion-windows-x64.zip.sha256"] and rel.get("prerelease"),
              f"release created as pre-release with archive + .sha256 ({names})")
        check("(replaced)" in buf.getvalue() and len(rel.get("assets", [])) == 2, "second upload replaces the assets")
        check(SECRET not in buf.getvalue(), "token not printed")
        got = fake.files["gsvr-companion-windows-x64.zip@v" + VERSION]
        check(hashlib.sha256(got).hexdigest() == digest, "uploaded bytes = archive")
        r = updater.check_for_update(client=updater.Client(fake.base, SECRET), key="windows-x64", current="0.1.0")
        check(r["status"] == "available", "the uploaded release is what the updater offers")
        for argv, why in ((["upload", "--tag", "v0.0.1", "--out", out, "--base", fake.base], "tag != version"),
                          (["upload", "--tag", "v" + VERSION, "--out", os.path.join(tmp, "nothing"), "--base", fake.base], "nothing")):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    release.main(argv)
                check(False, f"upload refused: {why}")
            except SystemExit as e:
                check(e.code not in (0, None), f"upload refused: {why}")
        os.remove(path + ".sha256")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                release.main(["upload", "--tag", "v" + VERSION, "--out", out, "--base", fake.base, "--dry-run"])
            check(False, "upload refused: archive without .sha256")
        except SystemExit as e:
            check("sha256" in str(e.code), "upload refused: archive without .sha256")
    finally:
        os.environ.pop("GITEA_TOKEN", None)
        fake.close()


def test_dispatch(tmp, game_root):
    print("[frozen] gsvr_main.py --run-converter dispatch")
    if runtime.FROZEN:
        print("  skip (already frozen: build_windows.bat smoke-tests the real exe)")
        return
    main = os.path.join(HERE, "gsvr_main.py")
    tools = os.path.join(REPO, "tools")
    r = subprocess.run([sys.executable, "-I", main, "--version"], capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and r.stdout.strip() == VERSION, f"--version → {r.stdout.strip()}")
    r = subprocess.run([sys.executable, "-I", main, "--run-converter", os.path.join(tools, "modelpack.py"), "--version"],
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and r.stdout.strip().startswith("gsm1-"), f"--run-converter modelpack.py --version → {r.stdout.strip()}")
    r = subprocess.run([sys.executable, "-I", main, "--run-converter", os.path.join(tools, "serve.py")],
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 2 and "only" in r.stderr, "--run-converter refuses other scripts")
    r = subprocess.run([sys.executable, "-I", main, "--run-converter", os.path.join(tools, "pack.py"), "--bsp", "/nonexistent.bsp", "x.bin"],
                       capture_output=True, text=True, timeout=60, cwd=tmp)
    check(r.returncode != 0, f"converter failure propagates as non-zero exit ({r.returncode})")
    if runtime.WIN:
        print("  skip shim-exe converter test on Windows (covered by the frozen smoke test)")
        return
    shim = os.path.join(tmp, "gsvr-cli")
    with open(shim, "w") as fh:                                   # stands in for the frozen gsvr-cli executable
        fh.write(f"#!/bin/sh\nexec '{sys.executable}' -I '{main}' \"$@\"\n")
    os.chmod(shim, 0o755)
    import builder, models
    conv = builder.Converter(os.path.join(tools, "pack.py"), python=shim, frozen=True)
    a = conv.argv(bsp="B", out="O", mod="valve", map="m", game_root="G", mod_dir="D")
    check(a[:3] == [shim, "--run-converter", os.path.join(tools, "pack.py")] and "-X" not in a and "-I" not in a
          and "--game-root" in a and "--mod" in a, "frozen Converter: --run-converter template, --help detection works through the exe")
    mb = models.ModelBuilder(os.path.join(tools, "modelpack.py"), python=shim, frozen=True)
    check(mb.version.startswith("v-") and mb.argv(game_root="G", mod="cstrike", out="O")[1] == "--run-converter",
          f"frozen ModelBuilder: --version through the exe ({mb.version})")
    bsp = os.path.join(game_root, "valve", "maps", "crossfire.bsp")
    if not os.path.isfile(bsp):
        print(f"  skip frozen-mode map build ({bsp} missing)")
        return
    res = conv.build(os.path.join(tmp, "cache"), "valve", "crossfire", bsp, game_root, os.path.join(game_root, "valve"),
                     log=lambda m: None)
    check(res["size"] > 0 and builder.validate_pack(res["path"]) == res["size"],
          f"crossfire built through the frozen-style exe ({res['size']} bytes, {res['seconds']} s)")


def controller_test(Controller, chk):
    """gui.Controller update check + install against a fake Gitea, for the CURRENT install kind (source or frozen),
    into a temp install folder. The token must never show up in the GUI log."""
    print("[gui] update check + install through the Controller")
    tmp = tempfile.mkdtemp()
    key, kind = updater.platform_key(), runtime.install_kind()
    fake = FakeGitea(require_token=SECRET)
    ok = True
    try:
        files = {"companion/gui.py": "new gui", "tools/pack.py": "new pack"} if kind == "source" else frozen_files("v99.0.0", runtime.GUI_EXE)
        data, sha = build_archive(tmp, key, "99.0.0", files)
        publish(fake, "v99.0.0", key, data, sha)
        inst = os.path.join(tmp, "inst")
        write_tree(inst, {k: "old" for k in files})
        c = Controller()
        c.settings.update(update_base=fake.base, update_token="", prereleases=True)
        ok &= chk(c.check_update() is None and c.update_error and "token" in c.update_error,
                  "private feed without token → check fails with a token hint")
        c.settings["update_token"] = SECRET
        r = c.check_update()
        ok &= chk(r is not None and r["status"] == "available" and r["latest"] == "v99.0.0", "check → update available v99.0.0")
        argv = c.install_update(root=inst, kind=kind)
        ok &= chk(argv is not None and all(open(os.path.join(inst, *k.split("/"))).read() == v for k, v in files.items()),
                  f"Controller.install_update swapped the files ({kind}, {key})")
        ok &= chk(argv is not None and os.path.dirname(argv[-1]).startswith(os.path.abspath(inst)), "relaunch argv points into the install")
        fake.files[updater.asset_name(key) + "@v99.0.0"] = data + b"tampered"
        ok &= chk(c.install_update(root=inst, kind=kind) is None and "mismatch" in (c.update_error or ""),
                  "tampered download refused by the Controller")
        logs = []
        while not c.log_queue.empty():
            logs.append(c.log_queue.get())
        ok &= chk(logs and not any(SECRET in l for l in logs), "token never in the GUI log (%d lines)" % len(logs))
    finally:
        fake.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_bundled_tools():
    """BUGS C-SYNC: the frozen build bundles every converter module the entry scripts import (gsvr.spec list)."""
    print("[bundle] converter modules of the frozen build")
    sys.path.insert(0, os.path.join(HERE, "packaging"))
    import toolfiles
    tools = os.path.join(REPO, "tools")
    files = toolfiles.tool_files(tools)
    check(not toolfiles.missing_imports(tools, files), "every tools/ import of the bundled scripts is bundled")
    for f in ("w3pack.py", "w3spellart.py", "w3missileart.py", "w3info.py", "w3mapfolders.py", "pack.py", "gamedirs.py"):
        check(f in files, f + " bundled")
    with open(os.path.join(HERE, "packaging", "gsvr.spec"), encoding="utf-8") as fh:
        check("toolfiles.tool_files(TOOLS)" in fh.read(), "gsvr.spec takes its list from toolfiles (no hand list)")


def test_mp_worker_entry():
    """BUGS L-SPAWN (warcraft3): w3pack bakes models in a spawn pool on Windows; a frozen worker is gsvr-cli started
    with --multiprocessing-fork, which freeze_support() must catch in __main__ before main() dispatches argv."""
    print("[bundle] frozen multiprocessing worker entry")
    import ast
    with open(os.path.join(HERE, "gsvr_main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    block = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
    check(len(block) == 1, "gsvr_main has one __main__ block")
    calls = [ast.dump(c.func) for c in ast.walk(block[0]) if isinstance(c, ast.Call)] if block else []
    fs = next((k for k, c in enumerate(calls) if "freeze_support" in c), -1)
    mn = next((k for k, c in enumerate(calls) if "'main'" in c), -1)
    check(fs >= 0 and 0 <= mn and fs < mn, "freeze_support() runs before main()")


def test_converter_low_priority():
    """maintainer 2026-10-23: the converter runs below normal priority (VRChat + SteamVR keep the PC)."""
    print("[priority] converter below normal priority")
    import runtime, builder, subprocess as sp
    kw = runtime.low_priority_kwargs()
    if sys.platform == "win32":
        check(kw.get("creationflags", 0) & runtime.BELOW_NORMAL_PRIORITY_CLASS, "Windows: BELOW_NORMAL_PRIORITY_CLASS")
    else:
        check(callable(kw.get("preexec_fn")), "POSIX: nice in the child")
    seen = {}
    orig, orig_popen = sp.run, sp.Popen

    def spy(argv, **k):
        seen.update(k)
        raise OSError("spy")
    b = builder.Converter(os.path.join(HERE, "x.py"), template="{python} -c pass {out}", timeout=5)
    sp.run = spy
    sp.Popen = spy                 # the converters run through buildprog.run (Popen, streaming progress)
    try:
        tmp = tempfile.mkdtemp(prefix="gsvr-prio-")
        bsp = os.path.join(tmp, "m.bsp")
        open(bsp, "wb").write(b"x")
        try:
            b.build(tmp, "valve", "m", bsp, tmp, tmp, log=lambda *_a: None)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        sp.run, sp.Popen = orig, orig_popen
    check(all(seen.get(k) is not None for k in kw), "builder starts the converter with them (%s)" % sorted(seen))


def run_all(game_root=None):
    tmp = tempfile.mkdtemp(prefix="gsvr-upd-")
    try:
        test_versions()
        test_bundled_tools()
        test_converter_low_priority()
        test_mp_worker_entry()
        test_check(tmp)
        test_install(tmp)
        test_release(tmp)
        test_dispatch(tmp, game_root or os.path.join(REPO, "input", "Half-Life"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return list(FAILS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-root", default=os.path.join(REPO, "input", "Half-Life"))
    a = ap.parse_args()
    fails = run_all(a.game_root)
    print(f"\nRESULT: {'FAIL (' + str(len(fails)) + ')' if fails else 'PASS'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
