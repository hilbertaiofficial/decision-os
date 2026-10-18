<img src="web/static/logo.svg" alt="DecisionOS" height="56">


**Connect the dots. Get work done.** DecisionOS is the decision and execution layer for the human-agent workforce, built by **Hilbert AI**. It connects Gmail, Google Calendar, Jira, Slack and GitHub. It finds the decision hidden across those tools, recommends a grounded next step, and carries out the approved action with an audit trail.

> A client asks in Gmail for mobile scope in Friday's release. Jira shows the mobile issue blocked. Slack asks whether the scope is in. DecisionOS groups those three signals into **one decision**, cites the evidence, drafts the client reply, the Jira comment and the Slack update, and waits for your approval before anything leaves the building.

**AI Builder Cup track:** Future of Work & Enterprise Productivity · **License:** Apache-2.0

| | |
|---|---|
| Live prototype | https://decisionos-8827317512.asia-south1.run.app (click **Try the live demo**, no sign-up) |
| Demo video | See the submission form |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Deploy | [deploy/README.md](deploy/README.md) |

## Built on Google Cloud

| Service | What DecisionOS uses it for | Code |
|---|---|---|
| **Gemini on Vertex AI** (`google-genai`) | The decision agent. Gemini reads one workspace's signals, correlates them into decisions and calls a typed `propose_action` function. Each call is validated before it is stored. | [`src/decisionos/gemini.py`](src/decisionos/gemini.py) |
| **Firestore** (Native mode) | Every record: users, organizations, encrypted OAuth grants, source snapshots, decisions/actions, approvals, runs, the durable job queue, the worker lease, rate limits and the audit trail. TTL policies expire sessions and tokens. | [`src/decisionos/store.py`](src/decisionos/store.py) |
| **Cloud Run** | Hosts the Flask portal and the in-container leased worker (min 1 instance, CPU always allocated). Secrets are mounted from Secret Manager. | [`Dockerfile`](Dockerfile), [`deploy/deploy.sh`](deploy/deploy.sh) |
| **Firebase Authentication** | Google sign-in in the browser. The ID token is verified server-side with the Firebase Admin SDK and exchanged for a revocable session. | [`src/decisionos/firebase_auth.py`](src/decisionos/firebase_auth.py), [`web/static/firebase-login.js`](web/static/firebase-login.js) |
| Google Workspace APIs | Gmail (read, draft, reply, mark read) and Calendar (invitations and RSVP) connectors. | [`src/decisionos/integrations.py`](src/decisionos/integrations.py) |

## How a decision moves

**Observe → correlate → recommend → approve → execute → verify → remember**

1. A person or a saved schedule queues a briefing. The job is written to Firestore and the browser returns immediately.
2. The worker syncs connected sources into Firestore and skips unchanged items it has already processed.
3. Gemini receives only that organization's snapshot, the owner's standing preferences, and recent action history. Source text is treated as untrusted data.
4. Gemini calls `propose_action(item_id, action_kind, payload, reason)`. The service rejects items outside the run, mismatched tools, and malformed payloads. Gemini can't choose URLs, recipients, accounts or organizations.
5. Policy decides what happens next. Routine drafts (and optionally mark-as-read) run quietly. Email sends, Calendar RSVPs, Jira and GitHub writes, and Slack posts wait for a human to approve the exact (editable) payload.
6. The worker executes the approved action. It is marked **succeeded** only after the provider confirms it. If a write times out or is interrupted, it becomes **uncertain** and is never resent automatically.
7. The briefing, the decisions, approvals and outcomes stay in the activity history.

## Ask DecisionOS (direct mode)

Type a request on the Overview page, for example *"Create a Jira bug for the EU checkout errors, tell #incidents I own customer comms, and set up a 30 minute review tomorrow at 11am IST with priya@acmeretail.example"*. Gemini turns it into previewed actions with two typed tools:

- `act_on_item`: reply, comment, RSVP or transition on an item DecisionOS already synced.
- `create_new`: new work that isn't tied to an item: `email_send`, `calendar_create`, `jira_create`, `slack_post`.

Every action lands in the decision queue as an editable preview, is re-validated on approval, and executes only after a person approves it. Gemini asks for missing details (for example an unknown email address) instead of guessing. Code: [`gemini.ask`](src/decisionos/gemini.py), [`Service.ask`](src/decisionos/service.py).

## Implemented connectors

| Tool | Reads | Executes (after approval unless noted) |
|---|---|---|
| Gmail | Unread inbox messages, thread metadata | Save draft (auto), mark read (optional auto), send reply in thread, send a new email (Ask) |
| Google Calendar | Upcoming events, pending invitations | Accept / decline / tentative, with conflict check; create an event (Ask) |
| Jira Cloud | Issues from your JQL across selected sites | Comment, workflow transition (resolved against real transitions), create an issue (Ask) |
| Slack | Recent messages in up to five selected channels | Bot reply in the source thread, new message in a selected channel (Ask) |
| GitHub | Issues and PRs in selected repositories | Comment, close |

Workspaces support several organizations, owner/admin/member/viewer roles, invitations, TOTP two-factor authentication and per-organization preferences.

## Try it

- **Hosted:** open the prototype link and click **Try the live demo**. You get a sample workspace with Gmail, Calendar, Jira, Slack and GitHub data. Click **Run briefing** and Gemini correlates the release-scope decision. Sample accounts never contact external services.
- **Your own tools:** sign in with Google (Firebase), create an organization and connect accounts. See [integration setup](docs/integrations.md).

## Run locally

Python 3.12+.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # set COS_ENCRYPTION_KEY and either GOOGLE_CLOUD_PROJECT (Firestore + Vertex AI) or DECISIONOS_STORE=memory + GEMINI_API_KEY
python web/app.py      # COS_WORKER_INLINE=1 runs the worker in-process; or run python src/run_worker.py separately
```

## Deploy to Google Cloud

```sh
PROJECT_ID=your-project FIREBASE_API_KEY=your-web-api-key ./deploy/deploy.sh
```

The script enables the APIs, creates Firestore with TTL policies, stores secrets in Secret Manager, creates a least-privilege service account and deploys to Cloud Run. Step-by-step instructions, including the Firebase console steps, are in [deploy/README.md](deploy/README.md).

## Tests

```sh
pytest -q
```

The suite runs offline. It uses an in-memory backend with Firestore semantics, a scripted Gemini client that exercises the real function-calling loop, and provider test doubles. It covers uniqueness and rollback in the store, organization isolation, roles, invitations, approvals, recovery, billing and connector request construction.
