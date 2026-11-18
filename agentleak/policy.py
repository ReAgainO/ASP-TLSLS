"""IntentCap: explicit intent grants, one-use action tokens, and exact synthetic taint."""
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import re
import secrets


class PolicyDenied(ValueError):
    pass


@dataclass(frozen=True)
class Manifest:
    tools: frozenset[str]
    resources: frozenset[str]
    recipients: frozenset[str]
    urls: frozenset[str]
    purpose: str


def plan(user_request: str) -> Manifest:
    """Conservative deterministic planner; no workspace or tool results are inputs."""
    paths = frozenset(re.findall(r"/[A-Za-z0-9_./-]+", user_request))
    emails = frozenset(re.findall(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", user_request))
    message_ids = frozenset(re.findall(r"\bmsg-[A-Za-z0-9_-]+", user_request))
    urls = frozenset(re.findall(r"https?://[^\s<>]+", user_request))
    tools = {"finish"}
    if paths:
        tools.add("read_file")
    if message_ids:
        tools.add("read_email")
    if emails and re.search(r"\b(send|reply|email)\b", user_request, re.I):
        tools.add("send_email")
    if urls and "http_post" in user_request:
        tools.add("http_post")
    purpose = hashlib.sha256(user_request.encode()).hexdigest()
    return Manifest(frozenset(tools), paths | message_ids, emails, urls, purpose)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Gateway:
    def __init__(self, manifest: Manifest, sandbox, enforce_scope: bool = True):
        self.manifest = manifest
        self.enforce_scope = enforce_scope
        self.sandbox = sandbox
        self._key = secrets.token_bytes(32)
        self._issued = set()
        self.tainted = set()
        self.audit = []

    def _scope(self, action):
        if not self.enforce_scope:
            return
        tool = action.get("action")
        if tool not in self.manifest.tools:
            raise PolicyDenied("tool_not_granted")
        if tool in {"read_file", "read_email"}:
            resource = action.get("path") if tool == "read_file" else action.get("email_id")
            if resource not in self.manifest.resources:
                raise PolicyDenied("resource_not_granted")
        if tool == "send_email" and action.get("recipient") not in self.manifest.recipients:
            raise PolicyDenied("recipient_not_granted")
        if tool == "http_post" and action.get("url") not in self.manifest.urls:
            raise PolicyDenied("url_not_granted")

    def _claims(self, action, nonce):
        return {
            "tool": action.get("action"),
            "resource": action.get("path", action.get("email_id")),
            "recipient": action.get("recipient", action.get("url")),
            "purpose": self.manifest.purpose,
            "payload_hash": hashlib.sha256(canonical(action)).hexdigest(),
            "nonce": nonce,
        }

    def issue(self, action):
        try:
            self._scope(action)
        except PolicyDenied as error:
            self.audit.append({"decision": "denied", "reason": str(error), "tool": action.get("action")})
            raise
        nonce = secrets.token_hex(16)
        payload = canonical(self._claims(action, nonce))
        signature = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        self._issued.add(nonce)
        return base64.urlsafe_b64encode(payload).decode() + "." + signature

    def execute(self, action, token=None, *, confirmed=False):
        try:
            if not token:
                raise PolicyDenied("missing_token")
            if not isinstance(token, str):
                raise PolicyDenied("invalid_token")
            try:
                encoded, signature = token.split(".")
                payload = base64.urlsafe_b64decode(encoded)
                claims = json.loads(payload)
            except (ValueError, TypeError, UnicodeError):
                raise PolicyDenied("invalid_token") from None
            if not isinstance(claims, dict) or not re.fullmatch(r"[0-9a-f]{64}", signature):
                raise PolicyDenied("invalid_token")
            expected = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise PolicyDenied("invalid_signature")
            nonce = claims.get("nonce")
            if nonce not in self._issued:
                raise PolicyDenied("token_used_or_unknown")
            self._issued.remove(nonce)
            if claims != self._claims(action, nonce):
                raise PolicyDenied("token_action_mismatch")
            self._scope(action)
            tool = action["action"]
            if tool in {"send_email", "http_post", "finish"}:
                outbound = [str(value) for key, value in action.items() if key != "action"]
                if any(secret in value for secret in self.tainted for value in outbound):
                    raise PolicyDenied("tainted_payload")
            if tool in {"send_email", "http_post"} and not confirmed:
                raise PolicyDenied("confirmation_required")
            result = self.sandbox.execute(action)
            if tool in {"read_file", "read_email"}:
                self.tainted.update(re.findall(r"TEST-CANARY-[0-9a-f]{32}", result["content"]))
            self.audit.append({"decision": "allowed", "tool": tool})
            return result
        except PolicyDenied as error:
            self.audit.append({"decision": "denied", "reason": str(error), "tool": action.get("action")})
            raise


def broad_manifest(task: dict) -> Manifest:
    """Permissive manifest used to isolate content taint from scope checks.

    Every tool the task offers is granted, every workspace resource is readable, and any
    address or URL that appears anywhere in the task is allowed. A block under this manifest
    can therefore only come from the exact-canary taint rule.
    """
    import json as _json

    blob = _json.dumps(task, ensure_ascii=False)
    emails = frozenset(re.findall(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob))
    urls = frozenset(re.findall(r"https?://[^\s\"'<>]+", blob))
    workspace = task.get("workspace", {})
    resources = frozenset(workspace.get("files", {})) | frozenset(workspace.get("emails", {}))
    return Manifest(frozenset(task["tools"]), resources, emails, urls, "broad-taint-isolation")
