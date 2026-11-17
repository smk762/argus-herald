import pytest

from argus_herald.textutil import clip_lines, extract_changelog, weighted_length


@pytest.mark.parametrize("limit", [5, 6, 10, 40])
def test_clip_lines_never_exceeds_the_limit(limit):
    clipped = clip_lines("x" * 200, limit)
    assert len(clipped) <= limit and clipped.endswith("\n...")
    assert len(clip_lines("one line\n" + "y" * 200, limit)) <= limit


def test_clip_lines_with_no_room_for_the_marker_is_empty():
    assert clip_lines("x" * 20, 4) == ""
    assert clip_lines("short", 10) == "short"


CHANGELOG = """# Changelog

## [1.20.0] - 2026-03-01
- twenty

## [1.2.1] - 2026-02-01
- patch

## 1.2-rc1
- candidate

## [1.2] - 2026-01-01
- the one

## [1.1](https://example.org/compare) - 2025-12-01
- linked
"""


def test_changelog_heading_matches_the_whole_version():
    assert extract_changelog(CHANGELOG, "1.2") == "- the one"
    assert extract_changelog(CHANGELOG, "1.2.1") == "- patch"
    assert extract_changelog(CHANGELOG, "1.20.0") == "- twenty"
    assert extract_changelog(CHANGELOG, "1.1") == "- linked"
    assert extract_changelog(CHANGELOG, "1.3") == ""
    assert extract_changelog("## 0.4.0 (2026-01-01)\n- bare\n", "0.4.0") == "- bare"
    assert extract_changelog("## 0.4.0\n- bare\n", "0.4") == ""


def test_weighted_length_counts_cjk_and_emoji_as_x_does():
    assert weighted_length("hello") == 5
    assert weighted_length("日本語のテキスト") == 16  # two each
    assert weighted_length("한국어") == 6
    assert weighted_length("👨‍👩‍👧‍👦") == 2  # one ZWJ family, seven code points
    assert weighted_length("👍🏽") == 2  # a skin tone is part of the emoji
    assert weighted_length("🇦🇺") == 2
    assert weighted_length("go 🚀 now") == 9
    assert weighted_length("café") == weighted_length("café") == 4  # NFC first
    assert weighted_length("“quoted” — it’s") == 15  # general punctuation is one each
    assert weighted_length("see https://example.org/a/very/long/path/that/goes/on 日本") == 4 + 23 + 1 + 4


def test_a_cjk_post_over_the_x_limit_is_refused():
    from argus_herald.platforms import TwitterPublisher

    with pytest.raises(ValueError):
        TwitterPublisher().validate("", "漢" * 200)
    TwitterPublisher().validate("", "漢" * 140)
