"""Exercise actual app handlers without importing startup jobs or credentials."""
import ast
import base64
import datetime
import hashlib
import hmac
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
from listing_commands import parse_listing_command, ListingInputError, LISTING_HELP


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
        self.listing = Mock()
        self.listing.lock = threading.RLock()
        for method in ('begin', 'merge_fields', 'label_images', 'finish', 'retry', 'status',
                       'cancel', 'assign_role', 'confirm_cover', 'receive_image'):
            getattr(self.listing, method).return_value = self.outcome
        self.store = Mock()
        self.store.get.return_value = None
        self.session = Mock()
        self.env = dict(
            __builtins__=__builtins__, SCRIPT_TOOLS=SCRIPT_TOOLS, types=types,
            re=re, json=json, datetime=datetime, time=Mock(),
            ALLOWED_GROUP_IDS={'group'}, GROUP_BOT_USER_ID='bot',
            GROUP_CHAT_LOG_MAX=20, group_chat_log={}, group_bot_msg_ids=set(),
            pending_group_image={}, script_workflow=self.workflow,
            listing_workflow=self.listing, listing_store=self.store,
            parse_listing_command=parse_listing_command,
            ListingInputError=ListingInputError, LISTING_HELP=LISTING_HELP,
            get_member_name=Mock(return_value='測試使用者'), append_chat_buffer=Mock(),
            new_group_tool_session=Mock(return_value=self.session),
            group_reply=Mock(), notify_script_result=Mock(),
            parse_leaked_tool_calls=Mock(return_value=[]),
            result_text=result_text, random=Mock(random=Mock(return_value=0.5)),
        )
        load_functions(['execute_group_function', 'group_handle_message', 'group_handle_image',
                        'function_response_part', 'is_script_intent', 'handle_listing_text'], self.env)
        self.event = NS(source=NS(group_id='group', user_id='user'), reply_token='reply',
                        message=NS(text='小六幫忙看看上架需求', id='message'), timestamp=1700000000000)

    def handle(self, response):
        # A second model call after writing would fail; it must never happen.
        self.session.send_message.side_effect = [response, RuntimeError('model unavailable')]
        self.env['group_handle_message'](self.event)

    def test_native_upload_only_returns_instructions_without_writes_or_second_ai(self):
        data = {'名稱': '測試劇本', '簡介': '包含(括號)以及單引號\'的完整內容'}
        self.handle(response_with_calls(('upload_script', {'data': data})))
        self.workflow.start.assert_not_called()
        self.listing.begin.assert_not_called()
        self.listing.finish.assert_not_called()
        self.assertEqual(self.session.send_message.call_count, 1)
        self.env['get_member_name'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()
        self.env['new_group_tool_session'].assert_called_once_with('group', include_memory=False)
        outcomes = self.env['notify_script_result'].call_args.args[2]
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0]['ok'])
        self.assertIn('尚未建立或發布', outcomes[0]['message'])

    def test_repeated_native_call_is_only_executed_once(self):
        self.env['execute_group_function'] = Mock(wraps=self.env['execute_group_function'])
        call = ('upload_script', {'data': {'名稱': '測試劇本'}})
        self.handle(response_with_calls(call, call))
        self.env['execute_group_function'].assert_called_once()
        self.workflow.start.assert_not_called()
        self.listing.begin.assert_not_called()

    def test_add_script_synonym_does_not_consume_cover_as_chat(self):
        self.event.message.text = '小六新增劇本《測試劇本》'
        self.env['pending_group_image'][('group', 'user')] = ('image', 1)
        self.env['group_handle_message'](self.event)
        self.workflow.take_chat_image.assert_not_called()
        self.workflow.start.assert_not_called()
        self.listing.begin.assert_called_once_with(('group', 'user'), {'名稱': '測試劇本'}, kind='upload')
        self.env['new_group_tool_session'].assert_not_called()

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
        self.env['group_handle_message'](self.event)
        self.listing.status.assert_called_once_with(('group', 'user'))
        self.workflow.status.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()

    def test_image_uses_workflow_and_deterministic_receipt(self):
        self.env['group_handle_image'](self.event)
        self.listing.receive_image.assert_called_once_with(('group', 'user'), 'message', event_timestamp=1700000000000)
        self.workflow.receive_image.assert_not_called()
        self.env['notify_script_result'].assert_called_once_with('group', 'reply', self.outcome)

    def test_native_cover_tool_cannot_start_or_publish_a_task(self):
        self.handle(response_with_calls(('replace_cover', {'name': '測試劇本'})))
        self.workflow.start.assert_not_called()
        self.listing.begin.assert_not_called()
        self.listing.finish.assert_not_called()
        self.assertEqual(self.session.send_message.call_count, 1)
        outcome = self.env['notify_script_result'].call_args.args[2][0]
        self.assertFalse(outcome['ok'])
        self.assertIn('尚未修改', outcome['message'])

    def test_explicit_begin_label_and_finish_bypass_ai_and_sheets(self):
        self.event.message.text = '陸總，上架《測試劇本》\n人數：7\n售價：2300'
        self.env['group_handle_message'](self.event)
        self.listing.begin.assert_called_once_with(
            ('group', 'user'), {'名稱': '測試劇本', '人數': ['7人'], '價格': 2300}, kind='upload')
        self.store.get.return_value = {'stage': 'collecting'}
        self.event.message.text = '接下來這 7 張是《測試劇本》的角色圖'
        self.env['group_handle_message'](self.event)
        self.listing.label_images.assert_called_once_with(('group', 'user'), '測試劇本', 'portraits', 7)
        self.event.message.text = '資料傳完，直接上架'
        self.env['group_handle_message'](self.event)
        self.listing.finish.assert_called_once_with(('group', 'user'))
        self.env['new_group_tool_session'].assert_not_called()
        self.env['get_member_name'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()

    def test_named_batch_can_begin_existing_image_task_before_label(self):
        self.event.message.text = '接下來這張是《測試劇本》的封面'
        self.env['group_handle_message'](self.event)
        self.listing.begin.assert_called_once_with(('group', 'user'), {'名稱': '測試劇本'}, kind='cover')
        self.listing.label_images.assert_called_once_with(('group', 'user'), '測試劇本', 'cover', 1)
        self.listing.finish.assert_not_called()

    def test_named_batch_inspects_store_inside_workflow_lock(self):
        def read_state(key):
            # CPython RLock exposes ownership for assertions without racing.
            self.assertTrue(self.listing.lock._is_owned())
            return None
        self.store.get.side_effect = read_state
        self.event.message.text = '接下來這張是《測試劇本》的封面'
        self.env['group_handle_message'](self.event)
        self.listing.begin.assert_called_once()
        self.assertFalse(self.listing.lock._is_owned())

    def test_failed_existing_task_lookup_does_not_begin_receiving_images(self):
        self.listing.begin.return_value = {'ok': False, 'message': '找不到同名劇本'}
        self.event.message.text = '接下來這張是《不存在》的封面'
        self.env['group_handle_message'](self.event)
        self.listing.label_images.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_manual_pair_and_cover_confirmation_bypass_model(self):
        for text in ('配對角色 3：傲慢魔女的親眷', '確認圖片 1 為封面'):
            self.event.message.text = text
            self.env['group_handle_message'](self.event)
        self.listing.assign_role.assert_called_once_with(('group', 'user'), 3, '傲慢魔女的親眷')
        self.listing.confirm_cover.assert_called_once_with(('group', 'user'), 1)
        self.listing.finish.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_another_group_member_uses_own_task_key(self):
        self.event.source.user_id = 'another-user'
        self.event.message.text = '資料傳完，直接上架'
        self.env['group_handle_message'](self.event)
        self.listing.finish.assert_called_once_with(('group', 'another-user'))
        self.env['group_handle_image'](self.event)
        self.listing.receive_image.assert_called_once_with(('group', 'another-user'), 'message', event_timestamp=1700000000000)

    def test_unauthorized_group_cannot_access_text_or_image_workflow(self):
        self.event.source.group_id = 'not-allowed'
        self.event.message.text = '資料傳完，直接上架'
        self.env['group_handle_message'](self.event)
        self.env['group_handle_image'](self.event)
        self.assertEqual(self.listing.mock_calls, [])
        self.env['notify_script_result'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_invalid_form_is_reported_without_ai_or_mutation(self):
        self.event.message.text = '陸總，上架'
        self.env['group_handle_message'](self.event)
        self.listing.begin.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()
        self.assertIn('劇本名稱', self.env['notify_script_result'].call_args.args[2]['message'])

    def test_persistent_store_failure_returns_safe_receipt_without_ai_fallback(self):
        self.listing.begin.side_effect = RuntimeError('private-token-value')
        self.event.message.text = '陸總，上架《測試劇本》'
        self.env['group_handle_message'](self.event)
        result = self.env['notify_script_result'].call_args.args[2]
        self.assertFalse(result['ok'])
        self.assertNotIn('private-token-value', result['message'])
        self.env['new_group_tool_session'].assert_not_called()
        self.workflow.start.assert_not_called()

    def test_image_store_failure_returns_safe_receipt_without_chat(self):
        self.listing.receive_image.side_effect = RuntimeError('private-token-value')
        self.env['group_handle_image'](self.event)
        result = self.env['notify_script_result'].call_args.args[2]
        self.assertFalse(result['ok'])
        self.assertNotIn('private-token-value', result['message'])
        self.assertEqual(self.env['pending_group_image'], {})

    def test_unlabelled_image_is_kept_only_for_chat_without_listing_receipt(self):
        self.listing.receive_image.return_value = {'ok': False, 'status': 'ignored', 'message': 'ignored'}
        self.env['group_handle_image'](self.event)
        self.env['notify_script_result'].assert_not_called()
        self.assertEqual(self.env['pending_group_image'][('group', 'user')][0], 'message')

    def test_duplicate_image_does_not_send_duplicate_receipt_or_enter_chat(self):
        self.listing.receive_image.return_value = {'ok': True, 'status': 'duplicate', 'message': 'duplicate'}
        self.env['group_handle_image'](self.event)
        self.env['notify_script_result'].assert_not_called()
        self.assertEqual(self.env['pending_group_image'], {})

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
        base_env = {'GROUP_BOT_TOKEN': 'test', 'GROUP_BOT_SECRET': 'test',
                    'APPDATA': str(Path(__file__).parent / 'nonexistent-test-config')}
        # Keep OS loader paths when clearing credentials: httpcore/trio may
        # lazily resolve system libraries on Windows during real SDK startup.
        base_env.update({name: os.environ[name] for name in ('PATH', 'SystemRoot', 'WINDIR') if name in os.environ})
        scenarios = (
            ('gemini-3.8-flash', {'GROUP_GEMINI_KEY': 'test-group'}, 'test-group', ''),
            ('gemini-3.1-pro-preview', {'GEMINI_API_KEY': 'test-fallback'}, 'test-fallback', ''),
            ('gemini-3.8-flash', {'GROUP_GEMINI_KEY': 'test-group', 'GEMINI_API_KEY': 'test-fallback',
                                  'GROUP_OWNER_ID': 'group-owner', 'LINE_MY_USER_ID': 'legacy-owner'},
             'test-group', 'group-owner'),
            ('gemini-3.8-flash', {'GROUP_GEMINI_KEY': 'test-group', 'LINE_MY_USER_ID': 'legacy-owner'},
             'test-group', 'legacy-owner'),
            ('claude-sonnet-5', {'GROUP_ANTHROPIC_API_KEY': 'test-claude-key'}, '', ''),
        )
        for model, settings, expected_key, expected_owner in scenarios:
            fake_env = {**base_env, **settings}
            fake_env['GROUP_MODEL'] = model
            provider = 'anthropic' if model.startswith('claude-') else 'gemini'
            with self.subTest(model=model, owner=expected_owner, key=expected_key), \
                 patch.dict(os.environ, fake_env, clear=True), \
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
                 patch.object(BackgroundScheduler, 'start') as scheduler_start, \
                 patch.object(threading.Thread, 'start'), \
                 patch.object(MessagingApi, 'get_bot_info', return_value=NS(user_id='test')):
                spec = importlib.util.spec_from_file_location('app_offline_smoke', Path(__file__).parents[1] / 'app.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(module.group_handler)
                self.assertEqual(module.GROUP_GEMINI_KEY, expected_key)
                self.assertEqual(module.GROUP_OWNER_ID, expected_owner)
                routes = {rule.rule for rule in module.app.url_map.iter_rules()}
                self.assertIn('/group/callback', routes)
                self.assertIn('/team-poll/<poll_id>', routes)
                self.assertNotIn('/callback', routes)
                scheduler_start.assert_called_once()
                jobs = module.scheduler.get_jobs()
                self.assertEqual(len(jobs), 2)
                self.assertEqual({job.func.__name__ for job in jobs},
                                 {'compress_group_memory', 'check_group_reminders'})
                client = module.app.test_client()
                self.assertEqual(client.post('/callback', json={'events': []}).status_code, 404)
                webhook_body = json.dumps({'destination': 'test', 'events': []})
                signature = base64.b64encode(hmac.new(b'test', webhook_body.encode(), hashlib.sha256).digest()).decode()
                self.assertEqual(client.post('/group/callback', data=webhook_body,
                                             headers={'X-Line-Signature': signature}).status_code, 200)
                self.assertEqual(client.post('/group/callback', data=webhook_body,
                                             headers={'X-Line-Signature': 'invalid'}).status_code, 400)
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
                    self.assertIsNone(module.group_gemini_client)
                    self.assertIs(module.get_group_ai_client(), module.group_claude_client)
                    self.assertIsNone(config.thinking_config)
                self.assertTrue(config.automatic_function_calling.disable)
                self.assertEqual(config.max_output_tokens, 8192)
                self.assertIsNone(config.temperature)
                self.assertIsNone(config.top_p)
                self.assertIsNone(config.top_k)
                response = client.get('/health')
                self.assertEqual(response.data, b'OK')
                self.assertEqual(response.headers['X-Group-Model'], model)
                self.assertEqual(response.headers['X-Group-Provider'], provider)
                self.assertEqual(response.headers['X-Bot-Release'], '2026-09-26-retire-xiaowu')
                if module.group_gemini_client is not None:
                    module.group_gemini_client.close()
                if module.group_claude_client is not None:
                    module.group_claude_client.close()


class GroupOwnerTests(unittest.TestCase):
    def test_private_admin_command_only_uses_configured_group_owner(self):
        for owner, sender, allowed in (('owner', 'owner', True), ('owner', 'other', False),
                                       ('', 'other', False), ('', '', False)):
            with self.subTest(owner=owner, sender=sender):
                api, sheet = Mock(), Mock()
                sheet.get_all_values.return_value = [['group', 'user', 'Test', '', '', '']]
                env = {'GROUP_OWNER_ID': owner, 'ApiClient': MagicMock(),
                       'MessagingApi': Mock(return_value=api), 'group_configuration': object(),
                       'ReplyMessageRequest': ReplyMessageRequest, 'TextMessage': TextMessage,
                       'get_sheet': Mock(return_value=sheet), 'group_chat_ai': Mock()}
                load_functions(['group_handle_message'], env)
                event = NS(source=NS(user_id=sender), reply_token='reply',
                           message=NS(text='[批量設性別]\nTest=男'))
                env['group_handle_message'](event)
                if allowed:
                    env['get_sheet'].assert_called_once_with('group_user_notes')
                    sheet.update_cell.assert_called_once_with(1, 7, '男')
                    api.reply_message.assert_called_once()
                else:
                    env['get_sheet'].assert_not_called()
                    api.reply_message.assert_not_called()
                env['group_chat_ai'].assert_not_called()


class GroupModelMigrationTests(unittest.TestCase):
    def test_claude_provider_never_silently_falls_back_to_gemini(self):
        env = {'GROUP_PROVIDER': 'anthropic', 'group_claude_client': None,
               'group_gemini_client': Mock()}
        load_functions(['get_group_ai_client'], env)
        with self.assertRaises(RuntimeError):
            env['get_group_ai_client']()
        client = object()
        env['group_claude_client'] = client
        self.assertIs(env['get_group_ai_client'](), client)

    def test_gemini_requires_its_group_client_without_private_bot_fallback(self):
        env = {'GROUP_PROVIDER': 'gemini', 'group_gemini_client': None}
        load_functions(['get_group_ai_client'], env)
        with self.assertRaisesRegex(RuntimeError, 'Gemini API'):
            env['get_group_ai_client']()
        client = object()
        env['group_gemini_client'] = client
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
