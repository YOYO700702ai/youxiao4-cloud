import copy
import unittest
from unittest.mock import Mock, patch

from listing_client import BotCatalogClient, ListingServiceError, RemoteJobStore, build_content


BASE = {'name': '測試', 'synopsis': '原簡介', 'playerMin': 2, 'playerMax': 2,
        'durationLabel': '4小時', 'durationMinutes': None, 'priceStatus': 'fixed', 'price': 800,
        'genres': ['推理'], 'customTags': ['台中'], 'sortOrder': 3,
        'cover': {'url': 'https://example.invalid/original.jpg', 'path': '', 'alt': '原封面'},
        'characters': [{'name': 'A', 'description': '原簡介A', 'image': {'url': 'https://example.invalid/a.jpg', 'path': '', 'alt': 'A'}, 'display': 'card'},
                       {'name': 'B', 'description': '原簡介B'}]}
DATA = {'名稱': '測試', '簡介': '原簡介', '人數': ['2人'], '時長': '4小時',
        '價格': 800, '類型': ['推理'], '類型標籤': '台中', '角色': ['A', 'B']}


class ListingClientTests(unittest.TestCase):
    def test_add_one_role_preserves_other_media_and_descriptions(self):
        job = {'kind': 'portraits', 'data': DATA, 'baseline': {'rawContent': BASE},
               'images': [{'purpose': 'portraits', 'confirmed': True, 'roleName': 'B',
                           'asset': {'url': 'https://new.invalid/b.jpg', 'path': 'b.jpg'}}]}
        out = build_content(job)
        self.assertEqual(out['characters'][0], BASE['characters'][0])
        self.assertEqual(out['characters'][1]['description'], '原簡介B')
        self.assertEqual(out['characters'][1]['image']['url'], 'https://new.invalid/b.jpg')
        self.assertEqual(out['cover'], BASE['cover'])
        self.assertEqual(BASE['characters'][1], {'name': 'B', 'description': '原簡介B'})

    def test_unknown_or_discontinuous_players_fail_before_network(self):
        for people in [['浮動人'], ['2人', '4人']]:
            with self.assertRaises(ListingServiceError):
                build_content({'data': {**DATA, '人數': people}})

    def test_publish_uses_operation_version_not_current_draft(self):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(side_effect=[
            {'script': {'id': 'script', 'draftVersionId': 'unrelated-newer'}, 'operationVersionId': 'original'},
            {'publication': {'state': 'live', 'verified': True, 'url': 'https://www.bglarp.com/scripts/test'}}])
        result = client.publish({'jobId': 'job', 'kind': 'upload', 'data': DATA,
                                 'baseline': {'rawContent': BASE}})
        self.assertEqual(result['status'], 'published')
        self.assertEqual(client._api.call_args.kwargs['body'], {'expectedVersionId': 'original'})

    def test_202_or_unverified_never_claims_publication(self):
        for publication in [{'state': 'sync_pending'}, {'state': 'live', 'verified': False}]:
            client = BotCatalogClient(None, 'test')
            client._api = Mock(side_effect=[{'script': {'id': 'script', 'draftVersionId': 'v'}}, {'publication': publication}])
            self.assertEqual(client.publish({'jobId': 'job', 'kind': 'upload', 'data': DATA, 'baseline': {'rawContent': BASE}})['status'], 'pending')

    @staticmethod
    def pending_publish_client(*readbacks):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(side_effect=[
            {'script': {'id': 'script', 'draftVersionId': 'v1'}, 'operationVersionId': 'v1'},
            {'publication': {'state': 'verification_pending', 'verified': False}},
            *readbacks])
        job = {'jobId': 'job', 'kind': 'upload', 'data': DATA, 'baseline': {'rawContent': BASE}}
        return client, job

    @staticmethod
    def readback_payload(state='verification_pending', **script_patch):
        return {'script': {'id': 'script', 'draftVersionId': 'v1', 'publishedVersionId': 'v1',
                           'status': 'published', 'syncStatus': 'synced', **script_patch},
                'publication': {'state': state, 'verified': state == 'live',
                                'url': 'https://www.bglarp.com/scripts/test'}}

    def test_publish_waits_briefly_for_public_readback_without_republishing(self):
        client, job = self.pending_publish_client(self.readback_payload(), self.readback_payload('live'))
        with patch('listing_client.time.monotonic', return_value=0), patch('listing_client.time.sleep'):
            result = client.publish(job)
        self.assertEqual(result['status'], 'published')
        self.assertEqual((result['scriptId'], result['versionId']), ('script', 'v1'))
        self.assertEqual([call.args for call in client._api.call_args_list],
                         [('POST', ''), ('POST', '/script/publish'), ('GET', '/script'), ('GET', '/script')])
        for read in client._api.call_args_list[2:]:
            self.assertEqual(read.kwargs['request_id'], 'job')
            self.assertEqual(read.kwargs['timeout'], (1, 5))
            self.assertNotIn('body', read.kwargs)

    def test_public_readback_still_pending_after_two_reads_is_not_completion(self):
        client, job = self.pending_publish_client(self.readback_payload(), self.readback_payload())
        with patch('listing_client.time.monotonic', return_value=0), patch('listing_client.time.sleep') as sleep:
            result = client.publish(job)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['versionId'], 'v1')
        self.assertEqual(client._api.call_count, 4)
        self.assertEqual(sleep.call_count, 2)

    def test_readback_conflict_or_auth_failure_stops_without_another_read_or_write(self):
        cases = [(self.readback_payload('live', draftVersionId='staff-later'), 'conflict'),
                 (self.readback_payload('live', publishedVersionId='staff-later'), 'conflict'),
                 (ListingServiceError('驗證失敗', 401), 'failed'),
                 (ListingServiceError('禁止存取', 403), 'failed'),
                 (ListingServiceError('版本衝突', 409), 'conflict')]
        for readback, expected in cases:
            with self.subTest(expected=expected, readback=readback):
                client, job = self.pending_publish_client(readback)
                with patch('listing_client.time.monotonic', return_value=0), patch('listing_client.time.sleep'):
                    result = client.publish(job)
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['versionId'], 'v1')
                self.assertEqual(client._api.call_count, 3)
                self.assertEqual(client._api.call_args.args, ('GET', '/script'))

    def test_unpublished_or_unsynced_readback_does_not_keep_polling(self):
        for changed in ({'status': 'draft'}, {'syncStatus': 'error'}, {'publishedVersionId': None}):
            with self.subTest(changed=changed):
                client, job = self.pending_publish_client(self.readback_payload(**changed))
                with patch('listing_client.time.monotonic', return_value=0), patch('listing_client.time.sleep'):
                    self.assertEqual(client.publish(job)['status'], 'pending')
                self.assertEqual(client._api.call_count, 3)

    def test_short_readback_timeout_preserves_pending_target_and_stops(self):
        client, job = self.pending_publish_client(ListingServiceError('官網連線逾時'))
        with patch('listing_client.time.monotonic', return_value=0), patch('listing_client.time.sleep'):
            result = client.publish(job)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual((result['scriptId'], result['versionId']), ('script', 'v1'))
        self.assertEqual(client._api.call_count, 3)

    def test_slow_initial_publish_does_not_add_readback_waits(self):
        client, job = self.pending_publish_client()
        with patch('listing_client.time.monotonic', side_effect=[0, 41, 41]), patch('listing_client.time.sleep') as sleep:
            self.assertEqual(client.publish(job)['status'], 'pending')
        self.assertEqual(client._api.call_count, 2)
        sleep.assert_not_called()

    def test_readback_timeout_shrinks_to_remaining_total_budget(self):
        client, job = self.pending_publish_client(self.readback_payload(), self.readback_payload())
        with patch('listing_client.time.monotonic', side_effect=[0, 0, 0, 0.25, 6.5, 7.25]), patch('listing_client.time.sleep'):
            self.assertEqual(client.publish(job)['status'], 'pending')
        final_timeout = client._api.call_args.kwargs['timeout']
        self.assertLessEqual(sum(final_timeout), 0.75)
        self.assertEqual(client._api.call_count, 4)

    def test_custom_readback_timeout_reaches_transport(self):
        session = Mock()
        session.request.return_value = Mock(status_code=200)
        session.request.return_value.json.return_value = self.readback_payload('live')
        client = BotCatalogClient(None, 'test', session=session)
        result = client.verify_publication({'jobId': 'job', 'publicationTarget': {
            'scriptId': 'script', 'versionId': 'v1'}}, timeout=(0.5, 1.5))
        self.assertEqual(result['status'], 'published')
        self.assertEqual(session.request.call_args.kwargs['timeout'], (0.5, 1.5))

    def test_verify_publication_only_accepts_the_saved_operation_version(self):
        client = BotCatalogClient(None, 'test')
        job = {'jobId': 'job', 'publicationTarget': {'scriptId': 'script', 'versionId': 'v1'}}
        live = {'script': {'id': 'script', 'draftVersionId': 'v1', 'publishedVersionId': 'v1',
                           'status': 'published', 'syncStatus': 'synced'},
                'publication': {'state': 'live', 'verified': True, 'url': 'https://www.bglarp.com/scripts/test'}}
        client._api = Mock(return_value=live)
        self.assertEqual(client.verify_publication(job)['status'], 'published')
        client._api.assert_called_once_with('GET', '/script', request_id='job')
        for patch in ({'id': 'other'}, {'draftVersionId': 'later'}, {'publishedVersionId': 'other'}):
            client._api.return_value = {**live, 'script': {**live['script'], **patch}}
            self.assertEqual(client.verify_publication(job)['status'], 'conflict')
        for patch in ({'publishedVersionId': None}, {'status': 'draft'}, {'syncStatus': 'error'}):
            client._api.return_value = {**live, 'script': {**live['script'], **patch}}
            self.assertEqual(client.verify_publication(job)['status'], 'pending')
        client._api.return_value = {**live, 'publication': {'state': 'live', 'verified': False}}
        self.assertEqual(client.verify_publication(job)['status'], 'pending')

    def test_verification_of_saved_unpublished_update_can_retry_its_exact_draft(self):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(return_value={
            'script': {'id': 'script', 'draftVersionId': 'v2', 'publishedVersionId': 'v1'},
            'publication': {'state': 'draft', 'verified': False}})
        job = {'jobId': 'job', 'expectedVersion': 'v1',
               'publicationTarget': {'scriptId': 'script', 'versionId': 'v2'}}
        self.assertEqual(client.verify_publication(job)['status'], 'pending')

    def test_legacy_pending_target_recovery_uses_original_operation_without_publish(self):
        client = BotCatalogClient(None, 'test')
        job = {'jobId': 'old-job', 'kind': 'upload', 'data': DATA, 'baseline': {'rawContent': BASE},
               'publishRequested': True, 'lastPublishStatus': 'pending'}
        client._api = Mock(side_effect=[
            {'script': {'id': 'script', 'draftVersionId': 'v1'}, 'operationVersionId': 'v1'},
            {'script': {'id': 'script', 'draftVersionId': 'v1', 'publishedVersionId': 'v1',
                        'status': 'published', 'syncStatus': 'synced'},
             'publication': {'state': 'live', 'verified': True, 'url': 'https://www.bglarp.com/scripts/test'}}])
        self.assertEqual(client.verify_publication(job)['status'], 'published')
        create, verify = client._api.call_args_list
        self.assertEqual(create.args, ('POST', ''))
        self.assertEqual(create.kwargs['operation'], 'create')
        self.assertEqual(create.kwargs['request_id'], 'old-job')
        self.assertEqual(verify.args, ('GET', '/script'))
        self.assertNotIn('/publish', str(client._api.call_args_list))

    def test_legacy_pending_never_accepts_a_newer_same_name_draft(self):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(side_effect=[
            {'script': {'id': 'script', 'draftVersionId': 'staff-later'}, 'operationVersionId': 'v1'},
            {'script': {'id': 'script', 'draftVersionId': 'staff-later', 'publishedVersionId': 'staff-later'},
             'publication': {'state': 'live', 'verified': True}}])
        job = {'jobId': 'old-job', 'kind': 'upload', 'data': DATA, 'baseline': {'rawContent': BASE},
               'publishRequested': True, 'lastPublishStatus': 'pending'}
        result = client.verify_publication(job)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['versionId'], 'v1')

    def test_legacy_pending_requires_original_operation_version_in_replay_response(self):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(return_value={'script': {'id': 'script', 'draftVersionId': 'staff-later'}})
        job = {'jobId': 'old-job', 'kind': 'upload', 'data': DATA, 'baseline': {'rawContent': BASE},
               'publishRequested': True, 'lastPublishStatus': 'pending'}
        self.assertEqual(client.verify_publication(job)['status'], 'failed')
        client._api.assert_called_once()

    def test_unknown_job_is_not_created_by_status_and_network_errors_do_not_publish(self):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(side_effect=ListingServiceError('連線失敗'))
        self.assertEqual(client.verify_publication({'jobId': 'job'})['status'], 'unknown')
        client._api.assert_not_called()
        self.assertEqual(client.verify_publication({'jobId': 'job', 'publicationTarget': {
            'scriptId': 'script', 'versionId': 'v1'}})['status'], 'failed')
        self.assertEqual(client._api.call_args.args, ('GET', '/script'))

    def test_failed_publish_keeps_operation_target_for_later_readback(self):
        client = BotCatalogClient(None, 'test')
        client._api = Mock(side_effect=[{'script': {'id': 'script', 'draftVersionId': 'v1'}},
                                       ListingServiceError('逾時')])
        result = client.publish({'jobId': 'job', 'kind': 'upload', 'data': DATA,
                                 'baseline': {'rawContent': BASE}})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual((result['scriptId'], result['versionId']), ('script', 'v1'))

    def test_errors_do_not_include_request_token(self):
        session = Mock()
        response = Mock(status_code=401)
        response.json.return_value = {'error': 'private diagnostic'}
        session.request.return_value = response
        client = BotCatalogClient(None, 'secret-test-token', session=session)
        with self.assertRaises(ListingServiceError) as error:
            client.lookup('測試')
        self.assertNotIn('secret-test-token', str(error.exception))
        self.assertNotIn('private diagnostic', str(error.exception))

    def test_remote_state_is_revision_locked_and_identifier_hashed(self):
        client = Mock()
        client._api.side_effect = [{'revision': 4, 'state': {'x': 1}}, {'revision': 5, 'state': {'x': 2}}]
        store = RemoteJobStore(client)
        key = ('private-group', 'private-user')
        self.assertEqual(store.get(key), {'x': 1})
        store.put(key, {'x': 2})
        call = client._api.call_args
        self.assertEqual(call.kwargs['body']['expectedRevision'], 4)
        self.assertNotIn('private-group', str(call))
        self.assertNotIn('private-user', str(call))
        self.assertEqual(len(call.kwargs['body']['state']['key']), 64)

    def test_media_urls_are_bounded_to_known_public_bucket(self):
        for url in ['http://localhost/private', 'https://rnipzotldpbdlzxjnoip.supabase.co.evil.test/storage/v1/object/public/script-covers/a.jpg',
                    'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/private/user/a.jpg']:
            with self.assertRaises(ListingServiceError):
                BotCatalogClient._check_asset_url(url)

    def test_existing_image_is_verified_without_new_put(self):
        client = BotCatalogClient(None, 'test')
        data = b'\xff\xd8\xfftest'
        url = 'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/public/script-covers/a.jpg'
        client._api = Mock(return_value={'publicUrl': url, 'path': 'a.jpg', 'alreadyUploaded': True})
        client.session = Mock()
        client.read_media = Mock(return_value=data)
        self.assertEqual(client.upload_media(data, 'a.jpg', 'job:media:1')['url'], url)
        client.session.put.assert_not_called()


if __name__ == '__main__':
    unittest.main()
