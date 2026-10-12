from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional, cast

from openai import OpenAI

from ..config import settings
from ..screening.guardrails import screen_input, wants_human
from ..screening.language import detect_language
from ..screening.prompts import EXTRACTION_TOOL, REPLY_SYSTEM, REPLY_USER_TEMPLATE, SYSTEM_PROMPT
from ..screening.schema import ExtractionResult, Intent, Lang, Sentiment, Stage
from ..screening.validation import (
    map_availability,
    map_schedule,
    parse_date_text,
    parse_years,
    validate_name,
)


class LLMUnavailable(RuntimeError):
    pass


class BaseLLM:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def extract(self, history: list[dict], stage: Stage, today: date) -> ExtractionResult:
        raise NotImplementedError

    def generate(self, stage: Stage, language: str, sentiment: Sentiment, plan: str,
                 faq_block: str, candidate_message: str, state: str) -> str:
        raise NotImplementedError


class OpenAICompatibleLLM(BaseLLM):
    """Talks to any OpenAI-compatible local server (llama.cpp / LM Studio / Strata)."""

    name = "openai-compatible"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = base_url or settings.llm_base_url
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=settings.llm_timeout, max_retries=0)
        self._healthy: Optional[bool] = None

    def available(self) -> bool:
        if self._healthy is not None:
            return self._healthy
        try:
            self.client.models.list()
            self._healthy = True
        except Exception:
            self._healthy = False
        return self._healthy

    def _extra_body(self) -> dict:
        # Qwen3 hybrid thinking switch: off = low latency, on = deeper reasoning.
        return {"chat_template_kwargs": {"thinking": settings.llm_thinking}}

    def extract(self, history: list[dict], stage: Stage, today: date) -> ExtractionResult:
        instruction = (
            f"Today is {today.isoformat()}. Current stage: {stage.value}. "
            "Extract structured data from the candidate's latest message. "
            "If the message contains a question, set intent=question and copy it into candidate_question. "
            "Only report values the candidate actually stated; use null otherwise."
        )
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "system", "content": instruction}]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=cast(Any, messages),
                tools=cast(Any, [EXTRACTION_TOOL]),
                tool_choice="auto",
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                extra_body=self._extra_body(),
            )
        except Exception as exc:
            raise LLMUnavailable(f"extraction call failed: {exc}") from exc

        msg = resp.choices[0].message
        payload: Optional[dict] = None
        if msg.tool_calls:
            raw = cast(Any, msg.tool_calls[0]).function.arguments
            payload = _load_json(raw)
        if payload is None and msg.content:
            payload = _load_json(msg.content)
        if payload is None:
            raise LLMUnavailable("model returned no parseable extraction payload")
        return ExtractionResult.model_validate(_coerce(payload))

    def generate(self, stage: Stage, language: str, sentiment: Sentiment, plan: str,
                 faq_block: str, candidate_message: str, state: str) -> str:
        faq_block = f"FAQ context (use only this):\n{faq_block}" if faq_block else ""
        prompt = REPLY_USER_TEMPLATE.format(
            stage=stage.value, language=language, sentiment=sentiment.value, plan=plan,
            faq_block=faq_block, candidate_message=candidate_message or "(none)", state=state,
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": REPLY_SYSTEM}, {"role": "user", "content": prompt}],
                temperature=settings.llm_temperature,
                max_tokens=320,
                extra_body=self._extra_body(),
            )
        except Exception as exc:
            raise LLMUnavailable(f"reply generation failed: {exc}") from exc
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise LLMUnavailable("model returned an empty reply")
        return text


class MockLLM(BaseLLM):
    """Deterministic rule-based stand-in for the LLM.

    Used by the test suite and as a demo safety net when the local server is
    down, so the product never crashes in front of the interview panel.
    """

    name = "mock"

    def available(self) -> bool:
        return True

    def extract(self, history: list[dict], stage: Stage, today: date) -> ExtractionResult:
        text = ""
        for item in reversed(history):
            if item.get("role") == "user":
                text = item.get("content", "")
                break
        return self._extract_from(text, stage, today)

    def _extract_from(self, text: str, stage: Stage, today: date) -> ExtractionResult:
        screened = screen_input(text)
        clean = screened.sanitized
        lang = detect_language(clean)
        low = clean.lower()

        if screened.reason == "prompt_injection":
            return ExtractionResult(intent=Intent.INJECTION, language=lang)
        if screened.reason == "abusive_language":
            return ExtractionResult(intent=Intent.ABUSE, language=lang)
        if wants_human(clean):
            return ExtractionResult(intent=Intent.QUESTION, language=lang, candidate_question=clean)
        if re.search(r"\b(bye|adi[oó]s|chao|hasta luego|stop|parar|no quiero seguir|never mind)\b", low):
            return ExtractionResult(intent=Intent.END, language=lang)
        if "?" in clean:
            return ExtractionResult(intent=Intent.QUESTION, language=lang, candidate_question=clean)

        result = ExtractionResult(intent=Intent.ANSWER, language=lang)

        if stage is Stage.NAME:
            bare = re.sub(
                r"^(?:me llamo|mi nombre es|soy|my name is|i am|i'm|name is|name:|nombre:)\s+",
                "", clean.strip(), flags=re.IGNORECASE,
            )
            name = validate_name(bare)
            result.full_name = str(name.value) if name.ok else None
        elif stage is Stage.LICENSE:
            if re.search(r"\b(no|negativo|no tengo|ninguna|none)\b", low):
                result.has_license = False
            elif re.search(r"\b(yes|s[ií]|sip|claro|tengo|valid|por supuesto|of course)\b", low):
                result.has_license = True
        elif stage is Stage.LOCATION:
            result.city = clean[:80]
            if "," in clean:
                parts = [p.strip() for p in clean.split(",")]
                result.city = parts[0][:80]
                result.zone = parts[1][:80] if len(parts) > 1 else None
        elif stage is Stage.AVAILABILITY:
            values = map_availability(clean)
            result.availability = [v.value for v in values] or None
        elif stage is Stage.SCHEDULE:
            sched = map_schedule(clean)
            result.preferred_schedule = sched.value if sched else None
        elif stage is Stage.EXPERIENCE:
            result.experience_years = parse_years(clean)
            platforms = re.findall(r"\b(glovo|uber eats|uber|deliveroo|just eat|justeat|riders|stuart|wolt|doordash|ubereats)\b", low)
            result.experience_platforms = sorted(set(platforms)) or None
            if result.experience_years is None and re.search(r"\b(experience|experiencia|repart|deliver)\b", low):
                result.experience_years = 0
        elif stage is Stage.START_DATE:
            parsed = parse_date_text(clean, today)
            result.start_date = parsed.isoformat() if parsed else None
        return result

    def generate(self, stage: Stage, language: str, sentiment: Sentiment, plan: str,
                 faq_block: str, candidate_message: str, state: str) -> str:
        if plan.startswith("Write one line"):
            return self._summary_note(plan, language)
        out: list[str] = []
        if faq_block:
            first = faq_block.strip().splitlines()[0] if faq_block.strip() else ""
            first = re.sub(r"^-\s*\[[^\]]+\]\s*", "", first).strip()
            if first:
                out.append(first)
        m = re.search(r"(?:Question to ask|Pending question|Say):\s*(.+)", plan)
        if m:
            out.append(m.group(1).strip())
        return "\n\n".join(out) if out else plan

    def _summary_note(self, plan: str, language: str) -> str:
        import ast

        m = re.search(r"Data:\s*(\{.*\})", plan, re.DOTALL)
        if not m:
            return ""
        try:
            data = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            return ""
        parts: list[str] = []
        if data.get("full_name"):
            parts.append(str(data["full_name"]))
        if data.get("city"):
            loc = data["city"]
            if data.get("zone"):
                loc += f" ({data['zone']})"
            if data.get("country"):
                loc += f", {data['country']}"
            parts.append(loc)
        avail = data.get("availability") or []
        if language == "en":
            avail_map = {"full_time": "full-time", "part_time": "part-time", "weekends": "weekends"}
            sched_map = {"morning": "morning shift", "afternoon": "afternoon shift", "evening": "evening shift", "flexible": "flexible schedule"}
        else:
            avail_map = {"full_time": "jornada completa", "part_time": "medio tiempo", "weekends": "fines de semana"}
            sched_map = {"morning": "turno mañana", "afternoon": "turno tarde", "evening": "turno noche", "flexible": "horario flexible"}
        if avail:
            parts.append(", ".join(avail_map.get(str(a), str(a)) for a in avail))
        sched = data.get("preferred_schedule")
        if sched:
            parts.append(sched_map.get(str(sched), str(sched)))
        years = data.get("experience_years")
        if years is not None:
            platforms = ", ".join(data.get("experience_platforms") or [])
            label = f"{years} yrs experience" if language == "en" else f"{years} años de experiencia"
            if platforms:
                label += f" ({platforms})"
            parts.append(label)
        if data.get("start_date"):
            parts.append(f"available from {data['start_date']}" if language == "en" else f"disponible desde {data['start_date']}")
        if data.get("has_license"):
            parts.append("valid driving license" if language == "en" else "licencia de conducir vigente")
        return " — ".join(parts)[:200]


def _load_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _coerce(payload: dict) -> dict:
    """Normalise common model quirks before pydantic validation."""
    out = dict(payload)
    for key in ("intent", "language", "sentiment"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = value.strip().lower()
    for key in ("full_name", "city", "zone", "candidate_question", "notes", "start_date", "preferred_schedule"):
        value = out.get(key)
        if isinstance(value, str):
            value = value.strip()
            out[key] = None if value in {"", "null", "none", "n/a", "unknown"} else value
    if isinstance(out.get("has_license"), str):
        low = out["has_license"].strip().lower()
        out["has_license"] = low in {"true", "yes", "si", "sí"}
    if isinstance(out.get("experience_years"), str):
        out["experience_years"] = parse_years(out["experience_years"])
    if isinstance(out.get("availability"), str):
        out["availability"] = [v.value for v in map_availability(out["availability"])] or None
    if isinstance(out.get("experience_platforms"), str):
        out["experience_platforms"] = [out["experience_platforms"]]
    return out


def build_llm() -> BaseLLM:
    if settings.llm_fallback == "mock":
        return MockLLM()
    primary = OpenAICompatibleLLM()
    if primary.available():
        return primary
    if settings.llm_fallback == "off":
        raise LLMUnavailable(f"no LLM server at {settings.llm_base_url} and fallback is disabled")
    return MockLLM()
