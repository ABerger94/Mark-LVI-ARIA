"""Phase H2 — MCP with the SDK absent: the adapter rule.

The registry walk (diagnostics probe 6 rebuilds it from a fresh
discovery) must never fail because an optional dependency is
missing. These tests simulate the absence by breaking
core/mcp_client._sdk at the seam — the one place the SDK is
reached for — and pin what survives: the module imports, its
five actions load, the roster still reads, and connect fails
with the install sentence instead of an exception.
"""
import pytest

from core import mcp_client
from memory import config_manager as cm


@pytest.fixture()
def no_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")

    def _gone():
        raise ImportError("MCP support is not installed "
                          "(pip install mcp)")
    monkeypatch.setattr(mcp_client, "_sdk", _gone)
    mcp_client.reset_for_tests()
    yield
    mcp_client.reset_for_tests()


def test_module_reports_unavailable(no_sdk):
    assert mcp_client.sdk_available() is False
    assert mcp_client.declarations() == []
    assert mcp_client.tool_names() == set()
    assert mcp_client.has_tool("mcp_x_y") is False


def test_connect_fails_with_the_install_sentence(no_sdk):
    cm.save_mcp_server("demo", {"command": "python", "args": []})
    from actions import mcp_connect, mcp_list_servers
    out = mcp_connect.mcp_connect({"name": "demo"})
    assert out.startswith("status=failed")
    assert "pip install mcp" in out
    out = mcp_list_servers.mcp_list_servers({})
    assert "[configured]" in out and "pip install mcp" in out


def test_actions_still_discover_cleanly(no_sdk):
    from pathlib import Path

    from core.action_loader import discover_actions
    root = Path(__file__).resolve().parent.parent
    registry = discover_actions(actions_dir=root / "actions",
                                reserved_names=set(),
                                logger=lambda _m: None)
    for name in ("mcp_setup", "mcp_connect", "mcp_disconnect",
                 "mcp_list_servers", "mcp_remove_server"):
        assert registry.has(name), name
    # The walk itself completed (this is the assertion that
    # matters — a missing optional dep must never break it), and
    # none of the MCP files is among whatever a headless box
    # legitimately rejects (a display-bound action, say).
    rejected = [r.file for r in registry._all_records if not r.valid]
    assert not [f for f in rejected if f.startswith("mcp_")], rejected
