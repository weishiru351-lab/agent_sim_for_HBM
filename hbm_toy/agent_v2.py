"""Small fixed-trace Agent comparison. This harness never generates LLM decisions.

prepare -> write/freeze one-shot proposals -> start -> external Agent steps ->
close all Agent sessions -> compare (baselines, no-feedback replay, then oracle).
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import random
import tempfile

from .experiment import activity_policy, read, save
from .model import Config, Request, digest
from .rule_study import exact_permutations, joint_policy, permutation_policy
from .trace_bridge import finish, init, load_trace, statistics, submit


TASKS = ("T00_sequential", "T01_regular", "T02_correlated", "T03_shuffled", "T04_phases")
CFG = Config(channel_bits=2, bank_bits=1, column_bits=1, row_bits=2)
BUDGET = 20


def hashes():
    folder = Path(__file__).parent
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in
            ("model.py", "experiment.py", "rule_study.py", "trace_bridge.py", "agent_v2.py")}


def make_tasks():
    """New synthetic constructions. Designer knows generators; not blind tasks."""
    def place(x, order):
        return sum(((x >> b) & 1) << i for i, b in enumerate(order))
    sequential = [i % 64 for i in range(192)]
    regular = [place((i * 5) % 64, [4, 1, 5, 0, 3, 2]) for i in range(192)]
    correlated = []
    for i in range(192):
        x = (i * 3 + i // 16) % 16
        block = x | ((((x >> 0) ^ (x >> 2)) & 1) << 4) | ((((x >> 1) ^ (x >> 3)) & 1) << 5)
        correlated.append(place(block, [3, 5, 0, 4, 1, 2]))
    shuffled = correlated[:]
    random.Random(20261008).shuffle(shuffled)
    phases = [place((i // 2) % 32, [2, 5, 0, 4, 1, 3]) for i in range(96)]
    phases += [place((i * 3) % 64, [5, 2, 4, 0, 3, 1]) for i in range(96)]
    return {name: [Request(i, x * 64, i // 8, "write" if i % 16 == 15 else "read")
                   for i, x in enumerate(blocks)] for name, blocks in
            zip(TASKS, (sequential, regular, correlated, shuffled, phases))}


def prepare(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    tasks = make_tasks()
    for name, requests in tasks.items():
        save(folder / "traces" / (name + ".json"),
             {"version": 1, "provenance": "synthetic Agent V2 pilot; not measured HBM/KV traffic",
              "requests": [asdict(r) for r in requests]})
        save(folder / "features" / (name + ".json"), statistics(requests, CFG))
    protocol = {
        "version": 2, "created_utc": datetime.now(timezone.utc).isoformat(), "backend": "toy-fcfs-v1",
        "config": CFG.to_dict(), "space": "swap_bits only; all six-bit permutations (720)",
        "budget": BUDGET, "shared_start": ["identity", "reverse_bits"], "source_hashes": hashes(),
        "trace_ids": {name: digest([asdict(r) for r in requests]) for name, requests in tasks.items()},
        "baseline_seeds": [0, 1, 2], "agent_runs_per_task": 1,
        "methods": ["frozen heuristics", "random", "joint-seeded local", "model one-shot", "feedback Agent"],
        "prompt": "Read fixed trace/statistics. Minimize completion_cycles using up to 20 backend calls "
                  "including identity and reverse. Submit legal swap_bits DSL with a brief testable hypothesis "
                  "and prediction; use feedback to decide the next proposal. No trace/config changes, "
                  "no oracle or competitor results during search. One-shot: freeze 18 candidates before "
                  "any task performance is revealed; no subsequent revision.",
        "one_shot_frozen": None,
        "information": "All methods have the same raw trace, config and statistics. Adaptive methods receive "
                       "their own metrics. One-shot intentionally receives no performance feedback. "
                       "Heuristic functions are the same available tools; backend calls are counted per run.",
        "cost_boundary": "Every unique actual backend execution counts, including failures. Cache hits and "
                         "invalid proposals do not; proposal cap=200. Oracle calls are separate, after search. "
                         "Feature extraction/verification/file I/O/model thinking are not backend time.",
        "limits": ["Agent both designed and ran tasks: not blind or preregistered external evaluation",
                   "one conversation run per task; same model also generated one-shot batch",
                   "one-shot commitment precedes all performance; prior task design/old experiments known",
                   "six-bit synthetic toy; no real HBM or LLM serving performance claim",
                   "no full XOR-space optimum; no held-out generalization assessment",
                   "model token count, runtime identifier, fees and isolated inference time unavailable"]}
    save(folder / "protocol.json", protocol)
    return {"prepared_tasks": list(tasks), "performance_evaluations": 0}


def check_frozen(folder):
    protocol = read(folder / "protocol.json")
    if protocol["source_hashes"] != hashes():
        raise ValueError("Frozen source changed; use a new study folder")
    cfg = Config(**protocol["config"])
    for name, trace_id in protocol["trace_ids"].items():
        requests = load_trace(folder / "traces" / (name + ".json"), cfg)
        if digest([asdict(r) for r in requests]) != trace_id:
            raise ValueError("Frozen task trace changed")
    if protocol["one_shot_frozen"]:
        for name, sha in protocol["one_shot_frozen"].items():
            if hashlib.sha256((folder / "one_shot" / (name + ".json")).read_bytes()).hexdigest() != sha:
                raise ValueError("One-shot candidates changed after commitment")
    return protocol


def start(folder):
    folder = Path(folder)
    protocol = check_frozen(folder)
    if protocol["one_shot_frozen"] or (folder / "agent").exists():
        raise ValueError("Study already started")
    frozen = {}
    for name in TASKS:
        path = folder / "one_shot" / (name + ".json")
        proposals = read(path)
        if not isinstance(proposals, list) or len(proposals) != 18:
            raise ValueError("Commit exactly 18 one-shot proposals per task before starting")
        from .model import Policy
        from .trace_bridge import validate_space
        for proposal in proposals:
            for field in ("hypothesis", "expected"):
                if not isinstance(proposal.get(field), str) or not proposal[field].strip():
                    raise ValueError("One-shot proposal needs rationale and prediction")
            validate_space(Policy.parse(proposal["dsl"], CFG), CFG, "permutation")
        frozen[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    protocol["one_shot_frozen"] = frozen
    protocol["search_started_utc"] = datetime.now(timezone.utc).isoformat()
    save(folder / "protocol.json", protocol)
    for name in TASKS:
        init(folder / "agent" / name, CFG, folder / "traces" / (name + ".json"), BUDGET, "permutation")
    return {"started_tasks": list(TASKS), "one_shot_committed_before_any_performance": True}


def baseline(trace, method, seed):
    requests = load_trace(trace, CFG)
    with tempfile.TemporaryDirectory(prefix="hbm_v2_baseline_") as tmp:
        folder = Path(tmp) / "session"
        state = init(folder, CFG, trace, BUDGET, "permutation", actor=method)
        rules = [activity_policy(requests, CFG, True), joint_policy(requests, CFG, True)]
        if method in {"heuristics", "local"}:
            for rule in rules:
                submit(folder, {"hypothesis": "frozen deterministic heuristic", "expected": "improve occupancy/locality",
                                "dsl": rule.to_dict()})
        rng = random.Random(seed)
        for attempt in range(BUDGET * 10 - 4):
            state = read(folder / "state.json")
            if method == "heuristics" or state["backend_calls"] >= BUDGET:
                break
            valid = [e for e in state["history"] if "metrics" in e]
            best = min(valid, key=lambda e: e["metrics"]["completion_cycles"])
            from .model import Policy
            order = [mask.bit_length() - 1 for mask in Policy.parse(best["dsl"], CFG).masks(CFG)]
            if method == "random" or attempt % 7 == 0:
                rng.shuffle(order)
            else:
                a, b = rng.sample(range(6), 2)
                order[a], order[b] = order[b], order[a]
            submit(folder, {"hypothesis": "fixed search schedule", "expected": "test permutation neighbor or restart",
                            "dsl": permutation_policy(order, CFG).to_dict()})
        result = finish(folder)
        result.update(method=method, seed=seed, is_llm_agent=False)
        return result


def compare(folder):
    folder = Path(folder)
    protocol = check_frozen(folder)
    if not protocol["one_shot_frozen"]:
        raise ValueError("Start and finish Agent searches before comparisons")
    if (folder / "comparison.json").exists():
        raise ValueError("Comparison exists; do not overwrite recorded results")
    for name in TASKS:
        if not read(folder / "agent" / name / "state.json")["closed"]:
            raise ValueError("Close ALL Agent sessions before revealing any comparison/oracle")
    cases = {}
    for name in TASKS:
        trace = folder / "traces" / (name + ".json")
        with tempfile.TemporaryDirectory(prefix="hbm_v2_oneshot_") as tmp:
            session = Path(tmp) / "session"
            init(session, CFG, trace, BUDGET, "permutation", actor="llm_one_shot_current_conversation")
            for proposal in read(folder / "one_shot" / (name + ".json")):
                submit(session, proposal)
            one_shot = finish(session)
        baselines = [baseline(trace, "heuristics", 0)] + [
            baseline(trace, method, seed) for method in ("random", "local") for seed in protocol["baseline_seeds"]]
        cases[name] = {"agent": read(folder / "agent" / name / "result.json"),
                       "one_shot": one_shot, "baselines": baselines}
    # Oracle evaluations begin only after all adaptive AND comparison searches finish.
    for name in TASKS:
        cases[name]["exact"] = exact_permutations(load_trace(folder / "traces" / (name + ".json"), CFG), CFG)
    result = {"protocol": protocol, "cases": cases,
              "search_backend_calls": sum(c["agent"]["backend_calls"] + c["one_shot"]["backend_calls"]
                                          + sum(b["backend_calls"] for b in c["baselines"]) for c in cases.values()),
              "oracle_backend_calls": sum(c["exact"]["backend_calls"] for c in cases.values())}
    save(folder / "comparison.json", result)
    return {name: {"agent": c["agent"]["best"]["metrics"]["completion_cycles"],
                   "one_shot": c["one_shot"]["best"]["metrics"]["completion_cycles"],
                   "exact": c["exact"]["best_cycles"]} for name, c in cases.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "start", "compare"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import json
    print(json.dumps({"prepare": prepare, "start": start, "compare": compare}[args.phase](args.output), indent=2))


if __name__ == "__main__":
    main()
