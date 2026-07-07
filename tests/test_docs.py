"""Tests for the pure Markdown-ish parsers in agent.tools.docs.

_parse_bold returns (clean_text, bold_ranges) where each range indexes into
clean_text; the tests assert the ranges actually bracket the bold substrings.
_parse_blocks turns a mini-markdown string into styled paragraph blocks.
"""

from agent.tools import docs


# --------------------------------------------------------------------------- #
# _parse_bold
# --------------------------------------------------------------------------- #

def _bold_substrings(text):
    clean, ranges = docs._parse_bold(text)
    return clean, [clean[s:e] for s, e in ranges]


def test_parse_bold_no_markers():
    assert docs._parse_bold("plain text") == ("plain text", [])


def test_parse_bold_single_range_brackets_the_word():
    clean, bolds = _bold_substrings("a **b** c")
    assert clean == "a b c"
    assert bolds == ["b"]


def test_parse_bold_multiple_ranges():
    clean, bolds = _bold_substrings("**x** y **z**")
    assert clean == "x y z"
    assert bolds == ["x", "z"]


# --------------------------------------------------------------------------- #
# _parse_blocks
# --------------------------------------------------------------------------- #

def _block(text):
    blocks = docs._parse_blocks(text)
    assert len(blocks) == 1
    return blocks[0]


def test_parse_blocks_heading_2():
    b = _block("## Section Title")
    assert b["style"] == "HEADING_2"
    assert b["text"] == "Section Title"
    assert b["bullet"] is False


def test_parse_blocks_heading_3():
    b = _block("### Subsection")
    assert b["style"] == "HEADING_3"
    assert b["text"] == "Subsection"


def test_parse_blocks_bullet():
    b = _block("- a point")
    assert b["style"] == "NORMAL_TEXT"
    assert b["text"] == "a point"
    assert b["bullet"] is True


def test_parse_blocks_plain_text():
    b = _block("just a line")
    assert b["style"] == "NORMAL_TEXT"
    assert b["bullet"] is False


def test_parse_blocks_blank_line_is_empty_normal():
    # A blank line *between* content becomes an empty NORMAL_TEXT spacer block.
    blocks = docs._parse_blocks("line one\n\nline three")
    assert len(blocks) == 3
    assert blocks[1]["text"] == "" and blocks[1]["bullet"] is False


def test_parse_blocks_empty_string_yields_nothing():
    assert docs._parse_blocks("") == []


def test_parse_blocks_horizontal_rule_is_dropped():
    assert docs._parse_blocks("---") == []


def test_parse_blocks_bold_range_in_heading():
    b = _block("## A **B** heading")
    s, e = b["bold_ranges"][0]
    assert b["text"][s:e] == "B"


def test_parse_blocks_continuation_line_appends_to_previous_bullet():
    blocks = docs._parse_blocks("- first line\n  continued here")
    assert len(blocks) == 1
    assert blocks[0]["text"] == "first line continued here"
    assert blocks[0]["bullet"] is True


def test_parse_blocks_continuation_offsets_bold_ranges():
    # The bold on the continuation line must be offset past the first line.
    blocks = docs._parse_blocks("- a\n  **b**")
    assert len(blocks) == 1
    block = blocks[0]
    assert block["text"] == "a b"
    s, e = block["bold_ranges"][0]
    assert block["text"][s:e] == "b"


def test_parse_blocks_mixed_document_order():
    doc = "## Head\n- one\n- two\nparagraph"
    blocks = docs._parse_blocks(doc)
    styles = [(b["style"], b["bullet"]) for b in blocks]
    assert styles == [
        ("HEADING_2", False),
        ("NORMAL_TEXT", True),
        ("NORMAL_TEXT", True),
        ("NORMAL_TEXT", False),
    ]
