from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from service.store import OnboardingRepository


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class ReviewTask:
    case_id: str
    status: str
    priority: int
    reason: str
    enqueued_at: str
    due_at: str
    lease_owner: str | None = None
    lease_until: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ReviewLeaseConflict(RuntimeError):
    pass


class ReviewQueue:
    """Durable SLA-ordered queue with expiring reviewer leases."""

    def __init__(
        self,
        repository: OnboardingRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.clock = clock

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ReviewTask:
        return ReviewTask(**{field: row[field] for field in ReviewTask.__dataclass_fields__})

    def enqueue(
        self,
        case_id: str,
        *,
        reason: str,
        priority: int = 50,
        sla_minutes: int = 60,
    ) -> ReviewTask:
        now = self.clock()
        priority = max(0, min(priority, 100))
        with self.repository._lock, self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO review_tasks(
                    case_id, status, priority, reason, enqueued_at, due_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    priority = MAX(review_tasks.priority, excluded.priority),
                    reason = excluded.reason,
                    due_at = MIN(review_tasks.due_at, excluded.due_at),
                    updated_at = excluded.updated_at
                WHERE review_tasks.status != 'completed'
                """,
                (
                    case_id,
                    priority,
                    reason,
                    iso(now),
                    iso(now + timedelta(minutes=sla_minutes)),
                    iso(now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._from_row(row)

    def list_ready(self, *, limit: int = 50) -> list[ReviewTask]:
        now = iso(self.clock())
        with self.repository._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_tasks
                WHERE status = 'queued' OR (status = 'leased' AND lease_until <= ?)
                ORDER BY priority DESC, due_at ASC, enqueued_at ASC
                LIMIT ?
                """,
                (now, max(1, min(limit, 500))),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_all(self, *, limit: int = 500) -> list[ReviewTask]:
        with self.repository._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_tasks
                ORDER BY priority DESC, due_at ASC, enqueued_at ASC
                LIMIT ?
                """,
                (max(1, min(limit, 5000)),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def claim_next(self, reviewer: str, *, lease_minutes: int = 15) -> ReviewTask | None:
        now = self.clock()
        with self.repository._lock, self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM review_tasks
                WHERE status = 'queued' OR (status = 'leased' AND lease_until <= ?)
                ORDER BY priority DESC, due_at ASC, enqueued_at ASC
                LIMIT 1
                """,
                (iso(now),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease_until = iso(now + timedelta(minutes=lease_minutes))
            connection.execute(
                """
                UPDATE review_tasks SET status = 'leased', lease_owner = ?, lease_until = ?,
                    updated_at = ? WHERE case_id = ?
                """,
                (reviewer, lease_until, iso(now), row["case_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (row["case_id"],)
            ).fetchone()
            connection.commit()
        assert claimed is not None
        return self._from_row(claimed)

    def require_lease(self, case_id: str, reviewer: str) -> ReviewTask:
        now = iso(self.clock())
        with self.repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
        if row is None or row["status"] != "leased" or row["lease_owner"] != reviewer:
            raise ReviewLeaseConflict("review task is not leased by this reviewer")
        if row["lease_until"] <= now:
            raise ReviewLeaseConflict("review lease has expired")
        return self._from_row(row)

    def complete(self, case_id: str, reviewer: str) -> ReviewTask:
        now = iso(self.clock())
        with self.repository._lock, self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
            if row is None or row["status"] != "leased" or row["lease_owner"] != reviewer:
                connection.rollback()
                raise ReviewLeaseConflict("review task is not leased by this reviewer")
            if row["lease_until"] <= now:
                connection.rollback()
                raise ReviewLeaseConflict("review lease has expired")
            connection.execute(
                """
                UPDATE review_tasks SET status = 'completed', completed_at = ?,
                    updated_at = ? WHERE case_id = ?
                """,
                (now, now, case_id),
            )
            completed = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
            connection.commit()
        assert completed is not None
        return self._from_row(completed)
