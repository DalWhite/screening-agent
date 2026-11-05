"""Typed field handlers: the building blocks of a screening question.

A job profile declares questions by *type* (``yes_no``, ``choice``,
``location``...). Each type is implemented once here and reused by every
profile, so adding a new screening process never requires new parsing code:

- ``schema``        JSON-schema properties exposed to the LLM extraction tool
- ``parse_text``    deterministic raw-text parsing (LLM fallback + MockLLM)
- ``apply``         validate an extraction payload into normalised data
- ``coerce_manual`` validate a recruiter edit from the backoffice
- ``display``       human-readable value for recruiters and summaries
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Optional

from .language import normalize
from .validation import (
    map_yes_no,
    match_keywords,
    parse_date_text,
    parse_experience_fallback,
    parse_tags,
    parse_years,
    validate_name,
    validate_start_date,
    validate_years,
)

if TYPE_CHECKING:  # pragma: no cover
    from .profile import QuestionSpec
    from .service_areas import ServiceAreas


@dataclass
class FieldContext:
    today: date
    areas: Optional["ServiceAreas"] = None


@dataclass
class FieldOutcome:
    """Result of validating one question's value."""

    values: dict[str, Any] = field(default_factory=dict)
    # sensitive raw values (e.g. a full ID number): used for external checks, never stored in the data
    secrets: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_params: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.values)


_NULLISH = {"", "null", "none", "n/a", "unknown"}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip().lower() in _NULLISH:
        return False
    if isinstance(value, (list, tuple)) and not value:
        return False
    return True


def _yes_no_label(value: Any, lang: str) -> str:
    if value is True:
        return "Yes" if lang == "en" else "Sí"
    if value is False:
        return "No"
    return "—"


class FieldHandler:
    type = "base"
    # sensitive handlers parse the raw (unredacted) message and never expose their field to the LLM
    sensitive = False

    def keys(self, q: "QuestionSpec") -> list[str]:
        """Data keys this question writes (primary field first)."""
        return [q.field]

    def schema(self, q: "QuestionSpec") -> dict[str, dict]:
        return {q.field: {"type": ["string", "null"], "description": q.description}}

    def parse_text(self, q: "QuestionSpec", text: str, ctx: FieldContext) -> dict[str, Any]:
        raise NotImplementedError

    def apply(self, q: "QuestionSpec", payload: dict[str, Any], ctx: FieldContext) -> Optional[FieldOutcome]:
        """Validate ``payload`` (extraction keys -> raw values). None if it holds nothing for this question."""
        raise NotImplementedError

    def empty_error(self, q: "QuestionSpec") -> str:
        return "empty"

    def coerce_manual(self, q: "QuestionSpec", key: str, value: Any, ctx: FieldContext) -> dict[str, Any]:
        if value in (None, ""):
            return {key: None}
        return {key: str(value).strip()[:300]}

    def display(self, q: "QuestionSpec", data: dict[str, Any], lang: str) -> str:
        value = data.get(q.field)
        if value in (None, [], ""):
            return "—"
        return str(value)


class NameHandler(FieldHandler):
    type = "name"
    _PREFIX = re.compile(
        r"^(?:me llamo|mi nombre es|soy|my name is|i am|i'm|name is|name:|nombre:)\s+", re.IGNORECASE
    )

    def parse_text(self, q, text, ctx):
        bare = self._PREFIX.sub("", (text or "").strip())
        result = validate_name(bare)
        return {q.field: str(result.value)} if result.ok else {}

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if not _present(raw):
            return None
        result = validate_name(str(raw))
        if result.ok:
            return FieldOutcome(values={q.field: str(result.value)})
        return FieldOutcome(error=str(result.error))

    def coerce_manual(self, q, key, value, ctx):
        text = str(value or "").strip()
        if not text:
            return {key: None}
        return {key: text[:100]}


class YesNoHandler(FieldHandler):
    type = "yes_no"

    def schema(self, q):
        return {q.field: {"type": ["boolean", "null"], "description": q.description}}

    def _from_text(self, q, text: str) -> Optional[bool]:
        if q.number_threshold is not None:
            m = re.search(r"(?<!\d)(\d{1,3})(?!\d)", normalize(text))
            if m:
                return int(m.group(1)) >= q.number_threshold
        return map_yes_no(text)

    def parse_text(self, q, text, ctx):
        value = self._from_text(q, text)
        return {} if value is None else {q.field: value}

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if not _present(raw):
            return None
        if isinstance(raw, bool):
            return FieldOutcome(values={q.field: raw})
        value = self._from_text(q, str(raw))
        if value is None:
            low = str(raw).strip().lower()
            if low in {"true", "yes", "si", "sí"}:
                value = True
            elif low in {"false", "no"}:
                value = False
        if value is None:
            return FieldOutcome(error="unclear_yes_no")
        return FieldOutcome(values={q.field: value})

    def empty_error(self, q):
        return "unclear_yes_no"

    def coerce_manual(self, q, key, value, ctx):
        if value in (None, ""):
            return {key: None}
        if isinstance(value, bool):
            return {key: value}
        low = str(value).strip().lower()
        if low in {"true", "1", "yes", "si", "sí"}:
            return {key: True}
        if low in {"false", "0", "no"}:
            return {key: False}
        raise ValueError("expected yes/no")

    def display(self, q, data, lang):
        return _yes_no_label(data.get(q.field), lang)


class ChoiceHandler(FieldHandler):
    """Single choice among the question's options (first keyword match wins)."""

    type = "choice"

    def schema(self, q):
        return {q.field: {"type": ["string", "null"], "enum": [o.value for o in q.options] + [None],
                          "description": q.description}}

    def match(self, q, text: str) -> list[str]:
        return [o.value for o in q.options if match_keywords(text, [o.value.replace("_", " "), *o.keywords])]

    def parse_text(self, q, text, ctx):
        hits = self.match(q, text)
        return {q.field: hits[0]} if hits else {}

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        valid = {o.value for o in q.options}
        # an option may legitimately be called "none": check options before treating the value as null
        if isinstance(raw, str) and raw.strip().lower() in valid:
            return FieldOutcome(values={q.field: raw.strip().lower()})
        if not _present(raw):
            return None
        value = str(raw).strip().lower()
        if value in valid:
            return FieldOutcome(values={q.field: value})
        hits = self.match(q, str(raw))
        if hits:
            return FieldOutcome(values={q.field: hits[0]})
        return FieldOutcome(error="unknown_option")

    def empty_error(self, q):
        return "unknown_option"

    def coerce_manual(self, q, key, value, ctx):
        if value in (None, ""):
            return {key: None}
        value = str(value).strip()
        if value not in {o.value for o in q.options}:
            raise ValueError(f"unknown option {value!r}")
        return {key: value}

    def display(self, q, data, lang):
        value = data.get(q.field)
        if value in (None, ""):
            return "—"
        return q.option_label(str(value), lang)


class MultiChoiceHandler(ChoiceHandler):
    type = "multi_choice"

    def schema(self, q):
        return {q.field: {"type": ["array", "null"], "items": {"type": "string", "enum": [o.value for o in q.options]},
                          "description": q.description}}

    def parse_text(self, q, text, ctx):
        hits = self.match(q, text)
        return {q.field: hits} if hits else {}

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        valid = {o.value for o in q.options}
        if not _present(raw) and not (isinstance(raw, str) and raw.strip().lower() in valid):
            return None
        items = raw if isinstance(raw, list) else [raw]
        values: list[str] = []
        for item in items:
            value = str(item).strip().lower()
            candidates = [value] if value in valid else self.match(q, str(item))
            for c in candidates:
                if c not in values:
                    values.append(c)
        if not values:
            return FieldOutcome(error="unknown_option")
        return FieldOutcome(values={q.field: values})

    def coerce_manual(self, q, key, value, ctx):
        if value in (None, "", []):
            return {key: []}
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        valid = {o.value for o in q.options}
        bad = [i for i in items if i not in valid]
        if bad:
            raise ValueError(f"unknown options {bad}")
        return {key: list(dict.fromkeys(items))}

    def display(self, q, data, lang):
        values = data.get(q.field) or []
        if not values:
            return "—"
        return ", ".join(q.option_label(str(v), lang) for v in values)


def _parse_number(text: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:[.,]\d+)?", text or "")
    if m:
        return float(m.group(0).replace(",", "."))
    years = parse_years(text)
    return float(years) if years is not None else None


class NumberHandler(FieldHandler):
    type = "number"

    def schema(self, q):
        return {q.field: {"type": ["number", "null"], "description": q.description}}

    def parse_text(self, q, text, ctx):
        value = _parse_number(text)
        return {} if value is None else {q.field: value}

    def _validate(self, q, value: Optional[float]) -> FieldOutcome:
        if value is None:
            return FieldOutcome(error="unparsed_number")
        low = q.min if q.min is not None else float("-inf")
        high = q.max if q.max is not None else float("inf")
        if value < low or value > high:
            return FieldOutcome(error="range")
        return FieldOutcome(values={q.field: int(value) if float(value).is_integer() else value})

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if not _present(raw):
            return None
        value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else _parse_number(str(raw))
        return self._validate(q, value)

    def empty_error(self, q):
        return "unparsed_number"

    def coerce_manual(self, q, key, value, ctx):
        if value in (None, ""):
            return {key: None}
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a number") from exc
        return {key: int(number) if number.is_integer() else number}


class ExperienceHandler(FieldHandler):
    """Years of experience plus optional tags (platforms, trades...) from a vocabulary."""

    type = "experience"

    def keys(self, q):
        return [q.field] + ([q.tags_field] if q.tags_field else [])

    def schema(self, q):
        props = {q.field: {"type": ["integer", "null"], "description": q.description}}
        if q.tags_field:
            hint = ", ".join(list(q.tags)[:12])
            props[q.tags_field] = {
                "type": ["array", "null"], "items": {"type": "string"},
                "description": f"Relevant items the candidate mentioned ({hint}...).",
            }
        return props

    def parse_text(self, q, text, ctx):
        years, tags = parse_experience_fallback(text, q.tags)
        out: dict[str, Any] = {}
        if years is not None:
            out[q.field] = years
        if tags and q.tags_field:
            out[q.tags_field] = tags
        return out

    def _canonical_tags(self, q, raw: Any) -> list[str]:
        items = raw if isinstance(raw, list) else [raw]
        out: list[str] = []
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            mapped = parse_tags(text, q.tags) if q.tags else []
            for tag in mapped or [text.lower()]:
                if tag not in out:
                    out.append(tag)
        return out[:8]

    def apply(self, q, payload, ctx):
        raw_years = payload.get(q.field)
        raw_tags = payload.get(q.tags_field) if q.tags_field else None
        if not _present(raw_years) and not _present(raw_tags):
            return None
        outcome = FieldOutcome()
        if _present(raw_tags):
            outcome.values[q.tags_field] = self._canonical_tags(q, raw_tags)
        if _present(raw_years):
            if isinstance(raw_years, (int, float)) and not isinstance(raw_years, bool):
                years: Optional[int] = int(raw_years)
            else:
                years = parse_years(str(raw_years))
            low = q.min if q.min is not None else 0
            high = q.max if q.max is not None else 50
            result = validate_years(years, low, high)
            if result.ok and years is not None:
                outcome.values[q.field] = years
            else:
                outcome.error = str(result.error)
        return outcome

    def empty_error(self, q):
        return "unparsed_years"

    def coerce_manual(self, q, key, value, ctx):
        if key == q.tags_field:
            if value in (None, "", []):
                return {key: []}
            items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",") if v.strip()]
            return {key: self._canonical_tags(q, items)}
        if value in (None, ""):
            return {key: None}
        try:
            return {key: max(0, int(float(value)))}
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a whole number of years") from exc

    def display(self, q, data, lang):
        years = data.get(q.field)
        tags = data.get(q.tags_field) if q.tags_field else None
        if years is None and not tags:
            return "—"
        text = ""
        if years is not None:
            unit = ("year" if years == 1 else "years") if lang == "en" else ("año" if years == 1 else "años")
            text = f"{years} {unit}"
        if tags:
            text = f"{text} ({', '.join(tags)})" if text else ", ".join(tags)
        return text


class DateHandler(FieldHandler):
    type = "date"

    def schema(self, q):
        return {q.field: {"type": ["string", "null"],
                          "description": f"{q.description} ISO YYYY-MM-DD. Resolve relative dates against today."}}

    def parse_text(self, q, text, ctx):
        parsed = parse_date_text(text, ctx.today)
        return {q.field: parsed.isoformat()} if parsed else {}

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if not _present(raw):
            return None
        parsed = parse_date_text(str(raw), ctx.today)
        result = validate_start_date(parsed, ctx.today, q.max_days_ahead)
        if result.ok:
            return FieldOutcome(values={q.field: result.value.isoformat()})
        return FieldOutcome(error=str(result.error))

    def empty_error(self, q):
        return "unparsed_date"

    def coerce_manual(self, q, key, value, ctx):
        if value in (None, ""):
            return {key: None}
        try:
            return {key: date.fromisoformat(str(value)[:10]).isoformat()}
        except ValueError as exc:
            raise ValueError("expected a YYYY-MM-DD date") from exc


class LocationHandler(FieldHandler):
    """City (+ zone) matched against the profile's service areas."""

    type = "location"

    def keys(self, q):
        return [q.field, q.zone_field, q.country_field, q.coverage_field]

    def schema(self, q):
        return {
            q.field: {"type": ["string", "null"], "description": q.description or "City, as the candidate said it."},
            q.zone_field: {"type": ["string", "null"], "description": "Neighbourhood / district / colonia / barrio if given."},
        }

    def parse_text(self, q, text, ctx):
        clean = (text or "").strip()
        if not clean:
            return {}
        out: dict[str, Any] = {q.field: clean[:80]}
        if "," in clean:
            parts = [p.strip() for p in clean.split(",")]
            out[q.field] = parts[0][:80]
            if len(parts) > 1 and parts[1]:
                out[q.zone_field] = parts[1][:80]
        return out

    def apply(self, q, payload, ctx):
        raw_city = payload.get(q.field)
        if not _present(raw_city):
            return None
        raw_city = str(raw_city).strip()
        raw_zone = payload.get(q.zone_field)
        raw_zone = str(raw_zone).strip() if _present(raw_zone) else None

        if ctx.areas is None:  # no coverage list: accept any city
            return FieldOutcome(values={q.field: raw_city[:80], q.zone_field: raw_zone})

        match = ctx.areas.match_city(raw_city)
        zone_match = ctx.areas.match_city(raw_zone) if raw_zone else None

        if match.matched:
            return FieldOutcome(
                values={q.field: match.city, q.country_field: match.country, q.coverage_field: True,
                        q.zone_field: raw_zone or match.zone or None},
                events=[{"type": "location_match", "payload": {"city": match.city, "reason": match.reason,
                                                               "confidence": round(match.confidence, 2)}}],
            )
        if zone_match and zone_match.matched:
            return FieldOutcome(
                values={q.field: zone_match.city, q.country_field: zone_match.country,
                        q.coverage_field: True, q.zone_field: raw_zone},
                events=[{"type": "location_match", "payload": {"city": zone_match.city, "reason": "zone_alias"}}],
            )
        if match.reason == "ambiguous":
            return FieldOutcome(
                error="ambiguous_city",
                error_params={"options": ", ".join(match.candidates), "value": raw_city},
                events=[{"type": "validation_fail", "payload": {"field": q.field, "error": "ambiguous",
                                                                "options": match.candidates}}],
                flags=[f"ambiguous_location:{','.join(match.candidates)}"],
            )
        return FieldOutcome(
            error="out_of_area",
            error_params={"value": raw_city, "city": raw_city},
            events=[{"type": "validation_fail", "payload": {"field": q.field, "error": "out_of_area", "input": raw_city,
                                                            "best": match.city, "confidence": round(match.confidence, 2)}}],
        )

    def coerce_manual(self, q, key, value, ctx):
        text = str(value or "").strip()
        if key != q.field:
            return {key: text or None}
        if not text:
            return {q.field: None, q.country_field: None, q.coverage_field: None}
        if ctx.areas is None:
            return {q.field: text[:80]}
        match = ctx.areas.match_city(text)
        if match.matched:
            return {q.field: match.city, q.country_field: match.country, q.coverage_field: True}
        return {q.field: text[:80], q.coverage_field: False}

    def display(self, q, data, lang):
        city = data.get(q.field)
        if not city:
            return "—"
        text = str(city)
        if data.get(q.zone_field):
            text += f" ({data[q.zone_field]})"
        if data.get(q.country_field):
            text += f", {data[q.country_field]}"
        return text


class TextHandler(FieldHandler):
    type = "text"

    def parse_text(self, q, text, ctx):
        clean = (text or "").strip()
        return {q.field: clean[:300]} if clean else {}

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if not _present(raw):
            return None
        return FieldOutcome(values={q.field: str(raw).strip()[:300]})


# ---------- identity documents ----------

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_DOC_PATTERNS = (
    ("nie", re.compile(r"(?<![A-Z0-9])([XYZ])[\s.-]?(\d{7})[\s.-]?([A-Z])(?![A-Z0-9])")),
    ("dni", re.compile(r"(?<![A-Z0-9])(\d{2}[.\s]?\d{3}[.\s]?\d{3})[\s-]?([A-Z])(?![A-Z0-9])")),
    ("passport", re.compile(r"(?<![A-Z0-9])([A-Z]{2,3}\d{6,7})(?![A-Z0-9])")),
)
DOC_LABELS = {"dni": "DNI", "nie": "NIE", "passport": "Pasaporte"}


def classify_document(value: str) -> Optional[str]:
    """Document type of a normalised value (no separators, upper case), or None."""
    if re.fullmatch(r"\d{8}[A-Z]", value):
        return "dni"
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", value):
        return "nie"
    if re.fullmatch(r"[A-Z]{2,3}\d{6,7}", value):
        return "passport"
    return None


def document_checksum_ok(value: str, doc_type: str) -> bool:
    """Spanish DNI/NIE control letter (mod 23). Passports have no public checksum."""
    if doc_type == "dni":
        return _DNI_LETTERS[int(value[:8]) % 23] == value[8]
    if doc_type == "nie":
        number = str("XYZ".index(value[0])) + value[1:8]
        return _DNI_LETTERS[int(number) % 23] == value[8]
    return True


def mask_document(value: str) -> str:
    return "•" * max(0, len(value) - 4) + value[-4:]


def find_document(text: str) -> Optional[str]:
    """First identity document in free text, normalised ("12.345.678-z" -> "12345678Z")."""
    upper = (text or "").upper()
    for _, pattern in _DOC_PATTERNS:
        m = pattern.search(upper)
        if m:
            return re.sub(r"[\s.-]", "", "".join(m.groups()))
    return None


class IdDocumentHandler(FieldHandler):
    """DNI / NIE / passport number. Validated locally (format + control letter);
    authenticity is an external, asynchronous check declared in the profile.

    Privacy: the full number is never sent to the LLM, never stored in the
    candidate data (only a masked value + the document type) and the stored
    transcript is PII-redacted. The full value travels only as a ``secret`` to
    the verification payload, which is purged once the check completes.
    """

    type = "id_document"
    sensitive = True

    def keys(self, q):
        return [q.field, q.type_field]

    def schema(self, q):
        return {}  # never exposed to the model

    def parse_text(self, q, text, ctx):
        found = find_document(text)
        if found:
            return {q.field: found}
        compact = re.sub(r"[\s.-]", "", (text or "").upper())
        # looks like an attempt at a number: report a format error rather than "not understood"
        if len(re.findall(r"\d", compact)) >= 6 and len(compact) <= 14:
            return {q.field: compact}
        return {}

    def validate(self, q, raw: Any) -> FieldOutcome:
        value = re.sub(r"[\s.-]", "", str(raw or "").upper())
        doc_type = classify_document(value)
        if doc_type is None or doc_type not in q.documents:
            return FieldOutcome(error="invalid_id", error_params={"options": ", ".join(DOC_LABELS[d] for d in q.documents)})
        if not document_checksum_ok(value, doc_type):
            return FieldOutcome(error="invalid_id_checksum")
        return FieldOutcome(values={q.field: mask_document(value), q.type_field: doc_type},
                            secrets={q.field: value})

    def apply(self, q, payload, ctx):
        raw = payload.get(q.field)
        if not _present(raw):
            return None
        return self.validate(q, raw)

    def empty_error(self, q):
        return "invalid_id"

    def coerce_manual(self, q, key, value, ctx):
        if key == q.type_field:
            raise ValueError("the document type is derived from the number")
        if value in (None, ""):
            return {q.field: None, q.type_field: None}
        outcome = self.validate(q, value)
        if outcome.error:
            raise ValueError("invalid document number" if outcome.error == "invalid_id" else "control letter does not match")
        return dict(outcome.values)

    def display(self, q, data, lang):
        value = data.get(q.field)
        if not value:
            return "—"
        kind = DOC_LABELS.get(str(data.get(q.type_field) or ""), "")
        return f"{kind} {value}".strip()


HANDLERS: dict[str, FieldHandler] = {
    h.type: h
    for h in (NameHandler(), YesNoHandler(), ChoiceHandler(), MultiChoiceHandler(), NumberHandler(),
              ExperienceHandler(), DateHandler(), LocationHandler(), TextHandler(), IdDocumentHandler())
}


def handler_for(q: "QuestionSpec") -> FieldHandler:
    return HANDLERS[q.type]
