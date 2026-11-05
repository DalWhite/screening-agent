"""The real LLM adapter gets a profile-specific extraction tool and persona.

The OpenAI client is faked: we capture what would be sent to the model and
feed back tool-call payloads shaped like real model output.
"""

import json
from datetime import date
from types import SimpleNamespace

from backend.llm.client import OpenAICompatibleLLM
from backend.screening.profile import get_profile
from backend.screening.schema import Intent, Sentiment

TODAY = date(2026, 10, 11)


class FakeCompletions:
    def __init__(self, payload=None, content=None):
        self.payload = payload
        self.content = content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool_calls = None
        if self.payload is not None:
            fn = SimpleNamespace(arguments=json.dumps(self.payload))
            tool_calls = [SimpleNamespace(function=fn)]
        message = SimpleNamespace(tool_calls=tool_calls, content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def make_llm(payload=None, content=None):
    llm = OpenAICompatibleLLM(base_url="http://fake/v1", api_key="x", model="m")
    completions = FakeCompletions(payload, content)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return llm, completions


def test_extraction_tool_is_built_from_the_profile():
    llm, calls = make_llm({"intent": "answer", "language": "es", "sentiment": "neutral"})
    profile = get_profile("construction_laborer")
    llm.extract([{"role": "user", "content": "hola"}], "work_permit", TODAY, profile=profile)
    sent = calls.calls[0]
    props = sent["tools"][0]["function"]["parameters"]["properties"]
    assert {"work_permit", "is_adult", "transport", "construction_trades", "zone"} <= set(props)
    assert "has_license" not in props  # courier-only field
    assert props["transport"]["enum"] == ["own_vehicle", "public_transport", "none", None]
    system = sent["messages"][0]["content"]
    assert "You are Andrés" in system and "Constructora Horizonte" in system
    assert "pending question is about 'work_permit'" in sent["messages"][-1]["content"]


def test_tool_call_payload_is_split_into_meta_and_fields():
    llm, _ = make_llm({
        "intent": "ANSWER", "language": "es", "sentiment": "positive",
        "full_name": "Marta Ruiz", "is_adult": True, "city": "Paterna", "zone": "null",
        "has_license": True,            # not a field of this profile: dropped
        "notes": "  tiene furgoneta ", "candidate_question": "none",
    })
    result = llm.extract([{"role": "user", "content": "..."}], "name", TODAY,
                         profile=get_profile("construction_laborer"))
    assert result.intent is Intent.ANSWER and result.sentiment is Sentiment.POSITIVE
    assert result.fields == {"full_name": "Marta Ruiz", "is_adult": True, "city": "Paterna", "zone": "null"}
    assert result.notes == "tiene furgoneta" and result.candidate_question is None


def test_json_in_content_and_unknown_enums_are_tolerated():
    llm, _ = make_llm(content='```json\n{"intent": "daydreaming", "language": "es", "sentiment": "neutral", "transport": "bus"}\n```')
    result = llm.extract([], "transport", TODAY, profile=get_profile("construction_laborer"))
    assert result.intent is Intent.UNKNOWN
    assert result.fields == {"transport": "bus"}  # validated later by the choice handler


def test_reply_generation_uses_the_profile_persona():
    llm, calls = make_llm(content="¿Eres mayor de 18 años?")
    text = llm.generate(stage="adult", language="es", sentiment=Sentiment.NEUTRAL, plan="Question to ask: x",
                        faq_block="", candidate_message="hola", state="{}",
                        profile=get_profile("construction_laborer"))
    assert text == "¿Eres mayor de 18 años?"
    assert calls.calls[0]["messages"][0]["content"].startswith("You are Andrés")


def test_default_profile_when_none_is_given():
    llm, calls = make_llm({"intent": "answer", "language": "es", "sentiment": "neutral"})
    llm.extract([], "license", TODAY)
    props = calls.calls[0]["tools"][0]["function"]["parameters"]["properties"]
    assert "has_license" in props
