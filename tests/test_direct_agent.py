"""Acceptance tests: raw owner journal + real Claude adapter tool loop + catalog tools.

Only the HTTP layers are fake (Anthropic and the website bot API). The journal
store is the real RemoteJobStore (CAS revisions) over the fake website.
"""
import json
import threading
import time
import unittest

from google.genai import types

from direct_catalog_tools import DirectCatalogTools, catalog_tool_declarations
from direct_material_agent import DirectMaterialAgent, CALL_GRACE, RECEIVE_WINDOW
from listing_client import RemoteJobStore
from tests.direct_fakes import FakeWebsite, JPEG, claude, text, tool, first_json, last_user_blocks

KEY = ('private', 'admin')
GKEY = ('group', 'userA')
CONTENT = {'name': '第五個季節', 'synopsis': '四季之外的第五個季節。', 'playerMin': 2, 'playerMax': 2,
           'durationLabel': '3小時', 'priceStatus': 'fixed', 'price': 600, 'genres': ['情感'],
           'characters': [{'name': '春'}, {'name': '秋'}]}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.site = FakeWebsite()
        self.store = RemoteJobStore(self.site)
        self.catalog = DirectCatalogTools(self.site, self.store)
        self.clock = Clock()
        self.downloads = []
        self.download_error = set()

    def download(self, ident):
        self.downloads.append(ident)
        if ident in self.download_error:
            self.download_error.discard(ident)
            raise TimeoutError('line content timeout')
        return JPEG

    def agent(self, steps, private=True, monotonic=None):
        client, self.http = claude(steps)
        extra = {'monotonic': monotonic} if monotonic else {}
        return DirectMaterialAgent(
            self.store, self.site, self.download, client=client, model='claude-opus-5-5',
            config_factory=lambda **kw: types.GenerateContentConfig(max_output_tokens=4096, **kw),
            tools=catalog_tool_declarations(), execute_tool=self.catalog.execute, private=private,
            clock=self.clock, spawn=lambda fn, *a: fn(*a), import_legacy=not private, **extra)

    def journal(self, agent, key=KEY):
        return self.store.get(agent._storage_key(key))

    @staticmethod
    def images(payload):
        return [b for m in payload['messages'] if m['role'] == 'user' and isinstance(m['content'], list)
                for b in m['content'] if b.get('type') == 'image']

    @staticmethod
    def tool_results(payload):
        return [json.loads(b['content'])['result'] for b in last_user_blocks(payload) if b.get('type') == 'tool_result']

    def publish_args(self, sources, **extra):
        return {'content': CONTENT, 'selectedCoverMediaId': 'i1', 'sourceMessageIds': sources,
                'characterMediaIds': [{'name': '春', 'mediaId': 'i2'}, {'name': '秋', 'mediaId': 'i3'}], **extra}

    def send_book(self, agent, key=KEY):
        agent.accept_text(key, '我想上架一本新劇本，資料我傳給你', message_id='t1')
        agent.accept_text(key, CONTENT['synopsis'] + '\n兩人，三小時，600元，角色：春、秋', message_id='t2')
        for ident in ('i1', 'i2', 'i3'):
            self.assertEqual(agent.receive_image(key, ident), 'saved')
        return agent.accept_text(key, '好了', message_id='t3')


class PrivateConversationTests(AgentCase):
    def test_text_images_then_natural_done_publishes_through_native_tool(self):
        seen = {}

        def decide(payload):
            seen['payload'] = payload
            return [tool('catalog_publish', self.publish_args(['t1', 't3']))]

        def answer(payload):
            seen['results'] = self.tool_results(payload)
            return [{'type': 'text', 'text': '《第五個季節》上架好了。'}]

        agent = self.agent([decide, answer])
        self.assertEqual(self.send_book(agent), 'invoke')
        # Earlier messages of the burst are answered by the latest turn only.
        self.assertEqual(agent.respond(KEY, 't1')['status'], 'superseded')
        self.assertEqual(len(self.http.payloads), 0)
        result = agent.respond(KEY, 't3')

        events = first_json(seen['payload'])['events']
        self.assertEqual([e['id'] for e in events], ['t1', 't2', 'i1', 'i2', 'i3', 't3'])
        self.assertIn('600元', events[1]['text'])
        self.assertEqual(len(self.images(seen['payload'])), 3)
        self.assertTrue(all(b['source']['type'] == 'url' for b in self.images(seen['payload'])))
        self.assertEqual(seen['payload']['model'], 'claude-opus-5-5')
        self.assertTrue(seen['results'][0]['publicationReceipt'])
        self.assertEqual(self.site.writes, [('POST', ''), ('POST', '/' + seen['results'][0]['scriptId'] + '/publish')])
        script = next(iter(self.site.scripts.values()))
        self.assertEqual(script['cover']['url'], self.journal(agent)['events'][2]['asset']['url'])
        self.assertEqual([c['image']['alt'] for c in script['characters']], ['春', '秋'])
        self.assertEqual(result['status'], 'replied')
        self.assertIn('https://www.bglarp.com/scripts/第五個季節', result['message'])
        self.assertNotIn('receive_materials', {t['name'] for t in seen['payload']['tools']})

    def test_ordinary_chat_replies_without_any_catalog_workflow(self):
        agent = self.agent([text('今天辛苦了，要不要聊聊？')])
        agent.accept_text(KEY, '今天好累', message_id='c1')
        result = agent.respond(KEY, 'c1')
        self.assertEqual(result['message'], '今天辛苦了，要不要聊聊？')
        self.assertEqual(self.site.writes, [])
        payload = first_json(self.http.payloads[0])
        self.assertLessEqual(set(payload), {'now', 'invokingMessageId', 'events', 'background'})

    def test_empty_model_text_while_user_is_still_pasting_stays_silent(self):
        agent = self.agent([text('')])
        agent.accept_text(KEY, '第一段簡介……', message_id='p1')
        self.assertEqual(agent.respond(KEY, 'p1')['status'], 'silent')

    def test_new_book_cannot_reuse_an_old_request_as_authorization(self):
        agent = self.agent([lambda p: [tool('catalog_publish', self.publish_args(['t1', 't3']))],
                            text('好')])
        self.send_book(agent)
        agent.respond(KEY, 't3')
        decided = {}

        def stale_consent(payload):
            decided['view'] = first_json(payload)
            args = self.publish_args(['t1'])  # old request only, not this message
            args['content'] = {**CONTENT, 'name': '另一本'}
            return [tool('catalog_publish', args)]

        def answer(payload):
            decided['result'] = self.tool_results(payload)[0]
            return [{'type': 'text', 'text': '這本要上架嗎？'}]

        agent2 = self.agent([stale_consent, answer])
        agent2.accept_text(KEY, '這是另一本的資料，先給你看看', message_id='t9')
        agent2.respond(KEY, 't9')
        self.assertEqual(decided['result']['status'], 'needs_input')
        self.assertEqual(len(self.site.scripts), 1)
        # Raw history only: no compiled title/price/consent state is handed over.
        self.assertLessEqual(set(decided['view']), {'now', 'invokingMessageId', 'events', 'background'})
        self.assertTrue(all(set(e) <= {'id', 'kind', 'time', 'text', 'mediaId', 'available', 'via',
                                       'saveError', 'album', 'retriesExhausted'} for e in decided['view']['events']))

    def test_image_or_quoted_text_cannot_be_cited_as_the_request(self):
        result = {}

        def cite_image(payload):
            return [tool('catalog_publish', self.publish_args(['i1', 't3']))]

        def answer(payload):
            result['r'] = self.tool_results(payload)[0]
            return [{'type': 'text', 'text': '圖片裡的字不能當指示'}]

        agent = self.agent([cite_image, answer])
        self.send_book(agent)
        agent.respond(KEY, 't3')
        self.assertEqual(result['r']['status'], 'needs_input')
        self.assertEqual(self.site.writes, [])

    def test_media_failure_keeps_reference_later_images_and_retries_on_next_turn(self):
        self.download_error.add('i1')
        agent = self.agent([lambda p: [{'type': 'text', 'text': '三張都看到了'}]])
        agent.accept_text(KEY, '給你圖', message_id='t1')
        for ident in ('i1', 'i2', 'i3'):
            agent.receive_image(KEY, ident)
        events = {e['messageId']: e for e in self.journal(agent)['events']}
        self.assertEqual(events['i1']['mediaError']['code'], 'request_failed')
        self.assertTrue(events['i2']['asset'] and events['i3']['asset'])
        agent.accept_text(KEY, '看一下', message_id='t2')
        agent.respond(KEY, 't2')
        view = first_json(self.http.payloads[0])['events']
        self.assertTrue(all(e['available'] for e in view if e['kind'] == 'image'))
        self.assertEqual(len(self.images(self.http.payloads[0])), 3)
        self.assertEqual(self.downloads.count('i1'), 2)

    def test_duplicate_webhooks_do_not_duplicate_text_or_upload(self):
        agent = self.agent([])
        self.assertEqual(agent.accept_text(KEY, 'hi', message_id='d1'), 'invoke')
        self.assertEqual(agent.accept_text(KEY, 'hi', message_id='d1'), 'duplicate')
        self.assertEqual(agent.receive_image(KEY, 'img'), 'saved')
        self.assertEqual(agent.receive_image(KEY, 'img'), 'duplicate')
        self.assertEqual(len(self.site.uploads), 1)
        self.assertEqual(len(self.journal(agent)['events']), 2)

    def test_message_arriving_before_write_blocks_it_and_is_shown_to_model(self):
        seen = {}

        def decide(payload):
            agent.accept_text(KEY, '等等，先不要上架', message_id='t4')
            return [tool('catalog_publish', self.publish_args(['t1', 't3']))]

        def answer(payload):
            seen['results'] = self.tool_results(payload)
            seen['texts'] = json.dumps(last_user_blocks(payload), ensure_ascii=False)
            return [{'type': 'text', 'text': '好，先不上架。'}]

        agent = self.agent([decide, answer])
        self.send_book(agent)
        result = agent.respond(KEY, 't3')
        self.assertEqual(seen['results'][0]['status'], 'stale_context')
        self.assertIn('先不要上架', seen['texts'])
        self.assertEqual(self.site.writes, [])
        # t4 gets its own turn; this obsolete turn stays silent.
        self.assertEqual(result['status'], 'superseded')

    def test_lost_publish_response_is_resumed_by_status_never_republished(self):
        self.site.fail[('POST', 'publish')] = 'after'
        first = {}

        def answer(payload):
            first['r'] = self.tool_results(payload)[0]
            return [{'type': 'text', 'text': '送出了'}]

        agent = self.agent([lambda p: [tool('catalog_publish', self.publish_args(['t1', 't3']))], answer])
        self.send_book(agent)
        reply = agent.respond(KEY, 't3')
        self.assertEqual(first['r']['status'], 'pending')
        self.assertFalse(first['r']['publicationReceipt'])
        self.assertIn('⚠️', reply['message'])
        operation = first['r']['operationId']
        later = {}

        def check(payload):
            later['receipts'] = json.dumps(payload['messages'][0]['content'], ensure_ascii=False)
            return [tool('catalog_status', {'operationId': operation})]

        def resume(payload):
            later['status'] = self.tool_results(payload)[0]
            return [tool('catalog_publish', {'resumeOperationId': operation, 'sourceMessageIds': ['t5']})]

        def done(payload):
            later['resume'] = self.tool_results(payload)[0]
            return [{'type': 'text', 'text': '已確認上架'}]

        agent2 = self.agent([check, resume, done])
        agent2.accept_text(KEY, '剛剛那本有成功嗎？沒有的話繼續', message_id='t5')
        agent2.respond(KEY, 't5')
        self.assertIn(operation, later['receipts'])
        self.assertTrue(later['status']['publicationReceipt'])
        self.assertTrue(later['resume']['publicationReceipt'])
        self.assertEqual(sum(1 for w in self.site.writes if w[1].endswith('/publish')), 1)

    def test_lost_draft_response_is_replayed_without_duplicate_book(self):
        self.site.fail[('POST', 'draft')] = 'after'
        got = {}

        def answer(payload):
            got['r'] = self.tool_results(payload)[0]
            return [{'type': 'text', 'text': '還不確定'}]

        agent = self.agent([lambda p: [tool('catalog_publish', self.publish_args(['t1', 't3']))], answer])
        self.send_book(agent)
        agent.respond(KEY, 't3')
        self.assertEqual(got['r']['status'], 'pending')

        def resume(payload):
            return [tool('catalog_publish', {'resumeOperationId': got['r']['operationId'], 'sourceMessageIds': ['t6']})]

        def done(payload):
            got['resume'] = self.tool_results(payload)[0]
            return [{'type': 'text', 'text': '好了'}]

        agent2 = self.agent([resume, done])
        agent2.accept_text(KEY, '再試一下剛剛那本', message_id='t6')
        agent2.respond(KEY, 't6')
        self.assertTrue(got['resume']['publicationReceipt'])
        self.assertEqual(len(self.site.scripts), 1)

    def queued_behind_earlier_turn(self, agent, monotonic, invoking_id, queued_seconds):
        """Hold the owner's turn lock (an earlier turn still running), advance the
        fake monotonic clock while the new turn waits, then let it run."""
        results = []
        turn_lock = agent._lock(KEY, '_turns')
        turn_lock.acquire()
        worker = threading.Thread(target=lambda: results.append(agent.respond(KEY, invoking_id)))
        try:
            worker.start()
            for _ in range(500):
                if agent._running.get(KEY):
                    break
                time.sleep(0.002)
            self.assertTrue(agent._running.get(KEY), 'turn never queued')
            monotonic.now += queued_seconds
        finally:
            turn_lock.release()
        worker.join(5)
        return results[0]

    def test_latest_message_queued_longer_than_budget_still_gets_its_full_turn(self):
        monotonic = Clock()
        agent = self.agent([text('收到，我來處理')], monotonic=monotonic)
        self.send_book(agent)
        result = self.queued_behind_earlier_turn(agent, monotonic, 't3', agent.turn_budget + 50)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(len(self.http.payloads), 1)
        self.assertEqual(self.http.timeouts[0][1], 55)  # full per-call allowance, nothing charged for queueing

    def test_old_message_queued_behind_a_turn_stays_superseded_without_model_call(self):
        monotonic = Clock()
        agent = self.agent([], monotonic=monotonic)
        self.send_book(agent)
        result = self.queued_behind_earlier_turn(agent, monotonic, 't1', agent.turn_budget + 50)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(self.http.payloads, [])

    def test_model_api_failure_keeps_materials_and_answers_truthfully(self):
        def fail(payload):
            raise RuntimeError('boom')

        agent = self.agent([fail])
        self.send_book(agent)
        result = agent.respond(KEY, 't3')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('已上架', result['message'])
        self.assertEqual(len(self.journal(agent)['events']), 6)


class GroupScopeTests(AgentCase):
    def test_unaddressed_group_chat_is_not_stored(self):
        agent = self.agent([], private=False)
        self.assertIsNone(agent.accept_text(GKEY, '大家晚餐吃什麼', message_id='x1'))
        self.assertEqual(agent.receive_image(GKEY, 'x2'), 'ignored')
        self.assertEqual(self.site.jobs, {})
        self.assertEqual(self.downloads, [])

    def test_image_right_after_call_is_kept_and_intake_window_is_model_opened(self):
        def open_intake(payload):
            self.assertIn('receive_materials', {t['name'] for t in payload['tools']})
            return [tool('receive_materials', {'open': True})]

        agent = self.agent([open_intake, text('好，傳吧')], private=False)
        self.assertEqual(agent.accept_text(GKEY, '小六 我要上架新書', message_id='g1', invoked=True), 'invoke')
        self.assertEqual(agent.receive_image(GKEY, 'gi1'), 'saved')  # before the model even answered
        self.assertEqual(agent.receive_image(('group', 'userB'), 'bi1'), 'ignored')
        agent.respond(GKEY, 'g1')
        self.clock.now += CALL_GRACE + 1
        self.assertEqual(agent.accept_text(GKEY, '簡介：……', message_id='g2'), 'saved')
        self.assertEqual(agent.receive_image(GKEY, 'gi2'), 'saved')
        self.clock.now += RECEIVE_WINDOW + CALL_GRACE + 1
        self.assertEqual(agent.receive_image(GKEY, 'gi3'), 'ignored')
        ids = [e['messageId'] for e in self.journal(agent, GKEY)['events'] if e['kind'] != 'assistant']
        self.assertEqual(ids, ['g1', 'gi1', 'g2', 'gi2'])

    def test_users_in_same_group_never_see_each_others_materials(self):
        agent = self.agent([text('B 的回覆')], private=False)
        agent.accept_text(GKEY, '小六 收資料', message_id='a1', invoked=True)
        agent.receive_image(GKEY, 'a-img')
        agent.accept_text(('group', 'userB'), '小六 你好', message_id='b1', invoked=True)
        agent.respond(('group', 'userB'), 'b1')
        dump = json.dumps(self.http.payloads[0], ensure_ascii=False)
        self.assertNotIn('a-img', dump)
        self.assertNotIn('收資料', dump)

    def test_legacy_job_contributes_raw_inputs_only(self):
        self.store.put(GKEY, {'assistant': True, 'data': {'名稱': '伶人破防指南', '價格': '850'},
                              'publishConsent': {'any': True},
                              'messages': [{'role': 'user', 'messageId': 'old1', 'text': '幫我上架伶人破防指南'}],
                              'images': [{'messageId': 'oldimg', 'asset': {
                                  'url': 'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/public/script-covers/bot/x/cover/a.jpg',
                                  'path': 'bot/x/cover/a.jpg'}}]})
        agent = self.agent([text('看到了')], private=False)
        agent.accept_text(GKEY, '小六 第五個季節的資料', message_id='n1', invoked=True)
        agent.respond(GKEY, 'n1')
        view = first_json(self.http.payloads[0])
        self.assertEqual([e['id'] for e in view['events']], ['old1', 'oldimg', 'n1'])
        self.assertEqual(view['events'][0]['via'], 'legacy')
        dump = json.dumps(self.http.payloads[0], ensure_ascii=False)
        self.assertNotIn('850', dump)
        self.assertNotIn('publishConsent', dump)

    def test_legacy_reference_without_asset_gets_bounded_download_retries(self):
        self.store.put(GKEY, {'assistant': True, 'messages': [],
                              'images': [{'messageId': 'lost-cover', 'asset': None}]})
        self.download_error.add('lost-cover')
        agent = self.agent([text('第一次'), text('第二次')], private=False)
        agent.accept_text(GKEY, '小六 第五個季節的封面呢', message_id='n1', invoked=True)
        agent.respond(GKEY, 'n1')
        first = first_json(self.http.payloads[0])['events'][0]
        self.assertEqual((first['id'], first['available']), ('lost-cover', False))
        agent.accept_text(GKEY, '小六 再看看', message_id='n2', invoked=True)
        agent.respond(GKEY, 'n2')
        self.assertTrue(first_json(self.http.payloads[1])['events'][0]['available'])
        self.assertEqual(self.downloads.count('lost-cover'), 2)
        # The group journal is never imported into the private conversation.
        private = self.agent([text('嗨')])
        private.accept_text(KEY, '嗨', message_id='p1')
        private.respond(KEY, 'p1')
        self.assertNotIn('lost-cover', json.dumps(self.http.payloads[0]))


ASSET_URL = 'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/public/script-covers/bot/a/cover/y.jpg'
EVENTS = [{'kind': 'text', 'messageId': 't3', 'text': '上架'},
          {'kind': 'image', 'messageId': 'i1', 'mediaId': 'i1', 'asset': {'url': ASSET_URL, 'path': 'bot/a/cover/y.jpg'}}]


class CatalogToolTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.current = True

    def context(self, call_id, events=None, invoking='t3'):
        return {'ownerKey': KEY, 'events': events or EVENTS, 'toolCallId': call_id,
                'invokingMessageId': invoking, 'is_current': lambda: self.current}

    def publish(self, call_id, **content):
        args = {'content': {**CONTENT, 'characters': [], **content}, 'selectedCoverMediaId': 'i1',
                'sourceMessageIds': ['t3']}
        return self.catalog.execute('catalog_publish', args, self.context(call_id))

    def resume(self, operation, call_id):
        return self.catalog.execute('catalog_publish', {'resumeOperationId': operation, 'sourceMessageIds': ['t3']},
                                    self.context(call_id))

    def mutations(self):
        return [w for w in self.site.writes if w[0] in ('POST', 'PATCH')]

    def test_replayed_update_call_returns_receipt_before_rechecking_version(self):
        self.site._catalog('POST', '', {'content': {**CONTENT, 'cover': {'url': ASSET_URL, 'path': 'bot/a/cover/y.jpg'}}},
                           'seed', 'seed', None)
        script = next(iter(self.site.scripts.values()))
        args = {'content': {**CONTENT, 'price': 700}, 'scriptId': script['id'],
                'expectedVersionId': script['draftVersionId'], 'sourceMessageIds': ['t3']}
        first = self.catalog.execute('catalog_update', args, self.context('call-1'))
        self.assertTrue(first['publicationReceipt'])
        again = self.catalog.execute('catalog_update', args, self.context('call-1'))
        self.assertEqual(again['operationId'], first['operationId'])
        self.assertTrue(again['publicationReceipt'])
        self.assertEqual(sum(1 for w in self.site.writes if w[0] == 'PATCH'), 1)

    def test_new_message_during_receipt_save_prevents_draft_and_leaves_no_operation(self):
        def cancel_on_receipt_put(method, path):
            if method == 'PUT':
                self.current = False  # e.g. 「等等先不要」 arrives while the claim is being saved
        self.site.on_call = cancel_on_receipt_put
        result = self.publish('call-a')
        self.assertEqual(result['status'], 'stale_context')
        self.assertEqual(self.mutations(), [])
        self.site.on_call = None
        state = self.store.get((*KEY, 'direct_catalog_operations_v1'))
        self.assertEqual(state['operations'], {})

    def test_new_message_after_draft_prevents_publish_and_resume_continues(self):
        def cancel_after_draft(method, path):
            if method == 'PUT' and self.site.writes:
                self.current = False
        self.site.on_call = cancel_after_draft
        result = self.publish('call-b')
        self.assertEqual(result['status'], 'stale_context')
        self.assertEqual(self.mutations(), [('POST', '')])
        self.site.on_call, self.current = None, True
        status = self.catalog.execute('catalog_status', {'operationId': result['operationId']}, self.context('s'))
        self.assertEqual(status['status'], 'draft_saved')
        self.assertTrue(self.resume(result['operationId'], 'call-c')['publicationReceipt'])

    def test_update_cancelled_during_baseline_read_sends_no_write(self):
        self.site._catalog('POST', '', {'content': {**CONTENT, 'cover': {'url': ASSET_URL, 'path': 'bot/a/cover/y.jpg'}}},
                           'seed', 'seed', None)
        self.site.writes.clear()
        script = next(iter(self.site.scripts.values()))

        def cancel_on_read(method, path):
            if method == 'GET' and path.startswith('/') and not path.startswith('/jobs/'):
                self.current = False
        self.site.on_call = cancel_on_read
        args = {'content': {**CONTENT, 'price': 1}, 'scriptId': script['id'],
                'expectedVersionId': script['draftVersionId'], 'sourceMessageIds': ['t3']}
        self.assertEqual(self.catalog.execute('catalog_update', args, self.context('u1'))['status'], 'stale_context')
        self.assertEqual(self.mutations(), [])

    def test_publish_failure_before_sending_is_resumed_with_one_publish(self):
        self.site.fail[('POST', 'publish')] = 'before'
        result = self.publish('call-d')
        self.assertEqual(result['status'], 'pending')
        resumed = self.resume(result['operationId'], 'call-e')
        self.assertTrue(resumed['publicationReceipt'])
        self.assertEqual(sum(1 for w in self.site.writes if w[1].endswith('/publish')), 1)
        self.assertEqual(len(self.site.scripts), 1)

    def test_lost_publish_response_is_not_published_again(self):
        self.site.fail[('POST', 'publish')] = 'after'
        result = self.publish('call-f')
        self.assertEqual(result['status'], 'pending')
        self.assertTrue(self.resume(result['operationId'], 'call-g')['publicationReceipt'])
        self.assertTrue(self.resume(result['operationId'], 'call-h')['publicationReceipt'])
        self.assertEqual(sum(1 for w in self.site.writes if w[1].endswith('/publish')), 1)

    def test_sync_error_is_retried_through_sync_route_not_republished(self):
        self.site.sync_fails = True
        result = self.publish('call-i')
        self.assertEqual(result['status'], 'sync_error')
        self.assertFalse(result['publicationReceipt'])
        self.site.sync_fails = False
        status = self.catalog.execute('catalog_status', {'operationId': result['operationId']}, self.context('s2'))
        self.assertEqual(status['status'], 'sync_error')  # status alone never writes
        resumed = self.resume(result['operationId'], 'call-j')
        self.assertTrue(resumed['publicationReceipt'])
        paths = [w[1] for w in self.mutations()]
        self.assertEqual(sum(p.endswith('/publish') for p in paths), 1)
        self.assertEqual(sum(p.endswith('/sync') for p in paths), 1)

    def test_receipts_stay_within_store_budget_for_many_long_books(self):
        long_text = '這是一段很長的劇本簡介。' * 660  # about 7900 characters
        for index in range(30):
            result = self.publish(f'long-{index}', name=f'長簡介劇本{index}', synopsis=long_text)
            self.assertTrue(result['publicationReceipt'], result)
        state = self.store.get((*KEY, 'direct_catalog_operations_v1'))
        self.assertLess(len(json.dumps(state, ensure_ascii=False).encode()), 450000)
        self.assertTrue(all('body' not in row for row in state['operations'].values()))

    def test_unresolved_draft_body_is_kept_for_safe_replay(self):
        self.site.fail[('POST', 'draft')] = 'after'
        result = self.publish('call-k', synopsis='簡介' * 3000)
        state = self.store.get((*KEY, 'direct_catalog_operations_v1'))
        self.assertIn('body', state['operations'][result['operationId']])
        for index in range(25):
            self.publish(f'more-{index}', name=f'其他{index}', synopsis='簡介' * 3000)
        state = self.store.get((*KEY, 'direct_catalog_operations_v1'))
        self.assertIn(result['operationId'], state['operations'])
        self.assertTrue(self.resume(result['operationId'], 'call-l')['publicationReceipt'])
        self.assertEqual(sum(1 for s in self.site.scripts.values() if s['name'] == CONTENT['name']), 1)


if __name__ == '__main__':
    unittest.main()
