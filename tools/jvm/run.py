#!/usr/bin/env python3
"""Run a map headless in the reference VM.

  python3 -m tools.jvm.run --map AzureTD --seconds 120 [--trace FILE] [--humans 1] [--seed 1] [--speed fast]

Game order: compile common.j + Blizzard.j + war3map.j; run every global initialiser (own budget each, J§4);
config(); lobby (slot states); main() at game time 0; then 25 ms turns of 5 ms game steps (T§1.2). Nobody
gives input ("a player builds nothing"): timers fire, waves spawn and walk.
Writes the native trace (default work/traces/<Map>.trace) and a JSON report next to it.
"""
import argparse
import csv
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
ROOT = os.environ.get('W3_ROOT') or os.path.abspath(os.path.join(HERE, '..', '..'))
WORK = os.path.join(ROOT, 'work')

from jvm import compiler, natives, vm as V, world   # noqa: E402
import w3map  # noqa: E402


BOM = b'\xef\xbb\xbf'


def strip_bom(raw):
    """JASS-BOM: drop ONE leading UTF-8 BOM (the game's loader accepts such scripts [inf]); done on the bytes so
    both the UTF-8 and the Latin-1 fallback decode see the same text. Nothing inside the script is touched."""
    return raw[len(BOM):] if raw.startswith(BOM) else raw


def read(path):
    with open(path, 'rb') as fh:
        return strip_bom(fh.read()).decode('latin-1')


def decode_script(raw):
    """W9-1: a MAP script is UTF-8 text (xhs/Gem TD/CF carry Korean / Chinese literals); Latin-1 only when the
    bytes are not valid UTF-8. common.j / Blizzard.j stay Latin-1 (read())."""
    raw = strip_bom(raw)
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def read_script(path):
    with open(path, 'rb') as fh:
        return decode_script(fh.read())


from jvm.w9_errors import EXPECTED_ERRORS, split_errors, error_summary  # noqa: E402,F401  (W9-1 part D)


def load_wts(map_dir):
    p = os.path.join(map_dir, 'war3map.wts')
    if not os.path.exists(p):
        return {}
    with open(p, 'rb') as fh:
        return w3map.parse_wts(fh.read())


# maps whose pack the player builds against a 1.24 - 1.28 install (hashtables, but the OLD 12-slot player layout, JVER 121): their scripts are the 1.26 ones. Under the
# 1.31 fallback in build() Player(15) is no player, so Kiga TD's host marker unit (CreateUnit(Player(15), 'h001')) is never made and the game never starts (BUGS KIGA-CORE).
CORE126_MAPS = ('fan_KigaTDv30p',)


def core126_sources(root=None):
    """[(common.j, text), (Blizzard.j, text)] of the 1.26 install (input/war3_126); work/core126/ is a cache, filled from the install on first use."""
    d = os.path.join(root or WORK, 'core126')
    if not os.path.exists(os.path.join(d, 'Blizzard.j')):
        import war3fs
        inst = war3fs.open_install(os.path.join(ROOT, 'input', 'war3_126'))
        os.makedirs(d, exist_ok=True)
        for n in ('common.j', 'Blizzard.j'):
            with open(os.path.join(d, n), 'wb') as fh:
                fh.write(inst.read('Scripts\\' + n))
    return [('common.j', read(os.path.join(d, 'common.j'))), ('Blizzard.j', read(os.path.join(d, 'Blizzard.j')))]


def core_sources(with_bj=True, ver='121'):
    d = os.path.join(WORK, 'core131' if ver == '131' else 'core')
    out = [('common.j', read(os.path.join(d, 'common.j')))]
    if with_bj:
        out.append(('Blizzard.j', read(os.path.join(d, 'Blizzard.j'))))
    return out


def build(map_name, trace=True, humans=1, seed=1, speed='fast', latency=1, map_dir=None, sources=None,
          autoplay=False, engine_trace=False, core_ver=None, races=None, real_ai=False, ai_levels=None):
    if map_dir is None and map_name:
        map_dir = os.path.join(WORK, 'maps', map_name)
    wts = load_wts(map_dir) if map_dir and os.path.isdir(map_dir) else {}
    given_ver = core_ver
    core_ver = given_ver or '121'
    if sources is None:
        src = read_script(os.path.join(map_dir, 'war3map.j'))
        if map_name in CORE126_MAPS and given_ver is None:
            sources = core126_sources() + [('war3map.j', src)]
        else:
            sources = [('common.j', read(os.path.join(WORK, 'core', 'common.j'))),
                       ('Blizzard.j', read(os.path.join(WORK, 'core', 'Blizzard.j'))), ('war3map.j', src)]
        try:
            prog = compiler.compile_sources(sources, resolve_string=lambda s: w3map.resolve(s, wts))
        except compiler.CompileError:
            # W9: a 1.24+ map (hashtables, GetHandleId …) needs the newer scripts — the player's 1.31 install
            if not os.path.exists(os.path.join(WORK, 'core131', 'common.j')):
                raise
            core_ver = '131'
            sources = [('common.j', read(os.path.join(WORK, 'core131', 'common.j'))),
                       ('Blizzard.j', read(os.path.join(WORK, 'core131', 'Blizzard.j'))), ('war3map.j', src)]
            prog = compiler.compile_sources(sources, resolve_string=lambda s: w3map.resolve(s, wts))
    else:
        # TRIGSTR_ literals resolved at load (Open: when the game resolves them)
        prog = compiler.compile_sources(sources, resolve_string=lambda s: w3map.resolve(s, wts))
    machine = V.VM(prog, speed=speed, latency_turns=latency)
    utypes = world.load_unit_types(map_dir if map_dir and os.path.isdir(map_dir) else None, wts)
    lines = [] if trace else None
    env = world.load_map_env(map_dir if map_dir and os.path.isdir(map_dir) else None)
    if autoplay == 'soak':
        from jvm import autoplay as AP
        humans = max(humans, AP.SOAK_FAN.get(map_name, {}).get('humans', 1))   # W9: one human slot per soak player
    nat = natives.Natives(machine, utypes, humans=humans, seed=seed, wts=wts, trace_lines=lines, env=env)
    nat.bind()
    if real_ai:
        # docs/ai_plan.md S1: the race .ai scripts of the player's install run as AI threads (off by default until S7)
        from jvm import aivm
        cj = next((t for n, t in sources if n == 'common.j'), None)
        if cj is not None:
            nat.ai_host = aivm.AiHost(nat, aivm.install_loader(cj, core_ver=core_ver))
    nat.slot_order = world.slot_order(map_dir if map_dir and os.path.isdir(map_dir) else None)
    nat.map_key = map_name
    nat.race_choice = races            # BUGS MELEE-RACE: lobby race per slot (natives.Natives.apply_race_choice); None = the map's own
    nat.core_ver = core_ver
    nat.ai_levels = ai_levels          # docs/ai_plan.md S7: lobby computer level per slot (0 easy, 1 normal, 2 insane); None = the game difficulty
    if engine_trace:
        nat.eng.trace = []
    if autoplay:
        from jvm import autoplay as AP
        nat.autoplayer = AP.attach_soak(nat, map_name) if autoplay == 'soak' else AP.attach(nat, map_name)
    return prog, machine, nat, lines


def start(machine, nat):
    """-> counted instructions per phase."""
    machine.run_global_inits()
    g = machine.total_ops
    nat.after_globals()
    machine.run_function('config', 'config')
    c = machine.total_ops
    nat.setup_lobby()
    machine.run_function('main', 'main')
    return {'globals': g, 'config': c - g, 'main': machine.total_ops - c}


def static_reachable(map_name):
    p = os.path.join(WORK, 'natives_by_map.csv')
    if not os.path.exists(p):
        return set()
    with open(p) as fh:
        return {r['native'] for r in csv.DictReader(fh) if r.get(map_name)}


def run(map_name, seconds, trace_path=None, humans=1, seed=1, speed='fast', latency=1, quiet=False,
        autoplay=False, engine_trace=False, tick=None, real_ai=False):
    t0 = time.time()
    prog, machine, nat, lines = build(map_name, trace=True, humans=humans, seed=seed, speed=speed,
                                      latency=latency, autoplay=autoplay, engine_trace=engine_trace, real_ai=real_ai)
    t_compile = time.time() - t0
    phases = start(machine, nat)
    init_ops = machine.total_ops
    init_handles = machine.H.live()
    samples = []
    work_sec = []
    sec = 0
    while sec < seconds:
        sec += 1
        w0 = nat.eng.work()
        if tick:
            tick(sec, nat, machine)       # W9R2-4: the audit scenario's dialog answers (jvm/w9_scenario.py)
        machine.run_until(sec)
        samples.append((sec, machine.H.live(), len(nat.units), len(machine.waiting)))
        work_sec.append(nat.eng.work() - w0)
    wall = time.time() - t0
    trace_path = trace_path or os.path.join(WORK, 'traces', map_name + '.trace')
    os.makedirs(os.path.dirname(trace_path), exist_ok=True)
    with open(trace_path, 'w', encoding='utf-8', newline='\n') as fh:
        for ln in lines:
            fh.write(ln + '\n')
    static = static_reachable(map_name)
    called = set(machine.native_counts)
    ops_sec = machine.ops_by_sec
    steady = [ops_sec.get(s, 0) for s in range(1, seconds)]
    _exp, _unexp = split_errors(map_name, machine.errors)
    rep = {
        'map': map_name, 'seconds': seconds, 'wall_s': round(wall, 2), 'compile_s': round(t_compile, 2),
        'instructions_bytecode': len(prog.ops), 'functions': len(prog.functions),
        'init_ops': init_ops, 'init_phases': phases, 'ops_total': machine.total_ops,
        'ops_by_second': [ops_sec.get(s, 0) for s in range(0, seconds + 1)],
        'ops_steady_mean': round(sum(steady) / max(1, len(steady)), 1), 'ops_steady_max': max(steady or [0]),
        'natives_called': len(called), 'native_calls': sum(machine.native_counts.values()),
        'native_counts': dict(sorted(machine.native_counts.items(), key=lambda kv: -kv[1])),
        'static_reachable': len(static),
        'called_not_in_static': sorted(n for n in called - static if not n.startswith('Convert')) if static else [],
        'static_not_called': len(static - called),
        'unknown': dict(sorted(machine.unknown_counts.items(), key=lambda kv: -kv[1])),
        'stubbed': dict(sorted(nat.stub_counts.items(), key=lambda kv: -kv[1])),
        'threads_created': machine.threads_created, 'thread_kinds': machine.thread_kinds,
        'status_counts': {V.STATUS_NAME[k]: v for k, v in sorted(machine.status_counts.items())},
        'max_concurrent_waits': machine.max_concurrent_waits,
        'handles_after_init': init_handles, 'handles_by_second': [s[1] for s in samples],
        'units_by_second': [s[2] for s in samples], 'handle_allocs': machine.H.allocs,
        'handle_frees': machine.H.frees, 'errors': _unexp[:50], 'n_errors': len(_unexp),
        'n_errors_expected': len(_exp), 'errors_expected': _exp[:5],
        'trace_lines': len(lines), 'trace': os.path.relpath(trace_path, ROOT),
        'engine': engine_report(nat, work_sec, seconds),
    }
    if nat.eng.trace is not None:
        with open(os.path.splitext(trace_path)[0] + '.engine', 'w') as fh:
            fh.write('\n'.join(nat.eng.trace) + '\n')
    with open(os.path.splitext(trace_path)[0] + '.report.json', 'w') as fh:
        json.dump(rep, fh, indent=1)
    if not quiet:
        print_report(rep)
    return rep


def engine_report(nat, work_sec, seconds):
    e = nat.eng
    st = e.stats
    steady = work_sec[1:] or [0]
    ap = getattr(nat, 'autoplayer', None)
    p0 = nat.players[0]
    return {
        'towers_built_p0': st['built'].get(0, 0), 'built_by_player': st['built'],
        'kills_by_player': st['kills'], 'deaths_by_owner': st['deaths'], 'bounty_by_player': st['bounty'],
        'attacks': st['attacks'], 'damage_events': st['damage_events'], 'missiles': st['missiles'],
        'repaths': st['repaths'], 'inputs': st['inputs'], 'inputs_rejected': st['inputs_rejected'],
        'p0_gold': p0.state.get(1, 0), 'p0_lumber': p0.state.get(2, 0),
        'work_total': e.work(), 'work_per_s_mean': round(sum(steady) / len(steady), 1), 'work_per_s_max': max(steady),
        'autoplay': [list(x) for x in ap.log] if ap else None,
        'map_globals': map_globals(nat),
    }


def map_globals(nat):
    from jvm import autoplay as AP
    return AP.map_globals(nat.vm, nat.map_key)


def print_report(r):
    print('%s: %d s game time in %.1f s wall (compile %.1f s), %d bytecode instructions' % (
        r['map'], r['seconds'], r['wall_s'], r['compile_s'], r['instructions_bytecode']))
    print('  ops: init %d %s, total %d, steady mean %.0f/s, max %d/s' % (
        r['init_ops'], r['init_phases'], r['ops_total'], r['ops_steady_mean'], r['ops_steady_max']))
    print('  ops per second:', r['ops_by_second'][:20], '...')
    print('  natives: %d distinct called (%d calls), static reachable %d, called-not-static %s' % (
        r['natives_called'], r['native_calls'], r['static_reachable'], r['called_not_in_static']))
    print('  unknown natives:', r['unknown'])
    print('  stubbed natives: %d distinct' % len(r['stubbed']))
    print('  threads: %d created %s, statuses %s, max concurrent waits %d' % (
        r['threads_created'], r['thread_kinds'], r['status_counts'], r['max_concurrent_waits']))
    hb = r['handles_by_second']
    print('  handles: after init %d, at end %d (allocs %d frees %d); by 10 s: %s' % (
        r['handles_after_init'], hb[-1] if hb else 0, r['handle_allocs'], r['handle_frees'], hb[9::10]))
    print('  units by 10 s:', r['units_by_second'][9::10])
    print('  errors (%s): %s' % (error_summary(r['n_errors'], r.get('n_errors_expected', 0)), r['errors'][:8]))
    print('  trace: %s (%d lines)' % (r['trace'], r['trace_lines']))
    e = r['engine']
    print('  engine: towers p0 %d, kills %s, deaths %s, bounty %s, attacks %d, damage events %d, missiles %d, '
          'repaths %d, inputs %d (rejected %d)' % (e['towers_built_p0'], e['kills_by_player'], e['deaths_by_owner'],
                                                    e['bounty_by_player'], e['attacks'], e['damage_events'],
                                                    e['missiles'], e['repaths'], e['inputs'], e['inputs_rejected']))
    print('  engine work: mean %.0f/s, max %d/s, total %d' % (e['work_per_s_mean'], e['work_per_s_max'],
                                                             e['work_total']))
    print('  map globals:', e['map_globals'])
    if e['autoplay']:
        print('  autoplay:', len(e['autoplay']), 'orders')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', required=True)
    ap.add_argument('--seconds', type=int, default=120)
    ap.add_argument('--trace')
    ap.add_argument('--humans', type=int, default=1)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--speed', default='fast', choices=sorted(V.STEPS_PER_TURN))
    ap.add_argument('--latency', type=int, default=1, help='resume round trip in turns')
    ap.add_argument('--autoplay', action='store_true', help='deterministic test player for player 1 (autoplay.py)')
    ap.add_argument('--soak', action='store_true', help='W8 soak player for a whole game (autoplay.SoakPlayer)')
    ap.add_argument('--real-ai', action='store_true', help='S1: computer players run the install\'s race .ai scripts')
    ap.add_argument('--engine-trace', action='store_true', help='engine debug lines "#E ..." into <trace>.engine')
    a = ap.parse_args()
    run(a.map, a.seconds, a.trace, a.humans, a.seed, a.speed, a.latency, autoplay='soak' if a.soak else a.autoplay,
        engine_trace=a.engine_trace, real_ai=a.real_ai)


if __name__ == '__main__':
    main()
