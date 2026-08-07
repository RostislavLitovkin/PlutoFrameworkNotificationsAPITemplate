# Notifications REST API Template for PlutoFramework

A Django REST API that delivers push notifications to mobile devices, where devices
authenticate by **app attestation** rather than by user accounts, and can register
**wallet addresses** — with proof of ownership — as notification targets.

- Android devices prove themselves with Play Integrity, iOS with App Attest. There is
  no signup, no password, no user table.
- A device exchanges a passed attestation for a JWT pair and reports its FCM token.
- A device registers wallet addresses as its **main keys** — each chain recorded
  separately, so one device can hold a Solana and a Polkadot registration at once.
  Solana registrations require an Ed25519 signature over a server-built message, so
  ownership is proven, not claimed.
- Your backend sends notifications with an API key, targeting a registered wallet
  address — with or without a chain qualifier — or a legacy generic identifier.

Built on Django 5.2, DRF, `fcm-django`, `djangorestframework-simplejwt`,
`djangorestframework-api-key`, and `pyattest`.

## Documentation

| | |
|---|---|
| [Configuration](docs/configuration.md) | Every environment variable, and the four without which nothing starts. |
| [API reference](docs/api-reference.md) | All endpoints, payloads, error codes, and the nonce rules. |
| [Client integration](docs/client-integration.md) | Attestation, tokens, FCM registration, wallet signing. |
| [Connecting an existing server](docs/server-integration.md) | Sending notifications, reverse proxies, shared database. |
| [Deployment](docs/deployment.md) | Docker, Compose, PaaS, production checklist, troubleshooting. |
| [Hetzner + GitHub Actions](docs/hetzner-deployment.md) | Server setup and automated deploys on every push to `main`. |
| [Development](docs/development.md) | Local setup, tests, code layout, adding a chain. |

## Quick start

```bash
cp .env.example .env          # fill it in — see docs/configuration.md
mkdir -p secrets              # drop the Firebase service account JSON here
docker compose up --build
```

The API is on `http://localhost:8000/`. Create an admin login with:

```bash
docker compose exec api python manage.py createsuperuser
```

Without Docker, see [development.md](docs/development.md#local-setup-without-docker).

## Endpoints

Trailing slashes are **required** — Django redirects the unslashed form, and clients
turn that redirect into a bodyless GET.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/nonce/` | none | Get a single-use challenge (120 s). |
| `POST /api/token/` | none | Exchange an attestation for a JWT pair. |
| `POST /api/token/refresh/` | refresh token | Renew the access token. |
| `POST /api/fcm/token-update/` | device JWT | Report the FCM registration token. |
| `POST /api/user/uid-update/` | device JWT | Set a legacy generic identifier. |
| `POST /api/user/wallet-link/` | device JWT | Register a wallet address as a main key. |
| `POST /api/user/wallet-unlink/` | device JWT | Remove a registered address. |
| `GET /api/user/registration/` | device JWT | Check what this device is registered for. |
| `POST /api/fcm/send-notification/` | API key | Send a notification. |
| `/admin/` | session | Django admin — devices, wallet links, API keys. |

## Wallet registration

A registered wallet address is a **main key** of the device's registration, not an
auxiliary link. A device may register several addresses across chains, and each
`(chain, address)` pair is its own record: registering the same device for a Polkadot
and a Solana wallet keeps both, separately, with neither overwriting the other. In
practice Solana is the primary chain; `uid` remains only as a legacy fallback for an
identifier your own backend already trusts.

| Chain | Ownership proof | Stored as |
|---|---|---|
| `solana` | Ed25519 signature, **required** | `verified: true` |
| `polkadot` | not yet implemented | `verified: false` |

> [!WARNING]
> Polkadot registrations are recorded **without** verifying ownership, matching the
> behaviour of `uid`. Any device holding a valid JWT can claim any Polkadot address.
> Closing this requires sr25519 verification and SS58 decoding — see
> `PolkadotVerifier` in `ApiApp/wallets.py`.

The flow: get a nonce, have the wallet sign the message below, post it with the device
JWT.

```
PlutoFramework wallet link
chain: <chain>
address: <address>
nonce: <nonce>
device: <device_id>
```

UTF-8, LF separators, no trailing newline, nonce used verbatim. The server rebuilds
this itself and never trusts a client-supplied message. Full contract and failure modes
in [api-reference.md](docs/api-reference.md#post-apiuserwallet-link).

Sending to a registered address — as a bare main key, or scoped to a chain:

```bash
curl -X POST https://<host>/api/fcm/send-notification/ \
  -H "Authorization: Api-Key <key>" -H "Content-Type: application/json" \
  -d '{"user_id":"<address>","title":"Hi","body":"..."}'

curl -X POST https://<host>/api/fcm/send-notification/ \
  -H "Authorization: Api-Key <key>" -H "Content-Type: application/json" \
  -d '{"chain":"solana","address":"<address>","title":"Hi","body":"..."}'
```

## Setup prerequisites

1. **PostgreSQL** — a database, user, and password.
2. **Firebase** — console → Settings → Service Accounts → *Generate New Private Key*.
   Store the JSON in `./secrets/`.
3. **Play Integrity** (Android) — enable the API in the Play Console, switch response
   encryption to **manual**, and take the decryption and verification keys. You also
   need the SHA-256 of your signing certificate.
4. **App Attest** (iOS) — the `<TEAM ID>.<bundle id>` pair.

Every resulting variable is listed in [`.env.example`](.env.example) and explained in
[configuration.md](docs/configuration.md).

## Tests

Runs on in-memory SQLite; no PostgreSQL and no `.env` required.

```bash
python manage.py test ApiApp --settings=ApiCore.settings_test
```
