from django.contrib import admin, messages
from django.utils import timezone

from .models import OutboxEmail
from .services import enqueue_email


@admin.register(OutboxEmail)
class OutboxEmailAdmin(admin.ModelAdmin):
    list_display = ["created_at", "to_email", "kind", "status", "attempts", "sent_at"]
    list_filter = ["status", "kind"]
    search_fields = ["to_email", "subject"]
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in OutboxEmail._meta.fields]
    actions = ["retry_now", "send_again"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_portal_admin

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description="Retry failed emails now")
    def retry_now(self, request, queryset):
        n = queryset.filter(status=OutboxEmail.Status.FAILED).update(
            status=OutboxEmail.Status.QUEUED, next_attempt_at=timezone.now(), attempts=0
        )
        self.message_user(request, f"{n} email(s) queued again.")

    @admin.action(description="Send a copy again (not for codes)")
    def send_again(self, request, queryset):
        n = 0
        for email in queryset.exclude(kind=OutboxEmail.Kind.CODE):
            enqueue_email(
                to=email.to_email, subject=email.subject, text=email.text_body, html=email.html_body,
                kind=email.kind, person=email.person,
            )
            n += 1
        level = messages.SUCCESS if n else messages.WARNING
        self.message_user(request, f"{n} email(s) queued again. Codes are never resent; ask the person to request a new one.", level)
