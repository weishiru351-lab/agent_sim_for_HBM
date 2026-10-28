"""Validate, cache and count candidate evaluations for an injected design task."""

import time

from agent_design_search.contracts import CandidateError, DesignTask


class Evaluator:
    def __init__(self, task: DesignTask, budget=None, *, measure=None):
        limit = getattr(task, "budget", None)
        if budget is None:
            budget = limit
        if budget is not None and (type(budget) is not int or budget < 0 or (limit is not None and budget > limit)):
            raise ValueError("Evaluation budget must fit the frozen task limit")
        self.task, self.budget = task, budget
        # A detailed-output version of task.measure still uses this cache and budget.
        self._measure = measure
        self.cache = {}
        self.backend_calls = 0
        self.validation_seconds = 0.0

    def evaluate(self, candidate):
        self.backend_seconds = 0.0
        start = time.perf_counter()
        try:
            try:
                candidate = self.task.normalize(candidate)
            except CandidateError:
                raise
            except ValueError as error:
                raise CandidateError("invalid_dsl", "$.dsl", str(error)) from error
            key = self.task.candidate_id(candidate)
            check = self.cache[key]["verification"] if key in self.cache else self.task.check(candidate)
            if check["status"] != "pass":
                raise CandidateError("verification_failed", "$.dsl", "Candidate correctness check failed",
                                     verification=check)
        finally:
            self.validation_seconds = time.perf_counter() - start
        if key in self.cache:
            return {**self.cache[key], "cached": True, "validation_seconds": self.validation_seconds}
        if self.budget is not None and self.backend_calls >= self.budget:
            raise ValueError("Performance evaluation budget exhausted")
        start = time.perf_counter()
        self.backend_calls += 1
        try:
            metrics = (self.task.measure if self._measure is None else self._measure)(candidate)
            self.task.objective.value(metrics)
        finally:
            self.backend_seconds = time.perf_counter() - start
        result = {"candidate_id": key, "dsl": candidate, "verification": check,
                  "metrics": metrics, "backend_seconds": self.backend_seconds,
                  "validation_seconds": self.validation_seconds,
                  "cached": False}
        self.cache[key] = result
        return result
