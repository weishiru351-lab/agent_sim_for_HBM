"""Persistent external-Agent sessions; the caller supplies the design task."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from agent_design_search.contracts import CandidateError
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.json_io import loads, read, save


PROTOCOL = "agent-memory-search-v3"


def load_state(folder, task):
    state = read(Path(folder) / "state.json")
    if state["protocol"] != PROTOCOL:
        raise ValueError("Historical session protocol; use its original implementation")
    if state["task"] != task.definition or state["objective"] != asdict(task.objective):
        raise ValueError("Frozen task changed; create a new session")
    return state


def persist(folder, state, task):
    save(Path(folder) / "state.json", state)
    save(Path(folder) / "feedback.json", {
        **state, "remaining_calls": state["budget"] - state["backend_calls"],
        "task_feedback": task.feedback(),
        "proposal_schema": {"hypothesis": "testable rationale", "expected": "prediction", "dsl": "task-specific candidate"},
        "note": "Invalid candidates and cache hits do not consume backend calls. "
                "Started failures count. Closing does not create a holdout."})


def session_init(folder, task, budget=None, seeds=(), actor="external_conversation"):
    if budget is None:
        budget = getattr(task, "budget", 20)
    elif hasattr(task, "budget") and budget != task.budget:
        raise ValueError("Budget differs from the frozen search task")
    if type(budget) is not int or budget < max(1, len(seeds)):
        raise ValueError("Budget must cover the shared starting candidates")
    for _, candidate in seeds:
        task.normalize(candidate)
    folder = Path(folder)
    # Existing unrelated files may remain, but never overwrite a session.
    folder.mkdir(parents=True, exist_ok=True)
    if any((folder / name).exists() for name in ("state.json", "feedback.json", "result.json")):
        raise ValueError("Session already exists")
    if hasattr(task, "snapshot"):
        save(folder / "task.json", task.snapshot())
    state = {"protocol": PROTOCOL, "task": task.definition, "objective": asdict(task.objective),
             "budget": budget, "backend_calls": 0, "max_proposals": budget * 10,
             "closed": False, "actor": actor, "history": [],
             "started_utc": datetime.now(timezone.utc).isoformat()}
    persist(folder, state, task)
    for label, candidate in seeds:
        submit(folder, task, {"hypothesis": label, "expected": "shared starting point", "dsl": candidate},
               actor="shared_baseline")
    return read(folder / "state.json")


def submit_file(folder, task, path):
    """Record malformed files as proposals too, under the same retry limit."""
    raw, error = None, None
    try:
        raw = Path(path).read_text(encoding="utf-8")
        proposal = loads(raw)
    except json.JSONDecodeError as exc:
        error = CandidateError("invalid_json", "$", exc.msg, line=exc.lineno, column=exc.colno)
    except ValueError as exc:
        error = CandidateError("invalid_json", "$", str(exc))
    except (OSError, UnicodeError) as exc:
        error = CandidateError("proposal_read_error", "$", str(exc), file=str(path))
    return submit(folder, task, raw if error else proposal, input_error=error)


def submit(folder, task, proposal, actor=None, input_error=None):
    state = load_state(folder, task)
    if state["closed"] or state["backend_calls"] >= state["budget"]:
        raise ValueError("Session closed or performance evaluation budget exhausted")
    if len(state["history"]) >= state["max_proposals"]:
        raise ValueError("Proposal limit exhausted")
    evaluator = Evaluator(task, state["budget"] - state["backend_calls"])
    evaluator.cache = {e["candidate_id"]: e for e in state["history"] if "metrics" in e}
    entry = {"actor": actor or state["actor"], "proposal": proposal,
             "proposal_number": len(state["history"]) + 1,
             "submitted_utc": datetime.now(timezone.utc).isoformat()}
    try:
        if input_error is not None:
            raise input_error
        if not isinstance(proposal, dict):
            raise CandidateError("invalid_proposal", "$", "Proposal must be an object")
        for key in ("hypothesis", "expected"):
            if not isinstance(proposal.get(key), str) or not proposal[key].strip():
                raise CandidateError("missing_rationale", f"$.{key}", f"Proposal requires nonempty {key}")
        result = evaluator.evaluate(proposal.get("dsl"))
        entry.update({k: result[k] for k in ("candidate_id", "dsl", "verification", "metrics",
                                            "backend_seconds", "cached")})
        entry.update(status="ok", hypothesis=proposal["hypothesis"], expected=proposal["expected"])
    except (ValueError, RuntimeError, OSError) as error:
        status = "backend_error" if evaluator.backend_calls else "invalid"
        detail = error if isinstance(error, CandidateError) else CandidateError(status, "$", str(error))
        entry.update(status=status, error=str(error), error_detail=detail.to_dict(),
                     backend_seconds=getattr(evaluator, "backend_seconds", 0.0))
    state["backend_calls"] += evaluator.backend_calls
    entry["validation_seconds"] = evaluator.validation_seconds
    entry["backend_calls"] = state["backend_calls"]
    valid = [e for e in [*state["history"], entry] if "metrics" in e]
    best = min(valid, key=task.objective.rank) if valid else None
    entry["best_value"] = task.objective.value(best["metrics"]) if best else None
    state["history"].append(entry)
    persist(folder, state, task)
    return entry


def session_finish(folder, task):
    state = load_state(folder, task)
    if state["closed"]:
        return read(Path(folder) / "result.json")
    valid = [e for e in state["history"] if "metrics" in e]
    if not valid:
        raise ValueError("No successful candidates")
    state["closed"] = True
    result = {**state, "best": min(valid, key=task.objective.rank),
              "finished_utc": datetime.now(timezone.utc).isoformat(),
              "model_id": None, "model_tokens": None, "model_cost": None,
              "holdout_calls_not_in_search_budget": 0,
              "validation_seconds": sum(e.get("validation_seconds", 0) for e in state["history"]),
              "backend_seconds": sum(e.get("backend_seconds", 0) for e in state["history"]
                                     if not e.get("cached", False))}
    save(Path(folder) / "result.json", result)
    persist(folder, state, task)
    return result
