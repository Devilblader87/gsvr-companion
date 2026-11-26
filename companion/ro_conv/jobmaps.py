"""RO first jobs lane (docs/design/ro_jobs1.md, w41): the maps the six classic first-job tests need beyond the towns / culvert / dungeon maps.

One table (WORLD) = the lane's maps in world id order: world id = JOBMAP_BASE + index (43..49, after the 12 dungeon maps 31..42; GsvrSceneBuilder.RoJobMapBase).
Every map is an ordinary ro_conv map pack (RSM 1.x only: 2026-11-25 survey of the owner's iRO client). All of them are ON DEMAND (cluster -2,
GsvrRoMaps.ClusterOnDemand): never fetched in the background, a warp fetches them behind the loading curtain; resident while the player is on one of
them or one warp away (the w39 rule of the dungeon maps), kept under the on-demand RAM budget.

Why each map (rAthena npc/pre-re/jobs/1-1/*.txt, GPL-3.0; numbers only):
  izlude_in   Swordman Association (Swordman#swd_1 74,172, the waiting room, Test Hall Staff 30,163)
  job_sword1  the Swordman test course (three sections, pitfalls, Mae at 223,167)
  moc_ruins   the Morroc ruins: Mr. Irrelevant (141,125, the way into the mushroom farm), Ponka-Hontas (91,150, the Morroc Solution of the Mage test)
  moc_pryd01  Pyramid 1F: the way from moc_ruins down to the Thief Guild
  moc_prydb1  Pyramid basement: Thief Guide (39,129) + Comrade (42,133)
  job_thief1  the mushroom farm of the Thief test
  moc_fild07  Sograt Desert 07 (north of Morroc): Mother Marthilda, one of the three ascetics of the Acolyte pilgrimage (41,355)
Not loaded (their test parts stay data only): prt_fild03 (Father Rubalkabara: needs prt_fild02 + prt_fild03, no road from the loaded fields), izlu2dun (the Byalan
Island Kafra of the Merchant test).
"""

JOBMAP_BASE = 43

# (map, label, kind)
WORLD = (
    ('izlude_in', 'Izlude interiors', 'interior'),
    ('job_sword1', 'Swordman test hall', 'dungeon'),
    ('moc_ruins', 'Morroc ruins', 'field'),
    ('moc_pryd01', 'Pyramid 1F', 'dungeon'),
    ('moc_prydb1', 'Pyramid basement', 'dungeon'),
    ('job_thief1', 'Mushroom farm', 'field'),
    ('moc_fild07', 'Sograt Desert 07', 'field'),
)
NAMES = tuple(r[0] for r in WORLD)
LABEL = dict((r[0], r[1]) for r in WORLD)
KIND = dict((r[0], r[2]) for r in WORLD)
# towns-lane maps this lane's warps touch (read only here)
TOWN_ENDS = ('izlude', 'morocc', 'moc_fild20')
# pool needs + RAM of the converted packs (owner's iRO client, ro_conv 3, 2026-11-25, tools/ro_towns_warps.py measure): (terrain blocks, object chunks, LZoneMB x10)
MEASURED = {
    'izlude_in': (8, 141, 43), 'job_sword1': (16, 61, 82), 'moc_ruins': (16, 198, 101), 'moc_pryd01': (16, 26, 80),
    'moc_prydb1': (8, 68, 27), 'job_thief1': (36, 118, 148), 'moc_fild07': (49, 416, 333),
}
# maps never asked for by a rAthena warp row but only by a script warp of the tests (GsvrRoJob1: Test Hall Staff -> job_sword1, Mr. Irrelevant -> job_thief1)
SCRIPT_ONLY = ('job_sword1', 'job_thief1')


def world_id(name):
    return JOBMAP_BASE + NAMES.index(name)
