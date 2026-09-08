from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from onboarding import OnboardingCase
from service.reviews import ReviewLeaseConflict, ReviewQueue
from service.store import OnboardingRepository


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, minutes: int) -> None:
        self.value += timedelta(minutes=minutes)


def queue_for(tmp_path: Path, clock: Clock) -> ReviewQueue:
    repository = OnboardingRepository(tmp_path / "queue.db")
    repository.create(OnboardingCase("low", "account", "retail"))
    repository.create(OnboardingCase("urgent", "account", "retail"))
    return ReviewQueue(repository, clock=clock)


def test_queue_orders_by_priority_then_sla(tmp_path: Path) -> None:
    clock = Clock()
    queue = queue_for(tmp_path, clock)
    queue.enqueue("low", reason="routine", priority=20, sla_minutes=10)
    queue.enqueue("urgent", reason="identity_conflict", priority=90, sla_minutes=60)

    assert [task.case_id for task in queue.list_ready()] == ["urgent", "low"]


def test_expired_lease_returns_to_queue_and_cannot_complete(tmp_path: Path) -> None:
    clock = Clock()
    queue = queue_for(tmp_path, clock)
    queue.enqueue("urgent", reason="identity_conflict", priority=90)
    task = queue.claim_next("reviewer-a", lease_minutes=5)
    assert task is not None and task.lease_owner == "reviewer-a"
    assert queue.list_ready() == []

    clock.advance(6)
    assert [task.case_id for task in queue.list_ready()] == ["urgent"]
    with pytest.raises(ReviewLeaseConflict, match="expired"):
        queue.complete("urgent", "reviewer-a")

    reclaimed = queue.claim_next("reviewer-b")
    assert reclaimed is not None and reclaimed.lease_owner == "reviewer-b"
    completed = queue.complete("urgent", "reviewer-b")
    assert completed.status == "completed"
