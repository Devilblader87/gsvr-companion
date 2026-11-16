"""VRChat log folder under Wine/Proton, native Linux, Flatpak: candidate search, mtime preference, override, health.

Run: python3 -I companion/test_logwatch_proton.py
The host file system is a temp dir (zroot) standing in for "/" (native) or the Z: drive (Wine).
"""
import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logwatch

REL = os.path.join(*logwatch.PROTON_REL)


def mk_lib(base, log_age=None):
    """base/steamapps + (optional) VRChat log dir with an output_log aged log_age seconds"""
    os.makedirs(os.path.join(base, "steamapps"), exist_ok=True)
    d = os.path.join(base, REL)
    if log_age is not None:
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "output_log_2026-11-15_10-00-00.txt")
        open(f, "w").write("x\n")
        t = time.time() - log_age
        os.utime(f, (t, t))
    return d


def write_vdf(steam_root, libs):
    body = "".join('\t"%d"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n' % (i, p) for i, p in enumerate(libs))
    open(os.path.join(steam_root, "steamapps", "libraryfolders.vdf"), "w").write('"libraryfolders"\n{\n%s}\n' % body)


class ProtonLogTest(unittest.TestCase):
    def setUp(self):
        self.z = tempfile.mkdtemp()
        self.home = os.path.join(self.z, "home", "player")
        os.makedirs(self.home)

    def find(self, **kw):
        return logwatch.default_log_dir(zroot=self.z, home_env=kw.pop("home_env", ""), wine=kw.pop("wine", True), **kw)

    def test_wine_z_mapping_default_steam_root(self):
        want = mk_lib(os.path.join(self.home, ".steam", "debian-installation"), 5)
        self.assertEqual(self.find(), want)           # home found through Z:\home\*, HOME unset

    def test_home_env_posix_translated(self):
        want = mk_lib(os.path.join(self.home, ".local", "share", "Steam"), 5)
        self.assertEqual(self.find(home_env="/home/player"), want)

    def test_native_linux(self):
        want = mk_lib(os.path.join(self.home, ".steam", "steam"), 5)
        self.assertEqual(self.find(wine=False), want)

    def test_flatpak(self):
        want = mk_lib(os.path.join(self.home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"), 5)
        self.assertEqual(self.find(), want)

    def test_vdf_library_via_z(self):
        root = os.path.join(self.home, ".steam", "steam")
        lib = os.path.join(self.z, "mnt", "games", "SteamLibrary")
        mk_lib(root)
        want = mk_lib(lib, 5)
        write_vdf(root, ["/mnt/games/SteamLibrary"])        # POSIX path in the VDF
        self.assertEqual(self.find(), want)

    def test_newest_log_wins(self):
        a = mk_lib(os.path.join(self.home, ".steam", "steam"), 3600)
        b = mk_lib(os.path.join(self.home, ".local", "share", "Steam"), 30)
        c = mk_lib(os.path.join(self.home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"), 7200)
        self.assertEqual(self.find(), b)
        self.assertEqual(len(logwatch.proton_candidates(self.z, "")), 3)
        self.assertNotIn(self.find(), (a, c))

    def test_no_logs_first_existing_dir_else_none(self):
        a = mk_lib(os.path.join(self.home, ".steam", "steam"))
        self.assertIsNone(self.find())                # steam exists, VRChat never ran: no dir to watch yet
        os.makedirs(a)
        self.assertEqual(self.find(), a)

    def test_nothing(self):
        self.assertIsNone(self.find())

    def test_override_wins(self):
        mk_lib(os.path.join(self.home, ".steam", "steam"), 5)
        self.assertEqual(self.find(override="/my/own/dir"), "/my/own/dir")

    def test_health(self):
        d = mk_lib(os.path.join(self.home, ".steam", "steam"), 5)
        self.assertTrue(logwatch.log_health(d)[0])
        old = mk_lib(os.path.join(self.home, ".local", "share", "Steam"), 3 * 3600)
        self.assertFalse(logwatch.log_health(old)[0])
        self.assertFalse(logwatch.log_health(os.path.join(self.z, "nope"))[0])
        self.assertFalse(logwatch.log_health(None)[0])
        self.assertIn("Linux/Proton", logwatch.LOG_WARNING)
        self.assertNotIn(self.z, logwatch.LOG_WARNING)


class StatusScrubTest(unittest.TestCase):
    def test_public_status_has_no_paths(self):
        import argparse, companion
        z = tempfile.mkdtemp()
        a = argparse.Namespace(vrchat_log_dir=os.path.join(z, "secret_user_dir"), wait_max=0, world_allow=None)
        st = companion.State.__new__(companion.State)
        st.args, st.unity_poll_t = a, time.time()
        self.assertFalse(st.log_ok())                 # polled by UnityPlayer, folder has no output_log
        st.unity_poll_t = 0
        st._log_ok_cache = None
        self.assertTrue(st.log_ok())                  # nobody polls: no warning


if __name__ == "__main__":
    unittest.main()
