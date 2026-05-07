# Checkout Model Discovery Replay Sanity Check

This directory contains the executable sanity replay for the checkout artifact.
It is separated from `examples/checkout/` so the static example configuration
and the generated replay layer are easy to inspect independently. The replay
demonstrates the reviewable chain:

```text
intent + operational evidence -> discovered model delta -> compiled optimistic/pessimistic bounds -> governance decision
```

This is a worked replay, not a production implementation of EmaC, not an
evaluation, and not a benchmark. The fixtures are synthetic and intentionally
small so reviewers can inspect the mechanism without Kubernetes, Prometheus,
Argo Rollouts, a tracing backend, or cloud access.

## Relation to the Existing Checkout Example

The canonical checkout intent at `../../examples/checkout/spec/emac.checkout.yaml`
declares a shared `payment-network` failure domain for `paya` and `payb`, and
the canonical derivation report at
`../../examples/checkout/compiled/report/derivation.checkout.yaml` shows the
conservative shared-fate failure. Those files are not mutated by this replay.

The replay reads the canonical EmaC intent and validates that the checkout
operator and objectives are the expected paper example. It also uses a
scenario-specific accepted model fixture:

```text
fixtures/accepted/baseline.accepted-model.yaml
```

That fixture represents the last accepted model before drift, where `paya` and
`payb` were assumed or inferred to be in separate effective payment domains.
The `payment_shared_fate_drift` scenario then moves from that accepted baseline
to the shared-domain model already illustrated by the canonical checkout
example.

The component IDs follow the existing repository convention: `paya` and `payb`
are the lower-case IDs for the payment alternatives.

## Inputs

Human-readable input fixtures live under `fixtures/`:

```text
fixtures/
  accepted/
    baseline.accepted-model.yaml
  atomic/
    checkout-atomic-availability.yaml
  evidence/
    baseline_separate_domains/
      scenario.yaml
      trace-evidence.yaml
      deployment-domains.yaml
    payment_shared_fate_drift/
      scenario.yaml
      trace-evidence.yaml
      deployment-domains.yaml
    missing_or_conflicting_evidence/
      scenario.yaml
      trace-evidence.yaml
      deployment-domains.yaml
  policy/
    sanity-policy.yaml
```

The atomic availability values match the existing checkout derivation:

```text
front=0.9995, cart=0.999, pricing=0.999, paya=0.995, payb=0.995
```

These local inputs stay unchanged across scenarios. The decision changes only
because the discovered journey model changes, or because confidence is too low
for automatic reconciliation.

## Scenarios

`baseline_separate_domains` shows the passing case. Trace evidence supports the
checkout edges, both payment alternatives are observed as a hedged race, and
deployment/provider metadata places `paya` and `payb` in separate effective
payment domains. The conservative bound keeps the race redundancy.

`payment_shared_fate_drift` shows the core shared-fate drift case. Local atomic
availability inputs are unchanged, but trace and deployment metadata now put
both payment alternatives in `payment-network-shared`. The generated delta
records a typed failure-domain assignment change for both payment branches.

`missing_or_conflicting_evidence` shows conservative behavior under weak
instrumentation. Traces show only `paya`, `payb` has no domain observations, and
`paya` metadata conflicts. The replay still emits an analysis model and bound,
but confidence is below the policy threshold, so governance returns `REVIEW`.

## Inference Heuristics

The replay implements only the minimum method needed for this sanity check:

```text
Inferred field          Evidence signal                         Confidence basis
operator edges          trace parent/child span linkage          trace coverage
payment race            overlapping payment alternatives          trace/intent agreement
branch frequencies      observed trace frequencies               deterministic counts
failure domains         deployment/provider labels + span attrs   agreement or conflict
model delta             diff from accepted model                  changed fields + A_minus impact
```

The operator graph is taken from the accepted fixture and validated against
trace evidence. Failure-domain assignment is evidence-backed and is the main
source of the model delta.

## Compiler Rules

The compiler is intentionally small and deterministic. It uses the checkout
operator graph:

```text
Series(front, Parallel(cart, pricing), Race(paya, payb))
```

Availability formulas:

```text
Parallel(cart, pricing, join=all) = A_cart * A_pricing
Race optimistic                  = 1 - (1 - A_paya) * (1 - A_payb)
A_plus                           = A_front * A_cart * A_pricing * Race optimistic
```

Pessimistic payment rule:

```text
separate known payment domains    -> use the optimistic race formula
shared effective payment domain   -> collapse race to max(A_paya, A_payb)
unknown or conflicting evidence   -> collapse race to max(A_paya, A_payb) and require REVIEW
```

The shared-fate rule matches the existing checkout derivation: full correlation
inside the payment domain removes the payment race availability gain. With the
fixture values, the shared-fate scenario produces the same rounded values as
the existing derivation report:

```text
A_plus  = 0.99747706
A_minus = 0.99251449
```

## Governance

The policy fixture is `fixtures/policy/sanity-policy.yaml`.

`PASS` means the pessimistic bound meets the objective, confidence is at or
above the auto-accept threshold, no high-impact unresolved delta is present, and
evidence is not flagged as incomplete or conflicting.

`REVIEW` means automation should not auto-accept the model or promote based on
it. The replay returns `REVIEW` when confidence is below threshold, evidence is
missing or conflicting, or a high-impact delta requires operator review.

`FAIL` means the conservative bound violates the objective with sufficient
confidence.

## Generated Outputs

Run:

```bash
python replay/checkout/tool/replay.py
```

The script uses Python 3 and PyYAML. No network access or live infrastructure is
required. The output is deterministic and is written inside this directory:

```text
  generated/
  models/
    baseline_separate_domains.discovered-model.yaml
    payment_shared_fate_drift.discovered-model.yaml
    missing_or_conflicting_evidence.discovered-model.yaml
  deltas/
    payment_shared_fate_drift.delta.yaml
    missing_or_conflicting_evidence.delta.yaml
  reports/
    baseline_separate_domains.derivation.yaml
    payment_shared_fate_drift.derivation.yaml
    missing_or_conflicting_evidence.derivation.yaml
  summaries/
    summary.json
    summary.csv
    summary.md
    provenance.md
```

`generated/summaries/summary.md` contains the compact table intended for paper
discussion. `generated/summaries/summary.json` and
`generated/summaries/summary.csv` are machine-readable summaries.
`generated/summaries/provenance.md` answers which fixture fields caused the
decision to change.

## Simplifications

This replay does not implement a Kubernetes, OpenTelemetry, Prometheus, or Argo
integration. It does not infer arbitrary operator graphs, optimize policies, or
learn a probabilistic model. It uses fixed synthetic traces, simple metadata
signals, deterministic confidence heuristics, and a single checkout compiler so
the Model Discovery and governance story is inspectable.
