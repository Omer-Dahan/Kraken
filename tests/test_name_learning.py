"""Channel tags, Hebrew episode wording, and what the bot learns when a name is corrected.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import learned_names
from confirmation_flow import (
    _handle_rename_reply,
    _new_group_action,
    accept_ignore_suggestion,
    learn_from_confirmation,
)
from learned_names import DEFAULT_IGNORE_WORDS, NameMemory, dropped_phrases
from media_organizer import find_exact_existing, parse_media_name, propose_folder, strip_release_noise
import bot_state as state


def fresh_memory():
    return NameMemory(Path(tempfile.mkdtemp()) / "learned_names.json")


class UsesFreshMemory(unittest.TestCase):
    def setUp(self):
        self.memory = fresh_memory()
        patcher = mock.patch.object(learned_names, "memory", self.memory)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReleaseNoise(unittest.TestCase):
    def strip(self, name):
        return strip_release_noise(name, DEFAULT_IGNORE_WORDS)

    def test_channel_tags_are_cut_wherever_they_sit(self):
        for name, expected in [
            ("לולו סרטים הבית של הדרקון S01E02.mkv", "הבית של הדרקון S01E02.mkv"),
            ("השימיה הסרט 2019 מתורגם.mkv", "הסרט 2019.mkv"),
            ("ז.מ.הסרט.2019.mkv", "הסרט.2019.mkv"),
            ("הסרט [לולוסרטים] 2021.mkv", "הסרט 2021.mkv"),
            ("מלולו סרטים: עוץ לי גוץ לי 2019.mkv", "עוץ לי גוץ לי 2019.mkv"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.strip(name), expected)

    def test_a_tag_only_matches_whole_words(self):
        """"ז.מ" may also be written "ז מ", but must not eat the start of "זמן"."""
        self.assertEqual(self.strip("זמן אמת 2020.mkv"), "זמן אמת 2020.mkv")
        self.assertEqual(self.strip("ז מ זמן אמת 2020.mkv"), "זמן אמת 2020.mkv")

    def test_links_handles_and_emoji_go_but_the_extension_stays(self):
        for name in [
            "הסרט 2020 @zira_media.mkv",
            "הסרט 2020 t.me/zira.mkv",
            "https://t.me/x/12 הסרט 2020.mkv",
            "🎬 הסרט 2020 🔥.mkv",
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.strip(name), "הסרט 2020.mkv")

    def test_hebrew_season_and_episode_become_markers_guessit_reads(self):
        for name, season, episode in [
            ("הבית של הדרקון עונה 2 פרק 5.mkv", 2, 5),
            ("הבית.של.הדרקון.עונה.2.פרק.5.mkv", 2, 5),
            ("הבית של הדרקון ע2פ5.mkv", 2, 5),
            ("הבית של הדרקון ע'2 פ'5.mkv", 2, 5),
            ("הבית של הדרקון בעונה 3.mkv", 3, None),
            ("הבית של הדרקון פרק 7.mkv", None, 7),
        ]:
            with self.subTest(name=name):
                parsed = parse_media_name(name)
                self.assertEqual(parsed["title"], "הבית של הדרקון")
                self.assertEqual((parsed["season"], parsed["episode"]), (season, episode))
                self.assertEqual(parsed["kind"], "episode")

    def test_a_leading_tag_no_longer_steals_the_title(self):
        """guessit alone made "זירה מדיה" the title and demoted the real one."""
        parsed = parse_media_name("זירה מדיה - הסרט שלי 2020.mp4")
        self.assertEqual((parsed["title"], parsed["year"]), ("הסרט שלי", 2020))

    def test_english_release_names_are_untouched(self):
        for name in ["Breaking.Bad.S01E03.1080p.mkv", "1917.1080p.BluRay.x264.mkv", "Se7en.1995.mkv"]:
            with self.subTest(name=name):
                self.assertEqual(self.strip(name), name)

    def test_a_name_that_was_nothing_but_noise_is_unknown(self):
        self.assertEqual(parse_media_name("לולו סרטים")["kind"], "unknown")


class IgnoreList(unittest.TestCase):
    def test_starts_from_the_defaults(self):
        self.assertEqual(fresh_memory().ignore_words(), tuple(DEFAULT_IGNORE_WORDS))

    def test_changes_survive_a_restart(self):
        memory = fresh_memory()
        self.assertTrue(memory.add_ignore_word("סרטי הלילה"))
        self.assertTrue(memory.remove_ignore_word("השימיה"))
        reloaded = NameMemory(memory.path)
        self.assertIn("סרטי הלילה", reloaded.ignore_words())
        self.assertNotIn("השימיה", reloaded.ignore_words())

    def test_separator_variants_count_as_the_same_phrase(self):
        memory = fresh_memory()
        self.assertFalse(memory.add_ignore_word("לולו.סרטים"))
        self.assertTrue(memory.remove_ignore_word("לולו_סרטים"))
        self.assertFalse(memory.remove_ignore_word("לולו סרטים"))

    def test_an_added_word_takes_effect_on_the_next_parse(self):
        memory = fresh_memory()
        with mock.patch.object(learned_names, "memory", memory):
            self.assertEqual(parse_media_name("סרטי הלילה הסרט 2020.mkv")["title"], "סרטי הלילה הסרט")
            memory.add_ignore_word("סרטי הלילה")
            self.assertEqual(parse_media_name("סרטי הלילה הסרט 2020.mkv")["title"], "הסרט")

    def test_a_broken_file_is_kept_aside_not_overwritten(self):
        path = Path(tempfile.mkdtemp()) / "learned_names.json"
        path.write_text("{not json", encoding="utf-8")
        memory = NameMemory(path)
        self.assertEqual(memory.ignore_words(), tuple(DEFAULT_IGNORE_WORDS))
        self.assertEqual(path.with_suffix(".json.bak").read_text(encoding="utf-8"), "{not json")


class DroppedPhrases(unittest.TestCase):
    def test_deleted_runs_are_offered(self):
        self.assertEqual(dropped_phrases("סרטי הלילה הבית של הדרקון", "הבית של הדרקון"), ["סרטי הלילה"])
        self.assertEqual(dropped_phrases("השימיה הסרט מתורגם", "הסרט"), ["השימיה", "מתורגם"])

    def test_words_cut_from_the_middle_are_an_edit_not_a_tag(self):
        self.assertEqual(dropped_phrases("Spider Man No Way Home", "Spider Man Way Home"), [])

    def test_articles_are_never_offered(self):
        """One tap on "The" would strip it from every title in the library."""
        self.assertEqual(dropped_phrases("The Matrix", "Matrix"), [])
        self.assertEqual(dropped_phrases("ז.מ הסרט", "הסרט"), ["ז מ"])

    def test_a_full_retype_offers_nothing(self):
        self.assertEqual(dropped_phrases("הבית של הדרקון", "House of the Dragon"), [])

    def test_years_and_known_phrases_are_not_offered(self):
        self.assertEqual(dropped_phrases("הסרט 2020", "הסרט"), [])
        self.assertEqual(dropped_phrases("השימיה הסרט", "הסרט", known=["השימיה"]), [])


class TitleAliases(UsesFreshMemory):
    def test_a_corrected_title_is_used_next_time(self):
        self.memory.remember_alias("הבית של הדרקון", "House of the Dragon", 2022)
        parsed = parse_media_name("הבית.של.הדרקון.S01E03.mkv")
        self.assertEqual((parsed["title"], parsed["year"]), ("House of the Dragon", 2022))
        self.assertEqual(parsed["detected_title"], "הבית של הדרקון")
        self.assertEqual(propose_folder(parsed), ("tv", "House of the Dragon (2022)"))

    def test_an_alias_without_a_year_keeps_the_names_own(self):
        self.memory.remember_alias("הסרט", "The Movie")
        self.assertEqual(parse_media_name("הסרט 2020.mkv")["year"], 2020)

    def test_confirming_what_was_detected_learns_nothing(self):
        self.assertFalse(self.memory.remember_alias("הסרט", "הסרט", 2020))
        self.assertFalse(self.memory.path.exists())


class LearningInTheConfirmationFlow(UsesFreshMemory):
    def setUp(self):
        super().setUp()
        self.bot = mock.MagicMock()
        self.bot.send_message = mock.AsyncMock()
        self.addCleanup(state.pending_actions.clear)

    def group(self, *file_names):
        items = [{"file_name": n, "parsed": parse_media_name(n), "message": None} for n in file_names]
        base, folder = propose_folder(items[0]["parsed"], preferred_base="tv")
        return _new_group_action(1, base, folder, items)

    def test_rename_then_confirm_teaches_both_the_tag_and_the_title(self):
        pid = self.group("סרטי הלילה הבית של הדרקון S01E01.mkv", "סרטי הלילה הבית של הדרקון S01E02.mkv")
        action = state.pending_actions[pid]
        self.assertEqual(action["folder"], "סרטי הלילה הבית של הדרקון")

        asyncio.run(_handle_rename_reply(self.bot, 1, pid, "הבית של הדרקון"))
        self.assertEqual(action["ignore_suggestions"], ["סרטי הלילה"])

        self.assertEqual(accept_ignore_suggestion(action, 0), "סרטי הלילה")
        self.assertIsNone(accept_ignore_suggestion(action, 0), "a second tap must not re-add it")
        self.assertIn("סרטי הלילה", self.memory.ignore_words())

        learn_from_confirmation(action, action["folder"])
        self.assertEqual(parse_media_name("סרטי הלילה הבית של הדרקון S01E09.mkv")["title"], "הבית של הדרקון")
        self.assertEqual(self.memory.alias_for("סרטי הלילה הבית של הדרקון"), ("הבית של הדרקון", None))

    def test_picking_an_existing_folder_is_learned_too(self):
        pid = self.group("Dragon.House.S01E01.mkv")
        learn_from_confirmation(state.pending_actions[pid], "House of the Dragon (2022)")
        stored = json.loads(self.memory.path.read_text(encoding="utf-8"))["aliases"]
        self.assertEqual(stored, {"dragon house": {"title": "House of the Dragon", "year": 2022}})


class AliasesDoNotMisfile(UsesFreshMemory):
    def test_an_alias_is_skipped_when_the_name_has_a_different_year(self):
        self.memory.remember_alias("Dune", "Dune Part Two", 2024)
        self.assertEqual(parse_media_name("Dune.1984.mkv")["title"], "Dune")
        self.assertEqual(parse_media_name("Dune.mkv")["title"], "Dune Part Two")

    def test_existing_folders_are_not_retitled_by_aliases(self):
        """A "House (2004)" folder must not read as whatever "House" was once corrected to."""
        self.memory.remember_alias("House", "House of the Dragon")
        library = Path(tempfile.mkdtemp())
        (library / "House (2004)").mkdir()
        self.assertIsNone(find_exact_existing(str(library), "House of the Dragon"))
        self.assertEqual(find_exact_existing(str(library), "House", 2004), "House (2004)")

    def test_a_typed_rename_is_taken_literally(self):
        self.memory.remember_alias("House", "House of the Dragon")
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        self.addCleanup(state.pending_actions.clear)
        items = [{"file_name": "X.S01E01.mkv", "parsed": parse_media_name("X.S01E01.mkv"), "message": None}]
        pid = _new_group_action(1, "tv", "X", items)
        asyncio.run(_handle_rename_reply(bot, 1, pid, "House"))
        self.assertEqual(state.pending_actions[pid]["folder"], "House")


if __name__ == "__main__":
    unittest.main()
