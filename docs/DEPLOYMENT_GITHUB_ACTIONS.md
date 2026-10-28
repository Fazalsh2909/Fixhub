# Automatic deployment to staging EC2 via GitHub Actions + AWS SSM

Pushing to `rc/public-app-v1` runs the full CI suite; when every required job
passes, a `deploy` job tells the staging EC2 instance (region `ap-south-1`,
public URL `https://fz-dev.runs-on.dev`) to deploy that exact commit over AWS
Systems Manager (SSM). There is no SSH port for GitHub, no long-lived AWS
keys in GitHub, and no self-hosted runner executing CI code on the server.

```
push to rc/public-app-v1
        |
        v
GitHub Actions: backend tests + frontend build     (unchanged CI jobs)
        |  all required jobs pass
        v
deploy job (needs: [backend, frontend])
  1. OIDC: GitHub exchanges its token for short-lived AWS credentials
  2. assumes the dedicated deployment IAM role
  3. preflight: instance must be SSM-Online
  4. SSM Run Command -> server: deploy the EXACT commit
  5. server runs infra/deploy/deploy.sh:
       sync canonical checkout -> sync service tree -> deps
       -> systemd config -> migrations -> drain worker
       -> restarts -> live security verify -> health checks
       (rollback to previous SHA on failure; DB never downgraded)
  6. workflow polls the command; non-zero server exit fails the job
```

## How to activate (one-time)

Complete **A (AWS)** and **B (GitHub)** in order. Everything below uses the
AWS Console and GitHub web UI; placeholders you must fill in are marked
`<...>` and each one explains where to find it.

### A. AWS setup

Region for everything below: **`ap-south-1`** (the staging instance's
region). Set the console region selector to `ap-south-1` first.

#### A1. Find the instance ID

EC2 Console -> Instances -> select the FixHub staging instance -> copy the
**Instance ID** (format `i-0...`). You will need it here and as the
`EC2_INSTANCE_ID` GitHub variable in step B2.

#### A2. Verify the instance is SSM-managed

Systems Manager Console -> **Node Management -> Fleet Manager** (or
**Nodes**). The instance must appear with **Managed = yes** and a ping that
is neither `ConnectionLost` nor long-stale.

If it is missing:

1. The SSM Agent is preinstalled on Amazon Linux and Ubuntu 24.04 AL2023-style
   AMIs. Check on the server (one manual SSH is acceptable for this one-time
   setup): `systemctl status snap.amazon-ssm-agent.amazon-ssm-agent.service`
   or `systemctl status amazon-ssm-agent`. If absent, install it per
   https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-manual-agent-install.html
   for your OS.
2. The instance needs an **instance profile** with the AWS-managed policy
   `AmazonSSMManagedInstanceCore`. EC2 Console -> instance -> Actions ->
   Security -> Modify IAM role. **Attach the role to the existing profile or
   add the managed policy to the existing role — never recreate the profile**
   (it may carry permissions the app depends on). If the instance has no
   profile at all: IAM Console -> Roles -> Create role -> EC2 service role ->
   attach `AmazonSSMManagedInstanceCore` -> then select it via "Modify IAM
   role". Reboot is not required; the agent registers within a few minutes.
3. Confirm outbound HTTPS (443) to the SSM endpoints is allowed. The instance
   must reach `ssm.<region>.amazonaws.com`, `ssmmessages.<region>.amazonaws.com`,
   and `ec2messages.<region>.amazonaws.com` — either through an internet
   gateway/NAT or interface VPC endpoints. Do not open inbound ports for SSM;
   it is outbound-only.

Re-check Fleet Manager until the instance shows **Online**. The workflow's
preflight step fails with a pointer to this section if it is not.

#### A3. Create the GitHub OIDC identity provider (once per account)

IAM Console -> Identity providers -> Add provider:

- Type: **OpenID Connect**
- URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

If `token.actions.githubusercontent.com` already exists (another repo uses
OIDC), reuse it — do not create a duplicate.

#### A4. Create the deployment role

IAM Console -> Roles -> Create role -> Custom trust policy. Paste, replacing
nothing else (this is the complete policy — repo AND branch restricted):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:Fazalsh2909/Fixhub:ref:refs/heads/rc/public-app-v1"
        }
      }
    }
  ]
}
```

- `<ACCOUNT_ID>`: your 12-digit AWS account id (console top-right menu).
- The `sub` condition hard-restricts assumption to this repository's
  `rc/public-app-v1` branch. Workflow dispatch runs on that branch carry the
  same `sub`, so manual deploys work too; PRs and other branches cannot
  assume the role even if the workflow guard were ever bypassed.
- Role name: `FixHubGitHubDeployRole` (the name actually in use; record it
  for B2).
- Do **not** attach any AWS-managed policy like AdministratorAccess.

Then add the inline **least-privilege permissions policy** (IAM -> Roles ->
`FixHubGitHubDeployRole` -> Permissions -> Add inline policy), replacing
`<ACCOUNT_ID>` and `<INSTANCE_ID>`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PreflightInstanceOnline",
      "Effect": "Allow",
      "Action": "ssm:DescribeInstanceInformation",
      "Resource": "*",
      "Condition": {
        "StringEquals": { "ssm:region": "ap-south-1" }
      }
    },
    {
      "Sid": "SendDeployCommandToStagingInstance",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ssm:ap-south-1:<ACCOUNT_ID>:instance/<INSTANCE_ID>",
        "arn:aws:ssm:ap-south-1::document/AWS-RunShellScript"
      ],
      "Condition": {
        "StringEquals": { "ssm:documentName": "AWS-RunShellScript" }
      }
    },
    {
      "Sid": "ReadDeployCommandResult",
      "Effect": "Allow",
      "Action": [
        "ssm:GetCommandInvocation",
        "ssm:ListCommandInvocations"
      ],
      "Resource": "arn:aws:ssm:ap-south-1:<ACCOUNT_ID>:instance/<INSTANCE_ID>"
    }
  ]
}
```

This allows exactly: check the instance is online, run `AWS-RunShellScript`
on that one instance, read that command's result. Nothing else — no
`AdministratorAccess`, no wildcard SSM on all instances, no other documents,
no other regions.

#### A5. One-time server preparation

None beyond what the server already has. The deploy path reuses the
existing server layout:

- `/home/ubuntu/Fixhub` — the **canonical Git checkout**. It is the only
  place on the server that needs `.git`; its `git fetch` already works for
  manual pulls. The api and worker services run from this tree with the
  venv `/home/ubuntu/.venvs/fixhub312`.
- `/opt/fixhub` — the **service-code tree** executed by the root-owned
  `fixhub-netd` and `fixhub-egress` services (with its own
  `backend/.venv`). It does **not** need to be a Git checkout and the
  deploy never creates one there: every deploy synchronizes it from a
  validated `git archive` of the exact target commit, read from the
  canonical checkout's object database. Only tracked paths are ever copied
  or removed, so untracked runtime state — `backend/.venv`, `backend/.env`,
  workspaces, databases — is untouched by construction, and both trees
  always end at the same SHA.
- The existing `install_api_security.py` flow.

SSM Run Command runs as root by default; the deploy script performs git
operations as the repository owner, and only inside the canonical checkout.

Optional server-tunable knobs live in `/etc/fixhub/deploy.env` (create it
root-owned `0600` if needed; it is never committed):

```bash
# /etc/fixhub/deploy.env — deploy.sh sources this if present
DRAIN_TIMEOUT_S=900            # max seconds to wait for a running RQ job
PUBLIC_HEALTH_URL=https://fz-dev.runs-on.dev/health
PUBLIC_HEALTH_REQUIRED=1       # 0 = skip the public check (e.g. tunnel
                               # doesn't route /health yet)
# FRONTEND_DIST_DIR=/var/www/fixhub   # uncomment to publish frontend builds
```

### B. GitHub setup

#### B1. Create the `staging` environment

Repo -> **Settings -> Environments -> New environment** -> name `staging`.
Then on the environment page, under Deployment protection rules:

- **Required reviewers**: leave empty (deployments are already gated by CI).
- **Deployment branches and tags**: select **Selected branches and tags**,
  add rule `rc/public-app-v1`. This is a second, UI-enforced gate on top of
  the workflow's `if` condition.

#### B2. Repository variables (non-secret)

Repo -> **Settings -> Secrets and variables -> Actions -> Variables tab ->
New repository variable**, three entries:

| Name | Value | Where to get it |
| --- | --- | --- |
| `AWS_REGION` | `ap-south-1` | fixed for this deployment |
| `EC2_INSTANCE_ID` | `i-0...` | step A1 |
| `AWS_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/FixHubGitHubDeployRole` | step A4 |

No secrets are required: OIDC issues short-lived credentials per run, so
there is no AWS key to store. (If you ever need a genuinely secret value,
use the **Secrets** tab on the same page — never commit it to git.)

#### B3. Run it

Push any commit to `rc/public-app-v1` (or Actions -> ci -> Run workflow with
branch `rc/public-app-v1`). Watch it under the **Actions** tab: the `deploy`
job appears only after `backend` and `frontend` both pass.

## Monitoring and troubleshooting

- **Where to look**: Actions -> the run -> `deploy (staging EC2)`. The
  "Wait for deployment and report" step prints the server's stdout/stderr.
- **Server-side logs**: every deploy appends a timestamped full log at
  `/var/log/fixhub/deploy-<utc-timestamp>.log` (symlinked as
  `deploy-latest.log`). SSM truncates inline command output (~24k chars) —
  the on-server file never truncates; check it when GitHub shows a truncated
  tail.
- **Failed SSM commands**: AWS Console -> Systems Manager -> Run Command ->
  Command history -> the command id (also printed by the workflow) shows
  status, target, and output. `StatusDetails` of `Timeout`/`Cancelled`
  usually means the agent dropped offline mid-deploy; check Fleet Manager
  and the instance's `amazon-ssm-agent` logs.
- **Deployment failed at health checks**: the workflow fails and the job
  summary reports the deployed SHA. When a previous successful deployment is
  recorded, the server attempts a code-only rollback to that SHA (see
  `/var/log/fixhub/deploy-latest.log` for `rollback verified` or the
  manual-recovery steps). The database is never downgraded automatically; if
  the previous code cannot serve the migrated schema, the log says exactly
  that and names the manual steps.
- **First automated deployment fails**: with no previous successful
  deployment recorded there is nothing to roll back to, and the log says so
  explicitly (`no previously deployed SHA recorded; automatic rollback not
  possible`) instead of claiming a rollback happened. This is safe to retry:
  the failed run leaves the service tree untouched or only partially synced,
  and the next successful run heals it with a full export of tracked files
  (nothing runtime-owned is ever deleted). To recover manually, use git only
  in `/home/ubuntu/Fixhub` — never in `/opt/fixhub`, which has no `.git` —
  then re-run the deploy script with the last good SHA; it re-validates the
  commit, re-syncs both trees, and restarts safely.
- **Worker busy at deploy time**: the deploy waits up to `DRAIN_TIMEOUT_S`
  (default 900s) for the running RQ job, then fails without killing it.
  Re-run the deployment when the job finishes, or raise the timeout in
  `/etc/fixhub/deploy.env`.
- **Two pushes in quick succession**: the second deployment waits (concurrency
  group `deploy-staging`, `cancel-in-progress: false`) and then deploys its
  own exact SHA.

## What deploy.sh will and will not do

Will: validate the SHA (40-hex + ancestor of `origin/rc/public-app-v1`);
sync the canonical checkout `/home/ubuntu/Fixhub` (api/worker) to that exact
SHA and synchronize the `/opt/fixhub` service tree (netd/egress — no `.git`
required) from a validated `git archive` of the same commit, copying only
tracked paths so untracked runtime state (`backend/.venv`, `backend/.env`,
workspaces) is untouched by construction; pip-sync a venv only when
`backend/requirements.txt` changed; reinstall tracked systemd drop-ins and
run `install_api_security.py` only when `infra/systemd/**` changed; run
`alembic upgrade head` once before restarts; drain the worker; restart
services in dependency order (egress -> netd -> worker -> api); verify the
live process still has `AUTH_COOKIE_SECURE=1`; health-check locally and
publicly; record the deployed SHA in `/var/lib/fixhub/deploy-state.json`.

Will not: run `git clean`; require or create `.git` under `/opt/fixhub`;
delete or rewrite `backend/.env`, virtualenvs, databases, or task
workspaces; delete a directory during sync (an upstream directory removal
fails the deploy instead of destroying possible runtime data); touch
host-managed systemd units or server-specific drop-ins it does not own;
restart netd/egress unless their code or configuration actually changed;
downgrade the database; interrupt active Firecracker sandboxes to make
deployment easier; print secrets.

Known limitation: the restart planner restarts `fixhub-egress` only when
sandbox or netd code changes. Backend changes outside `app/sandbox/` (for
example `app/config.py`) are synchronized into `/opt/fixhub` on every
deploy, but the running egress process keeps the previous code until the
next sandbox/netd change restarts it.

## Relation to the manual AUTH_COOKIE_SECURE runbook

`docs/API_SERVICE_SECURITY.md` remains the reference for the security
drop-in. The automated pipeline runs the same installer (in `--no-restart`
mode during config sync) and re-verifies the live process on every deploy,
so the manual `git pull` + installer steps are no longer needed after this
pipeline is active.
