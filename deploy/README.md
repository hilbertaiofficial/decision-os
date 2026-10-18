# Deploy DecisionOS on Google Cloud

The deployment uses Cloud Run (app and worker), Firestore (data), Vertex AI Gemini (agent), Firebase Authentication (sign-in) and Secret Manager (keys).

## 1. One-time setup (about 10 minutes)

1. Install and sign in to the Google Cloud CLI:
   ```sh
   brew install --cask google-cloud-sdk
   gcloud auth login
   ```
2. Create a project and attach billing: <https://console.cloud.google.com/projectcreate> and then **Billing → Link a billing account**. Note the project ID.
3. Add Firebase to the same project: <https://console.firebase.google.com> → **Add project** → choose the existing Google Cloud project.
4. In Firebase, open **Build → Authentication → Get started → Sign-in method → Google → Enable** and save.
5. In Firebase, open **Project settings → General → Your apps → Web (</>)** and register an app named `decisionos`. You don't need Firebase Hosting. Copy `apiKey` from the config. The web API key is public by design.

## 2. Deploy

```sh
cd decision-os
PROJECT_ID=your-project-id FIREBASE_API_KEY=AIza... ./deploy/deploy.sh
```

The script:

- enables the Cloud Run, Cloud Build, Artifact Registry, Firestore, Vertex AI, Secret Manager, Firebase and Identity Toolkit APIs
- creates the `(default)` Firestore database in Native mode (`asia-south1`) and TTL policies
- generates `FLASK_SECRET_KEY` and `COS_ENCRYPTION_KEY` into Secret Manager (it never overwrites existing keys)
- creates the `decisionos-run` service account with Firestore user, Vertex AI user and Secret accessor roles
- builds from source and deploys to Cloud Run with min 1 / max 1 instance and CPU always allocated, so the in-container worker keeps running

Override with `REGION=...`, `FIRESTORE_LOCATION=...`, `GEMINI_MODEL=...` or `EXTRA_ENV_VARS="SMTP_HOST=...,GOOGLE_CLIENT_ID=..."`.

## 3. After deploy

1. In Firebase, open **Authentication → Settings → Authorized domains** and add both hostnames the script prints.
2. Open `<url>/health`. It should return `{"status":"ok"}`.
3. Open `<url>`, click **Try the live demo** and then **Run briefing**. The briefing should say it came from Gemini and show proposed decisions.
4. Test **Continue with Google** in a private window.

If Gemini returns a model-not-found error, set an available model: `gcloud run services update decisionos --region asia-south1 --update-env-vars GEMINI_MODEL=gemini-2.5-flash`.

## Live connectors (optional)

Create OAuth apps as described in [integrations](../docs/integrations.md), with callbacks at `<url>/oauth/<provider>/callback`. Pass the client IDs and secrets with `EXTRA_ENV_VARS` (or Secret Manager), then redeploy. Email and password signup turns on only when `SMTP_HOST` and `SMTP_FROM` are set. Without them, sign-in uses Firebase only.

## Logs

```sh
gcloud run services logs read decisionos --region asia-south1 --limit 100
```
