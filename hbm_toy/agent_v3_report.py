"""Replay the completed factorial pilot or plot post-search optimality gaps."""

import argparse
from pathlib import Path
from statistics import mean

from .agent_v3 import CONDITIONS, check
from .agent_v2_diagnosis import phase_feedback
from .experiment import read
from .model import Config, Policy, simulate
from .trace_bridge import load_trace


def runs(case):
    return [*case['agents'].values(), case['one_shot'], *case['baselines']]


def replay(folder):
    folder = Path(folder)
    protocol = check(folder)
    data = read(folder / 'comparison.json')
    if data['protocol'] != protocol:
        raise ValueError('Protocol mismatch')
    cfg = Config(**protocol['config'])
    calls = 0
    for task, case in data['cases'].items():
        requests = load_trace(folder / 'traces' / (task + '.json'), cfg)
        for run in runs(case):
            cache, run_calls = {}, 0
            for entry in run['history']:
                if entry['status'] != 'ok':
                    raise ValueError('Failed/invalid records require separate review')
                policy = Policy.parse(entry['dsl'], cfg)
                key = policy.masks(cfg)
                cached = key in cache
                if not cached:
                    metrics = simulate(requests, policy, cfg, include_events=True)
                    events = metrics.pop('events')
                    cache[key] = (metrics, phase_feedback(requests, events, cfg, protocol['split_at']))
                    run_calls += 1
                metrics, phases = cache[key]
                if metrics != entry['metrics'] or cached != entry['cached'] or run_calls != entry['backend_calls']:
                    raise ValueError(f'Metrics/cache/budget mismatch: {task}, {entry["proposal_number"]}')
                if 'phase_feedback' in entry and entry['phase_feedback'] != phases:
                    raise ValueError('Phase feedback mismatch')
            if run_calls != run['backend_calls']:
                raise ValueError('Final run count mismatch')
            calls += run_calls
        print(task, 'all 12 histories reproduced')
    if calls != data['search_backend_calls']:
        raise ValueError('Total backend count mismatch')
    print('Verification backend executions:', calls, '(separate from search and oracle)')


def curve(run, budget):
    return [min(e['metrics']['completion_cycles'] for e in run['history']
                if e['status'] == 'ok' and e['backend_calls'] <= n) for n in range(1, budget + 1)]


def render(folder):
    import os
    import tempfile
    folder = Path(folder)
    data = read(folder / 'comparison.json')
    with tempfile.TemporaryDirectory(prefix='hbm_v3_plot_') as tmp:
        os.environ.setdefault('MPLCONFIGDIR', tmp)
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, 3, figsize=(17, 5.8))
        colors = ('#777777', '#0072b2', '#e69f00', '#009e73')
        labels = ('Old prompt / summary', 'New prompt / summary', 'Old prompt / cohort feedback', 'New prompt / cohort feedback')
        budget = data['protocol']['budget']
        xs = list(range(1, budget + 1))
        for ax, (task, case) in zip(axes, data['cases'].items()):
            optimum = case['exact']['best_cycles']
            for condition, color, label in zip(CONDITIONS, colors, labels):
                ax.step(xs, [v - optimum for v in curve(case['agents'][condition], budget)], where='post',
                        color=color, label=label, linewidth=2)
            for method, color in [('local', '#cc79a7'), ('random', '#332288')]:
                ys = [[v - optimum for v in curve(r, budget)] for r in case['baselines'] if r['method'] == method]
                ax.step(xs, [mean(v) for v in zip(*ys)], where='post', color=color, linestyle='--', label=method + ' (3-seed mean)')
                ax.fill_between(xs, [min(v) for v in zip(*ys)], [max(v) for v in zip(*ys)], step='post', color=color, alpha=.09)
            ax.step(xs, [v - optimum for v in curve(case['one_shot'], budget)], where='post', color='#999933',
                    linestyle=':', label='Coordinator batch (supplementary)')
            ax.axhline(0, color='black', linewidth=.8)
            ax.set(title=task, xlabel='Allowed backend evaluations', ylabel='Extra toy cycles above post-search optimum')
            ax.set_xticks([1, 5, 10, 15, 20])
            ax.grid(alpha=.2)
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc='lower center', ncol=4, fontsize=9)
        fig.suptitle('Agent V3: prompt x feedback pilot (one independent model context per condition)')
        fig.text(.5, .13, 'Oracle revealed after ALL searches. Shading: algorithm seed range, not confidence intervals. Synthetic toy.', ha='center', fontsize=9)
        fig.tight_layout(rect=(0, .16, 1, .94))
        fig.savefig(folder / 'convergence.png', dpi=160)
        plt.close(fig)
    print(folder / 'convergence.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('replay', 'render'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    {'replay': replay, 'render': render}[args.action](args.folder)


if __name__ == '__main__':
    main()
