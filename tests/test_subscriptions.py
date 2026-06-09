"""tests/test_subscriptions.py — хранилище подписок пользователей."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import subscriptions as subs


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Изолированное хранилище подписок во временной папке."""
    monkeypatch.setattr(subs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(subs, "STORE", tmp_path / "subscribers.json")
    return subs


class TestSubscriptions:
    def test_register_applies_defaults(self, store):
        s = store.register(111, "Иван")
        assert s["meetings"] is True          # default on
        assert s["entry_signal"] is True
        assert s["weekly_digest"] is True
        assert s["daily_pulse"] is False      # default off
        assert s["auctions"] is False

    def test_toggle_flips_value(self, store):
        store.register(111, "Иван")
        assert store.toggle(111, "daily_pulse") is True
        assert store.toggle(111, "daily_pulse") is False

    def test_toggle_unknown_key_raises(self, store):
        with pytest.raises(KeyError):
            store.toggle(111, "nonexistent")

    def test_subscribers_for_returns_opted_in(self, store):
        store.register(111, "A")
        store.register(222, "B")
        store.toggle(111, "daily_pulse")     # 111 -> on
        pulse_subs = store.subscribers_for("daily_pulse")
        assert 111 in pulse_subs and 222 not in pulse_subs
        # both are on meetings by default
        assert set(store.subscribers_for("meetings")) == {111, 222}

    def test_remove_blocked_user(self, store):
        store.register(111, "A")
        store.remove(111)
        assert store.subscribers_for("meetings") == []
        assert store.count() == 0

    def test_get_subs_unknown_returns_defaults(self, store):
        s = store.get_subs(999)
        assert s["meetings"] is True and s["daily_pulse"] is False

    def test_persists_across_reload(self, store):
        store.register(111, "A")
        store.toggle(111, "inflation")
        # перечитываем из файла
        assert store.get_subs(111)["inflation"] is True

    def test_normalize_adds_missing_keys(self, store):
        # старая запись без новых ключей
        store._save({"111": {"name": "old", "subs": {"meetings": True}}})
        s = store.get_subs(111)
        assert set(s.keys()) == {t["key"] for t in store.NOTIFICATION_TYPES}
