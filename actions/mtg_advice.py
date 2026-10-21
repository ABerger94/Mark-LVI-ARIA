"""
actions/mtg_advice.py — should this card go in this deck? (ARIA's,
ported).

Fetches the card from Scryfall and returns ARIA's advice block: the
card's data, its EDHREC rank, and the deck context it was asked
about — the material the Commander-slot judgement is made from.
"""

from __future__ import annotations

from core import mtg, outcome


def mtg_advice(parameters: dict = None, response=None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    name = (params.get("card_name") or "").strip()
    deck = (params.get("deck") or "").strip()
    if not name:
        return outcome.failed("mtg_advice", "no card name was given")
    card, err = mtg.get_card(name)
    if card is None:
        if err == "not_found":
            return outcome.failed(
                "mtg_advice", f"no card matches '{name}' on Scryfall",
                card_name=name)
        return outcome.failed("mtg_advice", err or "lookup failed",
                              card_name=name)
    block = mtg.format_advice(deck, card)
    return outcome.done("mtg_advice", card=card.get("name", name),
                        deck=deck or None) + "\n" + \
        outcome.untrusted("scryfall", block)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mtg_advice",
    "description": (
        "Judge a Magic card for a specific Commander deck: fetches "
        "the card from Scryfall and returns its text, cost, type, "
        "EDHREC rank and the deck context, so the slot can be judged "
        "against what the deck is doing. 'deck' names the deck or "
        "describes its strategy."
    ),
    "brief": "Fetch a card plus EDHREC rank to judge it for a deck.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "card_name": {
                "type": "STRING",
                "description": "Card name (approximate is fine)",
            },
            "deck": {
                "type": "STRING",
                "description": "The deck it would go in — name or strategy",
            },
        },
        "required": ["card_name", "deck"],
    },
    "handler": mtg_advice,
}
