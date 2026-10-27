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
  drop-in that sets `Environment=AUTH_COOKIE_SECURE=1` and appends
  `EnvironmentFile=/etc/fixhub/api-security.env`.
- `infra/systemd/install_api_security.py` — idempotent installer that writes
  both files.

The base `fixhub-api.service` unit is host-managed and not tracked, so the
repository adds a drop-in instead of overwriting the unit. The base unit reads
`/home/ubuntu/Fixhub/backend/.env` through `EnvironmentFile=`, and systemd
gives `EnvironmentFile=` precedence over `Environment=`. The drop-in therefore
adds a second, later `EnvironmentFile=`; systemd reads multiple files in order
and the later file wins, so `/etc/fixhub/api-security.env` overrides
`backend/.env` for this setting. `Environment=` remains as defense in depth.

## Apply on the EC2 host (after `git pull`)

```bash
cd /home/ubuntu/Fixhub
git pull
sudo python3 infra/systemd/install_api_security.py
```

The installer:

1. verifies `fixhub-api.service` exists,
2. writes `/etc/fixhub/api-security.env` (root-owned, mode 0600) with
   `AUTH_COOKIE_SECURE=1`,
3. installs the drop-in to
   `/etc/systemd/system/fixhub-api.service.d/10-security.conf`,
4. runs `systemctl daemon-reload`,
5. checks the merged `EnvironmentFile=` order and fails unless the security
   file is read last,
6. restarts only `fixhub-api`,
7. verifies the **running** process has `AUTH_COOKIE_SECURE=1`
   (reads `/proc/<MainPID>/environ`) and that
   `http://127.0.0.1:8000/health` returns 200,
8. retries transient startup states: a just-spawned process may not have
   exec'd yet, and the app may not be listening yet. A readable wrong value
   fails immediately instead of waiting.

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

Check the read order:

```bash
systemctl cat fixhub-api --no-pager | grep -i EnvironmentFile
```

`/etc/fixhub/api-security.env` must be the **last** `EnvironmentFile=`. If
another drop-in (for example `20-*.conf`) is read after it, rename this
drop-in so it sorts later, then re-run the installer. The installer refuses to
restart when the security file is not last.

If the installer reports `has AUTH_COOKIE_SECURE=0, expected 1`, systemd
really did start the process with the old value. Check the merged unit before
restarting again:

```bash
systemctl show fixhub-api -p EnvironmentFiles
systemctl cat fixhub-api --no-pager | grep -E 'EnvironmentFile|Environment='
```

If it reports `environment is not readable`, the process exited or had not
exec'd yet. The installer retries that state for up to 30 seconds; if it still
fails, check `systemctl status` and the journal.

If the security file is last but the process still has the old value, the
running process predates the change:

```bash
sudo systemctl daemon-reload
sudo systemctl restart fixhub-api
sudo python3 infra/systemd/install_api_security.py
```

Do not edit `backend/.env` to fix this; the tracked deployment mechanism
overrides it intentionally.

## Local development

Keep `AUTH_COOKIE_SECURE=0` in `backend/.env` for plain HTTP on localhost.
The staging/prod guard does not apply to `ENV=dev`, so local login keeps
working without HTTPS.

## Rollback

```bash
sudo rm /etc/systemd/system/fixhub-api.service.d/10-security.conf
sudo rm -f /etc/fixhub/api-security.env
sudo systemctl daemon-reload
sudo systemctl restart fixhub-api
```

Note: removing the drop-in while `ENV=staging|prod` makes the startup guard
refuse to boot again unless another configuration source sets
`AUTH_COOKIE_SECURE=1`.
