"""Filesystem operations under MEDIA_ROOT, exercised against a scratch directory.

file_browser.py is the only module here that deletes, renames and moves real files,
so it gets the closest coverage in the project: every guard it has exists because
the alternative is a destroyed media library, not a wrong string.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import file_browser as fb


def _symlinks_work(inside):
    """Windows needs Developer Mode or admin rights for os.symlink; Linux never does."""
    probe = os.path.join(inside, "__symlink_probe")
    try:
        os.symlink(inside, probe)
    except (OSError, NotImplementedError, AttributeError):
        return False
    os.remove(probe)
    return True


class LibraryTestCase(unittest.TestCase):
    """A scratch MEDIA_ROOT laid out like the real one: library folders at the top level."""

    def setUp(self):
        # The scratch root is deliberately nested one level inside a private parent, not
        # a bare mkdtemp: rename_entry's collision check looks at os.path.dirname(root),
        # so with the root sitting directly in the system temp dir, debris from a previous
        # failing run could satisfy that check and make a guard look present when it isn't.
        # A private parent also contains anything a still-unguarded operation drags out.
        parent = tempfile.mkdtemp(prefix="kraken-fb-")
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        self.root = os.path.join(parent, "media")
        os.makedirs(self.root)
        os.makedirs(self.abs("movies", "Dune (2021)"))
        os.makedirs(self.abs("tv", "The Office", "Season 03"))
        self.write("movies", "Dune (2021)", "Dune.mkv")
        self.write("tv", "The Office", "Season 03", "S03E01.mkv")

    def abs(self, *parts):
        return os.path.join(self.root, *parts)

    def write(self, *parts):
        path = self.abs(*parts)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        return path


class PathEscape(LibraryTestCase):
    """_norm_rel is the only thing between a callback string and the rest of the disk."""

    ESCAPES = ["..", "../etc", "movies/../../etc", "movies/../..", "\\..\\etc"]

    def test_delete_rejects_a_path_that_leaves_the_root(self):
        for rel in self.ESCAPES:
            with self.subTest(rel=rel), self.assertRaises(fb.FileManagerError):
                fb.delete_entry(self.root, rel)

    def test_rename_rejects_a_path_that_leaves_the_root(self):
        for rel in self.ESCAPES:
            with self.subTest(rel=rel), self.assertRaises(fb.FileManagerError):
                fb.rename_entry(self.root, rel, "whatever")

    def test_move_rejects_a_source_that_leaves_the_root(self):
        for rel in self.ESCAPES:
            with self.subTest(rel=rel), self.assertRaises(fb.FileManagerError):
                fb.move_entry(self.root, rel, "movies")

    def test_move_rejects_a_destination_that_leaves_the_root(self):
        for rel in self.ESCAPES:
            with self.subTest(rel=rel), self.assertRaises(fb.FileManagerError):
                fb.move_entry(self.root, "movies/Dune (2021)", rel)

    def test_make_dir_rejects_a_parent_that_leaves_the_root(self):
        for rel in self.ESCAPES:
            with self.subTest(rel=rel), self.assertRaises(fb.FileManagerError):
                fb.make_dir(self.root, rel, "whatever")

    def test_a_leading_slash_is_read_as_relative_to_the_root_not_the_disk(self):
        # Not an error - "/movies" is what the UI means by the movies folder, and
        # normalizing it is safer than rejecting a string a user could reasonably type.
        self.assertEqual(fb.safe_join(self.root, "/movies"), self.abs("movies"))
        self.assertEqual(fb.safe_join(self.root, "/etc/passwd"), self.abs("etc", "passwd"))


class LibraryRootProtection(LibraryTestCase):
    """The top-level folders ARE the library (movies, tv, and the staging dir).

    delete_entry already refuses to touch them, because the same three taps that
    remove one leaf file would otherwise remove a whole library. Rename and move are
    just as irreversible from the user's point of view - a renamed /media/tv breaks
    Jellyfin and the qBittorrent staging path at once - so they refuse it too.
    """

    def test_delete_refuses_the_root_itself(self):
        with self.assertRaises(fb.FileManagerError):
            fb.delete_entry(self.root, "")
        self.assertTrue(os.path.isdir(self.root))

    def test_delete_refuses_a_top_level_library_folder(self):
        with self.assertRaises(fb.FileManagerError):
            fb.delete_entry(self.root, "movies")
        self.assertTrue(os.path.isdir(self.abs("movies")))

    def test_rename_refuses_the_root_itself(self):
        with self.assertRaises(fb.FileManagerError):
            fb.rename_entry(self.root, "", "hijacked")
        self.assertTrue(os.path.isdir(self.root))

    def test_rename_refuses_a_top_level_library_folder(self):
        with self.assertRaises(fb.FileManagerError):
            fb.rename_entry(self.root, "movies", "films")
        self.assertTrue(os.path.isdir(self.abs("movies")))

    def test_move_refuses_a_top_level_library_folder(self):
        with self.assertRaises(fb.FileManagerError):
            fb.move_entry(self.root, "tv", "movies")
        self.assertTrue(os.path.isdir(self.abs("tv")))


class OperationsInsideALibraryFolder(LibraryTestCase):
    """The other half of LibraryRootProtection: everything one level down still works."""

    def test_a_file_inside_a_show_can_be_deleted(self):
        fb.delete_entry(self.root, "tv/The Office/Season 03/S03E01.mkv")
        self.assertFalse(os.path.exists(self.abs("tv", "The Office", "Season 03", "S03E01.mkv")))

    def test_a_show_folder_can_be_deleted_with_everything_in_it(self):
        fb.delete_entry(self.root, "tv/The Office")
        self.assertFalse(os.path.exists(self.abs("tv", "The Office")))
        self.assertTrue(os.path.isdir(self.abs("tv")))

    def test_a_show_folder_can_be_renamed(self):
        fb.rename_entry(self.root, "tv/The Office", "The Office (US)")
        self.assertTrue(os.path.isdir(self.abs("tv", "The Office (US)")))
        self.assertFalse(os.path.exists(self.abs("tv", "The Office")))

    def test_a_show_folder_can_be_moved_between_libraries(self):
        fb.move_entry(self.root, "tv/The Office", "movies")
        self.assertTrue(os.path.isdir(self.abs("movies", "The Office")))
        self.assertFalse(os.path.exists(self.abs("tv", "The Office")))

    def test_a_new_folder_can_be_created(self):
        fb.make_dir(self.root, "movies", "Blade Runner (1982)")
        self.assertTrue(os.path.isdir(self.abs("movies", "Blade Runner (1982)")))


class Collisions(LibraryTestCase):
    """Every destructive operation refuses to land on a name that is already taken,
    rather than silently replacing whatever was there."""

    def test_make_dir_refuses_an_existing_name(self):
        with self.assertRaises(fb.FileManagerError):
            fb.make_dir(self.root, "movies", "Dune (2021)")

    def test_rename_refuses_an_existing_sibling(self):
        os.makedirs(self.abs("tv", "Other Show"))
        with self.assertRaises(fb.FileManagerError):
            fb.rename_entry(self.root, "tv/The Office", "Other Show")
        self.assertTrue(os.path.isdir(self.abs("tv", "The Office")))

    def test_move_refuses_a_name_already_taken_in_the_destination(self):
        os.makedirs(self.abs("movies", "The Office"))
        with self.assertRaises(fb.FileManagerError):
            fb.move_entry(self.root, "tv/The Office", "movies")
        self.assertTrue(os.path.isdir(self.abs("tv", "The Office")))

    def test_a_folder_cannot_be_moved_into_its_own_subfolder(self):
        with self.assertRaises(fb.FileManagerError):
            fb.move_entry(self.root, "tv/The Office", "tv/The Office/Season 03")
        self.assertTrue(os.path.isdir(self.abs("tv", "The Office", "Season 03")))

    def test_a_folder_cannot_be_moved_where_it_already_is(self):
        with self.assertRaises(fb.FileManagerError):
            fb.move_entry(self.root, "tv/The Office", "tv")

    def test_moving_to_a_destination_that_does_not_exist_is_refused(self):
        with self.assertRaises(fb.FileManagerError):
            fb.move_entry(self.root, "tv/The Office", "nope")


class NewNameValidation(LibraryTestCase):
    def test_a_name_that_cleans_down_to_nothing_is_rejected(self):
        for name in ["", "   ", "...", "///", '<>:"|?*']:
            with self.subTest(name=name), self.assertRaises(fb.FileManagerError):
                fb.make_dir(self.root, "movies", name)

    def test_unknown_is_a_legitimate_folder_name(self):
        # "Unknown" is the sanitizer's fallback for an empty name, not a reserved word -
        # a user typing it means a folder called Unknown, and used to get "invalid name".
        fb.make_dir(self.root, "movies", "Unknown")
        self.assertTrue(os.path.isdir(self.abs("movies", "Unknown")))

    def test_path_characters_are_stripped_instead_of_being_smuggled_through(self):
        fb.make_dir(self.root, "movies", "Movie: The/Sequel")
        self.assertTrue(os.path.isdir(self.abs("movies", "Movie TheSequel")))
        self.assertFalse(os.path.exists(self.abs("movies", "Movie", "Sequel")))

    def test_renaming_to_a_name_with_path_characters_stays_in_the_same_parent(self):
        fb.rename_entry(self.root, "tv/The Office", "../escaped")
        self.assertTrue(os.path.isdir(self.abs("tv", "escaped")))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "escaped")))


class MissingEntries(LibraryTestCase):
    def test_renaming_something_that_is_gone_is_reported_not_crashed(self):
        with self.assertRaises(fb.FileManagerError):
            fb.rename_entry(self.root, "tv/Ghost Show", "x")

    def test_moving_something_that_is_gone_is_reported_not_crashed(self):
        with self.assertRaises(fb.FileManagerError):
            fb.move_entry(self.root, "tv/Ghost Show", "movies")

    def test_deleting_something_that_is_gone_is_reported_not_crashed(self):
        with self.assertRaises(fb.FileManagerError):
            fb.delete_entry(self.root, "tv/Ghost Show")


@unittest.skipUnless(_symlinks_work(tempfile.gettempdir()), "symlinks unavailable on this host")
class SymlinkEscape(LibraryTestCase):
    """os.path.exists/isdir follow symlinks, so a link planted under the root (by a
    downloaded torrent, say) would otherwise let rmtree act well outside MEDIA_ROOT."""

    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(prefix="kraken-outside-")
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        with open(os.path.join(self.outside, "important.txt"), "w", encoding="utf-8") as handle:
            handle.write("do not delete")

    def test_delete_refuses_a_symlink_pointing_outside_the_root(self):
        os.symlink(self.outside, self.abs("movies", "escape"), target_is_directory=True)
        with self.assertRaises(fb.FileManagerError):
            fb.delete_entry(self.root, "movies/escape")
        self.assertTrue(os.path.exists(os.path.join(self.outside, "important.txt")))


class ReturnedRelativePaths(LibraryTestCase):
    """The browser navigates by the strings these return, so a wrong one silently
    points the NEXT operation at a different folder than the one on screen."""

    def test_make_dir_returns_the_path_relative_to_the_root(self):
        self.assertEqual(fb.make_dir(self.root, "movies", "Arrival (2016)"), "movies/Arrival (2016)")
        self.assertEqual(fb.make_dir(self.root, "", "anime"), "anime")

    def test_rename_returns_the_new_path_in_the_same_parent(self):
        self.assertEqual(fb.rename_entry(self.root, "tv/The Office", "The Office (US)"), "tv/The Office (US)")

    def test_move_returns_the_path_in_the_destination(self):
        self.assertEqual(fb.move_entry(self.root, "tv/The Office", "movies"), "movies/The Office")


class Listing(LibraryTestCase):
    def test_directories_and_files_are_separated_and_sorted_case_insensitively(self):
        self.write("movies", "zebra.mkv")
        self.write("movies", "Apple.mkv")
        os.makedirs(self.abs("movies", "zzz Collection"))
        os.makedirs(self.abs("movies", "Alpha Collection"))
        dirs, files = fb.list_entries(self.abs("movies"))
        self.assertEqual(dirs, ["Alpha Collection", "Dune (2021)", "zzz Collection"])
        self.assertEqual(files, ["Apple.mkv", "zebra.mkv"])

    def test_a_missing_directory_lists_as_empty_rather_than_raising(self):
        self.assertEqual(fb.list_entries(self.abs("nope")), ([], []))


class ReadOnlyDeletion(LibraryTestCase):
    """Deleting media a torrent client left read-only, which is the common real-world case.

    Both halves of delete_entry get their own retry path (rmtree's handler, os.remove's
    except clause), so both are exercised here rather than trusting one to cover the other.
    """

    def test_a_read_only_file_is_still_deleted(self):
        path = self.write("movies", "Dune (2021)", "readonly.mkv")
        os.chmod(path, 0o444)
        fb.delete_entry(self.root, "movies/Dune (2021)/readonly.mkv")
        self.assertFalse(os.path.exists(path))

    def test_a_folder_of_read_only_files_is_still_deleted(self):
        folder = self.abs("tv", "The Office", "Season 03")
        os.chmod(self.write("tv", "The Office", "Season 03", "S03E02.mkv"), 0o444)
        os.chmod(self.abs("tv", "The Office", "Season 03", "S03E01.mkv"), 0o444)
        os.chmod(folder, 0o555)
        fb.delete_entry(self.root, "tv/The Office/Season 03")
        self.assertFalse(os.path.exists(folder))

    def test_a_denial_that_chmod_cannot_lift_still_reaches_the_user(self):
        """The retry is a recovery attempt, not a way to swallow a genuine failure."""
        with patch("file_browser._grant_write"), \
             patch("os.remove", side_effect=PermissionError("Permission denied")):
            with self.assertRaises(fb.FileManagerError) as ctx:
                fb.delete_entry(self.root, "movies/Dune (2021)/Dune.mkv")
        self.assertIn("Permission Denied", str(ctx.exception))


class PermissionErrorHandling(LibraryTestCase):
    """Verifies that filesystem permission errors are caught and converted to user-friendly FileManagerErrors."""

    @patch("shutil.move", side_effect=PermissionError("Permission denied"))
    def test_move_entry_permission_denied(self, mock_move):
        with self.assertRaises(fb.FileManagerError) as ctx:
            fb.move_entry(self.root, "movies/Dune (2021)", "tv")
        self.assertIn("Permission Denied", str(ctx.exception))

    @patch("shutil.rmtree", side_effect=PermissionError("Permission denied"))
    def test_delete_entry_permission_denied(self, mock_rmtree):
        with self.assertRaises(fb.FileManagerError) as ctx:
            fb.delete_entry(self.root, "movies/Dune (2021)")
        self.assertIn("Permission Denied", str(ctx.exception))

    @patch("os.rename", side_effect=PermissionError("Permission denied"))
    def test_rename_entry_permission_denied(self, mock_rename):
        with self.assertRaises(fb.FileManagerError) as ctx:
            fb.rename_entry(self.root, "movies/Dune (2021)", "Dune (2021) 4K")
        self.assertIn("Permission Denied", str(ctx.exception))

    @patch("os.makedirs", side_effect=PermissionError("Permission denied"))
    def test_make_dir_permission_denied(self, mock_makedirs):
        with self.assertRaises(fb.FileManagerError) as ctx:
            fb.make_dir(self.root, "movies", "New Folder")
        self.assertIn("Permission Denied", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
