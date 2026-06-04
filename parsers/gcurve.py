import sys
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timedelta


# Стандартные сроки G-кривой ЦБ РФ (лет)
MATURITIES = [0.25, 0.50, 0.75, 1.00, 2.00, 3.00,
              5.00, 7.00, 10.00, 15.00, 20.00, 30.00]

SEGMENT_SHORT  = [0.25, 0.50, 0.75, 1.00, 2.00]
SEGMENT_MEDIUM = [3.00, 5.00, 7.00]
SEGMENT_LONG   = [10.00, 15.00, 20.00, 30.00]


# ─────────────────────────────────────────────
# 1. ПОЛУЧЕНИЕ ДАННЫХ
# ─────────────────────────────────────────────

def fetch_post(date_str):
    url = "https://www.cbr.ru/hd_base/zcyc_params/zcyc/"
    response = requests.post(
        url,
        data={"UniDbQuery.Posted": "True", "UniDbQuery.To": date_str},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": url,
        }
    )
    response.raise_for_status()
    return response.text


def parse_gcurve(html, date_str):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        if not script.string:
            continue
        if '"data"' not in script.string or "categories" not in script.string:
            continue
        match = re.search(r'"data":\[([^\]]+)\]', script.string)
        if not match:
            continue
        raw = match.group(1).strip()
        values = []
        for item in raw.split(","):
            item = item.strip()
            values.append(None if item == "null" else float(item))
        if len(values) != len(MATURITIES):
            return None
        if all(v is None for v in values):
            return None
        df = pd.DataFrame({
            "date":           date_str,
            "срок_лет":       MATURITIES,
            "доходность_пct": values,
        }).dropna(subset=["доходность_пct"])
        return df
    return None


def get_gcurve(date_str=None):
    if date_str is None:
        date_str = (datetime.today() - timedelta(days=1)).strftime("%d.%m.%Y")
    return parse_gcurve(fetch_post(date_str), date_str)


def get_last_gcurve():
    today = datetime.today()
    for days_ago in range(1, 10):
        date = today - timedelta(days=days_ago)
        if date.weekday() >= 5:
            continue
        date_str = date.strftime("%d.%m.%Y")
        df = get_gcurve(date_str)
        if df is not None:
            return df, date_str
    return None, None


def get_key_rate():
    url = "https://www.cbr.ru/hd_base/KeyRate/"
    try:
        response = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10
        )
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    
    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table", class_="data")
    if not table:
        return None
    rows = table.find_all("tr")
    if len(rows) < 2:
        return None
    cells = rows[1].find_all("td")
    if len(cells) < 2:
        return None
    date_str = cells[0].get_text(strip=True)
    rate_str = cells[1].get_text(strip=True).replace(",", ".")
    try:
        rate = float(rate_str)
        print(f"✓ Ключевая ставка ЦБ: {rate}% (с {date_str})")
        return rate
    except ValueError:
        return None


# ─────────────────────────────────────────────
# 2. ИСТОРИЯ
# ─────────────────────────────────────────────

def get_history(days=30):
    today   = datetime.today()
    results = []
    print(f"Собираем историю за {days} рабочих дней...\n")
    for days_ago in range(1, days * 2):
        if len(results) >= days:
            break
        date = today - timedelta(days=days_ago)
        if date.weekday() >= 5:
            continue
        date_str = date.strftime("%d.%m.%Y")
        df = get_gcurve(date_str)
        if df is not None:
            results.append(df)
            print(f"  ✓ {date_str}")
        else:
            print(f"  — {date_str} нет данных")
    if not results:
        print("Данных не найдено")
        return None
    history = pd.concat(results, ignore_index=True)
    history.to_csv("data/gcurve_history.csv", index=False)
    print(f"\n✓ Сохранено {len(results)} дней → data/gcurve_history.csv")
    return history


# ─────────────────────────────────────────────
# 3. АНАЛИЗ — ПИРАМИДА МИНТО
# ─────────────────────────────────────────────
#
# Структура каждого вывода:
#
#   ┌─────────────────────────────────────┐
#   │  УРОВЕНЬ 1: ВЫВОД + ЧТО ДЕЛАТЬ     │  ← читай только это
#   ├─────────────────────────────────────┤
#   │  УРОВЕНЬ 2: ПОЧЕМУ (3 причины)     │  ← простыми словами
#   ├─────────────────────────────────────┤
#   │  УРОВЕНЬ 3: ДЕТАЛИ ДЛЯ ПРОФИ      │  ← термины и цифры
#   └─────────────────────────────────────┘

def _segment_avg(df, maturities):
    mask = df["срок_лет"].isin(maturities)
    return df.loc[mask, "доходность_пct"].mean()


def _recommendation(ожид_снижение, min_yield, min_срок):
    """Простая рекомендация на основе глубины ожидаемого снижения."""
    if ожид_снижение > 2.0:
        return (
            f"Покупай длинные ОФЗ (срок 10–15 лет)\n"
            f"  Рынок ждёт снижения ставки на {ожид_снижение:.1f}% — "
            f"длинные бумаги вырастут в цене сильнее всего"
        )
    elif ожид_снижение > 0.5:
        return (
            f"Можно держать позицию в длинных ОФЗ, но без агрессии\n"
            f"  Рынок ждёт небольшого снижения на {ожид_снижение:.1f}%"
        )
    else:
        return (
            f"Без изменений — рынок не ждёт движения ставки\n"
            f"  Короткие ОФЗ или депозиты предпочтительнее"
        )


def analyze_curve_vs_key_rate(df, key_rate):
    date_str      = df["date"].iloc[0]
    min_yield     = df["доходность_пct"].min()
    min_срок      = df.loc[df["доходность_пct"].idxmin(), "срок_лет"]
    ожид_снижение = key_rate - min_yield

    y2          = df.loc[df["срок_лет"] == 2.0,  "доходность_пct"].values[0]
    y10         = df.loc[df["срок_лет"] == 10.0, "доходность_пct"].values[0]
    наклон_2_10 = round(y10 - y2, 2)

    avg_short  = _segment_avg(df, SEGMENT_SHORT)
    avg_medium = _segment_avg(df, SEGMENT_MEDIUM)
    avg_long   = _segment_avg(df, SEGMENT_LONG)

    # ── УРОВЕНЬ 1: ВЫВОД ──────────────────────────────────────────
    print(f"\n{'█'*55}")
    if ожид_снижение > 0.5:
        вывод = (f"Рынок ждёт снижения ставки ЦБ с {key_rate}%"
                 f" до ~{min_yield:.1f}%")
    else:
        вывод = "Рынок не ждёт значимых изменений ставки ЦБ"

    print(f"  {вывод}")
    print(f"  Горизонт: ~{min_срок:.0f} {'год' if min_срок <= 1 else 'года' if min_срок <= 4 else 'лет'}")
    print(f"{'█'*55}")

    print(f"\n  ЧТО ДЕЛАТЬ:")
    for line in _recommendation(ожид_снижение, min_yield, min_срок).split("\n"):
        print(f"  {line}")

    # ── УРОВЕНЬ 2: ПОЧЕМУ ─────────────────────────────────────────
    print(f"\n  ПОЧЕМУ — 3 сигнала:")

    # Сигнал 1: разрыв между КС и кривой
    разрыв_1Y = key_rate - df.loc[df["срок_лет"] == 1.0, "доходность_пct"].values[0]
    print(f"\n  ① Краткосрочные ОФЗ дают {df.loc[df['срок_лет']==1.0,'доходность_пct'].values[0]:.2f}%")
    print(f"    при ключевой ставке {key_rate}%")
    print(f"    → Рынок уже «не верит» в текущую ставку на горизонте года")

    # Сигнал 2: форма кривой
    if наклон_2_10 < 0:
        форма_объяснение = "кривая перевёрнута — очень сильный сигнал снижения"
    elif наклон_2_10 < 0.5:
        форма_объяснение = "кривая плоская — рынок в ожидании"
    else:
        форма_объяснение = "кривая нормальная — рынок спокоен"
    print(f"\n  ② Длинные ОФЗ (10 лет) дают {y10:.2f}%,")
    print(f"    короткие (2 года) — {y2:.2f}%")
    print(f"    → Форма кривой: {форма_объяснение}")

    # Сигнал 3: где минимум
    print(f"\n  ③ Минимальная доходность на рынке: {min_yield:.2f}%")
    print(f"    на бумагах со сроком {min_срок} лет")
    print(f"    → Именно туда рынок «смотрит» как на будущую ставку")

    # ── УРОВЕНЬ 3: ДЕТАЛИ ─────────────────────────────────────────
    print(f"\n  {'─'*51}")
    print(f"  ДЕТАЛИ — для профессионалов ({date_str})")
    print(f"  {'─'*51}")
    print(f"  {'Срок':>6}  {'Участок':<9}  {'Доходность':>11}  {'vs КС':>8}")
    print(f"  {'─'*51}")

    for _, row in df.iterrows():
        срок   = row["срок_лет"]
        yield_ = row["доходность_пct"]
        delta  = yield_ - key_rate
        arrow  = "↓" if delta < 0 else "↑"
        seg    = ("короткий" if срок in SEGMENT_SHORT
                  else "средний " if срок in SEGMENT_MEDIUM
                  else "длинный ")
        print(f"  {срок:>6.2f}  {seg}   {yield_:>9.2f}%  "
              f"  {arrow}{delta:>+.2f}%")

    print(f"  {'─'*51}")
    print(f"  КС: {key_rate:.1f}%  |  "
          f"Наклон 2–10: {наклон_2_10:+.2f}%  |  "
          f"Мин. доходность: {min_yield:.2f}% ({min_срок}л)")
    print(f"  Средние по участкам — "
          f"короткий: {avg_short:.2f}%  "
          f"средний: {avg_medium:.2f}%  "
          f"длинный: {avg_long:.2f}%")


def analyze_trend(history):
    dates = sorted(
        history["date"].unique(),
        key=lambda d: datetime.strptime(d, "%d.%m.%Y"),
        reverse=True
    )
    if len(dates) < 6:
        print("Мало данных (нужно минимум 6 рабочих дней)")
        return

    latest_date = dates[0]
    week_ago    = dates[4]
    latest = history[history["date"] == latest_date].set_index("срок_лет")
    week   = history[history["date"] == week_ago  ].set_index("срок_лет")

    # Изменения по участкам
    def avg_delta(segment):
        now  = latest.loc[latest.index.isin(segment), "доходность_пct"].mean()
        then = week.loc[  week.index.isin(segment),   "доходность_пct"].mean()
        return round(now - then, 3)

    d_short  = avg_delta(SEGMENT_SHORT)
    d_medium = avg_delta(SEGMENT_MEDIUM)
    d_long   = avg_delta(SEGMENT_LONG)

    наклон_now  = (latest.loc[10.0, "доходность_пct"]
                 - latest.loc[2.0,  "доходность_пct"])
    наклон_then = (week.loc[10.0,   "доходность_пct"]
                 - week.loc[2.0,    "доходность_пct"])

    # ── УРОВЕНЬ 1: ВЫВОД ──────────────────────────────────────────
    print(f"\n{'█'*55}")

    if d_short < -0.05 and abs(d_long) < 0.05:
        вывод_тренд = ("Короткие ОФЗ падают в доходности — "
                       "рынок всё активнее ставит на снижение ставки")
        рекомендация = "Сигнал усиливается. Длинные ОФЗ становятся привлекательнее"
    elif d_short < -0.05 and d_long > 0.05:
        вывод_тренд = ("Короткие ОФЗ дешевеют в доходности, "
                       "длинные дорожают — рынок разделился")
        рекомендация = ("Рынок верит в снижение ставки, "
                        "но опасается инфляции в долгосроке")
    elif d_short < -0.05 and d_long < -0.05:
        вывод_тренд = "Доходности падают по всей кривой — бычий сигнал"
        рекомендация = "Рынок массово покупает ОФЗ. Позиция в длинных бумагах оправдана"
    elif d_short > 0.05 and d_long > 0.05:
        вывод_тренд = "Доходности растут по всей кривой — рынок пересматривает ожидания"
        рекомендация = "Осторожно с длинными ОФЗ. Возможна переоценка ожиданий по КС"
    else:
        вывод_тренд = "Кривая стабильна — значимых движений нет"
        рекомендация = "Держи текущую позицию, новых сигналов нет"

    print(f"  {вывод_тренд}")
    print(f"{'█'*55}")
    print(f"\n  ЧТО ДЕЛАТЬ: {рекомендация}")

    # ── УРОВЕНЬ 2: ПОЧЕМУ ─────────────────────────────────────────
    print(f"\n  ПОЧЕМУ — движение за неделю ({week_ago} → {latest_date}):")

    for name, delta, segment in [
        ("Короткие ОФЗ (до 2 лет) ", d_short,  SEGMENT_SHORT),
        ("Средние ОФЗ  (3–7 лет)  ", d_medium, SEGMENT_MEDIUM),
        ("Длинные ОФЗ  (от 10 лет)", d_long,   SEGMENT_LONG),
    ]:
        arrow = "↓" if delta < -0.03 else ("↑" if delta > 0.03 else "→")
        знак  = "падает" if delta < -0.03 else ("растёт" if delta > 0.03
                                                 else "без изменений")
        avg_now = latest.loc[latest.index.isin(segment), "доходность_пct"].mean()
        print(f"\n  {name}: доходность {знак} {arrow}{delta:+.2f}%")
        print(f"    сейчас в среднем {avg_now:.2f}%")

    # ── УРОВЕНЬ 3: ДЕТАЛИ ─────────────────────────────────────────
    print(f"\n  {'─'*55}")
    print(f"  ДЕТАЛИ — для профессионалов")
    print(f"  {'─'*55}")
    print(f"  {'Срок':>6}  {'Участок':<9}  "
          f"{'Неделю назад':>13}  {'Сейчас':>9}  {'Δ':>8}")
    print(f"  {'─'*55}")

    for mat in MATURITIES:
        if mat not in latest.index or mat not in week.index:
            continue
        y_now  = latest.loc[mat, "доходность_пct"]
        y_then = week.loc[mat,   "доходность_пct"]
        delta  = y_now - y_then
        arrow  = "↓" if delta < -0.05 else ("↑" if delta > 0.05 else "→")
        seg    = ("короткий" if mat in SEGMENT_SHORT
                  else "средний " if mat in SEGMENT_MEDIUM
                  else "длинный ")
        print(f"  {mat:>6.2f}  {seg}   "
              f"{y_then:>12.2f}%  {y_now:>8.2f}%  "
              f"{arrow}{delta:>+.2f}%")

    print(f"  {'─'*55}")
    print(f"  Наклон кривой (2–10 лет): "
          f"{наклон_then:+.2f}% → {наклон_now:+.2f}% "
          f"({наклон_now - наклон_then:+.2f}%)")


# ─────────────────────────────────────────────
# 4. ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1] == "history":
        history = get_history(days=30)
        if history is not None:
            analyze_trend(history)

    else:
        key_rate = get_key_rate()
        if key_rate is None:
            print("⚠ КС не спарсилась, используем 14.5%")
            key_rate = 14.5

        df, date_str = get_last_gcurve()
        if df is not None:
            analyze_curve_vs_key_rate(df, key_rate)
            filename = f"data/gcurve_{date_str.replace('.', '')}.csv"
            df.to_csv(filename, index=False)
            print(f"\n✓ Сохранено: {filename}")
        else:
            print("Данные не найдены")