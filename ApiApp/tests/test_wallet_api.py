"""Tests for wallet linking, unlinking, and notification targeting.

These need a database:

    python manage.py test ApiApp --settings=ApiCore.settings_test

Signatures here are produced at test time because each test draws a fresh random
nonce. The base58 encoding itself is pinned independently in test_wallets.py by a
vector built with a hand-written encoder, so signing with the same library here
does not hide an encoding error.
"""
from datetime import datetime
from unittest.mock import patch

import base58
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework_api_key.models import APIKey

from ApiApp.models import AttestedFCMDevice, Nonce, WalletLink
from ApiApp.utils import generate_device_jwt
from ApiApp.wallets import build_link_message

SOLANA_ADDRESS = "FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF"
SECOND_SOLANA_ADDRESS = "3ogUn1GNXoASaRbxPNeVJnVv5rG4EPBtmQmX61jVorUe"
POLKADOT_ADDRESS = "15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5"

_signing_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_other_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))


def sign(message: bytes, key: Ed25519PrivateKey = _signing_key) -> str:
    return base58.b58encode(key.sign(message)).decode()


class DeviceApiTestCase(APITestCase):
    """A registered, authenticated Android device."""

    def setUp(self):
        self.device = AttestedFCMDevice.objects.create(
            device_id="device-a", type="android", registration_id="fcm-token-a"
        )
        self.authenticate(self.device)

    def authenticate(self, device):
        access, _ = generate_device_jwt(device.device_id, device.type)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def link_payload(self, chain, address, device_id=None, nonce=None, sign_with=_signing_key):
        """Build a wallet-link body, signing when the chain requires it."""
        nonce = nonce if nonce is not None else Nonce.objects.create_nonce()
        payload = {"nonce": nonce, "chain": chain, "address": address}

        if chain == "solana":
            payload["signature"] = sign(
                build_link_message(
                    chain=chain,
                    address=address,
                    nonce=nonce,
                    device_id=device_id or self.device.device_id,
                ),
                key=sign_with,
            )

        return payload

    def link(self, chain, address, **kwargs):
        return self.client.post(
            reverse("wallet_link"), self.link_payload(chain, address, **kwargs), format="json"
        )


class WalletLinkTests(DeviceApiTestCase):
    def test_links_solana_wallet_with_valid_signature(self):
        response = self.link("solana", SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["verified"], True)
        link = WalletLink.objects.get()
        self.assertEqual(link.device, self.device)
        self.assertEqual(link.chain, "solana")
        self.assertEqual(link.address, SOLANA_ADDRESS)
        self.assertTrue(link.verified)

    def test_rejects_signature_from_a_different_key(self):
        response = self.link("solana", SOLANA_ADDRESS, sign_with=_other_key)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletLink.objects.exists())

    def test_rejects_missing_signature_for_solana(self):
        payload = self.link_payload("solana", SOLANA_ADDRESS)
        del payload["signature"]

        response = self.client.post(reverse("wallet_link"), payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletLink.objects.exists())

    def test_rejects_signature_issued_for_another_device(self):
        """A signature captured from another device must not be replayable."""
        response = self.link("solana", SOLANA_ADDRESS, device_id="someone-elses-device")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletLink.objects.exists())

    def test_rejects_malformed_solana_address(self):
        response = self.link("solana", "0OIl-not-base58")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletLink.objects.exists())

    def test_rejects_unknown_chain(self):
        response = self.client.post(
            reverse("wallet_link"),
            {"nonce": Nonce.objects.create_nonce(), "chain": "bitcoin", "address": "x"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_links_polkadot_wallet_unverified_without_a_signature(self):
        """sr25519 is not implemented, so the link is recorded but unproven."""
        response = self.link("polkadot", POLKADOT_ADDRESS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["verified"], False)
        self.assertFalse(WalletLink.objects.get().verified)

    def test_rejects_a_reused_nonce(self):
        payload = self.link_payload("solana", SOLANA_ADDRESS)
        first = self.client.post(reverse("wallet_link"), payload, format="json")

        replay = self.client.post(reverse("wallet_link"), payload, format="json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 400)

    def test_rejects_an_unknown_nonce(self):
        response = self.link("solana", SOLANA_ADDRESS, nonce="never-issued-by-the-server")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletLink.objects.exists())

    def test_requires_device_authentication(self):
        self.client.credentials()  # drop the JWT

        response = self.client.post(
            reverse("wallet_link"),
            {"nonce": "n", "chain": "solana", "address": SOLANA_ADDRESS, "signature": "s"},
            format="json",
        )

        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(WalletLink.objects.exists())

    def test_one_device_can_link_several_addresses_across_chains(self):
        self.link("solana", SOLANA_ADDRESS)
        self.link("solana", SECOND_SOLANA_ADDRESS, sign_with=_other_key)
        self.link("polkadot", POLKADOT_ADDRESS)

        self.assertEqual(self.device.wallets.count(), 3)

    def test_relinking_the_same_address_does_not_duplicate_it(self):
        self.link("solana", SOLANA_ADDRESS)
        response = self.link("solana", SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(WalletLink.objects.count(), 1)


class WalletUnlinkTests(DeviceApiTestCase):
    def unlink(self, chain, address):
        return self.client.post(
            reverse("wallet_unlink"), {"chain": chain, "address": address}, format="json"
        )

    def test_unlinks_an_existing_wallet(self):
        self.link("solana", SOLANA_ADDRESS)

        response = self.unlink("solana", SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(WalletLink.objects.exists())

    def test_unlinking_an_unknown_address_is_idempotent(self):
        response = self.unlink("solana", SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 200)

    def test_cannot_unlink_another_devices_wallet(self):
        self.link("solana", SOLANA_ADDRESS)
        other = AttestedFCMDevice.objects.create(
            device_id="device-b", type="ios", registration_id="fcm-token-b"
        )
        self.authenticate(other)

        response = self.unlink("solana", SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WalletLink.objects.filter(device=self.device).exists())


class RegistrationStatusTests(DeviceApiTestCase):
    """What a device can learn about its own registration."""

    def status(self):
        return self.client.get(reverse("registration_status"))

    def test_reports_platform_uid_and_linked_wallets(self):
        self.device.uid = "customer-42"
        self.device.save(update_fields=["uid"])
        self.link("solana", SOLANA_ADDRESS)

        response = self.status()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["device_id"], "device-a")
        self.assertEqual(response.data["platform"], "android")
        self.assertEqual(response.data["uid"], "customer-42")
        self.assertEqual(
            response.data["wallets"],
            [
                {
                    "chain": "solana",
                    "address": SOLANA_ADDRESS,
                    "verified": True,
                    "linked_at": WalletLink.objects.get().created_at,
                }
            ],
        )

    def test_renders_linked_at_as_an_iso_8601_string(self):
        """The wire format api-reference.md promises, not just the Python object."""
        self.link("solana", SOLANA_ADDRESS)

        linked_at = self.status().json()["wallets"][0]["linked_at"]

        self.assertEqual(
            datetime.fromisoformat(linked_at), WalletLink.objects.get().created_at
        )

    def test_reports_notifications_enabled_when_an_fcm_token_is_on_file(self):
        response = self.status()

        self.assertEqual(response.data["notifications_enabled"], True)

    def test_reports_notifications_disabled_without_an_fcm_token(self):
        """The device attested but never called token-update, so nothing can reach it."""
        self.device.registration_id = None
        self.device.save(update_fields=["registration_id"])

        response = self.status()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["notifications_enabled"], False)

    def test_reports_an_empty_wallet_list_and_null_uid_for_a_fresh_device(self):
        response = self.status()

        self.assertEqual(response.data["wallets"], [])
        self.assertIsNone(response.data["uid"])

    def test_does_not_report_another_devices_wallets(self):
        self.link("solana", SOLANA_ADDRESS)
        other = AttestedFCMDevice.objects.create(
            device_id="device-b", type="ios", registration_id="fcm-token-b"
        )
        self.authenticate(other)

        response = self.status()

        self.assertEqual(response.data["device_id"], "device-b")
        self.assertEqual(response.data["wallets"], [])

    def test_requires_device_authentication(self):
        self.client.credentials()  # drop the JWT

        response = self.status()

        self.assertIn(response.status_code, (401, 403))

    def test_reports_404_when_the_device_row_is_gone(self):
        """A valid token outliving its device — reinstall plus a purge."""
        self.device.delete()

        response = self.status()

        self.assertEqual(response.status_code, 404)


class NotificationTargetingTests(APITestCase):
    """Device selection, exercised directly against the database."""

    def setUp(self):
        self.device = AttestedFCMDevice.objects.create(
            device_id="device-a", type="android", registration_id="fcm-token-a", uid="legacy-uid"
        )
        self.other = AttestedFCMDevice.objects.create(
            device_id="device-b", type="ios", registration_id="fcm-token-b"
        )
        WalletLink.objects.create(
            device=self.device, chain="solana", address=SOLANA_ADDRESS, verified=True
        )

    def test_targets_the_device_holding_the_address(self):
        targets = AttestedFCMDevice.targets_by_wallet("solana", SOLANA_ADDRESS)

        self.assertEqual(list(targets), [self.device])

    def test_wallet_targeting_is_scoped_to_the_chain(self):
        """The same string on another chain must not match."""
        targets = AttestedFCMDevice.targets_by_wallet("polkadot", SOLANA_ADDRESS)

        self.assertEqual(list(targets), [])

    def test_targets_by_legacy_uid(self):
        targets = AttestedFCMDevice.targets_by_uid("legacy-uid")

        self.assertEqual(list(targets), [self.device])

    def test_skips_devices_that_have_no_fcm_token(self):
        tokenless = AttestedFCMDevice.objects.create(device_id="device-c", type="android")
        WalletLink.objects.create(
            device=tokenless, chain="solana", address=SECOND_SOLANA_ADDRESS, verified=True
        )

        targets = AttestedFCMDevice.targets_by_wallet("solana", SECOND_SOLANA_ADDRESS)

        self.assertEqual(list(targets), [])


class SendNotificationTests(APITestCase):
    def setUp(self):
        self.device = AttestedFCMDevice.objects.create(
            device_id="device-a", type="android", registration_id="fcm-token-a", uid="legacy-uid"
        )
        AttestedFCMDevice.objects.create(
            device_id="device-b", type="ios", registration_id="fcm-token-b"
        )
        WalletLink.objects.create(
            device=self.device, chain="solana", address=SOLANA_ADDRESS, verified=True
        )
        _, key = APIKey.objects.create_key(name="test-sender")
        self.client.credentials(HTTP_AUTHORIZATION=f"Api-Key {key}")

    def send(self, **payload):
        payload.setdefault("title", "Hello")
        payload.setdefault("body", "You have an update")
        return self.client.post(
            reverse("fcm_send_notification"), payload, format="json"
        )

    def test_sends_only_to_the_device_holding_the_address(self):
        with patch.object(AttestedFCMDevice, "send_message") as send_message:
            response = self.send(chain="solana", address=SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["success_count"], 1)
        self.assertEqual(send_message.call_count, 1)

    def test_uid_targeting_still_works(self):
        """Regression: existing senders must keep working unchanged."""
        with patch.object(AttestedFCMDevice, "send_message") as send_message:
            response = self.send(user_id="legacy-uid")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["success_count"], 1)
        self.assertEqual(send_message.call_count, 1)

    def test_rejects_both_targeting_modes_at_once(self):
        response = self.send(user_id="legacy-uid", chain="solana", address=SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 400)

    def test_rejects_an_address_without_a_chain(self):
        response = self.send(address=SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 400)

    def test_rejects_a_request_with_no_target(self):
        response = self.send()

        self.assertEqual(response.status_code, 400)

    def test_reports_404_when_no_device_holds_the_address(self):
        response = self.send(chain="solana", address=SECOND_SOLANA_ADDRESS)

        self.assertEqual(response.status_code, 404)

    def test_requires_an_api_key(self):
        self.client.credentials()

        response = self.send(chain="solana", address=SOLANA_ADDRESS)

        self.assertIn(response.status_code, (401, 403))
