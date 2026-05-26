"""Unit tests for windows.current_own_session_day."""
from datetime import date, datetime, timezone
import pytest
import windows

UTC = timezone.utc


def dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


@pytest.mark.parametrize("now,expected", [
    (dt(2026, 4, 10, 18, 0),  date(2026, 4,  9)),
    (dt(2026, 4, 10, 19, 0),  date(2026, 4,  9)),
    (dt(2026, 4, 10, 19, 1),  date(2026, 4, 10)),
    (dt(2026, 4, 10, 23, 59), date(2026, 4, 10)),
    (dt(2026, 4, 11,  0,  0), date(2026, 4, 10)),
    (dt(2026, 4, 11,  3, 15), date(2026, 4, 10)),
    (dt(2026, 4, 11,  6, 59), date(2026, 4, 10)),
    (dt(2026, 4, 11,  7,  0), date(2026, 4, 10)),
    (dt(2026, 4, 11,  7,  1), date(2026, 4, 10)),
    (dt(2026, 4, 11, 12,  0), date(2026, 4, 10)),
    (dt(2026, 4, 11, 18, 59), date(2026, 4, 10)),
    (dt(2026, 4, 11, 19,  1), date(2026, 4, 11)),
])
def test_current_own_session_day(now, expected):
    assert windows.current_own_session_day(now) == expected


def test_current_own_session_day_requires_utc():
    with pytest.raises(AssertionError):
        windows.current_own_session_day(datetime(2026, 4, 10, 19, 0))
