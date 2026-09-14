"""Opt-in, read-only Gemini function-call probe; never imports the bot app.

Running this file makes two paid Gemini requests. Importing it makes none.
Credentials come only from the process environment, never from project files.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import time


DEFAULT_MODEL = "gemini-3.1-pro-preview"
TOOL_NAME = "echo_script_probe"
EXPECTED_ARGS = {
    "title": "DIAGNOSTIC-FICTION-ALPHA",
    "players": 6,
    "duration_minutes": 90,
}
PROBE_PROMPT = (
    "This is a read-only diagnostic with fictional data. Call echo_script_probe "
    "exactly once with title DIAGNOSTIC-FICTION-ALPHA, players 6, and "
    "duration_minutes 90. After receiving its successful result, respond with "
    "exactly ACK and nothing else. Do not request another tool."
)


class ProbeFailure(Exception):
    """A failure whose public message is selected locally, not from the API."""


class _DiscardOutput(io.TextIOBase):
    def write(self, text):
        return len(text)

    def flush(self):
        pass


@contextlib.contextmanager
def _quiet_sdk_output():
    previous_disable_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(_DiscardOutput()), contextlib.redirect_stderr(
            _DiscardOutput()
        ):
            yield
    finally:
        logging.disable(previous_disable_level)


def _load_sdk():
    # Keep import safe for tests and tooling without an installed SDK or a key.
    from google import genai
    from google.genai import types

    return genai, types


def _model_name(value):
    if isinstance(value, str) and re.fullmatch(
        r"(?:models/)?gemini-[A-Za-z0-9._-]{1,100}", value
    ):
        return value
    return None


def _http_options(types):
    return types.HttpOptions(
        timeout=20_000,
        retry_options=types.HttpRetryOptions(attempts=1),
    )


def _config(types, mode):
    declaration = types.FunctionDeclaration(
        name=TOOL_NAME,
        description="Validate and echo fictional diagnostic fields; no side effects.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "players": {"type": "integer"},
                "duration_minutes": {"type": "integer"},
            },
            "required": ["title", "players", "duration_minutes"],
            "additionalProperties": False,
        },
    )
    return types.GenerateContentConfig(
        system_instruction=(
            "Use only the diagnostic echo tool. After its successful response, "
            "reply with exactly ACK. Never contact any other service."
        ),
        tools=[types.Tool(function_declarations=[declaration])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=mode)
        ),
        thinking_config=types.ThinkingConfig(thinking_level="low"),
        max_output_tokens=2048,
        http_options=_http_options(types),
    )


def _parts(response):
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return getattr(content, "parts", None) or []


def _function_calls(response):
    return [
        part.function_call
        for part in _parts(response)
        if getattr(part, "function_call", None) is not None
    ]


def _record_response(result, response):
    result["model_returned"].append(
        _model_name(getattr(response, "model_version", None)) or "unavailable"
    )
    usage = getattr(response, "usage_metadata", None)
    counts = {}
    for label, field in (
        ("input", "prompt_token_count"),
        ("output", "candidates_token_count"),
        ("thinking", "thoughts_token_count"),
        ("total", "total_token_count"),
    ):
        value = getattr(usage, field, None)
        counts[label] = value if type(value) is int and value >= 0 else None
    result["tokens"].append(counts)


def _error_code(error):
    value = getattr(error, "code", None)
    if type(value) is int and 100 <= value <= 599:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-5][0-9]{2}", value):
        return int(value)
    return None


def run_probe(environ=None):
    """Return only redacted diagnostics; perform no tool or external-service writes."""
    started = time.monotonic()
    result = {
        "status": "failed",
        "model_requested": DEFAULT_MODEL,
        "model_returned": [],
        "tokens": [],
        "tool_name": None,
        "roundtrip_seconds": 0.0,
    }
    client = None
    public_failure = "Diagnostic request failed."
    # SDK diagnostics may contain request contents or raw server errors. Only the
    # summary constructed here may reach stdout/stderr, including during close.
    with _quiet_sdk_output():
        try:
            environment = os.environ if environ is None else environ
            raw_model = environment.get("GROUP_MODEL", DEFAULT_MODEL)
            model = _model_name(raw_model)
            if not model:
                result["model_requested"] = "invalid"
                public_failure = "GROUP_MODEL must be a Gemini model identifier."
                raise ProbeFailure()
            result["model_requested"] = model
            key = environment.get("GROUP_GEMINI_KEY", "").strip() or environment.get(
                "GEMINI_API_KEY", ""
            ).strip()
            if not key:
                public_failure = "No group or fallback Gemini API key is configured."
                raise ProbeFailure()
            genai, types = _load_sdk()
            client = genai.Client(api_key=key, http_options=_http_options(types))
            session = client.chats.create(model=model, config=_config(types, "ANY"))
            response = session.send_message(PROBE_PROMPT)
            _record_response(result, response)
            calls = _function_calls(response)
            public_failure = "Expected exactly one native diagnostic function call."
            if len(calls) != 1 or calls[0].name != TOOL_NAME:
                raise ProbeFailure()
            call = calls[0]
            result["tool_name"] = TOOL_NAME
            public_failure = "Diagnostic function arguments did not match the fixture."
            if not isinstance(call.args, dict) or call.args != EXPECTED_ARGS:
                raise ProbeFailure()
            if any(type(call.args[name]) is not type(value) for name, value in EXPECTED_ARGS.items()):
                raise ProbeFailure()
            public_failure = "The native function call did not include a usable ID."
            native_id = getattr(call, "id", None)
            # 3.8 requires a call ID; 3.1 may omit it. Never invent one.
            requires_id = model.removeprefix("models/") == "gemini-3.8-flash"
            if (requires_id and native_id is None) or (native_id is not None and (
                not isinstance(native_id, str) or not native_id or len(native_id) > 512
            )):
                raise ProbeFailure()
            # The same SDK chat retains the unmodified model turn and signatures.
            # This is the only local "tool": equality checks plus a fixed echo.
            tool_result = types.Part(
                function_response=types.FunctionResponse(
                    id=native_id,
                    name=call.name,
                    response={"ok": True, "echo": dict(EXPECTED_ARGS)},
                )
            )
            public_failure = "Diagnostic follow-up request failed."
            final = session.send_message([tool_result], config=_config(types, "NONE"))
            _record_response(result, final)
            public_failure = "The model did not acknowledge the diagnostic result."
            if _function_calls(final) or (getattr(final, "text", None) or "").strip() != "ACK":
                raise ProbeFailure()
            result["status"] = "ok"
        except (Exception, KeyboardInterrupt) as error:
            error_type = type(error).__name__
            result["error_type"] = (
                error_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type) else "Error"
            )
            result["error_code"] = _error_code(error)
            result["message"] = (
                public_failure if isinstance(error, ProbeFailure) else "Gemini diagnostic request failed."
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
    result["roundtrip_seconds"] = round(max(0.0, time.monotonic() - started), 3)
    return result


def main():
    result = run_probe()
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
