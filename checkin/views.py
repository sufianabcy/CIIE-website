from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.models import Event
from registrations.models import Registration

from . import services
from .forms import UndoForm, WalkInForm
from .models import CheckIn, ScanLog


def _door_event(request, slug) -> Event:
    event = get_object_or_404(Event, slug=slug)
    if not event.door_open_for(request.user):
        raise PermissionDenied("You are not on this event's door team.")
    return event


def _gate(request) -> str:
    gate = (request.POST.get("gate") or request.GET.get("gate") or request.session.get("door_gate") or "").strip()
    request.session["door_gate"] = gate[:30]
    return gate[:30]


def _device(request) -> str:
    return request.META.get("HTTP_USER_AGENT", "")[:200]


@login_required
def door(request, slug):
    event = _door_event(request, slug)
    return render(
        request,
        "checkin/door.html",
        {"event": event, "gate": request.session.get("door_gate", ""), "is_coordinator": event.coordinated_by(request.user)},
    )


@login_required
@require_POST
def scan(request, slug):
    event = _door_event(request, slug)
    result = services.scan(
        event, request.POST.get("q", ""), by=request.user, gate=_gate(request), device=_device(request)
    )
    return render(request, "checkin/_result.html", {"event": event, "result": result})


@login_required
@require_POST
def check_in_found(request, slug, registration_id):
    event = _door_event(request, slug)
    registration = get_object_or_404(Registration, pk=registration_id, event=event)
    gate = _gate(request)
    result = services.check_in(registration, method=CheckIn.Method.SEARCH, by=request.user, gate=gate)
    services._log(event, f"search: {registration.person}", result.outcome, by=request.user, gate=gate, registration=registration, device=_device(request))
    return render(request, "checkin/_result.html", {"event": event, "result": result})


@login_required
@require_POST
def undo(request, slug, checkin_id):
    event = _door_event(request, slug)
    checkin = get_object_or_404(CheckIn, pk=checkin_id, event=event)
    form = UndoForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else "Undone at the door"
    services.undo(checkin, by=request.user, reason=reason)
    return render(request, "checkin/_undone.html", {"event": event, "checkin": checkin})


@login_required
def walk_in(request, slug):
    event = _door_event(request, slug)
    form = WalkInForm(request.POST or None)
    result = None
    if request.method == "POST" and form.is_valid():
        try:
            email, kl_id = form.split_identity()
        except ValidationError:
            form.add_error("kl_id_or_email", "Enter a KL ID or a valid email address.")
        else:
            result = services.walk_in(
                event,
                name=form.cleaned_data["name"],
                email=email,
                kl_id=kl_id,
                phone=form.cleaned_data["phone"],
                kind=form.cleaned_data["kind"],
                by=request.user,
                gate=_gate(request),
            )
            form = WalkInForm()
    return render(request, "checkin/walk_in.html", {"event": event, "form": form, "result": result})


def counts_for(event: Event) -> dict:
    active = event.checkins.filter(undone_at__isnull=True)
    registered = event.registrations.filter(status=Registration.Status.CONFIRMED, source__in=Registration.SEAT_SOURCES).count()
    by_method = dict(active.values_list("method").annotate(n=Count("id")).values_list("method", "n"))
    hour_ago = timezone.now() - timezone.timedelta(hours=1)
    return {
        "registered": registered,
        "checked_in": active.count(),
        "by_method": [(label, by_method.get(value, 0)) for value, label in CheckIn.Method.choices],
        "by_gate": active.values("gate").annotate(n=Count("id")).order_by("-n"),
        "by_member": active.values("checked_in_by__full_name", "checked_in_by__email").annotate(n=Count("id")).order_by("-n"),
        "undone": event.checkins.filter(undone_at__isnull=False).count(),
        "last_hour": active.filter(checked_in_at__gte=hour_ago).count(),
        "problems_last_hour": event.scan_logs.filter(created_at__gte=hour_ago)
        .filter(Q(outcome=ScanLog.Outcome.NOT_FOUND) | Q(outcome=ScanLog.Outcome.WRONG_EVENT) | Q(outcome=ScanLog.Outcome.ALREADY))
        .count(),
        "recent": event.checkins.select_related("registration__person", "checked_in_by")[:15],
        "updated_at": timezone.now(),
    }


@login_required
def counts(request, slug):
    event = get_object_or_404(Event, slug=slug)
    if not event.coordinated_by(request.user):
        raise PermissionDenied
    template = "checkin/_counts_panel.html" if request.headers.get("HX-Request") else "checkin/counts.html"
    return render(request, template, {"event": event, "c": counts_for(event)})


@login_required
def join_door_team(request, slug, token):
    event = get_object_or_404(Event, slug=slug, door_token=token)
    if event.ends_at < timezone.now():
        messages.error(request, "This event is over, so its door-team link no longer works.")
        return redirect("event_list")
    if request.method == "POST":
        event.door_team.add(request.user)
        messages.success(request, f"You're on the door team for {event.title}. Your own name is stamped on every check-in.")
        return redirect("door", slug=event.slug)
    return render(request, "checkin/join.html", {"event": event})


@login_required
def door_list(request, slug):
    """A printable list per gate, for when the network fails."""
    event = get_object_or_404(Event, slug=slug)
    if not event.coordinated_by(request.user):
        raise PermissionDenied
    registrations = (
        event.registrations.filter(status=Registration.Status.CONFIRMED)
        .select_related("person")
        .order_by("person__full_name")
    )
    return render(request, "checkin/door_list.html", {"event": event, "registrations": registrations, "printed_at": timezone.now()})


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
