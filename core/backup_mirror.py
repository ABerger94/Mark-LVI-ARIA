"""
core/backup_mirror.py — the rolling backups, copied off the machine.

WHY THIS EXISTS
    Phase G1 and the config hardening gave both stores a rolling
    rotation of five known-good backups — and put them in
    memory/backups/ and config/backups/, on the same disk, in the
    same folder tree as the files they protect. That cures the
    torn write, the bad save, the corrupt JSON. It cures nothing
    about a dead drive, a stolen laptop, or a folder deleted in a
    spring clean: the original and all five of its backups share
    every one of those fates.

    So: when a store writes a fresh snapshot into its rotation,
    the rotation is also copied to a mirror directory the user
    chooses (config key "backup_mirror_dir") — a folder synced by
    something else: a Google Drive or OneDrive folder, a NAS
    mount, a second drive. Pointed there, the mirror always holds
    a copy of the full current rotation for both stores, and
    restoring from it is copying the files back by hand — no code
    path in this app reads the mirror, which is deliberate: a
    backup the app can read is a backup the app can corrupt.

THE RULES
    * NEVER BREAK THE PRIMARY PATH. Mirroring runs after the real
      snapshot has landed, swallows every failure, and reports a
      broken mirror exactly once per process as an incident
      (kind "backup_mirror_failed"); after that it degrades
      silently — a dead mirror must not become a per-save incident
      generator, and must never cost a save.
    * FILENAMES ARE THE STORES' OWN. long_term.N.json and
      api_keys.N.json do not collide, so one flat directory holds
      both rotations, newest slot first, exactly as on disk here.
    * A COPY IS VERIFIED BEFORE IT COUNTS. Sizes must match after
      the copy; a short copy is a failed mirror, not a small one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_failed = False   # the once-per-process incident latch


def mirror_dir() -> str:
    """The configured mirror directory, or '' when unset. Read
    through config_manager's getter; any failure reads as unset —
    a mirror lookup must never break the save that triggered it."""
    try:
        from memory.config_manager import get_backup_mirror_dir
        return get_backup_mirror_dir()
    except Exception:
        return ""


def mirror_backups(slots) -> bool:
    """Copy every existing file in `slots` (one store's rotation)
    into the mirror directory. True when the mirror is unset
    (nothing to do — not a failure) or every copy landed and
    verified; False when a copy failed. Never raises."""
    global _failed
    dest_dir = mirror_dir()
    if not dest_dir:
        return True
    try:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for slot in slots:
            slot = Path(slot)
            if not slot.exists():
                continue
            target = dest / slot.name
            shutil.copyfile(slot, target)
            if target.stat().st_size != slot.stat().st_size:
                raise OSError(
                    f"mirror copy of {slot.name} is short "
                    f"({target.stat().st_size} of "
                    f"{slot.stat().st_size} bytes)")
        return True
    except Exception as e:
        if not _failed:
            _failed = True
            try:
                from core import incidents
                incidents.record(
                    "backup_mirror", "backup_mirror_failed",
                    f"could not mirror backups to {dest_dir!r}: {e} "
                    f"— the on-machine backups are unaffected; "
                    f"further failures this run are silent")
            except Exception:
                pass
        return False


def reset_latch_for_tests() -> None:
    global _failed
    _failed = False
