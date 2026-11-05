"""Human labels for flags and reasons produced by the engine (rule flags come from profiles)."""

from __future__ import annotations

from typing import Optional

from ..screening.profile import JobProfile, localize, registry

SYSTEM_FLAGS: dict[str, dict[str, str]] = {
    "needs_review": {"es": "Revisión necesaria", "en": "Needs review"},
    "requested_human_callback": {"es": "Pidió hablar con una persona", "en": "Asked for a human"},
    "unresolved": {"es": "Respuesta sin validar", "en": "Unresolved answer"},
    "waitlist_out_of_area": {"es": "Lista de espera (zona)", "en": "Area waitlist"},
    "ambiguous_location": {"es": "Ubicación ambigua", "en": "Ambiguous location"},
    "abusive_language": {"es": "Lenguaje abusivo", "en": "Abusive language"},
    "self_harm": {"es": "Riesgo personal (atender)", "en": "Personal risk (follow up)"},
    "silence": {"es": "Abandono por silencio", "en": "Went silent"},
    "llm_unavailable": {"es": "Modelo no disponible", "en": "Model unavailable"},
    "check_error": {"es": "Verificación fallida", "en": "Check failed"},
}

STATUS_LABELS: dict[str, dict[str, str]] = {
    "active": {"es": "En curso", "en": "In progress"},
    "qualified": {"es": "Pre-calificado", "en": "Pre-qualified"},
    "disqualified": {"es": "No califica", "en": "Disqualified"},
    "abandoned": {"es": "Incompleto", "en": "Incomplete"},
    "verifying": {"es": "En verificación", "en": "Verifying"},
}

CHECK_STATUS_LABELS: dict[str, dict[str, str]] = {
    "not_requested": {"es": "No solicitada", "en": "Not requested"},
    "queued": {"es": "En cola", "en": "Queued"},
    "submitted": {"es": "En curso", "en": "In progress"},
    "pending": {"es": "Pendiente", "en": "Pending"},
    "valid": {"es": "Válido", "en": "Valid"},
    "invalid": {"es": "No válido", "en": "Invalid"},
    "error": {"es": "Error", "en": "Error"},
    "superseded": {"es": "Sustituida", "en": "Superseded"},
}

PROVIDER_LABELS: dict[str, dict[str, str]] = {
    "mock_id_registry": {"es": "Registro de identidad (simulado)", "en": "ID registry (mock)"},
    "webhook": {"es": "Servicio externo (webhook)", "en": "External service (webhook)"},
    "manual": {"es": "Manual (reclutador)", "en": "Manual (recruiter)"},
}

SOURCE_LABELS: dict[str, dict[str, str]] = {
    "web": {"es": "Chat web", "en": "Web chat"},
    "whatsapp": {"es": "WhatsApp", "en": "WhatsApp"},
    "voice": {"es": "Teléfono", "en": "Phone"},
    "backoffice": {"es": "Alta manual", "en": "Added manually"},
}


def flag_label(flag: str, profile: Optional[JobProfile] = None, lang: str = "es") -> str:
    base, _, detail = flag.partition(":")
    profiles = [profile] if profile is not None else registry().all()
    for p in profiles:
        for rule in p.rules:
            if flag == rule.id or flag in rule.flags:
                return localize(rule.label, lang, flag)
    if base in SYSTEM_FLAGS:
        label = localize(SYSTEM_FLAGS[base], lang, flag)
        if detail and profile is not None:
            q = profile.question(detail)
            check = profile.check(detail)
            detail = localize(q.label, lang, detail) if q else localize(check.label, lang, detail) if check else detail
        return f"{label}: {detail}" if detail else label
    return flag


def all_flag_labels(lang: str = "es") -> dict[str, str]:
    labels = {k: localize(v, lang, k) for k, v in SYSTEM_FLAGS.items()}
    for p in registry().all():
        for rule in p.rules:
            labels.setdefault(rule.id, localize(rule.label, lang, rule.id))
            for flag in rule.flags:
                labels.setdefault(flag, localize(rule.label, lang, flag))
    return labels
