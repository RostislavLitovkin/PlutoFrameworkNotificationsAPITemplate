# Deploying to Hetzner with GitHub Actions

A push to `main` runs the tests, builds the image, pushes it to GitHub Container
Registry, and tells the server to pull and restart. The server never holds the source
tree and never builds anything, so a deploy is a pull plus a restart — and what runs in
production is byte-for-byte what CI tested.

```
push to main
   └─ GitHub Actions
        ├─ test    python manage.py test ApiApp --settings=ApiCore.settings_test
        ├─ build   docker build → ghcr.io/<owner>/<repo>:<sha>
        └─ deploy  ssh → docker compose pull && up -d → wait for healthy
                              │
                    Hetzner ──┴── Caddy :443 → 127.0.0.1:8000 → api ─ db
```

The workflow is [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) and
the stack it deploys is [`docker-compose.prod.yml`](../docker-compose.prod.yml).

## 1. Create the server

In the Hetzner Cloud console: **Add Server**.

| Setting | Choose |
|---|---|
| Image | Ubuntu 24.04 |
| Type | **CX22** (x86) or **CAX11** (ARM). 2 vCPU / 4 GB is comfortable for Django plus PostgreSQL. |
| SSH key | Add your own key — this is for you, not for CI. |
| Firewall | Inbound: `22`, `80`, `443`. Nothing else. |

> [!IMPORTANT]
> If you pick a **CAX** server you are on ARM64, and the default image build targets
> x86. Set a repository variable `BUILD_PLATFORM` to `linux/arm64`, or the container
> dies at startup with `exec format error`.

Do not open port 8000 in the firewall. The API binds to loopback and is reached only
through the reverse proxy.

Point a DNS `A` record at the server's IP before step 5, so Caddy can obtain a
certificate.

## 2. Base setup

SSH in as root and prepare a deploy user:

```bash
adduser --disabled-password --gecos "" deploy
curl -fsSL https://get.docker.com | sh      # Docker's official install script
usermod -aG docker deploy
install -d -o deploy -g deploy /opt/pluto-notifications
```

> [!NOTE]
> Membership of the `docker` group is equivalent to root — anyone who can run
> `docker` can mount the host filesystem. That is acceptable on a box dedicated to
> this service; do not add the deploy user to a shared machine's docker group.

## 3. Configuration on the server

Everything secret is provisioned once, by hand, and never travels through CI.

```bash
su - deploy
cd /opt/pluto-notifications
mkdir -p secrets
nano .env
```

Fill `.env` from [`.env.example`](../.env.example). The values that differ from a local
setup:

```dotenv
DEBUG=0
DJANGO_ALLOWED_HOSTS=notifications.example.com
CSRF_TRUSTED_ORIGINS=https://notifications.example.com
USE_X_FORWARDED_PROTO=1
FIREBASE_CREDENTIALS_FILE=/run/secrets/firebase-service-account.json
PG_PASSWORD=<a long random string>
```

`USE_X_FORWARDED_PROTO=1` is safe here specifically because Caddy overwrites
`X-Forwarded-Proto` on every request rather than passing a client-supplied value
through. See [configuration.md](configuration.md) for what each variable does.

Then copy the Firebase service account up:

```bash
scp firebase-service-account.json deploy@<server>:/opt/pluto-notifications/secrets/
```

Lock it down — it can send notifications to all your users:

```bash
chmod 600 /opt/pluto-notifications/secrets/firebase-service-account.json
chmod 600 /opt/pluto-notifications/.env
```

## 4. The deploy key

Generate a key **for CI only**, separate from your personal one, so it can be revoked
without locking yourself out.

On your machine:

```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions"
ssh-copy-id -i deploy_key.pub deploy@<server>
```

Now pin the server's host key. Take it from the server itself rather than trusting
whatever answers the network:

```bash
ssh deploy@<server> cat /etc/ssh/ssh_host_ed25519_key.pub
ssh-keyscan -t ed25519 <server>          # must contain the same key material
```

The `ssh-keyscan` output line is what goes into the `SSH_KNOWN_HOSTS` secret. Pinning
it is what stops the workflow handing the deploy key to an impostor; the usual
shortcut, `StrictHostKeyChecking=no`, accepts anything that answers on that address.

## 5. TLS and the reverse proxy

Caddy gets a Let's Encrypt certificate automatically and renews it.

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

`/etc/caddy/Caddyfile`, in full:

```caddyfile
notifications.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
systemctl reload caddy
```

That is the whole proxy configuration — Caddy sets `X-Forwarded-Proto` and
`X-Forwarded-For` correctly by default and handles certificates without further
input. If you would rather use nginx, there is a config in
[server-integration.md](server-integration.md#behind-a-reverse-proxy); you will also
need certbot.

## 6. GitHub secrets and variables

**Settings → Secrets and variables → Actions.**

Secrets:

| Name | Value |
|---|---|
| `SSH_HOST` | Server IP or hostname |
| `SSH_USER` | `deploy` |
| `SSH_PRIVATE_KEY` | Contents of `deploy_key` — the whole file, including the BEGIN/END lines |
| `SSH_KNOWN_HOSTS` | The `ssh-keyscan` line from step 4 |

Variables (both optional):

| Name | Default | Set it when |
|---|---|---|
| `DEPLOY_PATH` | `/opt/pluto-notifications` | You put the stack somewhere else |
| `BUILD_PLATFORM` | `linux/amd64` | You are on a **CAX** (ARM) server → `linux/arm64` |

No registry credential is needed. The workflow logs the server in to GHCR with a token
scoped to that single run, and logs out afterwards.

## 7. First deploy

Push to `main`, or run the workflow manually from the **Actions** tab.

The deploy job finishes only once the container reports healthy — `up -d` returning is
not success, since migrations run at startup and can fail. If the healthcheck never
passes, the job prints `docker compose ps` and the last 150 log lines and fails.

Once it is green, create an admin login and an API key for your backend:

```bash
ssh deploy@<server>
cd /opt/pluto-notifications
export API_IMAGE=$(cat current-image.txt)

docker compose exec api python manage.py createsuperuser
docker compose exec api python manage.py shell -c "
from rest_framework_api_key.models import APIKey
print(APIKey.objects.create_key(name='backend')[1])
"
```

> [!NOTE]
> `export API_IMAGE=$(cat current-image.txt)` is required before **any** `docker
> compose` command on the server. The production compose file names an exact image
> rather than building one, and CI writes the deployed tag to `current-image.txt`.

## What the workflow does

| Job | Does | Fails when |
|---|---|---|
| `test` | Runs the suite on in-memory SQLite | A test fails — nothing is built or deployed |
| `build` | Builds and pushes `:<sha>` and `:latest` to GHCR, with layer caching | The image does not build |
| `deploy` | Ships the compose file, pulls, restarts, waits for healthy | The container never reports healthy |

Every image is tagged with its commit SHA, so a rollback names an exact build. Deploys
are serialised — a second push waits rather than cancelling the first, which could
otherwise leave the server between states.

The `deploy` job runs in a GitHub environment called `production`. Add required
reviewers under **Settings → Environments → production** if you want deploys to be
approved rather than automatic.

## Rolling back

Previous images stay on the server — cleanup only removes untagged layers — so a
rollback needs no network:

```bash
cd /opt/pluto-notifications
docker image ls ghcr.io/rostislavlitovkin/plutoframeworknotificationsapitemplate
API_IMAGE=ghcr.io/rostislavlitovkin/plutoframeworknotificationsapitemplate:<old-sha> \
  docker compose up -d
echo "<that same image>" > current-image.txt
```

If the image was pruned you must re-pull, which needs a registry login:

```bash
echo <a PAT with read:packages> | docker login ghcr.io -u <your-username> --password-stdin
```

**A rollback does not undo migrations.** If the release you are backing out of added
one, old code will be running against a new schema. Reverse the migration first, or
roll forward with a fix instead.

## Operations

```bash
cd /opt/pluto-notifications && export API_IMAGE=$(cat current-image.txt)

docker compose logs -f api            # follow logs
docker compose ps                     # health status
docker compose restart api            # restart without redeploying
journalctl -u caddy -f                # proxy and certificate logs
```

Backups — the database lives in the `pgdata` volume:

```bash
docker compose exec -T db pg_dump -U pluto pluto_notifications | gzip > backup-$(date +%F).sql.gz
```

Put that in a cron job and copy the output off the server; a snapshot of a running
PostgreSQL volume is not a reliable backup on its own. Hetzner's own backups (enabled
per-server, billed as a percentage of the server price) cover the "server caught fire"
case, not "someone dropped a table".

Upgrading the base image or dependencies is an ordinary commit — push to `main` and the
workflow handles the rest.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Permission denied (publickey)` | The public half of `deploy_key` is not in `/home/deploy/.ssh/authorized_keys`, or `SSH_USER` is wrong. |
| `Host key verification failed` | `SSH_KNOWN_HOSTS` is stale — it changes if the server is rebuilt or reimaged. Re-run `ssh-keyscan`. |
| `exec format error` in the api logs | ARM/x86 mismatch. Set `BUILD_PLATFORM=linux/arm64` for CAX servers. |
| `denied` when the server pulls | The GHCR package is not linked to the repository. **Package → Settings → Manage Actions access**, add the repo with read access. |
| `scp: No such file or directory` | `DEPLOY_PATH` does not exist on the server, or is not writable by `deploy`. |
| Deploy fails at the health wait | Almost always a failed migration or a bad `.env`. The job prints the logs; `docker compose logs api` shows the same. |
| Caddy returns 502 | The api container is down, or `API_BIND` was changed so nothing listens on `127.0.0.1:8000`. |
| `API_IMAGE must be set` | You ran `docker compose` by hand without `export API_IMAGE=$(cat current-image.txt)`. |
| Admin login fails with a CSRF error | `CSRF_TRUSTED_ORIGINS` is missing the `https://` origin, or `USE_X_FORWARDED_PROTO=1` is not set. |

For symptoms that are not deployment-specific — startup crashes, 400s on every
request, notifications that never arrive — see the table in
[deployment.md](deployment.md#troubleshooting).
