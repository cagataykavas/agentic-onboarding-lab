from dataclasses import replace

import pytest

from audit_integrity import build_checkpoint, verify_checkpoint
from onboarding import Actor, AuditEvent, Stage


def event(event_id: int, details: dict[str, object] | None = None) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        case_id="ONB-DEMO",
        actor=Actor.SYSTEM,
        event_type=f"event_{event_id}",
        stage_before=Stage.STARTED,
        stage_after=Stage.CONSENT_CAPTURED,
        timestamp=f"2026-09-18T10:00:0{event_id}+00:00",
        details=details or {},
    )


def test_checkpoint_is_deterministic_and_json_ready():
    events = [event(1, {"confidence": 0.98}), event(2, {"fields": ["name", "country"]})]
    first = build_checkpoint(events)
    second = build_checkpoint(events)
    assert first == second
    assert first.event_count == 2
    assert len(first.terminal_digest) == 64
    assert first.to_dict()["algorithm"] == "sha256"


@pytest.mark.parametrize(
    "mutated",
    [
        [event(1), event(2, {"decision": "changed"})],
        [event(2), event(1)],
        [event(1)],
        [event(1), event(2), event(3)],
    ],
)
def test_detects_mutation_reordering_deletion_and_append(mutated):
    checkpoint = build_checkpoint([event(1), event(2)])
    result = verify_checkpoint(mutated, checkpoint)
    assert result.valid is False
    assert result.reason in {"event_count_mismatch", "terminal_digest_mismatch"}


def test_mapping_key_order_does_not_change_digest():
    left = build_checkpoint([event(1, {"b": 2, "a": 1})])
    right = build_checkpoint([event(1, {"a": 1, "b": 2})])
    assert left == right


def test_accepts_matching_external_checkpoint():
    events = [event(1), event(2)]
    result = verify_checkpoint(events, build_checkpoint(events))
    assert result.valid is True
    assert result.reason is None


def test_rejects_unknown_checkpoint_policy():
    events = [event(1)]
    expected = replace(build_checkpoint(events), schema_version=99)
    result = verify_checkpoint(events, expected)
    assert result.valid is False
    assert result.reason == "unsupported_checkpoint_policy"


def test_rejects_non_finite_and_unsupported_values():
    with pytest.raises(ValueError):
        build_checkpoint([event(1, {"score": float("nan")})])
    with pytest.raises(TypeError, match="unsupported audit value"):
        build_checkpoint([event(1, {"opaque": object()})])
