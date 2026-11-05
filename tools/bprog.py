"""Machine-readable build progress of the converters (companion/buildprog.py parses it from stdout).

Protocol (one line each, stdout, flushed; everything else the converters print is ignored by the parser):
    @plan <stage>=<weight> <stage>=<weight> ...     optional, once at the start: the stages of this run and their rough share of the time
    @progress <stage> <done> <total>                 stage <stage> is <done>/<total> finished (items: models, maps, steps ...)
Stages without a plan entry count with weight 10. Lines are throttled (0.25 s) except the last one of a stage.
"""
import sys, time

_last = {}


def plan(**weights):
    print("@plan " + " ".join("%s=%d" % (k, v) for k, v in weights.items()), flush=True)


def progress(stage, done, total):
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    now = time.monotonic()
    if done < total and now - _last.get(stage, -1.0) < 0.25:
        return
    _last[stage] = now
    print("@progress %s %d %d" % (stage, done, total), flush=True)
