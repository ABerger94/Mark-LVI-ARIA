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

import random

PLUGIN = {
    "name": "dice_roller",                     # snake_case, unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    "description": (
        "Rolls a die with a specified number of sides. "
        "Trigger phrases include 'roll a die', 'throw a dice', or 'roll a D20'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "sides": {"type": "INTEGER", "description": "The number of sides on the die to roll."},
        },
        "required": ["sides"],   # omit or leave empty for a zero-argument tool
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    parameters: dict of the args Gemini extracted, matching PLUGIN['parameters'].
    player: the JarvisUI instance — use player.write_log(f"JARVIS: ...") to log,
            same as actions/*.py. May be None.
    session_memory: reserved, usually None today (core tools mostly pass None too).
    Return a short natural-language string — this is spoken back to the user.
    Never raise: catch your own errors and return a spoken error string instead
    (the loader also catches exceptions as a second safety net, but don't rely on it).
    """
    sides = parameters.get("sides")
    
    try:
        if sides < 1:
            return "I cannot roll a die with fewer than one side, sir."
            
        roll = random.randint(1, sides)
        result_text = f"The roll on the D{sides} resulted in a {roll}."
    except Exception as e:
        return f"Sir, the dice roller failed: {e}"
        
    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
