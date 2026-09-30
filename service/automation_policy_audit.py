"""Audit confidence-based onboarding automation before a policy is promoted."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA = "onboarding-automation-outcomes/v1"
REPORT_SCHEMA = "onboarding-automation-audit/v1"
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 10_000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
IMPACT_TIERS = ("low", "medium", "high")


class EvidenceError(ValueError):
    """Raised when an artifact cannot be trusted or safely evaluated."""


@dataclass(frozen=True)
class AuditPolicy:
    min_labeled_records: int = 50
    min_automated_records: int = 30
    max_evidence_age_seconds: int = 86_400
    max_future_skew_seconds: int = 30
    calibration_bins: int = 10
    max_brier_score: float = 0.15
    max_expected_calibration_error: float = 0.10
    max_automated_error_upper_bound: float = 0.10
    min_medium_impact_automated_records: int = 20
    max_medium_impact_error_upper_bound: float = 0.20

    def __post_init__(self) -> None:
        integers = (
            self.min_labeled_records,
            self.min_automated_records,
            self.max_evidence_age_seconds,
            self.max_future_skew_seconds,
            self.calibration_bins,
            self.min_medium_impact_automated_records,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in integers
        ):
            raise ValueError("integer policy values must be non-negative integers")
        if self.min_labeled_records < 1 or self.min_automated_records < 1:
            raise ValueError("record minimums must be positive")
        if self.min_medium_impact_automated_records < 1:
            raise ValueError("medium-impact record minimum must be positive")
        if not 2 <= self.calibration_bins <= 100:
            raise ValueError("calibration_bins must be in [2, 100]")
        for name, value in (
            ("max_brier_score", self.max_brier_score),
            ("max_expected_calibration_error", self.max_expected_calibration_error),
            ("max_automated_error_upper_bound", self.max_automated_error_upper_bound),
            ("max_medium_impact_error_upper_bound", self.max_medium_impact_error_upper_bound),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")


@dataclass(frozen=True)
class OutcomeRecord:
    case_id: str
    confidence: float
    correct: bool
    automated: bool
    mandatory_review: bool
    impact_tier: str

    def canonical_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutcomeArtifact:
    generated_at: datetime
    model_id: str
    policy_id: str
    candidate_threshold: float
    records: tuple[OutcomeRecord, ...]

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ARTIFACT_SCHEMA,
            "generated_at": self.generated_at.isoformat().replace("+00:00", "Z"),
            "model_id": self.model_id,
            "policy_id": self.policy_id,
            "candidate_threshold": self.candidate_threshold,
            "records": [record.canonical_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> OutcomeArtifact:
        _exact_keys(
            values,
            {
                "schema_version",
                "generated_at",
                "model_id",
                "policy_id",
                "candidate_threshold",
                "records",
            },
            "artifact",
        )
        if values["schema_version"] != ARTIFACT_SCHEMA:
            raise EvidenceError("unsupported artifact schema")
        generated_at = _timestamp(values["generated_at"])
        model_id = _identifier(values["model_id"], "model_id")
        policy_id = _identifier(values["policy_id"], "policy_id")
        candidate_threshold = _probability(values["candidate_threshold"], "candidate_threshold")
        raw_records = values["records"]
        if not isinstance(raw_records, list) or not 1 <= len(raw_records) <= MAX_RECORDS:
            raise EvidenceError(f"records must contain between 1 and {MAX_RECORDS} items")
        records: list[OutcomeRecord] = []
        case_ids: set[str] = set()
        for index, raw in enumerate(raw_records):
            _exact_keys(
                raw,
                {
                    "case_id",
                    "confidence",
                    "correct",
                    "automated",
                    "mandatory_review",
                    "impact_tier",
                },
                f"records[{index}]",
            )
            case_id = _identifier(raw["case_id"], f"records[{index}].case_id")
            if case_id in case_ids:
                raise EvidenceError("case_id values must be unique")
            case_ids.add(case_id)
            impact_tier = raw["impact_tier"]
            if impact_tier not in IMPACT_TIERS:
                raise EvidenceError(f"records[{index}].impact_tier is unsupported")
            records.append(
                OutcomeRecord(
                    case_id=case_id,
                    confidence=_probability(raw["confidence"], f"records[{index}].confidence"),
                    correct=_boolean(raw["correct"], f"records[{index}].correct"),
                    automated=_boolean(raw["automated"], f"records[{index}].automated"),
                    mandatory_review=_boolean(
                        raw["mandatory_review"], f"records[{index}].mandatory_review"
                    ),
                    impact_tier=impact_tier,
                )
            )
        return cls(generated_at, model_id, policy_id, candidate_threshold, tuple(records))


@dataclass(frozen=True)
class Finding:
    code: str
    message: str


def _exact_keys(value: Any, expected: set[str], field: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceError(f"{field} keys do not match the schema")


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise EvidenceError(f"{field} is not a bounded identifier")
    return value


def _probability(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{field} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise EvidenceError(f"{field} must be finite and in [0, 1]")
    return normalized


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{field} must be a boolean")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError("generated_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise EvidenceError("generated_at is not valid ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError("generated_at must include a timezone")
    return parsed.astimezone(UTC)


def _wilson_upper(errors: int, total: int, z: float = 1.959963984540054) -> float:
    if total < 1:
        return 1.0
    rate = errors / total
    denominator = 1.0 + z * z / total
    center = rate + z * z / (2.0 * total)
    margin = z * math.sqrt(rate * (1.0 - rate) / total + z * z / (4.0 * total * total))
    return min(1.0, (center + margin) / denominator)


def _calibration(
    records: tuple[OutcomeRecord, ...], bins: int
) -> tuple[float, list[dict[str, Any]]]:
    grouped: list[list[OutcomeRecord]] = [[] for _ in range(bins)]
    for record in records:
        index = min(int(record.confidence * bins), bins - 1)
        grouped[index].append(record)
    ece = 0.0
    output: list[dict[str, Any]] = []
    for index, group in enumerate(grouped):
        if not group:
            continue
        average_confidence = sum(record.confidence for record in group) / len(group)
        accuracy = sum(record.correct for record in group) / len(group)
        ece += len(group) / len(records) * abs(average_confidence - accuracy)
        output.append(
            {
                "lower": index / bins,
                "upper": (index + 1) / bins,
                "count": len(group),
                "average_confidence": average_confidence,
                "accuracy": accuracy,
            }
        )
    return ece, output


def audit_artifact(
    artifact: OutcomeArtifact,
    policy: AuditPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = policy or AuditPolicy()
    if artifact.generated_at.tzinfo is None or artifact.generated_at.utcoffset() is None:
        raise EvidenceError("generated_at must include a timezone")
    artifact = OutcomeArtifact.from_dict(artifact.canonical_dict())
    evaluated_source = now or datetime.now(UTC)
    if evaluated_source.tzinfo is None or evaluated_source.utcoffset() is None:
        raise EvidenceError("evaluation time must include a timezone")
    evaluated_at = evaluated_source.astimezone(UTC)
    age = evaluated_at - artifact.generated_at
    records = artifact.records
    automated = tuple(record for record in records if record.automated)
    automated_errors = sum(not record.correct for record in automated)
    mandatory_violations = sum(record.automated and record.mandatory_review for record in records)
    threshold_violations = sum(
        record.automated and record.confidence < artifact.candidate_threshold for record in records
    )
    brier_score = sum((record.confidence - float(record.correct)) ** 2 for record in records) / len(
        records
    )
    ece, bins = _calibration(records, policy.calibration_bins)
    automated_upper = _wilson_upper(automated_errors, len(automated))
    findings: list[Finding] = []

    if age < -timedelta(seconds=policy.max_future_skew_seconds):
        findings.append(Finding("TIME001", "artifact timestamp exceeds allowed future skew"))
    elif age > timedelta(seconds=policy.max_evidence_age_seconds):
        findings.append(Finding("TIME002", "artifact is older than the evidence-age budget"))
    if len(records) < policy.min_labeled_records:
        findings.append(Finding("SAMPLE001", "labeled record count is below policy"))
    if len(automated) < policy.min_automated_records:
        findings.append(Finding("SAMPLE002", "automated record count is below policy"))
    if mandatory_violations:
        findings.append(Finding("AUTH001", "mandatory-review records were automated"))
    if threshold_violations:
        findings.append(Finding("POLICY001", "automation occurred below the candidate threshold"))
    if brier_score > policy.max_brier_score:
        findings.append(Finding("QUALITY001", "Brier score exceeds policy"))
    if ece > policy.max_expected_calibration_error:
        findings.append(Finding("QUALITY002", "expected calibration error exceeds policy"))
    if automated_upper > policy.max_automated_error_upper_bound:
        findings.append(Finding("QUALITY003", "automated error-rate upper bound exceeds policy"))

    impact_metrics: dict[str, dict[str, Any]] = {}
    for tier in IMPACT_TIERS:
        tier_records = tuple(record for record in records if record.impact_tier == tier)
        tier_automated = tuple(record for record in tier_records if record.automated)
        tier_errors = sum(not record.correct for record in tier_automated)
        impact_metrics[tier] = {
            "records": len(tier_records),
            "automated": len(tier_automated),
            "automated_errors": tier_errors,
            "automated_error_rate": tier_errors / len(tier_automated) if tier_automated else None,
            "automated_error_rate_upper_bound": _wilson_upper(tier_errors, len(tier_automated))
            if tier_automated
            else None,
        }
    medium = impact_metrics["medium"]
    if 0 < medium["automated"] < policy.min_medium_impact_automated_records:
        findings.append(Finding("SAMPLE003", "medium-impact automated sample is below policy"))
    elif (
        medium["automated"] >= policy.min_medium_impact_automated_records
        and medium["automated_error_rate_upper_bound"] > policy.max_medium_impact_error_upper_bound
    ):
        findings.append(Finding("QUALITY004", "medium-impact error upper bound exceeds policy"))

    findings.sort(key=lambda finding: finding.code)
    artifact_bytes = json.dumps(
        artifact.canonical_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "status": "accepted" if not findings else "rejected",
        "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "model_id": artifact.model_id,
        "policy_id": artifact.policy_id,
        "candidate_threshold": artifact.candidate_threshold,
        "audit_policy": asdict(policy),
        "metrics": {
            "labeled_records": len(records),
            "automated_records": len(automated),
            "automation_coverage": len(automated) / len(records),
            "automated_errors": automated_errors,
            "automated_error_rate": automated_errors / len(automated) if automated else None,
            "automated_error_rate_upper_bound": automated_upper,
            "error_bound_confidence_level": 0.95,
            "mandatory_review_violations": mandatory_violations,
            "threshold_violations": threshold_violations,
            "brier_score": brier_score,
            "expected_calibration_error": ece,
            "calibration_bins": bins,
            "impact_tiers": impact_metrics,
        },
        "findings": [asdict(finding) for finding in findings],
        "limitations": [
            "Retrospective labels must represent the deployment population and decision costs.",
            "Calibration and Wilson bounds do not detect dataset shift or prove causal safety.",
        ],
    }
    report_bytes = json.dumps(
        report, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    report["evidence_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    return report


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise EvidenceError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise EvidenceError(f"non-finite JSON value: {value}")


def load_artifact(path: Path) -> OutcomeArtifact:
    metadata = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EvidenceError("artifact must be a regular, non-symlink file")
    if metadata.st_size > MAX_INPUT_BYTES:
        raise EvidenceError(f"artifact exceeds the {MAX_INPUT_BYTES}-byte budget")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise EvidenceError("artifact changed before it could be read")
        payload = handle.read(MAX_INPUT_BYTES + 1)
        final = os.fstat(handle.fileno())
    if len(payload) > MAX_INPUT_BYTES:
        raise EvidenceError(f"artifact exceeds the {MAX_INPUT_BYTES}-byte budget")
    if (opened.st_size, opened.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise EvidenceError("artifact changed while it was being read")
    try:
        values = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("artifact is not valid JSON") from error
    if not isinstance(values, dict):
        raise EvidenceError("artifact root must be an object")
    return OutcomeArtifact.from_dict(values)


def _atomic_write(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(values, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit an onboarding automation policy")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-labeled-records", type=int, default=50)
    parser.add_argument("--min-automated-records", type=int, default=30)
    parser.add_argument("--max-brier-score", type=float, default=0.15)
    parser.add_argument("--max-ece", type=float, default=0.10)
    parser.add_argument("--max-error-upper-bound", type=float, default=0.10)
    values = parser.parse_args(argv)
    try:
        policy = AuditPolicy(
            min_labeled_records=values.min_labeled_records,
            min_automated_records=values.min_automated_records,
            max_brier_score=values.max_brier_score,
            max_expected_calibration_error=values.max_ece,
            max_automated_error_upper_bound=values.max_error_upper_bound,
        )
        report = audit_artifact(load_artifact(values.input), policy)
        _atomic_write(values.output, report)
    except (EvidenceError, OSError, TypeError, ValueError) as error:
        print(json.dumps({"status": "malformed", "error": str(error)}))
        return 3
    print(json.dumps({"status": report["status"], "evidence_sha256": report["evidence_sha256"]}))
    return 0 if report["status"] == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
