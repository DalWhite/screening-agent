from backend.screening.language import detect_language, dominant_language, resolve_language
from backend.screening.schema import Lang


def test_detect_spanish():
    assert detect_language("tengo licencia vigente y vivo en Valencia") is Lang.ES


def test_detect_english():
    assert detect_language("I have a driving license and full availability") is Lang.EN


def test_resolve_follows_latest():
    assert resolve_language(Lang.EN, Lang.ES) is Lang.EN
    assert resolve_language(Lang.ES, Lang.EN) is Lang.ES


def test_resolve_mixed_keeps_conversation():
    assert resolve_language(Lang.MIXED, Lang.EN) is Lang.EN


def test_dominant_language_spanish_tie():
    assert dominant_language([Lang.ES, Lang.EN]) is Lang.ES
    assert dominant_language([Lang.ES, Lang.ES, Lang.EN]) is Lang.ES
    assert dominant_language([Lang.EN, Lang.EN, Lang.ES]) is Lang.EN
