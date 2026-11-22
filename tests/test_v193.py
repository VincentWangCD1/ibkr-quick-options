"""Offline monitor layouts, popup lifecycle and DPI-context restoration."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from popup_position import fit_position, PointerPopup, WindowsDesktop, _Prompt, messagebox
import tkinter as tk


class FakeDesktop:
    def __init__(self):
        self.pointer_reads = 0
        self.pointer = (2200, 300)
        self.work = (1920, 0, 3840, 1040)
        self.rect = (100, 100, 700, 400)
        self.moves = []

    def capture_pointer(self):
        self.pointer_reads += 1
        return self.pointer, self.work

    def bounds(self, _):
        return 90000, self.rect

    def move(self, handle, point):
        self.moves.append((handle, point))
        left, top, right, bottom = self.rect
        self.rect = (*point, point[0] + right - left, point[1] + bottom - top)

    def work_area(self, point):
        return (0, 0, 1920, 1040) if point[0] < 1920 else self.work


class PopupTests(unittest.TestCase):
    def setup_popup(self):
        desktop = FakeDesktop()
        window = Mock()
        window.winfo_exists.return_value = True
        window.after_idle.return_value = 'idle-1'
        with patch('popup_position.sys.platform', 'win32'), patch('popup_position.WindowsDesktop', return_value=desktop):
            popup = PointerPopup(window)
        return popup, window, desktop

    def test_right_monitor_and_taskbar(self):
        work = (1920, 0, 3840, 1040)
        self.assertEqual(fit_position((2200, 300), (600, 400), work, near_pointer=True), (2212, 312))
        x, y = fit_position((3838, 1038), (600, 400), work, near_pointer=True)
        self.assertGreaterEqual(x, 1920)
        self.assertLessEqual(x + 600, 3840)
        self.assertLessEqual(y + 400, 1040)

    def test_negative_left_monitor_preserves_signed_desktop_coordinates(self):
        self.assertEqual(fit_position((-1600, 100), (600, 400), (-1920, 0, 0, 1040), near_pointer=True), (-1588, 112))
        self.assertEqual(fit_position((-10, 1030), (600, 400), (-1920, 0, 0, 1040), near_pointer=True), (-622, 618))

    def test_above_monitor_and_left_top_taskbars(self):
        work = (50, -1000, 1920, 0)
        x, y = fit_position((20, -1040), (500, 500), work, near_pointer=True)
        self.assertEqual((x, y), (50, -1000))

    def test_portrait_monitor_and_oversized_popup_keep_title_accessible(self):
        work = (3840, 100, 4920, 2020)
        self.assertEqual(fit_position((4900, 2000), (700, 800), work, near_pointer=True), (4188, 1188))
        self.assertEqual(fit_position((4000, 300), (2000, 2200), work, near_pointer=True), (3840, 100))

    def test_all_edges_and_dpi_scaled_sizes_stay_in_work_area(self):
        for work in ((0, 0, 1920, 1040), (-2560, -200, 0, 1200), (1920, 100, 3840, 2080)):
            for scale in (1, 1.25, 1.5, 2):
                size = (int(500 * scale), int(400 * scale))
                for px in (work[0], work[0]+5, work[2]-5, work[2]-1):
                    for py in (work[1], work[1]+5, work[3]-5, work[3]-1):
                        x, y = fit_position((px, py), size, work, near_pointer=True)
                        self.assertTrue(work[0] <= x <= work[2]-size[0])
                        self.assertTrue(work[1] <= y <= work[3]-size[1])

    def test_withdraw_then_place_before_show_and_first_map(self):
        popup, window, desktop = self.setup_popup()
        window.withdraw.assert_called_once()
        window.deiconify.assert_not_called()
        window.deiconify.side_effect = lambda: self.assertEqual(desktop.rect[:2], (2212, 312))
        popup.show()
        window.deiconify.assert_called_once()
        # Simulate Tk recreating the wrapper on map, and the pointer moving away.
        desktop.rect = (100, 100, 700, 400)
        desktop.pointer = (200, 100)
        popup._mapped(SimpleNamespace(widget=window))
        self.assertEqual(desktop.rect[:2], (2212, 312))
        self.assertEqual(desktop.pointer_reads, 1)
        self.assertFalse(popup.opening)

    def test_child_map_does_not_finish_initial_placement(self):
        popup, window, desktop = self.setup_popup()
        popup._mapped(SimpleNamespace(widget=Mock()))
        self.assertTrue(popup.opening)
        self.assertFalse(desktop.moves)

    def test_preview_growth_clamps_to_original_monitor_without_following_pointer(self):
        popup, window, desktop = self.setup_popup()
        desktop.pointer = (2200, 900)
        popup.point = desktop.pointer
        popup.show()
        popup._mapped(SimpleNamespace(widget=window))
        left, top = desktop.rect[:2]
        desktop.rect = (left, top, left+600, top+650)
        desktop.pointer = (100, 100)
        popup._fit_after_resize()
        self.assertEqual(desktop.rect[3], 1040)
        self.assertGreaterEqual(desktop.rect[0], 1920)
        self.assertEqual(desktop.pointer_reads, 1)

    def test_manual_drag_is_respected_and_later_growth_uses_new_monitor(self):
        popup, window, desktop = self.setup_popup()
        popup.show()
        popup._mapped(SimpleNamespace(widget=window))
        desktop.moves.clear()
        desktop.rect = (200, 700, 800, 1000)
        popup._fit_after_resize()
        self.assertEqual(desktop.moves, [])
        self.assertEqual(popup.work, (0, 0, 1920, 1040))
        desktop.rect = (200, 700, 800, 1300)
        popup._fit_after_resize()
        self.assertEqual(desktop.rect, (200, 440, 800, 1040))

    def test_configure_is_coalesced_and_pending_callback_cancelled_on_destroy(self):
        popup, window, desktop = self.setup_popup()
        popup.opening = False
        event = SimpleNamespace(widget=window)
        popup._configured(SimpleNamespace(widget=Mock()))
        popup._configured(event)
        popup._configured(event)
        window.after_idle.assert_called_once()
        popup._destroyed(SimpleNamespace(widget=Mock()))
        window.after_cancel.assert_not_called()
        popup._destroyed(event)
        window.after_cancel.assert_called_once_with('idle-1')
        self.assertIsNone(popup.pending)

    def test_destroyed_popup_callback_is_noop(self):
        popup, window, desktop = self.setup_popup()
        window.winfo_exists.return_value = False
        popup._fit_after_resize()
        self.assertFalse(desktop.moves)

    def test_native_positioning_error_does_not_block_showing_close_ticket(self):
        popup, window, desktop = self.setup_popup()
        desktop.move = Mock(side_effect=OSError('synthetic failure'))
        with self.assertLogs('popup_position', level='WARNING'):
            popup.show()
        window.deiconify.assert_called_once()

    def test_fallback_uses_parent_on_secondary_screen_and_signed_tk_origin(self):
        window = Mock()
        window.winfo_pointerxy.return_value = (-1500, 300)
        parent = window.master.winfo_toplevel.return_value
        parent.winfo_rootx.return_value, parent.winfo_rooty.return_value = -1800, 100
        parent.winfo_width.return_value, parent.winfo_height.return_value = 1200, 900
        window.winfo_width.return_value, window.winfo_height.return_value = 500, 300
        with patch('popup_position.sys.platform', 'other'):
            popup = PointerPopup(window)
        popup.show()
        window.geometry.assert_called_with('+-1488+312')


class DpiContextTests(unittest.TestCase):
    def desktop(self):
        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.api = Mock()
        desktop.api.SetThreadDpiAwarenessContext.return_value = 123456
        return desktop

    def test_restores_context_on_success_and_exception(self):
        for fail in (False, True):
            desktop = self.desktop()
            try:
                with desktop.physical_coordinates():
                    if fail:
                        raise ValueError('test')
            except ValueError:
                pass
            calls = desktop.api.SetThreadDpiAwarenessContext.call_args_list
            self.assertEqual(calls[0].args[0].value, ctypes.c_void_p(-3).value)
            self.assertEqual(calls[-1].args, (123456,))

    def test_move_only_targets_supplied_window_and_does_not_resize_or_activate(self):
        desktop = self.desktop()
        desktop.move(900001234, (-1600, -100))
        desktop.api.SetWindowPos.assert_called_once_with(900001234, None, -1600, -100, 0, 0, 0x15)
        desktop.api.SetThreadDpiAwarenessContext.assert_called_with(123456)


class ConfirmationTests(unittest.TestCase):
    def prompt(self):
        prompt = _Prompt.__new__(_Prompt)
        prompt.parent, prompt.window, prompt.placement = Mock(), Mock(), Mock()
        prompt.yes, prompt.cancel = Mock(), Mock()
        prompt.parent.grab_current.return_value = None
        prompt.window.winfo_exists.return_value = False
        prompt.result = False
        return prompt

    def test_modal_prompt_returns_false_without_explicit_acceptance(self):
        prompt = self.prompt()
        self.assertIs(prompt.show(), False)
        prompt.placement.show.assert_called_once()
        prompt.window.grab_set.assert_called_once()
        prompt.cancel.focus_set.assert_called_once()
        prompt.parent.wait_window.assert_called_once_with(prompt.window)

    def test_explicit_yes_returns_true(self):
        prompt = self.prompt()
        prompt.parent.wait_window.side_effect = lambda _window: prompt.finish(True)
        self.assertIs(prompt.show(), True)

    def test_escape_window_close_and_cancel_remain_false(self):
        prompt = self.prompt()
        prompt.parent.wait_window.side_effect = lambda _window: prompt.finish(False)
        self.assertIs(prompt.show(), False)

    def test_enter_only_accepts_when_yes_button_has_focus(self):
        for focus_yes in (True, False):
            prompt = self.prompt()
            prompt.window.focus_get.return_value = prompt.yes if focus_yes else prompt.cancel
            self.assertEqual(prompt.enter(), "break")
            self.assertIs(prompt.result, focus_yes)

    def test_destroying_owner_while_waiting_cannot_confirm(self):
        prompt = self.prompt()
        prompt.parent.wait_window.side_effect = tk.TclError('owner destroyed')
        self.assertIs(prompt.show(), False)

    def test_previous_modal_grab_restored_after_prompt(self):
        for kind in ('local', 'global'):
            prompt = self.prompt()
            previous = Mock()
            previous.grab_status.return_value = kind
            prompt.parent.grab_current.return_value = previous
            self.assertFalse(prompt.show())
            getattr(previous, 'grab_set_global' if kind == 'global' else 'grab_set').assert_called_once()

    def test_existing_messagebox_api_and_owner_are_preserved(self):
        parent = Mock()
        with patch('popup_position._Prompt') as prompt:
            prompt.return_value.show.return_value = True
            self.assertIs(messagebox.askyesno('确认', '内容', parent=parent), True)
            prompt.assert_called_with('确认', '内容', parent, question=True)
            self.assertEqual(messagebox.showerror('错误', '内容', parent=parent), 'ok')
            prompt.assert_called_with('错误', '内容', parent, question=False)


if __name__ == '__main__':
    unittest.main()
