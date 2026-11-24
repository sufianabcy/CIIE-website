import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from checkin.models import CheckIn, ScanLog
from mailer.models import OutboxEmail
from mailer.services import STUCK_AFTER, current_budget, send_pending

from .models import BackupRun


@staff_member_required
def status(request):
    if not request.user.is_portal_admin:
        raise PermissionDenied
    now = timezone.now()
    hour_ago = now - timedelta(hours=1)
    last_good = BackupRun.objects.filter(ok=True).first()
    outbox = OutboxEmail.objects
    context = {
        "now": now,
        "budget": current_budget(),
        "cap": settings.MAIL_DAILY_CAP,
        "reserve": settings.MAIL_CODE_RESERVE,
        "queued": outbox.filter(status=OutboxEmail.Status.QUEUED).count(),
        "failed": outbox.filter(status=OutboxEmail.Status.FAILED).count(),
        "stuck": outbox.filter(status=OutboxEmail.Status.SENDING, claimed_at__lt=now - STUCK_AFTER).count(),
        "codes_last_hour": outbox.filter(kind=OutboxEmail.Kind.CODE, created_at__gte=hour_ago).count(),
        "checkins_last_hour": CheckIn.objects.filter(recorded_at__gte=hour_ago).count(),
        "scans_last_hour": ScanLog.objects.filter(created_at__gte=hour_ago).count(),
        "last_good_backup": last_good,
        "backup_overdue": not last_good or last_good.started_at < now - timedelta(hours=26),
        "recent_backups": BackupRun.objects.all()[:5],
        "recent_failures": outbox.filter(status=OutboxEmail.Status.FAILED)[:10],
        "mail_inline": settings.MAIL_SEND_INLINE,
        "email_backend": settings.EMAIL_BACKEND.rsplit(".", 2)[-2],
    }
    return render(request, "ops/status.html", context)


@csrf_exempt
def tick(request):
    """Send queued mail. For free hosting with no worker: call it from any scheduler.

    Needs the OPS_TICK_TOKEN in an "X-Tick-Token" header (or ?token= if the
    scheduler cannot set headers).
    """
    expected = settings.OPS_TICK_TOKEN
    given = request.headers.get("X-Tick-Token") or request.GET.get("token") or ""
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        return HttpResponseForbidden("bad token")
    tried = send_pending(limit=settings.MAIL_RATE_PER_MINUTE)
    return JsonResponse({"emails_tried": tried, "queued": OutboxEmail.objects.filter(status=OutboxEmail.Status.QUEUED).count()})
