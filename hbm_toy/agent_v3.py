"""Frozen 2x2 prompt/feedback pilot with external, independent model proposers."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from .agent_v2 import baseline
from .agent_v2_diagnosis import phase_feedback
from .experiment import activity_policy, read, save
from .model import Config, Policy, Request, digest, simulate, verify
from .rule_study import exact_permutations, joint_policy, permutation_policy
from .trace_bridge import load_trace, shared_policies, statistics, validate_space


CFG = Config(channel_bits=2, bank_bits=1, column_bits=1, row_bits=2)
TASKS = ("N01_burst", "N02_paced", "N03_gap")
CONDITIONS = ("old_summary", "new_summary", "old_phase", "new_phase")
OLD_PROMPT = ("Read the fixed trace/statistics. Minimize completion_cycles using up to 20 actual backend "
              "calls including identity and reverse. Submit legal swap_bits DSL with a brief testable "
              "hypothesis and prediction; use your feedback to decide the next proposal. You may use "
              "the available activity/joint heuristic proposals; evaluating them counts. No trace/config "
              "changes, oracle, competitor scores, or additional uncounted simulation during search.")


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def commitment(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def sources():
    root = Path(__file__).resolve().parent.parent
    paths = ["hbm_toy/" + n + ".py" for n in
             ("model", "experiment", "rule_study", "trace_bridge", "agent_v2", "agent_v2_diagnosis", "agent_v3")]
    paths.append("examples/agent_v3_prompt.md")
    return {p: hashlib.sha256((root / p).read_text(encoding="utf-8").encode()).hexdigest() for p in paths}


def tasks():
    def place(x, order):
        return sum(((x >> bit) & 1) << i for i, bit in enumerate(order))
    patterns = [
        ([place((i // 3) % 32, [1, 4, 0, 5, 2, 3]) for i in range(96)],
         [place((i * 7 + i // 12) % 64, [4, 0, 3, 1, 5, 2]) for i in range(96)]),
        ([place((i * 7 + i // 8) % 64, [3, 0, 5, 2, 1, 4]) for i in range(96)],
         [place(((i // 2) * 5) % 64, [1, 5, 2, 4, 0, 3]) for i in range(96)]),
        ([place((i % 3) * 16 + (i // 3) % 16, [5, 2, 4, 0, 3, 1]) for i in range(96)],
         [place((i * 9) % 64, [2, 4, 1, 3, 0, 5]) for i in range(96)]),
    ]
    result = {}
    for index, (name, (first, second)) in enumerate(zip(TASKS, patterns)):
        result[name] = [Request(i, block * 64,
                               i // 8 if index == 0 else (i * 4 if index == 1 else
                               (i // 8 if i < 96 else 4096 + (i - 96) // 8)),
                               "write" if i % 12 == 11 else "read", int(i >= 96))
                        for i, block in enumerate(first + second)]
    return result


def prepare(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    generated = tasks()
    new_prompt = (Path(__file__).resolve().parent.parent / "examples/agent_v3_prompt.md").read_text(encoding="utf-8")
    protocol = {"version": 3, "created_utc": timestamp(), "config": CFG.to_dict(), "budget": 20,
                "tasks": list(TASKS), "conditions": list(CONDITIONS), "split_at": 96,
                "space": "all 720 six-bit permutations; swap_bits only; static mapping",
                "prompts": {"old": OLD_PROMPT, "new": new_prompt}, "source_hashes": sources(),
                "trace_ids": {name: digest([asdict(r) for r in reqs]) for name, reqs in generated.items()},
                "shared_start": ["identity", "reverse_bits"], "baseline_seeds": [0, 1, 2],
                "started_utc": None, "one_shot_commitments": None,
                "context_design": "Four fresh non-forked model contexts, one per condition; three tasks "
                                  "within a condition share that context. Sibling files/results prohibited "
                                  "by experiment instructions, not filesystem security isolation.",
                "feedback_design": "All conditions see identical input statistics, phase boundary, "
                                   "and heuristic DSL tools. Phase conditions additionally receive cohort "
                                   "metrics extracted from the SAME simulation, without resets or extra runs.",
                "one_shot_design": "Supplementary coordinator-generated batch frozen before any new "
                                   "performance. Coordinator knows V2 diagnosis; different context from "
                                   "the four workers, so NOT a clean ablation of any worker.",
                "cost_boundary": "Unique executed candidates and backend failures count; cache hits and "
                                 "invalid DSL do not; 200 proposal cap. Time includes event extraction. "
                                 "Oracle revealed only after all searches; model cost unavailable.",
                "limits": ["Synthetic toy, one model run per condition per task; no significance claim",
                           "Task designer knows generators, no external blind test",
                           "Prompt and feedback effects are exploratory observations, not established causality",
                           "Explicit known midpoint, not automatic phase detection",
                           "No Ramulator, real KV trace, or held-out generalization assessment"]}
    for name, requests in generated.items():
        save(folder / "traces" / (name + ".json"), {"version": 1, "requests": [asdict(r) for r in requests]})
        rules = {"activity": activity_policy(requests, CFG, True).to_dict(),
                 "joint": joint_policy(requests, CFG, True).to_dict()}
        save(folder / "inputs" / (name + ".json"),
             {"config": CFG.to_dict(), "statistics": statistics(requests, CFG), "split_at": 96,
              "available_heuristic_proposals": rules, "trace_id": protocol["trace_ids"][name]})
    for condition in CONDITIONS:
        save(folder / "briefings" / (condition + ".json"), {
            "condition": condition, "prompt": protocol["prompts"][condition.split("_")[0]],
            "tasks": list(TASKS), "budget_per_task": 20, "config": CFG.to_dict(), "split_at": 96,
            "feedback": "cohort and aggregate metrics" if condition.endswith("phase") else "aggregate metrics only",
            "common_tools": "Input JSON contains activity/joint heuristic DSLs; evaluate through submit to count calls.",
            "restrictions": "Read only this briefing, inputs/, traces/, and your own sessions. No source inspection, "
                            "prior archives, peer briefings/sessions, one_shot/, or comparison/oracle. "
                            "No direct simulator/evaluator calls outside submit. Use the provided Python APIs. "
                            "Write proposals only within your own sessions. Freeze sources/config/inputs."})
    protocol["input_commitments"] = {str(path.relative_to(folder)).replace('\\', '/'): commitment(read(path))
                                     for kind in ("inputs", "briefings") for path in sorted((folder / kind).glob("*.json"))}
    save(folder / "protocol.json", protocol)
    return {"tasks": list(TASKS), "conditions": list(CONDITIONS), "backend_calls": 0}


def check(folder):
    folder = Path(folder)
    p = read(folder / "protocol.json")
    if p["source_hashes"] != sources():
        raise ValueError("Frozen implementation changed")
    for path, sha in p["input_commitments"].items():
        if commitment(read(folder / path)) != sha:
            raise ValueError("Frozen input/briefing changed")
    for name in p["tasks"]:
        reqs = load_trace(folder / "traces" / (name + ".json"), Config(**p["config"]))
        if digest([asdict(r) for r in reqs]) != p["trace_ids"][name]:
            raise ValueError("Frozen trace changed")
    if p["one_shot_commitments"]:
        if p.get("protocol_commitment") != commitment({k: v for k, v in p.items() if k != "protocol_commitment"}):
            raise ValueError("Frozen protocol changed")
        for name, sha in p["one_shot_commitments"].items():
            if commitment(read(folder / "one_shot" / (name + ".json"))) != sha:
                raise ValueError("Frozen one-shot proposals changed")
    return p


def write_feedback(folder, state):
    save(folder / "feedback.json", {
        **{k: state[k] for k in ("condition", "task", "config", "budget", "backend_calls", "closed", "history")},
        "remaining_calls": state["budget"] - state["backend_calls"],
        "objective": "minimize completion_cycles", "decode_order_low_to_high": ["channel", "bank", "column", "row"],
        "input_file": str(Path(state["study"]) / "inputs" / (state["task"] + ".json")),
        "note": "No extra simulator calls outside this interface. Do not inspect other conditions or archives."})


def submit(folder, proposal):
    folder = Path(folder)
    state = read(folder / "state.json")
    study = Path(state["study"])
    protocol = check(study)
    if state["config"] != protocol["config"] or state["budget"] != protocol["budget"]:
        raise ValueError("Session configuration/budget changed")
    if state["closed"] or state["backend_calls"] >= state["budget"] or len(state["history"]) >= 200:
        raise ValueError("Session closed or budget exhausted")
    cfg = Config(**state["config"])
    requests = load_trace(study / "traces" / (state["task"] + ".json"), cfg)
    entry = {"proposal": proposal, "proposal_number": len(state["history"]) + 1,
             "submitted_utc": timestamp(), "cached": False, "backend_seconds": 0}
    try:
        if not isinstance(proposal, dict) or any(not isinstance(proposal.get(k), str) or not proposal[k].strip()
                                                for k in ("hypothesis", "expected")):
            raise ValueError("Proposal requires hypothesis and expected text")
        policy = Policy.parse(proposal.get("dsl"), cfg)
        validate_space(policy, cfg, "permutation")
        verified = verify(policy, cfg)
        if verified["status"] != "pass":
            raise ValueError("Mapping verification failed")
    except (ValueError, TypeError) as error:
        entry.update(status="invalid", error=str(error))
    else:
        key = digest({"masks": policy.masks(cfg), "config": cfg.to_dict(), "trace": protocol["trace_ids"][state["task"]]})
        entry.update(candidate_id=key, dsl=policy.to_dict(), verification=verified,
                     hypothesis=proposal["hypothesis"], expected=proposal["expected"])
        cached = next((e for e in state["history"] if e.get("candidate_id") == key and e["status"] == "ok"), None)
        if cached:
            entry.update(status="ok", cached=True, metrics=cached["metrics"])
            if "phase_feedback" in cached:
                entry["phase_feedback"] = cached["phase_feedback"]
        else:
            state["backend_calls"] += 1
            start = time.perf_counter()
            try:
                metrics = simulate(requests, policy, cfg, include_events=True)
                events = metrics.pop("events")
                if state["condition"].endswith("phase"):
                    entry["phase_feedback"] = phase_feedback(requests, events, cfg, protocol["split_at"])
                entry.update(status="ok", metrics=metrics)
            except Exception as error:
                entry.update(status="backend_error", error=f"{type(error).__name__}: {error}")
            entry["backend_seconds"] = time.perf_counter() - start
    entry["backend_calls"] = state["backend_calls"]
    state["history"].append(entry)
    entry["best_cycles"] = min((e["metrics"]["completion_cycles"] for e in state["history"] if e["status"] == "ok"), default=None)
    save(folder / "state.json", state)
    write_feedback(folder, state)
    return entry


def init_session(study, condition, task, folder=None):
    study = Path(study).resolve()
    p = check(study)
    if condition not in (*CONDITIONS, "coordinator_one_shot") or task not in TASKS:
        raise ValueError("Unknown condition/task")
    if not p["started_utc"]:
        raise ValueError("Freeze all one-shot proposals before performance")
    folder = Path(folder) if folder is not None else study / "sessions" / condition / task
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "state.json", {"protocol": "hbm-factorial-v3", "study": str(study),
         "condition": condition, "task": task, "config": p["config"], "budget": p["budget"],
         "trace_id": p["trace_ids"][task], "backend_calls": 0, "history": [], "closed": False,
         "started_utc": timestamp()})
    for name, policy in shared_policies(Config(**p["config"]), "permutation"):
        submit(folder, {"hypothesis": name, "expected": "shared starting point", "dsl": policy.to_dict()})
    return folder


def start(folder):
    folder = Path(folder)
    p = check(folder)
    if p["started_utc"]:
        raise ValueError("Already started")
    commitments = {}
    for name in TASKS:
        batch = read(folder / "one_shot" / (name + ".json"))
        if not isinstance(batch, list) or len(batch) != 18:
            raise ValueError("Freeze exactly 18 one-shot candidates per task")
        seen = {policy.masks(CFG) for _, policy in shared_policies(CFG, "permutation")}
        for proposal in batch:
            if not isinstance(proposal, dict) or any(not isinstance(proposal.get(k), str) or not proposal[k].strip()
                                                   for k in ("hypothesis", "expected")):
                raise ValueError("One-shot rationale missing")
            policy = Policy.parse(proposal["dsl"], CFG)
            validate_space(policy, CFG, "permutation")
            if policy.masks(CFG) in seen:
                raise ValueError("One-shot candidates must be unique and exclude shared starts")
            seen.add(policy.masks(CFG))
        commitments[name] = commitment(batch)
    p.update(started_utc=timestamp(), one_shot_commitments=commitments)
    p["protocol_commitment"] = commitment(p)
    save(folder / "protocol.json", p)
    for condition in CONDITIONS:
        for task in TASKS:
            init_session(folder, condition, task)
    return {"started_sessions": 12, "initial_backend_calls": 24}


def finish(folder, note):
    folder = Path(folder)
    state = read(folder / "state.json")
    check(Path(state["study"]))
    if state["closed"]:
        return read(folder / "result.json")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Record a stopping note")
    valid = [e for e in state["history"] if e["status"] == "ok"]
    if not valid:
        raise ValueError("No successful candidate")
    state.update(closed=True, finished_utc=timestamp(), stopping_note=note)
    result = {**state, "best": min(valid, key=lambda e: e["metrics"]["completion_cycles"]),
              "backend_seconds": sum(e["backend_seconds"] for e in state["history"]),
              "model_id": "inherited_runtime_model_identifier_unavailable", "model_tokens": None, "model_cost": None}
    save(folder / "state.json", state)
    save(folder / "result.json", result)
    write_feedback(folder, state)
    return result


def compare(folder):
    import tempfile
    folder = Path(folder)
    p = check(folder)
    if not p["started_utc"] or (folder / "comparison.json").exists():
        raise ValueError("Not started or comparison already exists")
    for condition in CONDITIONS:
        for task in TASKS:
            if not read(folder / "sessions" / condition / task / "state.json")["closed"]:
                raise ValueError("Finish ALL four conditions before any comparisons/oracle")
    cases = {}
    for task in TASKS:
        with tempfile.TemporaryDirectory(prefix="hbm_v3_oneshot_") as tmp:
            session = init_session(folder, "coordinator_one_shot", task, Path(tmp) / "session")
            for proposal in read(folder / "one_shot" / (task + ".json")):
                submit(session, proposal)
            one_shot = finish(session, "Precommitted supplementary batch; no feedback decisions")
        trace = folder / "traces" / (task + ".json")
        cases[task] = {"agents": {c: read(folder / "sessions" / c / task / "result.json") for c in CONDITIONS},
                       "one_shot": one_shot,
                       "baselines": [baseline(trace, "heuristics", 0)] +
                       [baseline(trace, method, seed) for method in ("random", "local") for seed in p["baseline_seeds"]]}
    for task in TASKS:
        cases[task]["exact"] = exact_permutations(load_trace(folder / "traces" / (task + ".json"), CFG), CFG)
    result = {"protocol": p, "cases": cases,
              "search_backend_calls": sum(sum(r["backend_calls"] for r in c["agents"].values()) + c["one_shot"]["backend_calls"]
                                          + sum(r["backend_calls"] for r in c["baselines"]) for c in cases.values()),
              "oracle_backend_calls": sum(c["exact"]["backend_calls"] for c in cases.values())}
    save(folder / "comparison.json", result)
    return {task: {**{name: r["best"]["metrics"]["completion_cycles"] for name, r in c["agents"].items()},
                   "one_shot": c["one_shot"]["best"]["metrics"]["completion_cycles"], "exact": c["exact"]["best_cycles"]}
            for task, c in cases.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "step", "finish", "compare"))
    parser.add_argument("--folder", type=Path, required=True, help="study folder, or session folder for step/finish")
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--note")
    args = parser.parse_args()
    if args.action == "step":
        result = submit(args.folder, read(args.proposal))
    elif args.action == "finish":
        result = finish(args.folder, args.note)
        result = {"backend_calls": result["backend_calls"], "best_cycles": result["best"]["metrics"]["completion_cycles"]}
    else:
        result = {"prepare": prepare, "start": start, "compare": compare}[args.action](args.folder)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
