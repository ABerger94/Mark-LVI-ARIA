"""
core/filetools.py — the file-commander trio ARIA had and LVI did not:
duplicate finding, filtered find, and folder organising.

Ported from ARIA's aria/tools/fileops.py — the algorithms are hers,
kept deliberately: the two-stage duplicate hash (size, then an 8 KB
quick hash, then a full SHA-256 only where both collide), the find
filters (fnmatch pattern, minimum size, maximum age, content
substring, 200-hit cap), and the organise category map with its
unique-name suffixing. What changed is the surface: ARIA answered
with bracketed prose strings; these return core/outcome lines, with
the listings wrapped as untrusted content (file names are text this
machine did not write).

TWO DELIBERATE DIFFERENCES FROM ARIA
  * Her fileops refused system directories (/etc, /usr, C:\\Windows)
    outright. The migration's Phase C ruling removed root fences from
    file_controller on Alek's explicit word — she can access all
    files — so the refusal is not carried over. What protects files
    here instead: duplicates and find are read-only, and organise's
    real run is confirm-gated with a full undo pushed (the same
    pattern file_controller's organise-desktop uses).
  * Her organise special-cased ARIA's routines directory. LVI's
    routines live under the app's own memory/ folder; organising is
    non-recursive and moves only files directly inside the target, so
    a routines folder inside a target is a directory and is skipped
    like every other directory. No special case is needed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

# Cap on a rendered listing. ARIA truncated tool output globally;
# these three produce the longest listings in the toolset, so they
# keep a local version of the same discipline.
_MAX_OUTPUT = 8000


def _truncate(text: str) -> str:
    if len(text) > _MAX_OUTPUT:
        return text[:_MAX_OUTPUT] + f"\n...[truncated, {len(text)} chars total]"
    return text


def _is_dotfile(name: str) -> bool:
    return name.startswith(".")


# ── Duplicates ───────────────────────────────────────────────────────────────

_QUICK_HASH_BYTES = 8192


def _sha256_file(path: Path, limit_bytes: Optional[int] = None) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            remaining = limit_bytes
            while True:
                chunk = f.read(65536 if remaining is None
                               else min(65536, remaining))
                if not chunk:
                    break
                h.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break
        return h.hexdigest()
    except Exception:
        return None


def find_duplicates(directory: Path) -> tuple[int, list[list[Path]]]:
    """(files scanned, duplicate groups) under directory, recursively.

    Two-stage, exactly as ARIA ran it: group by size, quick-hash the
    first 8 KB of size-collision candidates, full-hash only where the
    quick hashes also collide. Report-only — nothing here deletes."""
    by_size: dict[int, list[Path]] = {}
    scanned = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if _is_dotfile(name):
                continue
            full = Path(root) / name
            try:
                size = os.path.getsize(full)
            except Exception:
                continue
            scanned += 1
            if size == 0:
                continue
            by_size.setdefault(size, []).append(full)

    candidates = [p for paths in by_size.values() if len(paths) > 1
                  for p in paths]
    by_quick: dict[tuple, list[Path]] = {}
    for p in candidates:
        qh = _sha256_file(p, _QUICK_HASH_BYTES)
        if qh is None:
            continue
        try:
            size = os.path.getsize(p)
        except Exception:
            continue
        by_quick.setdefault((size, qh), []).append(p)

    dup_groups: list[list[Path]] = []
    for paths in by_quick.values():
        if len(paths) < 2:
            continue
        by_full: dict[str, list[Path]] = {}
        for p in paths:
            fh = _sha256_file(p)
            if fh is None:
                continue
            by_full.setdefault(fh, []).append(p)
        for grp in by_full.values():
            if len(grp) > 1:
                dup_groups.append(grp)
    return scanned, dup_groups


def render_duplicates(directory: Path, scanned: int,
                      dup_groups: list[list[Path]]) -> str:
    lines = [f"Scanned {scanned} files in {directory}."]
    if not dup_groups:
        lines.append("No duplicates found.")
        return "\n".join(lines)
    lines.append(f"{len(dup_groups)} duplicate group(s) — REPORT ONLY, "
                 f"nothing deleted:")
    for i, grp in enumerate(dup_groups, 1):
        try:
            size = os.path.getsize(grp[0])
        except Exception:
            size = -1
        lines.append(f"Group {i} ({len(grp)} files, {size} bytes each):")
        for p in grp:
            lines.append(f"  {os.path.relpath(p, directory)}")
    lines.append("Report only: tell the user before deleting anything.")
    return _truncate("\n".join(lines))


# ── Filtered find ────────────────────────────────────────────────────────────

_MAX_CONTENT_FILE_MB = 50
_FIND_CAP = 200


def find_advanced(directory: Path, pattern: str = "",
                  min_size_mb: float = 0, max_age_days: float = 0,
                  content_contains: str = "",
                  now: Optional[float] = None) -> tuple[int, list[Path], bool]:
    """(total matches, first-200 hits, truncated?) for the filters.

    Recursive; dotfiles skipped, as ARIA skipped them. `now` is
    injectable so age filtering is testable without touching mtimes
    more than the fixture already does."""
    import time as _time
    min_size = float(min_size_mb or 0) * 1024 * 1024
    cutoff = None
    if max_age_days:
        base = now if now is not None else _time.time()
        cutoff = base - float(max_age_days) * 86400
    needle = content_contains or ""

    hits: list[Path] = []
    total = 0
    truncated = False
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if _is_dotfile(name):
                continue
            full = Path(root) / name
            try:
                st = os.stat(full)
                if st.st_size < min_size:
                    continue
                if cutoff is not None and st.st_mtime < cutoff:
                    continue
                if pattern and not fnmatch.fnmatch(name, pattern):
                    continue
                if needle:
                    if st.st_size > _MAX_CONTENT_FILE_MB * 1024 * 1024:
                        continue
                    try:
                        with open(full, "r", encoding="utf-8",
                                  errors="replace") as f:
                            if needle not in f.read():
                                continue
                    except Exception:
                        continue
                total += 1
                if len(hits) < _FIND_CAP:
                    hits.append(full)
                else:
                    truncated = True
            except Exception:
                continue
    return total, hits, truncated


def render_find(directory: Path, total: int, hits: list[Path],
                truncated: bool) -> str:
    lines = [f"Found {total} match(es) in {directory}:"]
    for h in hits:
        rel = os.path.relpath(h, directory)
        try:
            st = os.stat(h)
            lines.append(f"  {rel}  ({st.st_size / (1024 * 1024):.2f} MB)")
        except Exception:
            lines.append(f"  {rel}")
    if truncated:
        lines.append(f"...[showing first {_FIND_CAP} of {total}]")
    return _truncate("\n".join(lines))


# ── Organise ─────────────────────────────────────────────────────────────────

EXT_MAP: dict[str, list[str]] = {
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
               ".tif", ".svg", ".ico", ".heic", ".avif"],
    "Documents": [".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt",
                  ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".epub", ".log"],
    "Videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m2ts",
               ".ts", ".flv", ".m4v"],
    "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus",
              ".wma", ".mid"],
    "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"],
    "Code": [".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json",
             ".xml", ".yml", ".yaml", ".toml", ".sh", ".bat", ".ps1",
             ".c", ".cpp", ".h", ".hpp", ".java", ".rs", ".go", ".sql"],
}
_OTHER = "Other"


def _category_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    for cat, exts in EXT_MAP.items():
        if ext in exts:
            return cat
    return _OTHER


def _unique_dest(dest_dir: Path, name: str) -> Path:
    base, ext = os.path.splitext(name)
    cand = dest_dir / name
    i = 1
    while cand.exists():
        cand = dest_dir / f"{base}_{i}{ext}"
        i += 1
    return cand


def plan_organize(directory: Path) -> tuple[list[tuple[Path, Path]], int]:
    """(moves, skipped) for sorting directory's own files into category
    subfolders. Non-recursive; dotfiles and directories never move."""
    plan: list[tuple[Path, Path]] = []
    skipped = 0
    entries = sorted(os.listdir(directory))
    for name in entries:
        src = directory / name
        try:
            if src.is_dir():
                if _is_dotfile(name):
                    skipped += 1
                continue
            if _is_dotfile(name) or not src.is_file():
                skipped += 1
                continue
            dest = _unique_dest(directory / _category_for(name), name)
            plan.append((src, dest))
        except Exception:
            skipped += 1
    return plan, skipped


def render_plan(directory: Path, plan: list[tuple[Path, Path]],
                skipped: int, dry_run: bool) -> str:
    lines = [f"{'PLAN' if dry_run else 'MOVED'} — {directory}:"]
    for src, dest in plan:
        lines.append(f"  {os.path.relpath(src, directory)} -> "
                     f"{os.path.relpath(dest, directory)}")
    if skipped:
        lines.append(f"(skipped {skipped} dotfiles/dirs)")
    if dry_run:
        lines.append(f"\nDry run only — nothing was moved. Call again "
                     f"with dry_run=false to execute ({len(plan)} moves).")
    return _truncate("\n".join(lines))


def execute_organize(plan: list[tuple[Path, Path]]) -> tuple[int, list[str]]:
    """Perform the moves. Returns (moved count, error strings)."""
    moved = 0
    errors: list[str] = []
    for src, dest in plan:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved += 1
        except Exception as e:
            errors.append(f"{src.name}: {e}")
    return moved, errors
