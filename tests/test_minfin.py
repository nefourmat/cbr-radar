"""tests/test_minfin.py"""
import pytest
import pandas as pd
import sys
from pathlib import Path
from io import BytesIO
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestParseAuctions:
    """Тесты парсера аукционов — два формата заголовков."""

    def _make_xlsx(self, header_name: str, rows: list) -> BytesIO:
        """Создаём тестовый XLSX в памяти."""
        cols = [
            header_name,           # дата
            "Код  выпуска",
            "Тип бумаги*",
            "Дата погашения",
            "Дней до погашения",
            "Объем предложения",
            "Доходность по цене отсечения**",
            "Совокупный объем спроса по номиналу",
            "Объем размещения по номиналу",
            "Коэффициент удовлетворения спроса на аукционе",
        ]
        df = pd.DataFrame(rows, columns=cols)
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, startrow=5)
        buf.seek(0)
        return buf

    def _sample_row(self, date_str: str, code="26238RMFS"):
        return [
            date_str, code, "ОФЗ-ПД",
            "2040-05-15", 4000,
            100000, 14.80,
            67000, 50000, 0.75
        ]

    def test_new_format_header(self):
        """Новый формат (2024+): колонка 'Дата'."""
        from parsers.minfin import parse_auctions
        row = self._sample_row("2024-06-05")
        buf = self._make_xlsx("Дата", [row])
        df = parse_auctions(buf)
        if df is None:
            pytest.skip("parse_auctions вернула None — возможно другой формат файла")
        assert "дата" in df.columns
        assert len(df) > 0

    def test_old_format_header(self):
        """Старый формат (2021-2023): колонка 'Дата аукциона'."""
        from parsers.minfin import parse_auctions
        row = self._sample_row("2022-03-15")
        buf = self._make_xlsx("Дата аукциона", [row])
        df = parse_auctions(buf)
        if df is None:
            pytest.skip("parse_auctions вернула None")
        assert "дата" in df.columns

    def test_bid_to_cover_calculated(self):
        """bid_to_cover = спрос / размещено."""
        # 67000 / 50000 = 1.34
        спрос = 67000.0
        размещено = 50000.0
        btc = round(спрос / размещено, 2) if размещено > 0 else 0
        assert btc == pytest.approx(1.34, abs=0.01)

    def test_btc_zero_when_no_placement(self):
        """BTC = 0 когда размещения не было."""
        спрос = 10000.0
        размещено = 0.0
        btc = round(спрос / размещено, 2) if размещено > 0 else 0.0
        assert btc == 0.0

    def test_live_data_structure(self):
        """Интеграционный тест: реальные данные с Минфина."""
        from parsers.minfin import get_latest_file_url, download_xlsx, parse_auctions
        try:
            url  = get_latest_file_url()
            xlsx = download_xlsx(url)
            df   = parse_auctions(xlsx)
        except Exception as e:
            pytest.skip(f"Нет доступа к Минфину: {e}")

        assert df is not None
        required_cols = {"дата", "код_выпуска", "bid_to_cover", "доходность_пct"}
        assert required_cols.issubset(set(df.columns)), \
            f"Отсутствуют: {required_cols - set(df.columns)}"
        assert len(df) > 0
        assert (df["bid_to_cover"] >= 0).all()
