# Connecting an existing server

Two separate questions, answered in turn:

1. [Sending notifications](#sending-notifications-from-your-backend) — your backend
   calls this API when something happens.
2. [Running it alongside what you already have](#running-it-alongside-an-existing-service) —
   reverse proxy, shared database, shared Firebase project.

---

## Sending notifications from your backend

`POST /api/fcm/send-notification/` is the only endpoint your backend needs. It is
authenticated with an API key, entirely separately from the device JWTs.

### Issue an API key

Through the admin: **/admin/ → API Key Manager → API keys → Add**. The full key is
shown **once**, on the confirmation screen. It is not recoverable — only a hash is
stored.

Or from the shell, which is what you want in a deploy script:

```bash
python manage.py shell -c "
from rest_framework_api_key.models import APIKey
print(APIKey.objects.create_key(name='billing-service')[1])
"
```

Issue one key per calling service. Revoke by ticking **revoked** in the admin or
deleting the row; either takes effect immediately, and a per-service key means
revoking one caller does not interrupt the others. Keys also accept an expiry date.

### Call it

```
Authorization: Api-Key <key>
Content-Type: application/json
```

```python
import httpx

def notify_wallet(chain: str, address: str, title: str, body: str) -> bool:
    response = httpx.post(
        f"{NOTIFICATIONS_URL}/api/fcm/send-notification/",
        headers={"Authorization": f"Api-Key {NOTIFICATIONS_API_KEY}"},
        json={"chain": chain, "address": address, "title": title, "body": body},
        timeout=10,
    )

    # Nobody has linked that address, or nobody has an FCM token yet. Expected, not
    # an error — do not alert, do not retry.
    if response.status_code == 404:
        return False

    response.raise_for_status()
    return response.json()["success_count"] > 0
```

```csharp
var request = new HttpRequestMessage(HttpMethod.Post, "/api/fcm/send-notification/")
{
    Content = JsonContent.Create(new { chain, address, title, body })
};
request.Headers.Authorization = new AuthenticationHeaderValue("Api-Key", apiKey);

var response = await http.SendAsync(request);
```

`AuthenticationHeaderValue("Api-Key", key)` is right — the scheme is literally
`Api-Key`, not `Bearer`.

### Choosing a target

| Target | Use when |
|---|---|
| `chain` + `address` | The event belongs to a wallet. Ownership was proven for Solana. |
| `user_id` | You already have an identifier your own system trusts (customer ID, account number). |

Send one or the other, never both — a request carrying both is rejected with 400.

`user_id` matches `AttestedFCMDevice.uid`, which any authenticated device can set to
any value. Treat it as a routing hint, never as authorisation: do not put anything in
the notification body that the wrong recipient must not see.

### What the responses mean

| Status | Meaning | Your move |
|---|---|---|
| `200` | Delivery was attempted. Check `success_count` / `failure_count`. | Log the failure count; individual failures are usually stale FCM tokens, which get cleaned up automatically. |
| `400` | Malformed payload — no target, both targets, or `title`/`body` too long (150 / 500). | Fix the caller. Never retry. |
| `401` | Missing, wrong, or revoked API key. | Fix the credential. Never retry. |
| `404` | No device matches. | Normal. Do not alert. |

A `200` means FCM accepted the request, not that a notification appeared on a screen.
There is no delivery receipt.

### Operational notes

- **Not idempotent.** Retrying a timed-out call can deliver twice. Retry only on
  connection errors and 5xx, and prefer notifications that are harmless when repeated.
- **Synchronous.** The endpoint loops over matching devices and calls FCM once per
  device before responding. A `uid` shared by many devices makes the call slow, so
  send from a background job rather than inside a user-facing request.
- **Best-effort.** A per-device failure is logged and counted, not raised. The overall
  request still returns 200.

### Broadcasting

`send-notification` targets individuals. For "everyone", devices are already
subscribed to FCM topics — `global`, `android`, `ios`, and one per linked chain
(`solana`, `polkadot`). Send to those from the Firebase console or straight through
the FCM API with the same service account. This API is not involved.

---

## Running it alongside an existing service

### Behind a reverse proxy

Three settings have to agree with the proxy or the deployment misbehaves in ways that
look unrelated to the proxy:

```dotenv
DJANGO_ALLOWED_HOSTS=notifications.example.com
CSRF_TRUSTED_ORIGINS=https://notifications.example.com
USE_X_FORWARDED_PROTO=1
```

- `DJANGO_ALLOWED_HOSTS` must list the **public** hostname, the one in the `Host`
  header the proxy forwards. Not the container name, not the internal IP.
- `CSRF_TRUSTED_ORIGINS` is what makes admin login work over HTTPS. Without it Django
  compares the `Origin` header against its own idea of the origin, they differ, and
  login fails with a bare "CSRF verification failed" that has nothing to do with
  cookies.
- `USE_X_FORWARDED_PROTO=1` makes `request.is_secure()` reflect the client's scheme
  rather than the plain-HTTP hop between proxy and container.

Enable that last one **only** when the proxy overwrites `X-Forwarded-Proto` on every
request. If a client can supply its own, it can convince Django its plaintext request
arrived over TLS.

nginx, on a subdomain:

```nginx
server {
    listen 443 ssl;
    server_name notifications.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;   # always set, never append
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    }
}
```

Add `--forwarded-allow-ips="<proxy IP>"` to the gunicorn command if you also rely on
`X-Forwarded-For`; gunicorn ignores forwarding headers from untrusted peers by default.

### On a subpath of an existing site

To serve the API at `https://example.com/notifications/` instead of its own subdomain,
tell Django where it is mounted with the `SCRIPT_NAME` header, which gunicorn strips
from the path before Django routes it:

```nginx
location /notifications/ {
    proxy_pass http://127.0.0.1:8000/notifications/;
    proxy_set_header Host        $host;
    proxy_set_header SCRIPT_NAME /notifications;
}
```

> [!WARNING]
> gunicorn accepts `SCRIPT_NAME` from whoever sends it. The `proxy_set_header` above
> must be present on **every** location that proxies to this app, so a client-supplied
> header is always overwritten. Otherwise a caller can rewrite the routing prefix.

A subdomain avoids this entirely. Prefer it unless you have a reason not to.

### Sharing a PostgreSQL server

Use the same server, but give this app its **own database**:

```sql
CREATE DATABASE pluto_notifications;
CREATE USER pluto WITH PASSWORD '...';
GRANT ALL PRIVILEGES ON DATABASE pluto_notifications TO pluto;
```

Do not point it at a database another Django project already uses. Django's own
bookkeeping tables — `django_migrations`, `auth_user`, `django_content_type` — are not
namespaced per project, and two projects sharing them will corrupt each other's
migration state.

Then in `.env`:

```dotenv
PG_DATABASE=pluto_notifications
PG_USER=pluto
PG_PASSWORD=...
PG_HOST=db.internal.example.com
PG_PORT=5432
```

The tables this app creates are `ApiApp_attestedfcmdevice`, `ApiApp_walletlink`,
`ApiApp_nonce`, plus the standard Django, `fcm_django`, and
`rest_framework_api_key` tables.

To use an external database with the supplied compose file, drop the `db` service and
the `depends_on` block, and remove the `PG_HOST` / `PG_PORT` overrides from the `api`
service so the values in `.env` are used:

```yaml
services:
  api:
    build: .
    env_file: [.env]
    volumes: ["./secrets:/run/secrets:ro"]
    ports: ["8000:8000"]
    restart: unless-stopped
```

### Sharing a Firebase project

The service account needs the **Firebase Cloud Messaging API** enabled and permission
to send messages; nothing else. If another service already sends notifications from
the same Firebase project, it can keep its own service account — several are fine.

What must **not** be shared is the FCM registration token namespace: a device token
belongs to one app. If your existing backend also stores tokens for the same app,
decide which system owns delivery. Both sending to the same device is not an error,
but it does mean duplicate notifications.
