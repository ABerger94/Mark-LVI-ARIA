"""
actions/mtg_card.py — look up a Magic card on Scryfall (ARIA's,
ported).

Fuzzy name match against Scryfall's named-card endpoint: oracle
text, type line, mana cost, and the current USD market price from
the response this call just received (never cached — prices move).
"""

from __future__ import annotations

from core import mtg, outcome


def mtg_card(parameters: dict = None, response=None, player=None,
             session_memory=None) -> str:
    params = parameters or {}
    name = (params.get("card_name") or "").strip()
    if not name:
        return outcome.failed("mtg_card", "no card name was given")
    card, err = mtg.get_card(name)
    if card is None:
        if err == "not_found":
            return outcome.failed(
                "mtg_card", f"no card matches '{name}' on Scryfall",
                card_name=name)
        return outcome.failed("mtg_card", err or "lookup failed",
                              card_name=name)
    block = mtg.format_card(card)
    return outcome.done("mtg_card", card=card.get("name", name),
                        set=card.get("set", None),
                        rarity=card.get("rarity", None)) + "\n" + \
        outcome.untrusted("scryfall", block)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mtg_card",
    "description": (
        "Look up a Magic: The Gathering card by name on Scryfall "
        "(fuzzy matching, so an approximate name works). Returns the "
        "oracle text, type line, mana cost and the current USD market "
        "price. Use mtg_advice instead when the question is whether "
        "a card belongs in a specific deck."
    ),
    "brief": "Look up a Magic card: text, type, cost, current price.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "card_name": {
                "type": "STRING",
                "description": "Card name (approximate is fine — Scryfall fuzzy-matches)",
            },
        },
        "required": ["card_name"],
    },
    "handler": mtg_card,
}
