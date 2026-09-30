"""Caption AI: prompt building, JSON parsing, model fallback."""
import asyncio
import json

import pytest
from PIL import Image

from app.core import captioner
from app.core.captioner import CaptionError, build_prompt, normalize, parse_response
from app.platforms import REGISTRY


def _image(tmp_path):
    path = tmp_path / "art.png"
    Image.new("RGB", (800, 600), (30, 90, 160)).save(path, "PNG")
    return path


# ------------------------------------------------------------------- prompt
def test_prompt_contains_selected_site_rules_only():
    prompt = build_prompt(character="Raiden Shogun", series="Genshin Impact",
                          notes="", mature=False, platforms=["bluesky", "telegram"])
    assert "Raiden Shogun" in prompt
    assert "Genshin Impact" in prompt
    assert captioner.SITE_RULES["bluesky"]["caption"] in prompt
    assert captioner.SITE_RULES["telegram"]["caption"] in prompt
    assert "discord" not in prompt


def test_prompt_mentions_maturity_when_asked():
    assert "suggestive or mature" in build_prompt(character="X", mature=True, platforms=["bluesky"])
    assert "safe for work" in build_prompt(character="X", mature=False, platforms=["bluesky"])


def test_site_rule_limits_match_the_connectors():
    for platform_id, rule in captioner.SITE_RULES.items():
        connector = REGISTRY.get(platform_id)
        if connector is not None:
            assert rule["limit"] == connector.caption_limit


def test_site_rules_cover_the_later_sites():
    for platform_id in ("instagram", "tumblr", "deviantart", "pixiv"):
        assert platform_id in captioner.SITE_RULES
    prompt = build_prompt(character="Stelle",
                          platforms=["instagram", "tumblr", "deviantart"])
    assert "10-15 hashtags" in prompt
    assert "Tumblr's own tag field" in prompt
    assert "letters, numbers and underscore" in prompt
    assert "keep the caption under" in prompt


def test_pixiv_rules_reach_the_prompt():
    prompt = build_prompt(character="星", platforms=["pixiv", "bluesky"])
    assert "in Japanese" in prompt
    assert "at most 10 tags" in prompt
    assert "official Japanese title" in prompt
    assert "at most 32 characters" in prompt  # the strictest title limit wins
    assert "at most 50 characters" not in prompt


def test_pixiv_prompt_asks_for_certain_names_and_no_ai_tags():
    prompt = build_prompt(character="Stelle", platforms=["pixiv"])
    assert "only if you are certain" in prompt or "when you are certain" in prompt
    assert "exactly as the artist typed it" in prompt
    assert "Never add an AI-related tag here" in prompt
    assert '"Stelle" is added as a tag automatically' in prompt


def test_pixiv_prompt_requires_the_tag_order():
    prompt = build_prompt(character="nino", platforms=["pixiv"])
    assert "(1) the series' official Japanese title" in prompt
    assert "(2) the character's official Japanese full name" in prompt
    assert "(3) then descriptive tags" in prompt
    assert "otherwise the name exactly as the artist typed it" in prompt
    assert "nino → 二乃" in prompt  # a short romanized name gets its Japanese name


def test_pixiv_drops_ai_tags_but_other_sites_keep_them():
    data = {"platforms": {
        "pixiv": {"caption": "c",
                  "tags": ["星", "aiart", "AIアート", "AI生成", "AI-Art", "aiartwork", "AI"]},
        "bluesky": {"caption": "c", "tags": ["Stelle", "aiart"]},
    }}
    result = normalize(data, ["pixiv", "bluesky"])
    assert result["platforms"]["pixiv"]["tags"] == ["星"]
    assert result["platforms"]["bluesky"]["tags"] == ["Stelle", "aiart"]


def test_pixiv_keeps_tags_that_merely_contain_ai():
    data = {"platforms": {"pixiv": {"caption": "c",
                                    "tags": ["崩壊：スターレイル", "hair", "Train"]}}}
    result = normalize(data, ["pixiv"])
    assert result["platforms"]["pixiv"]["tags"] == ["崩壊：スターレイル", "hair", "Train"]


def test_the_character_is_tagged_exactly_as_typed():
    data = {"platforms": {"pixiv": {"caption": "c", "tags": ["崩壊：スターレイル"]}}}
    result = normalize(data, ["pixiv"], character="Stelle")
    assert result["platforms"]["pixiv"]["tags"] == ["Stelle", "崩壊：スターレイル"]

    again = normalize({"platforms": {"pixiv": {"caption": "c", "tags": ["stelle", "星"]}}},
                      ["pixiv"], character="Stelle")
    assert again["platforms"]["pixiv"]["tags"] == ["stelle", "星"]  # never duplicated


def test_a_spaced_character_name_becomes_one_tag():
    data = {"platforms": {"pixiv": {"caption": "c", "tags": ["五等分の花嫁"]}}}
    result = normalize(data, ["pixiv"], character="quintessential quintuplets")
    tags = result["platforms"]["pixiv"]["tags"]
    assert tags[0] == "quintessentialquintuplets"
    assert all(" " not in tag for tag in tags)


def test_the_character_insert_never_pushes_past_ten_tags():
    data = {"platforms": {"pixiv": {"caption": "c",
                                    "tags": [f"tag{index}" for index in range(10)]}}}
    tags = normalize(data, ["pixiv"], character="Stelle")["platforms"]["pixiv"]["tags"]
    assert len(tags) == 10
    assert tags[0] == "Stelle" and "tag9" not in tags  # the last one made room


def test_other_sites_do_not_gain_the_character_tag():
    data = {"platforms": {"bluesky": {"caption": "c", "tags": ["art"]}}}
    result = normalize(data, ["bluesky"], character="Stelle")
    assert result["platforms"]["bluesky"]["tags"] == ["art"]


def test_generate_keeps_the_artists_character_and_drops_ai_tags(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"alt_text": "a", "platforms": {
            "pixiv": {"caption": "c", "tags": ["クインテス", "aiart", "AIアート"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], character="Quintus",
        platforms=["pixiv"]))
    assert result["platforms"]["pixiv"]["tags"] == ["Quintus", "クインテス"]


# ------------------------------------------------------------ lazy mode (M6)
def test_the_lazy_prompt_asks_for_identification():
    prompt = captioner.build_lazy_prompt(platforms=["bluesky", "pixiv"])
    assert "recognise the character and the series from the image" in prompt
    assert '"character"' in prompt and '"confident"' in prompt
    assert "Use exactly these site keys: bluesky, pixiv" in prompt
    assert "Do not guess" in prompt


def test_generate_lazy_identifies_and_captions(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({
            "character": "Nino Nakano", "series": "The Quintessential Quintuplets",
            "confident": "true", "alt_text": "a test",
            "platforms": {"bluesky": {"caption": "c", "tags": ["aiart", "Nino"]},
                          "pixiv": {"caption": "jp", "tags": ["aiart", "中野二乃"]}},
        })

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)],
        platforms=["bluesky", "pixiv"], mature=True))

    assert result["character"] == "Nino Nakano"
    assert result["series"] == "The Quintessential Quintuplets"
    assert result["confident"] is True
    assert result["alt_text"] == "a test"
    # Pixiv gets no AI tag and keeps the artist's spelling of the character
    assert result["platforms"]["pixiv"]["tags"][0] == "NinoNakano"  # one tag, no space
    assert "aiart" not in result["platforms"]["pixiv"]["tags"]
    assert "aiart" in result["platforms"]["bluesky"]["tags"]  # other sites keep it


def test_generate_lazy_is_not_confident_without_a_character(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"character": "", "series": "", "confident": True,
                           "platforms": {"bluesky": {"caption": "c", "tags": []}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], platforms=["bluesky"]))
    assert result["confident"] is False  # nothing to show the user but "Needs you"


def test_generate_lazy_needs_a_key_and_sites(tmp_path):
    with pytest.raises(CaptionError):
        asyncio.run(captioner.generate_lazy(cfg={}, images=[_image(tmp_path)],
                                            platforms=["bluesky"]))
    with pytest.raises(CaptionError):
        asyncio.run(captioner.generate_lazy(cfg={"api_key": "k"},
                                            images=[_image(tmp_path)], platforms=[]))


def test_normalize_keeps_a_per_site_title():
    result = normalize(
        {"platforms": {"pixiv": {"caption": "c", "tags": [], "title": "星の夜"}}},
        ["pixiv"])
    assert result["platforms"]["pixiv"]["title"] == "星の夜"

    without = normalize({"platforms": {"pixiv": {"caption": "c", "tags": []}}}, ["pixiv"])
    assert "title" not in without["platforms"]["pixiv"]


# ------------------------------------------------------------------ parsing
def test_parse_plain_json():
    assert parse_response('{"alt_text": "a"}')["alt_text"] == "a"


def test_parse_fenced_json_with_junk():
    raw = 'Sure! Here you go:\n```json\n{"alt_text": "b", "platforms": {}}\n```\nHope it helps.'
    assert parse_response(raw)["alt_text"] == "b"


def test_parse_skips_a_broken_object_before_the_real_one():
    raw = 'Here {not json} and the real one: {"alt_text": "c"}'
    assert parse_response(raw)["alt_text"] == "c"


def test_parse_rejects_junk():
    with pytest.raises(CaptionError):
        parse_response("there is no json here")


def test_normalize_cleans_tags_and_fills_missing_sites():
    data = {"alt_text": " a ", "platforms": {
        "bluesky": {"caption": " hi ", "tags": ["#art", "aiart"]},
        "discord": {"caption": "x", "tags": "#one #two"},
    }}
    result = normalize(data, ["bluesky", "telegram"])
    assert result["alt_text"] == "a"
    assert result["platforms"]["bluesky"] == {"caption": "hi", "tags": ["art", "aiart"]}
    assert result["platforms"]["telegram"] == {"caption": "", "tags": []}


def test_normalize_strips_a_duplicated_tag_line():
    # Real output from mimo-v2.6-flash: the caption already ends with the tags.
    caption = (
        "Stelle at the night festival, all lanterns and fireworks 🎆\n\n"
        "#Stelle #HonkaiStarRail #aiart #yukata #festival\n\n"
        "#Stelle #HonkaiStarRail #aiart #yukata #festival"
    )
    entry = normalize({"platforms": {"bluesky": {"caption": caption, "tags": []}}},
                      ["bluesky"])["platforms"]["bluesky"]
    assert entry["caption"] == "Stelle at the night festival, all lanterns and fireworks 🎆"
    assert entry["tags"] == ["Stelle", "HonkaiStarRail", "aiart", "yukata", "festival"]


def test_normalize_strips_inline_hashtags_at_the_end():
    # Real output from glm-5.3-flash: hashtags inline at the end of a sentence.
    entry = normalize({
        "platforms": {"telegram": {
            "caption": "Lanterns everywhere! #Stelle #HonkaiStarRail #aiart",
            "tags": ["aiart"],
        }},
    }, ["telegram"])["platforms"]["telegram"]
    assert entry["caption"] == "Lanterns everywhere!"
    assert entry["tags"] == ["aiart", "Stelle", "HonkaiStarRail"]


def test_prompt_without_an_image_does_not_ask_about_the_artwork():
    prompt = build_prompt(character="Stelle", series="Honkai: Star Rail",
                          platforms=["bluesky"], has_image=False)
    assert "No image is attached" in prompt
    assert "do not describe poses, clothing or scenery" in prompt
    assert "empty string" in prompt
    assert "Look at the image" not in prompt


# ----------------------------------------------------------------- generate
def test_missing_key_raises():
    with pytest.raises(CaptionError):
        asyncio.run(captioner.generate(cfg={}, images=[], character="X", platforms=["bluesky"]))


def test_primary_model_result(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        assert model == captioner.DEFAULT_VISION_MODEL
        return '{"alt_text": "desc", "platforms": {"telegram": {"caption": "cap", "tags": ["a", "b"]}}}'

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], character="Ch", series="S",
        notes="", mature=False, platforms=["telegram"],
    ))
    assert result["used_model"] == captioner.DEFAULT_VISION_MODEL
    assert result["fallback"] is False
    assert result["platforms"]["telegram"]["caption"] == "cap"
    assert result["alt_text"] == "desc"


def test_fallback_goes_backup_then_name_only(monkeypatch, tmp_path):
    calls = []

    async def fake_call(cfg, model, messages, timeout):
        has_image = any(part.get("type") == "image_url" for part in messages[0]["content"])
        calls.append((model, has_image))
        if has_image:
            raise CaptionError("blocked")
        return json.dumps({"alt_text": "a",
                           "platforms": {"bluesky": {"caption": "c", "tags": ["#t"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate(
        cfg={"api_key": "k", "vision_model": "vm", "backup_model": "bm"},
        images=[_image(tmp_path)], character="Ch", platforms=["bluesky"],
    ))
    assert calls == [("vm", True), ("bm", True), ("bm", False)]
    assert result["fallback"] is True
    assert result["used_model"] == "bm"
    assert result["platforms"]["bluesky"]["tags"] == ["t"]


def test_no_image_goes_straight_to_name_only(monkeypatch):
    calls = []

    async def fake_call(cfg, model, messages, timeout):
        calls.append(any(part.get("type") == "image_url" for part in messages[0]["content"]))
        return '{"alt_text": "", "platforms": {"discord": {"caption": "c", "tags": []}}}'

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate(
        cfg={"api_key": "k"}, images=[], character="Ch", platforms=["discord"],
    ))
    assert calls == [False]
    assert result["fallback"] is True
    assert result["platforms"]["discord"]["caption"] == "c"


def test_all_attempts_failing_raises(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        raise CaptionError("nope")

    monkeypatch.setattr(captioner, "_call", fake_call)
    with pytest.raises(CaptionError):
        asyncio.run(captioner.generate(
            cfg={"api_key": "k"}, images=[_image(tmp_path)], character="Ch", platforms=["bluesky"],
        ))


# ------------------------------------------------------------ M2 additions
def test_prompt_carries_the_series_rule_and_no_ai_wording():
    prompt = build_prompt(character="Stelle", platforms=["bluesky"])
    assert '"series"' in prompt
    assert "must never mention AI" in prompt
    assert "mature, tasteful, suggestive or explicit" in prompt


def test_prompt_drops_the_ai_tag_rule_when_disabled():
    with_tag = build_prompt(character="X", platforms=["bluesky"], add_ai_tag=True)
    without = build_prompt(character="X", platforms=["bluesky"], add_ai_tag=False)
    assert "aiart" in with_tag
    assert "Include an AI-art tag" not in without
    assert "Do not add any AI-related tags" in without


def test_normalize_keeps_the_detected_series():
    result = normalize({"series": " Honkai: Star Rail ", "alt_text": "", "platforms": {}},
                       ["bluesky"])
    assert result["series"] == "Honkai: Star Rail"


def test_prompt_asks_for_a_short_title():
    prompt = build_prompt(character="Stelle", platforms=["bluesky"])
    assert '"title"' in prompt
    assert "at most 50 characters" in prompt


def test_normalize_keeps_a_title_cut_at_a_word_boundary():
    long = "Stelle enjoys a summer festival night in a floral kimono"
    result = normalize({"title": long, "platforms": {}}, ["bluesky"])
    assert result["title"] == "Stelle enjoys a summer festival night in a floral"


def test_generate_passes_add_ai_tag_from_config(monkeypatch):
    prompts = []

    async def fake_call(cfg, model, messages, timeout):
        prompts.append(messages[0]["content"][0]["text"])
        return ('{"series": "S", "alt_text": "", '
                '"platforms": {"bluesky": {"caption": "c", "tags": []}}}')

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate(
        cfg={"api_key": "k", "add_ai_tag": False}, images=[], character="Ch",
        platforms=["bluesky"],
    ))
    assert "Include an AI-art tag" not in prompts[0]
    assert result["series"] == "S"


def test_name_only_attempt_gets_the_no_image_prompt(monkeypatch, tmp_path):
    prompts = []

    async def fake_call(cfg, model, messages, timeout):
        has_image = any(part.get("type") == "image_url" for part in messages[0]["content"])
        prompts.append((has_image, messages[0]["content"][0]["text"]))
        if has_image:
            raise CaptionError("blocked")
        return '{"alt_text": "", "platforms": {"bluesky": {"caption": "c", "tags": []}}}'

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], character="Stelle", platforms=["bluesky"],
    ))
    assert result["fallback"] is True
    assert result["alt_text"] == ""  # the UI then keeps the artist's own alt text
    assert [with_image for with_image, _ in prompts] == [True, True, False]
    assert "Look at the image" in prompts[0][1]
    assert "No image is attached" in prompts[2][1]


# --------------------------------------------------------------- HTTP errors
class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_http_error_messages_have_hints_and_details():
    assert "API key rejected" in captioner.http_error_message("m", _FakeResponse(401))

    retired = captioner.http_error_message(
        "kimi-k2.6", _FakeResponse(410, {"error": {"message": "model retired"}}))
    assert "HTTP 410" in retired
    assert "retired or renamed" in retired
    assert "model retired" in retired

    limited = captioner.http_error_message("m", _FakeResponse(429, text="slow down please"))
    assert "rate limit reached" in limited
    assert "slow down please" in limited

    missing = captioner.http_error_message("m", _FakeResponse(404))
    assert "not available" in missing


# ------------------------------------------------------- mixed posts (M6.1)
def test_tag_names_become_one_tag():
    assert captioner.tag_name("Honkai: Star Rail") == "HonkaiStarRail"
    assert captioner.tag_name(" Nino Nakano ") == "NinoNakano"
    assert captioner.tag_name("中野二乃") == "中野二乃"
    assert captioner.tag_name("") == ""


def test_ensure_tags_keeps_every_character_and_drops_generics_first():
    tags = [f"generic{index}" for index in range(12)]

    kept = captioner.ensure_tags(tags, ["Stelle", "Honkai: Star Rail", "Nami"], "pixiv")

    assert kept[:3] == ["Stelle", "HonkaiStarRail", "Nami"]
    assert len(kept) == captioner.TAG_LIMITS["pixiv"] == 10  # the cap is respected
    assert captioner.ensure_tags(["stelle"], ["Stelle"]) == ["stelle"]  # never doubled
    roomy = captioner.ensure_tags(["art"], ["Stelle"], "")
    assert roomy == ["Stelle", "art"]  # no limit: nothing is thrown away


def test_the_tag_limits_match_the_connectors():
    for site, limit in captioner.TAG_LIMITS.items():
        connector = REGISTRY.get(site)
        if connector is not None and connector.max_tags is not None:
            assert limit == connector.max_tags, site


def test_the_mixed_prompt_asks_for_a_universal_caption():
    prompt = captioner.build_mixed_prompt(
        characters=["Stelle", "Nami"], series=["Honkai: Star Rail", "One Piece"],
        platforms=["bluesky", "pixiv"])

    assert "universal" in prompt and "engagement-style" in prompt
    assert "Which one is your fav" in prompt          # the tone the user asked for
    assert "Vary the wording" in prompt
    assert "Stelle, Nami" in prompt
    assert "Honkai: Star Rail, One Piece" in prompt
    assert "every character and every game/series" in prompt
    assert "drop generic tags first" in prompt
    assert "推しはどの子" in prompt                    # the universal Pixiv title example
    assert "300" in prompt                            # Bluesky's limit still applies
    assert "in Japanese" in prompt                    # Pixiv's own rules still apply


def test_the_mixed_prompt_polishes_an_artists_draft():
    prompt = captioner.build_mixed_prompt(
        characters=["Stelle"], platforms=["bluesky"],
        caption_draft="Good morning ☀️ which is your favourite?")
    assert "Good morning ☀️ which is your favourite?" in prompt
    assert "keep its meaning" in prompt and "call to action" in prompt
    assert "Fix grammar and spelling" in prompt
    assert "at most one or two fitting emoji" in prompt
    assert "Adapt it per site" in prompt


def test_the_mixed_prompt_keeps_an_exact_caption_verbatim():
    prompt = captioner.build_mixed_prompt(
        characters=["Stelle"], platforms=["bluesky"],
        caption_draft="Good morning ☀️", caption_exact=True)
    assert "Good morning ☀️" in prompt
    assert "word for word" in prompt and "write only the tags" in prompt


def test_generate_mixed_tags_every_character(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        # the model forgot Nami and the series, and slipped an AI tag in
        return json.dumps({
            "title": "Which one?", "alt_text": "Stelle here. Nami there.",
            "platforms": {"pixiv": {"caption": "どの子が好き？", "title": "推しはどの子？",
                                    "tags": ["aiart", "Stelle"]},
                          "bluesky": {"caption": "Hello!", "tags": ["aiart"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_mixed(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], characters=["Stelle", "Nami"],
        series=["Honkai: Star Rail", "One Piece"], platforms=["bluesky", "pixiv"]))

    assert result["mixed"] is True and result["confident"] is True
    assert result["character"] == "Stelle, Nami"
    pixiv = result["platforms"]["pixiv"]["tags"]
    assert {"Stelle", "Nami", "HonkaiStarRail", "OnePiece"} <= set(pixiv)
    assert "aiart" not in pixiv                       # Pixiv drops AI tags
    assert len(pixiv) <= captioner.TAG_LIMITS["pixiv"]
    assert result["platforms"]["pixiv"]["title"] == "推しはどの子？"
    assert result["platforms"]["bluesky"]["caption"] == "Hello!"
    assert "aiart" in result["platforms"]["bluesky"]["tags"]  # other sites keep it


def test_generate_mixed_polishes_the_draft(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"alt_text": "a", "platforms": {
            "bluesky": {"caption": "POLISHED ✨", "tags": ["art"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_mixed(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], characters=["Stelle"],
        platforms=["bluesky"], caption_draft="MODEL DRAFT"))

    assert result["platforms"]["bluesky"]["caption"] == "POLISHED ✨"  # per site
    assert "art" in result["platforms"]["bluesky"]["tags"]


def test_generate_mixed_keeps_an_exact_caption(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"alt_text": "a", "platforms": {
            "bluesky": {"caption": "POLISHED ✨", "tags": ["art"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_mixed(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], characters=["Stelle"],
        platforms=["bluesky"], caption_draft="Good morning ☀️", caption_exact=True))

    assert result["platforms"]["bluesky"]["caption"] == "Good morning ☀️"  # untouched
    assert "art" in result["platforms"]["bluesky"]["tags"]


def test_mixed_needs_a_key_characters_and_sites(tmp_path):
    with pytest.raises(CaptionError):
        asyncio.run(captioner.generate_mixed(cfg={}, images=[_image(tmp_path)],
                                             characters=["Stelle"], platforms=["bluesky"]))
    with pytest.raises(CaptionError):
        asyncio.run(captioner.generate_mixed(cfg={"api_key": "k"}, images=[_image(tmp_path)],
                                             characters=[], platforms=["bluesky"]))


def test_lazy_can_be_told_to_keep_the_artists_caption(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"character": "Stelle", "series": "Honkai: Star Rail",
                           "confident": True, "platforms": {
                               "bluesky": {"caption": "MODEL", "tags": ["art"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    polished = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], platforms=["bluesky"],
        caption_draft="Good morning ☀️"))
    assert polished["platforms"]["bluesky"]["caption"] == "MODEL"  # the model's rewrite

    exact = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], platforms=["bluesky"],
        caption_draft="Good morning ☀️", caption_exact=True))
    assert exact["platforms"]["bluesky"]["caption"] == "Good morning ☀️"  # untouched


# ------------------------------------------- AI sizes, timeouts, retries (M6.1b)
def test_the_small_image_path_is_used_for_identify(monkeypatch, tmp_path):
    seen = {}

    def fake_prepare(path, **kwargs):
        seen.update(kwargs)
        return __import__("app.core.images", fromlist=["Prepared"]).Prepared(
            data=b"jpeg", mime="image/jpeg", width=8, height=8, filename="a.jpg")

    monkeypatch.setattr(captioner, "prepare", fake_prepare)

    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"characters": ["Stelle"], "series": "S", "confident": True})

    monkeypatch.setattr(captioner, "_call", fake_call)
    asyncio.run(captioner.identify(cfg={"api_key": "k"}, image=_image(tmp_path)))

    assert seen["max_side"] == captioner.SMALL_IMAGE_SIDE == 512
    assert seen["max_bytes"] == captioner.SMALL_IMAGE_BYTES == 300_000


def test_identify_has_its_own_timeout_and_caption_calls_get_more(monkeypatch, tmp_path):
    timeouts = []

    async def fake_call(cfg, model, messages, timeout):
        timeouts.append(timeout)
        prompt = messages[0]["content"][0]["text"]
        if "You name the characters" in prompt:
            return json.dumps({"characters": ["Stelle"], "series": "S", "confident": True})
        return json.dumps({"alt_text": "a", "platforms": {"bluesky": {"caption": "c", "tags": []}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    asyncio.run(captioner.identify(cfg={"api_key": "k"}, image=_image(tmp_path)))
    asyncio.run(captioner.generate_lazy(cfg={"api_key": "k"}, images=[_image(tmp_path)],
                                        platforms=["bluesky"]))
    asyncio.run(captioner.generate(cfg={"api_key": "k"}, images=[_image(tmp_path)],
                                   character="Stelle", platforms=["bluesky"]))

    assert timeouts == [captioner.IDENTIFY_TIMEOUT, captioner.VISION_TIMEOUT,
                        captioner.VISION_TIMEOUT]
    assert captioner.IDENTIFY_TIMEOUT == 60.0 and captioner.VISION_TIMEOUT == 120.0


def test_mixed_attaches_at_most_four_small_images(monkeypatch, tmp_path):
    messages_seen = []
    prepared = []

    def fake_prepare(path, **kwargs):
        prepared.append(kwargs)
        return __import__("app.core.images", fromlist=["Prepared"]).Prepared(
            data=b"jpeg", mime="image/jpeg", width=8, height=8, filename="a.jpg")

    async def fake_call(cfg, model, messages, timeout):
        messages_seen.append(messages)
        return json.dumps({"alt_text": "a", "platforms": {"bluesky": {"caption": "c", "tags": []}}})

    monkeypatch.setattr(captioner, "prepare", fake_prepare)
    monkeypatch.setattr(captioner, "_call", fake_call)
    paths = [tmp_path / f"{index}.png" for index in range(22)]

    asyncio.run(captioner.generate_mixed(cfg={"api_key": "k"}, images=paths,
                                         characters=["Stelle", "Nami"], platforms=["bluesky"]))

    parts = [part for part in messages_seen[0][0]["content"] if part["type"] == "image_url"]
    assert len(parts) == captioner.MIXED_IMAGE_LIMIT == 4
    assert all(seen["max_side"] == 512 and seen["max_bytes"] == 300_000 for seen in prepared)
    assert prepared[0]["max_side"] == 512


def test_a_spread_covers_the_first_and_last_image():
    picked = captioner._spread([f"image-{index}" for index in range(22)], 4)
    assert picked[0] == "image-0" and picked[-1] == "image-21"
    assert len({*picked}) == 4
    assert captioner._spread(["only"], 4) == ["only"]


def test_transient_failures_are_marked_retryable(monkeypatch, tmp_path):
    import httpx

    class Response:
        status_code = 503
        text = "busy"

        def json(self):
            return {"message": "busy"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(captioner.httpx, "AsyncClient", Client)
    with pytest.raises(captioner.CaptionUnavailable):
        asyncio.run(captioner._call({}, "m", [], 5))

    class Boom(Client):
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("no route")

    monkeypatch.setattr(captioner.httpx, "AsyncClient", Boom)
    with pytest.raises(captioner.CaptionUnavailable):
        asyncio.run(captioner._call({}, "m", [], 5))


def test_permanent_failures_are_not_retryable(monkeypatch):
    class Response:
        status_code = 401
        text = "bad key"

        def json(self):
            return {"message": "bad key"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(captioner.httpx, "AsyncClient", Client)
    with pytest.raises(captioner.CaptionError) as info:
        asyncio.run(captioner._call({}, "m", [], 5))
    assert not isinstance(info.value, captioner.CaptionUnavailable)


def test_every_transient_failure_makes_the_whole_call_retryable(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        raise captioner.CaptionUnavailable(f"{model}: timed out")

    monkeypatch.setattr(captioner, "_call", fake_call)
    with pytest.raises(captioner.CaptionUnavailable):
        asyncio.run(captioner.identify(cfg={"api_key": "k"}, image=_image(tmp_path)))

    async def mixed_call(cfg, model, messages, timeout):
        raise captioner.CaptionError("the model refused")

    monkeypatch.setattr(captioner, "_call", mixed_call)
    with pytest.raises(captioner.CaptionError) as info:
        asyncio.run(captioner.identify(cfg={"api_key": "k"}, image=_image(tmp_path)))
    assert not isinstance(info.value, captioner.CaptionUnavailable)


def test_failures_log_the_model_attempt_and_seconds(monkeypatch, tmp_path, caplog):
    async def fake_call(cfg, model, messages, timeout):
        raise captioner.CaptionUnavailable(f"{model}: timed out after {timeout:.0f}s")

    monkeypatch.setattr(captioner, "_call", fake_call)
    with caplog.at_level("INFO", logger="artdrop.caption"):
        with pytest.raises(captioner.CaptionUnavailable):
            asyncio.run(captioner.identify(cfg={"api_key": "k"}, image=_image(tmp_path)))

    assert "attempt 1" in caplog.text and "attempt 2" in caplog.text
    assert captioner.DEFAULT_VISION_MODEL in caplog.text
    assert "failed after" in caplog.text


# ------------------------------------------------ the split caption call (blocker)
def test_the_caption_call_is_split_into_two_halves(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    async def fake_call(cfg, model, messages, timeout):
        prompt = messages[0]["content"][0]["text"]
        sites = [site for site in ("bluesky", "telegram", "tumblr", "pixiv")
                 if f"- {site}:" in prompt]
        seen.append(sites)
        return json.dumps({"character": "Stelle", "series": "Honkai: Star Rail",
                           "confident": True, "alt_text": "a",
                           "platforms": {site: {"caption": f"{site} cap", "tags": []}
                                         for site in sites}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)],
        platforms=["bluesky", "telegram", "tumblr", "pixiv"]))

    assert len(seen) == 2  # two halves, not one seven-site call
    assert any(half == ["pixiv"] for half in seen)
    assert any(sorted(half) == ["bluesky", "telegram", "tumblr"] for half in seen)
    assert set(result["platforms"]) == {"bluesky", "telegram", "tumblr", "pixiv"}
    assert result["platforms"]["pixiv"]["caption"] == "pixiv cap"


def test_one_slow_half_does_not_fail_the_other(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        prompt = messages[0]["content"][0]["text"]
        if "- pixiv:" in prompt:
            raise captioner.CaptionUnavailable("the Japanese half timed out")
        return json.dumps({"alt_text": "a", "platforms": {
            "bluesky": {"caption": "bs cap", "tags": []}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], platforms=["bluesky", "pixiv"]))

    assert result["platforms"]["bluesky"]["caption"] == "bs cap"
    assert "pixiv" not in result["platforms"]  # that half failed; its sites are skipped


def test_both_halves_failing_is_still_retryable(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        raise captioner.CaptionUnavailable("down")

    monkeypatch.setattr(captioner, "_call", fake_call)
    with pytest.raises(captioner.CaptionUnavailable):
        asyncio.run(captioner.generate_lazy(cfg={"api_key": "k"},
                                            images=[_image(tmp_path)],
                                            platforms=["bluesky", "pixiv"]))


# ------------------------------------------------- the folder name (change of plan)
def test_a_given_name_is_used_verbatim(monkeypatch, tmp_path):
    captured = {}

    async def fake_call(cfg, model, messages, timeout):
        prompt = messages[0]["content"][0]["text"]
        captured["prompt"] = prompt
        return json.dumps({"series": "Wuthering Waves", "alt_text": "a",
                           "platforms": {"bluesky": {"caption": "c", "tags": ["art"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], platforms=["bluesky"],
        given={"name": "Changli", "series": "", "japanese": ""}))

    assert '"Changli"' in captured["prompt"]
    assert "folder name" in captured["prompt"]
    assert "Changli" in result["platforms"]["bluesky"]["tags"]
    assert result["series"] == "Wuthering Waves"  # the model may fill the series


def test_a_given_japanese_name_tags_pixiv(monkeypatch, tmp_path):
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"alt_text": "a", "platforms": {
            "pixiv": {"caption": "c", "tags": ["aiart"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_lazy(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], platforms=["pixiv"],
        given={"name": "Changli", "series": "Wuthering Waves", "japanese": "長離"}))

    tags = result["platforms"]["pixiv"]["tags"]
    assert "長離" in tags and "WutheringWaves" in tags and "aiart" not in tags


def test_a_universal_caption_needs_no_names(monkeypatch, tmp_path):
    captured = {}

    async def fake_call(cfg, model, messages, timeout):
        captured["prompt"] = messages[0]["content"][0]["text"]
        return json.dumps({"alt_text": "a", "platforms": {
            "bluesky": {"caption": "Good morning ✨", "tags": ["animeart", "fanart"]}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_mixed(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], characters=[], series=["One Piece"],
        platforms=["bluesky"]))

    assert "without naming any of them" in captured["prompt"]
    assert "generic art tags" in captured["prompt"]
    assert {"animeart", "fanart", "OnePiece"} == set(result["platforms"]["bluesky"]["tags"])


def test_an_unsure_chain_is_not_an_error(monkeypatch, tmp_path):
    """Both models answering "not confident" is an answer, not a failure."""
    calls = []

    async def fake_call(cfg, model, messages, timeout):
        calls.append(model)
        return json.dumps({"characters": [], "series": "", "confident": False})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.identify(cfg={"api_key": "k"}, image=_image(tmp_path)))

    assert result["confident"] is False
    assert calls == [captioner.DEFAULT_VISION_MODEL, captioner.DEFAULT_BACKUP_MODEL]
    assert "error" not in result  # never an AI error: the worker sorts it into a set


def test_a_given_series_is_a_fact_and_other_names_are_banned():
    block = captioner.given_block({"name": "Citlali", "series": "Genshin Impact"})
    assert '"Genshin Impact" (a fact)' in block
    assert "Do not name any other character" in block
    identified = captioner.given_block({"name": "Stelle", "series": "", "source": "identify"})
    assert "identified beforehand" in identified and "folder name" not in identified


def test_the_character_check_catches_a_drifting_caption():
    agree = {"character": "March 7th", "seen": ["March 7th", "March 7th"],
             "platforms": {"bluesky": {"caption": "March 7th, ready for adventure"}}}
    assert captioner.character_mismatch(agree, ["March 7th"], ["Chiori", "Elysia"]) == ""

    # the Pixiv half thought it was someone else (the Elysia case)
    pixiv = {**agree, "seen": ["March 7th", "Elysia"]}
    assert "Elysia" in captioner.character_mismatch(pixiv, ["March 7th"])

    # the caption itself names another known character (the Chiori case)
    wrong = {"character": "", "seen": [""],
             "platforms": {"bluesky": {"caption": "Chiori from Genshin Impact"}}}
    assert "Chiori" in captioner.character_mismatch(wrong, ["March 7th"], ["Chiori"])

    # short and long forms of one name agree; an unknown set is never checked
    assert captioner.character_mismatch({"seen": ["Raiden Shogun"]}, ["Raiden"]) == ""
    assert captioner.character_mismatch({"seen": ["Hu Tao"]}, ["Hu-Tao"]) == ""
    assert captioner.character_mismatch({"seen": ["Anyone"]}, []) == ""
    # a duo reported in one string is fine as long as both names belong
    assert captioner.character_mismatch({"seen": ["Nahida & Wanderer"]},
                                        ["Nahida", "Wanderer"]) == ""


def test_a_blank_mixed_caption_gets_a_catchy_fallback(monkeypatch, tmp_path):
    """Live bug: the Pixiv half answered with an empty caption for a mix."""
    async def fake_call(cfg, model, messages, timeout):
        return json.dumps({"alt_text": "a", "platforms": {
            "pixiv": {"caption": "", "tags": []}}})

    monkeypatch.setattr(captioner, "_call", fake_call)
    result = asyncio.run(captioner.generate_mixed(
        cfg={"api_key": "k"}, images=[_image(tmp_path)], characters=["Kafka", "Stelle"],
        series=["Honkai: Star Rail"], platforms=["bluesky", "pixiv"]))

    pixiv = result["platforms"]["pixiv"]
    assert pixiv["caption"] in captioner.MIXED_FALLBACK_JA
    assert pixiv["title"] == captioner.MIXED_FALLBACK_TITLE_JA
    assert result["platforms"]["bluesky"]["caption"]  # the skipped site got one too
    assert "Kafka" in pixiv["tags"] and "Stelle" in pixiv["tags"]
