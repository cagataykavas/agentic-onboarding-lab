from datetime import UTC, datetime, timedelta

from onboarding import Actor, AuditEvent, EvidenceRecord, EvidenceStatus, OnboardingCase, Stage
from service.metrics import MetricsCalculator
from service.reviews import ReviewTask


def event(case_id: str, event_type: str) -> AuditEvent:
    return AuditEvent(
        event_id=1,
        case_id=case_id,
        actor=Actor.SYSTEM,
        event_type=event_type,
        stage_before=Stage.RISK_SCREENING,
        stage_after=Stage.COMPLETED,
        timestamp="2026-01-01T00:00:00+00:00",
        details={},
    )


def test_metrics_are_derived_from_audit_history_and_queue_sla() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    automatic = OnboardingCase("a", "account", "retail", stage=Stage.COMPLETED)
    automatic.audit.append(event("a", "automated_approval"))
    automatic.evidence.append(
        EvidenceRecord(
            "ev-1",
            "identity",
            "synthetic",
            "test",
            EvidenceStatus.VALID,
            0.99,
            now.isoformat(),
        )
    )
    escalated = OnboardingCase("b", "account", "retail", stage=Stage.HUMAN_REVIEW)
    escalated.audit.append(event("b", "case_escalated"))
    task = ReviewTask(
        case_id="b",
        status="queued",
        priority=80,
        reason="conflict",
        enqueued_at=(now - timedelta(hours=2)).isoformat(),
        due_at=(now - timedelta(hours=1)).isoformat(),
    )

    metrics = MetricsCalculator().calculate([automatic, escalated], [task], now=now)

    assert metrics.completion_rate == 0.5
    assert metrics.human_review_rate == 0.5
    assert metrics.automated_approvals == 1
    assert metrics.average_evidence_per_case == 0.5
    assert metrics.overdue_reviews == 1
    assert metrics.stage_counts == {"completed": 1, "human_review": 1}
