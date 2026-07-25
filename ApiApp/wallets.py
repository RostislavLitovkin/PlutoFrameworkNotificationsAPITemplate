"""Wallet address validation and ownership-proof verification.

Deliberately free of Django imports — no models, no settings, no DRF. That keeps
the crypto independently unit-testable without a database or a populated .env,
and gives per-chain support a single home.

Adding a chain means adding one ChainVerifier subclass and registering it in
VERIFIERS. Nothing else in the codebase needs to know the difference.
"""
from abc import ABC, abstractmethod

import base58
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ED25519_PUBLIC_KEY_LENGTH = 32
ED25519_SIGNATURE_LENGTH = 64


class InvalidAddress(ValueError):
    """Raised when an address is not well-formed for its chain."""


def build_link_message(*, chain: str, address: str, nonce: str, device_id: str) -> bytes:
    """
    Build the message a wallet must sign to prove it owns an address.

    The server always builds this itself — a client-supplied message is never
    accepted. The first line is domain separation, so the signature cannot be
    replayed as a transaction or against another service. Including device_id
    (taken from the JWT, never the request body) stops a captured signature
    being reused by a different device, and the nonce makes it single-use.

    The exact bytes are a client contract, documented in the README: UTF-8, LF
    separators, no trailing newline, and the nonce exactly as `/api/nonce`
    returned it (not base64-decoded).
    """
    return (
        "PlutoFramework wallet link\n"
        f"chain: {chain}\n"
        f"address: {address}\n"
        f"nonce: {nonce}\n"
        f"device: {device_id}"
    ).encode("utf-8")


class ChainVerifier(ABC):
    """
    Per-chain address rules and ownership proof.

    Note this exposes *validation*, not decoding: no caller needs the raw public
    key, so deriving one stays private to the chains that have one. A chain whose
    addresses are opaque can still implement the contract honestly.
    """

    chain: str
    requires_signature: bool

    @abstractmethod
    def validate_address(self, address: str) -> None:
        """Raise InvalidAddress if the address is not well-formed for this chain."""

    @abstractmethod
    def verify_signature(self, address: str, message: bytes, signature: str) -> bool:
        """
        Return True only if signature is a valid signature over message by address.

        Returns False rather than raising for malformed signatures, so callers
        have exactly one failure path to handle.
        """


class SolanaVerifier(ChainVerifier):
    """Solana addresses are base58-encoded Ed25519 public keys."""

    chain = "solana"
    requires_signature = True

    def decode_address(self, address: str) -> bytes:
        """Return the raw 32-byte Ed25519 public key behind a Solana address."""
        try:
            raw = base58.b58decode(address)
        except (ValueError, TypeError) as error:
            raise InvalidAddress("Address is not valid base58.") from error

        if len(raw) != ED25519_PUBLIC_KEY_LENGTH:
            raise InvalidAddress(
                f"Address must decode to {ED25519_PUBLIC_KEY_LENGTH} bytes, "
                f"got {len(raw)}."
            )

        return raw

    def validate_address(self, address: str) -> None:
        self.decode_address(address)

    def verify_signature(self, address: str, message: bytes, signature: str) -> bool:
        try:
            public_key = Ed25519PublicKey.from_public_bytes(self.decode_address(address))
        except InvalidAddress:
            return False

        try:
            raw_signature = base58.b58decode(signature)
        except (ValueError, TypeError):
            return False

        if len(raw_signature) != ED25519_SIGNATURE_LENGTH:
            return False

        try:
            public_key.verify(raw_signature, message)
        except InvalidSignature:
            return False

        return True


class PolkadotVerifier(ChainVerifier):
    """
    Polkadot addresses, currently stored without an ownership proof.

    Verifying these needs sr25519 (not available in `cryptography`) plus an SS58
    decoder, so for now addresses are accepted opaquely — exactly the behaviour of
    the existing `uid` field — and their links are recorded with verified=False.

    Whoever implements this: polkadot-js `signRaw` wraps payloads in
    `<Bytes>...</Bytes>` before signing, so verification must try both the wrapped
    and unwrapped forms of the message built by build_link_message().
    """

    chain = "polkadot"
    requires_signature = False

    def validate_address(self, address: str) -> None:
        # No SS58 decoding yet, so any non-blank string is accepted. Blank input is
        # already rejected by the serializer's CharField.
        return None

    def verify_signature(self, address: str, message: bytes, signature: str) -> bool:
        return False


VERIFIERS: dict[str, ChainVerifier] = {
    verifier.chain: verifier for verifier in (SolanaVerifier(), PolkadotVerifier())
}
