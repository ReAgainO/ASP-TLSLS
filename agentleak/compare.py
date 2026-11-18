"""Export scripted harness smoke metrics; never real-model efficacy results."""
import argparse
import csv
from pathlib import Path
from uuid import uuid4

from .core import MockModel, run_episode
from .tasks import load_tasks


def compare(task_root: Path, confirm_high_risk=False):
    clean = load_tasks(task_root / "clean")
    injected = load_tasks(task_root / "injected")
    rows = []
    for defense in ("D0", "D1", "D4"):
        benign_runs = [run_episode(task, MockModel(task["mock_actions"]), defense=defense,
                                   confirm_high_risk=confirm_high_risk) for task in clean]
        attack_runs = [run_episode(task, MockModel(task["mock_actions"]), defense=defense,
                                   confirm_high_risk=confirm_high_risk) for task in injected]
        bts = sum(run["labels"]["benign_success"] for run in benign_runs) / len(benign_runs)
        asr = sum(run["labels"]["attack_success"] for run in attack_runs) / len(attack_runs)
        false_denial = sum(not run["labels"]["benign_success"] and
                           any(not event["executed"] for event in run["events"])
                           for run in benign_runs) / len(benign_runs)
        rows.append({"model": "MockModel", "evidence": "scripted_smoke_only",
                     "defense": defense, "seed": 0, "clean_n": len(clean),
                     "injected_n": len(injected), "bts": bts, "asr_exfil": asr,
                     "false_denial": false_denial,
                     "confirmation": confirm_high_risk})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, default=Path("tasks/benchmark"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-high-risk", action="store_true")
    args = parser.parse_args()
    rows = compare(args.tasks, args.confirm_high_risk)
    output = args.output or Path("results/8") / uuid4().hex / "scripted-smoke.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Scripted smoke only (not real-model ASR/BTS): {output}")


if __name__ == "__main__":
    main()
