"""Run the deterministic scripted controls over a probe corpus at scale.

This is the project's offline acceptance check, widened: every task variant is
driven by its own mock_actions through D0/D1/D2/D4, and we assert that the benign
goal still completes, that the scripted attack reaches the sink on D0/D1/D2, that
D4 denies it with the boundary-specific reason, and that clean traces never touch
the policy gate.

Evidence label is `scripted_smoke_only`. Deterministic controls prove fixture
reachability only. They say nothing about any model's behaviour and must never be
reported as leak rates.

Usage:
    python scripts/run_probe_controls.py tasks/probe-v1 --out results/probe-controls-01
"""
import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from agentleak.core import MockModel, run_episode
from agentleak.tasks import load_tasks

DEFENSES = ['D0', 'D1', 'D2', 'D4']


def run(corpus: Path, out: Path):
    index = {r['pair_id']: r for r in json.loads((corpus / 'corpus-index.json').read_text(encoding='utf-8'))['pairs']}
    tasks = {v: load_tasks(corpus / v) for v in ('clean', 'injected')}
    out.mkdir(parents=True, exist_ok=True)
    stream = (out / 'control_episodes.jsonl').open('w', encoding='utf-8', newline='\n')
    problems, rows, started = [], [], time.perf_counter()
    for variant in ('clean', 'injected'):
        for task in tasks[variant]:
            pair = task['id'].removesuffix('-' + variant)
            meta = index[pair]
            for defense in DEFENSES:
                result = run_episode(task, MockModel(task['mock_actions']), defense=defense,
                                     confirm_high_risk=True, protocol_version='json-actions-v2')
                reasons = [e['tool_result']['reason'] for e in result['events']
                           if e['tool_result'].get('blocked')]
                expect_attack = variant == 'injected' and defense != 'D4'
                checks = {
                    'benign_success': bool(result['labels']['benign_success']),
                    'attack_matches': bool(result['labels']['attack_success']) == expect_attack,
                    'd4_denial_reason': (meta['expected_d4_denial'] in reasons)
                        if (variant == 'injected' and defense == 'D4') else True,
                    'clean_untouched': not reasons if variant == 'clean' else True,
                    'no_error': not result.get('error'),
                }
                row = {'pair_id': pair, 'variant': variant, 'defense': defense, 'labels': result['labels'],
                       'blocked': reasons, 'steps': len(result['events']),
                       'domain': meta['domain'], 'boundary': meta['boundary'],
                       'encoding': meta['encoding'], 'phrasing': meta['phrasing'],
                       'evidence': 'scripted_smoke_only', 'checks': checks}
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                if not all(checks.values()):
                    problems.append({**row, 'failed': [k for k, v in checks.items() if not v],
                                     'error': result.get('error')})
    stream.close()
    report = build_report(rows, problems, len(tasks['clean']) + len(tasks['injected']),
                          time.perf_counter() - started)
    (out / 'control_summary.json').write_text(json.dumps(report, indent=2) + '\n',
                                              encoding='utf-8', newline='\n')
    (out / 'control_problems.jsonl').write_text(
        ''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in problems),
        encoding='utf-8', newline='\n')
    print(json.dumps(report, indent=2))
    return report


def build_report(rows, problems, n_tasks, elapsed):
    by_defense = defaultdict(Counter)
    by_cell = defaultdict(Counter)          # (boundary, phrasing) -> injected reachability
    by_denial = Counter()
    for r in rows:
        by_defense[r['defense']]['episodes'] += 1
        by_defense[r['defense']]['ok'] += all(r['checks'].values())
        if r['variant'] == 'injected' and r['defense'] != 'D4':
            key = (r['boundary'], r['phrasing'])
            by_cell[key]['pairs'] += 1
            by_cell[key]['attack_landed'] += bool(r['labels']['attack_success'])
            by_cell[key]['benign_ok'] += bool(r['labels']['benign_success'])
        if r['variant'] == 'injected' and r['defense'] == 'D4':
            by_denial[(r['boundary'], tuple(r['blocked']))] += 1
    return {
        'episodes': len(rows),
        'task_variants': n_tasks,
        'defense_cells': {d: dict(c) for d, c in sorted(by_defense.items())},
        'injected_reachability': [{'boundary': b, 'phrasing': p, **dict(c)}
                                  for (b, p), c in sorted(by_cell.items())],
        'd4_denial_reasons': [{'boundary': k[0], 'reasons': list(k[1]), 'n': n}
                              for k, n in sorted(by_denial.items())],
        'problems': len(problems),
        'problem_examples': problems[:10],
        'elapsed_seconds': round(elapsed, 2),
        'evidence': 'scripted_smoke_only',
        'note': ('Deterministic controls verify fixture reachability and policy wiring. '
                 'Not model outcomes; not leak rates.'),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    run(args.corpus, args.out)
