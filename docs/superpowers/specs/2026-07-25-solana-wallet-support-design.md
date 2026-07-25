# Solana Wallet Support — Design

Date: 2026-07-25
Status: Approved for planning

## Context

The request was to "add Solana wallet support, not just Polkadot wallets". Investigation
found that **this repository contains no Polkadot-specific code**. The only wallet
touchpoint is `AttestedFCMDevice.uid` (`ApiApp/models.py:17`) — an opaque `TextField`
documented as *"you could use wallet address"*, set through `POST /api/user/uid-update/`
(`ApiApp/views.py:74`) with no chain awareness, no format validation, and no proof of
ownership. Authentication is device-level only (Play Integrity on Android, App Attest on
iOS, exchanged for a device JWT). `SendNotificationView` targets devices by filtering
`uid=user_id`.

A Solana base58 address therefore already fits into `uid` today. The real gaps blocking
multi-chain support are:

1. **One `uid` per device.** A device cannot hold a Polkadot *and* a Solana address; a
   second `uid-update` overwrites the first. A wallet with several accounts per chain is
   unrepresentable.
2. **No chain context.** A sender cannot say "notify the owner of this *Solana* address".
   FCM topics are only `['global', 'android'|'ios']`.
3. **No ownership proof.** Any device holding a valid JWT can set `uid` to any address and
   receive that user's notifications.

The sibling client repo (`P:\programming\PlutoFramework`) has no Solana support and no
calls to this API yet, so the API shape here is unconstrained by an existing client.

## Decisions

| Decision | Choice |
|---|---|
| Scope | Multi-address per device with chain tagging, plus signature-based ownership proof |
| Crypto dependencies | Solana verification only for now; Polkadot addresses accepted unverified |
| Existing `uid` field | Kept unchanged; the wallet table is purely additive |

### Accepted security gap

Polkadot links are stored **without** signature verification, matching the current `uid`
behaviour. The "any device can claim another user's address" hole therefore remains open
for Polkadot. This was chosen deliberately to avoid pulling in `py-sr25519-bindings` and
a hand-written SS58 decoder in this change. The design isolates verification behind one
interface so closing the gap later is one class plus one dependency, with no schema,
endpoint, or client-contract change.

## Architecture

Two concerns, deliberately separated:

- **`ApiApp/wallets.py`** (new) — all address decoding and signature verification. Contains
  **no Django imports**: no models, no settings, no DRF. This makes it unit-testable
  without a database or a populated `.env`, and gives chain support a single home.
- **`ApiApp/serializers.py`, `views.py`, `urls.py`, `models.py`, `admin.py`** — thin wiring
  following the existing `DeviceRegisterSerializer` / `FCMTokenUpdateView` patterns.

The existing attestation code in `ApiApp/utils.py` (267 lines) is not modified; wallet logic
goes in its own module rather than growing that file further.

### Verifier interface

```python
class InvalidAddress(ValueError): ...


class ChainVerifier(ABC):
    chain: str                    # "solana" | "polkadot"
    requires_signature: bool

    @abstractmethod
    def validate_address(self, address: str) -> None:
        """Raise InvalidAddress if the address is not well-formed for this chain."""

    @abstractmethod
    def verify_signature(self, address: str, message: bytes, signature: str) -> bool:
        """Return True only if signature is valid for address over message."""


VERIFIERS: dict[str, ChainVerifier] = {
    "solana": SolanaVerifier(),
    "polkadot": PolkadotVerifier(),
}
```

The interface exposes *validation*, not decoding: no caller needs the raw public key, so
deriving it stays private to the verifier that needs it. This also keeps the contract
honest for a chain whose addresses are not decoded at all.

- **`SolanaVerifier`** — `requires_signature = True`. `validate_address` base58-decodes and
  requires exactly 32 bytes. `verify_signature` decodes the address to a public key and the
  signature (base58, exactly 64 bytes), then calls
  `Ed25519PublicKey.from_public_bytes(pk).verify(sig, message)`, returning `False` on
  `InvalidSignature`.
- **`PolkadotVerifier`** — `requires_signature = False`. `validate_address` is a no-op,
  accepting any string exactly as `uid` does today (no SS58 decoding in this change).
  `verify_signature` returns `False`; it is unreachable while `requires_signature` is
  `False`, and gets a real implementation when sr25519 lands.

## Data model

New model in `ApiApp/models.py`, one migration:

```python
class WalletLink(models.Model):
    class Chain(models.TextChoices):
        POLKADOT = "polkadot", _("Polkadot")
        SOLANA = "solana", _("Solana")

    device = models.ForeignKey(
        AttestedFCMDevice, related_name="wallets", on_delete=models.CASCADE
    )
    chain = models.CharField(verbose_name=_("Chain"), max_length=32, choices=Chain.choices)
    address = models.TextField(verbose_name=_("Wallet address"))
    verified = models.BooleanField(verbose_name=_("Ownership verified"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "chain", "address"], name="uniq_device_chain_address"
            )
        ]
        indexes = [models.Index(fields=["chain", "address"])]
        verbose_name = "Wallet link"
        verbose_name_plural = "Wallet links"
```

`AttestedFCMDevice.uid` and `POST /api/user/uid-update/` are untouched. No data migration;
nothing existing breaks.

### Model rationale

- **Many addresses per chain per device.** A wallet holds several accounts and all should be
  reachable. Uniqueness is only `(device, chain, address)`.
- **Matching is an exact `(chain, address)` string comparison.** For Solana this is lossless:
  base58 is canonical, so one key maps to exactly one string. The decoded public key is
  therefore *not* stored — it is derivable from the address on demand, and storing it would
  be redundant. The accepted cost is that SS58 prefix-agnostic matching for Polkadot (the
  same key rendering differently on Polkadot vs Kusama vs the generic prefix) is
  unavailable; it arrives with the sr25519 work, which requires an SS58 decoder anyway.
- **`verified` is a stored column, not an implied property.** Solana links are written with
  `verified=True`, Polkadot links with `verified=False`. The gap is visible in the admin
  rather than tacit, and flips to `True` for Polkadot with no schema change.

## Signed message

The server constructs the message itself from the authenticated JWT and the nonce record. A
client-supplied message is never accepted.

```
PlutoFramework wallet link
chain: <chain>
address: <address>
nonce: <nonce>
device: <device_id from JWT>
```

Exact encoding, to remove client-side ambiguity:

- UTF-8, `\n` (LF) line separators, **no trailing newline**.
- `<chain>` is the lowercase chain id as stored, e.g. `solana`.
- `<address>` is the address string exactly as submitted in the request body.
- `<nonce>` is the nonce string **exactly as returned by `POST /api/nonce`** — it is not
  base64-decoded first. (This differs from the attestation path in `utils.py`, which does
  decode it.)
- `<device_id>` is taken from the validated JWT (`request.device_id`), never from the body.

Properties this buys:

- **Domain separation.** The literal first line means the signature cannot be replayed as a
  transaction or against another service.
- **Device binding.** Because `device_id` comes from the token, a captured signature cannot
  be replayed by a different device.
- **Replay protection.** The nonce is issued by the existing `POST /api/nonce` and passed
  through the same atomic single-use `Nonce.consume()` with its 120-second expiry, reusing
  the infrastructure `DeviceRegisterSerializer` already relies on.

Phantom and Solflare `signMessage` sign exactly the bytes handed to them, with no wrapper,
so byte-equality is achievable client-side. A code comment will record that polkadot-js
instead wraps payloads in `<Bytes>…</Bytes>`, since whoever adds sr25519 must handle both
the wrapped and unwrapped forms.

## Endpoints

### `POST /api/user/wallet-link/`

Auth: `IsRegisteredDevice` + `DeviceJWTAuthentication` (same as `UidUpdateView`).

Request: `{ "nonce": str, "chain": "solana"|"polkadot", "address": str, "signature": str? }`

`signature` is required when the chain's verifier has `requires_signature = True` (Solana),
and ignored otherwise (Polkadot).

Flow:

1. Serializer validates field presence and the `chain` choice.
2. Look up the `Nonce` and call `.consume()`; reject if missing, expired, or already used.
3. Resolve the verifier from `VERIFIERS[chain]`.
4. `validate_address`, rejecting a malformed address regardless of chain. Then, if
   `requires_signature`: rebuild the message and `verify_signature`, rejecting on failure,
   and set `verified=True`. Otherwise set `verified=False`.
5. `WalletLink.objects.update_or_create(device=..., chain=..., address=..., defaults={"verified": ...})`.
6. Subscribe the device to the chain FCM topic (see *Topic subscription* below).

Response `200`: `{ "chain": str, "address": str, "verified": bool }`

### `POST /api/user/wallet-unlink/`

Auth: same as above. Request: `{ "chain": str, "address": str }`

Deletes the caller's matching link, then unsubscribes the device from the chain topic if no
links of that chain remain. Included rather than deferred: without it, a link the user has
removed in the app keeps delivering that account's notifications.

Response `200`: `{ "message": "Wallet unlinked successfully." }`. Deleting a link that does
not exist is also `200` — it is idempotent and reveals nothing.

### `POST /api/fcm/send-notification/` (modified)

`NotificationPayloadSerializer` gains optional `address` and `chain` alongside the existing
`user_id`. Validation: **exactly one** targeting mode must be supplied.

- `user_id` alone → existing behaviour, unchanged.
- `address` + `chain` → `AttestedFCMDevice.targets_by_wallet(chain, address)`, which filters
  `wallets__chain` / `wallets__address` over devices that hold an FCM token.

  Correction to an earlier draft of this spec: `.distinct()` is *not* required here. The
  unique constraint on `(device, chain, address)` means a device can match at most once, so
  the join cannot duplicate rows. It is retained as cheap insurance in case targeting is
  later widened (for example to an entire chain), and the code says so rather than
  implying necessity.
- `address` without `chain`, `user_id` together with `address`, or neither supplied → `400`.

The existing `.exclude(registration_id__isnull=True)` filter and the per-device send loop
are retained.

### Topic subscription

Existing behaviour subscribes a device to `['global', device.type]` inside
`FCMTokenUpdateView` after setting `registration_id`. Chain topics are named after the chain
id (`solana`, `polkadot`).

Both orderings must work, since FCM subscription requires a registration token:

- **Link before token-update:** `wallet-link` skips subscription when
  `device.registration_id` is `None`. `FCMTokenUpdateView` therefore subscribes to
  `['global', device.type]` plus the device's distinct linked chains
  (`device.wallets.values_list("chain", flat=True).distinct()`), catching up any links made
  earlier. Distinct matters because several links can share one chain.
- **Token-update before link:** `wallet-link` subscribes directly.

Subscription failures are logged and swallowed, matching the existing loop in
`FCMTokenUpdateView`.

## Error handling

| Condition | Response |
|---|---|
| Nonce missing / expired / already consumed | `400` — matches existing attestation behaviour |
| Unknown `chain` | `400` via `ChoiceField` |
| `signature` absent for a chain requiring it | `400` |
| Address not valid base58, or not 32 bytes | `400` "Invalid Solana address." |
| Signature not valid base58, or not 64 bytes | `400` |
| Signature does not verify | `400` "Signature verification failed." |

Two rules beyond status codes:

- A link attempt must **never** reveal whether the address is already linked to a *different*
  device. That would expose the address-to-device graph to anyone holding a device JWT.
  Linking only ever reports on the caller's own device.
- Signatures are never written to logs. Logging otherwise follows the existing
  `logger.debug` style in this codebase.

## Admin

Register `WalletLink` with `list_display = (device, chain, address, verified, created_at)`,
`list_filter = ("chain", "verified")`, and `search_fields = ("address",)`, plus a
`TabularInline` on `AttestedFCMDeviceAdmin` so a device's links are visible in place.

## Testing

`ApiApp/tests.py` is currently the empty Django stub, so this is the first real suite. Split
by what each test needs to run:

**No database, no `.env` — `wallets.py` only:**

- Known-answer Solana vector: a fixed Ed25519 keypair, its base58 address, and a signature
  over the exact message string; verification passes.
- Negative cases: flipped signature byte, truncated public key, a signature valid for a
  *different* address, non-base58 input, and correct-base58-but-wrong-length input.
- Base58 alphabet pin: the well-known Solana System Program address
  `11111111111111111111111111111111` must decode to 32 zero bytes. This catches a wrong
  alphabet ordering (Bitcoin vs Ripple), which a self-generated vector would not.

**Requires database:**

- Nonce replay rejected on a second link attempt with the same nonce.
- Expired nonce rejected.
- One device links a Polkadot address and two Solana addresses; all three persist.
- Re-linking the same `(chain, address)` is idempotent: `update_or_create` refreshes
  `verified` without adding a second row. The unique constraint is the backstop for
  concurrent requests, not the mechanism.
- `send-notification` by `address` reaches only the linked device.
- Regression: `send-notification` by `user_id` still works unchanged.
- `wallet-link` without a device JWT is rejected.

### Known test-environment obstacle

`ApiCore/settings.py:39` calls `os.getenv("DJANGO_ALLOWED_HOSTS").split(",")`, which raises
`AttributeError` when the variable is unset, and `DATABASES` is hardwired to PostgreSQL. So
`python manage.py test` will not run on a clean checkout without a populated `.env`. The
`wallets.py` tests avoid this entirely by design (no Django import).

Resolved during implementation by adding **`ApiCore/settings_test.py`** (not in the original
design): it seeds the required environment variables before importing `ApiCore.settings`,
then swaps in in-memory SQLite. Two further obstacles surfaced only when running it:

- `PLAY_INTEGRITY_CONFIG` is built eagerly at settings import, and pyattest immediately
  base64-decodes the decryption key, `load_der_public_key`s the verification key, and hex-parses
  the app signing key. The test settings therefore supply throwaway values in each of those
  shapes. (That eager construction also means production fails at import with an incomplete
  environment — pre-existing, and left alone as out of scope.)
- WhiteNoise warns on every request until `collectstatic` runs, and Django logs every
  deliberate 4xx assertion as a warning. Both are silenced in test settings only, so that a
  genuine warning is visible.

The suite runs as:

    python manage.py test ApiApp --settings=ApiCore.settings_test

## Dependencies

Added to `requirements.txt`:

- `base58==2.1.1` — pure Python, no runtime dependencies, universal wheel.
- `cryptography` — **already imported directly** by `ApiApp/models.py` and `ApiApp/utils.py`
  while being present only transitively through `pyattest`. Declaring it fixes a latent
  break and supplies Ed25519; no new package is actually installed.

Verified as not needed for this change: `py-sr25519-bindings`, `substrate-interface`,
`PyNaCl`.

## Out of scope

- **Polkadot sr25519 verification** — needs `py-sr25519-bindings` (0.2.4 ships prebuilt
  manylinux, musllinux, and Windows wheels through cp314, so no Rust toolchain at deploy
  time) plus an SS58 decoder using stdlib `hashlib.blake2b`. Lands as
  `PolkadotVerifier.decode_address` / `.verify_signature` and flipping
  `requires_signature = True`.
- **SS58 prefix-agnostic matching** — storing the decoded public key and matching on it, so a
  sender can target any prefix form of a Polkadot account. Depends on the SS58 decoder above.
- **ECDSA Polkadot accounts** — rarer than sr25519/ed25519; deferred.
- **Client-side implementation** in `PlutoFramework`. This change is server-side only. The
  exact signed-message format is documented in the README so the client can match it
  byte-for-byte.
- **Listing a device's links** over the API. The client knows what it linked; the admin shows
  the rest.

## README updates

Add the two new endpoints to the URL list, document the exact signed-message format and the
base58 signature encoding for client implementers, and state plainly that Polkadot links are
recorded unverified pending sr25519 support.
