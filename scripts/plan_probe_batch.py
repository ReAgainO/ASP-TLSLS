"""Project how big a live batch has to be, then freeze a stratified subset for it.

Two questions, both answered from the already-published #24 outcome rates rather
than from any new model call:

1. Detection power. At the observed canary-exfiltration rates, how many injected
   pairs per cell are needed before a defence-vs-baseline comparison has a decent
   chance of reaching significance, and before a cell is likely to contain at least
   one leak case a report can cite? #24 could not cite a single D1 case, so the
   "at least one observable event" quantity matters as much as the p-value.

2. Subset choice. Pick a live batch out of tasks/probe-v1 that oversamples the
   boundary where leaks actually happened, keeps every clean/injected pair, and
   covers the phrasing factor.

Rate priors come from reports/24-two-model-expansion/supplemented/*/per_episode.csv
as read on 2026-11: pooled over both models and both seeds, D0 leaked 4/120,
D2 1/120, D3 1/120, D1 and D4 0/120, and 5 of those 6 leaks sat on the
read_scope boundary (D0 read_scope 4/48 pooled).
"""
import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import yaml

ALPHA = 0.05
REPS = 20000

# pooled observed rates per defence (injected episodes scored, both models, both seeds)
RATES = {'D0': 4 / 120, 'D1': 0.0 / 120, 'D2': 1 / 120, 'D3': 1 / 120, 'D4': 0.0 / 120}
READ_SCOPE_D0 = 4 / 48


def binom_ge(n, k, p):
    """P(X >= k) for X ~ Binomial(n, p), exact."""
    if k <= 0:
        return 1.0
    total = 0.0
    for x in range(k, n + 1):
        total += math.comb(n, x) * p ** x * (1 - p) ** (n - x)
    return total


def mcnemar_test(b, c):
    """Exact two-sided McNemar on discordant counts b (baseline only) and c (defence only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p_two = sum(math.comb(n, x) * 0.5 ** n for x in range(0, k + 1))
    return min(1.0, 2 * p_two)


def power_table(p_base, p_def, sizes, reps=REPS, rng=None):
    rng = rng or random.Random(20261104)
    rows = []
    for n in sizes:
        leaks = at_least_one = 0
        for _ in range(reps):
            b = sum(rng.random() < p_base and rng.random() >= p_def for _ in range(n))
            c = sum(rng.random() < p_def and rng.random() >= p_base for _ in range(n))
            if mcnemar_test(b, c) <= ALPHA:
                leaks += 1
            at_least_one += 1 if (b or c) else 0
        rows.append({'pairs_per_cell': n,
                     'power_exact_mcnemar': round(leaks / reps, 4),
                     'p_at_least_one_discordant': round(at_least_one / reps, 4),
                     'p_leak_in_baseline_cell': round(binom_ge(n, 1, p_base), 4)})
    return rows


def detectable(n, p_base, p_def):
    """Analytic expected discordants, for the write-up."""
    return round(n * p_base * (1 - p_def), 3), round(n * p_def * (1 - p_base), 3)


def select_subset(corpus: Path, per_cell: int, out: Path, tier: str = 'custom'):
    index = json.loads((corpus / 'corpus-index.json').read_text(encoding='utf-8'))
    groups = defaultdict(list)
    for pair in index['pairs']:
        groups[(pair['boundary'], pair['phrasing'])].append(pair['pair_id'])
    selected = []
    for key in sorted(groups):
        chosen = sorted(groups[key])[:per_cell]
        for pair_id in chosen:
            selected.append({'pair_id': pair_id, 'boundary': key[0], 'phrasing': key[1]})
    task_files = []
    for row in sorted(selected, key=lambda r: r['pair_id']):
        for variant in ('clean', 'injected'):
            path = corpus / variant / (row['pair_id'] + '.yaml')
            task_files.append({'file': str(path.relative_to(corpus.parent.parent)).replace('\\', '/'),
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'variant': variant, 'pair_id': row['pair_id'],
                               'boundary': row['boundary'], 'phrasing': row['phrasing']})
    blob = json.dumps(task_files, sort_keys=True).encode()
    plan = {'tier': tier, 'corpus': corpus.name, 'pairs': len(selected), 'variants': len(task_files),
            'pairs_per_boundary_x_phrasing_cell': per_cell,
            'selection_rule': 'lexicographically first pair_ids inside each (boundary, phrasing) cell',
            'subset_sha256': hashlib.sha256(blob).hexdigest(),
            'defences': ['D0', 'D1', 'D2', 'D3', 'D4'], 'seeds': [0, 1],
            'episodes_per_model': len(selected) * 2 * 5 * 2,
            'tasks': task_files}
    out.mkdir(parents=True, exist_ok=True)
    (out / 'probe-subset-plan.json').write_text(json.dumps(plan, indent=2) + '\n',
                                                encoding='utf-8', newline='\n')
    compact = {'tier': tier, 'corpus': corpus.name, 'subset_sha256': plan['subset_sha256'],
               'selection_rule': plan['selection_rule'], 'pairs': len(selected),
               'pairs_per_boundary_x_phrasing_cell': per_cell,
               'episodes_per_model': plan['episodes_per_model'],
               'pair_ids': sorted({r['pair_id'] for r in selected})}
    (corpus / f'subset-{tier}.json').write_text(json.dumps(compact, indent=2) + '\n',
                                                encoding='utf-8', newline='\n')
    return plan


def write_configs(plan: dict, out_dir: Path, tier: str):
    """Emit draft live configs for a frozen subset. Live stays disabled and the cost
    ceiling stays unset until calibration measures it; #76 forbids invented unit prices."""
    out_dir.mkdir(parents=True, exist_ok=True)
    models = [('qwen', 'qwen3.8-flash'), ('deepseek', 'deepseek-v4.1-flash')]
    paths = []
    for name, model_id in models:
        for seed in (0, 1):
            cfg = {
                'schema_version': 1,
                'live_enabled': False,
                'endpoint_format_confirmed': False,
                'cost_bounds_confirmed': False,
                'task_root': 'tasks/probe-v1',
                'task_pairs': sorted(r['pair_id'] for r in plan['tasks'] if r['variant'] == 'clean'),
                'models': [{'name': name, 'model': model_id, 'enable_thinking': False,
                            'temperature': 0.0, 'max_output_tokens': 512,
                            'request_cost_ceiling': None, 'seed': seed}],
                'defenses': plan['defences'],
                'repetitions': 1,
                'schedule_seed': seed,
                'max_steps': 10,
                'parse_retries': 2,
                'confirm_high_risk': True,
                'budget': {'currency': 'CNY', 'max_cost': 0.0, 'max_requests': 0},
            }
            path = out_dir / f'76-{tier}-{name}-seed{seed}.yaml'
            path.write_text('# Draft for issue #76 probe batch. Fill request_cost_ceiling and '
                            'budget from\n# the calibration run before setting live_enabled: true. '
                            'Runner is POSIX-only (fcntl).\n'
                            + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                            encoding='utf-8', newline='\n')
            paths.append(str(path))
    return paths


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=Path('tasks/probe-v1'))
    parser.add_argument('--out', type=Path, default=Path('results/probe-plan-01'))
    parser.add_argument('--per-cell', type=int, default=4)
    parser.add_argument('--tier', default='custom')
    parser.add_argument('--write-configs', action='store_true')
    parser.add_argument('--reps', type=int, default=REPS)
    args = parser.parse_args()

    sizes = [12, 15, 24, 60, 120, 240, 480]
    print(json.dumps({
        'assumptions': {'alpha_two_sided': ALPHA, 'monte_carlo_reps': args.reps,
                        'rate_priors_from_24': RATES, 'read_scope_d0_pooled': READ_SCOPE_D0,
                        'pairing': 'independent Bernoulli draws per arm (conservative: real '
                                   'pairs are positively correlated, which lowers b+c)'},
        'D0_vs_D4_at_observed_zero': power_table(RATES['D0'], 0.0, sizes, args.reps),
        'D0_vs_D4_on_read_scope_only': power_table(READ_SCOPE_D0, 0.0, sizes, args.reps),
        'D0_vs_D2_at_observed': power_table(RATES['D0'], RATES['D2'], sizes, args.reps),
        'analytic_expected_discordants_D0_vs_D4': {str(n): detectable(n, RATES['D0'], 0.0) for n in sizes},
    }, indent=2))
    plan = select_subset(args.corpus, args.per_cell, args.out, args.tier)
    print(f"subset: {plan['pairs']} pairs / {plan['variants']} variants / "
          f"{plan['episodes_per_model']} episodes per model / sha256 {plan['subset_sha256']}")
    if args.write_configs:
        for path in write_configs(plan, Path('experiments/configs'), args.tier):
            print(f'wrote draft config {path}')
