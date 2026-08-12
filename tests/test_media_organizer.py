"""Filename -> folder decisions. Run from the Kraken directory:

    python -m unittest discover -s tests -t .

The cases here are real filenames that were getting filed wrong, not invented ones -
guessit reads plain numbers in a movie's name as episode numbers, which is what sent
movies into /media/tv.
"""

import os
import unittest

from media_organizer import (
    build_target_dir,
    clean_name,
    parse_media_name,
    propose_folder,
    sanitize_file_name,
    sanitize_folder_name,
)
from telegram_bot import is_library_media


class SeasonMarkerDetection(unittest.TestCase):
    def test_real_season_markers_are_detected(self):
        for name in [
            "Breaking.Bad.S01E03.1080p.mkv",
            "Show.Name.S03.COMPLETE.1080p.mkv",
            "The.Office.US.3x07.mkv",
            "Some Show Season 4 Episode 2.mkv",
            "הסדרה שלי עונה 2.mkv",
        ]:
            with self.subTest(name=name):
                self.assertTrue(parse_media_name(name)["series_marker"])

    def test_movie_noise_is_not_mistaken_for_a_season(self):
        # Resolutions, audio layouts, codec suffixes and titles that merely contain digits.
        for name in [
            "Some.Movie.2020.1920x1080.mkv",
            "Iron Man 2 1080p AAC 2.0.mkv",
            "Se7en.1995.mkv",
            "Some.Movie.2019.x264-e4.mkv",
            "Movie.Name.101.mkv",
            "S.W.A.T.2003.mkv",
        ]:
            with self.subTest(name=name):
                self.assertFalse(parse_media_name(name)["series_marker"])


class NumericTitles(unittest.TestCase):
    def test_all_number_title_is_kept_instead_of_being_read_as_a_year(self):
        parsed = parse_media_name("1917.1080p.BluRay.x264.mkv")
        self.assertEqual(parsed["title"], "1917")
        self.assertIsNone(parsed["year"])
        self.assertEqual(propose_folder(parsed, preferred_base="movies"), ("movies", "1917"))

    def test_a_real_year_after_a_title_is_still_a_year(self):
        parsed = parse_media_name("Interstellar.2014.2160p.mkv")
        self.assertEqual(parsed["title"], "Interstellar")
        self.assertEqual(parsed["year"], 2014)
        self.assertEqual(propose_folder(parsed, preferred_base="movies"), ("movies", "Interstellar (2014)"))


class BaseSelection(unittest.TestCase):
    """The bug this whole change exists for: movies landing in /media/tv."""

    def test_movie_with_digits_stays_in_movies_when_movies_is_selected(self):
        for name in ["Movie.Name.101.mkv", "Movie 2 of 3.mkv", "A.Movie.Name.Ep.2.mkv"]:
            with self.subTest(name=name):
                base, _ = propose_folder(parse_media_name(name), preferred_base="movies")
                self.assertEqual(base, "movies")

    def test_explicit_season_marker_beats_the_selected_destination(self):
        base, folder = propose_folder(parse_media_name("Breaking.Bad.S01E03.mkv"), preferred_base="movies")
        self.assertEqual((base, folder), ("tv", "Breaking Bad"))

    def test_movie_follows_the_user_into_tv_without_a_marker(self):
        base, _ = propose_folder(parse_media_name("Interstellar.2014.2160p.mkv"), preferred_base="tv")
        self.assertEqual(base, "tv")

    def test_without_a_preference_guessit_decides_on_its_own(self):
        self.assertEqual(propose_folder(parse_media_name("Movie.Name.101.mkv"))[0], "tv")
        self.assertEqual(propose_folder(parse_media_name("Interstellar.2014.mkv"))[0], "movies")

    def test_unparseable_names_propose_nothing(self):
        self.assertEqual(propose_folder(parse_media_name(""), preferred_base="movies"), (None, None))


class TargetLayout(unittest.TestCase):
    """The Jellyfin-readable layout: Movies/Title (Year) and TV/Title/Season NN.

    Expected paths are joined with os.path.join rather than hardcoded with "/", so these
    assert the layout itself and still pass when run on a dev machine that isn't the
    Linux VM the bot deploys to.
    """

    def test_series_without_a_known_season_still_gets_one(self):
        self.assertEqual(
            build_target_dir("/media", "tv", "Chernobyl"),
            os.path.join("/media", "tv", "Chernobyl", "Season 01"),
        )

    def test_season_is_zero_padded(self):
        self.assertEqual(
            build_target_dir("/media", "tv", "The Office", 7),
            os.path.join("/media", "tv", "The Office", "Season 07"),
        )

    def test_season_zero_is_specials_not_a_missing_value(self):
        self.assertEqual(
            build_target_dir("/media", "tv", "The Office", 0),
            os.path.join("/media", "tv", "The Office", "Season 00"),
        )

    def test_movies_never_get_a_season_folder(self):
        self.assertEqual(
            build_target_dir("/media", "movies", "Dune (2021)", 3),
            os.path.join("/media", "movies", "Dune (2021)"),
        )


class NameCleaning(unittest.TestCase):
    """clean_name is the shared cleaner; the two sanitize_* wrappers differ only in what
    they do when nothing survives, and that difference is load-bearing in both callers."""

    def test_clean_name_reports_nothing_left_instead_of_inventing_a_name(self):
        for name in ["", "   ", "...", "///", '<>:"|?*', None]:
            with self.subTest(name=name):
                self.assertEqual(clean_name(name), "")

    def test_folder_names_fall_back_but_unknown_stays_a_real_name(self):
        self.assertEqual(sanitize_folder_name(""), "Unknown")
        self.assertEqual(sanitize_folder_name("Unknown"), "Unknown")
        self.assertEqual(clean_name("Unknown"), "Unknown")

    def test_a_file_name_cannot_carry_a_path_out_of_its_target_directory(self):
        for hostile in [
            "../../etc/cron.d/payload.mkv",
            "/etc/passwd.mkv",
            "..\\..\\windows\\evil.mkv",
            "sub/dir/Show.S01E01.mkv",
        ]:
            with self.subTest(name=hostile):
                cleaned = sanitize_file_name(hostile)
                self.assertNotIn("/", cleaned)
                self.assertNotIn("\\", cleaned)
                # The only thing that matters: the cleaned name, when joined onto any
                # directory, cannot escape that directory. No separators means no escape.
                self.assertFalse(os.path.isabs(cleaned))

    def test_a_name_that_is_only_traversal_leaves_nothing(self):
        for hostile in ["..", ".", "../..", "/"]:
            with self.subTest(name=hostile):
                self.assertEqual(sanitize_file_name(hostile), "")

    def test_an_ordinary_release_name_survives_untouched(self):
        self.assertEqual(sanitize_file_name("The.Office.S03E01.1080p.mkv"), "The.Office.S03E01.1080p.mkv")


class IncomingMediaClassification(unittest.TestCase):
    """Which Telegram media is library material. Everything here except the videos used
    to be filed under /media as a fabricated .mp4."""

    def test_videos_are_accepted_by_mime_type_or_by_extension(self):
        self.assertTrue(is_library_media("clip.mp4", "video/mp4", has_document=True, is_sticker=False))
        self.assertTrue(is_library_media(
            "Show.S01E01.mkv", "application/octet-stream", has_document=True, is_sticker=False
        ))

    def test_a_large_document_with_no_extension_is_still_accepted(self):
        self.assertTrue(is_library_media("release", "application/octet-stream", has_document=True, is_sticker=False))

    def test_a_photo_is_not_library_media(self):
        # Photos carry no document at all, and no filename either.
        self.assertFalse(is_library_media("", "", has_document=False, is_sticker=False))

    def test_a_voice_note_is_not_library_media(self):
        self.assertFalse(is_library_media("", "audio/ogg", has_document=True, is_sticker=False))

    def test_a_stray_image_document_is_not_library_media(self):
        self.assertFalse(is_library_media("", "image/jpeg", has_document=True, is_sticker=False))

    def test_a_video_sticker_is_rejected_despite_its_video_mime_type(self):
        self.assertFalse(is_library_media("sticker.webm", "video/webm", has_document=True, is_sticker=True))


class FixPermissionsTolerance(unittest.TestCase):
    def test_fix_permissions_handles_permission_error_gracefully(self):
        from unittest.mock import patch
        from media_organizer import fix_permissions

        with patch("os.chmod", side_effect=PermissionError("Permission denied")):
            # Must not raise an exception
            fix_permissions("/media/tv/TestShow")


if __name__ == "__main__":
    unittest.main()
