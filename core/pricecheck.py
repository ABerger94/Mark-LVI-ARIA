"""
core/pricecheck.py — price-drop watches (ARIA's, ported).

ARIA's design, kept: a watch is a URL, a target price and a label.
A check pass fetches each page, extracts a price (JSON-LD product
markup first — the most reliable signal on a retail page — then the
lowest $-amount on the page), records it as the last seen price, and
alerts ONCE when the price lands at or below target: the watch is
marked alerted so the same drop does not announce itself on every
pass, exactly as hers behaved.

Two substrate changes, both following this codebase's precedents
rather than ARIA's:
  * The store is JSON at memory/price_watches.json, beside the other
    LVI stores (ARIA used sqlite tables inside her memory DB).
  * The hourly pass ARIA scheduled is an in-app scheduler task of
    kind "pricecheck" (core/scheduler.py), created by
    ensure_pricecheck_task() — the same idempotent shape as hers:
    only when watches exist, only if no such task is already there.
    The scheduler's tick runs the pass and speaks any alerts through
    the bound say hook.

The fetch is one injectable function (fetch_page) so tests run with
stubbed pages and no network.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# JSON-LD product markup is the most reliable signal; fall back to
# $ amounts. (ARIA's regexes, verbatim.)
_JSONLD_PRICE_RE = re.compile(r'"price"\s*:\s*"?([0-9][0-9,]*\.[0-9]{2})"?')
_DOLLAR_PRICE_RE = re.compile(r'\$\s*([0-9][0-9,]*\.[0-9]{2})')

_PAGE_CAP = 2_000_000


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch this constant to a tmp path (the routines pattern).
STORE_PATH = _base_dir() / "memory" / "price_watches.json"


def _store_path() -> Path:
    return STORE_PATH


def _load() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("watches"), list):
            return data
    except Exception:
        pass
    return {"next_id": 1, "watches": []}


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def extract_price(text: str):
    """Best-effort price extraction. Returns float or None.

    ARIA's logic verbatim: a JSON-LD "price" wins; otherwise the
    LOWEST dollar amount on the page (a product page mentions the
    list price, the sale price, shipping — the sale price is the one
    a watch cares about, and it is never the highest number there)."""
    if not text:
        return None
    m = _JSONLD_PRICE_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    prices = []
    for m in _DOLLAR_PRICE_RE.finditer(text):
        try:
            prices.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return min(prices) if prices else None


def fetch_page(url: str) -> str:
    """GET a page as text. Best-effort: the extractor works on
    whatever came back, markup and all."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ARIA-Agent/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(_PAGE_CAP).decode("utf-8", errors="replace")


def add_watch(url: str, target: float, label: str = "item") -> dict:
    data = _load()
    watch = {
        "id": data["next_id"],
        "url": url,
        "target": float(target),
        "label": label or "item",
        "last_price": None,
        "alerted": False,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    data["next_id"] += 1
    data["watches"].append(watch)
    _save(data)
    return watch


def list_watches() -> list[dict]:
    return list(_load()["watches"])


def remove_watch(watch_id: int) -> bool:
    data = _load()
    before = len(data["watches"])
    data["watches"] = [w for w in data["watches"]
                        if w.get("id") != int(watch_id)]
    if len(data["watches"]) == before:
        return False
    _save(data)
    return True


def check_watches(fetch: Optional[Callable[[str], str]] = None
                  ) -> tuple[str, list[str]]:
    """One check pass. Returns (summary, alert lines).

    A watch whose page will not load, or shows no extractable price,
    is skipped for this pass — its last_price stands and it is NOT
    marked alerted, so a transient failure can never eat a drop."""
    fetch = fetch or fetch_page
    data = _load()
    watches = data["watches"]
    if not watches:
        return "[No price watches to check]", []
    checked = 0
    alerts: list[str] = []
    dirty = False
    for w in watches:
        checked += 1
        try:
            price = extract_price(fetch(w.get("url") or ""))
        except Exception:
            continue
        if price is None:
            continue
        w["last_price"] = price
        dirty = True
        if price <= float(w["target"]) and not w.get("alerted"):
            w["alerted"] = True
            alerts.append(
                f"DROP: '{w['label']}' now ${price:.2f} "
                f"(target ${float(w['target']):.2f}) — {w['url']}")
    if dirty:
        _save(data)
    summary = f"Checked {checked} price watch(es)."
    if alerts:
        summary += "\n" + "\n".join(alerts)
    return summary, alerts


def render_watches(watches: list[dict]) -> str:
    """ARIA's list format."""
    if not watches:
        return "[No active price watches]"
    lines = []
    for w in watches:
        last = w.get("last_price")
        lines.append(
            f"#{w['id']} {w['label']} — target ${float(w['target']):.2f}"
            f"{f', last seen ${last:.2f}' if last else ', not checked yet'}"
            f"{' [alerted]' if w.get('alerted') else ''}\n  {w['url']}")
    return "\n".join(lines)


def ensure_pricecheck_task() -> bool:
    """Create the hourly in-app pricecheck task if watches exist and
    none is scheduled. Idempotent, as ARIA's was. Returns True when a
    task was created. Best-effort: any failure means 'not created',
    never an exception into a tool call."""
    try:
        if not list_watches():
            return False
        from core import scheduler
        for row in scheduler.list_rows():
            if row.get("kind") == "pricecheck":
                return False
        scheduler.add_task("pricecheck", "pricecheck", interval_s=3600)
        return True
    except Exception:
        return False
