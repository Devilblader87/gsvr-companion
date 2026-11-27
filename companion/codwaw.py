"""Call of Duty: World at War (BYOG) for the companion: install detection, the "codwaw" game entry, the weapon pack and the
map packs. Stdlib only. The converters are the vendored codwaw tools in tools/ (tools/sync_codwaw_converter.sh copies them
verbatim from the codwaw repo, GPL-3.0; tools/codwaw_SOURCE.txt = source commit + sha256 per file). The install finder below
is the codwaw repo's companion draft (companion/codwaw/install.py, same author, GPL-3.0), folded into one module.

Routes (the existing ones, nothing new for the world):
  [GSVR-REQ] models=codwaw req=<id>        -> GET /pack/models.bin = the weapon pack (GSM1 + CSK1 / WDF1 / WDJ1 / SAL1 / CHUD / CDUM,
                                              tools/codwaw_pack.py: Thompson, Kar98k, M1 Garand + the MP soldier dummies)
  [GSVR-REQ] map=codwaw/mp_dome req=<id>   -> GET /pack/current.bin = the map as GSV3 (tools/waw_mappack.py, GsvrPackLoader reads it)
status.json "ownership": {"codwaw": true} once an install with zone/<lang>/common_mp.ff + main/iw_00.iwd was found;
/index.json lists the maps of MAPS that the install has (mp_dome first; mp_castle does not fit 100 MB yet, see the codwaw docs).

An install = main/iw_00.iwd + zone/<language>/common_mp.ff (any language folder: English, German, ...). Places tried, in order:
  1. explicit roots (--codwaw-root), source "manual"
  2. Steam: every library of every Steam install (libraryfolders.vdf), appmanifest_10090.acf (fully installed) ->
     steamapps/common/<installdir>; without a manifest the default folder "Call of Duty World at War", source "steam"
  3. the Windows registry keys of the retail / Activision installer, source "registry"
Nothing is copied: the converters read the files in place, the packs go to the companion cache.
"""
import hashlib, os, shlex, subprocess, sys, time

import builder, buildprog, runtime

MOD = "codwaw"
NAME = "Call of Duty: World at War"
APPID = 10090
STEAM_DIR = "Call of Duty World at War"
MAPS = ("mp_dome",)             # maps the world can load today (GSV3 <= 100 MB); more once the converter has instancing / DXT
STATE_UNINSTALLED, STATE_FULLY_INSTALLED = 1, 4
RETAIL_REG = (("HKLM", r"SOFTWARE\WOW6432Node\Activision\Call of Duty WAW", "InstallPath"),
              ("HKLM", r"SOFTWARE\Activision\Call of Duty WAW", "InstallPath"),
              ("HKLM", r"SOFTWARE\WOW6432Node\Activision\Call of Duty World at War", "InstallPath"),
              ("HKLM", r"SOFTWARE\WOW6432Node\Activision\Call of Duty WAW", "InstallDir"))
STEAM_REG = (("HKCU", r"Software\Valve\Steam", "SteamPath"),
             ("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
             ("HKLM", r"SOFTWARE\Valve\Steam", "InstallPath"))
LANG_ORDER = ("english", "german", "french", "italian", "spanish", "russian", "polish", "japanese")
WEAPON_CONVERTER = "codwaw_pack.py"
MAP_CONVERTER = "waw_mappack.py"
SOURCE_FILE = "codwaw_SOURCE.txt"


def _reg_read(hive, key, value):
    if sys.platform != "win32":
        return None
    try:
        import winreg
        h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        with winreg.OpenKey(h, key) as k:
            v = winreg.QueryValueEx(k, value)[0]
        return v if isinstance(v, str) else None
    except (OSError, ImportError):
        return None


reg_read = _reg_read          # tests replace this
STEAM_ROOTS = None            # tests: a list of Steam roots instead of the detected ones


# ---- minimal KeyValues (VDF / ACF) parser: quoted / bare tokens, nested blocks, // comments; keys lower-cased
def _tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            j, out = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    out.append({"n": "\n", "t": "\t"}.get(text[j + 1], text[j + 1]))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            yield ("s", "".join(out))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            yield ("s", text[i:j])
            i = j


def parse_vdf(text):
    stack, cur, key = [], {}, None
    for t in _tokens(text):
        if t == "{":
            if key is None:
                raise ValueError("block without key")
            new = {}
            cur[key] = new
            stack.append(cur)
            cur, key = new, None
        elif t == "}":
            if not stack:
                raise ValueError("unbalanced }")
            cur, key = stack.pop(), None
        elif key is None:
            key = t[1].lower()
        else:
            cur[key] = t[1]
            key = None
    return cur


def load_vdf(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_vdf(fh.read())


# ---- installs
def languages(root):
    """zone/<lang> folders that hold common_mp.ff, sorted by LANG_ORDER then name."""
    zd = os.path.join(root, "zone")
    try:
        names = [d for d in os.listdir(zd) if os.path.isfile(os.path.join(zd, d, "common_mp.ff"))]
    except OSError:
        return []
    return sorted(names, key=lambda d: (LANG_ORDER.index(d.lower()) if d.lower() in LANG_ORDER else 99, d.lower()))


def is_install(root):
    return bool(root) and os.path.isfile(os.path.join(root, "main", "iw_00.iwd")) and bool(languages(root))


def steam_roots():
    if STEAM_ROOTS is not None:
        return list(STEAM_ROOTS)
    out = []
    for hive, key, val in STEAM_REG:
        v = reg_read(hive, key, val)
        if v:
            out.append(os.path.normpath(v.strip().strip('"')))
    if sys.platform == "win32":
        out.append(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"))
    elif sys.platform == "darwin":
        out.append(os.path.expanduser("~/Library/Application Support/Steam"))
    else:
        out += [os.path.expanduser(p) for p in ("~/.steam/steam", "~/.local/share/Steam")]
    seen, res = set(), []
    for p in out:
        k = os.path.normcase(p)
        if k not in seen and os.path.isdir(os.path.join(p, "steamapps")):
            seen.add(k)
            res.append(p)
    return res


def library_folders(steam_root):
    libs = [os.path.normpath(steam_root)]
    p = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(p):
        try:
            lf = load_vdf(p).get("libraryfolders") or {}
        except (OSError, ValueError):
            lf = {}
        for k, v in lf.items():
            if k.isdigit():
                path = v.get("path") if isinstance(v, dict) else v
                if path and os.path.normpath(path) not in libs:
                    libs.append(os.path.normpath(path))
    return [d for d in libs if os.path.isdir(os.path.join(d, "steamapps"))]


def find_installs(manual=()):
    """-> [{root, source, languages}] every distinct install, manual roots first."""
    found, seen = [], set()

    def add(root, source):
        if not root:
            return
        root = os.path.normpath(root)
        k = os.path.normcase(os.path.realpath(root))
        if k in seen or not is_install(root):
            return
        seen.add(k)
        found.append({"root": root, "source": source, "languages": languages(root)})

    for r in manual or ():
        add(r, "manual")
    for sr in steam_roots():
        for lib in library_folders(sr):
            mf = os.path.join(lib, "steamapps", f"appmanifest_{APPID}.acf")
            if os.path.isfile(mf):
                try:
                    st = load_vdf(mf).get("appstate", {})
                    flags = int(st.get("stateflags", "0") or 0)
                except (OSError, ValueError):
                    st, flags = {}, 0
                if flags & STATE_FULLY_INSTALLED and not flags & STATE_UNINSTALLED:
                    add(os.path.join(lib, "steamapps", "common", st.get("installdir") or STEAM_DIR), "steam")
            add(os.path.join(lib, "steamapps", "common", STEAM_DIR), "steam")
    for hive, key, val in RETAIL_REG:
        add(reg_read(hive, key, val), "registry")
    return found


def pick_language(langs, want="auto"):
    if want and want != "auto":
        for d in langs:
            if d.lower() == want.lower():
                return d
        raise RuntimeError(f"language '{want}' not installed (have: {', '.join(langs)})")
    if not langs:
        raise RuntimeError("no zone/<language>/common_mp.ff in this install")
    return langs[0]


def ownership(manual=None, lang="auto"):
    """-> {"codwaw": {...}} in the shape of steam.ownership_* entries (+ "installs", "lang")."""
    ent = {"appid": APPID, "name": NAME, "allowed": False, "source": "manual-override" if manual else "install",
           "game_root": None, "reason": "no Call of Duty: World at War install found (Steam app 10090 or --codwaw-root)",
           "installs": [], "lang": None}
    try:
        found = find_installs(manual or ())
    except Exception as e:                       # noqa: BLE001 - a broken Steam library must not stop the other games
        found = []
        ent["reason"] = f"install search failed: {str(e)[:120]}"
    for f in found:
        try:
            lang = pick_language(f["languages"], lang)
        except RuntimeError as e:
            ent["reason"] = str(e)
            continue
        ent.update(allowed=True, game_root=f["root"], lang=lang, installs=found,
                   source="manual-override" if f["source"] == "manual" else f["source"],
                   reason=f"{f['source']} install, zone/{lang} ({', '.join(f['languages'])})")
        break
    return {MOD: ent}


def build_index(own):
    """-> {"codwaw": {appid, name, game_root, lang, maps: {name: {path, size, dir}}}} for an allowed install. dir = "main" (the
    iwd folder the map converter reads besides the map's fastfile). Paths come from the install only, never from requests."""
    e = own.get(MOD)
    if not e or not e.get("allowed"):
        return {}
    zd = os.path.join(e["game_root"], "zone", e["lang"])
    maps = {}
    for name in MAPS:
        p = os.path.join(zd, name + ".ff")
        if os.path.isfile(p):
            maps[name] = {"path": p, "size": os.path.getsize(p), "dir": "main", "title": name}
    return {MOD: {"appid": APPID, "name": NAME, "game_root": e["game_root"], "lang": e["lang"], "maps": maps}}


def source_version():
    """sha1 over tools/codwaw_SOURCE.txt (codwaw commit + per-file sha256 of the vendored converter) -> 'c-<12 hex>'; the cache
    version of both codwaw packs, so only a converter sync rebuilds them (not an unrelated tools/ change)."""
    try:
        with open(os.path.join(runtime.tools_dir(), SOURCE_FILE), "rb") as fh:
            return "c-" + hashlib.sha1(fh.read()).hexdigest()[:12]
    except OSError:
        return "c-unknown"


class MapConverter(builder.Converter):
    """tools/waw_mappack.py <map.ff> <out> --iwd <install>/main through builder.Converter (cache, progress tracker, validate_pack
    GSV3 check). mod_dir = <install>/main (index "dir")."""

    def __init__(self, converter=None, timeout=900, frozen=None, python=None):
        conv = converter or os.path.join(runtime.tools_dir(), MAP_CONVERTER)
        frozen = runtime.FROZEN if frozen is None else frozen
        super().__init__(conv, runtime.script_prefix(frozen) + " {bsp} {out} --iwd {mod_dir}", python=python,
                         timeout=timeout, frozen=frozen)

    def _version(self):
        return "v-" + hashlib.sha1((source_version() + "|" + self.template).encode()).hexdigest()[:12]


def weapon_input_files(root, lang):
    """Files the weapon converter reads (cache key): zone/<lang>/common_mp.ff + localized_mp_*.ff, main/*.iwd -> [(rel, size, mtime)]."""
    out = []
    zd = os.path.join(root, "zone", lang)
    for d, test in ((zd, lambda n: n == "common_mp.ff" or (n.startswith("localized_mp_") and n.endswith(".ff"))),
                    (os.path.join(root, "main"), lambda n: n.endswith(".iwd"))):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for fn in names:
            if not test(fn.lower()):
                continue
            pth = os.path.join(d, fn)
            try:
                st = os.stat(pth)
            except OSError:
                continue
            out.append((os.path.relpath(pth, root).replace("\\", "/"), st.st_size, st.st_mtime_ns))
    return out


REQUIRED_CHUNKS = (b"CSK1", b"WDF1", b"SAL1")


def validate_weapons(path):
    """GSM1 arithmetic (models.validate_models) + the GSX1 block with the CoD chunks -> size."""
    import models, struct
    size = models.validate_models(path)
    with open(path, "rb") as fh:
        head = fh.read(80)
        hdr, ver, w, m, s, e, u, i, p, n, aw, ah, mips, nb, _f = struct.unpack_from("<15i", head, 4)
        atlas = sum((aw >> l) * (ah >> l) * 4 for l in range(mips))
        pre = hdr + 4 * (w * 32 + m * 8 + s * 6 + e * 4 + i + u * 2 + p * 6 + n) + atlas
        if nb < 8:
            raise RuntimeError("GSX1 block missing (not a CoD pack)")
        fh.seek(pre + nb - 8)
        tail = fh.read(8)
        blk = struct.unpack_from("<i", tail, 0)[0]
        if tail[4:] != b"GSX1" or not 8 <= blk <= nb or blk % 4:
            raise RuntimeError("GSX1 block missing (not a CoD pack)")
        fh.seek(pre + nb - blk)
        body = fh.read(blk - 8)
    tags, o = set(), 0
    while o + 8 <= len(body):
        sz = struct.unpack_from("<i", body, o + 4)[0]
        if sz < 0 or sz % 4 or o + 8 + sz > len(body):
            raise RuntimeError("GSX1 chunk size invalid")
        tags.add(body[o:o + 4])
        o += 8 + sz
    miss = [t.decode() for t in REQUIRED_CHUNKS if t not in tags]
    if miss:
        raise RuntimeError(f"CoD chunk(s) missing: {', '.join(miss)}")
    return size


class WeaponBuilder:
    """tools/codwaw_pack.py build --game-root <install> --lang <lang> <out> as a subprocess, cached per install language + input
    state + converter source. Same build() signature as models.ModelBuilder, so models.run_job / prewarm drive it."""

    def __init__(self, converter=None, timeout=900, frozen=None, python=None, lang="auto"):
        self.converter = os.path.abspath(converter or os.path.join(runtime.tools_dir(), WEAPON_CONVERTER))
        self.python = python or sys.executable
        self.frozen = runtime.FROZEN if frozen is None else frozen
        self.timeout = timeout
        self.lang = lang
        self.template = runtime.script_prefix(self.frozen) + " build --game-root {game_root} --lang {lang} {out}"
        self.version = "v-" + hashlib.sha1((source_version() + "|" + self.template).encode()).hexdigest()[:12]

    def mod_version(self, mod):
        return self.version

    def argv(self, **kw):
        out = [tok.format(python=self.python, converter=self.converter, **kw) for tok in shlex.split(self.template, posix=True)]
        if out and out[0] == self.python and "-X" not in out[1:3] and not self.frozen:
            out[1:1] = ["-X", "utf8"]
        return out

    def build(self, cache_dir, mod, game_root, log=print):
        t0 = time.monotonic()
        os.makedirs(cache_dir, exist_ok=True)
        lang = pick_language(languages(game_root), self.lang)
        files = weapon_input_files(game_root, lang)
        if not any(r.lower().endswith("/common_mp.ff") for r, _s, _m in files):
            raise RuntimeError(f"zone/{lang}/common_mp.ff not found")
        h = hashlib.sha1()
        for rel, size, mtime in files:
            h.update(f"{rel}\0{size}\0{mtime}\n".encode())
        key = f"codwaw-weapons-{lang.lower()}-{h.hexdigest()[:16]}-{self.version}"
        final = os.path.join(cache_dir, key + ".gsm")
        if os.path.isfile(final):
            try:
                size = validate_weapons(final)
                return dict(path=final, size=size, key=key, cached=True, seconds=round(time.monotonic() - t0, 3))
            except RuntimeError as e:
                log(f"codwaw: cached weapon pack rejected ({e}), rebuilding")
                os.remove(final)
        tmp = os.path.join(cache_dir, key + ".tmp")
        argv = self.argv(game_root=game_root, lang=lang, out=tmp)
        log("codwaw weapons build: " + " ".join(shlex.quote(a) for a in argv))
        try:
            with buildprog.TRACKER.job("models", MOD, "weapons") as job:
                r = buildprog.run(argv, self.timeout, on_line=job.feed, **runtime.low_priority_kwargs(),
                                  cwd=cache_dir, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"codwaw converter timeout after {self.timeout}s")
        except OSError as e:
            raise RuntimeError(f"codwaw converter failed to start: {e}")
        for line in [x for x in (r.stdout + r.stderr).strip().splitlines() if not x.startswith("@")][-4:]:
            log("  codwaw_pack: " + line)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            raise RuntimeError(f"codwaw converter exit {r.returncode}: {tail[0][:200]}")
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError("codwaw converter produced no output")
        try:
            size = validate_weapons(tmp)
        except RuntimeError as e:
            os.remove(tmp)
            raise RuntimeError(f"codwaw weapon pack rejected: {e}")
        os.replace(tmp, final)
        return dict(path=final, size=size, key=key, cached=False, seconds=round(time.monotonic() - t0, 3))


def game_entry(own):
    """GAME-FOLDERS row (games.py) for the GUI table: found / root / source."""
    e = own.get(MOD)
    if e is None:
        return None
    g = {"id": MOD, "name": NAME, "found": bool(e.get("allowed")), "root": "", "source": "none", "mods": [],
         "setting": "codwaw_root", "hint": ""}
    if g["found"]:
        g["root"] = e.get("game_root") or ""
        g["source"] = {"manual-override": "manual", "steam": "steam-manifest", "registry": "registry"}.get(e.get("source"), "install-folder")
        g["mods"] = [e.get("lang") or ""]
    else:
        g["hint"] = "Call of Duty: World at War (Steam) not found: install it or start the companion with --codwaw-root <folder>"
    return g
