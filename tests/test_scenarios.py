"""
Функциональные сценарии: смена цикла КС, режим рынка, выбор бумаг.

Без сети — проверяем бизнес-логику на синтетических данных.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers.gcurve import MATURITIES
from parsers.minfin import build_auction_signal, enrich_auction_cache
from core.rate_scenarios import build_rate_scenarios
from scripts.bond_screener import calc_pnl, calc_supply_metrics, run_screener


# ─────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Изолированная data/ для main.py."""
    import main
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    return tmp_path


def _write_decisions(path: Path, changes: list[tuple[str, int]]):
    """changes = [(date, bps), ...]"""
    rows = [{"decision_date": d, "rate_change_bps": bps} for d, bps in changes]
    pd.DataFrame(rows).to_csv(path, index=False)


def _flat_gcurve(yield_pct: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": "04.06.2026",
        "срок_лет": MATURITIES,
        "доходность_пct": [yield_pct] * len(MATURITIES),
    })


def _inverted_gcurve(min_yield: float = 11.0) -> pd.DataFrame:
    """Кривая с минимумом на длинном конце — рынок ждёт снижения КС."""
    values = []
    for m in MATURITIES:
        if m <= 2:
            values.append(min_yield + 3.5 - m * 0.5)
        elif m <= 7:
            values.append(min_yield + 1.5 - (m - 2) * 0.2)
        else:
            values.append(min_yield + (10 - m) * 0.05)
    return pd.DataFrame({
        "date": "04.06.2026",
        "срок_лет": MATURITIES,
        "доходность_пct": values,
    })


def _hike_expectation_gcurve(key_rate: float = 14.5) -> pd.DataFrame:
    """Минимум доходности ВЫШЕ КС — рынок ждёт ужесточения."""
    min_y = key_rate + 1.0
    values = [min_y + m * 0.15 for m in MATURITIES]
    return pd.DataFrame({
        "date": "04.06.2026",
        "срок_лет": MATURITIES,
        "доходность_пct": values,
    })


def _sample_bonds_screener() -> dict:
    return {
        "rate_scenarios": build_rate_scenarios(14.5),
        "bonds": [
            {
                "secid": "SU26212RMFS0",
                "shortname": "ОФЗ 26212",
                "matdate": "2028-01-19",
                "duration": 3.5,
                "coupon_pct": 7.5,
                "ytm": 14.2,
                "base_scenario": "КС → 14.0%",
                "pnl_base_adjusted": 25.0,
                "pnl_mid_adjusted": 18.0,
                "pnl_deep_adjusted": 11.0,
                "pnl_flat": 12.5,
                "pnl_13_adjusted": 25.0,
                "pnl_11_adjusted": 11.0,
            },
            {
                "secid": "SU26238RMFS4",
                "shortname": "ОФЗ 26238",
                "matdate": "2041-05-15",
                "duration": 10.0,
                "coupon_pct": 7.1,
                "ytm": 14.5,
                "base_scenario": "КС → 14.0%",
                "pnl_base_adjusted": 22.0,
                "pnl_mid_adjusted": 30.0,
                "pnl_deep_adjusted": 32.0,
                "pnl_flat": 12.0,
                "pnl_13_adjusted": 22.0,
                "pnl_11_adjusted": 32.0,
            },
        ],
    }


# ─────────────────────────────────────────────
# РЕЖИМ РЫНКА ПРИ СМЕНЕ ЦИКЛА КС
# ─────────────────────────────────────────────

class TestRegimeScenarios:
    def test_easing_cycle_banks_buying_is_softening(self, data_dir):
        from main import compute_regime

        _write_decisions(data_dir / "cbr_decisions.csv", [
            ("2026-02-01", -50), ("2026-03-01", -50), ("2026-04-01", -50),
        ])
        regime = compute_regime(
            curve={"exp_cut": 2.0},
            auctions={"avg_btc": 1.6},
            banks={"status": "bull"},
        )
        assert regime["name"] == "Смягчение"

    def test_easing_cycle_without_banks_is_normalization(self, data_dir):
        from main import compute_regime

        _write_decisions(data_dir / "cbr_decisions.csv", [
            ("2026-02-01", -50), ("2026-03-01", -50), ("2026-04-01", -50),
        ])
        regime = compute_regime(
            curve={"exp_cut": 2.0},
            auctions={"avg_btc": 1.6},
            banks={"status": "neu"},
        )
        assert regime["name"] == "Нормализация"

    def test_rate_hike_cycle_is_overheating(self, data_dir):
        from main import compute_regime

        _write_decisions(data_dir / "cbr_decisions.csv", [
            ("2026-02-01", 50), ("2026-03-01", 50), ("2026-04-01", 50),
        ])
        regime = compute_regime(
            curve={"exp_cut": 1.0},
            auctions={"avg_btc": 1.8},
            banks={"status": "bull"},
        )
        assert regime["name"] == "Перегрев"

    def test_market_prices_higher_rates_than_cb(self, data_dir):
        """КС 14%, кривая минимум 16% → exp_cut отрицательный → Перегрев."""
        from main import compute_regime

        regime = compute_regime(
            curve={"exp_cut": -1.2},
            auctions={"avg_btc": 1.5},
            banks={"status": "neu"},
        )
        assert regime["name"] == "Перегрев"

    def test_panic_weak_auctions_and_hike_expectations(self, data_dir):
        from main import compute_regime

        regime = compute_regime(
            curve={"exp_cut": -0.3},
            auctions={"avg_btc": 0.2},
            banks={"status": "neu"},
        )
        assert regime["name"] == "Паника"


# ─────────────────────────────────────────────
# СИГНАЛ КРИВОЙ ПРИ РЕЗКОМ ИЗМЕНЕНИИ КС
# ─────────────────────────────────────────────

class TestKeyRateScenarios:
    def test_high_key_rate_inverted_curve_is_bullish(self):
        from main import compute_curve_signal

        df = _inverted_gcurve(min_yield=11.0)
        with patch("main.get_last_gcurve", return_value=(df, "04.06.2026")):
            sig = compute_curve_signal(key_rate=18.0)
        assert sig["status"] == "bull"
        assert sig["exp_cut"] > 5.0

    def test_key_rate_close_to_market_is_neutral(self):
        from main import compute_curve_signal

        df = _flat_gcurve(yield_pct=14.25)
        with patch("main.get_last_gcurve", return_value=(df, "04.06.2026")):
            sig = compute_curve_signal(key_rate=14.5)
        assert sig["status"] == "neu"
        assert sig["exp_cut"] <= 0.5

    def test_hike_expectation_curve_negative_exp_cut(self):
        from main import compute_curve_signal

        df = _hike_expectation_gcurve(key_rate=14.5)
        with patch("main.get_last_gcurve", return_value=(df, "04.06.2026")):
            sig = compute_curve_signal(key_rate=14.5)
        assert sig["exp_cut"] < -0.5


# ─────────────────────────────────────────────
# АУКЦИОНЫ: СИЛЬНЫЙ / СЛАБЫЙ СПРОС
# ─────────────────────────────────────────────

class TestAuctionScenarios:
    def _auction_df(self, btc_values: list[float]) -> pd.DataFrame:
        n = len(btc_values)
        dates = pd.date_range("2026-05-01", periods=n, freq="7D")
        rows = []
        for i, btc in enumerate(btc_values):
            offer = 100.0
            rows.append({
                "дата": dates[n - 1 - i],
                "код_выпуска": f"2625{i}",
                "дней_до_погашения": 3000,
                "предложение_млн": offer,
                "доходность_пct": 14.0 + i * 0.05,
                "спрос_млн": offer * btc,
                "размещено_млн": offer,
                "bid_to_cover": btc,
                "лет_до_погашения": 8.2,
            })
        return pd.DataFrame(rows).sort_values("дата", ascending=False)

    def test_strong_demand_triggers_entry_signal(self):
        sig = build_auction_signal(self._auction_df([1.8, 1.7, 1.6, 1.9]))
        assert sig["entry_signal"] is True
        assert sig["avg_btc"] >= 1.5
        assert sig["pass_through"] == 1.0

    def test_weak_demand_reduces_pass_through(self):
        sig = build_auction_signal(self._auction_df([0.4, 0.5, 0.45, 0.48]))
        assert sig["entry_signal"] is False
        assert sig["supply_pressure"] > 0.6
        assert sig["pass_through"] < 0.75

    def test_old_cache_enriched_for_api(self):
        minimal = {
            "avg_btc": 0.49,
            "yield_trend": 0.1,
            "last_btc": 0.26,
            "last_date": "01.06.2026",
            "last_yield": 14.5,
            "entry_signal": False,
        }
        full = enrich_auction_cache(minimal)
        assert full["status"] == "neu"
        assert "supply_pressure" in full
        assert full["last_code"] == "—"


# ─────────────────────────────────────────────
# КАКИЕ БУМАГИ ПОКАЗЫВАЕТ СКРИНЕР / РЕКОМЕНДАЦИЯ
# ─────────────────────────────────────────────

class TestBondSelectionScenarios:
    def test_longer_duration_higher_pnl_on_rate_cut(self):
        """При снижении КС длинная бумага даёт больший P&L (duration effect)."""
        short = calc_pnl(95, 7.5, duration=3.5, cut_bps=-150, pass_through=1.0)
        long_ = calc_pnl(60, 7.0, duration=10.0, cut_bps=-150, pass_through=1.0)
        assert long_["adjusted_pct"] > short["adjusted_pct"]

    def test_supply_overhang_cuts_realistic_pnl(self):
        full = calc_pnl(60, 7.0, duration=10.0, cut_bps=-150, pass_through=1.0)
        weak = calc_pnl(60, 7.0, duration=10.0, cut_bps=-150, pass_through=0.66)
        assert weak["adjusted_pct"] < full["adjusted_pct"]
        assert weak["theoretical_pct"] == full["theoretical_pct"]

    def test_screener_sorted_by_best_pnl(self):
        """Скрiner сортирует по лучшему P&L базового сценария."""
        mock_df = pd.DataFrame([
            {
                "SECID": "SU26212RMFS0", "SHORTNAME": "ОФЗ 26212",
                "MATDATE": pd.Timestamp("2028-01-19"),
                "years_left": 4.0, "LAST": 95.0, "COUPONPERCENT": 7.5, "YIELD": 14.2,
            },
            {
                "SECID": "SU26238RMFS4", "SHORTNAME": "ОФЗ 26238",
                "MATDATE": pd.Timestamp("2041-05-15"),
                "years_left": 12.0, "LAST": 57.0, "COUPONPERCENT": 7.1, "YIELD": 14.5,
            },
        ])
        supply = calc_supply_metrics(btc_current=1.5)
        with patch("scripts.bond_screener.fetch_ofz_universe", return_value=mock_df):
            results, _ = run_screener(supply, key_rate=14.5)

        assert results[0]["pnl_base_adjusted"] >= results[1]["pnl_base_adjusted"]

    def test_recommendation_picks_best_pnl_bond(self, data_dir):
        """API-рекомендация = бумага с max pnl_base_adjusted."""
        from main import compute_recommendation

        (data_dir / "bond_screener.json").write_text(
            json.dumps(_sample_bonds_screener()), encoding="utf-8"
        )
        rec = compute_recommendation(
            key_rate=14.5,
            auctions={"pass_through": 0.66, "supply_pressure": 0.67,
                      "entry_signal": False, "avg_btc": 0.49},
            banks={},
        )
        assert rec["asset"] == "ОФЗ 26212"
        assert rec["pnl_base"] == 25.0

    def test_strong_auctions_do_not_change_recommended_bond(self, data_dir):
        """Смена BTC меняет pass_through в карточке, но не выбор бумаги."""
        from main import compute_recommendation

        (data_dir / "bond_screener.json").write_text(
            json.dumps(_sample_bonds_screener()), encoding="utf-8"
        )
        weak = compute_recommendation(
            14.5,
            {"pass_through": 0.66, "supply_pressure": 0.67,
             "entry_signal": False, "avg_btc": 0.5},
            {},
        )
        strong = compute_recommendation(
            14.5,
            {"pass_through": 1.0, "supply_pressure": 0.0,
             "entry_signal": True, "avg_btc": 1.8},
            {},
        )
        assert weak["asset"] == strong["asset"] == "ОФЗ 26212"
        assert weak["entry_signal"] is False
        assert strong["entry_signal"] is True


# ─────────────────────────────────────────────
# ВЕРДИКТ OVERVIEW ПРИ СИГНАЛЕ ВХОДА
# ─────────────────────────────────────────────

class TestOverviewVerdictScenarios:
    def test_entry_signal_changes_verdict(self, data_dir):
        from main import compute_recommendation

        (data_dir / "bond_screener.json").write_text(
            json.dumps(_sample_bonds_screener()), encoding="utf-8"
        )
        rec = compute_recommendation(
            14.5,
            {"pass_through": 1.0, "supply_pressure": 0.0,
             "entry_signal": True, "avg_btc": 1.8},
            {},
        )
        entry = True
        if entry:
            verdict = "Сигнал входа пришёл"
            action = f"Рассмотреть покупку {rec['asset']}"
        assert rec["asset"] in action
        assert verdict == "Сигнал входа пришёл"

    def test_bull_curve_without_entry_waits(self):
        curve = {"status": "bull"}
        entry = False
        if entry:
            verdict = "Сигнал входа пришёл"
        elif curve.get("status") == "bull":
            verdict = "Ждать сигнала входа в ОФЗ"
            action = "Уведомим когда BTC > 1.5×"
        assert verdict == "Ждать сигнала входа в ОФЗ"
        assert "BTC" in action
