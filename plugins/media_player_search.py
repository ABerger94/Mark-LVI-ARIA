"""
Drop-in JARVIS plugin template.

Copy this file, rename it (no leading underscore), fill in PLUGIN and run().
No other file needs to change — JARVIS discovers this automatically at startup.

ONE RULE IF YOUR PLUGIN NEEDS TO ASK A MODEL SOMETHING
    Go through `core.gemini`. Never build your own `genai.Client`.

        from core import gemini
        answer = gemini.text(prompt, tier=gemini.FAST)          # short decisions
        answer = gemini.text(prompt, tier=gemini.SMART,         # reasoning, code
                             timeout_ms=60_000)
        data   = gemini.as_json(prompt, tier=gemini.SMART)      # structured reply
        resp   = gemini.call(contents, tier=gemini.SMART)       # the full object

    You get three things that are impossible to add afterwards: a deadline on
    every call, a ladder that steps to another model when one is out of quota or
    not answering, and — since Mark LVI — whatever provider the user configured
    in ⚙ → ENGINE & PROVIDERS, so your plugin keeps working on Ollama or another
    API when Gemini's text quota is gone.

    A hardcoded client gets none of that and hangs forever when its one model is
    unwell, which is the exact fault the ladder was built to end.
"""

import os
import subprocess

PLUGIN = {
    "name": "media_player_search",
    "description": (
        "Searches folders on the E drive to find and present a show or movie when instructed to 'Play' media."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "media_title": {"type": "STRING", "description": "The name of the movie or TV show to find."},
        },
        "required": ["media_title"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    Scans specified media directories on E: drive for a matching video file name.
    """
    media_title = parameters.get("media_title", "")
    base_path = r"E:\media"
    
    if player:
        player.write_log(f"JARVIS: Searching media folders for: {media_title}")

    try:
        if not os.path.exists(base_path):
            return f"Sir, I cannot access the media directory at {base_path}."

        found_files = []
        video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.m4v')

        for root, dirs, files in os.walk(base_path):
            for file in files:
                if file.lower().endswith(video_extensions) and media_title.lower() in file.lower():
                    found_files.append(os.path.join(root, file))

        if not found_files:
            return f"I'm sorry, sir, but I couldn't find any media matching '{media_title}' in the local folders."

        # Open the first match using the default system handler
        subprocess.run(['explorer', found_files[0]], check=False)
        return f"Playing '{os.path.basename(found_files[0])}' now, sir."

    except Exception as e:
        return f"Sir, I encountered an error while searching for the media: {e}"
