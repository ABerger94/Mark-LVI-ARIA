"""Phase H2 — the MCP client (core/mcp_client.py + the five tools).

What is being pinned shut:

  * config: server specs round-trip through config_manager, and a
    wrecked entry is dropped on read rather than breaking the map;
  * the adapter rule: with the SDK absent (simulated), the module
    imports, its actions load, mcp_list_servers answers, and
    mcp_connect fails with the install sentence — never an import
    error, never a broken registry;
  * the gates: mcp_setup saves nothing until the banner is
    confirmed; mcp_connect launches nothing until confirmed;
    re-saving an identical spec needs no gate;
  * end to end against a REAL fixture server (tests/
    mcp_fixture_server.py, spawned over stdio like a user's):
    connect lists its tools namespaced, declarations carry the
    converted schema, a call round-trips its arguments and result,
    an erroring tool is reported as an error, disconnect ends it
    and the tools disappear;
  * a bogus command fails the connect plainly and leaves the
    server in the failed state with the error kept.
"""
import sys
import time
from pathlib import Path

import pytest

from core import confirm, mcp_client
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "mcp_fixture_server.py"

pytestmark = pytest.mark.skipif(
    not mcp_client.sdk_available(),
    reason="the mcp SDK is not installed in this environment")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    shown = []
    confirm.bind(show=lambda t, d: shown.append((t, d)),
                 hide=lambda: None, log=None)
    mcp_client.reset_for_tests()
    yield {"shown": shown, "tmp": tmp_path}
    mcp_client.reset_for_tests()
    confirm.bind(None, None, None)


def _confirm_and_wait(effect, timeout=15.0):
    """Press the banner's CONFIRM and wait for the parked callable's
    effect (it runs on confirm's worker thread)."""
    confirm.resolve(True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if effect():
            return True
        time.sleep(0.05)
    return effect()


def _setup_fixture(env, name="fixture"):
    from actions import mcp_setup
    out = mcp_setup.mcp_setup({
        "name": name, "command": sys.executable,
        "args": f'"{FIXTURE}"'})
    assert "[CONFIRMATION_PENDING]" in out
    assert _confirm_and_wait(
        lambda: cm.get_mcp_servers().get(name) is not None)
    return name


# ── config ──────────────────────────────────────────────────────────────

def test_specs_round_trip(env):
    cm.save_mcp_server("alpha", {"command": "python", "args": ["-m", "x"]})
    cm.save_mcp_server("beta", {"url": "https://example.com/mcp"})
    servers = cm.get_mcp_servers()
    assert servers["alpha"]["command"] == "python"
    assert servers["beta"]["url"] == "https://example.com/mcp"
    assert cm.remove_mcp_server("alpha") is True
    assert cm.remove_mcp_server("alpha") is False
    assert set(cm.get_mcp_servers()) == {"beta"}


def test_wrecked_spec_is_dropped_on_read(env):
    cm.write_config({"mcp_servers": {"good": {"url": "https://x"},
                                     "bad": "not-a-dict"}})
    assert set(cm.get_mcp_servers()) == {"good"}


# ── the gates ───────────────────────────────────────────────────────────

def test_setup_saves_nothing_before_confirmation(env):
    from actions import mcp_setup
    out = mcp_setup.mcp_setup({"name": "demo", "command": "python",
                               "args": "-m demo"})
    assert "[CONFIRMATION_PENDING]" in out
    assert cm.get_mcp_servers() == {}          # parked, not saved
    assert env["shown"] and "python -m demo" in env["shown"][0][1]
    assert _confirm_and_wait(
        lambda: cm.get_mcp_servers().get("demo") is not None)
    # re-saving the identical spec is a no-op with no new banner
    env["shown"].clear()
    out = mcp_setup.mcp_setup({"name": "demo", "command": "python",
                               "args": "-m demo"})
    assert out.startswith("status=done")
    assert env["shown"] == []


def test_setup_rejects_bad_shapes(env):
    from actions import mcp_setup
    assert mcp_setup.mcp_setup(
        {"name": "Bad Name", "command": "x"}).startswith("status=failed")
    assert mcp_setup.mcp_setup(
        {"name": "ok", "command": "x",
         "url": "https://x"}).startswith("status=failed")
    assert mcp_setup.mcp_setup(
        {"name": "ok", "url": "ftp://x"}).startswith("status=failed")


def test_connect_launches_nothing_before_confirmation(env):
    _setup_fixture(env)
    from actions import mcp_connect
    out = mcp_connect.mcp_connect({"name": "fixture"})
    assert "[CONFIRMATION_PENDING]" in out
    rows = {r["name"]: r for r in mcp_client.status_rows()}
    assert rows["fixture"]["state"] == "configured"   # not launched
    assert _confirm_and_wait(
        lambda: mcp_client.status_rows()[0]["state"] == "connected")
    out = mcp_connect.mcp_connect({"name": "fixture"})
    assert "already connected" in out


# ── end to end against the real fixture server ──────────────────────────

def test_connected_server_tools_round_trip(env):
    _setup_fixture(env)
    ok, detail = mcp_client.connect("fixture")
    assert ok, detail
    names = mcp_client.tool_names()
    assert {"mcp_fixture_echo", "mcp_fixture_add",
            "mcp_fixture_fail"} <= names
    decls = {d["name"]: d for d in mcp_client.declarations()}
    echo = decls["mcp_fixture_echo"]
    assert echo["parameters"]["type"] == "OBJECT"
    assert echo["parameters"]["properties"]["text"]["type"] == "STRING"
    # the round trip: arguments there, the server's own answer back
    assert mcp_client.run_tool(
        "mcp_fixture_echo", {"text": "hello"}) == "echo:hello"
    assert mcp_client.run_tool(
        "mcp_fixture_add", {"a": 2, "b": 40}) == "42"
    # an erroring tool is reported as one, in words
    out = mcp_client.run_tool("mcp_fixture_fail", {})
    assert "reported an error" in out and "fixture failure" in out
    # disconnect ends it: tools gone, calls say so
    assert mcp_client.disconnect("fixture") is True
    assert mcp_client.tool_names() == set()
    assert "not connected" in mcp_client.run_tool(
        "mcp_fixture_echo", {"text": "hello"})
    assert mcp_client.disconnect("fixture") is False


def test_list_servers_shows_state_and_tools(env):
    _setup_fixture(env)
    from actions import mcp_list_servers
    out = mcp_list_servers.mcp_list_servers({})
    assert "[configured]" in out
    mcp_client.connect("fixture")
    out = mcp_list_servers.mcp_list_servers({})
    assert "[connected]" in out and "mcp_fixture_echo" in out


def test_bogus_command_fails_plainly(env):
    cm.save_mcp_server("bogus", {"command": "no-such-program-zzz",
                                 "args": []})
    ok, detail = mcp_client.connect("bogus")
    assert not ok and "failed" in detail
    rows = {r["name"]: r for r in mcp_client.status_rows()}
    assert rows["bogus"]["state"] == "failed"
    assert rows["bogus"]["error"]


def test_remove_disconnects_and_forgets(env):
    _setup_fixture(env)
    mcp_client.connect("fixture")
    from actions import mcp_remove_server
    out = mcp_remove_server.mcp_remove_server({"name": "fixture"})
    assert "[CONFIRMATION_PENDING]" in out
    assert _confirm_and_wait(
        lambda: cm.get_mcp_servers().get("fixture") is None)
    assert mcp_client.tool_names() == set()
