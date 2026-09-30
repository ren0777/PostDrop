"""Multiple accounts per site: storage, keys and the one-time migration."""
import pytest

from app.core import accounts, secrets


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(secrets, "load", lambda key: dict(data.get(key, {})))
    monkeypatch.setattr(secrets, "save", lambda key, cfg: data.__setitem__(key, dict(cfg)))
    monkeypatch.setattr(secrets, "delete", lambda key: data.pop(key, None))
    return data


def test_create_save_and_remove_accounts(store):
    key = accounts.create("instagram", "Solo")
    assert key == "solo"
    assert accounts.keys("instagram") == ["solo"]

    accounts.update("instagram", key, {"can_invite": True, "collaborators": "x"})
    cards = accounts.cards("instagram")
    assert len(cards) == 1
    card = cards[0]
    assert card["id"] == "instagram:solo"
    assert card["name"] == "Solo"
    assert card["connected"] is False
    assert card["can_invite"] is True
    assert card["collaborators"] == "x"
    assert "tokens" not in card  # tokens never travel in a payload

    accounts.remove("instagram", "solo")
    assert accounts.keys("instagram") == []
    assert "instagram:solo" not in store


def test_duplicate_names_get_their_own_key(store):
    accounts.create("instagram", "Solo")
    assert accounts.create("instagram", "Solo") == "solo-2"
    assert accounts.keys("instagram") == ["solo", "solo-2"]


def test_merged_config_holds_shared_and_account_fields(store):
    store["instagram"] = {"client_id": "cid", "imgbb_api_key": "key", "accounts": ["main"]}
    store["instagram:main"] = {"name": "Main", "tokens": {"access_token": "t"}}
    cfg = accounts.merged("instagram", "main")
    assert cfg["client_id"] == "cid"
    assert cfg["imgbb_api_key"] == "key"
    assert cfg["tokens"]["access_token"] == "t"
    assert cfg["card_id"] == "instagram:main"


def test_legacy_single_account_migrates_once(store):
    store["instagram"] = {
        "client_id": "cid", "client_secret": "s", "imgbb_api_key": "k",
        "username": "artist",
        "tokens": {"access_token": "tok", "user_id": "1"},
    }
    cards = accounts.cards("instagram")
    assert len(cards) == 1
    card = cards[0]
    assert card["key"] == "main"
    assert card["name"] == "Main"
    assert card["username"] == "artist"
    assert card["connected"] is True
    assert card["can_invite"] is False        # the migrated account invites nobody
    assert card["collaborators"] == ""

    shared = store["instagram"]
    assert shared["client_id"] == "cid"
    assert shared["imgbb_api_key"] == "k"
    assert "tokens" not in shared and "username" not in shared
    assert store["instagram:main"]["tokens"]["access_token"] == "tok"

    # running it again changes nothing
    assert accounts.keys("instagram") == ["main"]
    assert len(accounts.cards("instagram")) == 1


def test_a_config_without_legacy_tokens_is_left_alone(store):
    store["instagram"] = {"client_id": "cid", "imgbb_api_key": "k"}
    assert accounts.keys("instagram") == []
    assert store["instagram"] == {"client_id": "cid", "imgbb_api_key": "k"}
