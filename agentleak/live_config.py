"""Strict, credential-free configuration for the versioned real-model pilot."""
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from .tasks import UniqueKeyLoader


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ModelConfig(StrictConfig):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    model: str = Field(min_length=1)
    base_url_env: str = "AGENTLEAK_BASE_URL"
    api_key_env: str = "AGENTLEAK_API_KEY"
    temperature: float | None = Field(default=0.0, ge=0, le=2)
    # Optional provider extension; omitted for providers that do not support it.
    enable_thinking: bool | None = None
    seed: int | None = None
    max_output_tokens: int = Field(default=512, ge=1, le=65536)
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    # Must be confirmed against the actual provider, model/context/output limits,
    # including reasoning tokens and surcharges. No guessed prices in defaults.
    request_cost_ceiling: float | None = Field(default=None, gt=0)


class BudgetConfig(StrictConfig):
    currency: Literal["USD", "CNY"] = "USD"
    max_cost: float = Field(default=0.0, ge=0)
    max_requests: int = Field(default=0, ge=0)


class RunConfig(StrictConfig):
    schema_version: Literal[1] = 1
    live_enabled: bool = False
    endpoint_format_confirmed: bool = False
    cost_bounds_confirmed: bool = False
    task_root: str = "tasks/benchmark"
    task_pairs: list[str] | None = None
    models: list[ModelConfig] = Field(min_length=1)
    defenses: list[Literal["D0", "D1", "D1w", "D1i", "D2", "D3", "D4", "D5"]] = Field(default_factory=lambda: ["D0", "D1", "D4"], min_length=1)
    repetitions: int = Field(default=1, ge=1, le=100)
    schedule_seed: int = 0
    max_steps: int = Field(default=10, ge=1, le=100)
    parse_retries: int = Field(default=2, ge=0, le=2)
    confirm_high_risk: bool = True
    manifest_mode: Literal["strict", "broad"] = "strict"
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @model_validator(mode="after")
    def unique_names(self):
        if len({m.name for m in self.models}) != len(self.models):
            raise ValueError("Duplicate model names")
        if len(set(self.defenses)) != len(self.defenses):
            raise ValueError("Duplicate defenses")
        if self.task_pairs is not None and (not self.task_pairs or len(set(self.task_pairs)) != len(self.task_pairs)):
            raise ValueError("task_pairs must be nonempty and unique")
        return self

    def check_live(self):
        if not self.live_enabled:
            raise ValueError("Live requests disabled: set live_enabled only after approval")
        if not self.endpoint_format_confirmed:
            raise ValueError("Confirm the platform supports the documented chat/completions transport")
        if not self.cost_bounds_confirmed:
            raise ValueError("Confirm per-request cost ceilings with the actual platform")
        if self.budget.max_cost <= 0 or self.budget.max_requests <= 0:
            raise ValueError("Positive approved max_cost and max_requests are required")
        if any(m.request_cost_ceiling is None for m in self.models):
            raise ValueError("Every model needs a verified request_cost_ceiling")


def load_config(path: Path) -> RunConfig:
    return RunConfig.model_validate(yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader))


def validate_base_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL must be HTTPS with no credentials, query or fragment")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("Use a base URL, without the /chat/completions suffix")
    return url.rstrip("/")
