"""Owner-centered confirmations on multi-monitor desktops."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from popup_position import PointerPopup
from tests.test_v193 import FakeDesktop


class CenteredPopupTests(unittest.TestCase):
    def native_popup(self, owner_rect, work):
        window = Mock()
        parent = window.master.winfo_toplevel.return_value
        parent.winfo_id.return_value, window.winfo_id.return_value = 17, 23
        desktop = FakeDesktop()
        desktop.bounds = lambda handle: (handle, owner_rect if handle == 17 else desktop.rect)
        desktop.work_area = Mock(return_value=work)
        with patch("popup_position.sys.platform", "win32"), patch("popup_position.WindowsDesktop", return_value=desktop):
            popup = PointerPopup(window, center_on_parent=True)
        return popup, window, desktop

    def test_centers_on_window_offset_within_secondary_monitor_even_on_first_map(self):
        for dx in (-1920, 1920):
            with self.subTest(dx=dx):
                popup, window, desktop = self.native_popup((dx + 200, 80, dx + 1600, 880), (dx, 0, dx + 1920, 1040))
                desktop.pointer = (400, 120)
                popup.show()
                self.assertEqual(desktop.rect, (dx + 600, 330, dx + 1200, 630))
                desktop.rect = (0, 0, 640, 360)  # Wrapper gets its final decorated size on map.
                popup._mapped(SimpleNamespace(widget=window))
                self.assertEqual(desktop.rect, (dx + 580, 300, dx + 1220, 660))
                self.assertEqual(desktop.pointer_reads, 0)
                desktop.work_area.assert_called_with((dx + 900, 480))

    def test_centers_above_taskbar_even_if_owner_partly_offscreen(self):
        popup, _, desktop = self.native_popup((2950, 550, 4430, 1370), (1920, 0, 3840, 1040))
        popup.show()
        self.assertEqual(desktop.rect, (3240, 740, 3840, 1040))

    def test_late_resize_stays_centered_and_manual_drag_is_respected(self):
        popup, window, desktop = self.native_popup((2200, 100, 3600, 900), (1920, 0, 3840, 1040))
        popup._mapped(SimpleNamespace(widget=window))
        desktop.rect = (2600, 350, 3300, 750)
        popup._fit_after_resize()
        self.assertEqual(desktop.rect, (2550, 300, 3250, 700))
        desktop.rect = (2000, 100, 2700, 500)
        popup._fit_after_resize()
        desktop.rect = (2000, 100, 2700, 550)
        popup._fit_after_resize()
        self.assertEqual(desktop.rect, (2000, 100, 2700, 550))

    def test_tk_fallback_centers_when_no_native_monitor_api(self):
        window = Mock()
        parent = window.master.winfo_toplevel.return_value
        parent.winfo_rootx.return_value, parent.winfo_rooty.return_value = -1600, 100
        parent.winfo_width.return_value, parent.winfo_height.return_value = 1200, 800
        window.winfo_width.return_value, window.winfo_height.return_value = 500, 300
        with patch("popup_position.sys.platform", "other"):
            popup = PointerPopup(window, center_on_parent=True)
        popup.show()
        window.geometry.assert_called_with("+-1250+350")
        window.winfo_pointerxy.assert_not_called()
