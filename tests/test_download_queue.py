"""Download-queue bookkeeping: the restart notice's queue count, and two queued files
that would land on the same path.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot_state as state
import download_engine


class PersistedQueueCount(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / ".queue_count"
        patcher = mock.patch.object(state, "QUEUE_COUNT_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(state.queued_downloads.clear)
        self.path = path

    def test_a_drained_queue_leaves_nothing_to_report(self):
        """The file used to outlive the queue, so every restart warned about 1 lost file."""
        state.queued_downloads["a"] = {}
        state.record_queue_count()
        self.assertTrue(self.path.exists())
        state.queued_downloads.clear()
        state.record_queue_count()
        self.assertFalse(self.path.exists())
        self.assertEqual(state.pop_persisted_queue_count(), 0)

    def test_the_count_is_reported_once(self):
        state.queued_downloads.update(a={}, b={})
        state.record_queue_count()
        self.assertEqual(state.pop_persisted_queue_count(), 2)
        self.assertEqual(state.pop_persisted_queue_count(), 0)


class SamePathDownloads(unittest.TestCase):
    def test_a_path_already_being_written_is_not_written_twice(self):
        target_dir = tempfile.mkdtemp()
        path = os.path.join(target_dir, "video.mp4")
        download_engine._paths_in_flight.add(path)
        self.addCleanup(download_engine._paths_in_flight.discard, path)

        with mock.patch.object(download_engine, "_download_to_path", mock.AsyncMock()) as download, \
                self.assertLogs(level="WARNING"):
            ok = asyncio.run(download_engine.start_video_download(
                mock.MagicMock(), None, None, 1, target_dir, "video.mp4", notify=False,
            ))
        self.assertFalse(ok)
        download.assert_not_awaited()

    def test_the_claim_is_released_even_when_the_download_fails(self):
        target_dir = tempfile.mkdtemp()
        failing = mock.AsyncMock(side_effect=RuntimeError("connection lost"))
        with mock.patch.object(download_engine, "_download_to_path", failing):
            with self.assertRaises(RuntimeError):
                asyncio.run(download_engine.start_video_download(
                    mock.MagicMock(), None, None, 1, target_dir, "video.mp4", notify=False,
                ))
        self.assertNotIn(os.path.join(target_dir, "video.mp4"), download_engine._paths_in_flight)


if __name__ == "__main__":
    unittest.main()
