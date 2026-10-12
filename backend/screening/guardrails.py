from __future__ import annotations

import re
from dataclasses import dataclass

MAX_INPUT_CHARS = 600

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above|earlier) (instructions|rules|prompts)",
    r"olvida (todas )?(las )?(instrucciones|reglas|prompts) (anteriores|previas)",
    r"you are now (a|an)",
    r"act[uú]a como si (no )?(tuvieras|fueras)",
    r"reveal (your )?(system|hidden) (prompt|instructions)",
    r"mu[eé]strame (tu )?(prompt|instrucciones) (de )?(sistema|oculto)",
    r"developer mode|modo desarrollador",
    r"jailbreak|DAN\b",
    r"system prompt",
]

_ABUSE_PATTERNS = [
    r"\b(fuck|shit|bitch|asshole|whore|cunt)\b",
    r"\b(mierda|cabr[oó]n|put[a] |gilipollas|imb[eé]cil|hijo de puta|joder|cojones)\b",
    r"\b(idiot|moron|stupid|dumb)\b",
    r"\b(est[uú]pido|idiota|tonto|in[uú]til)\b",
    r"\b(kill yourself|go die)\b",
]

_SELF_HARM = [r"\b(suicide|kill myself|end my life)\b", r"\b(suicidio|matarme|quitarme la vida)\b"]

_CONTACT_REQUEST = [r"\b(human|recruiter|real person|persona real|hablar con alguien|una persona)\b"]

_LONG_DIGITS = re.compile(r"(?<!\d)\d[\d\s-]{6,}\d(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DATE_LIKE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2}[/.]\d{4}")


@dataclass
class GuardrailResult:
    action: str  # "ok" | "redirect" | "refuse" | "end" | "truncate"
    reason: str
    sanitized: str


def screen_input(text: str) -> GuardrailResult:
    """Deterministic pre-LLM guardrail pass. Cheap, always runs, never trusts the model."""
    original = text or ""
    sanitized = original.strip()

    if len(sanitized) > MAX_INPUT_CHARS:
        sanitized = sanitized[:MAX_INPUT_CHARS].rstrip() + " ..."
        truncated = True
    else:
        truncated = False

    low = sanitized.lower()

    for pattern in _SELF_HARM:
        if re.search(pattern, low):
            return GuardrailResult("end", "self_harm", sanitized)

    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, low):
            return GuardrailResult("refuse", "prompt_injection", sanitized)

    for pattern in _ABUSE_PATTERNS:
        if re.search(pattern, low):
            return GuardrailResult("redirect", "abusive_language", sanitized)

    if re.fullmatch(r"[^a-záéíóúñü0-9\s]*", low) and len(low.strip()) > 0:
        return GuardrailResult("redirect", "no_meaningful_content", sanitized)

    if truncated:
        return GuardrailResult("truncate", "input_too_long", sanitized)

    return GuardrailResult("ok", "clean", sanitized)


def wants_human(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low) for p in _CONTACT_REQUEST)


def redact_pii(text: str) -> str:
    """Strip identifiers we do not need for screening (DNI/NIE/SSN/phone/card numbers, emails).

    Date-like substrings (ISO or slash/dot dates) are protected first so that a
    candidate typing "2026-11-02" as their start date is not mangled into [id redacted].
    """
    out = _EMAIL.sub("[email redacted]", text or "")
    protected: list[str] = []

    def _protect(match: "re.Match[str]") -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    out = _DATE_LIKE.sub(_protect, out)
    out = _LONG_DIGITS.sub("[id redacted]", out)
    for i, value in enumerate(protected):
        out = out.replace(f"\x00{i}\x00", value)
    return out


def boundary_message(reason: str, lang: str) -> str:
    messages = {
        "abusive_language": {
            "es": "Prefiero que hablemos con respeto. Si quieres seguir con la candidatura, dime tu nombre y continuamos.",
            "en": "Let's keep this respectful. If you'd like to continue with the application, tell me your name and we'll carry on.",
        },
        "prompt_injection": {
            "es": "Solo puedo ayudarte con esta candidatura. Volvamos a ella: {next}",
            "en": "I can only help with this application. Let's get back to it: {next}",
        },
        "no_meaningful_content": {
            "es": "No he entendido eso. {next}",
            "en": "I didn't follow that. {next}",
        },
        "input_too_long": {
            "es": "Tu mensaje es muy largo. {next}",
            "en": "That message is too long. {next}",
        },
        "off_topic": {
            "es": "Eso esta fuera de lo que puedo ayudarte. {next}",
            "en": "That is outside what I can help with. {next}",
        },
        "self_harm": {
            "es": "Lamento que estes pasando por esto. No soy un servicio de emergencia. En Espana llama al 024, en Mexico al 800 253 7442. Cerramos esta conversacion.",
            "en": "I'm sorry you're going through this. I'm not an emergency service. In Spain call 024, in Mexico 800 253 7442. I'm closing this conversation.",
        },
    }
    return messages.get(reason, messages["off_topic"]).get(lang, messages["off_topic"]["es"])
