from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from onboarding import OnboardingCase, Stage

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Server-owned capability contract; never accept this from the planner."""

    name: str
    revision: str
    allowed_stages: frozenset[Stage]
    required_arguments: tuple[str, ...] = ()
    optional_arguments: tuple[str, ...] = ()
    max_admissions_per_case: int = 1

    def __post_init__(self) -> None:
        arguments = self.required_arguments + self.optional_arguments
        if not _IDENTIFIER.fullmatch(self.name) or not _IDENTIFIER.fullmatch(self.revision):
            raise ValueError("tool name and revision must be stable identifiers")
        if not self.allowed_stages:
            raise ValueError("a tool must declare at least one allowed stage")
        if len(arguments) != len(set(arguments)):
            raise ValueError("tool arguments must be unique")
        if any(not _IDENTIFIER.fullmatch(argument) for argument in arguments):
            raise ValueError("tool arguments must be stable identifiers")
        if not 1 <= self.max_admissions_per_case <= 100:
            raise ValueError("max_admissions_per_case must be in [1, 100]")


@dataclass(frozen=True, slots=True)
class ToolProposal:
    call_id: str
    tool_name: str
    expected_case_version: int
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.call_id):
            raise ValueError("call_id must be a bounded stable identifier")
        if not _IDENTIFIER.fullmatch(self.tool_name):
            raise ValueError("tool_name must be a bounded stable identifier")
        if self.expected_case_version < 1:
            raise ValueError("expected_case_version must be positive")
        if not isinstance(self.arguments, dict):
            raise TypeError("arguments must be an object")

    @property
    def sha256(self) -> str:
        return _json_digest(
            {
                "call_id": self.call_id,
                "tool_name": self.tool_name,
                "expected_case_version": self.expected_case_version,
                "arguments": self.arguments,
            }
        )


@dataclass(frozen=True, slots=True)
class ToolAdmission:
    schema_version: int
    call_id: str
    tool_name: str
    tool_revision: str | None
    allowed: bool
    reasons: tuple[str, ...]
    case_id_sha256: str
    case_version: int
    case_stage: str
    proposal_sha256: str
    policy_sha256: str
    decision_sha256: str
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, replayed: bool = False) -> ToolAdmission:
        values = dict(payload)
        values["reasons"] = tuple(values["reasons"])
        values["replayed"] = replayed
        return cls(**values)


class ToolAdmissionPolicy:
    """Fail-closed admission for untrusted planner tool proposals."""

    def __init__(
        self,
        specs: tuple[ToolSpec, ...],
        *,
        max_argument_bytes: int = 4096,
        max_argument_value_chars: int = 512,
    ) -> None:
        if not specs or len({spec.name for spec in specs}) != len(specs):
            raise ValueError("tool catalog must be non-empty with unique names")
        if not 128 <= max_argument_bytes <= 65_536:
            raise ValueError("max_argument_bytes must be in [128, 65536]")
        if not 1 <= max_argument_value_chars <= 4096:
            raise ValueError("max_argument_value_chars must be in [1, 4096]")
        self._specs = {spec.name: spec for spec in specs}
        self.max_argument_bytes = max_argument_bytes
        self.max_argument_value_chars = max_argument_value_chars

    @property
    def sha256(self) -> str:
        specs = []
        for spec in sorted(self._specs.values(), key=lambda item: item.name):
            specs.append(
                {
                    "name": spec.name,
                    "revision": spec.revision,
                    "allowed_stages": sorted(stage.value for stage in spec.allowed_stages),
                    "required_arguments": list(spec.required_arguments),
                    "optional_arguments": list(spec.optional_arguments),
                    "max_admissions_per_case": spec.max_admissions_per_case,
                }
            )
        return _json_digest(
            {
                "specs": specs,
                "max_argument_bytes": self.max_argument_bytes,
                "max_argument_value_chars": self.max_argument_value_chars,
            }
        )

    def evaluate(
        self,
        case: OnboardingCase,
        proposal: ToolProposal,
        *,
        prior_admissions: int = 0,
    ) -> ToolAdmission:
        if prior_admissions < 0:
            raise ValueError("prior_admissions cannot be negative")

        proposal_sha256 = proposal.sha256
        policy_sha256 = self.sha256
        reasons: list[str] = []
        spec = self._specs.get(proposal.tool_name)

        if case.version != proposal.expected_case_version:
            reasons.append("case_version_mismatch")
        if case.stage is Stage.COMPLETED:
            reasons.append("terminal_case")
        if not case.consent:
            reasons.append("consent_required")
        if spec is None:
            reasons.append("unknown_tool")
        else:
            if case.stage not in spec.allowed_stages:
                reasons.append("stage_not_allowed")
            supplied = set(proposal.arguments)
            required = set(spec.required_arguments)
            allowed_arguments = required | set(spec.optional_arguments)
            if required - supplied:
                reasons.append("missing_arguments")
            if supplied - allowed_arguments:
                reasons.append("unexpected_arguments")
            if any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > self.max_argument_value_chars
                for value in proposal.arguments.values()
            ):
                reasons.append("invalid_argument_value")
            if prior_admissions >= spec.max_admissions_per_case:
                reasons.append("call_budget_exceeded")

        if len(_canonical_json(proposal.arguments)) > self.max_argument_bytes:
            reasons.append("argument_payload_too_large")

        identity = {
            "schema_version": 1,
            "call_id": proposal.call_id,
            "tool_name": proposal.tool_name,
            "tool_revision": spec.revision if spec else None,
            "allowed": not reasons,
            "reasons": tuple(reasons),
            "case_id_sha256": _sha256(case.case_id.encode()),
            "case_version": case.version,
            "case_stage": case.stage.value,
            "proposal_sha256": proposal_sha256,
            "policy_sha256": policy_sha256,
        }
        return ToolAdmission(
            **identity,
            decision_sha256=_json_digest(identity),
        )


def default_tool_policy() -> ToolAdmissionPolicy:
    return ToolAdmissionPolicy(
        (
            ToolSpec(
                name="identity_document_check",
                revision="v1",
                allowed_stages=frozenset({Stage.IDENTITY_PENDING, Stage.NEEDS_CUSTOMER_INPUT}),
                required_arguments=("evidence_id",),
                optional_arguments=("document_country",),
                max_admissions_per_case=3,
            ),
            ToolSpec(
                name="address_registry_lookup",
                revision="v1",
                allowed_stages=frozenset({Stage.PROFILE_PENDING, Stage.NEEDS_CUSTOMER_INPUT}),
                required_arguments=("address_evidence_id",),
                optional_arguments=("country",),
                max_admissions_per_case=2,
            ),
            ToolSpec(
                name="risk_screening",
                revision="v1",
                allowed_stages=frozenset({Stage.RISK_SCREENING}),
                required_arguments=("profile_snapshot_id",),
                max_admissions_per_case=1,
            ),
        )
    )


def replay(admission: ToolAdmission) -> ToolAdmission:
    return replace(admission, replayed=True)


def _canonical_json(payload: Any) -> bytes:
    if _contains_non_finite(payload):
        raise ValueError("tool proposal values must be finite")
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("tool proposal must be JSON-serializable") from error


def _contains_non_finite(payload: Any) -> bool:
    if isinstance(payload, float):
        return not math.isfinite(payload)
    if isinstance(payload, dict):
        return any(
            _contains_non_finite(key) or _contains_non_finite(value)
            for key, value in payload.items()
        )
    if isinstance(payload, (list, tuple)):
        return any(_contains_non_finite(value) for value in payload)
    return False


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_digest(payload: Any) -> str:
    return _sha256(_canonical_json(payload))
