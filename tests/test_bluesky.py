"""Bluesky rich-text facets (UTF-8 byte offsets)."""
from app.platforms.bluesky import build_facets


def _features(facets, kind):
    return [f for f in facets if f["features"][0]["$type"].endswith(kind)]


def test_tag_facet_uses_byte_offsets():
    text = "héllo ☕ #art rocks"
    tags = _features(build_facets(text), "#tag")
    assert len(tags) == 1
    start = text.index("#art")
    assert tags[0]["index"]["byteStart"] == len(text[:start].encode("utf-8"))
    assert tags[0]["index"]["byteEnd"] - tags[0]["index"]["byteStart"] == len("#art")
    assert tags[0]["features"][0]["tag"] == "art"


def test_link_facet():
    text = "see https://example.com/art for more"
    links = _features(build_facets(text), "#link")
    assert len(links) == 1
    assert links[0]["features"][0]["uri"] == "https://example.com/art"
    assert links[0]["index"]["byteStart"] == len("see ".encode("utf-8"))


def test_link_trailing_punctuation_is_trimmed():
    facet = build_facets("read https://example.com/a. ok")[0]
    assert facet["features"][0]["uri"] == "https://example.com/a"


def test_hash_inside_a_word_is_not_a_tag():
    assert build_facets("C#code is fine") == []
