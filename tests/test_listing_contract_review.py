"""Cross-module regressions from the listing API/command contract review."""

from copy import deepcopy
import unittest
from unittest.mock import Mock

from listing_client import BotCatalogClient, build_content
from listing_commands import parse_listing_command
from listing_workflow import ListingWorkflow


BASE = {'name': '測試', 'synopsis': '原簡介', 'playerMin': 2, 'playerMax': 2,
        'durationLabel': '4小時', 'durationMinutes': 240, 'priceStatus': 'tbd', 'price': None,
        'genres': ['推理'], 'customTags': ['台中'], 'sortOrder': 3,
        'cover': {'url': 'https://example.invalid/original.jpg', 'path': '',
                  'alt': '原本的無障礙說明', 'focalX': 25, 'focalY': 60},
        'characters': [{'name': 'A', 'description': '原簡介A',
                        'image': {'url': 'https://example.invalid/a.jpg', 'path': '', 'alt': '原A'}, 'display': 'card'},
                       {'name': 'B', 'description': '原簡介B'}]}
DATA = {'名稱': '測試', '簡介': '原簡介', '人數': ['2人'], '時長': '4小時',
        '價格': None, '類型': ['推理'], '類型標籤': '台中', '角色': ['A', 'B']}


class JsonStore:
    def __init__(self):
        self.states = {}

    def get(self, key):
        return deepcopy(self.states.get(key))

    def put(self, key, state):
        self.states[key] = deepcopy(state)


class ListingContractReviewTests(unittest.TestCase):
    def test_negated_or_discussed_images_do_not_start_collection(self):
        for text in ('接下來不傳圖片，我們討論《測試》的封面',
                     '下一張封面不適合《測試》',
                     '接下來要討論《測試》的角色圖，共 7 張'):
            with self.subTest(text=text):
                self.assertIsNone(parse_listing_command(text))

    def test_quoted_title_inside_synopsis_is_not_the_command_title(self):
        command = parse_listing_command('上架劇本\n劇本：魔女論破\n簡介：曾經玩過《別本》的人會懂')
        self.assertEqual(command['data']['名稱'], '魔女論破')
        self.assertEqual(command['data']['簡介'], '曾經玩過《別本》的人會懂')

    def test_existing_portrait_update_preserves_price_metadata_cover_and_other_roles(self):
        job = {'kind': 'portraits', 'data': DATA, 'baseline': {'rawContent': BASE},
               'images': [{'purpose': 'portraits', 'confirmed': True, 'roleName': 'B',
                           'asset': {'url': 'https://new.invalid/b.jpg', 'path': 'b.jpg'}}]}
        content = build_content(job)
        for field in ('name', 'synopsis', 'playerMin', 'playerMax', 'durationLabel',
                      'durationMinutes', 'priceStatus', 'price', 'cover', 'sortOrder', 'genres', 'customTags'):
            self.assertEqual(content[field], BASE[field], field)
        self.assertEqual(content['characters'][0], BASE['characters'][0])
        self.assertEqual(content['characters'][1]['description'], '原簡介B')
        self.assertEqual(content['characters'][1]['image']['url'], 'https://new.invalid/b.jpg')

    def test_fresh_media_consumes_actual_wrapped_grant_without_forwarding_bot_secret(self):
        raw = b'\xff\xd8\xffsynthetic-image'
        root = 'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/'
        public = root + 'public/script-covers/bot/job/cover/hash.jpg'
        signed = root + 'upload/sign/script-covers/bot/job/cover/hash.jpg?token=synthetic-upload-only'
        client = BotCatalogClient(None, 'private-bot-secret', session=Mock())
        client._api = Mock(return_value={'upload': {
            'bucket': 'script-covers', 'path': 'bot/job/cover/hash.jpg', 'publicUrl': public,
            'signedUrl': signed, 'token': 'synthetic-upload-only', 'alreadyUploaded': False}})
        client.session.put.return_value.status_code = 200
        client.read_media = Mock(return_value=raw)
        asset = client.upload_media(raw, 'ignored-name.jpg', 'job:media:123', kind='character')
        self.assertEqual(asset['url'], public)
        put = client.session.put.call_args
        self.assertEqual(put.args[0], signed)
        self.assertEqual(put.kwargs['headers'], {'Content-Type': 'image/jpeg'})
        self.assertNotIn('private-bot-secret', str(put))
        self.assertEqual(client._api.call_args.kwargs['body']['kind'], 'character')

    def test_pending_workflow_restart_repeats_fixed_content_and_operation_version(self):
        client = BotCatalogClient(None, 'test')
        client.lookup = Mock(return_value=None)
        client.upload_media = Mock(return_value={'url': 'https://assets.invalid/cover.jpg', 'path': 'cover.jpg'})
        client._api = Mock(side_effect=[
            {'script': {'id': 'script', 'draftVersionId': 'v1'}, 'operationVersionId': 'v1'},
            {'publication': {'state': 'verification_pending', 'verified': False}},
            {'script': {'id': 'script', 'draftVersionId': 'newer-unrelated'}, 'operationVersionId': 'v1'},
            {'publication': {'state': 'live', 'verified': True, 'url': 'https://www.bglarp.com/scripts/test'}},
        ])
        store = JsonStore()
        arguments = dict(store=store, backend=client, download=Mock(return_value=b'\xff\xd8\xfftest'),
                         identify=Mock(return_value={'confidence': 1, 'matchesScript': True}))
        workflow = ListingWorkflow(**arguments)
        key = ('group', 'user')
        workflow.begin(key, {**DATA, '價格': 800})
        workflow.label_images(key, purpose='cover')
        workflow.receive_image(key, 'line-image')
        self.assertEqual(workflow.finish(key)['status'], 'publish_failed')
        workflow = ListingWorkflow(**arguments)
        self.assertEqual(workflow.retry(key)['status'], 'published')
        first_create, first_publish, retry_create, retry_publish = client._api.call_args_list
        self.assertEqual(first_create.kwargs, retry_create.kwargs)
        self.assertEqual(first_publish.kwargs, retry_publish.kwargs)
        self.assertEqual(retry_publish.kwargs['body'], {'expectedVersionId': 'v1'})
        client.upload_media.assert_called_once()


if __name__ == '__main__':
    unittest.main()
