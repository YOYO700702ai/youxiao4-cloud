import copy
import unittest
from unittest.mock import Mock

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
