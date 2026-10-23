"""
MoviesPlugin for ARIA. Finds and plays movie files.
"""

import os
import subprocess

PLUGIN = {
    "name": "MoviesPlugin",
    "description": (
        "Plays a requested movie. Triggers when the user says 'Play <movie name> movie'. "
        "Finds and plays the corresponding .mp4 file from the local library."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "movie_name": {"type": "STRING", "description": "The name of the movie to play, extracted from user speech."},
        },
        "required": ["movie_name"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    Finds and plays <movie name>.mp4 from E:\\media\\movies.
    """
    movie_name = parameters.get("movie_name", "").strip()
    if not movie_name:
        return "I couldn't understand which version of the movie you wanted."

    movies_dir = r"E:\media\movies"
    file_path = os.path.join(movies_dir, f"{movie_name}.mp4")

    # Log attempt if player is available
    if player:
        try:
            player.write_log(f"JARVIS: Looking for movie: {file_path}")
        except Exception:
            pass

    if not os.path.exists(file_path):
        return f"Sir, I could not find the file {movie_name}.mp4 in the media directory."

    try:
        # Start file with OS associated player
        os.startfile(file_path)
        result_text = f"Playing {movie_name} now, Sir."
    except Exception as e:
        return f"Sir, I encountered an error trying to play {movie_name}: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass

    return result_text
