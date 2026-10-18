"""Exercise the Gemini function-calling loop with a scripted client, without network I/O."""
import unittest
from types import SimpleNamespace
from google.genai import types
from tests.helpers import make_store
from decisionos.service import Service


class ScriptedGemini:
    """Mimics client.models.generate_content: first a propose_action call, then a briefing."""
    def __init__(self, source_id, payload_extra=None):
        self.source_id, self.calls, self.requests = source_id, 0, []
        self.payload_extra = payload_extra or {}
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, model, contents, config):
        self.calls += 1; self.requests.append((model, contents, config))
        if self.calls == 1:
            assert config.tools[0].function_declarations[0].name == 'propose_action'
            call = types.FunctionCall(name='propose_action', args={'item_id': self.source_id, 'action_kind': 'slack_reply',
                'payload': dict({'body': 'Prepared through the Gemini tool loop.'}, **self.payload_extra), 'reason': 'Reply after owner review.'})
            content = types.Content(role='model', parts=[types.Part(function_call=call)])
        else:
            content = types.Content(role='model', parts=[types.Part.from_text(text='A Slack reply is ready for approval.')])
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)])


class GeminiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(); self.service = Service(self.store)
        self.service.seed('personal'); self.service.sync('personal', 'demo')
        self.source = self.store.one('items', {'kind': 'slack', 'org_id': 'personal'})

    def test_function_call_creates_validated_proposal(self):
        client = ScriptedGemini(self.source['id'], {'response': None})
        digest = self.service.gemini_plan('personal', 'demo', [self.source], gemini_client=client)
        self.assertIn('ready for approval', digest)
        action = self.service.snapshot('personal')['actions'][0]
        self.assertEqual(action['status'], 'proposed')
        self.assertEqual(action['payload'], {'body': 'Prepared through the Gemini tool loop.'})
        self.assertEqual(client.calls, 2)
        tool_reply = client.requests[1][1][-1].parts[0].function_response
        self.assertEqual(tool_reply.response['result']['status'], 'proposed')

    def test_out_of_scope_item_is_rejected_back_to_model(self):
        client = ScriptedGemini('not-in-this-run')
        self.service.gemini_plan('personal', 'demo', [self.source], gemini_client=client)
        self.assertEqual(self.service.snapshot('personal')['actions'], [])
        tool_reply = client.requests[1][1][-1].parts[0].function_response
        self.assertIn('outside this run', tool_reply.response['result']['error'])


if __name__ == '__main__': unittest.main()
