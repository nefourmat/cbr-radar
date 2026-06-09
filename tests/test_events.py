"""tests/test_events.py — календарь событий ДКП."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.events import get_upcoming_events, format_calendar


class TestUpcomingEvents:
    def test_returns_sorted_by_date(self):
        evs = get_upcoming_events(date(2026, 6, 9), days=30)
        dates = [e["date"] for e in evs]
        assert dates == sorted(dates)

    def test_all_events_within_window(self):
        today = date(2026, 6, 9)
        evs = get_upcoming_events(today, days=14)
        for e in evs:
            assert today <= e["date"] <= date(2026, 6, 23)

    def test_includes_cbr_meeting_when_in_window(self):
        # Заседание 19.06.2026 должно попасть в окно от 09.06
        evs = get_upcoming_events(date(2026, 6, 9), days=21)
        meetings = [e for e in evs if e["kind"] == "meeting"]
        assert any(e["date"] == date(2026, 6, 19) for e in meetings)
        # Заседание — точное событие высокой важности
        m = next(e for e in meetings if e["date"] == date(2026, 6, 19))
        assert m["confirmed"] is True
        assert m["importance"] == "high"

    def test_weekly_auction_on_wednesdays(self):
        evs = get_upcoming_events(date(2026, 6, 9), days=14)
        auctions = [e for e in evs if e["kind"] == "auction"]
        assert auctions, "ожидаем хотя бы один аукцион"
        for a in auctions:
            assert a["date"].weekday() == 2  # среда
            assert a["confirmed"] is False

    def test_empty_window_returns_empty(self):
        assert get_upcoming_events(date(2026, 6, 9), days=0) == [] or \
               all(e["date"] == date(2026, 6, 9) for e in get_upcoming_events(date(2026, 6, 9), days=0))

    def test_format_calendar_is_string(self):
        out = format_calendar(date(2026, 6, 9), days=21)
        assert isinstance(out, str) and "Календарь" in out
