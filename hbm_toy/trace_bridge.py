"""Fixed-trace file bridge with auditable calls; no language model inside."""

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .experiment import Evaluator, activity_policy, read, row_xor, save
from .model import Config, Policy, Request, digest, features


def load_trace(path, cfg):
    obj = read(path)
    if (not isinstance(obj, dict) or type(obj.get("version")) is not int
            or obj["version"] != 1 or not isinstance(obj.get("requests"), list)
            or not obj["requests"] or set(obj) - {"version", "requests", "provenance"}):
        raise ValueError("Trace requires version=1 and a nonempty requests list")
    result, ids = [], set()
    for item in obj["requests"]:
        if (not isinstance(item, dict) or not {"request_id", "address", "arrival"} <= set(item)
                or set(item) - {"request_id", "address", "arrival", "kind", "stream"}):
            raise ValueError("Invalid trace request fields")
        req = Request(**item)
        if any(type(v) is not int or v < 0 for v in
               (req.request_id, req.address, req.arrival, req.stream)):
            raise ValueError("Request integer fields must be nonnegative integers")
        if (req.request_id in ids or req.address >= cfg.blocks * cfg.granule_bytes
                or req.address % cfg.granule_bytes or req.kind not in {"read", "write"}):
            raise ValueError("Duplicate ID, invalid address/alignment, or invalid kind")
        ids.add(req.request_id)
        result.append(req)
    return sorted(result, key=lambda r: (r.arrival, r.request_id))


def statistics(requests, cfg):
    blocks = [r.address // cfg.granule_bytes for r in requests]
    pairs = []
    for a in range(cfg.block_bits):
        for b in range(a + 1, cfg.block_bits):
            counts = Counter(((x >> a) & 1) | (((x >> b) & 1) << 1) for x in blocks)
            pairs.append({"bits": [a, b], "counts_00_10_01_11": [counts[i] for i in range(4)]})
    width = max(1, (len(requests) + 3) // 4)
    return {**features(requests, cfg), "pair_joint_counts": pairs,
            "windows": [{"start_index": i, **features(requests[i:i + width], cfg)}
                        for i in range(0, len(requests), width)]}


def shared_policies(cfg, space):
    # Same evaluated starting points for every method. Never use oracle output.
    reverse = Policy(tuple(("swap_bits", i, cfg.block_bits - 1 - i)
                           for i in range(cfg.block_bits // 2)))
    return [("identity", Policy()), ("reverse_bits", reverse)] if space == "permutation" else [
        ("identity", Policy()), ("row_xor", row_xor(cfg))]


def validate_space(policy, cfg, space):
    if space == "permutation" and any(op[0] != "swap_bits" for op in policy.operations):
        raise ValueError("This session permits swap_bits only; XOR is outside its search space")


def session_requests(folder, state):
    cfg = Config(**state["config"])
    requests = load_trace(Path(folder) / "trace.json", cfg)
    if digest([asdict(r) for r in requests]) != state["trace_id"]:
        raise ValueError("Frozen trace changed; create a new session")
    return cfg, requests


def feedback(folder, state):
    save(Path(folder) / "feedback.json", {
        **{k: state[k] for k in ("protocol", "config", "trace_id", "space", "budget",
                                "backend_calls", "closed", "history")},
        "features": state["features"], "remaining_calls": state["budget"] - state["backend_calls"],
        "max_proposals": state["max_proposals"], "trace_file": "trace.json",
        "objective": "minimize completion_cycles on the frozen trace; static bijection",
        "decode_order_low_to_high": ["channel", "bank", "column", "row"],
        "proposal_schema": {"hypothesis": "brief testable rationale", "expected": "prediction",
                            "dsl": {"version": 0, "operations": []}},
        "note": "No oracle or other searcher's scores exposed. Repeated candidates are cached. "
                "Backend failures count; invalid DSLs do not. No new holdout is invented at finish."})


def init(folder, cfg, trace_path, budget=20, space="linear", actor="llm_file_bridge_current_conversation"):
    if type(budget) is not int or budget < 2 or space not in {"linear", "permutation"}:
        raise ValueError("Invalid budget or search space")
    requests = load_trace(trace_path, cfg)
    seeds = shared_policies(cfg, space)
    for _, policy in seeds:
        Policy.parse(policy.to_dict(), cfg)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "trace.json", {"version": 1, "requests": [asdict(r) for r in requests]})
    state = {"protocol": "hbm-file-bridge-v2", "config": cfg.to_dict(), "space": space,
             "trace_id": digest([asdict(r) for r in requests]), "budget": budget,
             "backend_calls": 0, "max_proposals": budget * 10, "closed": False,
             "actor": actor, "history": [], "features": statistics(requests, cfg),
             "started_utc": datetime.now(timezone.utc).isoformat()}
    save(folder / "state.json", state)
    for label, policy in seeds:
        submit(folder, {"hypothesis": label, "expected": "shared starting point",
                        "dsl": policy.to_dict()}, actor="shared_baseline")
    return read(folder / "state.json")


def submit(folder, proposal, actor=None):
    folder = Path(folder)
    state = read(folder / "state.json")
    if state["closed"] or state["backend_calls"] >= state["budget"]:
        raise ValueError("Session closed or performance evaluation budget exhausted")
    if len(state["history"]) >= state["max_proposals"]:
        raise ValueError("Proposal limit exhausted")
    cfg, requests = session_requests(folder, state)
    evaluator = Evaluator(cfg, requests)
    evaluator.cache = {e["candidate_id"]: e for e in state["history"] if "metrics" in e}
    entry = {"actor": actor or state["actor"], "proposal": proposal,
             "proposal_number": len(state["history"]) + 1,
             "submitted_utc": datetime.now(timezone.utc).isoformat()}
    try:
        if not isinstance(proposal, dict):
            raise ValueError("Proposal must be an object")
        for key in ("hypothesis", "expected"):
            if not isinstance(proposal.get(key), str) or not proposal[key].strip():
                raise ValueError(f"Proposal requires nonempty {key}")
        policy = Policy.parse(proposal.get("dsl"), cfg)
        validate_space(policy, cfg, state["space"])
        result = evaluator.evaluate(policy)
        entry.update({k: result[k] for k in ("candidate_id", "dsl", "verification", "metrics",
                                             "backend_seconds", "cached")})
        entry.update(status="ok", hypothesis=proposal["hypothesis"], expected=proposal["expected"])
    except (ValueError, RuntimeError) as error:
        entry.update(status="backend_error" if evaluator.backend_calls else "invalid", error=str(error))
    state["backend_calls"] += evaluator.backend_calls
    entry["backend_calls"] = state["backend_calls"]
    previous = [e["metrics"]["completion_cycles"] for e in state["history"] if "metrics" in e]
    if "metrics" in entry:
        previous.append(entry["metrics"]["completion_cycles"])
    entry["best_cycles"] = min(previous) if previous else None
    state["history"].append(entry)
    save(folder / "state.json", state)
    feedback(folder, state)
    return entry


def step(folder, proposal_path):
    return submit(folder, read(proposal_path))


def finish(folder):
    folder = Path(folder)
    state = read(folder / "state.json")
    session_requests(folder, state)
    if state["closed"]:
        return read(folder / "result.json")
    valid = [e for e in state["history"] if "metrics" in e]
    if not valid:
        raise ValueError("No successful candidates")
    state["closed"] = True
    result = {**state, "best": min(valid, key=lambda e: e["metrics"]["completion_cycles"]),
              "finished_utc": datetime.now(timezone.utc).isoformat(),
              "model_id": "current_conversation_runtime_id_unavailable", "model_tokens": None,
              "model_cost": None, "holdout_calls_not_in_search_budget": 0,
              "backend_seconds": sum(e.get("backend_seconds", 0) for e in state["history"]
                                     if not e.get("cached", False)),
              "limits": ["toy timing", "fixed supplied trace only; no held-out generalization claim",
                         "conversation-based proposals; end-to-end model cost not measurable here"]}
    save(folder / "result.json", result)
    save(folder / "state.json", state)
    feedback(folder, state)
    return result
