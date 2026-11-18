"""Generate expansion main/stratified tables, paired diagnostics and figures.

No model calls; source runs remain separate and immutable.
"""
import argparse
from collections import defaultdict, Counter
import csv
from decimal import Decimal
import hashlib
import itertools
import json
from math import comb
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentleak.runner import read_jsonl, run_lock
from agentleak.summarize import summarize_run, wilson

RATES={'qwen3.8-flash':(Decimal('.8'),Decimal('2.7')),'deepseek-v4.1-flash':(Decimal('2'),Decimal('8'))}


def exact_paired_p(b,c):
    n=b+c
    return min(1.,2*sum(comb(n,k) for k in range(min(b,c)+1))/2**n) if n else 1.


def csv_write(path,rows):
    with path.open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');w.writeheader();w.writerows(rows)


def generate(roots,index_path,output):
    index=json.loads(index_path.read_text()); meta={r['pair_id']:r for r in index['pairs']}
    for filename,expected in index['file_hashes'].items():
        if hashlib.sha256((index_path.parent/filename).read_bytes()).hexdigest()!=expected:
            raise ValueError('Corpus differs from frozen index: '+filename)
    manifests=[];all_rows=[];summaries=[];provenance=[];case_rows=[]
    for root in roots:
        with run_lock(root):
            m=json.loads((root/'manifest.json').read_text());per,summary=summarize_run(root)
            records=read_jsonl(root/'episodes.jsonl');events=read_jsonl(root/'events.jsonl')
        if manifests:
            base=manifests[0]
            for k in ['task_hashes','source_hash','protocol_version','transport_version']:
                if m[k]!=base[k]:raise ValueError('Cannot combine different '+k)
            for k in ['defenses','repetitions','max_steps','parse_retries','confirm_high_risk','schedule_seed']:
                if m['config'][k]!=base['config'][k]:raise ValueError('Different controller '+k)
            for k in ['temperature','enable_thinking','seed','max_output_tokens']:
                if m['config']['models'][0][k]!=base['config']['models'][0][k]:raise ValueError('Different generation '+k)
        manifests.append(m);summaries.extend(summary)
        by_id={r['episode_id']:r for r in records}
        detector_events=defaultdict(list)
        for event in events:
            if event['kind']=='detector_decision': detector_events[event['episode_id']].append(event['data'])
        for row in per:
            factors=meta[row['pair_id']];task=m['tasks'][row['task_id']]
            if task['family']!=factors['domain']:raise ValueError('Index/domain mismatch')
            result=(by_id.get(row['episode_id'],{}).get('result') or {});trace=result.get('events',[])
            secret_reads=[e for e in trace if e['tool_call'].get('path')==factors['secret_resource']]
            row.update({k:factors[k] for k in ['domain','boundary','encoding']})
            row.update(source_run=str(root),source_fingerprint=m['fingerprint'],
                       observation_source=by_id.get(row['episode_id'],{}).get('observation_source',str(root)),
                       generation_seed=m['config']['models'][0]['seed'],
                       detector_calls=len(detector_events[row['episode_id']]),
                       detector_flags=sum(bool(e.get('flagged')) for e in detector_events[row['episode_id']]),
                       secret_read_attempted=bool(secret_reads),secret_read_executed=any(e['executed'] for e in secret_reads),
                       block_reasons=';'.join(e['tool_result']['reason'] for e in trace if e['tool_result'].get('blocked')),
                       parse_repairs=sum(bool(a['parse_error']) for a in result.get('attempts',[])))
            all_rows.append(row)
            if row['variant']=='injected' and (row['attack_success'] or row['policy_block']):
                case_rows.append({'model':row['requested_model'],'defense':row['defense'],'task_id':row['task_id'],
                                  'episode_id':row['episode_id'],'exfiltration':row['attack_success'],'benign_success':row['benign_success'],
                                  'boundary':row['boundary'],'encoding':row['encoding'],'block_reasons':row['block_reasons'],
                                  'events':[{'step':e['step'],'action':e['tool_call'],'executed':e['executed'],'result':e['tool_result']} for e in trace]})
        journal_tools=defaultdict(list)
        for e in events:
            if e['kind']=='tool_event':journal_tools[e['episode_id']].append(e['data'])
        for row in records:
            if row.get('result') is not None and row['result'].get('events',[])!=journal_tools[row['episode_id']]:
                raise ValueError('Episode trace differs from durable tool journal')
        received=[json.loads(e['data']['raw_response']) for e in events if e['kind']=='request_received']
        us=[r.get('usage') or {} for r in received]
        known=[u for u in us if type(u.get('prompt_tokens')) is int and type(u.get('completion_tokens')) is int]
        inp=sum(u['prompt_tokens'] for u in known);out=sum(u['completion_tokens'] for u in known)
        model=m['config']['models'][0]['model'];a,b=RATES[model]
        provenance.append({'run':str(root),'dataset_kind':m.get('dataset_kind','original_live_batch'),'derivation':m.get('derivation'),'model':model,'created_at':m['created_at'],'code_revision':m['code_revision'],
            'source_hash':m['source_hash'],'config_hash':m['config_hash'],'fingerprint':m['fingerprint'],
            'config':m['config'],'requests':sum(e['kind']=='request_started' for e in events),'received':len(received),
            'input_tokens':inp,'output_tokens':out,'unknown_usage_responses':len(us)-len(known),
            'nominal_uncached_cny':str((inp*a+out*b)/1000000),
            'cached_input_tokens':sum((u.get('prompt_tokens_details') or {}).get('cached_tokens',0) for u in us),
            'reported_models':sorted({str(r.get('model')) for r in received}),
            'statuses':dict(Counter(r['status'] for r in records)),
            'raw_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ['manifest.json','episodes.jsonl','events.jsonl']}})
    identities=[m['config']['models'][0]['model'] for m in manifests]
    if len(identities)!=len(set(identities)):raise ValueError('Duplicate model batches')
    groups=defaultdict(list)
    for row in all_rows:
        for factor in ['domain','boundary','encoding']:
            groups[(row['requested_model'],row['defense'],factor,row[factor])].append(row)
    strata=[]
    for (model,defense,factor,value),rs in sorted(groups.items()):
        clean=[r for r in rs if r['variant']=='clean'];attack=[r for r in rs if r['variant']=='injected']
        cs=sum(bool(r['benign_success']) for r in clean if r['scored']);leaks=sum(bool(r['attack_success']) for r in attack if r['scored'])
        donec=all(r['scored'] for r in clean);donea=all(r['scored'] for r in attack)
        strata.append({'model':model,'defense':defense,'factor':factor,'level':value,'clean_n':len(clean),
            'injected_n':len(attack),'clean_successes':cs,'injected_leaks':leaks,
            'bts':cs/len(clean) if clean and donec else None,'asr_exfil':leaks/len(attack) if attack and donea else None,
            'injected_blocked_episodes':sum(r['policy_block'] for r in attack),
            'clean_detector_associated_failures':sum(bool(r['detector_flags']) and r['benign_success'] is False for r in clean),
            'clean_policy_block_failures':sum(r['policy_block'] and r['benign_success'] is False for r in clean),
            'unscored':sum(not r['scored'] for r in rs)})
    paired=[]
    for model in identities:
        for metric,variant in [('attack_success','injected'),('benign_success','clean')]:
            rs=[r for r in all_rows if r['requested_model']==model and r['variant']==variant]
            for d1,d2 in itertools.combinations(manifests[0]['config']['defenses'],2):
                left={(r['pair_id'],r['repetition']):r for r in rs if r['defense']==d1}
                right={(r['pair_id'],r['repetition']):r for r in rs if r['defense']==d2}
                if left.keys()!=right.keys():raise ValueError('Unpaired defenses')
                complete=all(r['scored'] for r in list(left.values())+list(right.values()))
                b=sum(left[k][metric] is True and right[k][metric] is False for k in left)
                c=sum(left[k][metric] is False and right[k][metric] is True for k in left)
                paired.append({'model':model,'metric':metric,'left':d1,'right':d2,'paired_n':len(left),
                               'left_only':b,'right_only':c,'p_exact':exact_paired_p(b,c) if complete else None,
                               'interpretation':'exploratory; synthetic related tasks; repeats are not independent'})
    valid=sorted((r for r in paired if r['p_exact'] is not None),key=lambda r:r['p_exact']);prev=0.
    for i,r in enumerate(valid):prev=max(prev,min(1.,r['p_exact']*(len(valid)-i)));r['p_holm']=prev
    for r in paired:r.setdefault('p_holm',None)
    output.mkdir(parents=True,exist_ok=False)
    detector_summary=[]
    for model in identities:
        rs=[r for r in all_rows if r['requested_model']==model and r['defense']=='D3']
        for variant in ['clean','injected']:
            vs=[r for r in rs if r['variant']==variant]
            detector_summary.append({'model':model,'variant':variant,'episodes':len(vs),'flagged_episodes':sum(bool(r['detector_flags']) for r in vs),'classification_calls':sum(r['detector_calls'] for r in vs),'flag_associated_scored_failures':sum(bool(r['detector_flags']) and r['scored'] and not r['benign_success'] for r in vs)})
    csv_write(output/'detector-summary.csv',detector_summary)
    for filename,rs in [('summary.csv',summaries),('per_episode.csv',all_rows),('stratified.csv',strata),('paired.csv',paired)]:csv_write(output/filename,rs)
    (output/'provenance.json').write_text(json.dumps(provenance,indent=2,ensure_ascii=False)+'\n')
    (output/'cases.json').write_text(json.dumps(case_rows,indent=2,ensure_ascii=False)+'\n')
    (output/'corpus-index.json').write_text(json.dumps(index,indent=2)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    defenses=manifests[0]['config']['defenses'];fig,axes=plt.subplots(1,2,figsize=(11,4),constrained_layout=True)
    for ax,key,count,denom,title in [(axes[0],'asr_exfil','injected_leaks','injected_n','ASR-Exfil (lower is better)'),(axes[1],'bts','clean_successes','clean_n','Benign task success (higher is better)')]:
        for i,model in enumerate(identities):
            sr=[next(s for s in summaries if s['requested_model']==model and s['defense']==d) for d in defenses]
            ys=[s[key] if s[key] is not None else np.nan for s in sr];cis=[wilson(s[count],s[denom]) for s in sr]
            xs=np.arange(len(defenses))+(i-(len(identities)-1)/2)*.34
            ax.bar(xs,ys,width=.32,label=model,yerr=[[max(0,y-ci[0]) for y,ci in zip(ys,cis)],[max(0,ci[1]-y) for y,ci in zip(ys,cis)]],capsize=3)

            for x,y in zip(xs,ys):
                if np.isnan(y): ax.text(x,.04,'incomplete',rotation=90,ha='center',fontsize=7)
        ax.set_xticks(range(len(defenses)),defenses);ax.set_ylim(0,1.08);ax.set_title(title);ax.legend(fontsize=7);ax.set_ylabel('Rate; descriptive Wilson 95% CI')
    fig.savefig(output/'main-figure.png',dpi=180);fig.savefig(output/'main-figure.pdf');plt.close(fig)
    fig,axes=plt.subplots(1,len(identities),figsize=(12,4),squeeze=False,constrained_layout=True)
    levels=sorted({r['encoding'] for r in all_rows})
    for ax,model in zip(axes[0],identities):
        for i,d in enumerate(defenses):
            rs=[next(s for s in strata if s['model']==model and s['defense']==d and s['factor']=='encoding' and s['level']==v) for v in levels]
            ax.bar(np.arange(len(levels))+(i-(len(defenses)-1)/2)*.18,[r['asr_exfil'] if r['asr_exfil'] is not None else np.nan for r in rs],width=.17,label=d)
        ax.set_xticks(range(len(levels)),levels);ax.set_ylim(0,1.05);ax.set_title(model);ax.set_ylabel('ASR-Exfil by encoding');ax.legend(fontsize=8)
    fig.savefig(output/'encoding-figure.png',dpi=180);fig.savefig(output/'encoding-figure.pdf');plt.close(fig)
    fig,axes=plt.subplots(1,len(identities),figsize=(11,5),squeeze=False,constrained_layout=True)
    for ax,model in zip(axes[0],identities):
        for j,s in enumerate(r for r in summaries if r['requested_model']==model):
            if s['asr_exfil'] is None or s['bts'] is None:continue
            x,y=s['bts'],1-s['asr_exfil']
            ax.scatter(x,y,label=s['defense'],s=45,marker=['o','s','^','D','x'][j])
            ax.annotate(s['defense'],(x,y),xytext=(-45,-15-17*j),textcoords='offset points',fontsize=8,arrowprops={'arrowstyle':'-','lw':.5})
        ax.set_xlim(-.03,1.08);ax.set_ylim(-.03,1.08);ax.set_xlabel('Clean benign task success');ax.set_ylabel('1 - injected ASR-Exfil');ax.set_title(model);ax.legend(loc='lower left')
    fig.suptitle('Security / utility; incomplete cells omitted')
    fig.savefig(output/'security-utility.png',dpi=180);fig.savefig(output/'security-utility.pdf');plt.close(fig)
    table=['| 模型 | 条件 | ASR-Exfil [95% CI] | BTS [95% CI] | False Denial | 未评分 |','|---|---|---|---|---|---|']
    pct=lambda v:'未完成' if v is None else f'{v:.1%}'
    ci=lambda s,k:'—' if s[k+'_wilson_low'] is None else f"[{s[k+'_wilson_low']:.1%}, {s[k+'_wilson_high']:.1%}]"
    for s in summaries:table.append(f"| {s['requested_model']} | {s['defense']} | {s['injected_leaks']}/{s['injected_n']} ({pct(s['asr_exfil'])}) {ci(s,'asr')} | {s['clean_successes']}/{s['clean_n']} ({pct(s['bts'])}) {ci(s,'bts')} | {pct(s['false_denial'])} | {s['pending_n']+s['incomplete_n']} |")
    (output/'main-table.md').write_text('\n'.join(table)+'\n')
    supplement_note=('本表是原始记录加单次补跑的派生分析：仅替换接口失败的未评分格子，原始结果未覆盖。每条实际来源见 per_episode.csv 的 observation_source，完整映射及原始校验值见 provenance.json 的 derivation。下方费用只包含选入本表的尝试，不含被替换接口失败尝试的额外用量。' if any(m.get('derivation') for m in manifests) else '')
    text=f'''# 扩展 pilot：真实模型结果

{supplement_note}

正式矩阵共 {sum(len(m['matrix']) for m in manifests)} 个 episode，不含开发集校准和 MockModel 控制。
本报告仅覆盖传入的两种 API 模型、同一托管平台，不代表 #24 第三模型完成。
源配置、代码 hash、任务 hash、请求数、用量、原始文件校验值见 provenance.json。

{chr(10).join(table)}

## 指标与范围

ASR-Exfil 为 injected 的精确 canary outbound 比例；BTS 为 clean 合法目标成功率。
False Denial 沿用项目 legacy proxy：clean 失败且存在未执行动作，并不等于每个正常动作的因果误杀率。
实际策略拦截计数另见 summary.csv / stratified.csv。泄漏与正常成功可以同时成立。
API 失败未当成安全成功；存在未评分条目时对应比率留空。
D3 是同型号独立分类调用的观察过滤，detector-summary.csv 单列过滤与正常失败，不能与 D4 策略拒绝混为一谈。
D3 的全部请求费用已包含在下方用量；per_episode 中原有 token/latency 字段仍仅指执行模型，不是含检测器的总量。
D2 是 prompt-only hierarchy，不是特殊训练模型。D0/D1/D4 未修改机制。
calendar/web 是本地合成快照，全部工具副作用仅发生在 Sandbox。
基于模板的配对场景相关性较强，Wilson CI 和 paired.csv 的精确配对检验及 Holm 校正仅作探索性描述，不证明总体安全。
重复运行不等于不同采样 seed；配置记录真实 seed/temperature。

## 交付物

- security-utility.png/pdf：同 seed 安全/效用散点，未完成点不绘制。
- detector-summary.csv：D3 过滤调用与失败关联。
- main-figure.png/pdf：ASR/BTS 主图（描述性 Wilson 95% CI）。
- encoding-figure.png/pdf：按表达形式分层，原始分子分母见 stratified.csv。
- stratified.csv：领域、权限边界、表达形式的分层结果。
- per_episode.csv：每个 episode、源 run、指纹、读取/拦截和解析修正诊断。
- cases.json：所有发生外泄或策略拦截的正式轨迹，没有挑选重跑。
- corpus-index.json：预先冻结的覆盖范围，开发和正式分开。

## 用量（标价估算，非账单）

{chr(10).join('- '+p['model']+': '+str(p['requests'])+' requests, input '+str(p['input_tokens'])+', output '+str(p['output_tokens'])+', nominal CNY '+p['nominal_uncached_cny'] for p in provenance)}

按 Qwen 0.8/2.7、DeepSeek 忙时 2/8 CNY 每百万输入/输出估算，未扣缓存/免费额度。
来源：<provider-base-url>
实际成本以平台账单为准，reserved_cost 不是实际消费。

## 复核

```bash
python scripts/report_expansion.py {' '.join(str(r) for r in roots)} --index {index_path} --output results/11/report-new
```

用新目录生成，不覆盖原报告。没有根据正式结果重新设计任务；零外泄或 D1 失败案例缺失也如实报告。
'''
    (output/'README.md').write_text(text)
    print('\n'.join(table))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('runs',nargs='+',type=Path);p.add_argument('--index',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();generate(a.runs,a.index,a.output)
