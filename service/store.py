from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

from onboarding import (
    Actor,
    AuditEvent,
    EvidenceRecord,
    EvidenceStatus,
    OnboardingCase,
    Stage,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS onboarding_cases (
    case_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    customer_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onboarding_stage ON onboarding_cases(stage);

CREATE TABLE IF NOT EXISTS review_tasks (
    case_id TEXT PRIMARY KEY REFERENCES onboarding_cases(case_id),
    status TEXT NOT NULL CHECK(status IN ('queued', 'leased', 'completed')),
    priority INTEGER NOT NULL,
    reason TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_queue
    ON review_tasks(status, priority DESC, due_at ASC, enqueued_at ASC);

CREATE TABLE IF NOT EXISTS command_receipts (
    command_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES onboarding_cases(case_id),
    request_hash TEXT NOT NULL,
    case_version INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_command_case ON command_receipts(case_id, created_at);
"""


class VersionConflict(RuntimeError):
    """Raised when a caller tries to persist a stale case snapshot."""


class DuplicateCase(RuntimeError):
    """Raised when a case identifier is already present."""


class IdempotencyConflict(RuntimeError):
    """Raised when a command key is reused for another case."""


def case_to_dict(case: OnboardingCase) -> dict:
    payload = asdict(case)
    payload["stage"] = case.stage.value
    payload["risk_flags"] = sorted(case.risk_flags)
    for evidence in payload["evidence"]:
        evidence["status"] = (
            evidence["status"].value
            if hasattr(evidence["status"], "value")
            else evidence["status"]
        )
    for event in payload["audit"]:
        for key in ("actor", "stage_before", "stage_after"):
            value = event[key]
            event[key] = value.value if hasattr(value, "value") else value
    return payload


def case_from_dict(payload: dict) -> OnboardingCase:
    evidence = [
        EvidenceRecord(
            evidence_id=row["evidence_id"],
            kind=row["kind"],
            value=row["value"],
            source=row["source"],
            status=EvidenceStatus(row["status"]),
            confidence=float(row["confidence"]),
            collected_at=row["collected_at"],
            missing_fields=tuple(row.get("missing_fields", [])),
            notes=tuple(row.get("notes", [])),
        )
        for row in payload.get("evidence", [])
    ]
    audit = [
        AuditEvent(
            event_id=int(row["event_id"]),
            case_id=row["case_id"],
            actor=Actor(row["actor"]),
            event_type=row["event_type"],
            stage_before=Stage(row["stage_before"]),
            stage_after=Stage(row["stage_after"]),
            timestamp=row["timestamp"],
            details=dict(row.get("details", {})),
        )
        for row in payload.get("audit", [])
    ]
    return OnboardingCase(
        case_id=payload["case_id"],
        product=payload["product"],
        customer_type=payload["customer_type"],
        stage=Stage(payload.get("stage", Stage.STARTED.value)),
        consent=bool(payload.get("consent", False)),
        profile=dict(payload.get("profile", {})),
        evidence=evidence,
        requested_fields=list(payload.get("requested_fields", [])),
        risk_flags=set(payload.get("risk_flags", [])),
        human_review_required=bool(payload.get("human_review_required", False)),
        decision_reason=payload.get("decision_reason"),
        decision_outcome=payload.get("decision_outcome"),
        audit=audit,
        version=int(payload.get("version", 0)),
    )


class OnboardingRepository:
    def __init__(self, database_path: str | Path = "onboarding.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(onboarding_cases)")
            }
            if "version" not in columns:
                connection.execute(
                    "ALTER TABLE onboarding_cases ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                )
            receipt_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(command_receipts)")
            }
            if "request_hash" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE command_receipts ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def create(self, case: OnboardingCase) -> OnboardingCase:
        case.version = 1
        payload = json.dumps(case_to_dict(case), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO onboarding_cases(
                        case_id, product, customer_type, stage, version, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case.case_id,
                        case.product,
                        case.customer_type,
                        case.stage.value,
                        case.version,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateCase(case.case_id) from exc
            connection.commit()
        return case

    def save(self, case: OnboardingCase, *, expected_version: int) -> OnboardingCase:
        next_version = expected_version + 1
        case.version = next_version
        payload = json.dumps(case_to_dict(case), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE onboarding_cases
                SET product = ?, customer_type = ?, stage = ?, version = ?, payload = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ? AND version = ?
                """,
                (
                    case.product,
                    case.customer_type,
                    case.stage.value,
                    next_version,
                    payload,
                    case.case_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                case.version = expected_version
                raise VersionConflict(
                    f"case {case.case_id} changed after version {expected_version}"
                )
            connection.commit()
        return case

    def mutate(
        self,
        case_id: str,
        operation: Callable[[OnboardingCase], object],
        *,
        expected_version: int | None = None,
        command_id: str | None = None,
        command_fingerprint: str | None = None,
        review_lease_owner: str | None = None,
    ) -> tuple[OnboardingCase, object]:
        """Load, mutate and compare-and-swap a case under one write transaction.

        The operation runs after BEGIN IMMEDIATE, so state, embedded audit events and
        version advance are committed together. A caller-provided version also protects
        clients from acting on a stale UI snapshot.
        """
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if command_id:
                receipt = connection.execute(
                    """
                    SELECT case_id, request_hash, result FROM command_receipts
                    WHERE command_id = ?
                    """,
                    (command_id,),
                ).fetchone()
                if receipt is not None:
                    if receipt["case_id"] != case_id:
                        connection.rollback()
                        raise IdempotencyConflict(
                            "idempotency key was already used for another case"
                        )
                    if receipt["request_hash"] != (command_fingerprint or ""):
                        connection.rollback()
                        raise IdempotencyConflict(
                            "idempotency key was reused with a different command"
                        )
                    current = connection.execute(
                        "SELECT payload, version FROM onboarding_cases WHERE case_id = ?",
                        (case_id,),
                    ).fetchone()
                    assert current is not None
                    replayed_case = case_from_dict(json.loads(current["payload"]))
                    replayed_case.version = int(current["version"])
                    connection.commit()
                    return replayed_case, {
                        "idempotent_replay": True,
                        "transition": json.loads(receipt["result"]),
                    }
            row = connection.execute(
                "SELECT payload, version FROM onboarding_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(case_id)
            stored_version = int(row["version"])
            if expected_version is not None and expected_version != stored_version:
                connection.rollback()
                raise VersionConflict(
                    f"expected version {expected_version}, current version is {stored_version}"
                )
            case = case_from_dict(json.loads(row["payload"]))
            case.version = stored_version
            lease_now = datetime.now(UTC).isoformat()
            if review_lease_owner is not None:
                lease = connection.execute(
                    """
                    SELECT status, lease_owner, lease_until FROM review_tasks
                    WHERE case_id = ?
                    """,
                    (case_id,),
                ).fetchone()
                if (
                    lease is None
                    or lease["status"] != "leased"
                    or lease["lease_owner"] != review_lease_owner
                    or lease["lease_until"] <= lease_now
                ):
                    connection.rollback()
                    raise ValueError("review task is not actively leased by this reviewer")
            result = operation(case)
            case.version = stored_version + 1
            payload = json.dumps(case_to_dict(case), sort_keys=True, separators=(",", ":"))
            cursor = connection.execute(
                """
                UPDATE onboarding_cases
                SET product = ?, customer_type = ?, stage = ?, version = ?, payload = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ? AND version = ?
                """,
                (
                    case.product,
                    case.customer_type,
                    case.stage.value,
                    case.version,
                    payload,
                    case.case_id,
                    stored_version,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise VersionConflict(f"case {case_id} was concurrently modified")
            if case.stage is Stage.HUMAN_REVIEW:
                reason = ",".join(sorted(case.risk_flags)) or "policy_escalation"
                connection.execute(
                    """
                    INSERT INTO review_tasks(
                        case_id, status, priority, reason, enqueued_at, due_at, updated_at
                    ) VALUES (?, 'queued', ?, ?, CURRENT_TIMESTAMP,
                              datetime('now', '+60 minutes'), CURRENT_TIMESTAMP)
                    ON CONFLICT(case_id) DO UPDATE SET
                        priority = MAX(review_tasks.priority, excluded.priority),
                        reason = excluded.reason,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE review_tasks.status != 'completed'
                    """,
                    (case.case_id, 80 if case.risk_flags else 50, reason),
                )
            if command_id:
                serialized_result = asdict(result) if is_dataclass(result) else result
                connection.execute(
                    """
                    INSERT INTO command_receipts(
                        command_id, case_id, request_hash, case_version, result
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        case.case_id,
                        command_fingerprint or "",
                        case.version,
                        json.dumps(serialized_result, sort_keys=True, separators=(",", ":")),
                    ),
                )
            if review_lease_owner is not None:
                cursor = connection.execute(
                    """
                    UPDATE review_tasks
                    SET status = 'completed', completed_at = ?, updated_at = ?
                    WHERE case_id = ? AND status = 'leased' AND lease_owner = ?
                      AND lease_until > ?
                    """,
                    (
                        lease_now,
                        lease_now,
                        case.case_id,
                        review_lease_owner,
                        lease_now,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise ValueError("review lease expired before the decision committed")
            connection.commit()
            return case, result

    def upsert(self, case: OnboardingCase) -> None:
        """Compatibility API; create new cases and CAS-update loaded cases."""
        existing = self.get(case.case_id)
        if existing is None:
            self.create(case)
        else:
            self.save(case, expected_version=case.version or existing.version)

    def get(self, case_id: str) -> OnboardingCase | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, version FROM onboarding_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        case = case_from_dict(json.loads(row["payload"]))
        case.version = int(row["version"])
        return case

    def list(self, stage: Stage | None = None, limit: int = 100) -> list[OnboardingCase]:
        query = "SELECT payload FROM onboarding_cases"
        params: list[object] = []
        if stage is not None:
            query += " WHERE stage = ?"
            params.append(stage.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [case_from_dict(json.loads(row["payload"])) for row in rows]
