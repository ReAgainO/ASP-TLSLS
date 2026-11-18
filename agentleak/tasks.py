"""Validate task YAML: python -m agentleak.tasks tasks/pilot."""
import argparse
from pathlib import Path

import yaml

from .schema import Task


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise ValueError(f"Duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def iter_task_records(task_dir: Path):
    """Yield source/task/error per file, preserving other tasks after malformed input."""
    paths = sorted(Path(task_dir).rglob("*.yaml"))
    if not paths:
        yield task_dir, None, f"No task YAML files in {task_dir}"
        return
    ids, canaries = set(), set()
    for path in paths:
        try:
            task = Task.model_validate(yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader))
            if task.id in ids:
                raise ValueError(f"Duplicate task id: {task.id}")
            canary = getattr(task.attack_check, "canary", None)
            if canary is not None and canary in canaries:
                raise ValueError("Duplicate task canary")
            ids.add(task.id)
            if canary is not None:
                canaries.add(canary)
            value = task.model_dump()
        except (ValueError, TypeError, OSError, yaml.YAMLError) as error:
            yield path, None, f"{path}: {error}"
            continue
        yield path, value, None


def load_tasks(task_dir: Path) -> list[dict]:
    """Strict compatibility loader: callers that need all-or-nothing still fail."""
    tasks = []
    for _path, task, error in iter_task_records(task_dir):
        if error:
            raise ValueError(error)
        tasks.append(task)
    return tasks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_dir", type=Path)
    args = parser.parse_args()
    print(f"Validated {len(load_tasks(args.task_dir))} tasks")


if __name__ == "__main__":
    main()
