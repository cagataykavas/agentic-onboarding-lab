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


def test_audit_trail_records_stage_changes():
    agent = OnboardingAgent()
    case = OnboardingCase("5", "demo", "retail")
    agent.capture_consent(case, True)
    assert len(case.audit) >= 2
    assert case.audit[0].stage_before is Stage.STARTED
