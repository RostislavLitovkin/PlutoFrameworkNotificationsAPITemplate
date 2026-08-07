# Client integration

How a mobile app talks to this API: attest, hold a JWT, report an FCM token, and link
wallet addresses. Examples are C#, matching the PlutoFramework client, but nothing here
is language-specific — the contract is the JSON in [api-reference.md](api-reference.md).

## The flow

```
first launch
  ├─ POST /api/nonce/            → nonce
  ├─ platform attestation        → attestation token
  ├─ POST /api/token/            → access + refresh
  └─ POST /api/fcm/token-update/ → device is now reachable

user connects a wallet
  ├─ POST /api/nonce/            → nonce
  ├─ wallet signs the link message
  └─ POST /api/user/wallet-link/ → address is now a main key of this device

every launch after that
  ├─ GET  /api/user/registration/ → confirm the device is still set up
  └─ POST /api/token/refresh/     when the access token expires (5 min)
```

## 1. Register the device

Attestation proves the request comes from a genuine, unmodified build of your app. It
is the only gate on this API — there is no user account, no password.

### Android

```csharp
// 1. Challenge
var nonce = (await http.PostAsync("/api/nonce/", null))
    .Deserialize<NonceResponse>().Nonce;

// 2. Play Integrity. The nonce must be PADDED to a multiple of 4 — Google echoes
//    the string back verbatim and the server decodes that echo as strict base64.
var paddedNonce = nonce.PadRight(nonce.Length + (4 - nonce.Length % 4) % 4, '=');
var integrityToken = await RequestIntegrityToken(paddedNonce);

// 3. Exchange
var tokens = await http.PostJsonAsync("/api/token/", new {
    nonce,                       // unpadded, exactly as received
    device_id = stableDeviceId,
    platform = "android",
    attestation = integrityToken,
});
```

Two details cause almost every Android failure:

- **Padding.** Skip it and verification fails with a generic
  `"Attestation verification failed."`; the server log shows
  `binascii.Error: Incorrect padding`.
- **Signing certificate.** `GOOGLE_PLAY_INTEGRITY_APP_SIGNING_KEY` must be the SHA-256
  of the certificate that signed the *installed* build. A debug build and a Play-signed
  build have different certificates, so a key that works locally rejects production.

`device_id` should be stable across launches but need not survive a reinstall. Use a
URL-safe value such as a GUID: the shared attestation handler runs it through a base64
decode even on Android, where the result is discarded.

### iOS

`device_id` is **not** free-form on iOS: it must be the base64 key ID that App Attest
generated, because the server decodes it and matches it against the attested public key.

```csharp
// First registration — attest a fresh key
var keyId = await DCAppAttestService.GenerateKeyAsync();       // base64 string
var nonceBytes = Base64UrlDecode(nonce);                        // 32 raw bytes
var attestation = await DCAppAttestService.AttestKeyAsync(
    keyId, clientDataHash: SHA256(nonceBytes));

await http.PostJsonAsync("/api/token/", new {
    nonce,
    device_id = keyId,
    platform = "ios",
    attestation = Convert.ToBase64String(attestation),
});
```

On later registrations the server already holds the public key for that `keyId`, so
send an **assertion** instead:

```csharp
var assertion = await DCAppAttestService.GenerateAssertionAsync(
    keyId, clientDataHash: SHA256(nonceBytes));

await http.PostJsonAsync("/api/token/", new {
    nonce, device_id = keyId, platform = "ios",
    assertion = Convert.ToBase64String(assertion),
});
```

`clientDataHash` is `SHA256(nonce_bytes)` in both cases — the raw 32 bytes, not the
nonce string.

Keep `keyId` in the keychain. Lose it and the device must attest a fresh key, which
creates a second device row.

## 2. Hold the tokens

| Token | Lifetime | Store in |
|---|---|---|
| access | 5 minutes | memory |
| refresh | 20 days | secure storage (Keychain / Keystore) |

```csharp
// On 401, refresh once and retry. If the refresh also fails, the refresh token has
// expired or was revoked — start again from attestation.
if (response.StatusCode == HttpStatusCode.Unauthorized)
{
    if (await TryRefreshAsync())
        response = await Retry(request);
    else
        await RegisterDeviceAsync();
}
```

Do not refresh on a timer. Refresh reactively on 401 — the access lifetime is short
and a background refresh loop wakes the app for nothing.

## 3. Report the FCM token

Nothing can be delivered until this call succeeds: a device with no registration token
is excluded from every send.

```csharp
await http.PostJsonAsync("/api/fcm/token-update/", new { fcm_token = token },
                         bearer: accessToken);
```

Call it after registration, and again whenever Firebase rotates the token
(`OnNewToken` on Android, `DidReceiveRegistrationToken` on iOS). It is safe to repeat.

## 4. Register a wallet

Registering a wallet makes its address a **main key** of this device: your backend
can then send to the bare address with `user_id`, exactly as it would send to a uid —
no chain qualifier needed. A device may register many addresses across many chains,
and each chain is recorded separately: registering the same device for a Polkadot and
a Solana wallet keeps both registrations side by side. Unlike `uid`, a Solana
registration is proof of ownership rather than a claim — and in practice Solana is
the chain that matters.

```csharp
var nonce = await GetNonceAsync();

// Byte-for-byte: UTF-8, LF separators, no trailing newline. The server rebuilds this
// itself and compares signatures against its own version, so any deviation fails.
var message = Encoding.UTF8.GetBytes(
    "PlutoFramework wallet link\n" +
    $"chain: {chain}\n" +
    $"address: {address}\n" +
    $"nonce: {nonce}\n" +
    $"device: {deviceId}");

var signature = Ed25519.Sign(message, privateKey);   // 64 bytes

await http.PostJsonAsync("/api/user/wallet-link/", new {
    nonce,
    chain = "solana",
    address,                                 // base58
    signature = Base58.Encode(signature),    // base58
}, bearer: accessToken);
```

Response: `{"chain": "solana", "address": "...", "verified": true}`.

Checklist when the server answers `Signature verification failed`:

- The nonce goes into the message **verbatim** — not base64-decoded, unlike the
  attestation flow.
- `device` must be the `device_id` this JWT was issued for. The server takes it from
  the token, so a signature built with a different value cannot match.
- No trailing newline.
- The signature is base58, not base64, and must decode to exactly 64 bytes.
- The nonce is already spent. Every failed attempt needs a fresh one.

Polkadot addresses can be registered with no signature, but are stored
`verified: false` — see the warning in [api-reference.md](api-reference.md#chain-support).

To remove one, `POST /api/user/wallet-unlink/` with `chain` and `address`. Removing
one chain's registration leaves the other chains' registrations untouched.

## 5. Check what the device is registered for

`GET /api/user/registration/` returns the server's view of this device: its `uid`, its
registered wallets, and whether an FCM token is on file.

```csharp
var status = await http.GetJsonAsync<RegistrationStatus>(
    "/api/user/registration/", bearer: accessToken);

if (!status.NotificationsEnabled)
    await ReportFcmTokenAsync();          // step 3 never completed

bool walletIsRegistered = status.Wallets
    .Any(w => w.Chain == "solana" && w.Address == address);
```

Use it to drive UI state rather than trusting local flags. Local state and server state
drift in ways the app cannot see: a reinstall creates a fresh `device_id` with no
wallets, an FCM token rotation silently breaks delivery until step 3 runs again, and a
wallet linked on one device is not linked on another. A toggle showing "notifications on"
because a local preference says so is exactly the bug this call prevents.

`notifications_enabled: false` is the answer to almost every "I registered but nothing
arrives" report — it means attestation succeeded but no FCM token ever reached the
server, so every send skips this device. Calling `/api/fcm/token-update/` fixes it, and
that call also catches up topic subscriptions for wallets linked in the meantime.

A `404` means the JWT is valid but the device row is gone. Start again from attestation.

## Trying it without an app

Attestation cannot be faked from a terminal, so a curl walkthrough stops at
`/api/token/`. To exercise the rest, mint a device and a token from the shell:

```bash
python manage.py shell
```

```python
from ApiApp.models import AttestedFCMDevice
from ApiApp.utils import generate_device_jwt

AttestedFCMDevice.objects.update_or_create(
    device_id="dev-device", defaults={"type": "android", "registration_id": "fake-fcm-token"}
)
print(generate_device_jwt("dev-device", "android")[0])   # access token
```

Then:

```bash
NONCE=$(curl -sX POST http://localhost:8000/api/nonce/ | python -c "import sys,json;print(json.load(sys.stdin)['nonce'])")

curl -X POST http://localhost:8000/api/user/wallet-link/ \
  -H "Authorization: Bearer $ACCESS" -H "Content-Type: application/json" \
  -d "{\"nonce\":\"$NONCE\",\"chain\":\"polkadot\",\"address\":\"5Grw...\"}"
```

Polkadot needs no signature, which makes it the easy path for smoke-testing the
plumbing. Do this on a development database only — the shortcut skips attestation
entirely.
