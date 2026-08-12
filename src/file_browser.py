#!/usr/bin/env python3
"""
Filesystem operations (list/move/rename/mkdir) confined to MEDIA_ROOT.
Provides safe path validation and name collision checks independent of Telethon.
"""

import os
import posixpath
import shutil

from media_organizer import clean_name, fix_permissions


class FileManagerError(Exception):
    """Raised for any validation failure. The message is safe to show directly to the user."""


def _norm_rel(rel_path):
    """Normalizes a relative path to posix form, rejecting anything that would escape the root."""
    rel_path = (rel_path or "").replace("\\", "/").strip("/")
    normalized = posixpath.normpath(rel_path) if rel_path else ""
    if normalized == ".":
        normalized = ""
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise FileManagerError("נתיב לא חוקי.")
    return normalized


def safe_join(root, rel_path):
    """Resolves rel_path under root - guaranteed not to escape it."""
    normalized = _norm_rel(rel_path)
    return os.path.join(root, *normalized.split("/")) if normalized else root


def list_entries(abs_dir):
    """Returns (dirs, files) directly under abs_dir, sorted case-insensitively for display."""
    if not os.path.isdir(abs_dir):
        return [], []
    dirs, files = [], []
    for name in os.listdir(abs_dir):
        (dirs if os.path.isdir(os.path.join(abs_dir, name)) else files).append(name)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    return dirs, files


def _validate_new_name(name):
    """Cleans a typed name, rejecting only one that cleans down to nothing.

    clean_name already strips path separators, so a name can never smuggle one through -
    "Movie: The/Sequel" becomes the single folder "Movie TheSequel", not two nested ones.
    """
    clean = clean_name(name)
    if not clean:
        raise FileManagerError("שם לא חוקי.")
    return clean


def _reject_collision(parent_abs, name):
    if os.path.exists(os.path.join(parent_abs, name)):
        raise FileManagerError(f'כבר קיים פריט בשם "{name}" במיקום הזה.')


def _reject_library_root(rel, action):
    """Refuses a destructive operation on the root itself or on a top-level folder in it.

    The top-level entries ARE the library: /media/movies, /media/tv and the qBittorrent
    staging dir. Only items INSIDE one of them are fair game. Without this, the same
    three taps that move or rename one episode would just as easily rename /media/tv out
    from under Jellyfin and STAGING_DIR at once - and unlike deleting a file, nothing in
    the UI marks that as the dangerous one. `action` is a Hebrew infinitive that reads
    correctly in both sentences below.
    """
    if not rel:
        raise FileManagerError(f"אי אפשר {action} תיקיית השורש.")
    if "/" not in rel:
        raise FileManagerError(f"אי אפשר {action} תיקיית ספרייה ראשית (כמו movies/tv) - רק פריטים בתוכה.")


def _require_within_root(root, abs_path):
    """Rejects a path that resolves outside root once symlinks are followed.

    os.path.exists/isdir follow symlinks, so a link under root pointing elsewhere (planted
    by a downloaded torrent, say) would otherwise let rmtree/rename/move act on a path well
    outside MEDIA_ROOT while rel_path itself looks perfectly ordinary.
    """
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(abs_path)
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        raise FileManagerError("נתיב לא חוקי (מצביע מחוץ לספריית המדיה).")
    return real_path


def make_dir(root, rel_dir, name):
    """Creates a new subfolder named `name` under rel_dir (relative to root)."""
    name = _validate_new_name(name)
    parent_abs = safe_join(root, rel_dir)
    if not os.path.isdir(parent_abs):
        raise FileManagerError("תיקיית היעד לא קיימת.")
    _reject_collision(parent_abs, name)
    new_abs = os.path.join(parent_abs, name)
    os.makedirs(new_abs)
    fix_permissions(new_abs)
    rel = _norm_rel(rel_dir)
    return f"{rel}/{name}" if rel else name


def rename_entry(root, rel_path, new_name):
    """Renames the file/folder at rel_path (relative to root), keeping it in the same parent dir."""
    new_name = _validate_new_name(new_name)
    rel = _norm_rel(rel_path)
    _reject_library_root(rel, "לשנות את השם של")
    src_abs = safe_join(root, rel_path)
    if not os.path.exists(src_abs):
        raise FileManagerError("הפריט כבר לא קיים.")
    _require_within_root(root, src_abs)
    parent_abs = os.path.dirname(src_abs)
    _reject_collision(parent_abs, new_name)
    dest_abs = os.path.join(parent_abs, new_name)
    os.rename(src_abs, dest_abs)
    fix_permissions(dest_abs)
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return f"{parent_rel}/{new_name}" if parent_rel else new_name


def delete_entry(root, rel_path):
    """
    Permanently deletes the file/folder at rel_path (relative to root).

    Only items INSIDE a top-level folder are deletable through this function - see
    _reject_library_root for why all three destructive operations share that rule.
    """
    rel = _norm_rel(rel_path)
    _reject_library_root(rel, "למחוק")
    abs_path = safe_join(root, rel_path)
    if not os.path.exists(abs_path):
        raise FileManagerError("הפריט כבר לא קיים.")
    _require_within_root(root, abs_path)
    if os.path.isdir(abs_path):
        shutil.rmtree(abs_path)
    else:
        os.remove(abs_path)


def move_entry(root, rel_src, rel_dest_dir):
    """Moves the file/folder at rel_src into the directory rel_dest_dir (both relative to root)."""
    _reject_library_root(_norm_rel(rel_src), "להעביר")
    src_abs = safe_join(root, rel_src)
    dest_dir_abs = safe_join(root, rel_dest_dir)
    if not os.path.exists(src_abs):
        raise FileManagerError("הפריט כבר לא קיים.")
    if not os.path.isdir(dest_dir_abs):
        raise FileManagerError("תיקיית היעד לא קיימת.")

    name = os.path.basename(src_abs)
    # Both ends are checked: a symlinked source would move something from outside the
    # library, and a symlinked destination would move something out of it.
    real_src = _require_within_root(root, src_abs)
    real_dest_dir = _require_within_root(root, dest_dir_abs)
    if real_dest_dir == real_src or real_dest_dir.startswith(real_src + os.sep):
        raise FileManagerError("אי אפשר להעביר תיקייה לתוך עצמה או לתוך תת-תיקייה שלה.")
    if os.path.dirname(real_src) == real_dest_dir:
        raise FileManagerError("הפריט כבר נמצא במיקום הזה.")

    _reject_collision(dest_dir_abs, name)
    dest_abs = os.path.join(dest_dir_abs, name)
    shutil.move(src_abs, dest_abs)
    fix_permissions(dest_abs)
    rel_dest = _norm_rel(rel_dest_dir)
    return f"{rel_dest}/{name}" if rel_dest else name
