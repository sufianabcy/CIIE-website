from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from events.models import Event
from registrations.models import Registration

from . import services
from .exports import credentials_file, template_file
from .forms import AutoAssignForm, CredentialsForm, JudgeSignInForm, MarksForm, UploadForm
from .models import Assignment, Criterion, Judge, Score

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SESSION_KEY = "judge_id"
SESSION_CHECK = "judge_check"


def _download(filename, content):
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _coordinated_event(request, slug):
    event = get_object_or_404(Event, slug=slug)
    if not event.coordinated_by(request.user):
        raise PermissionDenied
    return event


# --- Coordinator side -------------------------------------------------------


@login_required
def dashboard(request, slug):
    event = _coordinated_event(request, slug)
    confirmed = event.registrations.filter(status=Registration.Status.CONFIRMED)
    judges = (
        Judge.objects.filter(event=event)
        .annotate(mapped=Count("assignments"), submitted=Count("assignments", filter=Q(assignments__submitted_at__isnull=False)))
    )
    assignments = Assignment.objects.filter(judge__event=event)
    default_expiry = timezone.localtime(max(event.ends_at, timezone.now()) + timedelta(days=3))
    return render(
        request,
        "judging/dashboard.html",
        {
            "event": event,
            "criteria": Criterion.objects.filter(event=event),
            "judges": judges,
            "registered": confirmed.count(),
            "unmapped": confirmed.filter(judge_assignments__isnull=True).count(),
            "mappings": assignments.count(),
            "submitted": assignments.filter(submitted_at__isnull=False).count(),
            "upload_form": UploadForm(),
            "credentials_form": CredentialsForm(initial={"expires_at": default_expiry.strftime("%Y-%m-%dT%H:%M")}),
            "assign_form": AutoAssignForm(),
        },
    )


def _report(request, report, noun):
    level = messages.WARNING if report.problems else messages.SUCCESS
    messages.add_message(request, level, report.summary(noun))
    for problem in report.problems[:15]:
        messages.warning(request, problem)
    if len(report.problems) > 15:
        messages.warning(request, f"…and {len(report.problems) - 15} more.")


@login_required
@require_POST
def upload_faculty(request, slug):
    event = _coordinated_event(request, slug)
    form = UploadForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            _report(request, services.import_faculty(event, form.cleaned_data["file"]), "faculty")
        except Exception:  # a damaged or non-Excel file
            messages.error(request, "That file could not be read as Excel (.xlsx).")
    else:
        messages.error(request, " ".join(form.errors.get("file", ["Choose a file."])))
    return redirect("judging_dashboard", slug)


@login_required
@require_POST
def upload_mapping(request, slug):
    event = _coordinated_event(request, slug)
    form = UploadForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            _report(request, services.import_mapping(event, form.cleaned_data["file"]), "mapping(s)")
        except Exception:
            messages.error(request, "That file could not be read as Excel (.xlsx).")
    else:
        messages.error(request, " ".join(form.errors.get("file", ["Choose a file."])))
    return redirect("judging_dashboard", slug)


@login_required
@require_POST
def auto_assign(request, slug):
    event = _coordinated_event(request, slug)
    form = AutoAssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a number of faculty per student between 1 and 10.")
    elif not event.judges.filter(is_active=True).exists():
        messages.error(request, "Add faculty first.")
    else:
        n = services.auto_assign(event, form.cleaned_data["per_student"])
        messages.success(request, f"{n} new mapping(s) made. Existing mappings were kept.")
    return redirect("judging_dashboard", slug)


@login_required
@require_POST
def issue_credentials(request, slug):
    event = _coordinated_event(request, slug)
    form = CredentialsForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose who gets codes and when the codes stop working.")
        return redirect("judging_dashboard", slug)
    judges = Judge.objects.filter(event=event, is_active=True)
    if form.cleaned_data["which"] == "new":
        judges = judges.filter(access_code_hash="")
    issued = []
    with transaction.atomic():
        for judge in judges.select_for_update():
            issued.append((judge, judge.issue_credentials(form.cleaned_data["expires_at"])))
    if not issued:
        messages.info(request, "Every active faculty member already has a code. Choose \"Everyone\" to make new ones.")
        return redirect("judging_dashboard", slug)
    return _download(*credentials_file(event, issued, request.user))


@login_required
def template(request, slug, kind):
    _coordinated_event(request, slug)
    if kind not in {"faculty", "mapping"}:
        raise Http404
    return _download(*template_file(kind))


# --- Faculty side -----------------------------------------------------------


def current_judge(request):
    judge_id = request.session.get(SESSION_KEY)
    if not judge_id:
        return None
    judge = Judge.objects.select_related("event").filter(pk=judge_id).first()
    # A regenerated or revoked code, or an expired window, ends the session too.
    if not judge or not judge.can_sign_in() or request.session.get(SESSION_CHECK) != judge.access_code_hash[-16:]:
        request.session.pop(SESSION_KEY, None)
        return None
    return judge


def judge_required(view):
    def wrapper(request, *args, **kwargs):
        judge = current_judge(request)
        if not judge:
            return redirect("judge_signin")
        return view(request, judge, *args, **kwargs)

    wrapper.__name__ = view.__name__
    return wrapper


def _ip(request):
    return request.META.get("REMOTE_ADDR", "")


@never_cache
def signin(request):
    if current_judge(request):
        return redirect("judge_home")
    form = JudgeSignInForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        throttle = f"judge-signin-fail:{_ip(request)}"
        if cache.get(throttle, 0) >= 30:
            messages.error(request, "Too many wrong tries from this network. Wait an hour, or ask the organisers for help.")
            return render(request, "judging/signin.html", {"form": form})
        login_id = form.cleaned_data["login_id"].strip().upper()
        judge = Judge.objects.filter(login_id=login_id).first()
        if judge and judge.can_sign_in() and judge.check_code(form.cleaned_data["code"]):
            request.session.cycle_key()
            request.session[SESSION_KEY] = judge.pk
            request.session[SESSION_CHECK] = judge.access_code_hash[-16:]
            Judge.objects.filter(pk=judge.pk).update(failed_signins=0, last_signin_at=timezone.now())
            return redirect("judge_home")
        cache.set(throttle, cache.get(throttle, 0) + 1, 3600)
        if judge:
            Judge.objects.filter(pk=judge.pk).update(failed_signins=judge.failed_signins + 1)
        if judge and judge.credentials_expire_at and timezone.now() > judge.credentials_expire_at:
            messages.error(request, "This access code has expired. Ask the organisers for a new one.")
        else:
            messages.error(request, "That login ID and access code don't match. Check them against the message you got from the organisers.")
    return render(request, "judging/signin.html", {"form": form})


@require_POST
def signout(request):
    request.session.pop(SESSION_KEY, None)
    request.session.pop(SESSION_CHECK, None)
    return redirect("judge_signin")


@never_cache
@judge_required
def home(request, judge):
    criteria_count = Criterion.objects.filter(event=judge.event).count()
    assignments = (
        judge.assignments.select_related("registration__person")
        .annotate(scored=Count("scores"))
        .order_by("submitted_at", "registration__created_at")
    )
    done = sum(1 for a in assignments if a.submitted_at)
    return render(
        request, "judging/home.html",
        {"judge": judge, "assignments": assignments, "done": done, "criteria_count": criteria_count},
    )


@never_cache
@judge_required
def mark(request, judge, assignment_id):
    assignment = get_object_or_404(
        Assignment.objects.select_related("registration__person"), pk=assignment_id, judge=judge
    )
    event = judge.event
    criteria = Criterion.objects.filter(event=event)
    scores = {s.criterion_id: s.marks for s in assignment.scores.all()}
    form = MarksForm(request.POST or None, criteria=criteria, scores=scores, assignment=assignment)
    if request.method == "POST" and not assignment.is_submitted and form.is_valid():
        submitting = "submit" in request.POST
        if not submitting or form.require_all():
            with transaction.atomic():
                for c in form.criteria:
                    value = form.cleaned_data.get(f"c_{c.pk}")
                    if value is None:
                        Score.objects.filter(assignment=assignment, criterion=c).delete()
                    else:
                        Score.objects.update_or_create(assignment=assignment, criterion=c, defaults={"marks": value})
                assignment.remarks = form.cleaned_data["remarks"]
                if submitting:
                    assignment.submitted_at = timezone.now()
                assignment.save()
            if submitting:
                messages.success(request, f"Marks for {assignment.registration.person.display_name} submitted.")
                return redirect("judge_home")
            messages.success(request, "Saved. You can come back and change these until you submit.")
            return redirect("judge_mark", assignment.pk)
    questions = [q for q in event.questions.all() if not q.is_hidden]
    answers = assignment.registration.answers or {}
    details = []
    for q in questions:
        value = answers.get(q.key, "")
        details.append((q, ", ".join(value) if isinstance(value, list) else value))
    out_of = sum(c.max_marks for c in criteria)
    return render(
        request, "judging/mark.html",
        {"judge": judge, "assignment": assignment, "person": assignment.registration.person,
         "details": details, "form": form, "out_of": out_of, "total": sum(scores.values()) if scores else None},
    )
