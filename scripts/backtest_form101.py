"""
scripts/backtest_form101.py

Бэктест гипотезы: «Банки покупают ОФЗ раньше рынка»

Логика:
  1. Берём Form 101 историю — суммарная позиция топ-20 банков
  2. Считаем стрик: N месяцев подряд позиция растёт
  3. При стрике ≥ threshold — фиксируем сигнал
  4. Смотрим: снизил ли ЦБ ставку в следующие 6/12 мес?
  5. Считаем win rate

Запуск: python scripts/backtest_form101.py
"""

import sys
import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_DIR = Path("data")


# ─────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ
# ─────────────────────────────────────────────

def load_form101_signal():
    """
    Загружаем помесячную историю сигнала Form 101.
    Используем уже посчитанный signal.csv (из build_form101_history.py)
    """
    path = DATA_DIR / "form101_signal.csv"
    if not path.exists():
        print("  Нет form101_signal.csv — запусти build_form101_history.py")
        return None

    df = pd.read_csv(path)
    df["month_dt"] = pd.to_datetime(df["month"] + "-01")
    df = df.sort_values("month_dt").reset_index(drop=True)
    return df


def load_cbr_decisions():
    """История решений ЦБ по ставке."""
    path = DATA_DIR / "cbr_decisions.csv"
    df = pd.read_csv(path)
    df["decision_date"] = pd.to_datetime(df["decision_date"])

    # Только реальные изменения ставки
    changes = df[df["rate_change_bps"] != 0].copy()
    changes = changes.sort_values("decision_date")
    return changes


def get_ks_at_date(decisions_df, dt):
    """КС действовавшая на дату."""
    all_decisions = pd.read_csv(DATA_DIR / "cbr_decisions.csv")
    all_decisions["decision_date"] = pd.to_datetime(all_decisions["decision_date"])
    past = all_decisions[all_decisions["decision_date"] <= pd.Timestamp(dt)]
    if past.empty:
        return None
    return float(past.iloc[past["decision_date"].argmax()]["rate_pct"])


# ─────────────────────────────────────────────
# БЭКТЕСТ
# ─────────────────────────────────────────────

def run_backtest(signal_df, decisions_df,
                 streak_threshold=3,
                 horizon_months_list=[6, 9, 12]):
    """
    Для каждого месяца где стрик ≥ threshold:
    - Записываем сигнал
    - Смотрим что случилось через 6/9/12 мес
    - Считаем win rate
    """
    results = []

    for i, row in signal_df.iterrows():
        streak  = int(row["streak"])
        month   = row["month"]
        mdt     = row["month_dt"]
        total   = row["total_mln"]
        change  = row["change_mln"] if not pd.isna(row["change_mln"]) else 0

        # Сигнал: стрик достиг порога (первый месяц на этом уровне)
        prev_streak = int(signal_df.iloc[i-1]["streak"]) if i > 0 else 0
        is_signal   = (streak == streak_threshold and
                       prev_streak < streak_threshold)

        if not is_signal:
            continue

        # КС в момент сигнала
        ks_at_signal = get_ks_at_date(decisions_df, mdt)

        # Смотрим что случилось через каждый горизонт
        horizons = {}
        for h in horizon_months_list:
            horizon_dt = mdt + relativedelta(months=h)

            # Решения ЦБ в этом окне
            future = decisions_df[
                (decisions_df["decision_date"] > mdt) &
                (decisions_df["decision_date"] <= horizon_dt)
            ]
            cuts = future[future["rate_change_bps"] < 0]

            if not cuts.empty:
                first_cut      = cuts.iloc[0]
                months_to_cut  = ((first_cut["decision_date"] - mdt).days / 30.4)
                total_cut_bps  = int(cuts["rate_change_bps"].sum())
                outcome        = "cut"
            else:
                months_to_cut  = None
                total_cut_bps  = 0
                outcome        = "no_cut"

            horizons[f"{h}m"] = {
                "outcome":       outcome,
                "months_to_cut": round(months_to_cut, 1) if months_to_cut else None,
                "total_cut_bps": total_cut_bps,
            }

        results.append({
            "signal_month":  month,
            "signal_date":   mdt,
            "streak":        streak,
            "total_trln":    round(total / 1_000_000, 1),
            "change_bln":    round(change / 1000, 1),
            "ks_at_signal":  ks_at_signal,
            "horizons":      horizons,
        })

    return results


# ─────────────────────────────────────────────
# ДЕТАЛЬНЫЙ АНАЛИЗ ТЕКУЩЕГО СИГНАЛА
# ─────────────────────────────────────────────

def analyze_current_signal(signal_df, decisions_df):
    """
    Детальный разбор текущего (единственного) сигнала.
    Август 2024 — начало цикла накопления.
    """
    print("\n" + "═"*63)
    print("  ДЕТАЛЬНЫЙ АНАЛИЗ СИГНАЛА 2024–2026")
    print("═"*63)

    # Находим начало стрика накопления
    acc_months = signal_df[signal_df["growing"] == True].copy()
    consec = []
    streak_start = None
    max_streak = 0
    current_streak = []

    for _, row in signal_df.iterrows():
        if row["growing"]:
            current_streak.append(row)
            if len(current_streak) >= 3 and streak_start is None:
                streak_start = current_streak[0]["month_dt"]
        else:
            if len(current_streak) > max_streak:
                max_streak   = len(current_streak)
                consec       = current_streak[:]
            current_streak = []

    # Если текущий стрик самый длинный
    if len(current_streak) > max_streak:
        consec = current_streak[:]

    print(f"\n  Фаза накопления:")
    print(f"  {'Месяц':<12} {'Позиция трлн':>13} {'Изменение млрд':>16} {'Стрик':>8}")
    print(f"  {'─'*52}")

    for row in consec:
        chg = row["change_mln"]/1000 if not pd.isna(row["change_mln"]) else 0
        grow = "↑" if row["growing"] else "↓"
        print(
            f"  {row['month']:<12}"
            f" {row['total_mln']/1_000_000:>12.1f} трлн"
            f" {chg:>+13.0f} млрд"
            f" {int(row['streak']):>6} мес {grow}"
        )

    # Что случилось после
    if consec:
        start_dt = consec[0]["month_dt"]
        print(f"\n  Начало накопления: {consec[0]['month']}")
        print(f"  Длина стрика: {len(consec)} мес")
        print(f"  Суммарно докуплено: ₽{sum(r['change_mln'] for r in consec if not pd.isna(r['change_mln']))/1000:.0f} млрд")

        # Решения ЦБ после начала
        future = decisions_df[
            (decisions_df["decision_date"] > start_dt) &
            (decisions_df["rate_change_bps"] < 0)
        ]

        print(f"\n  Решения ЦБ после начала накопления:")
        print(f"  {'Дата':<14} {'КС%':>6} {'Δ бп':>7}  {'Лаг':>8}")
        print(f"  {'─'*40}")
        for _, dec in future.iterrows():
            lag = (dec["decision_date"] - start_dt).days / 30.4
            print(
                f"  {str(dec['decision_date'].date()):<14}"
                f" {dec['rate_pct']:>5.1f}%"
                f" {int(dec['rate_change_bps']):>+6} бп"
                f" {lag:>6.0f} мес"
            )


# ─────────────────────────────────────────────
# ВЫВОД РЕЗУЛЬТАТОВ
# ─────────────────────────────────────────────

def format_backtest(results, streak_threshold, signal_df, decisions_df):
    W = 63
    print()
    print("═" * W)
    print(f"  БЭКТЕСТ: БАНКИ ПОКУПАЮТ РАНЬШЕ РЫНКА")
    print(f"  Данные: Form 101 · {signal_df['month'].min()} — {signal_df['month'].max()}")
    print(f"  Порог сигнала: стрик накопления ≥ {streak_threshold} мес")
    print("═" * W)

    if not results:
        print(f"\n  ⚠ Ни одного сигнала с порогом {streak_threshold} мес")
        print(f"  Снизь порог до 2 или добавь данные за 2016–2023")
        print("\n  Тем не менее — анализируем имеющийся паттерн:\n")
        analyze_current_signal(signal_df, decisions_df)
        return

    print(f"\n  Найдено сигналов: {len(results)}\n")

    for i, r in enumerate(results, 1):
        print(f"  Сигнал #{i} · {r['signal_month']}")
        print(f"  {'─' * (W-2)}")
        print(f"  КС в момент сигнала: {r['ks_at_signal']}%")
        print(f"  Стрик накопления:    {r['streak']} мес")
        print(f"  Позиция топ-20:      ₽{r['total_trln']} трлн")
        print(f"  Изменение за месяц:  ₽{r['change_bln']:+.0f} млрд")
        print()

        for horizon_key, h in r["horizons"].items():
            icon = "✓" if h["outcome"] == "cut" else "✗"
            if h["outcome"] == "cut":
                result_str = (f"Снижение через {h['months_to_cut']:.0f} мес "
                              f"(−{abs(h['total_cut_bps'])} бп итого)")
            else:
                result_str = "Снижения не было"
            print(f"  {icon} Горизонт {horizon_key}: {result_str}")
        print()

    # Статистика по горизонтам
    print(f"  ИТОГОВАЯ СТАТИСТИКА")
    print(f"  {'─' * (W-2)}")
    for horizon_key in ["6m", "9m", "12m"]:
        cuts = sum(1 for r in results
                   if r["horizons"].get(horizon_key, {}).get("outcome") == "cut")
        total = len(results)
        wr    = round(cuts / total * 100) if total > 0 else 0
        lags  = [r["horizons"][horizon_key]["months_to_cut"]
                 for r in results
                 if r["horizons"].get(horizon_key, {}).get("months_to_cut")]
        lag_str = f"медиана лага {np.median(lags):.0f} мес" if lags else ""
        print(f"  Горизонт {horizon_key}: {cuts}/{total} снижений = {wr}% win rate  {lag_str}")

    print()
    print(f"  ВЫВОД ДЛЯ ПРОДУКТА")
    print(f"  {'─' * (W-2)}")

    # Лучший горизонт
    best_wrs = {}
    for hk in ["6m", "9m", "12m"]:
        cuts  = sum(1 for r in results
                    if r["horizons"].get(hk, {}).get("outcome") == "cut")
        best_wrs[hk] = cuts / len(results) * 100 if results else 0

    best_h  = max(best_wrs, key=best_wrs.get)
    best_wr = best_wrs[best_h]
    n_cuts  = sum(1 for r in results
                  if r["horizons"].get(best_h, {}).get("outcome") == "cut")

    if best_wr >= 60:
        print(f"  Когда топ-20 банков накапливают ≥{streak_threshold} мес подряд,")
        print(f"  ЦБ снижал ставку в горизонте {best_h}:")
        print(f"  → {n_cuts} из {len(results)} случаев = {best_wr:.0f}% win rate")
        print()
        print(f"  На текущих данных (35 мес) это ОДИН кейс.")
        print(f"  Для надёжной статистики нужны данные с 2016 года.")
        print(f"  Запусти: python scripts/build_form101_history.py --from 2016")
    else:
        print(f"  На текущих данных (35 мес) статистически недостаточно.")
        print(f"  Нужны данные с 2016 года — запусти часть Б.")

    print("═" * W)

    # Детальный анализ единственного сигнала
    analyze_current_signal(signal_df, decisions_df)


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Загружаем данные...\n")

    signal_df   = load_form101_signal()
    decisions_df = load_cbr_decisions()

    if signal_df is None:
        sys.exit(1)

    print(f"  Form 101: {len(signal_df)} месяцев "
          f"({signal_df['month'].min()} — {signal_df['month'].max()})")
    print(f"  Решения ЦБ: {len(decisions_df)} изменений ставки")

    # Показываем всю историю стриков
    print(f"\n  История стриков накопления:")
    print(f"  {'Месяц':<12} {'Всего трлн':>11} {'Стрик':>7} {'Рост':>6}")
    print(f"  {'─'*38}")
    for _, row in signal_df.iterrows():
        grow = "↑" if row["growing"] else "↓"
        hl   = " ◄" if int(row["streak"]) >= 3 else ""
        print(
            f"  {row['month']:<12}"
            f" {row['total_mln']/1_000_000:>10.1f} трлн"
            f" {int(row['streak']):>6} мес"
            f" {grow}{hl}"
        )

    # Бэктест с порогом 3 мес
    print("\nЗапускаем бэктест (порог: 3 месяца)...\n")
    results = run_backtest(signal_df, decisions_df, streak_threshold=3)
    format_backtest(results, 3, signal_df, decisions_df)

    # Сохраняем результаты
    output = {
        "generated_at":    datetime.now().isoformat(),
        "data_range":      f"{signal_df['month'].min()} — {signal_df['month'].max()}",
        "months_available": len(signal_df),
        "streak_threshold": 3,
        "signals_found":   len(results),
        "note": ("Недостаточно данных для надёжной статистики. "
                 "Нужны данные с 2016 года (часть Б)."),
        "results": [
            {k: v for k, v in r.items() if k != "signal_date"}
            for r in results
        ],
    }

    out_path = DATA_DIR / "backtest_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        import json
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n✓ Результаты сохранены: {out_path}")
    print("\nЧасть Б: python scripts/build_form101_history.py --from 2016")