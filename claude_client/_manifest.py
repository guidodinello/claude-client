"""Sidecar manifest for incremental, prunable pulls.

Lives inside each pulled directory so it travels with the mirror (and gets committed
alongside it in a git-versioned backup). Keyed by remote uuid rather than filename so a
deleted or renamed item's local path stays recoverable even when the filename scheme
only disambiguates on collision (see `docs.py::_resolve_doc_filenames`).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

MANIFEST_NAME = ".claude-pull-manifest.json"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    filename: str
    updated_at: str


def load(directory: Path) -> dict[str, ManifestEntry]:
    """Load the manifest for a pulled directory. Returns {} if absent or corrupt."""
    path = directory / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {uuid: ManifestEntry(**entry) for uuid, entry in raw.items()}
    except (json.JSONDecodeError, TypeError, KeyError):
        backup = path.with_name(path.name + ".corrupt")
        path.replace(backup)
        logger.error(
            "Corrupt pull manifest, backed up to %s and starting fresh (loses incremental"
            " pull/prune tracking for this directory until the next full pull)",
            backup,
        )
        return {}


def save(directory: Path, entries: dict[str, ManifestEntry]) -> None:
    """Write the manifest atomically (write to a temp file, then rename over it)."""
    path = directory / MANIFEST_NAME
    payload = {uuid: asdict(entry) for uuid, entry in entries.items()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def prune_targets(
    previous: dict[str, ManifestEntry], current: dict[str, ManifestEntry]
) -> list[tuple[str, str]]:
    """
    (uuid, filename) pairs from a prior manifest that no longer belong to any uuid
    confirmed present this run.

    `current` must contain only uuids actually confirmed present this run (fetched,
    skipped-as-unchanged, or carried forward after a fetch failure) — NOT the full
    manifest to be saved, which may also carry forward stale entries for uuids absent
    from the remote list entirely (see callers). Covers both genuine deletions (uuid
    absent from `current`) and renames (uuid still present but resolved to a different
    filename this run — the old filename is now an orphan). Never returns a filename
    that some *other* uuid claims in `current`: that guards against pruning a file that
    was just written under a reused/colliding name (e.g. a freed filename picked up by
    a new doc, or a slugify collision across orgs).
    """
    current_filenames = {entry.filename for entry in current.values()}
    targets = []
    for uuid, entry in previous.items():
        current_entry = current.get(uuid)
        if current_entry is not None and current_entry.filename == entry.filename:
            continue
        if entry.filename in current_filenames:
            continue
        targets.append((uuid, entry.filename))
    return targets
