from pathlib import Path

from backend.config import settings
from backend.screening.rag import FaqIndex

COURIER_FAQ = Path(settings.profiles_dir) / "delivery_courier" / "faq.json"


def test_pay_question_returns_pay_doc():
    index = FaqIndex(COURIER_FAQ)
    docs = index.search("¿cuánto pagan por hora?", lang="es", k=2)
    assert docs and docs[0].id == "pay_es"


def test_english_pay_question():
    index = FaqIndex(COURIER_FAQ)
    docs = index.search("how much do you pay per delivery?", lang="en", k=2)
    assert docs and docs[0].id == "pay_en"


def test_context_block_returns_ids():
    index = FaqIndex(COURIER_FAQ)
    block, ids = index.context_block("horario flexible", lang="es", k=2)
    assert block and ids


def test_irrelevant_query_low_confidence():
    index = FaqIndex(COURIER_FAQ)
    docs = index.search("asdf qwerty zxcv", lang="es", k=2)
    assert not docs or docs[0].id != "pay_es"
