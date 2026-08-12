"""qBittorrent client parsing and staging path helper tests.

Exercises response parsing for add_torrent_to_qbit and qbit_get_torrents against
mocked HTTP responses, and validates is_in_staging path normalization.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import unittest
from unittest.mock import MagicMock, patch

from qbit_client import is_in_staging, add_torrent_to_qbit, qbit_get_torrents


class StagingDirCheck(unittest.TestCase):
    @patch("qbit_client.STAGING_DIR", "/media/.incoming")
    def test_is_in_staging_normalizes_paths(self):
        self.assertTrue(is_in_staging("/media/.incoming"))
        self.assertTrue(is_in_staging("/media/.incoming/"))
        self.assertTrue(is_in_staging("/media/./.incoming"))
        self.assertFalse(is_in_staging("/media/movies"))
        self.assertFalse(is_in_staging(""))
        self.assertFalse(is_in_staging(None))


class ResponseParsing(unittest.TestCase):
    @patch("qbit_client.http_session.post")
    def test_add_torrent_rejects_fails_body(self, mock_post):
        res = MagicMock()
        res.status_code = 200
        res.text = "Fails.\n"
        mock_post.return_value = res

        import asyncio
        result = asyncio.run(add_torrent_to_qbit(urls="magnet:?xt=urn:btih:123"))
        self.assertFalse(result)

    @patch("qbit_client.http_session.post")
    def test_add_torrent_accepts_ok_body(self, mock_post):
        res = MagicMock()
        res.status_code = 200
        res.text = "Ok."
        mock_post.return_value = res

        import asyncio
        result = asyncio.run(add_torrent_to_qbit(urls="magnet:?xt=urn:btih:123"))
        self.assertTrue(result)

    @patch("qbit_client.http_session.get")
    def test_qbit_get_torrents_returns_none_on_error(self, mock_get):
        res = MagicMock()
        res.status_code = 403
        res.text = "Forbidden"
        mock_get.return_value = res

        import asyncio
        result = asyncio.run(qbit_get_torrents("all"))
        self.assertIsNone(result)

    @patch("qbit_client.http_session.get")
    def test_qbit_get_torrents_returns_list_on_200(self, mock_get):
        res = MagicMock()
        res.status_code = 200
        res.json.return_value = [{"name": "Torrent 1", "hash": "abc"}]
        mock_get.return_value = res

        import asyncio
        result = asyncio.run(qbit_get_torrents("all"))
        self.assertEqual(result, [{"name": "Torrent 1", "hash": "abc"}])


if __name__ == "__main__":
    unittest.main()
