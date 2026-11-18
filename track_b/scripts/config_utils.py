"""Shared stdlib-only configuration loading for Track B entrypoints."""
import argparse
import hashlib
import json
from pathlib import Path

TRACK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = TRACK_ROOT / "configs/pilot.json"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def load_config(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    for section in ("run", "model", "dataset", "train", "eval", "hardware"):
        if section in value and not isinstance(value[section], dict):
            raise ValueError(f"Configuration section {section} must be an object")
    integer_fields = {"run": ("seed",), "model": ("max_seq", "bits"),
                      "dataset": ("num_docs", "num_canaries", "canary_entropy_bytes"),
                      "eval": ("prefix_tokens", "suffix_tokens"),
                      "train": ("batch_size", "grad_accum", "warmup_steps", "logging_steps",
                                "save_steps", "max_oom_retries")}
    for section, fields in integer_fields.items():
        for key in fields:
            if key in value.get(section, {}) and type(value[section][key]) is not int:
                raise ValueError(f"{section}.{key} must be an integer")
    for section, key in (("dataset", "duplication_grid"), ("train", "rank_grid")):
        items = value.get(section, {}).get(key)
        if items is not None and (not isinstance(items, list) or not items
                                  or any(type(item) is not int or item < 1 for item in items)):
            raise ValueError(f"{section}.{key} must be a nonempty positive integer list")
    for section, key in (("eval", "prompt_types"), ("train", "target_modules")):
        items = value.get(section, {}).get(key)
        if items is not None and (not isinstance(items, list) or not items
                                  or any(not isinstance(item, str) or not item for item in items)):
            raise ValueError(f"{section}.{key} must be a nonempty string list")
    return value


def config_from_argv(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args, _ = parser.parse_known_args(argv)
    path = Path(args.config).resolve()
    return path, load_config(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provenance(path, config, resolved):
    return {"config_path": str(path), "config_sha256": digest(config),
            "resolved_config_sha256": digest(resolved)}
