"""Offline safety/recovery tests for the durable listing state machine."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from listing_workflow import ListingWorkflow, WorkflowStorageError


class MemoryStore:
    """A test-only JSON-roundtrip store: mutations never persist accidentally."""

    def __init__(self):
        self.states = {}
        self.saved = []
        self.fail_when = None

    def get(self, key):
        return deepcopy(self.states.get(tuple(key)))

    def put(self, key, state):
        if self.fail_when and self.fail_when(state):
            raise OSError('private storage details')
        value = json.loads(json.dumps(state, ensure_ascii=False))
        self.states[tuple(key)] = value
        self.saved.append(deepcopy(value))


class ListingWorkflowTests(unittest.TestCase):
    KEY = ('group', 'user')
    OTHER_USER = ('group', 'other')
    OTHER_GROUP = ('other-group', 'user')
    DATA = {'名稱': '魔女論破', '人數': ['7人'], '時長': '12～14小時',
            '價格': 2300, '類型': ['推理'], '簡介': '合成測試簡介',
            '類型標籤': '台中獨家、頭腦風暴、雙主持互動',
            '角色': ['貪婪魔女的親眷', '嫉妒魔女的親眷', '暴食魔女的親眷',
                   '色慾魔女的親眷', '傲慢魔女的親眷', '憤怒魔女的親眷', '怠惰魔女的親眷']}

    def setUp(self):
        self.now = 1_700_000_000.0
        self.store = MemoryStore()
        self.backend = Mock()
        self.backend.lookup.return_value = None
        self.remote_assets = {}
        self.backend.upload_media.side_effect = self.upload
        self.backend.publish.return_value = {'status': 'published',
                                             'url': 'https://www.bglarp.com/scripts/test',
                                             'scriptId': 'script', 'versionId': 'published-v1'}
        self.backend.read_media.side_effect = lambda asset: self.remote_assets[asset['path']]['bytes']
        self.download = Mock(side_effect=lambda message_id: b'\xff\xd8\xff' + message_id.encode())
        self.identify = Mock(return_value={'roleName': None, 'confidence': 0.99, 'matchesScript': True})
        self.make_workflow()

    def make_workflow(self):
        self.workflow = ListingWorkflow(store=self.store, backend=self.backend,
                                        download=self.download, identify=self.identify,
                                        clock=lambda: self.now)

    def upload(self, raw, filename, operation_key, *, kind):
        # A real backend must provide the same guarantee for lost responses.
        if operation_key not in self.remote_assets:
            self.remote_assets[operation_key] = {'bytes': raw, 'filename': filename, 'kind': kind}
        return {'url': 'https://assets.invalid/' + filename, 'path': operation_key}

    def begin(self, data=None, kind='upload', key=None):
        return self.workflow.begin(key or self.KEY, deepcopy(data or self.DATA), kind)

    def cover(self, key=None, message_id='cover'):
        key = key or self.KEY
        self.assertTrue(self.workflow.label_images(key, self.DATA['名稱'], 'cover')['ok'])
        return self.workflow.receive_image(key, message_id, self.now * 1000)

    def complete_ready_job(self):
        self.begin()
        self.cover()

    def baseline(self, **extra):
        return {'id': 'script-existing', 'content': deepcopy(self.DATA),
                'versionId': 'published-v7', 'publishedVersionId': 'published-v7',
                'hasUnpublishedChanges': False,
                'cover': {'url': 'https://assets.invalid/original.jpg', 'path': 'original'},
                'characters': [{'name': name, 'description': '原正式敘述'} for name in self.DATA['角色']],
                **extra}

    def test_task_details_and_images_never_publish_without_explicit_finish(self):
        self.complete_ready_job()
        self.workflow.retry(self.KEY)
        self.workflow.status(self.KEY)
        self.backend.publish.assert_not_called()
        self.assertEqual(self.store.get(self.KEY)['stage'], 'collecting')
        result = self.workflow.finish(self.KEY)
        self.assertEqual(result['status'], 'published')
        self.backend.publish.assert_called_once()

    def test_task_is_persisted_before_lookup(self):
        def lookup(name):
            state = self.store.get(self.KEY)
            self.assertEqual(state['stage'], 'initializing')
            self.assertEqual(state['data']['名稱'], name)
        self.backend.lookup.side_effect = lookup
        self.begin({'名稱': self.DATA['名稱']})

    def test_partial_data_can_be_saved_and_missing_fields_block_finish(self):
        self.begin({'名稱': self.DATA['名稱']})
        self.cover()
        result = self.workflow.finish(self.KEY)
        self.assertEqual(result['status'], 'needs_input')
        self.assertIn('人數', result['message'])
        self.assertFalse(self.store.get(self.KEY)['publishRequested'])
        self.workflow.merge_fields(self.KEY, self.DATA)
        self.backend.publish.assert_not_called()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_fixed_players_and_roles_must_match(self):
        payload = deepcopy(self.DATA)
        payload['角色'] = payload['角色'][:6]
        self.begin(payload)
        self.cover()
        result = self.workflow.finish(self.KEY)
        self.assertIn('與固定人數 7人 不符', result['message'])
        self.backend.publish.assert_not_called()

    def test_roles_are_optional_but_cover_is_required_for_new_script(self):
        payload = deepcopy(self.DATA)
        payload.pop('角色')
        self.begin(payload)
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'needs_input')
        self.cover()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_free_price_is_valid(self):
        payload = {**self.DATA, '價格': 0}
        self.begin(payload)
        self.cover()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_four_players_and_nonlegacy_genres_are_valid(self):
        payload = {**self.DATA, '人數': ['4人'], '類型': ['新手', '家庭歡樂'],
                   '角色': ['一', '二', '三', '四']}
        self.assertTrue(self.begin(payload)['ok'])
        self.cover()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_floating_people_needs_explicit_bounds_before_finish(self):
        self.begin({**self.DATA, '人數': ['浮動人'], '角色': []})
        self.cover()
        result = self.workflow.finish(self.KEY)
        self.assertIn('浮動人數還缺明確範圍', result['message'])
        self.workflow.merge_fields(self.KEY, {'人數': ['5～8人']})
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_normalization_rejects_unknown_fields_invalid_types_and_player_bounds(self):
        for extra in ({'新增欄位': '不能寫'}, {'人數': ['31人']}, {'人數': ['0人']},
                      {'人數': ['8～5人']}, {'價格': True}, {'類型': [123]},
                      {'類型': ['新手', '新手']}, {'簡介': ['不能猜']}):
            with self.subTest(extra=extra):
                self.assertFalse(self.begin({**self.DATA, **extra})['ok'])
        self.backend.lookup.assert_not_called()

    def test_website_field_limits_are_rejected_before_any_remote_operation(self):
        too_large = [
            {'名稱': '名' * 121}, {'名稱': '😀' * 61}, {'簡介': '字' * 8001},
            {'時長': '字' * 81}, {'類型': ['字' * 31]},
            {'類型': [f'分類{i}' for i in range(21)]},
            {'類型標籤': '字' * 31}, {'類型標籤': '、'.join(f'標籤{i}' for i in range(21))},
            {'角色': ['字' * 81]}, {'角色': [f'角色{i}' for i in range(31)]},
        ]
        for extra in too_large:
            with self.subTest(extra=extra):
                self.assertFalse(self.begin({**self.DATA, **extra})['ok'])
        self.backend.lookup.assert_not_called()
        self.backend.publish.assert_not_called()

    def test_rejected_field_patch_keeps_job_editable(self):
        self.complete_ready_job()
        before = self.store.get(self.KEY)['data']
        self.assertFalse(self.workflow.merge_fields(self.KEY, {'簡介': '字' * 8001})['ok'])
        self.assertEqual(self.store.get(self.KEY)['data'], before)
        self.assertFalse(self.store.get(self.KEY)['publishRequested'])
        self.assertTrue(self.workflow.merge_fields(self.KEY, {'簡介': '已修正'})['ok'])
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_discontinuous_players_block_before_freezing_publication(self):
        self.begin({**self.DATA, '人數': ['2人', '4人'], '角色': []})
        self.cover()
        result = self.workflow.finish(self.KEY)
        self.assertIn('人數範圍必須連續', result['message'])
        self.assertFalse(self.store.get(self.KEY)['publishRequested'])
        self.workflow.merge_fields(self.KEY, {'人數': ['2～4人']})
        self.assertEqual(self.store.get(self.KEY)['data']['人數'], ['2人', '3人', '4人'])
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_role_count_outside_player_range_blocks_before_publish(self):
        self.begin({**self.DATA, '人數': ['6～8人'], '角色': self.DATA['角色'][:5]})
        self.cover()
        result = self.workflow.finish(self.KEY)
        self.assertIn('與人數範圍 6～8人不符', result['message'])
        self.assertFalse(self.store.get(self.KEY)['publishRequested'])
        self.workflow.merge_fields(self.KEY, {'角色': self.DATA['角色']})
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_existing_new_options_do_not_block_picture_updates(self):
        content = {**self.DATA, '人數': ['4人'], '類型': ['新手'],
                   '角色': ['一', '二', '三', '四']}
        self.backend.lookup.return_value = self.baseline(content=content)
        self.assertTrue(self.begin({'名稱': self.DATA['名稱']}, 'cover')['ok'])
        self.cover()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_label_without_name_uses_active_task(self):
        self.begin()
        self.assertTrue(self.workflow.label_images(self.KEY, purpose='cover')['ok'])
        self.assertTrue(self.workflow.receive_image(self.KEY, 'cover')['ok'])

    def test_oversized_image_is_rejected_before_upload(self):
        self.begin()
        self.download.side_effect = None
        self.download.return_value = b'x' * (8 * 1024 * 1024 + 1)
        result = self.cover()
        self.assertEqual(result['status'], 'media_failed')
        self.backend.upload_media.assert_not_called()

    def test_no_job_or_unlabelled_chat_images_are_never_consumed(self):
        self.assertEqual(self.workflow.receive_image(self.KEY, 'old-chat')['status'], 'ignored')
        self.begin()
        self.assertEqual(self.workflow.receive_image(self.KEY, 'chat-after-begin')['status'], 'ignored')
        self.cover()
        self.download.assert_called_once_with('cover')
        self.assertEqual(len(self.store.get(self.KEY)['images']), 1)

    def test_old_timestamp_and_invalid_timestamp_do_not_consume_batch(self):
        self.begin()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'cover')
        for timestamp in ((self.now - 5) * 1000, float('nan'), 'yesterday', True):
            result = self.workflow.receive_image(self.KEY, 'bad-time', timestamp)
            self.assertEqual(result['status'], 'ignored')
        self.assertEqual(self.store.get(self.KEY)['batches'][0]['received'], 0)
        self.download.assert_not_called()
        self.assertTrue(self.workflow.receive_image(self.KEY, 'new', self.now * 1000)['ok'])

    def test_exact_batch_count_closes_and_ignores_later_images(self):
        self.begin()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 2)
        self.identify.side_effect = [
            {'roleName': role, 'confidence': 1, 'matchesScript': True}
            for role in self.DATA['角色'][:2]]
        for index in range(2):
            self.workflow.receive_image(self.KEY, f'role-{index}')
        result = self.workflow.receive_image(self.KEY, 'extra')
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(self.download.call_count, 2)
        self.assertTrue(self.store.get(self.KEY)['batches'][0]['closed'])
        self.backend.publish.assert_not_called()

    def test_declared_batch_must_be_complete_and_each_image_paired(self):
        self.complete_ready_job()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 2)
        self.workflow.receive_image(self.KEY, 'role-one')
        result = self.workflow.finish(self.KEY)
        self.assertIn('還差 1 張', result['message'])
        self.assertIn('人工配對', result['message'])
        self.backend.publish.assert_not_called()

    def test_group_and_user_tasks_and_images_are_isolated(self):
        self.begin()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'cover')
        for key in (self.OTHER_USER, self.OTHER_GROUP):
            self.assertEqual(self.workflow.receive_image(key, 'their-image')['status'], 'ignored')
            self.begin(key=key)
            self.cover(key=key, message_id='own-image')
        self.assertEqual(self.store.get(self.KEY)['images'], [])
        self.assertNotEqual(self.store.get(self.OTHER_GROUP)['jobId'], self.store.get(self.OTHER_USER)['jobId'])

    def test_fuzzy_role_and_simplified_spelling_require_manual_exact_pairing(self):
        self.complete_ready_job()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 1)
        self.identify.return_value = {'roleName': '色欲魔女的亲眷', 'confidence': 1, 'matchesScript': True}
        result = self.workflow.receive_image(self.KEY, 'ambiguous')
        self.assertIn('配對角色 2', result['message'])
        self.assertFalse(self.store.get(self.KEY)['images'][1]['confirmed'])
        self.assertFalse(self.workflow.assign_role(self.KEY, 2, '色欲魔女的亲眷')['ok'])
        self.assertTrue(self.workflow.assign_role(self.KEY, 2, '色慾魔女的親眷')['ok'])
        self.backend.publish.assert_not_called()
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_confidence_threshold_and_script_match_are_both_required(self):
        self.begin()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 4)
        self.identify.side_effect = [
            {'roleName': self.DATA['角色'][0], 'confidence': 0.949, 'matchesScript': True},
            {'roleName': self.DATA['角色'][1], 'confidence': 0.95, 'matchesScript': True},
            {'roleName': self.DATA['角色'][2], 'confidence': 1, 'matchesScript': False},
            {'roleName': self.DATA['角色'][3], 'confidence': True, 'matchesScript': True}]
        for n in range(4):
            self.workflow.receive_image(self.KEY, f'image-{n}')
        self.assertEqual([image['confirmed'] for image in self.store.get(self.KEY)['images']],
                         [False, True, False, False])

    def test_uncertain_cover_can_be_confirmed_without_reupload(self):
        self.begin()
        self.identify.return_value = {'confidence': 0.8, 'matchesScript': False}
        self.assertIn('確認圖片 1 為封面', self.cover()['message'])
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'needs_input')
        self.make_workflow()
        self.assertTrue(self.workflow.confirm_cover(self.KEY, 1)['ok'])
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')
        self.download.assert_called_once()
        self.backend.upload_media.assert_called_once()

    def test_duplicate_roles_are_blocked_without_overwriting_first_image(self):
        self.complete_ready_job()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 2)
        self.identify.return_value = {'roleName': self.DATA['角色'][0], 'confidence': 0.99, 'matchesScript': True}
        self.workflow.receive_image(self.KEY, 'role-a')
        self.workflow.receive_image(self.KEY, 'role-b')
        images = self.store.get(self.KEY)['images']
        self.assertTrue(images[1]['confirmed'])
        self.assertFalse(images[2]['confirmed'])
        self.assertEqual(images[2]['error'], 'duplicate_role')
        self.assertFalse(self.workflow.assign_role(self.KEY, 3, self.DATA['角色'][0])['ok'])
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'needs_input')
        self.assertTrue(self.workflow.assign_role(self.KEY, 3, self.DATA['角色'][1])['ok'])
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')

    def test_redelivery_never_reprocesses_an_image(self):
        self.begin()
        self.cover()
        self.make_workflow()
        self.assertEqual(self.workflow.receive_image(self.KEY, 'cover')['status'], 'duplicate')
        self.download.assert_called_once()
        self.backend.upload_media.assert_called_once()
        self.identify.assert_called_once()

    def test_completed_task_retries_and_finish_return_saved_receipt(self):
        self.complete_ready_job()
        receipt = self.workflow.finish(self.KEY)
        self.make_workflow()
        self.assertEqual(self.workflow.finish(self.KEY), receipt)
        self.assertEqual(self.workflow.retry(self.KEY), receipt)
        self.assertEqual(self.workflow.status(self.KEY), receipt)
        self.assertIn(receipt['url'], receipt['message'])
        self.backend.publish.assert_called_once()

    def test_published_task_allows_new_explicit_task_for_same_script(self):
        self.backend.lookup.return_value = self.baseline()
        self.begin({'名稱': self.DATA['名稱']}, 'cover')
        self.cover()
        first = self.workflow.finish(self.KEY)
        self.make_workflow()
        second = self.begin({'名稱': self.DATA['名稱']}, 'cover')
        self.assertNotEqual(first['jobId'], second['jobId'])
        self.assertEqual(second['status'], 'collecting')
        self.assertEqual(self.store.get(self.KEY)['images'], [])
        self.backend.publish.assert_called_once()

    def test_receipt_does_not_echo_nonofficial_public_links(self):
        self.complete_ready_job()
        self.backend.publish.return_value = {'status': 'published', 'url': 'https://www.bglarp.com.attacker.invalid/private'}
        receipt = self.workflow.finish(self.KEY)
        self.assertNotIn('url', receipt)
        self.assertNotIn('attacker.invalid', receipt['message'])

    def test_publish_failure_retries_same_job_without_reupload_after_restart(self):
        self.complete_ready_job()
        self.backend.publish.side_effect = [TimeoutError('private auth token'), self.backend.publish.return_value]
        first = self.workflow.finish(self.KEY)
        self.assertEqual(first['status'], 'publish_failed')
        self.assertNotIn('private auth token', first['message'])
        before = self.store.get(self.KEY)
        self.make_workflow()
        self.assertEqual(self.workflow.retry(self.KEY)['status'], 'published')
        attempts = self.backend.publish.call_args_list
        self.assertEqual(attempts[0].args[0]['jobId'], attempts[1].args[0]['jobId'])
        self.assertEqual(attempts[0].args[0]['publishOperationKey'], attempts[1].args[0]['publishOperationKey'])
        self.assertEqual(before['images'], attempts[1].args[0]['images'])
        self.backend.upload_media.assert_called_once()
        self.download.assert_called_once()

    def test_pending_sync_is_not_success_and_only_retry_completes(self):
        self.complete_ready_job()
        self.backend.publish.side_effect = [{'status': 'pending'}, {'status': 'published'}]
        self.assertFalse(self.workflow.finish(self.KEY)['ok'])
        self.assertEqual(self.workflow.status(self.KEY)['status'], 'publish_failed')
        self.assertFalse(self.workflow.finish(self.KEY)['ok'])
        self.assertEqual(self.backend.publish.call_count, 1)
        self.assertEqual(self.workflow.retry(self.KEY)['status'], 'published')

    def test_publication_payload_is_frozen_during_retry(self):
        self.complete_ready_job()
        self.backend.publish.return_value = {'status': 'failed'}
        self.workflow.finish(self.KEY)
        prior = self.store.get(self.KEY)
        self.assertFalse(self.workflow.merge_fields(self.KEY, {'價格': 999})['ok'])
        self.assertFalse(self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 1)['ok'])
        self.assertEqual(prior['data'], self.store.get(self.KEY)['data'])

    def test_lookup_failure_is_durable_and_retry_does_not_publish(self):
        self.backend.lookup.side_effect = [TimeoutError('private'), None]
        first = self.begin({'名稱': self.DATA['名稱']})
        self.assertEqual(first['status'], 'lookup_failed')
        self.assertNotIn('private', first['message'])
        job_id = first['jobId']
        self.make_workflow()
        result = self.workflow.retry(self.KEY)
        self.assertEqual(result['jobId'], job_id)
        self.assertEqual(result['status'], 'collecting')
        self.backend.publish.assert_not_called()

    def test_safe_lookup_conflict_guidance_survives_restart_and_retry(self):
        from listing_client import ListingServiceError

        guidance = '這本目前只有舊版資料，請先由員工後台匯入後再補圖。'
        self.backend.lookup.side_effect = [ListingServiceError(guidance, 409), self.baseline()]
        first = self.begin({'名稱': self.DATA['名稱']}, 'cover')
        self.assertEqual(first['status'], 'lookup_failed')
        self.assertIn(guidance, first['message'])
        self.assertIn('重試上架', first['message'])
        self.make_workflow()
        self.assertEqual(self.workflow.status(self.KEY)['message'], first['message'])
        retried = self.workflow.retry(self.KEY)
        self.assertEqual(retried['jobId'], first['jobId'])
        self.assertEqual(retried['status'], 'collecting')
        self.backend.publish.assert_not_called()

    def test_identification_failure_keeps_permanent_asset_for_restart(self):
        self.begin()
        self.identify.side_effect = [RuntimeError('sensitive API details'),
                                   {'confidence': 1, 'matchesScript': True}]
        self.cover()
        image = self.store.get(self.KEY)['images'][0]
        self.assertEqual(image['stage'], 'identification_failed')
        self.assertIsNotNone(image['asset'])
        self.make_workflow()
        self.workflow.retry(self.KEY)
        self.backend.read_media.assert_called_once_with(image['asset'])
        self.download.assert_called_once()
        self.backend.upload_media.assert_called_once()
        self.assertTrue(self.store.get(self.KEY)['images'][0]['confirmed'])
        self.backend.publish.assert_not_called()

    def test_identification_failure_can_be_manually_confirmed_without_model_retry(self):
        self.begin()
        self.identify.side_effect = RuntimeError('private')
        self.cover()
        self.workflow.confirm_cover(self.KEY, 1)
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')
        self.identify.assert_called_once()

    def test_upload_failure_retains_event_for_retry_without_automatic_publish(self):
        self.begin()
        self.backend.upload_media.side_effect = RuntimeError('private')
        result = self.cover()
        self.assertEqual(result['status'], 'media_failed')
        image = self.store.get(self.KEY)['images'][0]
        self.backend.upload_media.side_effect = self.upload
        self.make_workflow()
        self.workflow.retry(self.KEY)
        args = self.backend.upload_media.call_args_list
        self.assertEqual(args[0].args[2], image['operationKey'])
        self.assertEqual(args[1].args[2], image['operationKey'])
        self.backend.publish.assert_not_called()

    def test_failed_state_save_stops_before_upload(self):
        self.begin()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'cover')
        self.store.fail_when = lambda state: any(image['stage'] == 'uploading' for image in state['images'])
        with self.assertRaises(WorkflowStorageError):
            self.workflow.receive_image(self.KEY, 'image')
        self.backend.upload_media.assert_not_called()
        self.identify.assert_not_called()

    def test_lost_asset_save_reuses_idempotency_key_after_restart(self):
        self.begin()
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'cover')
        self.store.fail_when = lambda state: any(image.get('asset') for image in state['images'])
        with self.assertRaises(WorkflowStorageError):
            self.workflow.receive_image(self.KEY, 'image')
        self.assertEqual(len(self.remote_assets), 1)
        self.assertIsNone(self.store.get(self.KEY)['images'][0]['asset'])
        self.store.fail_when = None
        self.make_workflow()
        self.workflow.retry(self.KEY)
        self.assertEqual(len(self.remote_assets), 1)
        attempts = self.backend.upload_media.call_args_list
        self.assertEqual(attempts[0].args[2], attempts[1].args[2])

    def test_storage_failure_before_publish_prevents_remote_publication(self):
        self.complete_ready_job()
        self.store.fail_when = lambda state: state['stage'] == 'publishing'
        with self.assertRaises(WorkflowStorageError):
            self.workflow.finish(self.KEY)
        self.backend.publish.assert_not_called()

    def test_existing_title_is_never_overwritten_by_new_listing(self):
        self.backend.lookup.return_value = self.baseline()
        result = self.begin()
        self.assertEqual(result['status'], 'conflict')
        self.assertFalse(self.workflow.finish(self.KEY)['ok'])
        self.backend.publish.assert_not_called()

    def test_existing_portraits_use_published_baseline_and_version_lock(self):
        baseline = self.baseline()
        self.backend.lookup.return_value = baseline
        self.begin({'名稱': self.DATA['名稱']}, 'portraits')
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'portraits', 1)
        self.identify.return_value = {'roleName': self.DATA['角色'][0], 'confidence': 1, 'matchesScript': True}
        self.workflow.receive_image(self.KEY, 'role')
        self.assertEqual(self.workflow.finish(self.KEY)['status'], 'published')
        job = self.backend.publish.call_args.args[0]
        self.assertEqual(job['data'], baseline['content'])
        self.assertEqual(job['baseline'], baseline)
        self.assertEqual(job['expectedVersion'], 'published-v7')
        self.assertEqual(self.backend.upload_media.call_args.kwargs['kind'], 'character')

    def test_unpublished_draft_and_missing_version_block_existing_edits(self):
        for baseline in (self.baseline(hasUnpublishedChanges=True),
                         self.baseline(publishedVersionId=None, versionId=None)):
            with self.subTest(baseline=baseline):
                self.store = MemoryStore()
                self.make_workflow()
                self.backend.lookup.return_value = baseline
                result = self.begin({'名稱': self.DATA['名稱']}, 'cover')
                self.assertEqual(result['status'], 'conflict')
        self.backend.publish.assert_not_called()

    def test_server_version_conflict_remains_unpublished(self):
        self.backend.lookup.return_value = self.baseline()
        self.begin({'名稱': self.DATA['名稱']}, 'cover')
        self.cover()
        self.backend.publish.return_value = {'status': 'conflict'}
        result = self.workflow.finish(self.KEY)
        self.assertFalse(result['ok'])
        self.assertEqual(self.store.get(self.KEY)['lastPublishStatus'], 'conflict')
        self.assertIn('沒有覆蓋', result['message'])

    def test_cancel_disables_collection_and_new_begin_uses_new_job_id(self):
        self.begin()
        before = self.store.get(self.KEY)['jobId']
        self.workflow.label_images(self.KEY, self.DATA['名稱'], 'cover')
        self.workflow.cancel(self.KEY)
        self.assertEqual(self.workflow.receive_image(self.KEY, 'late')['status'], 'ignored')
        self.begin()
        self.assertNotEqual(before, self.store.get(self.KEY)['jobId'])
        self.backend.publish.assert_not_called()

    def test_changing_to_another_title_cannot_overwrite_active_task(self):
        self.begin()
        prior = self.store.get(self.KEY)
        self.assertFalse(self.begin({'名稱': '另一本'})['ok'])
        self.assertFalse(self.workflow.merge_fields(self.KEY, {'名稱': '另一本'})['ok'])
        self.assertEqual(self.store.get(self.KEY), prior)

    def test_asset_and_image_event_are_persisted_before_identification(self):
        self.begin()
        def identify(*args):
            state = self.store.get(self.KEY)
            self.assertEqual(state['processedEvents'], ['cover'])
            self.assertEqual(state['images'][0]['stage'], 'identifying')
            self.assertTrue(state['images'][0]['asset']['url'].startswith('https://'))
            return {'confidence': 1, 'matchesScript': True}
        self.identify.side_effect = identify
        self.cover()
        json.dumps(self.store.get(self.KEY))  # No raw image bytes in durable JSON.


if __name__ == '__main__':
    unittest.main()
