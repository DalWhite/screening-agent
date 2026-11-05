from __future__ import annotations

import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any, Optional, cast

from openai import OpenAI

from ..config import settings
from ..screening.guardrails import screen_input, wants_human
from ..screening.language import detect_language
from ..screening.prompts import REPLY_USER_TEMPLATE, extraction_tool, reply_system, system_prompt
from ..screening.schema import META_KEYS, ExtractionResult, Intent, Sentiment

if TYPE_CHECKING:  # pragma: no cover
    from ..screening.profile import JobProfile


class LLMUnavailable(RuntimeError):
    pass


class BaseLLM:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def extract(self, history: list[dict], stage: str, today: date,
                profile: Optional["JobProfile"] = None) -> ExtractionResult:
        raise NotImplementedError

    def generate(self, stage: str, language: str, sentiment: Sentiment, plan: str,
                 faq_block: str, candidate_message: str, state: str,
                 profile: Optional["JobProfile"] = None) -> str:
        raise NotImplementedError


def _profile(profile: Optional["JobProfile"]) -> "JobProfile":
    if profile is not None:
        return profile
    from ..screening.profile import get_profile

    return get_profile()


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

    def extract(self, history: list[dict], stage: str, today: date,
                profile: Optional["JobProfile"] = None) -> ExtractionResult:
        profile = _profile(profile)
        question = profile.question(stage)
        pending = f" The pending question is about '{question.field}' ({question.description})." if question else ""
        instruction = (
            f"Today is {today.isoformat()}. Current stage: {stage}.{pending} "
            "Extract structured data from the candidate's latest message. "
            "If the message contains a question, set intent=question and copy it into candidate_question. "
            "Only report values the candidate actually stated; use null otherwise."
        )
        messages = [{"role": "system", "content": system_prompt(profile)}] + history + [{"role": "system", "content": instruction}]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=cast(Any, messages),
                tools=cast(Any, [extraction_tool(profile)]),
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
        return _to_extraction(payload, profile)

    def generate(self, stage: str, language: str, sentiment: Sentiment, plan: str,
                 faq_block: str, candidate_message: str, state: str,
                 profile: Optional["JobProfile"] = None) -> str:
        profile = _profile(profile)
        faq_block = f"FAQ context (use only this):\n{faq_block}" if faq_block else ""
        prompt = REPLY_USER_TEMPLATE.format(
            stage=stage, language=language, sentiment=sentiment.value, plan=plan,
            faq_block=faq_block, candidate_message=candidate_message or "(none)", state=state,
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": reply_system(profile)}, {"role": "user", "content": prompt}],
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

    Used by the test suites and as a demo safety net when the local server is
    down, so the product never crashes in front of a candidate. Extraction is
    profile-driven: the pending question's field handler parses the raw text.
    """

    name = "mock"

    def available(self) -> bool:
        return True

    def extract(self, history: list[dict], stage: str, today: date,
                profile: Optional["JobProfile"] = None) -> ExtractionResult:
        text = ""
        for item in reversed(history):
            if item.get("role") == "user":
                text = item.get("content", "")
                break
        return self._extract_from(text, stage, today, _profile(profile))

    def _extract_from(self, text: str, stage: str, today: date, profile: "JobProfile") -> ExtractionResult:
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
        question = profile.question(stage)
        if question is not None:
            result.fields = question.handler.parse_text(question, clean, profile.context(today))
        return result

    def generate(self, stage: str, language: str, sentiment: Sentiment, plan: str,
                 faq_block: str, candidate_message: str, state: str,
                 profile: Optional["JobProfile"] = None) -> str:
        if plan.startswith("Write one line"):
            return self._summary_note(plan, language, _profile(profile))
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

    def _summary_note(self, plan: str, language: str, profile: "JobProfile") -> str:
        import ast

        m = re.search(r"Data:\s*(\{.*\})", plan, re.DOTALL)
        if not m:
            return ""
        try:
            data = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            return ""
        return summary_line(profile, data, language)


def summary_line(profile: "JobProfile", data: dict, language: str) -> str:
    """Deterministic one-line recruiter summary built from the profile's questions."""
    from ..screening.profile import localize

    parts: list[str] = []
    for q in profile.questions:
        value = q.handler.display(q, data, language)
        if value == "—":
            continue
        if q.type == "yes_no":
            parts.append(f"{localize(q.label, language, q.id)} {'✓' if data.get(q.field) else '✗'}")
        elif q.type == "date":
            parts.append(f"{'from' if language == 'en' else 'desde'} {value}")
        else:
            parts.append(value)
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


def _to_extraction(payload: dict, profile: "JobProfile") -> ExtractionResult:
    """Split a tool-call payload into meta keys and profile fields, normalising model quirks."""
    meta: dict[str, Any] = {}
    for key in ("intent", "language", "sentiment"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            meta[key] = value.strip().lower()
    for key in ("candidate_question", "notes"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip().lower() not in {"", "null", "none", "n/a", "unknown"}:
            meta[key] = value.strip()
    # only answer fields: the model can never set a sensitive value (ID number) or an external check result
    known = set(profile.question_keys()) - profile.sensitive_keys()
    fields = {k: v for k, v in payload.items() if k in known and k not in META_KEYS and v is not None}
    try:
        return ExtractionResult.model_validate({**meta, "fields": fields})
    except ValueError:
        # unknown enum values from the model: keep the data, default the meta
        return ExtractionResult(fields=fields, candidate_question=meta.get("candidate_question"), notes=meta.get("notes"))


def build_llm() -> BaseLLM:
    if settings.llm_fallback == "mock":
        return MockLLM()
    primary = OpenAICompatibleLLM()
    if primary.available():
        return primary
    if settings.llm_fallback == "off":
        raise LLMUnavailable(f"no LLM server at {settings.llm_base_url} and fallback is disabled")
    return MockLLM()
