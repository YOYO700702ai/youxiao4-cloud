"""Exercise the actual multimodal wire format without network or credentials."""
import json
import unittest
from unittest.mock import Mock, patch

from claude_adapter import ClaudeClient
from listing_reasoner import analyze_listing, decision_tool


class ListingReasonerTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.create_connection', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.http = Mock()
        self.client = ClaudeClient('synthetic-key', model='claude-opus-5-5', session=self.http)
        self.context = {'phase': 'organize', 'messages': [
            {'role': 'user', 'messageId': 'start', 'text': '陸總，這本幫我上架'},
            {'role': 'user', 'messageId': 'done', 'text': '都給你了，直接處理吧'}],
            'images': [{'index': 1}, {'index': 2}]}

    def response(self, blocks, reason='end_turn'):
        self.http.post.return_value = Mock(status_code=200)
        self.http.post.return_value.json.return_value = {
            'id': 'test-response', 'role': 'assistant', 'model': 'claude-opus-5-5',
            'content': blocks, 'stop_reason': reason,
            'usage': {'input_tokens': 20, 'output_tokens': 10}}

    def test_all_images_and_original_messages_reach_one_native_request(self):
        decision = {'action': 'publish', 'assignments': [
            {'index': 1, 'purpose': 'cover'}, {'index': 2, 'purpose': 'portraits', 'roleName': '角色甲'}],
            'authorization': {'publish': True, 'complete': True,
                              'publishEvidenceMessageId': 'start', 'completionEvidenceMessageId': 'done'}}
        self.response([{'type': 'tool_use', 'id': 'decision-1', 'name': 'prepare_listing', 'input': decision}], 'tool_use')
        images = [{'index': 1, 'bytes': b'\xff\xd8\xffjpeg'},
                  {'index': 2, 'bytes': b'\x89PNG\r\n\x1a\npng'}]
        actual = analyze_listing(self.client, 'claude-opus-5-5', self.context, images)
        self.assertEqual(actual, decision)
        self.http.post.assert_called_once()
        payload = self.http.post.call_args.kwargs['json']
        content = payload['messages'][0]['content']
        self.assertEqual(json.loads(content[0]['text']), self.context)
        self.assertEqual([b['source']['media_type'] for b in content if b['type'] == 'image'],
                         ['image/jpeg', 'image/png'])
        self.assertEqual([b['text'] for b in content if b.get('text', '').startswith('圖片')], ['圖片 1：', '圖片 2：'])
        self.assertEqual(payload['tools'][0]['name'], 'prepare_listing')
        self.assertEqual(payload['thinking'], {'type': 'adaptive'})
        self.assertNotEqual(payload.get('tool_choice', {}).get('type'), 'tool')

    def test_intent_phase_does_not_send_images(self):
        self.response([{'type': 'tool_use', 'id': 'decision-1', 'name': 'prepare_listing',
                        'input': {'action': 'collect'}}], 'tool_use')
        analyze_listing(self.client, 'claude-opus-5-5', {**self.context, 'phase': 'intent'}, [])
        payload = self.http.post.call_args.kwargs['json']
        self.assertFalse(any(b['type'] == 'image' for b in payload['messages'][0]['content']))

    def test_saved_album_uses_verified_urls_and_bounded_vision_timeout(self):
        self.response([{'type': 'tool_use', 'id': 'decision-1', 'name': 'prepare_listing',
                        'input': {'action': 'organize'}}], 'tool_use')
        url = 'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/public/script-covers/bot/task/cover/a.jpg'
        analyze_listing(self.client, 'claude-opus-5-5', self.context,
                        [{'index': 1, 'bytes': b'\xff\xd8\xfflarge-image', 'asset': {'url': url}}])
        request = self.http.post.call_args.kwargs
        visuals = [b for b in request['json']['messages'][0]['content'] if b['type'] == 'image']
        self.assertEqual(visuals, [{'type': 'image', 'source': {'type': 'url', 'url': url}}])
        self.assertEqual(request['timeout'], (4, 55))

    def test_plain_success_claim_is_not_an_executable_decision(self):
        self.response([{'type': 'text', 'text': '已上架完成！'}])
        with self.assertRaises(ValueError):
            analyze_listing(self.client, 'claude-opus-5-5', self.context, [])

    def test_multiple_or_wrong_native_tools_are_not_executed(self):
        for blocks in ([{'type': 'tool_use', 'id': 'a', 'name': 'publish_now', 'input': {}}],
                       [{'type': 'tool_use', 'id': key, 'name': 'prepare_listing', 'input': {'action': 'publish'}}
                        for key in ('a', 'b')]):
            self.response(blocks, 'tool_use')
            with self.assertRaises(ValueError):
                analyze_listing(self.client, 'claude-opus-5-5', self.context, [])

    def test_json_fallback_still_requires_the_decision_contract(self):
        self.response([{'type': 'text', 'text': '{"action":"collect","data":{"名稱":"測試"}}'}])
        self.assertEqual(analyze_listing(self.client, 'claude-opus-5-5', self.context, [])['data'], {'名稱': '測試'})
        self.response([{'type': 'text', 'text': '{"ok":true,"message":"已上架"}'}])
        with self.assertRaises(ValueError):
            analyze_listing(self.client, 'claude-opus-5-5', self.context, [])

    def test_schema_accepts_separate_sources_and_unknown_images(self):
        schema = decision_tool().function_declarations[0].parameters_json_schema
        props = schema['properties']
        self.assertIn('captureAction', schema['required'])
        self.assertEqual(props['captureAction']['enum'], ['open', 'none'])
        self.assertIn('completionEvidenceMessageId', props['authorization']['properties'])
        self.assertIn('unknown', props['assignments']['items']['properties']['purpose']['enum'])


if __name__ == '__main__':
    unittest.main()
