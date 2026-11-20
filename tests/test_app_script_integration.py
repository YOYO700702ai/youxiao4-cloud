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


BIND_CODE = 'test-only-binding-placeholder-0003'  # Not a real code.
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
    return NS(candidates=[NS(content=NS(parts=parts))], text='')


ROUTING_FUNCTIONS = ['group_handle_message', 'handle_private_text', 'handle_gender_command',
                     'group_handle_group_text', 'group_handle_image', 'handle_material_text',
                     'execute_material_tool', 'is_bot_addressed', 'run_material_turn',
                     'deliver_material_result']


class AppRoutingTests(unittest.TestCase):
    """Actual app handlers with fake agent/binding: who reaches the model at all."""

    def setUp(self):
        self.group_agent, self.private_agent = Mock(), Mock()
        self.group_agent.accept_text.return_value = None
        self.private_agent.accept_text.return_value = 'invoke'
        self.admin = Mock()
        self.admin.mentions_code.return_value = False
        self.admin.is_admin.return_value = False
        self.spawned = []
        self.env = dict(
            __builtins__=__builtins__, SCRIPT_TOOLS=SCRIPT_TOOLS, re=re, json=json, time=Mock(),
            ALLOWED_GROUP_IDS={'group'}, GROUP_BOT_USER_ID='bot', GROUP_OWNER_ID='',
            GROUP_CHAT_LOG_MAX=20, group_chat_log={}, group_bot_msg_ids=set(),
            PRIVATE_ADMIN=self.admin, PRIVATE_TOOL_NAMES=frozenset({'search_web', 'fetch_webpage'}),
            PRIVATE_DEBOUNCE_SECONDS=0,
            get_material_agent=Mock(side_effect=lambda private=False: self.private_agent if private else self.group_agent),
            _run_in_background=lambda fn, *args: self.spawned.append((fn, args)),
            _material_tool_pending={}, _material_catalog=Mock(),
            CATALOG_TOOL_NAMES={'catalog_lookup', 'catalog_publish', 'catalog_update', 'catalog_status'},
            GROUP_TOOL_NAMES={'create_team', 'create_team_poll', 'search_web', 'catalog_publish'},
            get_member_name=Mock(return_value='測試使用者'), append_chat_buffer=Mock(),
            group_reply=Mock(), group_chat_ai=Mock(), show_loading=Mock(),
            deliver_script_messages=Mock(return_value=['sent']), save_bot_msg_ids=Mock(),
            threading=Mock(), execute_group_function=Mock(return_value={'ok': True}),
            TextMessage=TextMessage, do_gacha=Mock(),
        )
        load_functions(ROUTING_FUNCTIONS, self.env)

    def private(self, text, uid='admin', kind='user'):
        source = NS(type=kind, user_id=uid)
        if kind == 'group':
            source.group_id = 'group'
        if kind == 'room':
            source.room_id = 'room'
        return NS(source=source, reply_token='reply', message=NS(text=text, id='m1'), timestamp=1)

    def group(self, text, uid='user', mention=None, quoted=None):
        return NS(source=NS(type='group', group_id='group', user_id=uid), reply_token='reply',
                  message=NS(text=text, id='g1', mention=mention, quoted_message_id=quoted), timestamp=1)

    def test_unbound_private_text_and_image_are_silent_and_never_kept(self):
        self.env['group_handle_message'](self.private('你好，幫我上架這本', uid='stranger'))
        self.env['group_handle_image'](NS(source=NS(type='user', user_id='stranger'),
                                          message=NS(id='img', image_set=None), reply_token='r'))
        self.env['get_material_agent'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.assertEqual(self.spawned, [])

    def test_binding_message_is_handled_before_any_model_or_journal(self):
        self.admin.mentions_code.return_value = True
        for result, replied in ((None, False), ('bound', True), ('already', True)):
            with self.subTest(result=result):
                self.admin.try_bind.reset_mock()
                self.env['group_reply'].reset_mock()
                self.admin.try_bind.return_value = result
                self.env['group_handle_message'](self.private('binding-line', uid='someone'))
                self.admin.try_bind.assert_called_once_with('someone', 'binding-line')
                self.assertEqual(self.env['group_reply'].called, replied)
                if replied:
                    self.assertNotIn('binding-line', self.env['group_reply'].call_args.args[1])
        self.env['get_material_agent'].assert_not_called()
        self.assertEqual(self.spawned, [])

    def test_group_or_room_text_can_never_bind_and_is_dropped_before_any_log(self):
        self.admin.mentions_code.return_value = True
        self.env['group_handle_message'](self.private('binding-line', kind='room'))
        self.env['group_handle_message'](self.group('小六 binding-line'))
        self.admin.try_bind.assert_not_called()
        self.env['get_material_agent'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()
        self.assertEqual(self.env['group_chat_log'], {})
        self.assertEqual(self.spawned, [])

    def test_unbound_legacy_owner_is_silent_too(self):
        self.env['GROUP_OWNER_ID'] = 'owner'
        for text in ('你好', '我的ID', '[批量設性別]\n某人=男'):
            self.env['group_handle_message'](self.private(text, uid='owner'))
        self.env['group_chat_ai'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['get_material_agent'].assert_not_called()

    def test_bound_admin_text_reaches_private_agent_without_mention(self):
        self.admin.is_admin.return_value = True
        self.env['group_handle_message'](self.private('今天好累喔'))
        self.private_agent.accept_text.assert_called_once_with(('private', 'admin'), '今天好累喔', message_id='m1')
        self.group_agent.accept_text.assert_not_called()
        fn, args = self.spawned[0]
        self.assertEqual(args, (('private', 'admin'), 'm1', 'reply', 'admin', True))

    def test_bound_admin_image_is_kept_quietly(self):
        self.admin.is_admin.return_value = True
        self.env['group_handle_image'](NS(source=NS(type='user', user_id='admin'),
                                          message=NS(id='img', image_set=None), reply_token='r'))
        self.private_agent.receive_image.assert_called_once_with(('private', 'admin'), 'img', image_set=None)
        self.env['group_reply'].assert_not_called()

    def test_private_turn_reply_uses_reply_token_and_silence_sends_nothing(self):
        self.private_agent.respond.return_value = {'status': 'replied', 'message': '好的'}
        self.env['run_material_turn'](('private', 'admin'), 'm1', 'reply', 'admin', True)
        self.env['show_loading'].assert_called_once_with('admin')
        self.assertEqual(self.env['deliver_script_messages'].call_args.args[:2], ('admin', 'reply'))
        self.env['deliver_script_messages'].reset_mock()
        for status in ('silent', 'superseded'):
            self.private_agent.respond.return_value = {'status': status, 'message': ''}
            self.env['run_material_turn'](('private', 'admin'), 'm2', 'reply', 'admin', True)
        self.env['deliver_script_messages'].assert_not_called()

    def test_private_tools_cannot_touch_group_teams(self):
        result = self.env['execute_material_tool']('create_team', {}, {'ownerKey': ('private', 'admin')})
        self.assertFalse(result['ok'])
        self.env['execute_material_tool']('search_web', {'query': 'x'}, {'ownerKey': ('private', 'admin')})
        self.assertIsNone(self.env['execute_group_function'].call_args.args[2])
        self.env['execute_material_tool']('catalog_lookup', {'name': 'x'}, {'ownerKey': ('private', 'admin')})
        self.env['_material_catalog'].execute.assert_called_once()

    def test_legacy_script_tools_are_unreachable_from_group_executor(self):
        for name in SCRIPT_TOOLS:
            self.assertFalse(self.env['execute_material_tool'](name, {}, {'ownerKey': ('group', 'user')})['ok'])
        self.env['execute_group_function'].assert_not_called()

    def test_addressed_group_text_goes_to_background_turn_without_keywords(self):
        self.group_agent.accept_text.return_value = 'invoke'
        self.env['group_bot_msg_ids'].add('botmsg')
        for text, kwargs in (('小六 好了', {}), ('隨便一句', {'quoted': 'botmsg'}),
                             ('看這個', {'mention': NS(mentionees=[NS(user_id='bot')])})):
            with self.subTest(text=text):
                self.spawned.clear()
                self.env['group_handle_message'](self.group(text, **kwargs))
                self.assertTrue(self.group_agent.accept_text.call_args.kwargs['invoked'])
                self.assertEqual(self.spawned[0][1][:2], (('group', 'user'), 'g1'))
        self.env['group_chat_ai'].assert_not_called()

    def test_unaddressed_group_chat_is_logged_but_not_material(self):
        self.env['group_handle_message'](self.group('小六說過這個要怎麼弄'))
        self.assertFalse(self.group_agent.accept_text.call_args.kwargs['invoked'])
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.env['group_chat_log']['group'][-1]['text'], '小六說過這個要怎麼弄')

    def test_text_inside_own_intake_is_quiet_and_not_group_log(self):
        self.group_agent.accept_text.return_value = 'saved'
        self.env['group_handle_message'](self.group('角色一：小明'))
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.env['group_chat_log'], {})
        self.env['group_reply'].assert_not_called()

    def test_other_member_uses_own_key_and_unauthorized_group_is_ignored(self):
        self.env['group_handle_message'](self.group('隨便', uid='other'))
        self.assertEqual(self.group_agent.accept_text.call_args.args[0], ('group', 'other'))
        self.group_agent.reset_mock()
        event = self.group('小六 上架')
        event.source.group_id = 'not-allowed'
        self.env['group_handle_message'](event)
        self.env['group_handle_image'](NS(source=NS(type='group', group_id='not-allowed', user_id='u'),
                                          message=NS(id='i', image_set=None), reply_token='r'))
        self.group_agent.accept_text.assert_not_called()
        self.group_agent.receive_image.assert_not_called()

    def test_storage_failure_on_addressed_group_text_gets_short_notice(self):
        self.group_agent.accept_text.side_effect = RuntimeError('store down')
        self.env['group_handle_message'](self.group('小六 好了'))
        self.env['group_reply'].assert_called_once()
        self.env['group_reply'].reset_mock()
        self.env['group_handle_message'](self.group('閒聊'))
        self.env['group_reply'].assert_not_called()


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

    def test_silent_ignored_receipt_never_sends_line_message(self):
        result = self.env['notify_script_result'](
            'group', 'reply', {'ok': False, 'status': 'ignored', 'message': ''})
        self.assertFalse(result)
        self.api.reply_message.assert_not_called()
        self.api.push_message.assert_not_called()

    def test_deferred_or_ignored_receipts_do_not_send_empty_messages(self):
        for status in ('deferred', 'ignored', 'duplicate'):
            self.assertFalse(self.env['notify_script_result'](
                'group', 'reply', {'ok': True, 'status': status, 'message': ''}))
        self.api.reply_message.assert_not_called()
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
                    'PRIVATE_ADMIN_BIND_CODE': BIND_CODE, 'ALLOWED_GROUP_IDS': 'group',
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
            ('claude-opus-5-5', {'GROUP_ANTHROPIC_API_KEY': 'test-claude-key'}, '', ''),
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
                self.assertEqual(client.get('/health').headers['X-Private-Admin'], 'yes')

                def text_event(source, text, ident):
                    return {'type': 'message', 'mode': 'active', 'timestamp': 1700000000000,
                            'webhookEventId': 'evt' + ident, 'deliveryContext': {'isRedelivery': False},
                            'replyToken': 'reply' + ident, 'source': source,
                            'message': {'id': ident, 'type': 'text', 'text': text, 'quoteToken': 'q' + ident}}
                owner = expected_owner or 'U-owner'
                events = [text_event({'type': 'user', 'userId': owner}, '你好', '1'),
                          text_event({'type': 'user', 'userId': 'U-stranger'}, BIND_CODE, '2'),
                          text_event({'type': 'group', 'groupId': 'group', 'userId': 'U-x'}, '小六 ' + BIND_CODE, '3'),
                          text_event({'type': 'room', 'roomId': 'room', 'userId': 'U-y'}, BIND_CODE, '4')]
                body = json.dumps({'destination': 'test', 'events': events})
                signature = base64.b64encode(hmac.new(b'test', body.encode(), hashlib.sha256).digest()).decode()
                with patch.object(MessagingApi, 'reply_message') as reply, \
                     patch.object(MessagingApi, 'push_message') as push:
                    self.assertEqual(client.post('/group/callback', data=body,
                                                 headers={'X-Line-Signature': signature}).status_code, 200)
                reply.assert_not_called()
                push.assert_not_called()
                self.assertNotIn(BIND_CODE, json.dumps(module.group_chat_log, ensure_ascii=False))
                self.assertEqual(module._material_agents, {})
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
                self.assertEqual(response.headers['X-Bot-Release'], '2026-11-20-private-history-3d')
                self.assertEqual(response.headers['X-Script-Workflow'], 'direct-agent-v1')
                if module.group_gemini_client is not None:
                    module.group_gemini_client.close()
                if module.group_claude_client is not None:
                    module.group_claude_client.close()


class GroupOwnerTests(unittest.TestCase):
    def test_gender_command_requires_the_bound_admin_not_group_owner_id(self):
        for owner, bound, allowed in (('owner', True, True), ('owner', False, False),
                                      ('', True, True), ('', False, False)):
            with self.subTest(owner=owner, bound=bound):
                sender = 'owner'
                api, sheet = Mock(), Mock()
                sheet.get_all_values.return_value = [['group', 'user', 'Test', '', '', '']]
                admin = Mock()
                admin.mentions_code.return_value = False
                admin.is_admin.return_value = bound
                env = {'GROUP_OWNER_ID': owner, 'ApiClient': MagicMock(),
                       'MessagingApi': Mock(return_value=api), 'group_configuration': object(),
                       'ReplyMessageRequest': ReplyMessageRequest, 'TextMessage': TextMessage,
                       'get_sheet': Mock(return_value=sheet), 'group_chat_ai': Mock(),
                       'PRIVATE_ADMIN': admin, 'get_material_agent': Mock()}
                load_functions(['group_handle_message', 'handle_private_text', 'handle_gender_command',
                                'group_handle_group_text'], env)
                event = NS(source=NS(type='user', user_id=sender), reply_token='reply',
                           message=NS(text='[批量設性別]\nTest=男', id='m'))
                env['group_handle_message'](event)
                if allowed:
                    env['get_sheet'].assert_called_once_with('group_user_notes')
                    sheet.update_cell.assert_called_once_with(1, 7, '男')
                    api.reply_message.assert_called_once()
                else:
                    env['get_sheet'].assert_not_called()
                    api.reply_message.assert_not_called()
                env['group_chat_ai'].assert_not_called()
                env['get_material_agent'].assert_not_called()


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
