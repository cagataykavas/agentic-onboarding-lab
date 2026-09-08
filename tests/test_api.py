from pathlib import Path

from fastapi.testclient import TestClient

import service.api as api_module
from service.reviews import ReviewQueue
from service.store import OnboardingRepository


def client_for(tmp_path: Path) -> TestClient:
    api_module.repository = OnboardingRepository(tmp_path / "onboarding.db")
    api_module.review_queue = ReviewQueue(api_module.repository)
    return TestClient(api_module.app)


def test_happy_path_onboarding(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    created = client.post("/cases", json={"product": "Everyday Banking", "customer_type": "retail"})
    assert created.status_code == 201
    case_id = created.json()["case_id"]

    consent = client.post(f"/cases/{case_id}/consent", json={"accepted": True})
    assert consent.status_code == 200
    assert consent.json()["case"]["stage"] == "identity_pending"

    identity = client.post(
        f"/cases/{case_id}/identity",
        json={
            "value": "Ada Example",
            "source": "synthetic_document",
            "confidence": 0.98,
            "valid": True,
            "missing_fields": [],
        },
    )
    assert identity.status_code == 200

    profile = client.patch(
        f"/cases/{case_id}/profile",
        json={
            "date_of_birth": "1995-05-04",
            "country": "Türkiye",
            "occupation": "Engineer",
        },
    )
    assert profile.status_code == 200

    address = client.post(
        f"/cases/{case_id}/address-evidence",
        json={"address": "Ankara", "confidence": 0.96, "conflict": False},
    )
    assert address.status_code == 200
    assert address.json()["case"]["stage"] == "completed"

    audit = client.get(f"/cases/{case_id}/audit")
    assert audit.status_code == 200
    assert len(audit.json()) >= 5


def test_conflict_routes_to_human_review(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    case_id = client.post("/cases", json={}).json()["case_id"]
    client.post(f"/cases/{case_id}/consent", json={"accepted": True})
    client.post(
        f"/cases/{case_id}/identity",
        json={
            "value": "Ada Example",
            "source": "synthetic_document",
            "confidence": 0.99,
            "valid": True,
        },
    )
    client.patch(
        f"/cases/{case_id}/profile",
        json={
            "date_of_birth": "1995-05-04",
            "country": "Türkiye",
            "occupation": "Engineer",
        },
    )
    result = client.post(
        f"/cases/{case_id}/address-evidence",
        json={"address": "Ankara", "confidence": 0.93, "conflict": True},
    )
    assert result.status_code == 200
    assert result.json()["case"]["stage"] == "human_review"

    queued = client.get("/reviews/queue")
    assert queued.status_code == 200
    assert queued.json()[0]["case_id"] == case_id
    claimed = client.post(
        "/reviews/claim", json={"reviewer": "analyst-1", "lease_minutes": 15}
    )
    assert claimed.status_code == 200
    assert claimed.json()["lease_owner"] == "analyst-1"

    unauthorized = client.post(
        f"/cases/{case_id}/review",
        json={
            "approve": False,
            "reason": "A different analyst must not decide leased work.",
            "reviewer": "analyst-2",
        },
    )
    assert unauthorized.status_code == 409
    assert client.get(f"/cases/{case_id}").json()["stage"] == "human_review"

    reviewed = client.post(
        f"/cases/{case_id}/review",
        json={
            "approve": True,
            "reason": "Synthetic conflict was manually resolved.",
            "reviewer": "analyst-1",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["case"]["stage"] == "completed"
    assert reviewed.json()["case"]["decision_outcome"] == "approved"
    assert client.get("/reviews/queue").json() == []


def test_stale_if_match_is_rejected_without_losing_state(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    created = client.post("/cases", json={}).json()
    case_id = created["case_id"]
    assert created["version"] == 1

    accepted = client.post(
        f"/cases/{case_id}/consent",
        headers={"If-Match": "1"},
        json={"accepted": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["case"]["version"] == 2

    stale = client.post(
        f"/cases/{case_id}/consent",
        headers={"If-Match": "1"},
        json={"accepted": False},
    )
    assert stale.status_code == 409
    current = client.get(f"/cases/{case_id}").json()
    assert current["stage"] == "identity_pending"
    assert current["version"] == 2


def test_idempotency_key_replays_without_duplicate_transition(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    created = client.post("/cases", json={}).json()
    case_id = created["case_id"]
    headers = {"If-Match": "1", "Idempotency-Key": "consent-command-1"}

    first = client.post(
        f"/cases/{case_id}/consent", headers=headers, json={"accepted": True}
    )
    replay = client.post(
        f"/cases/{case_id}/consent", headers=headers, json={"accepted": True}
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["case"]["version"] == 2
    assert len(replay.json()["case"]["audit"]) == 2

    changed_payload = client.post(
        f"/cases/{case_id}/consent", headers=headers, json={"accepted": False}
    )
    assert changed_payload.status_code == 409
    assert "different command" in changed_payload.json()["detail"]
