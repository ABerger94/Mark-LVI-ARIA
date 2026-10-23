"""
Open convoke.games/en/lobby, moxfield.com, and scryfall.com when I say "Let's play some Magic"
"""

import webbrowser

PLUGIN = {
    "name": "magic_setup",
    "description": (
        "Set up for a Magic: The Gathering game: open convoke.games, "
        "moxfield.com, and scryfall.com. ALWAYS call this tool FIRST and "
        "immediately when the user says 'let's play some magic' or anything "
        "close ('let's play magic', 'play some magic', 'magic time'). Do NOT "
        "reply conversationally and do NOT ask which deck - call this tool "
        "first, then talk."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    urls = [
        "https://convoke.games/en/lobby",
        "https://www.moxfield.com",
        "https://scryfall.com"
    ]

    try:
        # Open all three sites in the default browser
        for url in urls:
            webbrowser.open(url)

        if player:
            try:
                player.write_log("ARIA: Magic game setup complete.")
            except Exception:
                pass
        return "Magic setup complete. Opened Convoke, Moxfield, and Scryfall."

    except Exception as e:
        return f"Sorry, there was an issue setting up for Magic: {e}"
