from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from onboarding import OnboardingAgent, OnboardingCase, Stage
from service.metrics import MetricsCalculator
from service.reviews import ReviewQueue
from service.store import (
    IdempotencyConflict,
    OnboardingRepository,
    VersionConflict,
    case_to_dict,
)


class CreateCaseRequest(BaseModel):
    product: str = Field(default="Everyday Banking", min_length=2, max_length=120)
    customer_type: str = Field(default="retail", min_length=2, max_length=80)


class ConsentRequest(BaseModel):
    accepted: bool


class IdentityRequest(BaseModel):
    value: str = Field(min_length=2, max_length=250)
    source: str = Field(default="document_capture", min_length=2, max_length=120)
    confidence: float = Field(ge=0.0, le=1.0)
    valid: bool
    missing_fields: list[str] = Field(default_factory=list)


class ProfileRequest(BaseModel):
    full_name: str | None = None
    date_of_birth: str | None = None
    country: str | None = None
    address: str | None = None
    occupation: str | None = None


class AddressEvidenceRequest(BaseModel):
    address: str = Field(min_length=2, max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)
    conflict: bool = False


class ReviewerDecisionRequest(BaseModel):
    approve: bool
    reason: str = Field(min_length=5, max_length=1000)
    reviewer: str = Field(default="reviewer-demo", min_length=2, max_length=120)


class ClaimReviewRequest(BaseModel):
    reviewer: str = Field(min_length=2, max_length=120)
    lease_minutes: int = Field(default=15, ge=1, le=120)


DATABASE_PATH = Path(os.getenv("ONBOARDING_DATABASE_PATH", "onboarding.db"))
repository = OnboardingRepository(DATABASE_PATH)
review_queue = ReviewQueue(repository)
app = FastAPI(
    title="Agentic Onboarding Platform",
    version="0.3.0",
    description="Policy-bounded onboarding workflow with explicit human escalation and audit trails.",
)


def _load(case_id: str) -> OnboardingCase:
    case = repository.get(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


def _agent_for(case: OnboardingCase) -> OnboardingAgent:
    agent = OnboardingAgent()
    agent._event_counter = max((event.event_id for event in case.audit), default=0)
    return agent


def _fingerprint(action: str, payload: dict[str, object] | None = None) -> str:
    canonical = json.dumps(
        {"action": action, "payload": payload or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _mutate(
    case_id: str,
    operation,
    expected_version: int | None,
    idempotency_key: str | None,
    command_fingerprint: str,
    review_lease_owner: str | None = None,
) -> dict:
    try:
        case, result = repository.mutate(
            case_id,
            operation,
            expected_version=expected_version,
            command_id=idempotency_key,
            command_fingerprint=command_fingerprint,
            review_lease_owner=review_lease_owner,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc
    except (VersionConflict, IdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(result, dict) and result.get("idempotent_replay"):
        return {
            "case": case_to_dict(case),
            "transition": result["transition"],
            "idempotent_replay": True,
        }
    return {
        "case": case_to_dict(case),
        "transition": asdict(result),
        "idempotent_replay": False,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics/journey")
def journey_metrics() -> dict[str, object]:
    cases = repository.list(limit=500)
    tasks = review_queue.list_all(limit=5000)
    return MetricsCalculator().calculate(cases, tasks).to_dict()


@app.post("/cases", status_code=201)
def create_case(request: CreateCaseRequest) -> dict:
    case = OnboardingCase(
        case_id=f"ONB-{uuid.uuid4().hex[:12].upper()}",
        product=request.product,
        customer_type=request.customer_type,
    )
    repository.create(case)
    return case_to_dict(case)


@app.get("/cases")
def list_cases(
    stage: Stage | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    return [case_to_dict(case) for case in repository.list(stage=stage, limit=limit)]


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    return case_to_dict(_load(case_id))


@app.post("/cases/{case_id}/consent")
def capture_consent(
    case_id: str,
    request: ConsentRequest,
    if_match: int | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    return _mutate(
        case_id,
        lambda case: _agent_for(case).capture_consent(case, request.accepted),
        if_match,
        idempotency_key,
        _fingerprint("capture_consent", request.model_dump()),
    )


@app.post("/cases/{case_id}/identity")
def submit_identity(
    case_id: str,
    request: IdentityRequest,
    if_match: int | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    def operation(case: OnboardingCase):
        return _agent_for(case).submit_identity(
            case,
            value=request.value,
            source=request.source,
            confidence=request.confidence,
            valid=request.valid,
            missing_fields=tuple(request.missing_fields),
        )

    return _mutate(
        case_id,
        operation,
        if_match,
        idempotency_key,
        _fingerprint("submit_identity", request.model_dump()),
    )


@app.patch("/cases/{case_id}/profile")
def update_profile(
    case_id: str,
    request: ProfileRequest,
    if_match: int | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    fields = {key: value for key, value in request.model_dump().items() if value is not None}
    return _mutate(
        case_id,
        lambda case: _agent_for(case).update_profile(case, **fields),
        if_match,
        idempotency_key,
        _fingerprint("update_profile", request.model_dump()),
    )


@app.post("/cases/{case_id}/address-evidence")
def add_address_evidence(
    case_id: str,
    request: AddressEvidenceRequest,
    if_match: int | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    return _mutate(
        case_id,
        lambda case: _agent_for(case).add_address_evidence(
            case,
            address=request.address,
            confidence=request.confidence,
            conflict=request.conflict,
        ),
        if_match,
        idempotency_key,
        _fingerprint("add_address_evidence", request.model_dump()),
    )


@app.post("/cases/{case_id}/next")
def next_step(
    case_id: str,
    if_match: int | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    return _mutate(
        case_id,
        lambda case: _agent_for(case).next_step(case),
        if_match,
        idempotency_key,
        _fingerprint("next_step"),
    )


@app.post("/cases/{case_id}/review")
def review(
    case_id: str,
    request: ReviewerDecisionRequest,
    if_match: int | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    result = _mutate(
        case_id,
        lambda case: _agent_for(case).reviewer_decision(
            case, approve=request.approve, reason=request.reason
        ),
        if_match,
        idempotency_key,
        _fingerprint("reviewer_decision", request.model_dump()),
        request.reviewer,
    )
    result["review_task"] = "completed"
    return result


@app.get("/reviews/queue")
def list_review_queue(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    return [task.to_dict() for task in review_queue.list_ready(limit=limit)]


@app.post("/reviews/claim")
def claim_review(request: ClaimReviewRequest) -> dict:
    task = review_queue.claim_next(request.reviewer, lease_minutes=request.lease_minutes)
    if task is None:
        raise HTTPException(status_code=404, detail="review queue is empty")
    return task.to_dict()


@app.get("/cases/{case_id}/audit")
def audit(case_id: str) -> list[dict]:
    case = _load(case_id)
    return [asdict(event) for event in case.audit]
