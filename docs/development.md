# Development

## Local setup without Docker

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env            # fill it in — see docs/configuration.md
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

You need PostgreSQL running and reachable at `PG_HOST`. The quickest way to get one
without installing anything is the compose stack's database on its own:

```bash
docker compose up -d db
```

Then set `PG_HOST=localhost` and `PG_PORT=5432` in `.env`.

`manage.py` will not start without the four variables listed under
[startup requirements](configuration.md#startup-requirements) — including the Play
Integrity keys, even if you never test Android.

With `DEBUG=1`, static files are served without `collectstatic`. With `DEBUG=0` locally
you must run `python manage.py collectstatic` or the admin renders unstyled.

## Tests

The suite runs against in-memory SQLite with throwaway attestation config, so it needs
neither PostgreSQL nor a `.env`:

```bash
python manage.py test ApiApp --settings=ApiCore.settings_test
```

`ApiCore/settings_test.py` seeds the environment before importing the real settings,
because `ApiCore.settings` builds `PLAY_INTEGRITY_CONFIG` eagerly at import and would
otherwise raise. Values are throwaway and correctly *shaped* — base64 for the
decryption key, a base64 DER public key for verification, hex for the signing key.

The address and signature tests in `ApiApp/tests/test_wallets.py` import no Django at
all, so they also run standalone:

```bash
python -m unittest ApiApp.tests.test_wallets
```

That is deliberate: `ApiApp/wallets.py` has no Django imports, so the crypto is
testable without a database or a populated environment.

Inside the container:

```bash
docker compose exec api python manage.py test ApiApp --settings=ApiCore.settings_test
```

## Layout

| Path | Holds |
|---|---|
| `ApiCore/settings.py` | All configuration. Reads the environment at import. |
| `ApiCore/settings_test.py` | Test overrides: SQLite, seeded attestation config. |
| `ApiCore/urls.py` | Routes. Every endpoint has a trailing slash. |
| `ApiApp/views.py` | One view per endpoint, plus the FCM topic helpers. |
| `ApiApp/serializers.py` | Request validation and the wallet-link verification flow. |
| `ApiApp/models.py` | `AttestedFCMDevice`, `WalletLink`, `Nonce`. |
| `ApiApp/wallets.py` | Address rules and signature verification. **No Django imports.** |
| `ApiApp/utils.py` | Play Integrity / App Attest verification, JWT minting. |
| `ApiApp/auth.py` | `DeviceJWTAuthentication` — authenticates a device, not a user. |
| `ApiApp/permissions.py` | `IsRegisteredDevice`. |
| `ApiApp/managers.py` | Nonce creation and expiry sweep. |
| `docker/` | Entrypoint and health probe. |

### How authentication differs from stock DRF

`DeviceJWTAuthentication` returns `(None, validated_token)` — there is no `User`. The
device identity lives in the token's `device_id` claim, which views read as
`request.device_id`. `IsRegisteredDevice` checks that claim is present.

This means `request.user` is always anonymous on device endpoints. Anything relying on
`request.user` will silently do nothing.

## Adding a chain

`ApiApp/wallets.py` is the only file that needs to know how a chain works:

```python
class MyChainVerifier(ChainVerifier):
    chain = "mychain"
    requires_signature = True

    def validate_address(self, address: str) -> None:
        # raise InvalidAddress if malformed
        ...

    def verify_signature(self, address: str, message: bytes, signature: str) -> bool:
        # return False rather than raising, so callers have one failure path
        ...

VERIFIERS = {v.chain: v for v in (SolanaVerifier(), PolkadotVerifier(), MyChainVerifier())}
```

Then add the value to `WalletLink.Chain` and generate the migration:

```bash
python manage.py makemigrations ApiApp
```

`choices` changes produce a migration but no schema change. Nothing else needs
touching: serializers derive their choices from the model, and the link message format
already carries the chain name.

Add tests to `ApiApp/tests/test_wallets.py` — they need no database.

### Finishing Polkadot

`PolkadotVerifier` accepts addresses without proof and records them `verified: false`.
Closing that gap means:

1. An sr25519 implementation (`py-sr25519-bindings`) and an SS58 decoder — neither is
   in `cryptography`, which is why it was deferred.
2. `verify_signature` must try **both** the plain message and the
   `<Bytes>...</Bytes>`-wrapped form, because polkadot-js `signRaw` wraps payloads
   before signing.
3. Flip `requires_signature` to `True`.

No schema change, no endpoint change, no client contract change. Existing rows keep
`verified: false`, which is accurate.

## Migrations

```bash
python manage.py makemigrations ApiApp
python manage.py migrate
```

Existing migrations: `0001_initial`, `0002` (nonce model), `0003` (device `uid`),
`0004` (`WalletLink`).

## Design notes

`docs/superpowers/specs/2026-07-25-solana-wallet-support-design.md` records why wallet
linking is shaped the way it is, including the deliberately accepted Polkadot gap.
