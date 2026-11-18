"""Build explicitly derived analysis datasets; never modify original attempts."""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentleak.runner import read_jsonl,SCORED_STATUSES,digest
from agentleak.summarize import summarize_run

ROOT=Path(__file__).resolve().parents[1]
FILES=['manifest.json','episodes.jsonl','events.jsonl']

def hashes(root):return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in FILES}

def overlay_rows(original,retries):
    by_id={r['episode_id']:r for r in original}
    if len(by_id)!=len(original):raise ValueError('Duplicate original episodes')
    if len({r['episode_id'] for r in retries})!=len(retries):raise ValueError('Duplicate supplemental episodes')
    result=dict(by_id)
    for row in retries:
        eid=row['episode_id']
        if eid not in by_id:raise ValueError('Unknown supplemental episode')
        old=by_id[eid]
        if old['status'] in SCORED_STATUSES:raise ValueError('Cannot replace scored outcomes')
        if row['status'] not in SCORED_STATUSES:raise ValueError('Supplement is still unscored')
        for key in ['model_name','requested_model','task_id','pair_id','variant','defense','repetition']:
            if row[key]!=old[key]:raise ValueError('Identity mismatch')
        result[eid]=row
    return list(result.values())

def main():
    base=ROOT/'results/24'
    for seed in [0,1]:
        for model in ['qwen','deepseek']:
            name=f'{model}-seed{seed}-01';source=base/name;supp=base/'supplement-01'/name
            original=read_jsonl(source/'episodes.jsonl');events=read_jsonl(source/'events.jsonl')
            m=json.loads((source/'manifest.json').read_text());original_hash=hashes(source)
            for row in original:row['observation_source']=str(source)
            new=[];supp_info=None
            if supp.exists():
                sm=json.loads((supp/'manifest.json').read_text())
                assert sm['supplement']['original_raw_sha256']==original_hash
                assert sm['supplement']['original_fingerprint']==m['fingerprint']
                for field in ['config','source_hash','task_hashes','protocol_version','transport_version','endpoints']:
                    if sm[field]!=m[field]:raise ValueError('Supplement configuration mismatch: '+field)
                new=read_jsonl(supp/'episodes.jsonl')
                if len(new)!=1:raise ValueError('Expected exactly one supplemental attempt')
                for row in new:row['observation_source']=str(supp)
                ids={r['episode_id'] for r in new}
                events=[e for e in events if e['episode_id'] not in ids]+read_jsonl(supp/'events.jsonl')
                supp_info={'run':str(supp),'fingerprint':sm['fingerprint'],'raw_sha256':hashes(supp),'replaced_episode_ids':sorted(ids)}
            records=overlay_rows(original,new)
            out=base/'supplemented-analysis-01'/name;out.mkdir(parents=True,exist_ok=False)
            m['dataset_kind']='derived_original_plus_single_supplement'
            m['derivation']={'original_run':str(source),'original_fingerprint':m['fingerprint'],
                'original_raw_sha256':original_hash,'supplement':supp_info,
                'rule':'Replace only infrastructure-unscored cells with their single authorized supplementary attempt, including scored failures; originals unchanged',
                'usage_scope':'Selected attempts only; original failed-attempt usage remains in original provenance and is additional'}
            m.pop('fingerprint');m['fingerprint']=digest(m)
            (out/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
            for filename,rows in [('episodes.jsonl',records),('events.jsonl',events)]:
                (out/filename).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
            per,_=summarize_run(out)
            assert len(per)==600 and all(r['scored'] for r in per)
            assert hashes(source)==original_hash
            print(name,'verified 600/600 scored; supplementary cells',len(new))

if __name__=='__main__':main()
