# Deployment

The repository ships a production image and a compose stack. Everything below assumes
a filled-in `.env` — see [configuration.md](configuration.md).

## Docker Compose

The fastest complete deployment: API plus PostgreSQL on one host.

```bash
cp .env.example .env          # then fill it in
mkdir -p secrets              # and drop the Firebase JSON in
docker compose up --build -d
```

That gives you `http://localhost:8000/`. Create an admin login:

```bash
docker compose exec api python manage.py createsuperuser
```

What happens on `up`:

1. PostgreSQL starts and is polled with `pg_isready` until it answers.
2. Only then does the API container start — `depends_on` alone waits for the container,
   not for the database inside it, and the API migrates immediately on boot.
3. The entrypoint runs `migrate`, then `collectstatic`, then execs gunicorn.

Day-to-day:

```bash
docker compose logs -f api           # follow logs
docker compose ps                    # health status
docker compose exec api sh           # shell inside the container
docker compose down                  # stop, keep the database volume
docker compose down -v               # stop and DELETE the database volume
```

`down -v` destroys every device registration, wallet link, and API key. There is no
undo.

### Secrets

`.env` is excluded from the image by `.dockerignore` and passed in at runtime, so no
secret is baked into a layer. The Firebase service account is mounted read-only from
`./secrets` to `/run/secrets`:

```dotenv
FIREBASE_CREDENTIALS_FILE=/run/secrets/firebase-service-account.json
```

The file form exists because Docker Compose's `env_file` cannot carry a multi-line
value, and the service account's private key is multi-line. `FIREBASE_CREDENTIALS_JSON`
still works where the platform has a proper secrets store.

### Backups

The database lives in the `pgdata` named volume.

```bash
docker compose exec db pg_dump -U pluto pluto_notifications > backup.sql
cat backup.sql | docker compose exec -T db psql -U pluto pluto_notifications
```

## The image on its own

```bash
docker build -t pluto-notifications .
docker run -d --name notifications -p 8000:8000 --env-file .env \
  -v "$PWD/secrets:/run/secrets:ro" pluto-notifications
```

### What is inside

- `python:3.11-slim`, two stages. Compilers live in the build stage only.
- Runs as a non-root user, UID 10001. Application code is root-owned and not writable
  by the process serving it; only `/app/static` is.
- Gunicorn binds `0.0.0.0:$PORT`, defaulting to 8000. Platforms that assign a port
  (Render, Fly, Cloud Run, Heroku) work with no override.
- `HEALTHCHECK` probes `/admin/login/` and treats any HTTP reply as healthy, including
  a 400 from `ALLOWED_HOSTS`. It answers "is the server up", not "is it configured
  correctly" — a database blip should not restart a working container.
- Sizing through `WEB_CONCURRENCY`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT`.

### Startup behaviour

The entrypoint runs migrations and `collectstatic` before starting gunicorn. Both are
opt-out:

```bash
RUN_MIGRATIONS=0      # replicas starting together, or migrations as a release step
RUN_COLLECTSTATIC=0   # static served by a CDN, or a read-only filesystem
```

Turn `RUN_MIGRATIONS` off as soon as you run more than one replica. Concurrent
`migrate` calls race, and the losers can crash-loop. Run it once instead:

```bash
docker run --rm --env-file .env --entrypoint python pluto-notifications \
  manage.py migrate --noinput
```

`--entrypoint python` bypasses the startup script, so this runs the migration and
nothing else.

## Hetzner, deployed by GitHub Actions

A dedicated guide covers the full path from an empty Hetzner Cloud server to automatic
deploys on every push to `main` — server provisioning, TLS with Caddy, deploy keys,
rollback: **[hetzner-deployment.md](hetzner-deployment.md)**.

It uses [`docker-compose.prod.yml`](../docker-compose.prod.yml) rather than the compose
file above. The difference is that it pulls a pre-built image from GHCR instead of
building from source, so the server never holds the source tree, and it binds the API
to loopback so only the reverse proxy can reach it.

## Platform-as-a-service

Any platform that builds a Dockerfile works unmodified — the image reads `PORT`.

**Render, Railway, Fly.io:** point at the repository, let the platform detect the
Dockerfile, set the environment variables from `.env.example`, and attach a managed
PostgreSQL. Providers hand out a single `DATABASE_URL`; split it into `PG_DATABASE`,
`PG_USER`, `PG_PASSWORD`, `PG_HOST`, `PG_PORT`, since Django has no URL form.

Set `DJANGO_ALLOWED_HOSTS` to the assigned hostname — `.onrender.com` matches every
subdomain — plus `CSRF_TRUSTED_ORIGINS=https://<your-app>.onrender.com` and
`USE_X_FORWARDED_PROTO=1`, because these platforms terminate TLS in front of you.

**Without Docker** (a plain buildpack or a VM), the equivalent commands are:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn ApiCore.wsgi:application --bind 0.0.0.0:$PORT
```

Static files are served by WhiteNoise from inside the app, so no separate web server
is required for them.

## Behind a reverse proxy

Covered in [server-integration.md](server-integration.md#behind-a-reverse-proxy),
including the nginx config, the subpath case, and the settings that must agree with
the proxy.

## Production checklist

- [ ] `DEBUG=0`. With `DEBUG=1` the Play Integrity config is built with
      `production=False`, which **skips the app and device integrity verdicts** —
      attestation stops being a real gate.
- [ ] `SECRET_KEY` is unique to this deployment and was never committed.
- [ ] `DJANGO_ALLOWED_HOSTS` lists only hostnames you serve.
- [ ] `CSRF_TRUSTED_ORIGINS` set if the public origin differs from what Django sees.
- [ ] `USE_X_FORWARDED_PROTO=1` **only** behind a proxy that overwrites that header.
- [ ] TLS terminated in front of the app. Device JWTs and API keys are bearer
      credentials — over plain HTTP they are readable in transit.
- [ ] `GOOGLE_PLAY_INTEGRITY_APP_SIGNING_KEY` is the certificate that signed the
      *distributed* build, not the debug one.
- [ ] The admin is reachable only by people who should have it. It can read every
      device, wallet link, and FCM token. Restrict by IP at the proxy if you can.
- [ ] One API key per calling service, so one can be revoked without the others.
- [ ] Database backups scheduled and a restore actually tested.
- [ ] `RUN_MIGRATIONS=0` if more than one replica starts at once.

## Upgrading

```bash
git pull
docker compose up --build -d      # entrypoint applies new migrations on boot
```

With `RUN_MIGRATIONS=0`, run `migrate` yourself before rolling out.

Rolling back a release that added a migration needs the migration reversed first:
old code against a new schema fails in ways that are hard to read.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AttributeError: 'NoneType' object has no attribute 'split'` at startup | `DJANGO_ALLOWED_HOSTS` is not set. |
| `TypeError: argument should be a bytes-like object` at startup | A `GOOGLE_PLAY_INTEGRITY_*` key is missing. All three are required even for iOS-only. |
| `ValueError: invalid literal for int()` at startup | `DEBUG` is `true`/`false`. It must be `1` or `0`. |
| `Bad Request (400)` on every request | The `Host` header is not in `DJANGO_ALLOWED_HOSTS`. |
| `CSRF verification failed` on admin login | `CSRF_TRUSTED_ORIGINS` missing, or `USE_X_FORWARDED_PROTO` not set behind TLS termination. |
| API container exits immediately | Usually a failed `migrate`. `docker compose logs api` shows it; check the database is reachable and the credentials match. |
| `FileNotFoundError` for the service account at startup | `FIREBASE_CREDENTIALS_FILE` points at a path that is not there. The file must sit in `./secrets/` on the host to appear at `/run/secrets/` in the container. |
| `exec /app/docker/entrypoint.sh: no such file or directory` | The entrypoint was checked out with CRLF endings. `.gitattributes` forces LF — re-clone, or `dos2unix docker/entrypoint.sh`. |
| Admin renders unstyled | `collectstatic` did not run, or `RUN_COLLECTSTATIC=0`. |
| Endpoints answer 301, then 405 | The trailing slash is missing. Call `/api/nonce/`, not `/api/nonce`. |
| Notifications report success but never arrive | Firebase credentials are absent or wrong. The app boots fine without them and swallows subscription errors — check the logs for FCM failures. |
| `404 No registered devices found` | Nobody linked that address, or the matching devices never called `/api/fcm/token-update/`. |
