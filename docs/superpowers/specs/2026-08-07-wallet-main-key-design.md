# Wallet Addresses as Main Keys — Design

Date: 2026-08-07
Status: Implemented

## Context

The request was: "I do not like the way the Solana Wallet gets linked to the
registration. Make Solana as the main key too. If the user registers the same device
for Polkadot and Solana wallets, allow it and make them separately recorded. Most
likely, only the Solana Wallet will be used."

Before this change the API had two unequal identity namespaces:

1. **`AttestedFCMDevice.uid`** — the "main key". Single-valued, unverified, set through
   `POST /api/user/uid-update/`, and the only thing `user_id` targeting in
   `POST /api/fcm/send-notification/` would match. In practice this is where a
   Polkadot address ended up.
2. **`WalletLink` rows** — where Solana (and Polkadot) addresses landed via
   `POST /api/user/wallet-link/`. Second-class for sending: reachable only through the
   separate `{chain, address}` targeting mode, never through `user_id`.

So a Solana wallet was subordinate to the registration rather than an identity of it,
and a device could not hold a Polkadot and a Solana main key at once — a second
`uid-update` overwrites the first.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| What is a "main key" | Any registered wallet address, on any chain, plus the legacy `uid` | `user_id` targeting must reach a device by its Solana address alone — no chain qualifier, no uid ever set. |
| Storage | `WalletLink` rows, unchanged schema; no migration | The rows are already separately recorded per `(device, chain, address)`, which is exactly the "register both, record separately" requirement. The change is their *standing*, not their shape. |
| Same device on Polkadot + Solana | Allowed, two independent rows | Was already possible via wallet-link; now guaranteed by test and documented as the intended flow. Neither registration overwrites the other. |
| One device row per physical device | Kept | Attestation keys and the FCM token are per-device facts. Duplicating device rows per chain would fracture both. Identity multiplicity lives in the wallet rows instead. |
| `uid` / `uid-update` | Kept, demoted to legacy fallback | Backends with their own trusted identifier still need it. Wallet addresses no longer belong in it. |
| Ownership proof | Unchanged — Solana signed, Polkadot recorded unverified | The complaint was about standing, not about the proof ceremony. Registration keeps proving Solana ownership. |

## Implementation

One query change, no schema change:

- `AttestedFCMDevice.targets_by_uid(uid)` (matched `uid` only) became
  `targets_by_main_key(key)`:

  ```python
  cls.deliverable().filter(Q(uid=key) | Q(wallets__address=key)).distinct()
  ```

  `distinct()` is required, not decorative — a device can match through its uid and a
  wallet row at once, and must be sent to once.

- `SendNotificationView` routes `user_id` through `targets_by_main_key`. The wire
  contract is untouched: the same two targeting modes, same request and response
  shapes. `{chain, address}` remains available when the sender wants the match scoped
  to one chain.

## Security note

`user_id` targeting is now satisfiable by a wallet row *or* by the unverified legacy
`uid`, which any authenticated device can set to any string — including someone
else's Solana address. That interception risk is not new (uid always worked this
way); what is new is that senders will now legitimately use wallet addresses as
`user_id`. The documentation therefore keeps the standing warning: `user_id` is a
routing hint, never authorisation, and nothing secret belongs in a notification
body. `{chain, address}` targeting of a Solana address only ever matches
signature-verified registrations.

## Testing

Added to `ApiApp/tests/test_wallet_api.py`:

- A registered Solana address targets the device as a bare main key, uid never set.
- Same for a Polkadot address.
- One device registered for both chains holds two independent rows and is reachable
  through either address.
- A device whose uid equals its wallet address matches exactly once.
- Main-key targeting skips devices with no FCM token.
- End-to-end: `send-notification` with `user_id: <solana address>` delivers.
- Regression: legacy uid targeting unchanged.

## Documentation

README, `docs/api-reference.md`, `docs/client-integration.md`,
`docs/server-integration.md`, and `docs/development.md` reframed: wallet
registration — Solana first — is the primary identity flow; `uid` is the legacy
fallback for backend-issued identifiers.
