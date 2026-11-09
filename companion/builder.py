"""Runs the converter CLI (tools/pack.py) as a subprocess and caches packs.

Cache key = sha1(bsp bytes) + converter version (+ mod), so a converter update or a changed map
invalidates automatically. Packs are written to <cache>/<key>.tmp and renamed, so a served file is
never partially written.

Command template placeholders: {python} {converter} {bsp} {out} {mod} {map} {game_root} {mod_dir}.
Default template is auto-detected from `pack.py --help`:
  base:                    {python} -I {converter} --bsp {bsp} {out}
                           (frozen build: {python} --run-converter {converter} --bsp {bsp} {out}, {python} = gsvr-cli)
  + if help has --game-root:  --game-root {game_root}
  + if help has --mod:        --mod {mod}
"""
import re
import hashlib, os, shlex, struct, subprocess, sys

import buildprog, runtime

MAX_PACK = 100 * 1024 * 1024      # VRChat string/byte download limit


def validate_pack(path):
    """Checks a pack file before it is served -> size. Raises RuntimeError with the reason.
    Magic GSV<digit>, size <= MAX_PACK; for GSV2/GSV3 also that the header's section sizes fit the file
    (same arithmetic as unity/Assets/GoldSrcVR/Scripts/GsvrPackLoader.cs); for GSV3 additionally the section
    directory (count, 4-byte aligned entries that lie inside the file, MODL present)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError as e:
        raise RuntimeError(f"pack unreadable: {e}")
    if size == 0:
        raise RuntimeError("pack is empty")
    if head[:4] == b"W3P1":
        return _validate_w3p(path, size, head)
    if head[:3] != b"GSV" or not head[3:4].isdigit():
        raise RuntimeError(f"bad magic {head[:4]!r} (expected GSVn)")
    if size > MAX_PACK:
        raise RuntimeError(f"pack too large for VRChat ({size} > {MAX_PACK} bytes)")
    if head[:4] in (b"GSV2", b"GSV3"):
        ver = head[:4].decode()
        if len(head) < 64:
            raise RuntimeError(f"{ver} header truncated")
        hdr, v, i, _s, aw, ah, mips, lw, lh, ents = struct.unpack_from("<10i", head, 4)
        if hdr < 64 or v <= 0 or i <= 0 or i % 3 or aw <= 0 or ah <= 0 or not 1 <= mips <= 16 or lw <= 0 or lh <= 0 or ents < 0:
            raise RuntimeError(f"{ver} header values out of range")
        atlas = sum((aw >> l) * (ah >> l) * 4 for l in range(mips))
        need = hdr + (v * 11 + i) * 4 + atlas + lw * lh * 4 + ents
        if need > size:
            raise RuntimeError(f"{ver} pack truncated: header needs {need} bytes, file has {size}")
        if ver == "GSV3":
            wv, wi, dir_off = struct.unpack_from("<3i", head, 52)
            if not (0 <= wv <= v and 0 <= wi <= i and wi % 3 == 0) or dir_off % 4 or dir_off < need or dir_off + 4 > size:
                raise RuntimeError("GSV3 header world counts / directory offset out of range")
            with open(path, "rb") as fh:
                fh.seek(dir_off)
                n, = struct.unpack("<i", fh.read(4))
                if not 1 <= n <= 64 or dir_off + 4 + 16 * n > size:
                    raise RuntimeError("GSV3 section directory out of range")
                tags = set()
                for k in range(n):
                    tag, off, nbytes, cnt = struct.unpack("<4s3i", fh.read(16))
                    if off % 4 or nbytes % 4 or off < dir_off or nbytes < 0 or cnt < 0 or off + nbytes > size:
                        raise RuntimeError(f"GSV3 section {tag!r} out of range (pack truncated?)")
                    tags.add(tag)
                if b"MODL" not in tags:
                    raise RuntimeError("GSV3 pack without MODL section")
    return size


W3P_SECTIONS = (b"INFO", b"TATL", b"TVTX", b"TIDX", b"TLAY")      # sections the world loader needs


def _validate_w3p(path, size, head):
    """W3P1 (Warcraft III pack, tools/w3pack.py): section count, 4-byte aligned sections inside the file, the
    sections the loader needs present."""
    if size > MAX_PACK:
        raise RuntimeError(f"pack too large for VRChat ({size} > {MAX_PACK} bytes)")
    if len(head) < 16:
        raise RuntimeError("W3P1 header truncated")
    ver, n = struct.unpack_from("<2i", head, 4)
    if ver != 1 or not 1 <= n <= 128 or 16 + 16 * n > size:
        raise RuntimeError("W3P1 header values out of range")
    with open(path, "rb") as fh:
        fh.seek(16)
        d = fh.read(16 * n)
    tags = set()
    for k in range(n):
        tag, off, nbytes, _cnt = struct.unpack_from("<4s3i", d, 16 * k)
        if off % 4 or off < 16 + 16 * n or nbytes < 0 or off + nbytes > size:
            raise RuntimeError(f"W3P1 section {tag!r} out of range")
        tags.add(tag)
    need = (b"JGL1", b"UIAT") if b"JGL1" in tags else W3P_SECTIONS   # the start-menu pack (war3/__glue) has no map
    missing = [t.decode() for t in need if t not in tags]
    if missing:
        raise RuntimeError("W3P1 sections missing: " + ",".join(missing))
    return size


VERSION_TAIL = re.compile(r"-([vs]-[0-9a-f]{12})\.bin$")


def prune_cache(cache_dir, keep_versions, log=print, spare=()):
    """Delete cached packs built by another converter version (after a companion update they can never be hit
    again: the version is part of every cache key). Files without a version tail (prebuilt, war3_fan_sha.json …)
    stay, and so do files starting with one of spare (a converter that is not active right now, e.g. no WC3
    install found this start: its packs must survive). -> (files removed, bytes freed)."""
    keep = set(v for v in keep_versions if v)
    n = freed = 0
    if not keep or not os.path.isdir(cache_dir):
        return 0, 0
    for fn in os.listdir(cache_dir):
        m = VERSION_TAIL.search(fn)
        if not m or m.group(1) in keep or fn.startswith(tuple(spare)):
            continue
        p = os.path.join(cache_dir, fn)
        try:
            sz = os.path.getsize(p)
            os.remove(p)
        except OSError:
            continue
        n += 1
        freed += sz
    if n:
        log(f"cache: removed {n} packs of older converter versions ({freed / 1048576:.0f} MB)")
    return n, freed


def sha1_file(path, bufsize=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(bufsize), b""):
            h.update(b)
    return h.hexdigest()


class Converter:
    def __init__(self, converter, template=None, python=None, timeout=600, frozen=None):
        self.converter = os.path.abspath(converter)
        self.python = python or sys.executable          # frozen build: sys.executable is gsvr-cli(.exe)
        self.frozen = runtime.FROZEN if frozen is None else frozen
        self.timeout = timeout
        self.help = ""
        if not template:
            try:
                r = subprocess.run(runtime.script_argv(self.python, self.converter, "--help", frozen=self.frozen),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                                   stdin=subprocess.DEVNULL)
                self.help = r.stdout + r.stderr
            except (OSError, subprocess.SubprocessError):
                self.help = ""
            template = runtime.script_prefix(self.frozen) + " --bsp {bsp} {out}"
            if "--game-root" in self.help:
                template += " --game-root {game_root}"
            if "--mod " in self.help or "--mod\n" in self.help or "--mod MOD" in self.help:
                template += " --mod {mod}"
        self.template = template
        self.version = self._version()

    def _version(self):
        """'--version' output if the converter supports it, else sha1 over the converter's *.py files."""
        if "--version" in self.help:
            try:
                r = subprocess.run(runtime.script_argv(self.python, self.converter, "--version", frozen=self.frozen),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                                   stdin=subprocess.DEVNULL)
                v = (r.stdout or r.stderr).strip()
                if r.returncode == 0 and v:
                    return "v-" + hashlib.sha1(v.encode()).hexdigest()[:12]
            except (OSError, subprocess.SubprocessError):
                pass
        h = hashlib.sha1()
        d = os.path.dirname(self.converter)
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py"):
                with open(os.path.join(d, fn), "rb") as fh:
                    h.update(fn.encode() + b"\0" + fh.read())
        h.update(self.template.encode())
        return "s-" + h.hexdigest()[:12]

    def argv(self, **kw):
        # Each placeholder becomes exactly one argv element (no shell, no word-splitting of paths).
        out = []
        for tok in shlex.split(self.template, posix=True):
            out.append(tok.format(python=self.python, converter=self.converter, **kw))
        # -X utf8: the converter's stdout must not depend on the console code page (Windows cp1252 can't
        # encode '→' etc. → UnicodeEncodeError). PYTHONUTF8 would be ignored because of -I.
        # (A frozen gsvr-cli has no -X switch: UTF-8 mode is baked into the exe, see packaging/gsvr.spec.)
        if out and out[0] == self.python and "-X" not in out[1:3] and not self.frozen:
            out[1:1] = ["-X", "utf8"]
        return out

    def build(self, cache_dir, mod, mapname, bsp, game_root, mod_dir, log=print, extra=()):
        """-> dict(path, size, sha1, key, cached, seconds). Raises RuntimeError on failure.
        extra = more converter arguments appended to the command (the tram pack: --only-model); the caller then also
        passes a distinct mapname, which is part of the cache key."""
        import time
        t0 = time.monotonic()
        os.makedirs(cache_dir, exist_ok=True)
        if os.path.isfile(bsp):
            bsp_sha = sha1_file(bsp)
        else:
            # a map inside a CASC install (WC3 1.30/1.31): path + the install's build key identify the content
            info = os.path.join(game_root, ".build.info")
            raw = open(info, "rb").read() if os.path.isfile(info) else b""
            bsp_sha = hashlib.sha1(bsp.encode("utf-8") + b"\0" + raw).hexdigest()
        key = f"{mod}-{mapname}-{bsp_sha[:16]}-{self.version}"
        final = os.path.join(cache_dir, key + ".bin")
        if os.path.isfile(final) and os.path.getsize(final) > 0:
            return dict(path=final, size=os.path.getsize(final), key=key, cached=True,
                        seconds=round(time.monotonic() - t0, 3))
        tmp = os.path.join(cache_dir, key + ".tmp")
        argv = self.argv(bsp=bsp, out=tmp, mod=mod, map=mapname, game_root=game_root, mod_dir=mod_dir)
        argv += list(extra)
        log("build: " + " ".join(shlex.quote(a) for a in argv))
        try:
            with buildprog.TRACKER.job("war3" if mod == "war3" else "map", mod, mapname) as job:
                r = buildprog.run(argv, self.timeout, on_line=job.feed, **runtime.low_priority_kwargs(),
                                  cwd=cache_dir, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"converter timeout after {self.timeout}s")
        except OSError as e:
            raise RuntimeError(f"converter failed to start: {e}")
        for line in (r.stdout + r.stderr).strip().splitlines()[-10:]:
            log("  converter: " + line)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            raise RuntimeError(f"converter exit {r.returncode}: {tail[0][:200]}")
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError("converter produced no output")
        try:
            size = validate_pack(tmp)
        except RuntimeError as e:
            os.remove(tmp)
            raise RuntimeError(f"converter output rejected: {e}")
        os.replace(tmp, final)
        return dict(path=final, size=size, key=key, cached=False, seconds=round(time.monotonic() - t0, 3))
