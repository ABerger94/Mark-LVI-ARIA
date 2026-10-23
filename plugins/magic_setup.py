"""
Open the Convoke app, moxfield.com, and scryfall.com when I say "Let's play some Magic"
"""

import os
import webbrowser

PLUGIN = {
    "name": "magic_setup",
    "description": (
        "Set up for a Magic: The Gathering game: open the Convoke app, "
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
        "https://www.moxfield.com",
        "https://scryfall.com"
    ]

    try:
        # Open urls in default browser
        for url in urls:
            webbrowser.open(url)

        # Assumes 'Convoke' is in PATH or handles startfile differently.
        # Adjust command for local environment if necessary, e.g., absolute path.
        # Since 'Convoke app' wasn't specified as a path, we do our best.
        # Using specific command name; if it doesn't open, user sees failure.
        try:
            # On Windows, try simple execute. For known path, use r'path_to_app.exe'
            os.startfile('Convoke') # Attempts to open Convoke if it's in path/registered
        except FileNotFoundError:
            # If startfile fails, the sites are still opened
            return "Opened Moxfield and Scryfall, but I couldn't find the Convoke application."

        if player:
            try:
                player.write_log("JARVIS: Magic game setup complete.")
            except Exception:
                pass
        return "Magic setup complete. Opened Moxfield, Scryfall, and Convoke."

    except Exception as e:
        return f"Sorry, there was an issue setting up for Magic: {e}"
