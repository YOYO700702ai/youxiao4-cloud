"""Opt-in two-request Claude native-tool probe, with fictional data only.

Run with --live to permit two billable API requests. Importing or running without
--live makes no requests. Keys are read only from the process environment; this
module never imports app.py, reads .env files, or executes application tools.

The sole local tool validates and echoes a fixed fixture. Full assistant content
is retained for the continuation. Only allowlisted diagnostic metadata is output.
https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sys
import time


API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
TOOL_NAME = "echo_script_probe"
EXPECTED_ARGS = {
    "title": "DIAGNOSTIC-FICTION-ALPHA",
    "players": 6,
    "duration_minutes": 90,
}
PROBE_PROMPT = (
    "This is a diagnostic with fictional data only. Call echo_script_probe exactly "
    "once with title DIAGNOSTIC-FICTION-ALPHA, players 6, and duration_minutes 90. "
    "After receiving its successful result, respond with exactly ACK and nothing "
    "else. Never request another tool."
)
OUTPUT_FIELDS = frozenset({
    "status", "model_requested", "model_returned", "tokens", "tool_name",
    "roundtrip", "error_status", "error_type",
})
FAILURE_TYPES = frozenset({
    "opt_in_required", "invalid_arguments", "invalid_model", "missing_api_key",
    "invalid_api_key", "http_error", "invalid_json", "invalid_response",
    "invalid_tool_call", "invalid_tool_id", "invalid_tool_input", "invalid_ack",
    "timeout", "connection_error", "request_error", "interrupted", "unexpected_error",
})


class ProbeFailure(Exception):
    """Only locally selected, fixed error categories can reach the output."""

    def __init__(self, category, status=None):
        super().__init__()
        self.category = category if category in FAILURE_TYPES else "unexpected_error"
        self.status = status if type(status) is int and 100 <= status <= 599 else None


class _DiscardOutput(io.TextIOBase):
    def write(self, text):
        return len(text)

    def flush(self):
        pass


@contextlib.contextmanager
def _quiet_transport():
    # Includes imports, HTTP wire debug, response decoding, and session cleanup.
    previous_disable_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(_DiscardOutput()), contextlib.redirect_stderr(_DiscardOutput()):
            yield
    finally:
        logging.disable(previous_disable_level)


def _load_requests():
    import requests
    return requests


def _model_name(value):
    if isinstance(value, str) and re.fullmatch(r"claude-[A-Za-z0-9._-]{1,100}", value):
        return value
    return None


def _new_result():
    return {
        "status": "failed", "model_requested": DEFAULT_MODEL,
        "model_returned": [], "tokens": [], "tool_name": None,
        "roundtrip": 0.0, "error_status": None, "error_type": None,
    }


def _tool_definition():
    return {
        "name": TOOL_NAME,
        "description": "Validate and echo fictional diagnostic fields locally; no external effects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "players": {"type": "integer"},
                "duration_minutes": {"type": "integer"},
            },
            "required": ["title", "players", "duration_minutes"],
            "additionalProperties": False,
        },
    }


def _body(model, messages, *, first):
    return {
        "model": model,
        "max_tokens": 2048,
        "thinking": {"type": "disabled"},
        "system": "Use only the fictional diagnostic echo tool. After its successful result, reply exactly ACK.",
        "tools": [_tool_definition()],
        "tool_choice": ({"type": "tool", "name": TOOL_NAME, "disable_parallel_tool_use": True}
                        if first else {"type": "none"}),
        "messages": messages,
    }


def _post(session, headers, body):
    # Deliberately one call, no retry and no redirect to another endpoint.
    response = session.post(API_URL, headers=headers, json=body, timeout=(4, 20),
                            allow_redirects=False)
    if response.status_code != 200:
        # Never decode, print, or inspect an error response body or headers.
        raise ProbeFailure("http_error", response.status_code)
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise ProbeFailure("invalid_json") from None
    if (not isinstance(data, dict) or data.get("type") != "message"
            or data.get("role") != "assistant" or not isinstance(data.get("content"), list)
            or not data["content"] or not all(isinstance(item, dict) for item in data["content"])):
        raise ProbeFailure("invalid_response")
    return data


def _record_response(result, response):
    result["model_returned"].append(_model_name(response.get("model")) or "unavailable")
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    counts = {}
    for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(field)
        counts[field] = value if type(value) is int and 0 <= value <= 10**12 else None
    result["tokens"].append(counts)


def _validated_tool(response):
    calls = [item for item in response["content"] if item.get("type") == "tool_use"]
    if (response.get("stop_reason") != "tool_use" or len(calls) != 1
            or calls[0].get("name") != TOOL_NAME):
        raise ProbeFailure("invalid_tool_call")
    call = calls[0]
    native_id = call.get("id")
    if (not isinstance(native_id, str) or not native_id.strip() or len(native_id) > 512
            or any(ord(character) < 32 for character in native_id)):
        raise ProbeFailure("invalid_tool_id")
    values = call.get("input")
    if (not isinstance(values, dict) or values != EXPECTED_ARGS
            or any(type(values[key]) is not type(value) for key, value in EXPECTED_ARGS.items())):
        raise ProbeFailure("invalid_tool_input")
    return call


def run_probe(environ=None, session=None, *, live=False):
    """Return allowlisted metadata. Caller must opt in, including in mock tests."""
    result = _new_result()
    if live is not True:
        result["error_type"] = "opt_in_required"
        return result
    started = time.monotonic()
    transport = None
    requests_module = None
    owns_session = session is None
    with _quiet_transport():
        try:
            environment = os.environ if environ is None else environ
            model = _model_name(environment.get("GROUP_MODEL", DEFAULT_MODEL))
            if model is None:
                result["model_requested"] = "invalid"
                raise ProbeFailure("invalid_model")
            result["model_requested"] = model
            primary_key = environment.get("GROUP_ANTHROPIC_API_KEY", "")
            fallback_key = environment.get("ANTHROPIC_API_KEY", "")
            if not isinstance(primary_key, str) or not isinstance(fallback_key, str):
                raise ProbeFailure("invalid_api_key")
            key = primary_key.strip() or fallback_key.strip()
            if not key:
                raise ProbeFailure("missing_api_key")
            if any(character.isspace() or ord(character) < 32 for character in key):
                raise ProbeFailure("invalid_api_key")
            requests_module = _load_requests()
            transport = session if session is not None else requests_module.Session()
            if owns_session:
                transport.trust_env = False
                transport.mount("https://", requests_module.adapters.HTTPAdapter(max_retries=0))
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            initial_message = {"role": "user", "content": PROBE_PROMPT}
            first = _post(transport, headers, _body(model, [initial_message], first=True))
            _record_response(result, first)
            call = _validated_tool(first)
            result["tool_name"] = TOOL_NAME
            # This fixed echo is the only local tool action. Nothing is dispatched.
            echo = {"ok": True, "echo": dict(EXPECTED_ARGS)}
            continuation = [
                initial_message,
                {"role": "assistant", "content": first["content"]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call["id"],
                    "content": json.dumps(echo, ensure_ascii=True, separators=(",", ":")),
                    "is_error": False,
                }]},
            ]
            final = _post(transport, headers, _body(model, continuation, first=False))
            _record_response(result, final)
            content = final["content"]
            if (final.get("stop_reason") != "end_turn" or len(content) != 1
                    or content[0].get("type") != "text" or content[0].get("text") != "ACK"):
                raise ProbeFailure("invalid_ack")
            result["status"] = "ok"
        except (Exception, KeyboardInterrupt) as error:
            if isinstance(error, ProbeFailure):
                result["error_type"] = error.category
                result["error_status"] = error.status
            elif isinstance(error, KeyboardInterrupt):
                result["error_type"] = "interrupted"
            elif requests_module is not None and isinstance(error, requests_module.Timeout):
                result["error_type"] = "timeout"
            elif requests_module is not None and isinstance(error, requests_module.ConnectionError):
                result["error_type"] = "connection_error"
            elif requests_module is not None and isinstance(error, requests_module.RequestException):
                result["error_type"] = "request_error"
            else:
                result["error_type"] = "unexpected_error"
        finally:
            if owns_session and transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
    result["roundtrip"] = round(max(0.0, time.monotonic() - started), 3)
    return result


def main(argv=None, *, environ=None, session=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--live"]):
        result = _new_result()
        result["error_type"] = "invalid_arguments"
    else:
        result = run_probe(environ, session, live=arguments == ["--live"])
    print(json.dumps({key: result[key] for key in result if key in OUTPUT_FIELDS},
                     ensure_ascii=True, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
