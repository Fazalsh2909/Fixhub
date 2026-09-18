# Stay connected to GitHub (App mode)

Fixhub stays connected via a **GitHub App installation**. Tokens are short-lived
(~1h), cached in memory, never stored, never logged, never sent to the sandbox.

## 1. Create the App

1. GitHub → Settings → Developer settings → GitHub Apps → New.
2. Permissions: **Issues** (read), **Contents** (read), **Pull requests** (write),
   **Checks** (read, optional). Subscribe to: `issues`, `issue_comment`, `label`.
3. Note the **App ID** and **slug**; generate a **private key** (.pem).

## 2. Configure Fixhub

```bash
cp backend/.env.example backend/.env
# set:
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/fixhub-app.pem   # or paste PEM into GITHUB_PRIVATE_KEY
GITHUB_APP_SLUG=my-fixhub-debugger
GITHUB_WEBHOOK_SECRET=<random 32+ chars>
AUTO_TRIGGER_ON_ISSUE=true   # any opened/reopened issue on a connected repo starts the agent
```

## 3. Install + connect

1. Install the App on your account/org (`https://github.com/apps/<slug>/installations/new`).
2. Copy the **installation id** from the URL (`/installations/<id>`).
3. Open Fixhub → paste it into **GITHUB INSTALLATION** → the header shows `App ✓`.
4. Repos appear via `GET /api/github/repos?installation_id=…`; click **connect**.
5. Localhost webhooks: `smee.io` or `ngrok http 8001` → set the App webhook URL to
   `https://<tunnel>/webhooks/github` with the same secret. Without a tunnel, use
   the chatbot (`list issues` polls the API) — same tasks, no webhook needed.

## 4. Work a repo

- Chat: `list issues` → `fix #N` → **Run agent on task** → review **Diff** tab →
  **Approve & Commit** (opens a PR with Proof of Fix) or **Request changes**.
- Any public repo: paste `https://github.com/<owner>/<repo>` into **CLONE ANY OSS REPO**.
- Auto-catch (default on): opened/reopened issues on connected repos create
  tasks and the agent starts immediately — no label or comment needed.
  Set `AUTO_TRIGGER_ON_ISSUE=false` for manual triage (`fixhub-fix` label,
  `/fix` comment, or chat only). Every auto-run is cost-capped and nothing
  pushes to GitHub before Approve & Commit.

## Token lifecycle

`App JWT (10 min) → installation token (~1h, in-memory cache, 60s expiry margin)`.
`GET /api/github/status` reports connection without ever exposing tokens.
See `backend/app/github/app_auth.py`.
