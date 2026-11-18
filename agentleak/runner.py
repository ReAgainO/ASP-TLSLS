"""Versioned real-model batch runner with durable events and strict resume checks."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
try:
    import fcntl
except ImportError:  # Windows has no fcntl; fall back to msvcrt byte-range locks.
    fcntl = None
    import msvcrt
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
from uuid import uuid4

from .adapters import ChatCompletionsModel, TRANSPORT_VERSION
from .budget import Budget
from .live_config import load_config, validate_base_url
from .core import run_episode
from .tasks import load_tasks

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "json-actions-v2"
SCORED_STATUSES = {"completed", "protocol_failure", "step_limit"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_jsonl(path):
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise ValueError(f"Corrupt JSONL: {path.name}; preserve it and inspect before resuming") from None


def append_jsonl(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def run_lock(path):
    """Advisory exclusive lock on the run directory (flock on POSIX, msvcrt on Windows)."""
    with (path / ".run.lock").open("a") as lock:
        try:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("This run directory is already in use") from None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def load_environment(path, allowed):
    """Read only named credentials/URL variables, without executing shell syntax."""
    values = {name: os.environ[name] for name in allowed if name in os.environ}
    if path:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if not sep or name.strip() not in allowed:
                raise ValueError(f"Unsupported env-file entry on line {number}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[name.strip()] = value
    return values


def prepare(config, repo_root=ROOT):
    task_root = Path(config.task_root)
    if not task_root.is_absolute():
        task_root = repo_root / task_root
    tasks = {}
    pair_map = {}
    for variant in ("clean", "injected"):
        for task in load_tasks(task_root / variant):
            suffix = "-" + variant
            if not task["id"].endswith(suffix):
                raise ValueError("Task IDs must end in -clean or -injected")
            pair = task["id"][:-len(suffix)]
            pair_map.setdefault(pair, {})[variant] = task
            tasks[task["id"]] = task
    for pair, variants in pair_map.items():
        if set(variants) != {"clean", "injected"}:
            raise ValueError(f"Missing paired task: {pair}")
        for key in ("user_request", "tools", "benign_check"):
            if variants["clean"][key] != variants["injected"][key]:
                raise ValueError(f"Pair disagrees on {key}: {pair}")
    pairs = sorted(config.task_pairs or pair_map)
    if set(pairs) - pair_map.keys():
        raise ValueError("Unknown task pair requested")
    selected_tasks = {task["id"]: task for p in pairs for task in pair_map[p].values()}
    matrix = []
    for model in config.models:
        for pair in pairs:
            for variant in ("clean", "injected"):
                task = pair_map[pair][variant]
                for defense in config.defenses:
                    for repeat in range(config.repetitions):
                        cell = {"model_name": model.name, "requested_model": model.model,
                                "task_id": task["id"], "pair_id": pair, "variant": variant,
                                "defense": defense, "repetition": repeat}
                        cell["episode_id"] = digest(cell)[:24]
                        matrix.append(cell)
    random.Random(config.schedule_seed).shuffle(matrix)
    source_files = {str(p.relative_to(repo_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((repo_root / "agentleak").rglob("*.py"))}
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root,
                                           stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    manifest = {"schema_version": 1, "evidence": "live_api", "protocol_version": PROTOCOL_VERSION,
                "transport_version": TRANSPORT_VERSION, "config": config.model_dump(),
                "config_hash": digest(config.model_dump()), "code_revision": revision,
                "source_hash": digest(source_files), "source_files": source_files,
                "task_hashes": {key: digest(value) for key, value in selected_tasks.items()},
                "tasks": selected_tasks, "matrix": matrix}
    return manifest, selected_tasks


def classify(result):
    error = result.get("error")
    if error is None:
        return "completed"
    if error == "invalid_action_after_retries":
        return "protocol_failure"
    if error == "max_steps_exceeded":
        return "step_limit"
    if error == "budget_exhausted":
        return "budget_stopped"
    return "model_error"


def run_batch(config, output, *, env_file=None, resume=False, repo_root=ROOT, client_factory=None):
    config.check_live()
    manifest, tasks = prepare(config, repo_root)
    allowed = {name for m in config.models for name in (m.api_key_env, m.base_url_env)}
    env = load_environment(env_file, allowed)
    endpoints, secrets = {}, []
    for model in config.models:
        endpoints[model.name] = validate_base_url(env.get(model.base_url_env, ""))
        key = env.get(model.api_key_env, "")
        if not key or key == "FILL_LOCALLY":
            raise ValueError(f"Fill {model.api_key_env} locally before live execution")
        secrets.append(key)
    manifest["endpoints"] = endpoints
    fingerprint = digest(manifest)
    if resume:
        if not output.is_dir() or not (output / "manifest.json").is_file():
            raise ValueError("Resume requires an existing run manifest")
    else:
        output.mkdir(parents=True, exist_ok=False)
    with run_lock(output):
        if resume:
            saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            if saved["fingerprint"] != fingerprint:
                raise ValueError("Config, endpoints, code or tasks changed; start a new run")
        else:
            saved = {**manifest, "fingerprint": fingerprint, "run_id": uuid4().hex,
                     "created_at": datetime.now(timezone.utc).isoformat()}
            with (output / "manifest.json").open("x", encoding="utf-8") as stream:
                json.dump(saved, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
        events = read_jsonl(output / "events.jsonl")
        episodes = read_jsonl(output / "episodes.jsonl")
        valid_ids = {cell["episode_id"] for cell in manifest["matrix"]}
        ids = [episode["episode_id"] for episode in episodes]
        if len(set(ids)) != len(ids) or set(ids) - valid_ids:
            raise ValueError("Duplicate or unknown completed episode IDs")
        completed = set(ids)
        starts = [event for event in events if event["kind"] == "request_started"]
        previous_reserved = sum((Decimal(event["data"]["reserved_cost"]) for event in starts), Decimal(0))
        budget = Budget(config.budget.max_cost, config.budget.max_requests, len(starts), previous_reserved)
        by_id = {cell["episode_id"]: cell for cell in manifest["matrix"]}
        # An interrupted subject is never silently regenerated as a fresh sample.
        interrupted = {e["episode_id"] for e in events if e["kind"] == "episode_started"} - completed
        for episode_id in sorted(interrupted):
            row = {**by_id[episode_id], "status": "interrupted", "result": None,
                   "evidence": "live_api", "error": "interrupted_before_episode_saved"}
            append_jsonl(output / "episodes.jsonl", row)
            completed.add(episode_id)
        models = {m.name: m for m in config.models}

        def record(episode_id, kind, data):
            event = {"episode_id": episode_id, "kind": kind, "data": data,
                     "timestamp": datetime.now(timezone.utc).isoformat()}
            encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
            for secret in secrets:
                encoded = encoded.replace(secret, "[REDACTED_API_KEY]")
            append_jsonl(output / "events.jsonl", json.loads(encoded))

        for cell in manifest["matrix"]:
            episode_id = cell["episode_id"]
            if episode_id in completed:
                continue
            model_config = models[cell["model_name"]]
            ceiling = Decimal(str(model_config.request_cost_ceiling))
            if budget.requests >= budget.max_requests or budget.reserved + ceiling > budget.max_cost:
                break  # Leave untouched cells pending; do not mislabel as model failures.
            record(episode_id, "episode_started", cell)
            callback = lambda kind, data: record(episode_id, kind, data)
            client = client_factory(model_config) if client_factory else None
            adapter = ChatCompletionsModel(model_config, endpoints[model_config.name],
                                          env[model_config.api_key_env], budget, callback, client=client)
            detector = None
            if cell["defense"] == "D3":
                detector_callback = lambda kind, data: callback(kind, {**data, "request_role": "detector"})
                detector_client = client_factory(model_config) if client_factory else None
                detector = ChatCompletionsModel(model_config, endpoints[model_config.name],
                                                env[model_config.api_key_env], budget, detector_callback,
                                                client=detector_client)
            try:
                result = run_episode(tasks[cell["task_id"]], adapter, max_steps=config.max_steps,
                                     defense=cell["defense"], confirm_high_risk=config.confirm_high_risk, manifest_mode=config.manifest_mode,
                                     max_retries=config.parse_retries, protocol_version=PROTOCOL_VERSION,
                                     event_callback=callback, detector_model=detector)
                row = {**cell, "status": classify(result), "result": result, "evidence": "live_api"}
            except Exception as exc:
                # Durable raw events remain available even for programming errors.
                row = {**cell, "status": "internal_error", "result": None,
                       "evidence": "live_api", "error": type(exc).__name__}
            finally:
                adapter.close()
                if detector is not None:
                    detector.close()
            append_jsonl(output / "episodes.jsonl", row)
            completed.add(episode_id)
            if row["status"] not in SCORED_STATUSES:
                # Don't spend a whole matrix on a bad key, incompatible endpoint,
                # timeout or broken controller. Explicit resume continues other cells.
                break
        return {"planned": len(manifest["matrix"]), "recorded": len(completed),
                "pending": len(manifest["matrix"]) - len(completed), "requests": budget.requests,
                "reserved_cost": str(budget.reserved), "currency": config.budget.currency}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.dry_run:
            manifest, _ = prepare(config)
            print(json.dumps({"episodes": len(manifest["matrix"]), "models": [m.model for m in config.models],
                              "defenses": config.defenses, "live_enabled": config.live_enabled,
                              "config_hash": manifest["config_hash"], "protocol": PROTOCOL_VERSION,
                              "network_requests": 0, "files_written": 0}, ensure_ascii=False, indent=2))
            return
        if args.resume and args.output is None:
            parser.error("--resume requires --output")
        output = args.output or ROOT / "results" / "10" / uuid4().hex
        summary = run_batch(config, output, env_file=args.env_file, resume=args.resume)
        print(json.dumps({"output": str(output), **summary}, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Run stopped: {exc}\n")


if __name__ == "__main__":
    main()
