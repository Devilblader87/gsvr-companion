"""Language-switch window fit check (demo mode, simulated state, no folders / network):
EN -> DE -> JA -> EN through FirstRun.set_lang(); after every switch the window must be >= the content's required size and
never smaller than before. Exit 0 = ok. Usage: python3 -I -B companion/ui_langfit.py   (needs a display + Tk 8.6)"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import firstrun_ui  # noqa: E402
import i18n  # noqa: E402


def pump(root, secs=1.0):
    end = time.time() + secs
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def main():
    import tkinter as tk
    i18n.set_lang("en")
    settings, ents, st, running, vrc, job, upd = firstrun_ui.demo_inputs("step")
    root = tk.Tk()
    root.tk.call("tk", "scaling", 96.0 / 72.0)
    c = firstrun_ui._DemoController(settings, st, running)
    fr = firstrun_ui.FirstRun(root, c, lambda fn: fn(), on_advanced=lambda: None, on_lang=lambda code: i18n.set_lang(code),
                              on_check=lambda: None, tray_ok=lambda: True, detect_fn=lambda s: ents, vrc_fn=lambda s, conn: vrc)
    fr.demo_job, fr.upd = job, upd
    fr._open()
    root.geometry("+40+60")
    pump(root)
    bad, prev = [], (root.winfo_width(), root.winfo_height())
    print("start en", prev, "need", fr.required_size())
    for code in ("de", "ja", "en"):
        fr.set_lang(code)
        pump(root)
        w, h = root.winfo_width(), root.winfo_height()
        nw, nh = fr.required_size()
        print(code, (w, h), "need", (nw, nh), "minsize", root.minsize())
        if w < nw or h < nh:
            bad.append("%s: window %dx%d < required %dx%d" % (code, w, h, nw, nh))
        if w < prev[0] or h < prev[1]:
            bad.append("%s: window shrank %s -> %dx%d" % (code, prev, w, h))
        prev = (w, h)
    root.destroy()
    for b in bad:
        print("FAIL", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
