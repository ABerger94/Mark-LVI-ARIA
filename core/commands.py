"""
The Commands screen's data — built from the live tool registries, never
maintained by hand.

ARIA's original commands screen was a static guide, and it drifted: tools
were added, renamed and removed while the guide went on describing the
assistant she used to be. This module is the fix. At runtime main.py hands
build_command_list() the three declaration lists the session itself is
built from — the inline tools, the discovered actions, the enabled
plugins — and the screen shows exactly those, so it cannot describe a tool
she does not have, or hide one she does.

EXAMPLE_PHRASES is the one curated piece, and it is display sugar only: a
missing entry costs a tool its example line, never its row. The tests pin
its keys against the live registries, so renaming a tool loses its example
loudly instead of stranding a phrase on a tool that no longer exists.
"""

# Example phrases for the tools people actually reach for, keyed by tool
# name and written the way they would be said out loud. Everything not
# listed here is shown with its description alone.
EXAMPLE_PHRASES = {
    "system_status":     "How's the computer running?",
    "screen_process":    "What's on my screen right now?",
    "close_camera":      "Close the camera",
    "manage_monitor":    "Keep an eye on graphics card prices",
    "recall_memory":     "What do you remember about me?",
    "undo":              "Undo that last change",
    "shutdown_jarvis":   "That's all — shut down",
    "open_app":          "Open Spotify",
    "web_search":        "Search the web for the latest headlines",
    "weather_report":    "What's the weather like today?",
    "reminder":          "Remind me in an hour to call the dentist",
    "send_message":      "Send a WhatsApp saying I'm running late",
    "browser_control":   "Open YouTube in the browser",
    "computer_control":  "Click the OK button",
    "computer_settings": "Turn the volume up",
    "desktop_control":   "Change my wallpaper",
    "file_controller":   "What's taking up space on my drive?",
    "file_processor":    "Summarise this document",
    "flight_finder":     "Find me a flight to Chicago on Friday",
    "game_updater":      "Update my games on Steam",
    "code_helper":       "Explain what this script does",
    "dev_agent":         "Build me a to-do list app",
    "video_player":      "Play a video of the ocean",
}

_SOURCE_ORDER = ("Built-in", "Action", "Plugin")


def build_command_list(inline_decls, action_decls, plugin_decls) -> list[dict]:
    """Merge the three declaration lists into Commands-screen rows.

    Each row: {"name", "description", "source", "example"}. On a name
    collision the precedence follows the runtime — inline tools reserve
    their names, actions beat plugins — and the losing duplicate is
    dropped rather than shown twice: the screen answers "what can she
    do", and a shadowed tool is not something she can do. Rows are
    grouped by source, alphabetical within a group.
    """
    rows = {}
    for source, decls in (("Built-in", inline_decls),
                          ("Action", action_decls),
                          ("Plugin", plugin_decls)):
        for decl in decls or []:
            name = decl.get("name")
            if not name or name in rows:
                continue
            rows[name] = {
                "name": name,
                "description": (decl.get("description") or "").strip(),
                "source": source,
                "example": EXAMPLE_PHRASES.get(name, ""),
            }
    return sorted(rows.values(),
                  key=lambda r: (_SOURCE_ORDER.index(r["source"]),
                                 r["name"].lower()))
