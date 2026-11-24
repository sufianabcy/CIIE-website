import segno
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from events.models import Event
from people.decorators import profile_required
from results.models import Result

from .forms import RegistrationForm
from .models import Registration
from .services import RegistrationError, cancel, register, send_confirmation


@profile_required
def register_view(request, slug):
    event = get_object_or_404(Event, slug=slug, status=Event.Status.PUBLISHED)
    person = request.user
    existing = Registration.objects.filter(event=event, person=person, status=Registration.Status.CONFIRMED).first()
    if existing:
        return redirect(existing.get_pass_url())
    open_now, reason = event.registration_state()
    needs_kl = event.kl_only and not person.is_kl_verified
    questions = event.questions.filter(is_hidden=False)
    form = RegistrationForm(request.POST or None, questions=questions)
    if request.method == "POST" and open_now and not needs_kl and form.is_valid():
        try:
            registration, created = register(event=event, person=person, answers=form.answers())
        except RegistrationError as exc:
            messages.error(request, str(exc))
            return redirect(event)
        if created:
            send_confirmation(registration)
            messages.success(request, "You're registered. This is your pass: show it at the door.")
        return redirect(registration.get_pass_url())
    return render(
        request,
        "registrations/register.html",
        {"event": event, "form": form, "open_now": open_now, "closed_reason": reason, "needs_kl": needs_kl},
    )


@login_required
def my_registrations(request):
    if not request.user.profile_complete:
        return redirect("profile")
    now = timezone.now()
    registrations = (
        Registration.objects.filter(person=request.user)
        .select_related("event")
        .prefetch_related("checkins")
        .order_by("event__starts_at")
    )
    upcoming = [r for r in registrations if r.event.ends_at >= now and r.is_confirmed]
    past = [r for r in registrations if r.event.ends_at < now or not r.is_confirmed][::-1]
    results = Result.objects.filter(
        registration__person=request.user, status=Result.Status.PUBLISHED
    ).select_related("event")
    suggestions = []
    if not upcoming:
        suggestions = Event.objects.public().upcoming()[:3]
    return render(
        request,
        "registrations/my_registrations.html",
        {
            "next_up": upcoming[0] if upcoming else None,
            "upcoming": upcoming[1:],
            "past": past,
            "results": results,
            "suggestions": suggestions,
        },
    )


def _owned_registration(request, code) -> Registration:
    registration = get_object_or_404(Registration.objects.select_related("event", "person"), pass_code=code)
    user = request.user
    if not user.is_authenticated:
        raise Http404
    if registration.person_id != user.pk and not registration.event.door_open_for(user):
        raise Http404
    return registration


@login_required
def pass_view(request, code):
    registration = _owned_registration(request, code)
    # The QR holds only the random pass code, so nothing breaks when keys change.
    qr_svg = segno.make_qr(registration.pass_code, error="m").svg_inline(scale=8, border=4, omitsize=True)
    can_cancel = (
        registration.is_confirmed
        and registration.person_id == request.user.pk
        and registration.event.starts_at > timezone.now()
        and registration.active_checkin() is None
    )
    return render(
        request,
        "registrations/pass.html",
        {"registration": registration, "event": registration.event, "qr_svg": qr_svg, "can_cancel": can_cancel},
    )


@login_required
@require_POST
def cancel_view(request, code):
    registration = _owned_registration(request, code)
    if registration.person_id != request.user.pk:
        raise Http404
    if registration.event.starts_at <= timezone.now():
        messages.error(request, "The event has started, so this registration can no longer be cancelled.")
    else:
        try:
            cancel(registration, by=request.user)
            messages.success(request, "Registration cancelled. Your seat is free for someone else.")
        except RegistrationError as exc:
            messages.error(request, str(exc))
    return redirect("my_registrations")
