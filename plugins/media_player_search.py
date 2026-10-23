"""
Local media library search for ARIA.

Finds a movie or TV episode in the user's E:\\media library and opens it
in the system video player.

The library is organised one folder per title —
E:\\media\\movies\\<Title (Year)>\\<file>, E:\\media\\tv\\<Show>\\... — and
file names do not always match their folder ("1917 (2017)" holds
"1917 2019.mp4"), so matching looks at folder and file names together.
Among equally good matches the largest file wins: in a title's folder the
feature is the big file, while samples and trailers are small (and are
scored down by name as well).

Playback goes through os.startfile — the default system player — on
purpose: it is independent of the in-HUD video player.
"""

import os

PLUGIN = {
    "name": "media_player_search",
    "description": (
        "Play a movie or TV show from the user's LOCAL media library on the "
        "E: drive (E:\\media\\movies and E:\\media\\tv). Use this FIRST "
        "whenever the user asks to play, watch, or put on a named movie or "
        "show - 'play 1917', 'play Dune', 'watch Severance', 'put on "
        "<title>' - before any YouTube or online video tool. It searches "
        "the local library recursively, matching folder and file names, "
        "and opens the best match in the system video player. Fall back to "
        "YouTube only when this reports no local match, or when the user "
        "explicitly asks for YouTube."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "media_title": {
                "type": "STRING",
                "description": "The name of the movie or TV show to find, as the user said it.",
            },
        },
        "required": ["media_title"],
    },
}

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".m4v", ".avi", ".mov", ".m2ts", ".ts",
                    ".wmv", ".webm")
SEARCH_ROOTS = (r"E:\media\movies", r"E:\media\tv")
FALLBACK_ROOT = r"E:\media"
_JUNK_TOKENS = {"sample", "trailer", "featurette", "deleted", "extras"}


def _normalize(text):
    """Lowercase, punctuation to spaces, collapse whitespace."""
    chars = [c.lower() if c.isalnum() else " " for c in (text or "")]
    return " ".join("".join(chars).split())


def _candidate_text(path):
    """The searchable name of a library file: its folder plus its stem."""
    folder = os.path.basename(os.path.dirname(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    return _normalize(folder + " " + stem)


def _score(query_norm, query_tokens, cand_norm, cand_tokens):
    """How well one candidate matches the query. 0 means no match."""
    if not query_norm:
        return 0.0
    if query_norm == cand_norm:
        return 100.0
    # Whole-phrase match on word boundaries, so a short title like "It"
    # cannot match inside another word ("white").
    if f" {query_norm} " in f" {cand_norm} ":
        return 80.0 + len(query_norm)
    if query_tokens and query_tokens <= cand_tokens:
        return 60.0 + len(query_tokens)
    if query_tokens:
        overlap = len(query_tokens & cand_tokens)
        if overlap:
            return 30.0 * overlap / len(query_tokens)
    return 0.0


def _pick(candidates, media_title):
    """Best path from [(path, size)] for the title, or '' when none match."""
    q = _normalize(media_title)
    qt = set(q.split())
    scored = []
    for path, size in candidates:
        cn = _candidate_text(path)
        s = _score(q, qt, cn, set(cn.split()))
        if s <= 0:
            continue
        if set(cn.split()) & _JUNK_TOKENS and not (qt & _JUNK_TOKENS):
            s -= 15.0
        scored.append((s, size, path))
    if not scored:
        return ""
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[-1][2]


def _find_candidates(roots):
    """Every video file under the roots, as (path, size)."""
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.lower().endswith(VIDEO_EXTENSIONS):
                    p = os.path.join(dirpath, name)
                    try:
                        out.append((p, os.path.getsize(p)))
                    except OSError:
                        out.append((p, 0))
    return out


def run(parameters: dict, player=None, session_memory=None) -> str:
    media_title = (parameters or {}).get("media_title", "").strip()
    if not media_title:
        return "Which movie or show should I look for in your library?"

    roots = [r for r in SEARCH_ROOTS if os.path.isdir(r)]
    if not roots and os.path.isdir(FALLBACK_ROOT):
        roots = [FALLBACK_ROOT]
    if not roots:
        return "I can't reach your media library on the E: drive."

    if player is not None:
        try:
            player.write_log(f"ARIA: Searching the media library for: {media_title}")
        except Exception:
            pass

    best = _pick(_find_candidates(roots), media_title)
    if not best:
        return f"I couldn't find '{media_title}' in your media library."

    try:
        os.startfile(best)
    except Exception as e:
        return f"I found '{os.path.basename(best)}' but couldn't open it: {e}"
    return f"Playing '{os.path.basename(best)}' from your library."
