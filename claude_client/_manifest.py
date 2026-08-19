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
        logger.warning("Corrupt pull manifest at %s, ignoring", path)
        return {}


def save(directory: Path, entries: dict[str, ManifestEntry]) -> None:
    path = directory / MANIFEST_NAME
    payload = {uuid: asdict(entry) for uuid, entry in entries.items()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def prune_targets(previous: dict[str, ManifestEntry], remote_ids: set[str]) -> list[str]:
    """Filenames recorded for uuids no longer present remotely."""
    return [entry.filename for uuid, entry in previous.items() if uuid not in remote_ids]
