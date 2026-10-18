"""Direct mode (Ask DecisionOS): Gemini turns a request into previewed actions that wait for approval."""
import unittest
from types import SimpleNamespace
from google.genai import types
from tests.helpers import make_store
from decisionos.service import Service


class ScriptedAsk:
    def __init__(self, calls):
        self.calls, self.turn, self.responses = calls, 0, []
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, model, contents, config):
        self.turn += 1
        if self.turn == 1:
            parts = [types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in self.calls]
        else:
            self.responses = [p.function_response.response['result'] for p in contents[-1].parts]
            parts = [types.Part.from_text(text='Prepared the requested actions for your approval.')]
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role='model', parts=parts))])


class AskTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(); self.service = Service(self.store)
        self.service.seed('personal'); self.service.sync('personal', 'demo')
        self.jira = self.store.one('items', {'org_id': 'personal', 'kind': 'jira', 'external_id': 'sample-incident'})

    def ask(self, calls):
        client = ScriptedAsk(calls)
        reply, created = self.service.ask('personal', 'owner', 'Handle the checkout incident', gemini_client=client)
        return client, reply, created

    def test_creates_new_work_and_item_actions_pending_approval(self):
        client, reply, created = self.ask([
            ('create_new', {'action_kind': 'jira_create', 'fields': {'project': 'acme', 'summary': 'EU checkout 500s follow-up', 'description': 'Root cause review', 'issue_type': 'Bug'}, 'reason': 'Track the follow-up.'}),
            ('create_new', {'action_kind': 'slack_post', 'fields': {'channel': 'incidents', 'body': 'I own customer comms for Northwind.'}, 'reason': 'Tell the team.'}),
            ('act_on_item', {'item_id': self.jira['id'], 'action_kind': 'jira_comment', 'payload': {'body': 'Customer update sent.'}, 'reason': 'Keep the incident current.'}),
        ])
        self.assertIn('approval', reply)
        self.assertEqual(len(created), 3)
        actions = {a['kind']: a for a in self.service.snapshot('personal')['actions']}
        self.assertEqual(actions['jira_create']['payload']['project'], 'ACME')
        self.assertEqual(actions['slack_post']['payload']['channel'], 'CINC')
        self.assertTrue(all(a['status'] == 'proposed' for a in actions.values()))
        aid = actions['jira_create']['id']
        self.service.approve(aid, 'personal', None, 'owner'); self.service.execute_action(aid)
        done = self.store.one('actions', {'id': aid})
        self.assertEqual(done['status'], 'succeeded')
        self.assertIn('sample simulation', done['result']['detail'])

    def test_invalid_requests_are_returned_to_gemini_not_stored(self):
        client, reply, created = self.ask([
            ('create_new', {'action_kind': 'email_send', 'fields': {'to': 'not-an-email', 'subject': 'Hi', 'body': 'Hello'}, 'reason': 'x'}),
            ('create_new', {'action_kind': 'slack_post', 'fields': {'channel': 'random', 'body': 'Hello'}, 'reason': 'x'}),
            ('create_new', {'action_kind': 'calendar_create', 'fields': {'title': 'Sync', 'start': '2026-10-20T10:00:00', 'end': '2026-10-20T10:30:00', 'attendees': ''}, 'reason': 'x'}),
        ])
        self.assertEqual(created, [])
        self.assertEqual(self.service.snapshot('personal')['actions'], [])
        self.assertTrue(all('error' in r for r in client.responses))

    def test_edited_payload_is_revalidated_on_approval(self):
        _, _, created = self.ask([('create_new', {'action_kind': 'calendar_create', 'fields': {'title': 'Release review', 'start': '2026-10-20T10:00:00+05:30', 'end': '2026-10-20T10:30:00+05:30', 'attendees': 'priya@acmeretail.example'}, 'reason': 'Schedule it.'})])
        aid = created[0]['id']
        action = self.store.one('actions', {'id': aid})
        bad = dict(action['payload'], end='2026-10-20T09:00:00+05:30')
        with self.assertRaises(ValueError): self.service.approve(aid, 'personal', bad, 'owner')
        self.service.approve(aid, 'personal', None, 'owner'); self.service.execute_action(aid)
        self.assertEqual(self.store.one('actions', {'id': aid})['status'], 'succeeded')


if __name__ == '__main__': unittest.main()
