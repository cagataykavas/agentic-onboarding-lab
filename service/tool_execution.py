from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STATES = frozenset({"running", "succeeded", "failed"})


@dataclass(frozen=True, slots=True)
class ToolExecutionReceipt:
    """Privacy-reduced evidence for one fenced tool execution attempt."""

    schema_version: int
    call_id: str
    state: Literal["running", "succeeded", "failed"]
    attempt: int
    proposal_sha256: str
    admission_decision_sha256: str
    worker_sha256: str
    lease_expires_at: str
    result_sha256: str | None
    failure_code: str | None
    receipt_sha256: str
    should_execute: bool
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, replayed: bool = False) -> ToolExecutionReceipt:
        values = dict(payload)
        stored_digest = values.pop("receipt_sha256")
        if values.pop("schema_version") != 1:
            raise ValueError("execution receipt schema version is unsupported")
        values.pop("replayed", None)
        stored_should_execute = values.pop("should_execute")
        rebuilt = build_execution_receipt(
            **values,
            should_execute=stored_should_execute,
        )
        if rebuilt.receipt_sha256 != stored_digest:
            raise ValueError("execution receipt identity does not match its content")
        return replace(rebuilt, should_execute=False, replayed=True) if replayed else rebuilt


def build_execution_receipt(
    *,
    call_id: str,
    state: Literal["running", "succeeded", "failed"],
    attempt: int,
    proposal_sha256: str,
    admission_decision_sha256: str,
    worker_sha256: str,
    lease_expires_at: str,
    result_sha256: str | None = None,
    failure_code: str | None = None,
    should_execute: bool = False,
) -> ToolExecutionReceipt:
    if not _IDENTIFIER.fullmatch(call_id):
        raise ValueError("call_id must be a bounded stable identifier")
    if state not in _STATES:
        raise ValueError("execution state is invalid")
    if attempt < 1:
        raise ValueError("execution attempt must be positive")
    for value in (proposal_sha256, admission_decision_sha256, worker_sha256):
        if not _DIGEST.fullmatch(value):
            raise ValueError("execution identities must be lowercase SHA-256 digests")
    if result_sha256 is not None and not _DIGEST.fullmatch(result_sha256):
        raise ValueError("result identity must be a lowercase SHA-256 digest")
    if failure_code is not None and not _IDENTIFIER.fullmatch(failure_code):
        raise ValueError("failure_code must be a bounded stable identifier")
    lease = datetime.fromisoformat(lease_expires_at)
    if lease.tzinfo is None or lease.utcoffset() is None:
        raise ValueError("lease expiry must be timezone-aware")
    if state == "running" and (result_sha256 is not None or failure_code is not None):
        raise ValueError("running execution cannot contain a terminal result")
    if state == "succeeded" and (result_sha256 is None or failure_code is not None):
        raise ValueError("successful execution requires only a result identity")
    if state == "failed" and (failure_code is None or result_sha256 is not None):
        raise ValueError("failed execution requires only a failure code")

    identity = {
        "schema_version": 1,
        "call_id": call_id,
        "state": state,
        "attempt": attempt,
        "proposal_sha256": proposal_sha256,
        "admission_decision_sha256": admission_decision_sha256,
        "worker_sha256": worker_sha256,
        "lease_expires_at": lease.astimezone(UTC).isoformat(),
        "result_sha256": result_sha256,
        "failure_code": failure_code,
    }
    return ToolExecutionReceipt(
        **identity,
        receipt_sha256=canonical_digest(identity),
        should_execute=should_execute,
    )


def replay_execution(receipt: ToolExecutionReceipt) -> ToolExecutionReceipt:
    return replace(receipt, should_execute=False, replayed=True)


def hash_identifier(value: str, *, field: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a bounded stable identifier")
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
