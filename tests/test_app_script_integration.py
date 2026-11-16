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
        self.listing.prepare_update.return_value = None
        self.listing.handle_text.return_value = None
        self.listing.has_active_capture.return_value = False
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
            listing_backend=Mock(), update_catalog_script=Mock(),
            parse_listing_command=parse_listing_command,
            ListingInputError=ListingInputError, LISTING_HELP=LISTING_HELP,
            get_member_name=Mock(return_value='測試使用者'), append_chat_buffer=Mock(),
            new_group_tool_session=Mock(return_value=self.session),
            group_reply=Mock(), group_chat_ai=Mock(), notify_script_result=Mock(),
            parse_leaked_tool_calls=Mock(return_value=[]),
            result_text=result_text, random=Mock(random=Mock(return_value=0.5)),
        )
        load_functions(['execute_group_function', 'group_handle_message', 'group_handle_image',
                        'function_response_part', 'is_script_intent', 'is_bot_addressed',
                        'handle_listing_text'], self.env)
        self.event = NS(source=NS(group_id='group', user_id='user'), reply_token='reply',
                        message=NS(text='小六幫忙看看上架需求', id='message'), timestamp=1700000000000)

    def handle(self, response):
        # A second model call after writing would fail; it must never happen.
        self.session.send_message.side_effect = [response, RuntimeError('model unavailable')]
        self.env['group_handle_message'](self.event)

    def test_native_upload_passes_original_user_request_without_second_chat_ai(self):
        data = {'名稱': '測試劇本', '簡介': '包含(括號)以及單引號\'的完整內容'}
        self.listing.handle_text.side_effect = [None, self.outcome]
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
        self.assertEqual(outcomes, [self.outcome])
        self.listing.handle_text.assert_called_with(
            ('group', 'user'), self.event.message.text, message_id='message',
            event_timestamp=1700000000000, eligible=True)

    def test_repeated_native_call_is_only_executed_once(self):
        self.env['execute_group_function'] = Mock(wraps=self.env['execute_group_function'])
        self.listing.handle_text.side_effect = [None, self.outcome]
        call = ('upload_script', {'data': {'名稱': '測試劇本'}})
        self.handle(response_with_calls(call, call))
        self.env['execute_group_function'].assert_called_once()
        self.workflow.start.assert_not_called()
        self.listing.begin.assert_not_called()

    def test_add_script_synonym_does_not_consume_cover_as_chat(self):
        self.event.message.text = '小六新增劇本《測試劇本》'
        self.listing.handle_text.return_value = self.outcome
        self.env['pending_group_image'][('group', 'user')] = ('image', 1)
        self.env['group_handle_message'](self.event)
        self.workflow.take_chat_image.assert_not_called()
        self.workflow.start.assert_not_called()
        self.listing.handle_text.assert_called_once_with(
            ('group', 'user'), self.event.message.text, message_id='message',
            event_timestamp=1700000000000, eligible=True)
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
        self.env['update_notion_script'] = Mock(side_effect=AssertionError('legacy Notion path'))
        self.env['update_catalog_script'].return_value = {'ok': True, 'message': '價格已更新'}
        self.handle(response_with_calls(('update_script', {'name': '測試劇本', 'new_price': 800})))
        self.env['update_catalog_script'].assert_called_once_with(self.env['listing_backend'], '測試劇本', {'價格': 800})
        self.env['update_notion_script'].assert_not_called()
        self.listing.prepare_update.assert_called_once_with(('group', 'user'), '測試劇本')
        outcomes = self.env['notify_script_result'].call_args.args[2]
        self.assertEqual(outcomes, [{'ok': True, 'message': '價格已更新'}])
        self.workflow.remember_result.assert_called_once()

    def test_explicit_empty_update_fields_reach_catalog(self):
        self.env['update_catalog_script'].return_value = {'ok': False, 'message': '必要欄位不可清空'}
        self.handle(response_with_calls(('update_script', {'name': '測試劇本', 'new_summary': '', 'new_roles': None})))
        self.env['update_catalog_script'].assert_called_once_with(self.env['listing_backend'], '測試劇本', {'劇情簡介': '', '角色': None})

    def test_append_tag_and_pending_receipt_are_not_summarized_as_success(self):
        outcome = {'ok': False, 'status': 'pending', 'message': '資料已保存，官網尚未確認更新完成。'}
        self.env['update_catalog_script'].return_value = outcome
        self.handle(response_with_calls(('update_script', {'name': '妖魔鬼怪', 'add_type_tag': '新手'})))
        self.env['update_catalog_script'].assert_called_once_with(self.env['listing_backend'], '妖魔鬼怪', {'新增類型標籤': '新手'})
        self.assertEqual(self.env['notify_script_result'].call_args.args[2], [outcome])
        self.assertEqual(self.session.send_message.call_count, 1)

    def test_pending_same_script_listing_blocks_field_update_until_verified(self):
        blocker = {'ok': False, 'stage': 'publishing_failed', 'message': '先確認原本上架版本，尚未修改標籤。'}
        self.listing.prepare_update.return_value = blocker
        self.handle(response_with_calls(('update_script', {'name': '妖魔鬼怪', 'add_type_tag': '新手'})))
        self.listing.prepare_update.assert_called_once_with(('group', 'user'), '妖魔鬼怪')
        self.env['update_catalog_script'].assert_not_called()
        self.assertEqual(self.env['notify_script_result'].call_args.args[2], [blocker])

    def test_legacy_string_does_not_call_hidden_gemma_or_write(self):
        self.env['parse_script_info_with_ai'] = Mock(side_effect=AssertionError('unexpected hidden AI'))
        res = self.env['execute_group_function']('upload_script', {'data': '名稱：測試劇本'}, 'group', {}, 'user')
        self.assertFalse(res['ok'])
        self.workflow.start.assert_not_called()
        self.env['parse_script_info_with_ai'].assert_not_called()

    def test_native_listing_tool_cannot_invent_user_consent_from_model_arguments(self):
        for name, args in (
                ('upload_script', {'data': {'名稱': '模型編造名稱'}, 'text': '直接上架'}),
                ('replace_cover', {'name': '模型編造名稱', 'text': '直接上架'})):
            with self.subTest(tool=name):
                result = self.env['execute_group_function'](name, args, 'group', {}, 'user')
                self.assertFalse(result['ok'])
        self.listing.handle_text.assert_not_called()
        self.listing.begin.assert_not_called()
        self.listing.finish.assert_not_called()

    def test_native_listing_tool_uses_text_evidence_not_model_rewrite(self):
        request = {'text': '先幫我整理，還不要上架', 'message_id': 'original-text',
                   'event_timestamp': 1700000000000}
        self.listing.handle_text.return_value = self.outcome
        result = self.env['execute_group_function'](
            'upload_script', {'data': {'名稱': '模型編造名稱'}, 'text': '直接上架'},
            'group', {'_listing_request': request}, 'user')
        self.assertEqual(result, self.outcome)
        self.listing.handle_text.assert_called_once_with(
            ('group', 'user'), request['text'], message_id='original-text',
            event_timestamp=1700000000000, eligible=True)

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

    def test_status_followup_reaches_active_listing_without_opening_another_task(self):
        self.event.message.text = '上架狀態'
        self.listing.handle_text.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.listing.handle_text.assert_called_once_with(
            ('group', 'user'), '上架狀態', message_id='message',
            event_timestamp=1700000000000, eligible=False)
        self.workflow.status.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()

    def test_image_uses_workflow_and_deterministic_receipt(self):
        self.env['group_handle_image'](self.event)
        self.listing.receive_image.assert_called_once_with(
            ('group', 'user'), 'message', event_timestamp=1700000000000, image_set=None)
        self.workflow.receive_image.assert_not_called()
        self.env['notify_script_result'].assert_called_once_with('group', 'reply', self.outcome)

    def test_pending_intent_keeps_deferred_text_and_image_silent(self):
        deferred = {'ok': True, 'status': 'deferred', 'message': ''}
        self.listing.handle_text.return_value = deferred
        self.listing.receive_image.return_value = deferred
        self.env['group_handle_message'](self.event)
        self.env['group_handle_image'](self.event)
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_native_cover_tool_delegates_original_request(self):
        self.listing.handle_text.side_effect = [None, self.outcome]
        self.handle(response_with_calls(('replace_cover', {'name': '測試劇本'})))
        self.workflow.start.assert_not_called()
        self.listing.begin.assert_not_called()
        self.listing.finish.assert_not_called()
        self.assertEqual(self.session.send_message.call_count, 1)
        outcome = self.env['notify_script_result'].call_args.args[2][0]
        self.assertEqual(outcome, self.outcome)
        self.listing.handle_text.assert_called_with(
            ('group', 'user'), self.event.message.text, message_id='message',
            event_timestamp=1700000000000, eligible=True)

    def test_natural_start_labels_and_finish_preserve_original_messages(self):
        self.listing.handle_text.return_value = self.outcome
        messages = ['陸總，這本幫我上架', '接下來是封面',
                    '接下來是角色圖 6 張', '資料傳完，直接上架']
        for index, text in enumerate(messages):
            self.event.message.text = text
            self.event.message.id = f'text-{index}'
            self.env['group_handle_message'](self.event)
            call = self.listing.handle_text.call_args
            self.assertEqual(call.args, (('group', 'user'), text))
            self.assertEqual(call.kwargs['message_id'], f'text-{index}')
            self.assertEqual(call.kwargs['event_timestamp'], 1700000000000)
        self.listing.begin.assert_not_called()
        self.listing.label_images.assert_not_called()
        self.listing.finish.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()
        self.env['get_member_name'].assert_not_called()
        self.env['append_chat_buffer'].assert_not_called()

    def test_existing_image_task_reference_reaches_assistant_unchanged(self):
        self.listing.handle_text.return_value = self.outcome
        self.event.message.text = '接下來這張是《測試劇本》的封面'
        self.env['group_handle_message'](self.event)
        self.assertEqual(self.listing.handle_text.call_args.args[1], self.event.message.text)
        self.listing.begin.assert_not_called()
        self.listing.finish.assert_not_called()

    def test_album_metadata_is_preserved_for_completion_tracking(self):
        self.event.message.image_set = NS(id='album-1', index=2, total=6)
        self.env['group_handle_image'](self.event)
        self.listing.receive_image.assert_called_once_with(
            ('group', 'user'), 'message', event_timestamp=1700000000000,
            image_set=self.event.message.image_set)

    def test_ordinary_message_is_available_to_active_task_without_opening_one(self):
        result = self.env['handle_listing_text'](
            ('group', 'user'), '今天下午見', message_id='ordinary-text', event_timestamp=1700000000000)
        self.assertIsNone(result)
        self.listing.handle_text.assert_called_once_with(
            ('group', 'user'), '今天下午見', message_id='ordinary-text',
            event_timestamp=1700000000000, eligible=False)

    def test_unaddressed_listing_topics_and_instruction_quotes_stay_silent(self):
        self.env['random'].random.return_value = 0
        texts = (
            '這本要上架了', '上架資料', '官網資料在這裡', '網站目前都正常',
            '名稱：測試劇本', '上架說明', '上架教學',
            '他說陸總會自己處理', '範例：「陸總，上架這本」',
            '「陸總，上架這本」是使用說明', '陸總說明天再處理',
            '陸總剛剛說會上架', '```text\n陸總，上架這本\n```',
        )
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                self.event.message.text = text
                self.event.message.id = f'ordinary-{index}'
                self.env['group_handle_message'](self.event)
                self.assertFalse(self.listing.handle_text.call_args.kwargs['eligible'])
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()
        self.workflow.take_chat_image.assert_not_called()
        self.env['random'].random.assert_not_called()

    def test_direct_calls_are_distinct_from_mentions_inside_prose(self):
        positive = ('陸總，幫我上架', '小六幫我處理這本', '小六新增劇本《測試》',
                    '陸傲天', '@陸總 請幫忙', '小6，繼續', '傲天請看這張圖')
        negative = ('他叫小六來看', '上架資料給陸總', '陸總的說明在這',
                    '陸總表示會處理', '「小六幫我上架」', '陸總之前已經上架',
                    '小六剛才說有收到', '名稱：陸總')
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(self.env['is_bot_addressed'](text))
        for text in negative:
            with self.subTest(text=text):
                self.assertFalse(self.env['is_bot_addressed'](text))

    def test_unknown_quoted_message_does_not_wake_bot(self):
        self.event.message.text = '上架資料在這裡'
        self.event.message.quoted_message_id = 'someone-elses-message'
        self.env['random'].random.return_value = 0
        self.env['group_handle_message'](self.event)
        self.assertFalse(self.listing.handle_text.call_args.kwargs['eligible'])
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_directly_addressed_message_can_reach_intent_model_without_keywords(self):
        self.listing.handle_text.return_value = self.outcome
        result = self.env['handle_listing_text'](
            ('group', 'user'), '這份幫我處理一下', message_id='addressed-text',
            event_timestamp=1700000000000, addressed=True)
        self.assertEqual(result, self.outcome)
        self.listing.handle_text.assert_called_once_with(
            ('group', 'user'), '這份幫我處理一下', message_id='addressed-text',
            event_timestamp=1700000000000, eligible=True)

    def test_line_mention_reaches_listing_intent_before_image_chat(self):
        self.event.message.text = '這份幫我處理一下'
        self.event.message.mention = NS(mentionees=[NS(user_id='bot')])
        self.env['pending_group_image'][('group', 'user')] = ('image', 1)
        self.listing.handle_text.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.listing.handle_text.assert_called_once_with(
            ('group', 'user'), self.event.message.text, message_id='message',
            event_timestamp=1700000000000, eligible=True)
        self.workflow.take_chat_image.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_reply_to_bot_reaches_listing_intent_without_fixed_keywords(self):
        self.event.message.text = '全部都好了，照你剛剛說的處理'
        self.event.message.quoted_message_id = 'bot-response'
        self.env['group_bot_msg_ids'].add('bot-response')
        self.listing.handle_text.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.assertTrue(self.listing.handle_text.call_args.kwargs['eligible'])
        self.workflow.take_chat_image.assert_not_called()

    def test_bot_name_and_website_synonym_reach_listing_intent(self):
        self.event.message.text = '陸總，這本登上官網'
        self.env['pending_group_image'][('group', 'user')] = ('image', 1)
        self.listing.handle_text.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.assertTrue(self.listing.handle_text.call_args.kwargs['eligible'])
        self.workflow.take_chat_image.assert_not_called()

    def test_mentioning_another_person_does_not_open_listing_intent(self):
        self.event.message.text = '這份幫我處理一下'
        self.event.message.mention = NS(mentionees=[NS(user_id='someone-else')])
        self.listing.handle_text.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.assertFalse(self.listing.handle_text.call_args.kwargs['eligible'])

    def test_listing_problem_receipt_is_delivered_without_chat_ai_rewrite(self):
        outcome = {'ok': False, 'message': '找不到同名劇本'}
        self.listing.handle_text.return_value = outcome
        self.event.message.text = '接下來這張是《不存在》的封面'
        self.env['group_handle_message'](self.event)
        self.env['notify_script_result'].assert_called_once_with('group', 'reply', outcome)
        self.env['new_group_tool_session'].assert_not_called()

    def test_natural_followup_answers_reach_existing_listing_workflow(self):
        self.listing.handle_text.return_value = self.outcome
        for text in ('第三張就是傲慢魔女的親眷', '第一張當封面就好', '價格是 800 元'):
            self.event.message.text = text
            self.env['group_handle_message'](self.event)
            self.assertEqual(self.listing.handle_text.call_args.args[1], text)
        self.listing.assign_role.assert_not_called()
        self.listing.confirm_cover.assert_not_called()
        self.listing.finish.assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_another_group_member_uses_own_task_key(self):
        self.event.source.user_id = 'another-user'
        self.event.message.text = '資料傳完，直接上架'
        self.listing.handle_text.return_value = self.outcome
        self.env['group_handle_message'](self.event)
        self.assertEqual(self.listing.handle_text.call_args.args[0], ('group', 'another-user'))
        self.env['group_handle_image'](self.event)
        self.listing.receive_image.assert_called_once_with(
            ('group', 'another-user'), 'message', event_timestamp=1700000000000, image_set=None)

    def test_unauthorized_group_cannot_access_text_or_image_workflow(self):
        self.event.source.group_id = 'not-allowed'
        self.event.message.text = '資料傳完，直接上架'
        self.env['group_handle_message'](self.event)
        self.env['group_handle_image'](self.event)
        self.assertEqual(self.listing.mock_calls, [])
        self.env['notify_script_result'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_start_without_title_does_not_require_a_fixed_form(self):
        self.listing.handle_text.return_value = {'ok': True, 'message': '直接把文字與圖片傳過來就好。'}
        self.event.message.text = '陸總，上架'
        self.env['group_handle_message'](self.event)
        self.listing.begin.assert_not_called()
        self.assertEqual(self.listing.handle_text.call_args.args[1], '陸總，上架')
        self.env['new_group_tool_session'].assert_not_called()
        self.assertTrue(self.env['notify_script_result'].call_args.args[2]['ok'])

    def test_persistent_store_failure_returns_safe_receipt_without_ai_fallback(self):
        self.listing.handle_text.side_effect = RuntimeError('private-token-value')
        self.event.message.text = '陸總，上架《測試劇本》'
        self.env['group_handle_message'](self.event)
        result = self.env['notify_script_result'].call_args.args[2]
        self.assertFalse(result['ok'])
        self.assertNotIn('private-token-value', result['message'])
        self.env['new_group_tool_session'].assert_not_called()
        self.workflow.start.assert_not_called()

    def test_image_store_failure_returns_safe_receipt_without_chat(self):
        self.listing.has_active_capture.return_value = True
        self.listing.receive_image.side_effect = RuntimeError('private-token-value')
        self.env['group_handle_image'](self.event)
        result = self.env['notify_script_result'].call_args.args[2]
        self.assertFalse(result['ok'])
        self.assertNotIn('private-token-value', result['message'])
        self.assertEqual(self.env['pending_group_image'], {})

    def test_image_store_error_outside_capture_is_silent(self):
        self.listing.receive_image.side_effect = RuntimeError('private-token-value')
        self.env['random'].random.return_value = 0
        self.env['group_handle_image'](self.event)
        self.listing.has_active_capture.assert_called_once_with(('group', 'user'))
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.assertEqual(self.env['pending_group_image'][('group', 'user')][0], 'message')
        self.env['random'].random.assert_not_called()

    def test_text_store_error_outside_capture_does_not_interrupt_group_chat(self):
        self.event.message.text = '這張是操作說明，大家參考'
        self.listing.handle_text.side_effect = RuntimeError('private-token-value')
        self.env['random'].random.return_value = 0
        self.env['group_handle_message'](self.event)
        self.listing.has_active_capture.assert_called_once_with(('group', 'user'))
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_text_store_error_inside_capture_returns_safe_receipt(self):
        self.event.message.text = '這是接著補的資料'
        self.listing.handle_text.side_effect = RuntimeError('private-token-value')
        self.listing.has_active_capture.return_value = True
        self.env['group_handle_message'](self.event)
        result = self.env['notify_script_result'].call_args.args[2]
        self.assertFalse(result['ok'])
        self.assertNotIn('private-token-value', result['message'])
        self.env['new_group_tool_session'].assert_not_called()

    def test_unlabelled_image_is_kept_only_for_chat_without_listing_receipt(self):
        self.listing.receive_image.return_value = {'ok': False, 'status': 'ignored', 'message': 'ignored'}
        self.env['random'].random.return_value = 0
        self.env['group_handle_image'](self.event)
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.env['random'].random.assert_not_called()
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
                self.assertEqual(response.headers['X-Bot-Release'], '2026-11-16-listing-consent-fix')
                self.assertEqual(response.headers['X-Script-Workflow'], 'assistant-v3-scoped')
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
