import segno
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from checkin.models import CheckIn
from registrations.models import Registration
from results.models import Result

from .models import Event


def home(request):
    """The landing page: what KL-CIIE events are, the next few events, and how to join."""
    upcoming = list(Event.objects.public().upcoming()[:6])
    events_url = request.build_absolute_uri(reverse("event_list"))
    # A real, scannable QR on the hero pass: it opens the event list.
    hero_qr = segno.make_qr(events_url, error="m").svg_inline(scale=4, border=0, omitsize=True)
    registered = Registration.objects.filter(
        status=Registration.Status.CONFIRMED, event__is_practice=False
    ).count()
    return render(
        request,
        "events/home.html",
        {
            "upcoming": upcoming,
            "next_event": upcoming[0] if upcoming else None,
            "upcoming_count": Event.objects.public().upcoming().count(),
            "events_run": Event.objects.public().completed().count(),
            "registered_total": registered,
            "checked_in_total": CheckIn.objects.filter(undone_at__isnull=True, event__is_practice=False).count(),
            "hero_qr": hero_qr,
        },
    )


def event_list(request):
    tab = request.GET.get("tab", "upcoming")
    query = request.GET.get("q", "").strip()
    events = Event.objects.public()
    events = events.completed() if tab == "completed" else events.upcoming()
    if query:
        events = events.filter(Q(title__icontains=query) | Q(summary__icontains=query) | Q(venue__icontains=query))
    return render(request, "events/list.html", {"events": events[:60], "tab": tab, "q": query})


def event_detail(request, slug):
    event = get_object_or_404(Event, slug=slug)
    is_coordinator = event.coordinated_by(request.user)
    if (event.status == Event.Status.DRAFT or event.is_practice) and not is_coordinator:
        raise Http404
    open_now, closed_reason = event.registration_state()
    registration = None
    if request.user.is_authenticated:
        registration = Registration.objects.filter(event=event, person=request.user).first()
    results = event.results.filter(status=Result.Status.PUBLISHED).select_related("registration__person")
    return render(
        request,
        "events/detail.html",
        {
            "event": event,
            "registration": registration,
            "open_now": open_now,
            "closed_reason": closed_reason,
            "is_coordinator": is_coordinator,
            "results": results,
        },
    )


@login_required
def manage(request):
    from hackathon.access import overseen_events

    person = request.user
    supervised = overseen_events(person)
    if not person.is_coordinator and not person.door_events.exists() and not supervised.exists():
        raise PermissionDenied
    events = Event.objects.coordinated_by(person) if person.is_coordinator else Event.objects.none()
    events = Event.objects.filter(pk__in=events.values("pk")) | Event.objects.filter(pk__in=supervised.values("pk"))
    events = events.distinct().annotate(
        registered=Count("registrations", filter=Q(registrations__status=Registration.Status.CONFIRMED), distinct=True),
        checked_in=Count("checkins", filter=Q(checkins__undone_at__isnull=True), distinct=True),
    ).order_by("-starts_at")
    door_events = person.door_events.filter(ends_at__gte=_six_hours_ago()).order_by("starts_at")
    return render(request, "events/manage.html", {"events": events, "door_events": door_events})


def _six_hours_ago():
    from django.utils import timezone

    return timezone.now() - timezone.timedelta(hours=6)


def report_numbers(event: Event) -> dict:
    registrations = event.registrations.filter(status=Registration.Status.CONFIRMED)
    active = event.checkins.filter(undone_at__isnull=True)
    by_method = dict(active.values_list("method").annotate(n=Count("id")).values_list("method", "n"))
    return {
        "registered_online": registrations.filter(source__in=Registration.SEAT_SOURCES).count(),
        "walk_ins": by_method.get(CheckIn.Method.WALK_IN, 0),
        "manual_adds": by_method.get(CheckIn.Method.MANUAL_ADD, 0),
        "scanned": by_method.get(CheckIn.Method.SCAN, 0),
        "found_by_search": by_method.get(CheckIn.Method.SEARCH, 0),
        "backfilled": by_method.get(CheckIn.Method.BACKFILL, 0),
        "attended": active.count(),
        "kl_attended": active.filter(registration__person__kl_verified_at__isnull=False).count(),
        "results_published": event.results.filter(status=Result.Status.PUBLISHED).count(),
        "results_waiting": event.results.exclude(status=Result.Status.PUBLISHED).count(),
    }


@login_required
def report(request, slug):
    event = get_object_or_404(Event, slug=slug)
    if not event.coordinated_by(request.user):
        raise PermissionDenied
    numbers = report_numbers(event)
    missing = [
        label
        for label, value in [
            ("Organising unit", event.unit),
            ("Academic year", event.academic_year),
            ("Faculty in charge", event.faculty_in_charge_name),
            ("Faculty in charge ID", event.faculty_in_charge_id),
            ("Student lead", event.student_lead_name),
            ("Student lead ID", event.student_lead_id),
            ("Time slot", event.time_slot),
            ("Venue", event.venue),
        ]
        if not value
    ]
    if event.is_over and numbers["attended"] == 0:
        missing.append("Attendance (nobody is checked in)")
    if numbers["results_waiting"]:
        missing.append(f"{numbers['results_waiting']} result(s) not yet checked and published")
    return render(
        request,
        "events/report.html",
        {"event": event, "numbers": numbers, "missing": missing},
    )
