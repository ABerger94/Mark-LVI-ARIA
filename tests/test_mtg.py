"""Phase F-REST group 3 — the MTG pair (ARIA's, ported).

The network is stubbed at core/mtg.fetch_json with fixtures in the
real Scryfall response shape. Pinned shut:
  * the price in the output is the price in THIS response — change
    the fixture's price and the output changes (nothing is cached
    or hardcoded);
  * a 404 is a clean 'not found', while a transport failure is
    reported as a transport failure, never dressed up as a miss;
  * a double-faced card (no top-level oracle text) renders its
    faces' text instead of a blank.
"""
import urllib.error

import pytest

from core import mtg
from actions import mtg_card as card_action
from actions import mtg_advice as advice_action


# Recorded shape: the fields of a real /cards/named response that
# the tools read (the live object carries ~60 more keys).
SOL_RING = {
    "object": "card",
    "name": "Sol Ring",
    "mana_cost": "{1}",
    "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.",
    "prices": {"usd": "1.24", "usd_foil": "6.10", "eur": "1.05"},
    "edhrec_rank": 1,
    "set": "c21",
    "rarity": "uncommon",
}

DFC = {
    "object": "card",
    "name": "Delver of Secrets // Insectile Aberration",
    "type_line": "Creature — Human Wizard // Creature — Human Insect",
    "card_faces": [
        {"name": "Delver of Secrets", "mana_cost": "{U}",
         "oracle_text": "At the beginning of your upkeep, look at the "
                      "top card of your library."},
        {"name": "Insectile Aberration", "mana_cost": "",
         "oracle_text": "Flying"},
    ],
    "prices": {"usd": "0.42"},
    "edhrec_rank": 3120,
    "set": "isd",
    "rarity": "common",
}


@pytest.fixture
def scryfall(monkeypatch):
    state = {"card": SOL_RING, "error": None, "urls": []}

    def _fetch(url):
        state["urls"].append(url)
        if state["error"] is not None:
            raise state["error"]
        return state["card"]

    monkeypatch.setattr(mtg, "fetch_json", _fetch)
    return state


def test_card_block_carries_text_and_live_price(scryfall):
    result = card_action.mtg_card({"card_name": "sol ring"})
    assert "status=done" in result
    assert "Sol Ring {1}" in result
    assert "Artifact" in result
    assert "{T}: Add {C}{C}." in result
    assert "Market: ~$1.24" in result
    # The request went to the fuzzy named endpoint.
    assert "api.scryfall.com/cards/named?fuzzy=" in scryfall["urls"][0]


def test_price_is_whatever_the_response_says(scryfall):
    scryfall["card"] = {**SOL_RING, "prices": {"usd": "99.99"}}
    result = card_action.mtg_card({"card_name": "Sol Ring"})
    assert "Market: ~$99.99" in result


def test_card_without_price_omits_market_line(scryfall):
    scryfall["card"] = {**SOL_RING, "prices": {"usd": None}}
    result = card_action.mtg_card({"card_name": "Sol Ring"})
    assert "Market:" not in result
    assert "Sol Ring" in result


def test_404_is_not_found(scryfall):
    scryfall["error"] = urllib.error.HTTPError(
        "https://api.scryfall.com", 404, "Not Found", {}, None)
    result = card_action.mtg_card({"card_name": "Black Lotus Prime"})
    assert "status=failed" in result
    assert "no card matches" in result


def test_error_object_is_not_found(scryfall):
    scryfall["card"] = {"object": "error", "code": "not_found"}
    result = card_action.mtg_card({"card_name": "nope"})
    assert "status=failed" in result and "no card matches" in result


def test_transport_failure_is_not_a_miss(scryfall):
    scryfall["error"] = urllib.error.URLError("connection refused")
    result = card_action.mtg_card({"card_name": "Sol Ring"})
    assert "status=failed" in result
    assert "no card matches" not in result


def test_dfc_renders_face_text(scryfall):
    scryfall["card"] = DFC
    result = card_action.mtg_card({"card_name": "Delver of Secrets"})
    assert "look at the top card of your library" in result
    assert "Flying" in result
    assert "[no oracle text]" not in result


def test_advice_block_has_rank_and_deck_context(scryfall):
    result = advice_action.mtg_advice(
        {"card_name": "Sol Ring", "deck": "Narset storm"})
    assert "status=done" in result
    assert "Card: Sol Ring" in result
    assert "EDHREC: #1" in result
    assert "Deck context: Narset storm" in result


def test_missing_name_fails():
    assert "status=failed" in card_action.mtg_card({})
    assert "status=failed" in advice_action.mtg_advice({"deck": "x"})
