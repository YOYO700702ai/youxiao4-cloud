import hashlib
import json
import threading
import unittest

from admin_binding import PrivateAdminBinding
from listing_client import ListingServiceError, RemoteJobStore
from tests.direct_fakes import FakeWebsite

# Test-only placeholder; production reads PRIVATE_ADMIN_BIND_CODE from the environment.
CODE = 'test-only-binding-placeholder-0001'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeWebsite()

    def binding(self, code=CODE, **kwargs):
        return PrivateAdminBinding(RemoteJobStore(self.site), code, **kwargs)

    def stored(self):
        return json.dumps(self.site.jobs, ensure_ascii=False)

    def test_wrong_short_or_embedded_codes_never_bind(self):
        binding = self.binding()
        for attempt in ('', 'hello', CODE[:-1], CODE + 'x', '綁定 ' + CODE):
            self.assertIsNone(binding.try_bind('u1', attempt))
        self.assertFalse(binding.is_admin('u1'))
        self.assertEqual(self.site.jobs, {})
        weak = self.binding('short-code')
        self.assertEqual(weak.code, '')
        self.assertIsNone(weak.try_bind('u1', 'short-code'))
        self.assertFalse(weak.mentions_code('short-code'))

    def test_several_colleagues_bind_with_the_same_code_and_repeat_is_idempotent(self):
        binding = self.binding()
        self.assertEqual(binding.try_bind('u1', '  ' + CODE + '\n'), 'bound')
        self.assertEqual(binding.try_bind('u2', CODE), 'bound')
        self.assertEqual(binding.try_bind('u3', CODE), 'bound')
        self.assertEqual(binding.try_bind('u2', CODE), 'already')
        self.assertEqual(binding.try_bind('u1', CODE), 'already')
        for user in ('u1', 'u2', 'u3'):
            self.assertTrue(binding.is_admin(user))
        self.assertFalse(binding.is_admin('stranger'))
        self.assertIsNone(binding.try_bind('stranger', CODE[:-1]))
        self.assertFalse(self.binding().is_admin('stranger'))

    def test_bindings_survive_restart_and_store_no_code_or_raw_id(self):
        self.binding().try_bind('U-test-first', CODE)
        self.binding().try_bind('U-test-second', CODE)
        restarted = self.binding()
        self.assertTrue(restarted.is_admin('U-test-first'))
        self.assertTrue(restarted.is_admin('U-test-second'))
        self.assertFalse(restarted.is_admin('someone-else'))
        for secret in (CODE, 'U-test-first', 'U-test-second'):
            self.assertNotIn(secret, self.stored())

    def test_legacy_single_admin_record_is_recognized_and_kept_when_others_join(self):
        # Exactly what the single-admin release persisted for its one account.
        legacy_hash = hashlib.sha256(b'line-private-admin:U-test-legacy').hexdigest()
        RemoteJobStore(self.site).put(PrivateAdminBinding.KEY, {'adminUserHash': legacy_hash, 'boundAt': 1})
        self.assertTrue(self.binding().is_admin('U-test-legacy'))
        self.assertEqual(self.binding().try_bind('U-test-legacy', CODE), 'already')
        self.assertEqual(self.binding().try_bind('U-test-new', CODE), 'bound')
        restarted = self.binding()
        self.assertTrue(restarted.is_admin('U-test-legacy'))
        self.assertTrue(restarted.is_admin('U-test-new'))
        (state,) = [s for _, s in self.site.jobs.values()]
        # The legacy field still names the first administrator, so nobody rebinds.
        self.assertEqual((state['adminUserHash'], state['boundAt']), (legacy_hash, 1))

    def test_concurrent_new_binds_from_two_workers_both_survive(self):
        RemoteJobStore(self.site).put(PrivateAdminBinding.KEY, {
            'adminUserHash': hashlib.sha256(b'line-private-admin:U-test-legacy').hexdigest()})
        barrier = threading.Barrier(2)
        writes = []

        def hold_first_writes(method, path):
            if method == 'PUT':
                writes.append(path)
                if len(writes) <= 2:
                    barrier.wait(timeout=5)  # Both have read the same revision before either writes.
        self.site.on_call = hold_first_writes
        conflicts = []
        original = self.site._job

        def record_conflicts(method, ident, body):
            try:
                return original(method, ident, body)
            except ListingServiceError as exc:
                conflicts.append(exc.status)
                raise
        self.site._job = record_conflicts
        a, b = self.binding(), self.binding()
        results = {}
        threads = [threading.Thread(target=lambda n=n, x=x: results.__setitem__(n, x.try_bind(n, CODE)))
                   for n, x in (('u1', a), ('u2', b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.site.on_call = None
        self.assertEqual(conflicts, [409])  # A real revision conflict happened and was merged.
        self.assertEqual(results, {'u1': 'bound', 'u2': 'bound'})
        fresh = self.binding()
        for user in ('u1', 'u2', 'U-test-legacy'):
            self.assertTrue(fresh.is_admin(user))

    def test_other_worker_sees_a_newly_bound_colleague_after_the_cache_window(self):
        clock = Clock()
        a = self.binding(clock=clock, cache_seconds=60)
        b = self.binding(clock=clock, cache_seconds=60)
        self.assertEqual(a.try_bind('u1', CODE), 'bound')
        self.assertTrue(b.is_admin('u1'))
        self.assertFalse(b.is_admin('u2'))  # b now caches a member list without u2.
        self.assertEqual(a.try_bind('u2', CODE), 'bound')
        self.assertTrue(a.is_admin('u2'))
        clock.now += 61
        self.assertTrue(b.is_admin('u2'))
        gets = []
        self.site.job_get_hook = gets.append
        for _ in range(5):
            self.assertTrue(b.is_admin('u1'))
            self.assertTrue(b.is_admin('u2'))
        self.assertEqual(gets, [])  # Known administrators need no lookups.

    def test_lost_write_response_is_read_back_before_acknowledging(self):
        binding = self.binding()
        real_put = binding.store.put

        def put_then_timeout(key, state):
            real_put(key, state)
            raise ListingServiceError('官網連線暫時失敗', code='timeout', retryable=True)
        binding.store.put = put_then_timeout
        self.assertEqual(binding.try_bind('u1', CODE), 'bound')
        self.assertTrue(self.binding().is_admin('u1'))

    def test_storage_failure_never_acknowledges_or_disturbs_existing_admins(self):
        self.assertEqual(self.binding().try_bind('u1', CODE), 'bound')
        for failure in ('raise', 'dropped'):
            with self.subTest(failure=failure):
                binding = self.binding()

                def broken(key, state, failure=failure):
                    if failure == 'raise':
                        raise RuntimeError('store down')
                binding.store.put = broken
                self.assertIsNone(binding.try_bind('u2', CODE))
                self.assertFalse(binding.is_admin('u2'))
                self.assertTrue(self.binding().is_admin('u1'))
        reader = self.binding()

        def unreadable(key):
            raise RuntimeError('store down')
        reader.store.get = unreadable
        self.assertIsNone(reader.try_bind('u3', CODE))
        self.assertFalse(reader.is_admin('u1'))  # Unknown state fails closed.

    def test_unbound_lookups_are_cached(self):
        binding = self.binding()
        gets = []
        self.site.job_get_hook = gets.append
        for _ in range(5):
            self.assertFalse(binding.is_admin('stranger'))
        self.assertEqual(len(gets), 1)


if __name__ == '__main__':
    unittest.main()
