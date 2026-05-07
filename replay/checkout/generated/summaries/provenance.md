# Checkout Replay Provenance

This file answers: which evidence caused the decision to change?

Baseline decision: `PASS` for `baseline_separate_domains`.

## payment_shared_fate_drift

Decision change: `PASS` baseline -> `FAIL` drift.

Evidence signals:
- `fixtures/evidence/payment_shared_fate_drift/deployment-domains.yaml`: `services.paya.labels.emac.dev/effective-payment-domain` and `services.payb.labels.emac.dev/effective-payment-domain` both report `payment-network-shared`.
- `fixtures/evidence/payment_shared_fate_drift/trace-evidence.yaml`: payment spans for `paya` and `payb` both carry `emac.provider_domain: payment-network-shared` while also sharing `emac.redundancy_group: payment_race`.
- `generated/deltas/payment_shared_fate_drift.delta.yaml`: `spec.changes` records `failure_domain_assignment` changes for both payment branches and a `payment_race_domain_relation` change from `separate_effective_domains` to `shared_effective_domain`.

Compiler effect: `A_plus` remains unchanged, while `A_minus` drops by -0.00496257.

## missing_or_conflicting_evidence

Decision change: `PASS` baseline -> `REVIEW` weak evidence.

Evidence signals:
- `fixtures/evidence/missing_or_conflicting_evidence/trace-evidence.yaml`: traces include `front -> paya` but do not include `front -> payb`, so the payment race is not fully covered by trace evidence.
- `fixtures/evidence/missing_or_conflicting_evidence/deployment-domains.yaml`: `services.paya.labels.emac.dev/effective-payment-domain` reports `payment-network-a`, while `services.paya.provider.network` reports `payment-network-shared`; `services.payb.domain_observations` is empty.
- `generated/deltas/missing_or_conflicting_evidence.delta.yaml`: `spec.changes` records an untrusted payment-domain assignment and an `unknown_or_conflicting` payment race relation.

Governance effect: the compiler emits an analysis bound, but policy refuses automatic acceptance because confidence is below the auto-accept threshold and evidence flags are present.
