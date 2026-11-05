"""Per-mod model pack cache key (companion/models.py, tools/modelpack.py PACK_REV, tools/modelpack_rev_check.py).

Run: python3 -I companion/test_models_cache.py        (no game files needed)
Covers: --version --mod per mod (plain --version still works), a PACK_REV bump moves only that mod's key, adoption of the
0.9.23 global-version cache files, no adoption once the rev is bumped, the golden file agrees with the PACK_REV table, and
the rev check's verdicts.
"""
import hashlib, json, os, shutil, struct, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, "tools")
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS)
import models, modelpack, modelpack_rev_check as rc          # noqa: E402

CONV = os.path.join(TOOLS, "modelpack.py")
MODS = ("cstrike", "valve", "svencoop", "gearbox")


def tiny_pack():
    """Smallest GSM1 v1 pack validate_models accepts."""
    hdr = b"GSM1" + struct.pack("<15i", 64, 1, 1, 1, 0, 0, 1, 3, 1, 0, 8, 8, 1, 0, 0)
    return hdr + b"\0" * (4 * (32 + 8 + 3 + 2 + 6) + 8 * 8 * 4)


def tail(version_str):
    return "v-" + hashlib.sha1(version_str.encode()).hexdigest()[:12]


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mcachetest")
        self.root = os.path.join(self.d, "game")
        for m in MODS:
            os.makedirs(os.path.join(self.root, m, "models"))
            with open(os.path.join(self.root, m, "models", "v_test.mdl"), "wb") as fh:
                fh.write(b"x" * 10)
        self.cache = os.path.join(self.d, "cache")
        os.makedirs(self.cache)
        # fake converter run: writes a valid pack, leaves a marker so a test sees whether it ran
        self.fake = os.path.join(self.d, "fake_conv.py")
        with open(self.fake, "w") as fh:
            fh.write("import sys\nopen(sys.argv[1] + '.ran', 'w').close()\nopen(sys.argv[1], 'wb').write(%r)\n" % tiny_pack())
        self.mb = models.ModelBuilder(CONV, template=f"{{python}} {self.fake} {{out}}", frozen=False)
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def inhash(self, mod):
        h = hashlib.sha1()
        for rel, size, mtime in models.input_files(self.root, mod):
            h.update(f"{rel}\0{size}\0{mtime}\n".encode())
        return h.hexdigest()[:16]

    def ran(self):
        return [f for f in os.listdir(self.cache) if f.endswith(".ran")]


class Versions(Base):
    def test_per_mod_version_command(self):
        for m in MODS:
            out = subprocess.run([sys.executable, "-I", CONV, "--version", "--mod", m], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, modelpack.version_for_mod(m))
            self.assertIn(f"-{m}-r{modelpack.PACK_REV[m]}", out)
        plain = subprocess.run([sys.executable, "-I", CONV, "--version"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(plain, modelpack.source_version())
        self.assertNotIn("-r1", plain)

    def test_mods_have_distinct_cache_versions(self):
        vs = {m: self.mb.mod_version(m) for m in MODS}
        self.assertEqual(len(set(vs.values())), len(MODS))
        self.assertTrue(all(v.startswith("v-") for v in vs.values()))
        self.assertEqual(vs["cstrike"], tail(modelpack.version_for_mod("cstrike")))

    def test_rev_bump_moves_only_that_mod(self):
        before = {m: modelpack.version_for_mod(m) for m in MODS}
        old = dict(modelpack.PACK_REV)
        try:
            modelpack.PACK_REV["gearbox"] += 1
            after = {m: modelpack.version_for_mod(m) for m in MODS}
        finally:
            modelpack.PACK_REV.clear()
            modelpack.PACK_REV.update(old)
        self.assertEqual([m for m in MODS if before[m] != after[m]], ["gearbox"])

    def test_legacy_table_matches_rev_one(self):
        for m, (ver, tails) in models.LEGACY_ADOPT.items():
            self.assertEqual(ver, f"{modelpack.VERSION}-{m}-r1", "rev/VERSION moved: LEGACY_ADOPT is frozen at 0.9.23, remove or leave it")
            self.assertEqual(tails, ("v-6838689b2e54",))
        self.assertEqual(tail("gsm1-2.7-9032733190c4"), "v-6838689b2e54")        # the 0.9.23 global version


class Adopt(Base):
    def old_file(self, mod, t="v-6838689b2e54", data=None):
        p = os.path.join(self.cache, f"models-{mod}-{self.inhash(mod)}-{t}.gsm")
        with open(p, "wb") as fh:
            fh.write(tiny_pack() if data is None else data)
        return p

    def test_build_from_scratch_runs_converter(self):
        r = self.mb.build(self.cache, "cstrike", self.root, log=self.logs.append)
        self.assertFalse(r["cached"])
        self.assertEqual(len(self.ran()), 1)
        self.assertTrue(r["key"].endswith("-" + self.mb.mod_version("cstrike")))
        self.assertTrue(self.mb.build(self.cache, "cstrike", self.root, log=self.logs.append)["cached"])

    def test_adopts_old_cache_without_rebuild(self):
        olds = {m: self.old_file(m) for m in MODS}
        for m in MODS:
            r = self.mb.build(self.cache, m, self.root, log=self.logs.append)
            if f"{modelpack.VERSION}-{m}-r{modelpack.PACK_REV[m]}" != models.LEGACY_ADOPT[m][0]:
                self.assertFalse(r["cached"], m + ": rev bumped since 0.9.23, the old cache must not be adopted")
                continue
            self.assertTrue(r["cached"], m)
            self.assertFalse(os.path.exists(olds[m]), "old file is renamed, not copied")
            self.assertTrue(os.path.isfile(r["path"]))
            self.assertIn(self.mb.mod_version(m), r["path"])
        bumped = [m for m in MODS if f"{modelpack.VERSION}-{m}-r{modelpack.PACK_REV[m]}" != models.LEGACY_ADOPT[m][0]]
        self.assertEqual(len(self.ran()), len(bumped), "only the mods with a bumped rev are rebuilt")
        self.assertTrue(any("adopted" in l for l in self.logs))

    def test_does_not_adopt_other_input_state(self):
        p = os.path.join(self.cache, f"models-cstrike-{'0' * 16}-v-6838689b2e54.gsm")
        with open(p, "wb") as fh:
            fh.write(tiny_pack())
        self.assertFalse(self.mb.build(self.cache, "cstrike", self.root, log=self.logs.append)["cached"])
        self.assertTrue(os.path.isfile(p))

    def test_does_not_adopt_unknown_or_corrupt_old_file(self):
        self.old_file("valve", t="v-000000000000")                          # another (older) global version: output unknown
        self.assertFalse(self.mb.build(self.cache, "valve", self.root, log=self.logs.append)["cached"])
        self.old_file("gearbox", data=b"GSM1garbage")                       # fails validate_models
        self.assertFalse(self.mb.build(self.cache, "gearbox", self.root, log=self.logs.append)["cached"])

    def test_no_adoption_after_rev_bump(self):
        old = self.old_file("cstrike")
        saved = dict(models.LEGACY_ADOPT)
        try:
            models.LEGACY_ADOPT["cstrike"] = ("gsm1-2.7-cstrike-r0-not-the-current-one", saved["cstrike"][1])   # i.e. the mod's version moved on
            r = self.mb.build(self.cache, "cstrike", self.root, log=self.logs.append)
        finally:
            models.LEGACY_ADOPT.update(saved)
        self.assertFalse(r["cached"])
        self.assertTrue(os.path.isfile(old))


class Golden(unittest.TestCase):
    def test_golden_matches_rev_table(self):
        with open(rc.GOLDEN, encoding="utf-8") as fh:
            g = json.load(fh)
        for m in MODS:
            self.assertEqual(g[m]["rev"], modelpack.PACK_REV[m], f"{m}: run tools/modelpack_rev_check.py --update")
            self.assertEqual(len(g[m]["out_sha1"]), 40)

    def test_judge(self):
        gold = {"rev": 1, "out_sha1": "a" * 40}
        self.assertEqual(rc.judge("m", 1, "a" * 40, gold)[0], "ok")
        self.assertEqual(rc.judge("m", 2, "a" * 40, gold)[0], "ok")
        st, msg = rc.judge("m", 1, "b" * 40, gold)
        self.assertEqual(st, "fail")
        self.assertIn("bump PACK_REV", msg)
        self.assertEqual(rc.judge("m", 2, "b" * 40, gold)[0], "fail")      # bumped but golden not updated
        self.assertEqual(rc.judge("m", 1, "a" * 40, None)[0], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=1)
