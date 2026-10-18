"""Work collection, Gemini planning, policy gating, and durable action execution."""
import hashlib
import logging
import re
import json
import time
from datetime import datetime, timedelta, timezone
from .store import uid, DuplicateKeyError
from . import gemini

log = logging.getLogger(__name__)
from .integrations import GoogleConnector, JiraConnector, GitHubConnector, SlackConnector, Transport, IntegrationError, UncertainDelivery, item

ACTION_KINDS = {
    'email_draft': ('email', 'Save reply draft', {'body'}),
    'email_reply': ('email', 'Send email reply', {'body'}),
    'email_mark_read': ('email', 'Mark email as read', set()),
    'calendar_rsvp': ('calendar', 'Respond to invitation', {'response'}),
    'jira_comment': ('jira', 'Post Jira comment', {'body'}),
    'jira_transition': ('jira', 'Change Jira status', {'transition'}),
    'slack_reply': ('slack', 'Reply in Slack thread', {'body'}),
    'github_comment': ('github', 'Post GitHub comment', {'body'}),
    'github_close': ('github', 'Close GitHub issue/PR', set()),
    # Direct mode ("Ask DecisionOS"): new work requested by a person, not tied to a synced item.
    'email_send': ('email', 'Send new email', {'to', 'subject', 'body'}),
    'calendar_create': ('calendar', 'Create calendar event', {'title', 'start', 'end', 'attendees'}),
    'jira_create': ('jira', 'Create Jira issue', {'project', 'summary', 'description', 'issue_type'}),
    'slack_post': ('slack', 'Post Slack message', {'channel', 'body'}),
}
COMMAND_KINDS = {'email_send': 'google', 'calendar_create': 'google', 'jira_create': 'jira', 'slack_post': 'slack'}
EMAIL_RE = re.compile(r'[^\s@<>,]+@[^\s@<>,]+\.[^\s@<>,]+')


def email_list(value, limit):
    emails = [v.strip() for v in str(value or '').split(',') if v.strip()]
    if len(emails) > limit or any(not EMAIL_RE.fullmatch(e) for e in emails):
        raise ValueError(f'Enter up to {limit} valid email addresses, separated by commas.')
    return emails


def parse_time(value):
    try: when = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError: raise ValueError('Use a full date and time, for example 2026-10-20T15:00:00+05:30.') from None
    if when.tzinfo is None: raise ValueError('Include a time zone in the start and end time.')
    return when


def decode(row):
    return row


class Service:
    def __init__(self, store): self.store = store

    def connector(self, connection):
        http = Transport(self.store, connection['id'])
        if connection['provider'] == 'google': return GoogleConnector(http)
        if connection['provider'] == 'jira': return JiraConnector(http, [s for s in connection['metadata']['sites'] if s['id'] in connection['metadata'].get('selected_sites', [])], self.store.settings(connection['org_id'])['jira_jql'])
        if connection['provider'] == 'github': return GitHubConnector(http, connection['metadata']['identity'], connection['metadata'].get('repos', []))
        return SlackConnector(http, connection['metadata'])

    def seed(self, org_id):
        for provider, label, identity in [('google', 'alex@example.com', 'sample-google'), ('jira', 'Acme · product team', 'sample-jira'), ('slack', 'Acme Slack', 'sample-slack'), ('github', 'acme-inc/product', 'sample-github')]:
            self.store.connect(provider, label, {}, {'identity': identity, 'channels': [{'id': 'CDEMO', 'name': 'team-product'}, {'id': 'CINC', 'name': 'incidents'}], 'repos': [{'full_name': 'acme-inc/product'}]}, mode='demo', org_id=org_id)
        self.store.event('Sample accounts added. These never contact external services.',org_id=org_id)

    def samples(self, provider):
        """Sample workspace: a release-scope decision, a customer escalation, a blocked SDK upgrade, and routine noise."""
        today = datetime.now(timezone.utc).date()
        tomorrow, friday = today + timedelta(days=1), today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        def slot(day, start, end): return {'start': {'dateTime': f'{day}T{start}:00+00:00'}, 'end': {'dateTime': f'{day}T{end}:00+00:00'}}
        if provider == 'google':
            return [item('email', 'sample-newsletter', 'Your weekly product digest', 'The latest product news. No reply needed.', {'sender': 'Product Weekly <news@example.com>', 'reply_to': 'news@example.com', 'subject': 'Your weekly product digest'}),
                    item('email', 'sample-client', 'Can we add a mobile redesign to this sprint?', 'Hi Alex, could we include the mobile version of the new checkout in Friday\'s release? Our board demo is next Tuesday. Please confirm the cost and timeline. Thanks, Priya (Acme Retail)', {'sender': 'Priya Sharma <priya@acmeretail.example>', 'reply_to': 'priya@acmeretail.example', 'subject': 'Can we add a mobile redesign to this sprint?'}),
                    item('email', 'sample-escalation', 'URGENT: EU customers cannot complete checkout', 'Since 09:10 CET roughly 30% of our EU card payments fail with a 500 error at the final step. This is costing us orders every minute. Can you give us an ETA and a workaround? Daniel Weber, Head of E-commerce, Northwind', {'sender': 'Daniel Weber <daniel@northwind.example>', 'reply_to': 'daniel@northwind.example', 'subject': 'URGENT: EU customers cannot complete checkout'}),
                    item('email', 'sample-offer', 'Offer approval needed: Maya Chen, Senior Backend Engineer', 'Maya accepted verbally at the top of band. We need your approval on the written offer by Thursday or she will take the competing offer. Compensation details are in the attached ATS link. Ritu (Talent)', {'sender': 'Ritu Kapoor <ritu@hilbert.example>', 'reply_to': 'ritu@hilbert.example', 'subject': 'Offer approval needed: Maya Chen, Senior Backend Engineer'}),
                    item('email', 'sample-security', 'Security alert: new sign-in from an unrecognized device', 'A new sign-in to your account was detected from Chrome on Windows near Frankfurt. If this was you, no action is needed. Otherwise, review your account activity using the link below.', {'sender': 'Account Security <no-reply@accounts.example>', 'reply_to': 'no-reply@accounts.example', 'subject': 'Security alert: new sign-in from an unrecognized device'}),
                    item('calendar', 'sample-meeting', 'Product planning with Alex', 'Review this week\'s priorities together, including the mobile checkout request.', dict(slot(tomorrow, '15:00', '15:30'), pending=True, organizer={'email': 'alex@example.com'})),
                    item('calendar', 'sample-release-review', 'Friday release go/no-go', 'Final go/no-go for the 4.2 release. Agenda: scope changes, open blockers (ACME-24, ACME-27), incident status.', dict(slot(friday, '10:00', '10:45'), pending=True, organizer={'email': 'release-manager@hilbert.example'})),
                    item('calendar', 'sample-standup', 'Daily stand-up', 'Team stand-up.', dict(slot(tomorrow, '09:30', '09:45'), pending=False, organizer={'email': 'alex@example.com'}))]
        if provider == 'jira':
            return [item('jira', 'sample-issue', 'ACME-24 · Mobile navigation polish', 'Blocked: waiting for approved mobile scope before implementation. Estimate if added: 3 extra engineering days.', {'key': 'ACME-24', 'status': 'Blocked', 'priority': 'Medium', 'site_name': 'Acme'}),
                    item('jira', 'sample-incident', 'ACME-31 · Checkout returns 500 for EU card payments', 'Error rate on /checkout/confirm rose to 31% for EU cards after the 09:00 deploy. Suspected cause: payment SDK region config. Assignee: Arjun. No customer comms sent yet.', {'key': 'ACME-31', 'status': 'In Progress', 'priority': 'Highest', 'site_name': 'Acme'}),
                    item('jira', 'sample-sdk', 'ACME-27 · Upgrade payments SDK to v5', 'Blocked on security review sign-off. The v4 SDK reaches end of support on the 30th. Release 4.2 depends on this.', {'key': 'ACME-27', 'status': 'Blocked', 'priority': 'High', 'site_name': 'Acme'})]
        if provider == 'github':
            return [item('github', 'sample-pr', 'acme-inc/product#142 · Mobile nav: gate behind feature flag', 'Ready for review once the mobile scope question is resolved with the client.', {'repo': 'acme-inc/product', 'number': 142, 'is_pr': True}),
                    item('github', 'sample-pr-sdk', 'acme-inc/product#148 · Upgrade payments SDK to v5', 'CI: 2 failing checks (eu-region config tests). Waiting on security review. Linked: ACME-27.', {'repo': 'acme-inc/product', 'number': 148, 'is_pr': True})]
        return [item('slack', 'sample-thread', '#team-product · Can someone confirm the mobile scope?', 'Can someone confirm whether the mobile redesign is included this sprint? ACME-24 is blocked until we know, and engineering needs 3 more days if it is in.', {'channel': 'CDEMO', 'channel_name': 'team-product', 'thread_ts': 'sample-thread'}),
                item('slack', 'sample-incident-thread', '#incidents · Spike in checkout 500s (EU)', 'Arjun: rolled back the 09:00 deploy on one node, error rate down to 12% but not zero. Support is asking what to tell Northwind. Who owns customer comms on this?', {'channel': 'CINC', 'channel_name': 'incidents', 'thread_ts': 'sample-incident-thread'})]

    def in_scope(self, connection, source):
        if connection['mode'] == 'demo': return True
        if source['kind'] == 'slack':
            return source['data'].get('channel') in [c['id'] for c in connection['metadata'].get('channels', [])]
        if source['kind'] == 'jira':
            return source['data'].get('site') in connection['metadata'].get('selected_sites', [])
        if source['kind'] == 'github':
            return source['data'].get('repo') in [r['full_name'] for r in connection['metadata'].get('repos', [])]
        return True

    def invalidate_scope(self, cid):
        connection = self.store.connection(cid)
        for row in self.store.find('actions', {'connection_id':cid,'status':{'$in':['proposed','queued','failed']}}):
            if not self.in_scope(connection,row['snapshot']):
                self.store.update('actions',{'id':row['id']},{'status':'superseded','updated':time.time()})

    def sync(self, org_id, mode):
        count, failures = 0, []
        for connection in self.store.connections(mode,org_id):
            if connection['status']!='connected': continue
            try:
                incoming=self.samples(connection['provider']) if mode=='demo' else self.connector(connection).sync()
                for source in incoming:
                    query={'org_id':org_id,'connection_id':connection['id'],'kind':source['kind'],'external_id':source['external_id']}
                    with self.store.atomic():
                        old=self.store.one('items',query)
                        values=dict(source,mode=mode,updated=time.time())
                        if old:
                            iid=old['id']; self.store.update('items',{'id':iid},values)
                            if old['revision']!=source['revision']:
                                self.store.update('actions',{'item_id':iid,'status':{'$in':['proposed','queued','failed']}},{'status':'superseded','updated':time.time()},many=True)
                        else: self.store.insert('items',dict(query,**{k:v for k,v in values.items() if k not in query},created=time.time(),processed_revision=None))
                        count+=1
                self.store.update('connections',{'id':connection['id']},{'last_sync':time.time(),'error':None})
            except Exception as exc:
                error=str(exc) if isinstance(exc,IntegrationError) else 'Sync failed. Check provider configuration and try again.'
                self.store.update('connections',{'id':connection['id']},{'error':error})
                failures.append(connection['label']+': '+error)
        return count, failures

    def source(self, iid, org_id, mode=None):
        query={'id':iid,'org_id':org_id}
        if mode: query['mode']=mode
        source=self.store.one('items',query)
        if not source: raise ValueError('Source item does not belong to this organization.')
        return source

    def action(self, aid, org_id):
        return self.store.one('actions',{'id':aid,'org_id':org_id})

    def validate(self, kind, payload, source):
        if kind not in ACTION_KINDS or ACTION_KINDS[kind][0] != source['kind']:
            raise ValueError('Action does not match the source tool.')
        if not isinstance(payload, dict) or set(payload) != ACTION_KINDS[kind][2]:
            raise ValueError('The action has missing or unexpected fields.')
        if 'body' in payload and (not isinstance(payload['body'], str) or not payload['body'].strip() or len(payload['body']) > 12000):
            raise ValueError('Write a message between 1 and 12,000 characters.')
        if kind == 'calendar_rsvp' and (payload['response'] not in ('accepted', 'declined', 'tentative') or not source['data'].get('pending')):
            raise ValueError('Choose a valid response to a pending invitation.')
        if kind == 'jira_transition' and (not isinstance(payload['transition'], str) or not 1 <= len(payload['transition'].strip()) <= 100):
            raise ValueError('Enter the name of a valid Jira transition.')
        if kind == 'email_send':
            if not email_list(payload['to'], 10): raise ValueError('Add at least one recipient.')
            if not isinstance(payload['subject'], str) or not 1 <= len(payload['subject'].strip()) <= 200: raise ValueError('Write a subject up to 200 characters.')
        if kind == 'calendar_create':
            if not isinstance(payload['title'], str) or not 1 <= len(payload['title'].strip()) <= 200: raise ValueError('Give the event a title up to 200 characters.')
            start, end = parse_time(payload['start']), parse_time(payload['end'])
            if not timedelta(minutes=5) <= end - start <= timedelta(hours=8): raise ValueError('Events must last between 5 minutes and 8 hours.')
            email_list(payload['attendees'], 20)
        if kind == 'jira_create':
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,9}', str(payload['project'])): raise ValueError('Use a Jira project key such as ACME.')
            if not isinstance(payload['summary'], str) or not 1 <= len(payload['summary'].strip()) <= 250: raise ValueError('Write an issue summary up to 250 characters.')
            if not isinstance(payload['description'], str) or len(payload['description']) > 12000: raise ValueError('Keep the description under 12,000 characters.')
            if payload['issue_type'] not in ('Task', 'Bug', 'Story'): raise ValueError('Choose Task, Bug or Story.')
        if kind == 'slack_post':
            if len(payload['body']) > 4000: raise ValueError('Keep Slack messages under 4,000 characters.')
            if payload['channel'] != source['data'].get('channel'): raise ValueError('The Slack channel cannot be changed after the request.')

    def propose(self,org_id,mode,iid,kind,payload,reason):
        self.store.check_execution()
        source=self.source(iid,org_id,mode)
        self.validate(kind,payload,source)
        if not isinstance(reason,str) or not reason.strip(): raise ValueError('Explain why this action is useful.')
        fingerprint=hashlib.sha256(json.dumps([org_id,iid,source['revision'],kind]).encode()).hexdigest()
        existing=self.store.one('actions',{'fingerprint':fingerprint})
        if existing:return {'id':existing['id'],'status':existing['status']}
        if kind in ('email_draft','email_reply','jira_comment','slack_reply'):
            for done in self.store.find('actions',{'org_id':org_id,'item_id':iid,'kind':kind,'status':'succeeded'}):
                if done['payload']==payload:return {'id':done['id'],'status':done['status']}
        if self.store.one('actions',{'org_id':org_id,'item_id':iid,'status':{'$in':['executing','uncertain']}}):
            raise ValueError('Resolve the unconfirmed action before proposing another.')
        settings=self.store.settings(org_id)
        auto=(kind=='email_draft' and settings['auto_drafts']) or (kind=='email_mark_read' and settings['auto_mark_read'])
        conn=self.store.connection(source['connection_id'],org_id=org_id)
        with self.store.atomic():
            action=self.store.insert('actions',{'org_id':org_id,'mode':mode,'item_id':iid,'connection_id':conn['id'],
                'title':source['title'],'label':conn['label'],'kind':kind,'payload':payload,'snapshot':source,'reason':reason[:2000],
                'status':'queued' if auto else 'proposed','approved_by':'policy' if auto else None,'result':None,'error':None,
                'created':time.time(),'updated':time.time(),'fingerprint':fingerprint})
            if auto:self.store.enqueue('action',action['id'],org_id)
        return {'id':action['id'],'status':action['status']}

    def request_run(self,org_id,actor=None):
        if actor:self.authorize(org_id,actor,('owner','admin','member'))
        settings=self.store.settings(org_id)
        if not any(c['status']=='connected' for c in self.store.connections(settings['mode'],org_id)):
            raise ValueError('Connect accounts or add sample accounts before running a briefing.')
        if len(self.store.find('runs',{'org_id':org_id,'created':{'$gt':time.time()-3600}}))>=12:
            raise ValueError('This organization has reached its hourly briefing limit.')
        org=self.store.one('organizations',{'id':org_id})
        if org.get('plan','pro')=='trial' and org.get('trial_briefings_used',0)>=10:
            raise ValueError('Trial limit reached (10 briefings). Upgrade to Pro or Team to keep running briefings.')
        try:
            with self.store.atomic():
                run=self.store.insert('runs',{'org_id':org_id,'mode':settings['mode'],'engine':self.engine(settings),
                    'status':'queued','active':True,'requested_by':actor,'created':time.time(),'digest':None,'error':None})
                self.store.enqueue('run',run['id'],org_id)
                if org.get('plan','pro')=='trial':
                    self.store.update('organizations',{'id':org_id},{'trial_briefings_used':org.get('trial_briefings_used',0)+1})
            return run['id']
        except DuplicateKeyError:raise ValueError('A briefing is already queued or running.') from None

    def authorize(self,org_id,actor,roles):
        member=self.store.membership(org_id,actor)
        if not member or member['role'] not in roles:raise ValueError('Your organization role does not permit this action.')
        self.store.settings(org_id)

    def run(self,rid):
        run=self.store.claim('runs',{'id':rid,'status':'queued'},{'status':'running'})
        if not run:return
        org,mode=run['org_id'],run['mode']
        try:
            self.store.settings(org)
            if run.get('requested_by'):self.authorize(org,run['requested_by'],('owner','admin','member'))
            count,errors=self.sync(org,mode)
            allowed={c['id']:c for c in self.store.connections(mode,org) if c['status']=='connected' and not c.get('error')}
            rows=self.store.find('items',{'org_id':org,'mode':mode,'connection_id':{'$in':list(allowed)},'updated':{'$gte':run['created']}},sort=[('updated',-1)])
            rows=[r for r in rows if r.get('processed_revision')!=r['revision'] and self.in_scope(allowed[r['connection_id']],r)][:60]
            actionable=[r for r in rows if r['kind']!='calendar' or r['data'].get('pending')]
            if errors and not rows:raise IntegrationError('No new work could be processed. '+' '.join(errors))
            if not actionable:digest='No new items need processing. Existing decisions remain in your queue.'
            elif run['engine']=='gemini':digest=self.gemini_plan(org,mode,actionable)
            else:digest=self.sample_plan(org,mode,actionable)
            with self.store.atomic():
                for source in rows:self.store.update('items',{'id':source['id'],'org_id':org,'revision':source['revision']},{'processed_revision':source['revision']})
                self.store.update('runs',{'id':rid},{'status':'partial' if errors else 'succeeded','active':False,'digest':digest,'error':'\n'.join(errors) or None,'finished':time.time()})
                self.store.event('Briefing completed.',org_id=org)
        except Exception as exc:
            error=str(exc) if isinstance(exc,(IntegrationError,ValueError)) else 'The agent could not finish. Check model access and connection settings. Existing actions are preserved.'
            self.store.update('runs',{'id':rid},{'status':'failed','active':False,'error':error,'finished':time.time()})
            self.store.event('A briefing needs attention.','error',org_id=org)

    SAMPLE_PLAN = {
        'sample-newsletter': ('email_mark_read', {}, 'This newsletter needs no response. Clear it from unread items.'),
        'sample-client': ('email_draft', {'body': 'Hi Priya, thanks for the request. Adding mobile checkout to Friday\'s release would need about three more engineering days, so I\'m reviewing scope with the team at tomorrow\'s planning session and will confirm options and timing right after.'}, 'Same scope decision as ACME-24, PR #142 and the #team-product thread. Avoids committing to dates or cost before the planning review.'),
        'sample-escalation': ('email_draft', {'body': 'Hi Daniel, we\'re treating this as our highest priority. A partial rollback has already reduced failures and the team is working on a full fix now. I\'ll send the next update within the hour.'}, 'Matches ACME-31 and the #incidents thread. No customer update has been sent yet; this gives a holding reply without promising an ETA.'),
        'sample-offer': ('email_draft', {'body': 'Thanks Ritu. I\'ll review Maya\'s written offer today and get back to you before Thursday.'}, 'Compensation approval is a consequential decision for you; this only acknowledges the deadline.'),
        'sample-security': ('email_mark_read', {}, 'Unverified security notification. Check account activity directly in the provider rather than through links in the email.'),
        'sample-meeting': ('calendar_rsvp', {'response': 'accepted'}, 'Tomorrow\'s planning is where the mobile scope decision gets made. No conflicts found.'),
        'sample-release-review': ('calendar_rsvp', {'response': 'accepted'}, 'Go/no-go depends on ACME-24, ACME-27 and the checkout incident. Your attendance is needed.'),
        'sample-issue': ('jira_comment', {'body': 'Client has asked to add mobile scope to Friday\'s release; engineering estimates 3 extra days. Keeping this blocked until the scope decision at tomorrow\'s planning session.'}, 'Links the client email, the Slack question and PR #142 to one decision.'),
        'sample-incident': ('jira_comment', {'body': 'Customer impact confirmed by Northwind (EU card payments failing). Holding reply sent to the customer; next customer update due within the hour. Please post root cause and fix ETA here.'}, 'Ties the customer escalation to the incident so engineering and support share one status.'),
        'sample-sdk': ('jira_comment', {'body': 'Security review sign-off is the blocker for 4.2. v4 support ends on the 30th. Requesting security review this week.'}, 'Release 4.2 depends on this; raises the blocker before the Friday go/no-go.'),
        'sample-pr': ('github_comment', {'body': 'Holding until the mobile scope is confirmed with the client. See ACME-24.'}, 'This PR implements the scope that is still awaiting a decision.'),
        'sample-pr-sdk': ('github_comment', {'body': 'The failing eu-region config checks may relate to the ACME-31 checkout incident. Please confirm before merging.'}, 'The failing EU config tests overlap with the live EU checkout incident.'),
        'sample-thread': ('slack_reply', {'body': 'Mobile scope isn\'t confirmed yet. The client asked for it in Friday\'s release; we\'ll decide at tomorrow\'s planning. ACME-24 stays blocked until then.'}, 'Answers the team with the same facts as the client email and Jira issue.'),
        'sample-incident-thread': ('slack_reply', {'body': 'I\'ll own customer comms for Northwind. Holding reply drafted; I\'ll post updates in ACME-31. Arjun, please share root cause and ETA when you have it.'}, 'Resolves the missing owner for customer communication on the incident.'),
    }

    def sample_plan(self, org, mode, sources):
        for source in sources:
            plan = self.SAMPLE_PLAN.get(source['external_id'])
            if plan: self.propose(org, mode, source['id'], *plan)
        return ('Sample simulation: no external services or AI model were called.\n\n'
                '## Decisions needing you\n'
                '- **Checkout incident (Northwind, EU):** customer email, ACME-31 and #incidents describe the same outage. Holding reply, Jira update and Slack ownership reply are ready.\n'
                '- **Mobile scope for Friday:** client email, ACME-24, PR #142 and #team-product are one decision. Reply, Jira comment and Slack answer are ready; decide at tomorrow\'s planning.\n'
                '- **Release 4.2 go/no-go:** ACME-27 and PR #148 are blocked on security review; accept Friday\'s review.\n'
                '- **Offer approval:** Maya Chen\'s offer needs your approval by Thursday.\n\n'
                '## Handled or informational\n'
                '- Newsletter cleared. The security alert is unverified; check account activity directly in the provider.')

    def engine(self, settings):
        """Live mode always plans with Gemini. Sample accounts use Gemini when it is configured, else a scripted plan."""
        if settings['mode']=='live':return 'gemini'
        return 'gemini' if settings.get('engine')=='gemini' and gemini.configured() else 'sample'

    def gemini_plan(self, org, mode, sources, gemini_client=None):
        item_fields=('id','kind','external_id','title','body','data','revision')
        preferences = [r['rule'] for r in self.store.find('preferences',{'org_id':org},sort=[('created',1)])]
        calendar_context=self.store.find('items',{'org_id':org,'mode':mode,'kind':'calendar','updated':{'$gt':time.time()-300}},limit=100)
        recent_actions=self.store.find('actions',{'org_id':org,'mode':mode},sort=[('created',-1)],limit=60)
        context={'preferences':preferences,'items':[{k:s[k] for k in item_fields if k in s} for s in sources],
                 'calendar_context_read_only':[{k:c[k] for k in item_fields if k in c} for c in calendar_context],
                 'recent_action_history':[{k:a[k] for k in ('item_id','kind','payload','status')} for a in recent_actions]}
        allowed = {s['id'] for s in sources}
        def propose_action(item_id, action_kind, payload, reason):
            if item_id not in allowed: raise ValueError('Item is outside this run.')
            return self.propose(org, mode, item_id, action_kind, payload, reason)
        return gemini.plan(context, propose_action, gemini_client)

    def command_connection(self, org_id, mode, kind):
        provider = COMMAND_KINDS[kind]
        for conn in self.store.connections(mode, org_id):
            if conn['provider'] == provider and conn['status'] == 'connected': return conn
        raise ValueError('No connected ' + {'google': 'Google', 'jira': 'Jira', 'slack': 'Slack'}[provider] + ' account in this workspace.')

    def propose_command(self, org_id, mode, command, kind, payload, reason):
        """Prepare new work a person asked for. Always waits for human approval."""
        if kind not in COMMAND_KINDS: raise ValueError('Unsupported action.')
        if not isinstance(reason, str) or not reason.strip(): raise ValueError('Explain why this action is useful.')
        payload = {k: (v.strip() if isinstance(v, str) else v) for k, v in dict(payload).items() if k in ACTION_KINDS[kind][2]}
        payload = {k: payload.get(k, '') for k in ACTION_KINDS[kind][2]}
        conn = self.command_connection(org_id, mode, kind)
        data = {}
        if kind == 'slack_post':
            channels = {c['id']: c['name'] for c in conn['metadata'].get('channels', [])}
            if payload['channel'] not in channels:
                match = next((cid for cid, name in channels.items() if name == str(payload['channel']).lstrip('#')), None)
                if not match: raise ValueError('Choose one of the selected Slack channels: ' + ', '.join('#' + n for n in channels.values()))
                payload['channel'] = match
            data = {'channel': payload['channel'], 'channel_name': channels[payload['channel']]}
        if kind == 'jira_create':
            sites = conn['metadata'].get('selected_sites', [])
            data = {'site': sites[0] if sites else 'demo'}
            payload['project'] = str(payload['project']).upper()
            payload['issue_type'] = {'task': 'Task', 'bug': 'Bug', 'story': 'Story'}.get(str(payload['issue_type']).strip().lower(), payload['issue_type'] or 'Task')
        titles = {'email_send': lambda: 'Email to ' + payload['to'] + ' · ' + payload['subject'],
                  'calendar_create': lambda: 'New event · ' + payload['title'],
                  'jira_create': lambda: 'New ' + str(payload['issue_type']) + ' in ' + payload['project'] + ' · ' + payload['summary'],
                  'slack_post': lambda: '#' + data.get('channel_name', '') + ' · new message'}
        snapshot = {'id': None, 'kind': ACTION_KINDS[kind][0], 'external_id': 'command:' + command['id'], 'title': titles[kind]()[:200],
                    'body': command['text'], 'data': data, 'revision': 'command'}
        self.validate(kind, payload, snapshot)
        fingerprint = hashlib.sha256(json.dumps([org_id, command['id'], kind, payload], sort_keys=True).encode()).hexdigest()
        existing = self.store.one('actions', {'fingerprint': fingerprint})
        if existing: return {'id': existing['id'], 'status': existing['status']}
        action = self.store.insert('actions', {'org_id': org_id, 'mode': mode, 'item_id': None, 'connection_id': conn['id'], 'origin': 'command',
            'command_id': command['id'], 'title': snapshot['title'], 'label': conn['label'], 'kind': kind, 'payload': payload, 'snapshot': snapshot,
            'reason': reason[:2000], 'status': 'proposed', 'approved_by': None, 'result': None, 'error': None,
            'created': time.time(), 'updated': time.time(), 'fingerprint': fingerprint})
        return {'id': action['id'], 'status': 'proposed', 'title': snapshot['title']}

    def ask(self, org, actor, text, gemini_client=None):
        """Direct mode: turn a request into previewed actions that wait for approval."""
        self.authorize(org, actor, ('owner', 'admin', 'member'))
        text = (text or '').strip()
        if not 1 <= len(text) <= 1000: raise ValueError('Ask in up to 1,000 characters.')
        if not gemini.configured() and gemini_client is None: raise ValueError('Ask DecisionOS needs Gemini. Configure Vertex AI or a Gemini API key.')
        settings = self.store.settings(org); mode = settings['mode']
        command = self.store.insert('commands', {'org_id': org, 'user_id': actor, 'text': text, 'reply': None, 'status': 'running', 'created': time.time()})
        items = self.store.find('items', {'org_id': org, 'mode': mode}, sort=[('updated', -1)], limit=80)
        connections = [c for c in self.store.connections(mode, org) if c['status'] == 'connected']
        context = {'now_utc': datetime.now(timezone.utc).isoformat(timespec='minutes'),
                   'connected_tools': sorted({c['provider'] for c in connections}),
                   'slack_channels': [{'id': ch['id'], 'name': ch['name']} for c in connections if c['provider'] == 'slack' for ch in c['metadata'].get('channels', [])],
                   'jira_projects': sorted({i['data']['key'].split('-')[0] for i in items if i['kind'] == 'jira' and i['data'].get('key')}),
                   'preferences': [r['rule'] for r in self.store.find('preferences', {'org_id': org}, sort=[('created', 1)])],
                   'items': [{'id': i['id'], 'kind': i['kind'], 'title': i['title'], 'body': (i.get('body') or '')[:400],
                              'data': {k: i['data'].get(k) for k in ('key', 'status', 'priority', 'sender', 'channel_name', 'repo', 'number', 'pending') if k in i['data']}} for i in items]}
        allowed = {i['id'] for i in items}
        created = []
        def act_on_item(item_id, action_kind, payload, reason):
            if item_id not in allowed: raise ValueError('Item is outside this workspace.')
            result = self.propose(org, mode, item_id, action_kind, payload, reason); created.append(result); return result
        def create_new(action_kind, fields, reason):
            result = self.propose_command(org, mode, command, action_kind, fields, reason); created.append(result); return result
        try:
            reply = gemini.ask(text, context, act_on_item, create_new, gemini_client)
            self.store.update('commands', {'id': command['id']}, {'reply': reply, 'status': 'done', 'actions': len(created)})
        except Exception as exc:
            if not isinstance(exc, ValueError): log.exception('Ask DecisionOS failed')
            message = str(exc) if isinstance(exc, ValueError) else 'Gemini could not finish this request. Try again.'
            self.store.update('commands', {'id': command['id']}, {'reply': message, 'status': 'failed'})
            raise ValueError(message) from None
        self.store.event('Ask DecisionOS: ' + text[:120], org_id=org, actor=actor)
        return reply, created

    DEMO_PREFERENCES = ('Never commit to delivery dates, pricing or scope changes without my approval.',
                        'Customer-facing outage updates go out within the hour and are tracked in the Jira incident.',
                        'Keep replies short and friendly; sign emails as Alex.')
    DEMO_TEMPLATE_TTL = 12 * 3600

    def demo_template(self):
        meta = self.store.one('demo_templates', {'id': 'current'})
        if not meta or meta.get('status') != 'ready': return None
        return meta

    def refresh_demo_template(self, force=False):
        """Build a ready-made demo workspace (sample data plus a finished briefing) that new demos clone instantly."""
        meta = self.store.one('demo_templates', {'id': 'current'})
        if meta and not force and meta.get('status') == 'ready' and time.time() - meta.get('created', 0) < self.DEMO_TEMPLATE_TTL: return meta
        org = self.store.insert('organizations', {'name': 'Demo template', 'status': 'active', 'template': True, 'config': dict(self.store.settings(), mode='demo'),
                                                  'created': time.time(), 'auth_revision': 0, 'plan': 'pro', 'trial_briefings_used': 0})
        self.seed(org['id'])
        for rule in self.DEMO_PREFERENCES:
            self.store.insert('preferences', {'org_id': org['id'], 'rule': rule, 'created_by': None, 'created': time.time()})
        rid = self.request_run(org['id'])
        self.run(rid)
        run = self.store.one('runs', {'id': rid})
        if not run or run['status'] not in ('succeeded', 'partial'): return meta
        for queued in self.store.find('actions', {'org_id': org['id'], 'status': 'queued'}):
            self.execute_action(queued['id'])
        self.store.update('jobs', {'org_id': org['id'], 'active': True}, {'status': 'done', 'active': False, 'finished': time.time()}, many=True)
        previous = meta.get('org_id') if meta else None
        values = {'org_id': org['id'], 'run_id': rid, 'status': 'ready', 'created': time.time(), 'engine': run.get('engine')}
        if meta: self.store.update('demo_templates', {'id': 'current'}, values)
        else: self.store.insert('demo_templates', dict(values, id='current'))
        if previous and previous != org['id']:
            for collection in ('connections', 'items', 'actions', 'runs', 'jobs', 'preferences', 'events'):
                self.store.delete(collection, {'org_id': previous})
            self.store.delete('organizations', {'id': previous})
        return self.store.one('demo_templates', {'id': 'current'})

    def clone_demo(self, org_id):
        """Copy the ready-made demo into a new workspace. Returns False if no template is ready yet."""
        meta = self.demo_template()
        if not meta: return False
        source = meta['org_id']
        self.seed(org_id)
        by_identity = {c['identity']: c for c in self.store.connections('demo', org_id)}
        conn_map = {c['id']: by_identity[c['identity']] for c in self.store.connections('demo', source) if c['identity'] in by_identity}
        item_map = {}
        now = time.time()
        for row in self.store.find('items', {'org_id': source}):
            new = dict(row, id=uid(), org_id=org_id, connection_id=conn_map[row['connection_id']]['id'], updated=now, created=now)
            self.store.insert('items', new); item_map[row['id']] = new
        for row in self.store.find('actions', {'org_id': source}, sort=[('created', 1)]):
            item = item_map.get(row['item_id'])
            if not item or row['connection_id'] not in conn_map: continue
            conn = conn_map[row['connection_id']]
            copy = self.store.insert('actions', dict(row, id=uid(), org_id=org_id, item_id=item['id'], connection_id=conn['id'], label=conn['label'],
                snapshot=item, approved_by=('policy' if row.get('approved_by') == 'policy' else None), created=now, updated=now,
                fingerprint=hashlib.sha256(json.dumps([org_id, item['id'], item['revision'], row['kind']]).encode()).hexdigest()))
            if copy['status'] == 'queued': self.store.enqueue('action', copy['id'], org_id)
        run = self.store.one('runs', {'id': meta['run_id']})
        if run:
            self.store.insert('runs', dict(run, id=uid(), org_id=org_id, active=False, requested_by=None, created=now, finished=now))
        self.store.event('Demo workspace loaded with a ready briefing.', org_id=org_id)
        return True

    def approve(self,aid,org,payload=None,actor=None):
        self.authorize(org,actor,('owner','admin','member'))
        action=self.action(aid,org)
        if not action or action['status'] not in ('proposed','failed'):raise ValueError('This action is no longer awaiting approval.')
        payload=action['payload'] if payload is None else payload
        self.validate(action['kind'],payload,action['snapshot'])
        with self.store.atomic():
            if self.store.one('jobs',{'kind':'action','target':aid,'active':True}):raise ValueError('This action is already being processed.')
            if not self.store.update('actions',{'id':aid,'org_id':org,'status':{'$in':['proposed','failed']}},
                {'status':'queued','payload':payload,'approved_by':actor,'error':None,'updated':time.time()}):raise ValueError('This action was already submitted.')
            self.store.enqueue('action',aid,org)
            self.store.event('Action approved: '+action['kind'],org_id=org,actor=actor)
        return True

    def dismiss(self,aid,org,actor=None):
        self.authorize(org,actor,('owner','admin','member'))
        if not self.store.update('actions',{'id':aid,'org_id':org,'status':{'$in':['proposed','failed','uncertain']}},{'status':'dismissed','updated':time.time()}):
            raise ValueError('Decision unavailable or still executing.')

    def execute_action(self,aid):
        action=self.store.claim('actions',{'id':aid,'status':'queued'},{'status':'executing','updated':time.time()})
        if not action:return
        try:
            self.store.check_execution()
            settings=self.store.settings(action['org_id'])
            actor=action.get('approved_by')
            if actor=='policy':
                allowed=(action['kind']=='email_draft' and settings['auto_drafts']) or (action['kind']=='email_mark_read' and settings['auto_mark_read'])
                if not allowed:raise ValueError('This automatic action is no longer permitted by policy.')
            else:self.authorize(action['org_id'],actor,('owner','admin','member'))
            connection=self.store.connection(action['connection_id'],org_id=action['org_id'])
            if not connection or connection['status']!='connected':raise IntegrationError('Account disconnected.')
            if settings['mode']!=connection['mode']:raise ValueError('The workspace mode changed; review before retrying.')
            source=action['snapshot']
            if not self.in_scope(connection,source):raise IntegrationError('The source is no longer selected.')
            if action.get('origin')!='command':
                current=self.source(action['item_id'],action['org_id'])
                if current['revision']!=source['revision']:raise IntegrationError('The source changed. Run a new briefing.')
            self.validate(action['kind'],action['payload'],source)
            if connection['mode']=='demo':result={'detail':ACTION_KINDS[action['kind']][1]+' completed in the sample simulation. No external write was made.'}
            else:result=self.connector(connection).execute(action,source)
            self.store.update('actions',{'id':aid,'status':'executing'},{'status':'succeeded','result':result,'error':None,'updated':time.time()})
        except Exception as exc:
            uncertain=isinstance(exc,UncertainDelivery) or not isinstance(exc,(IntegrationError,ValueError))
            error=str(exc) if isinstance(exc,(IntegrationError,ValueError)) else 'Completion could not be confirmed. Check the provider before closing this action.'
            self.store.update('actions',{'id':aid,'status':'executing'},{'status':'uncertain' if uncertain else 'failed','error':error,'updated':time.time()})

    def snapshot(self,org):
        settings=self.store.settings(org)
        actions=self.store.find('actions',{'org_id':org,'mode':settings['mode']},sort=[('created',-1)])
        runs=self.store.find('runs',{'org_id':org,'mode':settings['mode']},sort=[('created',-1)],limit=10)
        return {'actions':actions,'runs':runs,'settings':settings,'connections':self.store.connections(org_id=org),
                'preferences':self.store.find('preferences',{'org_id':org},sort=[('created',-1)]),
                'pending':sum(a['status'] in ('proposed','failed','uncertain') for a in actions),
                'completed':sum(a['status']=='succeeded' for a in actions),
                'busy':any(r['status'] in ('queued','running') for r in runs) or any(a['status'] in ('queued','executing') for a in actions)}
