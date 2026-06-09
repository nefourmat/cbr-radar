"""
scripts/pattern_engine.py

Находит исторические паттерны похожие на текущую ситуацию.
Рассчитывает вероятность снижения КС на основе истории.

Запуск: python scripts/pattern_engine.py
Сохраняет: data/pattern_signal.json
"""

import json
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from parsers.gcurve import get_last_gcurve, get_key_rate

DATA_DIR = Path("data")

# ─────────────────────────────────────────────
# ИСКЛЮЧАЕМЫЕ ПЕРИОДЫ
# Нетипичные условия искажают паттерны
# ─────────────────────────────────────────────
EXCLUDED = [
    ("2020-03-01", "2020-07-31"),   # COVID острая фаза
    ("2022-02-24", "2022-05-31"),   # СВО первый шок (резкий хайк)
    ("2022-06-01", "2022-12-31"),   # быстрый разворот СВО — не типичный цикл
]


# ─────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ
# ─────────────────────────────────────────────

def load_gcurve():
    df = pd.read_csv(DATA_DIR / "gcurve_history_full.csv")
    if "maturity_years" in df.columns:
        df = df.rename(columns={
            "maturity_years": "срок_лет",
            "yield_pct":      "доходность_пct"
        })
    df["dt"] = pd.to_datetime(df["date"], format="%d.%m.%Y")
    return df


def load_auctions():
    df = pd.read_csv(DATA_DIR / "auctions_all.csv")
    df["дата"] = pd.to_datetime(df["дата"])
    return df


def load_decisions():
    df = pd.read_csv(DATA_DIR / "cbr_decisions.csv")
    df["decision_date"] = pd.to_datetime(df["decision_date"])
    # Оставляем только реальные решения (изменения + текущий уровень)
    df = df[df["rate_change_bps"] != 0].copy()
    return df.sort_values("decision_date")


def is_excluded(dt):
    for start, end in EXCLUDED:
        if pd.Timestamp(start) <= dt <= pd.Timestamp(end):
            return True
    return False


# ─────────────────────────────────────────────
# СТРОИМ ВЕКТОР СОСТОЯНИЯ ДЛЯ КАЖДОЙ НЕДЕЛИ
# ─────────────────────────────────────────────

def get_ks_at_date(decisions, dt):
    """КС действовавшая на конкретную дату."""
    past = decisions[decisions["decision_date"] <= dt]
    if past.empty:
        return None
    return past.iloc[-1]["rate_pct"]


def get_curve_state(gcurve, dt):
    """Состояние кривой на дату (ближайший торговый день)."""
    window = gcurve[
        (gcurve["dt"] >= dt - timedelta(days=7)) &
        (gcurve["dt"] <= dt)
    ]
    if window.empty:
        return None

    day_df = window[window["dt"] == window["dt"].max()]

    def get_yield(срок):
        row = day_df[day_df["срок_лет"] == срок]
        return row["доходность_пct"].values[0] if not row.empty else None

    y1  = get_yield(1.0)
    y2  = get_yield(2.0)
    y10 = get_yield(10.0)

    if y1 is None or y2 is None or y10 is None:
        return None

    return {
        "slope_2_10":  round(y10 - y2, 3),   # наклон кривой
        "y1":          y1,                     # 1Y доходность
        "y2":          y2,                     # 2Y доходность
        "y10":         y10,                    # 10Y доходность
        "min_yield":   day_df["доходность_пct"].min(),
    }


def get_btc_4w(auctions, dt):
    """Средний bid-to-cover за последние 4 недели до даты."""
    window = auctions[
        (auctions["дата"] >= dt - timedelta(weeks=4)) &
        (auctions["дата"] <= dt)
    ]
    if window.empty or "bid_to_cover" not in window.columns:
        return None
    btc = window["bid_to_cover"].dropna()
    return round(btc.mean(), 3) if len(btc) > 0 else None

def get_cycle_direction(decisions, dt, lookback=3):
    past = decisions[decisions["decision_date"] <= dt].tail(lookback)
    if past.empty:
        return 0
    avg = past["rate_change_bps"].mean()
    return 1 if avg < 0 else (-1 if avg > 0 else 0)


def build_weekly_states(gcurve, auctions, decisions):
    """
    Строим вектор состояния для каждой пятницы в истории.
    Вектор: [slope_2_10, expected_cut, btc_4w, ks_level]
    """
    states = []

    # Берём все пятницы в диапазоне данных
    min_dt = gcurve["dt"].min()
    max_dt = gcurve["dt"].max() - timedelta(weeks=12)

    current = min_dt
    while current <= max_dt:
        # Только пятницы
        if current.weekday() == 4:
            if not is_excluded(current):
                ks = get_ks_at_date(decisions, current)
                curve = get_curve_state(gcurve, current)
                btc = get_btc_4w(auctions, current)

                if ks and curve and btc:
                    expected_cut = round(ks - curve["min_yield"], 2)
                    cycle_dir = get_cycle_direction(decisions, current)
                    states.append({
                        "date":          current,
                        "ks":            ks,
                        "slope_2_10":    curve["slope_2_10"],
                        "y1":            curve["y1"],
                        "y10":           curve["y10"],
                        "min_yield":     curve["min_yield"],
                        "expected_cut":  expected_cut,
                        "btc_4w":        btc,
                        "cycle_direction": cycle_dir,
                    })
        current += timedelta(days=1)

    return pd.DataFrame(states)


# ─────────────────────────────────────────────
# ПОИСК ПОХОЖИХ ПАТТЕРНОВ
# ─────────────────────────────────────────────

def euclidean_distance(row, current_state, weights):
    """
    Взвешенное евклидово расстояние между двумя состояниями.
    Чем меньше — тем похожее.
    """
    total = 0
    for feature, weight in weights.items():
        if feature in row and feature in current_state:
            diff = (row[feature] - current_state[feature]) / (weight["scale"])
            total += (diff ** 2) * weight["w"]
    return np.sqrt(total)


def find_similar_weeks(states_df, current_state, top_n=5):
    """Находим N наиболее похожих исторических недель."""

    # Веса и масштабы — что важнее для определения паттерна
    weights = {
        "slope_2_10":     {"w": 2.0, "scale": 1.0},
        "expected_cut":   {"w": 2.0, "scale": 1.0},
        "btc_4w":         {"w": 1.5, "scale": 1.0},
        "ks":             {"w": 0.5, "scale": 5.0},
        "cycle_direction": {"w": 3.0, "scale": 1.0},  # ВАЖНО: фаза цикла
    }

    distances = []
    for _, row in states_df.iterrows():
        dist = euclidean_distance(row, current_state, weights)
        distances.append(dist)

    states_df = states_df.copy()
    states_df["distance"] = distances
    return states_df.nsmallest(top_n, "distance")


def what_happened_next(similar_weeks, decisions, weeks_ahead=10):
    """
    Для каждой похожей недели смотрим:
    было ли снижение КС в течение N недель?
    """
    results = []
    for _, week in similar_weeks.iterrows():
        dt       = week["date"]
        deadline = dt + timedelta(weeks=weeks_ahead)

        # Решения ЦБ после этой недели
        future = decisions[
            (decisions["decision_date"] > dt) &
            (decisions["decision_date"] <= deadline)
        ]

        cuts = future[future["rate_change_bps"] < 0]

        if not cuts.empty:
            first_cut     = cuts.iloc[0]
            weeks_to_cut  = (first_cut["decision_date"] - dt).days / 7
            cut_magnitude = abs(int(first_cut["rate_change_bps"]))
            outcome       = "cut"
        else:
            weeks_to_cut  = None
            cut_magnitude = 0
            outcome       = "no_cut"

        results.append({
            "date":          dt,
            "distance":      week["distance"],
            "slope_2_10":    week["slope_2_10"],
            "expected_cut":  week["expected_cut"],
            "btc_4w":        week["btc_4w"],
            "ks":            week["ks"],
            "outcome":       outcome,
            "weeks_to_cut":  weeks_to_cut,
            "cut_bps":       cut_magnitude,
        })

    return results




# ─────────────────────────────────────────────
# ТЕКУЩЕЕ СОСТОЯНИЕ
# ─────────────────────────────────────────────

def get_current_state(key_rate, gcurve_hist, auctions):
    """Текущее состояние рынка."""
    # Последняя кривая
    today     = gcurve_hist["dt"].max()
    curve_now = get_curve_state(gcurve_hist, today)
    btc_now   = get_btc_4w(auctions, today)

    if not curve_now or not btc_now:
        return None

    return {
        "ks":           key_rate,
        "slope_2_10":   curve_now["slope_2_10"],
        "y1":           curve_now["y1"],
        "y10":          curve_now["y10"],
        "min_yield":    curve_now["min_yield"],
        "expected_cut": round(key_rate - curve_now["min_yield"], 2),
        "btc_4w":       btc_now,
        "date":         today,
    }


# ─────────────────────────────────────────────
# ВЫВОД — ПИРАМИДА МИНТО
# ─────────────────────────────────────────────

def format_output(current_state, results, key_rate):
    W = 63
    lines = []

    # Статистика
    total     = len(results)
    cuts      = [r for r in results if r["outcome"] == "cut"]
    no_cuts   = [r for r in results if r["outcome"] == "no_cut"]
    prob      = round(len(cuts) / total * 100) if total > 0 else 0
    med_weeks = (round(np.median([r["weeks_to_cut"] for r in cuts]), 1)
                 if cuts else None)
    med_bps   = (round(np.median([r["cut_bps"] for r in cuts]))
                 if cuts else None)

    # ── УРОВЕНЬ 1: ВЫВОД ──────────────────────────────────────────
    lines += [
        "",
        "═" * W,
        "  ИСТОРИЧЕСКИЙ ПАТТЕРН",
        "═" * W,
    ]

    if prob >= 65:
        вывод = (f"Исторически похожие ситуации в {prob}% случаев "
                 f"приводили к снижению КС")
    elif prob >= 45:
        вывод = (f"Исторические данные дают смешанный сигнал ({prob}% снижений)")
    else:
        вывод = (f"Исторически похожие ситуации редко приводили к снижению "
                 f"({prob}% случаев)")

    lines.append(f"\n  {вывод}")

    if med_weeks and med_bps:
        lines.append(
            f"  Медиана: снижение через {med_weeks:.0f} недель "
            f"на {med_bps:.0f} бп"
        )

    # ── УРОВЕНЬ 2: ПОХОЖИЕ ПАТТЕРНЫ ───────────────────────────────
    lines += [
        f"\n  ПОХОЖИЕ НЕДЕЛИ В ИСТОРИИ (топ-{total}):",
        f"  {'─' * (W-2)}",
        f"  {'Дата':<13} {'Наклон':>7} {'BTC':>6} "
        f"{'Ожид.↓':>7} {'КС':>6}  {'Исход':<20}",
        f"  {'─' * (W-2)}",
    ]

    for r in results:
        if r["outcome"] == "cut":
            исход = f"↓ через {r['weeks_to_cut']:.0f}нед −{r['cut_bps']}бп"
        else:
            исход = "→ без изменений"

        lines.append(
            f"  {str(r['date'].date()):<13}"
            f" {r['slope_2_10']:>+6.2f}%"
            f" {r['btc_4w']:>6.2f}×"
            f" {r['expected_cut']:>6.2f}%"
            f" {r['ks']:>5.1f}%"
            f"  {исход}"
        )

    # Текущее состояние для сравнения
    lines += [
        f"  {'─' * (W-2)}",
        f"  {'СЕЙЧАС':<13}"
        f" {current_state['slope_2_10']:>+6.2f}%"
        f" {current_state['btc_4w']:>6.2f}×"
        f" {current_state['expected_cut']:>6.2f}%"
        f" {key_rate:>5.1f}%",
    ]

    # ── УРОВЕНЬ 3: ИНТЕРПРЕТАЦИЯ ───────────────────────────────────
    lines += [
        f"\n  ИНТЕРПРЕТАЦИЯ:",
        f"  {'─' * (W-2)}",
    ]

    lines.append(
        f"  Найдено {total} похожих периодов "
        f"(исключены: COVID 2020, СВО шок фев–май 2022, "
        f"разворот июн–дек 2022)"
    )

    if prob >= 65:
        lines.append(
            f"  Большинство ({len(cuts)}/{total}) завершились снижением КС"
        )
    elif prob >= 45:
        lines.append(
            f"  Результаты смешанные: {len(cuts)} снижений, "
            f"{len(no_cuts)} без изменений"
        )
    else:
        lines.append(
            f"  Большинство ({len(no_cuts)}/{total}) не привели к снижению"
        )

    if total < 5:
        lines.append(
            f"  ⚠ Мало данных для уверенной статистики "
            f"(нужно 10+ паттернов)"
        )
        lines.append(
            f"  Добавить данные: python scripts/build_history.py "
            f"(расширить до 10 лет)"
        )

    lines.append("═" * W)

    return "\n".join(lines), prob, med_weeks, med_bps


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Загружаем исторические данные...\n")

    gcurve    = load_gcurve()
    auctions  = load_auctions()
    decisions = load_decisions()

    print(f"  G-кривая:  {gcurve['dt'].dt.date.min()} — "
          f"{gcurve['dt'].dt.date.max()} "
          f"({gcurve['dt'].nunique()} дней)")
    print(f"  Аукционы:  {auctions['дата'].dt.date.min()} — "
          f"{auctions['дата'].dt.date.max()} "
          f"({len(auctions)} аукционов)")
    print(f"  Решения КС: {len(decisions)} решений\n")

    # Текущее состояние
    key_rate = get_key_rate()
    if key_rate is None:
        key_rate = 14.5

    print("Строим текущий вектор состояния...")
    current = get_current_state(key_rate, gcurve, auctions)
    if not current:
        print("Не удалось получить текущее состояние")
        sys.exit(1)

    print(f"  Наклон кривой 2–10:  {current['slope_2_10']:+.2f}%")
    print(f"  Ожидаемое снижение:  {current['expected_cut']:.2f}%")
    print(f"  Bid-to-cover (4нед): {current['btc_4w']:.2f}×")
    print(f"  КС текущая:          {current['ks']:.2f}%\n")

    # Строим исторические состояния
    print("Строим исторические состояния (каждая пятница)...")
    states = build_weekly_states(gcurve, auctions, decisions)
    print(f"  Чистых периодов для анализа: {len(states)} недель\n")

    if states.empty:
        print("Недостаточно данных для анализа")
        sys.exit(1)

    # Ищем похожие паттерны
    print("Ищем похожие паттерны...")
    decisions_clean = decisions[decisions["rate_change_bps"] != 0].copy()
    current_for_match = {
        "slope_2_10":      current["slope_2_10"],
        "expected_cut":    current["expected_cut"],
        "btc_4w":          current["btc_4w"],
        "ks":              current["ks"],
        "cycle_direction": get_cycle_direction(
                            decisions_clean,
                            current["date"]
                        ),
    }
    similar = find_similar_weeks(states, current_for_match, top_n=5)

    # Смотрим что случилось после
    print("Анализируем исходы (горизонт: 10 недель)...\n")
    results = what_happened_next(similar, decisions, weeks_ahead=10)

    # Форматируем вывод
    output, prob, med_weeks, med_bps = format_output(
        current, results, key_rate
    )
    print(output)

    # Сохраняем сигнал для дайджеста
    signal = {
        "generated_at":  datetime.now().isoformat(),
        "current_state": {
            k: float(v) if isinstance(v, (int, float, np.floating))
               else str(v)
            for k, v in current.items()
        },
        "probability_cut_10w": prob,
        "median_weeks_to_cut": float(med_weeks) if med_weeks else None,
        "median_cut_bps":      float(med_bps) if med_bps else None,
        "n_similar":           len(results),
        "n_cuts":              sum(1 for r in results if r["outcome"] == "cut"),
        "matches": [
            {
                "date":         str(r["date"].date()),
                "distance":     round(r["distance"], 3),
                "outcome":      r["outcome"],
                "weeks_to_cut": r["weeks_to_cut"],
                "cut_bps":      r["cut_bps"],
            }
            for r in results
        ],
    }

    signal_path = DATA_DIR / "pattern_signal.json"
    with open(signal_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Сигнал сохранён: {signal_path}")
    print(f"\nДля дайджеста: вероятность снижения КС = {prob}% "
          f"в горизонте 10 недель")