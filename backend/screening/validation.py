from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from dateutil import parser as dateparser

from .language import normalize

_NAME_RE = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-\s]{1,98}[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]?$")
_PLACEHOLDER_NAMES = {"asdf", "qwe", "aaa", "test", "prueba", "no se", "nose", "ninguno", "none", "n/a", "xxx"}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6,
}


@dataclass
class FieldResult:
    ok: bool
    value: Any = None
    error: str | None = None


def validate_name(text: str) -> FieldResult:
    raw = (text or "").strip()
    norm = normalize(raw)
    if not norm:
        return FieldResult(False, error="empty")
    if norm in _PLACEHOLDER_NAMES:
        return FieldResult(False, error="placeholder")
    if not _NAME_RE.match(raw):
        return FieldResult(False, error="format")
    if len(norm.split()) < 2:
        return FieldResult(False, error="single_word")
    return FieldResult(True, raw[:100])


_NO_RE = re.compile(r"\b(no|none|negativo|ninguna|ninguno|nunca|do not|dont|don't|negative|nada)\b")
_YES_RE = re.compile(r"\b(si|yes|yep|yeah|yup|sip|claro|correcto|afirmativo|por supuesto|tengo|cuento|dispongo|of course)\b")


def map_yes_no(text: str) -> Optional[bool]:
    norm = normalize(text)
    if not norm:
        return None
    no_m = _NO_RE.search(norm)
    yes_m = _YES_RE.search(norm)
    if no_m and yes_m:
        return yes_m.start() < no_m.start()
    if no_m:
        return False
    if yes_m:
        return True
    return None


def match_keywords(text: str, keywords: list[str]) -> bool:
    """True if any keyword appears in the text as a whole word/phrase (accent- and case-insensitive)."""
    norm = normalize(text)
    if not norm:
        return False
    for keyword in keywords:
        key = normalize(keyword)
        if key and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", norm):
            return True
    return False


def parse_tags(text: str, vocabulary: dict[str, list[str]]) -> list[str]:
    """Canonical tags mentioned in free text, in order of appearance.

    ``vocabulary`` maps a canonical tag to its aliases, e.g.
    ``{"glovo": ["glovo", "globo"], "albanileria": ["albanil", "albañilería"]}``.
    """
    norm = normalize(text)
    hits: list[tuple[int, str]] = []
    for tag, aliases in vocabulary.items():
        best: Optional[int] = None
        for alias in [tag, *aliases]:
            key = normalize(alias)
            if not key:
                continue
            m = re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", norm)
            if m and (best is None or m.start() < best):
                best = m.start()
        if best is not None:
            hits.append((best, tag))
    return [tag for _, tag in sorted(hits)]


_EXPERIENCE_HINT_RE = re.compile(r"\b(experiencia|experience|trabaj|worked|repart|deliver|delivery|rider|obra|construcci)\w*")


def parse_experience_fallback(text: str, vocabulary: Optional[dict[str, list[str]]] = None) -> tuple[Optional[int], list[str]]:
    """Raw-text fallback for experience questions when the LLM extraction fails.

    Returns (years, tags). Mentioning a known tag (platform, trade...) or an
    experience keyword without a number counts as 0 years rather than unknown.
    """
    years = parse_years(text)
    tags = parse_tags(text, vocabulary or {})
    if years is None and (tags or _EXPERIENCE_HINT_RE.search(normalize(text)) or map_yes_no(text) is not None):
        years = 0
    return years, tags


def parse_years(text: str) -> Optional[int]:
    norm = normalize(text)
    if re.search(r"\bno experience\b|\bsin experiencia\b|\bnone\b|\bninguna\b|\bzero\b|\b0\b", norm):
        return 0
    match = re.search(r"(\d{1,2})\s*(?:years?|anos?|yrs?)?", norm)
    if match:
        return max(0, min(int(match.group(1)), 50))
    if re.search(r"\bun\s+an[oa]s?\b|\bone\s+year\b", norm):
        return 1
    word_numbers = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    }
    for word, num in word_numbers.items():
        if re.search(rf"\b{word}\b", norm):
            return num
    return None


def parse_date_text(text: str, today: Optional[date] = None) -> Optional[date]:
    """Parse natural-language start dates in ES/EN into an ISO date."""
    today = today or date.today()
    norm = normalize(text)
    if not norm:
        return None

    if re.search(r"\bimmediately\b|\bhoy\b|\btoday\b|\basap\b|\bmanana\b|\btomorrow\b|\bright away\b|"
                 r"\binmediat\w*|\bcuanto antes\b|\bya mismo\b|\bahora mismo\b", norm):
        return today if re.search(r"\bhoy\b|\btoday\b", norm) else today + timedelta(days=1)

    if re.search(r"\bnext week\b|\bproxima semana\b|\bla proxima semana\b", norm):
        return today + timedelta(days=7)

    if re.search(r"\bthis week\b|\besta semana\b", norm):
        return today + timedelta(days=max(1, 6 - today.weekday()))

    relative = re.search(
        r"\b(?:in|en|dentro de|within)\s+(\d{1,2}|un|una|one|a|dos|two|tres|three|cuatro|four)\s*"
        r"(days?|weeks?|months?|dias?|semanas?|mes|meses)\b",
        norm,
    )
    if relative:
        words = {"un": 1, "una": 1, "one": 1, "a": 1, "dos": 2, "two": 2, "tres": 3, "three": 3, "cuatro": 4, "four": 4}
        raw_n = relative.group(1)
        n = int(raw_n) if raw_n.isdigit() else words[raw_n]
        unit = relative.group(2)
        if unit.startswith(("day", "dia")):
            return today + timedelta(days=n)
        if unit.startswith(("month", "mes")):
            return today + timedelta(days=30 * n)
        return today + timedelta(days=n * 7)

    weekday = next((d for w, d in _WEEKDAYS.items() if re.search(rf"\b{w}\b", norm)), None)
    if weekday is not None:
        delta = (weekday - today.weekday()) % 7
        return today + timedelta(days=delta or 7)

    iso_full = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", norm)
    if iso_full:
        y, m, d = (int(x) for x in iso_full.groups())
        try:
            return date(y, m, d)
        except ValueError:
            return None

    iso = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", norm)
    if iso:
        d, m, y = (int(x) for x in iso.groups())
        if y < 100:
            y += 2000
        try:
            return date(y, m, d)
        except ValueError:
            return None

    try:
        parsed = dateparser.parse(text, default=datetime.combine(today, datetime.min.time()), fuzzy=True)
        return parsed.date()
    except (ValueError, TypeError):
        return None


def validate_start_date(value: Optional[date], today: Optional[date] = None, max_days_ahead: int = 180) -> FieldResult:
    today = today or date.today()
    if value is None:
        return FieldResult(False, error="unparsed_date")
    if value < today:
        return FieldResult(False, error="past_date")
    if value > today + timedelta(days=max_days_ahead):
        return FieldResult(False, error="too_far")
    return FieldResult(True, value)


def validate_years(value: Optional[int], low: float = 0, high: float = 50) -> FieldResult:
    if value is None:
        return FieldResult(False, error="unparsed_years")
    if value < low or value > high:
        return FieldResult(False, error="range")
    return FieldResult(True, value)
