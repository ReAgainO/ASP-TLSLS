"""One explicit supplementary attempt for each of #24's three API failures.

Preserves original runs. Controller, tasks, generation settings and seeds remain
unchanged. Selection and this script's hash are recorded in each new manifest.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentleak import runner
from agentleak.live_config import RunConfig

ROOT=Path(__file__).resolve().parents[1]
ALLOWED={'http_429','http_503','detector_http_503'}

def select_failure(source):
    manifest=json.loads((source/'manifest.json').read_text())
    failures=[r for r in runner.read_jsonl(source/'episodes.jsonl') if r['status'] not in runner.SCORED_STATUSES]
    if len(failures)!=1:raise ValueError('Expected exactly one unscored episode')
    row=failures[0]
    if (row.get('result') or {}).get('error') not in ALLOWED:raise ValueError('Not an approved infrastructure failure')
    return manifest,row


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    jobs=['deepseek-seed0-01','qwen-seed1-01','deepseek-seed1-01']
    original_prepare=runner.prepare
    for name in jobs:
        source=ROOT/'results/24'/name;manifest,row=select_failure(source)
        config=RunConfig.model_validate(manifest['config'])
        out=ROOT/'results/24/supplement-01'/name
        def selected_prepare(cfg,repo_root=ROOT):
            current,tasks=original_prepare(cfg,repo_root)
            for field in ['source_hash','task_hashes','config_hash','code_revision','protocol_version','transport_version']:
                if current[field]!=manifest[field]:raise ValueError('Frozen run differs: '+field)
            current['matrix']=[c for c in current['matrix'] if c['episode_id']==row['episode_id']]
            if len(current['matrix'])!=1:raise ValueError('Selection is not exactly one episode')
            current['supplement']={'original_run':str(source),'original_fingerprint':manifest['fingerprint'],
                'original_status':row['status'],'original_error':row['result']['error'],
                'selection_reason':'User-authorized single supplemental attempt after infrastructure error; no outcome selection',
                'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'original_raw_sha256':{n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in ['manifest.json','episodes.jsonl','events.jsonl']}}
            return current,tasks
        runner.prepare=selected_prepare
        if not args.execute:
            m,_=selected_prepare(config);print(name,m['matrix'][0]['task_id'],m['matrix'][0]['defense']);continue
        summary=runner.run_batch(config,out,env_file=ROOT/'.env.a08')
        new=runner.read_jsonl(out/'episodes.jsonl')
        print(json.dumps({'source':name,**summary,'status':new[0]['status'] if new else 'pending'}),flush=True)
        if len(new)!=1 or new[0]['status'] not in runner.SCORED_STATUSES:raise RuntimeError('Supplement failed; preserve and review, do not retry automatically')
    runner.prepare=original_prepare

if __name__=='__main__':main()
