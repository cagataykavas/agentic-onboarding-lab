from pathlib import Path

from fastapi.testclient import TestClient

import service.api as api_module
from service.store import OnboardingRepository


def client_for(tmp_path: Path) -> TestClient:
    api_module.repository = OnboardingRepository(tmp_path / "onboarding.db")
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

    reviewed = client.post(
        f"/cases/{case_id}/review",
        json={"approve": True, "reason": "Synthetic conflict was manually resolved."},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["case"]["stage"] == "completed"
