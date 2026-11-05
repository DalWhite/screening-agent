from backend.screening.profile import get_profile

AREAS = get_profile("delivery_courier").areas


def test_madrid_exact():
    match = AREAS.match_city("Madrid")
    assert match.matched and match.city == "Madrid" and match.country == "ES"


def test_valencia_with_zone():
    match = AREAS.match_city("Valencia, Ruzafa")
    assert match.matched and match.city == "Valencia" and match.country == "ES"


def test_barcelona_spain_hint():
    match = AREAS.match_city("Barcelona, Spain")
    assert match.matched and match.city == "Barcelona" and match.country == "ES"


def test_out_of_area_cities():
    for city in ["Ourense", "Vigo", "Cuenca", "Tarragona", "Pamplona", "Santander"]:
        assert not AREAS.match_city(city).matched, city


def test_salamanca_zone_shadowing_quirk():
    match = AREAS.match_city("Salamanca")
    assert match.matched and match.city == "Madrid"
    assert match.country == "ES"


def test_leon_resolves_to_mexico_quirk():
    match = AREAS.match_city("Leon")
    assert match.matched and match.country == "MX"


def test_mexico_city():
    match = AREAS.match_city("Guadalajara")
    assert match.matched and match.country == "MX"


def test_is_service_area_flag():
    assert AREAS.is_service_area("Sevilla")
    assert not AREAS.is_service_area("Ourense")


def test_partial_words_do_not_match_cities():
    # regression: "vale" (as in "vale, Zaragoza") used to fuzzy-match Valencia
    assert not AREAS.match_city("vale").matched


def test_exact_city_beats_partial_zone():
    construction = get_profile("construction_laborer").areas
    match = construction.match_city("Malaga")
    assert match.matched and match.city == "Málaga" and match.zone is None
    assert construction.match_city("Velez-Malaga").zone == "Velez-Malaga"
