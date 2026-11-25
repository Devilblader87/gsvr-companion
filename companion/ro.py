"""Ragnarok Online pack route (GET /pack/ro.pack): serves a ready-made pack of the RO world. Stdlib only.

Same shape as rose.py (ROSE): this module only validates + serves. The packs  ro__<name>.pack  in the ro folder (--ro-dir, default
<cache dir>/ro) are built by rosebuild.py (flavor RO) from the player's OWN iRO client data (the folder holding data.grf) with the
converter `ro_conv`, or dropped in by hand (dev / test container). Nothing of the game ships with GSVR (BYOG).

Request line:  [GSVR-REQ] ro=<name> [req=<id>]     <name> matches [a-z0-9_]{1,32}   (the first one: prontera)
Container: RPG1 (the same 32 byte header as ROSE: magic RPG1, major 1, headerBytes, fileSize at offset 20) - validated by rose.validate.
"""
import os
import struct

import rose

NAME_RE = rose.NAME_RE
MAX_BYTES = 200 * 1024 * 1024          # a town map with its models + textures; rose.MAX_BYTES (100 MB) is for ROSE zones
DEFAULT_MAP = "prontera"
BUILD_KEYS = rose.BUILD_KEYS
new_slot = rose.new_slot
public = rose.public                    # {state, req, name, size, error} (+ stage, progress, eta_s while building): never a path
valid_name = rose.valid_name


def default_dir(cache_dir):
    return os.path.join(cache_dir, "ro")


def resolve(ro_dir, name):
    """-> absolute path of ro__<name>.pack inside ro_dir. Raises RuntimeError on a bad name / escape."""
    if not valid_name(name):
        raise RuntimeError("bad ro pack name (expected [a-z0-9_]{1,32})")
    base = os.path.realpath(ro_dir)
    path = os.path.realpath(os.path.join(base, f"ro__{name}.pack"))
    if os.path.dirname(path) != base:
        raise RuntimeError("ro pack path leaves the ro folder")
    return path


def validate(path):
    """-> size. FileNotFoundError when missing, RuntimeError (reason) when the RPG1 header is wrong / the file is too large."""
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > MAX_BYTES:
            raise RuntimeError(f"ro pack too large ({size} > {MAX_BYTES} bytes)")
        head = fh.read(rose.HEADER)
    if len(head) < rose.HEADER:
        raise RuntimeError("ro pack shorter than the 32 byte RPG1 header")
    if head[:4] != b"RPG1":
        raise RuntimeError(f"bad magic {head[:4]!r} (expected RPG1)")
    ver, hb = struct.unpack_from("<II", head, 4)
    if (ver >> 16) != rose.MAJOR:
        raise RuntimeError(f"unsupported RPG1 major version {ver >> 16} (expected {rose.MAJOR})")
    if hb < rose.HEADER or hb > size:
        raise RuntimeError("RPG1 headerBytes out of range")
    fsize = struct.unpack_from("<I", head, 20)[0]
    if fsize != size:
        raise RuntimeError(f"RPG1 fileSize {fsize} does not match the file ({size} bytes)")
    return size


def prepare(ro_dir, name):
    """Slot fields for a request: {state: ready|error, size, error, path}."""
    try:
        path = resolve(ro_dir, name)
        return {"state": "ready", "size": validate(path), "error": None, "path": path}
    except FileNotFoundError:
        return {"state": "error", "size": 0, "path": None, "error": f"no ro__{name}.pack in the ro folder (--ro-dir)"}
    except (RuntimeError, OSError) as e:
        return {"state": "error", "size": 0, "path": None, "error": str(e)}
