"""
actions/set_backup_mirror.py — point the backup mirror somewhere.

The rolling backups for the memory store and the config file live
beside the originals (core/backup_mirror.py has the why). This is
the setting's voice surface: name a folder — one a sync service
watches, ideally — and every fresh backup snapshot is copied
there too. Setting it mirrors the CURRENT rotations immediately,
so the mirror starts life populated rather than at the next save;
that first mirror is verified here and reported honestly: a path
that cannot be written is saved (the setting itself is valid) but
the tool answers unverified, with the failure named, instead of
claiming protection that does not exist.

Clear it with clear=true ("stop mirroring my backups").
"""

from __future__ import annotations

from core import backup_mirror, outcome
from memory import config_manager


def set_backup_mirror(parameters: dict = None, player=None,
                      session_memory=None) -> str:
    params = parameters or {}
    if params.get("clear"):
        config_manager.save_backup_mirror_dir("")
        if config_manager.get_backup_mirror_dir() == "":
            return outcome.done(
                "set_backup_mirror", mirror="off") + (
                "\nBackup mirroring is off. The on-machine backup "
                "rotations are unchanged.")
        return outcome.failed(
            "set_backup_mirror",
            "the mirror setting did not clear on re-read")

    path = (params.get("path") or "").strip()
    if not path:
        current = config_manager.get_backup_mirror_dir()
        if current:
            return outcome.done(
                "set_backup_mirror", mirror=current) + (
                f"\nBackups are mirrored to {current}. Pass a new "
                f"path to move the mirror, or clear=true to stop.")
        return outcome.failed(
            "set_backup_mirror",
            "no mirror folder is set and no path was given — name "
            "a folder (a Drive or OneDrive folder works well)")

    config_manager.save_backup_mirror_dir(path)
    if config_manager.get_backup_mirror_dir() != path:
        return outcome.failed(
            "set_backup_mirror",
            "the mirror setting did not save on re-read")

    # Mirror the current rotations now, and say what actually
    # happened: both stores, verified copies (backup_mirror's rule).
    from memory import config_manager as cm
    from memory import memory_manager as mm
    ok = backup_mirror.mirror_backups(cm._backup_slots())
    ok = backup_mirror.mirror_backups(mm._backup_slots()) and ok
    any_backups = any(p.exists() for p in
                      cm._backup_slots() + mm._backup_slots())
    if not any_backups:
        return outcome.done(
            "set_backup_mirror", mirror=path) + (
            f"\nBackups will mirror to {path}. No backups exist "
            f"yet to copy — the first snapshot each store writes "
            f"will land there.")
    if not ok:
        return outcome.unverified(
            "set_backup_mirror",
            "the mirror folder was saved but the first copy to it "
            "failed — it may be unwritable; the failure is in the "
            "incident log", mirror=path)
    return outcome.done("set_backup_mirror", mirror=path) + (
        f"\nBackups now mirror to {path} — the current rotations "
        f"were copied there and verified.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "set_backup_mirror",
    "description": (
        "Set (or clear) the off-machine mirror folder for the "
        "assistant's rolling backups — the memory store's and the "
        "config file's last-five-good-copies rotations, which "
        "otherwise live on the same disk as the originals. Pass "
        "path (a folder, ideally one synced by Google Drive, "
        "OneDrive or a NAS) to start mirroring there; the current "
        "rotations are copied immediately and verified. Pass "
        "clear=true to stop mirroring. With neither, reports where "
        "the mirror currently points."
    ),
    "brief": "Set the backup mirror folder ('path', or clear=true).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "Folder to mirror backups into",
            },
            "clear": {
                "type": "BOOLEAN",
                "description": "Stop mirroring (forget the folder)",
            },
        },
        "required": [],
    },
    "handler": set_backup_mirror,
}
