"""ROSE converter packaging guard (lane E): every module of rose_conv/SOURCE_HASHES must be a hidden import of gsvr.spec
(several are imported only inside functions, PyInstaller cannot see them), the data files and pathex must be listed, and
miniaudio pinned. Same idea as toolfiles.missing_imports.

  cd companion && python3 -I -m unittest discover -p "test_rose_packaging.py"
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "packaging", "gsvr.spec")
CONV = os.path.join(HERE, "rose_conv")
sys.path.insert(0, HERE)


def _spec():
    with open(SPEC, encoding="utf-8") as f:
        return f.read()


def _hash_modules():
    with open(os.path.join(CONV, "SOURCE_HASHES"), encoding="utf-8") as f:
        return [os.path.splitext(ln.split()[-1])[0] for ln in f.read().splitlines() if ln.strip()]


class RosePackaging(unittest.TestCase):
    def test_hash_list_complete(self):
        mods = _hash_modules()
        self.assertGreaterEqual(len(mods), 23)
        on_disk = {os.path.splitext(n)[0] for n in os.listdir(CONV) if n.endswith(".py") and n != "__init__.py"}
        self.assertEqual(set(mods), on_disk)

    def test_spec_hiddenimports_use_rose_modules(self):
        s = _spec()
        self.assertIn("SOURCE_HASHES", s)
        self.assertIn("ROSE_MODULES", s.split("hiddenimports=")[1])

    def test_spec_listing_evaluates_to_all_modules(self):
        """run the spec's ROSE block (no PyInstaller needed) and compare with SOURCE_HASHES"""
        s = _spec()
        ns = {"os": os, "COMP": HERE}
        exec(s[s.index("ROSE_CONV = "):s.index("ICON = ")], ns)      # noqa: S102 - our own spec text
        self.assertEqual(set(ns["ROSE_MODULES"]), set(_hash_modules()) | {"rose_conv"})
        self.assertEqual(sorted(os.path.basename(p) for p, d in ns["ROSE_DATA"]), ["VERSION_SOURCE", "lobby_default.json"])
        for p, d in ns["ROSE_DATA"]:
            self.assertTrue(os.path.isfile(p), p)
            self.assertEqual(d, "rose_conv")

    def test_spec_pathex_and_data(self):
        s = _spec()
        self.assertIn('os.path.join(COMP, "rose_conv")]', s.split("pathex=")[1].split("\n")[0])
        self.assertIn("ROSE_DATA", s.split("datas=")[1].split("hiddenimports=")[0])

    def test_miniaudio_pinned(self):
        with open(os.path.join(HERE, "packaging", "requirements-build.txt"), encoding="utf-8") as f:
            txt = f.read()
        self.assertTrue(any(ln.startswith("miniaudio==") for ln in txt.splitlines()))

    def test_smoke_check(self):
        import gsvr_main
        self.assertTrue(gsvr_main.rose_smoke())


if __name__ == "__main__":
    unittest.main()
