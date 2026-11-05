"""W9R2-4: the audit scenario shared by tools/map_audit.py and tools/fanmap_check.py.

Scenario (the one every number of docs/map_audit.md uses): humans = min(human slots of the map, 8), every dialog the
script shows is answered with its FIRST button (human input kind 4, at the next game second), no other input.
[obs] without the answer 15 survey rows said 'no wave progress' only because the difficulty dialog was never clicked
(Wintermaul X6.2M: 13 -> 433 units at 90 s once answered).
"""
import os

from jvm import natives as NT
from jvm import run as R


def human_slots(map_dir):
    """-> (n human-controllable slots, n slots, w3i format) from war3map.w3i."""
    import w3map
    p = os.path.join(map_dir, 'war3map.w3i')
    if not os.path.exists(p):
        return 1, 0, None
    try:
        w = w3map.parse_w3i(open(p, 'rb').read())
    except Exception:                      # noqa: BLE001
        return 1, 0, None
    pl = w.get('players') or []
    hum = [x for x in pl if x.get('type') == 'human']
    return max(1, len(hum)), len(pl), w.get('format') or w.get('version')


def dialog_answerer(pending):
    """wrap DialogDisplay so the audit can answer every shown dialog with its first button (human input: 'the
    host picks the first option'); -> restore function."""
    orig = NT.REG['DialogDisplay']

    def shown(N, p, d, show):
        if show:
            pl = N.o(p, NT.Player)
            pending.append((N.players.index(pl) if pl in N.players else 0, d))
        return orig(N, p, d, show)
    NT.REG['DialogDisplay'] = shown
    return lambda: NT.REG.__setitem__('DialogDisplay', orig)


class Scenario:
    """dialog answering as a per-second tick: sc = Scenario(); sc.start(); sc.tick(nat) before every run_until
    second; sc.stop().  answer=False leaves the dialogs open (the old behaviour, --solo-nodialog / --no-answer)."""

    def __init__(self, answer=True):
        self.answer = answer
        self.pending = []
        self.answered = 0
        self._restore = lambda: None

    def start(self):
        if self.answer:
            self._restore = dialog_answerer(self.pending)

    def tick(self, nat):
        for pidx, d in self.pending:
            nat.eng.post_input(pidx, 4, d, 0)
            self.answered += 1
        self.pending.clear()

    def stop(self):
        self._restore()
        self._restore = lambda: None


def run(key, seconds, humans=None, answer=True, trace_path=None, quiet=True, map_dir=None):
    """R.run with the audit scenario; -> report dict + rep['scenario'] = {humans, human_slots, answer, dialogs_answered}.
    humans None = min(human slots, 8) (the old scenario: 1); answer False = dialogs left open."""
    d = map_dir or os.path.join(R.WORK, 'maps', key)
    hs, _ns, _fmt = human_slots(d)
    n_h = humans if humans else (min(hs, 8) if answer else 1)
    sc = Scenario(answer)
    sc.start()
    try:
        rep = R.run(key, seconds, trace_path=trace_path, humans=n_h, quiet=quiet,
                    tick=lambda sec, nat, machine: sc.tick(nat))
    finally:
        sc.stop()
    rep['scenario'] = {'humans': n_h, 'human_slots': hs, 'answer': answer, 'dialogs_answered': sc.answered}
    return rep


def describe(sc):
    """the report header line."""
    return 'scenario: %s, humans %d (of %d human slots), dialogs answered %d' % (
        'audit (every dialog answered with its first button)' if sc['answer'] else 'solo-nodialog (dialogs left open)',
        sc['humans'], sc['human_slots'], sc['dialogs_answered'])
