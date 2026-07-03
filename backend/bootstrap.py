"""
RECAP - Storage bootstrap / schema versioning.

The vector index and FTS index are *derived* from SQLite (the single source of
truth), but their on-disk layout is versioned. When the schema version changes
(e.g. the LanceDB table drops its text payload, or FTS switches to external
content), old local data is incompatible. Rather than ship fragile migrations,
we do a clean rebuild: wipe the local stores so they are recreated under the new
schema. The page content is re-indexed automatically as the user browses.

Bump SCHEMA_VERSION whenever the on-disk shape of any store changes.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from backend.config import Settings

logger = logging.getLogger(__name__)

# v2: single-source-of-truth storage - external-content FTS5 + vector-only LanceDB
# v3: added meta table (embedding fingerprint) + pluggable embedding provider
SCHEMA_VERSION = 3

_MARKER_NAME = ".schema_version"


def ensure_schema_version(settings: Settings) -> None:
    """Wipe local stores and recreate the version marker if the schema changed.

    Safe to call on every startup: it is a no-op once the marker matches
    SCHEMA_VERSION. Only the derived stores + the SQLite DB are removed; user
    settings (.env) and code are untouched.
    """
    data_dir = settings.data_path
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / _MARKER_NAME

    current: int | None = None
    if marker.exists():
        try:
            current = int(marker.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            current = None

    if current == SCHEMA_VERSION:
        return

    # Remove the SQLite DB (+ WAL/SHM sidecars), the vector store, and the KG dir.
    db_path = settings.db_path
    targets = [
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
        settings.vector_store_path,
        settings.kg_path,
    ]
    wiped = []
    failed = []
    for path in targets:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
            elif path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("Could not remove %s during schema rebuild: %s", path, e)
        # Confirm it is actually gone (rmtree can leave files behind on Windows locks)
        if path.exists():
            failed.append(path.name)
        else:
            wiped.append(path.name)

    if failed:
        # Do NOT advance the marker - a partially-wiped store must be retried on the
        # next startup rather than being treated as an up-to-date (but dirty) rebuild.
        raise RuntimeError(
            "Schema rebuild incomplete; could not remove: "
            + ", ".join(failed)
            + ". Close any process using the data dir and restart."
        )

    marker.write_text(str(SCHEMA_VERSION), encoding="utf-8")
    logger.warning(
        "Storage schema changed (%s -> %s): local index rebuilt clean. "
        "Removed: %s. Pages will re-index as you browse.",
        current, SCHEMA_VERSION, ", ".join(wiped) or "(nothing)",
    )
