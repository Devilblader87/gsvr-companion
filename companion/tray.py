"""System tray icon for the GUI (Windows, stdlib ctypes only - no pystray). Closing the window hides it; the companion keeps
running; Quit only via the tray menu. Elsewhere (or if the tray cannot be created) the window is minimised instead of quit.
The pure decision lives in close_action() so it is unit-tested without Tk or Win32."""
import queue
import sys
import threading


def load_tray_icon(u32, path=None):
    """HICON of gsvr.ico at the small-icon size of the system (DPI aware), else IDI_APPLICATION. Never raises."""
    try:
        if path is None:
            import ui_theme
            path = ui_theme.icon_path()
        if path:
            h = u32.LoadImageW(None, path, 1, u32.GetSystemMetrics(49), u32.GetSystemMetrics(50), 0x10)   # IMAGE_ICON, SM_CX/CYSMICON, LR_LOADFROMFILE
            if h:
                return h
    except Exception:                                          # noqa: BLE001 - fall back to the stock icon
        pass
    return u32.LoadIconW(None, 32512)                          # IDI_APPLICATION


def close_action(tray_ok, hint_shown):
    """-> (what, show_hint). what = 'hide' (tray icon exists) | 'iconify' (Tk-only fallback); the one-time hint is shown on the first close."""
    return ("hide" if tray_ok else "iconify", not hint_shown)


class Tray:
    """events (open / quit) land in self.events; the Tk thread polls them (no Tk calls from the tray thread)."""

    def __init__(self, tip, open_label, quit_label):
        self.tip, self.open_label, self.quit_label = tip, open_label, quit_label
        self.events = queue.Queue()
        self.ok = False
        self._hwnd = None
        self._icon = None
        self._ready = threading.Event()

    def start(self):
        if sys.platform != "win32":
            return False
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(3)
        return self.ok

    def stop(self):
        if self.ok and self._hwnd:
            try:
                import ctypes
                u32 = ctypes.windll.user32
                u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
                u32.PostMessageW(self._hwnd, 0x0010, 0, 0)                        # WM_CLOSE -> removes the icon
            except Exception:                                    # noqa: BLE001
                pass

    def _run(self):
        try:
            import ctypes
            from ctypes import wintypes
            u32, s32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
            WM_USER, WM_TRAY, WM_DESTROY = 0x0400, 0x0400 + 20, 0x0002
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            u32.DefWindowProcW.restype = LRESULT
            u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            # 64-bit: without these ctypes truncates handles to c_int (HMODULE of the exe, HWND, HICON, HMENU)
            k32.GetModuleHandleW.restype = wintypes.HMODULE
            k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            u32.CreateWindowExW.restype = wintypes.HWND
            u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p]
            u32.LoadIconW.restype = wintypes.HANDLE
            u32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            u32.LoadImageW.restype = wintypes.HANDLE
            u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u32.GetSystemMetrics.argtypes = [ctypes.c_int]
            u32.DestroyIcon.argtypes = [wintypes.HANDLE]
            u32.CreatePopupMenu.restype = wintypes.HANDLE
            u32.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            u32.TrackPopupMenu.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
            u32.DestroyMenu.argtypes = [wintypes.HANDLE]
            u32.DestroyWindow.argtypes = [wintypes.HWND]
            u32.SetForegroundWindow.argtypes = [wintypes.HWND]
            u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            class WNDCLASS(ctypes.Structure):
                _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

            class NOTIFYICONDATA(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                            ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
                            ("szTip", ctypes.c_wchar * 128)]

            nid = NOTIFYICONDATA()

            def menu():
                m = u32.CreatePopupMenu()
                u32.AppendMenuW(m, 0, 1, self.open_label)
                u32.AppendMenuW(m, 0, 2, self.quit_label)
                pt = wintypes.POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                u32.SetForegroundWindow(self._hwnd)
                cmd = u32.TrackPopupMenu(m, 0x0100 | 0x0002, pt.x, pt.y, 0, self._hwnd, None)   # TPM_RETURNCMD | RIGHTBUTTON
                u32.DestroyMenu(m)
                if cmd == 1:
                    self.events.put("open")
                elif cmd == 2:
                    self.events.put("quit")

            def proc(hwnd, msg, wp, lp):
                if msg == WM_TRAY:
                    if lp in (0x0202, 0x0203):                   # left click / double click
                        self.events.put("open")
                    elif lp == 0x0205:                           # right button up
                        menu()
                    return 0
                if msg == 0x0010:                                # WM_CLOSE
                    s32.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE
                    u32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    u32.PostQuitMessage(0)
                    return 0
                return u32.DefWindowProcW(hwnd, msg, wp, lp)

            cb = WNDPROC(proc)
            wc = WNDCLASS()
            wc.lpfnWndProc = cb
            wc.hInstance = k32.GetModuleHandleW(None)
            wc.lpszClassName = "GSVRCompanionTray"
            self._icon = load_tray_icon(u32)
            wc.hIcon = self._icon
            u32.RegisterClassW(ctypes.byref(wc))
            self._hwnd = u32.CreateWindowExW(0, wc.lpszClassName, "GSVR", 0, 0, 0, 0, 0, None, None, wc.hInstance, None)
            if not self._hwnd:
                self._ready.set()
                return
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = 0x1 | 0x2 | 0x4                         # MESSAGE | ICON | TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._icon
            nid.szTip = self.tip[:127]
            self.ok = bool(s32.Shell_NotifyIconW(0, ctypes.byref(nid)))      # NIM_ADD
            self._ready.set()
            msg = wintypes.MSG()
            while self.ok and u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception:                                        # noqa: BLE001 - fall back to minimise
            self.ok = False
        finally:
            self._ready.set()
            ic, self._icon = self._icon, None
            if ic:                                               # the message loop ended: the icon is removed, free the HICON
                try:
                    import ctypes
                    ctypes.windll.user32.DestroyIcon.argtypes = [ctypes.c_void_p]
                    ctypes.windll.user32.DestroyIcon(ic)
                except Exception:                                # noqa: BLE001
                    pass
