"""
actions/file_organize.py — sort a folder into category subfolders
(ARIA's, ported).

Files directly inside the folder are sorted into Images / Documents /
Videos / Audio / Archives / Code / Other subfolders by extension —
non-recursive, dotfiles never move. Two modes, following the brief
and ARIA's own contract:

  * dry_run=true (the DEFAULT) computes and reports the move plan
    and touches nothing. It is not gated: nothing happens.
  * dry_run=false actually moves files, so it goes through
    core/confirm.py's on-screen gate like file delete does — and the
    completed run pushes ONE undo that reverses every move and clears
    the category folders it created while they are still empty (the
    same one-undo discipline as file_controller's organise-desktop).
"""

from __future__ import annotations

from pathlib import Path

from core import confirm, filetools, outcome
from core.undo import push_undo
from actions.file_controller import _resolve_path


def _run_organize(directory: Path,
                  plan: list[tuple[Path, Path]]) -> str:
    """The gated callable: perform the moves, push the undo, report."""
    moved, errors = filetools.execute_organize(plan)
    done_pairs = [(s, d) for (s, d) in plan if d.exists() and not s.exists()]
    if done_pairs:
        def _undo_organize(entries=tuple(done_pairs)):
            restored = 0
            for origin, moved_to in entries:
                try:
                    if moved_to.exists():
                        origin.parent.mkdir(parents=True, exist_ok=True)
                        import shutil
                        shutil.move(str(moved_to), str(origin))
                        restored += 1
                except Exception as e:
                    print(f"[file] undo organize: {moved_to.name}: {e}")
            for folder in {m.parent for _o, m in entries}:
                try:
                    if folder.exists() and folder.is_dir() \
                            and not any(folder.iterdir()):
                        folder.rmdir()
                except Exception:
                    pass
            return f"{restored} file(s) put back in {directory}."
        push_undo(f"organized {directory.name} ({len(done_pairs)} files)",
                  _undo_organize)
    result = f"Organized {moved}/{len(plan)} files in {directory}."
    if errors:
        result += "\nErrors: " + "; ".join(errors[:5])
    return result


def file_organize(parameters: dict = None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    raw = (params.get("directory") or "").strip()
    if not raw:
        return outcome.failed(
            "file_organize", "no directory was given to organize")
    directory = _resolve_path(raw)
    if not directory.is_dir():
        return outcome.failed(
            "file_organize", f"'{raw}' is not a directory",
            directory=str(directory))
    dry_run = params.get("dry_run", True)
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no")
    try:
        plan, skipped = filetools.plan_organize(directory)
    except Exception as e:
        return outcome.failed(
            "file_organize", f"could not plan the organize: {e}",
            directory=str(directory))
    if not plan:
        return outcome.done("file_organize", directory=str(directory),
                            moves=0, skipped=skipped) + \
            f"\nNo sortable files found in {directory}."

    if dry_run:
        report = filetools.render_plan(directory, plan, skipped, True)
        return outcome.done("file_organize", directory=str(directory),
                            moves=len(plan), dry_run=True) + "\n" + \
            outcome.untrusted("files", report)

    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen — "
                "answer that one first. Nothing was moved.")
    return confirm.request(
        key="file_organize",
        title=f"Organize {directory.name} ({len(plan)} files)",
        detail=(f"Move {len(plan)} file(s) in {directory} into category "
                f"subfolders (Images, Documents, Videos, Audio, Archives, "
                f"Code, Other). An undo is kept for the whole run."),
        run=lambda: _run_organize(directory, plan),
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_organize",
    "description": (
        "Sort the files directly inside a folder into category "
        "subfolders (Images, Documents, Videos, Audio, Archives, Code, "
        "Other) by extension. Non-recursive; dotfiles never move. "
        "dry_run defaults to TRUE: it only reports the plan and moves "
        "nothing. Call with dry_run=false to actually move files — "
        "that asks the user to confirm on screen first, and keeps one "
        "undo for the whole run."
    ),
    "brief": "Sort a folder into category subfolders (dry-run by default).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "directory": {
                "type": "STRING",
                "description": "Folder to organize, absolute path or shortcut (desktop, downloads, home, ...)",
            },
            "dry_run": {
                "type": "BOOLEAN",
                "description": "true (default): report the plan only. false: move the files, after on-screen confirmation",
            },
        },
        "required": ["directory"],
    },
    "handler": file_organize,
}
