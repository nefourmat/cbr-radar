"""tests/test_api.py"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI не установлен")
class TestHealthEndpoint:
    def test_health_returns_200(self):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_has_status_ok(self):
        response = client.get("/health")
        data = response.json()
        assert data.get("status") == "ok"

    def test_health_has_timestamp(self):
        response = client.get("/health")
        data = response.json()
        assert "time" in data


@pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI не установлен")
class TestOverviewEndpoint:
    def test_overview_returns_200(self):
        response = client.get("/api/overview")
        assert response.status_code == 200

    def test_overview_has_required_fields(self):
        response = client.get("/api/overview")
        data = response.json()
        required = {"key_rate", "verdict", "regime", "signals", "recommendation"}
        missing = required - set(data.keys())
        assert not missing, f"Отсутствуют поля: {missing}"

    def test_overview_key_rate_is_valid(self):
        response = client.get("/api/overview")
        data = response.json()
        rate = data.get("key_rate", 0)
        assert 1.0 <= rate <= 30.0, f"КС {rate}% за пределами разумного"

    def test_overview_regime_has_name(self):
        response = client.get("/api/overview")
        data = response.json()
        regime = data.get("regime", {})
        assert "name" in regime
        assert regime["name"] in {"Нормализация","Смягчение","Перегрев","Паника"}

    def test_overview_signals_have_three_keys(self):
        response = client.get("/api/overview")
        data = response.json()
        signals = data.get("signals", {})
        assert "curve" in signals
        assert "auctions" in signals
        assert "banks" in signals

    def test_meetings_returns_200(self):
        response = client.get("/api/meetings")
        assert response.status_code == 200

    def test_screener_returns_200(self):
        response = client.get("/api/screener")
        assert response.status_code == 200


@pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI не установлен")
class TestRecommendationStructure:
    def test_payout_has_four_scenarios(self):
        response = client.get("/api/overview")
        data = response.json()
        rec    = data.get("recommendation", {})
        payout = rec.get("payout", [])
        assert len(payout) == 4, f"Ожидали 4 сценария, получили {len(payout)}"

    def test_payout_has_base_scenario(self):
        response = client.get("/api/overview")
        data = response.json()
        rec    = data.get("recommendation", {})
        payout = rec.get("payout", [])
        base = [p for p in payout if p.get("base")]
        assert len(base) == 1, "Должен быть ровно 1 базовый сценарий"

    def test_pnl_values_are_reasonable(self):
        response = client.get("/api/overview")
        data = response.json()
        rec = data.get("recommendation", {})
        pnl = rec.get("pnl_base", 0)
        assert 0 < pnl < 100, f"P&L {pnl}% выглядит нереалистично"
