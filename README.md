# Notifications REST API Template for PlutoFramework

## URLs
```
GET /admin - All admin tools needed (including login)
POST /api/nonce - Generate the nonce (needed for app integrity attestation)
POST /api/token - Register device to get JWT pair
POST /api/token/refresh - Refresh access token using refresh token
POST /api/fcm/token-update - Update the FCM token
POST /api/fcm/send-notification - Send a notification (API key required)
POST /api/user/uid-update - Update the generic user identifier
POST /api/user/wallet-link - Link a wallet address to the device
POST /api/user/wallet-unlink - Remove a linked wallet address
```

## Wallet linking

A device can link several wallet addresses, across chains, and receive notifications
for each. This is separate from `uid`, which stays available as a generic,
unverified identifier.

### Supported chains

| Chain | Ownership proof | Notes |
|---|---|---|
| `solana` | **Required** — Ed25519 signature | Address is a base58 Ed25519 public key |
| `polkadot` | Not yet implemented | Link is stored with `verified: false` |

> [!WARNING]
> Polkadot links are recorded **without** verifying ownership, matching the
> behaviour of `uid`. Any device holding a valid JWT can claim any Polkadot
> address. Closing this requires sr25519 verification and SS58 decoding — see
> `PolkadotVerifier` in `ApiApp/wallets.py`.

### Linking flow

1. `POST /api/nonce` → `{"nonce": "..."}`
2. Have the wallet sign the message below.
3. `POST /api/user/wallet-link` with the device JWT:

```json
{
  "nonce": "<nonce from step 1>",
  "chain": "solana",
  "address": "<base58 address>",
  "signature": "<base58 signature>"
}
```

Response: `{"chain": "solana", "address": "...", "verified": true}`

### Message to sign

The server rebuilds this itself and never trusts a client-supplied message, so it
must match **byte for byte**:

```
PlutoFramework wallet link
chain: <chain>
address: <address>
nonce: <nonce>
device: <device_id>
```

- UTF-8, `\n` (LF) separators, **no trailing newline**.
- `<nonce>` is used exactly as `/api/nonce` returned it — do **not** base64-decode it
  first (unlike the attestation flow).
- `<device_id>` must equal the `device_id` the JWT was issued for; the server takes
  it from the token, so a signature made for one device will not work on another.
- The `signature` is base58-encoded and must decode to exactly 64 bytes.

Each nonce is single-use and expires after 120 seconds.

### Sending to a wallet address

`POST /api/fcm/send-notification` accepts either targeting mode, but not both:

```json
{"chain": "solana", "address": "<address>", "title": "Hi", "body": "..."}
{"user_id": "<uid>", "title": "Hi", "body": "..."}
```

Linked devices are also subscribed to an FCM topic named after the chain
(`solana`, `polkadot`) alongside the existing `global` and platform topics.
## Setup
### 1. Database
Set up a database on a remote server and get access credentials, then put them in .env file.

### 2. Firebase
In the Firebase console, open Settings > Service Accounts.
Click Generate New Private Key, then confirm by clicking Generate Key.
Securely store that JSON file.

### 3. Google Integrity API
Set up the project (including Play Console and Google Cloud).
Turn on Integrity API in Console settings.
Then change response encryption to manual and get the Decryption and Verification keys.

### .env example:
```dotenv
# App setup
DEBUG=0
DJANGO_ALLOWED_HOSTS=".onrender.com,0.0.0.0"
SECRET_KEY="***"
FIREBASE_CREDENTIALS_JSON='{
  "type": "service_account",
  "project_id": "...",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----...-----END PRIVATE KEY-----\n",
  "client_email": "...",
  "client_id": "...",
  "auth_uri": "https://accounts.google.com...",
  "token_uri": "https://oauth2.googleapis.com...",
  "auth_provider_x509_cert_url": "https://www.googleapis.com...",
  "client_x509_cert_url": "https://www.googleapis.com...",
  "universe_domain": "googleapis.com"
}'

# App attestation
APK_NAME="com.companyname.appname"
ATTESTATION_DECRYPTION_KEY="***"
ATTESTATION_VERIFICATION_KEY="***"
ATTESTATION_APP_SIGNING_KEY="XX:XX:XX:XX:XX:XX..."

# Database setup
PG_URL="postgresql://..."
PG_DATABASE="db_name"
PG_HOST="some.url.com"
PG_PASSWORD="***"
PG_PORT="5432"
PG_USER="admin"
```

### Install libraries
```shell
pip install -r requirements.txt
```
### Prepare the database
```shell
python manage.py migrate
```

### Add a superuser to access API as admin (optional)
```shell
python manage.py createsuperuser
```

## Tests
The suite runs against in-memory SQLite with throwaway attestation config, so it needs
neither PostgreSQL nor a `.env`:
```shell
python manage.py test ApiApp --settings=ApiCore.settings_test
```
The address and signature tests in `ApiApp/tests/test_wallets.py` import no Django at
all, so they can also run standalone:
```shell
python -m unittest ApiApp.tests.test_wallets
```

## Run
#### Development:
```shell
python manage.py runserver
```
> [!NOTE]  
> If DEBUG is off, you will have to collect static like in production

#### Production:
```shell
python manage.py collectstatic
gunicorn ApiCore.wsgi:application --bind 0.0.0.0:$PORT
```

> [!NOTE]  
> `$PORT` environment variable should be supplied by hosting platform