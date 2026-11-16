"""Maintainer tool for GSVR companion game modules (stdlib only; never uploads anything, never runs implicitly).

  python3 -I companion/packaging/modtool.py keygen  --out ~/.gsvr_module_key          # new ed25519 key; prints the public hex for modules.PUBKEYS
  python3 -I companion/packaging/modtool.py build   --module rose --version 1.0.0 [--out companion/build/modules]
  python3 -I companion/packaging/modtool.py release --serial 1 --key ~/.gsvr_module_key [--out companion/build/modules]
        -> gsvr-modules.json + gsvr-modules.json.sig for every gsvr-module-*.zip in --out (tag to create by hand: modules-<serial>)
  python3 -I companion/packaging/modtool.py verify  --dir companion/build/modules --pub <hex>

build packs the files of git HEAD (`git archive`), never the working tree, and prints the file list: what is released is what is committed.
Zips are deterministic (sorted, fixed timestamps) so a rebuild of the same sources has the same sha256. Upload by hand (see
docs/design/companion_modules.md "Release steps"): gh release create modules-<serial> <dir>/gsvr-module-*.zip <dir>/gsvr-modules.json <dir>/gsvr-modules.json.sig
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.dirname(HERE)
sys.path.insert(0, COMP)
import ed25519          # noqa: E402
import modules          # noqa: E402
from version import REPO_NAME, REPO_OWNER, VERSION   # noqa: E402

ROOT = os.path.realpath(os.path.dirname(COMP))

# module name -> files (source relative to companion/, destination inside the zip), import search paths, core minimum
DEFS = {
    "rose": {
        "dirs": ["rose_conv"],                                   # whole folder (*.py, VERSION_SOURCE, SOURCE_HASHES, lobby_default.json)
        "files": ["rosebuild.py", "rosefind.py"],
        "paths": ["", "rose_conv"],                              # rose_conv's flat converter modules import each other by plain name
    },
}
DIR_OK = (".py", ".json", ".txt", "")                            # "" = VERSION_SOURCE / SOURCE_HASHES
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def want(name):
    """repo-relative paths (below companion/) that make up the module: files and folders"""
    d = DEFS[name]
    return list(d["files"]) + list(d["dirs"])


def collect_tree(name):
    """{zip path: bytes} from the WORKING TREE. For tests / dev only: the CLI never uses it (see git_files)."""
    d = DEFS[name]
    out = {}
    for f in d["files"]:
        with open(os.path.join(COMP, f), "rb") as fh:
            out[f] = fh.read()
    for sub in d["dirs"]:
        for fn in sorted(os.listdir(os.path.join(COMP, sub))):
            p = os.path.join(COMP, sub, fn)
            if os.path.isfile(p) and os.path.splitext(fn)[1] in DIR_OK:
                with open(p, "rb") as fh:
                    out[sub + "/" + fn] = fh.read()
    return out


def git_files(name, rev="HEAD"):
    """{zip path: bytes} of the module at git `rev` (git archive): uncommitted edits and stray files never get released."""
    paths = ["companion/" + p for p in want(name)]
    r = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", rev, "--"] + paths, capture_output=True)
    if r.returncode:
        raise SystemExit("git archive failed: %s" % r.stderr.decode("utf-8", "replace").strip())
    out = {}
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.startswith("companion/"):
                continue
            rel = m.name[len("companion/"):]
            if "/" in rel and os.path.splitext(rel)[1] not in DIR_OK:
                continue
            out[rel] = tf.extractfile(m).read()
    return out


def provides(name, files):
    names = set()
    for rel in files:
        for base in DEFS[name]["paths"]:
            pre = (base + "/") if base else ""
            if rel.startswith(pre):
                rest = rel[len(pre):].split("/")
                if rest[0].endswith(".py"):
                    names.add(rest[0][:-3])
                elif len(rest) > 1:
                    names.add(rest[0])
    return sorted(n for n in names if n.isidentifier() and n != "__init__")


def build(name, version, out_dir, core_min=None, files=None):
    """files = {zip path: bytes}; the CLI passes git_files(name). Default (tests): the working tree."""
    files = collect_tree(name) if files is None else files
    hashes = {rel: modules.sha256_bytes(b) for rel, b in files.items()}
    mj = {"name": name, "version": version, "api": modules.MODULE_API, "core_min": core_min or VERSION, "paths": DEFS[name]["paths"],
          "provides": provides(name, hashes), "files": hashes}
    modules.check_member_names(list(files))                       # the same rules the app applies: fail here, not on the user's PC
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "gsvr-module-%s-%s.zip" % (name, version))
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in ["module.json"] + sorted(files):
            zi = zipfile.ZipInfo(rel, FIXED_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, json.dumps(mj, indent=1, sort_keys=True).encode("utf-8") if rel == "module.json" else files[rel])
    os.replace(tmp, path)
    return path


def in_repo(path):
    p = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if p == ROOT or p.startswith(ROOT + os.sep):
        return True
    d = os.path.dirname(p)                                        # any other git checkout (the main repo, another worktree) counts too
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        up = os.path.dirname(d)
        if up == d:
            return False
        d = up


def make_release(serial, key_path, out_dir):
    ents = {}
    for fn in sorted(os.listdir(out_dir)):
        m = modules.FILE_RE.match(fn)
        if not m:
            continue
        with zipfile.ZipFile(os.path.join(out_dir, fn)) as zf:
            raw = zf.read("module.json")
        mj = modules.parse_module_json(raw)
        if (mj["name"], mj["version"]) != m.groups():
            raise SystemExit("%s: module.json disagrees with the file name" % fn)
        cur = ents.get(mj["name"])
        if cur and modules.compare_versions(cur["version"], mj["version"]) >= 0:
            continue                                            # newest version per module only
        p = os.path.join(out_dir, fn)
        ents[mj["name"]] = {"version": mj["version"], "api": mj["api"], "core_min": mj.get("core_min", ""), "file": fn,
                            "size": os.path.getsize(p), "sha256": modules.sha256_file(p),
                            "module_json_sha256": modules.sha256_bytes(raw)}
    if not ents:
        raise SystemExit("no gsvr-module-*.zip in %s" % out_dir)
    manifest = {"schema": modules.SCHEMA, "repo": "%s/%s" % (REPO_OWNER, REPO_NAME), "serial": serial, "modules": ents}
    data = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
    with open(key_path, encoding="ascii") as fh:
        seed = bytes.fromhex(fh.read().strip())
    sig = ed25519.sign(seed, data)
    with open(os.path.join(out_dir, modules.MANIFEST_ASSET), "wb") as fh:
        fh.write(data)
    with open(os.path.join(out_dir, modules.SIG_ASSET), "w", encoding="ascii") as fh:
        fh.write(sig.hex() + "\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", required=True)
    b = sub.add_parser("build")
    b.add_argument("--module", required=True, choices=sorted(DEFS))
    b.add_argument("--version", required=True)
    b.add_argument("--out", default=os.path.join(COMP, "build", "modules"))
    r = sub.add_parser("release")
    r.add_argument("--serial", type=int, required=True)
    r.add_argument("--key", required=True)
    r.add_argument("--out", default=os.path.join(COMP, "build", "modules"))
    v = sub.add_parser("verify")
    v.add_argument("--dir", required=True)
    v.add_argument("--pub", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        if in_repo(a.out):
            raise SystemExit("refusing to write a private key inside the repository tree (%s): use a path outside it" % ROOT)
        if os.path.exists(a.out):
            raise SystemExit("%s exists, refusing to overwrite a key" % a.out)
        seed = os.urandom(32)
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(seed.hex() + "\n")
        print("private key written to %s (keep it OUT of the repo, back it up)" % a.out)
        print("public key for modules.PUBKEYS: %s" % ed25519.publickey(seed).hex())
    elif a.cmd == "build":
        files = git_files(a.module)
        print("packing from git HEAD (%d files):" % len(files))
        for rel in sorted(files):
            print("  " + rel)
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--"] + ["companion/" + p for p in want(a.module)],
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            print("NOTE: uncommitted changes are NOT in the module (HEAD only):\n" + dirty)
        print(build(a.module, a.version, a.out, files=files))
    elif a.cmd == "release":
        m = make_release(a.serial, a.key, a.out)
        print("serial %d: %s" % (m["serial"], ", ".join("%s %s" % (n, e["version"]) for n, e in sorted(m["modules"].items()))))
        print("tag to create: modules-%d" % a.serial)
    else:
        with open(os.path.join(a.dir, modules.MANIFEST_ASSET), "rb") as fh:
            data = fh.read()
        with open(os.path.join(a.dir, modules.SIG_ASSET), encoding="ascii") as fh:
            m = modules.verify_manifest(data, fh.read(), [a.pub])
        for n, e in m["modules"].items():
            got = modules.sha256_file(os.path.join(a.dir, e["file"]))
            if got != e["sha256"]:
                raise SystemExit("%s: sha256 mismatch" % e["file"])
        print("ok: serial %d, %d module(s)" % (m["serial"], len(m["modules"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
