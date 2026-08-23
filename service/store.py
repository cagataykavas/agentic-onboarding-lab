from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
import threading

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
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onboarding_stage ON onboarding_cases(stage);
"""


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
        audit=audit,
    )


class OnboardingRepository:
    def __init__(self, database_path: str | Path = "onboarding.db") -> None:
        self.database_path = str(database_path)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def upsert(self, case: OnboardingCase) -> None:
        payload = json.dumps(case_to_dict(case), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO onboarding_cases(case_id, product, customer_type, stage, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    product = excluded.product,
                    customer_type = excluded.customer_type,
                    stage = excluded.stage,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (case.case_id, case.product, case.customer_type, case.stage.value, payload),
            )

    def get(self, case_id: str) -> OnboardingCase | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM onboarding_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        return case_from_dict(json.loads(row["payload"]))

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
