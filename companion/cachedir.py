"""User-selectable cache folder: resolving the chosen path, validating it, and moving an existing cache.

Pure stdlib, no Tk - used by gui.py (Controller) and tested headless (test_cachedir.py).
The chosen folder is used directly as the cache; only a bare drive root (D:\\ or /) is mapped to <root>GSVR\\cache.
"""
import ntpath, os, shutil

MIN_FREE_BYTES = 2 << 30            # warn below 2 GB free
SKIP_SUFFIXES = (".tmp", ".moving")  # half-written builds / copies are never moved


def is_root(p, mod=os.path):
    drive, rest = mod.splitdrive(p)
    if drive and not rest.strip("\\/"):
        return True
    n = mod.normpath(p)
    return mod.dirname(n) == n


def resolve(setting, default, mod=os.path):
    """Setting value ("" = default) -> the folder the companion really uses. A bare drive root gets GSVR\\cache."""
    s = (setting or "").strip()
    if not s:
        return default
    if mod is os.path:
        s = os.path.expanduser(s)
    if is_root(s, mod):
        drive = mod.splitdrive(s)[0]
        return mod.join(drive + mod.sep if drive else mod.normpath(s), "GSVR", "cache")
    return mod.normpath(s)


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def inside(child, parent):
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def free_bytes(path):
    """Free space of the drive holding `path` (nearest existing parent); None if unknown."""
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        up = os.path.dirname(p)
        if up == p:
            break
        p = up
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def fmt_bytes(n):
    if n is None:
        return "?"
    return "%.1f GB" % (n / 1e9) if n >= 1e9 else "%.0f MB" % (n / 1e6)


def validate(path, game_roots=()):
    """-> (errors, warnings) as message lists for the final folder `path` (already resolved)."""
    errs, warns = [], []
    for g in game_roots:
        if g and inside(path, g):
            errs.append("The cache folder must not be inside a game install (%s)." % g)
            break
    if not errs:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".gsvr-write-test")
            with open(probe, "w") as fh:
                fh.write("x")
            os.remove(probe)
        except OSError as e:
            errs.append("Cannot write to %s (%s)." % (path, e))
    fb = free_bytes(path)
    if not errs and fb is not None and fb < MIN_FREE_BYTES:
        warns.append("Only %s free on that drive (recommended: at least 2 GB)." % fmt_bytes(fb))
    return errs, warns


def list_files(root):
    """[(relpath, size)] of everything movable below root (no *.tmp)."""
    out = []
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if fn.lower().endswith(SKIP_SUFFIXES):
                continue
            p = os.path.join(dp, fn)
            try:
                out.append((os.path.relpath(p, root), os.path.getsize(p)))
            except OSError:
                pass
    return out


def cache_size(root):
    return sum(s for _r, s in list_files(root)) if os.path.isdir(root) else 0


def move_cache(src, dst, progress=None, cancel=None, log=print):
    """Copy src -> dst file by file and verify every size; only when ALL copies are verified are the old files deleted.
    Cancel or any failure leaves the old cache completely intact (partial copies in dst are harmless cache files).
    Returns (ok, bytes_done, bytes_total)."""
    if not os.path.isdir(src):
        return True, 0, 0
    if _norm(src) == _norm(dst) or inside(dst, src) or inside(src, dst):
        log("[cache] source and target folder overlap - nothing moved")
        return False, 0, 0
    files = list_files(src)
    total = sum(s for _r, s in files)
    done = 0
    try:
        os.makedirs(dst, exist_ok=True)
        for rel, size in files:
            if cancel is not None and cancel.is_set():
                log("[cache] move cancelled - old cache kept in %s" % src)
                return False, done, total
            a, b = os.path.join(src, rel), os.path.join(dst, rel)
            os.makedirs(os.path.dirname(b), exist_ok=True)
            part = b + ".moving"
            shutil.copyfile(a, part)
            if os.path.getsize(part) != size or os.path.getsize(a) != size:
                os.remove(part)
                raise OSError("size mismatch for %s" % rel)
            os.replace(part, b)
            done += size
            if progress:
                progress(done, total)
    except OSError as e:
        log("[cache] move failed: %s - old cache kept in %s" % (e, src))
        return False, done, total
    for rel, _s in files:                   # everything verified -> now remove the old files
        try:
            os.remove(os.path.join(src, rel))
        except OSError as e:
            log("[cache] could not delete old %s: %s" % (rel, e))
    for dp, _dn, _fn in os.walk(src, topdown=False):
        if dp != src and not os.listdir(dp):
            try:
                os.rmdir(dp)
            except OSError:
                pass
    log("[cache] moved %d files (%s) to %s" % (len(files), fmt_bytes(total), dst))
    return True, done, total
