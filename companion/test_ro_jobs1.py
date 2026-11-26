"""RO first jobs lane (docs/design/ro_jobs1.md, w41): the job-test maps in the converter + the world tables, the NPC sprites, the generated data.

  - ro_conv.jobmaps: ids 43..49 after the dungeons, on demand (NO_PREBUILD, no warm-up), the same names / labels / pool needs as GsvrSceneBuilder.Ragnarok,
  - every j1_ NPC sprite id of Editor/ro_jobs1.txt is converted (NPC_SPRITES), the hidden NPC 111 is not; PACK_REV of the start pack bumped,
  - Mac only (skipped elsewhere): the measured packs == jobmaps.MEASURED, the owner's client has the 7 maps + every NPC sprite, and tools/ro_jobs1_gen.py check passes
    (committed tables == a fresh generation from the rAthena files, every NPC / warp / script target cell against the converted packs, the course has a trap-free path).
"""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'rose_conv'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from ro_conv import api, culvert, dungeons, jobmaps, towns  # noqa: E402

ED = os.path.join(ROOT, 'unity', 'Assets', 'GoldSrcVR', 'Editor')
RA = os.environ.get('GSVR_RA_JOBS1', '/path/to/gsvr-testdata/w41/jobs_ra')
PACKS = os.environ.get('GSVR_PACKS_JOBS1', '/path/to/gsvr-testdata/w41/jobs_allpacks')
CLIENT = os.environ.get('GSVR_RO_CLIENT', '/path/to/gsvr-testdata/ro_data/Ragnarok Online')


def rd(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


def builder_list(src, name, quoted):
    body = re.search(name + r'\s*=\s*\{([^}]*)\}', src).group(1)
    return re.findall(r'"([^"]+)"', body) if quoted else [int(x) for x in re.findall(r'-?\d+', re.sub(r'//[^\n]*', '', body))]


class Table(unittest.TestCase):
    def test_ids_after_the_dungeons(self):
        order = list(api.MAP_ORDER)
        self.assertEqual(jobmaps.JOBMAP_BASE, len(towns.NAMES) + len(culvert.NAMES) + len(dungeons.NAMES))
        self.assertEqual(tuple(order[jobmaps.JOBMAP_BASE:]), jobmaps.NAMES)
        self.assertEqual([jobmaps.world_id(m) for m in jobmaps.NAMES], list(range(43, 50)))
        self.assertLessEqual(len(order), towns.MAX_MAPS)

    def test_on_demand_never_prebuilt(self):
        for m in jobmaps.NAMES:
            self.assertEqual(api.PACKS[m]['kind'], 'zone')
            self.assertIn(m, api.NO_PREBUILD)
            self.assertNotIn(m, api.PREBUILD_ORDER)
            self.assertEqual(api.warm_after(m), [])
        for m in ('izlude', 'morocc', 'prontera'):
            self.assertFalse(set(api.warm_after(m)) & set(jobmaps.NAMES), m)

    def test_builder_tables(self):
        src = rd(os.path.join(ED, 'GsvrSceneBuilder.Ragnarok.cs'))
        names = builder_list(src, 'RoMapNames', True)
        self.assertEqual(names, list(api.MAP_ORDER))
        b = jobmaps.JOBMAP_BASE
        self.assertEqual(builder_list(src, 'RoMapLabels', True)[b:], [jobmaps.LABEL[m] for m in jobmaps.NAMES])
        self.assertEqual(builder_list(src, 'RoMapCluster', False)[b:], [-2] * len(jobmaps.NAMES))
        self.assertEqual(builder_list(src, 'RoMapTerrNeed', False)[b:], [jobmaps.MEASURED[m][0] for m in jobmaps.NAMES])
        self.assertEqual(builder_list(src, 'RoMapObjNeed', False)[b:], [jobmaps.MEASURED[m][1] for m in jobmaps.NAMES])
        self.assertIn('RoJobMapBase = %d;' % b, src)

    def test_npc_sprites(self):
        ids = set(int(l.split()[6]) for l in rd(os.path.join(ED, 'ro_jobs1.txt')).splitlines() if l.startswith('npc '))
        self.assertEqual(len(ids), 18)                                                # 16 new looks + the soldier 105 + the hidden 111
        self.assertTrue(ids - {111} <= set(api.NPC_SPRITES), sorted(ids - set(api.NPC_SPRITES)))
        self.assertNotIn(111, api.NPC_SPRITES)                                     # JT_HIDDEN_NPC: the Mixing Machine / Bookshelf are map models
        self.assertGreaterEqual(api.PACK_REV['prontera'], 6)

    def test_grouped_missing_sprite_warning(self):
        class V:
            def read(self, p):
                raise KeyError(p)
        warn = []
        self.assertEqual(api.npc_sprites(V(), warn), [])
        self.assertEqual(len(warn), 1)                                               # ONE line (the status shows 40 warnings at most)
        for sid in api.NPC_SPRITES:
            self.assertIn('npc %d,' % sid if sid != max(api.NPC_SPRITES) else 'npc %d ' % sid, warn[0] + ' ')


@unittest.skipUnless(all(os.path.isfile(os.path.join(PACKS, 'ro__%s.pack' % m)) for m in jobmaps.NAMES), 'no converted job-test packs on this machine')
class Packs(unittest.TestCase):
    def test_measured(self):
        import ro_towns_warps as TW
        for m in jobmaps.NAMES:
            d = TW.measure(os.path.join(PACKS, 'ro__%s.pack' % m))
            self.assertEqual((d['terr'], d['chunks'], int(round(d['mb'] * 10))), jobmaps.MEASURED[m], m)

    @unittest.skipUnless(os.path.isfile(os.path.join(RA, 'npc_pre-re_jobs_1-1_swordman.txt')), 'no rAthena job scripts on this machine')
    def test_generator_check(self):
        r = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'tools', 'ro_jobs1_gen.py'), 'check', '--ra', RA, '--packs', PACKS], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('0 cell / table failures', r.stdout)


@unittest.skipUnless(os.path.isfile(os.path.join(CLIENT, 'data.grf')), 'no iRO client on this machine')
class Client(unittest.TestCase):
    def test_maps_and_sprites_in_the_client(self):
        from ro_conv import grf
        v = grf.Vfs(CLIENT)
        for m in jobmaps.NAMES:
            for e in ('gat', 'gnd', 'rsw'):
                self.assertTrue(v.exists('data/%s.%s' % (m, e)), m + '.' + e)
        for sid, nm in api.NPC_SPRITES.items():
            for e in ('spr', 'act'):
                self.assertTrue(v.exists((api.NPC_PATH % nm) + '.' + e), '%d %s.%s' % (sid, nm, e))


if __name__ == '__main__':
    unittest.main()
