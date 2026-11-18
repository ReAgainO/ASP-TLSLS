"""Build the supplemented screen dataset without touching the original attempts.

Modelled on scripts/overlay_supplement_24.py, with two deliberate differences that are
recorded in the output provenance rather than hidden:

1. The supplementary runs select a subset of pairs/defences and carry their own budget,
   so the full config cannot be byte-equal to the original. Instead of comparing the whole
   config, this script pins the fields that decide scoring - source_hash, task_hashes for
   the merged cells, protocol/transport versions, endpoint - plus the per-model sampling
   parameters. Selection and guard fields are allowed to differ and are listed.
2. The supplementary configs name the model "<lane>-supp" to keep run directories distinct,
   so model_name is normalised back to the lane name before merging. The relabel is written
   into the derivation record.

Guarantees kept identical to the #24 tool: a scored outcome is never replaced, the
supplement must itself be scored, identity fields must match, and originals stay untouched.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentleak.runner import read_jsonl, SCORED_STATUSES, digest
from agentleak.summarize import summarize_run

ROOT = Path(__file__).resolve().parents[1]
FILES = ['manifest.json', 'episodes.jsonl', 'events.jsonl']
PINNED = ['source_hash', 'protocol_version', 'transport_version', 'endpoints']
SAMPLING = ['model', 'temperature', 'max_output_tokens', 'seed', 'enable_thinking',
            'timeout_seconds', 'request_cost_ceiling']
ALLOWED_TO_DIFFER = ['task_pairs', 'defenses', 'budget', 'schedule_seed', 'models[].name']


def hashes(root):
    return {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in FILES}


def compatible(original_manifest, supp_manifest):
    for field in PINNED:
        if field == 'endpoints':
            # endpoints are keyed by model name, and the supplementary configs deliberately
            # suffix the name to keep run directories apart, so compare the URLs themselves.
            if set(supp_manifest['endpoints'].values()) != set(original_manifest['endpoints'].values()):
                raise ValueError('Supplement endpoints differ: '
                                 f'{supp_manifest["endpoints"]} vs {original_manifest["endpoints"]}')
            continue
        if supp_manifest.get(field) != original_manifest.get(field):
            raise ValueError(f'Supplement mismatch on {field}')
    orig_models = {m['name'].removesuffix('-supp'): m for m in original_manifest['config']['models']}
    for task_id, digest_value in supp_manifest['task_hashes'].items():
        if original_manifest['task_hashes'].get(task_id) != digest_value:
            raise ValueError(f'Supplement task bytes differ for {task_id}')
    for m in supp_manifest['config']['models']:
        base = orig_models.get(m['name'].removesuffix('-supp'))
        if base is None:
            raise ValueError('Supplement model has no counterpart in the original lane')
        for field in SAMPLING:
            if m.get(field) != base.get(field):
                raise ValueError(f'Supplement sampling parameter {field} differs; not comparable')


def overlay_rows(original, retries, wanted):
    by_id = {r['episode_id']: r for r in original}
    if len(by_id) != len(original):
        raise ValueError('Duplicate original episodes')
    seen = set()
    mapping = {}
    result = dict(by_id)
    replaced = []
    for row in retries:
        key = (row['pair_id'], row['variant'], row['defense'])
        if key not in wanted or key in seen:
            continue  # cross-product extras are recorded in the run but never merged
        seen.add(key)
        old = next(r for r in by_id.values() if (r['pair_id'], r['variant'], r['defense']) == key)
        if old['status'] in SCORED_STATUSES:
            raise ValueError(f'Refusing to replace scored outcome {key}: {old["status"]}')
        if row['status'] not in SCORED_STATUSES:
            continue  # still unscored after the supplement: keep the original row visible
        merged = dict(row)
        merged['model_name'] = old['model_name']
        merged['episode_id'] = old['episode_id']
        merged['supplemented_from'] = row['episode_id']
        merged['original_status'] = old['status']
        result[old['episode_id']] = merged
        replaced.append({'pair_id': key[0], 'variant': key[1], 'defense': key[2],
                         'original_status': old['status'],
                         'original_error': (old.get('result') or {}).get('error') or old.get('error'),
                         'supplement_episode_id': row['episode_id'],
                         'supplement_status': row['status']})
        mapping[row['episode_id']] = old['episode_id']
    if len(seen) != len(wanted):
        missing = wanted - {(p['pair_id'], p['variant'], p['defense']) for p in replaced}
        print(f'  note: {len(missing)} targeted cell(s) not resolved by the supplement: {sorted(missing)}')
    return list(result.values()), replaced, mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lanes', nargs='+', required=True)
    parser.add_argument('--targets', default='results/76/supplement-targets.json')
    parser.add_argument('--out', default='results/76/supplemented-screen-01')
    args = parser.parse_args()

    targets = json.loads((ROOT / args.targets).read_text(encoding='utf-8'))
    out_root = ROOT / args.out
    out_root.mkdir(parents=True, exist_ok=True)
    for lane in args.lanes:
        source = ROOT / f'results/76/screen-{lane}'
        supp = ROOT / f'results/76/supplement-{lane}'
        wanted = {(t['pair_id'], t['variant'], t['defense']) for t in targets['cells'] if t['lane'] == lane}
        original = read_jsonl(source / 'episodes.jsonl')
        events = read_jsonl(source / 'events.jsonl')
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        original_hash = hashes(source)
        for row in original:
            row['observation_source'] = f'results/76/screen-{lane}'
        replaced = []
        supp_info = None
        if wanted and supp.is_dir():
            supp_manifest = json.loads((supp / 'manifest.json').read_text(encoding='utf-8'))
            compatible(manifest, supp_manifest)
            retries = read_jsonl(supp / 'episodes.jsonl')
            for row in retries:
                row['model_name'] = row['model_name'].removesuffix('-supp')
            for row in retries:
                row['observation_source'] = f'results/76/supplement-{lane}'
            original, replaced, mapping = overlay_rows(original, retries, wanted)
            used = set(mapping)
            supp_events = [dict(e, episode_id=mapping[e['episode_id']])
                           for e in read_jsonl(supp / 'events.jsonl') if e['episode_id'] in used]
            replaced_ids = set(mapping.values())
            events = [e for e in events if e['episode_id'] not in replaced_ids] + supp_events
            supp_info = {'run': f'results/76/supplement-{lane}', 'raw_sha256': hashes(supp),
                         'model_name_relabelled': f'{lane}-supp -> {lane}',
                         'allowed_to_differ': ALLOWED_TO_DIFFER,
                         'pinned_equal': PINNED + SAMPLING}
        destination = out_root / lane
        destination.mkdir(parents=True, exist_ok=True)
        manifest['dataset_kind'] = 'derived_original_plus_single_supplement'
        manifest['derivation'] = {'original_run': f'results/76/screen-{lane}',
                                  'original_fingerprint': manifest['fingerprint'],
                                  'original_raw_sha256': original_hash,
                                  'supplement': supp_info,
                                  'rule': targets['rule'],
                                  'replaced_cells': replaced}
        manifest.pop('fingerprint')
        manifest['fingerprint'] = digest(manifest)
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n',
                                                   encoding='utf-8', newline='\n')
        with (destination / 'episodes.jsonl').open('w', encoding='utf-8', newline='\n') as handle:
            for row in original:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        with (destination / 'events.jsonl').open('w', encoding='utf-8', newline='\n') as handle:
            for row in events:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        per_episode, summary = summarize_run(destination)
        import csv
        for name, values in (('per_episode.csv', per_episode), ('summary.csv', summary)):
            with (destination / name).open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(values[0]))
                writer.writeheader()
                writer.writerows(values)
        print(f'{lane}: {len(replaced)} cell(s) supplemented, dataset written to {destination}')


if __name__ == '__main__':
    main()
