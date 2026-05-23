import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_PATH = REPO_ROOT / "ops" / "webhook-receiver" / "webhook.yaml.example"
SECRET = "test-webhook-secret"


def _load_hook() -> dict[str, Any]:
    hooks = yaml.safe_load(HOOKS_PATH.read_text(encoding="utf-8"))
    assert isinstance(hooks, list)
    assert len(hooks) == 1
    return hooks[0]


def _payload_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _signature(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload_value(payload: dict[str, Any], name: str) -> Any:
    value: Any = payload
    for part in name.split("."):
        value = value[part]
    return value


def _match_rule(
    match: dict[str, Any],
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    body: bytes,
) -> bool:
    parameter = match["parameter"]
    source = parameter["source"]
    name = parameter["name"]
    match_type = match["type"]

    if source == "header":
        actual = headers.get(name)
    elif source == "payload":
        actual = _payload_value(payload, name)
    else:
        raise AssertionError(f"unexpected source: {source}")

    if match_type == "payload-hmac-sha256":
        expected = _signature(body)
        return hmac.compare_digest(expected, actual or "")

    if match_type == "value":
        return actual == match["value"]

    raise AssertionError(f"unexpected match type: {match_type}")


def _evaluate_rule(
    rule: dict[str, Any],
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    body: bytes,
) -> bool:
    if "and" in rule:
        return all(
            _evaluate_rule(child, headers=headers, payload=payload, body=body)
            for child in rule["and"]
        )
    if "match" in rule:
        return _match_rule(rule["match"], headers=headers, payload=payload, body=body)
    raise AssertionError(f"unexpected rule shape: {rule}")


def _accepted(headers: dict[str, str], payload: dict[str, Any], body: bytes) -> bool:
    hook = _load_hook()
    return _evaluate_rule(hook["trigger-rule"], headers=headers, payload=payload, body=body)


def test_webhook_config_accepts_signed_push_to_main() -> None:
    hook = _load_hook()
    payload = {"ref": "refs/heads/main"}
    body = _payload_body(payload)
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _signature(body),
    }

    assert hook["success-http-response-code"] == 202
    assert hook["http-methods"] == ["POST"]
    assert _accepted(headers, payload, body)


def test_webhook_config_rejects_bad_hmac_signature() -> None:
    payload = {"ref": "refs/heads/main"}
    body = _payload_body(payload)
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _signature(body, secret="wrong-secret"),
    }

    assert not _accepted(headers, payload, body)


def test_webhook_config_rejects_non_push_event() -> None:
    payload = {"ref": "refs/heads/main"}
    body = _payload_body(payload)
    headers = {
        "X-GitHub-Event": "ping",
        "X-Hub-Signature-256": _signature(body),
    }

    assert not _accepted(headers, payload, body)


def test_webhook_config_rejects_non_main_ref() -> None:
    payload = {"ref": "refs/heads/feature"}
    body = _payload_body(payload)
    headers = {
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": _signature(body),
    }

    assert not _accepted(headers, payload, body)
