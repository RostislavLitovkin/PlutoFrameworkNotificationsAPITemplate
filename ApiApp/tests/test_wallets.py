"""Tests for ApiApp.wallets — address validation and signature verification.

These tests deliberately import nothing from Django, so they run without a
database, without settings, and without a populated .env:

    python -m unittest ApiApp.tests.test_wallets

The Solana vector below was generated with an independent base58
implementation (not the `base58` package the production code uses), so a
wrong alphabet or byte ordering fails these tests rather than cancelling out.
"""
import unittest

from ApiApp.wallets import (
    VERIFIERS,
    InvalidAddress,
    PolkadotVerifier,
    SolanaVerifier,
    build_link_message,
)

# --- Known-answer vector: Ed25519 seed = bytes(range(32)) ---
ADDRESS = "FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF"
PUBKEY_HEX = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
NONCE = "kZ8mQpTvR3nX7bLwY2cF9dHjA5sG1eNu"
DEVICE_ID = "test-device-0001"
SIGNATURE = (
    "xrZWH2bLTUt9iFrEMBvhXTyExxUBYg5Xjr3LN4cbSiPyzmp8Tfq1gzymm8mRizcvgNUW6T6nMQvQiRVxrKZ49R4"
)

# An unrelated keypair (seed = bytes(range(32, 64))).
OTHER_ADDRESS = "3ogUn1GNXoASaRbxPNeVJnVv5rG4EPBtmQmX61jVorUe"

# The real Solana System Program address: 32 zero bytes.
SYSTEM_PROGRAM_ADDRESS = "11111111111111111111111111111111"


class BuildLinkMessageTests(unittest.TestCase):
    def test_produces_exact_documented_bytes(self):
        """The signed message is a client contract; pin it byte-for-byte."""
        message = build_link_message(
            chain="solana", address=ADDRESS, nonce=NONCE, device_id=DEVICE_ID
        )

        self.assertEqual(
            message,
            (
                b"PlutoFramework wallet link\n"
                b"chain: solana\n"
                b"address: FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF\n"
                b"nonce: kZ8mQpTvR3nX7bLwY2cF9dHjA5sG1eNu\n"
                b"device: test-device-0001"
            ),
        )

    def test_has_no_trailing_newline(self):
        message = build_link_message(
            chain="solana", address=ADDRESS, nonce=NONCE, device_id=DEVICE_ID
        )

        self.assertFalse(message.endswith(b"\n"))


class SolanaAddressValidationTests(unittest.TestCase):
    def setUp(self):
        self.verifier = SolanaVerifier()

    def test_accepts_valid_address(self):
        self.verifier.validate_address(ADDRESS)  # must not raise

    def test_accepts_system_program_address(self):
        self.verifier.validate_address(SYSTEM_PROGRAM_ADDRESS)

    def test_system_program_address_decodes_to_32_zero_bytes(self):
        """Pins the base58 alphabet against a published real-world address."""
        self.assertEqual(
            self.verifier.decode_address(SYSTEM_PROGRAM_ADDRESS), bytes(32)
        )

    def test_decodes_vector_address_to_expected_public_key(self):
        self.assertEqual(
            self.verifier.decode_address(ADDRESS).hex(), PUBKEY_HEX
        )

    def test_rejects_characters_outside_base58_alphabet(self):
        # 0, O, I and l are excluded from the base58 alphabet.
        with self.assertRaises(InvalidAddress):
            self.verifier.validate_address("0OIl" + ADDRESS[4:])

    def test_rejects_key_shorter_than_32_bytes(self):
        # base58 of 31 bytes: decodes cleanly but is the wrong length.
        with self.assertRaises(InvalidAddress):
            self.verifier.validate_address("2g" * 12)

    def test_rejects_empty_address(self):
        with self.assertRaises(InvalidAddress):
            self.verifier.validate_address("")


class SolanaSignatureTests(unittest.TestCase):
    def setUp(self):
        self.verifier = SolanaVerifier()
        self.message = build_link_message(
            chain="solana", address=ADDRESS, nonce=NONCE, device_id=DEVICE_ID
        )

    def test_valid_signature_verifies(self):
        self.assertTrue(
            self.verifier.verify_signature(ADDRESS, self.message, SIGNATURE)
        )

    def test_requires_a_signature(self):
        self.assertTrue(self.verifier.requires_signature)

    def test_signature_for_a_different_address_is_rejected(self):
        """A real signature replayed against someone else's address must fail."""
        self.assertFalse(
            self.verifier.verify_signature(OTHER_ADDRESS, self.message, SIGNATURE)
        )

    def test_tampered_signature_is_rejected(self):
        tampered = ("y" if SIGNATURE[0] != "y" else "z") + SIGNATURE[1:]

        self.assertFalse(
            self.verifier.verify_signature(ADDRESS, self.message, tampered)
        )

    def test_signature_over_a_different_message_is_rejected(self):
        """Swapping the nonce must invalidate the signature (replay defence)."""
        other_message = build_link_message(
            chain="solana", address=ADDRESS, nonce="a-different-nonce", device_id=DEVICE_ID
        )

        self.assertFalse(
            self.verifier.verify_signature(ADDRESS, other_message, SIGNATURE)
        )

    def test_signature_bound_to_device_id(self):
        """A signature captured for one device must not work for another."""
        other_message = build_link_message(
            chain="solana", address=ADDRESS, nonce=NONCE, device_id="attacker-device"
        )

        self.assertFalse(
            self.verifier.verify_signature(ADDRESS, other_message, SIGNATURE)
        )

    def test_wrong_length_signature_is_rejected_without_raising(self):
        self.assertFalse(
            self.verifier.verify_signature(ADDRESS, self.message, SIGNATURE[:-2])
        )

    def test_non_base58_signature_is_rejected_without_raising(self):
        self.assertFalse(
            self.verifier.verify_signature(ADDRESS, self.message, "not base58 0OIl!")
        )

    def test_empty_signature_is_rejected_without_raising(self):
        self.assertFalse(self.verifier.verify_signature(ADDRESS, self.message, ""))


class PolkadotVerifierTests(unittest.TestCase):
    def setUp(self):
        self.verifier = PolkadotVerifier()

    def test_does_not_require_a_signature_yet(self):
        """sr25519 is not implemented; links are recorded unverified."""
        self.assertFalse(self.verifier.requires_signature)

    def test_accepts_any_address_string(self):
        # Matches today's opaque `uid` behaviour until SS58 decoding lands.
        self.verifier.validate_address("15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5")

    def test_never_reports_a_signature_as_valid(self):
        self.assertFalse(
            self.verifier.verify_signature("15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5",
                                           b"any message", SIGNATURE)
        )


class VerifierRegistryTests(unittest.TestCase):
    def test_exposes_both_chains(self):
        self.assertEqual(set(VERIFIERS), {"solana", "polkadot"})

    def test_each_verifier_reports_its_own_chain(self):
        for chain, verifier in VERIFIERS.items():
            self.assertEqual(verifier.chain, chain)


if __name__ == "__main__":
    unittest.main()
