"""Offline probe tests. No real HTTP client is permitted to send requests."""

import contextlib
import copy
import importlib.util
import io
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests


PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_claude_ai.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("claude_ai_probe_test_target", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(data, status=200):
    value = Mock(status_code=status)
    value.json.return_value = data
    value.text = "RAW-RESPONSE-CONTAINING-TEST-KEY"
    return value


class ClaudeAiProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe()
        self.env = {"GROUP_ANTHROPIC_API_KEY": "test-group-key", "ANTHROPIC_API_KEY": "test-fallback-key"}
        self.first = {
            "type": "message", "role": "assistant", "model": "claude-sonnet-5", "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "Fictional probe."},
                        {"type": "tool_use", "id": "toolu_opaque_123", "name": self.probe.TOOL_NAME,
                         "input": dict(self.probe.EXPECTED_ARGS)}],
            "usage": {"input_tokens": 110, "output_tokens": 35},
        }
        self.final = {
            "type": "message", "role": "assistant", "model": "claude-sonnet-5", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "ACK"}],
            "usage": {"input_tokens": 180, "output_tokens": 3},
        }
        self.session = Mock()
        self.session.post.side_effect = [response(self.first), response(self.final)]
        self.network_guard = patch("requests.sessions.Session.request", side_effect=AssertionError("Live network forbidden"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def run_mock(self):
        return self.probe.run_probe(self.env, self.session, live=True)

    def test_import_never_loads_app_requests_or_reads_environment(self):
        import builtins
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in ("app", "requests", "dotenv", "anthropic"):
                raise AssertionError("Unexpected import")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import), patch(
                "os.environ", new=Mock(get=Mock(side_effect=AssertionError("Environment read on import")))):
            load_probe()

    def test_explicit_opt_in_required_without_environment_or_http_access(self):
        environment = Mock(get=Mock(side_effect=AssertionError("Unexpected environment access")))
        result = self.probe.run_probe(environment, self.session)
        self.assertEqual(result["error_type"], "opt_in_required")
        self.session.post.assert_not_called()

    def test_two_rounds_preserve_content_and_id_with_fixed_local_echo(self):
        original = copy.deepcopy(self.first)
        result = self.run_mock()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_requested"], "claude-sonnet-5")
        self.assertEqual(result["model_returned"], ["claude-sonnet-5"] * 2)
        self.assertEqual(result["tokens"][0]["input_tokens"], 110)
        self.assertEqual(result["tool_name"], "echo_script_probe")
        self.assertEqual(set(result), self.probe.OUTPUT_FIELDS)
        self.assertEqual(self.session.post.call_count, 2)
        bodies = [call.kwargs["json"] for call in self.session.post.call_args_list]
        for call in self.session.post.call_args_list:
            self.assertEqual(call.args[0], "https://api.anthropic.com/v1/messages")
            self.assertEqual(call.kwargs["timeout"], (4, 20))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["x-api-key"], "test-group-key")
            self.assertEqual(call.kwargs["headers"]["anthropic-version"], "2023-06-01")
        for body in bodies:
            self.assertEqual(body["max_tokens"], 2048)
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual([tool["name"] for tool in body["tools"]], ["echo_script_probe"])
        self.assertEqual(bodies[0]["tool_choice"]["type"], "tool")
        self.assertEqual(bodies[1]["tool_choice"], {"type": "none"})
        self.assertIs(bodies[1]["messages"][1]["content"], self.first["content"])
        self.assertEqual(self.first, original)
        tool_result = bodies[1]["messages"][2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "toolu_opaque_123")
        self.assertEqual(json.loads(tool_result["content"]), {"ok": True, "echo": self.probe.EXPECTED_ARGS})

    def test_fallback_key_and_custom_model(self):
        self.env["GROUP_ANTHROPIC_API_KEY"] = " "
        self.env["GROUP_MODEL"] = "claude-sonnet-5-20260101"
        result = self.run_mock()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_requested"], self.env["GROUP_MODEL"])
        self.assertEqual(self.session.post.call_args_list[0].kwargs["headers"]["x-api-key"], "test-fallback-key")

    def test_bad_configuration_never_calls_http_and_never_echoes_values(self):
        cases = [({}, "missing_api_key"),
                 ({"GROUP_MODEL": "PRIVATE-KEY-VALUE"}, "invalid_model"),
                 ({"GROUP_ANTHROPIC_API_KEY": "PRIVATE\nKEY"}, "invalid_api_key")]
        for environment, expected in cases:
            result = self.probe.run_probe(environment, self.session, live=True)
            self.assertEqual(result["error_type"], expected)
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.session.post.assert_not_called()

    def test_wrong_or_multiple_tools_stop_without_second_request(self):
        cases = [[], [{"type": "tool_use", "name": "delete_notion_script"}],
                 [self.first["content"][1], copy.deepcopy(self.first["content"][1])]]
        for content in cases:
            first = {**self.first, "content": content}
            self.session.post.reset_mock()
            self.session.post.side_effect = [response(first)]
            result = self.run_mock()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.session.post.call_count, 1)

    def test_missing_blank_or_invalid_native_id_is_never_invented(self):
        for value in (None, "", "   ", 123, "x" * 513, "tool\n123"):
            first = copy.deepcopy(self.first)
            first["content"][1]["id"] = value
            self.session.post.reset_mock()
            self.session.post.side_effect = [response(first)]
            self.assertEqual(self.run_mock()["error_type"], "invalid_tool_id")
            self.assertEqual(self.session.post.call_count, 1)

    def test_tool_inputs_require_exact_fixture_and_types(self):
        cases = [None, "arguments", {**self.probe.EXPECTED_ARGS, "players": "6"},
                 {**self.probe.EXPECTED_ARGS, "players": 6.0},
                 {**self.probe.EXPECTED_ARGS, "unexpected": True},
                 {**self.probe.EXPECTED_ARGS, "title": "DIFFERENT"}]
        for values in cases:
            first = copy.deepcopy(self.first)
            first["content"][1]["input"] = values
            self.session.post.reset_mock()
            self.session.post.side_effect = [response(first)]
            self.assertEqual(self.run_mock()["error_type"], "invalid_tool_input")
            self.assertEqual(self.session.post.call_count, 1)

    def test_exact_ack_required_and_final_tool_request_rejected(self):
        cases = [[{"type": "text", "text": " ACK"}], [{"type": "text", "text": "ACK\n"}],
                 [{"type": "text", "text": "ACK and done"}], [self.first["content"][1]],
                 [{"type": "text", "text": "ACK"}, {"type": "text", "text": ""}]]
        for content in cases:
            self.session.post.reset_mock()
            self.session.post.side_effect = [response(self.first), response({**self.final, "content": content})]
            self.assertEqual(self.run_mock()["error_type"], "invalid_ack")
            self.assertEqual(self.session.post.call_count, 2)

    def test_http_error_never_reads_raw_error_body_or_retries(self):
        for status in (301, 400, 401, 403, 429, 500, 529):
            error_response = response({"error": "test-group-key"}, status)
            self.session.post.reset_mock()
            self.session.post.side_effect = [error_response]
            result = self.run_mock()
            self.assertEqual(result["error_status"], status)
            self.assertEqual(result["error_type"], "http_error")
            error_response.json.assert_not_called()
            self.assertEqual(self.session.post.call_count, 1)
            self.assertNotIn("test-group-key", json.dumps(result))

    def test_connection_errors_are_redacted_with_no_retry(self):
        for error, category in [(requests.Timeout("test-group-key"), "timeout"),
                                (requests.ConnectionError("test-group-key"), "connection_error"),
                                (requests.RequestException("test-group-key"), "request_error"),
                                (RuntimeError("test-group-key"), "unexpected_error")]:
            self.session.post.reset_mock()
            self.session.post.side_effect = error
            result = self.run_mock()
            self.assertEqual(result["error_type"], category)
            self.assertEqual(self.session.post.call_count, 1)
            self.assertNotIn("test-group-key", json.dumps(result))

    def test_invalid_json_is_redacted(self):
        invalid = response(None)
        invalid.json.side_effect = ValueError("test-group-key")
        self.session.post.side_effect = [invalid]
        result = self.run_mock()
        self.assertEqual(result["error_type"], "invalid_json")
        self.assertNotIn("test-group-key", json.dumps(result))

    def test_non_message_response_and_wrong_finish_reason_fail(self):
        for data in ([], {}, {**self.first, "role": "user"},
                     {**self.first, "stop_reason": "max_tokens"}):
            self.session.post.reset_mock()
            self.session.post.side_effect = [response(data)]
            self.assertEqual(self.run_mock()["status"], "failed")
            self.assertEqual(self.session.post.call_count, 1)

    def test_metadata_is_allowlisted_not_raw_response_content(self):
        first = copy.deepcopy(self.first)
        first["model"] = "PRIVATE-KEY"
        first["usage"] = {"input_tokens": "PRIVATE-KEY", "output_tokens": True, "raw": "PRIVATE-KEY"}
        first["private"] = "PRIVATE-KEY"
        first["content"][0]["text"] = "PRIVATE-KEY"
        self.session.post.side_effect = [response(first), response(self.final)]
        result = self.run_mock()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_returned"][0], "unavailable")
        self.assertIsNone(result["tokens"][0]["input_tokens"])
        self.assertIsNone(result["tokens"][0]["output_tokens"])
        self.assertNotIn("PRIVATE-KEY", json.dumps(result))

    def test_main_prints_only_json_and_suppresses_transport_output(self):
        values = iter([response(self.first), response(self.final)])

        def noisy_post(*args, **kwargs):
            print("test-group-key")
            print("test-group-key", file=self.probe.sys.stderr)
            logging.error("test-group-key")
            return next(values)

        self.session.post.side_effect = noisy_post
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.probe.main(["--live"], environ=self.env, session=self.session)
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertEqual(set(json.loads(stdout.getvalue())), self.probe.OUTPUT_FIELDS)
        self.assertNotIn("test-group-key", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_main_errors_are_nonzero_and_unknown_arguments_are_not_echoed(self):
        for arguments in ([], ["--api-key=PRIVATE-KEY"]):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = self.probe.main(arguments, environ=self.env, session=self.session)
            self.assertNotEqual(code, 0)
            self.assertNotIn("PRIVATE-KEY", stdout.getvalue())
        self.session.post.assert_not_called()

    def test_own_session_explicitly_disables_retries_and_environment_proxy(self):
        with patch.object(requests, "Session", return_value=self.session):
            result = self.probe.run_probe(self.env, live=True)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(self.session.trust_env)
        adapter = self.session.mount.call_args.args[1]
        self.assertEqual(adapter.max_retries.total, 0)
        self.session.close.assert_called_once()

    def test_followup_failure_keeps_first_round_metadata_and_stops(self):
        self.session.post.side_effect = [response(self.first), requests.Timeout("test-group-key")]
        result = self.run_mock()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "timeout")
        self.assertEqual(len(result["tokens"]), 1)
        self.assertEqual(self.session.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
