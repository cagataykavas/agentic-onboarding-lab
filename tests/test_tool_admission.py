from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import service.api as api_module
from onboarding import OnboardingCase, Stage
from service.reviews import ReviewQueue
from service.store import IdempotencyConflict, OnboardingRepository
from service.tool_admission import ToolProposal, default_tool_policy


def _case(**changes) -> OnboardingCase:
    values = {
        "case_id": "ONB-TEST-1",
        "product": "account",
        "customer_type": "retail",
        "stage": Stage.IDENTITY_PENDING,
        "consent": True,
        "version": 2,
    }
    values.update(changes)
    return OnboardingCase(**values)


def _proposal(**changes) -> ToolProposal:
    values = {
        "call_id": "call-1",
        "tool_name": "identity_document_check",
        "expected_case_version": 2,
        "arguments": {"evidence_id": "identity-1"},
    }
    values.update(changes)
    return ToolProposal(**values)


def _client(tmp_path: Path) -> TestClient:
    api_module.repository = OnboardingRepository(tmp_path / "api.db")
    api_module.review_queue = ReviewQueue(api_module.repository)
    return TestClient(api_module.app)


def test_matching_tool_proposal_is_admitted_deterministically() -> None:
    policy = default_tool_policy()

    first = policy.evaluate(_case(), _proposal())
    second = policy.evaluate(_case(), _proposal())

    assert first.allowed
    assert first.reasons == ()
    assert first == second
    assert len(first.decision_sha256) == 64


@pytest.mark.parametrize(
    ("case_changes", "proposal_changes", "reason"),
    [
        ({"consent": False}, {}, "consent_required"),
        ({"stage": Stage.PROFILE_PENDING}, {}, "stage_not_allowed"),
        ({"stage": Stage.COMPLETED}, {}, "terminal_case"),
        ({}, {"expected_case_version": 1}, "case_version_mismatch"),
        ({}, {"tool_name": "shell"}, "unknown_tool"),
        ({}, {"arguments": {}}, "missing_arguments"),
        (
            {},
            {"arguments": {"evidence_id": "identity-1", "command": "rm"}},
            "unexpected_arguments",
        ),
        ({}, {"arguments": {"evidence_id": 17}}, "invalid_argument_value"),
    ],
)
def test_policy_fails_closed_with_stable_reasons(
    case_changes: dict, proposal_changes: dict, reason: str
) -> None:
    admission = default_tool_policy().evaluate(
        _case(**case_changes),
        _proposal(**proposal_changes),
    )

    assert not admission.allowed
    assert reason in admission.reasons


def test_call_budget_is_enforced() -> None:
    admission = default_tool_policy().evaluate(
        _case(),
        _proposal(),
        prior_admissions=3,
    )

    assert not admission.allowed
    assert admission.reasons == ("call_budget_exceeded",)


def test_report_does_not_expose_case_or_argument_values() -> None:
    admission = default_tool_policy().evaluate(
        _case(case_id="customer-secret"),
        _proposal(arguments={"evidence_id": "document-secret"}),
    )
    payload = admission.to_dict()

    assert "customer-secret" not in str(payload)
    assert "document-secret" not in str(payload)
    assert "arguments" not in payload


def test_non_finite_or_oversized_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        default_tool_policy().evaluate(
            _case(),
            _proposal(arguments={"evidence_id": float("nan")}),
        )

    admission = default_tool_policy().evaluate(
        _case(),
        _proposal(arguments={"evidence_id": "x" * 5000}),
    )
    assert "invalid_argument_value" in admission.reasons
    assert "argument_payload_too_large" in admission.reasons


def test_repository_replays_same_call_without_consuming_budget(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "tools.db")
    repository.create(_case(version=0))
    stored = repository.get("ONB-TEST-1")
    assert stored is not None
    stored.consent = True
    stored.stage = Stage.IDENTITY_PENDING
    repository.save(stored, expected_version=1)
    proposal = _proposal()

    first = repository.admit_tool_call("ONB-TEST-1", proposal, default_tool_policy())
    second = repository.admit_tool_call("ONB-TEST-1", proposal, default_tool_policy())

    assert first.allowed
    assert not first.replayed
    assert second.replayed
    assert first.decision_sha256 == second.decision_sha256


def test_call_id_cannot_be_rebound(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "tools.db")
    repository.create(_case(version=0))
    proposal = _proposal(expected_case_version=1)
    repository.admit_tool_call("ONB-TEST-1", proposal, default_tool_policy())

    with pytest.raises(IdempotencyConflict, match="reused"):
        repository.admit_tool_call(
            "ONB-TEST-1",
            _proposal(expected_case_version=1, arguments={"evidence_id": "changed"}),
            default_tool_policy(),
        )


def test_concurrent_same_call_creates_one_receipt(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "tools.db")
    repository.create(_case(version=0))
    proposal = _proposal(expected_case_version=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(
            pool.map(
                lambda _: repository.admit_tool_call("ONB-TEST-1", proposal, default_tool_policy()),
                range(16),
            )
        )

    assert sum(not admission.replayed for admission in admissions) == 1
    assert len({admission.decision_sha256 for admission in admissions}) == 1


def test_api_admits_and_replays_tool_call(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/cases", json={}).json()
    case_id = created["case_id"]
    consent = client.post(
        f"/cases/{case_id}/consent",
        headers={"If-Match": "1"},
        json={"accepted": True},
    ).json()["case"]
    payload = {
        "call_id": "identity-check-1",
        "tool_name": "identity_document_check",
        "expected_case_version": consent["version"],
        "arguments": {"evidence_id": "identity-upload-1"},
    }

    first = client.post(f"/cases/{case_id}/tool-calls/admit", json=payload)
    replayed = client.post(f"/cases/{case_id}/tool-calls/admit", json=payload)

    assert first.status_code == replayed.status_code == 200
    assert first.json()["allowed"] is True
    assert first.json()["replayed"] is False
    assert replayed.json()["replayed"] is True
    assert "identity-upload-1" not in first.text


def test_api_returns_decision_for_stale_proposal_and_404_for_unknown_case(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/cases", json={}).json()
    payload = {
        "call_id": "stale-call",
        "tool_name": "identity_document_check",
        "expected_case_version": 99,
        "arguments": {"evidence_id": "identity-upload-1"},
    }

    rejected = client.post(f"/cases/{created['case_id']}/tool-calls/admit", json=payload)
    missing = client.post(
        "/cases/not-found/tool-calls/admit",
        json={**payload, "call_id": "missing-case-call"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["allowed"] is False
    assert "case_version_mismatch" in rejected.json()["reasons"]
    assert missing.status_code == 404


def test_api_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    case_id = client.post("/cases", json={}).json()["case_id"]

    response = client.post(
        f"/cases/{case_id}/tool-calls/admit",
        json={
            "call_id": "call-with-extra-field",
            "tool_name": "identity_document_check",
            "expected_case_version": 1,
            "arguments": {"evidence_id": "identity-upload-1"},
            "override_policy": True,
        },
    )

    assert response.status_code == 422
