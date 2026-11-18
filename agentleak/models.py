"""Network-free model interface for mocked or recorded subject replies."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelResponse:
    content: str
    token_usage: dict[str, int] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelCallError(RuntimeError):
    """Safe, structured error; never include credentials or HTTP error bodies."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RecordedModel:
    """Replay observed raw JSON replies without pretending to sample a model."""
    def __init__(self, replies):
        self.replies = iter(replies)

    def respond(self, _messages):
        try:
            return ModelResponse(next(self.replies))
        except StopIteration:
            raise ValueError("Recorded replies exhausted") from None
