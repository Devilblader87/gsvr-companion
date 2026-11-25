"""RAGNAROK prototype world lane (docs/design/ro_world.md): static checks of the world entry / builder / scripts and the pure helpers of
GsvrRoPlay + GsvrRoBoard compiled with mono (skipped when mono's csc is missing). No Unity, no game files, no network."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UN = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR")
SCRIPTS, EDITOR = os.path.join(UN, "Scripts"), os.path.join(UN, "Editor")


def rd(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(os.path.isdir(UN), "no repo checkout (frozen / source zip)")
class RoWorld(unittest.TestCase):
    def entry(self):
        ws = [w for w in json.loads(rd(EDITOR, "gsvr_worlds.json"))["worlds"] if w["scene"].endswith("RagnarokTest.unity")]
        self.assertEqual(len(ws), 1)
        return ws[0]

    def test_world_entry(self):
        e = self.entry()
        self.assertEqual(e["name"], "GSVR - RO")                   # owner 2026-11-25 (public name)
        self.assertTrue(e["worldId"] == "" or e["worldId"].startswith("wrld_"))  # empty = the first upload creates the private world (GsvrWorldUpload), the id is written back
        self.assertTrue(e["label"].startswith("Ragnarok test"))  # GsvrWorldUpload.UpRagnarok -> Headless("Ragnarok test")
        self.assertLessEqual(e["recommendedCapacity"], e["capacity"])
        self.assertLessEqual(e["capacity"], 16)                  # GsvrRoPlay.MaxSlots sprite slots
        self.assertLessEqual(len(e["description"]), 430)
        self.assertIn("play.heimserver.me/r", e["description"])

    def test_low_profile_wording(self):
        lits = re.findall(r'"((?:[^"\\]|\\.)*)"', rd(SCRIPTS, "GsvrRoBoard.cs"))          # string literals only (comments may say "no logos")
        txt = " ".join([self.entry()["description"]] + lits).lower()
        for bad in (r"\bfree\b", r"(?<!un)official", r"\blogos?\b", r"https?://", r"download the game", r"\biso\b", r"torrent"):
            self.assertIsNone(re.search(bad, txt), bad)
        self.assertIn("unofficial", txt)

    def test_builder_wiring(self):
        b = rd(EDITOR, "GsvrSceneBuilder.Ragnarok.cs")
        self.assertIn('[MenuItem("GoldSrcVR/Build Ragnarok Test Scene")]', b)
        self.assertIn('"/pack/ro.pack"', b)                      # companion route (companion.py / ro.py, ro-proto: one name on both sides)
        self.assertIn('"Assets/GoldSrcVR/Scenes/RagnarokTest.unity"', b)
        self.assertNotIn("ro.rpg1", b)
        self.assertEqual(b.count('[MenuItem("GoldSrcVR/Build Ragnarok Test Scene")]')
                         + rd(EDITOR, "GsvrSceneBuilder.RoTest.cs").count('[MenuItem("GoldSrcVR/Build Ragnarok Test Scene")]'), 1)   # one menu, one scene
        for w in ("Add<GsvrRoProbe>", "Add<GsvrRoTest>", "autoTest.run = false", "Add<GsvrRoSpriteSet>", "Add<GsvrRoSprite>", "play.probe = probe",
                  "fetch.spriteSet = spriteSet", "spriteSet.listener = play", '"GoldSrcVR/RoSprite"'):
            self.assertIn(w, b)
        f = rd(SCRIPTS, "GsvrRoFetch.cs")
        self.assertIn('"[GSVR-REQ] ro="', f)
        self.assertIn('"ro", TokenType.DataDictionary', f)
        all_types = re.search(r"AllTypes\s*=\s*\{(.*?)\};", rd(EDITOR, "GsvrSceneBuilder.cs"), re.S).group(1)
        for t in ("GsvrRoFetch", "GsvrRoPlay", "GsvrRoBoard", "GsvrRoSpriteSet", "GsvrRoSprite", "GsvrRoProbe", "GsvrRoTest"):
            self.assertIn("typeof(%s)" % t, all_types)
        self.assertIn('Headless("Ragnarok test")', rd(EDITOR, "GsvrWorldUpload.cs"))

    def test_rose_untouched(self):
        # the lane adds files; ROSE scripts / builders stay as they are (only shared registries got a line)
        for f in ("GsvrRpg1Loader.cs", "GsvrRpg1Fetch.cs", "GsvrRoseNet.cs"):
            self.assertNotIn("GsvrRo" + "Play", rd(SCRIPTS, f))

    def test_sprite_contract_in_one_place(self):
        play = rd(SCRIPTS, "GsvrRoPlay.cs")
        for name in ("public GsvrRoSprite[] slotSprites", "sp2.facingYaw = ", "GsvrRoSprite.AnimWalk", "public void _OnRoSpritesReady()", "probe.goalDoneSeq = probe.goalSeq"):
            self.assertIn(name, play)
        sp = rd(SCRIPTS, "GsvrRoSprite.cs")
        for name in ("public float facingYaw", "public int anim", "AnimIdle = 0, AnimWalk = 1"):
            self.assertIn(name, sp)
        f = rd(SCRIPTS, "GsvrRoFetch.cs")
        self.assertLess(f.index("spriteSet.BuildFromBytes(bytes)"), f.index("loader.BuildFromBytes(bytes)"))     # the loader may drop the bytes


MONO = shutil.which("csc") or shutil.which("mcs")

STUB = r"""
using System;
static class Mathf {
  public const float Rad2Deg = 57.29578f;
  public static int RoundToInt(float f) { return (int)Math.Round(f, MidpointRounding.ToEven); }
  public static float Atan2(float y, float x) { return (float)Math.Atan2(y, x); }
  public static float Sqrt(float f) { return (float)Math.Sqrt(f); }
}
static class GsvrRoFetch { public const int PubIdle = 0, PubStatus = 1, PubPack = 2, PubDone = 3, PubFailed = 4; }
"""

MAIN = r"""
static class M {
  static int fails;
  static void C(bool c, string w) { if (!c) { Console.WriteLine("FAIL " + w); fails++; } }
  static int Main() {
    // heading: 0 = +Z, clockwise, 8 directions
    C(Play.LDirOf(0f) == 0 && Play.LDirOf(45f) == 1 && Play.LDirOf(90f) == 2 && Play.LDirOf(180f) == 4 && Play.LDirOf(-90f) == 6 && Play.LDirOf(-180f) == 4 && Play.LDirOf(359f) == 0 && Play.LDirOf(-22f) == 0 && Play.LDirOf(-23f) == 7, "LDirOf");
    for (int y = -720; y <= 720; y++) { int d = Play.LDirOf(y); C(d >= 0 && d <= 7, "LDirOf range " + y); }
    C(Play.LDirOf(Play.LYawOf(0f, 1f)) == 0 && Play.LDirOf(Play.LYawOf(1f, 0f)) == 2 && Play.LDirOf(Play.LYawOf(0f, -1f)) == 4 && Play.LDirOf(Play.LYawOf(-1f, 0f)) == 6 && Play.LDirOf(Play.LYawOf(1f, 1f)) == 1, "LYawOf axes");
    // walk / idle hysteresis, teleports are no walks
    C(!Play.LMoving(0.4f, false) && Play.LMoving(0.6f, false) && Play.LMoving(0.3f, true) && !Play.LMoving(0.2f, true) && !Play.LMoving(100f, true) && !Play.LMoving(100f, false), "LMoving");
    // slots
    int[] pid = new int[4];
    C(Play.LFreeSlot(pid, 4) == 0, "free first"); pid[0] = 7; pid[1] = 9;
    C(Play.LFreeSlot(pid, 4) == 2 && Play.LFindSlot(pid, 4, 9) == 1 && Play.LFindSlot(pid, 4, 5) == -1, "find / free");
    pid[2] = 1; pid[3] = 2; C(Play.LFreeSlot(pid, 4) == -1, "full");
    // local sprite hidden inside the head column
    C(Play.LLocalHidden(0.1f, 0.1f, 0f, 0f, 0.5f) && !Play.LLocalHidden(0.6f, 0f, 0f, 0f, 0.5f), "LLocalHidden");
    // grid fix: two bad checks, grace over, cooldown over
    C(!Play.LNeedFix(true, 5, 10f, 0f, 0f) && !Play.LNeedFix(false, 1, 10f, 0f, 0f) && Play.LNeedFix(false, 2, 10f, 5f, 9f) && !Play.LNeedFix(false, 2, 4f, 5f, 0f) && !Play.LNeedFix(false, 2, 10f, 5f, 11f), "LNeedFix");
    C(Play.LUseGood(100f, 90f, 20f) && !Play.LUseGood(100f, 70f, 20f) && !Play.LUseGood(100f, -1000f, 20f) && !Play.LUseGood(5f, 0f, 20f), "LUseGood");
    C(Play.LFell(-5f, 2f, 3f) && !Play.LFell(0f, 2f, 3f), "LFell");
    // RO direction of a world heading (probe.playerDir): N 4, E 6, S 0, W 2
    C(Play.LRoDir(0) == 4 && Play.LRoDir(2) == 6 && Play.LRoDir(4) == 0 && Play.LRoDir(6) == 2 && Play.LRoDir(1) == 5 && Play.LRoDir(7) == 3, "LRoDir");
    { byte[] cells = new byte[] { 1, 0, 2, 0 }, gat = new byte[4]; Play.LGatFromCells(cells, gat, 4); C(gat[0] == 0 && gat[1] == 1 && gat[2] == 0 && gat[3] == 1, "LGatFromCells"); }
    { float[] r = new float[2];
      C(!Play.LStepToward(0f, 0f, 10f, 0f, 1f, r) && Math.Abs(r[0] - 1f) < 1e-5 && r[1] == 0f, "LStepToward step");
      C(Play.LStepToward(9.5f, 0f, 10f, 0f, 1f, r) && r[0] == 10f, "LStepToward arrives"); }
    // board status
    C(Board.LKind(true, false, false, false, 0, false, 0) == Board.SkReady, "ready wins");
    C(Board.LKind(false, true, true, false, 1, false, 0) == Board.SkFailed && Board.LKind(false, false, false, true, 1, false, 0) == Board.SkFailed, "failed");
    C(Board.LKind(false, false, true, false, 3, false, 0) == Board.SkTown && Board.LKind(false, false, false, false, 2, false, 0) == Board.SkDownload, "town / download");
    C(Board.LKind(false, false, false, false, 1, true, 0) == Board.SkBuild && Board.LKind(false, false, false, false, 1, false, 2) == Board.SkBlocked && Board.LKind(false, false, false, false, 1, false, 0) == Board.SkAsk && Board.LKind(false, false, false, false, 0, false, 0) == Board.SkLook, "build / blocked / ask / look");
    C(Board.LLine(Board.SkTown, 42, "").Contains("42%") && Board.LLine(Board.SkFailed, 0, "boom").Contains("boom") && Board.LLine(Board.SkBuild, 7, "").Contains("7%"), "LLine");
    if (fails == 0) Console.WriteLine("ro world pure helpers ok");
    return fails == 0 ? 0 : 1;
  }
}
"""


def region(src, start, end):
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


@unittest.skipUnless(MONO and os.path.isdir(UN), "mono csc or the repo is missing")
class RoPure(unittest.TestCase):
    def test_pure_helpers(self):
        play = region(rd(SCRIPTS, "GsvrRoPlay.cs"), "    public const float MoveOn", "    // ═════════════════════════ setup")
        bsrc = rd(SCRIPTS, "GsvrRoBoard.cs")
        board = region(bsrc, "    public const string ColOk", "    private float _readyT") + region(bsrc, "    /// <summary>Pure: which status", "    void Start()")
        src = STUB + "static class Play {\n" + play + "\n}\nstatic class Board {\n" + board + "\n}\n" + MAIN
        d = tempfile.mkdtemp(prefix="ro_world_")
        try:
            cs, exe = os.path.join(d, "t.cs"), os.path.join(d, "t.exe")
            with open(cs, "w", encoding="utf-8") as fh:
                fh.write(src)
            r = subprocess.run([MONO, "-nologo", "-out:" + exe, cs], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            mono = shutil.which("mono")
            r = subprocess.run([mono, exe] if mono else [exe], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("ok", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
