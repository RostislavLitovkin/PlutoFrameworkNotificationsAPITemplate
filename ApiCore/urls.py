"""
URL configuration for PlutoFrameworkNotificationsAPITemplate project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

from rest_framework import routers

from rest_framework_simplejwt.views import TokenRefreshView

from ApiApp import views

router = routers.DefaultRouter()

urlpatterns = [
    path('', include(router.urls)),
    path('api/nonce/', views.NonceView.as_view(), name='nonce_obtain'),
    path('api/token/', views.DeviceRegisterView.as_view(), name='obtain_token_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/fcm/token-update/', views.FCMTokenUpdateView.as_view(), name='fcm_token_update'),
    path('api/fcm/send-notification/', views.SendNotificationView.as_view(), name='fcm_send_notification'),
    path('api/user/registration/', views.RegistrationStatusView.as_view(), name='registration_status'),
    path('api/user/uid-update/', views.UidUpdateView.as_view(), name='user_id_update'),
    path('api/user/wallet-link/', views.WalletLinkView.as_view(), name='wallet_link'),
    path('api/user/wallet-unlink/', views.WalletUnlinkView.as_view(), name='wallet_unlink'),
    path('admin/', admin.site.urls),
]
