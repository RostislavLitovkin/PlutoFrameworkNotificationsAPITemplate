import logging

from firebase_admin import messaging
from rest_framework import permissions, views, status
from rest_framework.response import Response
from rest_framework_api_key.permissions import HasAPIKey

from ApiApp.serializers import (DeviceRegisterSerializer, FCMTokenSerializer, UidSerializer,
                                NotificationPayloadSerializer, WalletLinkSerializer,
                                WalletUnlinkSerializer)
from ApiApp.auth import DeviceJWTAuthentication
from ApiApp.permissions import IsRegisteredDevice
from ApiApp.models import AttestedFCMDevice, Nonce, WalletLink

logger = logging.getLogger(__name__)


def subscribe_to_topics(device, topics):
    """
    Subscribe a device's FCM token to topics, tolerating FCM failures.

    Does nothing when the device has not reported a token yet; FCMTokenUpdateView
    catches those subscriptions up once it arrives.
    """
    if not device.registration_id:
        return

    for topic in topics:
        try:
            messaging.subscribe_to_topic([device.registration_id], topic)
        except Exception as e:
            logger.debug(f'Failed to subscribe device {device.id} to topic {topic}: {e}')


def unsubscribe_from_topics(device, topics):
    if not device.registration_id:
        return

    for topic in topics:
        try:
            messaging.unsubscribe_from_topic([device.registration_id], topic)
        except Exception as e:
            logger.debug(f'Failed to unsubscribe device {device.id} from topic {topic}: {e}')


def linked_chains(device):
    """The distinct chains this device has linked a wallet on."""
    return list(device.wallets.values_list('chain', flat=True).distinct())


class NonceView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, _):
        deleted = Nonce.objects.cleanup()
        logger.debug(f"Deleted {deleted} nonce records.")
        nonce = Nonce.objects.create_nonce()

        return Response({"nonce": nonce})


class DeviceRegisterView(views.APIView):
    permission_classes = [permissions.AllowAny]

    serializer_class = DeviceRegisterSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        tokens = serializer.save() # serializer returns JWT pair after saving the device instance

        return Response(tokens)


class FCMTokenUpdateView(views.APIView):
    permission_classes = [IsRegisteredDevice]
    authentication_classes = [DeviceJWTAuthentication]

    serializer_class = FCMTokenSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        fcm_token = serializer.validated_data['fcm_token']

        try:
            device = AttestedFCMDevice.objects.get(device_id=request.device_id)
        except AttestedFCMDevice.DoesNotExist:
            # shouldn't happen
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        device.registration_id = fcm_token
        device.save(update_fields=['registration_id'])

        # Subscribe to necessary FCM topics. Chain topics are included here too,
        # to catch up wallets linked before this device reported a token.
        subscribe_to_topics(device, ['global', device.type] + linked_chains(device))

        return Response({'message': 'Token updated successfully.'}, status=status.HTTP_200_OK)


class UidUpdateView(views.APIView):
    permission_classes = [IsRegisteredDevice]
    authentication_classes = [DeviceJWTAuthentication]

    serializer_class = UidSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        uid = serializer.validated_data['user_id']

        try:
            device = AttestedFCMDevice.objects.get(device_id=request.device_id)
        except AttestedFCMDevice.DoesNotExist:
            # shouldn't happen
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        device.uid = uid
        device.save(update_fields=['uid'])

        return Response({'message': 'User identifier updated successfully.'}, status=status.HTTP_200_OK)


class WalletLinkView(views.APIView):
    permission_classes = [IsRegisteredDevice]
    authentication_classes = [DeviceJWTAuthentication]

    serializer_class = WalletLinkSerializer

    def post(self, request):
        try:
            device = AttestedFCMDevice.objects.get(device_id=request.device_id)
        except AttestedFCMDevice.DoesNotExist:
            # shouldn't happen
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.serializer_class(data=request.data, context={'device': device})
        serializer.is_valid(raise_exception=True)

        link = serializer.save()

        subscribe_to_topics(device, [link.chain])

        return Response(
            {'chain': link.chain, 'address': link.address, 'verified': link.verified},
            status=status.HTTP_200_OK
        )


class WalletUnlinkView(views.APIView):
    permission_classes = [IsRegisteredDevice]
    authentication_classes = [DeviceJWTAuthentication]

    serializer_class = WalletUnlinkSerializer

    def post(self, request):
        try:
            device = AttestedFCMDevice.objects.get(device_id=request.device_id)
        except AttestedFCMDevice.DoesNotExist:
            # shouldn't happen
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        chain = serializer.validated_data['chain']
        address = serializer.validated_data['address']

        # Scoped to this device, so one device cannot unlink another's wallet. Removing
        # a link that isn't there is a no-op: idempotent, and it reveals nothing about
        # which addresses other devices have linked.
        WalletLink.objects.filter(device=device, chain=chain, address=address).delete()

        if not device.wallets.filter(chain=chain).exists():
            unsubscribe_from_topics(device, [chain])

        return Response(
            {'message': 'Wallet unlinked successfully.'}, status=status.HTTP_200_OK
        )


class RegistrationStatusView(views.APIView):
    """
    What the calling device is registered for.

    Read-only self-inspection: the device is resolved from the JWT claim, so this
    can only ever describe the caller. There is deliberately no way to ask about
    another device, or to look up an address and learn whether anyone registered it.
    """

    permission_classes = [IsRegisteredDevice]
    authentication_classes = [DeviceJWTAuthentication]

    def get(self, request):
        try:
            device = AttestedFCMDevice.objects.get(device_id=request.device_id)
        except AttestedFCMDevice.DoesNotExist:
            return Response({'detail': 'Device not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {
                'device_id': device.device_id,
                'platform': device.type,
                'uid': device.uid,
                # Mirrors the filter in AttestedFCMDevice.deliverable(): a device with
                # no FCM token is skipped by every send, so this is the difference
                # between "a row exists" and "a notification would actually arrive".
                'notifications_enabled': device.registration_id is not None,
                'wallets': [
                    {
                        'chain': wallet.chain,
                        'address': wallet.address,
                        'verified': wallet.verified,
                        'linked_at': wallet.created_at,
                    }
                    for wallet in device.wallets.all()
                ],
            },
            status=status.HTTP_200_OK
        )


class SendNotificationView(views.APIView):
    permission_classes = [HasAPIKey]

    serializer_class = NotificationPayloadSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_id = serializer.validated_data.get('user_id')
        title = serializer.validated_data['title']
        body = serializer.validated_data['body']

        if user_id:
            devices = AttestedFCMDevice.targets_by_uid(user_id)
        else:
            devices = AttestedFCMDevice.targets_by_wallet(
                serializer.validated_data['chain'], serializer.validated_data['address']
            )

        if not devices.exists():
            return Response(
                {'detail': 'No registered devices found for this user.'},
                status=status.HTTP_404_NOT_FOUND
            )

        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
        )

        success_count = 0
        failure_count = 0
        for device in devices:
            try:
                device.send_message(message)
                success_count += 1
            except Exception as e:
                logger.error(f'Error sending message to device {device.id}: {e}')
                failure_count += 1

        return Response(
            {
                'message': 'Notification process completed.',
                'success_count': success_count,
                'failure_count': failure_count,
            },
            status=status.HTTP_200_OK
        )