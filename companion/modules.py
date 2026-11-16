"""GSVR companion game modules: signed, hash-checked, pure-python add-ons installed next to the exe (design: docs/design/companion_modules.md).

First module: "rose" = rose_conv + rosebuild + rosefind. The exe always carries its own copy of every module (built-in); an installed
module only overrides it when it is enabled, verified and API-compatible, else the built-in copy is used (never a crash).

Trust chain. Nothing from a module is imported or run before every step passed, and the SAME chain is re-run at every start (activate):
  embedded ed25519 key  -->  signed manifest gsvr-modules.json (+ .sig, both stored in the cache at install)
  manifest entry (version, api, size, sha256 of the zip, module_json_sha256)  -->  module.json (hash bound by the signature)
  module.json: per-file sha256, provides, paths  -->  every file of the tree (no extra file, no __pycache__ / .pyc, no symlink)
  hard-coded ALLOWED table (names a module may provide)  -->  never a stdlib / core name, each name resolves inside the verified tree.
state.json (local) only CHOOSES between versions; it grants no trust: a forged state.json or module.json without the matching signed
manifest is ignored. Downloads only from https://github.com/<REPO_OWNER>/<REPO_NAME>/releases/download/<tag>/<asset> (URL built here).
No downgrade: a manifest serial lower than the highest seen is refused, a version <= the active one is not installed; the only way back is
rollback() to the locally kept previous version (re-verified like an activation). State changes are atomic (random staging dir + os.replace).
Stdlib only. Modules are pure python: native files (.pyd/.dll/.so/.exe...) and bytecode are refused.
"""
import hashlib
import importlib.machinery
import io
import json
import os
import re
import shutil
import sys
import sysconfig
import tempfile
import zipfile

import ed25519
from updater import compare_versions
from version import REPO_NAME, REPO_OWNER, VERSION

MODULE_API = 1                         # bump when a module may no longer work with this core (what core modules a module imports: see the design doc)
SCHEMA = 1
MANIFEST_ASSET = "gsvr-modules.json"
SIG_ASSET = "gsvr-modules.json.sig"
TAG_RE = re.compile(r"modules-(\d{1,9})")
NAME_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
VER_RE = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
FILE_RE = re.compile(r"gsvr-module-([a-z][a-z0-9]{1,15})-(\d{1,4}\.\d{1,4}\.\d{1,4})\.zip")
SHA_RE = re.compile(r"[0-9a-f]{64}")
MEMBER_RE = re.compile(r"[A-Za-z0-9_./-]+")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_MANIFEST = 256 * 1024
MAX_MODULE = 64 * 1024 * 1024
MAX_FILES = 500
MAX_UNPACKED = 256 * 1024 * 1024
BAD_EXT = (".pyd", ".dll", ".so", ".dylib", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".lnk", ".jar", ".pyc", ".pyo")
WIN_RESERVED = frozenset(["con", "prn", "aux", "nul"] + ["com%d" % i for i in range(1, 10)] + ["lpt%d" % i for i in range(1, 10)])

# What each module may provide (hard-coded, NOT read from the module): top-level import names, the import search folders, and the folder
# whose *.py files are the flat converter modules (rose_conv imports them by plain name). Anything else is refused, signed or not.
ALLOWED = {
    "rose": {"top": frozenset({"rose_conv", "rosebuild", "rosefind"}), "paths": ("", "rose_conv"), "flat": "rose_conv"},
}
# core modules a module must never shadow (the exe's own code); stdlib / builtin names are refused by _reserved()
CORE_NAMES = frozenset({"modules", "ed25519", "updater", "gui", "companion", "runtime", "version", "gsvr_main", "builder", "buildprog",
                        "models", "prebuilt", "games", "steam", "vdf", "logwatch", "selftest_update", "war3", "mapshare", "csshare",
                        "cachedir", "rose", "rose_conv_stub", "holo", "tkinter", "PIL", "miniaudio"})

# Ed25519 public keys (hex) allowed to sign the module manifest. Empty = no module can ever be installed or activated (fail closed).
# Maintainer: `python3 -I companion/packaging/modtool.py keygen --out <private key file outside the repo>` prints the value to paste here.
PUBKEYS = ()


class ModuleError(Exception):
    pass


def frozen():
    return bool(getattr(sys, "frozen", False))


# ---------------------------------------------------------------------------------------------- paths / settings
def settings_file():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GSVR", "gui.json")
    return os.path.expanduser("~/.config/gsvr/gui.json")


def modules_root():
    """cache folder. A frozen build ignores GSVR_MODULES_DIR (an environment variable must not redirect where code is loaded from);
    Windows: %LOCALAPPDATA% (not roaming), else next to gui.json."""
    if not frozen() and os.environ.get("GSVR_MODULES_DIR"):
        return os.environ["GSVR_MODULES_DIR"]
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "GSVR", "modules")
    return os.path.join(os.path.dirname(settings_file()), "modules")


def enabled(settings=None):
    """Off by default. GSVR_MODULES=1/0 wins in source runs only (ignored when frozen), else gui.json "modules_enabled"."""
    env = None if frozen() else os.environ.get("GSVR_MODULES")
    if env in ("0", "1"):
        return env == "1"
    try:
        with open(settings or settings_file(), encoding="utf-8") as fh:
            d = json.load(fh)
        return bool(d.get("modules_enabled")) if isinstance(d, dict) else False
    except (OSError, ValueError):
        return False


def set_enabled(on, settings=None):
    """Write modules_enabled into gui.json, keeping every other key. A gui.json that exists but cannot be parsed is NOT overwritten."""
    path = settings or settings_file()
    d = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            raise ModuleError("gui.json is unreadable, not touching it") from None
        if not isinstance(d, dict):
            raise ModuleError("gui.json is not a JSON object, not touching it")
    d["modules_enabled"] = bool(on)
    _write_json(path, d)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _write_bytes(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- manifest
def verify_manifest(data, sig_text, pubkeys=None):
    """Signed bytes + hex signature -> parsed manifest dict. ModuleError on a bad / missing signature or schema."""
    keys = PUBKEYS if pubkeys is None else pubkeys
    if not keys:
        raise ModuleError("no signing key embedded in this build: modules cannot be installed")
    try:
        sig = bytes.fromhex(sig_text.strip())
    except (ValueError, AttributeError):
        raise ModuleError("manifest signature is not hex") from None
    good = False
    for k in keys:
        try:
            good = ed25519.verify(bytes.fromhex(k), data, sig)
        except (ValueError, TypeError):
            continue
        if good:
            break
    if not good:
        raise ModuleError("manifest signature invalid")
    try:
        m = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ModuleError("manifest is not JSON") from None
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        raise ModuleError("unsupported manifest schema")
    if m.get("repo") != "%s/%s" % (REPO_OWNER, REPO_NAME):
        raise ModuleError("manifest is for another repository")
    ser = m.get("serial")
    if not isinstance(ser, int) or isinstance(ser, bool) or ser < 1:
        raise ModuleError("manifest serial missing")
    mods = m.get("modules")
    if not isinstance(mods, dict):
        raise ModuleError("manifest has no modules")
    for name, e in mods.items():
        ok = isinstance(name, str) and NAME_RE.fullmatch(name) and isinstance(e, dict) and isinstance(e.get("api"), int) \
            and not isinstance(e.get("api"), bool) and isinstance(e.get("size"), int) and 0 < e["size"] <= MAX_MODULE \
            and SHA_RE.fullmatch(str(e.get("sha256", ""))) and SHA_RE.fullmatch(str(e.get("module_json_sha256", ""))) \
            and isinstance(e.get("version"), str) and VER_RE.fullmatch(e["version"]) and isinstance(e.get("file"), str) \
            and FILE_RE.fullmatch(e["file"]) and isinstance(e.get("core_min", ""), str)
        if ok:
            fm = FILE_RE.fullmatch(e["file"])
            ok = fm.group(1) == name and fm.group(2) == e["version"] and (not e.get("core_min") or VER_RE.fullmatch(e["core_min"]))
        if not ok:
            raise ModuleError("manifest entry %r is malformed" % (name,))
    return m


def download_url(tag, asset):
    return "https://github.com/%s/%s/releases/download/%s/%s" % (REPO_OWNER, REPO_NAME, tag, asset)


# ---------------------------------------------------------------------------------------------- module trees
def check_member_names(names):
    """All member names of a module: strict charset, no traversal, no Windows-reserved / trailing-dot names, no case-insensitive
    duplicates, no bytecode / native files."""
    seen = set()
    for name in names:
        n = name
        if not isinstance(n, str) or not n or len(n) > 200 or not MEMBER_RE.fullmatch(n):
            raise ModuleError("unsafe path in module: %r" % (name,))
        parts = n.split("/")
        for p in parts:
            if p in ("", ".", "..") or len(p) > 100 or p.endswith(".") or p == "__pycache__":
                raise ModuleError("unsafe path in module: %r" % (name,))
            if p.split(".")[0].lower() in WIN_RESERVED:
                raise ModuleError("Windows-reserved name in module: %r" % (name,))
        if n.lower().endswith(BAD_EXT):
            raise ModuleError("native / executable / bytecode file in module: %r" % (name,))
        if n.lower() in seen:
            raise ModuleError("duplicate (case-insensitive) name in module: %r" % (name,))
        seen.add(n.lower())


def parse_module_json(raw):
    try:
        mj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ModuleError("module.json invalid") from None
    ok = isinstance(mj, dict) and isinstance(mj.get("name"), str) and NAME_RE.fullmatch(mj["name"]) \
        and isinstance(mj.get("api"), int) and isinstance(mj.get("files"), dict) and isinstance(mj.get("provides"), list) \
        and isinstance(mj.get("paths"), list) and isinstance(mj.get("version"), str) and VER_RE.fullmatch(mj["version"]) \
        and all(isinstance(k, str) and SHA_RE.fullmatch(str(v)) for k, v in mj["files"].items()) \
        and all(isinstance(n, str) and IDENT_RE.fullmatch(n) for n in mj["provides"]) \
        and all(isinstance(p, str) for p in mj["paths"])
    if not ok:
        raise ModuleError("module.json malformed")
    return mj


def _reserved(name):
    """True for names a module must never provide: core modules, builtins, the standard library."""
    if name in CORE_NAMES or name in sys.builtin_module_names or name in getattr(sys, "stdlib_module_names", ()):
        return True
    std = sysconfig.get_paths().get("stdlib")
    return bool(std and os.path.isdir(std) and importlib.machinery.PathFinder.find_spec(name, [std]) is not None)


def check_allowed(mj):
    """provides / paths of module.json against the hard-coded ALLOWED table; every provided name must resolve to a file of the tree."""
    rules = ALLOWED.get(mj["name"])
    if not rules:
        raise ModuleError("module %r is not allowed" % mj["name"])
    if set(mj["paths"]) - set(rules["paths"]) or len(set(mj["paths"])) != len(mj["paths"]):
        raise ModuleError("module.json paths not allowed")
    files = set(mj["files"])
    flat = {os.path.splitext(f.split("/", 1)[1])[0] for f in files
            if f.startswith(rules["flat"] + "/") and f.count("/") == 1 and f.endswith(".py") and f != rules["flat"] + "/__init__.py"}
    ok_names = set(rules["top"]) | flat
    for n in mj["provides"]:
        if n not in ok_names or _reserved(n):
            raise ModuleError("module may not provide %r" % n)
        found = False
        for base in mj["paths"]:
            pre = (base + "/") if base else ""
            if pre + n + ".py" in files or pre + n + "/__init__.py" in files:
                found = True
        if not found:
            raise ModuleError("provided name %r does not resolve inside the module" % n)
    if len(set(mj["provides"])) != len(mj["provides"]) or not mj["provides"]:
        raise ModuleError("module.json provides malformed")


def check_tree(path, name, entry):
    """Verify an unpacked / installed module folder against the SIGNED manifest entry. -> module.json dict."""
    try:
        with open(os.path.join(path, "module.json"), "rb") as fh:
            raw = fh.read(1 << 20)
    except OSError:
        raise ModuleError("module.json unreadable") from None
    if sha256_bytes(raw) != entry.get("module_json_sha256"):
        raise ModuleError("module.json does not match the signed manifest")
    mj = parse_module_json(raw)
    if mj["name"] != name or mj["version"] != entry["version"] or mj["api"] != entry["api"]:
        raise ModuleError("module.json does not match the signed manifest entry")
    if mj["api"] != MODULE_API:
        raise ModuleError("module api %r, this core needs %d" % (mj["api"], MODULE_API))
    core_min = entry.get("core_min")
    if core_min and compare_versions(VERSION, core_min) < 0:
        raise ModuleError("module needs core %s or newer" % core_min)
    check_member_names(list(mj["files"]))
    check_allowed(mj)
    seen = set()
    for d, dirs, files in os.walk(path):
        for x in dirs + files:
            if os.path.islink(os.path.join(d, x)):
                raise ModuleError("symlink in module tree")
        for x in dirs:
            if x == "__pycache__":
                raise ModuleError("__pycache__ in module tree")
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), path).replace(os.sep, "/")
            if f.lower().endswith((".pyc", ".pyo")):
                raise ModuleError("bytecode in module tree: %s" % rel)
            if rel != "module.json":
                seen.add(rel)
    if seen != set(mj["files"]):
        raise ModuleError("installed files differ from module.json")
    for rel, h in mj["files"].items():
        if sha256_file(os.path.join(path, *rel.split("/"))) != h:
            raise ModuleError("installed file modified: %s" % rel)
    return mj


def extract_verified(data, dest, entry):
    """Unpack the zip BYTES (already sha-checked, never written to disk) into dest. Every file must be listed in module.json with a matching
    sha256, module.json must match the signed entry, nothing else may be in the zip. Then check_tree runs on the result."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise ModuleError("module is not a zip") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ModuleError("module too large")
        check_member_names([i.filename for i in infos])
        for i in infos:
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise ModuleError("symlink in module")
        try:
            raw = zf.read("module.json")
        except KeyError:
            raise ModuleError("module.json missing") from None
        if sha256_bytes(raw) != entry["module_json_sha256"]:
            raise ModuleError("module.json does not match the signed manifest")
        mj = parse_module_json(raw)
        if mj["name"] != entry["name"] or mj["version"] != entry["version"] or mj["api"] != entry["api"]:
            raise ModuleError("module.json does not match the signed manifest")
        if {i.filename for i in infos} != set(mj["files"]) | {"module.json"}:
            raise ModuleError("module contents differ from module.json")
        blobs = {}
        for i in infos:
            blob = zf.read(i)
            if i.filename != "module.json" and sha256_bytes(blob) != mj["files"][i.filename]:
                raise ModuleError("file hash mismatch: %s" % i.filename)
            blobs[i.filename] = blob
    os.makedirs(dest)
    for n, blob in blobs.items():
        p = os.path.join(dest, *n.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(blob)
    return check_tree(dest, entry["name"], entry)


def _meta_paths(root, name, ver):
    base = os.path.join(root, name, ver)
    return base + ".manifest.json", base + ".manifest.sig"


def verify_installed(root, name, ver, pubkeys=None):
    """THE activation check, also used for rollback targets: the stored signed manifest + signature (re-verified with the embedded key),
    the entry for `name` must be exactly this version, then check_tree. -> module.json dict of the verified tree."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name not in ALLOWED:
        raise ModuleError("bad module name")
    if not isinstance(ver, str) or not VER_RE.fullmatch(ver):
        raise ModuleError("bad module version")
    mpath, spath = _meta_paths(root, name, ver)
    try:
        with open(mpath, "rb") as fh:
            data = fh.read(MAX_MANIFEST + 1)
        with open(spath, "r", encoding="ascii") as fh:
            sig = fh.read(1024)
    except (OSError, UnicodeDecodeError):
        raise ModuleError("no signed manifest stored for %s %s" % (name, ver)) from None
    if len(data) > MAX_MANIFEST:
        raise ModuleError("stored manifest too large")
    m = verify_manifest(data, sig, pubkeys)
    entry = m["modules"].get(name)
    if not entry or entry["version"] != ver:
        raise ModuleError("signed manifest does not contain %s %s" % (name, ver))
    return check_tree(os.path.join(root, name, ver), name, dict(entry, name=name))


# ---------------------------------------------------------------------------------------------- loader
class _Finder:
    """meta_path finder (first in line) that serves the module's top-level names from its folder. Needed in the frozen exe, where the
    built-in copies of the same names sit in the PYZ and could win against a plain sys.path entry. Refuses anything resolving outside."""

    def __init__(self, tree, dirs, provides):
        self.tree, self.dirs, self.provides = os.path.realpath(tree), dirs, frozenset(provides)

    def find_spec(self, fullname, path=None, target=None):
        if path is None and fullname in self.provides:
            spec = importlib.machinery.PathFinder.find_spec(fullname, self.dirs)
            origin = getattr(spec, "origin", None)
            if not origin or not os.path.realpath(origin).startswith(self.tree + os.sep):
                raise ImportError("module file outside the verified tree: %s" % fullname)
            return spec
        return None


_ACTIVE = {}          # name -> {"version", "path"} of the modules activated in this process
_ERRORS = {}          # name -> why the installed module was NOT used (built-in copy runs instead)


def active():
    return dict(_ACTIVE)


def errors():
    return dict(_ERRORS)


def _read_state(root):
    """state.json -> {"serial", "mods": {name: {"active","previous"}}}. Names / versions that do not match the strict patterns are
    dropped (listed in "_bad"): a traversal version can never reach a path join, a "rose\\n" name never matches."""
    st = {"serial": 0, "mods": {}, "_bad": []}
    try:
        with open(os.path.join(root, "state.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return st
    if not isinstance(raw, dict) or not isinstance(raw.get("mods"), dict):
        return st
    if isinstance(raw.get("serial"), int) and not isinstance(raw["serial"], bool) and raw["serial"] >= 0:
        st["serial"] = raw["serial"]
    for name, ent in raw["mods"].items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name not in ALLOWED or not isinstance(ent, dict):
            st["_bad"].append("module name %r" % (name,))
            continue
        clean = {}
        for which in ("active", "previous"):
            v = ent.get(which)
            if v is None or v == "":
                continue
            if isinstance(v, str) and VER_RE.fullmatch(v):
                clean[which] = v
            else:
                st["_bad"].append("%s %s %r" % (name, which, v))
        st["mods"][name] = clean
    return st


def activate(root=None, log=None, pubkeys=None):
    """Called once at process start (gsvr_main.main), before companion / gui import rosebuild etc. Returns {name: version} activated.
    Never raises: an unusable module is skipped (reason in errors()) and the built-in copy stays in use. Refuses everything when no
    signing key is embedded."""
    log = log or (lambda *_a: None)
    if not enabled():
        return {}
    keys = PUBKEYS if pubkeys is None else pubkeys
    if not keys:
        _ERRORS["*"] = "no signing key embedded: modules not used"
        return {}
    root = root or modules_root()
    got = {}
    try:
        sys.dont_write_bytecode = True                     # nothing may be written into a verified tree (stale pyc could later be loaded)
        st = _read_state(root)
        for b in st["_bad"]:
            _ERRORS["state"] = "ignored invalid state entry: %s" % b
            log("modules: ignored invalid state entry %s" % b)
        for name, ent in sorted(st["mods"].items()):
            if name in _ACTIVE:
                continue
            for which in ("active", "previous"):             # a broken active version falls back to the kept previous one
                ver = ent.get(which)
                if not ver or (which == "previous" and not ent.get("active")):
                    continue                                   # no active version (disabled) = built-in, never the old one
                try:
                    mj = verify_installed(root, name, ver, keys)
                    path = os.path.join(root, name, ver)
                    dirs = [os.path.join(path, *p.split("/")) if p else path for p in mj["paths"]]
                    for n in mj["provides"]:
                        sys.modules.pop(n, None)               # a built-in copy imported earlier must not mix with the module's (names are allow-listed)
                    sys.meta_path.insert(0, _Finder(path, dirs, mj["provides"]))
                    _ACTIVE[name] = {"version": mj["version"], "path": path}
                    got[name] = mj["version"]
                    if which == "previous":
                        _ERRORS[name] = "active version unusable (%s), running previous %s" % (_ERRORS.get(name, "?"), ver)
                    break
                except ModuleError as e:
                    _ERRORS[name] = "%s %s: %s" % (which, ver, e)
                    log("module %s %s not used: %s" % (name, ver, e))
    except Exception as e:                                    # noqa: BLE001 - startup must never fail because of modules
        log("modules: %r" % (e,))
    return got


# ---------------------------------------------------------------------------------------------- store (install / rollback)
class Store:
    """fetch(url, max_bytes) -> bytes is injectable (tests); the default reads through updater.Client (GitHub only). list_tags() -> modules-N tags."""

    def __init__(self, root=None, fetch=None, list_tags=None, pubkeys=None, url_for=download_url, log=None):
        self.root = root or modules_root()
        self._fetch, self._list_tags = fetch, list_tags
        self.pubkeys, self.url_for = pubkeys, url_for
        self.log = log or (lambda *_a: None)

    # --- state
    def state(self):
        return _read_state(self.root)

    def _save(self, st):
        _write_json(os.path.join(self.root, "state.json"), {k: v for k, v in st.items() if not k.startswith("_")})

    def installed(self, name):
        e = self.state()["mods"].get(name) or {}
        return e.get("active"), e.get("previous")

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name not in ALLOWED:
            raise ModuleError("unknown module %r" % (name,))

    # --- network (only via the two injectable functions)
    def fetch(self, url, max_bytes):
        if self._fetch:
            return self._fetch(url, max_bytes)
        if not url.startswith("https://github.com/%s/%s/releases/download/" % (REPO_OWNER, REPO_NAME)):
            raise ModuleError("refusing a non-official download URL")
        import updater
        try:
            return updater.Client().read(url, max_bytes)
        except updater.UpdateError as e:
            raise ModuleError(str(e)) from None

    def tags(self):
        if self._list_tags:
            return self._list_tags()
        import updater
        try:
            rels = updater.Client().api("/releases?limit=30")
        except Exception as e:                                # noqa: BLE001
            raise ModuleError("cannot list releases: %s" % e) from None
        return [r["tag_name"] for r in rels if isinstance(r, dict) and not r.get("draft") and isinstance(r.get("tag_name"), str)
                and TAG_RE.fullmatch(r["tag_name"])]

    def latest_manifest(self):
        """newest valid signed manifest -> (manifest, tag, raw bytes, sig text). A bad newest tag (broken upload, wrong signature) does not
        block updates: the next valid tag is used (at most 5 tried). The serial must match the tag and never go below the highest seen."""
        best = sorted((t for t in self.tags() if isinstance(t, str) and TAG_RE.fullmatch(t)), key=lambda t: -int(TAG_RE.fullmatch(t).group(1)))
        if not best:
            raise ModuleError("no module release found")
        last = None
        seen = self.state().get("serial", 0)
        for tag in best[:5]:
            try:
                data = self.fetch(self.url_for(tag, MANIFEST_ASSET), MAX_MANIFEST)
                sig = self.fetch(self.url_for(tag, SIG_ASSET), 1024).decode("ascii", "replace")
                m = verify_manifest(data, sig, self.pubkeys)
                if m["serial"] != int(TAG_RE.fullmatch(tag).group(1)):
                    raise ModuleError("manifest serial does not match its release tag")
                if m["serial"] < seen:
                    raise ModuleError("manifest older than one already seen (replay / downgrade refused)")
                return m, tag, data, sig
            except ModuleError as e:
                last = e
                self.log("module release %s skipped: %s" % (tag, e))
        raise last

    def check(self, name):
        """-> dict(status: "none"|"current"|"available"|"incompatible", version, api, message). Downloads the manifest only."""
        self._name(name)
        m = self.latest_manifest()[0]
        e = m["modules"].get(name)
        if not e:
            return {"status": "none", "message": "not in the release"}
        return self._judge(name, e)

    def _judge(self, name, e):
        r = {"version": e["version"], "api": e["api"]}
        if e["api"] != MODULE_API:
            r.update(status="incompatible", message="module api %d, this app speaks %d - update the app" % (e["api"], MODULE_API))
        elif e.get("core_min") and compare_versions(VERSION, e["core_min"]) < 0:
            r.update(status="incompatible", message="needs app %s or newer" % e["core_min"])
        else:
            cur = self.installed(name)[0]
            if cur and compare_versions(e["version"], cur) <= 0:
                r.update(status="current", message="up to date (%s)" % cur)
            else:
                r.update(status="available", message="%s available" % e["version"])
        return r

    def install(self, name):
        """Download + verify + atomically install the newest version of `name`. Takes effect at the next start. -> installed version."""
        self._name(name)
        m, tag, mdata, msig = self.latest_manifest()
        e = m["modules"].get(name)
        if not e:
            raise ModuleError("module %r is not in the release" % name)
        j = self._judge(name, e)
        if j["status"] in ("incompatible", "current"):
            raise ModuleError(j["message"])
        e = dict(e, name=name)
        os.makedirs(self.root, exist_ok=True)
        stage = tempfile.mkdtemp(prefix=".stg-", dir=self.root)      # random name: nothing predictable to pre-plant
        try:
            data = self.fetch(self.url_for(tag, e["file"]), e["size"])
            if len(data) != e["size"] or sha256_bytes(data) != e["sha256"]:
                raise ModuleError("module download does not match the signed manifest (size / sha256)")
            tree = os.path.join(stage, "tree")
            extract_verified(data, tree, e)                          # from memory, verified against the signed entry
            dest = os.path.join(self.root, name, e["version"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            mpath, spath = _meta_paths(self.root, name, e["version"])
            _write_bytes(mpath, mdata)                               # the signed manifest + signature travel with the tree
            _write_bytes(spath, msig.strip().encode("ascii") + b"\n")
            os.replace(tree, dest)                                   # atomic swap into place
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        st = self.state()
        ent = st["mods"].setdefault(name, {})
        old = ent.get("active")
        if old and old != e["version"]:
            ent["previous"] = old
        ent["active"] = e["version"]
        st["serial"] = max(st.get("serial", 0), m["serial"])
        self._save(st)
        self._prune(name, st)
        self.log("module %s %s installed (restart to use it)" % (name, e["version"]))
        return e["version"]

    def rollback(self, name):
        """Switch back to the kept previous version (offline, explicit only). The version rolled away from becomes `previous`.
        The target goes through the full activation check (signed manifest, module.json hash, file hashes)."""
        self._name(name)
        st = self.state()
        ent = st["mods"].get(name) or {}
        prev = ent.get("previous")
        if not prev:
            raise ModuleError("no previous version of %s is kept" % name)
        verify_installed(self.root, name, prev, self.pubkeys)
        ent["previous"], ent["active"] = ent.get("active"), prev
        self._save(st)
        self._prune(name, st)
        self.log("module %s rolled back to %s (restart to use it)" % (name, prev))
        return prev

    def _prune(self, name, st):
        """delete every kept version except active + previous (tree folder and its stored manifest / signature)"""
        ent = st["mods"].get(name) or {}
        keep = {ent.get("active"), ent.get("previous")}
        base = os.path.join(self.root, name)
        for v in os.listdir(base) if os.path.isdir(base) else ():
            ver = v
            for suf in (".manifest.json", ".manifest.sig", ".tmp"):
                if ver.endswith(suf):
                    ver = ver[:-len(suf)]
            if not VER_RE.fullmatch(ver) or ver in keep:
                continue
            p = os.path.join(base, v)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def disable(self, name):
        """forget the active version (keep files): the built-in copy runs again at the next start"""
        self._name(name)
        st = self.state()
        ent = st["mods"].get(name)
        if ent:
            ent["previous"], ent["active"] = ent.get("active"), None
            self._save(st)


# ---------------------------------------------------------------------------------------------- GUI rows
KNOWN = (("rose", "ROSE Online (Konverter)"),)


def rows(store=None):
    """-> [dict(name, title, running, installed, previous, error, can_rollback)] for the GUI list (no network)."""
    store = store or Store()
    out = []
    for name, title in KNOWN:
        act, prev = store.installed(name)
        out.append(dict(name=name, title=title, running=_ACTIVE.get(name, {}).get("version") or "eingebaut",
                        installed=act or "-", previous=prev or "", error=_ERRORS.get(name, ""), can_rollback=bool(prev)))
    return out
