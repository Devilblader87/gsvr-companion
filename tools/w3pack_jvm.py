"""W3P1 JVM sections: the map script compiled to the bytecode of the reference VM (tools/jvm) in the layout the Udon
VM (unity/Assets/W3VR/Scripts/W3Vm.cs) reads. Called by tools/w3pack.py; layout documented in docs/jvm_bytecode.md
"Pack sections". Bring your own game: common.j / Blizzard.j come from the player's install, war3map.j / .wts / .w3u
from the map archive, unit data from the install's SLKs -- nothing of it is stored in the repo.

The program is compiled by the reference compiler (tools/jvm/compiler.py) itself; a subclass only records the slot
class of every variable access so the Udon loop can use typed opcodes (one array access instead of three). The
behaviour is the reference's: `python3 tools/w3pack_jvm.py --check AzureTD` compiles from the extracted
work/maps/<Map>/ the way tools/jvm/run.py does and compares program + unit table with the pack-side build.
"""
import os
import math
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jass      # noqa: E402
import locstrings  # noqa: E402
import w3map     # noqa: E402
from jvm import compiler as C, natives as JN, world as JW, unitdata as UD   # noqa: E402

JVM_FORMAT = 1

# ---------------------------------------------------------------- Udon opcodes (typed); docs/jvm_bytecode.md
U_OPS = ['NOP', 'PUSH_I', 'PUSH_R', 'PUSH_S', 'PUSH_C',
         'LOAD_L_I', 'LOAD_L_R', 'LOAD_L_S', 'LOAD_G_I', 'LOAD_G_R', 'LOAD_G_S',
         'LOAD_LA_I', 'LOAD_LA_R', 'LOAD_LA_S', 'LOAD_GA_I', 'LOAD_GA_R', 'LOAD_GA_S',
         'STORE_L_I', 'STORE_L_R', 'STORE_L_S', 'STORE_G_I', 'STORE_G_R', 'STORE_G_S',
         'STORE_LA_I', 'STORE_LA_R', 'STORE_LA_S', 'STORE_GA_I', 'STORE_GA_R', 'STORE_GA_S',
         'CALL_N', 'CALL_J', 'RET', 'JMP', 'JZ', 'JNZ', 'POP', 'I2R',
         'ADD_I', 'ADD_R', 'ADD_S', 'SUB_I', 'SUB_R', 'MUL_I', 'MUL_R', 'DIV_I', 'DIV_R', 'MOD_I',
         'NEG_I', 'NEG_R', 'NOT', 'AND', 'OR',
         'EQ_I', 'EQ_R', 'EQ_S', 'EQ_B', 'NE_I', 'NE_R', 'NE_S', 'NE_B',
         'LT_I', 'LT_R', 'LE_I', 'LE_R', 'GT_I', 'GT_R', 'GE_I', 'GE_R', 'CAST', 'END']
U = {n: i for i, n in enumerate(U_OPS)}
TYPED = {'LOAD_L', 'LOAD_G', 'LOAD_LA', 'LOAD_GA', 'STORE_L', 'STORE_G', 'STORE_LA', 'STORE_GA'}
SC_SUFFIX = ('_I', '_R', '_S')

# native argument / result formats (trace + slot class)
F_NOTHING, F_INT, F_REAL, F_STR, F_BOOL, F_CODE, F_HANDLE, F_ENUM = range(8)
K_IMPL, K_STUB, K_STUB_CREATE, K_UNKNOWN = range(4)
UNIT_FIELDS = ('spd', 'hp', 'mana', 'mana0', 'bldg', 'fused', 'fmade', 'level', 'gold', 'lumber', 'points', 'collision')
# W5 engine unit table (JUT5): every unitdata key that is not a JUTY field and not a string, in unitdata.KEYS order
UNIT_STR_FIELDS = ('abilList', 'Builds', 'Trains', 'Researches', 'Upgrade', 'heroAbilList', 'auto', 'Requires',
                   'Requiresamount')
UNIT_FIELDS_W5 = tuple(k for k in UD.KEYS if k not in UNIT_FIELDS and UD.COLUMNS[k][2] is not UD.p_str
                       and k not in UD.SHOP_NUM and k not in UD.UPG_KEYS)
MISC_KEYS = ('DefenseArmor', 'DamageBonusSpells', 'DamageBonusNormal', 'DamageBonusPierce', 'DamageBonusSiege',
             'DamageBonusMagic', 'DamageBonusChaos', 'DamageBonusHero', 'EtherealDamageBonus',
             'EtherealDamageBonusAlly', 'MagicImmunesResistDamage', 'AttackHalfAngle', 'ReactionDelay',
             'ChanceToMiss', 'MissDamageReduction', 'MinUnitSpeed', 'MaxUnitSpeed', 'MinBldgSpeed', 'MaxBldgSpeed',
             'BoneDecayTime', 'StructureDecayTime', 'GuardDistance', 'GuardReturnTime', 'BuildingAngle', 'AllowMultiBounce', 'FrostMoveSpeedDecrease',
             'FrostAttackSpeedDecrease', 'FoodCeiling', 'TrainRefundRate', 'ResearchRefundRate',
             # UPKEEP (NOTES_production §2.3): thresholds + gold / lumber tax per level (lists of 10)
             'UpkeepUsage', 'UpkeepGoldTax', 'UpkeepLumberTax',
             # W8 heroes (NOTES_heroes Constants)
             'MaxHeroLevel', 'MaxUnitLevel', 'NeedHeroXP', 'NeedHeroXPFormulaA', 'NeedHeroXPFormulaB',
             'NeedHeroXPFormulaC', 'GrantHeroXP', 'GrantHeroXPFormulaA', 'GrantHeroXPFormulaB', 'GrantHeroXPFormulaC',
             'GrantNormalXP', 'GrantNormalXPFormulaA', 'GrantNormalXPFormulaB', 'GrantNormalXPFormulaC',
             'HeroFactorXP', 'SummonedKillFactor', 'HeroExpRange', 'GlobalExperience', 'MaxLevelHeroesDrainExp',
             'BuildingKillsGiveExp', 'StrAttackBonus', 'StrHitPointBonus', 'StrRegenBonus', 'AgiDefenseBonus',
             'AgiDefenseBase', 'AgiAttackSpeedBonus', 'AgiMoveBonus', 'IntManaBonus', 'IntRegenBonus',
             'HeroAbilityLevelSkip',
             # W8 items (NOTES_items §4.3)
             'PickupItemRange', 'DropItemRange', 'GiveItemRange', 'PawnItemRange', 'PawnItemRate',
             # PORT13-Inventory (CItem §5.4): the dead object's lifetime
             'EffectDeathTime',
             # W8 building upgrades (NOTES_skibi §3.4)
             'UpgradeRefundRate', 'RelativeUpgradeCost', 'UpgradeInProgressIdChange',
             # BUGS ETD-ORA8: construction cancel refund; PORT-Build: damage penalty
             'ConstructionRefundRate', 'ConstructionDamageRefundPenalty',
             # W9-4 (NOTES_items §10): AIms stacking
             'MoveSpeedBonusesStack',
             # W9R3-1 (NOTES_production §1.4): hero revive at an altar (cost, time, life / mana, refund)
             'ReviveBaseFactor', 'ReviveLevelFactor', 'ReviveMaxFactor', 'ReviveBaseLumberFactor',
             'ReviveLumberLevelFactor', 'HeroMaxReviveCostGold', 'HeroMaxReviveCostLumber', 'ReviveTimeFactor',
             'ReviveMaxTimeFactor', 'HeroMaxReviveTime', 'HeroReviveLifeFactor', 'HeroReviveManaFactor',
             'HeroReviveManaStart', 'ReviveRefundRate')


# W6 ability table (JABL): base codes the engine implements (tools/jvm/abilities.py), enum = index + 1 (U# AB_*)
AB_BASES = ('AOcr', 'AHbh', 'AEev', 'Aspo', 'Aven', 'Afrb', 'Afra', 'Alit', 'AUau', 'AHad', 'AOae', 'AEar', 'AHab',
            'AUav', 'Aoar', 'Aarm', 'Aapl', 'Assk', 'Amim', 'Agho', 'Adts', 'Atru', 'Adt1', 'Awan',
            'AAns', 'AHtb', 'AUfn', 'AEer', 'AUsl', 'AEbl', 'AOeq', 'AUdd',   # W8 spells (spells.CLASSES)
            'AHca',                                 # W8 frost arrows (abilities.HIT_PRIO + spells.arrow_*)
            'Aasl', 'Atwa', 'ANto',                 # W8 tornado: slow aura, wander, the summon spell
            'AHds', 'AHav', 'ANms', 'Arsk', 'Apiv',  # W8 Blizzard TD creep defences (NOTES_spells)
            'Apsh', 'Acpf', 'AOmi', 'AOvd', 'ANef', 'ANpi', 'Apig',
            'ANfb', 'AOcl', 'AEfk', 'AOws', 'ANbr', 'ANsw', 'AOsf',   # W8 Blizzard hero towers (NOTES_spells §3)
            'AInv', 'AIat', 'AIab', 'AIas', 'AIgo', 'AIam', 'AIim', 'AIsm', 'AIxm', 'AIem',   # W8 items (NOTES_items)
            'AIbl', 'ANfd', 'Absk',                 # W8 usable item abilities (NOTES_items §8, §9)
            'ANcl', 'AOwk', 'Aroa', 'AEtq', 'AUcs', 'Aivs', 'AUin', 'AOwd', 'ACtc', 'AEsf', 'AEsh', 'ANsi',  # td_plan T1
            'AIcb', 'Apoi', 'Aspi', 'Aslo', 'AIml',  # NOTES_abilities §5.12, §5.14-5.16, §5.18
            'AEim',                                 # NOTES_spells §2.3 activated immolation
            'AHhb', 'Ablo', 'Afzy', 'Afae', 'AIdd',  # NOTES_abilities §5.19-5.27
            'Alsh', 'Aens',                         # §5.22, §5.17
            'Aprg', 'Awar', 'AIha', 'AIhe',         # §5.23, §5.25, §5.27
            'Acri',                                 # Wintermaul: §5.28 cripple (A-SP58)
            'AHfa',                                 # searing arrows (NOTES_heroes §4.11, A-SP59)
            'Arai',                                 # raise dead (NOTES_abilities §5.30, A-SP60)
            'AUim',                                 # impale (NOTES_spells §5.13, A-SP61)
            'Aply',                                 # polymorph (NOTES_abilities §5.31, A-SP62)
            'AIma',                                 # item mana restore (NOTES_abilities §5.32, A-SP63)
            'AOhw',                                 # healing wave (NOTES_abilities §5.33, A-SP64)
            'Aloc',                                 # locust (NOTES_abilities §5.34, A-SP65)
            'Aroc',                                 # barrage (NOTES_abilities §5.35, A-SP66)
            'Arst',                                 # restoration = repair (NOTES_abilities §5.36, A-SP67)
            'Aneu', 'Aall', 'Apit', 'Asid',         # W8 shops (NOTES_items §7, shops.py; U# AB_ANEU …; Asud: A-SH6)
            'Asud',                                 # sell units dynamic + the stock natives (NOTES_items §7.7, A-SH9)
            'AIde',                                 # item armor bonus (NOTES_abilities §5.38, A-AB12)
            'ANca', 'Aabr',                         # cleaving attack, aura of blight (§5.39 / §5.40, A-AB13 / 14)
            'AIva',                                 # item life steal (§5.41, A-AB15)
            'Aliq',                                 # liquid fire (§5.42, A-AB16)
            'Afbk',                                 # feedback (§5.43, A-AB17)
            'AIta',                                 # reveal (§5.44, A-AB18)
            'Ainf', 'Auhf',                         # inner fire, unholy frenzy (§5.45 / §5.46, A-SP68 / 69)
            'ANab',                                 # acid bomb (§5.47, A-SP70)
            'ANfl',                                 # forked lightning (§5.48, A-SP71)
            'ANic',                                 # incinerate (§5.49, A-AB19)
            'Aphx',                                 # phoenix (§5.50, A-SP72)
            'AHfs',                                 # flame strike (NOTES_spells §5.14, A-SP73)
            'ANmo',                                 # monsoon (NOTES_spells §3.5, A-SP74)
            'Asds',                                 # kaboom (NOTES_abilities §5.52, A-AB21)
            'Aweb',                                 # web (NOTES_abilities §5.53, A-SP76)
            'ANst',                                 # stampede (NOTES_spells §5.18, A-SP77)
            'AHbz', 'ANrf',                         # blizzard / rain of fire (NOTES_spells §5.20, A-SP79)
            'Awrp',                                 # waygate (NOTES_abilities §5.54, A-AB22)
            'Arej',                                 # rejuvenation (NOTES_abilities §5.28, A-SP80)
            'Arpl', 'Arpm',                         # replenish life / mana (NOTES_abilities §5.55, A-SP81)
            'ANvc',                                 # volcano (NOTES_spells §5.15, A-SP82)
            'AUls',                                 # locust swarm (NOTES_spells §5.16, A-SP83)
            'AIms', 'AImm', 'AIrm', 'AIrg',         # W9-4 item bases: move speed, max mana, mana regen, regen potion (NOTES_items §10)
            'Arel', 'Aamk', 'AIsb', 'AIob', 'AIfb', 'AIpb',
            'Apo2',   # W9R2-3: (Apo2 = the poison sibling of AIpb, its own class, NOTES_items §11.7) life regeneration, attribute bonus, spell-effect / frost / fire / poison orbs (NOTES_items §11)
            'Adef', 'Acrs', 'ANht', 'Acan',   # W9R3-2 ability cluster, enum 150 .. 153 (NOTES_abilities §5.57 .. §5.60): defend, curse, howl of terror, cannibalize
            'AIrc',   # BOSS-ANKH: Ankh of Reincarnation (item ability), enum 154 (NOTES_heroes 6.1): DataA delay, DataB life
            'ACrn',   # BUGS ETD-REINC: unit Reincarnation (Element TD's undead creeps), enum 155: DataA delay, Cool
            'Ahar', 'Ahrl', 'Awha', 'Aaha', 'Agld', 'Agmb', 'Abgm', 'Aeat',   # PORT-Harvest: worker / mine / eat-tree, enum 156 .. 163
            'Aent', 'Aegm', 'Aenc',   # PORT-Entangle: tree entangle / entangled mine / wisp slots, enum 164 .. 166
            'Aroo',   # PORT4-Build: CAbilityRoot (Ancients root / uproot: DataA rooted weapons, DataB uprooted weapons, Dur / HeroDur), enum 167
            'AIre', 'AIra', 'AImr', 'AIrv',   # PORT6-Inventory: item restore / restore aoe / mana restore aoe / reveal map (CAbilityItem*), enum 168 .. 171
            'AIlu',   # PORT6-Inventory: item give lumber (powerup, CAbilityItemGiveLumber), enum 172
            'AIfs',   # PORT7-RestGameplay: figurine items (CAbilityFigurine; AIfs/AIfd/AIfh/AIff/AIfr/AIfu share the data code AIfs), enum 173
            'Arbr',   # PORT11-Upgrade: the armor-type ability the rart research drives (level = counter, DataA = armor), enum 174
            'Aave')   # PORT13-ConvertFix: the ConvertUnits morph data record (source in S.A, target in UnitID), enum 175
# classes that are another base's class with only code / name / order changed [obs vtable diffs]: packed as that base
AB_ALIAS = {'ACtb': 'AHtb',                 # creep storm bolt (NOTES_spells §5.12)
            'AOsh': 'AUcs', 'ANbf': 'AUcs',  # shockwave / breath of fire = the carrion swarm wave (NOTES_spells §5.17)
            'AOac': 'AEar',                 # command aura = the trueshot aura class (NOTES_abilities §5.51)
            'AHtc': 'ACtc',                 # hero thunder clap = the creep thunder clap class [inf] (BUGS W9-3-GEM1)
            'Arep': 'Arst',                 # repair = the restoration class (NOTES_abilities §5.36)
            'AIwb': 'Aweb'}                 # item web = the creep web launch with the Mweb missile (CAbilityItemWeb_p1 §3 [inf: same shape])
AB_LEVEL_REALS = ('Dur', 'HeroDur', 'Area', 'Rng', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'Cast', 'Cool', 'Cost')


def ability_table(abilities, utypes=None, nm=None, items=None):
    """{ability id int: abildata record} -> JABL bytes, count: per implemented ability id, base enum, levels, order id
    (spells.ability_order, 0 = none), reqLevel, levelSkip, Missilespeed (int), the Animnames text (JNAM, -1: none; BUGS SPELL-ANIM), then per level targs, first BuffID (0 = none), UnitID (0 = none), DataA text (JNAM, -1), the 16 reals
    of AB_LEVEL_REALS (float64). W8: also every ability of a unit type's heroAbilList (base enum 0 when the engine has
    no class for it: learning needs levels / reqLevel / levelSkip)."""
    from jvm import spells as SP
    hero_ids = set()
    for r in items or []:                        # item abilities count for the 4-ability cap (NOTES_items §5.1)
        for a in str(r.get('abilList', '') or '').split(','):
            a = a.strip()
            if len(a) == 4:
                hero_ids.add(jass.rawcode(a))
    for rec in (utypes or {}).values():
        for a in str(rec.get('heroAbilList', '') or '').split(','):
            a = a.strip()
            if len(a) == 4:
                hero_ids.add(jass.rawcode(a))
    out = bytearray()
    n = 0
    for aid, rec in (abilities or {}).items():
        code = AB_ALIAS.get(rec['code'], rec['code'])
        if code not in AB_BASES and aid not in hero_ids:
            continue
        lvs = rec['lv'] or []
        base = AB_BASES.index(code) + 1 if code in AB_BASES else 0
        anim = str(rec.get('anim', '') or '')
        out += struct.pack('<8i', aid if aid < 0x80000000 else aid - 0x100000000, base, len(lvs), SP.ability_order(rec),
                           int(rec.get('reqLevel', 0) or 0), int(rec.get('levelSkip', 0) or 0),
                           int(rec.get('mspeed', 0) or 0), nm(anim) if anim and nm else -1)    # BUGS SPELL-ANIM: Animnames entries
        for lv in lvs:
            b = jass.rawcode(lv['BuffID'][0]) if lv['BuffID'] else 0
            uid = lv.get('UnitID') or ''
            ui = jass.rawcode(uid) if len(uid) == 4 else 0
            sa = (lv.get('S') or {}).get('A', '')
            if rec['code'] == 'AIbl':
                sa = str(uid)                       # the per-race UnitID list (NOTES_items §8) as the text slot
            if rec['code'] == 'Aply':               # polymorph: the DataB|C|D|E type lists (NOTES_abilities §5.31)
                S = lv.get('S') or {}
                sa = '|'.join(str(S.get(k, '') or '') for k in ('B', 'C', 'D', 'E'))
            out += struct.pack('<4i', int(lv['targs']), b if b < 0x80000000 else b - 0x100000000,
                               ui if ui < 0x80000000 else ui - 0x100000000, nm(sa) if sa and nm else -1)
            vals = [float(lv[k]) for k in AB_LEVEL_REALS]
            na = (lv.get('S') or {}).get('A', '')
            if rec['code'] == 'Acpf' and len(na) == 4:
                vals[AB_LEVEL_REALS.index('A')] = float(jass.rawcode(na))     # the normal-form type id (morph)
            if rec['code'] in ('Arai', 'AIfs'):       # raise dead / figurine: the DataC / DataD unit types as reals (§5.30; CAbilityFigurine §5)
                S = lv.get('S') or {}
                for key, sv in (('C', S.get('C') or (lv.get('UnitID') if rec['code'] == 'Arai' else '') or ''), ('D', S.get('D') or '')):
                    sv = str(sv).strip()
                    ok = len(sv) == 4 and (utypes is None or jass.rawcode(sv) in utypes)
                    vals[AB_LEVEL_REALS.index(key)] = float(jass.rawcode(sv)) if ok else 0.0
            out += struct.pack('<16d', *vals)
        n += 1
    return bytes(out), n


class TypedCompiler(C.Compiler):
    """the reference compiler + one recorded slot class per emitted instruction (-1 = not a variable access)."""

    def __init__(self, resolve_string=None):
        C.Compiler.__init__(self, resolve_string)
        self.p.sc_of_pc = []

    def emit(self, op, a=0, b=0, cost=None):
        name = C.OPS[op]
        sc = -1
        if name in ('LOAD_L', 'STORE_L', 'LOAD_LA', 'STORE_LA'):
            for slot, typ, _arr in self.locals.values():
                if slot == a:
                    sc = self.T.slot(typ)
                    break
            if sc < 0:
                raise C.CompileError('slot class of local %d unknown in %s' % (a, self.cur.name))
        elif name in ('LOAD_G', 'STORE_G', 'LOAD_GA', 'STORE_GA'):
            sc = self.T.slot(self.p.globals[a]['type'])
        self.p.sc_of_pc.append(sc)
        return C.Compiler.emit(self, op, a, b, cost)


def compile_program(sources, wts):
    """sources: [(origin, text)] like tools/jvm/run.build. -> Program with sc_of_pc."""
    c = TypedCompiler(resolve_string=lambda s: w3map.resolve(s, wts))
    from jvm.compat import with_compat
    for origin, text in with_compat(sources):
        c.add_file(jass.parse(text, 'game'), origin)
    p = c.finish()
    p.warnings = c.warnings
    return p


def base_unit_table(read):
    """tools/jvm/world.engine_base from any install (read = name -> bytes): unitdata.base_records (units in the sorted
    order of the reference cache, w3u meta map, Misc constants, path textures)."""
    return UD.base_records(read)


def unit_types(base, w3u, wts):
    """tools/jvm/world.load_unit_types with the base records + w3u bytes given. -> {type id int: record} (order
    kept)."""
    types = UD.merge(base, w3u, wts)
    out = {}
    for k, v in types.items():
        v['id4'] = k
        out[jass.rawcode(k)] = v
    return out


# ---------------------------------------------------------------- serialisation

def _strtab(strings):
    blob = bytearray()
    offs = []
    for s in strings:
        offs.append(len(blob))
        blob += s.encode('utf-8')
    offs.append(len(blob))
    return struct.pack('<%di' % len(offs), *offs) + bytes(blob)


def _fmt(prog, t):
    if t == 'nothing':
        return F_NOTHING
    if t == 'integer':
        return F_INT
    if t == 'real':
        return F_REAL
    if t == 'string':
        return F_STR
    if t == 'boolean':
        return F_BOOL
    if t == 'code':
        return F_CODE
    if t in prog.enum_types:
        return F_ENUM
    return F_HANDLE


def native_kind(info):
    name, ret = info['name'], info['ret']
    if name in JN.REG:
        return K_IMPL
    if not JN.STUB_RE.search(name):
        return K_UNKNOWN
    if JN.stub_creates_handle(name, ret):
        return K_STUB_CREATE
    return K_STUB


def misc_lines(misc):
    return ['%s=%s' % (k, misc[k]) for k in MISC_KEYS if k in misc]


def upgrade_table(upgrades, nm):
    """{research id int: UpgradeData record} -> JUPG bytes, count: per id the id, the name (JNAM index, -1), then
    maxlevel, goldbase, goldmod, lumberbase, lumbermod, timebase, timemod (float64)."""
    from jvm import unitdata as U
    out = bytearray()
    for rid, r in (upgrades or {}).items():
        out += struct.pack('<ii', rid if rid < 0x80000000 else rid - 0x100000000,
                           nm(r['name']) if isinstance(r.get('name'), str) and r['name'] else -1)
        out += struct.pack('<7d', *[float(r.get(k, 0) or 0) for k in U.UPG_NUM])
    return bytes(out), len(upgrades or {})


def upgrade_effect_table(upgrades):
    """BUGS FF-UPG: the effect columns of every research (JUPG order) -> JUPE bytes, count: per research id, the global flag
    (int32), then 4 slots of (effect id as a fourcc int32, the slot's code column as a fourcc int32 (PORT4-Upgrade rlev: the
    ability to level; 0 none / older packs), base, mod float64); an empty slot has effect id 0."""
    out = bytearray()
    for rid, r in (upgrades or {}).items():
        out += struct.pack('<ii', rid if rid < 0x80000000 else rid - 0x100000000, 1 if r.get('global') else 0)
        for i in range(1, 5):
            e = r.get('effect%d' % i) or ''
            ec = _rc(e) if len(e) == 4 else 0
            cd = r.get('code%d' % i) or ''
            cc = _rc(cd) if isinstance(cd, str) and len(cd) == 4 else 0
            out += struct.pack('<iidd', ec if ec < 0x80000000 else ec - 0x100000000,
                               cc if cc < 0x80000000 else cc - 0x100000000,
                               float(r.get('base%d' % i, 0) or 0), float(r.get('mod%d' % i, 0) or 0))
    return bytes(out), len(upgrades or {})


def unit_upgrade_table(utypes, nm):
    """BUGS FF-UPG: per unit type (JUTY order) the "Upgrades Used" list (JNAM index of the comma string, -1 none), 0, and the
    armor gained per upgrade level (defUp, float64) -> JUUP bytes, count."""
    out = bytearray()
    for tid, rec in utypes.items():
        ul = rec.get('upgrades') or ''
        out += struct.pack('<iid', nm(ul) if ul else -1, 0, float(rec.get('defUp', 0) or 0))
    return bytes(out), len(utypes)


def upgrade_req_table(upgrades):
    """BUGS ETD-ORA3: the per-level research requirements -> JUQR bytes, count: per row research id, level (1-based),
    required id (a unit type or a research), amount (int32 x 4); rows in research order, then level, then list order."""
    from jvm import engine as E
    out = bytearray()
    n = 0
    for rid, r in (upgrades or {}).items():
        for lv in range(1, len(r.get('req') or []) + 1):
            for x, amt in E.parse_reqs(r['req'][lv - 1][0], r['req'][lv - 1][1]):
                out += struct.pack('<4i', rid if rid < 0x80000000 else rid - 0x100000000, lv,
                                   x if x < 0x80000000 else x - 0x100000000, amt)
                n += 1
    return bytes(out), n


ITEM_FLAGS = ('powerup', 'sellable', 'pawnable', 'usable', 'perishable', 'droppable', 'drop', 'ignoreCD')


def item_table(items, nm):
    """[itemdata record] (list order) -> JITM bytes, count: per item id, type, Level, oldLevel, pickRandom, flags
    (ITEM_FLAGS bits), uses, name (JNAM, -1), abilList (JNAM, -1), then HP (float64), then the cooldown group id (0 =
    none)."""
    out = bytearray()
    for r in items or []:
        iid = jass.rawcode(r['id4'])
        fl = 0
        for b, k in enumerate(ITEM_FLAGS):
            if r.get(k):
                fl |= 1 << b
        out += struct.pack('<9i', iid if iid < 0x80000000 else iid - 0x100000000, int(r['cls']), int(r['Level']),
                           int(r['oldLevel']), int(r['pickRandom']), fl, int(r['uses']),
                           nm(r['name']) if r.get('name') else -1, nm(r['abilList']) if r.get('abilList') else -1)
        out += struct.pack('<d', float(r['HP']))
        cd = r.get('cooldownID') or ''
        cdi = jass.rawcode(cd) if len(cd) == 4 else 0
        out += struct.pack('<i', cdi if cdi < 0x80000000 else cdi - 0x100000000)
    return bytes(out), len(items or [])


def item_shop_table(items):
    """W8 shops (NOTES_items §7): per JITM record goldcost, lumbercost, stockMax (int32), stockRegen, stockStart
    (float64 seconds) -> JITS bytes, count."""
    out = bytearray()
    for r in items or []:
        out += struct.pack('<3i2d', int(r.get('goldcost', 0) or 0), int(r.get('lumbercost', 0) or 0),
                           int(r.get('stockMax', 0) or 0), float(r.get('stockRegen', 0) or 0),
                           float(r.get('stockStart', 0) or 0))
    return bytes(out), len(items or [])


def unit_random_table(utypes, nm):
    """W9R2-1: per unit type (JUTY order) the UnitBalance nbrandom flag (int32) and the Propernames list (JNAM index of
    the comma string, -1 none) -> JRND bytes, count."""
    out = bytearray()
    for tid, rec in utypes.items():
        pn = rec.get('propernames') or ''
        out += struct.pack('<2i', 1 if int(rec.get('nbrandom', 0) or 0) == 1 else 0, nm(pn) if pn else -1)
    return bytes(out), len(utypes)


def unit_shop_table(utypes, nm):
    """W8 shops: per unit type with a sale list or a stock (unit-type order): type id, Makeitems / Sellitems /
    Sellunits (JNAM, -1), stockMax (int32), stockRegen, stockStart (float64) -> JSHP bytes, count."""
    out = bytearray()
    n = 0
    for tid, rec in utypes.items():
        lists = [rec.get(k) or '' for k in ('Makeitems', 'Sellitems', 'Sellunits')]
        mx = int(rec.get('stockMax', 0) or 0)
        rg, st = float(rec.get('stockRegen', 0) or 0), float(rec.get('stockStart', 0) or 0)
        if not any(lists) and not mx and not rg and not st:
            continue
        out += struct.pack('<4i', tid if tid < 0x80000000 else tid - 0x100000000,
                           *[nm(v) if v else -1 for v in lists])
        out += struct.pack('<i2d', mx, rg, st)
        n += 1
    return bytes(out), n


def _rc(id4):
    return (ord(id4[0]) << 24) | (ord(id4[1]) << 16) | (ord(id4[2]) << 8) | ord(id4[3])


def program_tables(prog, nm, kind_of):
    """the program part of the JVM sections as int lists: code (3 per instruction), function table, aux (param refs +
    local arrays), globals, native signatures, natives (name, signature, kind, 0) and the kind histogram.  nm(name) ->
    index in the caller's name table (called in the same order as always: functions, globals, natives)."""
    T = C.Types(prog)
    n = len(prog.ops)
    code = []
    for pc in range(n):
        name = C.OPS[prog.ops[pc]]
        if name in TYPED:
            sc = prog.sc_of_pc[pc]
            uop = U[name + SC_SUFFIX[sc]]
        elif name == 'PUSH_C':
            uop = U['PUSH_I']
        else:
            uop = U[name]
        code += [uop, prog.A[pc], prog.B[pc]]
    fun = []
    aux = []
    for f in prog.functions:
        lookup = 1 if prog.func_by_name.get(f.name) == f.index else 0
        off = len(aux)
        aux += [int(bool(x)) for x in f.param_ref]
        aux.append(len(f.local_arrays))
        for s in f.local_arrays:
            aux += [s, T.slot(f.local_types[s])]
        fun += [f.entry, f.nparams, f.nlocals, f.ret_sc, f.entry_cost, nm(f.name), lookup, off]
    glb = []
    for g in prog.globals:
        glb += [T.slot(g['type']), int(g['array']), nm(g['name'])]
    sig = []
    sig_ix = {}
    nat = []
    kinds = [0, 0, 0, 0]
    for info in prog.natives:
        key = (_fmt(prog, info['ret']),) + tuple(_fmt(prog, t) for t in info['params'])
        if key not in sig_ix:
            sig_ix[key] = len(sig)
            sig += [key[0], len(key) - 1] + list(key[1:])
        k = kind_of(info)
        kinds[k] += 1
        nat += [nm(info['name']), sig_ix[key], k, 0]
    return code, fun, aux, glb, sig, nat, kinds


def sections(prog, utypes, misc=None, pathtex=None, abilities=None, upgrades=None, items=None, core_ver='121',
             dests=None, slot_order=None, localized=None):
    """-> [(tag bytes, data bytes, count)] (docs/jvm_bytecode.md "Pack sections")."""
    n = len(prog.ops)
    names = []
    name_ix = {}

    def nm(s):
        if s not in name_ix:
            name_ix[s] = len(names)
            names.append(s)
        return name_ix[s]

    code, fun, aux, glb, sig, nat, kinds = program_tables(prog, nm, native_kind)
    uty = bytearray()
    for tid, rec in utypes.items():
        nmx = nm(rec['name']) if isinstance(rec.get('name'), str) else -1
        uty += struct.pack('<ii', tid if tid < 0x80000000 else tid - 0x100000000, nmx)
        uty += struct.pack('<12d', *[float(rec.get(k, 0) or 0) for k in UNIT_FIELDS])
    # W5 engine tables (docs/jvm_bytecode.md §10): JUT5 per type, JPTX footprints, JMSC constants
    ut5 = bytearray()
    ptx = bytearray()
    ptx_ix = {}
    for tid, rec in utypes.items():
        ut5 += struct.pack('<%dd' % len(UNIT_FIELDS_W5), *[float(rec.get(k, 0) or 0) for k in UNIT_FIELDS_W5])
        ut5 += struct.pack('<%di' % len(UNIT_STR_FIELDS), *[nm(rec.get(k) or '') if rec.get(k) else -1
                                                             for k in UNIT_STR_FIELDS])
        fp = -1
        tex = rec.get('pathTex') or ''
        if tex and pathtex is not None:
            key = UD.tex_key(tex)
            t = pathtex.get(key)
            if t:
                if key not in ptx_ix:
                    ptx_ix[key] = len(ptx_ix)
                    w, h, m = t
                    blob = bytes(m) + b'\0' * ((-len(m)) % 4)
                    ptx += struct.pack('<2i', w, h) + blob
                fp = ptx_ix[key]
        ut5 += struct.pack('<i', fp)
    # BUGS DS-2: JDST = destructable types (id, footprint alive / dead, hp) + the ones the game places (type index,
    # x, y, facing, alive); their path textures join JPTX
    def fp_of(tex):
        if not tex or pathtex is None:
            return -1
        key = UD.tex_key(tex)
        t = pathtex.get(key)
        if not t:
            return -1
        if key not in ptx_ix:
            ptx_ix[key] = len(ptx_ix)
            w, h, m = t
            ptx.extend(struct.pack('<2i', w, h) + bytes(m) + b'\0' * ((-len(m)) % 4))
        return ptx_ix[key]
    dtypes = (dests or {}).get('types') or {}
    dplaced = (dests or {}).get('placed') or []
    dix = {}
    dst = bytearray()
    for id4 in sorted(dtypes):
        r = dtypes[id4]
        dix[id4] = len(dix)
        dst += struct.pack('<iiif', _rc(id4), fp_of(r['pathTex']), fp_of(r['pathTexDeath']), float(r['hp']))
    for id4, x, y, ang, life in dplaced:
        dst += struct.pack('<iffii', dix[id4], x, y, int(round(math.degrees(ang) / 90.0)) % 4, 1 if life > 0 else 0)
    upg_sec = upgrade_table(upgrades, nm)
    uqr_sec = upgrade_req_table(upgrades)
    upe_sec = upgrade_effect_table(upgrades)
    uup_sec = unit_upgrade_table(utypes, nm)
    itm_sec = item_table(items, nm)
    its_sec = item_shop_table(items)
    shp_sec = unit_shop_table(utypes, nm)
    rnd_sec = unit_random_table(utypes, nm)
    abl_sec = ability_table(abilities, utypes, nm, items)   # before JNAM is written (it adds names)
    from jvm import orders as OR
    ords = []
    for oname, oid in OR.ORDERS:                 # W8 order-name table (OrderId / OrderId2String / Issue*Order)
        ords += [oid, nm(oname)]
    reals = [C.f32bits(r) for r in prog.reals]
    reals = [r - 0x100000000 if r >= 0x80000000 else r for r in reals]
    meta = ['jvm_format=%d' % JVM_FORMAT, 'instructions=%d' % n, 'functions=%d' % len(prog.functions),
            'natives=%d' % len(prog.natives), 'natives_impl=%d' % kinds[K_IMPL], 'natives_stub=%d' % (kinds[K_STUB] + kinds[K_STUB_CREATE]),
            'natives_unknown=%d' % kinds[K_UNKNOWN], 'globals=%d' % len(prog.globals), 'reals=%d' % len(prog.reals),
            'strings=%d' % len(prog.strings), 'unit_types=%d' % len(utypes),
            'engine_format=1', 'unit_fields_w5=%d' % len(UNIT_FIELDS_W5), 'footprints=%d' % len(ptx_ix)]
    if slot_order:                                 # BUGS FF-SLOTS: the slots the humans take, in order (teams)
        meta.append('slot_order=' + ','.join(str(i) for i in slot_order))

    def ints(v):
        return struct.pack('<%di' % len(v), *v)

    loc = []
    for k, v in (localized or ()):                 # BUGS GAMEOVER-DIALOG: GetLocalizedString key -> install text
        loc += [k, v]
    return [
        (b'JMET', ('\n'.join(meta) + '\n').encode('utf-8'), len(meta)),
        (b'JCOD', ints(code), n),
        (b'JCST', ints(prog.cost), n),
        (b'JREA', ints(reals), len(reals)),
        (b'JSTR', _strtab(prog.strings), len(prog.strings)),
        (b'JLOC', _strtab(loc), len(loc)),       # localised GetLocalizedString pairs: key, value, key, value ...
        (b'JFUN', ints(fun), len(prog.functions)),
        (b'JFAX', ints(aux), len(aux)),
        (b'JGLB', ints(glb), len(prog.globals)),
        (b'JINI', ints(prog.ginit), len(prog.ginit)),
        (b'JNAT', ints(nat), len(prog.natives)),
        (b'JSIG', ints(sig), len(sig)),
        (b'JUTY', bytes(uty), len(utypes)),
        (b'JUT5', bytes(ut5), len(utypes)),
        (b'JPTX', bytes(ptx), len(ptx_ix)),
        (b'JDST', struct.pack('<2i', len(dix), len(dplaced)) + bytes(dst), len(dix) + len(dplaced)),
        (b'JDTG', b''.join(struct.pack('<i', int(dtypes[i4].get('targ', 0))) for i4 in sorted(dtypes)), len(dix)),   # W9-PYR-DEST: targType flag per destructable type
        (b'JMSC', ('\n'.join(misc_lines(misc or {})) + '\n').encode('utf-8'), len(misc_lines(misc or {}))),
        (b'JNAM', _strtab(names), len(names)),
        (b'JABL',) + abl_sec,
        (b'JUPG',) + upg_sec,
        (b'JUQR',) + uqr_sec,                     # per-level research requirements (BUGS ETD-ORA3)
        (b'JUPE',) + upe_sec,                     # research effects: 4 slots per research (BUGS FF-UPG, NOTES_production 3a)
        (b'JUUP',) + uup_sec,                     # per unit type: Upgrades Used list + defUp (BUGS FF-UPG)
        (b'JORD', ints(ords), len(OR.ORDERS)),
        (b'JITM',) + itm_sec,
        (b'JITS',) + its_sec,                     # W8 shops: item cost + stock (NOTES_items §7)
        (b'JSHP',) + shp_sec,                     # W8 shops: unit-type sale lists + unit stock
        (b'JRND',) + rnd_sec,                     # W9R2-1: per unit type nbrandom + Propernames (randdata.py)
        (b'JVER', core_ver.encode('ascii'), 1),     # W9: the script set the map was compiled with (121 / 131)
    ]


# ---------------------------------------------------------------- JAIP: the AI programs (docs/ai_plan.md S1)

def container_bytes(secs):
    """a W3P1 container (layout of tools/w3pack.py) as bytes."""
    head = 16 + 16 * len(secs)
    off = head
    directory = bytearray()
    body = bytearray()
    for tag, data, count in secs:
        pad = (-len(data)) % 4
        directory += struct.pack('<4s3i', tag, off, len(data), count)
        body += data + b'\0' * pad
        off += len(data) + pad
    return struct.pack('<4s3i', b'W3P1', 1, len(secs), 0) + bytes(directory) + bytes(body)


def calls_start_melee_ai(prog):
    """True when StartMeleeAI is reachable from main / config / the global initialisers (calls and function
    constants, so trigger actions count).  Blizzard.j always carries the call in MeleeStartingAI; only a map that
    reaches it (the melee init) wants the AI programs."""
    nat_ix = [i for i, info in enumerate(prog.natives) if info['name'] == 'StartMeleeAI']
    if not nat_ix:
        return False
    target = nat_ix[0]
    order = sorted(prog.functions, key=lambda f: f.entry)
    end = {}
    for k, f in enumerate(order):
        end[f.index] = order[k + 1].entry if k + 1 < len(order) else len(prog.ops)
    todo = [f.index for f in prog.functions if f.name in ('main', 'config')] + list(prog.ginit)
    seen = set(todo)
    call_n, call_j, push_c = C.OPS.index('CALL_N'), C.OPS.index('CALL_J'), C.OPS.index('PUSH_C')
    while todo:
        f = prog.functions[todo.pop()]
        for pc in range(f.entry, end[f.index]):
            op = prog.ops[pc]
            nxt = -1
            if op == call_n:
                if prog.A[pc] == target:
                    return True
            elif op == call_j:
                nxt = prog.A[pc]
            elif op == push_c:
                nxt = prog.B[pc] - 1
            if 0 <= nxt < len(prog.functions) and nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return False


def ai_programs_section(prog, read, common_j, log=print, force=False):
    """JAIP: common.j + common.ai + <race>.ai compiled from the player's own install, one program per race script, as
    nested W3P1 containers (JMET JCOD JCST JREA JSTR JFUN JFAX JGLB JINI JNAT JSIG JNAM, the main program's layout; the
    JNAT kind column is aivm.native_kind).  Nothing when the map never reaches StartMeleeAI or the install has no .ai
    files: the bot then plays as before.  Install data stays local: this section never goes to a shared store."""
    from jvm import aivm
    if not force and not calls_start_melee_ai(prog):
        return None
    inner = []
    for key in aivm.RACE_SCRIPTS:
        srcs = aivm.ai_source_list(read, common_j, key)
        if srcs is None:
            log('w3pack: no Scripts\\%s / common.ai in the install: AI programs left out' % key)
            return None
        p = compile_program(srcs, {})
        names = []
        name_ix = {}

        def nm(s):
            if s not in name_ix:
                name_ix[s] = len(names)
                names.append(s)
            return name_ix[s]

        code, fun, aux, glb, sig, nat, _k = program_tables(p, nm, lambda info: aivm.native_kind(info['name']))
        reals = [C.f32bits(r) for r in p.reals]
        reals = [r - 0x100000000 if r >= 0x80000000 else r for r in reals]
        n = len(p.ops)
        meta = ['script=' + key, 'instructions=%d' % n, 'functions=%d' % len(p.functions),
                'natives=%d' % len(p.natives), 'globals=%d' % len(p.globals), 'budget=%d' % aivm.OP_BUDGET,
                'max_threads=%d' % aivm.MAX_THREADS, 'pass=%s' % aivm.pass_hash()]

        def ints(v):
            return struct.pack('<%di' % len(v), *v)
        inner.append(container_bytes([
            (b'JMET', ('\n'.join(meta) + '\n').encode('utf-8'), len(meta)),
            (b'JCOD', ints(code), n),
            (b'JCST', ints(p.cost), n),
            (b'JREA', ints(reals), len(reals)),
            (b'JSTR', _strtab(p.strings), len(p.strings)),
            (b'JFUN', ints(fun), len(p.functions)),
            (b'JFAX', ints(aux), len(aux)),
            (b'JGLB', ints(glb), len(p.globals)),
            (b'JINI', ints(p.ginit), len(p.ginit)),
            (b'JNAT', ints(nat), len(p.natives)),
            (b'JSIG', ints(sig), len(sig)),
            (b'JNAM', _strtab(names), len(names))]))
        log('w3pack: AI program %s: %d instructions, %d functions, %d natives' % (key, n, len(p.functions), len(p.natives)))
    return (b'JAIP', container_bytes([(b'APRG', blob, 1) for blob in inner]), len(inner))


def add_ai_section(pack_in, pack_out, read, common_j, log=print):
    """copy a finished pack and append the JAIP section (tools, parity runs: tools/usharp_offline/ai_check.cs). The
    other sections are copied byte for byte.  Returns False when the install has no AI scripts."""
    sec = ai_programs_section(None, read, common_j, log, force=True)
    if sec is None:
        return False
    with open(pack_in, 'rb') as fh:
        blob = fh.read()
    magic, ver, n, res = struct.unpack_from('<4s3i', blob, 0)
    assert magic == b'W3P1'
    old = [struct.unpack_from('<4s3i', blob, 16 + 16 * k) for k in range(n)]
    old = [e for e in old if e[0] != b'JAIP']
    head = 16 + 16 * (len(old) + 1)
    directory = bytearray()
    body = bytearray()
    off = head
    for tag, o, size, count in old:
        data = blob[o:o + size]
        pad = (-size) % 4
        directory += struct.pack('<4s3i', tag, off, size, count)
        body += data + b'\0' * pad
        off += size + pad
    tag, data, count = sec
    directory += struct.pack('<4s3i', tag, off, len(data), count)
    body += data + b'\0' * ((-len(data)) % 4)
    with open(pack_out, 'wb') as fh:
        fh.write(struct.pack('<4s3i', b'W3P1', ver, len(old) + 1, res) + bytes(directory) + bytes(body))
    return True


def R_decode_script(raw):
    from jvm import run as R
    return R.decode_script(raw)


def map_script(read_map):
    for name in ('war3map.j', 'Scripts\\war3map.j'):
        d = read_map(name)
        if d is not None:
            return d
    raise KeyError('war3map.j')


def core_version(common_j):
    """JVER of an install's common.j: '131' = the 1.29+ script set (player slots 0..23, neutral passive 27 ...), else '121'.
    BUGS LTW-SHOPS: not InitHashtable (1.24+) - a 1.24 .. 1.28 install (the maintainer's 1.26) has hashtables but still the 12-slot
    layout (PLAYER_NEUTRAL_PASSIVE = 15); called '131' its Player(15) was no player and LTW 7 made no shops."""
    return '131' if 'GetPlayerNeutralPassive' in common_j else '121'


def build_from_files(files, log=print, ai_programs=True):
    """w3pack.Files -> sections. Core scripts + SLKs from the install (as tools/extract_inputs.py), the rest from
    the map archive."""
    rmap = lambda n: files.map.read(n, missing_ok=True)      # noqa: E731
    # BUGS DS-1: the map's balance set (custom maps: Custom_V1) for every base table read below (set in w3pack.Files)
    def core(name, missing_ok=False):
        # BUGS FF-SLK: the map's own Units\\* tables (old-style custom units) before the install's
        if name.lower().startswith('units\\') and name.lower().endswith(('.slk', '.txt')):
            d = rmap(name)
            if d is not None:
                return d
        return files.core.read(name, missing_ok=missing_ok)
    wts_raw = rmap('war3map.wts')
    wts = w3map.parse_wts(wts_raw) if wts_raw else {}
    cj = core('Scripts\\common.j').decode('latin-1')
    core_ver = core_version(cj)
    bj = core('Scripts\\Blizzard.j').decode('latin-1')
    map_j = R_decode_script(map_script(rmap))
    srcs = [('common.j', cj),
            ('Blizzard.j', bj),
            ('war3map.j', map_j)]                           # W9-1: UTF-8, Latin-1 fallback (jvm.run)
    prog = compile_program(srcs, wts)
    base = base_unit_table(core)
    ut = unit_types(base, rmap('war3map.w3u'), wts)
    mm = rmap('war3mapMisc.txt')
    misc = UD.misc_constants(base['misc'], mm.decode('latin-1') if mm else None)
    from jvm import abildata as AD
    abil_id4 = AD.merge(AD.base_abilities(core), rmap('war3map.w3a'))
    abil = {jass.rawcode(k): v for k, v in abil_id4.items()}
    upg = {jass.rawcode(k): v for k, v in UD.merge_upgrades(base['upgrades'], rmap('war3map.w3q'), wts).items()}
    from jvm import itemdata as ID
    items = ID.merge(ID.base_items(core), rmap('war3map.w3t'), wts)
    from jvm import world as JWd
    ptex = dict(base['pathtex'])
    dtypes = JWd.dest_types_from(core, rmap('war3map.w3b'), ptex)
    dests = {'types': dtypes, 'placed': JWd.placed_dests_from(rmap('war3map.doo'), dtypes)}
    w3i = rmap('war3map.w3i')
    # BUGS GAMEOVER-DIALOG: GetLocalizedString keys from the install's FDF string tables (GlobalStrings / every
    # UI\\FrameDef string list). common.j / Blizzard.j / the map script name the literal keys; GAMEOVER_* always join.
    loc, loc_missing = locstrings.table(files.core, (cj, bj, map_j))
    if loc_missing:
        log('w3pack: localized strings not in the install: %s' % ', '.join(loc_missing[:20]))
    secs = sections(prog, ut, misc, ptex, abil, upg, items, core_ver, dests,
                    slot_order=JWd.slot_order_from(w3i) if w3i else None, localized=loc)
    files.ui_objs = {'units': {r['id4']: r for r in ut.values()},
                     'abil_anim': {k: v['anim'] for k, v in abil_id4.items() if v.get('anim')},   # BUGS SPELL-ANIM: for the model bake          # for the HUD icons (w3ui.command_icons)
                     'upgrades': UD.merge_upgrades(base['upgrades'], rmap('war3map.w3q'), wts)}   # BUGS ETD-TIP
    if ai_programs and os.environ.get('W3_PACK_AI') != '0':   # S7: on by default (W3_PACK_AI=0 / --no-ai-programs leaves it out); local packs
        try:                                        # the race AI scripts of the player's install (never shared)
            aip = ai_programs_section(prog, lambda n: files.core.read(n, missing_ok=True), cj, log)
            if aip is not None:
                secs.append(aip)
        except Exception as e:                      # untrusted / broken install script: the bot plays as before
            log('w3pack: AI programs skipped: %s: %s' % (type(e).__name__, e))
    log('w3pack: jvm %d instructions, %d functions, %d natives, %d globals, %d strings, %d unit types' % (
        len(prog.ops), len(prog.functions), len(prog.natives), len(prog.globals), len(prog.strings), len(ut)))
    return secs


# ---------------------------------------------------------------- self check against tools/jvm/run.py

def check(map_key):
    from jvm import run as R
    prog_ref, _m, _n, _l = R.build(map_key, trace=False)
    map_dir = os.path.join(R.WORK, 'maps', map_key)
    wts = R.load_wts(map_dir)
    srcs = [('common.j', R.read(os.path.join(R.WORK, 'core', 'common.j'))),
            ('Blizzard.j', R.read(os.path.join(R.WORK, 'core', 'Blizzard.j'))),
            ('war3map.j', R.read(os.path.join(map_dir, 'war3map.j')))]
    prog = compile_program(srcs, wts)
    a, b = prog_ref.to_arrays(), prog.to_arrays()
    same = all(a[k] == b[k] for k in a)
    ref_ut = JW.load_unit_types(map_dir, wts)
    w3u = os.path.join(map_dir, 'war3map.w3u')
    ut = unit_types(JW.engine_base(JW.map_data_set(map_dir), map_dir), open(w3u, 'rb').read() if os.path.exists(w3u) else None, wts)
    same_ut = list(ref_ut.items()) == list(ut.items())
    print('%s: program %s (%d instructions), unit types %s (%d)' % (
        map_key, 'identical' if same else 'DIFFERS', len(prog.ops), 'identical' if same_ut else 'DIFFER', len(ut)))
    return same and same_ut


def write_pack(path, secs):
    """a W3P1 file with only the given sections (layout of tools/w3pack.py)."""
    head = 16 + 16 * len(secs)
    off = head
    directory = bytearray()
    body = bytearray()
    for tag, data, count in secs:
        pad = (-len(data)) % 4
        directory += struct.pack('<4s3i', tag, off, len(data), count)
        body += data + b'\0' * pad
        off += len(data) + pad
    with open(path, 'wb') as fh:
        fh.write(struct.pack('<4s3i', b'W3P1', 1, len(secs), 0) + bytes(directory) + bytes(body))


def read_sections(path):
    with open(path, 'rb') as fh:
        blob = fh.read()
    magic, _ver, n, _r = struct.unpack_from('<4s3i', blob, 0)
    assert magic == b'W3P1'
    out = {}
    for k in range(n):
        tag, off, size, count = struct.unpack_from('<4s3i', blob, 16 + 16 * k)
        out[tag.decode()] = (blob[off:off + size], count)
    return out


def verify_pack(path, map_key):
    """the pack's JVM sections == sections() of the program tools/jvm/run.py builds from work/maps/<map_key>."""
    from jvm import run as R
    map_dir = os.path.join(R.WORK, 'maps', map_key)
    wts = R.load_wts(map_dir)
    srcs = [('common.j', R.read(os.path.join(R.WORK, 'core', 'common.j'))),
            ('Blizzard.j', R.read(os.path.join(R.WORK, 'core', 'Blizzard.j'))),
            ('war3map.j', R.read(os.path.join(map_dir, 'war3map.j')))]
    w3u = os.path.join(map_dir, 'war3map.w3u')
    env = JW.load_map_env(map_dir)
    import war3fs
    core = war3fs.open_install(os.path.join(R.ROOT, 'input', 'war3'))
    loc, _missing = locstrings.table(core, [s[1] for s in srcs])
    want = sections(compile_program(srcs, wts), unit_types(JW.engine_base(JW.map_data_set(map_dir), map_dir), open(w3u, 'rb').read()
                                                            if os.path.exists(w3u) else None, wts),
                    env['misc'], env['pathtex'], dests={'types': env['dest_types'], 'placed': env['dests']},
                    slot_order=JW.slot_order(map_dir), localized=loc)
    got = read_sections(path)
    bad = [t.decode() for t, d, c in want if got.get(t.decode()) != (d, c)]
    print('%s: JVM sections %s (%d sections)' % (path, 'identical to the reference build' if not bad else
                                                 'DIFFER: ' + ' '.join(bad), len(want)))
    return not bad


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--verify-pack':
        sys.exit(0 if verify_pack(sys.argv[2], sys.argv[3]) else 1)
    if len(sys.argv) >= 3 and sys.argv[1] == '--check':
        ok = all([check(m) for m in sys.argv[2:]])
        sys.exit(0 if ok else 1)
    print(__doc__)
