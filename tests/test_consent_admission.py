from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service.consent_admission import (
    ConsentArtifactError,
    ConsentGrant,
    ConsentPolicy,
    ToolConsentRule,
    ToolRequest,
    evaluate_artifact,
    evaluate_consent,
    load_artifact,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> ConsentPolicy:
    return ConsentPolicy(
        policy_id="onboarding-consent-v1",
        terms_version="terms-2026-09",
        tools={
            "identity.verify": ToolConsentRule(
                purposes=("identity_verification",),
                data_categories=("identity_document", "identity_attributes"),
            ),
            "address.verify": ToolConsentRule(
                purposes=("address_verification",),
                data_categories=("postal_address",),
            ),
        },
        max_grant_seconds=3600,
        max_request_age_seconds=60,
        max_clock_skew_seconds=2,
    )


@pytest.fixture
def grant(policy: ConsentPolicy) -> ConsentGrant:
    return ConsentGrant(
        grant_id="grant-001",
        case_id="ONB-ABC123",
        policy_id=policy.policy_id,
        terms_version=policy.terms_version,
        purposes=("identity_verification",),
        data_categories=("identity_document", "identity_attributes"),
        tool_ids=("identity.verify",),
        issued_at=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(minutes=20),
    )


@pytest.fixture
def tool_request() -> ToolRequest:
    return ToolRequest(
        request_id="request-001",
        case_id="ONB-ABC123",
        grant_id="grant-001",
        tool_id="identity.verify",
        purpose="identity_verification",
        data_categories=("identity_document",),
        requested_at=NOW - timedelta(seconds=1),
    )


def test_accepts_minimum_necessary_authorized_request(policy, grant, tool_request):
    decision = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)

    assert decision.accepted is True
    assert decision.reason_codes == ()
    assert decision.evidence["schema_version"] == "consent-admission/v1"
    assert "ONB-ABC123" not in json.dumps(decision.to_dict())


@pytest.mark.parametrize(
    ("changed_grant", "changed_request", "reason"),
    [
        ({"case_id": "ONB-OTHER"}, {}, "CASE_SCOPE_MISMATCH"),
        ({"grant_id": "grant-other"}, {}, "GRANT_REFERENCE_MISMATCH"),
        ({"policy_id": "legacy-policy"}, {}, "POLICY_ID_MISMATCH"),
        ({"terms_version": "terms-old"}, {}, "TERMS_VERSION_MISMATCH"),
        ({"tool_ids": ("address.verify",)}, {}, "TOOL_NOT_GRANTED"),
        ({"purposes": ("address_verification",)}, {}, "PURPOSE_NOT_GRANTED"),
        ({"data_categories": ("identity_attributes",)}, {}, "DATA_NOT_GRANTED"),
        ({}, {"tool_id": "address.verify"}, "PURPOSE_NOT_ALLOWED_FOR_TOOL"),
        ({}, {"tool_id": "unknown.tool"}, "UNKNOWN_TOOL"),
        ({}, {"purpose": "address_verification"}, "PURPOSE_NOT_ALLOWED_FOR_TOOL"),
        ({}, {"data_categories": ("biometric_template",)}, "DATA_NOT_ALLOWED_FOR_TOOL"),
    ],
)
def test_rejects_scope_and_policy_mismatch(
    policy, grant, tool_request, changed_grant, changed_request, reason
):
    decision = evaluate_consent(
        replace(grant, **changed_grant),
        replace(tool_request, **changed_request),
        policy,
        evaluated_at=NOW,
    )

    assert decision.accepted is False
    assert reason in decision.reason_codes


def test_rejects_expired_grant(policy, grant, tool_request):
    grant = replace(grant, expires_at=NOW - timedelta(seconds=2))
    tool_request = replace(tool_request, requested_at=NOW - timedelta(seconds=1))

    decision = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)

    assert "GRANT_EXPIRED" in decision.reason_codes


def test_rejects_request_at_or_after_revocation(policy, grant, tool_request):
    grant = replace(grant, revoked_at=NOW - timedelta(seconds=2))

    decision = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)

    assert "GRANT_REVOKED" in decision.reason_codes


def test_request_before_revocation_remains_valid(policy, grant, tool_request):
    grant = replace(grant, revoked_at=NOW + timedelta(seconds=1))

    assert evaluate_consent(grant, tool_request, policy, evaluated_at=NOW).accepted is True


def test_rejects_overlong_grant(policy, grant, tool_request):
    grant = replace(grant, issued_at=NOW - timedelta(hours=2))

    decision = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)

    assert "GRANT_LIFETIME_EXCEEDED" in decision.reason_codes


def test_rejects_stale_and_future_request(policy, grant, tool_request):
    stale = replace(tool_request, requested_at=NOW - timedelta(seconds=61))
    future = replace(tool_request, requested_at=NOW + timedelta(seconds=3))

    assert "STALE_REQUEST" in evaluate_consent(grant, stale, policy, evaluated_at=NOW).reason_codes
    assert (
        "REQUEST_IN_FUTURE"
        in evaluate_consent(grant, future, policy, evaluated_at=NOW).reason_codes
    )


def test_evidence_is_deterministic_and_binds_request(policy, grant, tool_request):
    first = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)
    second = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)
    changed = evaluate_consent(
        grant,
        replace(tool_request, request_id="request-002"),
        policy,
        evaluated_at=NOW,
    )

    assert first.to_dict() == second.to_dict()
    assert first.evidence["decision_sha256"] != changed.evidence["decision_sha256"]


def test_evidence_binds_complete_policy(policy, grant, tool_request):
    first = evaluate_consent(grant, tool_request, policy, evaluated_at=NOW)
    changed_policy = replace(
        policy,
        tools={
            **policy.tools,
            "address.verify": ToolConsentRule(
                purposes=("address_verification",),
                data_categories=("postal_address", "geolocation"),
            ),
        },
    )

    changed = evaluate_consent(grant, tool_request, changed_policy, evaluated_at=NOW)

    assert first.accepted is True and changed.accepted is True
    assert first.evidence["policy_sha256"] != changed.evidence["policy_sha256"]
    assert first.evidence["decision_sha256"] != changed.evidence["decision_sha256"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("purposes", ("identity_verification", "identity_verification")),
        ("data_categories", ()),
        ("tool_ids", ("bad tool",)),
    ],
)
def test_direct_call_revalidates_grant(policy, grant, tool_request, field, value):
    with pytest.raises(ConsentArtifactError):
        evaluate_consent(replace(grant, **{field: value}), tool_request, policy, evaluated_at=NOW)


def test_requires_timezone_aware_evaluation(policy, grant, tool_request):
    with pytest.raises(ConsentArtifactError, match="timezone"):
        evaluate_consent(grant, tool_request, policy, evaluated_at=NOW.replace(tzinfo=None))


def _artifact() -> dict:
    return {
        "policy": {
            "policy_id": "onboarding-consent-v1",
            "terms_version": "terms-2026-09",
            "max_grant_seconds": 3600,
            "max_request_age_seconds": 60,
            "max_clock_skew_seconds": 2,
            "tools": {
                "identity.verify": {
                    "purposes": ["identity_verification"],
                    "data_categories": ["identity_document"],
                }
            },
        },
        "grant": {
            "grant_id": "grant-001",
            "case_id": "ONB-ABC123",
            "policy_id": "onboarding-consent-v1",
            "terms_version": "terms-2026-09",
            "purposes": ["identity_verification"],
            "data_categories": ["identity_document"],
            "tool_ids": ["identity.verify"],
            "issued_at": "2026-09-25T08:50:00Z",
            "expires_at": "2026-09-25T09:20:00Z",
            "revoked_at": None,
        },
        "request": {
            "request_id": "request-001",
            "case_id": "ONB-ABC123",
            "grant_id": "grant-001",
            "tool_id": "identity.verify",
            "purpose": "identity_verification",
            "data_categories": ["identity_document"],
            "requested_at": "2026-09-25T08:59:59Z",
        },
        "evaluated_at": "2026-09-25T09:00:00Z",
    }


def test_artifact_parser_accepts_strict_contract():
    assert evaluate_artifact(_artifact()).accepted is True


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_policy_value_fails_closed(value):
    artifact = _artifact()
    artifact["policy"]["max_request_age_seconds"] = value

    with pytest.raises(ConsentArtifactError):
        evaluate_artifact(artifact)


def test_duplicate_json_field_is_rejected(tmp_path: Path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"grant":{},"grant":{}}')

    with pytest.raises(ConsentArtifactError, match="duplicate"):
        load_artifact(path)


def test_cli_exit_codes_and_redacted_evidence(tmp_path: Path):
    accepted_path = tmp_path / "accepted.json"
    accepted_path.write_text(json.dumps(_artifact()))
    accepted = subprocess.run(
        [sys.executable, "-m", "service.consent_admission", str(accepted_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    accepted_output = json.loads(accepted.stdout)

    rejected_artifact = _artifact()
    rejected_artifact["request"]["purpose"] = "marketing"
    rejected_path = tmp_path / "rejected.json"
    rejected_path.write_text(json.dumps(rejected_artifact))
    rejected = subprocess.run(
        [sys.executable, "-m", "service.consent_admission", str(rejected_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("{not-json")
    malformed = subprocess.run(
        [sys.executable, "-m", "service.consent_admission", str(malformed_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert accepted.returncode == 0
    assert accepted_output["accepted"] is True
    assert "ONB-ABC123" not in accepted.stdout
    assert rejected.returncode == 2
    assert malformed.returncode == 3
