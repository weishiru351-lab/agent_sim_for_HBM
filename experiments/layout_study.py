"""Development diagnostic: static layouts, an analytic selector and complete enumeration.

No model calls or Agent advantage claim. The enumeration is a separate oracle,
not a budget-matched search competitor. Readable interpretation lives in docs/experiments.md.
"""

import argparse
from dataclasses import replace
import hashlib
from itertools import permutations
from pathlib import Path

from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.json_io import digest, save
from agent_design_search.spaces.kv_layout_dsl import AXES, Layout
from experiments.legacy_cases import KVLayoutTask, LayoutConfig
from experiments.benchmark import default_output


def legal_layouts(cfg):
    for token in range(1, cfg.tokens + 1):
        for dimension in range(1, cfg.dimensions + 1):
            if Layout(token, dimension).allocated_bytes(cfg) <= cfg.capacity_bytes:
                for order in permutations(AXES):
                    yield Layout(token, dimension, order).to_dict()


def analytic_choice(cfg):
    """For a full-line tile t*d=L/e: W=T*D/d, R=D*sum(ceil(prefix/t)).

    Requires tiles dividing the shape. This selector optimizes this structured
    family only; the separate oracle tests its gap to the entire legal DSL.
    """
    prefixes = [n for n in range(1, cfg.tokens + 1)
                if cfg.read_every and (n % cfg.read_every == 0 or n == cfg.tokens)]
    estimates = []
    elements = cfg.line_bytes // cfg.element_bytes
    for token in range(1, min(cfg.tokens, elements) + 1):
        dimension = elements // token
        if elements % token or cfg.tokens % token or cfg.dimensions % dimension:
            continue
        writes = cfg.tokens * (cfg.dimensions // dimension)
        reads = cfg.dimensions * sum((n + token - 1) // token for n in prefixes)
        estimates.append({"dsl": Layout(token, dimension).to_dict(),
                          "predicted_bytes": (writes + reads) * cfg.line_bytes})
    return min(estimates, key=lambda e: e["predicted_bytes"]), len(estimates)


def run_case(cfg):
    task, static = KVLayoutTask(cfg), {}
    evaluator = Evaluator(task)
    for name, layout in (("token_contiguous", Layout(1, cfg.dimensions)),
                         ("dimension_contiguous", Layout(cfg.tokens, 1)), ("tile_4x8", Layout(4, 8))):
        static[name] = evaluator.evaluate(layout.to_dict())
    choice, estimates = analytic_choice(cfg)
    analytic = evaluator.evaluate(choice["dsl"])
    if analytic["metrics"]["transferred_bytes"] != choice["predicted_bytes"]:
        raise AssertionError("Analytic transfer estimate disagrees with execution")
    oracle, expressions = Evaluator(task), 0
    for candidate in legal_layouts(cfg):
        oracle.evaluate(candidate)
        expressions += 1
    best = min(oracle.cache.values(), key=task.objective.rank)
    return {"task": task.definition, "static": static,
            "analytic": {"result": analytic, "selected_dsl": choice["dsl"],
                         "predicted_bytes": choice["predicted_bytes"], "formula_estimates": estimates},
            "static_and_analytic_backend_calls": evaluator.backend_calls,
            "oracle": {"legal_expressions": expressions, "backend_calls": oracle.backend_calls, "best": best,
                       "score_digest": digest(sorted((key, entry["metrics"]) for key, entry in oracle.cache.items()))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    out = args.output or default_output()
    out.mkdir(parents=True, exist_ok=False)
    cases = {}
    for label, every in (("write_only", 0), ("read_at_end", 16), ("read_each_append", 1)):
        cases[label] = run_case(replace(LayoutConfig(), read_every=every))
        case = cases[label]
        print(label, {key: value["metrics"]["transferred_bytes"] for key, value in case["static"].items()},
              "oracle", case["oracle"]["best"]["metrics"]["transferred_bytes"], flush=True)
    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / "agent_design_search").rglob("*.py")), Path(__file__).resolve()]
    save(out / "study.json", {"version": 1, "kind": "development_diagnostic", "is_agent_experiment": False,
                             "source_hashes": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                               for p in sources}, "cases": cases,
                             "limits": ["synthetic serial gather/scatter, no cache or timing",
                                        "no measured GPU/PIM/HBM performance", "oracle calls separate from selection",
                                        "static one-block layout; no migration or firmware"]})
    print(f"Evidence: {out / 'study.json'}")


if __name__ == "__main__":
    main()
