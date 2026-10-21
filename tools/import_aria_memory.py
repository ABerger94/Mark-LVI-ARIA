#!/usr/bin/env python3
"""
tools/import_aria_memory.py — one-time import of ARIA's memory store
into this tree's memory/long_term.json (blueprint D1).

    python tools/import_aria_memory.py <aria-workspace-dir | aria_memory.db>
    python tools/import_aria_memory.py <export.json>
    python tools/import_aria_memory.py <...> --target other/long_term.json
    python tools/import_aria_memory.py <...> --dry-run

WHERE ARIA KEPT IT
    A SQLite database, aria_memory.db in her workspace directory, one
    table: memory(id, category, key, value, updated_at, embedding).
    The embedding column is hers alone (semantic recall) and is not
    carried: this store's recall is lexical by design (memory/
    memory_manager.search_memory), so the vectors would be dead
    weight in a JSON file. A JSON export is also accepted — a list of
    {"category", "key", "value"} objects, or {category: {key: value}} —
    for copies of her store that were exported rather than lifted as
    the .db itself.

THE MAPPING (her categories → this store's)
    identity → identity        preference → preferences (hers was
    projects → projects        singular; this store's is plural)
    relationships → relationships
    wishes → wishes
    notes, note, journal, consolidated, general → notes
    playlist → playlist        (kept as its own category: her DJ mode
                                resolves named playlists from it, and
                                the store tolerates extra categories —
                                load/search/UI all enumerate them. The
                                Phase F DJ port reads them from here.)
    system → SKIPPED           (internal state, not facts)
    anything else → notes
    Keys starting with "_" are SKIPPED wherever they live: ARIA
    treated them as internal/ephemeral and excluded them from her own
    prompt building for the same reason.

THE STORE'S RULES APPLY, NOT HERS
    Values are truncated to this store's 380-char cap (its own
    _truncate_value — a 2,000-char journal entry arrives as its first
    380 chars + "…"), the store is written through save_memory() so
    the 200k whole-store trim applies with its usual notice, and each
    entry keeps ARIA's updated date (converted to YYYY-MM-DD) rather
    than being re-stamped as learned today.

IDEMPOTENT
    Entries are keyed (category, key). A value identical to what's
    already stored is skipped; a different one is overwritten from
    ARIA's side. Running this twice changes nothing the second time.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory import memory_manager  # noqa: E402

_CATEGORY_MAP = {
    "identity": "identity",
    "preference": "preferences",
    "preferences": "preferences",
    "projects": "projects",
    "relationships": "relationships",
    "wishes": "wishes",
    "notes": "notes",
    "note": "notes",
    "journal": "notes",
    "consolidated": "notes",
    "general": "notes",
    "playlist": "playlist",
}
_SKIP_CATEGORIES = {"system"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _map_category(aria_category: str) -> str | None:
    cat = (aria_category or "").strip().lower()
    if cat in _SKIP_CATEGORIES:
        return None
    return _CATEGORY_MAP.get(cat, "notes")


def _map_date(updated_at) -> str:
    text = str(updated_at or "")
    if _DATE_RE.match(text):
        return text[:10]
    return datetime.now().strftime("%Y-%m-%d")


def read_aria_db(db_path: Path) -> list[dict]:
    """Rows from ARIA's sqlite store: [{category, key, value, updated}]."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memory)")]
        if not cols:
            raise SystemExit(f"{db_path}: no 'memory' table — "
                             f"not an ARIA memory database.")
        date_expr = "updated_at" if "updated_at" in cols else "NULL"
        rows = conn.execute(
            f"SELECT category, key, value, {date_expr} FROM memory "
            f"ORDER BY updated_at").fetchall()
    finally:
        conn.close()
    return [{"category": c, "key": k, "value": v, "updated": d}
            for c, k, v, d in rows]


def read_json_export(json_path: Path) -> list[dict]:
    """Her store as an exported JSON file. Two shapes are accepted:
    a list of {"category", "key", "value", "updated"?} objects, or
    {category: {key: value}}."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "key" in item:
                out.append({"category": item.get("category", "notes"),
                            "key": item.get("key"),
                            "value": item.get("value"),
                            "updated": item.get("updated")})
    elif isinstance(data, dict):
        for cat, entries in data.items():
            if isinstance(entries, dict):
                for key, value in entries.items():
                    if isinstance(value, dict):
                        out.append({"category": cat, "key": key,
                                    "value": value.get("value"),
                                    "updated": value.get("updated")})
                    else:
                        out.append({"category": cat, "key": key,
                                    "value": value, "updated": None})
    return out


def read_source(source: Path) -> list[dict]:
    """Resolve the CLI's source argument to ARIA memory rows."""
    if source.is_dir():
        db = source / "aria_memory.db"
        if not db.is_file():
            raise SystemExit(f"{source}: no aria_memory.db in that "
                             f"directory.")
        return read_aria_db(db)
    if source.suffix.lower() == ".json":
        return read_json_export(source)
    if source.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
        return read_aria_db(source)
    raise SystemExit(f"{source}: expected an ARIA workspace directory, "
                     f"an aria_memory.db file, or a JSON export.")


def import_aria_memory(source: Path, target: Path | None = None,
                       dry_run: bool = False) -> dict:
    """Import ARIA's rows into this store. Returns a report dict:
    {read, imported, updated, unchanged, skipped, by_category}."""
    rows = read_source(Path(source))

    old_path = memory_manager.MEMORY_PATH
    if target is not None:
        memory_manager.MEMORY_PATH = Path(target)
    try:
        memory = memory_manager.load_memory()
        report = {"read": len(rows), "imported": 0, "updated": 0,
                  "unchanged": 0, "skipped": 0, "by_category": {}}
        for row in rows:
            key = str(row.get("key") or "").strip()
            value = row.get("value")
            if not key or key.startswith("_") or value is None \
                    or str(value).strip() == "":
                report["skipped"] += 1
                continue
            cat = _map_category(str(row.get("category") or ""))
            if cat is None:
                report["skipped"] += 1
                continue
            value = memory_manager._truncate_value(str(value))
            bucket = memory.setdefault(cat, {})
            existing = bucket.get(key)
            if isinstance(existing, dict) \
                    and existing.get("value") == value:
                report["unchanged"] += 1
                continue
            if isinstance(existing, dict) \
                    or (isinstance(existing, str) and existing):
                report["updated"] += 1
            else:
                report["imported"] += 1
            bucket[key] = {"value": value,
                           "updated": _map_date(row.get("updated"))}
            report["by_category"][cat] = \
                report["by_category"].get(cat, 0) + 1
        changed = report["imported"] + report["updated"]
        if changed and not dry_run:
            memory_manager.save_memory(memory)
        report["changed"] = changed
        return report
    finally:
        memory_manager.MEMORY_PATH = old_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Import ARIA's memory store into long_term.json.")
    ap.add_argument("source",
                    help="ARIA workspace dir, aria_memory.db, or a "
                         "JSON export of her store")
    ap.add_argument("--target", default=None,
                    help="long_term.json to write (default: this "
                         "tree's memory/long_term.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args(argv)

    report = import_aria_memory(
        Path(args.source),
        Path(args.target) if args.target else None,
        dry_run=args.dry_run)
    print(f"Read {report['read']} ARIA memories: "
          f"{report['imported']} new, {report['updated']} updated, "
          f"{report['unchanged']} already here, "
          f"{report['skipped']} skipped (internal/empty).")
    for cat, n in sorted(report["by_category"].items()):
        print(f"  {cat}: {n}")
    if args.dry_run:
        print("Dry run — nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
