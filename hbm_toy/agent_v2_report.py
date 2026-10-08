"""Render archived Agent V2 curves or replay every recorded backend evaluation."""

import argparse
from pathlib import Path
from statistics import mean
import tempfile

from .experiment import Evaluator, read
from .model import Config, Policy
from .trace_bridge import load_trace


def runs(case):
    return [case["agent"], case["one_shot"], *case["baselines"]]


def curve(run, budget):
    return [min(e["metrics"]["completion_cycles"] for e in run["history"]
                if "metrics" in e and e["backend_calls"] <= call) for call in range(1, budget + 1)]


def replay(folder):
    data = read(folder / "comparison.json")
    cfg = Config(**data["protocol"]["config"])
    calls = 0
    for name, case in data["cases"].items():
        requests = load_trace(folder / "traces" / (name + ".json"), cfg)
        for run in runs(case):
            evaluator = Evaluator(cfg, requests)
            if evaluator.trace_id != run["trace_id"]:
                raise ValueError("Trace identity mismatch")
            for entry in run["history"]:
                if entry["status"] != "ok":
                    raise ValueError("This replay supports successful/cached histories; inspect failures separately")
                actual = evaluator.evaluate(Policy.parse(entry["dsl"], cfg))
                if actual["metrics"] != entry["metrics"] or actual["cached"] != entry["cached"]:
                    raise ValueError(f"Metrics/cache mismatch: {name}, proposal {entry['proposal_number']}")
                if evaluator.backend_calls != entry["backend_calls"]:
                    raise ValueError("Per-step budget accounting mismatch")
            if evaluator.backend_calls != run["backend_calls"]:
                raise ValueError("Final call count mismatch")
            calls += evaluator.backend_calls
        print(name, "all nine run histories reproduced", flush=True)
    if calls != data["search_backend_calls"]:
        raise ValueError("Aggregate call count mismatch")
    print(f"Replayed {calls} backend calls. These verification executions are separate from search/oracle costs.")


def render(folder):
    data = read(folder / "comparison.json")
    import os
    with tempfile.TemporaryDirectory(prefix="hbm_v2_plot_") as cache:
        os.environ.setdefault("MPLCONFIGDIR", cache)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 3, figsize=(15, 8))
        for ax, (name, case) in zip(axes.flat, data["cases"].items()):
            budget = data["protocol"]["budget"]
            x = list(range(1, budget + 1))
            for key, label, color in [("agent", "Feedback Agent (one run)", "#2166ac"),
                                      ("one_shot", "One-shot model (one batch)", "#d6604d")]:
                ax.step(x, curve(case[key], budget), where="post", label=label, color=color, linewidth=2)
            for method, color in [("local", "#1b9e77"), ("random", "#7570b3")]:
                ys = [curve(r, budget) for r in case["baselines"] if r["method"] == method]
                ax.step(x, [mean(v) for v in zip(*ys)], where="post", label=method + " (3-seed mean)", color=color)
                ax.fill_between(x, [min(v) for v in zip(*ys)], [max(v) for v in zip(*ys)],
                                step="post", alpha=.12, color=color)
            heuristic = next(r for r in case["baselines"] if r["method"] == "heuristics")
            ax.step(x, curve(heuristic, budget), where="post", label="Frozen heuristics", color="#777777", linestyle=":")
            ax.axhline(case["exact"]["best_cycles"], color="black", linestyle="--", label="720-permutation oracle")
            ax.set(title=name, xlabel="Allowed backend evaluations", ylabel="Best toy cycles (lower is better)")
            ax.set_xticks([1, 5, 10, 15, 20]); ax.grid(alpha=.2)
        axes.flat[-1].axis("off")
        handles, labels = axes.flat[0].get_legend_handles_labels()
        axes.flat[-1].legend(handles, labels, loc="center", fontsize=10)
        axes.flat[-1].text(.03, .1, "Stopped runs carry their incumbent forward.\nShading = min/max over 3 algorithm seeds,\nnot a confidence interval.\nSynthetic toy; not measured HBM performance.",
                           transform=axes.flat[-1].transAxes, fontsize=10)
        fig.suptitle("Agent V2 pilot: fixed traces, equal maximum budget, oracle revealed after search")
        fig.tight_layout()
        fig.savefig(folder / "convergence.png", dpi=160)
        plt.close(fig)
    print(folder / "convergence.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "replay"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    {"render": render, "replay": replay}[args.action](args.folder)


if __name__ == "__main__":
    main()
