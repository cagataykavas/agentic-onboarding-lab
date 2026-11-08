from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import service.api as api_module
from onboarding import OnboardingCase, Stage
from service.reviews import ReviewQueue
from service.store import ExecutionConflict, OnboardingRepository
from service.tool_admission import ToolProposal, default_tool_policy

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _admit_and_claim(
    repository: OnboardingRepository,
    *,
    call_id: str = "call-reconcile-1",
) -> tuple[str, str, int]:
    case = OnboardingCase(
        case_id="ONB-RECONCILE-1",
        product="account",
        customer_type="retail",
        stage=Stage.IDENTITY_PENDING,
        consent=True,
    )
    repository.create(case)
    admission = repository.admit_tool_call(
        case.case_id,
        ToolProposal(
            call_id=call_id,
            tool_name="identity_document_check",
            expected_case_version=1,
            arguments={"evidence_id": "identity-1"},
        ),
        default_tool_policy(),
    )
    claim = repository.claim_tool_execution(
        case.case_id,
        call_id,
        admission_decision_sha256=admission.decision_sha256,
        worker_id="executor-a",
        policy=default_tool_policy(),
        lease_seconds=30,
        now=NOW,
    )
    return case.case_id, admission.decision_sha256, claim.attempt


def _enqueue_expired(
    repository: OnboardingRepository,
    case_id: str,
    decision_sha256: str,
    *,
    call_id: str = "call-reconcile-1",
) -> None:
    with pytest.raises(ExecutionConflict, match="requires outcome reconciliation"):
        repository.claim_tool_execution(
            case_id,
            call_id,
            admission_decision_sha256=decision_sha256,
            worker_id="executor-b",
            policy=default_tool_policy(),
            now=NOW + timedelta(seconds=31),
        )


def test_expired_execution_enqueues_one_privacy_reduced_task(tmp_path: Path) -> None:
    database = tmp_path / "queued.db"
    repository = OnboardingRepository(database)
    case_id, decision_sha256, attempt = _admit_and_claim(repository)

    _enqueue_expired(repository, case_id, decision_sha256)
    _enqueue_expired(repository, case_id, decision_sha256)
    task = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-sensitive-id",
        now=NOW + timedelta(seconds=31),
    )

    assert task is not None
    assert task.execution_attempt == attempt
    assert task.reconciliation_attempt == 1
    assert task.should_reconcile
    assert task.reconciler_sha256 != "reconciler-sensitive-id"
    assert "reconciler-sensitive-id" not in str(task.to_dict())
    assert (
        repository.claim_tool_reconciliation(
            reconciler_id="competitor",
            now=NOW + timedelta(seconds=32),
        )
        is None
    )
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM tool_reconciliations").fetchone()[0]
    assert count == 1


def test_active_execution_never_enters_reconciliation_queue(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "active.db")
    case_id, decision_sha256, _ = _admit_and_claim(repository)

    with pytest.raises(ExecutionConflict, match="actively leased"):
        repository.claim_tool_execution(
            case_id,
            "call-reconcile-1",
            admission_decision_sha256=decision_sha256,
            worker_id="executor-b",
            policy=default_tool_policy(),
            now=NOW + timedelta(seconds=1),
        )

    assert (
        repository.claim_tool_reconciliation(
            reconciler_id="reconciler-a",
            now=NOW + timedelta(seconds=1),
        )
        is None
    )


def test_expired_reconciler_lease_can_be_reclaimed_with_higher_fence(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "reclaim.db")
    case_id, decision_sha256, _ = _admit_and_claim(repository)
    _enqueue_expired(repository, case_id, decision_sha256)
    first = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-a",
        lease_seconds=10,
        now=NOW + timedelta(seconds=31),
    )
    second = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-b",
        lease_seconds=10,
        now=NOW + timedelta(seconds=42),
    )

    assert first is not None and second is not None
    assert second.reconciliation_attempt == first.reconciliation_attempt + 1
    with pytest.raises(ExecutionConflict, match="stale"):
        repository.resolve_tool_reconciliation(
            "call-reconcile-1",
            execution_attempt=1,
            reconciliation_attempt=first.reconciliation_attempt,
            reconciler_id="reconciler-a",
            decision="not_applied",
            policy=default_tool_policy(),
            now=NOW + timedelta(seconds=43),
        )


def test_wrong_or_expired_reconciler_cannot_resolve_task(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "reconciler-fence.db")
    case_id, decision_sha256, execution_attempt = _admit_and_claim(repository)
    _enqueue_expired(repository, case_id, decision_sha256)
    task = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-a",
        lease_seconds=10,
        now=NOW + timedelta(seconds=31),
    )
    assert task is not None

    with pytest.raises(ExecutionConflict, match="another worker"):
        repository.resolve_tool_reconciliation(
            "call-reconcile-1",
            execution_attempt=execution_attempt,
            reconciliation_attempt=task.reconciliation_attempt,
            reconciler_id="reconciler-b",
            decision="not_applied",
            policy=default_tool_policy(),
            now=NOW + timedelta(seconds=32),
        )
    with pytest.raises(ExecutionConflict, match="expired"):
        repository.resolve_tool_reconciliation(
            "call-reconcile-1",
            execution_attempt=execution_attempt,
            reconciliation_attempt=task.reconciliation_attempt,
            reconciler_id="reconciler-a",
            decision="not_applied",
            policy=default_tool_policy(),
            now=NOW + timedelta(seconds=41),
        )


def test_not_applied_resolution_is_replayable_and_authorizes_higher_execution_fence(
    tmp_path: Path,
) -> None:
    repository = OnboardingRepository(tmp_path / "not-applied.db")
    case_id, decision_sha256, execution_attempt = _admit_and_claim(repository)
    _enqueue_expired(repository, case_id, decision_sha256)
    task = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-a",
        now=NOW + timedelta(seconds=31),
    )
    assert task is not None

    resolved = repository.resolve_tool_reconciliation(
        "call-reconcile-1",
        execution_attempt=execution_attempt,
        reconciliation_attempt=task.reconciliation_attempt,
        reconciler_id="reconciler-a",
        decision="not_applied",
        policy=default_tool_policy(),
        now=NOW + timedelta(seconds=32),
    )
    replayed = repository.resolve_tool_reconciliation(
        "call-reconcile-1",
        execution_attempt=execution_attempt,
        reconciliation_attempt=task.reconciliation_attempt,
        reconciler_id="reconciler-a",
        decision="not_applied",
        policy=default_tool_policy(),
        now=NOW + timedelta(seconds=33),
    )
    retry = repository.claim_tool_execution(
        case_id,
        "call-reconcile-1",
        admission_decision_sha256=decision_sha256,
        worker_id="executor-b",
        policy=default_tool_policy(),
        now=NOW + timedelta(seconds=34),
    )

    assert resolved.decision == "not_applied"
    assert replayed.replayed and replayed.receipt_sha256 == resolved.receipt_sha256
    assert retry.should_execute and retry.attempt == execution_attempt + 1
    with pytest.raises(ExecutionConflict, match="different resolution"):
        repository.resolve_tool_reconciliation(
            "call-reconcile-1",
            execution_attempt=execution_attempt,
            reconciliation_attempt=task.reconciliation_attempt,
            reconciler_id="reconciler-a",
            decision="applied",
            policy=default_tool_policy(),
            result={"status": "verified", "provider_reference": "provider-1"},
            now=NOW + timedelta(seconds=35),
        )


def test_applied_resolution_finalizes_original_attempt_without_storing_provider_values(
    tmp_path: Path,
) -> None:
    database = tmp_path / "applied.db"
    repository = OnboardingRepository(database)
    case_id, decision_sha256, execution_attempt = _admit_and_claim(repository)
    changed = repository.get(case_id)
    assert changed is not None
    changed.stage = Stage.PROFILE_PENDING
    repository.save(changed, expected_version=1)
    _enqueue_expired(repository, case_id, decision_sha256)
    task = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-a",
        now=NOW + timedelta(seconds=31),
    )
    assert task is not None
    result = {"status": "verified", "provider_reference": "private-provider-ref"}

    resolved = repository.resolve_tool_reconciliation(
        "call-reconcile-1",
        execution_attempt=execution_attempt,
        reconciliation_attempt=task.reconciliation_attempt,
        reconciler_id="reconciler-a",
        decision="applied",
        policy=default_tool_policy(),
        result=result,
        now=NOW + timedelta(seconds=32),
    )
    terminal = repository.claim_tool_execution(
        case_id,
        "call-reconcile-1",
        admission_decision_sha256=decision_sha256,
        worker_id="any-worker",
        policy=default_tool_policy(),
        now=NOW + timedelta(seconds=33),
    )

    assert resolved.decision == "applied"
    assert resolved.result_sha256 is not None
    assert resolved.terminal_execution_receipt_sha256 == terminal.receipt_sha256
    assert terminal.state == "succeeded" and terminal.attempt == execution_attempt
    assert terminal.replayed and not terminal.should_execute
    with sqlite3.connect(database) as connection:
        stored = " ".join(
            str(value)
            for row in connection.execute(
                "SELECT result_hash, reconciliation_receipt FROM tool_reconciliations"
            )
            for value in row
        )
    assert result["provider_reference"] not in stored


def test_invalid_applied_result_fails_closed_and_preserves_lease(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "invalid.db")
    case_id, decision_sha256, execution_attempt = _admit_and_claim(repository)
    _enqueue_expired(repository, case_id, decision_sha256)
    task = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-a",
        now=NOW + timedelta(seconds=31),
    )
    assert task is not None

    with pytest.raises(ValueError, match="result"):
        repository.resolve_tool_reconciliation(
            "call-reconcile-1",
            execution_attempt=execution_attempt,
            reconciliation_attempt=task.reconciliation_attempt,
            reconciler_id="reconciler-a",
            decision="applied",
            policy=default_tool_policy(),
            result={"status": "invented", "provider_reference": "provider-1"},
            now=NOW + timedelta(seconds=32),
        )
    resolved = repository.resolve_tool_reconciliation(
        "call-reconcile-1",
        execution_attempt=execution_attempt,
        reconciliation_attempt=task.reconciliation_attempt,
        reconciler_id="reconciler-a",
        decision="not_applied",
        policy=default_tool_policy(),
        now=NOW + timedelta(seconds=33),
    )
    assert resolved.decision == "not_applied"


def test_concurrent_expiry_observers_still_create_one_task(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "concurrent.db")
    case_id, decision_sha256, _ = _admit_and_claim(repository)

    def observe(index: int) -> str:
        try:
            repository.claim_tool_execution(
                case_id,
                "call-reconcile-1",
                admission_decision_sha256=decision_sha256,
                worker_id=f"executor-{index}",
                policy=default_tool_policy(),
                now=NOW + timedelta(seconds=31),
            )
        except ExecutionConflict as error:
            return str(error)
        return "unexpected authorization"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(observe, range(16)))

    assert set(outcomes) == {"expired execution requires outcome reconciliation"}
    first = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-a",
        now=NOW + timedelta(seconds=31),
    )
    second = repository.claim_tool_reconciliation(
        reconciler_id="reconciler-b",
        now=NOW + timedelta(seconds=31),
    )
    assert first is not None and second is None


def test_api_claims_and_resolves_reconciliation_without_echoing_provider_value(
    tmp_path: Path,
) -> None:
    api_module.repository = OnboardingRepository(tmp_path / "api.db")
    api_module.review_queue = ReviewQueue(api_module.repository)
    case_id, decision_sha256, _ = _admit_and_claim(
        api_module.repository,
        call_id="api-reconcile-1",
    )
    _enqueue_expired(
        api_module.repository,
        case_id,
        decision_sha256,
        call_id="api-reconcile-1",
    )
    client = TestClient(api_module.app)

    claimed = client.post(
        "/tool-reconciliations/claim",
        json={"reconciler_id": "api-reconciler", "lease_seconds": 60},
    )
    assert claimed.status_code == 200
    task = claimed.json()
    provider_reference = "api-private-provider-ref"
    resolved = client.post(
        "/tool-reconciliations/api-reconcile-1/resolve",
        json={
            "execution_attempt": task["execution_attempt"],
            "reconciliation_attempt": task["reconciliation_attempt"],
            "reconciler_id": "api-reconciler",
            "decision": "applied",
            "result": {"status": "verified", "provider_reference": provider_reference},
        },
    )

    assert resolved.status_code == 200
    assert resolved.json()["decision"] == "applied"
    assert provider_reference not in resolved.text
