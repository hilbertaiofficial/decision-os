"""Firestore-semantics store: uniqueness markers, rollback, claims, and leases."""
import time
import unittest
from tests.helpers import make_store
from decisionos.store import DuplicateKeyError, encode, decode
from datetime import datetime, timezone


class StoreTests(unittest.TestCase):
    def setUp(self): self.store = make_store()

    def test_unique_email(self):
        with self.assertRaises(DuplicateKeyError):
            self.store.insert('users', {'email': 'owner@example.com', 'name': 'Dup'})

    def test_active_run_unique_until_released(self):
        run = self.store.insert('runs', {'org_id': 'personal', 'active': True, 'status': 'queued'})
        with self.assertRaises(DuplicateKeyError):
            self.store.insert('runs', {'org_id': 'personal', 'active': True, 'status': 'queued'})
        self.store.update('runs', {'id': run['id']}, {'active': False, 'status': 'succeeded'})
        self.store.insert('runs', {'org_id': 'personal', 'active': True, 'status': 'queued'})

    def test_atomic_rolls_back_every_write(self):
        with self.assertRaises(RuntimeError):
            with self.store.atomic():
                self.store.insert('runs', {'id': 'r1', 'org_id': 'personal', 'active': True})
                self.store.update('organizations', {'id': 'personal'}, {'config.mode': 'live'})
                raise RuntimeError('boom')
        self.assertIsNone(self.store.one('runs', {'id': 'r1'}))
        self.assertEqual(self.store.settings('personal')['mode'], 'demo')
        self.store.insert('runs', {'id': 'r2', 'org_id': 'personal', 'active': True})

    def test_query_operators_and_sort(self):
        for n in range(5): self.store.insert('events', {'org_id': 'personal', 'created': n, 'level': 'info' if n % 2 else 'error'})
        rows = self.store.find('events', {'org_id': 'personal', 'created': {'$gte': 2}, 'level': {'$in': ['info']}}, sort=[('created', -1)])
        self.assertEqual([r['created'] for r in rows], [3])
        self.assertEqual(len(self.store.find('events', {'$or': [{'created': 0}, {'created': 4}]})), 2)

    def test_job_claim_and_lease(self):
        self.assertTrue(self.store.enqueue('run', 't1', 'personal'))
        self.assertFalse(self.store.enqueue('run', 't1', 'personal'))
        job = self.store.claim_job('w1')
        self.assertEqual(job['status'], 'running')
        self.assertIsNone(self.store.claim_job('w2'))
        self.assertTrue(self.store.lease('w1')); self.assertFalse(self.store.lease('w2'))
        self.assertTrue(self.store.owns_lease('w1'))

    def test_encoding_keeps_nested_lists_and_datetimes(self):
        doc = {'id': 'x', 'data': {'rows': [[1, 2], [3]]}, 'expires_at': datetime(2030, 1, 1, tzinfo=timezone.utc)}
        stored = encode(doc)
        self.assertEqual(stored['expires_at'], doc['expires_at'])
        self.assertEqual(decode(stored), doc)

    def test_rate_limit(self):
        self.assertTrue(all(self.store.rate_limit('k', 3, 60) for _ in range(3)))
        self.assertFalse(self.store.rate_limit('k', 3, 60))


if __name__ == '__main__': unittest.main()
