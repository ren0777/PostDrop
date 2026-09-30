"""Caption building and tag parsing."""
from app.core.post import build_caption, parse_tags


def test_tags_appended_after_blank_line():
    assert build_caption("Hello", ["art", "ai"], 100) == "Hello\n\n#art #ai"


def test_tags_only_when_no_caption():
    assert build_caption("", ["art", "ai"], 100) == "#art #ai"
    assert build_caption("   ", ["art"], 100) == "#art"


def test_drops_last_tags_before_touching_the_caption():
    caption = "x" * 20
    tags = ["aaaaaaaaaa", "bbbbbbbbbb"]  # "#" + 10 chars each
    text = build_caption(caption, tags, 40)
    assert text == caption + "\n\n#aaaaaaaaaa"


def test_truncates_caption_only_when_it_is_too_long_alone():
    text = build_caption("y" * 50, ["art"], 10)
    assert text == "y" * 9 + "…"
    assert "#art" not in text


def test_drops_all_tags_when_none_fit():
    assert build_caption("", ["averyveryverylongtag"], 5) == ""


def test_parse_tags_handles_hashes_commas_and_spaces():
    assert parse_tags("#art, illustration  aiart") == ["art", "illustration", "aiart"]


def test_parse_tags_dedupes_and_ignores_empties():
    assert parse_tags(["#art", "art", "  ", ""]) == ["art"]
    assert parse_tags(None) == []
