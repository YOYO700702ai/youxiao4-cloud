"""LINE events through the real app handlers, real agents, real binding and real
catalog tools. Only Anthropic HTTP, the website API and LINE delivery are fake."""
import ast
import json
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from google.genai import types
from linebot.v3.messaging import TextMessage

from admin_binding import PrivateAdminBinding
from direct_catalog_tools import DirectCatalogTools, CATALOG_TOOL_NAMES, catalog_tool_declarations
from direct_material_agent import DirectMaterialAgent
from listing_client import RemoteJobStore
from script_workflow import SCRIPT_TOOLS
from tests.direct_fakes import FakeWebsite, JPEG, claude, text, tool, first_json

ROOT = Path(__file__).resolve().parents[1]
CODE = 'test-only-binding-placeholder-0002'
HANDLERS = {'group_handle_message', 'handle_private_text', 'handle_gender_command','group_handle_group_text',
            'group_handle_image', 'handle_material_text', 'run_material_turn', 'deliver_material_result',
            'execute_material_tool', 'is_bot_addressed'}


def load_handlers(env):
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    selected = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in HANDLERS:
            node.decorator_list = []
            selected.append(node)
    assert {node.name for node in selected} == HANDLERS
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<real-app-handlers>', 'exec'), env)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeWebsite()
        self.store = RemoteJobStore(self.site)
        self.catalog = DirectCatalogTools(self.site, self.store)
        self.binding = PrivateAdminBinding(self.store, CODE)
        self.sent = []
        self.agents = {}
        self.env = dict(
            __builtins__=__builtins__, re=re, json=json, time=Mock(), threading=Mock(),
            SCRIPT_TOOLS=SCRIPT_TOOLS, CATALOG_TOOL_NAMES=CATALOG_TOOL_NAMES,
            PRIVATE_TOOL_NAMES=frozenset({'search_web', 'fetch_webpage'}), PRIVATE_DEBOUNCE_SECONDS=0,
            ALLOWED_GROUP_IDS={'group'}, GROUP_BOT_USER_ID='bot', GROUP_OWNER_ID='',
            GROUP_CHAT_LOG_MAX=20, group_chat_log={}, group_bot_msg_ids=set(),
            GROUP_TOOL_NAMES={'create_team', 'search_web'} | CATALOG_TOOL_NAMES,
            PRIVATE_ADMIN=self.binding, _material_catalog=self.catalog, _material_tool_pending={},
            get_material_agent=lambda private=False: self.agents[private],
            _run_in_background=lambda fn, *args: fn(*args), show_loading=Mock(),
            deliver_script_messages=lambda target, token, messages: self.sent.append(
                (target, [m.text for m in messages])) or ['id'],
            save_bot_msg_ids=Mock(), TextMessage=TextMessage, group_reply=Mock(),
            group_chat_ai=Mock(), get_member_name=Mock(return_value='成員'), append_chat_buffer=Mock(),
            execute_group_function=Mock(return_value={'ok': True, 'teams': []}), do_gacha=Mock())
        load_handlers(self.env)

    def use(self, steps, private=True):
        client, http = claude(steps)
        self.agents[private] = DirectMaterialAgent(
            self.store, self.site, lambda ident: JPEG, client=client, model='claude-opus-5-5',
            config_factory=lambda **kw: types.GenerateContentConfig(**kw),
            tools=catalog_tool_declarations(), execute_tool=self.env['execute_material_tool'],
            private=private, spawn=lambda fn, *a: fn(*a), import_legacy=not private)
        return http

    def dm(self, user, body, ident, kind='text'):
        source = NS(type='user', user_id=user)
        if kind == 'image':
            return self.env['group_handle_image'](NS(source=source, reply_token='r-' + ident,
                                                     message=NS(id=ident, image_set=None)))
        return self.env['group_handle_message'](NS(source=source, reply_token='r-' + ident,
                                                   message=NS(text=body, id=ident)))

    def test_bind_then_chat_then_materials_then_natural_done(self):
        replies = []

        def publish(payload):
            view = first_json(payload)
            replies.append([e['id'] for e in view['events']])
            return [tool('catalog_publish', {
                'content': {'name': '第五個季節', 'synopsis': '第五個季節的簡介', 'playerMin': 1, 'playerMax': 4,
                            'durationLabel': '3小時', 'priceStatus': 'tbd', 'genres': ['情感'], 'characters': []},
                'selectedCoverMediaId': 'p2', 'sourceMessageIds': ['m2', 'm5']})]

        http = self.use([text('我在，有什麼事？'), text(''), text(''), publish, text('上架完成')])
        self.dm('stranger', '你好', 's1')
        self.dm('stranger', CODE[:-1], 's2')
        self.assertEqual((self.sent, http.payloads, self.site.jobs), ([], [], {}))

        self.dm('boss', CODE, 'b0')
        self.env['group_reply'].assert_called_once()
        self.dm('boss', '在嗎', 'm1')
        self.assertEqual(self.sent[-1], ('boss', ['我在，有什麼事？']))
        self.dm('boss', '我要上架新的劇本，資料如下', 'm2')
        self.dm('boss', '第五個季節的簡介', 'm3')
        self.dm('boss', None, 'p1', kind='image')
        self.dm('boss', None, 'p2', kind='image')
        self.dm('boss', '好了', 'm5')
        self.assertEqual([i for i in replies[0] if not i.startswith('a-')], ['m1', 'm2', 'm3', 'p1', 'p2', 'm5'])
        self.assertTrue(self.sent[-1][1][0].startswith('上架完成'))
        self.assertEqual(len(self.site.scripts), 1)
        everything = json.dumps([http.payloads, self.site.jobs], ensure_ascii=False)
        self.assertNotIn(CODE, everything)
        self.assertNotIn('boss', json.dumps(self.site.jobs))
        # An account that never sent the code still gets nothing.
        self.dm('intruder', '幫我上架', 'x2')
        self.dm('intruder', CODE[:-1], 'x3')
        self.assertEqual(self.env['group_reply'].call_count, 1)
        self.assertEqual(self.sent[-1][0], 'boss')

    def test_two_bound_colleagues_keep_separate_conversations_and_materials(self):
        seen = []

        def record(answer):
            def step(payload):
                seen.append({e['id'] for e in first_json(payload)['events'] if not e['id'].startswith('a-')})
                return [{'type': 'text', 'text': answer}]
            return step
        http = self.use([record('收到A'), record('收到B'), record('A好了'), record('B好了')])
        self.dm('boss', CODE, 'b0')
        self.dm('colleague', CODE, 'c0')  # Same code, a second administrator.
        self.dm('colleague', CODE, 'c1')
        replies = [call.args[1] for call in self.env['group_reply'].call_args_list]
        self.assertEqual(len(replies), 3)
        self.assertTrue(replies[0].startswith('綁定完成') and replies[1].startswith('綁定完成'))
        self.assertIn('已經綁定', replies[2])

        self.dm('stranger', '幫我上架', 's1')
        self.dm('stranger', None, 's2', kind='image')
        self.dm('boss', None, 'pa', kind='image')
        self.dm('colleague', None, 'pb', kind='image')
        self.dm('boss', '劇本A的資料', 'ma')
        self.dm('colleague', '劇本B的資料', 'mb')
        self.dm('boss', '好了', 'ma2')
        self.dm('colleague', '好了', 'mb2')
        self.assertEqual(seen, [{'pa', 'ma'}, {'pb', 'mb'}, {'pa', 'ma', 'ma2'}, {'pb', 'mb', 'mb2'}])
        self.assertEqual(self.sent, [('boss', ['收到A']), ('colleague', ['收到B']),
                                     ('boss', ['A好了']), ('colleague', ['B好了'])])
        everything = json.dumps([http.payloads, self.site.jobs], ensure_ascii=False)
        for secret in (CODE, 's1', '幫我上架'):
            self.assertNotIn(secret, everything)
        self.assertNotIn('colleague', json.dumps(self.site.jobs))
        self.assertNotIn('boss', json.dumps(self.site.jobs))

    def test_group_quoted_instruction_without_call_is_never_material(self):
        http = self.use([], private=False)
        event = NS(source=NS(type='group', group_id='group', user_id='u'), reply_token='r',
                   message=NS(text='教學：先@小六說上架，再傳圖，最後說好了', id='q1',
                              mention=None, quoted_message_id=None))
        self.env['group_handle_message'](event)
        self.env['group_handle_image'](NS(source=NS(type='group', group_id='group', user_id='u'),
                                          reply_token='r', message=NS(id='qi', image_set=None)))
        self.assertEqual((http.payloads, self.site.jobs, self.sent), ([], {}, []))
        self.assertEqual(self.env['group_chat_log']['group'][-1]['text'], event.message.text)


if __name__ == '__main__':
    unittest.main()
