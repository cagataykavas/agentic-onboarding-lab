from __future__ import annotations

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


def _admit(
    repository: OnboardingRepository,
    *,
    consent: bool = True,
    call_id: str = "call-1",
) -> tuple[str, str]:
    case = OnboardingCase(
        case_id="ONB-EXEC-1",
        product="account",
        customer_type="retail",
        stage=Stage.IDENTITY_PENDING,
        consent=consent,
    )
    repository.create(case)
    proposal = ToolProposal(
        call_id=call_id,
        tool_name="identity_document_check",
        expected_case_version=1,
        arguments={"evidence_id": "identity-1"},
    )
    admission = repository.admit_tool_call(case.case_id, proposal, default_tool_policy())
    return case.case_id, admission.decision_sha256


def _claim(
    repository: OnboardingRepository,
    case_id: str,
    decision_sha256: str,
    *,
    worker_id: str = "worker-a",
    now: datetime = NOW,
):
    return repository.claim_tool_execution(
        case_id,
        "call-1",
        admission_decision_sha256=decision_sha256,
        worker_id=worker_id,
        policy=default_tool_policy(),
        lease_seconds=30,
        now=now,
    )


def test_claim_binds_admission_worker_and_fence_without_raw_worker(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "claim.db")
    case_id, decision_sha256 = _admit(repository)

    receipt = _claim(repository, case_id, decision_sha256)

    assert receipt.state == "running"
    assert receipt.attempt == 1
    assert receipt.should_execute
    assert not receipt.replayed
    assert receipt.admission_decision_sha256 == decision_sha256
    assert receipt.worker_sha256 != "worker-a"
    assert "worker-a" not in str(receipt.to_dict())
    assert len(receipt.receipt_sha256) == 64


def test_active_claim_replay_never_reauthorizes_execution(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "replay.db")
    case_id, decision_sha256 = _admit(repository)
    first = _claim(repository, case_id, decision_sha256)

    replayed = _claim(repository, case_id, decision_sha256, now=NOW + timedelta(seconds=1))

    assert replayed.replayed
    assert not replayed.should_execute
    assert replayed.receipt_sha256 == first.receipt_sha256


def test_concurrent_workers_produce_one_executable_claim(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "concurrent.db")
    case_id, decision_sha256 = _admit(repository)

    def claim(index: int):
        try:
            return _claim(
                repository,
                case_id,
                decision_sha256,
                worker_id=f"worker-{index}",
            )
        except ExecutionConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(claim, range(16)))

    admitted = [receipt for receipt in receipts if receipt is not None]
    assert len(admitted) == 1
    assert admitted[0].should_execute


def test_rejected_or_rebound_admission_cannot_be_claimed(tmp_path: Path) -> None:
    rejected_repository = OnboardingRepository(tmp_path / "rejected.db")
    case_id, decision_sha256 = _admit(rejected_repository, consent=False)

    with pytest.raises(ExecutionConflict, match="rejected"):
        _claim(rejected_repository, case_id, decision_sha256)

    admitted_repository = OnboardingRepository(tmp_path / "wrong-digest.db")
    case_id, decision_sha256 = _admit(admitted_repository)
    assert decision_sha256 != "0" * 64
    with pytest.raises(ExecutionConflict, match="identity"):
        _claim(admitted_repository, case_id, "0" * 64)


def test_case_or_policy_drift_after_admission_fails_before_claim(tmp_path: Path) -> None:
    case_repository = OnboardingRepository(tmp_path / "case-drift.db")
    case_id, decision_sha256 = _admit(case_repository)
    changed = case_repository.get(case_id)
    assert changed is not None
    changed.stage = Stage.PROFILE_PENDING
    case_repository.save(changed, expected_version=1)

    with pytest.raises(ExecutionConflict, match="case state changed"):
        _claim(case_repository, case_id, decision_sha256)

    policy_repository = OnboardingRepository(tmp_path / "policy-drift.db")
    case_id, decision_sha256 = _admit(policy_repository)
    changed_policy = default_tool_policy()
    changed_policy.max_argument_bytes += 1
    with pytest.raises(ExecutionConflict, match="policy changed"):
        policy_repository.claim_tool_execution(
            case_id,
            "call-1",
            admission_decision_sha256=decision_sha256,
            worker_id="worker-a",
            policy=changed_policy,
            now=NOW,
        )


def test_expired_lease_reclaim_increments_fence_and_rejects_old_worker(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "takeover.db")
    case_id, decision_sha256 = _admit(repository)
    first = _claim(repository, case_id, decision_sha256)
    second = _claim(
        repository,
        case_id,
        decision_sha256,
        worker_id="worker-b",
        now=NOW + timedelta(seconds=31),
    )

    assert first.attempt == 1
    assert second.attempt == 2
    assert second.should_execute
    with pytest.raises(ExecutionConflict, match="stale"):
        repository.complete_tool_execution(
            case_id,
            "call-1",
            admission_decision_sha256=decision_sha256,
            worker_id="worker-a",
            attempt=1,
            outcome="succeeded",
            policy=default_tool_policy(),
            result={"status": "verified", "provider_reference": "provider-1"},
            now=NOW + timedelta(seconds=32),
        )


def test_case_drift_during_execution_rejects_stale_completion(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "mid-flight-drift.db")
    case_id, decision_sha256 = _admit(repository)
    claim = _claim(repository, case_id, decision_sha256)
    changed = repository.get(case_id)
    assert changed is not None
    changed.stage = Stage.PROFILE_PENDING
    repository.save(changed, expected_version=1)

    with pytest.raises(ExecutionConflict, match="changed during"):
        repository.complete_tool_execution(
            case_id,
            "call-1",
            admission_decision_sha256=decision_sha256,
            worker_id="worker-a",
            attempt=claim.attempt,
            outcome="succeeded",
            policy=default_tool_policy(),
            result={"status": "verified", "provider_reference": "provider-1"},
            now=NOW + timedelta(seconds=1),
        )


def test_successful_completion_validates_and_binds_result_without_storing_values(
    tmp_path: Path,
) -> None:
    repository = OnboardingRepository(tmp_path / "complete.db")
    case_id, decision_sha256 = _admit(repository)
    claim = _claim(repository, case_id, decision_sha256)
    result = {"status": "verified", "provider_reference": "sensitive-provider-reference"}

    receipt = repository.complete_tool_execution(
        case_id,
        "call-1",
        admission_decision_sha256=decision_sha256,
        worker_id="worker-a",
        attempt=claim.attempt,
        outcome="succeeded",
        policy=default_tool_policy(),
        result=result,
        now=NOW + timedelta(seconds=1),
    )

    assert receipt.state == "succeeded"
    assert receipt.result_sha256 is not None
    assert receipt.failure_code is None
    assert not receipt.should_execute
    assert result["provider_reference"] not in str(receipt.to_dict())


@pytest.mark.parametrize(
    "result",
    [
        {"status": "invented", "provider_reference": "provider-1"},
        {"status": "verified"},
        {"status": "verified", "provider_reference": "provider-1", "raw": "secret"},
        {"status": "verified", "provider_reference": 17},
    ],
)
def test_invalid_provider_response_fails_closed(tmp_path: Path, result: dict) -> None:
    repository = OnboardingRepository(tmp_path / f"invalid-{len(str(result))}.db")
    case_id, decision_sha256 = _admit(repository)
    claim = _claim(repository, case_id, decision_sha256)

    with pytest.raises(ValueError, match="result|provider_reference"):
        repository.complete_tool_execution(
            case_id,
            "call-1",
            admission_decision_sha256=decision_sha256,
            worker_id="worker-a",
            attempt=claim.attempt,
            outcome="succeeded",
            policy=default_tool_policy(),
            result=result,
            now=NOW + timedelta(seconds=1),
        )


def test_terminal_completion_replays_only_an_identical_outcome(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "terminal.db")
    case_id, decision_sha256 = _admit(repository)
    claim = _claim(repository, case_id, decision_sha256)
    values = {
        "case_id": case_id,
        "call_id": "call-1",
        "admission_decision_sha256": decision_sha256,
        "worker_id": "worker-a",
        "attempt": claim.attempt,
        "outcome": "succeeded",
        "policy": default_tool_policy(),
        "result": {"status": "verified", "provider_reference": "provider-1"},
        "now": NOW + timedelta(seconds=1),
    }
    first = repository.complete_tool_execution(**values)
    replayed = repository.complete_tool_execution(**values)

    assert replayed.replayed
    assert replayed.receipt_sha256 == first.receipt_sha256
    changed = repository.get(case_id)
    assert changed is not None
    changed.stage = Stage.PROFILE_PENDING
    repository.save(changed, expected_version=1)
    terminal_claim = _claim(repository, case_id, decision_sha256, now=NOW + timedelta(seconds=2))
    assert terminal_claim.replayed
    assert not terminal_claim.should_execute

    with pytest.raises(ExecutionConflict, match="different terminal"):
        repository.complete_tool_execution(
            **{
                **values,
                "result": {"status": "rejected", "provider_reference": "provider-1"},
            }
        )


def test_expired_lease_and_unknown_failure_code_are_rejected(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "failure.db")
    case_id, decision_sha256 = _admit(repository)
    claim = _claim(repository, case_id, decision_sha256)

    with pytest.raises(ExecutionConflict, match="expired"):
        repository.complete_tool_execution(
            case_id,
            "call-1",
            admission_decision_sha256=decision_sha256,
            worker_id="worker-a",
            attempt=claim.attempt,
            outcome="failed",
            policy=default_tool_policy(),
            failure_code="timeout",
            now=NOW + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="allowed failure_code"):
        repository.complete_tool_execution(
            case_id,
            "call-1",
            admission_decision_sha256=decision_sha256,
            worker_id="worker-a",
            attempt=claim.attempt,
            outcome="failed",
            policy=default_tool_policy(),
            failure_code="stack-trace-secret",
            now=NOW + timedelta(seconds=1),
        )


def test_api_executes_claim_and_completion_contract(tmp_path: Path) -> None:
    api_module.repository = OnboardingRepository(tmp_path / "api.db")
    api_module.review_queue = ReviewQueue(api_module.repository)
    client = TestClient(api_module.app)
    case_id = client.post("/cases", json={}).json()["case_id"]
    consent = client.post(
        f"/cases/{case_id}/consent",
        headers={"If-Match": "1"},
        json={"accepted": True},
    ).json()["case"]
    admitted = client.post(
        f"/cases/{case_id}/tool-calls/admit",
        json={
            "call_id": "api-call-1",
            "tool_name": "identity_document_check",
            "expected_case_version": consent["version"],
            "arguments": {"evidence_id": "identity-upload-1"},
        },
    ).json()
    claim = client.post(
        f"/cases/{case_id}/tool-calls/api-call-1/claim",
        json={
            "admission_decision_sha256": admitted["decision_sha256"],
            "worker_id": "api-worker",
            "lease_seconds": 60,
        },
    )
    assert claim.status_code == 200
    claimed = claim.json()
    assert claimed["should_execute"] is True

    completed = client.post(
        f"/cases/{case_id}/tool-calls/api-call-1/complete",
        json={
            "admission_decision_sha256": admitted["decision_sha256"],
            "worker_id": "api-worker",
            "attempt": claimed["attempt"],
            "outcome": "succeeded",
            "result": {"status": "verified", "provider_reference": "private-provider-ref"},
        },
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "succeeded"
    assert "private-provider-ref" not in completed.text
