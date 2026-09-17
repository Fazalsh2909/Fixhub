# Prod deploy (single VPS, compose + Caddy)

Target: 1 VPS (2 vCPU / 4GB), Ubuntu 24.04, domain → server IP.

## 1. Prepare secrets (on server)

```bash
cp backend/.env.example backend/.env
openssl rand -hex 32   # → GITHUB_WEBHOOK_SECRET
openssl rand -hex 32   # → API_TOKEN
openssl rand -hex 24   # → POSTGRES_PASSWORD
```

`backend/.env`:
```env
APP_ENV=production
API_TOKEN=<rand-32>
POSTGRES_PASSWORD=<rand-24>
DATABASE_URL=postgresql+psycopg2://fixhub:${POSTGRES_PASSWORD}@db:5432/fixhub
REDIS_URL=redis://redis:6379/0
CORS_ORIGINS=https://yourdomain.com
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/fixhub-app.pem
GITHUB_APP_SLUG=...
GITHUB_WEBHOOK_SECRET=<rand-32>
```

Frontend token: browser console once —
`localStorage.setItem("fixhub_api_token","<same API_TOKEN>")`.

## 2. Launch

```bash
cd infra
POSTGRES_PASSWORD=<same> docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose ps
curl -f http://localhost:8080/health/ready
```

## 3. TLS (Caddy)

DNS A-record `yourdomain.com` → server IP, then:

```bash
DOMAIN=yourdomain.com caddy run --config infra/Caddyfile --adapter caddyfile
# GitHub App webhook URL → https://yourdomain.com/webhooks/github
```

## 4. Operate

- Health: `GET /health` (liveness), `GET /health/ready` (DB+queue).
- Logs: `docker compose logs -f api worker` (JSON, secrets redacted).
- Backup DB: `docker compose exec db pg_dump -U fixhub fixhub > backup-$(date +%F).sql`
- Update: `git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
- Rollback: `git checkout <prev-sha> && ... up -d --build` (migrations are `create_all`, no down-migration needed yet — add Alembic before destructive schema changes).

## Notes / limits

- API workers=2 in prod overlay requires Redis (compose provides it). Do not run `--workers >1` with in-memory queue.
- Sandbox is Docker shared-kernel, not a microVM — treat diffs as untrusted until Approve.
- Next to Strong (85+): Alembic migrations, Sentry, Prometheus `/metrics`, S3 for diffs, ECS/RDS path in `infra/main.tf`.
