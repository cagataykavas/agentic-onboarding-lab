from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import os
import uuid

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from onboarding import OnboardingAgent, OnboardingCase, Stage
from service.store import OnboardingRepository, case_to_dict


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
    missing_fields: list[str] = []


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


DATABASE_PATH = Path(os.getenv("ONBOARDING_DATABASE_PATH", "onboarding.db"))
repository = OnboardingRepository(DATABASE_PATH)
app = FastAPI(
    title="Agentic Onboarding Platform",
    version="0.2.0",
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


def _persist_result(case: OnboardingCase, result) -> dict:
    repository.upsert(case)
    return {"case": case_to_dict(case), "transition": asdict(result)}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/cases", status_code=201)
def create_case(request: CreateCaseRequest) -> dict:
    case = OnboardingCase(
        case_id=f"ONB-{uuid.uuid4().hex[:12].upper()}",
        product=request.product,
        customer_type=request.customer_type,
    )
    repository.upsert(case)
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
def capture_consent(case_id: str, request: ConsentRequest) -> dict:
    case = _load(case_id)
    try:
        result = _agent_for(case).capture_consent(case, request.accepted)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _persist_result(case, result)


@app.post("/cases/{case_id}/identity")
def submit_identity(case_id: str, request: IdentityRequest) -> dict:
    case = _load(case_id)
    try:
        result = _agent_for(case).submit_identity(
            case,
            value=request.value,
            source=request.source,
            confidence=request.confidence,
            valid=request.valid,
            missing_fields=tuple(request.missing_fields),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _persist_result(case, result)


@app.patch("/cases/{case_id}/profile")
def update_profile(case_id: str, request: ProfileRequest) -> dict:
    case = _load(case_id)
    fields = {key: value for key, value in request.model_dump().items() if value is not None}
    try:
        result = _agent_for(case).update_profile(case, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _persist_result(case, result)


@app.post("/cases/{case_id}/address-evidence")
def add_address_evidence(case_id: str, request: AddressEvidenceRequest) -> dict:
    case = _load(case_id)
    result = _agent_for(case).add_address_evidence(
        case,
        address=request.address,
        confidence=request.confidence,
        conflict=request.conflict,
    )
    return _persist_result(case, result)


@app.post("/cases/{case_id}/next")
def next_step(case_id: str) -> dict:
    case = _load(case_id)
    result = _agent_for(case).next_step(case)
    return _persist_result(case, result)


@app.post("/cases/{case_id}/review")
def review(case_id: str, request: ReviewerDecisionRequest) -> dict:
    case = _load(case_id)
    try:
        result = _agent_for(case).reviewer_decision(
            case,
            approve=request.approve,
            reason=request.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _persist_result(case, result)


@app.get("/cases/{case_id}/audit")
def audit(case_id: str) -> list[dict]:
    case = _load(case_id)
    return [asdict(event) for event in case.audit]
