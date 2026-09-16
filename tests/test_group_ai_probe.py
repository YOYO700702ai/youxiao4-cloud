"""Offline tests only: every SDK client and response is a test double."""

import contextlib
import importlib.util
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_group_ai.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("group_ai_probe_test_target", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(**kwargs):
    return SimpleNamespace(**kwargs)


FAKE_TYPES = SimpleNamespace(**{
    name: record
    for name in (
        "HttpOptions", "HttpRetryOptions", "FunctionDeclaration", "GenerateContentConfig",
        "Tool", "AutomaticFunctionCallingConfig", "ToolConfig", "FunctionCallingConfig",
        "ThinkingConfig", "Part", "FunctionResponse",
    )
})


def response(parts=None, text=None, version="gemini-3.8-flash", usage=True):
    return record(
        candidates=[record(content=record(parts=parts or []))],
        text=text,
        model_version=version,
        usage_metadata=record(
            prompt_token_count=100,
            candidates_token_count=12,
            thoughts_token_count=20,
            total_token_count=132,
        ) if usage else None,
    )


class GroupAiProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe()
        self.env = {"GROUP_GEMINI_KEY": "test-group-key", "GEMINI_API_KEY": "test-fallback-key"}
        self.call = record(
            id="call-opaque-1", name=self.probe.TOOL_NAME, args=dict(self.probe.EXPECTED_ARGS)
        )
        self.original_part = record(function_call=self.call, thought_signature=b"opaque-signature")
        self.first = response([self.original_part])
        self.final = response(text="ACK")
        self.session = Mock()
        self.session.send_message.side_effect = [self.first, self.final]
        self.client = Mock()
        self.client.chats.create.return_value = self.session
        self.sdk = record(Client=Mock(return_value=self.client))

    def run_mock(self):
        with patch.object(self.probe, "_load_sdk", return_value=(self.sdk, FAKE_TYPES)):
            return self.probe.run_probe(self.env)

    def test_import_has_no_sdk_import_or_environment_access(self):
        import builtins
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "app" or name == "google" or name.startswith("google."):
                raise AssertionError("Import must not load app or SDK")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import), patch(
            "os.environ", new=Mock(get=Mock(side_effect=AssertionError("Environment read on import")))):
            load_probe()

    def test_success_uses_one_chat_two_calls_and_preserves_native_id(self):
        result = self.run_mock()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_requested"], "gemini-3.8-flash")
        self.assertEqual(result["model_returned"], ["gemini-3.8-flash"] * 2)
        self.assertEqual(result["tokens"][0]["total"], 132)
        self.assertEqual(self.session.send_message.call_count, 2)
        self.client.chats.create.assert_called_once()
        tool_part = self.session.send_message.call_args_list[1].args[0][0]
        self.assertEqual(tool_part.function_response.id, self.call.id)
        self.assertEqual(tool_part.function_response.name, self.call.name)
        self.assertEqual(tool_part.function_response.response["echo"], self.probe.EXPECTED_ARGS)
        self.assertIs(self.first.candidates[0].content.parts[0], self.original_part)
        self.assertEqual(self.original_part.thought_signature, b"opaque-signature")
        self.client.close.assert_called_once()

    def test_configs_bound_requests_and_disable_automatic_execution(self):
        self.run_mock()
        first_config = self.client.chats.create.call_args.kwargs["config"]
        final_config = self.session.send_message.call_args_list[1].kwargs["config"]
        self.assertEqual(first_config.tool_config.function_calling_config.mode, "ANY")
        self.assertEqual(final_config.tool_config.function_calling_config.mode, "NONE")
        for config in (first_config, final_config):
            self.assertTrue(config.automatic_function_calling.disable)
            self.assertEqual(config.thinking_config.thinking_level, "low")
            self.assertEqual(config.max_output_tokens, 2048)
            self.assertEqual(config.http_options.timeout, 20_000)
            self.assertEqual(config.http_options.retry_options.attempts, 1)
            for unsupported in ("temperature", "top_p", "top_k", "candidate_count", "thinking_budget"):
                self.assertFalse(hasattr(config, unsupported))
            self.assertEqual(config.tools[0].function_declarations[0].name, self.probe.TOOL_NAME)

    def test_key_precedence_fallback_and_model_override(self):
        self.env["GROUP_MODEL"] = "gemini-3.8-flash-test"
        result = self.run_mock()
        self.assertEqual(self.sdk.Client.call_args.kwargs["api_key"], "test-group-key")
        self.assertEqual(result["model_requested"], self.env["GROUP_MODEL"])
        self.env["GROUP_GEMINI_KEY"] = " "
        self.session.send_message.side_effect = [self.first, self.final]
        self.run_mock()
        self.assertEqual(self.sdk.Client.call_args.kwargs["api_key"], "test-fallback-key")

    def test_missing_key_or_invalid_model_fails_before_sdk_load(self):
        for environment in ({}, {"GROUP_GEMINI_KEY": "key", "GROUP_MODEL": "unsafe secret text"}):
            with self.subTest(environment=bool(environment)), patch.object(self.probe, "_load_sdk") as loader:
                result = self.probe.run_probe(environment)
                self.assertEqual(result["status"], "failed")
                loader.assert_not_called()
                self.assertNotIn("unsafe secret text", json.dumps(result))

    def test_invalid_calls_stop_before_followup(self):
        cases = [
            response(text="echo_script_probe(...)"),
            response([self.original_part, self.original_part]),
            response([record(function_call=record(id="id", name="write_notion", args={}))]),
            response([record(function_call=record(id="", name=self.probe.TOOL_NAME, args=self.probe.EXPECTED_ARGS))]),
            response([record(function_call=record(id="id", name=self.probe.TOOL_NAME, args={"title": "wrong"}))]),
        ]
        for invalid in cases:
            with self.subTest(parts=len(invalid.candidates[0].content.parts)):
                self.session.send_message.reset_mock()
                self.session.send_message.side_effect = [invalid]
                result = self.run_mock()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(self.session.send_message.call_count, 1)

    def test_pro31_omitted_call_id_is_preserved_without_inventing_one(self):
        self.env["GROUP_MODEL"] = "gemini-3.1-pro-preview"
        self.first.model_version = self.final.model_version = self.env["GROUP_MODEL"]
        self.call.id = None
        self.assertEqual(self.run_mock()["status"], "ok")
        tool_part = self.session.send_message.call_args_list[1].args[0][0]
        self.assertIsNone(tool_part.function_response.id)
        self.assertEqual(tool_part.function_response.name, self.probe.TOOL_NAME)

    def test_flash38_still_requires_a_native_call_id(self):
        self.env["GROUP_MODEL"] = "gemini-3.8-flash"
        self.call.id = None
        self.assertEqual(self.run_mock()["status"], "failed")
        self.assertEqual(self.session.send_message.call_count, 1)

    def test_final_ack_required_and_no_extra_native_call_accepted(self):
        for final in (response(text="Done"), response([self.original_part], text="ACK")):
            with self.subTest(text=final.text):
                self.session.send_message.side_effect = [self.first, final]
                self.assertEqual(self.run_mock()["status"], "failed")

    def test_raw_errors_and_sdk_output_never_escape(self):
        class ApiError(Exception):
            code = 429

        raw_secret = "test-group-key RAW_PRIVATE_PROMPT RAW_SERVER_BODY"
        log_output = io.StringIO()
        logger = logging.getLogger("group-ai-probe-offline-test")
        handler = logging.StreamHandler(log_output)
        logger.addHandler(handler)
        previous_disable_level = logging.root.manager.disable

        def fail(*args, **kwargs):
            import sys
            print(raw_secret)
            print(raw_secret, file=sys.stderr)
            logger.error(raw_secret)
            raise ApiError(raw_secret)

        self.session.send_message.side_effect = fail
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = self.run_mock()
        finally:
            logger.removeHandler(handler)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(log_output.getvalue(), "")
        self.assertEqual(logging.root.manager.disable, previous_disable_level)
        self.assertEqual(result["error_type"], "ApiError")
        self.assertEqual(result["error_code"], 429)
        self.assertNotIn(raw_secret, json.dumps(result))
        self.assertEqual(self.session.send_message.call_count, 1)

    def test_returned_metadata_is_allowlisted_and_missing_usage_is_unknown(self):
        self.first.model_version = "test-group-key RAW_SECRET"
        self.first.usage_metadata = None
        result = self.run_mock()
        self.assertEqual(result["model_returned"][0], "unavailable")
        self.assertTrue(all(value is None for value in result["tokens"][0].values()))
        self.assertNotIn("RAW_SECRET", json.dumps(result))

    def test_main_emits_one_json_summary_and_nonzero_on_failure(self):
        for status, expected_exit in (("ok", 0), ("failed", 1)):
            stdout = io.StringIO()
            with patch.object(self.probe, "run_probe", return_value={"status": status}), contextlib.redirect_stdout(stdout):
                exit_code = self.probe.main()
            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(json.loads(stdout.getvalue()), {"status": status})
            self.assertEqual(len(stdout.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
