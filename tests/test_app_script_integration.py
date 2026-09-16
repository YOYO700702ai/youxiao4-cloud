"""Exercise actual app handlers without importing startup jobs or credentials."""
import ast
import base64
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, MagicMock, patch

import requests
from google.genai import types
from linebot.v3.messaging import TextMessage, ReplyMessageRequest, PushMessageRequest
from linebot.v3.messaging import MessagingApi
from apscheduler.schedulers.background import BackgroundScheduler

from script_workflow import SCRIPT_TOOLS, result_text


TREE = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))


def load_functions(names, env):
    selected = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    assert {node.name for node in selected} == set(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<app-functions>', 'exec'), env)
    return env


def response_with_calls(*calls):
    parts = [NS(function_call=NS(name=name, args=args), text=None) for name, args in calls]
    return NS(candidates=[NS(content=NS(parts=parts))], text='不應使用這句 AI 收尾')


class AppScriptTests(unittest.TestCase):
    def setUp(self):
        self.workflow = Mock()
        self.outcome = {'ok': True, 'message': '已新增到 Notion https://www.notion.so/test'}
        self.workflow.start.return_value = self.outcome
        self.session = Mock()
        self.env = dict(
            __builtins__=__builtins__, SCRIPT_TOOLS=SCRIPT_TOOLS, types=types,
            re=re, json=json, datetime=datetime, time=Mock(),
            ALLOWED_GROUP_IDS={'group'}, GROUP_BOT_USER_ID='bot',
            GROUP_CHAT_LOG_MAX=20, group_chat_log={}, group_bot_msg_ids=set(),
            pending_group_image={}, script_workflow=self.workflow,
            get_member_name=Mock(return_value='測試使用者'), append_chat_buffer=Mock(),
            new_group_tool_session=Mock(return_value=self.session),
            group_reply=Mock(), notify_script_result=Mock(),
            parse_leaked_tool_calls=Mock(return_value=[]),
            result_text=result_text,
        )
        load_functions(['execute_group_function', 'group_handle_message', 'group_handle_image',
                        'function_response_part', 'is_script_intent'], self.env)
        self.event = NS(source=NS(group_id='group', user_id='user'), reply_token='reply',
                        message=NS(text='小六 上架《測試劇本》', id='message'))

    def handle(self, response):
        # A second model call after writing would fail; it must never happen.
        self.session.send_message.side_effect = [response, RuntimeError('model unavailable')]
        self.env['group_handle_message'](self.event)

    def test_native_upload_uses_structured_fields_without_second_ai(self):
        data = {'名稱': '測試劇本', '簡介': '包含(括號)以及單引號\'的完整內容'}
        self.handle(response_with_calls(('upload_script', {'data': data})))
        self.workflow.start.assert_called_once_with(('group', 'user'), 'upload', data)
        self.assertEqual(self.session.send_message.call_count, 1)
        self.env['get_member_name'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()
        self.env['new_group_tool_session'].assert_called_once_with('group', include_memory=False)
        self.env['notify_script_result'].assert_called_once_with('group', 'reply', [self.outcome])

    def test_repeated_native_call_is_only_executed_once(self):
        call = ('upload_script', {'data': {'名稱': '測試劇本'}})
        self.handle(response_with_calls(call, call))
        self.workflow.start.assert_called_once()

    def test_add_script_synonym_does_not_consume_cover_as_chat(self):
        self.event.message.text = '小六新增劇本《測試劇本》'
        self.env['pending_group_image'][('group', 'user')] = ('image', 1)
        self.handle(response_with_calls(('upload_script', {'data': {'名稱': '測試劇本'}})))
        self.workflow.take_chat_image.assert_not_called()
        self.workflow.start.assert_called_once()

    def test_mixed_script_and_team_receipt_uses_protected_delivery(self):
        outcome = self.outcome
        event_info = {'announce_msg_ids': []}
        def execute(name, args, gid, pending, uid):
            if name == 'create_team':
                pending['signup'] = {'event': event_info, 'row_num': 5}
            return outcome
        self.env.update(execute_group_function=execute, format_signup_sheet=Mock(return_value='報名表'),
                        strip_leaked_tool_calls=lambda s: s, trim_group_session_history=Mock(),
                        TextMessage=TextMessage, deliver_script_messages=Mock(return_value=['sent']),
                        save_group_event=Mock())
        self.handle(response_with_calls(('create_team', {}), ('upload_script', {'data': {'名稱': '測試劇本'}})))
        self.env['deliver_script_messages'].assert_called_once()
        self.assertEqual(event_info['announce_msg_ids'], ['sent'])
        texts = [m.text for m in self.env['deliver_script_messages'].call_args.args[2]]
        self.assertIn('報名表', texts[0])
        self.assertTrue(any('Notion' in t for t in texts))

    def test_update_result_reaches_line_without_model_summary(self):
        self.env['update_notion_script'] = Mock(return_value=(True, '價格已更新'))
        self.handle(response_with_calls(('update_script', {'name': '測試劇本', 'new_price': 800})))
        self.env['update_notion_script'].assert_called_once_with('測試劇本', {'價格': 800})
        outcomes = self.env['notify_script_result'].call_args.args[2]
        self.assertEqual(outcomes, [{'ok': True, 'message': '價格已更新'}])
        self.workflow.remember_result.assert_called_once()

    def test_explicit_empty_update_fields_reach_notion(self):
        self.env['update_notion_script'] = Mock(return_value=(True, '已清空指定欄位'))
        self.handle(response_with_calls(('update_script', {'name': '測試劇本', 'new_summary': '', 'new_roles': None})))
        self.env['update_notion_script'].assert_called_once_with('測試劇本', {'劇情簡介': '', '角色': None})

    def test_legacy_string_does_not_call_hidden_gemma_or_write(self):
        self.env['parse_script_info_with_ai'] = Mock(side_effect=AssertionError('unexpected hidden AI'))
        res = self.env['execute_group_function']('upload_script', {'data': '名稱：測試劇本'}, 'group', {}, 'user')
        self.assertFalse(res['ok'])
        self.workflow.start.assert_not_called()
        self.env['parse_script_info_with_ai'].assert_not_called()

    def test_model_claim_without_native_tool_is_reported_as_not_executed(self):
        res = NS(candidates=[NS(content=NS(parts=[NS(text='已上架完成！', function_call=None)]))])
        self.handle(res)
        self.workflow.start.assert_not_called()
        self.assertFalse(self.env['notify_script_result'].call_args.args[2][0]['ok'])

    def test_model_timeout_stops_without_writes_or_unbounded_retry(self):
        self.session.send_message.side_effect = TimeoutError('test')
        self.env['group_handle_message'](self.event)
        self.assertEqual(self.session.send_message.call_count, 2)
        self.workflow.start.assert_not_called()
        self.assertIn('沒有執行劇本寫入', self.env['group_reply'].call_args.args[1])

    def test_status_command_bypasses_model_and_sheets(self):
        self.event.message.text = '上架狀態'
        self.workflow.status.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.workflow.status.assert_called_once_with(('group', 'user'))
        self.env['new_group_tool_session'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()

    def test_image_uses_workflow_and_deterministic_receipt(self):
        self.workflow.receive_image.return_value = self.outcome
        self.env['group_handle_image'](self.event)
        self.workflow.receive_image.assert_called_once_with(('group', 'user'), 'message')
        self.env['notify_script_result'].assert_called_once_with('group', 'reply', self.outcome)

    def test_sdk_accepts_nested_group_upload_schema(self):
        node = next(n for n in TREE.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'GROUP_FUNC_DECLS' for t in n.targets))
        env = {'types': types}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<tool-schema>', 'exec'), env)
        declaration = next(d for d in env['GROUP_FUNC_DECLS'] if d.name == 'upload_script')
        self.assertEqual(declaration.parameters.properties['data'].type, types.Type.OBJECT)
        self.assertIn('名稱', declaration.parameters.properties['data'].required)


class DeliveryAndCoverTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.push_message.return_value = NS(sent_messages=[NS(id='pushed')])
        context = MagicMock()
        self.env = dict(__builtins__=__builtins__,
                        ApiClient=Mock(return_value=context),
                        MessagingApi=Mock(return_value=self.api), group_configuration=object(),
                        TextMessage=TextMessage, ReplyMessageRequest=ReplyMessageRequest,
                        PushMessageRequest=PushMessageRequest, group_bot_msg_ids=set(), result_text=result_text)
        load_functions(['notify_script_result', 'deliver_script_messages'], self.env)

    def notify(self):
        return self.env['notify_script_result']('group', 'reply', {'ok': True, 'message': '完成'})

    def test_reply_success_does_not_use_paid_push(self):
        self.api.reply_message.return_value = NS(sent_messages=[NS(id='sent')])
        self.assertTrue(self.notify())
        self.api.push_message.assert_not_called()

    def test_expired_reply_token_falls_back_to_push(self):
        exc = RuntimeError('invalid')
        exc.status, exc.body = 400, '{"message":"Invalid reply token"}'
        self.api.reply_message.side_effect = exc
        self.assertTrue(self.notify())
        self.api.push_message.assert_called_once()

    def test_ambiguous_reply_timeout_does_not_send_duplicate_push(self):
        self.api.reply_message.side_effect = TimeoutError('sensitive internal response')
        self.assertFalse(self.notify())
        self.api.push_message.assert_not_called()

    def test_existing_identical_github_blob_skips_second_commit(self):
        content = b'cover'
        sha = hashlib.sha1(b'blob 5\0' + content).hexdigest()
        http = Mock()
        http.get.return_value = Mock(status_code=200, json=Mock(return_value={'sha': sha}))
        env = dict(__builtins__=__builtins__, os=__import__('os'), re=re, base64=base64,
                   requests=http, GITHUB_TOKEN='test', GITHUB_REPO='test/repo', GITHUB_BRANCH='main')
        load_functions(['upload_image_to_github', '_raise_github_upload_error'], env)
        url = env['upload_image_to_github'](content, '封面.jpg')
        self.assertIn('raw.githubusercontent.com/test/repo/main', url)
        http.put.assert_not_called()


class OfflineStartupTests(unittest.TestCase):
    def test_real_sdk_startup_and_group_route_without_network_or_jobs(self):
        fake_env = {'LINE_CHANNEL_ACCESS_TOKEN': 'test', 'LINE_CHANNEL_SECRET': 'test',
                    'LINE_MY_USER_ID': 'test', 'GEMINI_API_KEY': 'test',
                    'GROUP_BOT_TOKEN': 'test', 'GROUP_BOT_SECRET': 'test',
                    'APPDATA': str(Path(__file__).parent / 'nonexistent-test-config')}
        for model in ('gemini-3.8-flash', 'gemini-3.1-pro-preview', 'claude-sonnet-5'):
            fake_env['GROUP_MODEL'] = model
            fake_env['GROUP_ANTHROPIC_API_KEY'] = 'test-claude-key'
            provider = 'anthropic' if model.startswith('claude-') else 'gemini'
            with self.subTest(model=model), patch.dict(os.environ, fake_env, clear=True), \
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
                 patch.object(BackgroundScheduler, 'start'), patch.object(threading.Thread, 'start'), \
                 patch.object(MessagingApi, 'get_bot_info', return_value=NS(user_id='test')):
                spec = importlib.util.spec_from_file_location('app_offline_smoke', Path(__file__).parents[1] / 'app.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(module.group_handler)
                self.assertIn('/group/callback', {rule.rule for rule in module.app.url_map.iter_rules()})
                session = module.new_group_tool_session()
                config = session._config
                if isinstance(config, dict):
                    config = types.GenerateContentConfig(**config)
                self.assertEqual(config.http_options.timeout, 20000)
                self.assertEqual(config.http_options.retry_options.attempts, 1)
                self.assertTrue(config.tools)
                self.assertEqual(module.GROUP_MODEL, model)
                self.assertEqual(module.GROUP_PROVIDER, provider)
                if provider == 'gemini':
                    self.assertEqual(config.thinking_config.thinking_level, types.ThinkingLevel.LOW)
                    self.assertEqual(config.tool_config.function_calling_config.mode,
                                     types.FunctionCallingConfigMode.VALIDATED)
                else:
                    self.assertIs(module.get_group_ai_client(), module.group_claude_client)
                    self.assertIsNone(config.thinking_config)
                self.assertTrue(config.automatic_function_calling.disable)
                self.assertEqual(config.max_output_tokens, 8192)
                self.assertIsNone(config.temperature)
                self.assertIsNone(config.top_p)
                self.assertIsNone(config.top_k)
                response = module.app.test_client().get('/health')
                self.assertEqual(response.data, b'OK')
                self.assertEqual(response.headers['X-Group-Model'], model)
                self.assertEqual(response.headers['X-Group-Provider'], provider)
                module.gemini_client.close()
                if module.group_claude_client is not None:
                    module.group_claude_client.close()


class GroupModelMigrationTests(unittest.TestCase):
    def test_claude_provider_never_silently_falls_back_to_gemini(self):
        env = {'GROUP_PROVIDER': 'anthropic', 'group_claude_client': None,
               'group_gemini_client': Mock(), 'gemini_client': Mock()}
        load_functions(['get_group_ai_client'], env)
        with self.assertRaises(RuntimeError):
            env['get_group_ai_client']()
        client = object()
        env['group_claude_client'] = client
        self.assertIs(env['get_group_ai_client'](), client)

    def test_chat_image_and_memory_summary_use_the_selected_client(self):
        client = Mock()
        client.models.generate_content.return_value.text = '測試回覆'
        sheet = Mock()
        sheet.get_all_values.return_value = [['group', 'user', '測試成員', '測試內容']] * 5
        env = {'get_group_ai_client': Mock(return_value=client), 'GROUP_MODEL': 'claude-sonnet-5',
               'group_generation_config': Mock(return_value=object()), 'types': types,
               'MASHA_PERSONA': '測試設定', '_SAFETY_OFF': [], 'SHEETS_ENABLED': True,
               'group_configuration': object(), 'get_sheet': Mock(return_value=sheet),
               'ALLOWED_GROUP_IDS': {'group'}, 'load_group_user_notes': Mock(return_value=[]),
               'load_group_events_log': Mock(return_value=[]), 'json': json, 're': re,
               'clear_chat_buffer_for_group': Mock()}
        load_functions(['group_chat_ai', 'compress_group_memory'], env)
        self.assertEqual(env['group_chat_ai']('看圖片', image_bytes=b'test-image'), '測試回覆')
        image_request = client.models.generate_content.call_args.kwargs
        self.assertEqual(image_request['model'], 'claude-sonnet-5')
        self.assertEqual(image_request['contents'][0].inline_data.data, b'test-image')
        client.models.generate_content.return_value.text = '{"users":[],"events":[]}'
        env['compress_group_memory']()
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(client.models.generate_content.call_args.kwargs['model'], 'claude-sonnet-5')
        env['clear_chat_buffer_for_group'].assert_called_once_with('group')

    def test_function_response_retains_call_id_and_name(self):
        env = {'types': types}
        load_functions(['function_response_part'], env)
        part = env['function_response_part'](types.FunctionCall(id='call-123', name='list_teams'), {'ok': True})
        self.assertEqual(part.function_response.id, 'call-123')
        self.assertEqual(part.function_response.name, 'list_teams')
        self.assertEqual(part.function_response.response, {'result': {'ok': True}})

    def test_legacy_rollback_model_omits_unsupported_thinking_level(self):
        env = {'types': types, 'GROUP_MODEL': 'gemini-2.5-flash'}
        load_functions(['group_generation_config'], env)
        config = env['group_generation_config']()
        self.assertIsNone(config.thinking_config)
        self.assertEqual(config.http_options.timeout, 20000)

    def test_sheets_timeout_is_set_before_opening_remote_sheet(self):
        client = Mock()
        env = {'Credentials': Mock(), '_creds_dict': {}, 'SCOPES': [],
               'gspread': Mock(authorize=Mock(return_value=client)), 'GOOGLE_SHEET_ID': 'test'}
        load_functions(['get_sheet'], env)
        env['get_sheet']('memory')
        self.assertEqual(client.mock_calls[0], unittest.mock.call.set_timeout((4, 10)))

    def test_profile_timeout_has_local_fallback(self):
        api = Mock()
        api.get_group_member_profile.side_effect = TimeoutError('offline')
        env = {'ApiClient': MagicMock(), 'MessagingApi': Mock(return_value=api), 'group_configuration': object()}
        load_functions(['get_member_name'], env)
        self.assertEqual(env['get_member_name']('group', None), '成員')
        self.assertEqual(api.get_group_member_profile.call_args.kwargs['_request_timeout'], (4, 8))


if __name__ == '__main__':
    unittest.main()
