from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from service.lease_tokens import digest_lease_token, issue_lease_token, lease_token_matches
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
    lease_token: str | None = field(default=None, repr=False, compare=False)

    def to_dict(self, *, include_token: bool = False) -> dict[str, object]:
        payload = asdict(self)
        token = payload.pop("lease_token")
        if include_token:
            payload["lease_token"] = token
        return payload


class ReviewLeaseConflict(RuntimeError):
    pass


class ReviewQueue:
    """Durable SLA-ordered queue with expiring reviewer leases."""

    def __init__(
        self,
        repository: OnboardingRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], str] = issue_lease_token,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.token_factory = token_factory

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ReviewTask:
        return ReviewTask(
            **{
                field_name: row[field_name]
                for field_name in ReviewTask.__dataclass_fields__
                if field_name != "lease_token"
            }
        )

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
            lease_token = self.token_factory()
            lease_token_hash = digest_lease_token(lease_token)
            lease_until = iso(now + timedelta(minutes=lease_minutes))
            connection.execute(
                """
                UPDATE review_tasks SET status = 'leased', lease_owner = ?, lease_until = ?,
                    lease_token_hash = ?, updated_at = ? WHERE case_id = ?
                """,
                (reviewer, lease_until, lease_token_hash, iso(now), row["case_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (row["case_id"],)
            ).fetchone()
            connection.commit()
        assert claimed is not None
        task = self._from_row(claimed)
        return ReviewTask(**task.to_dict(), lease_token=lease_token)

    def require_lease(self, case_id: str, reviewer: str, lease_token: str) -> ReviewTask:
        now = iso(self.clock())
        with self.repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
        if (
            row is None
            or row["status"] != "leased"
            or row["lease_owner"] != reviewer
            or not lease_token_matches(row["lease_token_hash"], lease_token)
        ):
            raise ReviewLeaseConflict("review lease credential is invalid")
        if row["lease_until"] <= now:
            raise ReviewLeaseConflict("review lease has expired")
        return self._from_row(row)

    def complete(self, case_id: str, reviewer: str, lease_token: str) -> ReviewTask:
        now = iso(self.clock())
        with self.repository._lock, self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "leased"
                or row["lease_owner"] != reviewer
                or not lease_token_matches(row["lease_token_hash"], lease_token)
            ):
                connection.rollback()
                raise ReviewLeaseConflict("review lease credential is invalid")
            if row["lease_until"] <= now:
                connection.rollback()
                raise ReviewLeaseConflict("review lease has expired")
            connection.execute(
                """
                UPDATE review_tasks SET status = 'completed', completed_at = ?,
                    lease_token_hash = NULL, updated_at = ? WHERE case_id = ?
                """,
                (now, now, case_id),
            )
            completed = connection.execute(
                "SELECT * FROM review_tasks WHERE case_id = ?", (case_id,)
            ).fetchone()
            connection.commit()
        assert completed is not None
        return self._from_row(completed)
