"""Declarations of natives newer than the player's install (BUGS LTD-BLZ, 2026-10-26).

A map made for a newer game (Legion TD 3.46d: Reforged skin natives, 1.32) calls natives that the install's common.j
(1.31) does not declare; the compiler then stops with "unknown function". Only the signatures are listed here
[ext: the public 1.32 common.j API], and only for natives the map script really mentions, so that a map without them
compiles to the same native table as before. Behaviour lives in natives.py (skin = presentation only: the skin id
never changes game logic).
"""
import re

COMPAT_NATIVES = {
    'BlzCreateUnitWithSkin':
        'native BlzCreateUnitWithSkin takes player id, integer unitid, real x, real y, real face, integer skinId '
        'returns unit',
    'BlzCreateDestructableWithSkin':
        'native BlzCreateDestructableWithSkin takes integer objectId, real x, real y, real face, real scale, '
        'integer variation, integer skinId returns destructable',
    'BlzGetUnitSkin': 'native BlzGetUnitSkin takes unit whichUnit returns integer',
    'BlzSetUnitSkin': 'native BlzSetUnitSkin takes unit whichUnit, integer skinId returns nothing',
    'BlzSetDestructableSkin': 'native BlzSetDestructableSkin takes destructable d, integer skinId returns nothing',
}


def with_compat(sources):
    """sources [(origin, text)] (common.j, Blizzard.j, ..., war3map.j last) -> the same list plus a ('compat', text)
    entry before the last one when the map script mentions a COMPAT native that no earlier source declares."""
    if len(sources) < 2:
        return sources
    decl = '\n'.join(s[1] for s in sources[:-1])
    script = sources[-1][1]
    need = [n for n in COMPAT_NATIVES
            if n in script and not re.search(r'\bnative\s+%s\b' % n, decl)]
    if not need:
        return sources
    text = '\n'.join(COMPAT_NATIVES[n] for n in need) + '\n'
    return list(sources[:-1]) + [('compat', text), sources[-1]]
