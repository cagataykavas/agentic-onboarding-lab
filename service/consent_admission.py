"""Fail-closed, purpose-bound consent admission for agent tool calls.

The gate deliberately accepts only metadata about a proposed call. Raw customer
evidence and tool arguments stay outside this artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_INPUT_BYTES = 128 * 1024
_MAX_ITEMS = 64


class ConsentArtifactError(ValueError):
    """Raised when consent evidence or policy is malformed."""


@dataclass(frozen=True)
class ToolConsentRule:
    purposes: tuple[str, ...]
    data_categories: tuple[str, ...]


@dataclass(frozen=True)
class ConsentPolicy:
    policy_id: str
    terms_version: str
    tools: dict[str, ToolConsentRule]
    max_grant_seconds: int = 86_400
    max_request_age_seconds: int = 300
    max_clock_skew_seconds: int = 5


@dataclass(frozen=True)
class ConsentGrant:
    grant_id: str
    case_id: str
    policy_id: str
    terms_version: str
    purposes: tuple[str, ...]
    data_categories: tuple[str, ...]
    tool_ids: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class ToolRequest:
    request_id: str
    case_id: str
    grant_id: str
    tool_id: str
    purpose: str
    data_categories: tuple[str, ...]
    requested_at: datetime


@dataclass(frozen=True)
class AdmissionDecision:
    accepted: bool
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason_codes": list(self.reason_codes),
            "evidence": self.evidence,
        }


def _validate_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ConsentArtifactError(f"{field} must be a bounded identifier")
    return value


def _validate_items(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ConsentArtifactError(f"{field} must be an array")
    if (not value and not allow_empty) or len(value) > _MAX_ITEMS:
        raise ConsentArtifactError(f"{field} has an invalid item count")
    items = tuple(_validate_identifier(item, field) for item in value)
    if len(set(items)) != len(items):
        raise ConsentArtifactError(f"{field} contains duplicates")
    return items


def _validate_seconds(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConsentArtifactError(f"{field} must be between {minimum} and {maximum}")
    return value


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ConsentArtifactError(f"{field} must be a bounded ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConsentArtifactError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConsentArtifactError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _identity_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_text(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def policy_from_dict(payload: object) -> ConsentPolicy:
    if not isinstance(payload, dict):
        raise ConsentArtifactError("policy must be an object")
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, dict) or not raw_tools or len(raw_tools) > _MAX_ITEMS:
        raise ConsentArtifactError("policy.tools must be a bounded non-empty object")
    tools: dict[str, ToolConsentRule] = {}
    for raw_tool_id, raw_rule in raw_tools.items():
        tool_id = _validate_identifier(raw_tool_id, "tool_id")
        if not isinstance(raw_rule, dict):
            raise ConsentArtifactError("each tool policy must be an object")
        tools[tool_id] = ToolConsentRule(
            purposes=_validate_items(raw_rule.get("purposes"), "tool purposes"),
            data_categories=_validate_items(
                raw_rule.get("data_categories"), "tool data_categories"
            ),
        )
    return ConsentPolicy(
        policy_id=_validate_identifier(payload.get("policy_id"), "policy_id"),
        terms_version=_validate_identifier(payload.get("terms_version"), "terms_version"),
        tools=tools,
        max_grant_seconds=_validate_seconds(
            payload.get("max_grant_seconds", 86_400),
            "max_grant_seconds",
            minimum=60,
            maximum=2_592_000,
        ),
        max_request_age_seconds=_validate_seconds(
            payload.get("max_request_age_seconds", 300),
            "max_request_age_seconds",
            minimum=1,
            maximum=86_400,
        ),
        max_clock_skew_seconds=_validate_seconds(
            payload.get("max_clock_skew_seconds", 5),
            "max_clock_skew_seconds",
            minimum=0,
            maximum=300,
        ),
    )


def grant_from_dict(payload: object) -> ConsentGrant:
    if not isinstance(payload, dict):
        raise ConsentArtifactError("grant must be an object")
    revoked = payload.get("revoked_at")
    return ConsentGrant(
        grant_id=_validate_identifier(payload.get("grant_id"), "grant_id"),
        case_id=_validate_identifier(payload.get("case_id"), "case_id"),
        policy_id=_validate_identifier(payload.get("policy_id"), "policy_id"),
        terms_version=_validate_identifier(payload.get("terms_version"), "terms_version"),
        purposes=_validate_items(payload.get("purposes"), "purposes"),
        data_categories=_validate_items(payload.get("data_categories"), "data_categories"),
        tool_ids=_validate_items(payload.get("tool_ids"), "tool_ids"),
        issued_at=_parse_timestamp(payload.get("issued_at"), "issued_at"),
        expires_at=_parse_timestamp(payload.get("expires_at"), "expires_at"),
        revoked_at=None if revoked is None else _parse_timestamp(revoked, "revoked_at"),
    )


def request_from_dict(payload: object) -> ToolRequest:
    if not isinstance(payload, dict):
        raise ConsentArtifactError("request must be an object")
    return ToolRequest(
        request_id=_validate_identifier(payload.get("request_id"), "request_id"),
        case_id=_validate_identifier(payload.get("case_id"), "case_id"),
        grant_id=_validate_identifier(payload.get("grant_id"), "grant_id"),
        tool_id=_validate_identifier(payload.get("tool_id"), "tool_id"),
        purpose=_validate_identifier(payload.get("purpose"), "purpose"),
        data_categories=_validate_items(payload.get("data_categories"), "data_categories"),
        requested_at=_parse_timestamp(payload.get("requested_at"), "requested_at"),
    )


def _validate_policy(policy: ConsentPolicy) -> None:
    _validate_identifier(policy.policy_id, "policy_id")
    _validate_identifier(policy.terms_version, "terms_version")
    _validate_seconds(policy.max_grant_seconds, "max_grant_seconds", minimum=60, maximum=2_592_000)
    _validate_seconds(
        policy.max_request_age_seconds,
        "max_request_age_seconds",
        minimum=1,
        maximum=86_400,
    )
    _validate_seconds(
        policy.max_clock_skew_seconds,
        "max_clock_skew_seconds",
        minimum=0,
        maximum=300,
    )
    if not policy.tools or len(policy.tools) > _MAX_ITEMS:
        raise ConsentArtifactError("policy tools must be bounded and non-empty")
    for tool_id, rule in policy.tools.items():
        _validate_identifier(tool_id, "tool_id")
        if not isinstance(rule, ToolConsentRule):
            raise ConsentArtifactError("each tool policy must be a ToolConsentRule")
        _validate_items(rule.purposes, "tool purposes")
        _validate_items(rule.data_categories, "tool data_categories")


def evaluate_consent(
    grant: ConsentGrant,
    request: ToolRequest,
    policy: ConsentPolicy,
    *,
    evaluated_at: datetime,
) -> AdmissionDecision:
    """Evaluate one proposed tool call without executing it."""

    _validate_policy(policy)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ConsentArtifactError("evaluated_at must include a timezone")
    evaluated_at = evaluated_at.astimezone(UTC)

    # Re-parse dataclass fields so direct Python callers receive the same strictness as the CLI.
    normalized_grant = grant_from_dict(
        {
            "grant_id": grant.grant_id,
            "case_id": grant.case_id,
            "policy_id": grant.policy_id,
            "terms_version": grant.terms_version,
            "purposes": list(grant.purposes),
            "data_categories": list(grant.data_categories),
            "tool_ids": list(grant.tool_ids),
            "issued_at": _timestamp_text(grant.issued_at),
            "expires_at": _timestamp_text(grant.expires_at),
            "revoked_at": _timestamp_text(grant.revoked_at),
        }
    )
    normalized_request = request_from_dict(
        {
            "request_id": request.request_id,
            "case_id": request.case_id,
            "grant_id": request.grant_id,
            "tool_id": request.tool_id,
            "purpose": request.purpose,
            "data_categories": list(request.data_categories),
            "requested_at": _timestamp_text(request.requested_at),
        }
    )
    grant = normalized_grant
    request = normalized_request

    reasons: list[str] = []
    if grant.policy_id != policy.policy_id:
        reasons.append("POLICY_ID_MISMATCH")
    if grant.terms_version != policy.terms_version:
        reasons.append("TERMS_VERSION_MISMATCH")
    if grant.case_id != request.case_id:
        reasons.append("CASE_SCOPE_MISMATCH")
    if grant.grant_id != request.grant_id:
        reasons.append("GRANT_REFERENCE_MISMATCH")
    if grant.expires_at <= grant.issued_at:
        reasons.append("INVALID_GRANT_WINDOW")
    elif (grant.expires_at - grant.issued_at).total_seconds() > policy.max_grant_seconds:
        reasons.append("GRANT_LIFETIME_EXCEEDED")
    if request.requested_at < grant.issued_at:
        reasons.append("REQUEST_BEFORE_GRANT")
    if request.requested_at >= grant.expires_at:
        reasons.append("GRANT_EXPIRED")
    if grant.revoked_at is not None:
        if grant.revoked_at < grant.issued_at:
            reasons.append("INVALID_REVOCATION_TIME")
        if request.requested_at >= grant.revoked_at:
            reasons.append("GRANT_REVOKED")

    skew = (request.requested_at - evaluated_at).total_seconds()
    age = (evaluated_at - request.requested_at).total_seconds()
    if skew > policy.max_clock_skew_seconds:
        reasons.append("REQUEST_IN_FUTURE")
    if age > policy.max_request_age_seconds:
        reasons.append("STALE_REQUEST")

    rule = policy.tools.get(request.tool_id)
    if rule is None:
        reasons.append("UNKNOWN_TOOL")
    else:
        if request.purpose not in rule.purposes:
            reasons.append("PURPOSE_NOT_ALLOWED_FOR_TOOL")
        if not set(request.data_categories).issubset(rule.data_categories):
            reasons.append("DATA_NOT_ALLOWED_FOR_TOOL")
    if request.tool_id not in grant.tool_ids:
        reasons.append("TOOL_NOT_GRANTED")
    if request.purpose not in grant.purposes:
        reasons.append("PURPOSE_NOT_GRANTED")
    if not set(request.data_categories).issubset(grant.data_categories):
        reasons.append("DATA_NOT_GRANTED")

    grant_record = {
        "grant_id": grant.grant_id,
        "case_id": grant.case_id,
        "policy_id": grant.policy_id,
        "terms_version": grant.terms_version,
        "purposes": sorted(grant.purposes),
        "data_categories": sorted(grant.data_categories),
        "tool_ids": sorted(grant.tool_ids),
        "issued_at": _timestamp_text(grant.issued_at),
        "expires_at": _timestamp_text(grant.expires_at),
        "revoked_at": _timestamp_text(grant.revoked_at),
    }
    request_record = {
        "request_id": request.request_id,
        "case_id": request.case_id,
        "grant_id": request.grant_id,
        "tool_id": request.tool_id,
        "purpose": request.purpose,
        "data_categories": sorted(request.data_categories),
        "requested_at": _timestamp_text(request.requested_at),
    }
    policy_record = {
        "policy_id": policy.policy_id,
        "terms_version": policy.terms_version,
        "max_grant_seconds": policy.max_grant_seconds,
        "max_request_age_seconds": policy.max_request_age_seconds,
        "max_clock_skew_seconds": policy.max_clock_skew_seconds,
        "tools": {
            tool_id: {
                "purposes": sorted(rule.purposes),
                "data_categories": sorted(rule.data_categories),
            }
            for tool_id, rule in sorted(policy.tools.items())
        },
    }
    evidence_core = {
        "schema_version": "consent-admission/v1",
        "policy_id": policy.policy_id,
        "terms_version": policy.terms_version,
        "tool_id": request.tool_id,
        "purpose": request.purpose,
        "data_categories": sorted(request.data_categories),
        "evaluated_at": _timestamp_text(evaluated_at),
        "case_id_sha256": _identity_digest(request.case_id),
        "grant_id_sha256": _identity_digest(request.grant_id),
        "request_id_sha256": _identity_digest(request.request_id),
        "grant_sha256": _digest(grant_record),
        "request_sha256": _digest(request_record),
        "policy_sha256": _digest(policy_record),
    }
    evidence = {**evidence_core, "decision_sha256": _digest({**evidence_core, "reasons": reasons})}
    return AdmissionDecision(not reasons, tuple(reasons), evidence)


def _reject_constant(value: str) -> None:
    raise ConsentArtifactError(f"non-finite JSON value is not allowed: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ConsentArtifactError(f"duplicate JSON field: {key}")
        output[key] = value
    return output


def load_artifact(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_INPUT_BYTES:
        raise ConsentArtifactError("input artifact is empty or exceeds the byte budget")
    try:
        payload = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsentArtifactError("input must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ConsentArtifactError("input artifact must be an object")
    return payload


def evaluate_artifact(payload: dict[str, object]) -> AdmissionDecision:
    required = {"grant", "request", "policy", "evaluated_at"}
    if set(payload) != required:
        raise ConsentArtifactError(
            "artifact fields must be grant, request, policy and evaluated_at"
        )
    return evaluate_consent(
        grant_from_dict(payload["grant"]),
        request_from_dict(payload["request"]),
        policy_from_dict(payload["policy"]),
        evaluated_at=_parse_timestamp(payload["evaluated_at"], "evaluated_at"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit purpose-bound consent for one agent tool call"
    )
    parser.add_argument("artifact", type=Path, help="JSON consent admission artifact")
    args = parser.parse_args(argv)
    try:
        decision = evaluate_artifact(load_artifact(args.artifact))
    except (ConsentArtifactError, OSError) as exc:
        print(json.dumps({"accepted": False, "error": "MALFORMED_ARTIFACT", "detail": str(exc)}))
        return 3
    print(json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0 if decision.accepted else 2


if __name__ == "__main__":
    sys.exit(main())
