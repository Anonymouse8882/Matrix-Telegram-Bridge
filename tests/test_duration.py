from bridge.core.duration import format_duration, parse_duration


def test_parse_compound():
    assert parse_duration("1d2h30m") == 86400 + 2 * 3600 + 30 * 60


def test_parse_single_units():
    assert parse_duration("45s") == 45
    assert parse_duration("90m") == 5400
    assert parse_duration("3h") == 10800


def test_parse_zero_and_off():
    assert parse_duration("0") == 0
    assert parse_duration("off") == 0
    assert parse_duration("关闭") == 0


def test_parse_bare_seconds():
    assert parse_duration("300") == 300


def test_parse_invalid():
    assert parse_duration("abc") is None
    assert parse_duration("") is None


def test_format():
    assert format_duration(0) == "关闭"
    assert format_duration(45) == "45秒"
    assert format_duration(5400) == "1小时30分"
    assert format_duration(90000) == "1天1小时"
