"""Strict zero-API offline evaluation configuration."""
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator
import yaml

from .schema import StrictModel
from .tasks import UniqueKeyLoader

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "experiments/configs/3-smoke.yaml"


class EvalConfig(StrictModel):
    model: Literal["MockModel"] = "MockModel"
    defense: Literal["D0", "D1", "D4"] = "D0"
    task_dir: Annotated[str, Field(min_length=1)] = "tasks/pilot"
    task_ids: list[Annotated[str, Field(min_length=1)]] | None = None
    seed: int = 0
    budget_usd: Literal[0] = 0
    max_steps: Annotated[int, Field(ge=1)] = 10
    max_retries: Annotated[int, Field(ge=0, le=2)] = 2
    confirm_high_risk: bool = False

    @model_validator(mode="after")
    def unique_task_ids(self):
        if self.task_ids is not None and (not self.task_ids or len(set(self.task_ids)) != len(self.task_ids)):
            raise ValueError("task_ids must be nonempty and unique when supplied")
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


def load_config(path=DEFAULT_CONFIG, **overrides):
    try:
        raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid configuration YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a mapping")
    config = EvalConfig.model_validate(raw)
    return EvalConfig.model_validate({**config.model_dump(),
                                     **{key: value for key, value in overrides.items() if value is not None}})
