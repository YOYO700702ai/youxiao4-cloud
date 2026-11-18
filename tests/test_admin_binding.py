import json
import threading
import unittest

from admin_binding import PrivateAdminBinding
from listing_client import RemoteJobStore
from tests.direct_fakes import FakeWebsite

# Test-only placeholder; production reads PRIVATE_ADMIN_BIND_CODE from the environment.
CODE = 'test-only-binding-placeholder-0001'


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeWebsite()

    def binding(self, code=CODE):
        return PrivateAdminBinding(RemoteJobStore(self.site), code)

    def test_wrong_short_or_embedded_codes_never_bind(self):
        binding = self.binding()
        for attempt in ('', 'hello', CODE[:-1], CODE + 'x', '綁定 ' + CODE):
            self.assertIsNone(binding.try_bind('u1', attempt))
        self.assertFalse(binding.is_admin('u1'))
        weak = self.binding('short-code')
        self.assertEqual(weak.code, '')
        self.assertIsNone(weak.try_bind('u1', 'short-code'))
        self.assertFalse(weak.mentions_code('short-code'))

    def test_first_account_wins_and_code_cannot_be_reused(self):
        binding = self.binding()
        self.assertEqual(binding.try_bind('u1', '  ' + CODE + '\n'), 'bound')
        self.assertTrue(binding.is_admin('u1'))
        self.assertIsNone(binding.try_bind('u2', CODE))
        self.assertFalse(binding.is_admin('u2'))
        self.assertEqual(binding.try_bind('u1', CODE), 'already')

    def test_binding_survives_restart_and_stores_no_code_or_raw_id(self):
        self.binding().try_bind('U-real-line-id', CODE)
        restarted = self.binding()
        self.assertTrue(restarted.is_admin('U-real-line-id'))
        self.assertFalse(restarted.is_admin('someone-else'))
        stored = json.dumps(self.site.jobs)
        self.assertNotIn(CODE, stored)
        self.assertNotIn('U-real-line-id', stored)

    def test_concurrent_first_binds_from_two_processes_have_one_winner(self):
        barrier = threading.Barrier(2)
        reads = []

        def hold_first_reads(ident):
            reads.append(ident)
            if len(reads) <= 2:
                barrier.wait(timeout=5)  # Both read revision 0 before either writes.
        self.site.job_get_hook = hold_first_reads
        a, b = self.binding(), self.binding()
        results = {}
        threads = [threading.Thread(target=lambda n=n, x=x: results.__setitem__(n, x.try_bind(n, CODE)))
                   for n, x in (('u1', a), ('u2', b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(results.values(), key=str), [None, 'bound'])
        winner = next(user for user, value in results.items() if value == 'bound')
        loser = 'u2' if winner == 'u1' else 'u1'
        self.assertTrue(self.binding().is_admin(winner))
        self.assertFalse(self.binding().is_admin(loser))

    def test_storage_failure_never_acknowledges(self):
        binding = self.binding()

        def broken(*args, **kwargs):
            raise RuntimeError('store down')
        binding.store.put = broken
        self.assertIsNone(binding.try_bind('u1', CODE))
        self.assertFalse(binding.is_admin('u1'))

    def test_unbound_lookups_are_cached(self):
        binding = self.binding()
        gets = []
        self.site.job_get_hook = gets.append
        for _ in range(5):
            self.assertFalse(binding.is_admin('stranger'))
        self.assertEqual(len(gets), 1)


if __name__ == '__main__':
    unittest.main()
