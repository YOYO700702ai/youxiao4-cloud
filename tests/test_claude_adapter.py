"""Offline contract tests; no app import, credentials or external requests."""
import ast
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from google.genai import types

from claude_adapter import ClaudeAPIError, ClaudeClient, MESSAGES_URL, schema_to_json_schema


def reply(blocks=None, **overrides):
    value = {'id': 'msg_test', 'role': 'assistant', 'model': 'claude-sonnet-5',
             'content': blocks if blocks is not None else [{'type': 'text', 'text': '完成'}],
             'stop_reason': 'end_turn', 'usage': {'input_tokens': 10, 'output_tokens': 4}}
    value.update(overrides)
    response = Mock(status_code=200)
    response.json.return_value = value
    return response


def tool(identifier='tool_test', name='lookup', arguments=None):
    return {'type': 'tool_use', 'id': identifier, 'name': name,
            'input': {} if arguments is None else arguments}


def result(identifier='tool_test', value=None):
    return types.Part(function_response=types.FunctionResponse(
        id=identifier, name='lookup', response={'result': {'ok': True} if value is None else value}))


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch('socket.create_connection', side_effect=AssertionError('Network forbidden'))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.http = Mock()
        self.http.post.return_value = reply()
        self.client = ClaudeClient('synthetic-key', session=self.http)

    def payload(self):
        return self.http.post.call_args.kwargs['json']

    def test_official_request_and_google_response_contract(self):
        response = self.client.models.generate_content(contents='你好')
        self.assertIsInstance(response, types.GenerateContentResponse)
        self.assertEqual(response.text, '完成')
        self.assertEqual(response.candidates[0].content.role, 'model')
        self.assertEqual(response.model_version, 'claude-sonnet-5')
        self.assertEqual(response.usage_metadata.total_token_count, 14)
        self.assertEqual(self.http.post.call_args.args, (MESSAGES_URL,))
        options = self.http.post.call_args.kwargs
        self.assertEqual(options['headers']['anthropic-version'], '2023-06-01')
        self.assertEqual(options['headers']['x-api-key'], 'synthetic-key')
        self.assertEqual(options['timeout'], (4, 20))
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(self.payload()['thinking'], {'type': 'disabled'})
        self.assertEqual(self.payload()['max_tokens'], 8192)

    def test_schema_nullable_enum_arrays_empty_values_and_provider_fields(self):
        schema = types.Schema(type='OBJECT', properties={
            'name': types.Schema(type='STRING', nullable=True, enum=['a', 'b']),
            'tags': types.Schema(type='ARRAY', items=types.Schema(type='STRING')),
            'price': types.Schema(type='INTEGER', minimum=0)}, required=['name'],
            property_ordering=['price', 'name', 'tags'])
        converted = schema_to_json_schema(schema)
        self.assertEqual(converted['type'], 'object')
        self.assertEqual(converted['required'], ['name'])
        self.assertEqual(converted['properties']['name']['anyOf'],
                         [{'enum': ['a', 'b'], 'type': 'string'}, {'type': 'null'}])
        self.assertEqual(converted['properties']['tags']['items'], {'type': 'string'})
        self.assertEqual(converted['properties']['price']['minimum'], 0)
        self.assertNotIn('property_ordering', converted)
        self.assertEqual(schema_to_json_schema({'type': 'object', 'properties': {},
                         'additionalProperties': False, 'required': []}),
                         {'type': 'object', 'properties': {}, 'additionalProperties': False, 'required': []})

    def test_schema_recursive_combinators(self):
        schema = {'type': 'OBJECT', 'properties': {'x': {'any_of': [
            {'type': 'STRING', 'min_length': 1}, {'type': 'INTEGER'}]}}, 'unknownGoogleKey': 9}
        converted = schema_to_json_schema(schema)
        self.assertEqual(converted['properties']['x']['anyOf'][0], {'type': 'string', 'minLength': 1})
        self.assertNotIn('unknownGoogleKey', converted)

    def test_declarations_and_system_instruction(self):
        declaration = types.FunctionDeclaration(name='lookup', description='查詢',
            parameters=types.Schema(type='OBJECT', properties={'name': types.Schema(type='STRING')}, required=['name']))
        config = types.GenerateContentConfig(system_instruction='繁體中文',
            tools=[types.Tool(function_declarations=[declaration])], max_output_tokens=500,
            thinking_config=types.ThinkingConfig(thinking_level='low'),
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode='VALIDATED')))
        self.client.models.generate_content(model='custom-model', contents='查詢', config=config)
        self.assertEqual(self.payload()['model'], 'custom-model')
        self.assertEqual(self.payload()['system'], '繁體中文')
        self.assertEqual(self.payload()['tools'][0]['input_schema']['properties']['name']['type'], 'string')
        self.assertEqual(self.payload()['tool_choice'], {'type': 'auto'})
        self.assertNotIn('thinking_config', self.payload())
        self.assertEqual(self.payload()['max_tokens'], 500)

    def test_parameters_json_schema_and_parameterless_function(self):
        config = {'tools': [{'function_declarations': [
            {'name': 'ping'}, {'name': 'other', 'parameters_json_schema': {'type': 'object', 'properties': {}}}]}]}
        self.client.models.generate_content(contents='ping', config=config)
        self.assertEqual(len(self.payload()['tools']), 2)
        self.assertEqual(self.payload()['tools'][0]['input_schema'], {'type': 'object', 'properties': {}})

    def test_json_mode_adds_instruction_without_claiming_strict_schema(self):
        self.client.models.generate_content(contents='摘要', config={'response_mime_type': 'application/json'})
        self.assertIn('Return only valid JSON', self.payload()['system'])
        self.assertNotIn('response_mime_type', self.payload())

    def test_images_detect_magic_instead_of_claimed_mime(self):
        fixtures = [(b'\x89PNG\r\n\x1a\nimage', 'image/png'),
                    (b'\xff\xd8\xffimage', 'image/jpeg'),
                    (b'GIF89aimage', 'image/gif'),
                    (b'RIFF1234WEBPimage', 'image/webp')]
        for data, expected in fixtures:
            with self.subTest(expected=expected):
                self.client.models.generate_content(contents=[
                    types.Part.from_bytes(data=data, mime_type='image/jpeg'), types.Part(text='描述')])
                blocks = self.payload()['messages'][0]['content']
                self.assertEqual(blocks[0]['source']['media_type'], expected)
                self.assertEqual(blocks[1]['text'], '描述')

    def test_unknown_image_rejected_before_request(self):
        with self.assertRaisesRegex(ClaudeAPIError, 'unsupported_image_format'):
            self.client.models.generate_content(contents=types.Part.from_bytes(data=b'not-an-image', mime_type='image/jpeg'))
        self.http.post.assert_not_called()

    def test_native_tool_cycle_preserves_id_and_full_assistant_blocks(self):
        blocks = [{'type': 'thinking', 'thinking': 'opaque reasoning', 'signature': 'signed-test'},
                  {'type': 'redacted_thinking', 'data': 'opaque-test'},
                  tool(arguments={'name': '星空', 'clear': None, 'empty': []})]
        self.http.post.side_effect = [reply(blocks, stop_reason='tool_use'), reply()]
        chat = self.client.chats.create()
        first = chat.send_message('查詢')
        call = first.candidates[0].content.parts[0].function_call
        self.assertEqual(call.id, 'tool_test')
        self.assertIsNone(call.args['clear'])
        self.assertEqual(call.args['empty'], [])
        second = chat.send_message(result(call.id))
        self.assertEqual(second.text, '完成')
        sent = self.payload()['messages']
        self.assertEqual(sent[1]['content'], blocks)
        self.assertEqual(sent[2]['content'][0]['tool_use_id'], 'tool_test')
        self.assertEqual(json.loads(sent[2]['content'][0]['content']), {'result': {'ok': True}})

    def test_get_history_is_snapshot_and_can_recreate_signed_history(self):
        blocks = [{'type': 'thinking', 'thinking': 'test', 'signature': 'signed-test'}, tool()]
        self.http.post.return_value = reply(blocks, stop_reason='tool_use')
        chat = self.client.chats.create()
        chat.send_message('查詢')
        snapshot = chat.get_history(curated=True)
        self.assertEqual([item.role for item in snapshot], ['user', 'model'])
        restored = self.client.chats.create(history=snapshot)
        snapshot[1].blocks[0]['signature'] = 'changed'
        self.http.post.return_value = reply()
        restored.send_message(result())
        self.assertEqual(self.payload()['messages'][1]['content'], blocks)
        self.assertEqual(chat.get_history()[1].blocks[0]['signature'], 'signed-test')

    def test_missing_or_unmatched_tool_result_id_fails_before_request(self):
        for identifier in (None, 'wrong'):
            with self.subTest(identifier=identifier):
                self.http.post.reset_mock()
                with self.assertRaises(ClaudeAPIError):
                    self.client.chats.create().send_message(result(identifier))
                self.http.post.assert_not_called()

    def test_parallel_tools_require_complete_results_in_order_before_text(self):
        self.http.post.return_value = reply([tool('a'), tool('b')], stop_reason='tool_use')
        chat = self.client.chats.create()
        chat.send_message('兩項')
        with self.assertRaisesRegex(ClaudeAPIError, 'unmatched_tool_results'):
            chat.send_message([result('a')])
        with self.assertRaisesRegex(ClaudeAPIError, 'tool_result_order'):
            chat.send_message([types.Part(text='hi'), result('a'), result('b')])
        self.http.post.return_value = reply()
        chat.send_message([result('a'), result('b'), types.Part(text='結果')])
        self.assertEqual(len(self.payload()['messages'][-1]['content']), 3)

    def test_error_tool_results_marked(self):
        self.http.post.return_value = reply([tool()], stop_reason='tool_use')
        chat = self.client.chats.create()
        chat.send_message('查詢')
        self.http.post.return_value = reply()
        chat.send_message(result(value={'ok': False, 'message': '失敗'}))
        self.assertTrue(self.payload()['messages'][-1]['content'][0]['is_error'])

    def test_timeout_does_not_commit_or_retry_and_exception_is_safe(self):
        chat = self.client.chats.create()
        self.http.post.side_effect = requests.Timeout('secret url synthetic-key body')
        with self.assertRaises(ClaudeAPIError) as caught:
            chat.send_message('private text')
        self.assertEqual(str(caught.exception), 'Claude API: timeout')
        self.assertEqual(chat.get_history(), [])
        self.assertEqual(self.http.post.call_count, 1)
        self.http.post.side_effect = None
        chat.send_message('private text')
        self.assertEqual(len(self.payload()['messages']), 1)

    def test_tool_followup_failure_does_not_duplicate_result_on_retry(self):
        self.http.post.return_value = reply([tool()], stop_reason='tool_use')
        chat = self.client.chats.create()
        chat.send_message('查詢')
        self.http.post.side_effect = requests.ConnectionError('secret')
        with self.assertRaises(ClaudeAPIError):
            chat.send_message(result())
        self.assertEqual(len(chat.get_history()), 2)
        self.http.post.side_effect = None
        self.http.post.return_value = reply()
        chat.send_message(result())
        self.assertEqual(len(self.payload()['messages']), 3)

    def test_http_errors_never_read_or_reveal_body_or_retry(self):
        for status in (301, 400, 401, 403, 429, 500, 529):
            with self.subTest(status=status):
                self.http.post.reset_mock()
                response = Mock(status_code=status, text='secret synthetic-key')
                self.http.post.return_value = response
                with self.assertRaises(ClaudeAPIError) as caught:
                    self.client.models.generate_content(contents='hi')
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn('synthetic-key', str(caught.exception))
                self.assertEqual(self.http.post.call_count, 1)
                response.json.assert_not_called()

    def test_invalid_json_or_response_leaves_history_unchanged(self):
        for bad in (None, {}, {'role': 'assistant', 'content': ['bad']}):
            with self.subTest(bad=bad):
                self.http.post.return_value = Mock(status_code=200)
                self.http.post.return_value.json.return_value = bad
                chat = self.client.chats.create()
                with self.assertRaises(ClaudeAPIError):
                    chat.send_message('hi')
                self.assertEqual(chat.get_history(), [])

    def test_missing_duplicate_or_truncated_tool_calls_are_not_exposed(self):
        cases = [([tool(identifier=None)], 'tool_use'),
                 ([tool(), tool()], 'tool_use'), ([tool()], 'max_tokens'),
                 ([tool(arguments='not-dict')], 'tool_use')]
        for blocks, stop in cases:
            with self.subTest(stop=stop, blocks=blocks):
                self.http.post.return_value = reply(blocks, stop_reason=stop)
                chat = self.client.chats.create()
                with self.assertRaises(ClaudeAPIError):
                    chat.send_message('hi')
                self.assertEqual(chat.get_history(), [])

    def test_usage_includes_cache_read_and_creation(self):
        self.http.post.return_value = reply(usage={'input_tokens': 10, 'output_tokens': 4,
            'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 30})
        usage = self.client.models.generate_content(contents='hi').usage_metadata
        self.assertEqual(usage.prompt_token_count, 60)
        self.assertEqual(usage.total_token_count, 64)
        self.assertEqual(usage.cached_content_token_count, 20)

    def test_per_message_config_overrides_without_changing_session_defaults(self):
        chat = self.client.chats.create(config={'max_output_tokens': 80, 'system_instruction': '常駐'})
        chat.send_message('one', config={'max_output_tokens': 90})
        self.assertEqual(self.payload()['max_tokens'], 90)
        self.assertEqual(self.payload()['system'], '常駐')
        chat.send_message('two')
        self.assertEqual(self.payload()['max_tokens'], 80)

    def test_google_content_history_and_system_content(self):
        history = [types.Content(role='user', parts=[types.Part(text='first')]),
                   types.Content(role='model', parts=[types.Part(text='answer')])]
        chat = self.client.chats.create(history=history, config=types.GenerateContentConfig(
            system_instruction=types.Content(parts=[types.Part(text='繁體中文')])) )
        chat.send_message('next')
        self.assertEqual([m['role'] for m in self.payload()['messages']], ['user', 'assistant', 'user'])
        self.assertEqual(self.payload()['system'], '繁體中文')

    def test_missing_key_and_unsupported_tools_safe_error(self):
        with self.assertRaisesRegex(ClaudeAPIError, 'missing_api_key'):
            ClaudeClient('')
        with self.assertRaisesRegex(ClaudeAPIError, 'unsupported_tool'):
            self.client.models.generate_content(contents='hi', config={'tools': [{'google_search': {}}]})
        self.http.post.assert_not_called()

    def test_all_actual_group_declarations_translate_without_app_startup(self):
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        declaration = next(node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'GROUP_FUNC_DECLS' for target in node.targets))
        scope = {'types': types}
        exec(compile(ast.Module(body=[declaration], type_ignores=[]), '<group-schema>', 'exec'), scope)
        functions = scope['GROUP_FUNC_DECLS']
        config = types.GenerateContentConfig(tools=[types.Tool(function_declarations=functions)])
        self.client.models.generate_content(contents='查詢', config=config)
        self.assertEqual(len(self.payload()['tools']), len(functions))
        self.assertTrue(all(item['input_schema']['type'] == 'object' for item in self.payload()['tools']))
        self.assertIn('upload_script', {item['name'] for item in self.payload()['tools']})

    def test_same_chat_concurrent_requests_serialize_and_keep_history(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def post(*args, **kwargs):
            if kwargs['json']['messages'][0]['content'][0]['text'] == 'first' and len(kwargs['json']['messages']) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test release timeout')
            return reply()
        self.http.post.side_effect = post
        chat = self.client.chats.create()
        def send(text, start=None):
            try:
                if start is not None:
                    start.set()
                chat.send_message(text)
            except BaseException as error:
                errors.append(error)
        first = threading.Thread(target=send, args=('first',))
        second = threading.Thread(target=send, args=('second', second_started))
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertTrue(second_started.wait(1))
            self.assertEqual(self.http.post.call_count, 1)
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(errors)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(chat.get_history()), 4)
        self.assertEqual(len(self.payload()['messages']), 3)

    def test_zero_token_limit_and_malformed_json_fail_safely(self):
        with self.assertRaisesRegex(ClaudeAPIError, 'invalid_max_tokens'):
            self.client.models.generate_content(contents='hi', config={'max_output_tokens': 0})
        self.http.post.assert_not_called()
        self.http.post.return_value.json.side_effect = ValueError('secret body synthetic-key')
        with self.assertRaises(ClaudeAPIError) as caught:
            self.client.models.generate_content(contents='hi')
        self.assertEqual(str(caught.exception), 'Claude API: invalid_response')


if __name__ == '__main__':
    unittest.main()
