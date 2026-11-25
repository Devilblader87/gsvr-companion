"""Stand-in for the Ragnarok Online converter (companion/ro_conv/, other lane) with the contract rosebuild.RO needs. Stdlib only, NO game data.

Contract the real ro_conv must keep (rosebuild.Manager, flavor RO):
  VERSION (str), PACKS {name: {"kind": "zone", "label": ...}}, check_install(folder) -> (ok, reason)   (folder must hold data.grf),
  install_files(folder) -> ["data.grf"] (relative; size + mtime key the build cache), optional install_extra(folder) -> [lines],
  build(name, folder, out_path, progress=cb(stage, 0..1), cancel=callable) -> dict {warnings: [...]}, writes an RPG1 file to out_path,
  Cancelled / BuildError exceptions, optional pack_version(name).
A "Ragnarok install" for the stub is any folder with a file data.grf in it. Selected by the tests or by GSVR_RO_CONV=stub.
Knobs (module globals / env GSVR_RO_STUB_*): STEP_S, FAIL, HOLD, CALLS like rose_conv_stub.
"""
import os, struct, threading, time

VERSION = "ro-stub-1"
PACK_REV = {}


def pack_version(name):
    rev = PACK_REV.get(name, 0)
    return VERSION if not rev else "%s-p%d" % (VERSION, rev)


STAGES = (("grf", 2), ("terrain", 4), ("objects", 3), ("write", 1))
STEP_S = float(os.environ.get("GSVR_RO_STUB_STEP_S", "0.01"))
FAIL = set(n for n in os.environ.get("GSVR_RO_STUB_FAIL", "").split(",") if n)
FAIL_AT_ONCE = bool(FAIL)
HOLD = None
HOLD_FROM = 0
CALLS = []
NO_PART_CLEANUP = False
BURN = False
_lock = threading.Lock()


class BuildError(Exception):
    pass


class Cancelled(Exception):
    pass


PACKS = {
    "prontera": {"kind": "zone", "label": "Prontera"},
    "geffen": {"kind": "zone", "label": "Geffen"},
}


def check_install(path):
    if not path or not os.path.isdir(path):
        return False, "folder does not exist"
    if not os.path.isfile(os.path.join(path, "data.grf")):
        return False, "no data.grf in the folder"
    return True, "ok"


def install_files(path):
    """Relative paths of the install files the build manager's fingerprint covers (size + mtime): data.grf."""
    return ["data.grf"] if os.path.isfile(os.path.join(path, "data.grf")) else []


def _step():
    """one build step: sleep STEP_S, or with BURN busy-loop that long in pure Python (the real converter's GIL load)"""
    if not BURN:
        time.sleep(STEP_S)
        return
    end = time.perf_counter() + STEP_S
    x = 0
    while time.perf_counter() < end:
        for i in range(2000):
            x = (x * 31 + i) & 0xFFFFFFFF
    return x


def _pack_bytes(name, total=4096):
    head = b"RPG1" + struct.pack("<7I", (1 << 16) | 1, 32, total, 0, total, 0, 0)
    seed = sum(name.encode()) & 255
    return head + bytes((seed + i * 7) & 255 for i in range(total - len(head)))


def build(name, install_dir, out_path, progress=None, cancel=None):
    if name not in PACKS:
        raise BuildError(f"unknown pack {name!r}")
    ok, why = check_install(install_dir)
    if not ok:
        raise BuildError(f"Ragnarok install not usable: {why}")
    with _lock:
        CALLS.append(name)
    if FAIL_AT_ONCE and name in FAIL:
        raise BuildError("stub failure at once")
    t0 = time.monotonic()
    part = out_path + ".part"
    total = sum(w for _s, w in STAGES)
    done = 0
    with open(part, "wb") as fh:
        fh.write(b"partial")
    try:
        for stage, w in STAGES:
            for i in range(w):
                if cancel and cancel():
                    raise Cancelled(name)
                if HOLD is not None and done >= HOLD_FROM:
                    while not HOLD.wait(0.02):
                        if cancel and cancel():
                            raise Cancelled(name)
                _step()
                done += 1
                if progress:
                    progress(stage, done / total)
            if name in FAIL and stage == "terrain":
                raise BuildError(f"stub failure in {stage}")
        data = _pack_bytes(name)
        with open(part, "wb") as fh:
            fh.write(data)
        os.replace(part, out_path)
    except Cancelled:
        if not NO_PART_CLEANUP and os.path.exists(part):
            os.remove(part)
        raise
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    return {"name": name, "bytes": len(data), "sections": 3, "seconds": round(time.monotonic() - t0, 3), "warnings": []}
