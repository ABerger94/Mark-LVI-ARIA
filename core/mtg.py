"""
core/mtg.py — Scryfall lookups (ARIA's, ported).

ARIA's two MTG tools, with her request shape and her output formats:
the fuzzy named-card endpoint, and the card block she handed back
(name + mana cost, type line, oracle text, market price; for advice,
the same data plus EDHREC rank and the deck context).

Scryfall API notes (https://scryfall.com/docs/api — the contract
these calls are written against):
  * GET /cards/named?fuzzy=<name> returns one card object as JSON:
    {name, mana_cost, type_line, oracle_text, prices: {usd, ...},
     edhrec_rank, ...}. Double-faced cards carry card_faces[] with
    their own name/oracle_text and may omit the top-level fields.
  * An unmatched name is HTTP 404 with {object: "error"}.
  * The API is keyless but asks for a descriptive User-Agent and an
    Accept: application/json header — both are sent.
  * Prices are whatever the response says AT CALL TIME. Nothing here
    caches or hardcodes a price; the number in the output was in the
    response this call just received.

The fetch is one injectable function (fetch_json) so tests run on
recorded-shape fixtures with no network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

_NAMED_URL = "https://api.scryfall.com/cards/named?fuzzy="


def fetch_json(url: str) -> dict:
    """GET a Scryfall URL and parse the JSON body. Raises on HTTP
    errors (urllib.error.HTTPError carries the status code)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ARIA-Agent/1.0",
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def get_card(card_name: str,
             fetch: Optional[Callable[[str], dict]] = None
             ) -> tuple[Optional[dict], Optional[str]]:
    """(card, None) on a hit; (None, reason) on a miss or a failure.

    A miss is Scryfall's 404 / error object. Any other failure is the
    transport's own words — the caller reports it as-is rather than
    dressing a network problem up as 'card not found'."""
    fetch = fetch or fetch_json
    url = _NAMED_URL + urllib.parse.quote(str(card_name or ""))
    try:
        data = fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not_found"
        return None, f"Scryfall lookup failed: {e}"
    except Exception as e:
        return None, f"Scryfall lookup failed: {e}"
    if not data or data.get("object") == "error" or "name" not in data:
        return None, "not_found"
    return data, None


def _face_fields(c: dict) -> tuple[str, str, str]:
    """(name, mana_cost, oracle_text) with the double-faced fallback:
    a DFC's top level can omit mana_cost/oracle_text, which live on
    card_faces instead. ARIA printed blanks there; joining the faces
    is the one improvement this port makes, because a blank oracle
    text for a real card reads as 'card has no text'."""
    name = c.get("name", "?")
    mana = c.get("mana_cost", "")
    oracle = c.get("oracle_text")
    if oracle is None:
        faces = c.get("card_faces") or []
        if faces:
            oracle = "\n//\n".join(f.get("oracle_text", "") for f in faces)
            if not mana:
                mana = " // ".join(f.get("mana_cost", "") for f in faces)
    return name, mana or "", oracle or "[no oracle text]"


def format_card(c: dict) -> str:
    """ARIA's card block: name + cost, type, oracle (600 chars),
    and the market line when Scryfall quoted a USD price."""
    name, mana, oracle = _face_fields(c)
    lines = [
        f"{name} {mana}".strip(),
        c.get("type_line", ""),
        oracle[:600],
    ]
    usd = (c.get("prices") or {}).get("usd")
    if usd:
        lines.append(f"Market: ~${usd}")
    return "\n".join(lines)


def format_advice(deck: str, c: dict) -> str:
    """ARIA's advice block: the card's data, its EDHREC rank, and the
    deck context it was asked about. (ARIA's 'verdict' was never an
    algorithm — she returned exactly this and the model judged the
    slot from it. The port keeps that division of labour.)"""
    name, mana, oracle = _face_fields(c)
    return (
        f"Card: {name}\n"
        f"Type: {c.get('type_line', '')}\n"
        f"Mana: {mana}\n"
        f"Oracle: {oracle[:300]}\n"
        f"EDHREC: #{c.get('edhrec_rank', 'n/a')}\n"
        f"Deck context: {deck}"
    )
