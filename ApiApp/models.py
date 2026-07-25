from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from django.db import models
from django.utils import timezone
from fcm_django.models import AbstractFCMDevice
from django.utils.translation import gettext_lazy as _

from ApiApp.managers import NonceManager
from ApiCore.settings import ATTESTATION_NONCE_EXPIRY_SECONDS


class AttestedFCMDevice(AbstractFCMDevice):
    # What to identify the user with to then send notifications without using Firebase identifiers
    # (For example, you could use wallet address)
    uid = models.TextField(verbose_name=_("User identifier"), unique=False, null=True)

    registration_id = models.TextField(verbose_name=_("Registration token"), unique=False, null=True) # reset unique
    public_key_der = models.BinaryField(verbose_name=_("Public key (iOS)"), null=True, blank=True)

    class Meta:
        indexes = []
        verbose_name = "Attested FCM device"
        verbose_name_plural = "Attested FCM devices"

    def set_public_key(self, public_key: EllipticCurvePublicKey):
        self.public_key_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.save(update_fields=["public_key_der"])

    def get_public_key(self):
        if self.public_key_der is None:
            return None

        return serialization.load_der_public_key(bytes(self.public_key_der))

    @classmethod
    def deliverable(cls):
        """Devices that actually hold an FCM token."""
        return cls.objects.exclude(registration_id__isnull=True)

    @classmethod
    def targets_by_uid(cls, uid: str):
        """Devices registered under a generic user identifier."""
        return cls.deliverable().filter(uid=uid)

    @classmethod
    def targets_by_wallet(cls, chain: str, address: str):
        """
        Devices that have linked a wallet address on a given chain.

        The unique constraint on WalletLink already means a device matches at most
        once here; distinct() is kept so the contract holds if targeting is ever
        widened (for example to a whole chain).
        """
        return (
            cls.deliverable()
            .filter(wallets__chain=chain, wallets__address=address)
            .distinct()
        )


class WalletLink(models.Model):
    """
    A wallet address a device wants notifications for.

    A device may link several addresses, on several chains. `verified` records
    whether ownership was actually proven by a signature: Solana links are proven,
    Polkadot links are not yet (see ApiApp.wallets.PolkadotVerifier), so the
    difference is stored rather than assumed.
    """

    class Chain(models.TextChoices):
        POLKADOT = "polkadot", _("Polkadot")
        SOLANA = "solana", _("Solana")

    device = models.ForeignKey(
        AttestedFCMDevice,
        verbose_name=_("Device"),
        related_name="wallets",
        on_delete=models.CASCADE,
    )
    chain = models.CharField(verbose_name=_("Chain"), max_length=32, choices=Chain.choices)
    address = models.TextField(verbose_name=_("Wallet address"))
    verified = models.BooleanField(verbose_name=_("Ownership verified"), default=False)
    created_at = models.DateTimeField(verbose_name=_("Linked at"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device", "chain", "address"], name="uniq_device_chain_address"
            )
        ]
        indexes = [models.Index(fields=["chain", "address"])]
        verbose_name = "Wallet link"
        verbose_name_plural = "Wallet links"

    def __str__(self):
        return f"{self.chain}:{self.address}"


class Nonce(models.Model):
    nonce = models.CharField(verbose_name=_("Nonce"), max_length=255, primary_key=True)
    created_at = models.DateTimeField(verbose_name=_("Nonce created at"), db_index=True)
    consumed = models.BooleanField(verbose_name=_("Consumed"), default=False)

    objects = NonceManager()

    class Meta:
        verbose_name = "Nonce"
        verbose_name_plural = "Nonces"

    def consume(self) -> bool:
        """
        Consume the nonce if valid.
        Returns:
             bool: True if the nonce is consumed, False otherwise.
        """
        cutoff = timezone.now() - timedelta(
            seconds=ATTESTATION_NONCE_EXPIRY_SECONDS
        )

        updated = (
            Nonce.objects
            .filter(
                nonce=self.nonce,
                consumed=False,
                created_at__gte=cutoff
            )
            .update(consumed=True)
        )

        if updated == 1:
            self.consumed = True
            return True

        return False
