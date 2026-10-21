"""Phase B1 — the bridge, fixed (blueprint S2).

Two regressions being pinned shut:

  * Manual pairing was impossible by construction — the server issued
    8-character keys while the login page capped its input at 6, truncated
    every correct key before it was sent, and auto-submitted the truncation.
    The page's numbers are read from the page and the server's from the
    server (PAIRING_KEY_LENGTH); nothing is copied into this file, so if
    either side ever moves, this fails instead of pairing breaking quietly
    again.

  * HTTPS was opt-in despite the module docstring claiming TLS was
    mandatory, so default installs served the command channel in clear.
    It is now on unless config says false, and _ssl_enabled() still
    requires a certificate pair to actually exist — wanted-but-unmakeable
    keeps its loud plain-HTTP fallback in serve().
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── (a) pairing length: page and server must agree ───────────────────────────

def _page_lengths() -> dict:
    html = (ROOT / "dashboard" / "static" / "login.html").read_text(
        encoding="utf-8")
    found = {}
    m = re.search(r'maxlength="(\d+)"', html)
    assert m, "login page input carries no maxlength"
    found["maxlength"] = int(m.group(1))
    m = re.search(r"inp\.value\.length === (\d+)", html)
    assert m, "login page has no auto-submit length check"
    found["auto-submit length"] = int(m.group(1))
    m = re.search(r"key\.length < (\d+)", html)
    assert m, "login page has no minimum-length gate"
    found["minimum-length gate"] = int(m.group(1))
    return found


def test_login_page_accepts_the_keys_the_server_issues():
    from dashboard.server import PAIRING_KEY_LENGTH

    for where, n in _page_lengths().items():
        assert n == PAIRING_KEY_LENGTH, (
            f"login page {where} assumes {n} characters; "
            f"the server issues {PAIRING_KEY_LENGTH}")


def _server():
    import dashboard.server as srv_mod
    if not srv_mod._DEPS_OK:
        pytest.skip("fastapi/uvicorn not installed in this environment")
    return srv_mod.DashboardServer()


def test_issued_keys_have_the_declared_length():
    from dashboard.server import PAIRING_KEY_LENGTH

    key = _server().new_key()
    assert len(key) == PAIRING_KEY_LENGTH


def test_manual_pairing_login_flow():
    """The flow the bug broke: type the issued key, get a session.

    Also pins the QR path (same key, /auto-login) and the one-time rule —
    a spent key must not work twice.
    """
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import dashboard.server as srv_mod

    srv = _server()
    client = TestClient(srv.app)

    # QR first, on its own key: the flow that always worked must keep working.
    qr_key = srv.new_key()
    r = client.get(f"/auto-login?key={qr_key}")
    assert r.status_code == 200
    assert "jarvis_token" in r.text

    key = srv.new_key()
    assert len(key) == srv_mod.PAIRING_KEY_LENGTH
    r = client.post("/login", json={"pin": key})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["token"]

    # One-time: the same key cannot be spent a second time.
    r = client.post("/login", json={"pin": key})
    assert r.status_code == 401

    # A wrong key of the right shape is refused outright.
    wrong = "Z" * srv_mod.PAIRING_KEY_LENGTH
    assert wrong != key
    r = client.post("/login", json={"pin": wrong})
    assert r.status_code == 401


# ── (b) HTTPS: on by default, off only by explicit setting ──────────────────

def _point_at(tmp_path, monkeypatch, config="absent", certs=False):
    """Aim the dashboard's config resolution at a scratch tree."""
    import dashboard.server as srv_mod

    if config != "absent":
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "api_keys.json").write_text(
            json.dumps(config), encoding="utf-8")
    if certs:
        cert_dir = tmp_path / "config" / "certs"
        cert_dir.mkdir(parents=True, exist_ok=True)
        (cert_dir / "jarvis.key").write_text("key", encoding="utf-8")
        (cert_dir / "jarvis.crt").write_text("crt", encoding="utf-8")
    monkeypatch.setattr(srv_mod, "BASE_DIR", tmp_path)
    return srv_mod.DashboardServer


@pytest.mark.parametrize("config", ["absent", {}, {"unrelated": 1}])
def test_https_is_the_default(tmp_path, monkeypatch, config):
    cls = _point_at(tmp_path, monkeypatch, config=config, certs=True)
    assert cls._https_wanted() is True
    assert cls._ssl_enabled() is True


def test_https_default_but_no_certs_yet(tmp_path, monkeypatch):
    """Wanted, not yet in force: serve() generates the pair, then
    _ssl_enabled() turns true. Before generation it must read false —
    this is also the fallback signal when generation fails."""
    cls = _point_at(tmp_path, monkeypatch, certs=False)
    assert cls._https_wanted() is True
    assert cls._ssl_enabled() is False


def test_https_explicit_opt_out(tmp_path, monkeypatch):
    cls = _point_at(tmp_path, monkeypatch,
                    config={"dashboard_https": False}, certs=True)
    assert cls._https_wanted() is False
    assert cls._ssl_enabled() is False


def test_https_explicit_on_still_needs_certs(tmp_path, monkeypatch):
    cls = _point_at(tmp_path, monkeypatch,
                    config={"dashboard_https": True}, certs=False)
    assert cls._https_wanted() is True
    assert cls._ssl_enabled() is False
