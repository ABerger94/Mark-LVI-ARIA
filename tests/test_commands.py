"""Phase B2 — the Commands screen (blueprint P3).

The failure being pinned shut is drift: ARIA's original screen was a
hand-maintained guide that went on describing tools she no longer had.
Here the rows are built from the same three registries the session itself
is built from, and this suite builds them the same way main.py does at
runtime, then checks the screen against the registries — every registered
action must appear, nothing unregistered may appear, and the curated
example phrases may only name tools that exist right now.
"""
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _live_rows():
    """The screen's rows, built exactly as JarvisLive._commands_for_ui does."""
    import main
    from core.action_loader import discover_actions
    from core.commands import build_command_list
    from core.plugin_loader import discover_plugins

    inline = main.TOOL_DECLARATIONS
    reserved = {t["name"] for t in inline}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    plugins = discover_plugins(ROOT / "plugins",
                                 core_tool_names=reserved | actions.names(),
                                 logger=lambda msg: None)
    rows = build_command_list(inline,
                              actions.get_tool_declarations(),
                              plugins.get_tool_declarations())
    return rows, inline, actions, plugins


def test_overlay_lists_every_model_callable_tool():
    rows, inline, actions, plugins = _live_rows()

    expected = ({t["name"] for t in inline}
                | {d["name"] for d in actions.get_tool_declarations()}
                | {d["name"] for d in plugins.get_tool_declarations()})
    names = [r["name"] for r in rows]

    # Nothing missing, nothing invented, nothing shown twice — in
    # particular every action the registry accepted is on the screen.
    assert set(names) == expected
    assert actions.names() <= set(names)
    assert len(names) == len(set(names))
    # A row with no description is a broken row: the screen's whole job is
    # saying what each command does.
    assert all(r["description"] for r in rows)
    assert all(r["source"] in ("Built-in", "Action", "Plugin") for r in rows)


def test_examples_only_name_real_tools():
    """The curated map is display sugar keyed by tool name; if a tool is
    renamed or removed, its example must fail here rather than strand a
    phrase on a tool that no longer exists."""
    from core.commands import EXAMPLE_PHRASES

    rows, *_ = _live_rows()
    assert set(EXAMPLE_PHRASES) <= {r["name"] for r in rows}
    by_name = {r["name"]: r for r in rows}
    for name, phrase in EXAMPLE_PHRASES.items():
        assert by_name[name]["example"] == phrase


def test_build_command_list_precedence_and_shape():
    from core.commands import build_command_list

    rows = build_command_list(
        [{"name": "alpha", "description": "inline"}],
        [{"name": "alpha", "description": "shadowed action"},
         {"name": "beta", "description": "action"}],
        [{"name": "beta", "description": "shadowed plugin"},
         {"name": "gamma", "description": "plugin"}],
    )
    # Runtime precedence: inline reserves its names, actions beat plugins;
    # the shadowed duplicates are dropped, not shown twice.
    assert [(r["name"], r["source"]) for r in rows] == [
        ("alpha", "Built-in"), ("beta", "Action"), ("gamma", "Plugin")]
    assert rows[0]["description"] == "inline"


def test_ui_wiring():
    """The data path exists end to end: the window holds the callback slot,
    the facade forwards it, and JarvisLive fills it from the registries."""
    import main
    import ui

    assert "self.get_commands" in inspect.getsource(ui.MainWindow.__init__)
    assert isinstance(ui.JarvisUI.__dict__["get_commands"], property)
    assert "self.ui.get_commands = self._commands_for_ui" in \
        inspect.getsource(main)


def test_commands_overlay_constructs_and_filters():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    import ui

    rows, *_ = _live_rows()
    ov = ui.CommandsOverlay(rows, parent=None)
    assert len(ov._rows) == len(rows)
    assert all(not holder.isHidden() for holder, _ in ov._rows)

    # Filtering by a tool's name leaves exactly the rows whose searchable
    # text contains it (a description may legitimately mention another
    # tool's name — screen_process talks about close_camera).
    needle = rows[0]["name"]
    expected = sum(1 for _, hay in ov._rows if needle in hay)
    ov._search.setText(needle)
    visible = [h for h, _ in ov._rows if not h.isHidden()]
    assert len(visible) == expected >= 1
    assert ov._empty.isHidden()

    # Clearing the filter restores every row.
    ov._search.setText("")
    assert all(not holder.isHidden() for holder, _ in ov._rows)

    # A filter matching nothing shows the empty note instead of rows.
    ov._search.setText("zzz-no-such-command-zzz")
    assert all(holder.isHidden() for holder, _ in ov._rows)
    assert not ov._empty.isHidden()
    ov.deleteLater()
