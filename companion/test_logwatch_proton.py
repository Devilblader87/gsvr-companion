"""logwatch.proton_log_candidates / default selection with a fake Linux Steam tree (no Wine needed)."""
import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logwatch


class ProtonLog(unittest.TestCase):
    def test_finds_prefix_in_home_and_library(self):
        with tempfile.TemporaryDirectory() as t:
            home = os.path.join(t, "home", "u")
            a = os.path.join(home, ".steam", "debian-installation", *logwatch.VRC_SUB)
            lib = os.path.join(t, "games", "SteamLibrary")
            b = os.path.join(lib, *logwatch.VRC_SUB)
            os.makedirs(a); os.makedirs(b)
            os.makedirs(os.path.join(home, ".steam", "debian-installation", "steamapps"), exist_ok=True)
            with open(os.path.join(home, ".steam", "debian-installation", "steamapps", "libraryfolders.vdf"), "w") as fh:
                fh.write('"libraryfolders"\n{\n "1"\n {\n  "path"\t\t"%s"\n }\n}\n' % lib)
            open(os.path.join(a, "output_log_2026-11-15_10-00-00.txt"), "w").close()
            open(os.path.join(b, "output_log_2026-11-15_11-00-00.txt"), "w").close()
            os.utime(os.path.join(a, "output_log_2026-11-15_10-00-00.txt"), (time.time() - 3600,) * 2)
            c = logwatch.proton_log_candidates([home])
            self.assertIn(a, c); self.assertIn(b, c)
            self.assertEqual(max(c, key=logwatch._newest_mtime), b)

    def test_wine_path_mapping(self):
        self.assertEqual(logwatch._wine_z("/home/u/.steam"), "Z:\\home\\u\\.steam")

    def test_none_found(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(logwatch.proton_log_candidates([os.path.join(t, "nobody")]), [])


if __name__ == "__main__":
    unittest.main()
