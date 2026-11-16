"""Companion game modules (modules.py, ed25519.py, packaging/modtool.py): signature, hashes, loader, rollback, no-downgrade, hostile zips,
forged caches. Never touches the network: the release is a folder per tag and Store(fetch=..., list_tags=...) reads from it.

Run: python3 -I companion/test_modules.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "packaging"))
import ed25519      # noqa: E402
import modtool      # noqa: E402
import modules      # noqa: E402

SEED = bytes.fromhex("11" * 32)
PUB = ed25519.publickey(SEED).hex()
OTHER_SEED = bytes.fromhex("22" * 32)
OTHER = ed25519.publickey(OTHER_SEED).hex()


class Release:
    """a fake GitHub: folder per tag with the assets modtool made"""

    def __init__(self, top):
        self.top = top
        os.makedirs(top, exist_ok=True)
        self.keyfile = os.path.join(top, "key")
        with open(self.keyfile, "w") as fh:
            fh.write(SEED.hex())
        self.tags = []
        self.fetched = []

    def publish(self, serial, version, mutate=None):
        d = os.path.join(self.top, "modules-%d" % serial)
        os.makedirs(d)
        modtool.build("rose", version, d)
        if mutate:
            mutate(d)
        modtool.make_release(serial, self.keyfile, d)
        self.tags.append("modules-%d" % serial)
        return d

    def fetch(self, url, max_bytes):
        self.fetched.append(url)
        pre = "https://github.com/%s/%s/releases/download/" % (modules.REPO_OWNER, modules.REPO_NAME)
        assert url.startswith(pre), url
        tag, asset = url[len(pre):].split("/")
        with open(os.path.join(self.top, tag, asset), "rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise modules.ModuleError("too big")
        return data

    def store(self, root, pubkeys=(PUB,)):
        return modules.Store(root=root, fetch=self.fetch, list_tags=lambda: list(self.tags), pubkeys=pubkeys)


def signed_manifest(entries, serial=1, seed=SEED):
    m = {"schema": modules.SCHEMA, "repo": "%s/%s" % (modules.REPO_OWNER, modules.REPO_NAME), "serial": serial, "modules": entries}
    data = json.dumps(m, indent=1, sort_keys=True).encode("utf-8")
    return data, ed25519.sign(seed, data).hex()


def plant(root, name, ver, files, mj_extra=None, seed=SEED, write_meta=True, entry_extra=None):
    """Write an installed module tree + its stored signed manifest by hand (the attacker / a buggy release). -> module.json dict"""
    mj = {"name": name, "version": ver, "api": modules.MODULE_API, "core_min": "0.0.1", "paths": [""], "provides": sorted(
        {f[:-3] for f in files if f.endswith(".py") and "/" not in f}), "files": {k: modules.sha256_bytes(v) for k, v in files.items()}}
    mj.update(mj_extra or {})
    raw = json.dumps(mj, indent=1, sort_keys=True).encode("utf-8")
    tree = os.path.join(root, name, ver)
    os.makedirs(tree)
    for k, v in files.items():
        p = os.path.join(tree, *k.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(v)
    with open(os.path.join(tree, "module.json"), "wb") as fh:
        fh.write(raw)
    if write_meta:
        ent = {"version": ver, "api": modules.MODULE_API, "core_min": "", "file": "gsvr-module-%s-%s.zip" % (name, ver), "size": 10,
               "sha256": "0" * 64, "module_json_sha256": modules.sha256_bytes(raw)}
        ent.update(entry_extra or {})
        data, sig = signed_manifest({name: ent}, seed=seed)
        mp, sp = modules._meta_paths(root, name, ver)
        with open(mp, "wb") as fh:
            fh.write(data)
        with open(sp, "w") as fh:
            fh.write(sig)
    return mj


def write_state(root, mods, serial=1):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "state.json"), "w") as fh:
        json.dump({"serial": serial, "mods": mods}, fh)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rel = Release(os.path.join(self.tmp, "rel"))
        self.root = os.path.join(self.tmp, "mods")
        modules._ACTIVE.clear()
        modules._ERRORS.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Ed(unittest.TestCase):
    def test_rfc8032_vectors(self):
        vec = [("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "",
                "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
               ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "72",
                "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")]
        for seed, msg, pub in vec:
            seed, msg = bytes.fromhex(seed), bytes.fromhex(msg)
            self.assertEqual(ed25519.publickey(seed).hex(), pub)
            s = ed25519.sign(seed, msg)
            self.assertTrue(ed25519.verify(bytes.fromhex(pub), msg, s))
            self.assertFalse(ed25519.verify(bytes.fromhex(pub), msg + b"x", s))
        s1 = ed25519.sign(bytes.fromhex(vec[0][0]), b"").hex()
        self.assertTrue(s1.startswith("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"))

    def test_malformed_never_raises(self):
        for pub, sig in ((b"", b""), (b"\xff" * 32, b"\xff" * 64), (bytes(32), bytes(64)), (b"x" * 31, b"y" * 64)):
            self.assertFalse(ed25519.verify(pub, b"m", sig))
        s = bytearray(ed25519.sign(SEED, b"m"))
        s[40] ^= 1
        self.assertFalse(ed25519.verify(bytes.fromhex(PUB), b"m", bytes(s)))

    def test_bad_pubkey_hex_is_a_module_error(self):
        data, sig = signed_manifest({})
        for keys in (("zz",), ("abcd",), ("",)):
            with self.assertRaisesRegex(modules.ModuleError, "signature invalid"):
                modules.verify_manifest(data, sig, keys)
        modules.verify_manifest(data, sig, ("zz", PUB))              # a broken key entry does not hide a good one


class Install(Base):
    def test_install_activate_rollback(self):
        self.rel.publish(1, "1.0.0")
        st = self.rel.store(self.root)
        self.assertEqual(st.check("rose")["status"], "available")
        self.assertEqual(st.install("rose"), "1.0.0")
        self.assertEqual(st.installed("rose"), ("1.0.0", None))
        self.assertTrue(all(u.startswith("https://github.com/Devilblader87/gsvr-companion/releases/download/modules-1/")
                            for u in self.rel.fetched))
        mp, sp = modules._meta_paths(self.root, "rose", "1.0.0")
        self.assertTrue(os.path.isfile(mp) and os.path.isfile(sp))    # the signed manifest + signature are stored next to the tree
        self.assertEqual(st.check("rose")["status"], "current")
        with self.assertRaises(modules.ModuleError):
            st.install("rose")                                           # same version: nothing to do
        self.rel.publish(2, "1.0.1")
        self.assertEqual(st.install("rose"), "1.0.1")
        self.assertEqual(st.installed("rose"), ("1.0.1", "1.0.0"))
        self.assertEqual(st.rollback("rose"), "1.0.0")
        self.assertEqual(st.installed("rose"), ("1.0.0", "1.0.1"))
        self.assertEqual(st.rollback("rose"), "1.0.1")
        left = [n for n in os.listdir(self.root) if n.startswith(".stg")]
        self.assertEqual(left, [])
        rows = modules.rows(st)
        self.assertEqual((rows[0]["installed"], rows[0]["previous"], rows[0]["running"]), ("1.0.1", "1.0.0", "eingebaut"))

    def test_rollback_target_is_fully_verified(self):
        self.rel.publish(1, "1.0.0")
        self.rel.publish(2, "1.0.1")
        st = self.rel.store(self.root)
        self.rel.tags = ["modules-1"]
        st.install("rose")
        self.rel.tags = ["modules-1", "modules-2"]
        st.install("rose")
        with open(os.path.join(self.root, "rose", "1.0.0", "rosefind.py"), "a") as fh:
            fh.write("\n# evil\n")
        with self.assertRaisesRegex(modules.ModuleError, "modified"):
            st.rollback("rose")
        self.assertEqual(st.installed("rose"), ("1.0.1", "1.0.0"))        # state unchanged
        os.remove(modules._meta_paths(self.root, "rose", "1.0.0")[1])
        with self.assertRaisesRegex(modules.ModuleError, "no signed manifest"):
            st.rollback("rose")
        st2 = self.rel.store(self.root, pubkeys=(OTHER,))                 # stored manifest signed by a key we do not trust
        with self.assertRaises(modules.ModuleError):
            st2.rollback("rose")

    def test_no_downgrade_replay_and_wrong_key(self):
        self.rel.publish(5, "1.0.5")
        st = self.rel.store(self.root)
        st.install("rose")
        self.rel.publish(4, "1.0.4")
        self.rel.tags = ["modules-4"]
        with self.assertRaisesRegex(modules.ModuleError, "older"):
            st.install("rose")
        self.assertEqual(st.installed("rose")[0], "1.0.5")
        self.rel.tags = ["modules-5"]
        self.assertEqual(st.check("rose")["status"], "current")
        st2 = self.rel.store(os.path.join(self.tmp, "m2"), pubkeys=(OTHER,))
        with self.assertRaisesRegex(modules.ModuleError, "signature"):
            st2.install("rose")
        st3 = self.rel.store(os.path.join(self.tmp, "m3"), pubkeys=())
        with self.assertRaisesRegex(modules.ModuleError, "no signing key"):
            st3.install("rose")
        self.assertEqual(modules.PUBKEYS, ())                           # nothing shipped yet: fails closed

    def test_newest_bad_tag_falls_back_to_next_valid(self):
        self.rel.publish(1, "1.0.0")
        d = self.rel.publish(2, "1.0.1")
        with open(os.path.join(d, modules.SIG_ASSET), "w") as fh:
            fh.write("00" * 64)                                           # broken upload of the newest release
        st = self.rel.store(self.root)
        self.assertEqual(st.install("rose"), "1.0.0")
        self.assertEqual(st.installed("rose")[0], "1.0.0")

    def test_tampering(self):
        d = self.rel.publish(1, "1.0.0")
        man = os.path.join(d, "gsvr-modules.json")
        with open(man, "rb") as fh:
            data = fh.read()
        with open(man, "wb") as fh:
            fh.write(data.replace(b'"serial": 1', b'"serial": 9'))
        with self.assertRaisesRegex(modules.ModuleError, "signature"):
            self.rel.store(self.root).install("rose")
        with open(man, "wb") as fh:
            fh.write(data)
        with open(os.path.join(d, "gsvr-module-rose-1.0.0.zip"), "ab") as fh:
            fh.write(b"\0")
        with self.assertRaisesRegex(modules.ModuleError, "manifest|too big"):
            self.rel.store(self.root).install("rose")
        self.assertFalse(os.path.exists(os.path.join(self.root, "rose")))

    def test_serial_must_match_tag(self):
        self.rel.publish(3, "1.0.0")
        os.rename(os.path.join(self.rel.top, "modules-3"), os.path.join(self.rel.top, "modules-7"))
        self.rel.tags = ["modules-7"]
        with self.assertRaisesRegex(modules.ModuleError, "tag"):
            self.rel.store(self.root).install("rose")

    def test_api_mismatch_refused(self):
        old = modules.MODULE_API
        try:
            modules.MODULE_API = 99
            self.rel.publish(1, "1.0.0")
        finally:
            modules.MODULE_API = old
        st = self.rel.store(self.root)
        self.assertEqual(st.check("rose")["status"], "incompatible")
        with self.assertRaisesRegex(modules.ModuleError, "api"):
            st.install("rose")

    def test_non_official_url_refused(self):
        st = modules.Store(root=self.root, list_tags=lambda: ["modules-1"])
        with self.assertRaisesRegex(modules.ModuleError, "non-official"):
            st.fetch("https://evil.example/x", 10)
        with self.assertRaisesRegex(modules.ModuleError, "non-official"):
            st.fetch("https://github.com/someone/else/releases/download/modules-1/gsvr-modules.json", 10)

    def test_names_and_tags_with_newline_refused(self):
        st = self.rel.store(self.root)
        self.rel.publish(1, "1.0.0")
        for bad in ("rose\n", "rose ", "ROSE", "../rose", ""):
            for fn in (st.install, st.check, st.rollback, st.disable):
                with self.assertRaisesRegex(modules.ModuleError, "unknown module"):
                    fn(bad)
        self.assertIsNone(modules.NAME_RE.fullmatch("rose\n"))
        self.assertIsNone(modules.TAG_RE.fullmatch("modules-1\n"))
        self.assertIsNone(modules.VER_RE.fullmatch("1.0.0\n"))
        self.assertIsNone(modules.SHA_RE.fullmatch("0" * 64 + "\n"))
        self.assertIsNone(modules.FILE_RE.fullmatch("gsvr-module-rose-1.0.0.zip\n"))
        st.pubkeys = (PUB,)
        st._list_tags = lambda: ["modules-1\n"]
        with self.assertRaisesRegex(modules.ModuleError, "no module release"):
            st.install("rose")
        data, sig = signed_manifest({"rose\n": {"version": "1.0.0", "api": 1, "core_min": "", "file": "gsvr-module-rose-1.0.0.zip", "size": 1,
                                                "sha256": "0" * 64, "module_json_sha256": "0" * 64}})
        with self.assertRaisesRegex(modules.ModuleError, "malformed"):
            modules.verify_manifest(data, sig, (PUB,))


class Hostile(Base):
    BAD_NAMES = ["../x.py", "/abs.py", "a/../../b.py", "C:/x.py", "native.pyd", "run.exe", "a//b.py", "con.py", "a/NUL.txt", "COM1.py",
                 "lpt9", "x.py.", "a./b.py", "x y.py", "x\n.py", "x\\y.py", "\u00fcn\u00ef.py", "a/__pycache__/x.pyc", "x.pyc", "./x.py",
                 "a/./b.py", "", "x" * 201, "a:b.py", "x.py:stream"]

    def test_bad_member_names(self):
        for bad in self.BAD_NAMES:
            with self.assertRaises(modules.ModuleError, msg=repr(bad)):
                modules.check_member_names([bad])
        with self.assertRaisesRegex(modules.ModuleError, "duplicate"):
            modules.check_member_names(["rose_conv/A.py", "rose_conv/a.py"])
        modules.check_member_names(["rose_conv/rose_ai.py", "rosebuild.py", "rose_conv/VERSION_SOURCE", "a-b/c_d.json"])

    def zip_bytes(self, files, mj_files=None, name="rose", ver="1.0.0", provides=("x",)):
        mj = {"name": name, "version": ver, "api": 1, "core_min": "", "paths": [""], "provides": list(provides),
              "files": mj_files if mj_files is not None else {k: modules.sha256_bytes(v) for k, v in files.items()}}
        raw = json.dumps(mj).encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("module.json", raw)
            for k, v in files.items():
                zf.writestr(k, v)
        return buf.getvalue(), {"name": name, "version": ver, "api": 1, "module_json_sha256": modules.sha256_bytes(raw)}

    def test_hostile_zips(self):
        for bad in self.BAD_NAMES[:12]:
            data, ent = self.zip_bytes({bad or "empty": b"x = 1\n"})
            dest = os.path.join(self.tmp, "d")
            with self.assertRaises(modules.ModuleError, msg=repr(bad)):
                modules.extract_verified(data, dest, ent)
            self.assertFalse(os.path.exists(dest), bad)
        data, ent = self.zip_bytes({"rosebuild.py": b"x = 1\n", "x.py": b"y"}, mj_files={"rosebuild.py": modules.sha256_bytes(b"x = 1\n")})
        with self.assertRaisesRegex(modules.ModuleError, "differ"):
            modules.extract_verified(data, os.path.join(self.tmp, "d2"), ent)
        data, ent = self.zip_bytes({"rosebuild.py": b"x = 1\n"}, provides=("rosebuild",))
        with self.assertRaisesRegex(modules.ModuleError, "signed manifest"):
            modules.extract_verified(data, os.path.join(self.tmp, "d3"), dict(ent, version="2.0.0"))
        with self.assertRaisesRegex(modules.ModuleError, "signed manifest"):
            modules.extract_verified(data, os.path.join(self.tmp, "d3b"), dict(ent, module_json_sha256="1" * 64))
        data, ent = self.zip_bytes({"rosebuild.py": b"x = 1\n"}, mj_files={"rosebuild.py": "0" * 64}, provides=("rosebuild",))
        with self.assertRaisesRegex(modules.ModuleError, "hash"):
            modules.extract_verified(data, os.path.join(self.tmp, "d4"), dict(ent, name="rose"))

    def test_signed_but_not_allowed_provides_refused(self):
        evil = {"rosebuild.py": b"x = 1\n", "os.py": b"print('pwn')\n", "modules.py": b"pwn = 1\n", "json.py": b"pwn\n",
                "ed25519.py": b"pwn\n", "updater.py": b"pwn\n", "gui.py": b"pwn\n", "sys.py": b"pwn\n"}
        for prov in ("os", "modules", "json", "ed25519", "updater", "gui", "sys", "rose", "rose_conv_stub", "re"):
            root = os.path.join(self.tmp, "r_" + prov)
            files = dict(evil)
            files[prov + ".py"] = b"pwn = 1\n"
            plant(root, "rose", "1.0.0", files, mj_extra={"provides": [prov, "rosebuild"]})
            with self.assertRaisesRegex(modules.ModuleError, "may not provide", msg=prov):
                modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        root = os.path.join(self.tmp, "r_unresolved")                      # allowed name that is not in the tree
        plant(root, "rose", "1.0.0", {"rosebuild.py": b"x=1\n"}, mj_extra={"provides": ["rosebuild", "rosefind"]})
        with self.assertRaisesRegex(modules.ModuleError, "resolve"):
            modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        root = os.path.join(self.tmp, "r_paths")                           # a search path outside the allow-list
        plant(root, "rose", "1.0.0", {"rosebuild.py": b"x=1\n"}, mj_extra={"provides": ["rosebuild"], "paths": ["", "evil"]})
        with self.assertRaisesRegex(modules.ModuleError, "paths"):
            modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        root = os.path.join(self.tmp, "r_name")                            # a module name that is not in ALLOWED
        plant(root, "evilmod", "1.0.0", {"rosebuild.py": b"x=1\n"})
        with self.assertRaisesRegex(modules.ModuleError, "bad module name"):
            modules.verify_installed(root, "evilmod", "1.0.0", (PUB,))
        root = os.path.join(self.tmp, "r_good")
        plant(root, "rose", "1.0.0", {"rosebuild.py": b"x=1\n", "rosefind.py": b"y=2\n"}, mj_extra={"provides": ["rosebuild", "rosefind"]})
        self.assertEqual(modules.verify_installed(root, "rose", "1.0.0", (PUB,))["version"], "1.0.0")

    def test_planted_bytecode_and_extras_refused(self):
        root = os.path.join(self.tmp, "r")
        files = {"rosebuild.py": b"x=1\n"}
        plant(root, "rose", "1.0.0", files, mj_extra={"provides": ["rosebuild"]})
        tree = os.path.join(root, "rose", "1.0.0")
        modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        os.makedirs(os.path.join(tree, "__pycache__"))
        with open(os.path.join(tree, "__pycache__", "rosebuild.cpython-39.pyc"), "wb") as fh:
            fh.write(b"\0" * 16)
        with self.assertRaisesRegex(modules.ModuleError, "__pycache__"):
            modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        shutil.rmtree(os.path.join(tree, "__pycache__"))
        with open(os.path.join(tree, "rosebuild.pyc"), "wb") as fh:
            fh.write(b"\0")
        with self.assertRaisesRegex(modules.ModuleError, "bytecode"):
            modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        os.remove(os.path.join(tree, "rosebuild.pyc"))
        with open(os.path.join(tree, "extra.py"), "w") as fh:
            fh.write("x")
        with self.assertRaisesRegex(modules.ModuleError, "differ"):
            modules.verify_installed(root, "rose", "1.0.0", (PUB,))
        os.remove(os.path.join(tree, "extra.py"))
        os.symlink("/etc/passwd", os.path.join(tree, "link.py"))
        with self.assertRaisesRegex(modules.ModuleError, "symlink"):
            modules.verify_installed(root, "rose", "1.0.0", (PUB,))

    def test_state_with_hostile_values_is_ignored(self):
        victim = os.path.join(self.tmp, "victim")
        os.makedirs(victim)
        open(os.path.join(victim, "keep.txt"), "w").write("x")
        write_state(self.root, {"rose": {"active": "../../victim", "previous": "1.0.0\n"}, "rose\n": {"active": "1.0.0"},
                                "../victim": {"active": "1.0.0"}, "evilmod": {"active": "1.0.0"}})
        st = modules.Store(root=self.root, pubkeys=(PUB,))
        self.assertEqual(st.installed("rose"), (None, None))
        self.assertEqual(st.state()["mods"].get("rose"), {})
        self.assertEqual(sorted(st.state()["mods"]), ["rose"])
        self.assertGreaterEqual(len(st.state()["_bad"]), 4)
        with self.assertRaisesRegex(modules.ModuleError, "no previous"):
            st.rollback("rose")
        st._prune("rose", st.state())
        self.assertTrue(os.path.exists(os.path.join(victim, "keep.txt")))   # nothing outside the cache is ever touched
        os.environ["GSVR_MODULES"] = "1"
        try:
            self.assertEqual(modules.activate(self.root, pubkeys=(PUB,)), {})
        finally:
            del os.environ["GSVR_MODULES"]
        self.assertIn("state", modules.errors())

    def test_set_enabled_does_not_wipe_corrupt_settings(self):
        p = os.path.join(self.tmp, "gui.json")
        with open(p, "w") as fh:
            fh.write('{"worlds": "wrld_x", "broken": ')
        with self.assertRaisesRegex(modules.ModuleError, "unreadable"):
            modules.set_enabled(True, p)
        with open(p) as fh:
            self.assertEqual(fh.read(), '{"worlds": "wrld_x", "broken": ')
        with open(p, "w") as fh:
            fh.write('[1]')
        with self.assertRaises(modules.ModuleError):
            modules.set_enabled(True, p)
        with open(p, "w") as fh:
            json.dump({"worlds": "w"}, fh)
        modules.set_enabled(True, p)
        with open(p) as fh:
            self.assertEqual(json.load(fh), {"worlds": "w", "modules_enabled": True})
        modules.set_enabled(False, os.path.join(self.tmp, "new.json"))     # missing file is created

    def test_frozen_ignores_environment(self):
        os.environ["GSVR_MODULES_DIR"] = os.path.join(self.tmp, "evil")
        os.environ["GSVR_MODULES"] = "1"
        old = getattr(sys, "frozen", None)
        try:
            self.assertEqual(modules.modules_root(), os.path.join(self.tmp, "evil"))      # source run: dev override works
            self.assertTrue(modules.enabled(os.path.join(self.tmp, "none.json")))
            sys.frozen = True
            self.assertNotEqual(modules.modules_root(), os.path.join(self.tmp, "evil"))
            self.assertFalse(modules.enabled(os.path.join(self.tmp, "none.json")))
        finally:
            del os.environ["GSVR_MODULES_DIR"]
            del os.environ["GSVR_MODULES"]
            if old is None:
                del sys.frozen
            else:
                sys.frozen = old


class Loader(Base):
    def run_child(self, code, **env):
        pre = "import sys; sys.path.insert(0, %r)\n" % HERE
        e = dict(os.environ, GSVR_MODULES_DIR=self.root, GSVR_MODULES=env.pop("mods", "1"), **env)
        r = subprocess.run([sys.executable, "-I", "-c", pre + code], capture_output=True, text=True, env=e, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()

    PROBE = ("import modules, os\nmodules.PUBKEYS = %s\nprint(modules.activate())\nprint(modules.errors())\n"
             "import rose_conv, rosebuild, rosefind, rose_ai\n"
             "for m in (rose_conv, rosebuild, rosefind, rose_ai): print(os.path.dirname(os.path.abspath(m.__file__)))\n"
             "print(sys.dont_write_bytecode)\n") % repr((PUB,))
    PROBE_NOKEY = PROBE.replace("modules.PUBKEYS = %s\n" % repr((PUB,)), "")

    def test_module_overrides_builtin_only_when_enabled(self):
        self.rel.publish(1, "1.0.0")
        self.rel.store(self.root).install("rose")
        base = os.path.join(self.root, "rose", "1.0.0")
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{'rose': '1.0.0'}")
        self.assertEqual(out[2:6], [os.path.join(base, "rose_conv"), base, base, os.path.join(base, "rose_conv")])
        self.assertEqual(out[6], "True")                                  # bytecode writing off: the verified tree stays untouched
        for d, dirs, files in os.walk(base):
            self.assertNotIn("__pycache__", dirs)
            self.assertFalse([f for f in files if f.endswith(".pyc")])
        out = self.run_child(self.PROBE, mods="0")
        self.assertEqual(out[0], "{}")
        self.assertEqual(out[2:6], [os.path.join(HERE, "rose_conv"), HERE, HERE, os.path.join(HERE, "rose_conv")])

    def test_no_embedded_key_means_no_activation(self):
        self.rel.publish(1, "1.0.0")
        self.rel.store(self.root).install("rose")
        out = self.run_child(self.PROBE_NOKEY)                           # shipped PUBKEYS = (): installed module is NOT used
        self.assertEqual(out[0], "{}")
        self.assertIn("no signing key", out[1])
        self.assertEqual(out[2], os.path.join(HERE, "rose_conv"))

    def test_forged_local_module_and_state_do_not_load(self):
        evil = {"rosebuild.py": b"import os\nos.system('echo pwned')\n", "rosefind.py": b"y = 1\n"}
        write_state(self.root, {"rose": {"active": "9.9.9"}})
        # 1. forged module.json + state.json, no signed manifest at all
        plant(self.root, "rose", "9.9.9", evil, mj_extra={"provides": ["rosebuild", "rosefind"]}, write_meta=False)
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertIn("no signed manifest", out[1])
        # 2. manifest signed with the attacker's own key
        shutil.rmtree(os.path.join(self.root, "rose"))
        plant(self.root, "rose", "9.9.9", evil, mj_extra={"provides": ["rosebuild", "rosefind"]}, seed=OTHER_SEED)
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertIn("signature", out[1])
        # 3. a genuine signed manifest of 1.0.0 copied next to the forged 9.9.9 tree
        shutil.rmtree(os.path.join(self.root, "rose"))
        write_state(self.root, {})
        self.rel.publish(1, "1.0.0")
        self.rel.store(self.root).install("rose")
        shutil.rmtree(os.path.join(self.root, "rose", "9.9.9"), ignore_errors=True)
        plant(self.root, "rose", "9.9.9", evil, mj_extra={"provides": ["rosebuild", "rosefind"]}, write_meta=False)
        for suf in (".manifest.json", ".manifest.sig"):
            shutil.copy(os.path.join(self.root, "rose", "1.0.0" + suf), os.path.join(self.root, "rose", "9.9.9" + suf))
        write_state(self.root, {"rose": {"active": "9.9.9"}})
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertIn("signed manifest does not contain", out[1])
        # 4. the genuine 1.0.0 tree with a module.json rewritten to the attacker's file list (hash of module.json is bound by the signature)
        write_state(self.root, {"rose": {"active": "1.0.0"}})
        p = os.path.join(self.root, "rose", "1.0.0", "module.json")
        with open(p) as fh:
            mj = json.load(fh)
        mj["files"]["rosebuild.py"] = modules.sha256_bytes(b"import os\n")
        with open(os.path.join(self.root, "rose", "1.0.0", "rosebuild.py"), "w") as fh:
            fh.write("import os\n")
        with open(p, "w") as fh:
            json.dump(mj, fh)
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertIn("module.json does not match the signed manifest", out[1])

    def test_planted_pyc_in_cache_falls_back(self):
        self.rel.publish(1, "1.0.0")
        self.rel.store(self.root).install("rose")
        pc = os.path.join(self.root, "rose", "1.0.0", "rose_conv", "__pycache__")
        os.makedirs(pc)
        with open(os.path.join(pc, "rose_ai.cpython-39.pyc"), "wb") as fh:
            fh.write(b"\0" * 32)
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertIn("__pycache__", out[1])
        self.assertEqual(out[2], os.path.join(HERE, "rose_conv"))

    def test_tampered_cache_falls_back(self):
        self.rel.publish(1, "1.0.0")
        self.rel.publish(2, "1.0.1")
        st = self.rel.store(self.root)
        self.rel.tags = ["modules-1"]
        st.install("rose")
        self.rel.tags = ["modules-1", "modules-2"]
        st.install("rose")
        with open(os.path.join(self.root, "rose", "1.0.1", "rosebuild.py"), "a") as fh:
            fh.write("\nimport os; os.system('echo pwned')\n")
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{'rose': '1.0.0'}")
        self.assertIn("modified", out[1])
        os.remove(os.path.join(self.root, "rose", "1.0.0", "rosefind.py"))
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertEqual(out[2], os.path.join(HERE, "rose_conv"))

    def test_disable_and_incompatible_core(self):
        self.rel.publish(1, "1.0.0")
        st = self.rel.store(self.root)
        st.install("rose")
        st.disable("rose")
        self.assertEqual(self.run_child(self.PROBE)[0], "{}")
        st.rollback("rose")
        p = os.path.join(self.root, "rose", "1.0.0", "module.json")
        with open(p) as fh:
            mj = json.load(fh)
        mj["api"] = 2
        with open(p, "w") as fh:
            json.dump(mj, fh)
        out = self.run_child(self.PROBE)
        self.assertEqual(out[0], "{}")
        self.assertIn("signed manifest", out[1])

    def test_nothing_installed_changes_nothing(self):
        out = self.run_child("import sys, modules\nmodules.PUBKEYS = %s\nn=len(sys.meta_path)\nprint(modules.activate(), len(sys.meta_path)==n)\n"
                             % repr((PUB,)))
        self.assertEqual(out, ["{} True"])


class Tool(Base):
    def test_deterministic_build_and_provides(self):
        a = modtool.build("rose", "1.0.0", os.path.join(self.tmp, "a"))
        b = modtool.build("rose", "1.0.0", os.path.join(self.tmp, "b"))
        self.assertEqual(modules.sha256_file(a), modules.sha256_file(b))
        with zipfile.ZipFile(a) as zf:
            mj = modules.parse_module_json(zf.read("module.json"))
        self.assertTrue({"rose_conv", "rosebuild", "rosefind", "rose_ai", "rosevfs", "rpg1"} <= set(mj["provides"]))
        on_disk = {os.path.splitext(n)[0] for n in os.listdir(os.path.join(HERE, "rose_conv")) if n.endswith(".py") and n != "__init__.py"}
        self.assertTrue(on_disk <= set(mj["provides"]))
        self.assertIn("rose_conv/lobby_default.json", mj["files"])
        self.assertIn("rose_conv/VERSION_SOURCE", mj["files"])
        modules.check_allowed(mj)                                          # the real rose module passes the hard-coded allow-list

    def test_verify_cli(self):
        d = self.rel.publish(1, "1.0.0")
        self.assertEqual(modtool.main(["verify", "--dir", d, "--pub", PUB]), 0)
        with self.assertRaises(modules.ModuleError):
            modtool.main(["verify", "--dir", d, "--pub", OTHER])

    def test_keygen(self):
        k = os.path.join(self.tmp, "k")
        modtool.main(["keygen", "--out", k])
        if os.name != "nt":          # Windows has no POSIX mode bits (st_mode reports 0o666/0o444); the key file ACL is per-user there
            self.assertEqual(os.stat(k).st_mode & 0o077, 0)
        with self.assertRaises(SystemExit):
            modtool.main(["keygen", "--out", k])
        inside = os.path.join(modtool.ROOT, "companion", "build", "evil.key")
        with self.assertRaisesRegex(SystemExit, "inside the repository"):
            modtool.main(["keygen", "--out", inside])
        self.assertFalse(os.path.exists(inside))

    def test_git_pack_uses_head_not_working_tree(self):
        try:
            files = modtool.git_files("rose")
        except SystemExit:
            self.skipTest("no git checkout here (synced tree)")
        self.assertIn("rosebuild.py", files)
        self.assertIn("rose_conv/VERSION_SOURCE", files)
        self.assertTrue(all(f == "rosebuild.py" or f == "rosefind.py" or f.startswith("rose_conv/") for f in files))
        out = os.path.join(self.tmp, "g")
        modtool.build("rose", "1.0.0", out, files=files)
        modules.check_allowed(modules.parse_module_json(zipfile.ZipFile(os.path.join(out, "gsvr-module-rose-1.0.0.zip")).read("module.json")))

    def test_shipped_files_exist(self):
        files = modtool.collect_tree("rose")
        self.assertEqual(sorted(f for f in files if "/" not in f), ["rosebuild.py", "rosefind.py"])


if __name__ == "__main__":
    unittest.main()
