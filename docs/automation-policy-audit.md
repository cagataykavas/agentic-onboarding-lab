# Automation policy calibration gate

An agent confidence threshold should not be promoted from a point accuracy alone. A small sample can
show zero observed errors while still supporting a large plausible production error rate.

`service.automation_policy_audit` evaluates a bounded retrospective outcome artifact before a new
onboarding automation policy is released. It checks:

- Brier score and expected calibration error;
- a 95% Wilson upper bound for the automated error rate;
- medium-impact slice evidence and error uncertainty;
- zero-tolerance automation of mandatory-review cases;
- consistency between the recorded automation decision and candidate confidence threshold;
- evidence freshness, strict schema, unique cases and finite probabilities.

```bash
python -m service.automation_policy_audit \
  --input automation-outcomes.json \
  --output automation-audit.json
```

Exit code `0` means accepted, `2` means the release policy rejected valid evidence and `3` means the
artifact or invocation was malformed. The report contains aggregates and deterministic SHA-256
identities, but not case identifiers.

## Evidence contract and boundary

Each record needs a confidence, realized correctness label, observed automation decision,
mandatory-review flag and impact tier. Labels must be collected after the outcome is known and must
represent the intended deployment population. This gate does not detect dataset shift, label leakage,
selective missing labels or asymmetric business costs. Production promotion should also use a
versioned cohort manifest, delayed-outcome handling and post-release drift monitoring.
