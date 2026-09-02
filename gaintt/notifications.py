"""Honker-backed publication of {plan_id, version} signals over the same SQLite file.

The event carries only the identifier and the version, never Plan data (see
docs/adr/0003-honker-collaborative-editing.md). A publish failure must never
fail the write it follows -- callers catch nothing, this module swallows its own
errors and logs them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import honker

logger = logging.getLogger(__name__)

CHANNEL = "plan"


class PlanNotifier:
    """Wraps a honker.Database opened on the application's own SQLite file."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._db: Optional[Any] = None

    def _database(self) -> Any:
        if self._db is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = honker.open(str(self.db_path))
        return self._db

    def bootstrap(self) -> None:
        """Deploy the Honker schema into the app's SQLite file. Safe to call repeatedly."""
        with self._database().transaction() as tx:
            tx.bootstrap_honker_schema()

    def prune(self, older_than_s: int = 60 * 60 * 24 * 7, max_keep: int = 1000) -> int:
        try:
            return self._database().prune_notifications(older_than_s=older_than_s, max_keep=max_keep)
        except Exception:
            logger.warning("honker prune_notifications failed", exc_info=True)
            return 0

    def publish(self, plan_id: str, version: int) -> None:
        """Publish after a successful commit. Never raises -- a lost signal is not fatal."""
        try:
            with self._database().transaction() as tx:
                tx.notify(CHANNEL, {"plan_id": plan_id, "version": version})
        except Exception:
            logger.warning("honker publish failed for plan %s", plan_id, exc_info=True)

    def listen(self, fallback_poll_s: float = 15.0) -> Any:
        """Return Honker's async Listener; the HTTP boundary reconciles snapshot races."""
        return self._database().listen(CHANNEL, fallback_poll_s=fallback_poll_s)
