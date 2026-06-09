"""
scripts/ofz_pnl.py

Считает реальный P&L для тех кто купил ОФЗ-26238 в августе 2024,
и показывает сценарии реальной доходности для покупки сейчас.

Данные: MOEX ISS API (бесплатно, без регистрации)
Запуск: python scripts/ofz_pnl.py
"""

import sys
import requests
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

SECID     = "SU26238RMFS4"   # ОФЗ-26238, погашение 2041
FACE_VAL  = 1000             # номинал ₽1000
COUPON    = 35.4             # купон ₽35.4 каждые 6 месяцев
MATURITY  = date(2041, 5, 15)

# Даты купонных выплат (из данных MOEX)
COUPONS = [
    "2024-06-05", "2024-12-04",
    "2025-06-04", "2025-12-03",
    "2026-06-03",
]


# ─────────────────────────────────────────────
# ЗАГРУЗКА ЦЕН С MOEX ISS
# ─────────────────────────────────────────────

def fetch_moex_history(secid, board, market, engine,
                       date_from, date_to=None):
    """Загружает историю цен с MOEX ISS API."""
    if date_to is None:
        date_to = date.today().isoformat()

    all_rows = []
    start    = 0

    while True:
        url = (f"https://iss.moex.com/iss/history/engines/{engine}/"
               f"markets/{market}/boards/{board}/"
               f"securities/{secid}.json")
        params = {
            "from":       date_from,
            "till":       date_to,
            "start":      start,
            "iss.meta":   "off",
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        block = data.get("history", {})
        cols  = block.get("columns", [])
        rows  = block.get("data",    [])

        if not rows:
            break

        for row in rows:
            all_rows.append(dict(zip(cols, row)))

        start += len(rows)
        if len(rows) < 100:
            break

    return pd.DataFrame(all_rows)


def fetch_ofz_prices(date_from="2024-01-01"):
    """Цены ОФЗ-26238 с MOEX."""
    print(f"  Загружаем цены {SECID} с {date_from}...")
    df = fetch_moex_history(
        secid=SECID, board="TQOB",
        market="bonds", engine="stock",
        date_from=date_from
    )
    if df.empty:
        print("  Нет данных")
        return None

    df["TRADEDATE"] = pd.to_datetime(df["TRADEDATE"])

    # Цена в % от номинала
    price_col = "LEGALCLOSEPRICE" if "LEGALCLOSEPRICE" in df.columns else "CLOSE"
    if price_col not in df.columns:
        print(f"  Колонки: {list(df.columns)}")
        # Попробуем взять WAPRICE
        price_col = [c for c in df.columns if "PRICE" in c.upper() or "CLOSE" in c.upper()]
        if price_col:
            price_col = price_col[0]
        else:
            return None

    df = df[["TRADEDATE", price_col]].dropna()
    df.columns = ["date", "price_pct"]
    df["price_rub"] = df["price_pct"] / 100 * FACE_VAL
    df = df.sort_values("date")

    print(f"  ✓ {len(df)} торговых дней, "
          f"{df['date'].min().date()} — {df['date'].max().date()}")
    return df


def fetch_rgbi(date_from="2024-01-01"):
    """Индекс RGBI (полной доходности ОФЗ) с MOEX."""
    print(f"  Загружаем RGBI с {date_from}...")
    df = fetch_moex_history(
        secid="RGBI", board="SNDX",
        market="index", engine="stock",
        date_from=date_from
    )
    if df.empty:
        # Пробуем RGBITR (total return)
        df = fetch_moex_history(
            secid="RGBITR", board="SNDX",
            market="index", engine="stock",
            date_from=date_from
        )

    if df.empty:
        return None

    df["TRADEDATE"] = pd.to_datetime(df["TRADEDATE"])
    close_col = next(
        (c for c in df.columns if c in ["CLOSE", "VALUE", "CURRENTVALUE"]),
        None
    )
    if not close_col:
        print(f"  Колонки RGBI: {list(df.columns)}")
        return None

    df = df[["TRADEDATE", close_col]].dropna()
    df.columns = ["date", "value"]
    df = df.sort_values("date")
    print(f"  ✓ {len(df)} дней RGBI")
    return df


# ─────────────────────────────────────────────
# РАСЧЁТ P&L ДЛЯ КУПИВШИХ В АВГУСТЕ 2024
# ─────────────────────────────────────────────

def calc_historical_pnl(prices_df, entry_date_str="2024-08-01"):
    """
    Считаем P&L для тех кто купил в начале августа 2024
    (первая волна накопления банков по Form 101).
    """
    entry_date = pd.Timestamp(entry_date_str)

    # Ближайшая торговая дата к входу
    available = prices_df[prices_df["date"] >= entry_date]
    if available.empty:
        return None

    entry_row   = available.iloc[0]
    entry_price = entry_row["price_rub"]
    entry_dt    = entry_row["date"]

    # Текущая цена
    current_row   = prices_df.iloc[-1]
    current_price = current_row["price_rub"]
    current_dt    = current_row["date"]

    # Купоны полученные между входом и сегодня
    coupons_received = sum(
        COUPON for c in COUPONS
        if entry_dt <= pd.Timestamp(c) <= current_dt
    )

    # P&L
    price_gain   = current_price - entry_price
    total_return = price_gain + coupons_received
    pct_return   = total_return / entry_price * 100
    months_held  = (current_dt - entry_dt).days / 30.4
    # Защита от деления на ноль (одна строка в серии): пропускаем annualize
    if months_held < 1:
        annualized = pct_return / 100
    else:
        annualized = (1 + pct_return/100) ** (12/months_held) - 1

    return {
        "entry_date":        entry_dt.date(),
        "entry_price":       round(entry_price, 2),
        "entry_yield_pct":   round((COUPON * 2 / entry_price) * 100, 2),
        "current_date":      current_dt.date(),
        "current_price":     round(current_price, 2),
        "price_gain_rub":    round(price_gain, 2),
        "price_gain_pct":    round(price_gain / entry_price * 100, 2),
        "coupons_received":  round(coupons_received, 2),
        "total_return_rub":  round(total_return, 2),
        "total_return_pct":  round(pct_return, 2),
        "months_held":       round(months_held, 1),
        "annualized_pct":    round(annualized * 100, 2),
    }


# ─────────────────────────────────────────────
# СЦЕНАРИИ ДЛЯ ПОКУПКИ СЕЙЧАС
# ─────────────────────────────────────────────

def duration_approx(maturity_date, coupon_rate, yield_pct):
    """Приближённая дюрация Маколея (лет)."""
    years_left = (maturity_date - date.today()).days / 365
    y = yield_pct / 100
    c = coupon_rate / 100

    if abs(y - c) < 0.001:
        return years_left / 2

    # Формула Маколея
    d = (1 + y) / y - ((1 + y) + years_left * (c - y)) / (c * ((1 + y)**years_left - 1) + y)
    return max(1.0, min(d, years_left))


def calc_price_sensitivity(duration, yield_change_bps, yield_pct):
    """Изменение цены при изменении доходности на N бп."""
    # Модифицированная дюрация: mod_dur = MacDur / (1 + y), y в долях
    y = yield_pct / 100
    mod_duration = duration / (1 + y)
    return -mod_duration * (yield_change_bps / 100)


def scenarios_for_now(current_price, current_yield_pct, key_rate):
    """
    Сценарии P&L для покупки по текущей цене.
    Горизонт: 12–18 месяцев.
    """
    dur = duration_approx(MATURITY, 7.08, current_yield_pct)
    coupon_12m = COUPON * 2
    coupon_yield = coupon_12m / current_price * 100

    # Официальный CPI
    cpi_official = 9.8

    scenarios = []

    rate_cuts = [
        ("Базовый: КС → 13%",    -150, 12),
        ("Оптимистичный: КС → 11%", -350, 18),
        ("Пессимистичный: КС → 14%", -50, 18),
        ("Плоский: КС без изменений", 0,  12),
    ]

    for label, cut_bps, months in rate_cuts:
        # Изменение доходности ≈ изменение КС (упрощение)
        yield_change = cut_bps  # в бп
        price_chg_pct = calc_price_sensitivity(dur, yield_change, current_yield_pct)
        price_chg_rub = current_price * price_chg_pct / 100

        coupon_period = coupon_12m * (months / 12)
        total_rub     = price_chg_rub + coupon_period
        total_pct     = total_rub / current_price * 100
        annual_pct    = total_pct / (months / 12)

        # Реальная доходность при разных сценариях инфляции
        real_opt  = annual_pct - cpi_official
        real_base = annual_pct - (cpi_official + 2.0)
        real_pess = annual_pct - (cpi_official + 4.0)

        scenarios.append({
            "label":         label,
            "cut_bps":       cut_bps,
            "months":        months,
            "price_chg_pct": round(price_chg_pct, 1),
            "coupon_pct":    round(coupon_period / current_price * 100, 1),
            "total_pct":     round(total_pct, 1),
            "annual_pct":    round(annual_pct, 1),
            "real_opt":      round(real_opt,  1),
            "real_base":     round(real_base, 1),
            "real_pess":     round(real_pess, 1),
        })

    return scenarios, dur


# ─────────────────────────────────────────────
# ВЫВОД
# ─────────────────────────────────────────────

def format_output(pnl, scenarios, dur, current_price, current_yield):
    W = 65
    lines = []

    lines += [
        "",
        "═" * W,
        "  ОФЗ-26238 · АНАЛИЗ ДОХОДНОСТИ",
        f"  Дюрация: {dur:.1f} лет | Купон: 7.08% | Погашение: 2041",
        "═" * W,
    ]

    # Блок 1: P&L тех кто купил в августе 2024
    if pnl:
        lines += [
            "",
            f"  КТО КУПИЛ В АВГУСТЕ 2024 (начало волны накопления банков)",
            f"  {'─' * (W-2)}",
            f"  Дата входа:          {pnl['entry_date']}",
            f"  Цена входа:          ₽{pnl['entry_price']} "
            f"({pnl['entry_price']/FACE_VAL*100:.1f}% от номинала)",
            f"  Доходность к входу:  {pnl['entry_yield_pct']:.2f}%",
            "",
            f"  Цена сейчас:         ₽{pnl['current_price']} "
            f"({pnl['current_price']/FACE_VAL*100:.1f}% от номинала)",
            f"  Прирост цены:        ₽{pnl['price_gain_rub']:+.1f} "
            f"({pnl['price_gain_pct']:+.1f}%)",
            f"  Купоны получено:     ₽{pnl['coupons_received']:.1f}",
            "",
            f"  ИТОГО за {pnl['months_held']:.0f} мес:    "
            f"₽{pnl['total_return_rub']:+.1f} = "
            f"{pnl['total_return_pct']:+.1f}%",
            f"  Годовых (annualized): {pnl['annualized_pct']:+.1f}%",
        ]

    # Блок 2: Сценарии для покупки сейчас
    lines += [
        "",
        f"  СЦЕНАРИИ ДЛЯ ПОКУПКИ СЕЙЧАС",
        f"  Цена: ₽{current_price:.1f} ({current_price/FACE_VAL*100:.1f}% от номинала) "
        f"| Доходность: {current_yield:.2f}%",
        f"  {'─' * (W-2)}",
        f"  {'Сценарий':<28} {'Цена':>6} {'Купон':>6} "
        f"{'Итого':>7} {'Реал.(опт)':>10} {'Реал.(баз)':>10}",
        f"  {'─' * (W-2)}",
    ]

    for s in scenarios:
        lines.append(
            f"  {s['label']:<28}"
            f" {s['price_chg_pct']:>+5.1f}%"
            f" {s['coupon_pct']:>+5.1f}%"
            f" {s['total_pct']:>+6.1f}%"
            f" {s['real_opt']:>+9.1f}%"
            f" {s['real_base']:>+9.1f}%"
        )

    lines += [
        f"  {'─' * (W-2)}",
        f"  Реал.(опт)  = номинал − официальный CPI (9.8%)",
        f"  Реал.(баз)  = номинал − оценка реальной инфляции (~12%)",
    ]

    # Блок 3: Вывод для дайджеста
    lines += [
        "",
        f"  ВЫВОД ДЛЯ ДАЙДЖЕСТА",
        f"  {'─' * (W-2)}",
    ]

    base = next(s for s in scenarios if "Базовый" in s["label"])
    flat = next(s for s in scenarios if "Плоский" in s["label"])

    lines += [
        f"  При базовом сценарии (КС → 13% за 12 мес):",
        f"  Номинальный доход:   {base['total_pct']:+.1f}%",
        f"  Реальный (офиц.):   {base['real_opt']:+.1f}% "
        f"(при CPI 9.8%)",
        f"  Реальный (оценка):  {base['real_base']:+.1f}% "
        f"(при реальной инфляции ~12%)",
        "",
        f"  Без снижения КС (плоский сценарий):",
        f"  Номинальный доход:   {flat['total_pct']:+.1f}% (только купоны)",
        f"  Реальный (оценка):  {flat['real_base']:+.1f}%",
        "",
        f"  → Весь alpha в ОФЗ — это ставка на снижение КС,",
        f"    а не на carry. Carry при реальной инфляции ~12%",
        f"    практически нулевой или отрицательный.",
    ]

    lines.append("═" * W)
    return "\n".join(lines)


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Загружаем данные с MOEX ISS API...\n")

    prices = fetch_ofz_prices(date_from="2024-01-01")
    if prices is None:
        print("Не удалось загрузить цены")
        sys.exit(1)

    # Сохраняем историю цен
    prices.to_csv(DATA_DIR / "ofz26238_prices.csv", index=False)

    # P&L для купивших в августе 2024
    pnl = calc_historical_pnl(prices, entry_date_str="2024-08-01")

    # Текущая цена и доходность
    current_price = prices.iloc[-1]["price_rub"]
    # Приближённая доходность к погашению (простая оценка)
    years_left    = (MATURITY - date.today()).days / 365
    current_yield = 14.8  # используем рыночную доходность из дайджеста

    # Сценарии
    scenarios, dur = scenarios_for_now(
        current_price, current_yield, key_rate=14.5
    )

    # Вывод
    output = format_output(pnl, scenarios, dur, current_price, current_yield)
    print(output)

    # Сохраняем для дайджеста
    result = {
        "generated_at":   datetime.now().isoformat(),
        "secid":          SECID,
        "current_price":  current_price,
        "current_price_pct": current_price / FACE_VAL * 100,
        "duration_years": dur,
        "pnl_aug2024":    pnl,
        "scenarios":      scenarios,
    }

    import json
    with open(DATA_DIR / "ofz_analysis.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n✓ Сохранено: data/ofz_analysis.json")