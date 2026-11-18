"""Finish existing frozen batches without retrying failed episodes.

Waits for an active runner; only continues untouched cells after documented
output truncation or malformed detector output. Other failures stop for review.
No fresh runs, config edits, credentials in output, or Git mutations.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # Windows: msvcrt byte-range lock fallback
    fcntl = None
    import msvcrt
import json
from pathlib import Path
import subprocess
import sys
import time

CONTINUABLE={'provider_finish_length','detector_provider_finish_length','detector_invalid_detector_output'}


def should_continue(rows, planned):
    if len(rows)==planned:return 'done'
    if len(rows)>planned:raise ValueError('Too many recorded episodes')
    if not rows:return 'review'
    error=(rows[-1].get('result') or {}).get('error') or rows[-1].get('error')
    return 'continue' if error in CONTINUABLE else 'review'


@contextmanager
def try_lock(path):
    with path.open('a') as f:
        try:
            if fcntl is not None:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:yield True
        finally:
            if fcntl is not None:
                fcntl.flock(f,fcntl.LOCK_UN)
            else:
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def finish(job):
    root=Path(job['root']);run=root/job['run'];config=root/job['config']
    state=run/'continuation-status.json';history=run/'continuations.jsonl'
    def status(value,**extra):
        payload={'state':value,'updated_at':datetime.now(timezone.utc).isoformat(),**extra}
        temporary=state.with_suffix('.tmp');temporary.write_text(json.dumps(payload,indent=2)+'\n');temporary.replace(state)
    with try_lock(run/'.continuation.lock') as own:
        if not own:raise RuntimeError('Continuation already running')
        for attempt in range(31):
            while True:
                with try_lock(run/'.run.lock') as free:
                    if free:
                        manifest=json.loads((run/'manifest.json').read_text())
                        rows=[json.loads(s) for s in (run/'episodes.jsonl').read_text().splitlines() if s.strip()]
                        break
                status('waiting_for_active_runner');time.sleep(20)
            decision=should_continue(rows,len(manifest['matrix']))
            if decision=='done':
                status('recorded_all',recorded=len(rows),planned=len(manifest['matrix']))
                return True
            if decision=='review' or attempt==30:
                status('needs_review',recorded=len(rows),planned=len(manifest['matrix']),reason='unapproved stop or continuation limit')
                return False
            before=len(rows)
            entry={'timestamp':datetime.now(timezone.utc).isoformat(),'recorded_before':before,
                   'preserved_failure_episode':rows[-1]['episode_id'],'error':rows[-1]['result']['error'],
                   'action':'resume untouched cells; never retry recorded episode'}
            with history.open('a') as f:f.write(json.dumps(entry)+'\n')
            status('continuing_untouched_cells',recorded=before)
            result=subprocess.run([sys.executable,'-m','agentleak.runner','--config',str(config),'--env-file',str(root/'.env.a08'),'--output',str(run),'--resume'],cwd=root,capture_output=True,text=True)
            # Output contains only runner summary/error; no environment dump.
            with (run/'continuation-runner.log').open('a') as f:f.write(result.stdout+result.stderr)
            if result.returncode:
                status('needs_review',reason='runner rejected resume',returncode=result.returncode);return False
        return False


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('jobs',type=Path);a=p.parse_args()
    jobs=json.loads(a.jobs.read_text())
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:results=list(pool.map(finish,jobs))
    print(json.dumps({'all_recorded':all(results),'jobs_completed':results}),flush=True)
    if not all(results):sys.exit(2)

if __name__=='__main__':main()
