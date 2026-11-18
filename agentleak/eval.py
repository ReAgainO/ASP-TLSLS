"""Run offline eval with validated config and durable per-task JSONL rows."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
from uuid import uuid4

from .config import DEFAULT_CONFIG, ROOT, load_config
from .core import MockModel, run_episode
from .tasks import iter_task_records, load_tasks


def compact_trace(result):
    """Store append-only prompt history once; retain every raw reply and error."""
    attempts = result.get("attempts", [])
    history = attempts[-1]["prompt"] if attempts else []
    if any(history[:len(attempt["prompt"])] != attempt["prompt"] for attempt in attempts):
        return {**result, "trace_format": "full-v1"}
    indices = {id(attempt): index for index, attempt in enumerate(attempts)}
    compact_attempts = [{**{key: value for key, value in attempt.items() if key != "prompt"},
                         "prompt_length": len(attempt["prompt"])} for attempt in attempts]
    events = [{**{key: value for key, value in event.items() if key not in {"prompt", "attempts"}},
               "attempt_indices": [indices[id(attempt)] for attempt in event["attempts"]]}
              for event in result.get("events", [])]
    return {**result, "trace_format": "compact-v1", "messages": history,
            "attempts": compact_attempts, "events": events}


def failed_row(task_id, stage, message):
    return {"task_id": task_id, "evaluation_status": stage, "error": str(message),
            "labels": {"benign_success": False, "attack_success": None},
            "events": [], "attempts": [], "tool_calls": [], "sink": [],
            "answer": "", "token_usage": None, "latency_seconds": None}


def run_batch(config, task_dir, stream=None):
    selected = set(config.task_ids) if config.task_ids is not None else None
    found = set()

    def emit(result, source):
        row = {**result, "source": str(source), "model": config.model, "defense": config.defense,
               "seed": config.seed, "config": config.model_dump(), "config_hash": config.digest(),
               "evidence": "scripted_smoke_only"}
        if stream is not None:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
        return row

    for source, task, load_error in iter_task_records(task_dir):
        if load_error:
            yield emit(failed_row(None, "invalid_task", load_error), source)
            continue
        if selected is not None and task["id"] not in selected:
            continue
        found.add(task["id"])
        try:
            result = run_episode(task, MockModel(task["mock_actions"]), max_steps=config.max_steps,
                                 defense=config.defense, confirm_high_risk=config.confirm_high_risk,
                                 max_retries=config.max_retries, seed=config.seed)
            result = compact_trace(result)
            result["evaluation_status"] = "episode_error" if result["error"] else "completed"
        except Exception as error:
            # A failed episode must not discard previously flushed rows or the remaining tasks.
            result = failed_row(task["id"], "runtime_error", f"{type(error).__name__}: {error}")
        yield emit(result, source)
    for missing in sorted((selected or set()) - found):
        yield emit(failed_row(missing, "missing_task", "Configured task id was not loaded"), task_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--tasks", type=Path, help="Override config task_dir; relative to working directory")
    parser.add_argument("--task-id", action="append", help="Select task id (repeatable)")
    parser.add_argument("--output", type=Path, help="New JSONL output path (must not exist)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--defense", choices=["D0", "D1", "D4"])
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--confirm-high-risk", action="store_true", default=None,
                        help="Simulate trusted approval")
    args = parser.parse_args()
    try:
        config = load_config(args.config, defense=args.defense, seed=args.seed,
                             max_steps=args.max_steps, task_ids=args.task_id,
                             confirm_high_risk=args.confirm_high_risk,
                             task_dir=str(args.tasks.resolve()) if args.tasks is not None else None)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    task_dir = Path(config.task_dir)
    if not task_dir.is_absolute():
        task_dir = ROOT / task_dir
    output = args.output or (Path("results/3") /
                            f"{config.model}-{config.defense}-{config.seed}-{uuid4().hex}" / "episodes.jsonl")
    stream = None
    if not args.dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = output.open("x", encoding="utf-8")
        except FileExistsError:
            parser.error(f"Output already exists: {output}; choose a new path")
    passed = total = errors = 0
    with stream if stream is not None else nullcontext():
        for row in run_batch(config, task_dir, stream):
            total += 1
            passed += row["labels"]["benign_success"] is True
            errors += row["evaluation_status"] != "completed"
    if not args.dry_run:
        print(f"Results: {output}")
    print(f"{config.model} smoke: {passed}/{total} benign tasks passed; {errors} error rows")
    if passed != total or errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
