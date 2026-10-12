from backend.screening.rag import FaqIndex


def test_pay_question_returns_pay_doc():
    index = FaqIndex()
    docs = index.search("¿cuánto pagan por hora?", lang="es", k=2)
    assert docs and docs[0].id == "pay_es"


def test_english_pay_question():
    index = FaqIndex()
    docs = index.search("how much do you pay per delivery?", lang="en", k=2)
    assert docs and docs[0].id == "pay_en"


def test_context_block_returns_ids():
    index = FaqIndex()
    block, ids = index.context_block("horario flexible", lang="es", k=2)
    assert block and ids


def test_irrelevant_query_low_confidence():
    index = FaqIndex()
    docs = index.search("asdf qwerty zxcv", lang="es", k=2)
    assert not docs or docs[0].id != "pay_es"
