"""i18n catalog: complete in en/de/ja, same placeholders, language detection, fonts, ETA text. No Tk, no Windows."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import i18n  # noqa: E402


def placeholders(s):
    return sorted(re.findall(r"\{(\w+)\}", s))


class Catalog(unittest.TestCase):
    def test_all_entries_complete(self):
        for k, v in i18n.CATALOG.items():
            self.assertEqual(len(v), 3, k)
            for t in v:
                self.assertTrue(isinstance(t, str) and t.strip(), k)

    def test_placeholders_match(self):
        for k, v in i18n.CATALOG.items():
            self.assertEqual(placeholders(v[0]), placeholders(v[1]), k)
            self.assertEqual(placeholders(v[0]), placeholders(v[2]), k)

    def test_japanese_is_japanese(self):
        jp = re.compile(r"[぀-ヿ一-鿿]")
        same_ok = {"game.cstrike", "game.valve", "game.svencoop", "game.gearbox", "game.war3", "row.rose", "w.name.rose", "w.name.ro", "w.name.cs",
                   "w.name.sven", "how.steam", "how.steam-manifest", "how.none", "fr.game_pct", "tab.log", "g.ok",
                   "cl.wname.rose", "cl.wname.hl", "cl.wname.cs", "cl.wname.war3", "cl.short.rose", "cl.short.cs", "cl.short.war3"}
        for k, v in i18n.CATALOG.items():
            if k not in same_ok:
                self.assertTrue(jp.search(v[2]), "ja text of %s has no Japanese: %r" % (k, v[2]))

    def test_every_key_used_in_code_exists(self):
        keys = set()
        for fn in ("gui.py", "firstrun.py", "firstrun_ui.py", "games.py"):
            p = os.path.join(HERE, fn)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    keys |= set(re.findall(r"""\btr\(\s*["']([a-z0-9_.\-]+)["']""", fh.read()))
        self.assertTrue(keys)
        missing = sorted(k for k in keys if not k.endswith(".") and k not in i18n.CATALOG)
        self.assertEqual(missing, [])

    def test_tr_fallbacks(self):
        i18n.set_lang("ja")
        try:
            self.assertEqual(i18n.tr("nope.key"), "nope.key")
            self.assertIn("127.0.0.1:5", i18n.tr("head.running", port=5))
            self.assertEqual(i18n.tr("head.running"), i18n.CATALOG["head.running"][2])   # missing format arg -> raw text, no crash
        finally:
            i18n.set_lang("en")


class Detect(unittest.TestCase):
    def test_windows_ids(self):
        self.assertEqual(i18n.detect_system_lang(windows_langid=0x0411), "ja")      # ja-JP
        self.assertEqual(i18n.detect_system_lang(windows_langid=0x0407), "de")      # de-DE
        self.assertEqual(i18n.detect_system_lang(windows_langid=0x0C07), "de")      # de-AT
        self.assertEqual(i18n.detect_system_lang(windows_langid=0x0409), "en")
        self.assertEqual(i18n.detect_system_lang(windows_langid=0x040C), "en")      # French -> English fallback

    def test_env(self):
        self.assertEqual(i18n.detect_system_lang(env={"LANG": "ja_JP.UTF-8"}), "ja")
        self.assertEqual(i18n.detect_system_lang(env={"LC_ALL": "de_DE.UTF-8", "LANG": "ja_JP"}), "de")
        self.assertEqual(i18n.detect_system_lang(env={"LANG": "C"}), "en")
        self.assertEqual(i18n.detect_system_lang(env={}), "en")

    def test_resolve(self):
        self.assertEqual(i18n.resolve("ja"), "ja")
        self.assertEqual(i18n.resolve("DE"), "de")
        self.assertEqual(i18n.resolve("xx"), "en")
        self.assertEqual(i18n.resolve("auto", windows_langid=0x0411), "ja")
        self.assertEqual(i18n.resolve("", windows_langid=0x0407), "de")


class Fonts(unittest.TestCase):
    def test_pick(self):
        fams = ["Arial", "Segoe UI", "Yu Gothic UI", "Meiryo UI"]
        self.assertEqual(i18n.pick_font(fams, "ja"), "Yu Gothic UI")
        self.assertEqual(i18n.pick_font(fams, "en"), "Segoe UI")
        self.assertEqual(i18n.pick_font(["Arial", "meiryo ui"], "ja"), "meiryo ui")
        self.assertEqual(i18n.pick_font(["Arial"], "ja"), "")


class Eta(unittest.TestCase):
    def test_eta(self):
        self.assertEqual(i18n.format_eta(-1), "")
        self.assertEqual(i18n.format_eta(None), "")
        i18n.set_lang("ja")
        try:
            self.assertEqual(i18n.format_eta(42), "あと42秒")
            self.assertEqual(i18n.format_eta(95), "あと約2分")
        finally:
            i18n.set_lang("en")
        self.assertEqual(i18n.tr_in("de", "eta.min", n=3), "noch ca. 3 Min")


if __name__ == "__main__":
    unittest.main()
