# Emergence-as-Code (EmaC) — Example Configuration Repository

This repository accompanies the manuscript **"Emergence-as-Code for Self-Governing Reliable Systems"**.

It contains a **worked example** of the artifacts referenced in the paper:

- an **EmaC intent spec** (journey objective + operator graph + policy),
- a sample **Model Discovery output** (evidence-backed topology/routing hypothesis),
- **compiled governance artifacts**:
  - Prometheus recording/alerting rules (multiwindow, multi-burn-rate burn alerts),
  - Argo Rollouts `AnalysisTemplate` (progressive-delivery gate based on budget burn and tail latency).

> **Scope note:** This is **not** a full implementation of EmaC.
> The files are intentionally small and illustrative so that reviewers/readers can see
> what “intent”, “evidence”, and “compiled governance artifacts” look like in practice.

---

## Repository layout

```
examples/
  checkout/
    spec/
      emac.checkout.yaml              # EmaC intent + policy + bindings
    model_discovery/
      discovered.checkout.yaml        # Example discovered journey model (evidence)
    atomic/openslo/
      datasource.prometheus.yaml      # OpenSLO datasource
      slo.front.availability.yaml     # Atomic availability SLOs (examples)
      slo.cart.availability.yaml
      slo.pricing.availability.yaml
      slo.paya.availability.yaml
      slo.payb.availability.yaml
    compiled/
      prometheus/
        recording-rules.yaml          # Derived SLI & helper time-series
        alerts.yaml                   # Multiwindow, multi-burn-rate SLO alerts
      argo/
        analysis-template.yaml        # Canary gate (budget burn + latency)
        rollout-snippet.yaml          # Example Rollout step referencing the template
      report/
        derivation.checkout.yaml      # Example derivation output (bounds + assumptions)
```

---

## The worked example: Checkout journey

The journey being modeled is a simplified **checkout** flow with:

- **Series** composition over major stages
- a **Parallel** fan-out join to `cart` and `pricing`
- a **Race** redundancy pattern for `payA` vs `payB` (hedged or fallback-like behavior)

High-level operator graph:

```
J_checkout = Series(
  front,
  Parallel(cart, pricing),
  Race(payA, payB)
)
```

The key point is that **journey reliability is emergent**: it depends on routing, redundancy semantics,
shared failure domains, and tail behavior — not just per-service targets.

In this example, `payA` and `payB` have the same local availability, but may share a **payment-network**
failure domain. The **pessimistic bound** (full correlation inside the domain) removes the benefit of redundancy,
and the derived journey availability can drop below the journey objective.

---

## How to use these files

### 1) Read the intent
Start with:

- `examples/checkout/spec/emac.checkout.yaml`

This is the **EmaC source-of-truth**: objectives, operator semantics, failure-domain rules,
and the policy that says what the platform automation is allowed to do.

### 2) Inspect the evidence
Then look at:

- `examples/checkout/model_discovery/discovered.checkout.yaml`

This is a **representative output** of Model Discovery: the inferred topology/routing
and domain hypotheses that keep the model calibrated.

### 3) Apply compiled artifacts
If you want to *sketch* the deployment integration:

- Prometheus rules:
  - `kubectl apply -f examples/checkout/compiled/prometheus/recording-rules.yaml`
  - `kubectl apply -f examples/checkout/compiled/prometheus/alerts.yaml`

- Argo Rollouts analysis gate:
  - `kubectl apply -f examples/checkout/compiled/argo/analysis-template.yaml`

Then reference the AnalysisTemplate in your Rollout (see `rollout-snippet.yaml`).

> You will need to adapt metric names/labels to your environment.

---

## License

The repository content is provided for research/educational use.
