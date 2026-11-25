"""Demo / screenshots without a real install (simulated games, status and VRChat state; nothing is read from game folders, nothing goes
to the network, no tray icon, no companion process):
  GSVR_UI_DEMO=start|first|step|ready      the simple view in one state
  GSVR_UI_DEMO=advanced [GSVR_UI_TAB=worlds|status|games|log|settings]   the whole window, Advanced view open (gui_sim.py)
  [GSVR_LANG=en|de|ja] [GSVR_UI_SHOT=x.png  = PNG of the demo window only, then it closes]
python3 -I companion/ui_demo.py   (macOS: use a python.org / Homebrew python with Tk 8.6, never the system python3)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import firstrun_ui  # noqa: E402

if __name__ == "__main__":
    state = os.environ.get("GSVR_UI_DEMO", "step")
    if state.startswith("advanced") or state == "simple":
        os.environ["GSVR_UI_DEMO"] = state
        import gui  # noqa: E402
        gui.run_gui()
    else:
        firstrun_ui.run_demo(state, os.environ.get("GSVR_LANG", "en"), os.environ.get("GSVR_UI_SHOT") or None)
