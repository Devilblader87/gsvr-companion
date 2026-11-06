"""CS-SHARE: automatic transfer of CUSTOM Counter-Strike maps (and the custom files they need) from the host to the guests
through the same relay as the Warcraft III maps (mapshare.py, relay/relay.py).

  * The host's companion (CsSharer.maybe_share) packs a served non-stock map into ONE bundle (a ZIP with every entry
    STORED): maps/<name>.bsp plus the custom files the map needs - read from the BSP's entity lump (wad list, skyname,
    model / sprite / sound keys) and the optional maps/<name>.res - and uploads it to /v1/cs/<sha256 of the .bsp>. The key is
    the hash of the map, which the host publishes in its index.json ("share", 16 hex) so every world player's companion
    knows it from the map list; the relay re-computes it from the stored entry.
  * A guest's companion that is asked for 'cstrike/<name>' it does not have, together with that key, polls the relay
    (mapshare.wait_on_relay), downloads the bundle, validates it (validate_bundle) and writes maps/<name>.bsp and the
    needed files below <game>/cstrike_downloads/ - the folder the real game and the companion's map index search too, so
    the map also works in the real game.
  * NEVER stock Valve content: stock maps (the classic list) are never packed, stock .wad files (STOCK_WADS) and stock
    skies are never packed or written, and models / sprites / sounds / skies are only packed when they live in a custom
    content folder (cstrike_addon, cstrike_downloads), never when they are in the base folders cstrike / valve - those
    count as stock. Wads of the base folder are packed when they are not on the stock list.

Everything from the network and every BSP is untrusted: the BSP is only PARSED (header, lump bounds, entity text), the
bundle is checked entry by entry (names, STORED only, sizes, 50 MB), the guest re-derives which files the map may bring
from the bundled BSP itself (a bundle cannot carry anything else), never overwrites a file the player already has and
never writes outside <game>/cstrike_downloads. Custom assets that sit inside the base folder cstrike/ count as stock and are NOT
shared - put them in cstrike_downloads/ (or cstrike_addon/). A bundle is installed all-or-nothing as far as the map goes: every entry's
CRC is checked first, the files are staged, then moved into place with the .bsp last.
"""
import hashlib, json, os, re, shutil, struct, sys, tempfile, threading, time, zipfile, zlib

import mapshare, runtime

MODS = ("cstrike",)                              # mods whose custom maps are shared
MAX_BUNDLE_BYTES = mapshare.MAX_MAP_BYTES        # 50 MiB, the relay's pack limit too
BUNDLE_RESERVE = 64 * 1024                       # zip headers
MAX_ENTRIES = 256
MAX_ENT_LUMP = 1 << 20                           # GoldSrc allows 128 KiB of entities; a hostile lump may claim more
MAX_ENTITIES, MAX_KEYS, MAX_REFS = 8192, 96, 200
MAX_RES_BYTES = 64 * 1024
MAX_RES_LINES = 256
BSP_VERSION = 30
BSP_LUMPS = 15
BSP_HEADER = 4 + BSP_LUMPS * 8
KEY16 = re.compile(r"^[0-9a-f]{16}$")

# folders / extensions a bundle may carry (the relay checks the same: relay.CS_TOPDIRS)
TOPDIRS = {"maps": (".bsp", ".txt", ".res"), "overviews": (".bmp", ".txt", ".tga"), "models": (".mdl",), "sprites": (".spr",),
           "sound": (".wav",), "gfx": (".tga", ".bmp")}
SEG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-.]*$")
SKY_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
SKY_SIDES = ("bk", "dn", "ft", "lf", "rt", "up")
# Windows device names: a file called con.txt / nul.bsp / com1.wad is a device there, whatever the extension (relay.cs_name_ok: same list)
WIN_DEVICES = frozenset(["con", "prn", "aux", "nul"] + ["com%d" % i for i in range(1, 10)] + ["lpt%d" % i for i in range(1, 10)])
ZIP_ERRORS = (zipfile.BadZipFile, EOFError, RuntimeError, zlib.error, NotImplementedError)   # a damaged / odd zip is bad DATA (ValueError)


def is_device_seg(seg):
    return seg.split(".", 1)[0].lower() in WIN_DEVICES

# the maps Counter-Strike 1.6 ships (Steam, cstrike/maps) - never packed, whatever folder they sit in
CLASSIC = frozenset("""as_oilrig cs_747 cs_assault cs_backalley cs_estate cs_havana cs_italy cs_militia cs_office cs_siege
de_airstrip de_aztec de_cbble de_chateau de_dust de_dust2 de_inferno de_nuke de_piranesi de_prodigy de_storm de_survivor de_torn
de_train de_vertigo""".split())

# Valve's own .wad files of cstrike / valve (names only; the companion cache dir may add more in cs_stock_extra.txt)
STOCK_WADS = frozenset("""cstrike.wad halflife.wad liquids.wad xeno.wad decals.wad fonts.wad gfx.wad pldecal.wad tempdecal.wad
spraypaint.wad cached.wad itsitaly.wad cs_747.wad cs_assault.wad cs_bdog.wad cs_cbble.wad cs_dust.wad cs_havana.wad cs_italy.wad
cs_office.wad cs_siege.wad cs_backalley.wad cs_estate.wad cs_militia.wad de_aztec.wad de_cbble.wad de_chateau.wad de_dust.wad
de_inferno.wad de_nuke.wad de_piranesi.wad de_prodigy.wad de_storm.wad de_survivor.wad de_torn.wad de_train.wad de_vertigo.wad
de_airstrip.wad as_oilrig.wad dust.wad aztec.wad nuke.wad torn.wad prodigy.wad chateau.wad""".split())

STOCK_SKIES = frozenset("""2desert alien1 alien2 alien3 black city cliff desert desnoon dusk morning neb1 neb2 neb6 neb7 night
space xen8 xen9 xen10 cx doom1 hav jungle wasteland""".split())

SOUND_KEYS = ("message", "noise", "noise1", "noise2", "noise3", "movesnd", "stopsnd", "startsnd", "soundlist")
MODEL_KEYS = ("model", "weaponmodel", "viewmodel", "playermodel")
SPRITE_KEYS = ("model", "texture", "spritename", "sprite", "trailsprite")


class NotShareable(Exception):
    """the map is stock / too big / not a BSP: nothing to share (not an error)"""


# ----------------------------------------------------------------------------------------- names
def safe_rel(rel):
    """Bundle / dependency path: relative, '/'-separated, <= 120 chars, 1-6 segments of [A-Za-z0-9_-.] (no leading dot,
    no '..', no Windows device name like con / nul / com1), a known folder + extension, or a .wad in the root. Same rules as relay.cs_name_ok."""
    if not isinstance(rel, str) or not rel or len(rel) > 120 or rel.startswith("/") or rel.endswith("/"):
        return False
    segs = rel.split("/")
    if len(segs) > 6 or any(not SEG.match(g) or ".." in g or g.endswith(".") or is_device_seg(g) for g in segs):
        return False
    ext = os.path.splitext(segs[-1])[1].lower()
    if len(segs) == 1:
        return ext == ".wad"
    return segs[0].lower() in TOPDIRS and ext in TOPDIRS[segs[0].lower()]


def norm_ref(v, prefix, exts):
    """An entity / .res value -> 'prefix/rest' (lower case) when it is a plain relative path with one of exts, else None."""
    if not isinstance(v, str) or not v or len(v) > 118:
        return None
    s = v.replace("\\", "/").strip().lstrip("/").lower()
    if not s.startswith(prefix + "/"):
        if prefix != "sound":                           # sound keys are relative to sound/ ("ambience/wind.wav")
            return None
        s = "sound/" + s
    if not s.endswith(exts) or not safe_rel(s):
        return None
    return s


def is_stock_map(name, extra=frozenset()):
    return name.lower() in CLASSIC or name.lower() in extra


def load_extra_stock(cache_dir):
    """<cache dir>/cs_stock_extra.txt: more stock names (wads, skies, maps) and sha256 hashes, one per line, lower-cased"""
    out = set()
    if cache_dir:
        try:
            with open(os.path.join(cache_dir, "cs_stock_extra.txt"), encoding="utf-8") as fh:
                for l in fh:
                    l = l.strip().lower()
                    if l and not l.startswith("#") and len(l) <= 80:
                        out.add(l)
        except OSError:
            pass
    return frozenset(out)


# ----------------------------------------------------------------------------------------- BSP (parse only)
class BspInfo:
    def __init__(self):
        self.wads, self.sky = [], ""
        self.models, self.sprites, self.sounds = [], [], []
        self.entities = 0


def parse_entities(text):
    """Entity lump text -> list of {key: value} (bounded: entity / key counts, key and value lengths)"""
    out, cur = [], None
    for m in re.finditer(r'\{|\}|"([^"\r\n]{0,64})"[ \t]*"([^"\r\n]{0,512})"', text):
        tok = m.group(0)
        if tok == "{":
            cur = {}
        elif tok == "}":
            if cur is not None:
                out.append(cur)
                if len(out) >= MAX_ENTITIES:
                    break
            cur = None
        elif cur is not None and len(cur) < MAX_KEYS:
            cur[m.group(1).lower()] = m.group(2)
    return out


def _looks_like_entities(data, off, ln):
    """True when the lump is entity text: inside the file, first non-blank byte '{', first 8 bytes printable."""
    if ln <= 0 or off < 0 or off + ln > len(data):
        return False
    head = data[off:off + min(ln, 8)]
    return head.lstrip()[:1] == b"{" and all(c in (9, 10, 13, 0) or 32 <= c < 127 for c in head)


def bsp_lump_dir(data):
    """The 15 (offset, length) lump entries of a BSP v30 in canonical order. Blue Shift's maps are v30 but store
    LUMP_ENTITIES and LUMP_PLANES swapped (the engine swaps them back): entry 0 is not entity text but entry 1 is -> swap.
    Same rule as tools/bsp30.lump_dir (companion/test_csshare.py checks both agree)."""
    d = [struct.unpack_from("<ii", data, 4 + 8 * i) for i in range(BSP_LUMPS)]
    if not _looks_like_entities(data, *d[0]) and _looks_like_entities(data, *d[1]):
        d[0], d[1] = d[1], d[0]
    return d


def bsp_entity_text(data):
    """Header + lump bounds + entity text of a GoldSrc BSP v30. Raises ValueError. Nothing else is read."""
    if len(data) < BSP_HEADER or len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("not a BSP (size)")
    if struct.unpack_from("<i", data, 0)[0] != BSP_VERSION:
        raise ValueError("not a BSP v30")
    lumps = bsp_lump_dir(data)
    for off, ln in lumps:                               # the engine refuses lumps outside the file too
        if off < 0 or ln < 0 or off + ln > len(data):
            raise ValueError("BSP lump out of range")
    off, ln = lumps[0]
    if ln > MAX_ENT_LUMP:
        raise ValueError("BSP entity lump too large")
    return data[off:off + ln].split(b"\0", 1)[0].decode("latin-1")


def parse_bsp(data):
    """-> BspInfo with the files the map names: wad list, skyname, model / sprite / sound references"""
    ents = parse_entities(bsp_entity_text(data))
    info = BspInfo()
    info.entities = len(ents)
    for e in ents:
        if e.get("classname", "").lower() == "worldspawn":
            for w in e.get("wad", "").replace("\\", "/").split(";"):
                b = w.strip().split("/")[-1].lower()
                if b.endswith(".wad") and SEG.match(b) and ".." not in b and b not in info.wads and len(info.wads) < 64:
                    info.wads.append(b)
            sk = e.get("skyname", "").strip()
            if SKY_NAME.match(sk) and not info.sky:
                info.sky = sk.lower()
        for k in MODEL_KEYS:
            r = norm_ref(e.get(k, ""), "models", (".mdl",))
            if r and r not in info.models and len(info.models) < MAX_REFS:
                info.models.append(r)
        for k in SPRITE_KEYS:
            r = norm_ref(e.get(k, ""), "sprites", (".spr",))
            if r and r not in info.sprites and len(info.sprites) < MAX_REFS:
                info.sprites.append(r)
        for k in SOUND_KEYS:
            r = norm_ref(e.get(k, ""), "sound", (".wav",))
            if r and r not in info.sounds and len(info.sounds) < MAX_REFS:
                info.sounds.append(r)
    return info


def parse_res(text):
    """maps/<name>.res -> relative paths it lists (bounded; every one is checked again by wanted())"""
    out = []
    for line in text.splitlines()[:MAX_RES_LINES]:
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        tok = line.split('"')[1] if line.startswith('"') and line.count('"') >= 2 else line.split()[0]
        s = tok.replace("\\", "/").strip().lstrip("/").lower()
        if safe_rel(s) and s not in out:
            out.append(s)
    return out


def wanted(info, name, res_lines=(), extra=frozenset()):
    """The files a map may bring, in a fixed order: [(rel, kind)] with kind map | extra | wad | sky | asset. Stock wads and skies
    are not in it. Derived from the BSP (and its .res) only - the host packs these, the guest accepts exactly these."""
    out, seen = [], set()

    def add(rel, kind):
        if rel.lower() not in seen and safe_rel(rel):
            seen.add(rel.lower())
            out.append((rel, kind))
    add("maps/%s.bsp" % name, "map")
    for rel in ("maps/%s.txt" % name, "maps/%s.res" % name, "maps/%s_detail.txt" % name,
                "overviews/%s.bmp" % name, "overviews/%s.txt" % name, "overviews/%s.tga" % name):
        add(rel, "extra")
    for w in info.wads:
        if w not in STOCK_WADS and w not in extra:
            add(w, "wad")
    if info.sky and info.sky not in STOCK_SKIES and info.sky not in extra:
        for side in SKY_SIDES:
            add("gfx/env/%s%s.tga" % (info.sky, side), "sky")
    for rel in sorted(info.models) + sorted(info.sprites) + sorted(info.sounds):
        add(rel, "asset")
    for rel in res_lines:
        if rel.endswith(".wad"):
            if rel not in STOCK_WADS and rel not in extra:
                add(rel, "wad")
        elif rel.startswith(("models/", "sprites/", "sound/")) or rel.startswith("gfx/env/"):
            add(rel, "sky" if rel.startswith("gfx/env/") else "asset")
    return out


# ----------------------------------------------------------------------------------------- HOST: plan + bundle
def _gamedirs():
    t = runtime.tools_dir()
    if t not in sys.path:
        sys.path.insert(0, t)
    import gamedirs
    return gamedirs


class Plan:
    def __init__(self, name, sha):
        self.name, self.sha = name, sha
        self.files, self.skipped, self.total = [], [], 0      # files: [(rel, abs path, size)]


def _find(dirs, rel):
    """first content folder holding rel -> (abs path, folder) or None. rel is already safe_rel-checked; symlinks are not followed
    out of the folder."""
    for d in dirs:
        p = os.path.join(d, *rel.split("/"))
        try:
            if os.path.isfile(p) and os.path.realpath(p).startswith(os.path.realpath(d) + os.sep):
                return p, d
        except OSError:
            continue
    return None


def plan_bundle(bsp_path, game_root, mod="cstrike", extra=frozenset(), limit=MAX_BUNDLE_BYTES):
    """What to pack for the map at bsp_path. Raises NotShareable (stock map, too big, unreadable BSP)."""
    name = os.path.splitext(os.path.basename(bsp_path))[0].lower()
    import steam
    if not steam.safe_map_name(name):
        raise NotShareable("unsafe map name")
    if is_stock_map(name, extra):
        raise NotShareable("stock map")
    size = os.path.getsize(bsp_path)
    if size > limit - BUNDLE_RESERVE or size < BSP_HEADER:
        raise NotShareable("map larger than the limit" if size > BSP_HEADER else "not a BSP")
    with open(bsp_path, "rb") as fh:
        data = fh.read()
    sha = hashlib.sha256(data).hexdigest()
    if sha in extra:
        raise NotShareable("stock map (hash)")
    try:
        info = parse_bsp(data)
    except ValueError as e:
        raise NotShareable(str(e))
    gd = _gamedirs()
    dirs = gd.content_dirs(game_root, mod, fallback="valve")
    custom = {d for d in dirs if os.path.basename(d).lower() in (mod + "_addon", mod + "_downloads")}
    plan = Plan(name, sha)
    plan.files.append(("maps/%s.bsp" % name, bsp_path, size))
    plan.total = size
    # the .res first (it names more files), read from where the map lives, then the custom folders
    res_lines = []
    hit = _find(dirs, "maps/%s.res" % name)
    if hit:
        try:
            with open(hit[0], "rb") as fh:
                res_lines = parse_res(fh.read(MAX_RES_BYTES).decode("latin-1"))
        except OSError:
            pass
    for rel, kind in wanted(info, name, res_lines, extra)[1:]:
        hit = _find(dirs, rel)
        if not hit:
            continue
        path, folder = hit
        if kind == "asset" or kind == "sky":
            if folder not in custom:
                plan.skipped.append((rel, "base folder = stock"))
                continue
        try:
            fs = os.path.getsize(path)
        except OSError:
            continue
        if plan.total + fs > limit - BUNDLE_RESERVE or len(plan.files) >= MAX_ENTRIES - 1:
            plan.skipped.append((rel, "bundle limit"))
            continue
        plan.files.append((rel, path, fs))
        plan.total += fs
    return plan


def build_bundle(plan, out_path):
    """Deterministic STORED zip of the plan (fixed time stamps, fixed order): the same files always give the same bytes."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for rel, path, size in plan.files:
            zi = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            zi.file_size = size
            with open(path, "rb") as src, zf.open(zi, "w") as dst:
                shutil.copyfileobj(src, dst, 1 << 16)
    return os.path.getsize(out_path)


class ShareKeys:
    """16-hex keys (SHA-256 prefix of the .bsp) of the host's custom maps for index.json - cached by (path, size, mtime) in memory
    and in <cache dir>/cs_share_keys.json so a rescan never re-hashes an unchanged map."""
    def __init__(self, cache_dir=None, extra=frozenset()):
        self.path = os.path.join(cache_dir, "cs_share_keys.json") if cache_dir else None
        self.extra = extra
        self.lock = threading.Lock()
        self.cache = {}
        if self.path:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, dict):
                    self.cache = {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str) and KEY16.match(v)}
            except (OSError, ValueError):
                pass
        self.dirty = False

    @staticmethod
    def cache_key(path):
        """-> (cache key, size) or None when the file cannot be stat'ed"""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return "%s|%d|%d" % (os.path.normcase(os.path.abspath(path)), st.st_size, int(st.st_mtime)), st.st_size

    def key(self, name, path, compute=True):
        """-> 16 hex or None (stock map, too big, unreadable, or - with compute=False - not hashed yet). A hash is only computed when
        asked to (compute=True): the index scan itself never reads map files."""
        if is_stock_map(name, self.extra):
            return None
        ck = self.cache_key(path)
        if ck is None or ck[1] > MAX_BUNDLE_BYTES - BUNDLE_RESERVE:
            return None
        k = ck[0]
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        if not compute:
            return None
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
        except OSError:
            return None
        v = h.hexdigest()[:16]
        with self.lock:
            self.cache[k] = v
            self.dirty = True
        return v

    def prune(self, live_keys):
        """drop cache entries of maps that are gone / changed (live_keys = cache_key()s of the maps the last scan found)"""
        with self.lock:
            stale = [k for k in self.cache if k not in live_keys]
            for k in stale:
                del self.cache[k]
            if stale:
                self.dirty = True
        return len(stale)

    def save(self):
        if not self.path or not self.dirty:
            return
        with self.lock:
            data = json.dumps(self.cache, sort_keys=True)
            self.dirty = False
        try:
            fd, tmp = tempfile.mkstemp(prefix=".k", dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)
        except OSError:
            pass


class CsSharer:
    """Host side, same shape as mapshare.Sharer: fire-and-forget upload of a served custom map's bundle."""
    def __init__(self, relay_url, cache_dir, log=print):
        self.parts, self.cache_dir, self.log = mapshare.parse_relay_url(relay_url), cache_dir, log
        self.done, self.busy, self.lock = {}, set(), threading.Lock()
        self.extra = load_extra_stock(cache_dir)

    def maybe_share(self, bsp_path, game_root, mod="cstrike"):
        if not self.parts or mod not in MODS or not bsp_path:
            return None
        key = os.path.normcase(os.path.abspath(bsp_path))
        with self.lock:
            t = self.done.get(key)
            if key in self.busy or (t is not None and (t == 0 or time.time() - t < 600)):
                return None
            self.busy.add(key)
        th = threading.Thread(target=self._run, args=(key, bsp_path, game_root, mod), daemon=True)
        th.start()
        return th

    def _run(self, key, bsp_path, game_root, mod):
        ok = None
        tmp = None
        try:
            plan = plan_bundle(bsp_path, game_root, mod, self.extra)
            base_only = [r for r, why in plan.skipped if why == "base folder = stock"]
            if base_only:
                self.log("cs relay: %s names %d file(s) that exist only in cstrike/ and are NOT shared (put custom assets in "
                         "cstrike_downloads/ or cstrike_addon/): %s" % (plan.name, len(base_only), ", ".join(base_only[:6])))
            st = mapshare.relay_head(self.parts, plan.sha, "cs")
            if st == 200:
                ok = time.time()
                return
            if st != 404:
                self.log("cs relay: HEAD %s -> %s" % (plan.sha[:16], st))
                return
            os.makedirs(self.cache_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="csbundle-", suffix=".zip", dir=self.cache_dir)
            os.close(fd)
            size = build_bundle(plan, tmp)
            st = mapshare.relay_put(self.parts, plan.sha, tmp, kind="cs")
            self.log("cs relay: shared %s (%s, %d files, %d KB; PUT -> %s)%s" % (
                plan.name, plan.sha[:16], len(plan.files), size >> 10, st,
                ("; left out: " + ", ".join("%s (%s)" % s for s in plan.skipped[:6])) if plan.skipped else ""))
            ok = time.time() if st in (200, 201) else None
            if st == 409:
                self.log("cs relay: %s is already on the relay with a different bundle (first upload wins until it expires)" % plan.name)
        except NotShareable as e:
            self.log("cs relay: %s not shared (%s)" % (os.path.basename(bsp_path), e))
            ok = 0
        except Exception as e:                                  # noqa: BLE001 - relay trouble never touches the game
            self.log("cs relay: share %s failed: %s" % (os.path.basename(bsp_path), str(e)[:100]))
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            with self.lock:
                self.busy.discard(key)
                self.done[key] = ok if ok is not None else time.time() - 540


# ----------------------------------------------------------------------------------------- GUEST: validate + install
class BundleInfo:
    def __init__(self):
        self.name, self.bsp, self.entries, self.sha = "", None, {}, ""


def _validate(path, key16):
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise ValueError("bundle entry count")
        bi, total = BundleInfo(), 0
        for zi in infos:
            if not safe_rel(zi.filename):
                raise ValueError("bundle entry name")
            low = zi.filename.lower()
            if low in bi.entries:
                raise ValueError("duplicate bundle entry")
            if zi.compress_type != zipfile.ZIP_STORED or zi.flag_bits & 1 or zi.file_size != zi.compress_size:
                raise ValueError("bundle entry must be stored")
            total += zi.file_size
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("bundle content too large")
            bi.entries[low] = zi
            if low.startswith("maps/") and low.endswith(".bsp"):
                if low.count("/") != 1:
                    raise ValueError("the map must be maps/<name>.bsp")
                if bi.bsp is not None:
                    raise ValueError("more than one map in the bundle")
                bi.bsp = zi
        if bi.bsp is None:
            raise ValueError("no map in the bundle")
        import steam
        bi.name = os.path.splitext(bi.bsp.filename.split("/")[-1])[0].lower()
        if not steam.safe_map_name(bi.name):
            raise ValueError("unsafe map name")
        data = zf.read(bi.bsp)                                     # stored: exactly file_size bytes, CRC checked
        bi.sha = hashlib.sha256(data).hexdigest()
        if key16 and not bi.sha.startswith(key16):
            raise ValueError("bundle map does not match its key")
        parse_bsp(data)                                            # header + lump bounds + entities
    return bi


def validate_bundle(path, key16=None):
    """Structure + the map (exactly maps/<name>.bsp). Raises ValueError - also for a broken / truncated / CRC-bad zip, so the caller's
    fetch_failed back-off applies. -> BundleInfo (entries: lower-case rel -> ZipInfo)."""
    try:
        return _validate(path, key16)
    except ValueError:
        raise
    except ZIP_ERRORS + (OSError,) as e:
        raise ValueError("not a map bundle (%s)" % (str(e) or type(e).__name__)[:60])


def read_entry(path, bi, rel, limit):
    with zipfile.ZipFile(path) as zf:
        zi = bi.entries.get(rel.lower())
        if zi is None or zi.file_size > limit:
            return None
        return zf.read(zi)


def _copy_entry(zf, zi, out):
    """stream one entry (CRC is checked by zipfile at its end; BadZipFile on a mismatch); out may be None = verify only"""
    got = 0
    with zf.open(zi) as src:
        while True:
            b = src.read(1 << 16)
            if not b:
                break
            got += len(b)
            if got > zi.file_size:
                raise ValueError("bundle entry longer than declared")
            if out is not None:
                out.write(b)
    if got != zi.file_size:
        raise ValueError("bundle entry shorter than declared")


def install_bundle(path, game_root, mod="cstrike", expect_name=None, key16=None, extra=frozenset(), log=print):
    """Write the bundle's map + the files that map may bring into <game_root>/<mod>_downloads. Files the player already has (in any
    content folder) are never replaced. -> list of written rel paths. Raises ValueError (a bad bundle writes nothing).

    Order: (1) validate, (2) read every entry to be written once to check its CRC - nothing is written yet, (3) stage all of them in a
    temp folder below <mod>_downloads, (4) move them into place with the .bsp LAST. A crash / disk error in step 4 leaves nothing or
    a set without the map; the next attempt sees the map missing and repairs it. The map must end up present (written now or already
    installed), otherwise ValueError."""
    try:
        return _install(path, game_root, mod, expect_name, key16, extra, log)
    except ValueError:
        raise
    except ZIP_ERRORS as e:
        raise ValueError("bundle could not be installed (%s)" % (str(e) or type(e).__name__)[:60])


def _install(path, game_root, mod, expect_name, key16, extra, log):
    bi = validate_bundle(path, key16)
    if expect_name and bi.name != expect_name.lower():
        raise ValueError("bundle holds another map")
    map_rel = "maps/%s.bsp" % bi.name
    with zipfile.ZipFile(path) as zf:
        bsp_data = zf.read(bi.bsp)
    info = parse_bsp(bsp_data)
    res_text = read_entry(path, bi, "maps/%s.res" % bi.name, MAX_RES_BYTES)
    allowed = {rel.lower() for rel, _k in wanted(info, bi.name, parse_res(res_text.decode("latin-1")) if res_text else (), extra)}
    gd = _gamedirs()
    existing = gd.content_dirs(game_root, mod, fallback="valve")
    root = os.path.join(game_root, mod + "_downloads")
    todo = []                                                      # entries to write, the .bsp last
    for low, zi in bi.entries.items():
        if low not in allowed:
            log("cs relay: bundle entry %s is not something this map needs - skipped" % zi.filename)
        elif _find(existing, zi.filename):
            log("cs relay: %s already installed - kept" % zi.filename)
        else:
            todo.append(zi)
    todo.sort(key=lambda z: z.filename.lower() == map_rel)
    if not todo and not _find(existing, map_rel):
        raise ValueError("the bundle's map is neither written nor already installed")
    os.makedirs(root, exist_ok=True)
    rroot = os.path.realpath(root)
    for zi in todo:                                                # (2) nothing is written until every entry is known to be intact
        dest = os.path.join(root, *zi.filename.split("/"))
        if not os.path.realpath(dest).startswith(rroot + os.sep):
            raise ValueError("bundle path leaves the downloads folder")
    with zipfile.ZipFile(path) as zf:
        for zi in todo:
            _copy_entry(zf, zi, None)
        stage = tempfile.mkdtemp(prefix=".cs-stage-", dir=root)
        try:
            for zi in todo:                                        # (3) stage
                sp = os.path.join(stage, *zi.filename.split("/"))
                os.makedirs(os.path.dirname(sp), exist_ok=True)
                with open(sp, "wb") as out:
                    _copy_entry(zf, zi, out)
            written = []
            for zi in todo:                                        # (4) move into place, .bsp last
                dest = os.path.join(root, *zi.filename.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(os.path.join(stage, *zi.filename.split("/")), dest)
                written.append(zi.filename)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    if map_rel not in written and not _find(gd.content_dirs(game_root, mod, fallback="valve"), map_rel):
        raise ValueError("the bundle's map is neither written nor already installed")
    return written


def fetch_cs(relay_url, key16, name, game_root, mod, cache_dir, progress=None, cancel=None, log=print,
             timeout=mapshare.FETCH_TIMEOUT_S, sleep=time.sleep, clock=time.monotonic, poll=mapshare.POLL_S, extra=frozenset()):
    """Wait for the host's upload, download, validate, install. -> list of written rel paths.
    Raises LookupError (not on the relay in time / nowhere to write), mapshare.Cancelled, ValueError (bad data)."""
    parts = mapshare.parse_relay_url(relay_url)
    if not parts or not KEY16.match(key16) or mod not in MODS:
        raise LookupError("no usable relay")
    mapshare.wait_on_relay(parts, key16, progress, cancel, timeout, sleep, clock, poll, kind="cs")
    dest = os.path.join(game_root, mod + "_downloads")
    try:
        os.makedirs(dest, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".w", dir=dest)
        os.close(fd)
        os.remove(probe)
    except OSError as e:
        raise LookupError("cannot write to %s: %s" % (dest, str(e)[:60]))
    tmpdir = os.path.join(cache_dir, "csdl")
    os.makedirs(tmpdir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".part-", dir=tmpdir)
    os.close(fd)
    try:
        if progress:
            progress(0, "download")
        meta = {}
        try:
            st, digest = mapshare.relay_download(parts, key16, tmp, lambda p: progress and progress(p, "download"), cancel, meta, kind="cs")
        except OSError as e:
            raise LookupError("download failed: %s" % str(e)[:80])
        if st != 200:
            raise LookupError("relay answered %s" % st)
        want = meta.get("bundle_sha")                          # the bundle's own hash, bound by the relay at the first upload
        if want and want != digest:
            raise ValueError("downloaded bundle does not match the relay's record")
        written = install_bundle(tmp, game_root, mod, name, key16, extra, log)
        if progress:
            progress(100, "download")
        log("cs relay: fetched %s (%s): %s" % (name, key16, ", ".join(written[:8]) + (" ..." if len(written) > 8 else "")))
        return written
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
