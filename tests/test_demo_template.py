"""Preloaded demo: build a template workspace once, clone it instantly into new demo workspaces."""
import unittest
from tests.helpers import make_store
from decisionos.service import Service


class DemoTemplateTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(); self.service = Service(self.store)

    def test_clone_copies_briefing_items_and_pending_actions(self):
        self.assertFalse(self.service.clone_demo('personal'))
        meta = self.service.refresh_demo_template()
        self.assertEqual(meta['status'], 'ready')
        self.assertTrue(self.service.clone_demo('personal'))
        snap = self.service.snapshot('personal')
        template = self.service.snapshot(meta['org_id'])
        self.assertEqual(len(snap['actions']), len(template['actions']))
        self.assertEqual(snap['runs'][0]['digest'], template['runs'][0]['digest'])
        self.assertFalse(snap['busy'])
        own_items = {i['id'] for i in self.store.find('items', {'org_id': 'personal'})}
        self.assertTrue(all(a['item_id'] in own_items for a in snap['actions']))
        self.assertTrue(all(a['org_id'] == 'personal' for a in snap['actions']))

    def test_cloned_action_can_be_approved_and_executed(self):
        self.service.refresh_demo_template(); self.service.clone_demo('personal')
        action = next(a for a in self.service.snapshot('personal')['actions'] if a['status'] == 'proposed')
        self.service.approve(action['id'], 'personal', None, 'owner'); self.service.execute_action(action['id'])
        self.assertEqual(self.store.one('actions', {'id': action['id']})['status'], 'succeeded')

    def test_refresh_is_cached_until_stale(self):
        first = self.service.refresh_demo_template()
        self.assertEqual(self.service.refresh_demo_template()['org_id'], first['org_id'])
        second = self.service.refresh_demo_template(force=True)
        self.assertNotEqual(second['org_id'], first['org_id'])
        self.assertIsNone(self.store.one('organizations', {'id': first['org_id']}))


if __name__ == '__main__': unittest.main()
