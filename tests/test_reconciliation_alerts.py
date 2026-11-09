from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from service.reconciliation_alerts import (
    ReconciliationAlertInputError,
    evaluate_alerts,
    main,
    parse_policy,
    parse_snapshot,
)

NOW = datetime(2026, 11, 9, 21, 0, 30, tzinfo=UTC)


def snapshot_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": "2026-11-09T21:00:00+00:00",
        "queue_depth": 2,
        "queued_tasks": 1,
        "leased_tasks": 1,
        "expired_leases": 0,
        "reclaimed_tasks": 0,
        "resolved_tasks": 2,
        "applied_resolutions": 1,
        "not_applied_resolutions": 1,
        "oldest_unresolved_age_seconds": 60,
        "mean_resolution_latency_seconds": 10.5,
        "max_resolution_latency_seconds": 20.0,
    }


def policy_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_id": "reconciliation-production-v1",
        "max_snapshot_age_seconds": 60,
        "thresholds": {
            "max_queue_depth": 5,
            "max_expired_leases": 0,
            "max_oldest_unresolved_age_seconds": 120,
            "max_mean_resolution_latency_seconds": 30.0,
            "max_resolution_latency_seconds": 60.0,
        },
    }


def encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def evaluate(snapshot_value=None, policy_value=None, *, now=NOW):
    snapshot_source = snapshot_data() if snapshot_value is None else snapshot_value
    policy_source = policy_data() if policy_value is None else policy_value
    snapshot, parsed_snapshot = parse_snapshot(encoded(snapshot_source))
    policy, parsed_policy = parse_policy(encoded(policy_source))
    return evaluate_alerts(
        snapshot,
        policy,
        now=now,
        snapshot_data=parsed_snapshot,
        policy_data=parsed_policy,
    )


def test_reviewed_thresholds_pass_healthy_snapshot() -> None:
    report = evaluate()

    assert report["decision"] == "pass"
    assert report["policy_id"] == "reconciliation-production-v1"
    assert report["snapshot_age_seconds"] == 30
    assert report["violations"] == []
    assert len(report["snapshot_sha256"]) == 64
    assert len(report["policy_sha256"]) == 64


def test_all_threshold_breaches_are_stable_and_machine_readable() -> None:
    snapshot = snapshot_data()
    snapshot.update(
        generated_at="2026-11-09T20:59:29+00:00",
        queue_depth=6,
        queued_tasks=5,
        leased_tasks=1,
        expired_leases=1,
        oldest_unresolved_age_seconds=121,
        mean_resolution_latency_seconds=31.0,
        max_resolution_latency_seconds=61.0,
    )

    report = evaluate(snapshot)

    assert report["decision"] == "alert"
    assert [item["code"] for item in report["violations"]] == [
        "expired_leases_exceeded",
        "max_resolution_latency_exceeded",
        "mean_resolution_latency_exceeded",
        "oldest_unresolved_age_exceeded",
        "queue_depth_exceeded",
        "snapshot_stale",
    ]
    assert all(
        set(item) == {"code", "metric", "observed", "threshold"} for item in report["violations"]
    )


def test_thresholds_are_inclusive() -> None:
    snapshot = snapshot_data()
    snapshot.update(
        queue_depth=5,
        queued_tasks=4,
        oldest_unresolved_age_seconds=120,
        mean_resolution_latency_seconds=30.0,
        max_resolution_latency_seconds=60.0,
    )
    report = evaluate(snapshot, now=datetime(2026, 11, 9, 21, 1, tzinfo=UTC))

    assert report["decision"] == "pass"


def test_empty_snapshot_does_not_invent_latency_or_age_alerts() -> None:
    snapshot = snapshot_data()
    snapshot.update(
        queue_depth=0,
        queued_tasks=0,
        leased_tasks=0,
        resolved_tasks=0,
        applied_resolutions=0,
        not_applied_resolutions=0,
        oldest_unresolved_age_seconds=None,
        mean_resolution_latency_seconds=None,
        max_resolution_latency_seconds=None,
    )

    assert evaluate(snapshot)["decision"] == "pass"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"queue_depth": 3}, "queue depth"),
        ({"expired_leases": 2}, "expired leases"),
        ({"resolved_tasks": 3}, "decision counts"),
        ({"reclaimed_tasks": 5}, "reclaimed tasks"),
        ({"oldest_unresolved_age_seconds": None}, "unresolved age"),
        ({"mean_resolution_latency_seconds": None}, "latencies"),
        ({"mean_resolution_latency_seconds": 21.0}, "maximum latency"),
    ],
)
def test_snapshot_invariants_fail_closed(updates: dict[str, object], message: str) -> None:
    snapshot = snapshot_data()
    snapshot.update(updates)

    with pytest.raises(ReconciliationAlertInputError, match=message):
        parse_snapshot(encoded(snapshot))


def test_future_snapshot_is_invalid_but_stale_snapshot_is_an_alert() -> None:
    future = snapshot_data()
    future["generated_at"] = "2026-11-09T21:00:36+00:00"
    with pytest.raises(ReconciliationAlertInputError, match="future"):
        evaluate(future)

    stale = snapshot_data()
    stale["generated_at"] = "2026-11-09T20:58:00+00:00"
    assert [item["code"] for item in evaluate(stale)["violations"]] == ["snapshot_stale"]


def test_policy_and_snapshot_reject_ambiguous_json() -> None:
    with pytest.raises(ReconciliationAlertInputError, match="duplicate"):
        parse_policy(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ReconciliationAlertInputError, match="unknown fields"):
        policy = policy_data()
        policy["unreviewed"] = True
        parse_policy(encoded(policy))
    with pytest.raises(ReconciliationAlertInputError, match="non-finite"):
        parse_snapshot(encoded(snapshot_data()).replace(b"10.5", b"NaN"))


def test_policy_rejects_boolean_and_nonfinite_thresholds() -> None:
    policy = policy_data()
    policy["thresholds"]["max_queue_depth"] = True
    with pytest.raises(ReconciliationAlertInputError, match="integer"):
        parse_policy(encoded(policy))

    policy = policy_data()
    policy["thresholds"]["max_resolution_latency_seconds"] = float("inf")
    with pytest.raises(ReconciliationAlertInputError, match="non-finite"):
        parse_policy(encoded(policy))


def write_inputs(tmp_path: Path, snapshot=None, policy=None) -> tuple[Path, Path]:
    snapshot_path = tmp_path / "snapshot.json"
    policy_path = tmp_path / "policy.json"
    snapshot_path.write_text(json.dumps(snapshot_data() if snapshot is None else snapshot))
    policy_path.write_text(json.dumps(policy_data() if policy is None else policy))
    return snapshot_path, policy_path


def test_cli_exit_codes_and_atomic_report(tmp_path: Path) -> None:
    snapshot_path, policy_path = write_inputs(tmp_path)
    output = tmp_path / "decision.json"
    arguments = [
        str(snapshot_path),
        str(policy_path),
        "--now",
        "2026-11-09T21:00:30Z",
        "--output",
        str(output),
    ]

    assert main(arguments) == 0
    assert json.loads(output.read_text())["decision"] == "pass"
    assert not list(tmp_path.glob(".decision.json.*"))

    snapshot = snapshot_data()
    snapshot["expired_leases"] = 1
    snapshot_path.write_text(json.dumps(snapshot))
    assert main(arguments) == 1
    assert json.loads(output.read_text())["decision"] == "alert"

    snapshot_path.write_text("{}")
    assert main(arguments) == 2


def test_cli_rejects_symlinked_and_hard_linked_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_path, policy_path = write_inputs(tmp_path)
    symlink = tmp_path / "snapshot-link.json"
    symlink.symlink_to(snapshot_path)
    assert main([str(symlink), str(policy_path)]) == 2
    assert "unavailable" in capsys.readouterr().err

    hardlink = tmp_path / "snapshot-hardlink.json"
    os.link(snapshot_path, hardlink)
    assert main([str(hardlink), str(policy_path)]) == 2
    assert "singly linked" in capsys.readouterr().err


def test_canonical_evidence_identity_is_format_independent() -> None:
    compact = encoded(snapshot_data())
    expanded = json.dumps(snapshot_data(), indent=2, sort_keys=False).encode()
    snapshot_a, data_a = parse_snapshot(compact)
    snapshot_b, data_b = parse_snapshot(expanded)
    policy, policy_source = parse_policy(encoded(policy_data()))

    report_a = evaluate_alerts(
        snapshot_a,
        policy,
        now=NOW,
        snapshot_data=data_a,
        policy_data=policy_source,
    )
    report_b = evaluate_alerts(
        snapshot_b,
        policy,
        now=NOW,
        snapshot_data=data_b,
        policy_data=policy_source,
    )

    assert report_a["snapshot_sha256"] == report_b["snapshot_sha256"]
