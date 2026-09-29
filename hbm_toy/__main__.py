"""Run with python -m hbm_toy --help. No third-party runtime dependency."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from statistics import mean

from .experiment import (Evaluator, activity_policy, read, row_xor, run_search, save,
                         session_finish, session_init, session_step)
from .model import Config, Policy, WORKLOADS, simulate, workload
from .rtl import export_rtl

METHODS = ("random", "local", "activity_local", "packed_local")


def default_output():
    return Path("results") / datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")


def benchmark(args, cfg):
    out = args.output or default_output()
    out.mkdir(parents=True, exist_ok=False)
    results, baselines = [], {}
    for name in WORKLOADS:
        requests = workload(name, cfg, args.count, 0)
        baseline_eval = Evaluator(cfg, requests)
        baselines[name] = {label: baseline_eval.evaluate(policy) for label, policy in
                           (("identity", Policy()), ("row_xor", row_xor(cfg)),
                            ("activity", activity_policy(requests, cfg)),
                            ("activity_packed", activity_policy(requests, cfg, pack_columns=True)))}
        for method in METHODS:
            for seed in args.seeds:
                result = run_search(method, cfg, requests, args.budget, seed)
                result["workload"] = name
                heldout = workload(name, cfg, args.count, 0, holdout=True)
                result["holdout"] = simulate(heldout, Policy.parse(result["best"]["dsl"], cfg), cfg)
                result["holdout_identity"] = simulate(heldout, Policy(), cfg)
                result["holdout_calls_not_in_search_budget"] = 2
                results.append(result)
                save(out / "runs" / f"{name}_{method}_{seed}.json", result)
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
              "", "Each run JSON contains the DSL, metrics, trace hash, verification, cache flags, and complete candidate history.", ""]
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON configuration, omitted fields use toy defaults")
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("benchmark", help="Compare manual, random, local, and bit-activity baselines")
    bench.add_argument("--output", type=Path)
    bench.add_argument("--budget", type=int, default=20)
    bench.add_argument("--count", type=int, default=768)
    bench.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    init = commands.add_parser("session-init", help="Start an external model file-bridge session")
    init.add_argument("folder", type=Path)
    init.add_argument("--workload", choices=WORKLOADS, default="kv_interleaved")
    init.add_argument("--budget", type=int, default=20)
    init.add_argument("--count", type=int, default=768)
    init.add_argument("--seed", type=int, default=0)
    step = commands.add_parser("session-step", help="Evaluate one model proposal and export feedback")
    step.add_argument("folder", type=Path)
    step.add_argument("--proposal", required=True, type=Path)
    finish = commands.add_parser("session-finish", help="Close search and evaluate held-out workload once")
    finish.add_argument("folder", type=Path)
    evaluate = commands.add_parser("evaluate", help="Evaluate a DSL file")
    evaluate.add_argument("policy", type=Path)
    evaluate.add_argument("--workload", choices=WORKLOADS, default="stride")
    evaluate.add_argument("--count", type=int, default=768)
    rtl = commands.add_parser("export-rtl", help="Generate RTL and run Icarus if available")
    rtl.add_argument("policy", type=Path)
    rtl.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        cfg = Config(**read(args.config)) if args.config else Config()
        if args.command == "benchmark":
            benchmark(args, cfg)
            return
        if args.command == "session-init":
            result = session_init(args.folder, cfg, args.workload, args.count, args.seed, args.budget)
            print(f"Feedback: {args.folder / 'feedback.json'}; backend calls: {result['backend_calls']}")
            return
        if args.command == "session-step":
            result = session_step(args.folder, args.proposal)
        elif args.command == "session-finish":
            result = session_finish(args.folder)
            print(json.dumps({"best_cycles": result["best"]["metrics"]["completion_cycles"],
                              "holdout_cycles": result["holdout"]["completion_cycles"],
                              "backend_calls": result["backend_calls"]}, indent=2))
            return
        elif args.command == "evaluate":
            result = Evaluator(cfg, workload(args.workload, cfg, args.count)).evaluate(Policy.parse(read(args.policy), cfg))
        else:
            result = export_rtl(Policy.parse(read(args.policy), cfg), cfg, args.output)
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
