# Configuration

Every setting is read from the environment at import time by `ApiCore/settings.py`.
In development `python-dotenv` loads a `.env` file from the project root; in a
container the variables come from the environment itself. Copy `.env.example` to
`.env` and fill it in.

## Startup requirements

Four variables must be present or **the settings module cannot be imported** — every
`manage.py` command and the WSGI app fail immediately, before any request is served:

| Variable | Failure when missing |
|---|---|
| `DJANGO_ALLOWED_HOSTS` | `AttributeError: 'NoneType' object has no attribute 'split'` |
| `GOOGLE_PLAY_INTEGRITY_DECRYPTION_KEY` | `TypeError: argument should be a bytes-like object or ASCII string` |
| `GOOGLE_PLAY_INTEGRITY_VERIFICATION_KEY` | `TypeError: argument should be a bytes-like object or ASCII string` |
| `GOOGLE_PLAY_INTEGRITY_APP_SIGNING_KEY` | `AttributeError: 'NoneType' object has no attribute 'replace'` |

The three Play Integrity keys are required even on an iOS-only deployment, because
`PLAY_INTEGRITY_CONFIG` is built eagerly at import. `SECRET_KEY` imports fine when
missing but fails on the first request that needs it, so treat it as required too.

## Reference

### Django

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes | Generate: `python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"` |
| `DEBUG` | no (`0`) | Parsed as an **integer**. `1` or `0` — `true`/`false` raises `ValueError` on startup. |
| `DJANGO_ALLOWED_HOSTS` | yes | Comma-separated hostnames, no spaces. A leading dot matches subdomains: `.onrender.com`. |
| `CSRF_TRUSTED_ORIGINS` | no | Comma-separated origins **including scheme**: `https://api.example.com`. Needed when the public origin is not what Django sees — otherwise admin login fails the CSRF origin check. |
| `USE_X_FORWARDED_PROTO` | no (`0`) | `1` makes Django trust `X-Forwarded-Proto` for `request.is_secure()`. Only set this when a proxy terminates TLS **and** strips any client-supplied copy of that header; otherwise callers can fake HTTPS. |

`DEBUG=1` is for local work only. It disables `ALLOWED_HOSTS` enforcement, serves
static files without `collectstatic`, and — because `PLAY_INTEGRITY_CONFIG` is built
with `production=not DEBUG` — **skips app and device integrity verdict checks**, so
attestation stops being a real gate.

### Firebase Cloud Messaging

Supply the service account one way or the other. If both are set, the inline JSON wins.

| Variable | Notes |
|---|---|
| `FIREBASE_CREDENTIALS_JSON` | The service account JSON inline. Fine on platforms with a secrets UI. **Unusable in a Docker Compose `env_file`**, which cannot carry the multi-line private key. |
| `FIREBASE_CREDENTIALS_FILE` | Path to the JSON on disk. The container path when mounted through `docker-compose.yml` is `/run/secrets/firebase-service-account.json`. |

Neither is required to boot. Without credentials the app starts and every endpoint
works except notification delivery and FCM topic subscription — subscription failures
are logged at debug level and swallowed, so a missing service account shows up as
notifications that silently never arrive.

Get the file from the Firebase console: **Settings → Service Accounts → Generate New
Private Key**.

### App attestation

| Variable | Platform | Notes |
|---|---|---|
| `APK_NAME` | Android | Package name, e.g. `com.companyname.appname`. Checked against the verdict's `packageName`. |
| `GOOGLE_PLAY_INTEGRITY_DECRYPTION_KEY` | Android | Base64. Play Console → Play Integrity API → response encryption set to **manual**. |
| `GOOGLE_PLAY_INTEGRITY_VERIFICATION_KEY` | Android | Base64 DER public key, from the same screen. |
| `GOOGLE_PLAY_INTEGRITY_APP_SIGNING_KEY` | Android | SHA-256 of the signing certificate, hex, colons optional: `AB:CD:...`. Obtain via `./gradlew signingReport`. |
| `APP_ATTEST_APP_ID` | iOS | `<TEAM ID>.<bundle id>`, e.g. `ABCDE12345.com.companyname.appname`. |

The Play Integrity config sets `allow_non_play_distribution=True`, which is why the
signing key is mandatory: builds distributed outside the Play Store are accepted, but
only if the signing certificate matches.

### Database

Django has no single-URL form. A provider that gives you one `DATABASE_URL` such as
`postgresql://user:pass@host:5432/dbname` has to be split into these parts:

| Variable | Maps to |
|---|---|
| `PG_DATABASE` | `dbname` |
| `PG_USER` | `user` |
| `PG_PASSWORD` | `pass` |
| `PG_HOST` | `host` |
| `PG_PORT` | `5432` |

Under `docker-compose.yml`, `PG_HOST` and `PG_PORT` are overridden to `db` and `5432`
so the API reaches the database over the compose network whatever `.env` says.

### Container runtime

Read by the image, not by Django. All optional.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | Port gunicorn binds. Platforms that assign a port (Render, Fly, Cloud Run, Heroku) set this for you. |
| `API_PORT` | `8000` | Host port `docker compose` publishes. |
| `WEB_CONCURRENCY` | `3` | Gunicorn worker processes. Rule of thumb: `(2 × cores) + 1`. |
| `GUNICORN_THREADS` | `2` | Threads per worker. Any value above 1 switches gunicorn to the `gthread` worker. |
| `GUNICORN_TIMEOUT` | `60` | Seconds before a stuck worker is killed. |
| `RUN_MIGRATIONS` | `1` | Set `0` when replicas start concurrently or migrations run as a separate release step — parallel `migrate` calls race. |
| `RUN_COLLECTSTATIC` | `1` | Set `0` when static files come from a CDN or the filesystem is read-only. |

## Settings that are not environment-driven

Change these in `ApiCore/settings.py` if the defaults do not fit:

| Setting | Value | Effect |
|---|---|---|
| `ACCESS_TOKEN_LIFETIME` | 5 minutes | How often a client must call `/api/token/refresh/`. |
| `REFRESH_TOKEN_LIFETIME` | 20 days | After this, the device must re-attest from scratch. |
| `ATTESTATION_NONCE_EXPIRY_SECONDS` | 120 | Lifetime of a nonce, for both attestation and wallet linking. |
| `ATTESTATION_NONCE_CLEANUP_TIMEOUT_SECONDS` | 300 | Minimum gap between expired-nonce sweeps. |
| `DELETE_INACTIVE_DEVICES` | `True` | A device FCM rejects is deleted, taking its wallet links with it (`on_delete=CASCADE`). |
| `ONE_DEVICE_PER_USER` | `False` | Several devices may share a `uid` or a wallet address; all of them get notified. |

No `CACHES` backend is configured, so Django uses per-process local memory. The only
consumer is the nonce cleanup lock, which therefore throttles per gunicorn worker
rather than per deployment. Harmless — the sweep is idempotent — but if you add real
caching, use a shared backend.
