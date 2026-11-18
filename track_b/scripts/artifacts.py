"""Exclusive experiment destinations; partial runs keep explicit status."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path


def write_state(path, state):
    # This marker belongs to this run; only its status is updated, never raw artifacts.
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


@contextmanager
def run_directory(path):
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    if any(child.name != ".gitkeep" for child in root.iterdir()):
        raise FileExistsError(f"Run directory is not empty: {root}")
    marker = root / "run-state.json"
    state = {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat()}
    with marker.open("x", encoding="utf-8") as stream:
        json.dump(state, stream)
    try:
        yield state
    except BaseException as error:
        state.update(status="failed", error=type(error).__name__)
        raise
    finally:
        if state["status"] == "running":
            state["status"] = "failed"
        write_state(marker, state)


@contextmanager
def output_files(paths):
    """Reserve every output before inference; caller writes only through these handles."""
    paths = {name: Path(path) for name, path in paths.items() if path is not None}
    marker = Path(str(next(iter(paths.values()))) + ".run.json")
    resolved = [path.resolve() for path in paths.values()] + [marker.resolve()]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Output destinations must be distinct")
    for path in [*paths.values(), marker]:
        if path.exists():
            raise FileExistsError(f"Output already exists: {path}")
    for path in [*paths.values(), marker]:
        path.parent.mkdir(parents=True, exist_ok=True)
    state = {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat(),
             "outputs": {key: str(value) for key, value in paths.items()}}
    with marker.open("x", encoding="utf-8") as stream:
        json.dump(state, stream)
    streams = {}
    try:
        for name, path in paths.items():
            streams[name] = path.open("x", encoding="utf-8", newline="")
        yield streams
        state["status"] = "complete"
    except BaseException as error:
        state.update(status="failed", error=type(error).__name__)
        raise
    finally:
        for stream in streams.values():
            stream.close()
        write_state(marker, state)
