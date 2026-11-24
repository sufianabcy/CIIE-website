from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse

from .models import BackupRun, SiteSettings


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Registration", {"fields": ["registration_open", "banner_text"]}),
        ("Sign-in routes", {"fields": ["signin_email_code_enabled", "signin_google_enabled", "signin_microsoft_enabled"]}),
        ("Email", {"fields": ["email_paused", "sender_name"]}),
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        SiteSettings.load()
        return redirect(reverse("admin:ops_sitesettings_change", args=[1]))


@admin.register(BackupRun)
class BackupRunAdmin(admin.ModelAdmin):
    list_display = ["started_at", "ok", "size_bytes", "destinations", "triggered_by"]
    list_filter = ["ok"]
    readonly_fields = [f.name for f in BackupRun._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
