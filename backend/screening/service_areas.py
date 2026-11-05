from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .language import normalize


@dataclass
class LocationMatch:
    matched: bool
    city: str | None = None
    country: str | None = None
    zone: str | None = None
    confidence: float = 0.0
    candidates: list[str] = field(default_factory=list)
    reason: str = ""


class ServiceAreas:
    """Normalised + fuzzy matcher over a profile's service areas (cities, aliases, zones).

    Matching is deliberately conservative: an ambiguous answer produces a
    clarification question rather than a wrong disqualification.
    """

    def __init__(self, source):
        """``source``: path to a coverage json file ({"locations": [...]}) or the list of locations."""
        if isinstance(source, (str, Path)):
            self.locations = json.loads(Path(source).read_text(encoding="utf-8"))["locations"]
        else:
            self.locations = [dict(loc) for loc in source]
        self._index: dict[str, dict] = {}
        for loc in self.locations:
            names = [loc["city"], *loc.get("aliases", [])]
            for name in names:
                self._index[normalize(name)] = loc
        self._keys = list(self._index.keys())

    @staticmethod
    def _contains_words(haystack: str, needle: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None

    def _score(self, query: str, key: str) -> float:
        if query == key:
            return 1.0
        # containment only on word boundaries: "sevilla ciudad" ~ "sevilla", but "vale" !~ "valencia"
        if self._contains_words(key, query) or self._contains_words(query, key):
            return 0.92 - 0.01 * abs(len(key) - len(query))
        return SequenceMatcher(None, query, key).ratio()

    def match_city(self, text: str) -> LocationMatch:
        q = normalize(text)
        if not q:
            return LocationMatch(False, reason="empty")

        # Country hint lets us disambiguate same-name cities across ES/MX.
        hint = "MX" if re.search(r"\b(mexico|mexico df|cdmx)\b", q) else None
        if hint is None and re.search(r"\b(espana|spain|madrid|barcelona)\b", q):
            hint = "ES"

        scored = sorted(
            ((self._score(q, k), k, loc) for k, loc in self._index.items()),
            key=lambda x: x[0],
            reverse=True,
        )
        if not scored:
            return LocationMatch(False, reason="no_index")

        best_score, best_key, best_loc = scored[0]

        # Zone-level match: candidates often answer with a neighbourhood.
        zone_hits = []
        for loc in self.locations:
            for zone in loc.get("zones", []):
                zk = normalize(zone)
                if q == zk:
                    zone_hits.append((1.0, loc, zone))
                elif q in zk or zk in q:
                    zone_hits.append((0.85, loc, zone))
        exact_city = best_score >= 1.0
        # A partial zone hit ("Malaga" inside "Velez-Malaga") must not shadow an exact city/alias match.
        if zone_hits and (not exact_city or max(z[0] for z in zone_hits) >= 1.0):
            zone_hits.sort(key=lambda x: x[0], reverse=True)
            score, loc, zone = zone_hits[0]
            return LocationMatch(True, loc["city"], loc["country"], zone, score, reason="zone_match")

        if hint:
            filtered = [s for s in scored if s[2]["country"] == hint]
            if filtered:
                best_score, best_key, best_loc = filtered[0]

        if best_score >= 0.90:
            return LocationMatch(True, best_loc["city"], best_loc["country"], None, best_score, reason="exact_or_alias")

        ambiguous = [
            loc["city"] for s, k, loc in scored[:4]
            if s >= 0.80 and loc["city"] != best_loc["city"]
        ]
        if best_score >= 0.80 and len(ambiguous) >= 1:
            return LocationMatch(
                False,
                best_loc["city"], best_loc["country"], None, best_score,
                candidates=sorted(set(ambiguous))[:3], reason="ambiguous",
            )

        if best_score >= 0.74:
            return LocationMatch(True, best_loc["city"], best_loc["country"], None, best_score, reason="fuzzy")

        return LocationMatch(False, best_loc["city"], best_loc["country"], None, best_score, reason="out_of_area")

    def cities(self, country: str | None = None) -> list[str]:
        seen: list[str] = []
        for loc in self.locations:
            if country and loc["country"] != country:
                continue
            if loc["city"] not in seen:
                seen.append(loc["city"])
        return seen

    def is_service_area(self, text: str) -> bool:
        return self.match_city(text).matched

