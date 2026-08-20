from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class Stage(str, Enum):
    STARTED = "started"
    CONSENT_CAPTURED = "consent_captured"
    IDENTITY_PENDING = "identity_pending"
    IDENTITY_VERIFIED = "identity_verified"
    PROFILE_PENDING = "profile_pending"
    RISK_SCREENING = "risk_screening"
    NEEDS_CUSTOMER_INPUT = "needs_customer_input"
    HUMAN_REVIEW = "human_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class EvidenceStatus(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    CONFLICT = "conflict"


class Actor(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    kind: str
    value: str
    source: str
    status: EvidenceStatus
    confidence: float
    collected_at: str
    missing_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditEvent:
    event_id: int
    case_id: str
    actor: Actor
    event_type: str
    stage_before: Stage
    stage_after: Stage
    timestamp: str
    details: dict[str, object]


@dataclass
class OnboardingCase:
    case_id: str
    product: str
    customer_type: str
    stage: Stage = Stage.STARTED
    consent: bool = False
    profile: dict[str, str] = field(default_factory=dict)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    requested_fields: list[str] = field(default_factory=list)
    risk_flags: set[str] = field(default_factory=set)
    human_review_required: bool = False
    decision_reason: str | None = None
    audit: list[AuditEvent] = field(default_factory=list)

    def add_evidence(self, record: EvidenceRecord) -> None:
        self.evidence.append(record)

    def evidence_of_kind(self, kind: str) -> list[EvidenceRecord]:
        return [item for item in self.evidence if item.kind == kind]

    def latest_evidence(self, kind: str) -> EvidenceRecord | None:
        rows = self.evidence_of_kind(kind)
        return rows[-1] if rows else None


@dataclass(frozen=True)
class TransitionResult:
    stage: Stage
    message: str
    next_action: str
    requested_fields: tuple[str, ...] = ()


class OnboardingPolicy:
    REQUIRED_PROFILE_FIELDS = (
        "full_name",
        "date_of_birth",
        "country",
        "address",
        "occupation",
    )

    def __init__(
        self,
        *,
        minimum_identity_confidence: float = 0.90,
        minimum_address_confidence: float = 0.80,
        mandatory_review_flags: Iterable[str] = (
            "identity_conflict",
            "ownership_conflict",
            "high_risk_country",
        ),
    ) -> None:
        self.minimum_identity_confidence = minimum_identity_confidence
        self.minimum_address_confidence = minimum_address_confidence
        self.mandatory_review_flags = set(mandatory_review_flags)

    def missing_profile_fields(self, case: OnboardingCase) -> list[str]:
        return [
            field_name
            for field_name in self.REQUIRED_PROFILE_FIELDS
            if not case.profile.get(field_name)
        ]

    def identity_valid(self, case: OnboardingCase) -> bool:
        identity = case.latest_evidence("identity")
        return bool(
            identity
            and identity.status is EvidenceStatus.VALID
            and identity.confidence >= self.minimum_identity_confidence
        )

    def address_valid(self, case: OnboardingCase) -> bool:
        address = case.latest_evidence("address")
        if address is None:
            return False
        return (
            address.status is EvidenceStatus.VALID
            and address.confidence >= self.minimum_address_confidence
        )

    def requires_human_review(self, case: OnboardingCase) -> bool:
        if case.human_review_required:
            return True
        if self.mandatory_review_flags & case.risk_flags:
            return True
        if any(item.status is EvidenceStatus.CONFLICT for item in case.evidence):
            return True
        return False


class OnboardingAgent:
    """Deterministic reference orchestrator for an agentic onboarding flow.

    A production implementation could replace selected planning steps with an
    LLM/tool-calling agent. The policy boundary stays explicit so the planner
    cannot silently bypass mandatory review or evidence requirements.
    """

    def __init__(self, policy: OnboardingPolicy | None = None) -> None:
        self.policy = policy or OnboardingPolicy()
        self._event_counter = 0

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _record(
        self,
        case: OnboardingCase,
        *,
        actor: Actor,
        event_type: str,
        before: Stage,
        after: Stage,
        details: dict[str, object] | None = None,
    ) -> None:
        self._event_counter += 1
        case.audit.append(
            AuditEvent(
                event_id=self._event_counter,
                case_id=case.case_id,
                actor=actor,
                event_type=event_type,
                stage_before=before,
                stage_after=after,
                timestamp=self._now(),
                details=details or {},
            )
        )

    def _transition(
        self,
        case: OnboardingCase,
        stage: Stage,
        *,
        actor: Actor,
        event_type: str,
        details: dict[str, object] | None = None,
    ) -> None:
        before = case.stage
        case.stage = stage
        self._record(
            case,
            actor=actor,
            event_type=event_type,
            before=before,
            after=stage,
            details=details,
        )

    def capture_consent(self, case: OnboardingCase, accepted: bool) -> TransitionResult:
        if case.stage is not Stage.STARTED:
            raise ValueError("consent can only be captured at the start of a case")
        if not accepted:
            case.decision_reason = "consent_not_granted"
            self._transition(
                case,
                Stage.COMPLETED,
                actor=Actor.CUSTOMER,
                event_type="consent_declined",
            )
            return TransitionResult(
                case.stage,
                "Onboarding stopped because consent was not granted.",
                "end",
            )

        case.consent = True
        self._transition(
            case,
            Stage.CONSENT_CAPTURED,
            actor=Actor.CUSTOMER,
            event_type="consent_granted",
        )
        self._transition(
            case,
            Stage.IDENTITY_PENDING,
            actor=Actor.AGENT,
            event_type="request_identity_evidence",
        )
        return TransitionResult(
            case.stage,
            "Please provide an identity document so we can continue.",
            "collect_identity",
        )

    def submit_identity(
        self,
        case: OnboardingCase,
        *,
        value: str,
        source: str,
        confidence: float,
        valid: bool,
        missing_fields: tuple[str, ...] = (),
    ) -> TransitionResult:
        if case.stage not in {Stage.IDENTITY_PENDING, Stage.NEEDS_CUSTOMER_INPUT}:
            raise ValueError("identity evidence is not expected at this stage")

        status = EvidenceStatus.VALID if valid else EvidenceStatus.INVALID
        record = EvidenceRecord(
            evidence_id=f"identity-{len(case.evidence) + 1}",
            kind="identity",
            value=value,
            source=source,
            status=status,
            confidence=confidence,
            collected_at=self._now(),
            missing_fields=missing_fields,
        )
        case.add_evidence(record)

        if not valid or confidence < self.policy.minimum_identity_confidence:
            case.requested_fields[:] = list(missing_fields or ("clear_identity_document",))
            self._transition(
                case,
                Stage.NEEDS_CUSTOMER_INPUT,
                actor=Actor.AGENT,
                event_type="identity_needs_retry",
                details={"confidence": confidence, "missing": list(case.requested_fields)},
            )
            return TransitionResult(
                case.stage,
                "We could not verify the identity evidence yet. Please try again.",
                "collect_identity",
                tuple(case.requested_fields),
            )

        case.profile.setdefault("full_name", value)
        self._transition(
            case,
            Stage.IDENTITY_VERIFIED,
            actor=Actor.SYSTEM,
            event_type="identity_verified",
            details={"confidence": confidence},
        )
        self._transition(
            case,
            Stage.PROFILE_PENDING,
            actor=Actor.AGENT,
            event_type="profile_completion_started",
        )
        return self.next_step(case)

    def update_profile(self, case: OnboardingCase, **fields: str) -> TransitionResult:
        if case.stage not in {Stage.PROFILE_PENDING, Stage.NEEDS_CUSTOMER_INPUT}:
            raise ValueError("profile information is not expected at this stage")
        accepted = {key: value for key, value in fields.items() if value}
        case.profile.update(accepted)
        self._record(
            case,
            actor=Actor.CUSTOMER,
            event_type="profile_fields_submitted",
            before=case.stage,
            after=case.stage,
            details={"fields": sorted(accepted)},
        )
        return self.next_step(case)

    def add_address_evidence(
        self,
        case: OnboardingCase,
        *,
        address: str,
        confidence: float,
        conflict: bool = False,
    ) -> TransitionResult:
        record = EvidenceRecord(
            evidence_id=f"address-{len(case.evidence) + 1}",
            kind="address",
            value=address,
            source="synthetic_registry",
            status=EvidenceStatus.CONFLICT if conflict else EvidenceStatus.VALID,
            confidence=confidence,
            collected_at=self._now(),
        )
        case.add_evidence(record)
        case.profile.setdefault("address", address)
        if conflict:
            case.risk_flags.add("address_conflict")
            case.human_review_required = True
        return self.next_step(case)

    def next_step(self, case: OnboardingCase) -> TransitionResult:
        if not case.consent:
            return TransitionResult(case.stage, "Consent is required.", "collect_consent")

        if not self.policy.identity_valid(case):
            if case.stage is not Stage.IDENTITY_PENDING:
                self._transition(
                    case,
                    Stage.IDENTITY_PENDING,
                    actor=Actor.AGENT,
                    event_type="identity_required",
                )
            return TransitionResult(
                case.stage,
                "Identity verification is required before continuing.",
                "collect_identity",
            )

        missing = self.policy.missing_profile_fields(case)
        if missing:
            case.requested_fields[:] = missing
            if case.stage is not Stage.PROFILE_PENDING:
                self._transition(
                    case,
                    Stage.PROFILE_PENDING,
                    actor=Actor.AGENT,
                    event_type="profile_fields_required",
                    details={"fields": missing},
                )
            return TransitionResult(
                case.stage,
                "We only need the remaining profile details.",
                "collect_profile",
                tuple(missing),
            )

        if not self.policy.address_valid(case):
            return TransitionResult(
                case.stage,
                "We are validating your address information.",
                "validate_address",
            )

        if case.stage is not Stage.RISK_SCREENING:
            self._transition(
                case,
                Stage.RISK_SCREENING,
                actor=Actor.SYSTEM,
                event_type="risk_screening_started",
            )

        return self.screen_risk(case)

    def screen_risk(self, case: OnboardingCase) -> TransitionResult:
        country = case.profile.get("country", "").strip().lower()
        occupation = case.profile.get("occupation", "").strip().lower()

        # Synthetic rules for portfolio demonstration only.
        if country in {"demo-high-risk-country"}:
            case.risk_flags.add("high_risk_country")
        if occupation in {"synthetic-high-risk-occupation"}:
            case.risk_flags.add("enhanced_due_diligence")

        if self.policy.requires_human_review(case):
            self._transition(
                case,
                Stage.HUMAN_REVIEW,
                actor=Actor.SYSTEM,
                event_type="case_escalated",
                details={"risk_flags": sorted(case.risk_flags)},
            )
            return TransitionResult(
                case.stage,
                "A specialist will review the information before we complete onboarding.",
                "human_review",
            )

        self._transition(
            case,
            Stage.APPROVED,
            actor=Actor.SYSTEM,
            event_type="automated_approval",
            details={"risk_flags": sorted(case.risk_flags)},
        )
        case.decision_reason = "policy_requirements_satisfied"
        return self.complete(case)

    def reviewer_decision(
        self,
        case: OnboardingCase,
        *,
        approve: bool,
        reason: str,
    ) -> TransitionResult:
        if case.stage is not Stage.HUMAN_REVIEW:
            raise ValueError("case is not awaiting human review")

        final_stage = Stage.APPROVED if approve else Stage.REJECTED
        case.decision_reason = reason
        self._transition(
            case,
            final_stage,
            actor=Actor.REVIEWER,
            event_type="reviewer_decision",
            details={"approve": approve, "reason": reason},
        )
        return self.complete(case)

    def complete(self, case: OnboardingCase) -> TransitionResult:
        if case.stage not in {Stage.APPROVED, Stage.REJECTED}:
            raise ValueError("only a decided case can be completed")
        outcome = "approved" if case.stage is Stage.APPROVED else "rejected"
        self._transition(
            case,
            Stage.COMPLETED,
            actor=Actor.SYSTEM,
            event_type="onboarding_completed",
            details={"outcome": outcome, "reason": case.decision_reason},
        )
        return TransitionResult(
            case.stage,
            f"Onboarding is complete. Outcome: {outcome}.",
            "end",
        )


def demo() -> None:
    agent = OnboardingAgent()
    case = OnboardingCase(
        case_id="ONB-DEMO-001",
        product="Everyday Banking",
        customer_type="retail",
    )

    print(agent.capture_consent(case, True))
    print(
        agent.submit_identity(
            case,
            value="Ada Example",
            source="synthetic_document",
            confidence=0.97,
            valid=True,
        )
    )
    print(
        agent.update_profile(
            case,
            date_of_birth="1995-05-04",
            country="Türkiye",
            occupation="Engineer",
        )
    )
    print(agent.add_address_evidence(case, address="Ankara", confidence=0.94))

    print("\nAudit trail")
    for event in case.audit:
        print(
            f"{event.event_id:02d} | {event.actor.value:8s} | "
            f"{event.stage_before.value:22s} -> {event.stage_after.value:22s} | "
            f"{event.event_type}"
        )


if __name__ == "__main__":
    demo()
