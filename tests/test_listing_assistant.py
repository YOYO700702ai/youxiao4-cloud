"""Offline regressions for natural-language collection and batch vision."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from listing_assistant import AssistantListingWorkflow
from listing_workflow import ListingWorkflow, WorkflowStorageError


class Store:
    def __init__(self):
        self.values = {}
        self.fail_when = None

    def get(self, key):
        return deepcopy(self.values.get(tuple(key)))

    def put(self, key, value):
        if self.fail_when and self.fail_when(value):
            raise OSError('private storage detail')
        self.values[tuple(key)] = json.loads(json.dumps(value))


class AssistantListingTests(unittest.TestCase):
    KEY = ('group', 'user')
    DATA = {'名稱': '伶人破指南', '人數': ['2人'], '時長': '4小時',
            '價格': 1000, '類型': ['推理'], '簡介': '完整簡介', '角色': ['甲', '乙']}

    def setUp(self):
        self.now = 1700000000
        self.store = Store()
        self.backend = Mock()
        self.backend.lookup.return_value = None
        self.assets = {}
        def upload(raw, filename, operation_key, *, kind):
            self.assets.setdefault(operation_key, raw)
            return {'url': 'https://assets.invalid/' + filename, 'path': operation_key}
        self.backend.upload_media.side_effect = upload
        self.backend.read_media.side_effect = lambda asset: self.assets[asset['path']]
        self.backend.publish.return_value = {'status': 'published', 'url': 'https://www.bglarp.com/scripts/test',
                                             'scriptId': 'script', 'versionId': 'v1'}
        self.download = Mock(side_effect=lambda ident: b'\xff\xd8\xff' + ident.encode())
        self.identify = Mock()
        self.analyze = Mock(return_value=self.plan())
        self.workflow = AssistantListingWorkflow(store=self.store, backend=self.backend,
                                                download=self.download, identify=self.identify,
                                                analyze=self.analyze, clock=lambda: self.now)

    def plan(self, action='collect', **extra):
        return {'action': action, 'data': {}, 'assignments': [], 'questions': [], **extra}

    def begin(self):
        self.analyze.return_value = self.plan(data=deepcopy(self.DATA), captureAction='open', authorization={
            'publish': True, 'evidenceMessageId': 'start'})
        return self.workflow.handle_text(self.KEY, '陸總，幫我把這本上架', message_id='start',
                                         event_timestamp=(self.now - 10) * 1000, eligible=True)

    def activate(self, kind='upload'):
        self.analyze.return_value = self.plan(kind=kind, captureAction='open')
        return self.workflow.handle_text(self.KEY, '陸總，繼續收這本資料', message_id='open',
                                         event_timestamp=(self.now - 10) * 1000, eligible=True)

    def organize(self, images=1, **extra):
        assignments = [{'index': 1, 'purpose': 'cover'}]
        assignments.extend({'index': index, 'purpose': 'portraits', 'roleName': ['甲', '乙'][index - 2]}
                           for index in range(2, images + 1))
        return self.plan('publish', assignments=assignments, **extra)

    def finish(self, organize=None):
        self.analyze.side_effect = [self.plan('publish', authorization={
            'complete': True, 'evidenceMessageId': 'done'}), organize or self.organize()]
        return self.workflow.handle_text(self.KEY, '都給你了，幫我處理好', message_id='done',
                                         event_timestamp=self.now * 1000)

    def test_all_images_collected_without_labels_and_only_one_batch_vision(self):
        self.begin()
        for ident in ['cover', 'role-a', 'role-b']:
            self.workflow.receive_image(self.KEY, ident, event_timestamp=1699999999000)
        self.assertEqual(self.analyze.call_count, 1)
        self.identify.assert_not_called()
        self.assertEqual(self.finish(self.organize(3))['status'], 'published')
        calls = self.analyze.call_args_list
        self.assertEqual([call.args[0]['phase'] for call in calls], ['intent', 'intent', 'organize'])
        self.assertFalse(calls[0].args[0]['activeTask'])
        self.assertTrue(calls[1].args[0]['activeTask'])
        self.assertEqual(calls[-1].args[0]['images'][0]['timestamp'], 1699999999)
        self.assertEqual(calls[-1].args[0]['images'][0]['checksum'], sha256(b'\xff\xd8\xffcover').hexdigest())
        self.assertEqual([item['index'] for item in calls[-1].args[1]], [1, 2, 3])
        self.assertEqual([m['messageId'] for m in calls[-1].args[0]['messages']], ['start', 'done'])
        self.backend.publish.assert_called_once()
        self.assertNotIn('authorization', self.store.get(self.KEY))

    def test_unknown_name_and_text_are_durable_before_analysis(self):
        def inspect(context, images):
            self.assertEqual(self.store.get(self.KEY)['messages'][0]['text'], '我要上架這本，資料等等貼')
            return self.plan(captureAction='open')
        self.analyze.side_effect = inspect
        self.workflow.handle_text(self.KEY, '我要上架這本，資料等等貼', message_id='first', eligible=True)
        self.workflow.receive_image(self.KEY, 'cover')
        job = self.store.get(self.KEY)
        self.assertFalse(job['nameKnown'])
        self.assertTrue(job['images'][0]['asset'])
        self.backend.lookup.assert_not_called()

    def test_non_listing_chat_never_starts_model_or_new_job(self):
        self.assertIsNone(self.workflow.handle_text(self.KEY, '今天幾點下班', eligible=False))
        self.analyze.assert_not_called()
        self.assertIsNone(self.store.get(self.KEY))

    def test_unrelated_first_candidate_can_be_ignored(self):
        self.analyze.return_value = self.plan('ignore')
        self.assertIsNone(self.workflow.handle_text(self.KEY, '上架是什麼意思', eligible=True))
        self.assertEqual(self.store.get(self.KEY)['stage'], 'cancelled')
        self.backend.publish.assert_not_called()

    def test_unconfirmed_candidate_cannot_capture_concurrent_chat_images(self):
        returned = Mock()
        self.workflow.return_chat_image = returned
        def receive_then_ignore(context, images):
            self.assertEqual(self.workflow.receive_image(self.KEY, 'first-chat-image')['status'], 'deferred')
            self.assertEqual(self.workflow.receive_image(self.KEY, 'latest-chat-image')['status'], 'deferred')
            return self.plan('ignore')
        self.analyze.side_effect = receive_then_ignore
        result = self.workflow.handle_text(self.KEY, '陸總幫我看看這張圖', message_id='chat', eligible=True)
        self.assertIsNone(result)
        returned.assert_called_once_with(self.KEY, 'latest-chat-image')
        self.assertEqual(self.store.get(self.KEY)['stage'], 'cancelled')
        self.assertEqual(self.store.get(self.KEY)['images'], [])
        self.download.assert_not_called()
        self.backend.publish.assert_not_called()

    def test_finish_does_not_invent_authorization(self):
        self.workflow.begin(self.KEY, self.DATA)
        self.activate()
        self.workflow.receive_image(self.KEY, 'cover')
        self.analyze.return_value = self.organize()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'ready')
        self.assertEqual([message['messageId'] for message in self.store.get(self.KEY)['messages']], ['open'])
        self.backend.publish.assert_not_called()

    def test_image_message_cannot_be_consent_evidence(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'image')
        self.analyze.return_value = self.organize(authorization={'complete': True, 'evidenceMessageId': 'image'})
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'analysis_failed')
        self.backend.publish.assert_not_called()
        self.assertTrue(self.store.get(self.KEY)['images'][0]['asset'])

    def test_missing_field_can_be_answered_without_reauthorizing(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        outcome = self.finish(self.organize(data={'價格': None}, questions=['每人售價是多少？']))
        self.assertEqual(outcome['status'], 'needs_input')
        self.analyze.side_effect = [self.plan('organize', data={'價格': 1200}), self.organize()]
        outcome = self.workflow.handle_text(self.KEY, '陸總，價格是1200', message_id='price', eligible=True)
        self.assertEqual(outcome['status'], 'published')
        self.assertEqual(self.backend.publish.call_args.args[0]['data']['價格'], 1200)

    def test_completion_can_be_revoked_while_publish_request_is_retained(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.finish(self.organize(questions=['請確認簡介']))
        self.analyze.side_effect = [self.plan(authorization={'complete': False, 'evidenceMessageId': 'wait'})]
        outcome = self.workflow.handle_text(self.KEY, '陸總，還沒傳完，先等等', message_id='wait', eligible=True)
        self.assertEqual(outcome['status'], 'collecting')
        consent = self.store.get(self.KEY)['publishConsent']
        self.assertTrue(consent['publish'])
        self.assertFalse(consent['complete'])
        self.backend.publish.assert_not_called()

    def test_known_line_album_waits_then_continues_when_all_images_arrive(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover', event_timestamp=(self.now - 2) * 1000,
                                    image_set={'id': 'album', 'index': 1, 'total': 2})
        self.analyze.side_effect = None
        self.analyze.return_value = self.plan('publish', authorization={'complete': True, 'evidenceMessageId': 'done'})
        result = self.workflow.handle_text(self.KEY, '傳好了', message_id='done', event_timestamp=self.now * 1000)
        self.assertEqual(result['status'], 'collecting')
        self.backend.publish.assert_not_called()
        self.analyze.return_value = self.organize(2)
        result = self.workflow.receive_image(self.KEY, 'role-a', event_timestamp=(self.now - 1) * 1000,
                                             image_set={'id': 'album', 'index': 2, 'total': 2})
        self.assertEqual(result['status'], 'published')

    def test_new_image_during_analysis_invalidates_old_snapshot(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        def first_vision(context, images):
            self.workflow.receive_image(self.KEY, 'role-a', event_timestamp=(self.now - 1) * 1000)
            return self.organize()
        self.analyze.side_effect = [self.plan('publish', authorization={'complete': True, 'evidenceMessageId': 'done'}),
                                    first_vision, self.organize(2)]
        # unittest.Mock does not call callables in iterable side_effects.
        sequence = list(self.analyze.side_effect)
        self.analyze.side_effect = lambda context, images: ((item(context, images) if callable(item) else item)
                                                          if (item := sequence.pop(0)) is not None else None)
        result = self.workflow.handle_text(self.KEY, '都傳完了', message_id='done', event_timestamp=self.now * 1000)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(len(self.backend.publish.call_args.args[0]['images']), 2)
        self.backend.publish.assert_called_once()

    def test_cancellation_during_analysis_is_not_overwritten(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        def cancel(context, images):
            self.workflow.cancel(self.KEY)
            return self.organize()
        self.analyze.side_effect = cancel
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'cancelled')
        self.backend.publish.assert_not_called()

    def test_duplicate_image_event_does_not_reupload_or_duplicate(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.assertEqual(self.workflow.receive_image(self.KEY, 'cover')['status'], 'duplicate')
        self.assertEqual(len(self.store.get(self.KEY)['images']), 1)
        self.backend.upload_media.assert_called_once()

    def test_unknown_or_duplicate_assignment_indices_fail_closed(self):
        for assignments in [[{'index': 2, 'purpose': 'cover'}],
                            [{'index': 1, 'purpose': 'cover'}, {'index': 1, 'purpose': 'ignore'}], []]:
            with self.subTest(assignments=assignments):
                self.setUp()
                self.begin()
                self.workflow.receive_image(self.KEY, 'cover')
                self.analyze.return_value = self.plan('publish', assignments=assignments)
                self.assertEqual(self.workflow.finish(self.KEY)['status'], 'analysis_failed')
                self.backend.publish.assert_not_called()

    def test_model_failure_and_retry_keep_saved_images(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.analyze.side_effect = RuntimeError('private model detail')
        result = self.workflow.finish(self.KEY)
        self.assertEqual(result['status'], 'analysis_failed')
        self.assertNotIn('private', result['message'])
        self.analyze.side_effect = None
        self.analyze.return_value = self.organize()
        self.workflow.retry(self.KEY)
        self.backend.upload_media.assert_called_once()

    def test_failed_claim_does_not_download_or_upload(self):
        self.begin()
        self.store.fail_when = lambda value: bool(value['images'])
        with self.assertRaises(WorkflowStorageError):
            self.workflow.receive_image(self.KEY, 'cover')
        self.download.assert_not_called()
        self.backend.upload_media.assert_not_called()

    def test_legacy_images_and_manual_assignment_survive_migration(self):
        legacy = ListingWorkflow(store=self.store, backend=self.backend, download=self.download,
                                 identify=Mock(return_value={'confidence': 0.99, 'matchesScript': True}),
                                 clock=lambda: 1700000000)
        legacy.begin(self.KEY, self.DATA)
        legacy.label_images(self.KEY, purpose='cover')
        legacy.receive_image(self.KEY, 'cover')
        legacy.confirm_cover(self.KEY, 1)
        job_id = self.store.get(self.KEY)['jobId']
        self.analyze.return_value = self.plan('organize', authorization={
            'publish': True, 'complete': True, 'evidenceMessageId': 'done'})
        self.analyze.side_effect = [self.analyze.return_value, self.plan('publish', assignments=[{'index': 1, 'purpose': 'ignore'}])]
        result = self.workflow.handle_text(self.KEY, '陸總，資料傳完，直接上架', message_id='done', eligible=True)
        self.assertEqual(result['status'], 'published')
        job = self.backend.publish.call_args.args[0]
        self.assertEqual(job['jobId'], job_id)
        self.assertEqual(job['images'][0]['purpose'], 'cover')
        self.assertTrue(job['images'][0]['manuallyConfirmed'])

    def test_later_real_text_can_correct_migrated_manual_cover(self):
        legacy = ListingWorkflow(store=self.store, backend=self.backend, download=self.download,
                                 identify=Mock(return_value={'confidence': 0.99, 'matchesScript': True}),
                                 clock=lambda: 1700000000)
        legacy.begin(self.KEY, self.DATA)
        legacy.label_images(self.KEY, purpose='cover')
        legacy.receive_image(self.KEY, 'old-cover')
        legacy.confirm_cover(self.KEY, 1)
        self.activate()
        self.workflow.receive_image(self.KEY, 'new-cover')
        self.analyze.side_effect = [self.plan('publish', authorization={
            'publish': True, 'complete': True, 'evidenceMessageId': 'correction'}),
            self.plan('publish', assignments=[
                {'index': 1, 'purpose': 'ignore', 'correctionEvidenceMessageId': 'correction'},
                {'index': 2, 'purpose': 'cover', 'correctionEvidenceMessageId': 'correction'}])]
        result = self.workflow.handle_text(self.KEY, '第一張不要了，第二張才是封面，傳完上架', message_id='correction')
        self.assertEqual(result['status'], 'published')
        images = self.backend.publish.call_args.args[0]['images']
        self.assertEqual([entry['purpose'] for entry in images], ['ignore', 'cover'])
        self.assertEqual(images[0]['manualEvidenceMessageId'], 'correction')

    def test_duplicate_bytes_are_retained_and_exposed_for_model_deduplication(self):
        self.begin()
        self.download.side_effect = lambda ident: b'\xff\xd8\xffsame'
        self.workflow.receive_image(self.KEY, 'first')
        self.workflow.receive_image(self.KEY, 'again')
        outcome = self.finish(self.plan('publish', assignments=[
            {'index': 1, 'purpose': 'cover'}, {'index': 2, 'purpose': 'ignore'}]))
        self.assertEqual(outcome['status'], 'published')
        context = self.analyze.call_args.args[0]
        self.assertEqual(context['images'][0]['checksum'], context['images'][1]['checksum'])
        self.assertEqual(len(self.store.get(self.KEY)['images']), 2)

    def test_cover_update_preserves_published_baseline_and_version(self):
        baseline = {'id': 'existing', 'publishedVersionId': 'published-v3',
                    'content': deepcopy(self.DATA), 'hasUnpublishedChanges': False,
                    'cover': {'url': 'https://assets.invalid/old'}, 'characters': [{'name': '甲'}]}
        self.backend.lookup.return_value = baseline
        self.workflow.begin(self.KEY, {'名稱': self.DATA['名稱']}, kind='cover')
        self.activate(kind='cover')
        self.workflow.receive_image(self.KEY, 'new-cover')
        self.analyze.side_effect = [self.plan('publish', kind='cover', authorization={
            'publish': True, 'complete': True, 'evidenceMessageId': 'done'}), self.organize(kind='cover')]
        result = self.workflow.handle_text(self.KEY, '封面傳完，幫我換好', message_id='done')
        self.assertEqual(result['status'], 'published')
        job = self.backend.publish.call_args.args[0]
        self.assertEqual(job['baseline'], baseline)
        self.assertEqual(job['expectedVersion'], 'published-v3')
        self.assertEqual(job['data'], self.DATA)

    def test_existing_role_roster_is_available_before_batch_vision(self):
        self.backend.lookup.return_value = {'id': 'existing', 'publishedVersionId': 'v3',
                                            'content': deepcopy(self.DATA), 'hasUnpublishedChanges': False}
        self.workflow.begin(self.KEY, {'名稱': self.DATA['名稱']}, kind='portraits')
        self.activate(kind='portraits')
        self.workflow.receive_image(self.KEY, 'role-a')
        def reason(context, images):
            if context['phase'] == 'intent':
                return self.plan('publish', kind='portraits', data={'角色': ['甲']}, authorization={
                    'publish': True, 'complete': True, 'evidenceMessageId': 'done'})
            self.assertEqual(context['data']['角色'], ['甲', '乙'])
            return self.plan('publish', kind='portraits', assignments=[{'index': 1, 'purpose': 'portraits', 'roleName': '甲'}])
        self.analyze.side_effect = reason
        result = self.workflow.handle_text(self.KEY, '角色圖傳完了，幫我補上', message_id='done')
        self.assertEqual(result['status'], 'published')
        self.assertEqual(self.backend.publish.call_args.args[0]['data']['角色'], ['甲', '乙'])

    def test_only_verified_backend_result_can_report_published(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.backend.publish.return_value = {'status': 'pending'}
        result = self.finish()
        self.assertNotEqual(result['status'], 'published')
        self.assertTrue(self.store.get(self.KEY)['publishRequested'])

    def pending_job(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.backend.publish.return_value = {'status': 'pending', 'scriptId': 'script', 'versionId': 'v1'}
        self.backend.verify_publication.return_value = {'status': 'pending', 'scriptId': 'script', 'versionId': 'v1'}
        self.finish()
        self.analyze.side_effect = None

    def test_pending_status_only_reads_back_and_never_republishes(self):
        self.pending_job()
        original = self.store.get(self.KEY)
        self.analyze.return_value = self.plan('status')
        result = self.workflow.handle_text(self.KEY, '現在上架狀態如何', message_id='status', eligible=True)
        self.assertNotEqual(result['status'], 'published')
        self.backend.publish.assert_called_once()
        self.backend.verify_publication.assert_called_once()
        self.assertEqual(self.store.get(self.KEY)['data'], original['data'])
        self.assertEqual(self.analyze.call_args.args[0]['priorTask']['name'], self.DATA['名稱'])

    def test_analysis_entry_after_publication_freezes_does_not_retry(self):
        self.pending_job()
        self.workflow.finish(self.KEY)
        self.backend.publish.assert_called_once()
        self.workflow._running.add(self.KEY)
        try:
            self.workflow.finish(self.KEY)
        finally:
            self.workflow._running.discard(self.KEY)
        self.backend.publish.assert_called_once()
        self.assertTrue(self.store.get(self.KEY)['publishRequested'])

    def test_model_error_after_cancel_preserves_cancelled_receipt(self):
        self.begin()
        def cancel_then_fail(context, images):
            self.workflow.cancel(self.KEY)
            raise RuntimeError('late model failure')
        self.analyze.side_effect = cancel_then_fail
        result = self.workflow.finish(self.KEY)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result, self.store.get(self.KEY)['lastResult'])

    def test_pending_wait_or_new_data_cannot_republish_or_change_frozen_content(self):
        self.pending_job()
        original = self.store.get(self.KEY)
        self.analyze.return_value = self.plan(data={'價格': 1}, authorization={
            'complete': False, 'evidenceMessageId': 'wait'})
        self.workflow.handle_text(self.KEY, '陸總，先不要，價格要改成1', message_id='wait', eligible=True)
        self.backend.publish.assert_called_once()
        current = self.store.get(self.KEY)
        self.assertEqual(current['data'], original['data'])
        self.assertEqual(current['images'], original['images'])
        self.assertEqual(current['publishConsent'], original['publishConsent'])

    def test_pending_explicit_retry_reuses_original_publication_key(self):
        self.pending_job()
        original = self.store.get(self.KEY)
        self.analyze.return_value = self.plan('retry')
        self.workflow.handle_text(self.KEY, '請重試剛剛的發布', message_id='retry', eligible=True)
        self.assertEqual(self.backend.publish.call_count, 2)
        retried = self.backend.publish.call_args.args[0]
        self.assertEqual(retried['jobId'], original['jobId'])
        self.assertEqual(retried['publishOperationKey'], original['publishOperationKey'])

    def test_pending_cancel_does_not_republish_or_claim_rollback(self):
        self.pending_job()
        self.analyze.return_value = self.plan('cancel')
        result = self.workflow.handle_text(self.KEY, '先取消這個上架任務', message_id='cancel', eligible=True)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIn('不會自動刪除', result['message'])
        self.backend.publish.assert_called_once()

    def test_published_status_and_retry_preserve_completed_receipt(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        receipt = self.finish()
        original = self.store.get(self.KEY)
        self.analyze.side_effect = None
        for action in ('status', 'retry'):
            with self.subTest(action=action):
                self.analyze.return_value = self.plan(action)
                result = self.workflow.handle_text(self.KEY, '上架結果呢', message_id=action, eligible=True)
                self.assertEqual(result, receipt)
                self.assertEqual(self.store.get(self.KEY), original)
        self.backend.publish.assert_called_once()

    def test_published_new_task_starts_only_after_intent_and_does_not_inherit_consent(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.finish()
        original_id = self.store.get(self.KEY)['jobId']
        self.analyze.side_effect = None
        self.analyze.return_value = self.plan(data={'名稱': '另一本'})
        result = self.workflow.handle_text(self.KEY, '接著整理另一本的資料', message_id='new', eligible=True)
        self.assertEqual(result['status'], 'collecting')
        new = self.store.get(self.KEY)
        self.assertNotEqual(new['jobId'], original_id)
        self.assertEqual(new['data']['名稱'], '另一本')
        self.assertEqual(new['publishConsent'], {})
        self.assertEqual([m['messageId'] for m in new['messages']], ['new'])
        context = self.analyze.call_args.args[0]
        self.assertFalse(context['activeTask'])
        self.assertEqual(context['priorTask']['stage'], 'published')

    def test_published_ignored_message_does_not_replace_receipt(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.finish()
        original = self.store.get(self.KEY)
        self.analyze.side_effect = None
        self.analyze.return_value = self.plan('ignore')
        self.assertIsNone(self.workflow.handle_text(self.KEY, '陸總謝謝', message_id='thanks', eligible=True))
        self.assertEqual(self.store.get(self.KEY), original)

    def test_old_draft_without_capture_cannot_capture_an_uninvoked_screenshot(self):
        self.workflow.begin(self.KEY, self.DATA)
        before = self.store.get(self.KEY)
        before['analysisPending'] = True
        before['publishConsent'] = {'complete': True}
        self.store.put(self.KEY, before)
        self.analyze.reset_mock()
        self.assertEqual(self.workflow.receive_image(self.KEY, 'instructions-screenshot')['status'], 'ignored')
        self.assertIsNone(self.workflow.handle_text(self.KEY, '資料傳完直接上架', message_id='ordinary', eligible=False))
        self.assertEqual(self.store.get(self.KEY), before)
        self.download.assert_not_called()
        self.backend.upload_media.assert_not_called()
        self.analyze.assert_not_called()

    def test_keywords_without_invocation_never_start_capture(self):
        for text in ('資料傳完，直接上架', '接下來是封面', '這張角色圖是操作說明'):
            self.assertIsNone(self.workflow.handle_text(self.KEY, text, eligible=False))
        self.assertIsNone(self.store.get(self.KEY))
        self.analyze.assert_not_called()

    def test_finish_and_failure_both_reject_future_images_without_downloading(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.finish(self.organize(questions=['請確認價格']))
        calls = self.download.call_count
        self.assertEqual(self.workflow.receive_image(self.KEY, 'unrelated-after-finish',
                                                     event_timestamp=(self.now + 1) * 1000)['status'], 'ignored')
        self.assertEqual(self.download.call_count, calls)
        self.analyze.side_effect = None
        self.activate()
        self.analyze.side_effect = RuntimeError('private detail')
        self.workflow.finish(self.KEY)
        self.assertEqual(self.workflow.receive_image(self.KEY, 'unrelated-after-error')['status'], 'ignored')
        self.assertEqual(self.download.call_count, calls)
        self.assertFalse(self.workflow.has_active_capture(self.KEY))

    def test_idle_capture_expires_without_taking_new_text_or_image(self):
        self.begin()
        original_messages = self.store.get(self.KEY)['messages']
        self.now += 901
        self.analyze.reset_mock()
        self.assertIsNone(self.workflow.handle_text(self.KEY, '今天吃什麼', message_id='chat'))
        self.assertEqual(self.workflow.receive_image(self.KEY, 'ordinary-photo')['status'], 'ignored')
        self.assertEqual(self.store.get(self.KEY)['messages'], original_messages)
        self.assertEqual(self.store.get(self.KEY)['images'], [])
        self.assertFalse(self.workflow.has_active_capture(self.KEY))
        self.download.assert_not_called()
        self.analyze.assert_not_called()

    def test_ignored_chat_neither_extends_idle_timer_nor_stays_in_transcript(self):
        self.begin()
        original = self.store.get(self.KEY)
        self.now += 800
        self.analyze.return_value = self.plan('ignore')
        self.assertIsNone(self.workflow.handle_text(self.KEY, '晚餐吃什麼', message_id='chat'))
        current = self.store.get(self.KEY)
        self.assertEqual(current['messages'], original['messages'])
        self.assertEqual(current['captureSession']['lastActivityAt'], original['captureSession']['lastActivityAt'])
        self.now += 101
        self.assertEqual(self.workflow.receive_image(self.KEY, 'dinner')['status'], 'ignored')
        self.download.assert_not_called()

    def test_capture_is_bound_to_exact_group_and_sender(self):
        self.begin()
        for key in (('group', 'someone-else'), ('another-group', 'user')):
            self.assertEqual(self.workflow.receive_image(key, 'foreign-image')['status'], 'ignored')
            self.assertIsNone(self.workflow.handle_text(key, '傳完了', message_id='foreign-text'))
            self.assertIsNone(self.store.get(key))
        self.download.assert_not_called()

    def test_explicit_resume_reopens_capture_and_clears_old_completion(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        self.finish(self.organize(questions=['需要補圖']))
        old_id = self.store.get(self.KEY)['captureSession']['id']
        self.analyze.side_effect = None
        self.analyze.return_value = self.plan(captureAction='open')
        self.workflow.handle_text(self.KEY, '陸總，繼續收圖片', message_id='resume', eligible=True,
                                  event_timestamp=(self.now + 1) * 1000)
        current = self.store.get(self.KEY)
        self.assertNotEqual(current['captureSession']['id'], old_id)
        self.assertFalse(current['publishConsent']['complete'])
        before_calls = self.analyze.call_count
        self.assertEqual(self.workflow.receive_image(self.KEY, 'extra')['status'], 'collecting')
        self.assertEqual(self.analyze.call_count, before_calls)
        self.backend.publish.assert_not_called()

    def test_active_resume_keeps_current_album_session_id(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover', image_set={'id': 'album', 'index': 1, 'total': 2})
        ident = self.store.get(self.KEY)['captureSession']['id']
        self.analyze.return_value = self.plan(captureAction='open')
        self.workflow.handle_text(self.KEY, '陸總，繼續補圖', message_id='continue', eligible=True)
        current = self.store.get(self.KEY)
        self.assertEqual(current['captureSession']['id'], ident)
        self.assertEqual(self.workflow._waiting_sets(current), 1)

    def test_completed_album_rejects_late_timestamp_new_album_and_event_without_timestamp(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover', event_timestamp=(self.now - 2) * 1000,
                                    image_set={'id': 'album', 'index': 1, 'total': 2})
        self.analyze.return_value = self.plan('publish', authorization={'complete': True, 'evidenceMessageId': 'done'})
        self.workflow.handle_text(self.KEY, '傳完', message_id='done', event_timestamp=self.now * 1000)
        self.analyze.reset_mock()
        for ident, timestamp, metadata in [
            ('late-member', (self.now + 1) * 1000, {'id': 'album', 'index': 2, 'total': 2}),
            ('new-album', (self.now - 1) * 1000, {'id': 'another', 'index': 1, 'total': 1}),
            ('no-time', None, {'id': 'album', 'index': 2, 'total': 2})]:
            self.assertEqual(self.workflow.receive_image(self.KEY, ident, timestamp, metadata)['status'], 'ignored')
        self.assertEqual(self.download.call_count, 1)
        self.analyze.assert_not_called()
        self.now += 901
        self.assertEqual(self.workflow.receive_image(self.KEY, 'expired-member', (self.now - 902) * 1000,
                            {'id': 'album', 'index': 2, 'total': 2})['status'], 'ignored')
        self.assertFalse(self.store.get(self.KEY)['analysisPending'])

    def test_failed_storage_read_is_silent_without_proven_capture_scope(self):
        self.store.get = Mock(side_effect=OSError('private storage information'))
        self.assertIsNone(self.workflow.handle_text(self.KEY, '普通文字', eligible=False))
        self.assertEqual(self.workflow.receive_image(self.KEY, 'screenshot')['status'], 'ignored')
        self.assertFalse(self.workflow.has_active_capture(self.KEY))
        self.download.assert_not_called()

    def test_immediate_text_images_and_finish_queue_behind_explicit_opening(self):
        def reason(context, images):
            latest = context['messages'][-1]['messageId']
            if latest == 'start' and context['phase'] == 'intent':
                self.assertEqual(self.workflow.handle_text(self.KEY, '價格1000', 'details',
                                   (self.now - 8) * 1000)['status'], 'deferred')
                self.assertEqual(self.workflow.receive_image(self.KEY, 'cover',
                                   (self.now - 7) * 1000)['status'], 'deferred')
                self.assertEqual(self.workflow.handle_text(self.KEY, '全部傳完了', 'done',
                                   self.now * 1000)['status'], 'deferred')
                self.assertEqual([m['messageId'] for m in context['messages']], ['start'])
                self.assertEqual(self.store.get(self.KEY)['images'], [])
                return self.plan(data=self.DATA, captureAction='open', authorization={
                    'publish': True, 'evidenceMessageId': 'start'})
            if latest == 'details':
                return self.plan(data={'價格': 1000})
            if context['phase'] == 'intent':
                return self.plan('publish', authorization={'complete': True, 'evidenceMessageId': 'done'})
            return self.organize()
        self.analyze.side_effect = reason
        result = self.workflow.handle_text(self.KEY, '陸總，開始上架這本', 'start',
                                           (self.now - 10) * 1000, eligible=True)
        self.assertEqual(result['status'], 'published')
        self.assertEqual([m['messageId'] for m in self.store.get(self.KEY)['messages']], ['start', 'details', 'done'])
        self.download.assert_called_once_with('cover')

    def test_failed_candidate_returns_queued_chat_image_without_downloading_or_transcript_pollution(self):
        returned = Mock()
        self.workflow.return_chat_image = returned
        def fail(context, images):
            self.workflow.handle_text(self.KEY, '這只是聊天內容', 'queued-text')
            self.workflow.receive_image(self.KEY, 'chat-image')
            raise ValueError('secret https://private.invalid/key')
        self.analyze.side_effect = fail
        with self.assertLogs('listing_assistant', level='WARNING') as logged:
            result = self.workflow.handle_text(self.KEY, '陸總看一下', 'start', eligible=True)
        self.assertEqual(result['status'], 'analysis_failed')
        returned.assert_called_once_with(self.KEY, 'chat-image')
        self.assertNotIn('secret', str(logged.output))
        self.assertIn('ValueError', str(logged.output))
        self.assertEqual([m['messageId'] for m in self.store.get(self.KEY)['messages']], ['start'])
        self.download.assert_not_called()

    def test_image_sent_after_finish_while_intent_is_running_does_not_enter_task(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        returned = Mock()
        self.workflow.return_chat_image = returned
        def reason(context, images):
            if context['phase'] == 'intent':
                queued = self.workflow.receive_image(self.KEY, 'unrelated', (self.now + 1) * 1000)
                self.assertEqual(queued['status'], 'deferred')
                return self.plan('publish', authorization={'complete': True, 'evidenceMessageId': 'done'})
            return self.organize()
        self.analyze.side_effect = reason
        result = self.workflow.handle_text(self.KEY, '傳完了', 'done', self.now * 1000)
        self.assertEqual(result['status'], 'published')
        self.download.assert_called_once_with('cover')
        returned.assert_called_once_with(self.KEY, 'unrelated')
        self.assertEqual(len(self.store.get(self.KEY)['images']), 1)

    def test_deferred_event_remains_queued_when_durable_claim_fails(self):
        def reason(context, images):
            self.workflow.receive_image(self.KEY, 'queued-photo', (self.now - 1) * 1000)
            self.store.fail_when = lambda state: bool(state['images'])
            return self.plan(data=self.DATA, captureAction='open')
        self.analyze.side_effect = reason
        with self.assertRaises(WorkflowStorageError):
            self.workflow.handle_text(self.KEY, '陸總，開始收件', 'start', (self.now - 10) * 1000, eligible=True)
        pending = self.store.get(self.KEY)['pendingIntake']
        self.assertEqual([event['messageId'] for event in pending['events']], ['queued-photo'])
        self.download.assert_not_called()

    def test_direct_cancel_during_batch_analysis_prevents_publication(self):
        self.begin()
        self.workflow.receive_image(self.KEY, 'cover')
        def reason(context, images):
            latest = context['messages'][-1]['messageId']
            if latest == 'stop':
                return self.plan('cancel')
            if context['phase'] == 'intent':
                return self.plan('publish', authorization={'complete': True, 'evidenceMessageId': 'done'})
            queued = self.workflow.handle_text(self.KEY, '陸總先不要上，取消這次', 'stop',
                                                (self.now + 1) * 1000, eligible=True)
            self.assertEqual(queued['status'], 'deferred')
            return self.organize()
        self.analyze.side_effect = reason
        result = self.workflow.handle_text(self.KEY, '資料傳完了', 'done', self.now * 1000)
        self.assertEqual(result['status'], 'cancelled')
        self.backend.publish.assert_not_called()
        self.assertFalse(self.workflow.has_active_capture(self.KEY))

    def test_real_production_opening_response_accepts_uncited_false_complete(self):
        # Exact structured decision observed from the production provider for
        # "陸總，這本幫我上架"; a false default does not grant any permission.
        self.analyze.return_value = {
            'action': 'collect', 'captureAction': 'open', 'kind': 'upload',
            'authorization': {'publish': True, 'complete': False,
                              'publishEvidenceMessageId': 'diagnostic-start'},
            'reply': '好的，請把這本的文字資料和圖片直接傳給我。',
        }
        result = self.workflow.handle_text(self.KEY, '陸總，這本幫我上架', 'diagnostic-start',
                                           self.now * 1000, eligible=True)
        self.assertEqual(result['status'], 'collecting')
        self.assertTrue(self.workflow.has_active_capture(self.KEY))
        consent = self.store.get(self.KEY)['publishConsent']
        self.assertTrue(consent['publish'])
        self.assertFalse(consent['complete'])
        self.assertEqual(consent['publishEvidenceMessageId'], 'diagnostic-start')
        self.assertNotIn('completionEvidenceMessageId', consent)
        self.backend.publish.assert_not_called()

    def test_uncited_false_revokes_permission_without_fabricating_user_evidence(self):
        self.begin()
        self.analyze.return_value = self.plan(authorization={'publish': False})
        self.workflow.handle_text(self.KEY, '先整理給我看就好', 'reconsider', self.now * 1000)
        consent = self.store.get(self.KEY)['publishConsent']
        self.assertFalse(consent['publish'])
        self.assertNotIn('publishEvidenceMessageId', consent)
        self.backend.publish.assert_not_called()

    def test_true_permission_still_requires_real_user_text_evidence(self):
        for source in (None, 'image-id', 'nonexistent'):
            with self.subTest(source=source):
                self.setUp()
                evidence = {} if source is None else {'publishEvidenceMessageId': source}
                self.analyze.return_value = self.plan(captureAction='open', authorization={
                    'publish': True, 'complete': False, **evidence})
                with self.assertLogs('listing_assistant', level='WARNING') as logged:
                    result = self.workflow.handle_text(self.KEY, '陸總，開始上架', 'real-text', eligible=True)
                self.assertEqual(result['status'], 'analysis_failed')
                self.assertIn('reason=invalid_consent_evidence', str(logged.output))
                self.assertFalse(self.workflow.has_active_capture(self.KEY))
                self.backend.publish.assert_not_called()

    def test_valid_evidence_for_false_is_preserved(self):
        self.begin()
        self.analyze.return_value = self.plan(authorization={
            'publish': False, 'publishEvidenceMessageId': 'withdraw'})
        self.workflow.handle_text(self.KEY, '先別上架', 'withdraw', self.now * 1000)
        consent = self.store.get(self.KEY)['publishConsent']
        self.assertFalse(consent['publish'])
        self.assertEqual(consent['publishEvidenceMessageId'], 'withdraw')


if __name__ == '__main__':
    unittest.main()
