import json
import sys
import os
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

from parsers.gcurve  import (get_last_gcurve, get_key_rate,
                              get_history, SEGMENT_SHORT,
                              SEGMENT_MEDIUM, SEGMENT_LONG)
from parsers.minfin  import (get_latest_file_url, download_xlsx,
                              parse_auctions)

HYPOTHESES_FILE = Path("data/hypotheses.json")
OUTPUT_FILE     = Path("data/digest_latest.txt")
W = 63

MONTHS_RU = {1:"января",2:"февраля",3:"марта",4:"апреля",
             5:"мая",6:"июня",7:"июля",8:"августа",
             9:"сентября",10:"октября",11:"ноября",12:"декабря"}


# ─────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────

def conf_bar(pct, width=10):
    """Прогресс-бар уверенности."""
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def prob_bar(pct, width=10):
    """Прогресс-бар вероятности."""
    filled = min(width, pct * width // 100)
    return "█" * filled + "░" * (width - filled)

def div(char="─"):
    return char * W

def _arrow(value, threshold=0.03):
    if value < -threshold: return "↓"
    if value >  threshold: return "↑"
    return "→"

def _label(arrow):
    return {"↑":"усилилось","↓":"ослабло","→":"без изм."}[arrow]


# ─────────────────────────────────────────────
# СИГНАЛЫ
# ─────────────────────────────────────────────

def get_curve_signal(key_rate):
    df, date_str = get_last_gcurve()
    if df is None:
        return None

    min_yield     = df["доходность_пct"].min()
    min_срок      = df.loc[df["доходность_пct"].idxmin(), "срок_лет"]
    ожид_снижение = round(key_rate - min_yield, 2)
    y2  = df.loc[df["срок_лет"] == 2.0,  "доходность_пct"].values[0]
    y10 = df.loc[df["срок_лет"] == 10.0, "доходность_пct"].values[0]

    delta_short = None
    hist_path   = Path("data/gcurve_history.csv")
    if hist_path.exists():
        history = pd.read_csv(hist_path)
        if "maturity_years" in history.columns:
            history = history.rename(columns={"maturity_years":"срок_лет",
                                              "yield_pct":"доходность_пct"})
        dates = sorted(history["date"].unique(),
                       key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
                       reverse=True)
        if len(dates) >= 6:
            latest = history[history["date"]==dates[0]].set_index("срок_лет")
            prev   = history[history["date"]==dates[4]].set_index("срок_лет")
            now_s  = latest.loc[latest.index.isin(SEGMENT_SHORT),"доходность_пct"].mean()
            prev_s = prev.loc[prev.index.isin(SEGMENT_SHORT),"доходность_пct"].mean()
            delta_short = round(now_s - prev_s, 3)

    return {
        "date": date_str, "df": df,
        "min_yield": min_yield, "min_срок": min_срок,
        "ожид_снижение": ожид_снижение,
        "наклон_2_10": round(y10 - y2, 2),
        "y1": df.loc[df["срок_лет"]==1.0,"доходность_пct"].values[0],
        "delta_short": delta_short,
    }


def get_auction_signal():
    url      = get_latest_file_url()
    file_obj = download_xlsx(url)
    df       = parse_auctions(file_obj)

    cutoff = df["дата"].max() - pd.Timedelta(weeks=4)
    recent = df[df["дата"] >= cutoff].copy()
    if recent.empty:
        return None

    last     = recent.iloc[0]
    avg_btc  = recent["bid_to_cover"].mean()
    long_df  = recent[recent["лет_до_погашения"] > 7]
    long_btc = long_df["bid_to_cover"].mean() if not long_df.empty else 0
    yield_trend = (recent["доходность_пct"].iloc[0]
                 - recent["доходность_пct"].iloc[-1])

    return {
        "avg_btc":   round(avg_btc, 2),
        "long_btc":  round(long_btc, 2),
        "last_btc":  round(last["bid_to_cover"], 2),
        "last_date": last["дата"].strftime("%d.%m.%Y"),
        "last_код":  last["код_выпуска"],
        "last_yield":last["доходность_пct"],
        "last_спрос":round(last["спрос_млн"]),
        "yield_trend":round(yield_trend, 2),
        "ratio_to_norm": round(avg_btc / 1.5, 1),
    }


def get_smart_money_signal():
    path = Path("data/form101_latest.csv")
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    buyers = (df[df["change_mln"] > 0].sort_values("change_mln", ascending=False).head(3)
              if "change_mln" in df.columns else df.head(3))
    return [{"name": row.get("bank_name", f"REGN {int(row['bank_id'])}"),
             "change_млрд": round(row.get("change_mln",
                                          row.get("debt_securities_mln", 0)) / 1000, 1)}
            for _, row in buyers.iterrows()]


def get_cbr_probabilities():
    path = Path("data/cbr_probabilities.json")
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("meetings", [])


def get_inflation_signal(key_rate=None):
    """Сигнал по инфляции (реальная ставка = КС − инфляция, + инФОМ)."""
    try:
        from parsers.inflation import get_inflation_data, build_inflation_signal
        rows = get_inflation_data()
        if not rows:
            return None
        # инФОМ (наблюдаемая/ожидаемая) — опционально
        try:
            from parsers.inflation_expectations import get_inflation_expectations
            expectations = get_inflation_expectations()
        except Exception:
            expectations = None
        return build_inflation_signal(rows, key_rate=key_rate,
                                      expectations=expectations)
    except Exception:
        return None


# ─────────────────────────────────────────────
# ГИПОТЕЗЫ
# ─────────────────────────────────────────────

def load_hypotheses():
    if not HYPOTHESES_FILE.exists():
        return []
    with open(HYPOTHESES_FILE, encoding="utf-8") as f:
        return json.load(f)["hypotheses"]

def save_hypotheses(hypotheses):
    with open(HYPOTHESES_FILE, "w", encoding="utf-8") as f:
        json.dump({"hypotheses": hypotheses}, f, ensure_ascii=False, indent=2)

def calc_confidence(h, curve, auctions):
    score = 50
    try:
        decisions = pd.read_csv("data/cbr_decisions.csv")
        decisions["decision_date"] = pd.to_datetime(decisions["decision_date"])
        changes = (decisions[decisions["rate_change_bps"] != 0]
                   .sort_values("decision_date").tail(6))
        cuts = (changes["rate_change_bps"] < 0).sum()
        score += 15 if cuts >= 4 else (8 if cuts >= 2 else 0)
    except Exception:
        pass

    score += 12 if curve["ожид_снижение"] > 1.5 else (6 if curve["ожид_снижение"] > 0.5 else 0)

    sm_path = Path("data/form101_latest.csv")
    if sm_path.exists():
        sm_df = pd.read_csv(sm_path)
        if "change_mln" in sm_df.columns and (sm_df["change_mln"] > 0).sum() >= 3:
            score += 8

    score -= 10 if auctions["avg_btc"] < 0.7 else (5 if auctions["avg_btc"] < 1.0 else 0)
    score -= 8  if auctions["yield_trend"] > 0.3 else (4 if auctions["yield_trend"] > 0.1 else 0)

    return max(20, min(80, score))

def auto_update_hypotheses(hypotheses, curve, auctions):
    today = datetime.today().strftime("%Y-%m-%d")
    for h in hypotheses:
        if h["status"] != "open":
            continue
        new_conf = calc_confidence(h, curve, auctions)
        if new_conf != h["confidence"]:
            h["confidence"] = new_conf
            h["confidence_history"].append(
                {"date": today, "confidence": new_conf, "note": "авторасчёт"})
    return hypotheses


# ─────────────────────────────────────────────
# СБОРКА — ПИРАМИДА МИНТО
#
# УРОВЕНЬ 1  Главный вывод + действие          10 сек
# УРОВЕНЬ 2  Ставки на заседания ЦБ            30 сек  ← уникально
# УРОВЕНЬ 3  Гипотеза с P&L                   60 сек  ← зачем платят
# УРОВЕНЬ 4  Три сигнала (компактно)           90 сек
# УРОВЕНЬ 5  Расхождение + интерпретация        2 мин
# УРОВЕНЬ 6  Детали для профи                  по желанию
# ─────────────────────────────────────────────

def compose_digest(curve, auctions, smart_money, hypotheses):
    today_str = datetime.today().strftime("%d.%m.%Y")
    open_hyps = [h for h in hypotheses if h["status"] == "open"]
    closed    = [h for h in hypotheses if h["status"] != "open"]
    issue     = len(open_hyps) + len(closed) + 1
    L         = []

    bullish    = curve["ожид_снижение"] > 0.5
    auc_bull   = auctions["avg_btc"] >= 1.0
    divergence = bullish and not auc_bull

    # ── ШАПКА ─────────────────────────────────
    L += ["", "═"*W, f"  ЦБ-РАДАР  ·  ДАЙДЖЕСТ #{issue}  ·  {today_str}", "═"*W]

    # ── 1. ГЛАВНЫЙ ВЫВОД ──────────────────────
    if divergence:
        вывод  = "Рынок ждёт снижения КС — на аукционах покупают мало"
        next_w = (datetime.today() + timedelta(days=(2-datetime.today().weekday())%7))
        action = f"Ждать сигнала входа · следующий аукцион {next_w.strftime('%d.%m')}"
    elif bullish and auc_bull:
        вывод  = "Рынок ждёт снижения КС и активно покупает ОФЗ"
        action = "Позиция в длинных ОФЗ оправдана"
    else:
        вывод  = "Сигналы смешанные — рынок в ожидании"
        action = "Держать текущую позицию"

    L += ["", "█"*W, f"  {вывод}", "█"*W, f"  → {action}"]

    # ── 2. СТАВКИ НА ЗАСЕДАНИЯ ────────────────
    cbr = get_cbr_probabilities()
    if cbr:
        L += ["", f"  СТАВКИ РЫНКА НА ЗАСЕДАНИЯ ЦБ", f"  {div()}"]
        for m in cbr[:4]:
            parts   = m["date"][:10].split("-")
            dt_s    = f"{int(parts[2])} {MONTHS_RU[int(parts[1])]}"
            p       = m["prob_cut"]
            pbar_s  = prob_bar(p)
            impl    = m["implied_ks"]
            cut_bps = m.get("meeting_cut_bps", 0)
            cut_s   = f"−{abs(int(cut_bps))}бп" if cut_bps > 0 else "hold"
            L.append(f"  {dt_s:<10}  {pbar_s}  {p:>3}%  →  ~{impl:.1f}%  ({cut_s})")
        L += [f"  {div()}", "  Implied forward rates из G-кривой · аналог CME FedWatch"]

    # ── 3. ГИПОТЕЗА ───────────────────────────
    if open_hyps:
        L += ["", f"  {'═'*(W-2)}", f"  ТОРГОВАЯ ГИПОТЕЗА", f"  {'═'*(W-2)}"]
        for h in open_hyps:
            hist    = h["confidence_history"]
            conf    = h["confidence"]
            delta_c = (hist[-1]["confidence"] - hist[-2]["confidence"]
                       if len(hist) >= 2 else 0)
            d_str   = f"{delta_c:+d}% за нед" if delta_c else "новая"

            L += [
                "",
                f"  {h['title']}",
                f"  Уверенность: {conf}%  {conf_bar(conf)}  {d_str}",
                f"  {div()}",
                f"  {'Инструмент':<13} {h.get('instrument','—')}",
                f"  {'Войти при':<13} {h.get('action','—')}",
                f"  {'Ожидаемо':<13} {h.get('expected_pl','—')}",
            ]

            if "signals" in h:
                за     = [(k, v.get("note","")) for k,v in h["signals"].items()
                           if v["направление"]=="за"]
                против = [(k, v.get("note","")) for k,v in h["signals"].items()
                           if v["направление"]=="против"]
                if за:
                    L.append(f"  {'За':<13} "
                             f"{', '.join(f'{k} ({n})' for k,n in за)}")
                if против:
                    L.append(f"  {'Против':<13} "
                             f"{', '.join(f'{k} ({n})' for k,n in против)}")

            L += ["",
                  f"  ⏳ Сигнал входа: {h.get('что_повысит','—')}",
                  f"  🛑 Стоп: {h.get('invalidation','—')} → {h.get('invalidation_date','—')}"]

        if closed:
            confirmed = sum(1 for h in closed if h["status"]=="confirmed")
            L.append(f"\n  Track record: {confirmed}/{len(closed)} "
                     f"({round(confirmed/len(closed)*100)}%) подтверждено")

    # ── 4. ТРИ СИГНАЛА — КОМПАКТНО ────────────
    L += ["", f"  ТРИ СИГНАЛА НЕДЕЛИ", f"  {div()}"]

    # Кривая
    c_arr = _arrow(-(curve["delta_short"] or 0))
    L += [
        f"\n  ① Кривая ОФЗ{'':>37}{c_arr} {_label(c_arr)}",
        f"  {div('─')}",
        f"  1Y = {curve['y1']:.2f}% при КС {curve['y1']+curve['ожид_снижение']:.1f}%"
        f"  →  рынок ждёт КС ~{curve['min_yield']:.1f}%",
    ]
    if curve["delta_short"] is not None:
        dir_s = "снижается ↓ (ожидания укрепляются)" if curve["delta_short"] < 0 else "растёт ↑"
        L.append(f"  Короткий участок за неделю: {curve['delta_short']:+.2f}%  ({dir_s})")

    # Аукционы
    a_arr = _arrow(auctions["avg_btc"] - 1.0, threshold=0.15)
    # Считаем отношение к норме от сырого avg_btc (не от округлённого), защищаемся от деления на ноль
    avg_btc = auctions["avg_btc"]
    if avg_btc <= 0:
        norm_d = "спрос практически отсутствует"
    elif avg_btc < 1.5:
        norm_d = f"в {1.5/avg_btc:.1f}× меньше нормы"
    else:
        norm_d = "норма"
    L += [
        f"\n  ② Аукционы Минфина{'':>30}{a_arr} {_label(a_arr)}",
        f"  {div('─')}",
        f"  BTC {auctions['avg_btc']:.2f}×  ({norm_d}, норма ≥1.5×)"
        f"  ·  доходность {auctions['yield_trend']:+.2f}% за 4 нед",
        f"  Последний {auctions['last_date']}: спрос {auctions['last_спрос']:,} млн  ·"
        f"  {auctions['last_код']}  {auctions['last_yield']:.2f}%",
    ]

    # Умные деньги
    sm_arr = "↑" if smart_money else "→"
    L += [
        f"\n  ③ Умные деньги (Форма 101){'':>23}{sm_arr} {_label(sm_arr)}",
        f"  {div('─')}",
    ]
    if smart_money:
        for b in smart_money:
            L.append(f"  · {b['name'][:34]:<34}  +₽{b['change_млрд']:.1f} млрд")
        L.append("  Счета 501/502/504 — все долговые ЦБ (не только ОФЗ)")
    else:
        L.append("  Нет данных — запусти parsers/form101.py")

    # ── 4½. ИНФЛЯЦИЯ ──────────────────────────
    # КС восстанавливаем из кривой (y1 + ожидаемое снижение)
    ks_for_infl = None
    try:
        ks_for_infl = round(curve["y1"] + curve["ожид_снижение"], 1)
    except Exception:
        ks_for_infl = None
    infl = get_inflation_signal(ks_for_infl)
    if infl:
        i_arr = infl.get("arrow", "→")
        i_lab = infl.get("label", "Нет данных")
        L += [f"\n  ④ Инфляция (Росстат){'':>28}{i_arr} {i_lab}",
              f"  {div('─')}"]
        yoy    = infl.get("infl_yoy")
        target = infl.get("target", 4.0)
        period = infl.get("date", "")
        if yoy is not None:
            gap = infl.get("gap_vs_target")
            gap_s = f"  (отклонение {gap:+.2f} п.п.)" if gap is not None else ""
            L.append(f"  {yoy:.1f}% г/г при цели {target:.0f}%{gap_s}  ·  {period}")
        real = infl.get("real_rate")
        if real is not None:
            tone = "ДКП жёсткая" if real >= 5 else ("ДКП нейтральна" if real >= 2 else "ДКП мягкая")
            L.append(f"  Реальная ставка {real:+.2f} п.п. (КС − инфляция)  —  {tone}")
        t3 = infl.get("trend_3m")
        if t3 is not None:
            t_dir = "замедляется" if t3 < 0 else ("ускоряется" if t3 > 0 else "стабильна")
            L.append(f"  Тренд 3 мес: {t3:+.2f} п.п. — инфляция {t_dir}")
        # инФОМ: восприятие населения (может отсутствовать)
        observed = infl.get("observed")
        if observed is not None:
            expected = infl.get("expected")
            exp_s = f" · ожидаемая {expected:.1f}%" if expected is not None else ""
            L.append(f"  Наблюдаемая (инФОМ) {observed:.1f}%{exp_s}")
            # разрыв доверия: люди ощущают инфляцию намного выше официальной
            if yoy is not None and yoy > 0:
                ratio = observed / yoy
                L.append(f"  → Люди ощущают инфляцию в {ratio:.1f}× выше Росстата (разрыв доверия)")

    # ── 5. РАСХОЖДЕНИЕ ────────────────────────
    if divergence:
        L += ["", f"  ⚡ РАСХОЖДЕНИЕ СИГНАЛОВ", f"  {div('─')}",
              "  Кривая: рынок ждёт снижения КС",
              "  Аукционы: покупать по текущим ценам не спешат"]
        if auctions["yield_trend"] > 0.1:
            L.append(f"  + Доходность растёт ({auctions['yield_trend']:+.2f}%)"
                     " — рынок просит премию за вход")
        L.append("  → Ждут лучшей точки входа ИЛИ сомневаются в сроках снижения")

    # ── 6. ДЕТАЛИ ─────────────────────────────
    L += ["", f"  {div()}", "  ДЕТАЛИ", f"  {div()}",
          f"\n  G-кривая {curve['date']}:",
          f"  {'Срок':>6}  {'Участок':<9}  {'Доходность':>12}"]
    for _, row in curve["df"].iterrows():
        s = row["срок_лет"]; y = row["доходность_пct"]
        seg = ("короткий" if s in SEGMENT_SHORT
               else "средний " if s in SEGMENT_MEDIUM else "длинный ")
        L.append(f"  {s:>6.2f}л  {seg}   {y:>10.2f}%")

    L += [f"\n  Аукционы: BTC {auctions['avg_btc']:.2f}×"
          f"  ·  длинные {auctions['long_btc']:.2f}×",
          "", "═"*W, ""]

    return "\n".join(L)


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "close":
        hypotheses = load_hypotheses()
        open_h     = [h for h in hypotheses if h["status"] == "open"]
        if not open_h:
            print("Нет открытых гипотез"); sys.exit(0)
        for h in open_h:
            print(f"#{h['id']}: {h['title']} | {h['confidence']}%")
        try:
            hid    = int(input("\nID для закрытия: "))
            status = input("Результат (confirmed/invalidated): ").strip()
            result = input("Вывод: ").strip()
            for h in hypotheses:
                if h["id"] == hid:
                    h["status"]      = status
                    h["result"]      = result
                    h["date_closed"] = datetime.today().strftime("%Y-%m-%d")
                    print(f"✓ Гипотеза #{hid} закрыта: {status}")
        except (ValueError, EOFError):
            pass
        save_hypotheses(hypotheses)
        sys.exit(0)

    print("Загружаем данные...\n")

    key_rate = get_key_rate() or 14.5

    print("G-кривая...")
    curve = get_curve_signal(key_rate)
    if curve is None:
        print("Ошибка: нет данных кривой"); sys.exit(1)

    print("Аукционы...")
    auctions = get_auction_signal()
    if auctions is None:
        print("Ошибка: нет данных аукционов"); sys.exit(1)

    print("Умные деньги...")
    smart_money = get_smart_money_signal()
    if smart_money:
        print(f"  ✓ {len(smart_money)} банков")
    else:
        print("  ⚠ Запусти parsers/form101.py")

    print("Вероятности ЦБ...")
    if not Path("data/cbr_probabilities.json").exists():
        print("  ⚠ Запусти scripts/cbr_probabilities.py")

    hypotheses = load_hypotheses()
    hypotheses = auto_update_hypotheses(hypotheses, curve, auctions)
    save_hypotheses(hypotheses)

    digest = compose_digest(curve, auctions, smart_money, hypotheses)
    print(digest)
    OUTPUT_FILE.write_text(digest, encoding="utf-8")
    print(f"✓ Сохранено: {OUTPUT_FILE}")