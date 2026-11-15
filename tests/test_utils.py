"""Minimal unit tests for the most-regressed helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from silas.utils import clean_text, parse_volume_chapter, escape_xml, natural_sort_key


def test_clean_text_preserves_story_words():
    src = "The Advertisement for the VIP chapter said nothing useful."
    out = clean_text(src)
    assert "Advertisement" in out
    assert "VIP chapter" in out


def test_clean_text_drops_standalone_noise():
    src = "Once upon a time.\n\nAdvertisement\n\nThe end."
    out = clean_text(src)
    assert "Advertisement" not in out
    assert "Once upon a time" in out


def test_parse_part_sorts_before_next_chapter():
    a = parse_volume_chapter("https://example.com/chapter-12-part-10", "Chapter 12 Part 10")
    b = parse_volume_chapter("https://example.com/chapter-13", "Chapter 13")
    assert a < b  # (1, 12.0, 10) < (1, 13.0, 0)


def test_natural_sort_order():
    urls = [
        "https://x/chapter-13",
        "https://x/chapter-12-part-10",
        "https://x/chapter-12-part-2",
        "https://x/chapter-12",
    ]
    ordered = sorted(urls, key=lambda u: natural_sort_key(u))
    assert ordered == [
        "https://x/chapter-12",
        "https://x/chapter-12-part-2",
        "https://x/chapter-12-part-10",
        "https://x/chapter-13",
    ]


def test_escape_xml():
    assert "&lt;Skill Acquired: Fireball&gt;" == escape_xml("<Skill Acquired: Fireball>")
    assert "a &amp; b" == escape_xml("a & b")


if __name__ == "__main__":
    test_clean_text_preserves_story_words()
    test_clean_text_drops_standalone_noise()
    test_parse_part_sorts_before_next_chapter()
    test_natural_sort_order()
    test_escape_xml()
    print("all tests passed")
