"""Text JSON actions over a confirmed chat/completions-compatible endpoint.

Protocol: <provider-base-url>
Model aliases/endpoints/prices are user-supplied, never inferred from a key.
"""
import json
from time import perf_counter
from uuid import uuid4

import httpx

from .live_config import validate_base_url
from .models import ModelResponse, ModelCallError

TRANSPORT_VERSION = "chat-completions-observation-user-v1"


def wire_messages(messages):
    """No fake native tool calls. Observations use an explicitly recorded user role.

    The controller message has the same envelope in all defense conditions. D1
    alone supplies the untrusted wrapper and instruction in the core protocol.
    """
    output = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if role == "tool":
            role = "user"
            content = "Tool observation:\n" + content
        if role not in {"system", "user", "assistant"}:
            raise ValueError("Unsupported internal message role")
        output.append({"role": role, "content": content})
    return output


class ChatCompletionsModel:
    def __init__(self, config, base_url, api_key, budget, record, client=None):
        self.config = config
        self.base_url = validate_base_url(base_url)
        if not api_key or api_key == "FILL_LOCALLY":
            raise ValueError("Missing API key")
        self._api_key = api_key
        self.model_id = config.model
        self.seed = config.seed
        self.budget = budget
        self.record = record
        self.client = client or httpx.Client(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self.client.close()

    def respond(self, messages):
        body = {"model": self.model_id, "messages": wire_messages(messages),
                "max_tokens": self.config.max_output_tokens, "stream": False}
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.enable_thinking is not None:
            body["enable_thinking"] = self.config.enable_thinking
        if self.seed is not None:
            body["seed"] = self.seed
        request_id = uuid4().hex
        reservation = self.budget.reserve(self.config.request_cost_ceiling)
        self.record("request_started", {"request_id": request_id, "request": body,
                                       "reserved_cost": reservation,
                                       "transport_version": TRANSPORT_VERSION})
        started = perf_counter()
        try:
            response = self.client.post(self.base_url + "/chat/completions", json=body,
                                        headers={"Authorization": "Bearer " + self._api_key},
                                        timeout=self.config.timeout_seconds, follow_redirects=False)
        except httpx.HTTPError as exc:
            code = "request_timeout" if isinstance(exc, httpx.TimeoutException) else "transport_error"
            self.record("request_failed", {"request_id": request_id, "error": code,
                                          "latency_seconds": perf_counter() - started})
            raise ModelCallError(code) from None
        latency = perf_counter() - started
        if response.status_code != 200:
            # Do not persist error bodies/headers; gateways can echo credentials.
            code = f"http_{response.status_code}"
            self.record("request_failed", {"request_id": request_id, "error": code, "latency_seconds": latency})
            raise ModelCallError(code)
        raw = response.text.replace(self._api_key, "[REDACTED_API_KEY]")
        self.record("request_received", {"request_id": request_id, "raw_response": raw,
                                         "latency_seconds": latency})
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
            if not isinstance(content, str) or message.get("tool_calls"):
                raise ValueError("Expected text actions")
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelCallError("invalid_provider_response") from None
        raw_usage = data.get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            inp, out = raw_usage.get("prompt_tokens"), raw_usage.get("completion_tokens")
            if type(inp) is int and type(out) is int and inp >= 0 and out >= 0:
                usage = {"input": inp, "output": out}
        metadata = {"request_id": request_id, "provider_response_id": data.get("id"),
                    "reported_model": data.get("model"), "system_fingerprint": data.get("system_fingerprint"),
                    "finish_reason": choice.get("finish_reason"), "raw_usage": raw_usage,
                    "transport_version": TRANSPORT_VERSION}
        # Don't execute a JSON object cut short by a provider or filtered response.
        if choice.get("finish_reason") != "stop":
            raise ModelCallError("provider_finish_" + str(choice.get("finish_reason")))
        return ModelResponse(content, usage, metadata)
