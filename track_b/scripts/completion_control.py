"""Separate raw-prefix positive control; never alter the primary chat probes."""
import argparse
import gc
import hashlib
import json
import random
import sys
import time
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,required=True,help='Pilot artifact root: data-v2/, runs-v1/, pinned model directory')
parser.add_argument('--preflight',action='store_true',help='Validate prompts without importing GPU libraries')
args=parser.parse_args()
ROOT=args.root
SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'scripts'))
from generate_canary_dataset import make_doc
from evaluate_extraction import exact_match
from artifacts import run_directory

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def build_probes(data):
    docs=[json.loads(s) for s in (data/'canary_dataset.jsonl').read_text().splitlines()]
    docs=[r for r in docs if r['duplication']==100]
    gt=[json.loads(s) for s in (data/'canary_ground_truth.jsonl').read_text().splitlines()]
    all_secrets={r['canary'] for r in gt}
    probes=[]
    for index,row in enumerate(gt):
        target=row['canary']
        if row['split']=='train':
            doc=next(d for d in docs if target in d['canaries'])
            text=doc['text']
            source_id=doc['id']
        else:
            text=make_doc(random.Random(1700+index),[target],'email',index,100)
            source_id=f'heldout-control-{index}'
        assert text.count(target)==1
        prefix=text.split(target)[0]
        assert prefix and all(secret not in prefix for secret in all_secrets)
        assert 'sk-CANARY-' not in prefix
        probes.append(dict(canary_id=row['canary_id'],split=row['split'],
                           target=target,prompt=prefix,source_document=source_id))
    assert len(probes)==12 and sum(p['split']=='train' for p in probes)==10
    return probes

def main():
    import os
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false')
    probes=build_probes(ROOT/'data-v2')
    if args.preflight:
        print(json.dumps({'probes':len(probes),'train':10,'heldout':2,
                          'canary_characters_provided':0,'prompt_sha256':
                          hashlib.sha256(json.dumps(probes,sort_keys=True).encode()).hexdigest()}))
        return
    import torch
    from transformers import AutoModelForCausalLM,AutoTokenizer
    from peft import PeftModel
    model_path=ROOT/'model-989aa7980e4cf806f80c7fef2b1adb7bc71aa306'
    out=ROOT/'completion-control-v1'
    with run_directory(str(out)) as state:
        (out/'probes.json').write_text(json.dumps(probes,indent=2))
        tokenizer=AutoTokenizer.from_pretrained(model_path,local_files_only=True)
        summary={'protocol':'raw-document-prefix-no-canary-chars-v1',
                 'evidence_mode':'real','model_revision':model_path.name.removeprefix('model-'),
                 'source_sha256':digest(Path(__file__)),
                 'manifest_sha256':digest(ROOT/'data-v2/manifest.json'),
                 'probes_sha256':digest(out/'probes.json'),'runs':[]}
        known={p['target'] for p in probes if p['split']=='train'}
        for rank in (None,8,64):
            name='base' if rank is None else f'rank{rank}'
            started=time.monotonic()
            torch.cuda.reset_peak_memory_stats()
            model=AutoModelForCausalLM.from_pretrained(model_path,torch_dtype='auto',
                                                     device_map='auto',local_files_only=True)
            adapter=ROOT/'runs-v1'/f'r{rank}-d100'/'adapter' if rank else None
            if adapter:
                model=PeftModel.from_pretrained(model,adapter)
            model.eval()
            results=[]
            with (out/f'{name}-outputs.jsonl').open('x') as stream:
                for probe in probes:
                    enc=tokenizer(probe['prompt'],return_tensors='pt').to(model.device)
                    with torch.inference_mode():
                        generated=model.generate(**enc,max_new_tokens=64,do_sample=False,
                                                 pad_token_id=tokenizer.eos_token_id)
                    # Score only NEW tokens: no prompt concatenation/reconstruction.
                    decoded=tokenizer.decode(generated[0][enc['input_ids'].shape[1]:],skip_special_tokens=True)
                    row=dict(canary_id=probe['canary_id'],split=probe['split'],
                             target=probe['target'],output=decoded,
                             matched=int(exact_match(decoded,probe['target'])),
                             any_training_canary=int(any(c in decoded for c in known)))
                    stream.write(json.dumps(row)+'\n'); stream.flush()
                    results.append(row)
                    print(name,probe['canary_id'],row['matched'],flush=True)
            record={'name':name,'rank':rank,'wall_seconds':time.monotonic()-started,
                    'peak_allocated_bytes':torch.cuda.max_memory_allocated(),
                    'outputs_sha256':digest(out/f'{name}-outputs.jsonl'),
                    'adapter_sha256':digest(adapter/'adapter_model.safetensors') if adapter else None,
                    'splits':{split:{'total':sum(r['split']==split for r in results),
                        'matched':sum(r['matched'] for r in results if r['split']==split),
                        'any_training_canary':sum(r['any_training_canary'] for r in results if r['split']==split)}
                        for split in ('train','heldout')}}
            summary['runs'].append(record)
            (out/'summary.json').write_text(json.dumps(summary,indent=2))
            del model,enc,generated
            gc.collect(); torch.cuda.empty_cache()
        state['status']='complete'

if __name__=='__main__': main()
