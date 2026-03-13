"""
JSON-based progress tracker with atomic writes.

This module keeps track of which employers have been completed, which
ones failed, and which ones the user chose to skip. Everything gets
saved to a JSON checkpoint file so the automation can pick up where
it left off if you stop it mid-run.

Checkpoint file format (progress/checkpoint.json):

    {
      "last_updated": "2026-02-26T15:30:00Z",
      "completed": ["Company A", "Company B"],
      "failed": {
        "Company C": "not found in search"
      },
      "skipped": ["Company D"]
    }

Important design note:
    Failed employers ARE retried on the next run. Only "completed"
    and "skipped" employers get permanently skipped. So if something
    fails because of a network hiccup, it will get another chance
    next time you run the script.

To start completely fresh, just delete the checkpoint file.

Atomic writes:
    We write to a temp file first, then rename it over the real file.
    This way, if the script crashes mid-save, you never end up with a
    half-written checkpoint file. The old data stays intact.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

# Module-level logger. All checkpoint activity gets logged under this name.
logger = logging.getLogger(__name__)


def _empty_checkpoint() -> dict[str, Any]:
    """Create a fresh, empty checkpoint dictionary.

    This is used when no checkpoint file exists yet (first run) or
    whenever we need a clean starting point.

    Returns:
        A dict with the current timestamp and empty lists/dicts for
        completed, failed, and skipped employers.
    """
    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "completed": [],
        "failed": {},
        "skipped": [],
    }


# ---------------------------------------------------------------------------
# Atomic I/O helpers
# ---------------------------------------------------------------------------

def _save(data: dict[str, Any], path: str | Path) -> None:
    """Write the checkpoint data to disk atomically.

    "Atomically" means we write to a temporary file first and then
    rename it over the real file. That way, if the process gets killed
    in the middle of writing, the old checkpoint is still fine.

    Args:
        data: The full checkpoint dict to save. The "last_updated"
              timestamp gets refreshed automatically before writing.
        path: Where to save the checkpoint file. Usually
              progress/checkpoint.json.
    """
    path = Path(path)

    # Make sure the parent directory exists (creates it if not)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Stamp the current time so we know when this was last saved
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Write to a temp file in the same directory, then rename.
    # Renaming within the same filesystem is atomic on most OSes.
    dir_name = str(path.parent)
    with tempfile.NamedTemporaryFile(
        "w", dir=dir_name, delete=False, suffix=".tmp"
    ) as fh:
        json.dump(data, fh, indent=2)
        tmp_path = fh.name

    os.replace(tmp_path, str(path))
    logger.debug("Checkpoint saved → %s", path)


# ---------------------------------------------------------------------------
# Public API (the functions other modules actually call)
# ---------------------------------------------------------------------------

def load_progress(path: str | Path | None = None) -> dict[str, Any]:
    """Load the checkpoint from disk (or create a fresh one).

    If the checkpoint file does not exist yet, this returns a brand-new
    empty checkpoint instead of crashing. It also back-fills any missing
    keys so older checkpoint files still work.

    Args:
        path: Optional custom path to the checkpoint file. Defaults to
              whatever is set in config.PROGRESS_FILE.

    Returns:
        The checkpoint dict with "completed", "failed", and "skipped"
        keys guaranteed to exist.
    """
    path = Path(path or config.PROGRESS_FILE)
    if not path.exists():
        logger.info("No checkpoint file found — starting fresh")
        return _empty_checkpoint()

    with path.open() as fh:
        data: dict[str, Any] = json.load(fh)

    # Ensure keys exist (handles older formats that might be missing them)
    data.setdefault("completed", [])
    data.setdefault("failed", {})
    data.setdefault("skipped", [])
    logger.info(
        "Loaded checkpoint: %d completed, %d failed, %d skipped",
        len(data["completed"]),
        len(data["failed"]),
        len(data["skipped"]),
    )
    return data


def is_processed(employer_name: str, progress: dict[str, Any] | None = None) -> bool:
    """Check if an employer has already been completed successfully.

    Args:
        employer_name: The employer name to look up.
        progress: An already-loaded checkpoint dict. If not provided,
                  we load it from disk (handy but slower).

    Returns:
        True if the employer is in the "completed" list, False otherwise.
    """
    if progress is None:
        progress = load_progress()
    return employer_name in progress["completed"]


def is_skipped(employer_name: str, progress: dict[str, Any] | None = None) -> bool:
    """Check if an employer was manually skipped by the user.

    Args:
        employer_name: The employer name to look up.
        progress: An already-loaded checkpoint dict. If not provided,
                  we load it from disk.

    Returns:
        True if the employer is in the "skipped" list, False otherwise.
    """
    if progress is None:
        progress = load_progress()
    return employer_name in progress.get("skipped", [])


def mark_completed(
    employer_name: str,
    progress: dict[str, Any],
    path: str | Path | None = None,
) -> None:
    """Mark an employer as successfully completed and save.

    Also cleans up: if the employer was previously in the "failed" or
    "skipped" lists, it gets removed from those since it is now done.

    Args:
        employer_name: The employer that was just processed successfully.
        progress: The current checkpoint dict (will be mutated in place).
        path: Optional custom path for the checkpoint file.
    """
    path = Path(path or config.PROGRESS_FILE)

    # Add to completed (avoid duplicates)
    if employer_name not in progress["completed"]:
        progress["completed"].append(employer_name)

    # Remove from failed / skipped if previously recorded
    progress["failed"].pop(employer_name, None)
    if employer_name in progress.get("skipped", []):
        progress["skipped"].remove(employer_name)

    _save(progress, path)
    logger.info("✅  Marked completed: %s", employer_name)


def mark_failed(
    employer_name: str,
    reason: str,
    progress: dict[str, Any],
    path: str | Path | None = None,
) -> None:
    """Mark an employer as failed (with a reason) and save.

    Failed employers will be retried on the next run. They are NOT
    treated as permanently done.

    Args:
        employer_name: The employer that failed.
        reason: A short explanation of what went wrong (e.g. "not found
                in search").
        progress: The current checkpoint dict (will be mutated in place).
        path: Optional custom path for the checkpoint file.
    """
    path = Path(path or config.PROGRESS_FILE)
    progress["failed"][employer_name] = reason
    _save(progress, path)
    logger.warning("❌  Marked failed: %s — %s", employer_name, reason)


def mark_skipped(
    employer_name: str,
    progress: dict[str, Any],
    path: str | Path | None = None,
) -> None:
    """Mark an employer as user-skipped and save.

    Skipped employers are permanently skipped on future runs (unlike
    failed ones, which get retried).

    Args:
        employer_name: The employer the user chose to skip.
        progress: The current checkpoint dict (will be mutated in place).
        path: Optional custom path for the checkpoint file.
    """
    path = Path(path or config.PROGRESS_FILE)

    # Add to skipped (avoid duplicates)
    skipped = progress.setdefault("skipped", [])
    if employer_name not in skipped:
        skipped.append(employer_name)

    # Remove from failed if previously recorded
    progress["failed"].pop(employer_name, None)

    _save(progress, path)
    logger.info("⏭️  Marked skipped: %s", employer_name)
