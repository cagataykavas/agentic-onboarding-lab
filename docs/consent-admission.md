# Purpose-bound consent admission

Customer consent is not a boolean permission for every action an onboarding agent might take. This
gate turns consent into a bounded authorization checked immediately before an external tool call.

## Contract

An admission artifact contains four objects:

- a consent grant bound to one case, policy version, terms version, purpose set, data-category set,
  tool allowlist and validity window;
- a proposed tool request containing metadata only, never raw customer evidence or tool arguments;
- a server-owned policy mapping tools to their allowed purposes and minimum data vocabulary;
- a timezone-aware evaluation timestamp supplied by the trusted execution boundary.

The gate accepts only when all scopes intersect exactly enough to authorize the call. It rejects
unknown tools, case or grant mismatches, stale/future requests, expired or revoked grants, excessive
grant lifetimes, purpose drift and data-category expansion.

```bash
python -m service.consent_admission admission.json
```

Exit code `0` means admitted, `2` means valid evidence was rejected by policy and `3` means the
artifact itself was malformed. JSON parsing rejects duplicate fields and non-finite values, and the
input is capped at 128 KiB.

## Evidence and privacy

The result contains stable reason codes, policy/tool metadata and SHA-256 identities for the grant,
complete server policy, request and final decision. Case, grant and request identifiers are represented by hashes. The
artifact must contain no raw document text, biometric payload, address, model prompt or tool output.

SHA-256 makes exact content changes visible; it does not authenticate who issued the grant. A
production issuer should sign the canonical grant or protect it with a KMS-backed MAC and persist
revocation state in an authoritative, strongly consistent store.

## Trust boundaries

- Tool rules, the evaluation clock and current revocation state are server-owned inputs.
- Admission must run directly before dispatch. A decision is not a reusable bearer token.
- The executor must bind the admitted request digest to the exact arguments it dispatches; this
  reference gate intentionally handles metadata rather than sensitive arguments.
- Consent does not replace legal-basis analysis, authentication, per-tool authorization, human
  review, rate limiting or downstream data-retention controls.
- Revocation cannot undo a side effect already committed by an external provider. The service needs
  cancellation/reconciliation procedures for in-flight calls.

## Calibration and next step

Keep purposes and data categories few, reviewed and versioned. Grant lifetimes should reflect the
shortest operational window that still supports the customer journey. Monitor rejection codes, but
never log the submitted grant or customer identifiers.

The next increment should persist signed consent receipts and revocations transactionally with the
case, then bind the admission decision digest to the outbound tool request and resulting evidence.
