"""qBittorrent client parsing and staging path helper tests.

Exercises response parsing for add_torrent_to_qbit and qbit_get_torrents against
mocked HTTP responses, and validates is_in_staging path normalization.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import asyncio
import logging
import unittest
from unittest.mock import MagicMock, patch

from qbit_client import is_in_staging, add_torrent_to_qbit, qbit_get_torrents


def _mock_session(**response_attrs):
    """Patches the client's session factory, returning the mock session it will hand out.

    The HTTP session is built per-thread by _get_http_session rather than living in a
    module-level `http_session`, because every call runs through asyncio.to_thread and a
    requests.Session is not thread-safe. Patching the factory is what keeps these tests
    pointed at the seam the code actually uses.
    """
    session = MagicMock()
    response = MagicMock(**response_attrs)
    session.post.return_value = response
    session.get.return_value = response
    return patch("qbit_client._get_http_session", return_value=session), session


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
    """qBittorrent signals failure in the body as often as in the status code."""

    def setUp(self):
        # The error paths below log at ERROR by design; muting it keeps a passing run
        # from printing tracebacks that look like failures.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_add_torrent_rejects_fails_body(self):
        """A rejected info-hash comes back as HTTP 200 with the body "Fails."."""
        patcher, _ = _mock_session(status_code=200, text="Fails.\n")
        with patcher:
            self.assertFalse(asyncio.run(add_torrent_to_qbit(urls="magnet:?xt=urn:btih:123")))

    def test_add_torrent_accepts_ok_body(self):
        patcher, _ = _mock_session(status_code=200, text="Ok.")
        with patcher:
            self.assertTrue(asyncio.run(add_torrent_to_qbit(urls="magnet:?xt=urn:btih:123")))

    def test_add_torrent_accepts_an_empty_body(self):
        """Some qBittorrent builds answer a successful add with no body at all."""
        patcher, _ = _mock_session(status_code=200, text="")
        with patcher:
            self.assertTrue(asyncio.run(add_torrent_to_qbit(urls="magnet:?xt=urn:btih:123")))

    def test_qbit_get_torrents_returns_none_on_error(self):
        """None means "could not reach qBittorrent" - callers must not read it as "no torrents"."""
        patcher, _ = _mock_session(status_code=403, text="Forbidden")
        with patcher:
            self.assertIsNone(asyncio.run(qbit_get_torrents("all")))

    def test_qbit_get_torrents_returns_list_on_200(self):
        patcher, session = _mock_session(status_code=200)
        session.get.return_value.json.return_value = [{"name": "Torrent 1", "hash": "abc"}]
        with patcher:
            self.assertEqual(asyncio.run(qbit_get_torrents("all")), [{"name": "Torrent 1", "hash": "abc"}])

    def test_an_empty_library_is_a_list_not_none(self):
        """The distinction None vs [] is what the torrents screen branches on."""
        patcher, session = _mock_session(status_code=200)
        session.get.return_value.json.return_value = []
        with patcher:
            self.assertEqual(asyncio.run(qbit_get_torrents("all")), [])

    def test_a_connection_failure_is_reported_as_none(self):
        """requests raising is the unreachable-host case, not a bad status code."""
        with patch("qbit_client._get_http_session", side_effect=OSError("connection refused")):
            self.assertIsNone(asyncio.run(qbit_get_torrents("all")))


if __name__ == "__main__":
    unittest.main()
