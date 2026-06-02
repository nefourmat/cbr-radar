"""
scripts/bond_screener.py

Мультибондовый скринер ОФЗ с поправкой на supply overhang.

Ключевая идея:
  Теоретический P&L (КС −150бп → yield −150бп) завышен.
  При низком спросе на аукционах Минфин давит на цены —
  pass-through ratio < 1.0 (yield падает медленнее КС).

  Supply pressure = 1 − BTC_current / BTC_normal
  Pass-through    = 1 − supply_pressure × 0.5
  Adj. yield cut  = cut_bps × pass_through

Запуск: python scripts/bond_screener.py
"""

import sys
import json
import requests
import pandas as pd
from datetime import date, datetime
from pathlib import Path

DATA_DIR   = Path("data")
FACE_VALUE = 1000
BTC_NORMAL = 1.5   # исторически нормальный bid-to-cover

SCENARIOS = [
    {"label": "КС → 13.0%", "cut_bps": -150},
    {"label": "КС → 12.0%", "cut_bps": -250},
    {"label": "КС → 11.0%", "cut_bps": -350},
    {"label": "Flat (hold)", "cut_bps":    0},
]


# ─────────────────────────────────────────────
# SUPPLY PRESSURE
# ─────────────────────────────────────────────

def calc_supply_metrics(btc_current=None):
    """
    Считаем supply pressure и pass-through из данных аукционов.
    Если btc_current не передан — берём из auctions_all.csv.
    """
    if btc_current is None:
        try:
            df = pd.read_csv(DATA_DIR / "auctions_all.csv")
            df["дата"] = pd.to_datetime(df["дата"])
            cutoff = df["дата"].max() - pd.Timedelta(weeks=4)
            recent = df[df["дата"] >= cutoff]
            btc_current = recent["bid_to_cover"].mean() if not recent.empty else 0.5
        except Exception:
            btc_current = 0.5

    # Supply pressure [0, 1]: 0 = нет давления, 1 = максимальное
    supply_pressure = max(0.0, min(1.0, 1 - btc_current / BTC_NORMAL))

    # Pass-through ratio [0.5, 1.0]
    # При нулевом давлении: 1.0 (полный pass-through)
    # При максимальном давлении: 0.5 (только 50% снижения КС → yield)
    pass_through = 1.0 - supply_pressure * 0.5

    return {
        "btc_current":      round(btc_current, 2),
        "btc_normal":       BTC_NORMAL,
        "supply_pressure":  round(supply_pressure, 2),
        "pass_through":     round(pass_through, 2),
        "overhang_active":  btc_current < BTC_NORMAL,
        "entry_signal":     btc_current >= BTC_NORMAL,
    }


# ─────────────────────────────────────────────
# MOEX ДАННЫЕ
# ─────────────────────────────────────────────

def fetch_ofz_universe():
    url  = ("https://iss.moex.com/iss/engines/stock/markets/bonds"
            "/boards/TQOB/securities.json?iss.meta=off"
            "&iss.only=securities,marketdata")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    sec_df = pd.DataFrame(data["securities"]["data"],
                          columns=data["securities"]["columns"])
    md_df  = pd.DataFrame(data["marketdata"]["data"],
                          columns=data["marketdata"]["columns"])
    df     = sec_df.merge(md_df[["SECID","LAST","YIELD"]],
                          on="SECID", how="left")

    today = date.today()
    df = df[df["SECID"].str.startswith("SU26")]
    df["MATDATE"]    = pd.to_datetime(df["MATDATE"], errors="coerce")
    df               = df.dropna(subset=["MATDATE"])
    df["years_left"] = df["MATDATE"].dt.date.apply(
        lambda d: (d - today).days / 365.0
    )
    df = df[df["years_left"] > 3]
    df = df[df["LAST"].notna() & (df["LAST"] > 0)]
    df = df[df["COUPONPERCENT"].notna() & (df["COUPONPERCENT"] > 0)]
    return df


# ─────────────────────────────────────────────
# ФИНАНСОВЫЕ РАСЧЁТЫ
# ─────────────────────────────────────────────

def calc_ytm(price_pct, coupon_pct, years_left):
    price  = price_pct / 100 * 100
    coupon = coupon_pct / 100 * 100 / 2
    n      = max(1, int(years_left * 2))
    ytm    = coupon_pct / 100

    for _ in range(100):
        r   = ytm / 2
        pv  = sum((coupon + (100 if t == n else 0)) / (1 + r) ** t
                  for t in range(1, n + 1))
        dpv = sum(-t * (coupon + (100 if t == n else 0))
                  / ((1 + r) ** (t + 1)) / 2
                  for t in range(1, n + 1))
        f   = pv - price
        ytm = max(0.001, min(0.50, ytm - f / (dpv or -1)))
        if abs(f) < 0.0001:
            break

    return round(ytm * 100, 3)


def calc_duration(coupon_pct, ytm_pct, years_left):
    n   = max(1, int(years_left * 2))
    r   = ytm_pct / 100 / 2
    c   = coupon_pct / 100 * 100 / 2
    num = den = 0
    for t in range(1, n + 1):
        cf   = c + (100 if t == n else 0)
        pv   = cf / (1 + r) ** t
        num += (t / 2) * pv
        den += pv
    return round(num / den if den > 0 else years_left, 2)


def calc_pnl(price_pct, coupon_pct, duration, cut_bps,
             pass_through=1.0, horizon_years=1.0):
    """
    P&L с поправкой на supply overhang.

    theoretical_yield_cut = cut_bps (полный pass-through)
    adjusted_yield_cut    = cut_bps × pass_through (реалистично)
    """
    price = price_pct / 100 * FACE_VALUE

    # Теоретический
    th_dy         = cut_bps / 10000
    th_price_chg  = -duration * th_dy * price
    th_coupon     = coupon_pct / 100 * FACE_VALUE * horizon_years
    th_total      = th_price_chg + th_coupon

    # Скорректированный (с учётом supply overhang)
    adj_dy        = (cut_bps * pass_through) / 10000
    adj_price_chg = -duration * adj_dy * price
    adj_coupon    = th_coupon  # купоны не меняются
    adj_total     = adj_price_chg + adj_coupon

    def pct(x): return round(x / price * 100, 1)

    return {
        "theoretical_pct": pct(th_total),
        "adjusted_pct":    pct(adj_total),
        "price_chg_th":    pct(th_price_chg),
        "price_chg_adj":   pct(adj_price_chg),
        "coupon_pct":      pct(th_coupon),
        "delta_pct":       pct(adj_total - th_total),  # потери от overhang
    }


# ─────────────────────────────────────────────
# СКРИНЕР
# ─────────────────────────────────────────────

def run_screener(supply):
    df = fetch_ofz_universe()
    print(f"  Найдено {len(df)} ОФЗ (фикс. купон, срок > 3 лет)\n")

    results = []
    for _, row in df.iterrows():
        price_pct  = float(row["LAST"])
        coupon_pct = float(row["COUPONPERCENT"])
        years_left = float(row["years_left"])
        matdate    = row["MATDATE"].date()

        moex_yield = row.get("YIELD")
        ytm = (round(float(moex_yield), 3)
               if moex_yield and float(moex_yield) > 0
               else calc_ytm(price_pct, coupon_pct, years_left))

        dur = calc_duration(coupon_pct, ytm, years_left)

        scenarios = {}
        for sc in SCENARIOS:
            scenarios[sc["label"]] = calc_pnl(
                price_pct, coupon_pct, dur,
                sc["cut_bps"],
                pass_through=supply["pass_through"],
            )

        results.append({
            "secid":       row["SECID"],
            "shortname":   row.get("SHORTNAME", row["SECID"]),
            "matdate":     matdate,
            "years_left":  round(years_left, 1),
            "price_pct":   round(price_pct, 1),
            "coupon_pct":  round(coupon_pct, 2),
            "ytm":         ytm,
            "duration":    dur,
            "scenarios":   scenarios,
        })

    results.sort(key=lambda x: x["duration"], reverse=True)
    return results


# ─────────────────────────────────────────────
# ВЫВОД
# ─────────────────────────────────────────────

def format_output(results, supply):
    W = 80
    L = []
    pt = supply["pass_through"]

    L += [
        "",
        "═" * W,
        "  МУЛЬТИБОНДОВЫЙ СКРИНЕР ОФЗ",
        f"  Данные: MOEX ISS · {date.today()}  ·  Горизонт: 12 мес",
        "═" * W,
    ]

    # Supply overhang блок
    sp_pct  = round(supply["supply_pressure"] * 100)
    btc_cur = supply["btc_current"]
    L += [
        "",
        f"  SUPPLY OVERHANG",
        f"  {'─'*(W-2)}",
        f"  BTC текущий:        {btc_cur:.2f}×  (норма ≥ {BTC_NORMAL}×)",
        f"  Давление предложения: {sp_pct}%",
        f"  Pass-through ratio:  {pt:.2f}  "
        f"(yield падает на {round(pt*100)}% от снижения КС)",
        "",
    ]

    if supply["overhang_active"]:
        discount = round((1 - pt) * 100)
        L += [
            f"  ⚠ Overhang АКТИВЕН: теоретический P&L завышен на ~{discount}%",
            f"  Таблица показывает: Теория / Реально (с поправкой)",
            f"  Сигнал входа: BTC > {BTC_NORMAL}× на аукционах",
        ]
    else:
        L += [
            f"  ✓ Overhang отсутствует: теория = практика",
        ]

    # Таблица
    L += [
        "",
        f"  {'Серия':<10} {'Погаш':>6} {'Дюр':>5} {'YTM':>6}  "
        f"{'→13% (теор/реал)':>18}  "
        f"{'→11% (теор/реал)':>18}  "
        f"{'Flat':>7}",
        f"  {'─'*78}",
    ]

    for r in results:
        mat_yr = r["matdate"].strftime("%Y")
        sc13   = r["scenarios"]["КС → 13.0%"]
        sc11   = r["scenarios"]["КС → 11.0%"]
        scF    = r["scenarios"]["Flat (hold)"]

        L.append(
            f"  {r['shortname']:<10} {mat_yr:>6} "
            f"{r['duration']:>4.1f}л {r['ytm']:>5.2f}%  "
            f"  {sc13['theoretical_pct']:>+6.1f}%/{sc13['adjusted_pct']:>+6.1f}%  "
            f"  {sc11['theoretical_pct']:>+6.1f}%/{sc11['adjusted_pct']:>+6.1f}%  "
            f"  {scF['adjusted_pct']:>+6.1f}%"
        )

    L += [f"  {'─'*78}",
          "  Теор = без поправки  ·  Реал = с поправкой на supply overhang"]

    # Топ-3 по скорректированному P&L при КС→13%
    top3 = sorted(results,
                  key=lambda x: x["scenarios"]["КС → 13.0%"]["adjusted_pct"],
                  reverse=True)[:3]

    L += [
        "",
        f"  ТОП-3 РЕАЛЬНЫЙ P&L (КС → 13%, BTC = {btc_cur:.2f}×)",
        f"  {'─'*78}",
    ]

    for i, r in enumerate(top3, 1):
        sc13 = r["scenarios"]["КС → 13.0%"]
        scF  = r["scenarios"]["Flat (hold)"]
        loss = abs(round(sc13["theoretical_pct"] - sc13["adjusted_pct"], 1))
        L += [
            "",
            f"  {i}. {r['shortname']}  ·  погаш {r['matdate']}  "
            f"·  дюрация {r['duration']}л  ·  купон {r['coupon_pct']:.2f}%",
            f"     Цена: {r['price_pct']:.1f}%  ·  YTM: {r['ytm']:.2f}%",
            f"     Теоретически:      {sc13['theoretical_pct']:>+.1f}%",
            f"     Реально (overhang): {sc13['adjusted_pct']:>+.1f}%  "
            f"(−{loss}% потери от слабого спроса)",
            f"     Без снижения КС:   {scF['adjusted_pct']:>+.1f}% (только купоны)",
        ]

    # Когда overhang исчезнет
    full_pt_top = sorted(results,
                         key=lambda x: x["scenarios"]["КС → 13.0%"]["theoretical_pct"],
                         reverse=True)[0]
    sc_full = full_pt_top["scenarios"]["КС → 13.0%"]

    L += [
        "",
        f"  КАК ИЗМЕНИТСЯ P&L КОГДА BTC > {BTC_NORMAL}× (overhang исчезнет)",
        f"  {'─'*78}",
        f"  Pass-through вернётся к 1.0 — теория = практика",
        f"  Лучшая бумага ({full_pt_top['shortname']}): "
        f"{sc13['adjusted_pct']:+.1f}% → {sc_full['theoretical_pct']:+.1f}%",
        f"  Именно поэтому BTC > {BTC_NORMAL}× — наш главный сигнал входа.",
        "",
        "═" * W,
        "",
    ]

    return "\n".join(L)


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Загружаем данные с MOEX ISS...")
    supply = calc_supply_metrics()
    print(f"  BTC текущий: {supply['btc_current']:.2f}×  "
          f"Supply pressure: {round(supply['supply_pressure']*100)}%  "
          f"Pass-through: {supply['pass_through']:.2f}\n")

    results = run_screener(supply)
    output  = format_output(results, supply)
    print(output)

    # Сохраняем
    # Сохраняем
    out = {
        "generated_at":  datetime.now().isoformat(),
        "supply_metrics": {
            "btc_current":     float(supply["btc_current"]),
            "btc_normal":      float(supply["btc_normal"]),
            "supply_pressure": float(supply["supply_pressure"]),
            "pass_through":    float(supply["pass_through"]),
            "overhang_active": bool(supply["overhang_active"]),
            "entry_signal":    bool(supply["entry_signal"]),
        },
        "bonds": [
            {
                "secid":                r["secid"],
                "shortname":            r["shortname"],
                "matdate":              r["matdate"].isoformat(),
                "duration":             float(r["duration"]),
                "price_pct":            float(r["price_pct"]),
                "coupon_pct":           float(r["coupon_pct"]),
                "ytm":                  float(r["ytm"]),
                "pnl_13_theoretical":   float(r["scenarios"]["КС → 13.0%"]["theoretical_pct"]),
                "pnl_13_adjusted":      float(r["scenarios"]["КС → 13.0%"]["adjusted_pct"]),
                "pnl_11_adjusted":      float(r["scenarios"]["КС → 11.0%"]["adjusted_pct"]),
                "pnl_flat":             float(r["scenarios"]["Flat (hold)"]["adjusted_pct"]),
            }
            for r in results
        ],
    }

    out_path = DATA_DIR / "bond_screener.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"✓ Сохранено: {out_path}")