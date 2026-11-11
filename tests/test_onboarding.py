from onboarding import OnboardingAgent, OnboardingCase, Stage


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
    recommendation = case.agent_recommendation
    assert recommendation is not None
    assert recommendation.outcome == "rejected"
    assert recommendation.reason_codes == ("address_conflict",)
    assert recommendation.evidence_ids == ("identity-1", "address-2")
    assert recommendation.policy_version == "onboarding-policy-v1"
    assert case.audit[-1].details["recommendation_id"] == recommendation.recommendation_id


def test_reviewer_decision_is_paired_with_agent_recommendation():
    agent = OnboardingAgent()
    case = OnboardingCase("paired", "demo", "retail")
    complete_identity(agent, case)
    agent.update_profile(
        case,
        date_of_birth="1990-01-01",
        country="Türkiye",
        occupation="Engineer",
        address="Ankara",
    )
    agent.add_address_evidence(case, address="Istanbul", confidence=0.90, conflict=True)
    recommendation = case.agent_recommendation
    assert recommendation is not None

    agent.reviewer_decision(
        case,
        approve=True,
        reason="Synthetic conflict resolved.",
        recommendation_id=recommendation.recommendation_id,
    )

    decision = next(event for event in case.audit if event.event_type == "reviewer_decision")
    assert decision.details["recommendation_id"] == recommendation.recommendation_id
    assert decision.details["agreed_with_agent"] is False


def test_audit_trail_records_stage_changes():
    agent = OnboardingAgent()
    case = OnboardingCase("5", "demo", "retail")
    agent.capture_consent(case, True)
    assert len(case.audit) >= 2
    assert case.audit[0].stage_before is Stage.STARTED
