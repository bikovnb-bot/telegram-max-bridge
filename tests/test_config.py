from bridge.config import Route, _parse_routes, serialize_routes


def test_parse_routes_basic():
    routes = _parse_routes("-100123:456:office;-100999:789:home", None, None)
    assert routes == [
        Route(-100123, 456, "office"),
        Route(-100999, 789, "home"),
    ]


def test_parse_routes_without_label():
    routes = _parse_routes("-100123:456", None, None)
    assert routes == [Route(-100123, 456, "")]


def test_parse_routes_empty_falls_back_to_legacy():
    routes = _parse_routes(None, -100123, 456)
    assert routes == [Route(-100123, 456, "default")]


def test_parse_routes_empty_and_no_legacy():
    assert _parse_routes(None, None, None) == []
    assert _parse_routes("", None, None) == []


def test_parse_routes_ignores_malformed_entries():
    routes = _parse_routes("not-a-number:456;;-100123:456:ok", None, None)
    assert routes == [Route(-100123, 456, "ok")]


def test_parse_routes_prefers_routes_over_legacy():
    routes = _parse_routes("-100123:456:x", -999, 111)
    assert routes == [Route(-100123, 456, "x")]


def test_serialize_routes_roundtrip():
    routes = [Route(-100123, 456, "office"), Route(-100999, 789, "")]
    serialized = serialize_routes(routes)
    assert serialized == "-100123:456:office;-100999:789:"
    assert _parse_routes(serialized, None, None) == routes


def test_serialize_empty_routes():
    assert serialize_routes([]) == ""
