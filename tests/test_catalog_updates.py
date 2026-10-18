"""Scoped catalog writes and exact-version publication receipts, no live calls."""
import copy
import unittest
from unittest.mock import Mock

from catalog_updates import build_update_patch, update_catalog_script
from listing_client import BotCatalogClient, ListingServiceError


SOURCE = {'id': 'script', 'name': '妖魔鬼怪', 'status': 'published', 'syncStatus': 'synced',
          'draftVersionId': 'v1', 'publishedVersionId': 'v1',
          'price': 800, 'priceStatus': 'fixed', 'customTags': ['歡樂', '沉浸'],
          'genres': ['機制'], 'playerMin': 6, 'playerMax': 6,
          'durationLabel': '3小時', 'durationMinutes': 180, 'synopsis': '原本\u200b簡介',
          'cover': {'url': 'https://example.invalid/cover.webp', 'focalX': 35, 'focalY': 60},
          'characters': [{'name': '角色甲', 'description': '保留簡介', 'display': 'card',
                          'image': {'url': 'https://example.invalid/a.webp'}},
                         {'name': '角色乙', 'description': '第二個'}], 'sortOrder': 3}


def make_client():
    client = Mock(spec=BotCatalogClient)
    client.lookup.return_value = {'id': 'script', 'versionId': 'v1', 'publishedVersionId': 'v1',
                                  'hasUnpublishedChanges': False, 'rawContent': copy.deepcopy(SOURCE)}
    client._api.side_effect = [{'script': {'id': 'script', 'draftVersionId': 'v2'}, 'operationVersionId': 'v2'},
                              {'publication': {'state': 'live', 'verified': True}}]
    client.verify_publication.return_value = {'status': 'published', 'url': 'https://www.bglarp.com/scripts/test'}
    return client


class CatalogUpdateTests(unittest.TestCase):
    def test_price_patch_only_changes_price_and_status_with_version_precondition(self):
        client = make_client()
        outcome = update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})
        self.assertTrue(outcome['ok'])
        patch, publish = client._api.call_args_list
        self.assertEqual(patch.args, ('PATCH', '/script'))
        self.assertEqual(patch.kwargs['body'], {'content': {'price': 1000, 'priceStatus': 'fixed'}, 'expectedVersionId': 'v1'})
        self.assertEqual(publish.args, ('POST', '/script/publish'))
        self.assertEqual(publish.kwargs['body'], {'expectedVersionId': 'v2'})
        self.assertEqual(client.verify_publication.call_args.args[0]['publicationTarget'], {'scriptId': 'script', 'versionId': 'v2'})
        self.assertEqual(client.lookup.return_value['rawContent'], SOURCE)

    def test_append_tag_keeps_existing_tags_and_omits_every_other_content_field(self):
        client = make_client()
        update_catalog_script(client, '妖魔鬼怪', {'新增類型標籤': '新手 / 歡樂'})
        content = client._api.call_args_list[0].kwargs['body']['content']
        self.assertEqual(content, {'customTags': ['歡樂', '沉浸', '新手']})

    def test_replace_tag_and_explicit_clear_are_not_append(self):
        self.assertEqual(build_update_patch({'類型標籤': '新手'}, SOURCE), {'customTags': ['新手']})
        self.assertEqual(build_update_patch({'類型標籤': None}, SOURCE), {'customTags': []})

    def test_retained_role_keeps_image_description_and_display(self):
        patch = build_update_patch({'角色': '角色甲/新角色'}, SOURCE)
        self.assertEqual(patch['characters'][0], SOURCE['characters'][0])
        self.assertEqual(patch['characters'][1], {'name': '新角色', 'description': ''})
        self.assertEqual(set(patch), {'characters'})
        patch['characters'][0]['image']['url'] = 'changed'
        self.assertEqual(SOURCE['characters'][0]['image']['url'], 'https://example.invalid/a.webp')

    def test_explicit_clear_optional_fields_is_preserved(self):
        self.assertEqual(build_update_patch({'價格': None, '角色': None, '類型': None}, SOURCE),
                         {'price': None, 'priceStatus': 'tbd', 'characters': [], 'genres': []})

    def test_model_integral_float_price_is_accepted_but_fraction_nan_and_bool_are_not(self):
        self.assertEqual(build_update_patch({'價格': 1000.0}, SOURCE), {'price': 1000, 'priceStatus': 'fixed'})
        for invalid in (True, 1.5, float('inf'), float('nan')):
            with self.assertRaises(ListingServiceError):
                build_update_patch({'價格': invalid}, SOURCE)

    def test_duration_does_not_retain_conflicting_old_minutes(self):
        self.assertEqual(build_update_patch({'時長': '4小時'}, SOURCE), {'durationLabel': '4小時', 'durationMinutes': None})

    def test_people_range_is_explicit(self):
        self.assertEqual(build_update_patch({'人數': '5～8人'}, SOURCE), {'playerMin': 5, 'playerMax': 8})

    def test_invalid_or_unpublishable_fields_fail_before_writes(self):
        for fields in ({}, {'未知': '值'}, {'價格': True}, {'價格': -1}, {'價格': 100001},
                       {'人數': '5人/7人'}, {'人數': '浮動人'}, {'人數': None},
                       {'時長': None}, {'劇情簡介': ''}, {'新增類型標籤': None},
                       {'類型標籤': '替換', '新增類型標籤': '新增'}, {'類型標籤': '長' * 31}):
            with self.subTest(fields=fields):
                client = make_client()
                outcome = update_catalog_script(client, '妖魔鬼怪', fields)
                self.assertFalse(outcome['ok'])
                client._api.assert_not_called()

    def test_missing_or_unpublished_script_is_never_edited(self):
        for baseline in (None, {'id': 'script', 'versionId': 'v2', 'publishedVersionId': 'v1',
                                'hasUnpublishedChanges': True, 'rawContent': SOURCE},
                         {'id': 'script', 'versionId': 'v1', 'publishedVersionId': 'v1',
                          'hasUnpublishedChanges': False, 'rawContent': {**SOURCE, 'status': 'unpublished'}}):
            with self.subTest(baseline=baseline):
                client = make_client()
                client.lookup.return_value = baseline
                outcome = update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})
                self.assertFalse(outcome['ok'])
                client._api.assert_not_called()

    def test_unchanged_request_does_not_create_another_version(self):
        client = make_client()
        client._api.side_effect = None
        client._api.return_value = {'publication': {'state': 'live', 'verified': True}}
        outcome = update_catalog_script(client, '妖魔鬼怪', {'新增類型標籤': '歡樂'})
        self.assertTrue(outcome['ok'])
        client._api.assert_not_called()
        client.verify_publication.assert_called_once()

    def test_unchanged_but_pending_request_only_reads_existing_version(self):
        client = make_client()
        client.verify_publication.return_value = {'status': 'pending'}
        outcome = update_catalog_script(client, '妖魔鬼怪', {'價格': 800})
        self.assertFalse(outcome['ok'])
        self.assertEqual(outcome['status'], 'pending')
        client._api.assert_not_called()
        self.assertEqual(client.verify_publication.call_args.args[0]['publicationTarget'], {'scriptId': 'script', 'versionId': 'v1'})

    def test_unchanged_request_cannot_publish_after_concurrent_staff_edit(self):
        client = make_client()
        client.verify_publication.return_value = {'status': 'conflict'}
        self.assertFalse(update_catalog_script(client, '妖魔鬼怪', {'價格': 800})['ok'])
        client._api.assert_not_called()

    def test_publish_response_cannot_claim_success_without_verified_readback(self):
        for status in ('pending', 'failed', 'unknown', 'conflict'):
            with self.subTest(status=status):
                client = make_client()
                client.verify_publication.return_value = {'status': status}
                outcome = update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})
                self.assertFalse(outcome['ok'])
                self.assertIn('尚未確認', outcome['message'])
                self.assertEqual(outcome['versionId'], 'v2')

    def test_publish_uses_operation_version_not_latest_draft(self):
        client = make_client()
        client._api.side_effect = [{'script': {'id': 'script', 'draftVersionId': 'other-staff-draft'}, 'operationVersionId': 'v2'}, {}]
        update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})
        self.assertEqual(client._api.call_args.kwargs['body'], {'expectedVersionId': 'v2'})

    def test_wrong_script_response_is_never_published(self):
        client = make_client()
        client._api.side_effect = [{'script': {'id': 'wrong', 'draftVersionId': 'v2'}, 'operationVersionId': 'v2'}]
        self.assertFalse(update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})['ok'])
        self.assertEqual(client._api.call_count, 1)
        client.verify_publication.assert_not_called()

    def test_missing_operation_version_never_falls_back_to_latest_staff_draft(self):
        client = make_client()
        client._api.side_effect = [{'script': {'id': 'script', 'draftVersionId': 'other-staff-draft'}}]
        outcome = update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})
        self.assertFalse(outcome['ok'])
        self.assertEqual(client._api.call_count, 1)
        client.verify_publication.assert_not_called()

    def test_version_conflict_and_publish_timeout_never_claim_completion(self):
        for side_effect in ([ListingServiceError('版本衝突', 409)],
                            [{'script': {'id': 'script', 'draftVersionId': 'v2'}, 'operationVersionId': 'v2'},
                             ListingServiceError('連線逾時')]):
            client = make_client()
            client._api.side_effect = side_effect
            self.assertFalse(update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})['ok'])
            client.verify_publication.assert_not_called()

    def test_transport_error_does_not_direct_field_edit_to_unrelated_listing_job(self):
        client = make_client()
        client.lookup.side_effect = ListingServiceError('官網連線逾時，資料已保留。請傳「重試上架」接續，勿重新建立同名劇本。')
        outcome = update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})
        self.assertFalse(outcome['ok'])
        self.assertNotIn('重試上架', outcome['message'])

    def test_real_client_readback_requires_synced_live_exact_version(self):
        for sync_status, state, verified, version, expected in (
                ('synced', 'live', True, 'v2', True),
                ('pending', 'live', True, 'v2', False),
                ('synced', 'verification_pending', False, 'v2', False),
                ('synced', 'live', True, 'later', False)):
            with self.subTest(sync=sync_status, state=state, version=version):
                client = BotCatalogClient(None, 'test')
                client._api = Mock(side_effect=[{'scripts': [SOURCE]},
                    {'script': {'id': 'script', 'draftVersionId': 'v2'}, 'operationVersionId': 'v2'}, {},
                    {'script': {**SOURCE, 'draftVersionId': version, 'publishedVersionId': version, 'syncStatus': sync_status},
                     'publication': {'state': state, 'verified': verified, 'url': 'https://www.bglarp.com/scripts/test'}}])
                self.assertEqual(update_catalog_script(client, '妖魔鬼怪', {'價格': 1000})['ok'], expected)


if __name__ == '__main__':
    unittest.main()
