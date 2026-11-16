"""ROSE pack route (GET /pack/rose.rpg1): serves a ready-made RPG1 file to the ROSE world. Stdlib only.

This module only validates + serves. The packs  rose__<name>.rpg1  (double underscore like the other kinds' <mod>__<map>
names) in the rose folder (--rose-dir, default <cache dir>/rose) are built by rosebuild.py from the player's own ROSE install
(--rose-install / GUI field) with the vendored converter rose_conv, or dropped in by hand (dev / test container).

Request line:  [GSVR-REQ] rose=<name> [req=<id>]     <name> matches [a-z0-9_]{1,32}
Validation (cheap, the world validates fully): file exists inside the rose folder (no traversal, no symlink out), size <= MAX_BYTES,
32 byte header: magic RPG1, major version 1 (high 16 bits of the u32 at offset 4), headerBytes >= 32, fileSize (u32 at offset 20)
equals the real size.
"""
import os, re, struct

NAME_RE = re.compile(r"[a-z0-9_]{1,32}\Z")
MAX_BYTES = 100 * 1024 * 1024
HEADER = 32
MAJOR = 1


def new_slot():
    return {"state": "idle", "req": None, "name": None, "size": 0, "error": None, "path": None, "key": None}


BUILD_KEYS = ("stage", "progress", "eta_s")


def public(slot):
    """status.json "rose": {state, req, name, size, error} (+ stage, progress 0..1, eta_s (-1 unknown) while "building",
    rosebuild.py builds the pack from the player's own ROSE install)."""
    building = slot.get("state") == "building"
    return {k: v for k, v in slot.items() if k not in ("path", "key") and (building or k not in BUILD_KEYS)}


TEST_FILE = "test_scenario.txt"


def test_scenario(rose_dir):
    """status.json "test_scenario": the one-build test selection (tools/vrctest_round.sh): the first line of <rose dir>/test_scenario.txt when it
    is a valid scenario name ([a-z0-9_]{1,32}), else "". Only the test container ever has that file; a world acts on it only in a test build
    (GsvrRoseTestSelect.armed, refused by GsvrUploadGuard for uploads)."""
    try:
        with open(os.path.join(rose_dir, TEST_FILE), "rb") as fh:
            raw = fh.read(64)
    except (OSError, TypeError):
        return ""
    name = raw.decode("ascii", "ignore").strip().split("\n")[0].strip()
    return name if NAME_RE.match(name) else ""


def valid_name(name):
    return isinstance(name, str) and NAME_RE.match(name) is not None


def default_dir(cache_dir):
    return os.path.join(cache_dir, "rose")


def resolve(rose_dir, name):
    """-> absolute path of rose__<name>.rpg1 inside rose_dir. Raises RuntimeError on a bad name / escape."""
    if not valid_name(name):
        raise RuntimeError("bad rose pack name (expected [a-z0-9_]{1,32})")
    base = os.path.realpath(rose_dir)
    path = os.path.realpath(os.path.join(base, f"rose__{name}.rpg1"))
    if os.path.dirname(path) != base:
        raise RuntimeError("rose pack path leaves the rose folder")
    return path


def validate(path):
    """-> size. Raises FileNotFoundError when missing, RuntimeError (with the reason) when the header is wrong."""
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > MAX_BYTES:
            raise RuntimeError(f"rose pack too large ({size} > {MAX_BYTES} bytes)")
        head = fh.read(HEADER)
    if len(head) < HEADER:
        raise RuntimeError("rose pack shorter than the 32 byte RPG1 header")
    if head[:4] != b"RPG1":
        raise RuntimeError(f"bad magic {head[:4]!r} (expected RPG1)")
    ver, hb = struct.unpack_from("<II", head, 4)
    if (ver >> 16) != MAJOR:
        raise RuntimeError(f"unsupported RPG1 major version {ver >> 16} (expected {MAJOR})")
    fsize = struct.unpack_from("<I", head, 20)[0]
    if hb < HEADER or hb > size:
        raise RuntimeError("RPG1 headerBytes out of range")
    if fsize != size:
        raise RuntimeError(f"RPG1 fileSize {fsize} does not match the file ({size} bytes)")
    return size


def prepare(rose_dir, name):
    """Slot fields for a request: {state: ready|error, size, error, path}."""
    try:
        path = resolve(rose_dir, name)
        return {"state": "ready", "size": validate(path), "error": None, "path": path}
    except FileNotFoundError:
        return {"state": "error", "size": 0, "path": None,
                "error": f"no rose__{name}.rpg1 in the rose folder (--rose-dir)"}
    except (RuntimeError, OSError) as e:
        return {"state": "error", "size": 0, "path": None, "error": str(e)}
