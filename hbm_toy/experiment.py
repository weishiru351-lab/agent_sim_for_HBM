"""Budgeted baselines and a file protocol for a real, external model proposer."""

from dataclasses import asdict
import json
from pathlib import Path
import random
import time

from .model import Config, Policy, digest, features, simulate, verify, workload


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def row_xor(cfg):
    row_start = cfg.channel_bits + cfg.bank_bits + cfg.column_bits
    return Policy(tuple(("xor_bit", dst, row_start + dst % cfg.row_bits)
                        for dst in range(min(cfg.channel_bits + cfg.bank_bits, cfg.max_operations))))


def activity_policy(requests, cfg, pack_columns=False):
    """A transparent hand-written bit-transition heuristic, NOT an AI agent or DReAM reproduction."""
    rates = features(requests, cfg)["bit_transition_rates"]
    selected = sorted(range(cfg.block_bits), key=lambda bit: (-rates[bit], bit))
    labels, ops = list(range(cfg.block_bits)), []
    targets = cfg.channel_bits + cfg.bank_bits + (cfg.column_bits if pack_columns else 0)
    for dst, original_bit in enumerate(selected[:targets]):
        src = labels.index(original_bit)
        if src != dst and len(ops) < cfg.max_operations:
            ops.append(("swap_bits", dst, src))
            labels[dst], labels[src] = labels[src], labels[dst]
    return Policy(tuple(ops))


def random_op(rng, cfg):
    a, b = rng.sample(range(cfg.block_bits), 2)
    return rng.choice(("xor_bit", "swap_bits")), a, b


def random_policy(rng, cfg):
    return Policy(tuple(random_op(rng, cfg) for _ in range(rng.randint(1, cfg.max_operations))))


def mutate(policy, rng, cfg):
    ops = list(policy.operations)
    choices = ["replace", "delete"] if ops else []
    if len(ops) < cfg.max_operations:
        choices.append("insert")
    action = rng.choice(choices)
    if action == "insert":
        ops.insert(rng.randrange(len(ops) + 1), random_op(rng, cfg))
    elif action == "delete":
        ops.pop(rng.randrange(len(ops)))
    else:
        ops[rng.randrange(len(ops))] = random_op(rng, cfg)
    return Policy(tuple(ops))


class Evaluator:
    def __init__(self, cfg, requests):
        self.cfg, self.requests = cfg, requests
        self.cache = {}
        self.backend_calls = 0
        self.trace_id = digest([asdict(r) for r in requests])

    def evaluate(self, policy):
        policy = Policy.parse(policy.to_dict(), self.cfg)
        key = digest({"masks": policy.masks(self.cfg), "config": self.cfg.to_dict(),
                      "trace": self.trace_id, "backend": "toy-fcfs-v1"})
        if key in self.cache:
            return {**self.cache[key], "cached": True}
        check = verify(policy, self.cfg)
        if check["status"] != "pass":
            raise ValueError("Functional mapping check failed")
        start = time.perf_counter()
        self.backend_calls += 1
        metrics = simulate(self.requests, policy, self.cfg)
        result = {"candidate_id": key, "dsl": policy.to_dict(), "verification": check,
                  "metrics": metrics, "backend_seconds": time.perf_counter() - start,
                  "cached": False}
        self.cache[key] = result
        return result


def run_search(method, cfg, requests, budget=20, seed=0):
    if method not in {"random", "local", "activity_local", "packed_local"} or budget < 2:
        raise ValueError("Unknown search method or budget below 2")
    if cfg.block_bits < 2 or cfg.max_operations < 1:
        raise ValueError("Search needs at least two bits and one allowed operation")
    rng = random.Random(seed)
    evaluator = Evaluator(cfg, requests)
    history, best = [], None
    start = time.perf_counter()
    # Identical evaluated starting points for all searchers; both count in budget.
    seeds = [Policy(), row_xor(cfg)]
    if method in {"activity_local", "packed_local"}:
        seeds.append(activity_policy(requests, cfg, pack_columns=method == "packed_local"))
    for attempt in range(budget * 30):
        if evaluator.backend_calls >= budget:
            break
        if attempt < len(seeds):
            proposal = seeds[attempt]
        elif method == "random" or attempt % 7 == 0:
            proposal = random_policy(rng, cfg)
        else:
            proposal = mutate(Policy.parse(best["dsl"], cfg), rng, cfg)
        result = evaluator.evaluate(proposal)
        if best is None or result["metrics"]["completion_cycles"] < best["metrics"]["completion_cycles"]:
            best = result
        history.append({**result, "proposal_number": attempt + 1, "backend_calls": evaluator.backend_calls,
                        "best_cycles": best["metrics"]["completion_cycles"]})
    return {"method": method, "is_llm_agent": False, "seed": seed, "budget": budget,
            "backend_calls": evaluator.backend_calls, "proposals": len(history),
            "trace_id": evaluator.trace_id, "config": cfg.to_dict(),
            "wall_seconds": time.perf_counter() - start, "best": best, "history": history}


def write_feedback(folder, state):
    cfg = Config(**state["config"])
    requests = workload(state["workload"], cfg, state["count"], state["seed"])
    save(Path(folder) / "feedback.json", {
        "protocol": "hbm-file-bridge-v1", "actor": "external_language_model",
        "closed": state["closed"], "config": state["config"],
        "workload": state["workload"], "trace_id": state["trace_id"],
        "features": features(requests, cfg),
        "objective": "minimize completion_cycles; static bijective mapping; no config or trace changes",
        "budget": state["budget"], "backend_calls": state["backend_calls"],
        "remaining_calls": state["budget"] - state["backend_calls"],
        "max_proposals": state["budget"] * 10,
        "proposal_schema": {"hypothesis": "brief hypothesis based on feedback",
                            "expected": "predicted metric changes and tradeoffs",
                            "dsl": {"version": 0, "operations": []}},
        "dsl_rules": "Sequential xor_bit(dst,src) and swap_bits(a,b), distinct bits in [0,block_bits); max_operations limit. Bits are block bits; byte offset unchanged.",
        "decode_order_low_to_high": ["channel", "bank", "column", "row"],
        "history": state["history"],
        "note": "No held-out metrics are exposed until finish. Tools are deterministic; this file does not contain an autonomous LLM runtime."})


def session_init(folder, cfg, name, count, seed, budget, trace_path=None, space="linear"):
    if trace_path is not None:
        from .trace_bridge import init
        return init(folder, cfg, trace_path, budget, space)
    if budget < 2:
        raise ValueError("Budget must include at least the two baseline evaluations")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    requests = workload(name, cfg, count, seed)
    evaluator = Evaluator(cfg, requests)
    history = [{**evaluator.evaluate(policy), "actor": "shared_baseline", "hypothesis": label}
               for label, policy in (("identity", Policy()), ("row_xor", row_xor(cfg)))]
    state = {"protocol": "hbm-file-bridge-v1", "config": cfg.to_dict(), "workload": name,
             "count": count, "seed": seed, "budget": budget, "trace_id": evaluator.trace_id,
             "backend_calls": evaluator.backend_calls, "closed": False, "history": history}
    save(folder / "state.json", state)
    write_feedback(folder, state)
    return state


def session_step(folder, proposal_path):
    folder = Path(folder)
    state = read(folder / "state.json")
    if state["protocol"] == "hbm-file-bridge-v2":
        from .trace_bridge import step
        return step(folder, proposal_path)
    if state["closed"] or state["backend_calls"] >= state["budget"]:
        raise ValueError("Session is closed or evaluation budget exhausted")
    if len(state["history"]) >= state["budget"] * 10:
        raise ValueError("Proposal limit exhausted")
    cfg = Config(**state["config"])
    requests = workload(state["workload"], cfg, state["count"], state["seed"])
    evaluator = Evaluator(cfg, requests)
    if evaluator.trace_id != state["trace_id"]:
        raise ValueError("Workload changed since session creation")
    for entry in state["history"]:
        if "candidate_id" in entry:
            evaluator.cache[entry["candidate_id"]] = entry
    proposal = read(proposal_path)
    if not isinstance(proposal, dict):
        raise ValueError("Proposal must be a JSON object")
    try:
        for field in ("hypothesis", "expected"):
            if not isinstance(proposal.get(field), str) or not proposal[field].strip():
                raise ValueError(f"Proposal needs nonempty {field}")
        policy = Policy.parse(proposal.get("dsl"), cfg)
        result = evaluator.evaluate(policy)
        entry = {**result, "actor": "llm_file_bridge_current_conversation",
                 "hypothesis": proposal["hypothesis"], "expected": proposal["expected"]}
        state["backend_calls"] += evaluator.backend_calls
    except ValueError as error:
        entry = {"actor": "llm_file_bridge_current_conversation", "status": "invalid",
                 "error": str(error), "proposal": proposal}
    entry["proposal_number"] = len(state["history"]) + 1
    entry["backend_calls"] = state["backend_calls"]
    state["history"].append(entry)
    save(folder / "state.json", state)
    write_feedback(folder, state)
    return entry


def session_finish(folder):
    folder = Path(folder)
    state = read(folder / "state.json")
    if state["protocol"] == "hbm-file-bridge-v2":
        from .trace_bridge import finish
        return finish(folder)
    if state["closed"]:
        return read(folder / "result.json")
    valid = [entry for entry in state["history"] if "metrics" in entry]
    best = min(valid, key=lambda e: e["metrics"]["completion_cycles"])
    cfg = Config(**state["config"])
    heldout = workload(state["workload"], cfg, state["count"], state["seed"], holdout=True)
    result = {"method": "llm_file_bridge", "is_llm_agent": True,
              "execution_mode": "current conversation writes proposals and invokes tools; not an unattended service",
              "model_id": "current_conversation_not_recorded_by_runtime",
              "model_tokens": None, "model_cost": None,
              "config": state["config"], "trace_id": state["trace_id"],
              "count": state["count"], "seed": state["seed"],
              "workload": state["workload"], "budget": state["budget"],
              "backend_calls": state["backend_calls"], "best": best, "history": state["history"],
              "holdout": simulate(heldout, Policy.parse(best["dsl"], cfg), cfg),
              "holdout_identity": simulate(heldout, Policy(), cfg),
              "holdout_calls_not_in_search_budget": 2}
    state["closed"] = True
    save(folder / "result.json", result)
    save(folder / "state.json", state)
    write_feedback(folder, state)
    return result
