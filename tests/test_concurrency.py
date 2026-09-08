import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from onboarding import OnboardingAgent, OnboardingCase
from service.store import OnboardingRepository, VersionConflict


def test_compare_and_swap_allows_only_one_stale_writer(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "cases.db")
    repository.create(OnboardingCase("case-1", "current-account", "retail"))
    first = repository.get("case-1")
    second = repository.get("case-1")
    assert first is not None and second is not None

    OnboardingAgent().capture_consent(first, True)
    repository.save(first, expected_version=1)
    OnboardingAgent().capture_consent(second, False)

    with pytest.raises(VersionConflict):
        repository.save(second, expected_version=1)

    stored = repository.get("case-1")
    assert stored is not None
    assert stored.consent is True
    assert stored.version == 2
    assert [event.event_type for event in stored.audit] == [
        "consent_granted",
        "request_identity_evidence",
    ]


def test_mutations_are_serialized_without_lost_audit_events(tmp_path: Path) -> None:
    repository = OnboardingRepository(tmp_path / "cases.db")
    repository.create(OnboardingCase("case-2", "current-account", "retail"))
    barrier = threading.Barrier(2)

    def consent(accepted: bool) -> str:
        barrier.wait()
        try:
            case, _ = repository.mutate(
                "case-2",
                lambda current: OnboardingAgent().capture_consent(current, accepted),
                expected_version=1,
            )
            return f"ok:{case.version}"
        except (VersionConflict, ValueError) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consent, [True, False]))

    assert sum(item.startswith("ok:") for item in results) == 1
    assert any(item in {"VersionConflict", "ValueError"} for item in results)
    stored = repository.get("case-2")
    assert stored is not None
    assert stored.version == 2
    assert stored.audit
