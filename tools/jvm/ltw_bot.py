#!/usr/bin/env python3
"""Line Tower Wars 7 v1.1~4 with the bot (tools/jvm/bot.py, docs/bot.md): slot 1 (player 2) is "Computer (easy)",
slot 0 is a scripted human that buys the most expensive unit it can every SEND_GAP s from 81 s on (so creeps walk the bot's lane).

  python3 -m jvm.ltw_bot                  checks (exit 1 on a failed check)
  python3 -m jvm.ltw_bot --export DIR     + DIR/<tag>.{trace,inputs} for the U# harness: the inputs file has only the
                                          scripted human's events (the bot's come from the U# bot itself) and
                                          DIR/<tag>.botinputs all events incl. the bot's (a replay without a bot)
                                          W3_MAPS "fan_linetowerwars7v114*2b2@300=DIR/<tag>.trace,DIR/<tag>.inputs"
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                         # noqa: E402
from jvm import run as R            # noqa: E402
from jvm import bot as B            # noqa: E402

MAP = 'fan_linetowerwars7v114'
SECONDS = 300
BOT_MASK = 2                        # slot 1
SEND_FROM, SEND_GAP = 81, 12        # the scripted human
LANE_X = (-4608.0, -3584.0)         # Start_2 of the map: the bot's lane (player 2)
STRAIGHT = 5300.0                   # spawn y ~2300 -> End y ~-3100 (map regions Start_2 / End_2)


def tag():
    return '%s_bot_%ds' % (MAP, SECONDS)


def scenario(seconds=SECONDS, with_bot=True, off_at=None, enabled=True, sample=None):
    prog, machine, nat, lines = R.build(MAP, humans=2, engine_trace=bool(os.environ.get('W3_ENGTRACE')))
    eng = nat.eng
    rec_h, rec_all = [], []
    orig = eng.post_input

    def post_input(player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
        r = (machine.turn, player, kind, unit_serial, oid, float(x), float(y), target_serial)
        rec_all.append(r)
        if player == 0:
            rec_h.append(r)
        return orig(player, kind, unit_serial, oid, x, y, target_serial)
    eng.post_input = post_input
    bots = B.attach(nat, [1]) if with_bot else []
    for b_ in bots:
        b_.enabled = enabled
    off_n = None
    R.start(machine, nat)
    p0 = nat.players[0]
    tracks = {}                      # serial -> [(x, y)] of p0 units inside the bot's lane (one sample per second)
    t = 0
    while t < seconds:
        t += 1
        machine.run_until(t)
        if sample is not None:
            sample(t, nat, eng, bots, rec_h)
        if off_at is not None and t == off_at:
            for b_ in bots:
                b_.enabled = False                  # the master switches the slot off: it just stops acting
            off_n = len(rec_all)
        if t >= SEND_FROM and (t - SEND_FROM) % SEND_GAP == 0:
            best = None
            for u in nat.units:
                if u.owner is p0 and u.alive and not u.dead and getattr(u, 'shop', None):
                    for e in eng.shops.listing(u, 0):
                        if e[0] == 1 and e[6] == 0 and (best is None or e[2] > best[2]):
                            best = (u, e[1], e[2])
            if best is not None:
                eng.post_input(0, 5, best[0].serial, best[1])
        for u in nat.units:
            if u.owner is p0 and u.alive and not u.dead and not u.is_bldg:
                tr = tracks.setdefault(u.serial, [])
                if not tr or tr[-1] != (u.x, u.y):
                    tr.append((u.x, u.y))
    scenario.off_n = off_n
    return nat, lines, eng, bots, rec_h, rec_all, tracks


def towers(nat, slot=1):
    return sorted((u.x, u.y) for u in nat.units if u.owner.id == slot and u.alive and not u.dead and u.is_bldg
                  and u.data.get('id4') in ('h000', 'h003', 'h008') and u.y > -3200)


def rows_of(nat, bot):
    """-> {row: sorted column list} of the bot's finished / building towers on its planned grid."""
    out = {}
    W = bot.W
    for x, y in towers(nat, bot.slot):
        c = (x - (bot.cx0 + bot.jl * W / 2.0)) / W
        k = bot.d * (y - bot.cy0) / W
        if abs(c - round(c)) > 0.01 or abs(k - round(k)) > 0.01:
            continue
        c, k = int(round(c)), int(round(k))
        if (bot.entry_k - k) % 2 or c < 0 or c >= bot.n or k > bot.entry_k:
            continue
        out.setdefault((bot.entry_k - k) // 2, []).append(c)
    return {r: sorted(v) for r, v in out.items()}


def detour(track):
    """-> (path length, straight y distance, samples) of the longest run of the track that walks down the bot's lane
    (one sample per second; a jump of more than 700 units or a step back up = a leaked creep sent to the start again)."""
    best = (0.0, 0.0, 0)
    seg = []

    def flush():
        nonlocal best
        if len(seg) >= 5:
            ln = sum(math.hypot(seg[i + 1][0] - seg[i][0], seg[i + 1][1] - seg[i][1]) for i in range(len(seg) - 1))
            if ln > best[0]:
                best = (ln, seg[0][1] - seg[-1][1], len(seg))
    for p in track:
        if not (LANE_X[0] - 300 <= p[0] <= LANE_X[1] + 300 and p[1] < 2100):
            continue
        if seg and (p[1] > seg[-1][1] + 1.0 or math.hypot(p[0] - seg[-1][0], p[1] - seg[-1][1]) > 700.0):
            flush()
            seg = []
        seg.append(p)
    flush()
    return best


def check(verbose=True, export=None, seconds=SECONDS):
    fails = []

    def ok(c, what):
        if verbose:
            print('  %s %s' % ('ok  ' if c else 'FAIL', what))
        if not c:
            fails.append(what)
    nat, lines, eng, bots, rec_h, rec_all, tracks = scenario(seconds)
    bot = bots[0]
    for st, tx in bot.log[:3]:
        if verbose:
            print('   ', st * 0.005, tx)
    rows = rows_of(nat, bot)
    ntow = sum(len(v) for v in rows.values())
    full = [r for r in sorted(rows) if len(rows[r]) == bot.n - 1 and bot.gap(r) not in rows[r]]
    gaps_ok = all(bot.gap(r) == (0 if r % 2 == 0 else bot.n - 1) for r in full)
    if verbose:
        print('towers %d in rows %s; full rows %s; bot sends %d, spent towers %d / units %d; gold %d; rejected %d'
              % (ntow, {r: len(v) for r, v in rows.items()}, full, bot.sends, bot.spent_tow, bot.spent_send,
                 nat.players[1].state.get(1, 0), eng.stats['inputs_rejected']))
    ok(not [ln for ln in lines if 'ERROR' in ln or 'Traceback' in ln], 'no errors in the trace')
    ok(bot.state == 1 and bot.n >= 5 and bot.nrows >= 10, 'lane found from the map data: %d columns, %d rows, tower %s'
       % (bot.n, bot.nrows, nat.unit_types[bot.tid].get('id4') if bot.tid else '-'))
    ok(ntow >= 12, 'the bot built %d towers (>= 12)' % ntow)
    ok(len(full) >= 2 and gaps_ok, 'full rows with one gap, alternating left / right: rows %s (gap columns %s)'
       % (full, [bot.gap(r) for r in full]))
    ok(bot.sends >= 3, 'the bot sent %d units (>= 3)' % bot.sends)
    share = bot.spent_send * 100.0 / max(1, bot.spent_tow + bot.spent_send)
    ok(25.0 <= share <= 45.0, 'sending share of the spending %.0f %% (25..45)' % share)
    best = 0.0
    nseen = 0
    for s_, tr in tracks.items():
        ln, dy, n_ = detour(tr)
        if n_ < 5:
            continue
        nseen += 1
        best = max(best, ln - dy)
    ok(nseen >= 2 and best > 400.0, 'creeps in the bot lane: %d tracked, longest detour %.0f units over the straight line' % (nseen, best))
    if verbose:
        print('rejected inputs:', [ln for ln in lines if ' input p' in ln and not ln.endswith(('-> 0', '-> True'))][:20])
    ok(eng.stats['inputs_rejected'] == 0, 'no rejected input (%d)' % eng.stats['inputs_rejected'])
    if export:
        t = tag()
        with open(os.path.join(export, t + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
        for ext, rec in (('.inputs', rec_h), ('.botinputs', rec_all)):
            with open(os.path.join(export, t + ext), 'w') as fh:
                for r in rec:
                    fh.write('%d %d %d %d %d %r %r %d\n' % r)
        if verbose:
            print('  wrote %s.{trace,inputs,botinputs} (%d lines, %d human inputs, %d with the bot)'
                  % (os.path.join(export, t), len(lines), len(rec_h), len(rec_all)))
    return fails


def lives_of(machine):
    """udg_Lives (1-based: player 1 = index 1 ...), the map's lives (25 at the start)."""
    v = machine.global_value('udg_Lives')
    return {k: v.get(k, v.z) for k in (1, 2)} if isinstance(v, dict) else {}


def record(seconds, export=None):
    """W9-3: a long run (--seconds N, e.g. 900): per minute the towers of both slots, the units sent, the lives and the kills;
    acceptance: 0 errors, and either a defeat / elimination text within 15 min or a lives trajectory showing the game would
    not end. -> (rows, fails, texts)"""
    import re
    rows = []

    def sample(t, nat, eng, bots, rec_h):
        if t % 60:
            return
        tw = [sum(1 for u in nat.units if u.owner.id == sl and u.alive and not u.dead and u.is_bldg
                  and u.data.get('id4') in ('h000', 'h003', 'h008') and u.y > -3200) for sl in (0, 1)]
        sends = sum(1 for r in rec_h if r[2] == 5)
        lv = lives_of(nat.vm)
        rows.append((t // 60, tw[0], tw[1], sends, bots[0].sends, lv.get(1), lv.get(2),
                     eng.stats['kills'].get(0, 0), eng.stats['kills'].get(1, 0), len(nat.units)))
    nat, lines, eng, bots, rec_h, rec_all, tracks = scenario(seconds, sample=sample)
    print('min  towers(p0 p1)  sends(p0 bot)  lives(p0 p1)  kills(p0 p1)  units')
    for r in rows:
        print('%3d  %5d %5d  %6d %6d  %6s %6s  %6d %6d  %5d' % r)
    texts = []
    for x in nat.last_text:
        if x[1] == 'text':
            tx = re.sub(r'\|[cC][0-9a-fA-F]{8}|\|[rR]', '', x[3])
            if re.search(r'won the game|lost the game', tx, re.I):
                texts.append((x[0] / 1000.0, tx[:100]))
    fails = []
    if [ln for ln in lines if 'ERROR' in ln or 'Traceback' in ln] or nat.vm.errors:
        fails.append('errors in the trace / VM')
    ends = sorted(set(t for t in texts if t[0] <= 900.0))          # one text per player slot: once
    last = rows[-1] if rows else None
    print('end texts:', ends[:6])
    print('verdict:', 'defeat / elimination text at %.1f s' % ends[0][0] if ends else
          'no end text in %d s; lives %s / %s at the end (%s)' % (seconds, last and last[5], last and last[6],
                                                                   'trajectory above'))
    if export:
        t = tag().replace('_%ds' % SECONDS, '_%ds' % seconds)
        with open(os.path.join(export, t + '.trace'), 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(lines) + '\n')
    return rows, fails, texts


def main():
    export = None
    if '--export' in sys.argv:
        export = sys.argv[sys.argv.index('--export') + 1]
        os.makedirs(export, exist_ok=True)
    if '--seconds' in sys.argv:                       # W9-3: a long record instead of the 300 s checks
        n = int(sys.argv[sys.argv.index('--seconds') + 1])
        rows, fails, _t = record(n, export)
        print('ltw_bot record: %s' % ('ok' if not fails else 'FAILED: %s' % fails))
        sys.exit(1 if fails else 0)
    fails = check(True, export)
    print('ltw_bot: %s' % ('all checks passed' if not fails else '%d FAILED' % len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
