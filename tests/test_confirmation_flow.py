"""The season picker and the 🔄 movie/series flip, exercised on pending-action dicts.

These are the pure parts of confirmation_flow - no Telethon calls - so they can be run
without a bot. Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import unittest

from confirmation_flow import (
    MAX_SEASON,
    MIN_SEASON,
    _confirmation_buttons,
    _sync_season,
    _target_display,
    adjust_season,
    flip_base,
    item_season,
)
from media_organizer import parse_media_name


def group_action(base, folder, *file_names):
    action = {
        "type": "video_group",
        "chat_id": 1,
        "base": base,
        "folder": folder,
        "season": None,
        "candidate": None,
        "candidate_score": 0,
        "items": [{"file_name": n, "parsed": parse_media_name(n), "message": None} for n in file_names],
        "status_msg": None,
    }
    _sync_season(action)
    return action


def torrent_action(base, folder, name):
    action = {
        "type": "torrent",
        "chat_id": 1,
        "base": base,
        "folder": folder,
        "parsed": parse_media_name(name),
        "season": parse_media_name(name).get("season"),
        "candidate": None,
        "candidate_score": 0,
        "hash": "deadbeef",
        "name": name,
        "status_msg": None,
    }
    _sync_season(action)
    return action


class SeasonResolution(unittest.TestCase):
    def test_movies_have_no_season_at_all(self):
        action = group_action("movies", "Dune (2021)", "Dune.2021.2160p.mkv")
        self.assertIsNone(action["season"])
        self.assertIsNone(item_season(action, action["items"][0]["parsed"]))

    def test_series_with_no_detectable_season_defaults_to_one(self):
        action = group_action("tv", "Chernobyl", "Chernobyl.1080p.mkv")
        self.assertEqual(action["season"], 1)

    def test_a_shared_season_is_picked_up_from_the_filenames(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv", "The.Office.S03E02.mkv")
        self.assertEqual(action["season"], 3)

    def test_a_mixed_batch_keeps_each_files_own_season(self):
        action = group_action("tv", "The Office", "The.Office.S01E01.mkv", "The.Office.S02E01.mkv")
        self.assertIsNone(action["season"], "one picker value would flatten S01 and S02 into one folder")
        self.assertEqual(item_season(action, action["items"][0]["parsed"]), 1)
        self.assertEqual(item_season(action, action["items"][1]["parsed"]), 2)

    def test_the_picker_overrides_every_files_own_season(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv", "The.Office.S03E02.mkv")
        adjust_season(action, +1)
        self.assertEqual(item_season(action, action["items"][0]["parsed"]), 4)


class SeasonPicker(unittest.TestCase):
    def test_plus_and_minus_move_the_season(self):
        action = torrent_action("tv", "The Office", "The.Office.S03.mkv")
        self.assertEqual(adjust_season(action, +1), 4)
        self.assertEqual(adjust_season(action, -1), 3)

    def test_it_clamps_instead_of_going_negative_or_absurd(self):
        action = torrent_action("tv", "The Office", "The.Office.S01.mkv")
        for _ in range(5):
            adjust_season(action, -1)
        self.assertEqual(action["season"], MIN_SEASON)
        action["season"] = MAX_SEASON
        self.assertEqual(adjust_season(action, +1), MAX_SEASON)

    def test_there_is_no_picker_for_movies_or_mixed_batches(self):
        movie = group_action("movies", "Dune (2021)", "Dune.2021.mkv")
        mixed = group_action("tv", "The Office", "The.Office.S01E01.mkv", "The.Office.S02E01.mkv")
        self.assertIsNone(adjust_season(movie, +1))
        self.assertIsNone(adjust_season(mixed, +1))


class Flip(unittest.TestCase):
    def test_movie_to_series_gains_a_season(self):
        action = group_action("movies", "Movie Name", "Movie.Name.101.mkv")
        self.assertEqual(flip_base(action), "tv")
        self.assertEqual(action["season"], 1)

    def test_series_to_movie_drops_the_season(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv")
        self.assertEqual(flip_base(action), "movies")
        self.assertIsNone(action["season"])

    def test_flipping_back_restores_the_detected_season(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv")
        flip_base(action)
        flip_base(action)
        self.assertEqual((action["base"], action["season"]), ("tv", 3))


class ConfirmationKeyboard(unittest.TestCase):
    @staticmethod
    def _callback_data(rows):
        return [b.data.decode() for row in rows for b in row if getattr(b, "data", None)]

    def test_every_confirmation_can_be_flipped(self):
        for action in [
            group_action("movies", "Dune (2021)", "Dune.2021.mkv"),
            group_action("tv", "The Office", "The.Office.S03E01.mkv"),
        ]:
            with self.subTest(base=action["base"]):
                data = self._callback_data(_confirmation_buttons("abc123", action))
                self.assertIn("flip:abc123", data)

    def test_the_season_row_only_shows_where_it_can_do_something(self):
        series = self._callback_data(_confirmation_buttons("abc123", group_action("tv", "X", "X.S02E01.mkv")))
        movie = self._callback_data(_confirmation_buttons("abc123", group_action("movies", "Y", "Y.2021.mkv")))
        mixed = self._callback_data(
            _confirmation_buttons("abc123", group_action("tv", "Z", "Z.S01E01.mkv", "Z.S02E01.mkv"))
        )
        self.assertIn("season:abc123:1", series)
        self.assertIn("season:abc123:-1", series)
        self.assertFalse([d for d in movie if d.startswith("season:")])
        self.assertFalse([d for d in mixed if d.startswith("season:")])


class TargetPreview(unittest.TestCase):
    def test_a_mixed_batch_does_not_pretend_to_have_one_destination(self):
        action = group_action("tv", "The Office", "The.Office.S01E01.mkv", "The.Office.S02E01.mkv")
        self.assertTrue(_target_display(action).endswith("Season XX"))

    def test_a_single_season_batch_shows_the_real_folder(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv")
        self.assertTrue(_target_display(action).endswith("Season 03"))


if __name__ == "__main__":
    unittest.main()
