"""The Telegram-flavoured markdown parser the bot installs on its client.

Every screen's formatting depends on this, and the bug it exists to fix was invisible
in code review - the strings looked fine, the parser just dropped them - so the real
screen text is what gets asserted here, not synthetic samples.

Run from the Kraken directory:

    python -m unittest discover -s tests -t .
"""

import unittest

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntityStrike,
)

import telegram_markdown as md


def kinds(entities):
    return [type(e).__name__ for e in entities]


class Formatting(unittest.TestCase):
    def test_single_asterisks_are_bold(self):
        text, entities = md.parse("*bold*")
        self.assertEqual(text, "bold")
        self.assertEqual(kinds(entities), ["MessageEntityBold"])
        self.assertEqual((entities[0].offset, entities[0].length), (0, 4))

    def test_each_marker_maps_to_its_entity(self):
        for source, expected in [
            ("*x*", MessageEntityBold),
            ("_x_", MessageEntityItalic),
            ("~x~", MessageEntityStrike),
            ("`x`", MessageEntityCode),
            ("```x```", MessageEntityPre),
        ]:
            with self.subTest(source=source):
                text, entities = md.parse(source)
                self.assertEqual(text, "x")
                self.assertIsInstance(entities[0], expected)

    def test_a_fenced_block_wins_over_a_single_backtick(self):
        text, entities = md.parse("```x```")
        self.assertEqual(text, "x")
        self.assertEqual(kinds(entities), ["MessageEntityPre"])


class RealScreenText(unittest.TestCase):
    """The strings that shipped broken, asserted as the user sees them."""

    def test_the_main_menu_header_is_bold(self):
        text, entities = md.parse("👋 *בוט ההורדות של Jellyfin*")
        self.assertEqual(text, "👋 בוט ההורדות של Jellyfin")
        self.assertEqual(kinds(entities), ["MessageEntityBold"])

    def test_an_emoji_does_not_shift_the_entity(self):
        """Telegram counts UTF-16 units, so the 👋 ahead of the span is two, not one."""
        text, entities = md.parse("👋 *בוט*")
        self.assertEqual(entities[0].offset, 3)
        self.assertEqual(entities[0].length, 3)
        self.assertEqual(text[len("👋 "):], "בוט")

    def test_two_bold_spans_on_one_line_both_survive(self):
        text, entities = md.parse("📥 *5 קבצים* מ-*folder* שויכו")
        self.assertEqual(text, "📥 5 קבצים מ-folder שויכו")
        self.assertEqual(kinds(entities), ["MessageEntityBold", "MessageEntityBold"])

    def test_a_path_keeps_its_code_span(self):
        text, entities = md.parse("🎯 יעד נוכחי: 🎬 סרטים - `/media/movies`")
        self.assertEqual(kinds(entities), ["MessageEntityCode"])
        self.assertTrue(text.endswith("/media/movies"))

    def test_bold_and_code_coexist_on_a_progress_line(self):
        text, entities = md.parse("📁 *שם:* `Dune.mkv`")
        self.assertEqual(text, "📁 שם: Dune.mkv")
        self.assertEqual(kinds(entities), ["MessageEntityBold", "MessageEntityCode"])


class MarkersThatAreNotFormatting(unittest.TestCase):
    """Media names arrive from torrents and carry stray punctuation - it must stay literal."""

    def test_an_unmatched_marker_is_left_alone(self):
        text, entities = md.parse("2 * 3 = 6")
        self.assertEqual(text, "2 * 3 = 6")
        self.assertEqual(entities, [])

    def test_an_empty_span_is_not_an_entity(self):
        text, entities = md.parse("a ** b")
        self.assertEqual(text, "a ** b")
        self.assertEqual(entities, [])

    def test_markers_inside_a_code_span_stay_literal(self):
        text, entities = md.parse("`/media/My *Movie*/`")
        self.assertEqual(text, "/media/My *Movie*/")
        self.assertEqual(kinds(entities), ["MessageEntityCode"])

    def test_an_unmatched_marker_does_not_eat_the_rest_of_the_message(self):
        text, entities = md.parse("*Dune (2021) הורד בהצלחה")
        self.assertEqual(text, "*Dune (2021) הורד בהצלחה")
        self.assertEqual(entities, [])


class UnderscoresInFilenames(unittest.TestCase):
    """Release names are underscore-separated, and italics must not eat the separators.

    The first version of this parser paired those underscores up, consumed every one of
    them and ran the words together, so a delete confirmation showed a name that looked
    like it had never had spaces at all.
    """

    NAME = "לולו_סרטים_בלייד_ראנר_2049_2017_ת_מ_1080P.mkv"

    def test_a_filename_keeps_every_underscore(self):
        text, entities = md.parse(f"🗑 *{self.NAME}* נמחק לצמיתות.")
        self.assertIn(self.NAME, text)
        self.assertEqual(text.count("_"), self.NAME.count("_"))
        self.assertEqual(kinds(entities), ["MessageEntityBold"])

    def test_no_italic_is_invented_inside_a_name(self):
        _, entities = md.parse(f"🗑 *{self.NAME}* נמחק.")
        self.assertNotIn("MessageEntityItalic", kinds(entities))

    def test_an_ascii_release_name_is_untouched(self):
        text, entities = md.parse("The.Office_S03E01_1080p.mkv")
        self.assertEqual(text, "The.Office_S03E01_1080p.mkv")
        self.assertEqual(entities, [])

    def test_deliberate_italics_still_work(self):
        """The two real uses in the UI - both wrap a parenthesised phrase, not a word."""
        for source, expected in [
            ("_(תיקייה ריקה)_", "(תיקייה ריקה)"),
            ("_(אין טורנטים בקטגוריה הזו)_", "(אין טורנטים בקטגוריה הזו)"),
        ]:
            with self.subTest(source=source):
                text, entities = md.parse(source)
                self.assertEqual(text, expected)
                self.assertEqual(kinds(entities), ["MessageEntityItalic"])

    def test_italics_survive_a_filename_later_in_the_message(self):
        """The closing marker has to skip past the name's underscores to find its pair."""
        text, entities = md.parse(f"_(ריק)_ ואז {self.NAME}")
        self.assertEqual(kinds(entities), ["MessageEntityItalic"])
        self.assertTrue(text.endswith(self.NAME))

    def test_a_trailing_underscore_is_not_a_delimiter(self):
        text, entities = md.parse("file_name_")
        self.assertEqual(text, "file_name_")
        self.assertEqual(entities, [])


class RoundTrip(unittest.TestCase):
    """Telethon calls unparse when it reads a message back out of a Message object."""

    CASES = [
        "👋 *בוט ההורדות של Jellyfin*",
        "📁 *שם:* `Dune.mkv`",
        "📥 *5 קבצים* מ-*folder* שויכו",
        "plain text with no markers",
    ]

    def test_unparse_restores_the_original(self):
        for source in self.CASES:
            with self.subTest(source=source):
                text, entities = md.parse(source)
                self.assertEqual(md.unparse(text, entities), source)

    def test_unparse_without_entities_is_the_text_itself(self):
        self.assertEqual(md.unparse("plain", []), "plain")


if __name__ == "__main__":
    unittest.main()
