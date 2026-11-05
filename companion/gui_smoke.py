import sys, os, tkinter as tk
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("GSVR_GUI_SMOKE", "1")
orig = tk.Tk.mainloop
def short(self, n=0):
    self.after(4000, self.destroy)          # build everything, run 4 s of after() callbacks, then close
    orig(self, n)
tk.Tk.mainloop = short
tk.Misc.mainloop = short
import gui
gui.run_gui()
print("GUI SMOKE OK")
