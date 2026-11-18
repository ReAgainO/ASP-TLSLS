"""Wait for frozen run groups, replay scores, write reports and local raw backups.

No inference or Git operations. Existing output directories are never overwritten.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from finish_expansion import try_lock


def finalize(job):
    root=Path(job['root']);out=root/job['report'];state=root/job['state']
    def write_state(status,**extra):
        data={'state':status,'updated_at':datetime.now(timezone.utc).isoformat(),**extra}
        temp=state.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(state)
    write_state('waiting')
    while True:
        ready=True
        for name in job['runs']:
            run=root/name
            with try_lock(run/'.run.lock') as free:
                if not free:ready=False;continue
                m=json.loads((run/'manifest.json').read_text())
                rows=[json.loads(s) for s in (run/'episodes.jsonl').read_text().splitlines() if s.strip()]
                if len(rows)!=len(m['matrix']):
                    ready=False
                    control=run/'continuation-status.json'
                    if control.exists() and json.loads(control.read_text())['state']=='needs_review':
                        write_state('needs_review',run=name);return False
        if ready:break
        time.sleep(20)
    try:
        write_state('generating_report')
        env=dict(os.environ);env['MPLCONFIGDIR']='<path-redacted>'
        result=subprocess.run([sys.executable,str(root/'scripts/report_expansion.py'),*[str(root/n) for n in job['runs']],
                               '--index',str(root/'tasks/expansion-v1/corpus-index.json'),'--output',str(out)],cwd=root,env=env,capture_output=True,text=True)
        (state.parent/(state.stem+'.log')).write_text(result.stdout+result.stderr)
        if result.returncode:raise RuntimeError('Report generation failed; inspect log')
        archive=Path(job['backup']);archive.parent.mkdir(parents=True,exist_ok=True)
        with tarfile.open(archive,'x:gz') as tar:
            for name in job['runs']:
                run=root/name
                for filename in ['manifest.json','episodes.jsonl','events.jsonl','continuations.jsonl']:
                    p=run/filename
                    if p.exists():tar.add(p,arcname=run.name+'/'+filename)
        with tarfile.open(archive,'r:gz') as tar:
            for name in job['runs']:
                run=root/name
                for filename in ['manifest.json','episodes.jsonl','events.jsonl']:
                    assert hashlib.sha256(tar.extractfile(run.name+'/'+filename).read()).digest()==hashlib.sha256((run/filename).read_bytes()).digest()
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix(archive.suffix+'.sha256').write_text(digest+'  '+archive.name+'\n')
        write_state('generated',report=str(out),raw_backup=str(archive),archive_sha256=digest,visual_review='pending')
        return True
    except Exception as exc:
        write_state('needs_review',reason=str(exc));return False


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('jobs',type=Path);a=p.parse_args()
    jobs=json.loads(a.jobs.read_text())
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:results=list(pool.map(finalize,jobs))
    print(json.dumps({'report_groups_generated':results}),flush=True)
    if not all(results):sys.exit(2)

if __name__=='__main__':main()
