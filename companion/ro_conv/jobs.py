"""RO second-job body sprites (w37 jobs2, docs/design/ro_jobs2.md) from the player's own client (BYOG, read only).

The world draws a player's body by NAME (GsvrRoSprite.bodyName): Novice = body_m / body_f (sprite.py), a second job = body_j<rAthena job id>_<sex>,
e.g. body_j7_m (Knight), body_j13_f (Knight on a Peco Peco). GsvrRoJob2.LBodyName builds the same name on the world side.

Client file names: the body sprite of a job is data/sprite/인간족/몸통/<남|여>/<name>_<남|여>.spr/.act. The names below were checked once
against the owner's iRO data.grf (2026-11-25, every one present; Bard has no female and Dancer no male body, as in the game). Only the file
names are in the code - nothing of the client is in git.
"""
import os
import struct

from . import sprite

# rAthena job id -> client body name (Korean, as in the GRF)
JOB_BODY = {
    7: '기사',              # Knight
    13: '페코페코_기사',     # Knight on a Peco Peco (Knight2)
    14: '크루세이더',        # Crusader
    21: '신페코크루세이더',   # Crusader on a Grand Peco (Crusader2)
    8: '프리스트',          # Priest
    15: '몽크',             # Monk
    9: '위저드',            # Wizard
    16: '세이지',           # Sage
    11: '헌터',             # Hunter
    19: '바드',             # Bard (male only)
    20: '무희',             # Dancer (female only)
    10: '제철공',           # Blacksmith
    18: '연금술사',          # Alchemist
    12: '어세신',           # Assassin
    17: '로그',             # Rogue
}
ONLY_SEX = {19: 'm', 20: 'f'}
SEX_DIR = {'m': '남', 'f': '여'}
BODY_ROOT = 'data/sprite/인간족/몸통'
JOB_ORDER = (7, 13, 14, 21, 8, 15, 9, 16, 11, 19, 20, 10, 18, 12, 17)
# start-pack budget: idle only (8 directions). Real iRO client 2026-11-25: 28 bodies idle = 4.0 MiB, idle + walk = 32.0 MiB (the start pack is 17.4 MiB).
# GsvrRoSprite falls back to the idle clip for an anim the pack lacks, so a walking job body stands in its idle pose (walk: a lazy job pack, follow-up).
START_ANIMS = (sprite.ANIM_IDLE,)


def body_name(job, sex):
    """World-side sprite name of a job body (== GsvrRoJob2.LBodyName)."""
    return 'body_j%d_%s' % (job, sex)


def body_path(job, sex):
    """Client path (no extension) of a job body, None for a sex the job does not have."""
    if job not in JOB_BODY or sex not in SEX_DIR:
        return None
    if ONLY_SEX.get(job, sex) != sex:
        return None
    s = SEX_DIR[sex]
    return '%s/%s/%s_%s' % (BODY_ROOT, s, JOB_BODY[job], s)


def job_sprites(vfs, sexes=('m', 'f'), warn=None, anims=START_ANIMS):
    """-> list of converted sprites (sprite.convert_sprite results) for every job body of `sexes` the client has, in JOB_ORDER then sex order.
    Missing bodies give ONE warning with their count (no folder path); a broken file its own warning. Never raises for a single body."""
    out = []
    missing = []
    _E = (sprite.FormatError, ValueError, IndexError, struct.error)
    for job in JOB_ORDER:
        for sx in sexes:
            p = body_path(job, sx)
            if p is None:
                continue
            try:
                out.append(sprite.convert_sprite(body_name(job, sx), sprite.KIND_BODY, vfs.read(p + '.spr'), vfs.read(p + '.act'), anims=anims))
            except KeyError:
                missing.append(body_name(job, sx))
            except _E as e:
                if warn is not None:
                    warn.append('sprites: %s: %s' % (body_name(job, sx), str(e).replace(os.sep, '/').split('/')[-1]))
    if missing and warn is not None:
        warn.append('sprites: %d job bodies not in the client data (Novice look in the world): %s' % (len(missing), ' '.join(missing[:6]) + (' ...' if len(missing) > 6 else '')))
    return out
