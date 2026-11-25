"""Startup timing harness: the longest Tk main-thread block in the first seconds (owner 2026-11-24: "the window lags while I drag it in
the first 5-10 s, until it has found the games").

Runs the WHOLE window (gui.run_gui) with gui_sim inputs in the slow profile (6 s game scan that also burns CPU in its thread,
slow status / VRChat / update check / companion start) while the window is moved every 16 ms like a drag. Every Tk callback
(after / after_idle and the widget event bindings that go through Tk's callback wrapper) is timed, plus a 10 ms heartbeat measures
the real gaps of the main loop (GIL starvation included). Nothing is read from game folders, nothing goes to the network.

  python3 -I -B companion/ui_timing.py [seconds=8] [--view simple|advanced]     -> exit 0 when every callback < LIMIT_MS
(macOS: Homebrew / python.org python with Tk 8.6, never the system python3)
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LIMIT_MS = 50.0          # longest allowed single main-thread callback after the window is up
GAP_MS = 100.0           # longest allowed heartbeat gap (the 10 ms tick came this late) after the window is up


def run(seconds=8.0, view="simple"):
    import tkinter as tk
    calls = []                                   # (t_start, ms, name)
    orig = tk.CallWrapper.__call__

    def name_of(f):
        for cell in (getattr(f, "__closure__", None) or ()):          # after(): the real callback sits in callit's closure
            v = cell.cell_contents
            if callable(v) and getattr(v, "__qualname__", "") not in ("", "Misc.after.<locals>.callit"):
                return getattr(v, "__qualname__", repr(v))
        return getattr(f, "__qualname__", repr(f))

    def timed(self, *args):
        t0 = time.perf_counter()
        try:
            return orig(self, *args)
        finally:
            calls.append((t0, (time.perf_counter() - t0) * 1000.0, name_of(self.func)))
    tk.CallWrapper.__call__ = timed

    import gui
    import gui_sim
    sim = gui_sim.Sim("en", view=view, slow=True, close_after_s=seconds, drag=True)
    beats = []
    t_start = time.perf_counter()
    orig_ready = sim.ready

    def ready(**kw):
        beats.append(time.perf_counter())
        sim.t_ready = time.perf_counter()

        def beat():
            beats.append(time.perf_counter())
            try:
                sim.root.after(10, beat)
            except Exception:                    # noqa: BLE001
                pass
        sim.root.after(10, beat)
        orig_ready(**kw)
    sim.ready = ready
    try:
        gui.run_gui(sim=sim)
    finally:
        tk.CallWrapper.__call__ = orig
    t_ready = getattr(sim, "t_ready", t_start)
    after = [c for c in calls if c[0] >= t_ready and c[2] != "Tk.destroy"]       # the harness closing the window is not startup
    worst = sorted(after, key=lambda c: -c[1])[:8]
    gaps = [(b - a) * 1000.0 for a, b in zip(beats, beats[1:])]
    return {"build_ms": round((t_ready - t_start) * 1000.0, 1), "callbacks": len(after),
            "max_callback_ms": round(worst[0][1], 1) if worst else 0.0,
            "worst": [(round(ms, 1), name, round(t - t_ready, 2)) for t, ms, name in worst],
            "max_gap_ms": round(max(gaps), 1) if gaps else 0.0, "beats": len(beats)}


def main(argv):
    seconds = float(next((a for a in argv if a.replace(".", "").isdigit()), 8))
    view = "advanced" if "advanced" in argv else "simple"
    if "--cpu" in argv:
        import gui_sim
        gui_sim.CPU_ONLY = True
    r = run(seconds, view)
    r["view"] = view
    print(json.dumps(r, indent=1))
    ok = r["max_callback_ms"] < LIMIT_MS and r["max_gap_ms"] < GAP_MS
    print("TIMING %s: longest callback %.1f ms (limit %.0f), longest main-loop gap %.1f ms (limit %.0f)"
          % ("OK" if ok else "FAIL", r["max_callback_ms"], LIMIT_MS, r["max_gap_ms"], GAP_MS))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
