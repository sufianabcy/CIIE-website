"""Rubric rules: scoring bands, rubric locking, team scores, the advancement rule and which teams go on."""

from decimal import Decimal

from .models import (
    DEFAULT_BANDS, DEFAULT_PROMPTS, DEFAULT_RECOMMENDATIONS, Assignment, Round, RoundOutcome, RubricSettings, Score, Team,
)

DEFAULT_WEIGHTS = {1: Decimal("0.25"), 2: Decimal("0.35"), 3: Decimal("0.40")}


def settings_for(event) -> RubricSettings:
    row, _ = RubricSettings.objects.get_or_create(
        event=event, defaults={"bands": DEFAULT_BANDS, "recommendations": DEFAULT_RECOMMENDATIONS},
    )
    return row


def bands(event) -> list[dict]:
    """Highest first."""
    return sorted(settings_for(event).bands or DEFAULT_BANDS, key=lambda b: -Decimal(str(b["min"])))


def band_for(event, marks, out_of) -> str:
    """The band a mark falls in, e.g. 26 out of 30 (87%) is Strong."""
    if marks is None or not out_of:
        return ""
    percent = Decimal(str(marks)) * 100 / Decimal(str(out_of))
    for b in bands(event):
        if percent >= Decimal(str(b["min"])):
            return b["name"]
    return bands(event)[-1]["name"] if bands(event) else ""


def band_rank(event, name) -> int:
    """0 for the top band; larger is lower. Unknown names rank lowest."""
    names = [b["name"] for b in bands(event)]
    return names.index(name) if name in names else len(names)


def prompts(round_) -> list[str]:
    return round_.comment_prompts or DEFAULT_PROMPTS


def weight(round_) -> Decimal:
    if round_.weight is not None:
        return round_.weight
    rounds = list(Round.objects.filter(event=round_.event).values_list("position", flat=True))
    if len(rounds) == 3 and round_.position in DEFAULT_WEIGHTS:
        return DEFAULT_WEIGHTS[round_.position]
    return (Decimal(1) / len(rounds)).quantize(Decimal("0.01")) if rounds else Decimal(0)


def weights_problem(event) -> str:
    total = sum((weight(r) for r in Round.objects.filter(event=event)), Decimal(0))
    return "" if abs(total - 1) < Decimal("0.005") else f"The round weights add up to {total.normalize():f}, not 1."


def criteria_total(round_) -> Decimal:
    return sum((c.max_marks for c in round_.criteria.all()), Decimal(0))


def total_problem(round_) -> str:
    total = criteria_total(round_)
    if round_.criteria.exists() and total != round_.total_marks:
        return f"The criteria add up to {total.normalize():f}, but {round_.name} is out of {round_.total_marks.normalize():f}."
    return ""


def is_locked(round_) -> bool:
    """Once any marks exist for the round, its rubric is locked (an admin can unlock it)."""
    return Score.objects.filter(criterion__round=round_).exists() and not round_.rubric_unlocked


# --- Scores -----------------------------------------------------------------------------------


def team_score(round_, team) -> dict | None:
    """Averages of the submitted marksheets for this team and round: total and per criterion. None if none submitted."""
    submitted = list(Assignment.objects.filter(round=round_, team=team, submitted_at__isnull=False).prefetch_related("scores"))
    if not submitted:
        return None
    per_criterion = {}
    for c in round_.criteria.all():
        given = [s.marks for a in submitted for s in a.scores.all() if s.criterion_id == c.pk]
        per_criterion[c.pk] = (sum(given, Decimal(0)) / len(given)).quantize(Decimal("0.01")) if given else None
    totals = [a.total() for a in submitted]
    return {
        "total": (sum(totals, Decimal(0)) / len(totals)).quantize(Decimal("0.01")),
        "criteria": per_criterion, "judges": len(submitted),
        "override": any(a.advance_anyway for a in submitted),
        "self_scores": [a.self_score for a in submitted if a.self_score is not None],
    }


def percent(round_, total) -> Decimal | None:
    """A round score out of 100, against the criteria's maximums (the round's stated total if it has no criteria)."""
    out_of = round_.out_of or round_.total_marks
    if total is None or not out_of:
        return None
    return (Decimal(total) * 100 / out_of).quantize(Decimal("0.01"))


def meets_rule(round_, score) -> tuple[bool, list[str]]:
    """Whether a team's round score meets the round's advancement rule, and why not."""
    if score is None:
        return False, ["no submitted marks"]
    why = []
    if round_.min_total is not None and score["total"] < round_.min_total:
        why.append(f"total {score['total'].normalize():f} is under {round_.min_total.normalize():f}")
    if round_.key_criterion_id and round_.key_min_band:
        key = round_.key_criterion
        got = band_for(round_.event, score["criteria"].get(key.pk), key.max_marks)
        if band_rank(round_.event, got) > band_rank(round_.event, round_.key_min_band):
            why.append(f"{key.label} is {got or 'not marked'}, needs at least {round_.key_min_band}")
    return not why, why


def previous_round(round_):
    return Round.objects.filter(event=round_.event, position__lt=round_.position).order_by("-position").first()


def eliminating(event) -> bool:
    """Whether teams are eliminated between rounds at this hackathon (off: every team goes through every round)."""
    return settings_for(event).eliminate_teams


def eligible_team_ids(round_):
    """Teams that may take part in this round: all of them, unless teams are eliminated between rounds; then those
    advanced from the previous round once an admin has decided (all before)."""
    if round_ is None or not eliminating(round_.event):
        return None
    before = previous_round(round_)
    if before is None or not before.outcomes.exists():
        return None
    return set(before.outcomes.filter(advanced=True).values_list("team_id", flat=True))


def eligible_teams(round_):
    ids = eligible_team_ids(round_)
    teams = Team.objects.filter(event=round_.event)
    return teams if ids is None else teams.filter(pk__in=ids)


def furthest_round(team, rounds) -> str:
    if rounds and not eliminating(rounds[0].event):
        # Nobody is eliminated: the last round the team was allocated in (or the first round).
        judged = [r for r in rounds if Assignment.objects.filter(round=r, team=team).exists()]
        return (judged or rounds)[-1].name if judged else rounds[0].name
    reached = rounds[0].name if rounds else ""
    for r in rounds[1:]:
        ids = eligible_team_ids(r)
        if ids is None or team.pk in ids:
            reached = r.name
        else:
            break
    return reached


def outcome(round_, team):
    return RoundOutcome.objects.filter(round=round_, team=team).first()


def copy_rubric(source_event, target_event):
    """Copy rounds (matched by order), their criteria and the rubric settings from another hackathon."""
    from .models import Criterion

    made = 0
    for src in Round.objects.filter(event=source_event).prefetch_related("criteria"):
        dst, _ = Round.objects.get_or_create(event=target_event, position=src.position, defaults={"name": src.name})
        if is_locked(dst):
            continue
        for field in ("name", "focus", "suggested_questions", "total_marks", "weight", "min_total", "key_min_band",
                      "allow_override", "comment_prompts"):
            setattr(dst, field, getattr(src, field))
        dst.save()
        dst.criteria.all().delete()
        key = None
        for c in src.criteria.all():
            new = Criterion.objects.create(round=dst, label=c.label, tag=c.tag, max_marks=c.max_marks, position=c.position,
                                           description=c.description, strong_text=c.strong_text, weak_text=c.weak_text)
            if src.key_criterion_id == c.pk:
                key = new
        dst.key_criterion = key
        dst.save(update_fields=["key_criterion"])
        made += 1
    src_settings, dst_settings = settings_for(source_event), settings_for(target_event)
    for field in ("bands", "red_flags", "self_score_max", "recommendations", "guidance", "eliminate_teams"):
        setattr(dst_settings, field, getattr(src_settings, field))
    dst_settings.save()
    return made
