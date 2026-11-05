"""W9R2-2: item pools and the hero-revivable event (docs/NOTES_items.md item pools, docs/NOTES_triggers.md section 11,
docs/BUGS.md W9R2-2). Python reference; the Udon twin is unity/Assets/W3VR/Scripts/W3NatPool.cs (impl ids 1350-1359) plus
the hooks in W3World.cs (StepKill), W3Engine.cs / W3Vm.cs (UE_HREVIVABLE, CTX_REVIVABLE).

Clean room: behaviour only, tags [obs] observed in the game / a map, [ext] common.j meaning, [inf] our choice.

  item pool   CreateItemPool() -> handle; ItemPoolAddItemType(pool, itemId, weight) updates the weight of the FIRST cell
              with that id in place, or appends a new cell when the id is absent (insertion order kept, never a second
              cell for the same id); the 32-bit weight is stored verbatim (negative / NaN allowed, CItemPool_p1 §4);
              ItemPoolRemoveItemType removes only the FIRST entry of the id (CItemPool_p1 §5);
              PlaceRandomItem(pool, x, y) -> item: an EMPTY pool returns null and draws nothing, otherwise ONE real draw
              unit() * total weight (float32, game stream 0x23, the stream of ChooseRandomItem and PlaceRandomUnit) is walked
              in insertion order with a running sum and the first entry where draw < run wins (strict), including zero and
              negative weights; a run-off and a chosen id 0xffffffff fall back to the FIRST entry (CItemPool_p1 §6). There
              is no total guard: a zero/negative total still draws and creates the first entry [inf: the draw's zero span
              is Open]. The item is made through the normal creation path (new_item = CreateItem: no events) at (x, y).
              An id without an item type draws (the choice was made) and returns null like CreateItem [inf].
              DestroyItemPool / Save/LoadItemPoolHandle exist.
              [obs: Island Troll Tribes 1.91 calls CreateItemPool 2, ItemPoolAddItemType 14, PlaceRandomItem 138 in 300 s.]
  revivable   EVENT_PLAYER_HERO_REVIVABLE / EVENT_UNIT_HERO_REVIVABLE: when a hero (type id starting A-Z, not an illusion) dies,
              the owner's player registrations run first, then the unit's, right after that death's EVENT_UNIT_DEATH and
              EVENT_WIDGET_DEATH [inf order]. A hero that reincarnates by itself would not raise it; the engine has no
              reincarnation [inf]. GetRevivableUnit() = the dying hero: its own context slot (CTX_REVIVABLE), kept by the
              thread over TriggerSleepAction like GetTriggerUnit; null outside the event [ext]. REVIVE_START / REVIVE_FINISH /
              REVIVE_CANCEL (altar production) stay unraised: W10 altar / tavern work.
              [obs: ITT and Azeroth Wars Strategy revive the hero 5 s after the event with ReviveHeroLoc(GetRevivableUnit()).]
"""
from . import vm as V
from .vm import Agent, r32
from .natives import nat, new_item


class ItemPool(Agent):
    tname = 'itempool'

    def __init__(self):
        self.types = []                  # [(item type id, weight)] in insertion order


@nat('CreateItemPool')
def _createitempool(N):
    return ItemPool()


@nat('ItemPoolAddItemType')
def _itempooladd(N, pool, tid, weight):
    po = N.o(pool, ItemPool)
    if po is not None:
        for k, (t, _w) in enumerate(po.types):          # the first id match overwrites its weight in place (§4)
            if t == tid:
                po.types[k] = (t, weight)
                return
        po.types.append((tid, weight))                  # absent: append, verbatim (zero / negative / NaN kept)


@nat('ItemPoolRemoveItemType')
def _itempoolremove(N, pool, tid):
    po = N.o(pool, ItemPool)
    if po is not None:
        for k, (t, _w) in enumerate(po.types):          # only the first cell (CItemPool_p1 §5)
            if t == tid:
                del po.types[k]
                break


@nat('PlaceRandomItem')
def _placerandomitem(N, pool, x, y):
    po = N.o(pool, ItemPool)
    if po is None or not po.types:
        return None                                    # empty pool: no draw, no item (CItemPool_p1 §6 step 1)
    total = 0.0
    for _t, w in po.types:                             # every entry, zero and negative included
        total = r32(total + w)
    r = r32(N.streams.game[0x23].unit() * total)       # no total guard: zero/negative still draws (step 3)
    tid = po.types[0][0]                               # run-off fallback = the first entry (step 5)
    run = 0.0
    for t, w in po.types:
        run = r32(run + w)
        if r < run:                                    # strictly less-than: a zero weight never matches
            tid = t
            break
    if tid == -1 or tid == 0xFFFFFFFF:
        tid = po.types[0][0]                           # the id sentinel also falls back to the first entry
    return new_item(N, tid, x, y)


@nat('GetRevivableUnit')
def _getrevivableunit(N):
    return N.vm.ctx_top(V.CTX_REVIVABLE)


def hero_died(N, u, killer):
    """Natives.kill_unit, after EVENT_UNIT_DEATH / EVENT_WIDGET_DEATH: the revivable event of a dying hero."""
    if u.is_hero() and not u.illusion:
        N.hero_event(u, 'REVIVABLE', {'dying': N.H(u), 'killing': N.H(killer)}, {V.CTX_REVIVABLE: N.H(u)})
