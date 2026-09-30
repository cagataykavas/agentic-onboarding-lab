from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service.automation_policy_audit import (
    ARTIFACT_SCHEMA,
    MAX_INPUT_BYTES,
    AuditPolicy,
    EvidenceError,
    OutcomeArtifact,
    OutcomeRecord,
    audit_artifact,
    load_artifact,
    main,
)

NOW = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


def _record(index: int, **changes) -> OutcomeRecord:
    values = {
        "case_id": f"case-{index:04d}",
        "confidence": 0.95,
        "correct": True,
        "automated": True,
        "mandatory_review": False,
        "impact_tier": "low" if index < 60 else "medium",
    }
    values.update(changes)
    return OutcomeRecord(**values)


def _artifact(records: tuple[OutcomeRecord, ...] | None = None, **changes) -> OutcomeArtifact:
    values = {
        "generated_at": NOW - timedelta(minutes=1),
        "model_id": "onboarding-risk-v7",
        "policy_id": "automation-policy-2026-09",
        "candidate_threshold": 0.90,
        "records": records or tuple(_record(index) for index in range(100)),
    }
    values.update(changes)
    return OutcomeArtifact(**values)


def _write(path: Path, artifact: OutcomeArtifact | None = None) -> None:
    path.write_text(json.dumps((artifact or _artifact()).canonical_dict()), encoding="utf-8")


def test_healthy_policy_is_accepted_with_uncertainty_metrics() -> None:
    report = audit_artifact(_artifact(), now=NOW)
    assert report["status"] == "accepted"
    assert report["metrics"]["automation_coverage"] == 1.0
    assert report["metrics"]["automated_error_rate"] == 0.0
    assert 0.03 < report["metrics"]["automated_error_rate_upper_bound"] < 0.04
    assert report["metrics"]["impact_tiers"]["medium"]["automated"] == 40


def test_report_is_deterministic_and_does_not_expose_case_ids() -> None:
    first = audit_artifact(_artifact(), now=NOW)
    second = audit_artifact(_artifact(), now=NOW)
    assert first == second
    assert len(first["evidence_sha256"]) == 64
    assert "case-0001" not in json.dumps(first)


@pytest.mark.parametrize(
    "records,code",
    [
        (tuple(_record(index) for index in range(10)), "SAMPLE001"),
        (
            tuple(_record(index, automated=index < 10) for index in range(100)),
            "SAMPLE002",
        ),
        (
            tuple(_record(index, mandatory_review=index == 0) for index in range(100)),
            "AUTH001",
        ),
        (
            tuple(_record(index, confidence=0.80 if index == 0 else 0.95) for index in range(100)),
            "POLICY001",
        ),
        (
            tuple(_record(index, correct=index >= 10) for index in range(100)),
            "QUALITY003",
        ),
    ],
)
def test_release_guardrails_reject(records: tuple[OutcomeRecord, ...], code: str) -> None:
    report = audit_artifact(_artifact(records), now=NOW)
    assert report["status"] == "rejected"
    assert code in {finding["code"] for finding in report["findings"]}


def test_calibration_and_brier_fail_independently() -> None:
    records = tuple(_record(index, confidence=0.60) for index in range(100))
    policy = replace(
        AuditPolicy(),
        max_brier_score=1.0,
        max_automated_error_upper_bound=1.0,
    )
    report = audit_artifact(_artifact(records, candidate_threshold=0.5), policy, NOW)
    assert {finding["code"] for finding in report["findings"]} == {"QUALITY002"}

    policy = replace(policy, max_expected_calibration_error=1.0, max_brier_score=0.10)
    report = audit_artifact(_artifact(records, candidate_threshold=0.5), policy, NOW)
    assert {finding["code"] for finding in report["findings"]} == {"QUALITY001"}


def test_medium_impact_requires_enough_evidence_and_bounded_error() -> None:
    sparse = tuple(
        _record(index, impact_tier="medium" if index < 5 else "low") for index in range(100)
    )
    report = audit_artifact(_artifact(sparse), now=NOW)
    assert "SAMPLE003" in {finding["code"] for finding in report["findings"]}

    risky = tuple(
        _record(index, impact_tier="medium", correct=index != 0) for index in range(20)
    ) + tuple(_record(index + 20, impact_tier="low") for index in range(80))
    policy = replace(AuditPolicy(), max_automated_error_upper_bound=1.0)
    report = audit_artifact(_artifact(risky), policy, NOW)
    assert "QUALITY004" in {finding["code"] for finding in report["findings"]}


def test_stale_and_future_artifacts_are_policy_rejections() -> None:
    stale = audit_artifact(_artifact(generated_at=NOW - timedelta(days=2)), now=NOW)
    future = audit_artifact(_artifact(generated_at=NOW + timedelta(minutes=1)), now=NOW)
    assert "TIME002" in {finding["code"] for finding in stale["findings"]}
    assert "TIME001" in {finding["code"] for finding in future["findings"]}


@pytest.mark.parametrize(
    "change,match",
    [
        ({"schema_version": "wrong"}, "schema"),
        ({"generated_at": "2026-09-30T18:00:00"}, "timezone"),
        ({"candidate_threshold": True}, "numeric"),
        ({"model_id": "not allowed spaces"}, "identifier"),
    ],
)
def test_artifact_header_validation_fails_closed(change: dict, match: str) -> None:
    values = _artifact().canonical_dict()
    values.update(change)
    with pytest.raises(EvidenceError, match=match):
        OutcomeArtifact.from_dict(values)


def test_record_schema_identity_and_types_fail_closed() -> None:
    values = _artifact().canonical_dict()
    values["records"][1]["case_id"] = values["records"][0]["case_id"]
    with pytest.raises(EvidenceError, match="unique"):
        OutcomeArtifact.from_dict(values)

    values = _artifact().canonical_dict()
    values["records"][0]["automated"] = 1
    with pytest.raises(EvidenceError, match="boolean"):
        OutcomeArtifact.from_dict(values)

    values = _artifact().canonical_dict()
    values["records"][0]["unexpected"] = "field"
    with pytest.raises(EvidenceError, match="schema"):
        OutcomeArtifact.from_dict(values)


def test_loader_rejects_duplicate_nonfinite_oversized_and_symlink(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
    with pytest.raises(EvidenceError, match="duplicate"):
        load_artifact(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"confidence":NaN}', encoding="utf-8")
    with pytest.raises(EvidenceError, match="non-finite"):
        load_artifact(nonfinite)

    oversized = tmp_path / "oversized.json"
    oversized.write_text("x" * (MAX_INPUT_BYTES + 1), encoding="utf-8")
    with pytest.raises(EvidenceError, match="budget"):
        load_artifact(oversized)

    valid = tmp_path / "valid.json"
    linked = tmp_path / "linked.json"
    _write(valid)
    linked.symlink_to(valid)
    with pytest.raises(EvidenceError, match="non-symlink"):
        load_artifact(linked)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def test_cli_distinguishes_acceptance_rejection_and_malformed_input(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("service.automation_policy_audit.datetime", _FixedDateTime)
    accepted = tmp_path / "accepted.json"
    rejected = tmp_path / "rejected.json"
    malformed = tmp_path / "malformed.json"
    _write(accepted)
    _write(
        rejected,
        _artifact(tuple(_record(index, correct=index >= 10) for index in range(100))),
    )
    malformed.write_text("{", encoding="utf-8")

    assert main(["--input", str(accepted), "--output", str(tmp_path / "a.json")]) == 0
    assert main(["--input", str(rejected), "--output", str(tmp_path / "r.json")]) == 2
    assert main(["--input", str(malformed), "--output", str(tmp_path / "m.json")]) == 3
    assert json.loads((tmp_path / "a.json").read_text())["status"] == "accepted"


def test_artifact_schema_constant_is_serialized() -> None:
    assert _artifact().canonical_dict()["schema_version"] == ARTIFACT_SCHEMA
