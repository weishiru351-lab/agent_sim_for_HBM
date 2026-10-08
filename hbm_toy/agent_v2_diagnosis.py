"""Post-search diagnosis of the archived T04 failure; never a new Agent score.

The original simulator and search protocol remain unchanged. Event summaries
retain queue/row state across the workload boundary; isolated phase replays
are explicitly counted as additional, cold-start diagnostic executions.
"""

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
from itertools import combinations, permutations
from pathlib import Path

from .agent_v2 import check_frozen
from .experiment import read, save
from .model import Config, Policy, simulate
from .rule_study import permutation_policy
from .trace_bridge import load_trace


def event_summary(events, cfg):
    """Describe a request cohort in its original continuous execution.

    Drain timestamps and mean latencies of cohorts are NOT additive phase costs.
    Queue waits partition latency together with issue-to-finish service time.
    """
    n = len(events)
    if not n:
        raise ValueError("Cannot summarize an empty cohort")
    states = Counter(e["row_state"] for e in events)
    channel_counts = [0] * (1 << cfg.channel_bits)
    bank_counts = [[0] * (1 << cfg.bank_bits) for _ in channel_counts]
    for e in events:
        channel_counts[e["channel"]] += 1
        bank_counts[e["channel"]][e["bank"]] += 1
    return {
        "requests": n,
        "first_arrival": min(e["arrival"] for e in events),
        "last_arrival": max(e["arrival"] for e in events),
        "last_finish": max(e["finish"] for e in events),
        "avg_latency": sum(e["finish"] - e["arrival"] for e in events) / n,
        "avg_backpressure_wait": sum(e["admitted"] - e["arrival"] for e in events) / n,
        "avg_controller_wait": sum(e["issue"] - e["admitted"] for e in events) / n,
        "avg_issue_to_finish": sum(e["finish"] - e["issue"] for e in events) / n,
        "row_hit_rate": states["row_hits"] / n,
        "row_conflict_rate": states["row_conflicts"] / n,
        "channel_requests": channel_counts,
        "bank_requests": bank_counts,
    }


def phase_feedback(requests, events, cfg, split_at):
    """Split by input request cohort, not by completion time or reset state."""
    if type(split_at) is not int or not 0 < split_at < len(requests):
        raise ValueError("split_at must lie inside the ordered request trace")
    ordered = sorted(requests, key=lambda r: (r.arrival, r.request_id))
    ids = {r.request_id for r in ordered}
    event_ids = [e["request_id"] for e in events]
    if len(ids) != len(ordered) or len(event_ids) != len(set(event_ids)) or set(event_ids) != ids:
        raise ValueError("Events must cover each request exactly once")
    first_ids = {r.request_id for r in ordered[:split_at]}
    boundary = ordered[split_at].arrival
    first = [e for e in events if e["request_id"] in first_ids]
    second = [e for e in events if e["request_id"] not in first_ids]
    # Strictly before boundary: same-cycle arrivals are not yet counted here.
    outstanding = sum(e["arrival"] < boundary and e["finish"] > boundary for e in first)
    return {
        "split_at_request_index": split_at,
        "second_cohort_first_arrival": boundary,
        "first_cohort_outstanding_at_boundary": outstanding,
        "first_cohort": event_summary(first, cfg),
        "second_cohort": event_summary(second, cfg),
        "semantics": "Cohorts of one continuous run; drain timestamps are not additive phase times. "
                     "No reset at the boundary. Outstanding counts arrivals strictly before boundary.",
    }


def order_of(entry, cfg):
    return tuple(mask.bit_length() - 1 for mask in Policy.parse(entry["dsl"], cfg).masks(cfg))


def neighbors(order):
    for a, b in combinations(range(len(order)), 2):
        changed = list(order)
        changed[a], changed[b] = changed[b], changed[a]
        yield (a, b), tuple(changed)


def diagnose(archive, output):
    archive, output = Path(archive), Path(output)
    protocol = check_frozen(archive)
    comparison_path = archive / "comparison.json"
    comparison_sha = hashlib.sha256(comparison_path.read_bytes()).hexdigest()
    data = read(comparison_path)
    if data["protocol"] != protocol:
        raise ValueError("Archived protocol mismatch")
    case = data["cases"]["T04_phases"]
    cfg = Config(**protocol["config"])
    requests = load_trace(archive / "traces" / "T04_phases.json", cfg)
    # This is the known development example's generator boundary, not a detector.
    split_at = 96
    oracle = dict(zip(permutations(range(cfg.block_bits)), case["exact"]["cycles_in_permutation_order"]))
    if len(oracle) != 720 or min(oracle.values()) != case["exact"]["best_cycles"]:
        raise ValueError("Unexpected archived permutation oracle")
    output.mkdir(parents=True, exist_ok=False)
    measurements = {}
    aliases = {}

    def measure(label, order, expected_metrics=None):
        key = "".join(map(str, order))
        aliases[label] = key
        if key not in measurements:
            policy = permutation_policy(order, cfg)
            result = simulate(requests, policy, cfg, include_events=True)
            events = result.pop("events")
            if result["completion_cycles"] != oracle[tuple(order)]:
                raise ValueError("Replay differs from original oracle")
            measurements[key] = {"order": order, "dsl": policy.to_dict(), "metrics": result,
                                 "phase_feedback": phase_feedback(requests, events, cfg, split_at)}
        if expected_metrics is not None and measurements[key]["metrics"] != expected_metrics:
            raise ValueError("Replay differs from archived candidate metrics")
        return key

    history = []
    for e in case["agent"]["history"]:
        order = order_of(e, cfg)
        call = e["backend_calls"]
        key = measure(f"agent_call_{call}", order, e["metrics"])
        n = [{"swap_positions": swap, "order": o, "cycles": oracle[o]} for swap, o in neighbors(order)]
        history.append({"call": call, "measurement": key, "hypothesis": e["hypothesis"],
                        "expected": e["expected"], "cycles": e["metrics"]["completion_cycles"],
                        "neighbors_from_existing_oracle": n,
                        "strictly_improving_neighbor_count": sum(x["cycles"] < oracle[order] for x in n)})
    anchors = {"agent_best": case["agent"]["best"], "one_shot_best": case["one_shot"]["best"]}
    for method, label in [("heuristics", "heuristic_best"), ("local", "local_seed0_best")]:
        run = next(r for r in case["baselines"] if r["method"] == method and r["seed"] == 0)
        anchors[label] = run["best"]
    for label, entry in anchors.items():
        measure(label, order_of(entry, cfg), entry["metrics"])

    # Matched 2x2 intervention: bank stays bit 3, vary channel pair and column.
    factorial = []
    for channels in [(4, 5), (2, 4)]:
        for column in [1, 0]:
            order = (*channels, 3, column, *sorted(set(range(6)) - set(channels) - {3, column}))
            label = f"factorial_channels_{channels[0]}{channels[1]}_column_{column}"
            factorial.append({"channels": channels, "bank": 3, "column": column,
                              "measurement": measure(label, order)})

    # Explicit counterfactuals selected AFTER seeing all original results.
    for label, order in {
        "missed_from_call6": (5, 4, 2, 0, 1, 3),
        "missed_from_call10": (5, 2, 4, 0, 1, 3),
        "escape_intermediate": (4, 5, 2, 1, 0, 3),
        "escape_target": (4, 5, 2, 0, 1, 3),
    }.items():
        measure(label, order)

    cold = {}
    cold_calls = 0
    for label in anchors:
        policy = Policy.parse(measurements[aliases[label]]["dsl"], cfg)
        parts = []
        for part in (requests[:split_at], requests[split_at:]):
            origin = min(r.arrival for r in part)
            part = [replace(r, arrival=r.arrival - origin) for r in part]
            parts.append(simulate(part, policy, cfg))
            cold_calls += 1
        cold[label] = {"first_cohort_only": parts[0], "second_cohort_only": parts[1],
                       "note": "Cold-start isolated executions, not additive decomposition of the full score."}
    result = {
        "purpose": "Post-search development diagnosis, not an independent Agent experiment",
        "source_comparison_sha256": comparison_sha,
        "source_trace_id": protocol["trace_ids"]["T04_phases"],
        "diagnostic_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config": cfg.to_dict(), "aliases": aliases, "measurements": measurements,
        "agent_history_audit": history, "factorial": factorial, "cold_start_controls": cold,
        "accounting": {"full_trace_diagnostic_executions": len(measurements),
                       "cold_start_diagnostic_executions": cold_calls,
                       "total_diagnostic_backend_calls": len(measurements) + cold_calls,
                       "additional_oracle_executions": 0, "new_agent_search_executions": 0},
        "limits": ["Already-seen synthetic toy task; all counterfactuals selected after seeing results",
                   "Phase boundary supplied from known generator, not inferred automatically",
                   "No evidence yet that richer feedback improves future Agent decisions",
                   "Original 20-call search is unchanged; diagnosis calls are separate",
                   "Neighborhood costs reuse original post-search oracle, not free search-time feedback"],
    }
    check_frozen(archive)
    if hashlib.sha256(comparison_path.read_bytes()).hexdigest() != comparison_sha:
        raise ValueError("Original archive changed")
    save(output / "diagnosis.json", result)
    return result["accounting"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("results/agent_v2_pilot"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(diagnose(args.archive, args.output))


if __name__ == "__main__":
    main()
