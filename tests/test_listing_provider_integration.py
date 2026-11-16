"""Replay a real provider decision through the actual bridge and workflow.

The Anthropic HTTP response is the only model boundary faked here. The real
ClaudeClient response conversion, listing reasoner, and workflow validation all
run; no app startup, credentials, network request, or LINE message is involved.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_adapter import ClaudeClient, MESSAGES_URL
from listing_assistant import AssistantListingWorkflow
from listing_reasoner import analyze_listing


# Exact prepare_listing arguments observed in the live Opus dry run. In
# particular, the harmless false default has no completion evidence field.
OBSERVED_START_DECISION = {
    'action': 'collect',
    'captureAction': 'open',
    'kind': 'upload',
    'authorization': {
        'publish': True,
        'complete': False,
        'publishEvidenceMessageId': 'diagnostic-start',
    },
    'reply': '好的，請把這本的文字資料和圖片直接傳給我。',
}


class JsonStore:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return json.loads(json.dumps(self.values.get(tuple(key))))

    def put(self, key, state):
        self.values[tuple(key)] = json.loads(json.dumps(state))


class ListingProviderIntegrationTests(unittest.TestCase):
    KEY = ('staff-group', 'requesting-user')

    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.http = Mock()
        self.client = ClaudeClient('offline-synthetic-key', model='claude-opus-5-5', session=self.http)
        self.addCleanup(self.client.close)
        self.store = JsonStore()
        self.backend = Mock()
        self.download = Mock(side_effect=AssertionError('unexpected image download'))
        self.workflow = AssistantListingWorkflow(
            store=self.store, backend=self.backend, download=self.download,
            analyze=lambda context, images: analyze_listing(
                self.client, 'claude-opus-5-5', context, images),
            clock=lambda: 1700000000.0,
        )
        self.set_provider_decision(OBSERVED_START_DECISION)

    def set_provider_decision(self, decision):
        self.http.post.return_value = Mock(status_code=200)
        self.http.post.return_value.json.return_value = {
            'id': 'offline-native-tool-response', 'type': 'message',
            'role': 'assistant', 'model': 'claude-opus-5-5',
            'content': [{'type': 'tool_use', 'id': 'prepare-listing-1',
                         'name': 'prepare_listing', 'input': deepcopy(decision)}],
            'stop_reason': 'tool_use',
            'usage': {'input_tokens': 100, 'output_tokens': 50},
        }

    def start(self):
        return self.workflow.handle_text(
            self.KEY, '陸總，這本幫我上架', message_id='diagnostic-start',
            event_timestamp=1700000000000, eligible=True)

    def test_observed_native_decision_opens_capture_and_keeps_other_senders_out(self):
        result = self.start()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'collecting')
        self.assertTrue(self.workflow.has_active_capture(self.KEY))
        saved = self.store.get(self.KEY)
        self.assertEqual(saved['captureSession']['startMessageId'], 'diagnostic-start')
        self.assertTrue(saved['publishConsent']['publish'])
        self.assertFalse(saved['publishConsent']['complete'])
        self.assertFalse(saved['publishRequested'])
        self.assertEqual(saved['images'], [])
        self.assertEqual([m['messageId'] for m in saved['messages']], ['diagnostic-start'])

        self.http.post.assert_called_once()
        request = self.http.post.call_args
        self.assertEqual(request.args, (MESSAGES_URL,))
        body = request.kwargs['json']
        self.assertEqual(body['model'], 'claude-opus-5-5')
        self.assertEqual(body['tools'][0]['name'], 'prepare_listing')
        self.assertEqual(body['thinking'], {'type': 'adaptive'})
        context = json.loads(body['messages'][0]['content'][0]['text'])
        self.assertEqual(context['phase'], 'intent')
        self.assertEqual(context['messages'][-1]['messageId'], 'diagnostic-start')
        self.assertTrue(context['explicitlyInvoked'])

        for foreign_key in (('staff-group', 'another-user'), ('another-group', 'requesting-user')):
            with self.subTest(key=foreign_key):
                outcome = self.workflow.receive_image(
                    foreign_key, 'unrelated-instruction-screenshot', event_timestamp=1700000001000)
                self.assertEqual(outcome['status'], 'ignored')
                self.assertIsNone(self.store.get(foreign_key))
        self.assertEqual(self.store.get(self.KEY), saved)
        self.download.assert_not_called()
        self.assertEqual(self.backend.mock_calls, [])
        self.http.post.assert_called_once()

    def test_native_publish_true_still_requires_real_user_evidence(self):
        decision = deepcopy(OBSERVED_START_DECISION)
        decision['authorization']['publishEvidenceMessageId'] = 'invented-user-message'
        self.set_provider_decision(decision)
        with self.assertLogs('listing_assistant', level='WARNING'):
            result = self.start()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'analysis_failed')
        self.assertFalse(self.workflow.has_active_capture(self.KEY))
        saved = self.store.get(self.KEY)
        self.assertFalse(saved['publishRequested'])
        self.assertEqual(saved['images'], [])
        self.assertEqual([m['messageId'] for m in saved['messages']], ['diagnostic-start'])
        self.download.assert_not_called()
        self.backend.publish.assert_not_called()
        self.http.post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
