"""The main menu and the downloads screen's tab strip - the two keyboards whose layout is
decided by code rather than written out literally. Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import unittest

import bot_state as state
from keyboards import get_main_keyboard
from torrents_screen import _tab_rows


def labels(rows):
    return [b.text for row in rows for b in row]


def callback_data(rows):
    return [b.data.decode() for row in rows for b in row if getattr(b, "data", None)]


class MainMenu(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, state, "active_download_count", state.active_download_count)
        state.active_download_count = 0

    def test_exactly_one_destination_is_marked(self):
        for mode, expected in (("movies", "סרטים"), ("tv", "סדרות")):
            with self.subTest(mode=mode):
                marked = [text for text in labels(get_main_keyboard(mode)) if text.startswith("✅")]
                self.assertEqual(len(marked), 1)
                self.assertIn(expected, marked[0])

    def _downloads_button(self):
        return next(text for text in labels(get_main_keyboard("movies")) if text.startswith("📥"))

    def test_the_badge_only_appears_while_something_is_running(self):
        self.assertNotIn("·", self._downloads_button())
        state.active_download_count = 3
        self.assertEqual(self._downloads_button(), "📥 הורדות · 3 פעילות")

    def test_a_single_download_is_counted_in_the_singular(self):
        state.active_download_count = 1
        self.assertEqual(self._downloads_button(), "📥 הורדות · 1 פעילה")

    def test_the_badge_does_not_change_where_the_button_goes(self):
        idle = callback_data(get_main_keyboard("movies"))
        state.active_download_count = 7
        self.assertEqual(callback_data(get_main_keyboard("movies")), idle)


class DownloadTabs(unittest.TestCase):
    def test_error_and_queue_stay_hidden_while_empty(self):
        shown = labels(_tab_rows({"active": 2, "waiting": 0, "completed": 9}, "active"))
        self.assertEqual(len(shown), 3)
        self.assertNotIn("tor:filter:error", callback_data(_tab_rows({"active": 2}, "active")))

    def test_a_non_empty_queue_brings_its_tab_back(self):
        data = callback_data(_tab_rows({"active": 1, "queue": 2}, "active"))
        self.assertIn("tor:filter:queue", data)
        self.assertNotIn("tor:filter:error", data)

    def test_the_tab_you_are_on_is_shown_even_at_zero(self):
        rows = _tab_rows({"active": 0, "waiting": 0, "completed": 0, "error": 0, "queue": 0}, "error")
        marked = [text for text in labels(rows) if text.startswith("▶")]
        self.assertEqual(marked, ["▶ שגיאה 0"])

    def test_every_tab_carries_its_own_count(self):
        counts = {"active": 2, "waiting": 1, "completed": 12, "queue": 4}
        shown = labels(_tab_rows(counts, "active"))
        for key, count in counts.items():
            with self.subTest(tab=key):
                self.assertTrue(any(text.endswith(f" {count}") for text in shown))

    def test_a_full_strip_splits_evenly_instead_of_stranding_one_tab(self):
        rows = _tab_rows({"active": 2, "waiting": 1, "completed": 12, "error": 1, "queue": 4}, "queue")
        self.assertEqual([len(row) for row in rows], [3, 2])

    def test_no_row_is_ever_wider_than_four(self):
        for selected in ("active", "queue", "error"):
            counts = {"active": 1, "waiting": 1, "completed": 1, "error": 1, "queue": 1}
            with self.subTest(selected=selected):
                self.assertLessEqual(max(len(row) for row in _tab_rows(counts, selected)), 4)


if __name__ == "__main__":
    unittest.main()
