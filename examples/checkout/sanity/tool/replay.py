#!/usr/bin/env python3
"""Deterministic Model Discovery sanity replay for the checkout example."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import csv
import json
import sys

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only without PyYAML
    raise SystemExit(
        "The checkout sanity replay requires PyYAML. "
        "Install the dependency from examples/checkout/sanity/requirements.txt."
    ) from exc


SANITY_ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_ROOT = SANITY_ROOT.parent
FIXTURES = SANITY_ROOT / "fixtures"
GENERATED = SANITY_ROOT / "generated"

SCENARIO_ORDER = [
    "baseline_separate_domains",
    "payment_shared_fate_drift",
    "missing_or_conflicting_evidence",
]
COMPONENTS = ["front", "cart", "pricing", "paya", "payb"]
PAYMENT_BRANCHES = ["paya", "payb"]
EXPECTED_EDGES = [
    ["front", "cart"],
    ["front", "pricing"],
    ["front", "paya"],
    ["front", "payb"],
]


class StableDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.dump(
            payload,
            handle,
            Dumper=StableDumper,
            sort_keys=False,
            allow_unicode=False,
            default_flow_style=False,
            width=100,
        )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")


def rel(path: Path) -> str:
    path = path.resolve()
    try:
        return path.relative_to(SANITY_ROOT).as_posix()
    except ValueError:
        return "../" + path.relative_to(CHECKOUT_ROOT).as_posix()


def r8(value: float) -> float:
    return round(float(value), 8)


def edge_key(edge: list[str] | tuple[str, str]) -> str:
    return f"{edge[0]}->{edge[1]}"


def expression_from_intent(intent: dict) -> str:
    operator = intent["spec"]["operator"]
    series_steps = operator["steps"]
    front = series_steps[0]["ref"]
    parallel = series_steps[1]
    race = series_steps[2]
    parallel_refs = ", ".join(branch["ref"] for branch in parallel["branches"])
    race_refs = ", ".join(branch["ref"] for branch in race["branches"])
    return f"Series({front}, Parallel({parallel_refs}), Race({race_refs}))"


def validate_canonical_intent(intent: dict) -> None:
    expression = expression_from_intent(intent)
    expected = "Series(front, Parallel(cart, pricing), Race(paya, payb))"
    objective = float(intent["spec"]["objectives"]["availability"]["target"])
    latency = float(intent["spec"]["objectives"]["latency"]["objective"]["threshold"])
    if expression != expected:
        raise ValueError(f"canonical intent operator mismatch: {expression!r}")
    if objective != 0.995:
        raise ValueError(f"canonical intent availability target mismatch: {objective!r}")
    if latency != 0.4:
        raise ValueError(f"canonical intent p99 threshold mismatch: {latency!r}")


def span_overlaps(left: dict, right: dict) -> bool:
    left_start = int(left.get("start_ms", 0))
    left_end = left_start + int(left.get("duration_ms", 0))
    right_start = int(right.get("start_ms", 0))
    right_end = right_start + int(right.get("duration_ms", 0))
    return left_start < right_end and right_start < left_end


def ordered_unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def extract_atomic_availability(atomic: dict) -> dict[str, float]:
    components = atomic["spec"]["components"]
    return {name: float(components[name]["availability"]) for name in COMPONENTS}


def infer_edges(spans: list[dict]) -> tuple[list[dict], float, dict]:
    spans_by_id = {span["span_id"]: span for span in spans}
    expected = {edge_key(edge) for edge in EXPECTED_EDGES}
    support = defaultdict(list)

    for span in spans:
        parent_id = span.get("parent_span_id")
        if not parent_id:
            continue
        parent = spans_by_id.get(parent_id)
        if not parent:
            continue
        pair = [parent.get("service"), span.get("service")]
        key = edge_key(pair)
        if key in expected:
            support[key].append(
                {
                    "trace_id": span["trace_id"],
                    "parent_span_id": parent_id,
                    "span_id": span["span_id"],
                }
            )

    observed = set(support)
    missing = sorted(expected - observed)
    coverage = len(observed) / len(expected)
    confidence = 0.95 if coverage == 1.0 else max(0.20, coverage * 0.85)

    edges = []
    for edge in EXPECTED_EDGES:
        key = edge_key(edge)
        edges.append(
            {
                "from": edge[0],
                "to": edge[1],
                "callType": "sync",
                "status": "observed" if key in observed else "expected_from_accepted_model_only",
                "confidence": r8(confidence if key in observed else 0.20),
                "provenance": support.get(key, []),
            }
        )

    diagnostics = {
        "expectedEdges": sorted(expected),
        "observedEdges": sorted(observed),
        "missingEdges": missing,
        "coverage": r8(coverage),
    }
    return edges, r8(confidence), diagnostics


def infer_race(spans: list[dict]) -> tuple[dict, float, list[str]]:
    trace_ids = sorted({span["trace_id"] for span in spans})
    payment_by_trace = defaultdict(list)
    branch_counts = Counter()

    for span in spans:
        attrs = span.get("attributes") or {}
        if span.get("service") in PAYMENT_BRANCHES or attrs.get("emac.redundancy_group") == "payment_race":
            payment_by_trace[span["trace_id"]].append(span)
            if span.get("service") in PAYMENT_BRANCHES:
                branch_counts[span["service"]] += 1

    traces_with_any_payment = 0
    traces_with_both = 0
    overlapping_traces = 0
    for trace_spans in payment_by_trace.values():
        services = {span.get("service") for span in trace_spans}
        if services & set(PAYMENT_BRANCHES):
            traces_with_any_payment += 1
        if set(PAYMENT_BRANCHES).issubset(services):
            traces_with_both += 1
            paya_spans = [span for span in trace_spans if span.get("service") == "paya"]
            payb_spans = [span for span in trace_spans if span.get("service") == "payb"]
            if any(span_overlaps(left, right) for left in paya_spans for right in payb_spans):
                overlapping_traces += 1

    missing_branches = [branch for branch in PAYMENT_BRANCHES if branch_counts[branch] == 0]
    overlap_ratio = overlapping_traces / traces_with_any_payment if traces_with_any_payment else 0.0
    if not missing_branches and overlap_ratio >= 0.80:
        confidence = 0.95
        status = "trace_and_intent_agree"
    elif not missing_branches:
        confidence = 0.75
        status = "branches_observed_without_consistent_overlap"
    else:
        confidence = 0.35
        status = "incomplete_branch_evidence"

    root_trace_count = max(1, len(trace_ids))
    branch_frequency = {
        branch: r8(branch_counts[branch] / root_trace_count) for branch in PAYMENT_BRANCHES
    }
    flags = ["incomplete_payment_branch_coverage"] if missing_branches else []
    race = {
        "id": "payment_race",
        "pattern": "race",
        "semantics": "hedged",
        "group": PAYMENT_BRANCHES,
        "status": status,
        "branchObservationFrequency": branch_frequency,
        "overlapRatio": r8(overlap_ratio),
        "tracesWithBothBranches": traces_with_both,
        "confidence": r8(confidence),
        "provenance": {
            "traceSignal": "payment spans share emac.redundancy_group=payment_race and overlap in time",
            "observedPaymentTraces": traces_with_any_payment,
        },
    }
    return race, r8(confidence), flags


def collect_domain_observations(deployment: dict, trace: dict, deployment_path: Path, trace_path: Path) -> dict:
    observations = {component: [] for component in COMPONENTS}

    for component, service in (deployment.get("services") or {}).items():
        if component not in observations:
            continue
        for item in service.get("domain_observations") or []:
            if not item.get("value"):
                continue
            observations[component].append(
                {
                    "value": str(item["value"]),
                    "file": rel(deployment_path),
                    "field": item.get("field", f"services.{component}.domain_observations"),
                    "signal": item.get("signal", "domain observation"),
                }
            )

    for span in trace.get("spans") or []:
        component = span.get("service")
        if component not in PAYMENT_BRANCHES:
            continue
        attrs = span.get("attributes") or {}
        provider_domain = attrs.get("emac.provider_domain")
        if provider_domain:
            observations[component].append(
                {
                    "value": str(provider_domain),
                    "file": rel(trace_path),
                    "field": f"spans.{span['span_id']}.attributes.emac.provider_domain",
                    "signal": "trace payment-domain attribute",
                }
            )

    return observations


def choose_domains(observations: dict) -> tuple[dict, dict, float, list[str]]:
    effective = {}
    details = {}
    flags = []

    for component in COMPONENTS:
        component_observations = observations.get(component, [])
        values = [item["value"] for item in component_observations]

        if not values:
            effective[component] = ["unknown"]
            details[component] = {
                "domains": ["unknown"],
                "status": "missing",
                "confidence": 0.20,
                "observations": [],
            }
            flags.append(f"missing_domain_evidence:{component}")
            continue

        if component not in PAYMENT_BRANCHES:
            domains = ordered_unique(values)
            effective[component] = domains
            details[component] = {
                "domains": domains,
                "status": "consistent",
                "confidence": 0.95,
                "observations": component_observations,
            }
            continue

        counts = Counter(values)
        if len(counts) == 1:
            domain = values[0]
            status = "consistent"
            confidence = 0.95
        else:
            domain = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
            status = "conflicting"
            confidence = max(0.35, 0.35 + 0.30 * (counts[domain] / len(values)))
            flags.append(f"conflicting_domain_evidence:{component}")

        effective[component] = [domain]
        details[component] = {
            "domains": [domain],
            "status": status,
            "confidence": r8(confidence),
            "alternatives": dict(sorted(counts.items())),
            "observations": component_observations,
        }

    payment_confidence = min(details[branch]["confidence"] for branch in PAYMENT_BRANCHES)
    return effective, details, r8(payment_confidence), flags


def payment_relation(effective_domains: dict, domain_details: dict) -> str:
    statuses = [domain_details[branch]["status"] for branch in PAYMENT_BRANCHES]
    if any(status != "consistent" for status in statuses):
        return "unknown_or_conflicting"
    paya_domain = effective_domains["paya"][0]
    payb_domain = effective_domains["payb"][0]
    if paya_domain == payb_domain:
        return "shared_effective_domain"
    return "separate_effective_domains"


def infer_model(scenario_dir: Path, accepted_model: dict, intent_path: Path) -> tuple[dict, dict]:
    scenario_path = scenario_dir / "scenario.yaml"
    trace_path = scenario_dir / "trace-evidence.yaml"
    deployment_path = scenario_dir / "deployment-domains.yaml"

    scenario = load_yaml(scenario_path)
    trace = load_yaml(trace_path)
    deployment = load_yaml(deployment_path)
    spans = trace.get("spans") or []

    edges, edge_confidence, edge_diagnostics = infer_edges(spans)
    race, race_confidence, race_flags = infer_race(spans)
    observations = collect_domain_observations(deployment, trace, deployment_path, trace_path)
    effective_domains, domain_details, domain_confidence, domain_flags = choose_domains(observations)
    relation = payment_relation(effective_domains, domain_details)

    flags = []
    if edge_diagnostics["missingEdges"]:
        flags.append("incomplete_trace_coverage")
    flags.extend(race_flags)
    flags.extend(domain_flags)

    weighted = (0.40 * edge_confidence) + (0.25 * race_confidence) + (0.35 * domain_confidence)
    if flags:
        weighted *= 0.85
    overall_confidence = r8(min(0.99, max(0.0, weighted)))

    operator_graph = accepted_model["spec"]["operatorGraph"]
    model = {
        "apiVersion": "emac.sanity/v1",
        "kind": "DiscoveredJourneyModel",
        "metadata": {
            "name": f"checkout-{scenario['metadata']['name']}",
            "labels": {
                "emac.example": "true",
                "emac.sanity.generated": "true",
                "journey": "checkout",
                "scenario": scenario["metadata"]["name"],
            },
        },
        "spec": {
            "scenario": scenario["metadata"]["name"],
            "inputArtifacts": {
                "canonicalIntent": rel(intent_path),
                "acceptedModel": "fixtures/accepted/baseline.accepted-model.yaml",
                "scenario": rel(scenario_path),
                "traceEvidence": rel(trace_path),
                "deploymentDomainEvidence": rel(deployment_path),
            },
            "operatorGraph": operator_graph,
            "topology": {
                "nodes": accepted_model["spec"]["topology"]["nodes"],
                "edges": edges,
                "edgeDiagnostics": edge_diagnostics,
            },
            "redundancy": [race],
            "failureDomains": {
                "effective": effective_domains,
                "paymentRaceRelation": relation,
                "details": domain_details,
            },
            "confidence": {
                "overall": overall_confidence,
                "fields": {
                    "operatorEdges": edge_confidence,
                    "paymentRace": race_confidence,
                    "failureDomains": domain_confidence,
                },
                "evidenceFlags": flags,
                "basis": [
                    "operator edges use trace parent/child span linkage",
                    "payment race uses overlapping payment alternatives plus accepted intent",
                    "failure domains use deployment/provider labels and payment span attributes",
                ],
            },
            "provenance": {
                "operatorEdges": {
                    edge_key([edge["from"], edge["to"]]): edge["provenance"] for edge in edges
                },
                "failureDomains": {
                    component: details["observations"]
                    for component, details in domain_details.items()
                },
            },
        },
    }
    inputs = {
        "scenario": scenario,
        "trace": trace,
        "deployment": deployment,
        "scenarioPath": scenario_path,
        "tracePath": trace_path,
        "deploymentPath": deployment_path,
    }
    return model, inputs


def compile_availability(availability: dict, failure_domains: dict, domain_details: dict, policy: dict) -> dict:
    front = availability["front"]
    cart = availability["cart"]
    pricing = availability["pricing"]
    paya = availability["paya"]
    payb = availability["payb"]
    objective = float(policy["spec"]["objectiveAvailability"])

    parallel_all = cart * pricing
    race_plus = 1 - ((1 - paya) * (1 - payb))
    a_plus = front * parallel_all * race_plus

    relation = payment_relation(failure_domains, domain_details)
    if relation == "separate_effective_domains":
        race_minus = race_plus
        pessimistic_rule = "separateKnownDomains: use independent race formula"
        assumptions = [
            "paya and payb have distinct known effective payment domains",
            "payment race keeps independent redundancy in the pessimistic replay bound",
        ]
    elif relation == "shared_effective_domain":
        race_minus = max(paya, payb)
        pessimistic_rule = "sharedEffectiveDomain: collapse race to best single alternative"
        assumptions = [
            "paya and payb share one effective payment domain",
            "full correlation inside that domain removes race availability gain",
        ]
    else:
        race_minus = max(paya, payb)
        pessimistic_rule = "unknownOrConflictingDomain: collapse race and require REVIEW"
        assumptions = [
            "payment domain evidence is missing or conflicting",
            "automation treats payment alternatives as correlated until reconciled",
        ]

    a_minus = front * parallel_all * race_minus
    bound_width = a_plus - a_minus
    payment_loss = race_plus - race_minus

    contributors = []
    if relation != "separate_effective_domains":
        contributors.append(
            {
                "name": "payment_race_domain_correlation",
                "reason": "payment race redundancy is removed or not trusted in the pessimistic bound",
                "raceAPlus": r8(race_plus),
                "raceAMinus": r8(race_minus),
                "paymentRaceLoss": r8(payment_loss),
            }
        )
    if a_minus < objective:
        contributors.append(
            {
                "name": "objective_violation",
                "reason": "A_minus is below the policy objective",
                "objectiveAvailability": r8(objective),
                "shortfall": r8(objective - a_minus),
            }
        )

    return {
        "operatorGraph": "Series(front, Parallel(cart, pricing), Race(paya, payb))",
        "atomicAvailability": {component: r8(value) for component, value in availability.items()},
        "failureDomainAssumptions": {
            "paymentRaceRelation": relation,
            "paymentDomains": {
                "paya": failure_domains["paya"],
                "payb": failure_domains["payb"],
            },
            "pessimisticRule": pessimistic_rule,
            "assumptions": assumptions,
        },
        "results": {
            "parallelCartPricing": r8(parallel_all),
            "raceOptimistic": r8(race_plus),
            "racePessimistic": r8(race_minus),
            "A_plus": r8(a_plus),
            "A_minus": r8(a_minus),
            "bound_width": r8(bound_width),
        },
        "dominantContributors": contributors,
    }


def fake_domain_details_for_accepted(accepted_model: dict) -> dict:
    return {
        component: {
            "domains": accepted_model["spec"]["failureDomains"]["effective"][component],
            "status": "consistent",
            "confidence": 0.90,
            "observations": [],
        }
        for component in COMPONENTS
    }


def build_delta(
    scenario: str,
    accepted_model: dict,
    discovered_model: dict,
    accepted_compilation: dict,
    discovered_compilation: dict,
    policy: dict,
) -> dict:
    accepted_domains = accepted_model["spec"]["failureDomains"]["effective"]
    discovered_domains = discovered_model["spec"]["failureDomains"]["effective"]
    discovered_details = discovered_model["spec"]["failureDomains"]["details"]
    threshold = float(policy["spec"]["highImpactDeltaThreshold"])

    changes = []
    for component in PAYMENT_BRANCHES:
        before = accepted_domains[component]
        after = discovered_domains[component]
        status = discovered_details[component]["status"]
        if before != after or status != "consistent":
            changes.append(
                {
                    "type": "failure_domain_assignment",
                    "component": component,
                    "before": before,
                    "after": after,
                    "status": status,
                    "confidence": discovered_details[component]["confidence"],
                    "provenance": discovered_details[component]["observations"],
                }
            )

    relation_before = accepted_model["spec"]["failureDomains"]["paymentRaceRelation"]
    relation_after = discovered_model["spec"]["failureDomains"]["paymentRaceRelation"]
    impact_delta = (
        discovered_compilation["results"]["A_minus"] - accepted_compilation["results"]["A_minus"]
    )
    impact_classification = "high" if abs(impact_delta) >= threshold else "low"
    if relation_before != relation_after:
        changes.append(
            {
                "type": "payment_race_domain_relation",
                "group": PAYMENT_BRANCHES,
                "before": relation_before,
                "after": relation_after,
                "predictedImpactOnA_minus": {
                    "acceptedA_minus": accepted_compilation["results"]["A_minus"],
                    "discoveredA_minus": discovered_compilation["results"]["A_minus"],
                    "delta": r8(impact_delta),
                    "highImpactThreshold": r8(threshold),
                    "classification": impact_classification,
                },
            }
        )

    return {
        "apiVersion": "emac.sanity/v1",
        "kind": "ModelDelta",
        "metadata": {
            "name": f"checkout-{scenario}",
            "labels": {
                "emac.example": "true",
                "emac.sanity.generated": "true",
                "journey": "checkout",
                "scenario": scenario,
            },
        },
        "spec": {
            "scenario": scenario,
            "comparedToAcceptedModel": "fixtures/accepted/baseline.accepted-model.yaml",
            "discoveredModel": f"generated/models/{scenario}.discovered-model.yaml",
            "changes": changes,
            "summary": {
                "hasHighImpactDelta": impact_classification == "high",
                "A_minusBefore": accepted_compilation["results"]["A_minus"],
                "A_minusAfter": discovered_compilation["results"]["A_minus"],
                "A_minusDelta": r8(impact_delta),
                "governanceImplication": "requires conservative gate evaluation",
            },
        },
    }


def governance_decision(
    scenario: str,
    discovered_model: dict,
    compilation: dict,
    delta: dict | None,
    policy: dict,
) -> tuple[str, list[str]]:
    objective = float(policy["spec"]["objectiveAvailability"])
    confidence_threshold = float(policy["spec"]["autoAcceptConfidence"])
    confidence = discovered_model["spec"]["confidence"]["overall"]
    flags = discovered_model["spec"]["confidence"]["evidenceFlags"]
    a_minus = compilation["results"]["A_minus"]
    has_high_impact_delta = bool(delta and delta["spec"]["summary"]["hasHighImpactDelta"])

    reasons = []
    if confidence < confidence_threshold:
        reasons.append(
            f"overall confidence {confidence:.2f} is below auto-accept threshold {confidence_threshold:.2f}"
        )
    if flags:
        reasons.append("evidence flags are present: " + ", ".join(flags))

    if reasons:
        return "REVIEW", reasons

    if a_minus < objective:
        return "FAIL", [f"A_minus {a_minus:.8f} is below objective {objective:.8f}"]

    if has_high_impact_delta:
        return "REVIEW", ["high-impact model delta requires operator review"]

    return "PASS", ["A_minus meets objective and evidence confidence is sufficient"]


def interpretation_for(scenario: str, compilation: dict, decision: str) -> str:
    if scenario == "baseline_separate_domains":
        return (
            "Evidence supports separate effective payment domains, so the pessimistic replay "
            "keeps the payment race redundancy and the conservative gate passes."
        )
    if scenario == "payment_shared_fate_drift":
        return (
            "Local SLO inputs are unchanged, but evidence moves paya and payb into one "
            "effective payment domain. A_plus stays the same while A_minus collapses the "
            f"payment race and the conservative gate returns {decision}."
        )
    return (
        "The replay can still compile an analysis model, but missing payb traces and "
        "conflicting payment-domain metadata keep confidence below policy threshold, so "
        f"the governance decision is {decision}."
    )


def build_report(
    scenario: str,
    inputs: dict,
    discovered_model: dict,
    compilation: dict,
    decision: str,
    decision_reasons: list[str],
    delta: dict | None,
    policy: dict,
    intent_path: Path,
    atomic_path: Path,
) -> dict:
    return {
        "apiVersion": "emac.sanity/v1",
        "kind": "SanityDerivationReport",
        "metadata": {
            "name": f"checkout-{scenario}",
            "labels": {
                "emac.example": "true",
                "emac.sanity.generated": "true",
                "journey": "checkout",
                "scenario": scenario,
            },
        },
        "spec": {
            "scenario": scenario,
            "inputArtifacts": {
                "canonicalIntent": rel(intent_path),
                "acceptedModel": "fixtures/accepted/baseline.accepted-model.yaml",
                "atomicSloInput": rel(atomic_path),
                "policy": "fixtures/policy/sanity-policy.yaml",
                "scenario": rel(inputs["scenarioPath"]),
                "traceEvidence": rel(inputs["tracePath"]),
                "deploymentDomainEvidence": rel(inputs["deploymentPath"]),
            },
            "generatedArtifacts": {
                "discoveredModel": f"generated/models/{scenario}.discovered-model.yaml",
                "delta": None if delta is None else f"generated/deltas/{scenario}.delta.yaml",
            },
            "operatorGraph": compilation["operatorGraph"],
            "atomicAvailabilityInputs": compilation["atomicAvailability"],
            "localSloView": "unchanged/green",
            "policy": {
                "objectiveAvailability": r8(policy["spec"]["objectiveAvailability"]),
                "evidenceWindow": policy["spec"].get("evidenceWindow"),
                "autoAcceptConfidence": r8(policy["spec"]["autoAcceptConfidence"]),
                "highImpactDeltaThreshold": r8(policy["spec"]["highImpactDeltaThreshold"]),
            },
            "failureDomainAssumptions": compilation["failureDomainAssumptions"],
            "composition": {
                "formulas": policy["spec"]["compilation"]["formulas"],
                "optimisticEstimate": {
                    "symbol": "A_plus",
                    "value": compilation["results"]["A_plus"],
                    "paymentRaceRule": policy["spec"]["compilation"]["formulas"]["raceOptimistic"],
                },
                "pessimisticBound": {
                    "symbol": "A_minus",
                    "value": compilation["results"]["A_minus"],
                    "paymentRaceRule": compilation["failureDomainAssumptions"]["pessimisticRule"],
                },
                "boundWidth": compilation["results"]["bound_width"],
                "intermediateValues": {
                    "parallelCartPricing": compilation["results"]["parallelCartPricing"],
                    "raceOptimistic": compilation["results"]["raceOptimistic"],
                    "racePessimistic": compilation["results"]["racePessimistic"],
                },
            },
            "confidence": discovered_model["spec"]["confidence"],
            "governance": {
                "decision": decision,
                "reasons": decision_reasons,
                "passRequires": policy["spec"]["governance"]["passRequires"],
            },
            "dominantContributors": compilation["dominantContributors"],
            "interpretation": interpretation_for(scenario, compilation, decision),
        },
    }


def result_label(discovered_model: dict) -> str:
    relation = discovered_model["spec"]["failureDomains"]["paymentRaceRelation"]
    if relation == "separate_effective_domains":
        return "separate payment domains"
    if relation == "shared_effective_domain":
        return "shared payment domain delta"
    return "low-confidence model delta"


def write_summary_md(summary_path: Path, rows: list[dict]) -> None:
    lines = [
        "# Checkout Model Discovery Replay Summary",
        "",
        "| Scenario | Local SLO view | Model Discovery result | A_plus | A_minus | Confidence | Decision |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {scenario} | {localSloView} | {modelDiscoveryResult} | {A_plus:.8f} | "
            "{A_minus:.8f} | {confidence:.2f} | {decision} |".format(**row)
        )

    lines.extend(
        [
            "",
            "The replay shows that the local SLO inputs can remain unchanged while evidence-backed "
            "Model Discovery changes the accepted journey model. In the shared-fate drift scenario, "
            "the optimistic estimate does not expose the issue, but the pessimistic bound collapses "
            "the payment race into a single effective domain and changes the promotion decision. In "
            "the missing-evidence scenario, EmaC remains usable for analysis but refuses automatic "
            "acceptance because confidence is below policy threshold.",
            "",
            "These files are generated by `python examples/checkout/sanity/tool/replay.py`.",
        ]
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_summary_csv(summary_path: Path, rows: list[dict]) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "scenario",
        "localSloView",
        "modelDiscoveryResult",
        "A_plus",
        "A_minus",
        "bound_width",
        "confidence",
        "decision",
        "paymentRaceRelation",
        "evidenceFlags",
    ]
    with summary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["evidenceFlags"] = ";".join(row["evidenceFlags"])
            writer.writerow(csv_row)


def write_provenance_md(path: Path, rows: list[dict], deltas: dict[str, dict]) -> None:
    lines = [
        "# Checkout Replay Provenance",
        "",
        "This file answers: which evidence caused the decision to change?",
        "",
    ]

    baseline_decision = rows[0]["decision"]
    lines.extend(
        [
            f"Baseline decision: `{baseline_decision}` for `baseline_separate_domains`.",
            "",
            "## payment_shared_fate_drift",
            "",
            "Decision change: `PASS` baseline -> `FAIL` drift.",
            "",
            "Evidence signals:",
            "- `fixtures/evidence/payment_shared_fate_drift/deployment-domains.yaml`: "
            "`services.paya.labels.emac.dev/effective-payment-domain` and "
            "`services.payb.labels.emac.dev/effective-payment-domain` both report "
            "`payment-network-shared`.",
            "- `fixtures/evidence/payment_shared_fate_drift/trace-evidence.yaml`: payment spans for "
            "`paya` and `payb` both carry `emac.provider_domain: payment-network-shared` while "
            "also sharing `emac.redundancy_group: payment_race`.",
            "- `generated/deltas/payment_shared_fate_drift.delta.yaml`: "
            "`spec.changes` records `failure_domain_assignment` changes for both payment branches "
            "and a `payment_race_domain_relation` change from `separate_effective_domains` to "
            "`shared_effective_domain`.",
            "",
            "Compiler effect: `A_plus` remains unchanged, while `A_minus` drops by "
            f"{deltas['payment_shared_fate_drift']['spec']['summary']['A_minusDelta']:.8f}.",
            "",
            "## missing_or_conflicting_evidence",
            "",
            "Decision change: `PASS` baseline -> `REVIEW` weak evidence.",
            "",
            "Evidence signals:",
            "- `fixtures/evidence/missing_or_conflicting_evidence/trace-evidence.yaml`: traces include "
            "`front -> paya` but do not include `front -> payb`, so the payment race is not fully "
            "covered by trace evidence.",
            "- `fixtures/evidence/missing_or_conflicting_evidence/deployment-domains.yaml`: "
            "`services.paya.labels.emac.dev/effective-payment-domain` reports "
            "`payment-network-a`, while `services.paya.provider.network` reports "
            "`payment-network-shared`; `services.payb.domain_observations` is empty.",
            "- `generated/deltas/missing_or_conflicting_evidence.delta.yaml`: `spec.changes` records "
            "an untrusted payment-domain assignment and an `unknown_or_conflicting` payment race "
            "relation.",
            "",
            "Governance effect: the compiler emits an analysis bound, but policy refuses automatic "
            "acceptance because confidence is below the auto-accept threshold and evidence flags are "
            "present.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    intent_path = CHECKOUT_ROOT / "spec" / "emac.checkout.yaml"
    accepted_path = FIXTURES / "accepted" / "baseline.accepted-model.yaml"
    atomic_path = FIXTURES / "atomic" / "checkout-atomic-availability.yaml"
    policy_path = FIXTURES / "policy" / "sanity-policy.yaml"

    canonical_intent = load_yaml(intent_path)
    validate_canonical_intent(canonical_intent)
    accepted_model = load_yaml(accepted_path)
    atomic = load_yaml(atomic_path)
    policy = load_yaml(policy_path)
    availability = extract_atomic_availability(atomic)

    accepted_compilation = compile_availability(
        availability,
        accepted_model["spec"]["failureDomains"]["effective"],
        fake_domain_details_for_accepted(accepted_model),
        policy,
    )

    rows = []
    deltas = {}

    for scenario in SCENARIO_ORDER:
        scenario_dir = FIXTURES / "evidence" / scenario
        discovered_model, inputs = infer_model(scenario_dir, accepted_model, intent_path)
        discovered_path = GENERATED / "models" / f"{scenario}.discovered-model.yaml"

        compilation = compile_availability(
            availability,
            discovered_model["spec"]["failureDomains"]["effective"],
            discovered_model["spec"]["failureDomains"]["details"],
            policy,
        )

        delta = None
        if scenario != "baseline_separate_domains":
            delta = build_delta(
                scenario,
                accepted_model,
                discovered_model,
                accepted_compilation,
                compilation,
                policy,
            )
            deltas[scenario] = delta

        decision, decision_reasons = governance_decision(
            scenario, discovered_model, compilation, delta, policy
        )
        report = build_report(
            scenario,
            inputs,
            discovered_model,
            compilation,
            decision,
            decision_reasons,
            delta,
            policy,
            intent_path,
            atomic_path,
        )

        write_yaml(discovered_path, discovered_model)
        if delta is not None:
            write_yaml(GENERATED / "deltas" / f"{scenario}.delta.yaml", delta)
        write_yaml(GENERATED / "reports" / f"{scenario}.derivation.yaml", report)

        rows.append(
            {
                "scenario": scenario,
                "localSloView": "unchanged/green",
                "modelDiscoveryResult": result_label(discovered_model),
                "A_plus": compilation["results"]["A_plus"],
                "A_minus": compilation["results"]["A_minus"],
                "bound_width": compilation["results"]["bound_width"],
                "confidence": discovered_model["spec"]["confidence"]["overall"],
                "decision": decision,
                "paymentRaceRelation": discovered_model["spec"]["failureDomains"][
                    "paymentRaceRelation"
                ],
                "evidenceFlags": discovered_model["spec"]["confidence"]["evidenceFlags"],
            }
        )

    summary = {
        "apiVersion": "emac.sanity/v1",
        "kind": "SanityReplaySummary",
        "metadata": {
            "name": "checkout-model-discovery-replay",
            "labels": {
                "emac.example": "true",
                "emac.sanity.generated": "true",
                "journey": "checkout",
            },
        },
        "spec": {
            "canonicalIntent": rel(intent_path),
            "acceptedModel": "fixtures/accepted/baseline.accepted-model.yaml",
            "policy": {
                "objectiveAvailability": r8(policy["spec"]["objectiveAvailability"]),
                "evidenceWindow": policy["spec"].get("evidenceWindow"),
                "autoAcceptConfidence": r8(policy["spec"]["autoAcceptConfidence"]),
                "highImpactDeltaThreshold": r8(policy["spec"]["highImpactDeltaThreshold"]),
            },
            "formulaNotes": policy["spec"]["compilation"],
            "scenarios": rows,
        },
    }
    write_json(GENERATED / "summaries" / "summary.json", summary)
    write_summary_md(GENERATED / "summaries" / "summary.md", rows)
    write_summary_csv(GENERATED / "summaries" / "summary.csv", rows)
    write_provenance_md(GENERATED / "summaries" / "provenance.md", rows, deltas)

    print("Wrote checkout sanity replay outputs:")
    for row in rows:
        print(
            "  {scenario}: A_plus={A_plus:.8f} A_minus={A_minus:.8f} "
            "confidence={confidence:.2f} decision={decision}".format(**row)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
