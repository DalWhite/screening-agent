from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..config import settings
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
    """Normalised + fuzzy matcher over the client's 45 service areas.

    Matching is deliberately conservative: an ambiguous answer produces a
    clarification question rather than a wrong disqualification.
    """

    def __init__(self, path=None):
        path = path or settings.data_dir / "service_areas.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.locations = raw["locations"]
        self._index: dict[str, dict] = {}
        for loc in self.locations:
            names = [loc["city"], *loc.get("aliases", [])]
            for name in names:
                self._index[normalize(name)] = loc
        self._keys = list(self._index.keys())

    def _score(self, query: str, key: str) -> float:
        if query == key:
            return 1.0
        if query in key or key in query:
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
        if zone_hits:
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


AREAS = ServiceAreas()
