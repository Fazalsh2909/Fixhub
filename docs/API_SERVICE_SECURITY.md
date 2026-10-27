# Deployed API security configuration (`AUTH_COOKIE_SECURE`)

## Why the API refuses to start

`app.main._enforce_production_guards()` refuses `ENV=staging` and `ENV=prod`
startup unless `AUTH_COOKIE_SECURE=1`. `app.api.auth` uses the same setting to
add the `Secure` attribute to the session cookie. This is intentional: a
deployed HTTPS app must never issue a cookie that could be sent over plain
HTTP.

If the service crash-loops with:

```text
RuntimeError: refusing staging startup with insecure auth cookie
(set AUTH_COOKIE_SECURE=1)
```

the guard is working; the deployed process is missing the setting.

## What the repository provides

- `infra/systemd/fixhub-api.service.d-10-security.conf` — tracked systemd
  drop-in that sets `Environment=AUTH_COOKIE_SECURE=1` for the API.
- `infra/systemd/install_api_security.py` — idempotent installer.

The base `fixhub-api.service` unit is host-managed and not tracked, so the
repository adds a drop-in instead of overwriting the unit. `Environment=`
becomes a process environment variable, which takes precedence over
`backend/.env`.

## Apply on the EC2 host (after `git pull`)

```bash
cd /home/ubuntu/Fixhub
git pull
sudo python3 infra/systemd/install_api_security.py
```

The installer:

1. verifies `fixhub-api.service` exists,
2. installs the drop-in to
   `/etc/systemd/system/fixhub-api.service.d/10-security.conf`,
3. runs `systemctl daemon-reload`,
4. restarts only `fixhub-api`,
5. verifies the **running** process has `AUTH_COOKIE_SECURE=1`
   (reads `/proc/<MainPID>/environ`),
6. checks `http://127.0.0.1:8000/health`.

It exits non-zero instead of claiming success. It never edits the base unit,
the worker, the database, migrations, or Firecracker/networking config.

## Verify manually

```bash
systemctl status fixhub-api --no-pager
sudo cat /proc/$(systemctl show -p MainPID --value fixhub-api)/environ \
  | tr '\0' '\n' | grep AUTH_COOKIE_SECURE
curl -fsS http://127.0.0.1:8000/health
```

The `/proc` check must print `AUTH_COOKIE_SECURE=1`. Do not treat the service
as fixed until it does.

## If verification fails with an `EnvironmentFile` hint

systemd gives `EnvironmentFile=` precedence over `Environment=`. If the base
unit reads an env file that sets `AUTH_COOKIE_SECURE=0`, set it to `1` in that
host-managed file (never commit it), then re-run the installer:

```bash
systemctl cat fixhub-api --no-pager | grep -i EnvironmentFile
sudo python3 infra/systemd/install_api_security.py
```

## Local development

Keep `AUTH_COOKIE_SECURE=0` in `backend/.env` for plain HTTP on localhost.
The staging/prod guard does not apply to `ENV=dev`, so local login keeps
working without HTTPS.

## Rollback

```bash
sudo rm /etc/systemd/system/fixhub-api.service.d/10-security.conf
sudo systemctl daemon-reload
sudo systemctl restart fixhub-api
```

Note: removing the drop-in while `ENV=staging|prod` makes the startup guard
refuse to boot again unless another configuration source sets
`AUTH_COOKIE_SECURE=1`.
