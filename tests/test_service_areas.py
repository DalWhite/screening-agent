from backend.screening.service_areas import AREAS


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
