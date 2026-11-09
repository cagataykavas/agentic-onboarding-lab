from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from service.tool_execution import canonical_digest

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ToolReconciliationReceipt:
    """Privacy-reduced evidence for one unknown provider-outcome decision."""

    schema_version: int
    call_id: str
    execution_attempt: int
    state: Literal["leased", "resolved"]
    reconciliation_attempt: int
    execution_receipt_sha256: str
    reconciler_sha256: str
    lease_expires_at: str
    decision: Literal["applied", "not_applied"] | None
    result_sha256: str | None
    terminal_execution_receipt_sha256: str | None
    receipt_sha256: str
    should_reconcile: bool
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, replayed: bool = False
    ) -> ToolReconciliationReceipt:
        values = dict(payload)
        stored_digest = values.pop("receipt_sha256")
        schema_version = values.pop("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("reconciliation receipt schema version is unsupported")
        values.pop("replayed", None)
        stored_should_reconcile = values.pop("should_reconcile")
        if type(stored_should_reconcile) is not bool:
            raise ValueError("reconciliation receipt action flag must be boolean")
        rebuilt = build_reconciliation_receipt(
            **values,
            should_reconcile=stored_should_reconcile,
        )
        if rebuilt.receipt_sha256 != stored_digest:
            raise ValueError("reconciliation receipt identity does not match its content")
        if replayed:
            return replace(rebuilt, should_reconcile=False, replayed=True)
        return rebuilt


@dataclass(frozen=True, slots=True)
class ToolReconciliationMetrics:
    """Low-cardinality snapshot derived from durable reconciliation state."""

    schema_version: int
    generated_at: str
    queue_depth: int
    queued_tasks: int
    leased_tasks: int
    expired_leases: int
    reclaimed_tasks: int
    resolved_tasks: int
    applied_resolutions: int
    not_applied_resolutions: int
    oldest_unresolved_age_seconds: int | None
    mean_resolution_latency_seconds: float | None
    max_resolution_latency_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_reconciliation_receipt(
    *,
    call_id: str,
    execution_attempt: int,
    state: Literal["leased", "resolved"],
    reconciliation_attempt: int,
    execution_receipt_sha256: str,
    reconciler_sha256: str,
    lease_expires_at: str,
    decision: Literal["applied", "not_applied"] | None = None,
    result_sha256: str | None = None,
    terminal_execution_receipt_sha256: str | None = None,
    should_reconcile: bool = False,
) -> ToolReconciliationReceipt:
    if not _IDENTIFIER.fullmatch(call_id):
        raise ValueError("call_id must be a bounded stable identifier")
    if (
        type(execution_attempt) is not int
        or type(reconciliation_attempt) is not int
        or execution_attempt < 1
        or reconciliation_attempt < 1
    ):
        raise ValueError("reconciliation attempts must be positive")
    if state not in {"leased", "resolved"}:
        raise ValueError("reconciliation state is invalid")
    for value in (execution_receipt_sha256, reconciler_sha256):
        if not _DIGEST.fullmatch(value):
            raise ValueError("reconciliation identities must be SHA-256 digests")
    for value in (result_sha256, terminal_execution_receipt_sha256):
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("reconciliation result identities must be SHA-256 digests")
    lease = datetime.fromisoformat(lease_expires_at)
    if lease.tzinfo is None or lease.utcoffset() is None:
        raise ValueError("reconciliation lease expiry must be timezone-aware")
    if state == "leased" and any(
        value is not None for value in (decision, result_sha256, terminal_execution_receipt_sha256)
    ):
        raise ValueError("leased reconciliation cannot contain a decision")
    if state == "resolved":
        if decision == "applied" and (
            result_sha256 is None or terminal_execution_receipt_sha256 is None
        ):
            raise ValueError("applied reconciliation requires terminal identities")
        if decision == "not_applied" and (
            result_sha256 is not None or terminal_execution_receipt_sha256 is not None
        ):
            raise ValueError("not-applied reconciliation cannot contain result identities")
        if decision not in {"applied", "not_applied"}:
            raise ValueError("resolved reconciliation requires a decision")
    if type(should_reconcile) is not bool:
        raise ValueError("should_reconcile must be boolean")
    if state == "resolved" and should_reconcile:
        raise ValueError("resolved reconciliation cannot authorize provider lookup")

    identity = {
        "schema_version": 1,
        "call_id": call_id,
        "execution_attempt": execution_attempt,
        "state": state,
        "reconciliation_attempt": reconciliation_attempt,
        "execution_receipt_sha256": execution_receipt_sha256,
        "reconciler_sha256": reconciler_sha256,
        "lease_expires_at": lease.astimezone(UTC).isoformat(),
        "decision": decision,
        "result_sha256": result_sha256,
        "terminal_execution_receipt_sha256": terminal_execution_receipt_sha256,
    }
    return ToolReconciliationReceipt(
        **identity,
        receipt_sha256=canonical_digest(identity),
        should_reconcile=should_reconcile,
    )


def replay_reconciliation(
    receipt: ToolReconciliationReceipt,
) -> ToolReconciliationReceipt:
    return replace(receipt, should_reconcile=False, replayed=True)
