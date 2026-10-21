"""Phase F-REST group 4 — price watches (ARIA's, ported).

The network is stubbed at the check pass's fetch seam. Pinned shut:
  * price extraction keeps ARIA's precedence — JSON-LD first, then
    the LOWEST dollar amount on the page;
  * a drop alerts exactly once (the alerted flag), and a failed or
    price-less fetch can never eat a drop by marking state;
  * adding a watch ensures the hourly scheduler task exists — the
    'checks run hourly' line is a fact, tested against a stubbed
    scheduler (the real one is group 5; the seam is the lazy import).
"""
import sys
import types

import pytest

from core import pricecheck
from actions import watch_price as watch_action
from actions import list_price_watches as list_action
from actions import check_price_watches as check_action
from actions import unwatch_price as unwatch_action


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(pricecheck, "STORE_PATH",
                        tmp_path / "price_watches.json")


@pytest.fixture
def fake_scheduler(monkeypatch):
    fake = types.ModuleType("core.scheduler")
    fake.rows = []
    fake.added = []

    def _list_rows():
        return list(fake.rows)

    def _add_task(kind, prompt, interval_s=0):
        fake.added.append((kind, prompt, interval_s))
        fake.rows.append({"kind": kind, "prompt": prompt})
        return len(fake.rows)

    fake.list_rows = _list_rows
    fake.add_task = _add_task
    monkeypatch.setitem(sys.modules, "core.scheduler", fake)
    return fake


# ── extraction ───────────────────────────────────────────────────────────────

def test_extract_prefers_jsonld():
    page = '{"@type":"Product","offers":{"price": "129.99"}} only $59.99'
    assert pricecheck.extract_price(page) == 129.99


def test_extract_falls_back_to_lowest_dollar_amount():
    page = "Was $89.99 — now $64.50! Shipping $5.00 extra"
    # $5.00 is the lowest number on the page; ARIA's rule takes the
    # minimum, shipping included. The test pins the rule as shipped,
    # not as one might wish it.
    assert pricecheck.extract_price(page) == 5.00


def test_extract_none_when_no_price():
    assert pricecheck.extract_price("no prices here") is None
    assert pricecheck.extract_price("") is None


# ── store round trip ─────────────────────────────────────────────────────────

def test_add_list_remove(fake_scheduler):
    result = watch_action.watch_price(
        {"url": "https://example.com/goose", "target_price": "$40",
         "label": "porch goose"})
    assert "status=done" in result and "watch_id=1" in result

    listing = list_action.list_price_watches({})
    assert "porch goose" in listing and "target $40.00" in listing
    assert "not checked yet" in listing

    removed = unwatch_action.unwatch_price({"watch_id": 1})
    assert "status=done" in removed
    assert "No active price watches" in list_action.list_price_watches({})


def test_watch_price_rejects_bad_target():
    result = watch_action.watch_price(
        {"url": "https://example.com/x", "target_price": "cheap"})
    assert "status=failed" in result
    assert pricecheck.list_watches() == []


def test_unwatch_missing_id_fails_honestly():
    assert "status=failed" in unwatch_action.unwatch_price({"watch_id": 9})


# ── the check pass ───────────────────────────────────────────────────────────

def test_drop_alerts_once_and_records_last_price():
    pricecheck.add_watch("https://example.com/a", 50.0, "thing")
    pages = {"https://example.com/a": "Price: $45.00 today"}
    summary, alerts = pricecheck.check_watches(fetch=pages.get)
    assert "Checked 1 price watch" in summary
    assert len(alerts) == 1 and "now $45.00" in alerts[0]
    watch = pricecheck.list_watches()[0]
    assert watch["last_price"] == 45.0 and watch["alerted"] is True

    # Same price again: still recorded, not re-alerted.
    summary2, alerts2 = pricecheck.check_watches(fetch=pages.get)
    assert alerts2 == []


def test_failed_fetch_never_eats_a_drop():
    pricecheck.add_watch("https://example.com/b", 50.0, "thing2")

    def _boom(url):
        raise OSError("network down")

    summary, alerts = pricecheck.check_watches(fetch=_boom)
    assert alerts == []
    watch = pricecheck.list_watches()[0]
    assert watch["last_price"] is None and watch["alerted"] is False


def test_check_action_reports_the_drop():
    pricecheck.add_watch("https://example.com/c", 10.0, "cheap thing")
    import core.pricecheck as pc
    original = pc.fetch_page
    pc.fetch_page = lambda url: "only $8.25"
    try:
        result = check_action.check_price_watches({})
    finally:
        pc.fetch_page = original
    assert "alerts=1" in result and "DROP" in result


def test_check_with_no_watches():
    result = check_action.check_price_watches({})
    assert "checked=0" in result and "No price watches" in result


# ── the hourly task ──────────────────────────────────────────────────────────

def test_ensure_creates_one_hourly_task(fake_scheduler):
    assert pricecheck.ensure_pricecheck_task() is False  # no watches yet
    pricecheck.add_watch("https://example.com/d", 5.0, "x")
    assert pricecheck.ensure_pricecheck_task() is True
    assert fake_scheduler.added == [("pricecheck", "pricecheck", 3600)]
    # Idempotent: a second call creates nothing.
    assert pricecheck.ensure_pricecheck_task() is False
    assert len(fake_scheduler.added) == 1
