"""Callback data structure and routing tests.

Verifies that every inline button drawn by the UI generators emits callback_data
with valid prefixes and expected field counts so no button causes an IndexError.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import unittest

from confirmation_flow import _confirmation_buttons
from file_manager_ui import _int_part
from keyboards import get_main_keyboard
from torrents_screen import _tab_rows


class CallbackHelper(unittest.TestCase):
    def test_int_part_returns_integer_when_valid(self):
        parts = ["fb", "delete", "5", "12"]
        self.assertEqual(_int_part(parts, 2), 5)
        self.assertEqual(_int_part(parts, 3), 12)

    def test_int_part_returns_none_on_missing_or_invalid_index(self):
        parts = ["fb", "act", "epoch"]
        self.assertIsNone(_int_part(parts, 3))
        self.assertIsNone(_int_part(parts, 10))

    def test_int_part_handles_empty_list(self):
        self.assertIsNone(_int_part([], 0))


class MainMenuCallbacks(unittest.TestCase):
    def test_all_main_menu_buttons_have_valid_callback_data(self):
        keyboard = get_main_keyboard("movies")
        for row in keyboard:
            for btn in row:
                data = btn.data.decode("utf-8") if getattr(btn, "data", None) else None
                self.assertIsNotNone(data)
                # Known main menu callback targets
                self.assertTrue(
                    data in ("set_movies", "set_tv", "tor:open", "fb:open", "jf:open", "help")
                )


class ConfirmationButtonsRouting(unittest.TestCase):
    def test_confirmation_buttons_data_format(self):
        action = {
            "type": "video_group",
            "chat_id": 100,
            "base": "tv",
            "folder": "The Office",
            "season": 3,
            "candidate": "The Office (US)",
            "candidate_score": 90,
            "items": [{"file_name": "S03E01.mkv"}],
        }
        rows = _confirmation_buttons("test_pid", action)
        valid_prefixes = ("confirm:", "use_existing:", "flip:", "season:", "rename:", "cancel:")
        for row in rows:
            for btn in row:
                data = btn.data.decode("utf-8")
                self.assertTrue(data.startswith(valid_prefixes), f"Unknown prefix: {data}")
                parts = data.split(":")
                self.assertGreaterEqual(len(parts), 2)
                self.assertEqual(parts[1], "test_pid")


class TorrentTabStripRouting(unittest.TestCase):
    def test_tab_strip_callback_data_format(self):
        counts = {"active": 2, "waiting": 1, "completed": 5}
        rows = _tab_rows(counts, "active")
        for row in rows:
            for btn in row:
                data = btn.data.decode("utf-8")
                self.assertTrue(data.startswith("tor:filter:"))
                parts = data.split(":")
                self.assertEqual(len(parts), 3)
                self.assertIn(parts[2], ("active", "waiting", "completed", "error", "queue"))


if __name__ == "__main__":
    unittest.main()
