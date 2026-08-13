#!/usr/bin/env python3
"""
Telegram-flavoured Markdown for Telethon.

Telethon's bundled parser is CommonMark, where bold is `**text**` and `*text*` is
*italic* - and since it emits no entity for that italic, a single-asterisk span comes
out of the parser as literal asterisks around unstyled text. Every screen in this bot
is written in Telegram's own flavour instead (`*bold*`, matching the Bot API's
"Markdown" mode), so all of it rendered as visible asterisks.

Rewriting ~50 strings to `**` would have fixed the symptom while leaving the next
`*bold*` anyone types just as broken, so the parser moves to meet the strings.

Supported, all non-nesting and all requiring a non-empty body:

    *bold*  _italic_  ~strike~  `code`  ```pre```

`code` and ```pre``` are literal spans: markers inside them are left alone, which is
what keeps a path like `/media/My *Movie*/` intact.
"""

import re

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntityStrike,
)

# Longest marker first: ``` has to win over ` on the same character.
_DELIMITERS = [
    ("```", MessageEntityPre),
    ("`", MessageEntityCode),
    ("*", MessageEntityBold),
    ("_", MessageEntityItalic),
    ("~", MessageEntityStrike),
]
_LITERAL = {MessageEntityPre, MessageEntityCode}

_PATTERN = re.compile(
    "|".join(f"(?P<g{i}>{re.escape(marker)})" for i, (marker, _) in enumerate(_DELIMITERS))
)


def parse(message):
    """Turns Telegram-flavoured markdown into (stripped_text, entities) for Telethon."""
    text = []
    entities = []
    # Offsets are counted in UTF-16 code units, not characters: that is what Telegram
    # measures, and the difference is not academic here - every screen in this bot opens
    # with an emoji, each of which is one character but two units. Counting characters
    # would slide every entity on the line left by one per emoji.
    offset = 0
    pos = 0

    while pos < len(message):
        match = _PATTERN.search(message, pos)
        if not match:
            break

        marker = match.group()
        entity_cls = dict(_DELIMITERS)[marker]
        body_start = match.end()
        end = message.find(marker, body_start)
        # An unmatched marker, or an empty `**`, is ordinary punctuation - a lone
        # asterisk in a release name must not swallow the rest of the message.
        if end == -1 or end == body_start:
            text.append(message[pos:body_start])
            offset += _utf16_len(message[pos:body_start])
            pos = body_start
            continue

        before = message[pos:match.start()]
        text.append(before)
        offset += _utf16_len(before)

        body = message[body_start:end]
        if entity_cls in _LITERAL:
            inner_entities = []
        else:
            body, inner_entities = parse(body)
        for entity in inner_entities:
            entity.offset += offset
            entities.append(entity)

        length = _utf16_len(body)
        if entity_cls is MessageEntityPre:
            entities.append(MessageEntityPre(offset=offset, length=length, language=""))
        else:
            entities.append(entity_cls(offset=offset, length=length))
        text.append(body)
        offset += length
        pos = end + len(marker)

    text.append(message[pos:])
    entities.sort(key=lambda e: e.offset)
    return "".join(text), entities


def unparse(text, entities):
    """Re-inserts markers around entities. Telethon calls this when reading messages back."""
    if not entities:
        return text

    reverse = {cls: marker for marker, cls in _DELIMITERS if cls is not MessageEntityPre}
    reverse[MessageEntityPre] = "```"
    # Insertions run back to front so an earlier entity's offsets stay valid.
    inserts = []
    for entity in entities:
        marker = reverse.get(type(entity))
        if marker:
            inserts.append((entity.offset, marker))
            inserts.append((entity.offset + entity.length, marker))

    result = text
    for index, marker in sorted(inserts, reverse=True):
        index = _char_index(result, index)
        result = result[:index] + marker + result[index:]
    return result


def _utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def _char_index(text, utf16_offset):
    """Converts a UTF-16 offset back to a Python string index."""
    if utf16_offset <= 0:
        return 0
    return len(text.encode("utf-16-le")[: utf16_offset * 2].decode("utf-16-le", "ignore"))
