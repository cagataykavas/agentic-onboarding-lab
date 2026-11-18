import pytest

from onboarding import Actor, OnboardingAgent, OnboardingCase, OnboardingPolicy, Stage


def complete_identity(agent: OnboardingAgent, case: OnboardingCase):
    agent.capture_consent(case, True)
    return agent.submit_identity(
        case,
        value="Ada Example",
        source="synthetic_document",
        confidence=0.97,
        valid=True,
    )


def test_consent_moves_case_to_identity_collection():
    case = OnboardingCase("1", "demo", "retail")
    result = OnboardingAgent().capture_consent(case, True)
    assert result.stage is Stage.IDENTITY_PENDING


def test_missing_profile_fields_are_requested():
    agent = OnboardingAgent()
    case = OnboardingCase("2", "demo", "retail")
    result = complete_identity(agent, case)
    assert result.stage is Stage.PROFILE_PENDING
    assert "occupation" in result.requested_fields


def test_happy_path_completes():
    agent = OnboardingAgent()
    case = OnboardingCase("3", "demo", "retail")
    complete_identity(agent, case)
    agent.update_profile(
        case,
        date_of_birth="1990-01-01",
        country="Türkiye",
        occupation="Engineer",
        address="Ankara",
    )
    result = agent.add_address_evidence(case, address="Ankara", confidence=0.95)
    assert result.stage is Stage.COMPLETED
    assert case.decision_reason == "policy_requirements_satisfied"


def test_conflicting_address_routes_to_human():
    agent = OnboardingAgent()
    case = OnboardingCase("4", "demo", "retail")
    complete_identity(agent, case)
    agent.update_profile(
        case,
        date_of_birth="1990-01-01",
        country="Türkiye",
        occupation="Engineer",
        address="Ankara",
    )
    result = agent.add_address_evidence(
        case,
        address="Istanbul",
        confidence=0.90,
        conflict=True,
    )
    assert result.stage is Stage.HUMAN_REVIEW
    assert case.human_review_required


def test_audit_trail_records_stage_changes():
    agent = OnboardingAgent()
    case = OnboardingCase("5", "demo", "retail")
    agent.capture_consent(case, True)
    assert len(case.audit) >= 2
    assert case.audit[0].stage_before is Stage.STARTED


def test_policy_provenance_is_canonical_and_configuration_sensitive():
    first = OnboardingPolicy(
        mandatory_review_flags=["ownership_conflict", "identity_conflict"],
        policy_version="policy-17",
    ).provenance()
    reordered = OnboardingPolicy(
        mandatory_review_flags=["identity_conflict", "ownership_conflict"],
        policy_version="policy-17",
    ).provenance()
    stricter = OnboardingPolicy(
        minimum_identity_confidence=0.95,
        mandatory_review_flags=["identity_conflict", "ownership_conflict"],
        policy_version="policy-17",
    ).provenance()

    assert first == reordered
    assert first["digest"].startswith("sha256:")
    assert len(first["digest"]) == 71
    assert first["digest"] != stricter["digest"]


@pytest.mark.parametrize("threshold", [-0.01, 1.01, float("nan"), float("inf")])
def test_policy_rejects_invalid_confidence_thresholds(threshold: float):
    with pytest.raises(ValueError, match="finite number between 0 and 1"):
        OnboardingPolicy(minimum_identity_confidence=threshold)


def test_automated_audit_events_capture_policy_snapshot():
    policy = OnboardingPolicy(policy_version="policy-17")
    agent = OnboardingAgent(policy)
    case = OnboardingCase("6", "demo", "retail")

    agent.capture_consent(case, True)

    customer_event, agent_event = case.audit
    assert customer_event.actor is Actor.CUSTOMER
    assert "policy_provenance" not in customer_event.details
    assert agent_event.actor is Actor.AGENT
    assert agent_event.details["policy_provenance"] == policy.provenance()
