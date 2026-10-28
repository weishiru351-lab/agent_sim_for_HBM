"""Compare ordinary search methods on the four synthetic workloads."""

from datetime import datetime
from pathlib import Path
import os
from statistics import mean

from agent_design_search.spaces.dram_dsl import Policy
from agent_design_search.exploration.evaluation import Evaluator
from agent_design_search.exploration.dram_mapping_candidates import activity_policy, row_xor, random_policy, mutate
from agent_design_search.exploration.search_algorithms import random_local_search
from experiments.legacy_cases import simulate_mapping as simulate
from agent_design_search.json_io import save
from experiments.legacy_cases import AddressMappingTask, Config, WORKLOADS, workload


METHODS = ("random", "local", "activity_local", "packed_local")


def run_mapping_search(method, cfg, requests, budget=20, seed=0):
    """Compose the historical four-method comparison from shared algorithms/operators."""
    if method not in METHODS or budget < 2:
        raise ValueError("Unknown search method or budget below 2")
    if cfg.block_bits < 2 or cfg.max_operations < 1:
        raise ValueError("Search needs at least two bits and one allowed operation")
    task = AddressMappingTask(cfg, requests)
    seeds = [Policy(), row_xor(cfg, cfg.max_operations)]
    if method in {"activity_local", "packed_local"}:
        seeds.append(activity_policy(requests, cfg, pack_columns=method == "packed_local", max_operations=cfg.max_operations))
    result = random_local_search(task, "random" if method == "random" else "local",
                                 [p.to_dict() for p in seeds],
                                 lambda rng: random_policy(rng, cfg, cfg.max_operations).to_dict(),
                                 lambda candidate, rng: mutate(Policy.parse(candidate, cfg, cfg.max_operations), rng, cfg, cfg.max_operations).to_dict(),
                                 budget, seed)
    for entry in result["history"]:
        entry["best_cycles"] = entry.pop("best_value")
    return {**result, "method": method, "trace_id": task.trace_id, "config": cfg.to_dict()}


def default_output():
    return Path("results") / datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")


def benchmark(args, cfg):
    out = args.output or default_output()
    out.mkdir(parents=True, exist_ok=False)
    results, baselines = [], {}
    for name in WORKLOADS:
        requests = workload(name, cfg, args.count, 0)
        baseline_eval = Evaluator(AddressMappingTask(cfg, requests))
        baselines[name] = {label: baseline_eval.evaluate(policy.to_dict()) for label, policy in
                           (("identity", Policy()), ("row_xor", row_xor(cfg, cfg.max_operations)),
                            ("activity", activity_policy(requests, cfg, max_operations=cfg.max_operations)),
                            ("activity_packed", activity_policy(requests, cfg, pack_columns=True, max_operations=cfg.max_operations)))}
        for method in METHODS:
            for seed in args.seeds:
                result = run_mapping_search(method, cfg, requests, args.budget, seed)
                result["workload"] = name
                heldout = workload(name, cfg, args.count, 0, holdout=True)
                result["holdout"] = simulate(heldout, Policy.parse(result["best"]["dsl"], cfg), cfg)
                result["holdout_identity"] = simulate(heldout, Policy(), cfg)
                result["holdout_calls_not_in_search_budget"] = 2
                results.append(result)
        print(f"Completed {name}", flush=True)
    summary = {"version": 2, "config": cfg.to_dict(), "requests": args.count,
               "seeds": args.seeds, "budget": args.budget, "baselines": baselines, "runs": results,
               "limits": ["synthetic workloads", "simplified FCFS timing model", "no LLM in these baselines",
                          "holdout variants are synthetic, not real inference traces", "no HBM throughput claim",
                          "packed heuristic added after observing the first conversation-agent run"]}
    save(out / "benchmark.json", summary)
    lines = ["# Toy mapping benchmark", "", "Synthetic requests, toy cycles; **not measured HBM performance**.",
             "All methods below are ordinary algorithms, not LLM agents.", "",
             f"Budget: {args.budget} actual backend calls per search; seeds: {args.seeds}; requests per trace: {args.count}.",
             "The two shared initial candidates consume budget. Held-out evaluations occur only after selection.", "",
             "| Workload | Identity cycles | Row-XOR cycles | Activity cycles | Packed activity cycles |", "| --- | ---: | ---: | ---: | ---: |"]
    for name, entries in baselines.items():
        values = [entries[key]["metrics"]["completion_cycles"] for key in ("identity", "row_xor", "activity", "activity_packed")]
        lines.append(f"| {name} | " + " | ".join(str(v) for v in values) + " |")
    lines += ["", "| Workload | Search | Mean best cycles (min–max) | Train speedup vs identity | Holdout speedup vs identity |",
              "| --- | --- | ---: | ---: | ---: |"]
    for name in WORKLOADS:
        for method in METHODS:
            subset = [r for r in results if r["workload"] == name and r["method"] == method]
            cycles = [r["best"]["metrics"]["completion_cycles"] for r in subset]
            base = baselines[name]["identity"]["metrics"]["completion_cycles"]
            holdout = mean(r["holdout_identity"]["completion_cycles"] / r["holdout"]["completion_cycles"] for r in subset)
            lines.append(f"| {name} | {method} | {mean(cycles):.1f} ({min(cycles)}–{max(cycles)}) | {mean(base/c for c in cycles):.2f}x | {holdout:.2f}x |")
    lines += ["", "`activity_local` is a hand-written bit-transition seed plus local search, NOT an AI agent.",
              "`packed_local` also packs active bits into columns. It is a post-hoc diagnostic control added after the first conversation-agent run.",
              "All searchers use the same two baseline seeds; every additional heuristic evaluation is counted.",
              "Search seeds vary proposals; each method sees the same workload seed. Results do not establish statistical significance.",
              "", "benchmark.json stores all runs, including DSLs, metrics, trace hashes, verification, cache flags, and candidate histories.", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    # Optional figure export: the experiment itself works without matplotlib.
    try:
        os.environ.setdefault("MPLCONFIGDIR", str((out / "matplotlib_cache").resolve()))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 2, figsize=(11, 7))
        for ax, name in zip(axes.flat, WORKLOADS):
            for method in METHODS:
                subset = [r for r in results if r["workload"] == name and r["method"] == method]
                curves = []
                for r in subset:
                    curves.append([min(e["best_cycles"] for e in r["history"] if e["backend_calls"] <= call)
                                   for call in range(1, args.budget + 1)])
                avg = [mean(c[i] for c in curves) for i in range(args.budget)]
                ax.plot(range(1, args.budget + 1), avg, label=method)
            ax.set(title=name, xlabel="Backend evaluations", ylabel="Best toy cycles (lower is better)")
            ax.grid(alpha=.25)
            ax.legend(fontsize=8)
        fig.suptitle("Synthetic toy model: ordinary search baselines, not LLM results")
        fig.tight_layout()
        fig.savefig(out / "convergence.png", dpi=150)
        plt.close(fig)
        with (out / "report.md").open("a", encoding="utf-8") as handle:
            handle.write("\n![Search curves](convergence.png)\n")
    except ImportError:
        pass
    print(f"Report: {out / 'report.md'}")


if __name__ == "__main__":
    import argparse
    from agent_design_search.json_io import read
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--budget", type=int, default=20)
    parser.add_argument("--count", type=int, default=768)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    args = parser.parse_args()
    benchmark(args, Config(**read(args.config)) if args.config else Config())
