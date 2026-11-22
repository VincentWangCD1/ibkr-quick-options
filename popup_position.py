"""Position Tk popups at the pointer or centered on their parent window.

Win32 calls use physical coordinates in a temporary thread DPI context.  Neither
the process DPI mode nor Tk scaling is changed.  Only the supplied popup moves.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import logging
import sys
import tkinter as tk
import localized_widgets as ttk


def fit_position(point, size, work, *, near_pointer=False, gap=12):
    """Return a signed desktop origin; never interpret negatives as right offsets."""
    x, y = point
    width, height = size
    left, top, right, bottom = work
    if near_pointer:
        x, y = x + gap, y + gap
        if x + width > right:
            x = point[0] - width - gap
        if y + height > bottom:
            y = point[1] - height - gap
    return (max(left, min(x, max(left, right - width))),
            max(top, min(y, max(top, bottom - height))))


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class WindowsDesktop:
    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
            "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "MonitorFromPoint": ([wintypes.POINT, wintypes.DWORD], wintypes.HANDLE),
            "GetMonitorInfoW": ([wintypes.HANDLE, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    @contextmanager
    def physical_coordinates(self):
        # PER_MONITOR_AWARE, supported since Windows 10 1607. Always restore,
        # including on failure; no Tk events/window creation in this context.
        previous = self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))
        if not previous:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            self.api.SetThreadDpiAwarenessContext(previous)

    def _work_area(self, point):
        monitor = self.api.MonitorFromPoint(wintypes.POINT(*point), 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self.api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom

    def capture_pointer(self):
        with self.physical_coordinates():
            point = wintypes.POINT()
            if not self.api.GetCursorPos(ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
            xy = point.x, point.y
            return xy, self._work_area(xy)

    def bounds(self, child_id):
        with self.physical_coordinates():
            # winfo_id identifies Tk's client; GA_ROOT identifies its decorated
            # wrapper. Include the title bar/borders when avoiding the taskbar.
            handle = self.api.GetAncestor(child_id, 2) or child_id
            rect = wintypes.RECT()
            if not self.api.GetWindowRect(handle, ctypes.byref(rect)):
                raise ctypes.WinError(ctypes.get_last_error())
            return handle, (rect.left, rect.top, rect.right, rect.bottom)

    def work_area(self, point):
        with self.physical_coordinates():
            return self._work_area(point)

    def move(self, handle, point):
        with self.physical_coordinates():
            # NOSIZE | NOZORDER | NOACTIVATE: never resize/focus unrelated windows.
            if not self.api.SetWindowPos(handle, None, *point, 0, 0, 0x0015):
                raise ctypes.WinError(ctypes.get_last_error())


class PointerPopup:
    """Create before populating a Toplevel, then call show() after building it.

    Choose the anchor when the popup opens. Keep it in the monitor's work
    area and respect subsequent manual drags.
    """
    def __init__(self, window, *, center_on_parent=False):
        self.window = window
        self.center_on_parent = center_on_parent
        window.withdraw()
        self.desktop = None
        self.pending = None
        self.size = None
        self.last_origin = None
        self.opening = True
        parent = window.master.winfo_toplevel()
        if sys.platform == "win32":
            try:
                self.desktop = WindowsDesktop()
                if center_on_parent:
                    _, (left, top, right, bottom) = self.desktop.bounds(parent.winfo_id())
                    self.point = (left + right) // 2, (top + bottom) // 2
                    self.work = self.desktop.work_area(self.point)
                else:
                    self.point, self.work = self.desktop.capture_pointer()
            except (OSError, AttributeError):
                self.desktop = None
                logging.getLogger(__name__).warning("弹窗显示器定位不可用，使用主窗口范围定位", exc_info=True)
        if self.desktop is None:
            left, top = parent.winfo_rootx(), parent.winfo_rooty()
            width, height = max(1, parent.winfo_width()), max(1, parent.winfo_height())
            self.work = left, top, left + width, top + height
            self.point = (left + width // 2, top + height // 2) if center_on_parent else window.winfo_pointerxy()
        window.bind("<Map>", self._mapped, add="+")
        window.bind("<Configure>", self._configured, add="+")
        window.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        if event.widget is self.window and self.pending is not None:
            self.window.after_cancel(self.pending)
            self.pending = None

    def _place(self, initial=False):
        if self.desktop is not None:
            handle, rect = self.desktop.bounds(self.window.winfo_id())
            size = rect[2] - rect[0], rect[3] - rect[1]
            current = rect[:2]
        else:
            size = self.window.winfo_width(), self.window.winfo_height()
            current = self.window.winfo_x(), self.window.winfo_y()
        if self.center_on_parent and (initial or current == self.last_origin):
            origin = self.point[0] - size[0] // 2, self.point[1] - size[1] // 2
        else:
            origin = self.point if initial else current
        point = fit_position(origin, size, self.work, near_pointer=initial and not self.center_on_parent)
        if self.desktop is not None:
            if point != current:
                self.desktop.move(handle, point)
        else:
            # A leading '+' followed by a signed integer means absolute desktop
            # coordinates in Tk; '-123' alone means offset from the right edge.
            self.window.geometry(f"+{point[0]}+{point[1]}")
        self.size, self.last_origin = size, point

    def show(self):
        self.window.update_idletasks()
        self._safe_place(initial=True)
        self.window.deiconify()

    def _safe_place(self, initial=False):
        try:
            self._place(initial)
        except (OSError, tk.TclError):
            logging.getLogger(__name__).warning("弹窗位置调整失败", exc_info=True)

    def _mapped(self, event):
        if event.widget is self.window and self.opening:
            # Tk can recreate its native wrapper on the first map. Position it
            # again before painting, then leave all later user moves alone.
            self._safe_place(initial=True)
            self.opening = False

    def _configured(self, event):
        if event.widget is not self.window or self.opening:
            return
        if self.pending is None:
            self.pending = self.window.after_idle(self._fit_after_resize)

    def _fit_after_resize(self):
        self.pending = None
        try:
            if not self.window.winfo_exists():
                return
            if self.desktop is not None:
                _, rect = self.desktop.bounds(self.window.winfo_id())
                size = rect[2] - rect[0], rect[3] - rect[1]
                if size == self.size:
                    # Dragging to another monitor changes the later resize
                    # boundary, but never snaps a user-dragged window back.
                    self.work = self.desktop.work_area((rect[0] + size[0] // 2,
                                                        rect[1] + size[1] // 2))
                    return
            else:
                size = self.window.winfo_width(), self.window.winfo_height()
                if size == self.size:
                    return
            self._place()
        except (OSError, tk.TclError):
            logging.getLogger(__name__).warning("弹窗尺寸变化后定位失败", exc_info=True)


class _Prompt:
    """Center confirmations on the owner and show errors near the pointer.

    A local grab and wait_window keep the usual modal behavior. Window close,
    Escape, or parent destruction always cancel; no broker calls live here.
    """
    def __init__(self, title, message, parent, question):
        self.result = False
        self.parent = parent
        if getattr(parent, "bean_mode", False):
            from privacy_view import mask_money
            message = mask_money(message)
        self.window = window = ttk.Toplevel(parent)
        self.placement = PointerPopup(window, center_on_parent=question)
        window.title(title)
        window.transient(parent.winfo_toplevel())
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, justify="left", wraplength=580,
                  style="Confirmation.TLabel" if question else "TLabel").pack(fill="x", pady=(0, 14))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.yes = None
        if question:
            self.yes = ttk.Button(buttons, text="是 / 确认", style="Confirmation.TButton", command=lambda: self.finish(True))
            self.yes.pack(side="left", padx=(0, 12))
            self.yes.bind("<Return>", self.enter)
        self.cancel = ttk.Button(buttons, text="否 / 取消" if question else "确定", command=lambda: self.finish(False))
        self.cancel.pack(side="right")
        self.cancel.bind("<Return>", self.enter)
        window.protocol("WM_DELETE_WINDOW", lambda: self.finish(False))
        window.bind("<Escape>", lambda _event: self.finish(False))
        window.bind("<Return>", self.enter)

    def enter(self, _event=None):
        self.finish(self.yes is not None and self.window.focus_get() is self.yes)
        return "break"

    def finish(self, accepted):
        self.result = bool(accepted)
        self.window.destroy()

    def show(self):
        previous = self.parent.grab_current()
        previous_status = previous.grab_status() if previous is not None else None
        try:
            self.placement.show()
            self.window.wait_visibility()
            self.window.grab_set()
            self.cancel.focus_set()
            self.parent.wait_window(self.window)
        except tk.TclError:
            # Closing the owner while waiting must never confirm a trade.
            self.result = False
        finally:
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
                if previous is not None and previous.winfo_exists():
                    if previous_status == "global":
                        previous.grab_set_global()
                    elif previous_status == "local":
                        previous.grab_set()
            except tk.TclError:
                pass
        return self.result


class PointerMessagebox:
    @staticmethod
    def askyesno(title, message, *, parent):
        return _Prompt(title, message, parent, question=True).show()

    @staticmethod
    def showerror(title, message, *, parent):
        _Prompt(title, message, parent, question=False).show()
        return "ok"


messagebox = PointerMessagebox()
