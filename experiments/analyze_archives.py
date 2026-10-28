"""Current-code regression and optional plots; historical replay uses tools/replay_history.py."""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from statistics import mean
import tempfile

from agent_design_search.spaces.dram_dsl import Policy, verify_mapping
from experiments.legacy_cases import simulate_mapping as simulate
from agent_design_search.json_io import read, digest
from experiments.legacy_cases import Config
from agent_design_search.inputs.workload import load_trace


def event_summary(events, cfg):
    """Describe a request cohort in its original continuous execution.

    Drain timestamps and mean latencies of cohorts are NOT additive phase costs.
    Queue waits partition latency together with issue-to-finish service time.
    """
    n = len(events)
    if not n:
        raise ValueError("Cannot summarize an empty cohort")
    states = Counter(e["row_state"] for e in events)
    channel_counts = [0] * (1 << cfg.channel_bits)
    bank_counts = [[0] * (1 << cfg.bank_bits) for _ in channel_counts]
    for e in events:
        channel_counts[e["channel"]] += 1
        bank_counts[e["channel"]][e["bank"]] += 1
    return {
        "requests": n,
        "first_arrival": min(e["arrival"] for e in events),
        "last_arrival": max(e["arrival"] for e in events),
        "last_finish": max(e["finish"] for e in events),
        "avg_latency": sum(e["finish"] - e["arrival"] for e in events) / n,
        "avg_backpressure_wait": sum(e["admitted"] - e["arrival"] for e in events) / n,
        "avg_controller_wait": sum(e["issue"] - e["admitted"] for e in events) / n,
        "avg_issue_to_finish": sum(e["finish"] - e["issue"] for e in events) / n,
        "row_hit_rate": states["row_hits"] / n,
        "row_conflict_rate": states["row_conflicts"] / n,
        "channel_requests": channel_counts,
        "bank_requests": bank_counts,
    }


def phase_feedback(requests, events, cfg, split_at):
    """Split by input request cohort, not by completion time or reset state."""
    if type(split_at) is not int or not 0 < split_at < len(requests):
        raise ValueError("split_at must lie inside the ordered request trace")
    ordered = sorted(requests, key=lambda r: (r.arrival, r.request_id))
    ids = {r.request_id for r in ordered}
    event_ids = [e["request_id"] for e in events]
    if len(ids) != len(ordered) or len(event_ids) != len(set(event_ids)) or set(event_ids) != ids:
        raise ValueError("Events must cover each request exactly once")
    first_ids = {r.request_id for r in ordered[:split_at]}
    boundary = ordered[split_at].arrival
    first = [e for e in events if e["request_id"] in first_ids]
    second = [e for e in events if e["request_id"] not in first_ids]
    # Strictly before boundary: same-cycle arrivals are not yet counted here.
    outstanding = sum(e["arrival"] < boundary and e["finish"] > boundary for e in first)
    return {
        "split_at_request_index": split_at,
        "second_cohort_first_arrival": boundary,
        "first_cohort_outstanding_at_boundary": outstanding,
        "first_cohort": event_summary(first, cfg),
        "second_cohort": event_summary(second, cfg),
        "semantics": "Cohorts of one continuous run; drain timestamps are not additive phase times. "
                     "No reset at the boundary. Outstanding counts arrivals strictly before boundary.",
    }


def runs(case):
    return [*(case['agents'].values() if 'agents' in case else [case['agent']]),
            case['one_shot'], *case['baselines']]


def curve(run, budget):
    return [min(e['metrics']['completion_cycles'] for e in run['history']
                if 'metrics' in e and e['backend_calls'] <= n) for n in range(1, budget + 1)]


def regression(folder):
    """Re-evaluate saved candidates, without rewriting scores or source commitments.

    This checks refactored behavior, not new model decisions or historical source
    identity. Backend wall time and timestamps intentionally are not compared.
    Invalid/failed histories need the dedicated session tests, not this replay.
    """
    folder = Path(folder)
    protocol = read(folder / 'protocol.json')
    factorial = protocol['version'] == 3
    if factorial:
        def commitment(value):
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            return hashlib.sha256(canonical.encode()).hexdigest()
        expected = protocol['protocol_commitment']
        if commitment({k: v for k, v in protocol.items() if k != 'protocol_commitment'}) != expected:
            raise ValueError('Frozen protocol changed')
        commitments = {**protocol['input_commitments'], **{
            'one_shot/' + name + '.json': sha for name, sha in protocol['one_shot_commitments'].items()}}
        for path, sha in commitments.items():
            if commitment(read(folder / path)) != sha:
                raise ValueError('Frozen input/briefing/proposals changed')
    data = read(folder / 'comparison.json')
    if data['protocol'] != protocol:
        raise ValueError('Protocol mismatch')
    cfg, calls = Config(**protocol['config']), 0
    for task, case in data['cases'].items():
        requests = load_trace(folder / 'traces' / (task + '.json'), cfg)
        trace_id = digest([asdict(r) for r in requests])
        if trace_id != protocol['trace_ids'][task]:
            raise ValueError('Trace mismatch')
        for run in runs(case):
            cache, run_calls = {}, 0
            for entry in run['history']:
                if entry['status'] != 'ok':
                    raise ValueError('Failed/invalid histories need separate review')
                policy = Policy.parse(entry['dsl'], cfg)
                masks = policy.masks(cfg)
                cached = masks in cache
                if not cached:
                    metrics = simulate(requests, policy, cfg, include_events=True)
                    events = metrics.pop('events')
                    cache[masks] = (metrics, events)
                    run_calls += 1
                metrics, events = cache[masks]
                identity = {'masks': masks, 'config': cfg.to_dict(), 'trace': trace_id}
                if run.get('protocol') != 'hbm-factorial-v3':
                    identity['backend'] = 'toy-fcfs-v1'
                if entry['candidate_id'] != digest(identity):
                    raise ValueError('Candidate identity mismatch')
                if metrics != entry['metrics'] or cached != entry['cached'] or run_calls != entry['backend_calls']:
                    raise ValueError(f'Metrics/cache/budget mismatch: {task}, {entry["proposal_number"]}')
                if entry['verification'] != verify_mapping(policy, cfg):
                    raise ValueError('Mapping verification mismatch')
                if 'phase_feedback' in entry and entry['phase_feedback'] != phase_feedback(requests, events, cfg, protocol['split_at']):
                    raise ValueError('Phase feedback mismatch')
            if run_calls != run['backend_calls']:
                raise ValueError('Final budget mismatch')
            calls += run_calls
        print(task, 'saved histories match current implementation', flush=True)
    if calls != data['search_backend_calls']:
        raise ValueError('Aggregate budget mismatch')
    print('Current-code regression executions:', calls, '(not new search evidence)')
    return calls


def render(folder):
    """Plot both studies with shared curve/statistical conventions."""
    import os
    data = read(folder / 'comparison.json')
    factorial = data['protocol']['version'] == 3
    with tempfile.TemporaryDirectory(prefix='hbm_plot_') as tmp:
        os.environ.setdefault('MPLCONFIGDIR', tmp)
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1 if factorial else 2, 3,
                                 figsize=(17, 5.8) if factorial else (15, 8))
        budget = data['protocol']['budget']
        xs = list(range(1, budget + 1))
        colors = ('#777777', '#0072b2', '#e69f00', '#009e73')
        labels = ('Old prompt / summary', 'New prompt / summary',
                  'Old prompt / cohort feedback', 'New prompt / cohort feedback')
        for ax, (task, case) in zip(axes.flat, data['cases'].items()):
            optimum = case['exact']['best_cycles']
            def values(run):
                return [v - (optimum if factorial else 0) for v in curve(run, budget)]
            if factorial:
                for condition, color, label in zip(data['protocol']['conditions'], colors, labels):
                    ax.step(xs, values(case['agents'][condition]), where='post', color=color, label=label, linewidth=2)
            else:
                ax.step(xs, values(case['agent']), where='post', label='Feedback Agent (one run)', color='#2166ac', linewidth=2)
            ax.step(xs, values(case['one_shot']), where='post', color='#999933', linestyle=':',
                    label='Coordinator batch (supplementary)' if factorial else 'One-shot model (one batch)')
            for method, color in [('local', '#1b9e77'), ('random', '#7570b3')]:
                ys = [values(r) for r in case['baselines'] if r['method'] == method]
                ax.step(xs, [mean(v) for v in zip(*ys)], where='post', label=method + ' (3-seed mean)', color=color)
                ax.fill_between(xs, [min(v) for v in zip(*ys)], [max(v) for v in zip(*ys)], step='post', color=color, alpha=.1)
            heuristic = next(r for r in case['baselines'] if r['method'] == 'heuristics')
            ax.step(xs, values(heuristic), where='post', label='Frozen heuristics', color='#555555', linestyle=':')
            ax.axhline(0 if factorial else optimum, color='black', linestyle='--', label='720-permutation oracle')
            ax.set(title=task, xlabel='Allowed backend evaluations',
                   ylabel='Extra toy cycles above optimum' if factorial else 'Best toy cycles')
            ax.set_xticks([1, 5, 10, 15, 20]); ax.grid(alpha=.2)
        handles, labels = axes.flat[0].get_legend_handles_labels()
        if factorial:
            fig.legend(handles, labels, loc='lower center', ncol=4, fontsize=8)
            fig.tight_layout(rect=(0, .2, 1, .94))
        else:
            axes.flat[-1].axis('off')
            axes.flat[-1].legend(handles, labels, loc='center', fontsize=9)
            fig.tight_layout(rect=(0, .05, 1, .94))
        fig.suptitle('Prompt x feedback pilot' if factorial else 'Fixed-trace feedback search pilot')
        fig.text(.5, .16 if factorial else .01,
                 'Synthetic toy. Oracle revealed after searches. Seed range is not a confidence interval.',
                 ha='center', fontsize=9)
        fig.savefig(folder / 'convergence.png', dpi=160)
        plt.close(fig)
    print(folder / 'convergence.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('regression', 'render'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    if args.action == 'regression':
        regression(args.folder)
    else:
        render(args.folder)


if __name__ == '__main__':
    main()
