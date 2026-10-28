"""Shared search loop and random/local algorithms; domains supply candidate operators."""

import random
import time

from agent_design_search.exploration.evaluation import Evaluator


def search(task, propose, budget, max_proposals=None):
    if type(budget) is not int or budget < 1:
        raise ValueError("Budget must be a positive integer")
    evaluator = Evaluator(task, budget)
    history, best = [], None
    start = time.perf_counter()
    for attempt in range(max_proposals if max_proposals is not None else budget * 30):
        if evaluator.backend_calls >= budget:
            break
        result = evaluator.evaluate(propose(attempt, best))
        if best is None or task.objective.rank(result) < task.objective.rank(best):
            best = result
        history.append({**result, "proposal_number": attempt + 1, "backend_calls": evaluator.backend_calls,
                        "best_value": task.objective.value(best["metrics"])})
    return {"budget": budget, "backend_calls": evaluator.backend_calls, "proposals": len(history),
            "wall_seconds": time.perf_counter() - start, "best": best, "history": history}


def random_local_search(task, method, initial_candidates, random_candidate, mutate_candidate=None,
                        budget=20, seed=0):
    """Evaluate seeds, then sample or mutate the best; restart every seventh attempt.

    Candidates are JSON objects. Generators receive the seeded RNG; a mutation
    also receives the current best candidate. All evaluations share one budget.
    """
    if method not in {"random", "local"} or (method == "local" and mutate_candidate is None):
        raise ValueError("Choose random or local; local search needs a mutation operator")
    rng = random.Random(seed)
    def propose(attempt, best):
        if attempt < len(initial_candidates):
            return initial_candidates[attempt]
        if method == "random" or attempt % 7 == 0:
            return random_candidate(rng)
        return mutate_candidate(best["dsl"], rng)
    return {**search(task, propose, budget), "method": method, "is_llm_agent": False, "seed": seed}
