from datetime import date

from backend.screening.schema import Availability, Schedule
from backend.screening.validation import (
    map_availability,
    map_schedule,
    map_yes_no,
    parse_date_text,
    parse_years,
    validate_name,
    validate_start_date,
    validate_years,
)

TODAY = date(2026, 10, 11)


def test_name_valid():
    assert validate_name("Laura Jiménez Ortiz").ok


def test_name_rejects_placeholders():
    assert not validate_name("asdf").ok
    assert not validate_name("no se").ok


def test_name_rejects_too_short():
    assert not validate_name("L").ok


def test_availability_full_time_variants():
    for text in ["jornada completa", "tengo disponibilidad completa", "full time", "todo el día"]:
        assert Availability.FULL_TIME in map_availability(text), text


def test_availability_part_time():
    assert map_availability("medio tiempo") == [Availability.PART_TIME]


def test_availability_weekends():
    assert map_availability("solo fines de semana") == [Availability.WEEKENDS]


def test_availability_unknown():
    assert map_availability("depende") == []


def test_schedule_variants():
    assert map_schedule("por la mañana") is Schedule.MORNING
    assert map_schedule("tarde") is Schedule.AFTERNOON
    assert map_schedule("noche") is Schedule.EVENING
    assert map_schedule("me da igual") is Schedule.FLEXIBLE


def test_schedule_unknown():
    assert map_schedule("cuando quiera") is None


def test_parse_years():
    assert parse_years("2 años") == 2
    assert parse_years("no experience") == 0
    assert parse_years("three years") == 3


def test_parse_date_iso():
    assert parse_date_text("2026-11-02", TODAY) == date(2026, 11, 2)


def test_parse_date_slash():
    assert parse_date_text("15/11/2026", TODAY) == date(2026, 11, 15)


def test_parse_date_next_week():
    assert parse_date_text("la próxima semana", TODAY) == date(2026, 10, 18)


def test_parse_date_weekday():
    assert parse_date_text("el lunes que viene", TODAY) == date(2026, 10, 12)


def test_start_date_rejects_past():
    assert not validate_start_date(date(2026, 1, 1), TODAY).ok


def test_start_date_rejects_far_future():
    assert not validate_start_date(date(2027, 6, 1), TODAY).ok


def test_start_date_accepts_near():
    assert validate_start_date(date(2026, 11, 2), TODAY).ok


def test_validate_years():
    assert validate_years(2).ok
    assert not validate_years(-1).ok


def test_yes_no_affirmative_variants():
    for text in ["sí tengo licencia de coche de moto pues", "sí", "yes", "claro que sí", "tengo carnet"]:
        assert map_yes_no(text) is True, text


def test_yes_no_negative_variants():
    for text in ["no tengo", "no", "none", "no cuento con ella"]:
        assert map_yes_no(text) is False, text


def test_yes_no_first_match_wins():
    assert map_yes_no("sí no sé") is True


def test_yes_no_ambiguous_returns_none():
    assert map_yes_no("depende") is None
    assert map_yes_no("quizá más adelante") is None
    assert map_yes_no("asdf") is None
