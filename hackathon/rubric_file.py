"""A hackathon's whole rubric as one JSON file: download it, or import one (preview first, then all or nothing).

The file format ("format": "kl-ciie-rubric", "version": 1):

    guidance            text shown at the top of every faculty marksheet (optional)
    bands               [{name, min_percent, max_percent, meaning}]
    self_score_out_of   a number, or null for no self-score
    round_weights       one number per round, by position, adding up to 1
    recommendations     [text]
    red_flags           [text]
    rounds              [{name, focus, total, suggested_questions, advance, comment_prompts, criteria}]
        advance         null, or {min_total, key_criterion, key_min_band, may_advance_anyway}
        criteria        [{name, tag, max, strong, weak}]

Rounds are matched to the hackathon's rounds by position. A round the file would change is refused while its
rubric is locked (marks exist), and then nothing at all is imported.
"""

import json
from decimal import Decimal, InvalidOperation

from django.db import transaction

from . import rubric
from .models import Criterion, Round, Score

FORMAT = "kl-ciie-rubric"
VERSION = 1


class RubricFileError(Exception):
    """The file can't be imported; .problems lists every reason, in plain words."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(" ".join(self.problems))


# --- Download --------------------------------------------------------------------------------------


def _number(value):
    """A Decimal as a JSON number: 30 rather than 30.00, 0.25 rather than 0.250."""
    if value is None:
        return None
    value = Decimal(str(value)).normalize()
    return int(value) if value == value.to_integral_value() else float(value)


def band_rows(event) -> list[dict]:
    """The bands, highest first, each with its range and meaning (a missing top end is worked out from the band above)."""
    out, above = [], None
    for b in rubric.bands(event):
        low = Decimal(str(b["min"]))
        if b.get("max") is not None:
            high = Decimal(str(b["max"]))
        elif above is None:
            high = Decimal(100)
        else:
            high = above - 1 if above - 1 >= low else above
        out.append({"name": b["name"], "min_percent": _number(low), "max_percent": _number(high), "meaning": b.get("meaning", "")})
        above = low
    return out


def round_dict(round_) -> dict:
    criteria = list(round_.criteria.all())
    advance = None
    if round_.min_total is not None or round_.key_criterion_id or round_.allow_override:
        key = next((c for c in criteria if c.pk == round_.key_criterion_id), None)
        advance = {
            "min_total": _number(round_.min_total),
            "key_criterion": key.label if key else None,
            "key_min_band": round_.key_min_band or None if key else None,
            "may_advance_anyway": bool(round_.allow_override),
        }
    return {
        "name": round_.name,
        "focus": round_.focus,
        "total": _number(round_.total_marks),
        "suggested_questions": round_.suggested_questions,
        "advance": advance,
        "comment_prompts": list(rubric.prompts(round_)),
        "criteria": [{"name": c.label, "tag": c.tag, "max": _number(c.max_marks), "strong": c.strong_text, "weak": c.weak_text}
                     for c in criteria],
    }


def export_rubric(event) -> dict:
    settings_row = rubric.settings_for(event)
    rounds = list(Round.objects.filter(event=event).prefetch_related("criteria"))
    return {
        "format": FORMAT,
        "version": VERSION,
        "guidance": settings_row.guidance,
        "bands": band_rows(event),
        "self_score_out_of": settings_row.self_score_max,
        "round_weights": [_number(rubric.weight(r)) for r in rounds],
        "recommendations": list(settings_row.recommendations or []),
        "red_flags": list(settings_row.red_flags or []),
        "rounds": [round_dict(r) for r in rounds],
    }


def export_text(event) -> str:
    return json.dumps(export_rubric(event), indent=2, ensure_ascii=False) + "\n"


# --- Reading and checking a file -----------------------------------------------------------------


def _dec(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text(value) -> str | None:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else None


def _texts(value, what, problems) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        problems.append(f"\"{what}\" must be a list of texts.")
        return []
    return [x.strip() for x in value if x.strip()]


def parse(raw) -> dict:
    """Read and check a rubric file (bytes or text). Returns the cleaned rubric, or raises RubricFileError with every problem."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RubricFileError(["That file isn't a rubric file: it isn't text."]) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise RubricFileError(["That file isn't a rubric file: it couldn't be read as JSON (.json)."]) from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise RubricFileError([f"That file isn't a KL-CIIE rubric file: it should say \"format\": \"{FORMAT}\"."])
    if data.get("version") != VERSION:
        raise RubricFileError([f"This rubric file is version {data.get('version')!r}; the portal reads version {VERSION}."])
    problems = []

    guidance = _text(data.get("guidance"))
    if guidance is None:
        problems.append("\"guidance\" must be text.")

    bands = []
    raw_bands = data.get("bands")
    if not isinstance(raw_bands, list) or not raw_bands:
        problems.append("\"bands\" must be a list with at least one band.")
        raw_bands = []
    for i, b in enumerate(raw_bands, start=1):
        if not isinstance(b, dict):
            problems.append(f"Band {i} must have a name, min_percent, max_percent and meaning.")
            continue
        name, low, high, meaning = _text(b.get("name")), _dec(b.get("min_percent")), _dec(b.get("max_percent")), _text(b.get("meaning"))
        label = f"Band {i}" + (f" ({name})" if name else "")
        if not name:
            problems.append(f"Band {i} has no name.")
        elif len(name) > 40:
            problems.append(f"{label}: the name is longer than 40 characters.")
        if low is None or high is None:
            problems.append(f"{label}: min_percent and max_percent must be numbers.")
            continue
        if not (0 <= low <= high <= 100):
            problems.append(f"{label}: needs 0 ≤ min_percent ≤ max_percent ≤ 100.")
        if meaning is None:
            problems.append(f"{label}: the meaning must be text.")
        bands.append({"name": name or "", "min": low, "max": high, "meaning": meaning or ""})
    names = [b["name"].lower() for b in bands]
    for name in {n for n in names if n and names.count(n) > 1}:
        problems.append(f"Two bands are called \"{name}\".")
    bands.sort(key=lambda b: -b["min"])
    for higher, lower in zip(bands, bands[1:]):
        if lower["max"] >= higher["min"]:
            problems.append(f"Bands {lower['name']} ({_number(lower['min'])}–{_number(lower['max'])}%) and "
                            f"{higher['name']} ({_number(higher['min'])}–{_number(higher['max'])}%) overlap.")
    band_names = {b["name"].lower(): b["name"] for b in bands}

    self_score = data.get("self_score_out_of")
    if self_score is not None:
        number = _dec(self_score)
        if number is None or number != number.to_integral_value() or not 1 <= number <= 1000:
            problems.append("\"self_score_out_of\" must be a whole number from 1 to 1000, or null.")
            self_score = None
        else:
            self_score = int(number)

    recommendations = _texts(data.get("recommendations"), "recommendations", problems)
    for text in recommendations:
        if len(text) > 100:
            problems.append(f"The recommendation \"{text[:40]}...\" is longer than 100 characters.")
    red_flags = _texts(data.get("red_flags"), "red_flags", problems)

    rounds = []
    raw_rounds = data.get("rounds")
    if not isinstance(raw_rounds, list) or not raw_rounds:
        problems.append("\"rounds\" must be a list with at least one round.")
        raw_rounds = []
    for i, r in enumerate(raw_rounds, start=1):
        if not isinstance(r, dict):
            problems.append(f"Round {i} must be a set of name, focus, total, criteria and so on.")
            continue
        name = _text(r.get("name"))
        where = f"Round {i}" + (f" ({name})" if name else "")
        if not name:
            problems.append(f"Round {i} has no name.")
        elif len(name) > 100:
            problems.append(f"{where}: the name is longer than 100 characters.")
        focus, questions = _text(r.get("focus")), _text(r.get("suggested_questions"))
        if focus is None or questions is None:
            problems.append(f"{where}: focus and suggested_questions must be text.")
        total = _dec(r.get("total"))
        if total is None or total <= 0:
            problems.append(f"{where}: total must be a number above 0.")
        prompts = _texts(r.get("comment_prompts"), f"{where}: comment_prompts", problems)
        criteria = []
        raw_criteria = r.get("criteria")
        if not isinstance(raw_criteria, list) or not raw_criteria:
            problems.append(f"{where}: needs at least one criterion.")
            raw_criteria = []
        for j, c in enumerate(raw_criteria, start=1):
            if not isinstance(c, dict):
                problems.append(f"{where}, criterion {j}: must have a name, tag, max, strong and weak.")
                continue
            cname, tag, top = _text(c.get("name")), _text(c.get("tag")), _dec(c.get("max"))
            strong, weak = _text(c.get("strong")), _text(c.get("weak"))
            cwhere = f"{where}, criterion {j}" + (f" ({cname})" if cname else "")
            if not cname:
                problems.append(f"{where}, criterion {j}: has no name.")
            elif len(cname) > 200:
                problems.append(f"{cwhere}: the name is longer than 200 characters.")
            if tag is None or (tag and len(tag) > 60):
                problems.append(f"{cwhere}: the tag must be text of at most 60 characters (it may be \"\").")
            if top is None or top <= 0 or top != top.quantize(Decimal("0.01")):
                problems.append(f"{cwhere}: max must be a number above 0, with at most 2 decimals.")
                top = None
            if strong is None or weak is None:
                problems.append(f"{cwhere}: strong and weak must be text.")
            criteria.append({"name": cname or "", "tag": tag or "", "max": top, "strong": strong or "", "weak": weak or ""})
        labels = [c["name"].lower() for c in criteria]
        for label in {x for x in labels if x and labels.count(x) > 1}:
            problems.append(f"{where}: two criteria are called \"{label}\".")
        if total is not None and criteria and all(c["max"] is not None for c in criteria):
            added = sum((c["max"] for c in criteria), Decimal(0))
            if added != total:
                problems.append(f"{where}: the criteria add up to {_number(added)}, but the round total is {_number(total)}.")
        advance = r.get("advance")
        rule = None
        if advance is not None:
            if not isinstance(advance, dict):
                problems.append(f"{where}: advance must be null or a set of min_total, key_criterion, key_min_band and may_advance_anyway.")
            else:
                min_total = advance.get("min_total")
                min_total = None if min_total is None else _dec(min_total)
                if advance.get("min_total") is not None and (min_total is None or min_total < 0):
                    problems.append(f"{where}: advance min_total must be a number, or null.")
                key = _text(advance.get("key_criterion")) or ""
                band = _text(advance.get("key_min_band")) or ""
                if key and key.lower() not in labels:
                    problems.append(f"{where}: the advance rule names the criterion \"{key}\", which isn't in this round.")
                if band and band.lower() not in band_names:
                    problems.append(f"{where}: the advance rule names the band \"{band}\", which isn't one of the bands.")
                if key and not band:
                    problems.append(f"{where}: the advance rule names a key criterion but no key_min_band.")
                anyway = advance.get("may_advance_anyway", False)
                if not isinstance(anyway, bool):
                    problems.append(f"{where}: may_advance_anyway must be true or false.")
                rule = {"min_total": min_total, "key_criterion": next((c["name"] for c in criteria if c["name"].lower() == key.lower()), key),
                        "key_min_band": band_names.get(band.lower(), band), "may_advance_anyway": anyway is True}
        rounds.append({"name": name or "", "focus": focus or "", "total": total, "suggested_questions": questions or "",
                       "advance": rule, "comment_prompts": prompts, "criteria": criteria})

    weights = data.get("round_weights")
    clean_weights = None
    if weights not in (None, []):
        numbers = [_dec(w) for w in weights] if isinstance(weights, list) else [None]
        if any(w is None or not 0 <= w <= 1 or w != w.quantize(Decimal("0.01")) for w in numbers):
            problems.append("\"round_weights\" must be a list of numbers from 0 to 1, with at most 2 decimals.")
        elif len(numbers) != len(rounds):
            problems.append(f"\"round_weights\" has {len(numbers)} weight(s) but the file has {len(rounds)} round(s): give one per round.")
        elif abs(sum(numbers, Decimal(0)) - 1) >= Decimal("0.005"):
            problems.append(f"The round weights add up to {_number(sum(numbers, Decimal(0)))}; they must add up to 1.")
        else:
            clean_weights = numbers
    if problems:
        raise RubricFileError(problems)
    return {"guidance": guidance or "", "bands": bands, "self_score_out_of": self_score, "round_weights": clean_weights,
            "recommendations": recommendations, "red_flags": red_flags, "rounds": rounds}


# --- Preview and import ------------------------------------------------------------------------


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True, default=lambda d: str(Decimal(str(d)).normalize())) == \
        json.dumps(b, sort_keys=True, default=lambda d: str(Decimal(str(d)).normalize()))


def _file_round_as_dict(spec_round) -> dict:
    """A cleaned file round in the same shape as round_dict(), to see whether importing would change a round."""
    advance = spec_round["advance"]
    return {
        "name": spec_round["name"], "focus": spec_round["focus"], "total": _number(spec_round["total"]),
        "suggested_questions": spec_round["suggested_questions"],
        "advance": None if advance is None else {
            "min_total": _number(advance["min_total"]), "key_criterion": advance["key_criterion"] or None,
            "key_min_band": advance["key_min_band"] or None if advance["key_criterion"] else None,
            "may_advance_anyway": advance["may_advance_anyway"],
        },
        "comment_prompts": spec_round["comment_prompts"] or list(rubric.prompts(Round())),
        "criteria": [{"name": c["name"], "tag": c["tag"], "max": _number(c["max"]), "strong": c["strong"], "weak": c["weak"]}
                     for c in spec_round["criteria"]],
    }


def plan(event, spec) -> dict:
    """What an import would do: per round, the existing round it lands on (or new), whether it changes, and whether
    it is locked; plus warnings."""
    existing = list(Round.objects.filter(event=event).prefetch_related("criteria"))
    weights = spec["round_weights"]
    rows, locked, warnings = [], [], []
    for i, spec_round in enumerate(spec["rounds"]):
        current = existing[i] if i < len(existing) else None
        changes = True
        if current is not None:
            same_weight = weights is None or (current.weight is not None and current.weight == weights[i])
            changes = not (_same(round_dict(current), _file_round_as_dict(spec_round)) and same_weight)
            if changes and Score.objects.filter(criterion__round=current).exists():
                locked.append(current.name)  # marks exist: the rubric is locked, and its criteria can't be replaced
        rows.append({"spec": spec_round, "current": current, "changes": changes,
                     "weight": weights[i] if weights is not None else None, "position": i + 1})
    if len(existing) > len(spec["rounds"]):
        extra = ", ".join(r.name for r in existing[len(spec["rounds"]):])
        warnings.append(f"This hackathon has {len(existing)} rounds and the file has {len(spec['rounds'])}. "
                        f"Left as they are: {extra}. Check the round weights afterwards.")
    settings_row = rubric.settings_for(event)
    if any(r.criteria.all() for r in existing) or settings_row.guidance or settings_row.red_flags:
        warnings.append("This hackathon already has rubric content. Importing replaces the criteria, descriptors, advance rules "
                        "and prompts of the rounds in the file, and the bands, red flags, recommendations, self-score, weights "
                        "and guidance. Round times stay as they are.")
    return {"rounds": rows, "locked": locked, "warnings": warnings}


@transaction.atomic
def apply(event, spec) -> dict:
    """Import a checked rubric. All or nothing: refused, naming the rounds, if any round it changes is locked."""
    result = plan(event, spec)
    if result["locked"]:
        names = ", ".join(result["locked"])
        raise RubricFileError([f"Nothing was imported: marks already exist in {names}, so {'its' if len(result['locked']) == 1 else 'their'} "
                               "rubric is locked. Import a file that leaves those rounds as they are."])
    last = Round.objects.filter(event=event).order_by("-position").first()
    next_position = (last.position if last else 0) + 1
    for row in result["rounds"]:
        spec_round = row["spec"]
        round_ = row["current"]
        if round_ is None:
            round_ = Round(event=event, position=next_position)  # no times: the organisers set them
            next_position += 1
        elif not row["changes"]:
            continue
        advance = spec_round["advance"] or {}
        round_.name = spec_round["name"]
        round_.focus = spec_round["focus"]
        round_.suggested_questions = spec_round["suggested_questions"]
        round_.total_marks = spec_round["total"]
        round_.weight = row["weight"]
        round_.min_total = advance.get("min_total")
        round_.key_min_band = advance.get("key_min_band", "") if advance.get("key_criterion") else ""
        round_.allow_override = bool(advance.get("may_advance_anyway"))
        round_.comment_prompts = spec_round["comment_prompts"]
        round_.key_criterion = None
        round_.save()
        round_.criteria.all().delete()
        key = None
        for position, c in enumerate(spec_round["criteria"], start=1):
            made = Criterion.objects.create(round=round_, label=c["name"], tag=c["tag"], max_marks=c["max"], position=position,
                                            strong_text=c["strong"], weak_text=c["weak"])
            if advance.get("key_criterion") and c["name"].lower() == advance["key_criterion"].lower():
                key = made
        round_.key_criterion = key
        round_.save(update_fields=["key_criterion"])
    settings_row = rubric.settings_for(event)
    settings_row.guidance = spec["guidance"]
    settings_row.bands = [{"name": b["name"], "min": _number(b["min"]), "max": _number(b["max"]), "meaning": b["meaning"]}
                          for b in spec["bands"]]
    settings_row.self_score_max = spec["self_score_out_of"]
    settings_row.recommendations = spec["recommendations"]
    settings_row.red_flags = spec["red_flags"]
    settings_row.save()
    return result
