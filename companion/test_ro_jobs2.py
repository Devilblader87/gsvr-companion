"""RO second jobs (w37 jobs2, docs/design/ro_jobs2.md): job body sprites (ro_conv/jobs.py) on a SYNTHETIC client, the start pack integration,
the table generator tools/ro_job2db.py (YAML subset reader + rows) and the committed table Editor/ro_jobs2.txt. No game data.
Mac only (skipped elsewhere): the owner's iRO data.grf has every job body; the generator reproduces ro_jobs2.txt from the rAthena files in $RO_RA_DIR.

  cd companion && python3 -I test_ro_jobs2.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'tools'))
from ro_conv import sprite as S, jobs as J, api, grf  # noqa: E402
import test_ro_sprite as RS  # noqa: E402
import test_ro_e2e as E  # noqa: E402
import ro_job2db as G  # noqa: E402

TABLE = os.path.join(REPO, 'unity/Assets/GoldSrcVR/Editor/ro_jobs2.txt')
OWNER = os.environ.get('GSVR_RO_INSTALL', '/path/to/gsvr-testdata Online')
RA_DIR = os.environ.get('RO_RA_DIR', '/path/to/gsvr-testdata')


class _Vfs:
    def __init__(self, files):
        self.files = files

    def read(self, path):
        return self.files[path]


def _body_files(jobs_sexes):
    bs, ba = RS._fixture_body()
    out = {}
    for job, sx in jobs_sexes:
        p = J.body_path(job, sx)
        out[p + '.spr'] = bs
        out[p + '.act'] = ba
    return out


class JobBodies(unittest.TestCase):
    def test_names_and_paths(self):
        self.assertEqual(J.body_name(7, 'm'), 'body_j7_m')
        self.assertEqual(J.body_path(7, 'f'), 'data/sprite/인간족/몸통/여/기사_여')
        self.assertEqual(J.body_path(13, 'm'), 'data/sprite/인간족/몸통/남/페코페코_기사_남')
        self.assertIsNone(J.body_path(19, 'f'))          # Bard: male only
        self.assertIsNone(J.body_path(20, 'm'))          # Dancer: female only
        self.assertIsNone(J.body_path(1, 'm'))           # first jobs are not this lane's
        self.assertEqual(sorted(J.JOB_ORDER), sorted(J.JOB_BODY))
        self.assertEqual(len(J.JOB_ORDER), 15)

    def test_convert_all_present(self):
        pairs = [(j, s) for j in J.JOB_ORDER for s in ('m', 'f') if J.body_path(j, s)]
        self.assertEqual(len(pairs), 28)
        w = []
        sp = J.job_sprites(_Vfs(_body_files(pairs)), warn=w)
        self.assertEqual(w, [])
        self.assertEqual([x['name'] for x in sp][:4], ['body_j7_m', 'body_j7_f', 'body_j13_m', 'body_j13_f'])
        self.assertEqual(len(sp), 28)
        self.assertEqual(sp[0]['kind'], S.KIND_BODY)
        rsat, rsan = S.build_set(sp)
        d = S.dec_rsan(rsan)
        self.assertEqual([x['name'] for x in d['sprites']], [x['name'] for x in sp])
        self.assertEqual(d['sprites'][0]['clips'], 8)            # idle only: 8 directions

    def test_idle_only_by_default(self):
        sp = J.job_sprites(_Vfs(_body_files([(7, 'm')])), sexes=('m',))
        self.assertEqual(J.START_ANIMS, (S.ANIM_IDLE,))
        self.assertEqual(len(sp), 1)
        sp2 = J.job_sprites(_Vfs(_body_files([(7, 'm')])), sexes=('m',), anims=(S.ANIM_IDLE, S.ANIM_WALK))
        self.assertEqual((len(sp[0]['clips']), len(sp2[0]['clips'])), (8, 16))
        self.assertEqual(sum(len(c['frames']) for c in sp[0]['clips'][:8]), 8)

    def test_missing_one_warning_no_path(self):
        w = []
        sp = J.job_sprites(_Vfs(_body_files([(9, 'm')])), warn=w)
        self.assertEqual([x['name'] for x in sp], ['body_j9_m'])
        self.assertEqual(len(w), 1, w)
        self.assertTrue(w[0].startswith('sprites: 27 job bodies not in the client data'), w[0])
        self.assertNotIn('/', w[0])
        self.assertNotIn('인간족', w[0])

    def test_broken_file_warns(self):
        f = _body_files([(7, 'm')])
        f[J.body_path(7, 'm') + '.act'] = b'XX\x00\x00'
        w = []
        sp = J.job_sprites(_Vfs(f), sexes=('m',), warn=w)
        self.assertEqual(sp, [])
        self.assertTrue(any(x.startswith('sprites: body_j7_m:') for x in w), w)


class StartPack(unittest.TestCase):
    def test_sprite_sections_append_jobs_after_others(self):
        files = E.client_files(True)
        for k, v in _body_files([(7, 'm'), (7, 'f'), (13, 'm')]).items():
            files[k.replace('/', '\\')] = v
        with tempfile.TemporaryDirectory() as d:
            grf.write_grf(os.path.join(d, 'data.grf'), files)
            v = grf.Vfs(d)
            w = []
            secs = api.sprite_sections(v, warn=w)
            names = [x['name'] for x in S.dec_rsan(secs[1][3])['sprites']]
            self.assertEqual(secs[1][2], 2 + 1 + 3 + 2)    # body_m head_m poring + 3 job bodies (no female Novice in the fixture) + w43 equip's indexed hair 1 / body
            self.assertEqual(names, ['body_m', 'head_m', 'poring', 'body_j7_m', 'body_j7_f', 'body_j13_m', 'hi_1_m', 'bi_m'])
            self.assertTrue(any('job bodies not in the client data' in x for x in w), w)
            api.JOB2_SPRITES = False
            try:
                self.assertEqual(api.sprite_sections(v, warn=[])[1][2], 3 + 2)
            finally:
                api.JOB2_SPRITES = True

    def test_pack_rev_bumped(self):
        self.assertGreaterEqual(api.PACK_REV['prontera'], 3)
        self.assertTrue(api.JOB2_SPRITES)


class Generator(unittest.TestCase):
    def test_yaml_subset(self):
        y = ("Header:\n  Type: SKILL_DB\n  Version: 3\n\nBody:\n  - Id: 62\n    Name: KN_BOWLINGBASH   # c\n    MaxLevel: 10\n    Requires:\n      SpCost:\n"
             "        - Level: 1\n          Amount: 13\n        - Level: 2\n          Amount: 14\n    CastTime: 700\n    Flags:\n      TargetTrap: true\n  - Id: 63\n    Name: \"KN_RIDING\"\n")
        d = G.load_yaml(y)
        self.assertEqual(d['Header']['Version'], 3)
        b = d['Body']
        self.assertEqual(len(b), 2)
        self.assertEqual(b[0]['Name'], 'KN_BOWLINGBASH')
        self.assertEqual(b[0]['Requires']['SpCost'][1], {'Level': 2, 'Amount': 14})
        self.assertIs(b[0]['Flags']['TargetTrap'], True)
        self.assertEqual(b[1]['Name'], 'KN_RIDING')
        self.assertEqual(G._per_level(b[0]['Requires']['SpCost'], 'Amount', 4), [13, 14, 14, 14])
        self.assertEqual(G._per_level(700, 'Time', 3), [700, 700, 700])
        self.assertEqual(G._per_level(None, 'Time', 2), [0, 0])

    def test_design_tables(self):
        ids = [j[0] for j in G.JOBS]
        self.assertEqual(sorted(ids), [7, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18, 19, 20])
        self.assertEqual(set(G.TASKS), set(ids))
        self.assertEqual(set(G.SIGNATURE), set(ids))
        for j, v in G.SIGNATURE.items():
            self.assertTrue(4 <= len(v) <= 6, (j, v))
        for j, (k, tid, c) in G.TASKS.items():
            self.assertIn(k, (1, 2))
            if k == 1:
                self.assertIn(tid, (1002, 1008, 1063, 1113))
        self.assertEqual({j[5] for j in G.JOBS} - {'prontera', 'geffen', 'payon', 'alberta', 'morocc'}, set())

    def test_committed_table(self):
        with open(TABLE, encoding='utf-8') as f:
            t = f.read()
        rows = [l.split() for l in t.splitlines() if l and not l.startswith('#')]
        kinds = {}
        for r in rows:
            kinds[r[0]] = kinds.get(r[0], 0) + 1
        self.assertEqual(kinds['job'], 13)
        self.assertEqual(kinds['jbonus'], 13)
        self.assertEqual(kinds['jexp2'], 1)
        self.assertEqual((kinds['hp'], kinds['sp'], kinds['aspd']), (13, 13, 13))
        for k in ('lvsp', 'lvcast', 'lvdelay', 'lvcool', 'lvdur'):
            self.assertEqual(kinds[k], kinds['lvsp'], k)
            self.assertGreater(kinds[k], 150)
        self.assertLess(kinds['skill'], 320)
        self.assertLess(kinds['tree'], 320)
        sig = {r[-1] for r in rows if r[0] == 'skill' and r[10] == '1'}
        self.assertEqual(sig, {n for v in G.SIGNATURE.values() for n in v})
        for r in rows:                       # numbers only (+ aegis / job / town names): no free text from the game
            if r[0] == 'skill':
                self.assertRegex(r[-1], r'^[A-Z]{2,3}_[A-Z0-9_]+$')
            if r[0] == 'job':
                self.assertEqual(len(r), 14)


@unittest.skipUnless(os.path.isfile(os.path.join(OWNER, 'data.grf')), 'owner iRO client not on this machine')
class OwnerClient(unittest.TestCase):
    def test_every_job_body_present(self):
        v = grf.Vfs(OWNER)
        names = set(n.replace('\\', '/').lower() for n in v.names())
        miss = []
        for j in J.JOB_ORDER:
            for s in ('m', 'f'):
                p = J.body_path(j, s)
                if p and (p + '.spr') not in names:
                    miss.append(J.body_name(j, s))
        self.assertEqual(miss, [])


@unittest.skipUnless(os.path.isfile(os.path.join(RA_DIR, 'db_pre-re_skill_db.yml')) or os.path.isfile(os.path.join(RA_DIR, 'skill_db.yml')), 'rAthena files not here')
class Regenerate(unittest.TestCase):
    def test_table_reproduces(self):
        with open(TABLE, encoding='utf-8') as f:
            self.assertEqual(G.build(RA_DIR), f.read())


if __name__ == '__main__':
    unittest.main(verbosity=1)
