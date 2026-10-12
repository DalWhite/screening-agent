from __future__ import annotations

import re
import unicodedata

from .schema import Lang

_ES_MARKERS = {
    "tengo", "tengo", "mi", "nombre", "me", "llamo", "vivo", "licencia", "si", "no", "disponibilidad",
    "medio", "tiempo", "completo", "fin", "semana", "manana", "tarde", "noche", "flexible", "experiencia",
    "anos", "empezar", "puedo", "cuando", "horas", "trabajo", "trabajar", "repartidor", "repartir",
    "mochila", "bicicleta", "moto", "coche", "barrio", "colonias", "ciudad", "por", "que", "como",
    "cuanto", "dias", "desde", "hasta", "gracias", "hola", "buenas", "vale", "ok", "sip", "nose",
    "prefiero", "turno", "jornada", "inicio", "fecha", "contrato", "seguro", "documentos",
}
_EN_MARKERS = {
    "my", "name", "is", "live", "have", "license", "yes", "no", "availability", "part", "time", "full",
    "weekend", "morning", "afternoon", "evening", "flexible", "experience", "years", "start", "can",
    "when", "work", "working", "delivery", "bike", "car", "zone", "city", "what", "how", "much",
    "days", "from", "until", "thanks", "hello", "hi", "i", "im", "id", "would", "like", "prefer",
    "shift", "contract", "insurance", "documents", "available", "begin",
}

_ES_TOKEN_RE = re.compile(r"\b(qué|cómo|cuándo|días|año|años|corazón|español|española|también|aún)\b", re.IGNORECASE)
_ACCENT_RE = re.compile(r"[áéíóúñÁÉÍÓÚÑüÜ]")


def normalize(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace. Used for matching and language heuristics."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def detect_language(text: str) -> Lang:
    """Cheap, deterministic first-pass language detection (no LLM round-trip)."""
    if not text or not text.strip():
        return Lang.ES
    tokens = normalize(text).split()
    if not tokens:
        return Lang.ES
    es = sum(1 for t in tokens if t in _ES_MARKERS)
    en = sum(1 for t in tokens if t in _EN_MARKERS)
    if _ES_TOKEN_RE.search(text) or _ACCENT_RE.search(text):
        es += 2
    if es == 0 and en == 0:
        return Lang.ES
    if es == 0:
        return Lang.EN
    if en == 0:
        return Lang.ES
    # Both present: code-switching. The dominant language wins; ties go to Spanish.
    if abs(es - en) <= 1:
        return Lang.MIXED
    return Lang.ES if es > en else Lang.EN


def resolve_language(detected: Lang, conversation: Lang) -> Lang:
    """Reply language policy: follow the candidate's latest message, keep Spanish on ties."""
    if detected is Lang.MIXED:
        return conversation if conversation in (Lang.ES, Lang.EN) else Lang.ES
    if detected in (Lang.ES, Lang.EN):
        return detected
    return conversation


def dominant_language(languages: list[Lang]) -> Lang:
    counts = {Lang.ES: 0, Lang.EN: 0}
    for lang in languages:
        if lang is Lang.ES:
            counts[Lang.ES] += 1
        elif lang is Lang.EN:
            counts[Lang.EN] += 1
    return Lang.EN if counts[Lang.EN] > counts[Lang.ES] else Lang.ES
