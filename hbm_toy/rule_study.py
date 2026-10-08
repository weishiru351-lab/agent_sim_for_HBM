"""Controlled rule-discovery study; synthetic traces and the unchanged toy backend.

Run discovery first, then validation with the same source. This is a deterministic
experiment designed by the current conversation, not an autonomous Agent runner.
The exact oracle covers bit permutations only, not the full XOR DSL.
"""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from itertools import permutations
from pathlib import Path
import random
import time

from .experiment import activity_policy, read, save
from .model import Config, Policy, Request, digest, features, simulate, verify, workload


SMALL_CONFIG = Config(channel_bits=2, bank_bits=1, column_bits=1, row_bits=2)
DISCOVERY_CASES = [
    {"name": "duplicate_ordered", "family": "duplicate", "order": "ordered", "seed": 0},
    {"name": "parity_ordered", "family": "parity", "order": "ordered", "seed": 0},
]
# Defined before evaluating any of these cases. Seeds change both bit placement
# and, for shuffled cases, request order. They are NOT independent real workloads.
VALIDATION_CASES = [
    {"name": f"{family}_{order}_{seed}", "family": family, "order": order, "seed": seed}
    for family in ("duplicate", "parity")
    for order in ("ordered", "shuffled")
    for seed in (101, 303)
] + [
    {"name": "full_ordered", "family": "full", "order": "ordered", "seed": 0},
    {"name": "full_shuffled", "family": "full", "order": "shuffled", "seed": 101},
    {"name": "phase_mix", "family": "phase_mix", "order": "ordered", "seed": 101},
    {"name": "repeated_bursts", "family": "full", "order": "bursts", "seed": 303},
]
METHODS = ("identity", "activity_packed", "joint_only", "joint_conditional")
SEARCH_SEEDS = (0, 1, 2)
SEARCH_BUDGET = 20


def permutation_policy(order, cfg):
    """Compile output-bit -> input-bit order into at most width-1 swaps."""
    if sorted(order) != list(range(cfg.block_bits)):
        raise ValueError("order must be a permutation of all block bits")
    labels, ops = list(range(cfg.block_bits)), []
    for dst, bit in enumerate(order):
        src = labels.index(bit)
        if src != dst:
            ops.append(("swap_bits", dst, src))
            labels[dst], labels[src] = labels[src], labels[dst]
    return Policy.parse(Policy(tuple(ops)).to_dict(), cfg)


def selector(block, bits):
    return sum(((block >> bit) & 1) << dst for dst, bit in enumerate(bits))


def joint_policy(requests, cfg, conditional_columns):
    """Greedy joint occupancy, then global or within-bank column activity.

    No backend evaluations: minimize the sum of squared joint bucket counts for
    each added channel/bank bit. Break ties by global transition count, then bit
    index. Column bits absorb frequent changes; the conditional version counts
    changes between consecutive accesses to the SAME selected channel and bank.
    This is a hand-written rule hypothesis, not a learned or proven-optimal rule.
    """
    if not requests:
        raise ValueError("requests must not be empty")
    ordered = sorted(requests, key=lambda r: (r.arrival, r.request_id))
    blocks = [r.address // cfg.granule_bytes for r in ordered]
    transitions = [sum(((a ^ b) >> bit) & 1 for a, b in zip(blocks, blocks[1:]))
                   for bit in range(cfg.block_bits)]
    remaining, selected = set(range(cfg.block_bits)), []
    for _ in range(cfg.channel_bits + cfg.bank_bits):
        def score(bit):
            counts = Counter(selector(block, selected + [bit]) for block in blocks)
            return sum(n * n for n in counts.values()), -transitions[bit], bit
        bit = min(remaining, key=score)
        selected.append(bit)
        remaining.remove(bit)
    column_changes = transitions
    if conditional_columns:
        previous, differences = {}, []
        for block in blocks:
            key = selector(block, selected)
            if key in previous:
                differences.append(block ^ previous[key])
            previous[key] = block
        column_changes = [sum((delta >> bit) & 1 for delta in differences)
                          for bit in range(cfg.block_bits)]
    columns = sorted(remaining, key=lambda b: (-column_changes[b], b))[:cfg.column_bits]
    remaining.difference_update(columns)
    # Leave the remaining row bits in their resulting order: sorting them would
    # waste DSL operations without changing row equality in this backend.
    order = list(range(cfg.block_bits))
    for dst, bit in enumerate(selected + columns):
        src = order.index(bit)
        order[dst], order[src] = order[src], order[dst]
    return permutation_policy(order, cfg)


def make_trace(spec, count=256):
    """Six-bit stress cases; duplicate/parity bits model correlated address fields.

    These constructions deliberately expose a known heuristic blind spot. They
    are not measured KV traces and do not establish prevalence on real hardware.
    """
    if count < 1:
        raise ValueError("count must be positive")
    family, order, seed = spec["family"], spec["order"], spec["seed"]
    rng = random.Random(seed)
    placement = list(range(6))
    if seed:
        rng.shuffle(placement)
    blocks = []
    for i in range(count):
        x = (i // 4 if order == "bursts" else i) % (64 if family == "full" else 32)
        if family == "full":
            block = x
        elif family in {"duplicate", "phase_mix"}:
            block = (x & 1) | ((x & 1) << 1) | ((x >> 1) << 2)
        elif family == "parity":
            block = (x & 1) | (((x ^ (x >> 1)) & 1) << 1) | ((x >> 1) << 2)
        else:
            raise ValueError("unknown family")
        # Phase mix uses the same address relation in two different bit layouts.
        active_placement = list(range(6)) if family == "phase_mix" and i < count // 2 else placement
        blocks.append(selector(block, active_placement))
    if order == "shuffled":
        rng.shuffle(blocks)
    elif order not in {"ordered", "bursts"}:
        raise ValueError("unknown order")
    return [Request(i, block * SMALL_CONFIG.granule_bytes, i // 8) for i, block in enumerate(blocks)]


def evaluate_rule(name, requests, cfg):
    start = time.perf_counter()
    policy = (Policy() if name == "identity" else
              activity_policy(requests, cfg, pack_columns=True) if name == "activity_packed" else
              joint_policy(requests, cfg, conditional_columns=name == "joint_conditional"))
    generation_seconds = time.perf_counter() - start
    check = verify(policy, cfg)
    if check["status"] != "pass":
        raise ValueError("mapping verification failed")
    start = time.perf_counter()
    metrics = simulate(requests, policy, cfg)
    return {"dsl": policy.to_dict(), "metrics": metrics, "verification": check,
            "generation_seconds": generation_seconds, "backend_seconds": time.perf_counter() - start,
            "selection_backend_calls": 0, "measurement_backend_calls": 1}


def exact_permutations(requests, cfg):
    if cfg.block_bits > 6:
        raise ValueError("exact study is capped at six bits")
    start = time.perf_counter()
    cycles, best_policy, best_cycles = [], None, float("inf")
    for order in permutations(range(cfg.block_bits)):
        policy = permutation_policy(order, cfg)
        value = simulate(requests, policy, cfg)["completion_cycles"]
        cycles.append(value)
        if value < best_cycles:
            best_cycles, best_policy = value, policy
    return {"space": "all bit permutations, lexicographic output-to-input order; XOR excluded",
            "backend_calls": len(cycles), "wall_seconds": time.perf_counter() - start,
            "best_cycles": best_cycles, "best_dsl": best_policy.to_dict(),
            "minimizer_count": cycles.count(best_cycles), "cycles_in_permutation_order": cycles}


def permutation_search(requests, cfg, method, seed, budget=SEARCH_BUDGET):
    """Same permutation-only space as the oracle; 20 unique evaluated mappings.

    Both methods start with identity and the frozen packed heuristic. Local
    search uses swaps plus a random restart every seventh proposal. Full
    candidates and costs are independent of the exact oracle's results.
    """
    rng = random.Random(seed)
    history, seen, best_order, best_cycles = [], set(), None, float("inf")
    packed = activity_policy(requests, cfg, pack_columns=True)
    packed_order = [mask.bit_length() - 1 for mask in packed.masks(cfg)]
    start = time.perf_counter()
    for attempt in range(budget * 100):
        if len(history) >= budget:
            break
        if attempt < 2:
            order = list(range(cfg.block_bits)) if attempt == 0 else packed_order[:]
        elif method == "random" or attempt % 7 == 0:
            order = rng.sample(range(cfg.block_bits), cfg.block_bits)
        else:
            order = best_order[:]
            a, b = rng.sample(range(cfg.block_bits), 2)
            order[a], order[b] = order[b], order[a]
        key = tuple(order)
        if key in seen:
            continue
        seen.add(key)
        value = simulate(requests, permutation_policy(order, cfg), cfg)["completion_cycles"]
        if value < best_cycles:
            best_order, best_cycles = order[:], value
        history.append({"order": order, "cycles": value, "best_cycles": best_cycles})
    return {"method": method, "seed": seed, "budget": budget, "backend_calls": len(history),
            "best_cycles": best_cycles, "best_order": best_order, "history": history,
            "wall_seconds": time.perf_counter() - start}


def run_case(spec):
    requests = make_trace(spec)
    methods = {name: evaluate_rule(name, requests, SMALL_CONFIG) for name in METHODS}
    # The oracle is a diagnostic upper-quality reference, never input to a rule.
    searches = [permutation_search(requests, SMALL_CONFIG, method, seed)
                for method in ("random", "local") for seed in SEARCH_SEEDS]
    exact = exact_permutations(requests, SMALL_CONFIG)
    result = {"spec": spec, "trace_id": digest([asdict(r) for r in requests]),
              "features": features(requests, SMALL_CONFIG), "methods": methods,
              "searches": searches, "exact": exact}
    print(spec["name"], {name: item["metrics"]["completion_cycles"] for name, item in methods.items()},
          "exact", exact["best_cycles"], flush=True)
    return result


def source_hashes():
    root = Path(__file__).resolve().parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("model.py", "experiment.py", "rule_study.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("discovery", "validation"))
    parser.add_argument("--output", type=Path, required=True, help="study folder; discovery creates it")
    args = parser.parse_args()
    folder = args.output
    hashes = source_hashes()
    if args.phase == "discovery":
        folder.mkdir(parents=True, exist_ok=False)
        protocol = {"version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                    "backend": "toy-fcfs-v1", "config": SMALL_CONFIG.to_dict(), "count": 256,
                    "discovery_cases": DISCOVERY_CASES, "validation_cases": VALIDATION_CASES,
                    "source_hashes": hashes, "frozen_baseline": "activity_policy(pack_columns=True), unchanged",
                    "proposed_rule": "joint_conditional", "ablation": "joint_only",
                    "search_budget": SEARCH_BUDGET, "search_seeds": SEARCH_SEEDS,
                    "hypotheses_before_evaluation": [
                        "High marginal bit activity can hide redundant channel/bank selectors.",
                        "Greedy joint occupancy can avoid redundant selector bits.",
                        "Column activity should be measured within each selected channel/bank, not globally."],
                    "limits": ["synthetic stress cases designed with the heuristic in view",
                               "validation generators known to the rule designer; outputs not yet evaluated",
                               "exact only over permutations, not the XOR DSL",
                               "rule recomputed from each validation trace; no validation backend feedback used",
                               "not a blind Agent-vs-human or Agent-vs-search efficiency experiment",
                               "current conversation proposed and implemented hypotheses; no autonomous Agent runtime",
                               "LLM thinking time, tokens, and human design costs not measured"]}
        save(folder / "protocol.json", protocol)
        save(folder / "discovery.json", {"source_hashes": hashes, "cases": [run_case(s) for s in DISCOVERY_CASES]})
    else:
        protocol = read(folder / "protocol.json")
        discovery = read(folder / "discovery.json")
        if protocol["source_hashes"] != hashes or discovery["source_hashes"] != hashes:
            raise ValueError("sources changed after discovery; create a new study instead of reusing the validation split")
        if (folder / "validation.json").exists():
            raise ValueError("validation already exists; use a new study folder for a rerun")
        cases = [run_case(s) for s in protocol["validation_cases"]]
        # Existing 16-bit workloads check applicability outside the reduced space;
        # no exhaustive reference or tuned post-validation fallback is applied.
        original = []
        cfg = Config()
        for name in ("sequential", "stride", "kv_interleaved", "random"):
            for holdout in (False, True):
                requests = workload(name, cfg, 768, 0, holdout=holdout)
                methods = {method: evaluate_rule(method, requests, cfg) for method in METHODS}
                original.append({"workload": name, "holdout": holdout,
                                 "trace_id": digest([asdict(r) for r in requests]), "methods": methods})
                print("original", name, holdout,
                      {m: v["metrics"]["completion_cycles"] for m, v in methods.items()}, flush=True)
        save(folder / "validation.json", {"source_hashes": hashes, "cases": cases,
                                         "original_config": cfg.to_dict(), "original_count": 768,
                                         "original_workloads": original})


if __name__ == "__main__":
    main()
