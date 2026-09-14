"""Offline behavior tests for the standalone script/cover workflow.

No app import, credentials, LINE requests, or third-party API calls are needed.
Run: python -m unittest discover -s tests -p test_script_workflow.py -v
"""
import contextlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from script_workflow import EventGate, ScriptWorkflow


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptWorkflowTests(unittest.TestCase):
    KEY = ('test-group', 'test-user')
    OTHER_USER = ('test-group', 'another-user')
    OTHER_GROUP = ('another-group', 'test-user')
    PAYLOAD = {'名稱': '測試劇本', '簡介': '合成測試資料', '價格': 800}
    JPEG_A = b'\xff\xd8\xffsynthetic-image-a'
    JPEG_B = b'\xff\xd8\xffsynthetic-image-b'
    PNG = b'\x89PNG\r\n\x1a\nsynthetic-png'

    def setUp(self):
        self.clock = FakeClock()
        self.images = {}
        self.image_bytes = {'image-a': self.JPEG_A, 'image-b': self.JPEG_B,
                            'image-png': self.PNG}
        self.validate = Mock(side_effect=lambda payload: dict(payload))
        self.download = Mock(side_effect=lambda image_id: self.image_bytes[image_id])
        self.upload = Mock(side_effect=lambda data, filename:
                           'https://covers.invalid/' + quote(filename))
        self.create = Mock(return_value=(True, 'https://notion.invalid/new-page'))
        self.replace = Mock(return_value=(True, '封面已更新'))
        self.workflow = ScriptWorkflow(
            images=self.images, validate=self.validate, download=self.download,
            upload=self.upload, create=self.create, replace=self.replace,
            ttl=900, clock=self.clock,
        )
        # Deliberately failing mock callbacks should not make test output noisy.
        self.output = io.StringIO()
        self.stdout_context = contextlib.redirect_stdout(self.output)
        self.stdout_context.__enter__()
        self.addCleanup(self.stdout_context.__exit__, None, None, None)

    def start_upload(self, key=None, payload=None):
        return self.workflow.start(key or self.KEY, 'upload',
                                   dict(payload or self.PAYLOAD))

    def complete_upload(self, key=None, image_id='image-a', payload=None):
        key = key or self.KEY
        self.workflow.receive_image(key, image_id)
        return self.start_upload(key=key, payload=payload)

    def assert_completed(self, outcome, key=None):
        key = key or self.KEY
        self.assertTrue(outcome['ok'])
        self.assertNotIn(key, self.workflow.jobs)
        self.assertNotIn(key, self.images)
        self.assertEqual(self.workflow.status(key), outcome)

    def test_image_before_details_completes_once(self):
        self.assertIsNone(self.workflow.receive_image(self.KEY, 'image-a'))
        self.create.assert_not_called()
        outcome = self.start_upload()
        self.assert_completed(outcome)
        self.download.assert_called_once_with('image-a')
        self.upload.assert_called_once()
        self.create.assert_called_once()
        self.assertEqual(self.create.call_args.args[0], self.PAYLOAD)
        self.replace.assert_not_called()

    def test_details_before_image_waits_then_completes(self):
        waiting = self.start_upload()
        self.assertFalse(waiting['ok'])
        self.assertTrue(waiting['waiting_image'])
        self.assertTrue(self.workflow.status(self.KEY)['waiting_image'])
        self.download.assert_not_called()
        outcome = self.workflow.receive_image(self.KEY, 'image-a')
        self.assert_completed(outcome)
        self.create.assert_called_once()

    def test_download_failure_retains_details_and_image_for_explicit_retry(self):
        self.download.side_effect = [RuntimeError('private callback diagnostic'), self.JPEG_A]
        failed = self.complete_upload()
        self.assertFalse(failed['ok'])
        self.assertIn('下載', failed['message'])
        self.assertNotIn('private callback diagnostic', failed['message'])
        self.assertIn(self.KEY, self.workflow.jobs)
        self.assertIn(self.KEY, self.images)
        self.assertEqual(self.workflow.status(self.KEY), failed)
        self.upload.assert_not_called()
        self.create.assert_not_called()
        self.assert_completed(self.workflow.retry(self.KEY))
        self.assertEqual(self.download.call_count, 2)
        self.upload.assert_called_once()
        self.create.assert_called_once()

    def test_empty_download_is_failure_and_can_be_retried(self):
        self.download.side_effect = [b'', self.JPEG_A]
        failed = self.complete_upload()
        self.assertFalse(failed['ok'])
        self.assertIn(self.KEY, self.images)
        self.upload.assert_not_called()
        self.assert_completed(self.workflow.retry(self.KEY))
        self.create.assert_called_once()

    def test_github_failure_retains_details_and_image(self):
        self.upload.side_effect = [RuntimeError('synthetic upload failure'),
                                   'https://covers.invalid/retry.jpg']
        failed = self.complete_upload()
        self.assertFalse(failed['ok'])
        self.assertIn('GitHub', failed['message'])
        self.assertIn(self.KEY, self.workflow.jobs)
        self.assertIn(self.KEY, self.images)
        self.create.assert_not_called()
        self.assert_completed(self.workflow.retry(self.KEY))
        self.assertEqual(self.upload.call_count, 2)
        self.create.assert_called_once_with(self.PAYLOAD, 'https://covers.invalid/retry.jpg')

    def test_notion_rejection_retries_without_uploading_cover_again(self):
        self.create.side_effect = [(False, 'synthetic Notion validation failure'),
                                   (True, 'https://notion.invalid/retried-page')]
        failed = self.complete_upload()
        self.assertFalse(failed['ok'])
        self.assertIn('Notion', failed['message'])
        self.assertIn(self.KEY, self.workflow.jobs)
        first_url = self.create.call_args.args[1]
        self.assert_completed(self.workflow.retry(self.KEY))
        self.download.assert_called_once()
        self.upload.assert_called_once()
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(self.create.call_args.args[1], first_url)

    def test_notion_exception_retries_without_uploading_cover_again(self):
        self.create.side_effect = [RuntimeError('private Notion diagnostic'),
                                   (True, 'https://notion.invalid/retried-page')]
        failed = self.complete_upload()
        self.assertFalse(failed['ok'])
        self.assertNotIn('private Notion diagnostic', failed['message'])
        self.assertIn(self.KEY, self.workflow.jobs)
        self.assert_completed(self.workflow.retry(self.KEY))
        self.download.assert_called_once()
        self.upload.assert_called_once()
        self.assertEqual(self.create.call_count, 2)

    def test_cover_replacement_failure_retries_saved_url(self):
        self.replace.side_effect = [(False, 'synthetic cover failure'), (True, '封面已更新')]
        waiting = self.workflow.start(self.KEY, 'cover', '  舊劇本  ')
        self.assertTrue(waiting['waiting_image'])
        failed = self.workflow.receive_image(self.KEY, 'image-a')
        self.assertFalse(failed['ok'])
        first_url = self.replace.call_args.args[1]
        self.assert_completed(self.workflow.retry(self.KEY))
        self.replace.assert_called_with('舊劇本', first_url)
        self.assertEqual(self.replace.call_count, 2)
        self.upload.assert_called_once()
        self.create.assert_not_called()

    def test_new_image_after_notion_failure_replaces_saved_cover_url(self):
        self.create.side_effect = [(False, 'synthetic Notion failure'), (True, 'done')]
        self.assertFalse(self.complete_upload()['ok'])
        first_url = self.create.call_args.args[1]
        outcome = self.workflow.receive_image(self.KEY, 'image-b')
        self.assert_completed(outcome)
        self.assertEqual(self.upload.call_count, 2)
        self.assertNotEqual(self.create.call_args.args[1], first_url)
        self.assertEqual(self.download.call_args_list[1].args, ('image-b',))

    def test_same_image_redelivery_after_success_does_not_write_or_notify_twice(self):
        original = self.complete_upload()
        repeated = self.workflow.receive_image(self.KEY, 'image-a')
        self.assertIsNone(repeated)
        self.assertEqual(self.workflow.status(self.KEY), original)
        self.create.assert_called_once()
        self.upload.assert_called_once()
        self.assertNotIn(self.KEY, self.images)

    def test_same_image_redelivery_during_failure_requires_explicit_retry(self):
        self.create.side_effect = [(False, 'synthetic failure'), (True, 'done')]
        self.assertFalse(self.complete_upload()['ok'])
        self.assertIsNone(self.workflow.receive_image(self.KEY, 'image-a'))
        self.create.assert_called_once()
        self.assert_completed(self.workflow.retry(self.KEY))
        self.assertEqual(self.create.call_count, 2)
        self.upload.assert_called_once()

    def test_retry_after_success_returns_receipt_without_writing_again(self):
        outcome = self.complete_upload()
        self.assertEqual(self.workflow.retry(self.KEY), outcome)
        self.assertEqual(self.workflow.retry(self.KEY), outcome)
        self.create.assert_called_once()
        self.upload.assert_called_once()

    def test_conflicting_operation_is_rejected_without_replacing_pending_upload(self):
        self.start_upload()
        rejected = self.workflow.start(self.KEY, 'cover', '另一個劇本')
        self.assertFalse(rejected['ok'])
        outcome = self.workflow.receive_image(self.KEY, 'image-a')
        self.assert_completed(outcome)
        self.create.assert_called_once()
        self.assertEqual(self.create.call_args.args[0], self.PAYLOAD)
        self.replace.assert_not_called()

    def test_different_payload_is_rejected_without_overwriting_pending_details(self):
        self.start_upload()
        changed = {**self.PAYLOAD, '名稱': '另一個劇本'}
        rejected = self.start_upload(payload=changed)
        self.assertFalse(rejected['ok'])
        self.workflow.receive_image(self.KEY, 'image-a')
        self.assertEqual(self.create.call_args.args[0], self.PAYLOAD)
        self.create.assert_called_once()

    def test_same_pending_request_is_safe_to_repeat(self):
        self.assertTrue(self.start_upload()['waiting_image'])
        self.assertTrue(self.start_upload()['waiting_image'])
        self.assertEqual(len(self.workflow.jobs), 1)
        self.assert_completed(self.workflow.receive_image(self.KEY, 'image-a'))
        self.create.assert_called_once()

    def test_images_and_pending_jobs_are_isolated_by_group_and_user(self):
        self.workflow.receive_image(self.KEY, 'image-a')
        for other_key in (self.OTHER_USER, self.OTHER_GROUP):
            waiting = self.start_upload(key=other_key, payload={'名稱': '其他人的劇本'})
            self.assertTrue(waiting['waiting_image'])
        self.create.assert_not_called()
        self.assert_completed(self.start_upload())
        self.assertEqual(self.create.call_count, 1)
        for other_key in (self.OTHER_USER, self.OTHER_GROUP):
            self.assertTrue(self.workflow.status(other_key)['waiting_image'])
            self.assert_completed(self.workflow.receive_image(other_key, 'image-b'), other_key)
        self.assertEqual(self.create.call_count, 3)
        self.assertEqual([call.args[0]['名稱'] for call in self.create.call_args_list],
                         ['測試劇本', '其他人的劇本', '其他人的劇本'])

    def test_cancel_removes_pending_job_and_image_without_running_remote_callbacks(self):
        self.download.side_effect = RuntimeError('synthetic download failure')
        self.complete_upload()
        cancelled = self.workflow.cancel(self.KEY)
        self.assertTrue(cancelled['ok'])
        self.assertNotIn(self.KEY, self.workflow.jobs)
        self.assertNotIn(self.KEY, self.images)
        self.assertFalse(self.workflow.retry(self.KEY)['ok'])
        self.upload.assert_not_called()
        self.create.assert_not_called()
        self.replace.assert_not_called()

    def test_cancel_one_user_does_not_cancel_other_users(self):
        self.start_upload()
        self.start_upload(key=self.OTHER_USER)
        self.workflow.cancel(self.KEY)
        self.assertTrue(self.workflow.status(self.OTHER_USER)['waiting_image'])
        self.assert_completed(self.workflow.receive_image(self.OTHER_USER, 'image-b'), self.OTHER_USER)
        self.create.assert_called_once()

    def test_pending_job_expires_and_late_image_does_not_auto_publish(self):
        self.start_upload()
        self.clock.advance(900)
        expired = self.workflow.status(self.KEY)
        self.assertFalse(expired['ok'])
        self.assertIn('超過', expired['message'])
        self.assertNotIn(self.KEY, self.workflow.jobs)
        self.assertIsNone(self.workflow.receive_image(self.KEY, 'image-a'))
        self.create.assert_not_called()
        self.assert_completed(self.start_upload())
        self.create.assert_called_once()

    def test_old_image_expires_before_new_details_arrive(self):
        self.workflow.receive_image(self.KEY, 'image-a')
        self.clock.advance(900)
        waiting = self.start_upload()
        self.assertTrue(waiting['waiting_image'])
        self.assertNotIn(self.KEY, self.images)
        self.download.assert_not_called()
        self.create.assert_not_called()

    def test_completed_receipt_expires_without_repeating_side_effects(self):
        outcome = self.complete_upload()
        self.assertEqual(self.workflow.status(self.KEY), outcome)
        self.clock.advance(900)
        self.assertFalse(self.workflow.status(self.KEY)['ok'])
        self.assertFalse(self.workflow.retry(self.KEY)['ok'])
        self.create.assert_called_once()

    def test_failed_job_keeps_its_available_image_for_the_retry_window(self):
        self.workflow.receive_image(self.KEY, 'image-a')
        self.clock.advance(840)
        self.download.side_effect = [RuntimeError('synthetic transient failure'), self.JPEG_A]
        self.assertFalse(self.start_upload()['ok'])
        # The failed operation promises a retry window. Its older image must
        # remain usable even when the original image-arrival TTL passes.
        self.clock.advance(120)
        self.assert_completed(self.workflow.retry(self.KEY))
        self.assertEqual(self.download.call_count, 2)
        self.create.assert_called_once()

    def test_validation_error_preserves_image_and_has_no_remote_side_effects(self):
        self.workflow.receive_image(self.KEY, 'image-a')
        self.validate.side_effect = ValueError('缺少劇本名稱')
        rejected = self.start_upload()
        self.assertFalse(rejected['ok'])
        self.assertIn('缺少劇本名稱', rejected['message'])
        self.assertNotIn(self.KEY, self.workflow.jobs)
        self.assertIn(self.KEY, self.images)
        self.download.assert_not_called()
        self.create.assert_not_called()

    def test_missing_sender_is_rejected_before_creating_a_job(self):
        for key in (None, ('', 'user'), ('group', '')):
            with self.subTest(key=key):
                outcome = self.workflow.start(key, 'upload', self.PAYLOAD)
                self.assertFalse(outcome['ok'])
        self.assertFalse(self.workflow.jobs)
        self.validate.assert_not_called()
        self.download.assert_not_called()
        self.create.assert_not_called()

    def test_long_colliding_titles_receive_bounded_distinct_cover_urls(self):
        prefix = '測試' * 100
        first = {'名稱': prefix + '/第一本'}
        second = {'名稱': prefix + ':第二本'}
        self.assert_completed(self.complete_upload(payload=first))
        self.assert_completed(self.complete_upload(key=self.OTHER_USER, payload=second), self.OTHER_USER)
        filenames = [call.args[1] for call in self.upload.call_args_list]
        urls = [call.args[1] for call in self.create.call_args_list]
        self.assertNotEqual(filenames[0], filenames[1])
        self.assertNotEqual(urls[0], urls[1])
        for filename in filenames:
            self.assertLessEqual(len(filename), 120)
            self.assertFalse(any(char in filename for char in '\\/*?:"<>|'))

    def test_same_title_with_different_images_gets_different_cover_urls(self):
        self.assert_completed(self.complete_upload())
        self.assert_completed(self.complete_upload(key=self.OTHER_USER, image_id='image-b'), self.OTHER_USER)
        urls = [call.args[1] for call in self.create.call_args_list]
        self.assertNotEqual(urls[0], urls[1])

    def test_png_and_jpeg_content_keep_appropriate_filename_extensions(self):
        self.complete_upload(image_id='image-png')
        self.complete_upload(key=self.OTHER_USER, image_id='image-a')
        filenames = [call.args[1] for call in self.upload.call_args_list]
        self.assertTrue(filenames[0].endswith('.png'))
        self.assertTrue(filenames[1].endswith('.jpg'))

    def test_concurrent_retry_is_serialized_and_publishes_only_once(self):
        self.workflow.receive_image(self.KEY, 'image-a')
        upload_entered = threading.Event()
        release_upload = threading.Event()
        retry_started = threading.Event()

        def blocked_upload(data, filename):
            upload_entered.set()
            if not release_upload.wait(timeout=5):
                raise AssertionError('test did not release mock upload')
            return 'https://covers.invalid/serialized.jpg'

        def concurrent_retry():
            retry_started.set()
            return self.workflow.retry(self.KEY)

        self.upload.side_effect = blocked_upload
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.start_upload)
            try:
                self.assertTrue(upload_entered.wait(timeout=2))
                second = executor.submit(concurrent_retry)
                self.assertTrue(retry_started.wait(timeout=2))
                self.assertFalse(second.done())
                self.create.assert_not_called()
            finally:
                release_upload.set()
            first_outcome = first.result(timeout=3)
            second_outcome = second.result(timeout=3)
        self.assert_completed(first_outcome)
        self.assertEqual(second_outcome, first_outcome)
        self.download.assert_called_once()
        self.upload.assert_called_once()
        self.create.assert_called_once()


    def test_invalid_cover_url_does_not_reach_notion_and_remains_retryable(self):
        invalid_values = [None, '', 123, 'http://covers.invalid/image.jpg', '/relative.jpg']
        self.upload.side_effect = invalid_values + ['https://covers.invalid/valid.jpg']
        failed = self.complete_upload()
        self.assertFalse(failed['ok'])
        self.create.assert_not_called()
        for _ in invalid_values[1:]:
            failed = self.workflow.retry(self.KEY)
            self.assertFalse(failed['ok'])
            self.assertIn(self.KEY, self.workflow.jobs)
            self.assertIn(self.KEY, self.images)
            self.create.assert_not_called()
        self.assert_completed(self.workflow.retry(self.KEY))
        self.create.assert_called_once_with(self.PAYLOAD, 'https://covers.invalid/valid.jpg')

    def test_remember_result_exposes_direct_action_receipt_without_remote_calls(self):
        outcome = {'ok': True, 'message': '合成的修改完成紀錄'}
        self.workflow.remember_result(self.KEY, outcome)
        self.assertEqual(self.workflow.status(self.KEY), outcome)
        self.assertEqual(self.workflow.retry(self.KEY), outcome)
        self.download.assert_not_called()
        self.upload.assert_not_called()
        self.create.assert_not_called()
        self.replace.assert_not_called()

    def test_remembered_failure_is_isolated_expires_and_can_be_cancelled(self):
        outcome = {'ok': False, 'message': '合成的直接操作失敗紀錄'}
        self.workflow.remember_result(self.KEY, outcome)
        self.assertEqual(self.workflow.status(self.KEY), outcome)
        self.assertNotEqual(self.workflow.status(self.OTHER_USER), outcome)
        self.clock.advance(900)
        self.assertNotEqual(self.workflow.status(self.KEY), outcome)
        self.workflow.remember_result(self.KEY, outcome)
        self.workflow.cancel(self.KEY)
        self.assertNotEqual(self.workflow.status(self.KEY), outcome)
        self.create.assert_not_called()


    def test_chat_cannot_consume_image_owned_by_a_failed_script_job(self):
        self.upload.side_effect = [RuntimeError('synthetic GitHub failure'),
                                   'https://covers.invalid/retry.jpg']
        self.assertFalse(self.complete_upload()['ok'])
        self.assertIsNone(self.workflow.take_chat_image(self.KEY))
        self.assertIn(self.KEY, self.images)
        self.assert_completed(self.workflow.retry(self.KEY))
        self.create.assert_called_once()

    def test_chat_consumes_only_its_users_recent_unowned_image_once(self):
        self.workflow.receive_image(self.KEY, 'image-a')
        self.assertIsNone(self.workflow.take_chat_image(self.OTHER_USER))
        self.assertIn(self.KEY, self.images)
        taken = self.workflow.take_chat_image(self.KEY)
        self.assertEqual(taken[0], 'image-a')
        self.assertIsNone(self.workflow.take_chat_image(self.KEY))
        self.assertNotIn(self.KEY, self.images)
        self.workflow.receive_image(self.KEY, 'image-b')
        self.clock.advance(300)
        self.assertIsNone(self.workflow.take_chat_image(self.KEY))
        self.assertIn(self.KEY, self.images)  # Still available for a script's longer TTL.
        self.create.assert_not_called()
        self.upload.assert_not_called()



class EventGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = EventGate(ttl=900, clock=self.clock)

    def event(self, event_id='event-a', message_id='message-a'):
        return SimpleNamespace(webhook_event_id=event_id,
                               message=SimpleNamespace(id=message_id))

    def test_completed_event_redelivery_does_not_run_handler_twice(self):
        callback = Mock(return_value='original receipt')
        wrapped = self.gate.wrap(callback)
        self.assertEqual(wrapped(self.event()), 'original receipt')
        self.assertIsNone(wrapped(self.event()))
        callback.assert_called_once()

    def test_handler_failure_releases_event_for_a_later_retry(self):
        callback = Mock(side_effect=[RuntimeError('synthetic callback failure'), 'retried receipt'])
        wrapped = self.gate.wrap(callback)
        with self.assertRaisesRegex(RuntimeError, 'synthetic callback failure'):
            wrapped(self.event())
        self.assertEqual(wrapped(self.event()), 'retried receipt')
        self.assertIsNone(wrapped(self.event()))
        self.assertEqual(callback.call_count, 2)

    def test_message_id_is_used_when_webhook_event_id_is_missing(self):
        callback = Mock(return_value='fallback receipt')
        wrapped = self.gate.wrap(callback)
        event = self.event(event_id=None)
        self.assertEqual(wrapped(event), 'fallback receipt')
        self.assertIsNone(wrapped(event))
        callback.assert_called_once()

    def test_distinct_webhook_events_are_not_deduplicated_by_shared_message_id(self):
        callback = Mock(return_value='receipt')
        wrapped = self.gate.wrap(callback)
        self.assertEqual(wrapped(self.event('event-a', 'same-message')), 'receipt')
        self.assertEqual(wrapped(self.event('event-b', 'same-message')), 'receipt')
        self.assertEqual(callback.call_count, 2)

    def test_event_without_any_identifier_still_reaches_handler(self):
        callback = Mock(return_value='unidentified receipt')
        wrapped = self.gate.wrap(callback)
        event = SimpleNamespace()
        self.assertEqual(wrapped(event), 'unidentified receipt')
        self.assertEqual(wrapped(event), 'unidentified receipt')
        self.assertEqual(callback.call_count, 2)

    def test_completed_marker_expires_at_ttl(self):
        callback = Mock(return_value='receipt')
        wrapped = self.gate.wrap(callback)
        wrapped(self.event())
        self.clock.advance(899)
        self.assertIsNone(wrapped(self.event()))
        self.clock.advance(1)
        self.assertEqual(wrapped(self.event()), 'receipt')
        self.assertEqual(callback.call_count, 2)

    def test_concurrent_redelivery_does_not_enter_handler_twice(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_handler(event):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError('test did not release mock handler')
            return 'first receipt'

        callback = Mock(side_effect=blocked_handler)
        wrapped = self.gate.wrap(callback)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(wrapped, self.event())
            try:
                self.assertTrue(entered.wait(timeout=2))
                duplicate = executor.submit(wrapped, self.event())
                self.assertIsNone(duplicate.result(timeout=2))
                callback.assert_called_once()
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3), 'first receipt')
        self.assertIsNone(wrapped(self.event()))
        callback.assert_called_once()

    def test_failed_event_does_not_remove_another_events_completed_marker(self):
        completed = Mock(return_value='completed')
        self.gate.wrap(completed)(self.event('event-a'))
        failing = Mock(side_effect=ValueError('synthetic failure'))
        with self.assertRaises(ValueError):
            self.gate.wrap(failing)(self.event('event-b'))
        self.assertIsNone(self.gate.wrap(completed)(self.event('event-a')))
        retry = Mock(return_value='retry completed')
        self.assertEqual(self.gate.wrap(retry)(self.event('event-b')), 'retry completed')
        completed.assert_called_once()
        retry.assert_called_once()


if __name__ == '__main__':
    unittest.main()
