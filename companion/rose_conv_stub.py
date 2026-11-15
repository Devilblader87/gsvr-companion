"""Stand-in for the vendored ROSE converter (companion/rose_conv/, lane B) with the same API. Stdlib only, NO game data.

Selected only by the tests (rosebuild.Manager(conv=rose_conv_stub)) or by the env var GSVR_ROSE_CONV=stub; the companion
otherwise imports the real `rose_conv`. A "ROSE install" for the stub is any folder with a file data.idx in it. The packs it
writes are synthetic RPG1 files (valid 32 byte header + filler) that pass rose.validate.

Knobs for the tests (module globals): STEP_S (sleep per stage step), FAIL (names whose build raises BuildError), HOLD
(threading.Event or None: each step waits for it, so a test can keep a build running), CALLS (names, in build order),
NO_PART_CLEANUP (True: leave the .part behind on cancel, to test the manager's own cleanup), HOLD_FROM (HOLD only blocks from this step
on: a build can be held past 50 %), BURN (each step busy-loops STEP_S in pure Python instead of sleeping: the real converter's
GIL load). Env (live / child tests): GSVR_ROSE_STUB_STEP_S, GSVR_ROSE_STUB_FAIL (comma list of names that fail at once),
GSVR_ROSE_STUB_BURN=1.
"""
import os, struct, threading, time

VERSION = "rv-stub-1"
STAGES = (("vfs", 2), ("terrain", 4), ("objects", 3), ("write", 1))
STEP_S = float(os.environ.get("GSVR_ROSE_STUB_STEP_S", "0.01"))   # live tests slow it down to watch "building"
BURN = os.environ.get("GSVR_ROSE_STUB_BURN", "") == "1"           # each step busy-loops (pure Python, holds the GIL) like rose_conv
FAIL = set(n for n in os.environ.get("GSVR_ROSE_STUB_FAIL", "").split(",") if n)
FAIL_AT_ONCE = bool(FAIL)               # env failures raise before the first step (a converter that fails instantly)
HOLD = None
HOLD_FROM = 0
CALLS = []
NO_PART_CLEANUP = False
_lock = threading.Lock()


class BuildError(Exception):
    pass


class Cancelled(Exception):
    pass


PACKS = {
    "birthisland": {"kind": "zone", "zone": 20, "label": "Birth Island", "args": ["--zone", "20", "--ui"]},
    "luxemtower": {"kind": "zone", "zone": 21, "args": ["--zone", "21", "--all", "--ui"]},
    "adventurersplain": {"kind": "zone", "zone": 22, "args": ["--zone", "22", "--all", "--ui"]},
    "breezyhills": {"kind": "zone", "zone": 23, "args": ["--zone", "23", "--all", "--ui"]},
    "zant": {"kind": "zone", "zone": 1, "args": ["--zone", "1", "--all", "--ui"]},
    "elverloondesert": {"kind": "zone", "zone": 24, "args": ["--zone", "24", "--all", "--ui"]},
    "animalake": {"kind": "zone", "zone": 25, "args": ["--zone", "25", "--all", "--ui"]},
    "forestofwisdom": {"kind": "zone", "zone": 26, "args": ["--zone", "26", "--all", "--ui"]},
    "kenjibeach": {"kind": "zone", "zone": 27, "args": ["--zone", "27", "--all", "--ui"]},
    "gorgeofsilence": {"kind": "zone", "zone": 28, "args": ["--zone", "28", "--all", "--ui"]},
    "goblincaveb1": {"kind": "zone", "zone": 31, "args": ["--zone", "31", "--all", "--ui"]},
    "goblincaveb2": {"kind": "zone", "zone": 32, "args": ["--zone", "32", "--all", "--ui"]},
    "goblincaveb3": {"kind": "zone", "zone": 33, "args": ["--zone", "33", "--all", "--ui"]},
    "junonpolis": {"kind": "zone", "zone": 2, "args": ["--zone", "2", "--all", "--ui"]},
    "login": {"kind": "lobby", "args": ["--lobby"]},
    "avatar": {"kind": "avatar", "args": []},
}


def check_install(path):
    if not path or not os.path.isdir(path):
        return False, "folder does not exist"
    if not os.path.isfile(os.path.join(path, "data.idx")):
        return False, "no data.idx (not a ROSE install)"
    return True, "ok"


def install_files(path):
    """Relative paths of the install files the build manager's fingerprint covers (real converter: data.idx + .VFS stores)."""
    return ["data.idx"] if os.path.isfile(os.path.join(path, "data.idx")) else []


def install_extra(path):
    """like rose_conv.install_extra: a stat line of the loose Sound folder (adding it makes the packs stale)"""
    snd = os.path.join(path, "Sound")
    if not os.path.isdir(snd):
        return ["Sound -"]
    names = sorted(os.listdir(snd))
    return ["Sound %d %d" % (len(names), sum(os.path.getsize(os.path.join(snd, n)) for n in names))]


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
        raise BuildError(f"ROSE install not usable: {why}")
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
