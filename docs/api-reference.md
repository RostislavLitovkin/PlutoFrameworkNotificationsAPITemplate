# API reference

All endpoints accept and return JSON. Every route is `POST`, except
[`GET /api/user/registration/`](#get-apiuserregistration), which only reads.

## Trailing slashes are mandatory

The URLconf registers `api/nonce/`, not `api/nonce`. Django's `APPEND_SLASH` turns a
request for the unslashed path into a **301 redirect**, and HTTP clients follow a 301
by re-issuing the request as `GET` with no body. The call then fails as a confusing
405 or 404 rather than as a redirect. With `DEBUG=1` Django raises a `RuntimeError`
that says so explicitly; in production it just silently misbehaves.

Always call `/api/nonce/`, `/api/token/`, and so on.

## Authentication

| Scheme | Header | Used by |
|---|---|---|
| None | — | `/api/nonce/`, `/api/token/` |
| Device JWT | `Authorization: Bearer <access token>` | `/api/token/refresh/`¹, `/api/fcm/token-update/`, `/api/user/uid-update/`, `/api/user/wallet-link/`, `/api/user/wallet-unlink/`, `/api/user/registration/` |
| API key | `Authorization: Api-Key <key>` | `/api/fcm/send-notification/` |

¹ `/api/token/refresh/` takes the **refresh** token in the request body, not in a header.

The device JWT carries a `device_id` claim, set when the device passed attestation.
Every device-scoped endpoint reads the identity from that claim and never from the
request body, so one device cannot act on another's records. `IsRegisteredDevice`
rejects any token without the claim.

Missing or invalid credentials return **401**. A well-formed token whose device row no
longer exists returns **404** (`{"detail": "Device not found."}`).

---

## POST /api/nonce/

Issues a single-use challenge. Required before attestation and before wallet linking.

**Auth:** none. **Body:** none.

```http
POST /api/nonce/ HTTP/1.1
```

```json
{ "nonce": "A1PJjlqu8-KFVl36A5XlGAcbUEhOA2VITj30N8XPRmA" }
```

The value is 43 characters of unpadded URL-safe base64 (32 random bytes). It expires
after **120 seconds** and can be consumed once. The two flows consume it differently —
see [Nonce handling](#nonce-handling) below.

Each call also sweeps expired nonces, at most once every 300 seconds per process.

---

## POST /api/token/

Verifies app attestation and returns a JWT pair. Creates the device record on first
call and updates it afterwards.

**Auth:** none.

| Field | Type | Required | Notes |
|---|---|---|---|
| `nonce` | string | yes | From `/api/nonce/`. |
| `device_id` | string | yes | Stable per install. On iOS this must be the **base64 App Attest key ID**. |
| `platform` | string | yes | `android` or `ios`. |
| `attestation` | string | no | Android: the Play Integrity token. iOS: the base64 attestation object, on first registration. |
| `assertion` | string | no | iOS only, on later registrations once the device has an attested key on file. |

```json
{
  "access": "eyJhbGciOi...",
  "refresh": "eyJhbGciOi..."
}
```

Access tokens live **5 minutes**, refresh tokens **20 days**.

**Errors** — all `400`, with the reason in the body:

| Body | Cause |
|---|---|
| `["Nonce does not exist."]` | Unknown nonce. |
| `["Nonce is not valid."]` | Already consumed, or older than 120 seconds. |
| `["Handler init failed: ..."]` | Missing `attestation` for Android, or neither `attestation`+`device_id` nor `assertion`+stored key for iOS. |
| `["Attestation verification failed."]` | The token did not verify. |
| `["Verification error: ..."]` | Verification raised — malformed token, wrong keys, mismatched package name. |

Failures are deliberately vague to the caller; the server log carries the full
exception.

---

## POST /api/token/refresh/

Standard SimpleJWT refresh. The new access token inherits the `device_id` and `type`
claims, so the device does not re-attest until the refresh token itself expires.

**Auth:** none (the refresh token is the credential).

```json
{ "refresh": "eyJhbGciOi..." }
```

```json
{ "access": "eyJhbGciOi..." }
```

`401` if the refresh token is expired or invalid.

---

## POST /api/fcm/token-update/

Stores the device's FCM registration token and subscribes it to topics.

**Auth:** device JWT.

| Field | Type | Required |
|---|---|---|
| `fcm_token` | string (≤255) | yes |

```json
{ "message": "Token updated successfully." }
```

Subscribes the token to `global`, the platform (`android` / `ios`), and every chain
the device has already linked a wallet on. That last part is the catch-up path: a
wallet linked before the FCM token arrived gets its topic subscription here.

Call this whenever FCM rotates the token, or notifications stop reaching the device.

---

## POST /api/user/uid-update/

Sets the device's generic user identifier.

**Auth:** device JWT.

| Field | Type | Required |
|---|---|---|
| `user_id` | string (≤255) | yes |

```json
{ "message": "User identifier updated successfully." }
```

`uid` is **unverified and single-valued** — one per device, overwritten on each call,
and any authenticated device can claim any value. Use it for an identifier your own
backend already trusts. For wallet addresses, prefer `/api/user/wallet-link/`, which
proves ownership and holds many addresses at once.

---

## POST /api/user/wallet-link/

Links a wallet address to the calling device, proving ownership where the chain
supports it.

**Auth:** device JWT.

| Field | Type | Required | Notes |
|---|---|---|---|
| `nonce` | string | yes | Fresh, from `/api/nonce/`. |
| `chain` | string | yes | `solana` or `polkadot`. |
| `address` | string (≤255) | yes | Solana: base58 Ed25519 public key. |
| `signature` | string | for Solana | Base58, must decode to exactly 64 bytes. |

```json
{ "chain": "solana", "address": "9xQe...", "verified": true }
```

Linking the same address twice updates the existing row rather than duplicating it.
A device may hold many addresses across many chains.

On success the device is also subscribed to an FCM topic named after the chain.

**Errors** — `400` with a field-keyed body:

| Body | Cause |
|---|---|
| `{"nonce": ["Nonce does not exist."]}` | Unknown nonce. |
| `{"nonce": ["Nonce is not valid."]}` | Consumed or expired. |
| `{"address": ["Address is not valid base58."]}` | Solana address is not base58. |
| `{"address": ["Address must decode to 32 bytes, got N."]}` | Wrong length for an Ed25519 key. |
| `{"signature": ["A signature is required to link a solana address."]}` | Missing signature. |
| `{"signature": ["Signature verification failed."]}` | Bad signature, wrong message bytes, or a signature made for a different device. |
| `{"chain": ["\"...\" is not a valid choice."]}` | Unsupported chain. |

Address format and signature presence are checked **before** the nonce is consumed, so
a typo does not burn a nonce. Once those pass, the nonce is consumed before the
signature is verified — a failed attempt cannot be retried with the same nonce, which
is what stops an attacker grinding signatures against one challenge.

### Message to sign

The server rebuilds this itself and never accepts a client-supplied message, so it
must match **byte for byte**:

```
PlutoFramework wallet link
chain: <chain>
address: <address>
nonce: <nonce>
device: <device_id>
```

- UTF-8, `\n` (LF) separators, **no trailing newline**.
- `<nonce>` exactly as `/api/nonce/` returned it — do **not** base64-decode it first.
- `<device_id>` is taken from the JWT, so a signature made for one device is useless
  on another.

The first line is domain separation: the signature cannot be replayed as a
transaction or against another service.

### Chain support

| Chain | Ownership proof | Stored as |
|---|---|---|
| `solana` | Ed25519 signature, required | `verified: true` |
| `polkadot` | none yet | `verified: false` |

> [!WARNING]
> Polkadot links are recorded **without** verifying ownership, matching the behaviour
> of `uid`. Any device holding a valid JWT can claim any Polkadot address and receive
> notifications aimed at it. Closing this needs sr25519 verification and an SS58
> decoder — see `PolkadotVerifier` in `ApiApp/wallets.py`.

---

## POST /api/user/wallet-unlink/

Removes a linked address from the calling device.

**Auth:** device JWT.

| Field | Type | Required |
|---|---|---|
| `chain` | string | yes |
| `address` | string (≤255) | yes |

```json
{ "message": "Wallet unlinked successfully." }
```

Idempotent, and scoped to the calling device: unlinking an address that is not linked
succeeds and reveals nothing about what other devices hold. When the device has no
remaining addresses on that chain, it is unsubscribed from the chain topic.

---

## GET /api/user/registration/

Reports what the calling device is registered for. This is the endpoint that answers
"is this account or wallet set up to receive notifications?".

**Auth:** device JWT. **Body:** none — this is the one `GET` in the API.

```http
GET /api/user/registration/ HTTP/1.1
Authorization: Bearer <access token>
```

```json
{
  "device_id": "abc-123",
  "platform": "android",
  "uid": "customer-42",
  "notifications_enabled": true,
  "wallets": [
    {
      "chain": "solana",
      "address": "9xQe...",
      "verified": true,
      "linked_at": "2026-08-04T10:12:33.248146Z"
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `device_id` | string | From the JWT claim. Echoed so a client can spot a token issued for a device it no longer is. |
| `platform` | string | `android` or `ios`. |
| `uid` | string / null | The identifier set by `/api/user/uid-update/`; `null` if never set. |
| `notifications_enabled` | bool | Whether an FCM token is on file. |
| `wallets` | array | Every linked address; `[]` if none. |

### `notifications_enabled` is the field that matters

It is true when the device has reported an FCM token — exactly the condition
`/api/fcm/send-notification/` uses to pick targets. So it does not mean "a record
exists", it means **a notification sent right now would actually be attempted**.

A device that attested successfully but never completed `/api/fcm/token-update/`
reports `false` while still holding a perfectly valid JWT and, possibly, linked
wallets. That combination is the most common cause of "I registered but nothing
arrives" — check this field first.

### Checking one specific wallet

There is no query-by-address mode. Fetch the list and check it client-side:

```csharp
var status = await http.GetJsonAsync<RegistrationStatus>(
    "/api/user/registration/", bearer: accessToken);

bool willReceive = status.NotificationsEnabled
    && status.Wallets.Any(w => w.Chain == "solana" && w.Address == address);
```

One round trip covers every address, and a device can only ever read its own state —
there is no way to ask whether *someone else's* address is registered. If you need
that from your backend, the honest signal is the `404` from
`/api/fcm/send-notification/`.

**Errors:**

| Status | Cause |
|---|---|
| `401` / `403` | Missing, expired, or malformed token, or a token with no `device_id` claim. |
| `404` | Valid token whose device row no longer exists — re-register from `/api/nonce/`. |

---

## POST /api/fcm/send-notification/

Sends a notification to every device matching the target. This is the endpoint your
backend calls.

**Auth:** API key — `Authorization: Api-Key <key>`.

Exactly one targeting mode:

| Field | Type | Notes |
|---|---|---|
| `user_id` | string (≤255) | Targets devices whose `uid` matches. |
| `chain` + `address` | string + string | Targets devices that linked that address on that chain. |
| `title` | string (≤150) | Required. |
| `body` | string (≤500) | Required. |

```json
{ "chain": "solana", "address": "9xQe...", "title": "Transfer received", "body": "+2.5 SOL" }
```

```json
{ "user_id": "customer-42", "title": "Transfer received", "body": "+2.5 SOL" }
```

Response:

```json
{ "message": "Notification process completed.", "success_count": 2, "failure_count": 0 }
```

Only devices that have reported an FCM token are considered. Delivery is attempted
per device and a failure on one does not abort the rest — hence the two counters. A
`200` means the requests were made, not that anything was displayed.

**Errors:**

| Status | Cause |
|---|---|
| `400` | Both targeting modes given, neither given, or `chain`/`address` supplied alone. |
| `401` | Missing or invalid API key. |
| `404` | No device holds that `uid` or address, or none has an FCM token yet. |

Treat `404` as a normal outcome, not an incident: it just means nobody is listening.

---

## Nonce handling

One endpoint issues nonces, and the two flows that consume them treat the value
differently. Getting this wrong is the most common integration failure.

| Flow | What to do with the nonce string |
|---|---|
| Wallet linking | Use it **verbatim** in the message to sign. |
| Android attestation | Pad it to a multiple of 4 with `=`, then set it as the Play Integrity nonce. |
| iOS attestation | URL-safe-base64-decode it to 32 raw bytes and use those as the challenge. |

The padding rule for Android matters: the nonce is 43 characters, and Play Integrity
echoes back exactly the string you set. The server decodes both sides with padding
added, but Google's echo is decoded **without** it — an unpadded 43-character value
raises `binascii.Error: Incorrect padding`, which surfaces as
`"Attestation verification failed."` with no hint about why.

For iOS, `clientDataHash` is `SHA256(nonce_bytes)` for both `attestKey` and
`generateAssertion`.

## FCM topics

Devices are subscribed to `global`, their platform (`android` / `ios`), and one topic
per linked chain (`solana`, `polkadot`).

Nothing in this API sends *to* a topic — `/api/fcm/send-notification/` addresses
devices individually. Topics exist so you can broadcast from the Firebase console or
directly through the FCM API, for example to everyone holding a Solana wallet.

Subscription happens on a best-effort basis: FCM errors are logged at debug level and
swallowed, so linking never fails because Firebase was briefly unavailable. The
catch-up in `/api/fcm/token-update/` is what makes that safe.
