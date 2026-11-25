"""Companion small update: no client-source words in the texts, the VRChat menu path, close-to-tray decision, world-connected line."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import firstrun  # noqa: E402
import i18n  # noqa: E402
import i18n_catalog  # noqa: E402
import tray  # noqa: E402

BANNED = re.compile(r"searchmmo|jrose|irose|osrose|download a rose|install a rose|ROSEのクライアント|ROSE-Online-Client", re.I)


def all_texts():
    for k, v in i18n.CATALOG.items():
        for i, t in enumerate(v):
            yield k, i, t


class Texts(unittest.TestCase):
    def test_no_client_source_words(self):
        for k, i, t in all_texts():
            self.assertIsNone(BANNED.search(t), (k, i))

    def test_rose_not_found_says_folder_and_discord(self):
        for k in ("err.nf.rose", "help.nf.rose"):
            en, de, ja = i18n.CATALOG[k]
            for t in (en, de, ja):
                self.assertIn("data.idx", t)
                self.assertIn("Discord", t)

    def test_vrchat_path(self):
        for t in i18n.CATALOG["fr.vrc_hint"]:
            self.assertIn("Comfort & Safety", t)
            self.assertIn("Allow Untrusted URLs", t)
            self.assertNotIn("Avatar/Security", t)
            self.assertNotIn("Sicherheit", t)

    def test_new_keys_complete(self):
        for k in ("fr.done_pending", "fr.world_conn", "fr.world_wait", "tray.open", "tray.quit", "tray.hint", "tray.tip"):
            self.assertEqual(len(i18n.CATALOG[k]), 3)
            self.assertTrue(all(t.strip() for t in i18n.CATALOG[k]))


class Close(unittest.TestCase):
    def test_close_action(self):
        self.assertEqual(tray.close_action(True, False), ("hide", True))
        self.assertEqual(tray.close_action(True, True), ("hide", False))
        self.assertEqual(tray.close_action(False, False), ("iconify", True))
        self.assertEqual(tray.close_action(False, True), ("iconify", False))

    def test_non_windows_has_no_tray(self):
        if sys.platform != "win32":
            t = tray.Tray("t", "o", "q")
            self.assertFalse(t.start())
            self.assertFalse(t.ok)


class Icon(unittest.TestCase):
    def test_icon_path_resolves_in_dev(self):
        import ui_theme
        p = ui_theme.icon_path()
        self.assertTrue(p and p.endswith("gsvr.ico") and os.path.isfile(p))

    def test_spec_uses_icon_and_bundles_it(self):
        spec = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "packaging", "gsvr.spec"), encoding="utf-8").read()
        self.assertEqual(spec.count("icon=icon"), 2)
        self.assertIn("([(ICON, \".\")] if icon else [])", spec)

    def test_installer_icon(self):
        iss = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "packaging", "gsvr_setup.iss"), encoding="utf-8").read()
        self.assertIn("SetupIconFile=gsvr.ico", iss)

    def test_load_tray_icon_ok_and_fallbacks(self):
        from unittest import mock
        u = mock.Mock()
        u.LoadImageW.return_value = 77
        self.assertEqual(tray.load_tray_icon(u, "x.ico"), 77)
        self.assertEqual(u.LoadImageW.call_args[0][2:], (1, u.GetSystemMetrics.return_value, u.GetSystemMetrics.return_value, 0x10))
        u.LoadImageW.return_value = 0                            # load failed -> stock icon
        u.LoadIconW.return_value = 5
        self.assertEqual(tray.load_tray_icon(u, "x.ico"), 5)
        u.LoadImageW.side_effect = OSError("boom")               # exception -> stock icon
        self.assertEqual(tray.load_tray_icon(u, "x.ico"), 5)
        u.LoadIconW.assert_called_with(None, 32512)

    def test_set_window_icon_only_windows(self):
        import ui_theme
        from unittest import mock
        w = mock.Mock()
        self.assertFalse(ui_theme.set_window_icon(w, "darwin"))
        w.iconbitmap.assert_not_called()
        self.assertTrue(ui_theme.set_window_icon(w, "win32"))
        w.iconbitmap.side_effect = RuntimeError("x")
        self.assertFalse(ui_theme.set_window_icon(w, "win32"))


if __name__ == "__main__":
    unittest.main()
