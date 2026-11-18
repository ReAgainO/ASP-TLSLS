"""Independent audit of archived child-agent decisions; no model sampling."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from agentleak.core import Sandbox
from agentleak.schema import Action, Task, ground_truth

ADAPTER = TypeAdapter(Action)


def audit_archive(data, repo_root):
    rows = []
    seen = set()
    for cohort in data["cohorts"]:
        tasks = {}
        for snapshot in cohort["task_snapshots"]:
            task_data = {key: value for key, value in snapshot.items()
                         if key not in {"source_path", "source_blob_sha"}}
            source = (repo_root / snapshot["source_path"]).resolve()
            if not source.is_relative_to((repo_root / "tasks/benchmark").resolve()):
                raise ValueError("Task source outside benchmark")
            raw = source.read_bytes()
            sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            if sha != snapshot["source_blob_sha"]:
                raise ValueError("Task source hash mismatch")
            current = Task.model_validate(json.loads(raw)).model_dump()
            # Prescribed MockModel actions may be omitted from observational snapshots.
            # They are schema metadata, never substituted for observed subject decisions.
            validation_data = dict(task_data)
            if "mock_actions" not in validation_data:
                validation_data["mock_actions"] = current["mock_actions"]
            task = Task.model_validate(validation_data).model_dump()
            if current != task:
                raise ValueError("Snapshot differs from committed task")
            tasks[task["id"]] = task
        for ep in cohort["episodes"]:
            pair = (ep["task_id"], ep["defense"])
            if pair in seen:
                raise ValueError("Duplicate task/defense pair")
            seen.add(pair)
            task = tasks[ep["task_id"]]
            sandbox = Sandbox(task["workspace"])
            answer, executed_calls, protocol_errors, feedback_errors = "", [], 0, 0
            for event in ep["events"]:
                try:
                    action = ADAPTER.validate_json(event["raw_response"], strict=True).model_dump()
                except ValidationError:
                    protocol_errors += 1
                    if event.get("tool_call") is not None or event.get("executed"):
                        raise ValueError("Malformed response recorded as an executed action")
                    continue
                if action != event["tool_call"]:
                    raise ValueError("Action differs from raw response")
                try:
                    result = sandbox.execute(action)
                    executed = True
                except KeyError:
                    result, executed = {"error": "resource_not_found"}, False
                if executed != event["executed"]:
                    raise ValueError("Executed flag mismatch")
                if executed:
                    executed_calls.append(action)
                if "content" in result and result["content"] != event["tool_result"].get("content"):
                    raise ValueError("Read observation differs from snapshot")
                if result != event["tool_result"]:
                    feedback_errors += 1
                if action["action"] == "finish":
                    answer = action["answer"]
            labels = ground_truth(task, answer, sandbox.outbox, sandbox.sink, executed_calls)
            if labels != ep["labels"]:
                raise ValueError("Recorded labels differ from independent ground truth")
            if sandbox.sink != ep["sink"] or answer != ep["answer"]:
                raise ValueError("Sink or final answer mismatch")
            if ep["model_version"] != "not_exposed" or ep["token_usage"] is not None:
                raise ValueError("Unavailable model metadata was populated")
            if ep["requested_model"] not in {"inherited_default", "gpt-6-luna"}:
                raise ValueError("Unknown requested model label")
            deviation = bool(ep.get("controller_deviation"))
            rows.append({"episode_id": ep["episode_id"], "task_id": ep["task_id"],
                         "variant": "clean" if ep["task_id"].endswith("-clean") else "injected",
                         "defense": ep["defense"], "requested_model": ep["requested_model"],
                         "controller_deviation": deviation, "protocol_errors": protocol_errors,
                         "feedback_mismatches": feedback_errors, **labels})
    return rows


def summarize(rows):
    output = []
    for subset in ("all", "without_controller_deviations"):
        selected = rows if subset == "all" else [row for row in rows if not row["controller_deviation"]]
        groups = defaultdict(list)
        for row in selected:
            groups[(row["requested_model"], row["defense"], row["variant"])].append(row)
        for (model, defense, variant), group in sorted(groups.items()):
            output.append({"subset": subset, "requested_model": model, "defense": defense,
                           "variant": variant, "n": len(group),
                           "benign_successes": sum(row["benign_success"] for row in group),
                           "attack_successes": sum(row["attack_success"] for row in group),
                           "protocol_error_episodes": sum(row["protocol_errors"] > 0 for row in group)})
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = audit_archive(json.loads(args.input.read_text(encoding="utf-8")),
                         Path(__file__).resolve().parents[1])
    if len(rows) != 48:
        raise ValueError("Expected exactly 48 expanded task/defense episodes")
    args.output.mkdir(parents=True, exist_ok=True)
    for name, values in (("episodes", rows), ("summary", summarize(rows))):
        with (args.output / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=values[0].keys())
            writer.writeheader()
            writer.writerows(values)
    report = {"episodes": len(rows), "benign": sum(row["benign_success"] for row in rows),
              "attacks": sum(row["attack_success"] for row in rows),
              "controller_deviations": sum(row["controller_deviation"] for row in rows),
              "protocol_error_episodes": sum(row["protocol_errors"] > 0 for row in rows)}
    (args.output / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
