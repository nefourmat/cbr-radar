"""
parsers/inflation_expectations.py — Наблюдаемая и ожидаемая инфляция (инФОМ ЦБ)

«Реальная» (ощущаемая населением) инфляция из ежемесячного опроса инФОМ,
который публикует Банк России. Обычно в 2–3 раза выше официальной — это
разрыв доверия между Росстатом и восприятием людей.

Источник: https://www.cbr.ru/analytics/dkp/inflationary_expectations/
Файлы:    /Collection/Collection/File/<id>/Infl_exp_YY-MM.xlsx (новый сверху)
Лист:     «Данные за все годы» — медианные оценки по месяцам.

Медианные ряды (col0):
    «наблюдаемая инфляция (в %)»  — что люди думают о росте цен за прошлый год
    «ожидаемая инфляция (в %)»    — ожидания на год вперёд

Ключевые функции:
    get_latest_xlsx_url()           → str | None
    fetch_expectations_xlsx()       → bytes | None
    parse_expectations(xlsx_bytes)  → dict | None
    get_inflation_expectations()    → dict | None   (полный цикл)

Fallback: при любой ошибке возвращает None (не бросает исключение).
"""

import io
import re
from datetime import datetime

import requests

BASE     = "https://www.cbr.ru"
PAGE_URL = BASE + "/analytics/dkp/inflationary_expectations/"
HEADERS  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_HIST_POINTS = 6   # сколько последних точек истории отдавать


def get_latest_xlsx_url():
    """Находит ссылку на самый свежий файл Infl_exp_YY-MM.xlsx. None при ошибке."""
    try:
        r = requests.get(PAGE_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        links = re.findall(
            r'href="(/Collection/Collection/File/\d+/Infl_exp_[0-9-]+\.xlsx)"',
            r.text,
        )
        return BASE + links[0] if links else None
    except requests.exceptions.RequestException:
        return None


def fetch_expectations_xlsx():
    """Скачивает свежий xlsx инФОМ. None при ошибке."""
    url = get_latest_xlsx_url()
    if not url:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.exceptions.RequestException:
        return None


def _find_date_row(df):
    """Строка с максимумом дат (заголовок временного ряда)."""
    best_i, best_n = 0, -1
    for i in range(min(6, len(df))):
        n = sum(isinstance(v, datetime) for v in df.iloc[i])
        if n > best_n:
            best_i, best_n = i, n
    return best_i if best_n > 0 else None


def _series_row(df, must_have, exclude=("среди",)):
    """
    Находит строку медианного ряда по подстроке в col0
    (например 'наблюдаемая инфляция' + '(в %)'), исключая подгруппы ('среди ...').
    Возвращает индекс строки или None.
    """
    col0 = df.iloc[:, 0].astype(str)
    for i, lab in enumerate(col0):
        low = lab.lower()
        if all(m in low for m in must_have) and not any(e in low for e in exclude):
            return i
    return None


def _row_history(df, row_i, date_cols, n=_HIST_POINTS):
    """Последние n значений ряда (по колонкам с датами), новые в конце."""
    vals = []
    for j in date_cols[-n:]:
        v = df.iat[row_i, j]
        vals.append(round(float(v), 2) if isinstance(v, (int, float)) and v == v else None)
    return vals


def parse_expectations(xlsx_bytes):
    """
    Парсит xlsx инФОМ. Возвращает:
        {
          "date":              "2026-05-01",
          "observed":          15.08,   # наблюдаемая инфляция (медиана), %
          "expected":          13.02,   # ожидаемая инфляция (медиана), %
          "observed_history":  [...],   # последние месяцы (новые в конце)
          "expected_history":  [...],
        }
    None — если структуру распознать не удалось.
    """
    if not xlsx_bytes:
        return None
    try:
        import pandas as pd
        xl = pd.ExcelFile(io.BytesIO(xlsx_bytes))
        # Предпочитаем лист с полным рядом, иначе — для графиков
        sheet = None
        for name in xl.sheet_names:
            if "все годы" in name.lower():
                sheet = name
                break
        if sheet is None:
            for name in xl.sheet_names:
                if "график" in name.lower():
                    sheet = name
                    break
        if sheet is None:
            return None

        df = xl.parse(sheet, header=None)
        drow = _find_date_row(df)
        if drow is None:
            return None
        date_cols = [j for j, v in enumerate(df.iloc[drow]) if isinstance(v, datetime)]
        if not date_cols:
            return None
        last_col  = date_cols[-1]
        last_date = df.iat[drow, last_col]

        obs_i = _series_row(df, ("наблюдаемая инфляция", "(в %)"))
        exp_i = _series_row(df, ("ожидаемая инфляция", "(в %)"))
        if obs_i is None or exp_i is None:
            return None

        def _val(i):
            v = df.iat[i, last_col]
            return round(float(v), 2) if isinstance(v, (int, float)) and v == v else None

        observed = _val(obs_i)
        expected = _val(exp_i)
        if observed is None or expected is None:
            return None

        return {
            "date":             last_date.date().isoformat()
                                if hasattr(last_date, "date") else str(last_date),
            "observed":         observed,
            "expected":         expected,
            "observed_history": _row_history(df, obs_i, date_cols),
            "expected_history": _row_history(df, exp_i, date_cols),
        }
    except Exception:
        return None


def get_inflation_expectations():
    """Полный цикл: скачать + распарсить. None при любой ошибке."""
    return parse_expectations(fetch_expectations_xlsx())


if __name__ == "__main__":
    data = get_inflation_expectations()
    if not data:
        print("Не удалось получить инФОМ-данные")
    else:
        print(f"Период:                {data['date']}")
        print(f"Наблюдаемая инфляция:  {data['observed']}%")
        print(f"Ожидаемая инфляция:    {data['expected']}%")
        print(f"История наблюдаемой:   {data['observed_history']}")
        print(f"История ожидаемой:     {data['expected_history']}")
