import logging
from rest_framework import serializers

from ApiApp.models import AttestedFCMDevice, Nonce, WalletLink
from ApiApp.utils import generate_device_jwt, AttestationHandler
from ApiApp.wallets import VERIFIERS, InvalidAddress, build_link_message

logger = logging.getLogger(__name__)


class FCMTokenSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(max_length=255)


class UidSerializer(serializers.Serializer):
    user_id = serializers.CharField(max_length=255)


class WalletLinkSerializer(serializers.Serializer):
    """
    Link a wallet address to the calling device, proving ownership where possible.

    Expects a `device` in the serializer context. The device identifier is taken
    from the authenticated JWT and never from the request body, so a signature
    captured from one device cannot be replayed by another.
    """

    nonce = serializers.CharField(max_length=255)
    chain = serializers.ChoiceField(choices=WalletLink.Chain.choices)
    address = serializers.CharField(max_length=255)
    signature = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    verified = serializers.BooleanField(read_only=True)

    def validate(self, attrs):
        chain = attrs['chain']
        address = attrs['address']
        nonce = attrs['nonce']
        signature = attrs.get('signature')
        device = self.context['device']

        verifier = VERIFIERS[chain]

        # Cheap checks first, so a malformed request does not burn a valid nonce.
        try:
            verifier.validate_address(address)
        except InvalidAddress as error:
            raise serializers.ValidationError({'address': str(error)})

        if verifier.requires_signature and not signature:
            raise serializers.ValidationError(
                {'signature': f'A signature is required to link a {chain} address.'}
            )

        try:
            nonce_record = Nonce.objects.get(nonce=nonce)
        except Nonce.DoesNotExist:
            raise serializers.ValidationError({'nonce': 'Nonce does not exist.'})

        # Consumed before verifying, so a failed attempt cannot be retried or raced.
        if not nonce_record.consume():
            raise serializers.ValidationError({'nonce': 'Nonce is not valid.'})

        if verifier.requires_signature:
            message = build_link_message(
                chain=chain, address=address, nonce=nonce, device_id=device.device_id
            )

            if not verifier.verify_signature(address, message, signature):
                logger.debug(f'Signature verification failed for {chain} link on {device.id}')
                raise serializers.ValidationError(
                    {'signature': 'Signature verification failed.'}
                )

            attrs['verified'] = True
        else:
            # No ownership proof available for this chain yet; recorded as unverified.
            attrs['verified'] = False

        return attrs

    def create(self, validated_data):
        link, _ = WalletLink.objects.update_or_create(
            device=self.context['device'],
            chain=validated_data['chain'],
            address=validated_data['address'],
            defaults={'verified': validated_data['verified']},
        )

        return link


class WalletUnlinkSerializer(serializers.Serializer):
    chain = serializers.ChoiceField(choices=WalletLink.Chain.choices)
    address = serializers.CharField(max_length=255)


class NotificationPayloadSerializer(serializers.Serializer):
    user_id = serializers.CharField(max_length=255, required=False)
    chain = serializers.ChoiceField(choices=WalletLink.Chain.choices, required=False)
    address = serializers.CharField(max_length=255, required=False)
    title = serializers.CharField(max_length=150, required=True)
    body = serializers.CharField(max_length=500, required=True)

    def validate(self, attrs):
        """Exactly one targeting mode: a generic user_id, or a wallet address."""
        user_id = attrs.get('user_id')
        address = attrs.get('address')
        chain = attrs.get('chain')

        if user_id and (address or chain):
            raise serializers.ValidationError(
                'Provide either user_id or address and chain, not both.'
            )

        if not user_id and not address and not chain:
            raise serializers.ValidationError(
                'Provide either user_id or address and chain.'
            )

        if address and not chain:
            raise serializers.ValidationError(
                {'chain': 'chain is required when targeting a wallet address.'}
            )

        if chain and not address:
            raise serializers.ValidationError(
                {'address': 'address is required when targeting a chain.'}
            )

        return attrs


class DeviceRegisterSerializer(serializers.Serializer):
    nonce = serializers.CharField(max_length=255)
    device_id = serializers.CharField(max_length=255)
    platform = serializers.ChoiceField(choices=['android', 'ios'])
    attestation = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    assertion = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)

    def validate(self, attrs):
        logger.debug("DeviceRegisterSerializer.validate called")
        logger.debug(f"Incoming attrs keys: {list(attrs.keys())}")

        nonce = attrs.get('nonce')
        device_id = attrs.get("device_id")
        platform = attrs.get("platform")
        attestation = attrs.get("attestation")
        assertion = attrs.get("assertion")

        logger.debug(f"nonce={nonce}")
        logger.debug(f"device_id={device_id}")
        logger.debug(f"platform={platform}")
        logger.debug(f"has_attestation={bool(attestation)}")
        logger.debug(f"has_assertion={bool(assertion)}")

        try:
            nonce_record = Nonce.objects.get(nonce=nonce)
            logger.debug("Nonce record found")

            if not nonce_record.consume():
                logger.debug("Nonce is not valid")
                raise serializers.ValidationError("Nonce is not valid.")

        except Nonce.DoesNotExist:
            logger.debug("Nonce does not exist in DB")
            raise serializers.ValidationError("Nonce does not exist.")

        public_key = None
        try:
            device = AttestedFCMDevice.objects.get(device_id=device_id, type=platform)
            public_key = device.get_public_key()
            logger.debug("Existing device found, public key loaded")
        except AttestedFCMDevice.DoesNotExist:
            logger.debug("No existing device found")

        try:
            logger.debug("Initializing AttestationHandler")
            handler = AttestationHandler(nonce, platform, attestation, assertion, device_id, public_key)
        except Exception as e:
            logger.exception("AttestationHandler init failed")
            raise serializers.ValidationError(f"Handler init failed: {str(e)}")

        try:
            logger.debug("Starting multiplatform_verify")
            verified = handler.multiplatform_verify()
            logger.debug(f"Verification result: {verified}")

            if not verified:
                raise serializers.ValidationError("Attestation verification failed.")

        except Exception as e:
            logger.exception("Verification threw exception")
            raise serializers.ValidationError(f"Verification error: {str(e)}")

        if platform == 'ios' and public_key is None:
            logger.debug("Creating new iOS device record with public key")
            device = AttestedFCMDevice.objects.create(
                device_id=device_id,
                type=platform
            )
            device.set_public_key(handler.get_public_key())

        logger.debug("Validation successful")
        return attrs

    def create(self, validated_data):
        device_id = validated_data['device_id']
        platform = validated_data['platform']

        device, _ = AttestedFCMDevice.objects.update_or_create(
            device_id=device_id,
            defaults={
                "type": platform,
            }
        )

        access, refresh = generate_device_jwt(device_id, platform)

        return {
            "access": str(access),
            "refresh": str(refresh)
        }
