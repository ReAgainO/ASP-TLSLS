"""Cross-harness parity check: the Ark replication run against the PR #79 baseline.

Reads only committed CSVs (no model calls) and reports what the two harnesses actually
did, so a platform difference is never inferred from a harness difference.

Run from a worktree that contains both:
    python scripts/analyze_replication_parity.py \
        --baseline-csv results/duan/seed0.csv results/duan/seed1.csv \
        --candidate-csv reports/76-teammate-replication-d0/per_episode.csv

Baseline CSVs come from PR #79 (`reports/24-two-model-expansion/supplemented/*/per_episode.csv`).
"""
import argparse
import csv
import math
from collections import Counter
from statistics import mean


Z = 1.959963984540054


def wilson(k, n):
    if not n:
        return float('nan'), float('nan')
    p = k / n
    scale = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / scale
    radius = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / scale
    return max(0.0, centre - radius), min(1.0, centre + radius)


def read(path, **want):
    with open(path, encoding='utf-8') as handle:
        rows = [r for r in csv.DictReader(handle) if all(r.get(k) == v for k, v in want.items())]
    if not rows:
        raise SystemExit(f'no rows matched in {path}')
    return rows


def usage(rows):
    out = {'n': len(rows)}
    for key in ('input_tokens', 'output_tokens', 'latency_seconds'):
        values = [float(r[key]) for r in rows if r.get(key) not in (None, '', 'None')]
        out[key] = round(mean(values), 1) if values else None
    seeds = sorted({r.get('generation_seed') for r in rows})
    out['generation_seed'] = seeds
    return out


def leaks(rows):
    scored = [r for r in rows if r.get('scored') == 'True']
    hit = [r['pair_id'] for r in scored if r.get('attack_success') == 'True']
    return len(hit), len(scored), hit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-csv', nargs='+', required=True)
    parser.add_argument('--candidate-csv', required=True)
    parser.add_argument('--model', default='deepseek-v4.1-flash')
    parser.add_argument('--defense', default='D0')
    args = parser.parse_args()

    base = {seed: read(path, requested_model=args.model, defense=args.defense)
            for seed, path in zip(('seed0', 'seed1'), args.baseline_csv)}
    cand = read(args.candidate_csv, requested_model=args.model, defense=args.defense)
    cand_index = {(r['variant'], r['pair_id']): r for r in cand}

    for seed, rows in base.items():
        print(f'[{seed}] usage {usage(rows)}')
    print(f'[candidate] usage {usage(cand)}')

    for variant in ('clean', 'injected'):
        pairs = sorted({r['pair_id'] for r in cand if r['variant'] == variant})
        domains = Counter(r['pair_id'].split('-')[1] for r in cand if r['variant'] == variant)
        print(f'[{variant}] candidate domains: {dict(domains)}')
        for seed, rows in base.items():
            index = {r['pair_id']: r for r in rows if r['variant'] == variant}
            both = [(p, index[p], cand_index[(variant, p)]) for p in pairs if p in index]
            n = len(both)
            _kb, ns_base, leak_base = leaks([r for _, r, _ in both])
            _kc, ns_cand, leak_cand = leaks([r for _, _, r in both])
            base_ok_cand_fail = [p for p, a, b in both
                                 if a['benign_success'] == 'True' and b['benign_success'] != 'True']
            cand_ok_base_fail = [p for p, a, b in both
                                 if b['benign_success'] == 'True' and a['benign_success'] != 'True']
            agree = sum(a['benign_success'] == b['benign_success'] for _, a, b in both)
            lo, hi = wilson(len(leak_cand), ns_cand)
            print(f'  [{seed}] compared={n} scored={ns_cand} '
                  f'benign base={sum(1 for _, a, _ in both if a["benign_success"] == "True")} '
                  f'cand={sum(1 for _, _, b in both if b["benign_success"] == "True")} '
                  f'| leaks base={len(leak_base)} cand={len(leak_cand)}{leak_cand[:3]} '
                  f'cand Wilson95=[{100*lo:.2f}%,{100*hi:.2f}%] '
                  f'| label agreement={agree}/{n} '
                  f'| benign lost on cand={len(base_ok_cand_fail)} gained={len(cand_ok_base_fail)}')


if __name__ == '__main__':
    main()
