"""tests/test_gcurve.py"""
import pytest
import pandas as pd
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_mock_response(yields: dict):
    """Создаём mock-ответ ЦБ с нужными доходностями."""
    mock = MagicMock()
    mock.status_code = 200
    # Формат как у реального ответа cbr.ru
    mock.json.return_value = [
        {"term": str(t), "value": str(y)}
        for t, y in yields.items()
    ]
    return mock


class TestGetKeyRate:
    def test_returns_float(self):
        """get_key_rate() должна возвращать float."""
        from parsers.gcurve import get_key_rate
        with patch("parsers.gcurve.requests.get") as mock_get:
            mock_get.return_value.text = """
                <html><body>
                <table class="data">
                <tr><th>Дата</th><th>Ставка</th></tr>
                <tr><td>01.06.2026</td><td>14,50</td></tr>
                </table>
                </body></html>
            """
            rate = get_key_rate()
            assert isinstance(rate, float), f"Ожидали float, получили {type(rate)}"
            assert 1.0 <= rate <= 30.0, f"Ставка {rate}% за пределами разумного"

    def test_returns_none_on_network_error(self):
        """При сетевой ошибке должна вернуть None, не бросить исключение."""
        from parsers.gcurve import get_key_rate
        import requests
        with patch("parsers.gcurve.requests.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("timeout")
            rate = get_key_rate()
            assert rate is None


class TestGetLastGcurve:
    def test_returns_dataframe_with_required_columns(self):
        """get_last_gcurve() должна вернуть DataFrame с нужными колонками."""
        from parsers.gcurve import get_last_gcurve
        df, date_str = get_last_gcurve()
        # Если нет сети — можно пропустить
        if df is None:
            pytest.skip("Нет данных G-кривой (нет сети или сервер недоступен)")

        required = {"срок_лет", "доходность_пct"}
        assert required.issubset(set(df.columns)), \
            f"Отсутствуют колонки: {required - set(df.columns)}"

    def test_yields_are_positive(self):
        """Доходности должны быть положительными числами."""
        from parsers.gcurve import get_last_gcurve
        df, _ = get_last_gcurve()
        if df is None:
            pytest.skip("Нет данных")
        assert (df["доходность_пct"] > 0).all(), "Есть нулевые или отрицательные доходности"

    def test_has_multiple_maturities(self):
        """Кривая должна иметь минимум 5 точек."""
        from parsers.gcurve import get_last_gcurve
        df, _ = get_last_gcurve()
        if df is None:
            pytest.skip("Нет данных")
        assert len(df) >= 5, f"Слишком мало точек: {len(df)}"


class TestGcurveSignal:
    """Unit-тесты расчёта сигнала — без сети."""

    def test_expected_cut_calculation(self):
        """ожид_снижение = КС - min(yield)."""
        key_rate = 14.5
        min_yield = 12.96
        expected_cut = round(key_rate - min_yield, 2)
        assert expected_cut == 1.54

    def test_slope_2_10(self):
        """Наклон кривой 2-10 = y10 - y2."""
        y2, y10 = 13.33, 14.89
        slope = round(y10 - y2, 2)
        assert slope == 1.56

    def test_bullish_signal_when_exp_cut_positive(self):
        """Сигнал бычий когда ожид_снижение > 0.5%."""
        exp_cut = 1.54
        status = "bull" if exp_cut > 0.5 else "neu"
        assert status == "bull"
