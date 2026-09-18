"""Tamper-evident checkpoints for onboarding audit trails."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from hmac import compare_digest


GENESIS_DIGEST = "0" * 64


@dataclass(frozen=True)
class AuditCheckpoint:
    event_count: int
    terminal_digest: str
    algorithm: str = "sha256"
    schema_version: int = 1

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    observed: AuditCheckpoint
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "observed": self.observed.to_dict(),
            "reason": self.reason,
        }


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_value(item) for item in value), key=repr)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported audit value: {type(value).__name__}")


def _event_payload(event: object) -> bytes:
    normalized = _json_value(event)
    if not isinstance(normalized, dict):
        raise TypeError("audit events must be dataclasses or mappings")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_checkpoint(events: Iterable[object]) -> AuditCheckpoint:
    """Build a deterministic chain where every digest commits to prior history."""
    previous = bytes.fromhex(GENESIS_DIGEST)
    count = 0
    for count, event in enumerate(events, start=1):
        payload = _event_payload(event)
        previous = hashlib.sha256(previous + len(payload).to_bytes(8, "big") + payload).digest()
    return AuditCheckpoint(event_count=count, terminal_digest=previous.hex())


def verify_checkpoint(
    events: Iterable[object], expected: AuditCheckpoint
) -> VerificationResult:
    """Compare an audit trail with a checkpoint stored outside the case record."""
    if expected.algorithm != "sha256" or expected.schema_version != 1:
        observed = build_checkpoint(events)
        return VerificationResult(False, observed, "unsupported_checkpoint_policy")

    observed = build_checkpoint(events)
    if observed.event_count != expected.event_count:
        return VerificationResult(False, observed, "event_count_mismatch")
    if not compare_digest(observed.terminal_digest, expected.terminal_digest):
        return VerificationResult(False, observed, "terminal_digest_mismatch")
    return VerificationResult(True, observed)
