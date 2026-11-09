"""--follow-all-logs: logwatch.MultiTailer follows both VRChat clients' logs (two-client tests).

Run: python3 -I companion/test_logwatch_multi.py
"""
import os, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logwatch


class MultiTailerTest(unittest.TestCase):
    def test_two_logs(self):
        d = tempfile.mkdtemp()
        a = os.path.join(d, "output_log_2026-11-09_06-00-00.txt")
        old = os.path.join(d, "output_log_2026-11-01_06-00-00.txt")
        with open(old, "w") as fh:
            fh.write("x Joining wrld_old\n")
        os.utime(old, (time.time() - 86400, time.time() - 86400))
        with open(a, "w") as fh:
            fh.write("x Joining wrld_aaa\n[GSVR-REQ] map=valve/c0a0 req=1\n")
        stop, got = threading.Event(), []
        t = threading.Thread(target=lambda: [got.append(l) for l in logwatch.MultiTailer(d, poll=0.05).lines(stop)], daemon=True)
        t.start()
        time.sleep(0.3)
        b = os.path.join(d, "output_log_2026-11-09_06-01-00.txt")
        with open(b, "w") as fh:
            fh.write("y [GSVR-REQ] map=valve/c0a0b req=2\n")
        time.sleep(2.5)
        with open(a, "a") as fh:
            fh.write("z [GSVR-REQ] map=valve/c0a0a req=3\n")
        time.sleep(0.5)
        stop.set()
        t.join(2)
        txt = "\n".join(got)
        self.assertIn("wrld_aaa", txt)
        self.assertNotIn("wrld_old", txt)
        self.assertNotIn("req=1", txt)                 # history of a log present at the start is not replayed
        self.assertIn("__OPENED__ " + b, txt)
        self.assertIn("req=2", txt)                    # the second client's log, from its start
        self.assertIn("req=3", txt)                    # the first client's log keeps being followed


if __name__ == "__main__":
    unittest.main()
