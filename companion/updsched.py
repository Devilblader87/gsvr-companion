"""When the GUI re-checks the release feed (pure logic, no Tk, no network; tested in test_updsched.py).

The companion often runs for days (Windows autostart --minimized), so the single check at startup is not enough:
 - every CHECK_EVERY (6 h) while running, only when the setting auto_update_check is on;
 - when the window is restored / shown again (at most every MIN_GAP, so minimise/restore cycling does not hit the feed);
 - after a failed check the next try is sooner (RETRY_FIRST, doubling up to CHECK_EVERY) - a flaky network never makes the GUI hammer the feed;
 - never while an install runs and never while another check is in flight.
A manual check (button) always runs and counts as a fresh check.
"""
CHECK_EVERY = 6 * 3600.0
MIN_GAP = 30 * 60.0          # restore / show re-check only if the last successful check is older than this
RETRY_FIRST = 5 * 60.0       # first retry after an error; doubles per further error up to CHECK_EVERY


class Scheduler:
    def __init__(self, now=0.0):
        self.last_ok = now           # the startup check counts: it is scheduled right after the GUI opens
        self.fails = 0
        self.next_at = now + CHECK_EVERY
        self.inflight = False

    def _blocked(self, enabled, installing):
        return (not enabled) or installing or self.inflight

    def due(self, now, enabled=True, installing=False):
        """periodic tick: True = start a (quiet) check now"""
        return (not self._blocked(enabled, installing)) and now >= self.next_at

    def on_restore(self, now, enabled=True, installing=False):
        """window restored / shown: True = start a (quiet) check now"""
        if self._blocked(enabled, installing):
            return False
        if self.fails:                                 # in backoff: wait for the retry time, do not shortcut it
            return now >= self.next_at
        return now - self.last_ok >= MIN_GAP

    def started(self):
        self.inflight = True

    def finished(self, ok, now):
        self.inflight = False
        if ok:
            self.fails = 0
            self.last_ok = now
            self.next_at = now + CHECK_EVERY
        else:
            self.fails += 1
            self.next_at = now + min(CHECK_EVERY, RETRY_FIRST * (2 ** (self.fails - 1)))
