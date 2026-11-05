"""Play profile of Kindergarten TD! v.3.0 (fan_KigaTDv30p, Dinah, hand-written/obfuscated JASS, 1.26 scripts; see w9_play.py for the interface).

The map: every player starts with a child builder (eC00, Night Elf style: it is CONSUMED by the building it places). The toys the child
builds (h002 ... h05P, 0 gold in the unit data: the map charges its own price) are replaced at construction finish by a random tower of
the map (ReplaceUnitBJ). The setup is three dialogs shown to the player: language
(Deutsch / English), difficulty (4 buttons, Normal = the 2nd; the dialog is filled once more for every player who clicks after the host changed, so 8
buttons mean the LAST four are the live ones) and options (3 buttons: Share money / Perfection / Ready = last). 8 lanes of
creeps run from the players' start points to the tree house (0, -5120): with all eight lanes played they share one exit, which is
what makes the path search heavy (docs/path_field.md "Kiga TD").

KigaPlay: answers the three dialogs, spends the test gold (input kind 3, like the other profiles) and builds one toy at a spot around the
child's start point; the map turns the child into the toy's own builder (uC13 'Lego Baumeister' ...), which then builds the towers (the soak
player's rules). The "b" chat command that would make a new child is dead code of the map (its flag is never set). Eight KigaPlay objects play
the profile, one per human slot. Floors come from the first accepted run. The script is a 1.26 one: run.CORE126_MAPS.
"""
import jass

from . import w9_play as W
from .engine import SPS

PROFILES = {}

TOYS = ('h002', 'h007', 'h006', 'h00P', 'h00V', 'h011', 'h019', 'h05P')      # the toys without a requirement (h00I / h04E need R002, h008 is the 700 gold special)
BUILDER = 'eC00'


def A(name, ok, detail=''):
    return (name, bool(ok), detail)


class KigaPlay(W.Play):
    """one child player: dialogs (every shown dialog is answered once; 4k buttons -> the 2nd of the last four, 3 -> the last), test gold every
    30 s, one toy at the first free spot around the start point, then the soak player's tower rules for the toy builder."""

    def __init__(self, N, player=0, bonus=1000, radius=1280.0):
        W.Play.__init__(self, N, player=player, bonus=bonus, early=True, upgrade_on=False, radius=radius)
        self.dq = []
        self.n_toy = 0

    def click_dialogs(self, now):
        N = self.N
        hp = N.H(N.players[self.player])
        seen = getattr(self, 'dlg_seen', 0)
        texts = N.last_text
        self.dlg_seen = len(texts)
        for t in texts[seen:]:
            if t[1] == 'dialog' and t[2] == hp and t[4]:
                self.dq.append(t[3])
        while self.dq:
            h = self.dq.pop(0)
            o = N.o(h)
            if o is not None and getattr(o, 'buttons', None):
                nb = len(o.buttons)
                self.eng.post_input(self.player, 4, h, nb - 3 if nb % 4 == 0 else 2 if nb == 3 else 0)
                self.log.append((now * 5, 'dialog %d buttons' % nb))
                return True
        return False

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if self.click_dialogs(now):
            return
        units = self.own()
        b = next((u for u in units if u.data.get('id4') == BUILDER), None)
        if b is None:
            W.Play.on_turn(self, turn)              # the toy builder (u...): towers, test gold, upgrades like the soak player
            return
        if self.ax is None:
            self.ax, self.ay = b.x, b.y
        if self.bonus and now >= getattr(self, 'next_bonus', 0):
            self.next_bonus = now + 30 * SPS
            self.eng.post_input(self.player, 3, 0, self.bonus)
            return
        if b.build is None and b.order == 0 and not b.paused:
            tid = jass.rawcode(TOYS[self.n_toy % len(TOYS)])
            p = self.spot(tid, b)
            if p is not None:
                self.n_toy += 1
                self.eng.post_input(self.player, 1, b.serial, tid, p[0], p[1])
                self.log.append((now * 5, 'toy %s' % TOYS[(self.n_toy - 1) % len(TOYS)]))


PLAYERS = 8                                    # the map has 9 human slots; 8 lanes of creeps meet at the tree house
BONUS = 1000                                   # test gold per player and 30 s (input kind 3, like the other soak profiles)


def make_players(N, n=PLAYERS, bonus=BONUS):
    """one KigaPlay per human slot (each registers itself on the VM's turn hook); -> the first one."""
    N.kiga_players = [KigaPlay(N, player=i, bonus=bonus) for i in range(n)]
    return N.kiga_players[0]


def lvl(series):
    v = [x for x in series if x is not None]
    return max(v) if v else 0


def kiga_asserts(rep):
    s = rep['series']
    ups = rep['nat'].autoplayer
    left = [u for u in rep['nat'].units if u.data.get('id4') == BUILDER and u.alive and not u.dead and u.owner.id < PLAYERS]
    return [
        A('towers_built >= 5', rep['towers_built'] >= 5, rep['towers_built']),
        A('kills_by_player[0] >= 20', rep['kills'].get(0, 0) >= 20, rep['kills'].get(0, 0)),
        A('toy builders turned into tower builders', ups.n_toy >= 1 and not left, '%d toys, %d eC00 left' % (ups.n_toy, len(left))),
        A('level global V >= 3', lvl(s.get('V', [])) >= 3, 'V max %s' % lvl(s.get('V', []))),
        A('lives global E found, <= 40', s.get('E') and s['E'][-1] is not None and s['E'][-1] <= 40, s.get('E', [None])[-1]),
        A('rejected/inputs <= 20 %', rep['rejected'] <= 0.2 * max(1, rep['inputs']), '%d/%d' % (rep['rejected'], rep['inputs'])),
    ]


# obfuscated script: V = level ("Level "+I2S(V)), E = secrets left (lives, 40 at the start; "N remaining secrets"), N = leaks this round
PROFILES['fan_KigaTDv30p'] = dict(
    key='fan_KigaTDv30p', humans=PLAYERS, seconds=300, make=make_players, report_globals=('V', 'E', 'N'), asserts=kiga_asserts)
