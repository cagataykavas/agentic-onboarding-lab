# Reconciliation health alert gate

`service.reconciliation_alerts` turns the low-cardinality reconciliation snapshot into a
deterministic operational decision. Thresholds live in a reviewed policy artifact rather than
application code, so a release or scheduled health check can identify stale telemetry, stuck
unknown outcomes, expired worker leases, and slow resolution without adding task-level labels.

This gate does not guess suitable production objectives. Operators must choose thresholds from the
provider lookup objective, reconciler capacity, tolerated queueing delay, and incident policy.

## Policy contract

The policy is strict JSON. Duplicate or unknown fields, booleans in numeric fields, non-finite
numbers, zero freshness windows, and out-of-range values fail closed.

```json
{
  "schema_version": 1,
  "policy_id": "reconciliation-production-v1",
  "max_snapshot_age_seconds": 60,
  "thresholds": {
    "max_queue_depth": 25,
    "max_expired_leases": 0,
    "max_oldest_unresolved_age_seconds": 300,
    "max_mean_resolution_latency_seconds": 120,
    "max_resolution_latency_seconds": 300
  }
}
```

All thresholds are inclusive: an observed value alerts only when it is greater than the reviewed
maximum. Latency thresholds are evaluated only after at least one resolution; an empty latency
series is `null`, not zero. Queue age is evaluated only when unresolved work exists.

## Evaluation

Capture the service-owned snapshot and evaluate it promptly:

```bash
curl --fail --silent \
  http://127.0.0.1:8000/metrics/tool-reconciliation \
  --output artifacts/reconciliation-snapshot.json

python -m service.reconciliation_alerts \
  artifacts/reconciliation-snapshot.json \
  policies/reconciliation-alerts.json \
  --output artifacts/reconciliation-alert-decision.json
```

The decision artifact contains canonical SHA-256 identities for the complete policy and snapshot,
the policy ID, evaluation and snapshot timestamps, snapshot age, and stable violations. It never
contains call IDs, case IDs, worker identities, provider references, or raw tool results.

| Exit | Meaning |
|---:|---|
| `0` | fresh, internally consistent snapshot is within every threshold |
| `1` | well-formed evidence breaches one or more reviewed thresholds |
| `2` | policy or snapshot is malformed, ambiguous, unsupported, or future-dated |

Staleness is an alert (`1`), because missing recent telemetry is operationally actionable. A
snapshot more than five seconds in the future is invalid (`2`), because its age cannot be trusted.

## Fail-closed invariants

Before applying thresholds, the gate verifies:

- `queue_depth == queued_tasks + leased_tasks`;
- expired leases cannot exceed leased work;
- resolved tasks equal `applied` plus `not_applied` decisions;
- reclaimed tasks cannot exceed all retained tasks;
- non-empty queues have an oldest unresolved age and empty queues do not;
- resolved tasks have both mean and maximum latency, while an empty series has neither; and
- maximum resolution latency is not below the mean.

Inputs are capped at 128 KiB and must be singly linked regular files. Symlinks, hard links,
read-time mutation, duplicate fields, non-finite numbers, and counters outside bounded ranges are
rejected. Reports are written through `fsync` and atomic replacement.

## Limitations and next step

The policy and snapshot digests detect later modification but do not authenticate the files or
prove the snapshot was scraped from the intended service. One point-in-time aggregate cannot
establish a rate, percentile distribution, or sustained breach, and cumulative latency can be
biased by retention. The example thresholds are illustrative rather than claimed production SLOs.

The next step is to scrape the same fixed schema into a trusted telemetry backend, define a bounded
retention or verified rollup policy, and require sustained-window alert evidence before paging or
automated release rollback.
