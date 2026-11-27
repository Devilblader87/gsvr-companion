"""RO second jobs complete (w46 jobs2, docs/design/ro_jobs2.md "w46"): the generated tables Editor/ro_skills2.txt (tools/ro_skills_gen.py --second) and Editor/ro_jobs2q.txt
(tools/ro_jobs2q_gen.py), the stand-in NPC sprites of the start pack (api.JOB2_NPC_IDS). No game data in git.
Everywhere: the committed quest table == the generator's hand data (stand-in cells kept), every stand-in sprite id is in JOB2_NPC_IDS / NPC_SPRITES, PACK_REV prontera >= 9.
Mac only (skipped elsewhere): the rAthena scripts in $RO_RA_NPC (every number of the quests) and $RO_RA_DIR (skill_db / skill_tree: ro_skills2.txt == a fresh generation),
the owner's client (all 19 stand-in sprites resolve through npcidentity / jobname).

  cd companion && python3 -I test_ro_jobs2all.py
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'tools'))
from ro_conv import api  # noqa: E402
import ro_jobs2q_gen as Q  # noqa: E402

QT = os.path.join(REPO, 'unity/Assets/GoldSrcVR/Editor/ro_jobs2q.txt')
S2 = os.path.join(REPO, 'unity/Assets/GoldSrcVR/Editor/ro_skills2.txt')
RA_NPC = os.environ.get('RO_RA_NPC', '/path/to/gsvr-testdata')
RA_DIR = os.environ.get('RO_RA_DIR', '/path/to/gsvr-testdata')
RA_DB = os.environ.get('RO_RA_DB', '/path/to/gsvr-testdata')
OWNER = os.environ.get('GSVR_RO_INSTALL', '/path/to/gsvr-testdata Online')


class Jobs2All(unittest.TestCase):
    def test_quest_table_is_the_hand_data(self):
        with open(QT, encoding='utf-8') as f:
            cur = f.read()
        self.assertEqual(Q.build(None, Q.committed_cells(cur)), cur)

    def test_every_job_has_its_chain(self):
        self.assertEqual(sorted(Q.JOBS), [7, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18, 19, 20])
        for j, J in Q.JOBS.items():
            self.assertEqual(J['steps'][0][0], 'talk', J['name'])
            self.assertEqual(J['steps'][-1][0], 'change', J['name'])
            self.assertLessEqual(len(J['steps']), 9, J['name'])          # the step lives in a quest ext slot count (0..99) - the chain stays short

    def test_standin_sprites_in_the_start_pack(self):
        ids = set()
        with open(QT, encoding='utf-8') as f:
            for ln in f:
                p = ln.split()
                if p and p[0] == 'npc':
                    ids.add(int(p[6]))
        self.assertTrue(ids <= set(api.JOB2_NPC_IDS) | set(api.NPC_SPRITES), sorted(ids - set(api.JOB2_NPC_IDS) - set(api.NPC_SPRITES)))
        self.assertGreaterEqual(api.PACK_REV['prontera'], 9)

    def test_skills2_table(self):
        with open(S2, encoding='utf-8') as f:
            t = f.read()
        rows = [ln.split() for ln in t.splitlines() if ln and not ln.startswith('#')]
        self.assertEqual(sum(1 for r in rows if r[0] == 'skill'), 214)
        self.assertEqual(sum(1 for r in rows if r[0] == 'tree'), 240)
        self.assertTrue(all(7 <= int(r[1]) <= 20 and int(r[1]) not in (13,) for r in rows if r[0] == 'tree'))

    @unittest.skipUnless(os.path.isdir(os.path.join(RA_NPC, 'jobs', '2-1')), 'rAthena npc scripts not here (Mac only)')
    def test_numbers_against_rathena(self):
        r = subprocess.run([sys.executable, '-I', os.path.join(REPO, 'tools', 'ro_jobs2q_gen.py'), 'check', '--ra', RA_NPC], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skipUnless(os.path.isfile(os.path.join(RA_DIR, 'db_pre-re_skill_db.yml')), 'rAthena db not here (Mac only)')
    def test_skills2_fresh(self):
        r = subprocess.run([sys.executable, '-I', os.path.join(REPO, 'tools', 'ro_skills_gen.py'), '--ra', RA_DIR, '--ra', RA_DB, '--check2', S2], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skipUnless(os.path.isdir(OWNER), 'owner client not here (Mac only)')
    def test_standins_resolve_in_the_owner_client(self):
        from ro_conv import grf
        warn = []
        out = api.job2_npc_sprites(grf.Vfs(OWNER), warn)
        self.assertEqual(len(out), len([s for s in api.JOB2_NPC_IDS if s not in api.NPC_SPRITES]), warn)
        self.assertEqual(warn, [])


if __name__ == '__main__':
    unittest.main()
