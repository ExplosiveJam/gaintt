"""Application service: SQLite persistence, versions, atomic Turns, and chat history."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .domain import Plan, Task, apply_mutations, seed_plan
from .notifications import PlanNotifier

logger = logging.getLogger(__name__)


class PlanNotFoundError(LookupError):
    pass


class StalePlanError(RuntimeError):
    """The caller attempted to write an old Plan version."""

    code = "stale_plan"


@dataclass
class TurnResult:
    plan: Plan
    changes: List[Dict[str, Any]]
    turn_id: str


class PlanService:
    def __init__(self, db_path: Path | str, notifier: Optional[PlanNotifier] = None) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.notifier = notifier or PlanNotifier(self.db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_members (
                    plan_id TEXT NOT NULL REFERENCES plans(id),
                    member_id TEXT NOT NULL,
                    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (plan_id, member_id)
                );
                CREATE INDEX IF NOT EXISTS plan_members_member_idx ON plan_members(member_id);
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id),
                    base_version INTEGER NOT NULL,
                    resulting_version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    reverted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL REFERENCES plans(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        self.notifier.bootstrap()

    def create_plan(
        self,
        member_id: str,
        tasks: Optional[Dict[str, Task]] = None,
        plan: Optional[Plan] = None,
    ) -> Plan:
        created = plan or seed_plan(str(uuid.uuid4()))
        if tasks is not None:
            created = Plan(
                id=created.id,
                name=created.name,
                plan_start=created.plan_start,
                tasks=tasks,
                version=created.version,
            )
        created.schedule()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO plans (id, payload, version) VALUES (?, ?, ?)",
                (created.id, json.dumps(created.to_dict(), ensure_ascii=False), created.version),
            )
            connection.execute(
                "INSERT INTO plan_members (plan_id, member_id) VALUES (?, ?)",
                (created.id, member_id),
            )
        return created

    def _row_plan(self, connection: sqlite3.Connection, plan_id: str) -> Plan:
        row = connection.execute("SELECT payload FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PlanNotFoundError(f"Plan '{plan_id}' does not exist")
        return Plan.from_dict(json.loads(row["payload"]))

    def get_plan(self, plan_id: str) -> Plan:
        with self._connect() as connection:
            return self._row_plan(connection, plan_id)

    def get_member_plan(self, member_id: str) -> Optional[Plan]:
        """The most recently joined Plan for this member, or None."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT plans.payload FROM plans
                JOIN plan_members ON plan_members.plan_id = plans.id
                WHERE plan_members.member_id = ?
                ORDER BY plan_members.joined_at DESC, plan_members.rowid DESC
                LIMIT 1
                """,
                (member_id,),
            ).fetchone()
            return Plan.from_dict(json.loads(row["payload"])) if row else None

    def get_or_create_plan(self, member_id: str) -> Plan:
        existing = self.get_member_plan(member_id)
        return existing or self.create_plan(member_id)

    def is_member(self, plan_id: str, member_id: str) -> bool:
        with self._connect() as connection:
            plan_row = connection.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if plan_row is None:
                raise PlanNotFoundError(f"Plan '{plan_id}' does not exist")
            member_row = connection.execute(
                "SELECT 1 FROM plan_members WHERE plan_id = ? AND member_id = ?",
                (plan_id, member_id),
            ).fetchone()
            return member_row is not None

    def add_member(self, plan_id: str, member_id: str) -> None:
        """Grant membership, refreshing joined_at so the plan becomes this member's current one.

        Kept deliberately (see P1-1 in the ticket): bumping joined_at on every
        visit is exactly what made get_member_plan's "most recently joined" order
        racy for /api/export and /api/turns when several tabs/Plans share one
        cookie -- those two endpoints now take plan_id explicitly instead of
        relying on this ordering (is_plan_member), so they no longer read this
        table's order at all. get_or_create_plan (bare GET /api/plan, used only
        when a visit carries no plan_id in the URL) still legitimately wants "the
        Plan this member most recently opened via a capability link" -- that is
        this method's only remaining reader, and the semantics there are correct:
        an anonymous visitor returning to "/" should land back on whatever Plan
        they last opened, capability link or not.
        """
        with self._lock, self._connect() as connection:
            plan_row = connection.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if plan_row is None:
                raise PlanNotFoundError(f"Plan '{plan_id}' does not exist")
            connection.execute(
                """
                INSERT INTO plan_members (plan_id, member_id, joined_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(plan_id, member_id) DO UPDATE SET joined_at = excluded.joined_at
                """,
                (plan_id, member_id),
            )

    def member_count(self, plan_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM plan_members WHERE plan_id = ?", (plan_id,)).fetchone()
            return int(row["n"])

    def plan_id_for_turn(self, turn_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT plan_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise PlanNotFoundError(f"Turn '{turn_id}' does not exist")
            return str(row["plan_id"])

    def create_imported_plan(self, member_id: str, plan: Plan) -> Plan:
        """Create a new Plan from an import for a visitor without an active Plan."""
        created = plan.clone()
        created.id = str(uuid.uuid4())
        created.version = 1
        return self.create_plan(member_id, plan=created)

    def import_into_plan(self, plan_id: str, base_version: int, plan: Plan) -> Plan:
        """Replace an existing Plan's content in place (same id, next version).

        Used by /api/import: an Excel import must land on the Plan every member
        already has open, not mint a fresh one only the importer can see (P1-3).
        Content changes, identity and membership do not -- so URLs and SSE
        subscriptions keep working unchanged, and the new version is published
        exactly like any other write.
        """
        replacement = plan.clone()
        replacement.id = plan_id
        with self._lock, self._connect() as connection:
            current = self._row_plan(connection, plan_id)
            if current.version != int(base_version):
                raise StalePlanError(f"Plan version is {current.version}, but the request was based on {base_version}")
            replacement.version = current.version + 1
            replacement.schedule()
            connection.execute(
                "UPDATE plans SET payload = ?, version = ? WHERE id = ?",
                (json.dumps(replacement.to_dict(), ensure_ascii=False), replacement.version, plan_id),
            )
        self._publish_after_commit(plan_id, replacement.version)
        return replacement

    def _publish_after_commit(self, plan_id: str, version: int) -> None:
        """Publish a {plan_id, version} signal. A publish failure must never fail the write it follows."""
        try:
            self.notifier.publish(plan_id, version)
        except Exception:
            logger.warning("publishing plan %s version %s failed; the write already committed", plan_id, version, exc_info=True)

    def apply_turn(self, plan_id: str, base_version: int, mutations: Iterable[Dict[str, Any]]) -> TurnResult:
        mutation_list = [dict(mutation) for mutation in mutations]
        with self._lock, self._connect() as connection:
            current = self._row_plan(connection, plan_id)
            if current.version != int(base_version):
                raise StalePlanError(f"Plan version is {current.version}, but the request was based on {base_version}")
            candidate = current.clone()
            changes = apply_mutations(candidate, mutation_list)
            candidate.version = current.version + 1
            turn_id = str(uuid.uuid4())
            connection.execute(
                "UPDATE plans SET payload = ?, version = ? WHERE id = ?",
                (json.dumps(candidate.to_dict(), ensure_ascii=False), candidate.version, plan_id),
            )
            connection.execute(
                "INSERT INTO turns (id, plan_id, base_version, resulting_version, snapshot, changes) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    plan_id,
                    current.version,
                    candidate.version,
                    json.dumps(current.to_dict(), ensure_ascii=False),
                    json.dumps(changes, ensure_ascii=False),
                ),
            )
            result = TurnResult(candidate, changes, turn_id)
        self._publish_after_commit(plan_id, candidate.version)
        return result

    def revert_turn(self, turn_id: str) -> TurnResult:
        with self._lock, self._connect() as connection:
            turn = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if turn is None:
                raise PlanNotFoundError(f"Turn '{turn_id}' does not exist")
            current = self._row_plan(connection, turn["plan_id"])
            if turn["reverted"]:
                raise StalePlanError("This Turn has already been reverted")
            if current.version != turn["resulting_version"]:
                raise StalePlanError("The Plan changed after this Turn; revert is no longer available")
            restored = Plan.from_dict(json.loads(turn["snapshot"]))
            restored.version = current.version + 1
            connection.execute(
                "UPDATE plans SET payload = ?, version = ? WHERE id = ?",
                (json.dumps(restored.to_dict(), ensure_ascii=False), restored.version, current.id),
            )
            connection.execute("UPDATE turns SET reverted = 1 WHERE id = ?", (turn_id,))
            plan_id = current.id
            result = TurnResult(
                restored,
                [{"kind": "reverted", "label": "Ход откатан"}],
                turn_id,
            )
        self._publish_after_commit(plan_id, restored.version)
        return result

    def can_revert(self, turn_id: str) -> bool:
        with self._connect() as connection:
            turn = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if turn is None or turn["reverted"]:
                return False
            current = self._row_plan(connection, turn["plan_id"])
            return current.version == turn["resulting_version"]

    def record_chat(self, plan_id: str, role: str, content: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO chat_messages (plan_id, role, content) VALUES (?, ?, ?)",
                (plan_id, role, content),
            )

    def chat_history(self, plan_id: str, limit: int = 12) -> List[Dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role, content FROM chat_messages WHERE plan_id = ? ORDER BY id DESC LIMIT ?",
                (plan_id, limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def list_turns(self, plan_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            current = self._row_plan(connection, plan_id)
            rows = connection.execute(
                "SELECT id, resulting_version, changes, reverted FROM turns WHERE plan_id = ? ORDER BY rowid",
                (plan_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "version": row["resulting_version"],
                "changes": json.loads(row["changes"]),
                "can_revert": not row["reverted"] and current.version == row["resulting_version"],
            }
            for row in rows
        ]
