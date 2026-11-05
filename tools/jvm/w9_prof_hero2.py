#!/usr/bin/env python3
"""W9R3-4: hero-play profiles for three more hero maps (x hero siege 3.53, CSD light 4959, Dark Deeds 6 3646), on the
controller of w9_prof_hero (HeroPlay, constructor keywords only). Loaded by glob from tools/jvm/w9_play.py.
Floors are 80 % of the first accepted 300 s run, never lowered afterwards (docs/w9_criteria.md 2b).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from jvm import w9_prof_hero as H                    # noqa: E402

PROFILES = {}
for _key, _kw, _floors, _extra in (
        ('fan_xherosiege353', {'pick_owners': (8,), 'chat': ('-random',), 'roam': 2000.0}, {'spells': 20, 'kills': 19}, {}),
        ('fan_4959CSDlight', {'support': 1500.0}, {'spells': 2, 'kills': 1}, {}),
        ('fan_3646DarkDeeds6', {}, {'spells': 1, 'kills': 1},
         {'xfail': {'hero owned by 60 s': 'W9R3-4-DD', 'hero level >= 2': 'W9R3-4-DD',
                    'spell effects >= 1': 'W9R3-4-DD', 'kills_by_player[0] >= 1': 'W9R3-4-DD'}})):
    PROFILES[_key] = dict({'key': _key, 'humans': 2 if _kw.get('player') else 1, 'seconds': 300, 'make': H.make(**_kw),
                           'hero_kw': _kw, 'asserts': H.asserts_factory(_floors), 'allowed_errors': '', 'xfail': {},
                           'floors': _floors}, **_extra)
