"""The season picker and the 🔄 movie/series flip, exercised on pending-action dicts.

These are the pure parts of confirmation_flow - no Telethon calls - so they can be run
without a bot. Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot_state as state
import confirmation_flow
from confirmation_flow import (
    MAX_SEASON,
    MIN_SEASON,
    _confirmation_buttons,
    _drop_awaiting_text,
    _handle_rename_reply,
    _set_awaiting_text,
    _sync_season,
    _target_display,
    adjust_season,
    flip_base,
    item_season,
    open_folder_picker,
    pick_existing_folder,
    rename_prompt,
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


class AbandonedPrompts(unittest.TestCase):
    """A rename prompt replaces the confirmation's buttons, so walking away from it must
    bring them back - otherwise the files behind it can never be confirmed."""

    def setUp(self):
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()
        self.bot.edit_message = mock.AsyncMock()
        self.addCleanup(state.pending_actions.clear)
        self.addCleanup(state.awaiting_text_input.clear)

    def open_confirmation(self, pending_id):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv")
        action["status_msg"] = mock.Mock(id=77)
        state.pending_actions[pending_id] = action
        return action

    def test_dropping_a_rename_prompt_redraws_its_confirmation(self):
        self.open_confirmation("aaa")
        state.awaiting_text_input[1] = {"kind": "rename", "target": "aaa"}
        asyncio.run(_drop_awaiting_text(self.bot, 1))
        self.assertNotIn(1, state.awaiting_text_input)
        self.bot.edit_message.assert_awaited_once()
        rows = self.bot.edit_message.await_args.kwargs["buttons"]
        self.assertIn(b"confirm:aaa", [b.data for row in rows for b in row])

    def test_a_new_prompt_brings_back_the_confirmation_it_replaces(self):
        self.open_confirmation("aaa")
        self.open_confirmation("bbb")
        asyncio.run(_set_awaiting_text(self.bot, 1, "rename", "aaa"))
        asyncio.run(_set_awaiting_text(self.bot, 1, "rename", "bbb"))
        self.assertEqual(state.awaiting_text_input[1]["target"], "bbb")
        self.bot.edit_message.assert_awaited_once()  # "aaa" redrawn with its buttons

    def test_tapping_the_same_rename_twice_is_not_a_replacement(self):
        self.open_confirmation("aaa")
        asyncio.run(_set_awaiting_text(self.bot, 1, "rename", "aaa"))
        asyncio.run(_set_awaiting_text(self.bot, 1, "rename", "aaa"))
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message.assert_not_awaited()

    def test_the_rename_prompt_keeps_a_way_back(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv")
        _, rows = rename_prompt("aaa", action)
        self.assertEqual(rows[-1][0].data, b"rename_back:aaa")


class RenameKeepsTheSeason(unittest.TestCase):
    def setUp(self):
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()
        self.addCleanup(state.pending_actions.clear)

    def test_a_title_only_rename_keeps_the_picked_season(self):
        state.pending_actions["aaa"] = action = group_action("tv", "Office", "Office.S01E01.mkv")
        adjust_season(action, +2)
        asyncio.run(_handle_rename_reply(self.bot, 1, "aaa", "The Office"))
        self.assertEqual(action["season"], 3)

    def test_a_season_typed_into_the_rename_still_wins(self):
        state.pending_actions["aaa"] = action = group_action("tv", "Office", "Office.S01E01.mkv")
        adjust_season(action, +2)
        asyncio.run(_handle_rename_reply(self.bot, 1, "aaa", "The Office S05"))
        self.assertEqual(action["season"], 5)


class BatchFinalizing(unittest.TestCase):
    def test_one_show_failing_does_not_drop_the_rest_of_the_burst(self):
        state.incoming_batches[1] = {"items": [
            {"message": None, "file_name": n, "parsed": parse_media_name(n), "target_path": "/x"}
            for n in ("Dune.2021.mkv", "Heat.1995.mkv")
        ], "timer": None}
        self.addCleanup(state.incoming_batches.clear)
        offer = mock.AsyncMock(side_effect=[RuntimeError("telegram said no"), None])
        anchor = mock.AsyncMock()
        with mock.patch.object(confirmation_flow, "BATCH_DEBOUNCE_SECONDS", 0), \
                mock.patch.object(confirmation_flow, "_offer_group", offer), \
                mock.patch.object(confirmation_flow.keyboards, "send_menu_anchor", anchor), \
                self.assertLogs(level="ERROR"):
            asyncio.run(confirmation_flow._finalize_batch(mock.MagicMock(), None, 1))
        self.assertEqual(offer.await_count, 2)
        anchor.assert_awaited_once()


class UsesTempLibrary(unittest.TestCase):
    """A MEDIA_ROOT in a temp dir, holding the given movies/ and tv/ folders."""

    movies = ()
    shows = ()

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        for base, names in (("movies", self.movies), ("tv", self.shows)):
            (root / base).mkdir()
            for name in names:
                (root / base / name).mkdir()
        patcher = mock.patch.object(confirmation_flow, "MEDIA_ROOT", str(root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(state.pending_actions.clear)


class ExistingFolderIsShown(UsesTempLibrary):
    shows = ("The Office",)

    def test_a_folder_already_in_the_library_is_not_offered_as_new(self):
        action = group_action("tv", "The Office", "The.Office.S03E01.mkv")
        label = _confirmation_buttons("abc123", action)[0][0].text
        self.assertIn("הקיימת", label)
        self.assertNotIn("צור", label)
        self.assertIn("כבר קיימת", confirmation_flow._confirmation_text(action))

    def test_a_new_folder_still_says_create(self):
        action = group_action("tv", "Severance", "Severance.S01E01.mkv")
        self.assertIn("צור", _confirmation_buttons("abc123", action)[0][0].text)
        self.assertNotIn("כבר קיימת", confirmation_flow._confirmation_text(action))


class FolderPicker(UsesTempLibrary):
    shows = [f"Show {n:02d}" for n in range(10)] + ["the office"]
    movies = ("Heat (1995)",)

    def picker_data(self, rows):
        return [b.data.decode() for row in rows for b in row]

    def test_the_rename_screen_lists_existing_folders_a_page_at_a_time(self):
        action = group_action("tv", "Offis", "Offis.S01E01.mkv")
        open_folder_picker(action)
        text, rows = rename_prompt("aaa", action)
        data = self.picker_data(rows)
        self.assertEqual(data[:2], ["pick:aaa:0", "pick:aaa:1"])
        self.assertIn("pickpage:aaa:1", data)
        self.assertIn("(עמוד 1/2)", text)
        _, last_page = rename_prompt("aaa", action, page=1)
        self.assertIn("pickpage:aaa:0", self.picker_data(last_page))

    def test_picking_a_folder_points_the_confirmation_at_it(self):
        action = group_action("tv", "Offis", "Offis.S01E01.mkv")
        open_folder_picker(action)
        index = action["folder_choices"].index("the office")
        self.assertEqual(pick_existing_folder(action, index), "the office")
        self.assertEqual((action["base"], action["folder"]), ("tv", "the office"))
        self.assertIn("הקיימת", _confirmation_buttons("aaa", action)[0][0].text)

    def test_a_folder_from_the_other_library_switches_movie_and_series(self):
        action = group_action("tv", "Heat", "Heat.S01E01.mkv")
        open_folder_picker(action, "movies")
        self.assertEqual(pick_existing_folder(action, 0), "Heat (1995)")
        self.assertEqual((action["base"], action["season"]), ("movies", None))

    def test_a_stale_button_picks_nothing(self):
        action = group_action("tv", "Offis", "Offis.S01E01.mkv")
        open_folder_picker(action)
        self.assertIsNone(pick_existing_folder(action, 99))
        (Path(confirmation_flow.MEDIA_ROOT) / "tv" / "Show 00").rmdir()
        self.assertIsNone(pick_existing_folder(action, 0))
        self.assertEqual(action["folder"], "Offis")

    def test_typing_an_existing_folder_in_another_case_reaches_it(self):
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        state.pending_actions["aaa"] = action = group_action("tv", "Offis", "Offis.S01E01.mkv")
        asyncio.run(_handle_rename_reply(bot, 1, "aaa", "The Office"))
        self.assertEqual(action["folder"], "the office")


if __name__ == "__main__":
    unittest.main()
