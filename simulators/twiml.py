"""TwiML parsing: what Twilio reads back from our webhooks.

Only the verbs the screening agent emits (plus a few common neighbours) are
modelled. Unknown verbs are kept as ``Unsupported`` so a simulator run can flag
them instead of silently ignoring TwiML that real Twilio would execute.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional, Union


class TwiMLError(ValueError):
    """The webhook answered something Twilio would reject (error 12100 / 12200)."""


def _root(xml: str) -> ET.Element:
    text = (xml or "").strip()
    if not text:
        raise TwiMLError("empty TwiML document (Twilio error 12100)")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TwiMLError(f"malformed TwiML (Twilio error 12100): {exc}") from exc
    if root.tag != "Response":
        raise TwiMLError(f"TwiML root must be <Response>, got <{root.tag}> (Twilio error 12200)")
    return root


def _text(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()


# ---------- messaging ----------

def parse_messaging(xml: str) -> list[str]:
    """Bodies of every ``<Message>`` (``<Message>text</Message>`` or ``<Message><Body>``)."""
    root = _root(xml)
    bodies: list[str] = []
    for node in root:
        if node.tag != "Message":
            continue
        body = node.find("Body")
        bodies.append(_text(body) if body is not None else _text(node))
    return bodies


# ---------- voice ----------

@dataclass
class Say:
    text: str
    language: Optional[str] = None
    voice: Optional[str] = None


@dataclass
class Gather:
    action: Optional[str] = None
    method: str = "POST"
    input: str = "dtmf"
    language: Optional[str] = None
    speech_timeout: Optional[str] = None
    timeout: Optional[str] = None
    says: list[Say] = field(default_factory=list)


@dataclass
class Redirect:
    url: str
    method: str = "POST"


@dataclass
class Hangup:
    pass


@dataclass
class Pause:
    length: float = 1.0


@dataclass
class Unsupported:
    tag: str


Verb = Union[Say, Gather, Redirect, Hangup, Pause, Unsupported]


def _say(node: ET.Element) -> Say:
    return Say(text=_text(node), language=node.get("language"), voice=node.get("voice"))


def parse_voice(xml: str) -> list[Verb]:
    root = _root(xml)
    verbs: list[Verb] = []
    for node in root:
        if node.tag == "Say":
            verbs.append(_say(node))
        elif node.tag == "Gather":
            gather = Gather(
                action=node.get("action"), method=(node.get("method") or "POST").upper(),
                input=node.get("input") or "dtmf", language=node.get("language"),
                speech_timeout=node.get("speechTimeout"), timeout=node.get("timeout"),
            )
            for child in node:
                if child.tag == "Say":
                    gather.says.append(_say(child))
                else:  # Gather only nests <Say>, <Play> and <Pause>
                    gather.says.append(Say(text=f"[{child.tag}]"))
            verbs.append(gather)
        elif node.tag == "Redirect":
            verbs.append(Redirect(url=_text(node), method=(node.get("method") or "POST").upper()))
        elif node.tag == "Hangup":
            verbs.append(Hangup())
        elif node.tag == "Pause":
            verbs.append(Pause(length=float(node.get("length") or 1)))
        else:
            verbs.append(Unsupported(tag=node.tag))
    return verbs
