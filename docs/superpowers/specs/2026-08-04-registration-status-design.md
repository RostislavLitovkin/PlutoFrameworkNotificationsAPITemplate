# Registration Status Endpoint — Design

Date: 2026-08-04
Status: Approved for implementation

## Context

The request was: "Is there a way to find out if the account/wallet has been registered for
the notifications? If not, create a way."

There is not. Every route in `ApiCore/urls.py` writes state or issues credentials; none
reads registration state back out:

| Route | Direction |
|---|---|
| `POST /api/nonce/` | issues a challenge |
| `POST /api/token/`, `/api/token/refresh/` | issues credentials |
| `POST /api/fcm/token-update/` | writes `registration_id` |
| `POST /api/user/uid-update/` | writes `uid` |
| `POST /api/user/wallet-link/`, `/wallet-unlink/` | writes `WalletLink` rows |
| `POST /api/fcm/send-notification/` | sends |

The only existing signal is indirect: `SendNotificationView` returns
`404 {"detail": "No registered devices found for this user."}` (`ApiApp/views.py:206`)
when no deliverable device matches the target. That is a side effect of actually sending,
so it cannot be used as a check without firing a real notification, and it is only
reachable with an API key.

This leaves a client with no way to answer "am I set up to receive notifications?" — which
matters most after a reinstall, an FCM token rotation, or a user toggling notifications and
wanting the UI to reflect real server state rather than local cache.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Caller | The device itself, via device JWT | The asked-for question is client-side UI state. A server-side (API-key) variant would let any key holder probe arbitrary addresses for registration — a privacy leak with no requested use case. |
| HTTP method | `GET` | Semantically a read. Costs one line of nuance in the "every route is POST" rule; no existing client breaks, since the route is new. |
| Query shape | Return full device state; no query-by-address mode | One round trip, client checks membership locally, and nothing to probe. |
| Scope | Read-only. No new models, no migration. | Everything needed is already on `AttestedFCMDevice` and `WalletLink`. |

## Endpoint

### `GET /api/user/registration/`

**Auth:** device JWT — `DeviceJWTAuthentication` + `IsRegisteredDevice`, matching the other
`/api/user/` routes. The device is resolved from the `device_id` JWT claim and never from
the request, so a device can only ever describe itself.

**200:**

```json
{
  "device_id": "abc-123",
  "platform": "android",
  "uid": "customer-42",
  "notifications_enabled": true,
  "wallets": [
    { "chain": "solana", "address": "9xQe...", "verified": true, "linked_at": "2026-08-04T10:12:33Z" }
  ]
}
```

| Field | Meaning |
|---|---|
| `device_id` | The value from the JWT claim; echoed so a client can detect a stale token. |
| `platform` | `android` or `ios` (the model's `type`). |
| `uid` | The generic user identifier, or `null` when never set. |
| `notifications_enabled` | `registration_id is not None`. |
| `wallets` | Every linked address; `[]` when none. |

`notifications_enabled` is the load-bearing field. It mirrors exactly the condition
`AttestedFCMDevice.deliverable()` (`ApiApp/models.py:41`) uses to pick send targets, so it
answers "would a notification actually reach me?" rather than the weaker "does a row
exist". A device that attested but never called `/api/fcm/token-update/` reports `false` —
the single most likely cause of a "notifications stopped working" report.

**Errors:**

| Status | Cause |
|---|---|
| `401` / `403` | Missing, malformed, or expired token; token without a `device_id` claim. |
| `404` | Valid token whose device row no longer exists — `{"detail": "Device not found."}`, matching `FCMTokenUpdateView`. |

### Deliberately excluded

- **The FCM token itself.** The client already holds it; echoing it back adds leak surface
  for no gain. `notifications_enabled` carries the only information a client needs.
- **The topic list.** Topic subscription is best-effort: FCM failures are logged and
  swallowed (`ApiApp/views.py:31`). The server cannot honestly report what a device is
  subscribed to, so it will not claim to.
- **A server-side (API-key) lookup by address.** Not requested, and it would let a key
  holder enumerate which addresses have registered devices.

## Implementation

- `RegistrationStatusView(views.APIView)` in `ApiApp/views.py`, `get()` only.
- Response built inline from the device, matching `WalletLinkView`'s hand-built dict. No
  output serializer for a single read path.
- `wallets` read via `device.wallets.all()` — the existing `related_name` on `WalletLink`.
- One URL entry: `path('api/user/registration/', ..., name='registration_status')`.

## Testing

`RegistrationStatusTests(DeviceApiTestCase)` in `ApiApp/tests/test_wallet_api.py`, reusing
the existing authentication and link helpers:

1. Reports the device's platform, uid, and linked wallets.
2. Reports `notifications_enabled: false` when the device has no FCM token.
3. Returns an empty wallet list for a device that has linked nothing.
4. Does not include another device's wallets.
5. Requires device authentication.
6. Returns 404 when the device row has been deleted.

## Documentation

- `docs/api-reference.md` — endpoint section, auth table row, and an amendment to the
  "Every route is `POST`" opener.
- `docs/client-integration.md` — a step covering checking registration state on launch.
- `README.md` — the endpoint table.
