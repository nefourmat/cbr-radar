"""
parsers/inflation.py — Инфляция и реальная ставка (ЦБ РФ)

Источник: https://www.cbr.ru/hd_base/infl/
Формат: HTML-таблица (windows-1251) с колонками:
    Дата (MM.YYYY) | Ключевая ставка, % | Инфляция, % г/г | Цель по инфляции, %

Главный сигнал — РЕАЛЬНАЯ СТАВКА (КС − инфляция) и ТРЕНД инфляции:
высокая реальная ставка + замедление инфляции = пространство для снижения КС.

Ключевые функции:
    fetch_inflation_html()        → str | None     HTML страницы
    parse_inflation(html)         → list[dict]|None строки (новые сверху)
    get_inflation_data()          → list[dict]|None fetch + parse
    build_inflation_signal(rows)  → dict            сигнал для /api/overview

Fallback: при сетевой ошибке возвращает None (не бросает исключение).
"""

import requests
from bs4 import BeautifulSoup

URL     = "https://www.cbr.ru/hd_base/infl/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TARGET_DEFAULT = 4.0   # цель ЦБ по инфляции, % (на случай отсутствия колонки)


def _num(s):
    """'5,58' / '14,50' / '4 000' → float; пустое/мусор → None."""
    if s is None:
        return None
    s = str(s).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_inflation_html():
    """GET страницы инфляции ЦБ (windows-1251). None при сетевой ошибке."""
    try:
        r = requests.get(URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = "windows-1251"
        return r.text
    except requests.exceptions.RequestException:
        return None


def parse_inflation(html):
    """
    Парсит HTML-таблицу инфляции ЦБ.
    Возвращает список словарей (новые периоды сверху):
        {"date": "04.2026", "key_rate": 14.5, "infl_yoy": 5.58, "target": 4.0}
    None — если таблица не найдена / пустая.
    """
    if not html:
        return None
    soup  = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return None

    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 3:
            continue  # пропускаем заголовок / служебные строки
        date_str = cells[0]
        key_rate = _num(cells[1])
        infl_yoy = _num(cells[2])
        target   = _num(cells[3]) if len(cells) > 3 else TARGET_DEFAULT
        if infl_yoy is None or not date_str:
            continue
        rows.append({
            "date":     date_str,
            "key_rate": key_rate,
            "infl_yoy": infl_yoy,
            "target":   target if target is not None else TARGET_DEFAULT,
        })

    return rows or None


def get_inflation_data():
    """Полный цикл: скачать + распарсить. None при любой ошибке."""
    return parse_inflation(fetch_inflation_html())


def _merge_expectations(signal, expectations, infl):
    """
    Добавляет в сигнал наблюдаемую/ожидаемую инфляцию инФОМ (если есть).
    observed — что население ощущает (обычно в 2–3× выше официальной),
    gap_vs_official — разрыв доверия между Росстатом и восприятием.
    """
    if not expectations:
        return signal
    observed = expectations.get("observed")
    expected = expectations.get("expected")
    signal["observed"] = observed
    signal["expected"] = expected
    signal["observed_history"] = expectations.get("observed_history", [])
    signal["expected_history"] = expectations.get("expected_history", [])
    signal["survey_date"] = expectations.get("date")
    if observed is not None and infl is not None:
        signal["gap_vs_official"] = round(observed - infl, 2)
    # тренд ожидаемой инфляции за 3 мес (новые точки в конце истории)
    eh = expectations.get("expected_history") or []
    signal["exp_trend_3m"] = round(eh[-1] - eh[-4], 2) if len(eh) >= 4 else 0.0
    if observed is not None:
        signal["description"] += (
            f" · наблюдаемая {observed:.0f}%"
            + (f" (ожидаемая {expected:.0f}%)" if expected is not None else "")
        )
    return signal


def build_inflation_signal(rows, key_rate=None, expectations=None):
    """
    Строит сигнал инфляции для главного экрана.

    Логика:
      real_rate = КС − инфляция        → жёсткость ДКП (чем выше, тем больше
                                          пространства для снижения КС)
      trend_3m  = инфляция_тек − 3мес   → < 0: дезинфляция (благоприятно)
      gap       = инфляция − цель       → насколько выше таргета 4%

    expectations (инФОМ, опционально) добавляет:
      observed  — наблюдаемая инфляция (что ощущает население)
      expected  — ожидаемая инфляция (на год вперёд)
      gap_vs_official — наблюдаемая − официальная (разрыв доверия)

    status: 'bull'  — дезинфляция + жёсткая ДКП → сигнал к снижению КС
            'warn'  — ускорение инфляции → ЦБ держит/повышает
            'neu'   — стабильно
    """
    if not rows:
        empty = {
            "status": "neu", "label": "Нет данных", "arrow": "→",
            "date": "—", "infl_yoy": 0.0, "target": TARGET_DEFAULT,
            "gap_vs_target": 0.0, "real_rate": 0.0, "key_rate": key_rate or 0.0,
            "trend_3m": 0.0, "history": [],
            "description": "Данные по инфляции временно недоступны",
        }
        return _merge_expectations(empty, expectations, None)

    latest   = rows[0]
    infl     = round(latest["infl_yoy"], 2)
    target   = round(latest.get("target") or TARGET_DEFAULT, 2)
    # КС: приоритет аргумента (живая КС из gcurve), иначе из таблицы инфляции
    kr       = key_rate if key_rate is not None else latest.get("key_rate")
    kr       = round(kr, 2) if kr is not None else None

    gap       = round(infl - target, 2)
    real_rate = round(kr - infl, 2) if kr is not None else None

    history = [round(r["infl_yoy"], 2) for r in rows[:6]]
    # тренд за 3 мес: текущая минус значение 3 месяца назад (если есть)
    trend_3m = round(history[0] - history[3], 2) if len(history) >= 4 else 0.0

    # Классификация
    disinflation = trend_3m <= -0.2
    accelerating = trend_3m >= 0.3
    hard_policy  = real_rate is not None and real_rate >= 4.0

    if accelerating:
        status, label, arrow = "warn", "Ускорение", "↑"
    elif disinflation:
        status, label, arrow = "bull", "Дезинфляция", "↓"
    else:
        status, label, arrow = "neu", "Стабильно", "→"

    # Человекочитаемое описание
    trend_word = ("замедляется" if trend_3m < -0.05
                  else "ускоряется" if trend_3m > 0.05
                  else "стабильна")
    parts = [f"Инфляция {infl:.1f}% г/г ({trend_word}"]
    if abs(trend_3m) >= 0.05:
        parts[0] += f", {trend_3m:+.1f} п.п. за 3 мес"
    parts[0] += ")"
    if real_rate is not None:
        policy = "жёсткая" if hard_policy else "умеренная"
        parts.append(f"реальная ставка {real_rate:.1f} п.п. — ДКП {policy}")
    description = " · ".join(parts)

    signal = {
        "status":        status,
        "label":         label,
        "arrow":         arrow,
        "date":          latest["date"],
        "infl_yoy":      infl,
        "target":        target,
        "gap_vs_target": gap,
        "real_rate":     real_rate if real_rate is not None else 0.0,
        "key_rate":      kr if kr is not None else 0.0,
        "trend_3m":      trend_3m,
        "history":       history,
        "description":   description,
    }
    return _merge_expectations(signal, expectations, infl)


if __name__ == "__main__":
    data = get_inflation_data()
    if not data:
        print("Не удалось получить данные по инфляции")
    else:
        sig = build_inflation_signal(data)
        print(f"Период:          {sig['date']}")
        print(f"Инфляция г/г:    {sig['infl_yoy']}%")
        print(f"Цель:            {sig['target']}%")
        print(f"Отклонение:      {sig['gap_vs_target']:+.2f} п.п.")
        print(f"Реальная ставка: {sig['real_rate']:+.2f} п.п.")
        print(f"Тренд (3 мес):   {sig['trend_3m']:+.2f} п.п.")
        print(f"Статус:          {sig['status']} / {sig['label']} {sig['arrow']}")
        print(f"История:         {sig['history']}")
        print(f"Описание:        {sig['description']}")
