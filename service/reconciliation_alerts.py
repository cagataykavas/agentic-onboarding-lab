"""Evaluate reconciliation health snapshots against a reviewed alert policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 128 * 1024
MAX_COUNTER = 2_147_483_647
MAX_SECONDS = 31_536_000
MAX_FUTURE_SKEW_SECONDS = 5
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class ReconciliationAlertInputError(ValueError):
    """Alert policy or reconciliation evidence is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class AlertThresholds:
    max_queue_depth: int
    max_expired_leases: int
    max_oldest_unresolved_age_seconds: int
    max_mean_resolution_latency_seconds: float
    max_resolution_latency_seconds: float


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    policy_id: str
    max_snapshot_age_seconds: int
    thresholds: AlertThresholds


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    generated_at: datetime
    queue_depth: int
    queued_tasks: int
    leased_tasks: int
    expired_leases: int
    reclaimed_tasks: int
    resolved_tasks: int
    applied_resolutions: int
    not_applied_resolutions: int
    oldest_unresolved_age_seconds: int | None
    mean_resolution_latency_seconds: float | None
    max_resolution_latency_seconds: float | None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReconciliationAlertInputError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReconciliationAlertInputError("input is not canonical JSON") from exc


def _parse_json(payload: bytes, context: str) -> dict[str, Any]:
    if not 1 <= len(payload) <= MAX_INPUT_BYTES:
        raise ReconciliationAlertInputError(f"{context} exceeds its byte limit")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ReconciliationAlertInputError(f"{context} contains a non-finite number")
            ),
        )
    except ReconciliationAlertInputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationAlertInputError(f"{context} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReconciliationAlertInputError(f"{context} must be an object")
    return value


def _exact_fields(value: dict[str, Any], expected: set[str], context: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing:
        raise ReconciliationAlertInputError(f"{context} missing fields: {', '.join(missing)}")
    if unknown:
        raise ReconciliationAlertInputError(f"{context} has unknown fields: {', '.join(unknown)}")


def _bounded_int(value: object, context: str, maximum: int = MAX_COUNTER) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ReconciliationAlertInputError(f"{context} must be an integer in [0, {maximum}]")
    return value


def _bounded_number(value: object, context: str) -> float:
    if type(value) not in {int, float}:
        raise ReconciliationAlertInputError(f"{context} must be a finite non-negative number")
    converted = float(value)
    if not math.isfinite(converted) or not 0 <= converted <= MAX_SECONDS:
        raise ReconciliationAlertInputError(f"{context} must be a finite non-negative number")
    return converted


def _optional_seconds(value: object, context: str) -> float | None:
    return None if value is None else _bounded_number(value, context)


def _timestamp(value: object, context: str) -> datetime:
    if not isinstance(value, str):
        raise ReconciliationAlertInputError(f"{context} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReconciliationAlertInputError(f"{context} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReconciliationAlertInputError(f"{context} must include an explicit UTC offset")
    return parsed.astimezone(UTC)


def parse_policy(payload: bytes) -> tuple[AlertPolicy, dict[str, Any]]:
    data = _parse_json(payload, "alert policy")
    _exact_fields(
        data,
        {"schema_version", "policy_id", "max_snapshot_age_seconds", "thresholds"},
        "alert policy",
    )
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise ReconciliationAlertInputError("alert policy schema_version must be 1")
    policy_id = data["policy_id"]
    if not isinstance(policy_id, str) or _IDENTIFIER.fullmatch(policy_id) is None:
        raise ReconciliationAlertInputError("policy_id must be a bounded stable identifier")
    maximum_age = _bounded_int(
        data["max_snapshot_age_seconds"], "max_snapshot_age_seconds", maximum=3_600
    )
    if maximum_age == 0:
        raise ReconciliationAlertInputError("max_snapshot_age_seconds must be positive")
    thresholds = data["thresholds"]
    if not isinstance(thresholds, dict):
        raise ReconciliationAlertInputError("thresholds must be an object")
    fields = {
        "max_queue_depth",
        "max_expired_leases",
        "max_oldest_unresolved_age_seconds",
        "max_mean_resolution_latency_seconds",
        "max_resolution_latency_seconds",
    }
    _exact_fields(thresholds, fields, "thresholds")
    policy = AlertPolicy(
        policy_id=policy_id,
        max_snapshot_age_seconds=maximum_age,
        thresholds=AlertThresholds(
            max_queue_depth=_bounded_int(thresholds["max_queue_depth"], "max_queue_depth"),
            max_expired_leases=_bounded_int(thresholds["max_expired_leases"], "max_expired_leases"),
            max_oldest_unresolved_age_seconds=_bounded_int(
                thresholds["max_oldest_unresolved_age_seconds"],
                "max_oldest_unresolved_age_seconds",
                maximum=MAX_SECONDS,
            ),
            max_mean_resolution_latency_seconds=_bounded_number(
                thresholds["max_mean_resolution_latency_seconds"],
                "max_mean_resolution_latency_seconds",
            ),
            max_resolution_latency_seconds=_bounded_number(
                thresholds["max_resolution_latency_seconds"],
                "max_resolution_latency_seconds",
            ),
        ),
    )
    return policy, data


def parse_snapshot(payload: bytes) -> tuple[ReconciliationSnapshot, dict[str, Any]]:
    data = _parse_json(payload, "reconciliation snapshot")
    fields = {
        "schema_version",
        "generated_at",
        "queue_depth",
        "queued_tasks",
        "leased_tasks",
        "expired_leases",
        "reclaimed_tasks",
        "resolved_tasks",
        "applied_resolutions",
        "not_applied_resolutions",
        "oldest_unresolved_age_seconds",
        "mean_resolution_latency_seconds",
        "max_resolution_latency_seconds",
    }
    _exact_fields(data, fields, "reconciliation snapshot")
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise ReconciliationAlertInputError("snapshot schema_version must be 1")
    snapshot = ReconciliationSnapshot(
        generated_at=_timestamp(data["generated_at"], "generated_at"),
        queue_depth=_bounded_int(data["queue_depth"], "queue_depth"),
        queued_tasks=_bounded_int(data["queued_tasks"], "queued_tasks"),
        leased_tasks=_bounded_int(data["leased_tasks"], "leased_tasks"),
        expired_leases=_bounded_int(data["expired_leases"], "expired_leases"),
        reclaimed_tasks=_bounded_int(data["reclaimed_tasks"], "reclaimed_tasks"),
        resolved_tasks=_bounded_int(data["resolved_tasks"], "resolved_tasks"),
        applied_resolutions=_bounded_int(data["applied_resolutions"], "applied_resolutions"),
        not_applied_resolutions=_bounded_int(
            data["not_applied_resolutions"], "not_applied_resolutions"
        ),
        oldest_unresolved_age_seconds=(
            None
            if data["oldest_unresolved_age_seconds"] is None
            else _bounded_int(
                data["oldest_unresolved_age_seconds"],
                "oldest_unresolved_age_seconds",
                maximum=MAX_SECONDS,
            )
        ),
        mean_resolution_latency_seconds=_optional_seconds(
            data["mean_resolution_latency_seconds"], "mean_resolution_latency_seconds"
        ),
        max_resolution_latency_seconds=_optional_seconds(
            data["max_resolution_latency_seconds"], "max_resolution_latency_seconds"
        ),
    )
    if snapshot.queue_depth != snapshot.queued_tasks + snapshot.leased_tasks:
        raise ReconciliationAlertInputError("queue depth does not match queued plus leased tasks")
    if snapshot.expired_leases > snapshot.leased_tasks:
        raise ReconciliationAlertInputError("expired leases exceed leased tasks")
    if snapshot.resolved_tasks != (snapshot.applied_resolutions + snapshot.not_applied_resolutions):
        raise ReconciliationAlertInputError("resolution decision counts are inconsistent")
    total_tasks = snapshot.queue_depth + snapshot.resolved_tasks
    if snapshot.reclaimed_tasks > total_tasks:
        raise ReconciliationAlertInputError("reclaimed tasks exceed total tasks")
    if (snapshot.queue_depth > 0) != (snapshot.oldest_unresolved_age_seconds is not None):
        raise ReconciliationAlertInputError("unresolved age does not match queue state")
    has_latencies = (
        snapshot.mean_resolution_latency_seconds is not None
        and snapshot.max_resolution_latency_seconds is not None
    )
    if (snapshot.resolved_tasks > 0) != has_latencies:
        raise ReconciliationAlertInputError("resolution latencies do not match resolved tasks")
    if has_latencies and (
        snapshot.max_resolution_latency_seconds < snapshot.mean_resolution_latency_seconds
    ):
        raise ReconciliationAlertInputError("maximum latency is below mean latency")
    return snapshot, data


def evaluate_alerts(
    snapshot: ReconciliationSnapshot,
    policy: AlertPolicy,
    *,
    now: datetime,
    snapshot_data: dict[str, Any],
    policy_data: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic alert decision over validated evidence."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ReconciliationAlertInputError("evaluation time must include an explicit UTC offset")
    evaluated_at = now.astimezone(UTC)
    age_seconds = int((evaluated_at - snapshot.generated_at).total_seconds())
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
        raise ReconciliationAlertInputError("snapshot generated_at is in the future")
    observed_age = max(0, age_seconds)
    violations: list[dict[str, Any]] = []

    def threshold(code: str, metric: str, observed: float, maximum: float) -> None:
        if observed > maximum:
            violations.append(
                {"code": code, "metric": metric, "observed": observed, "threshold": maximum}
            )

    threshold(
        "snapshot_stale",
        "snapshot_age_seconds",
        observed_age,
        policy.max_snapshot_age_seconds,
    )
    threshold(
        "queue_depth_exceeded",
        "queue_depth",
        snapshot.queue_depth,
        policy.thresholds.max_queue_depth,
    )
    threshold(
        "expired_leases_exceeded",
        "expired_leases",
        snapshot.expired_leases,
        policy.thresholds.max_expired_leases,
    )
    if snapshot.oldest_unresolved_age_seconds is not None:
        threshold(
            "oldest_unresolved_age_exceeded",
            "oldest_unresolved_age_seconds",
            snapshot.oldest_unresolved_age_seconds,
            policy.thresholds.max_oldest_unresolved_age_seconds,
        )
    if snapshot.mean_resolution_latency_seconds is not None:
        threshold(
            "mean_resolution_latency_exceeded",
            "mean_resolution_latency_seconds",
            snapshot.mean_resolution_latency_seconds,
            policy.thresholds.max_mean_resolution_latency_seconds,
        )
        threshold(
            "max_resolution_latency_exceeded",
            "max_resolution_latency_seconds",
            snapshot.max_resolution_latency_seconds,
            policy.thresholds.max_resolution_latency_seconds,
        )
    violations.sort(key=lambda item: (item["code"], item["metric"]))
    return {
        "schema_version": 1,
        "policy_id": policy.policy_id,
        "evaluated_at": evaluated_at.isoformat(),
        "snapshot_generated_at": snapshot.generated_at.isoformat(),
        "snapshot_age_seconds": observed_age,
        "snapshot_sha256": hashlib.sha256(_canonical_json(snapshot_data)).hexdigest(),
        "policy_sha256": hashlib.sha256(_canonical_json(policy_data)).hexdigest(),
        "decision": "pass" if not violations else "alert",
        "violations": violations,
    }


def _read_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReconciliationAlertInputError(f"{path.name} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ReconciliationAlertInputError(f"{path.name} must be a singly linked regular file")
        if not 1 <= before.st_size <= MAX_INPUT_BYTES:
            raise ReconciliationAlertInputError(f"{path.name} exceeds its byte limit")
        chunks: list[bytes] = []
        remaining = MAX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(payload) != before.st_size or before_identity != after_identity:
            raise ReconciliationAlertInputError(f"{path.name} changed while being read")
        return payload
    finally:
        os.close(descriptor)


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    payload = _canonical_json(report) + b"\n"
    if output is None:
        sys.stdout.write(payload.decode("utf-8"))
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate agent tool-reconciliation health against reviewed thresholds"
    )
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--now", help="evaluation timestamp; defaults to current UTC time")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        snapshot, snapshot_data = parse_snapshot(_read_file(arguments.snapshot))
        policy, policy_data = parse_policy(_read_file(arguments.policy))
        now = _timestamp(arguments.now, "now") if arguments.now else datetime.now(UTC)
        report = evaluate_alerts(
            snapshot,
            policy,
            now=now,
            snapshot_data=snapshot_data,
            policy_data=policy_data,
        )
        _write_report(report, arguments.output)
    except (OSError, ReconciliationAlertInputError) as exc:
        sys.stderr.write(f"reconciliation alert input error: {exc}\n")
        return 2
    return 0 if report["decision"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
