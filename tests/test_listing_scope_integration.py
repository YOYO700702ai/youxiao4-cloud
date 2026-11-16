"""Exercise real LINE handlers and durable collection boundaries without I/O."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from linebot.v3.messaging import TextMessage


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from listing_assistant import AssistantListingWorkflow
from script_workflow import result_text


class JsonStore:
    """Copy through JSON so tests exercise the persisted state shape."""
    def __init__(self):
        self.values = {}
        self.writes = 0

    def get(self, key):
        value = self.values.get(tuple(key))
        return json.loads(json.dumps(value))

    def put(self, key, value):
        self.values[tuple(key)] = json.loads(json.dumps(value))
        self.writes += 1


def load_handlers(env):
    wanted = {'group_handle_message', 'group_handle_image', 'handle_listing_text',
              'is_bot_addressed', 'is_script_intent', 'notify_script_result'}
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    selected = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            node.decorator_list = []
            selected.append(node)
    assert {node.name for node in selected} == wanted
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<real-app-handlers>', 'exec'), env)


class ListingScopeIntegrationTests(unittest.TestCase):
    KEY = ('staff-group', 'sender')
    DATA = {'名稱': '測試劇本', '人數': ['2人'], '時長': '4小時',
            '類型': ['推理'], '簡介': '保留原始簡介', '角色': ['甲', '乙']}

    def setUp(self):
        self.now = 1700000000.0
        self.store = JsonStore()
        self.backend = Mock()
        self.backend.lookup.return_value = None
        self.assets = {}

        def upload(raw, filename, operation_key, *, kind):
            self.assets[operation_key] = raw
            return {'url': 'https://assets.invalid/' + filename, 'path': operation_key}

        self.backend.upload_media.side_effect = upload
        self.backend.read_media.side_effect = lambda asset: self.assets[asset['path']]
        self.backend.publish.return_value = {'status': 'published', 'scriptId': 'script',
                                             'versionId': 'v1', 'url': 'https://www.bglarp.com/scripts/test'}
        self.download = Mock(side_effect=lambda ident: b'\xff\xd8\xff' + ident.encode())
        self.analyze = Mock(side_effect=AssertionError('unsolicited content reached the model'))
        self.workflow = AssistantListingWorkflow(
            store=self.store, backend=self.backend, download=self.download,
            analyze=self.analyze, clock=lambda: self.now)
        self.env = {
            '__builtins__': __builtins__, 're': re,
            'time': NS(time=lambda: self.now),
            'ALLOWED_GROUP_IDS': {'staff-group'}, 'GROUP_BOT_USER_ID': 'bot',
            'GROUP_CHAT_LOG_MAX': 20, 'group_chat_log': {}, 'group_bot_msg_ids': set(),
            'pending_group_image': {}, 'listing_workflow': self.workflow,
            'script_workflow': Mock(), 'get_member_name': Mock(return_value='員工'),
            'append_chat_buffer': Mock(), 'TextMessage': TextMessage, 'result_text': result_text,
            'deliver_script_messages': Mock(return_value=['offline-receipt']),
            'group_reply': Mock(), 'group_chat_ai': Mock(),
            'new_group_tool_session': Mock(side_effect=AssertionError('unexpected group AI call')),
            # The former 2% branches would always execute with this value.
            'random': Mock(random=Mock(return_value=0)),
        }
        load_handlers(self.env)
        self.env['notify_script_result'] = Mock(wraps=self.env['notify_script_result'])

    def event(self, ident, *, text=None, seconds=0):
        self.now = 1700000000.0 + seconds
        message = NS(id=ident)
        if text is not None:
            message.text = text
        return NS(source=NS(group_id=self.KEY[0], user_id=self.KEY[1]),
                  reply_token='reply-' + ident, message=message, timestamp=int(self.now * 1000))

    def seed_previous_release_job(self):
        """A prior assistant release left a failed, complete task collecting."""
        job = {
            'schemaVersion': 1, 'assistant': True, 'jobId': 'previous-release-job',
            'kind': 'upload', 'data': {**deepcopy(self.DATA), '價格': 1000},
            'requestedData': deepcopy(self.DATA), 'nameKnown': True,
            'createdAt': self.now - 3600, 'updatedAt': self.now - 3500,
            'stage': 'collecting', 'baseline': None, 'expectedVersion': None,
            'batches': [], 'processedEvents': ['old-cover'],
            'messages': [{'role': 'user', 'messageId': 'old-done',
                          'text': '資料傳完，直接上架', 'timestamp': self.now - 3500}],
            'images': [{'index': 1, 'messageId': 'old-cover', 'purpose': 'cover',
                        'roleName': None, 'confirmed': True, 'stage': 'ready',
                        'asset': {'url': 'https://assets.invalid/old-cover.jpg', 'path': 'old-cover'}}],
            'publishRequested': False, 'publishOperationKey': 'previous-release-job:publish',
            'lastResult': {'ok': False, 'status': 'analysis_failed', 'message': '先前整理未成功'},
            'publishConsent': {'publish': True, 'complete': True,
                               'publishEvidenceMessageId': 'old-done',
                               'completionEvidenceMessageId': 'old-done'},
            'questions': [], 'inputRevision': 2, 'analyzedRevision': -1,
            'analysisPending': True, 'imageSets': {}, 'imageSetIssues': [],
            'resolvedIdentity': ['upload', self.DATA['名稱']],
        }
        self.assertNotIn('captureSession', job)
        self.store.values[self.KEY] = json.loads(json.dumps(job))
        return deepcopy(job)

    def assert_no_unsolicited_work(self):
        self.download.assert_not_called()
        self.backend.upload_media.assert_not_called()
        self.backend.read_media.assert_not_called()
        self.backend.publish.assert_not_called()
        self.analyze.assert_not_called()
        self.env['notify_script_result'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()
        self.env['random'].random.assert_not_called()

    def test_instruction_screenshot_does_not_resume_previous_release_job(self):
        before = self.seed_previous_release_job()
        self.env['group_handle_image'](self.event('instruction-screenshot'))
        self.assert_no_unsolicited_work()
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.store.get(self.KEY), before)
        self.assertEqual(self.env['pending_group_image'][self.KEY][0], 'instruction-screenshot')

    def test_unaddressed_teaching_text_does_not_enter_previous_job(self):
        before = self.seed_previous_release_job()
        for index, text in enumerate((
                '這裡是上架資料，大家先看一下',
                '操作說明：「陸總，上架這本」，接著傳圖片',
                '他說陸總會自己整理圖片', '上架說明')):
            self.env['group_handle_message'](self.event('teaching-' + str(index), text=text, seconds=index))
        self.assert_no_unsolicited_work()
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.store.get(self.KEY), before)

    def test_name_first_third_person_statement_is_silently_ignored_by_real_workflow(self):
        self.analyze.side_effect = None
        self.analyze.return_value = {
            'action': 'ignore', 'captureAction': 'none', 'addressedToBot': False,
        }
        self.env['group_handle_message'](self.event(
            'third-person', text='陸總今天說要上架'))
        self.analyze.assert_called_once()
        context = self.analyze.call_args.args[0]
        self.assertEqual(context['messages'][-1]['text'], '陸總今天說要上架')
        self.assertTrue(context['explicitlyInvoked'])
        self.assertFalse(self.workflow.has_active_capture(self.KEY))
        state = self.store.get(self.KEY)
        self.assertEqual(state['images'], [])
        self.assertFalse(state['publishRequested'])
        self.assertFalse(any(m['messageId'] == 'third-person' for m in state['messages']))
        self.download.assert_not_called()
        self.backend.publish.assert_not_called()
        self.env['deliver_script_messages'].assert_not_called()
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.env['new_group_tool_session'].assert_not_called()

    def test_explicit_collection_accepts_images_then_stops_after_finish_needs_input(self):
        self.analyze.side_effect = None
        self.analyze.return_value = {
            'action': 'collect', 'captureAction': 'open', 'data': deepcopy(self.DATA),
            'authorization': {'publish': True, 'evidenceMessageId': 'start'},
        }
        self.env['group_handle_message'](self.event(
            'start', text='陸總我要上架這本，我現在把資料給你'))
        self.assertTrue(self.workflow.has_active_capture(self.KEY))
        for seconds, ident in enumerate(('cover', 'role-a', 'role-b'), start=1):
            self.env['group_handle_image'](self.event(ident, seconds=seconds))
        self.assertEqual(self.download.call_count, 3)
        self.assertEqual(self.backend.upload_media.call_count, 3)
        self.assertEqual(self.analyze.call_count, 1)

        self.analyze.side_effect = [
            {'action': 'publish', 'authorization': {'complete': True, 'evidenceMessageId': 'done'}},
            {'action': 'publish', 'assignments': [
                {'index': 1, 'purpose': 'cover'},
                {'index': 2, 'purpose': 'portraits', 'roleName': '甲'},
                {'index': 3, 'purpose': 'portraits', 'roleName': '乙'},
            ], 'questions': ['請補每人價格。']},
        ]
        done = self.event('done', text='好了', seconds=4)
        done.message.mention = NS(mentionees=[NS(user_id='bot')])
        self.env['group_handle_message'](done)
        before = self.store.get(self.KEY)
        # A missing field keeps the draft collecting, but must not leave the
        # receiver open to unrelated screenshots sent later in the group.
        self.assertEqual(before['stage'], 'collecting')
        self.assertEqual(before['lastResult']['status'], 'needs_input')
        self.assertFalse(self.workflow.has_active_capture(self.KEY))
        self.assertEqual(len(before['images']), 3)
        self.assertEqual(self.analyze.call_count, 3)
        finish_intent = self.analyze.call_args_list[1].args[0]
        self.assertEqual(finish_intent['messages'][-1]['text'], '好了')
        self.assertTrue(finish_intent['explicitlyInvoked'])
        self.backend.publish.assert_not_called()
        writes = self.store.writes
        notifications = self.env['notify_script_result'].call_count

        self.env['group_handle_image'](self.event('later-instruction-screenshot', seconds=5))
        self.assertEqual(self.store.get(self.KEY), before)
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(self.download.call_count, 3)
        self.assertEqual(self.backend.upload_media.call_count, 3)
        self.assertEqual(self.analyze.call_count, 3)
        self.assertEqual(self.env['notify_script_result'].call_count, notifications)
        self.env['group_reply'].assert_not_called()
        self.env['group_chat_ai'].assert_not_called()
        self.assertEqual(self.env['pending_group_image'][self.KEY][0], 'later-instruction-screenshot')


if __name__ == '__main__':
    unittest.main()
