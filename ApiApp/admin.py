from django.contrib import admin

from fcm_django.models import FCMDevice
from .models import AttestedFCMDevice, WalletLink


admin.site.unregister(FCMDevice)


class WalletLinkInline(admin.TabularInline):
    model = WalletLink
    extra = 0
    fields = ("chain", "address", "verified", "created_at")
    readonly_fields = ("created_at",)


@admin.register(AttestedFCMDevice)
class AttestedFCMDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "uid",
        "name",
        "type",
        "device_id",
        "registration_id",
        "active",
        "date_created",
    )
    list_filter = ("type", "active")
    search_fields = ("name", "registration_id")
    inlines = [WalletLinkInline]


@admin.register(WalletLink)
class WalletLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "device", "chain", "address", "verified", "created_at")
    list_filter = ("chain", "verified")
    search_fields = ("address",)
