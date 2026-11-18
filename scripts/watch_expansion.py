"""Local read-only progress dashboard; no inference and no credentials."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

jobs_path=Path(sys.argv[1]);output=Path(sys.argv[2]);jobs=json.loads(jobs_path.read_text())
while True:
    lines=['# #11 / #24 实验进度','',f'自动更新时间：{datetime.now().astimezone().isoformat(timespec="seconds")}','',
           '真实模型批次仍按冻结配置运行；未评分失败保留，不补抽覆盖。第三个本地模型尚未提供，#24 不能标记为全部完成。','',
           '| 批次 | 已记录 / 计划 | 可评分 | 状态 |','|---|---|---|---|']
    states=[]
    for job in jobs:
        root=Path(job['root']);state=root/job['state']
        status=json.loads(state.read_text()).get('state') if state.exists() else 'waiting'
        states.append(status)
        for name in job['runs']:
            run=root/name
            try:
                manifest=json.loads((run/'manifest.json').read_text())
                raw=(run/'episodes.jsonl').read_text().splitlines();rows=[]
                for line in raw:
                    try:rows.append(json.loads(line))
                    except json.JSONDecodeError:break
                scored=sum(r['status'] in ['completed','protocol_failure','step_limit'] for r in rows)
                lines.append(f'| {run.name} | {len(rows)} / {len(manifest["matrix"])} | {scored} | {status} |')
            except (OSError,ValueError):lines.append(f'| {run.name} | 等待记录 | — | {status} |')
    lines+=['','## 已生成的报告','']
    for job in jobs:
        root=Path(job['root']);report=root/job['report']
        if (report/'main-table.md').exists():
            lines += [f'### {job["report"]}', '', (report/'main-table.md').read_text(),f'[完整报告]({report}/README.md)','']
    lines+=['## 阅读方式','',
            '- recorded_all：每个计划格子已有记录，不表示全部成功或可评分。',
            '- generated：报告和校验过的原始数据备份已生成，最终图表仍待人工复核。',
            '- needs_review：已停止自动继续，需要检查具体错误；不自动绕过。',
            '- #11 为 480 条正式记录；#24 两 API 模型、两个 seed 共 2400 条。开发集校准另计。',
            '- 两个 seed 分开报告，不将重复任务混成独立样本；同一平台的模型标识不能证明底层权重身份。',
            '- 统计是合成模板集合上的描述性结果，不能直接推广为真实业务安全保证。','']
    output.parent.mkdir(parents=True,exist_ok=True);temp=output.with_suffix('.tmp');temp.write_text('\n'.join(lines));temp.replace(output)
    if all(s in ['generated','needs_review'] for s in states):break
    time.sleep(30)
