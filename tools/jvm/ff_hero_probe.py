"""BUGS FF-HERO-ABIL probe (reference VM only, no script run): every hero type of a map, every hero ability of its heroAbilList:
base implemented in the engine (pack AB_BASES + AB_ALIAS)? For the implemented ones with an order id: the hero (level 10, all skills
learned) casts it at an enemy cluster (or a unit / point as its target kind says) and the line says whether the order was accepted and
how much mana it cost. The effect itself is covered by the rule tests of each base.

  python3 tools/jvm/ff_hero_probe.py fan_FootmanFrenzyXV484 [fan_8821FootmanFrenzyv67]
"""
import sys
import os
import collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import jass
import w3pack_jvm as P
from jvm import run as R
from jvm import spells as SP

IMPL = set(P.AB_BASES)
T = 0.0


def adv(m, dt):
    global T
    T += dt
    m.run_until(T)


def snap(nat, units):
    return [(u.serial, round(u.life, 2), len(u.buffs), round(u.mana, 1)) for u in units if u.alive]


for key in sys.argv[1:]:
    prog, machine, nat, lines = R.build(key, humans=1)
    eng, ab, sp = nat.eng, nat.eng.ab, nat.eng.sp
    ut = nat.unit_types
    p0, p1 = nat.players[0], nat.players[1]
    res = collections.OrderedDict()
    heroes = [tid for tid, rec in ut.items() if str(rec.get('heroAbilList', '') or '').strip() and rec.get('Primary')]
    print('==', key, len(heroes), 'hero types', flush=True)
    foe = jass.rawcode('hfoo') if jass.rawcode('hfoo') in ut else next(t for t in ut)
    for tid in sorted(heroes, key=lambda t: ut[t].get('id4', '')):
        rec = ut[tid]
        hl = [a.strip() for a in str(rec.get('heroAbilList', '')).split(',') if len(a.strip()) == 4]
        for aid4 in hl:
            aid = jass.rawcode(aid4)
            r = ab.rec(aid)
            if r is None:
                res[(aid4, '?')] = ('no record', [])
                continue
            base = r['code']
            eb = P.AB_ALIAS.get(base, base)
            if eb not in IMPL:
                res.setdefault((aid4, base), ('NOT IMPLEMENTED', []))
                continue
            oid = sp.order_id(aid)
            if (aid4, base) in res:
                continue
            if not oid:
                res[(aid4, base)] = ('passive / no order id (implemented base)', [])
                continue
            h = nat.create_unit(p0, tid, 0.0, 0.0, 270.0)
            eng.hero.set_level(h, 10)
            h.skill = 20
            for a2 in h.hslots:
                eng.hero.learn(h, a2, free=True)
            h.mana = h.maxmana
            ally = nat.create_unit(p0, foe, -150.0, 0.0, 0.0)
            ally.life = ally.maxlife * 0.5
            foes = [nat.create_unit(p1, foe, 300.0 + 60 * (i % 3), -120.0 + 80 * (i // 3), 180.0) for i in range(6)]
            for f in foes:
                f.life = f.maxlife * 0.9
            lv = ab.lv(h, aid)
            tk = SP.target_kind(base if base in SP.CLASSES else eb, lv)
            m0, n0 = h.mana, len(nat.units)
            before = snap(nat, foes + [ally, h])
            tgt = foes[0]
            if tk == SP.T_UNIT:
                ok = eng.issue(h, oid, None, None, tgt)
                if not ok:
                    ok = eng.issue(h, oid, None, None, ally)
            elif tk == SP.T_POINT:
                ok = eng.issue(h, oid, 360.0, 0.0)
            else:
                ok = eng.issue(h, oid)
            adv(machine, 3.0)
            after = snap(nat, foes + [ally, h])
            spent = m0 - h.mana
            newu = len(nat.units) - n0
            verdict = 'accepted=%s mana_spent=%.0f new_units=%d' % (ok, spent, newu)
            res[(aid4, base)] = (verdict, [hex(tid)])
            for u in foes + [ally, h]:
                nat.remove_unit(u)
            for u in list(nat.units):
                if u.summoned and u.alive:
                    nat.remove_unit(u)
            sp.cool.clear()
    for k, (v, _x) in res.items():
        print('  ', k[0], k[1], v)
