from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from onboarding import OnboardingCase, Stage
from service.reviews import ReviewTask


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class JourneyMetrics:
    total_cases: int
    completed_cases: int
    completion_rate: float
    automated_approvals: int
    reviewer_decisions: int
    human_review_rate: float
    average_evidence_per_case: float
    queued_reviews: int
    leased_reviews: int
    overdue_reviews: int
    stage_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class MetricsCalculator:
    """Derive operational metrics from case histories, not mutable counters."""

    def calculate(
        self,
        cases: list[OnboardingCase],
        review_tasks: list[ReviewTask],
        *,
        now: datetime | None = None,
    ) -> JourneyMetrics:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        stage_counts = Counter(case.stage.value for case in cases)
        completed = sum(case.stage is Stage.COMPLETED for case in cases)
        automated = sum(
            any(event.event_type == "automated_approval" for event in case.audit)
            for case in cases
        )
        reviewed = sum(
            any(event.event_type == "reviewer_decision" for event in case.audit)
            for case in cases
        )
        escalated = sum(
            any(event.event_type == "case_escalated" for event in case.audit)
            for case in cases
        )
        evidence_count = sum(len(case.evidence) for case in cases)
        queued = sum(task.status == "queued" for task in review_tasks)
        leased = sum(task.status == "leased" for task in review_tasks)
        overdue = sum(
            task.status != "completed" and parse_timestamp(task.due_at) < now
            for task in review_tasks
        )
        total = len(cases)
        return JourneyMetrics(
            total_cases=total,
            completed_cases=completed,
            completion_rate=completed / total if total else 0.0,
            automated_approvals=automated,
            reviewer_decisions=reviewed,
            human_review_rate=escalated / total if total else 0.0,
            average_evidence_per_case=evidence_count / total if total else 0.0,
            queued_reviews=queued,
            leased_reviews=leased,
            overdue_reviews=overdue,
            stage_counts=dict(sorted(stage_counts.items())),
        )
