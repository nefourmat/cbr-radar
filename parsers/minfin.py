import requests
import pandas as pd
from bs4 import BeautifulSoup
from io import BytesIO
from datetime import datetime

BASE_URL = "https://minfin.gov.ru"

# Колонки которые нам нужны + понятные имена
COLUMNS_MAP = {
    # Новый формат (2024+)
    "Дата":                                       "дата",
    # Старый формат (2021–2023)
    "Дата аукциона":                              "дата",
    # Общие колонки
    "Код  выпуска":                               "код_выпуска",
    "Код выпуска":                                "код_выпуска",
    "Тип бумаги**":                               "тип",
    "Тип бумаги*":                                "тип",
    "Дней до погашения":                          "дней_до_погашения",
    "Объем предложения":                          "предложение_млн",
    "Доходность по цене отсечения***":            "доходность_пct",
    "Доходность по цене отсечения**":             "доходность_пct",
    "Совокупный объем спроса по номиналу":        "спрос_млн",
    "Объем размещения по номиналу":               "размещено_млн",
    "Коэффициент удовлетворения спроса на аукционе": "коэф_удовл",
}


# ─────────────────────────────────────────────
# 1. ЗАГРУЗКА
# ─────────────────────────────────────────────

def get_latest_file_url() -> str | None:
    """
    Конструируем URL файла аукционов по предсказуемому паттерну.
    Не скрапим страницу — она блокирует иностранные IP (Railway).
    
    Паттерн: /library/YYYY/MM/main/INTERNET_Auction_Results_rus_YYYY_YYYYMMDD.xlsx
    где YYYYMMDD — дата последнего обновления (обычно конец текущего месяца
    или дата последнего аукциона).
    """
    from datetime import date, timedelta
    import requests

    today = date.today()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # Пробуем несколько кандидатов: последние 90 дней с шагом 7 дней
    candidates = []
    for days_ago in range(0, 120):
        d = today - timedelta(days=days_ago)
        url = (
            f"https://minfin.gov.ru/common/upload/library/"
            f"{d.year}/{d.month:02d}/main/"
            f"INTERNET_Auction_Results_rus_{d.year}_{d.strftime('%Y%m%d')}.xlsx"
        )
        candidates.append(url)

    # Проверяем каждый кандидат HEAD-запросом
    for url in candidates:
        try:
            r = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url
        except Exception:
            continue

    return None


def download_xlsx(url):
    headers  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return BytesIO(response.content)


# ─────────────────────────────────────────────
# 2. ПАРСИНГ
# ─────────────────────────────────────────────

def parse_auctions(file_obj):
    # Берём первый лист — не зависим от имени
    try:
        xl     = pd.ExcelFile(file_obj)
        sheet  = xl.sheet_names[0]
        df     = pd.read_excel(xl, sheet_name=sheet, header=5)
    except Exception:
        return None

    df.columns = [str(c).strip() for c in df.columns]

    existing = {k: v for k, v in COLUMNS_MAP.items() if k in df.columns}
    df = df[list(existing.keys())].rename(columns=existing)

    # Конвертируем дату — убираем epoch (01.01.1970)
    df["дата"] = pd.to_datetime(df["дата"], errors="coerce")
    min_valid_date = pd.Timestamp("2020-01-01")
    df = df[df["дата"].notna() & (df["дата"] > min_valid_date)].copy()

    # Числа в числа
    for col in ["дней_до_погашения", "предложение_млн", "доходность_пct",
                "спрос_млн", "размещено_млн", "коэф_удовл"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Убираем строки доп. размещения — нет конкурентного спроса
    df = df[df["спрос_млн"].notna() & (df["спрос_млн"] > 0)].copy()

    # bid-to-cover
    df["bid_to_cover"] = (df["спрос_млн"] / df["предложение_млн"]).round(2)

    # Срок в годах
    df["лет_до_погашения"] = (df["дней_до_погашения"] / 365).round(1)

    df = df.sort_values("дата", ascending=False).reset_index(drop=True)
    return df


BTC_NORMAL = 1.5


def _supply_metrics(avg_btc: float) -> tuple[float, float]:
    sp = max(0.0, min(1.0, 1 - avg_btc / BTC_NORMAL))
    pt = 1.0 - sp * 0.5
    return round(sp, 2), round(pt, 2)


def build_auction_signal(df, last_n_weeks=4) -> dict:
    """Полный сигнал аукционов для API и кэша."""
    cutoff = df["дата"].max() - pd.Timedelta(weeks=last_n_weeks)
    recent = df[df["дата"] >= cutoff].copy()
    if recent.empty:
        raise ValueError("Нет аукционов за последние недели")

    last     = recent.iloc[0]
    avg_btc  = float(recent["bid_to_cover"].mean())
    long_df  = recent[recent["лет_до_погашения"] > 7]
    long_btc = float(long_df["bid_to_cover"].mean()) if not long_df.empty else avg_btc
    yield_trend = float(
        recent["доходность_пct"].iloc[0] - recent["доходность_пct"].iloc[-1]
    )
    sp, pt = _supply_metrics(avg_btc)
    entry  = avg_btc >= BTC_NORMAL

    if avg_btc >= 1.5:
        status, label, arrow = "bull", "Сильный спрос", "↑"
    elif avg_btc >= 1.0:
        status, label, arrow = "neu", "Умеренный спрос", "→"
    else:
        status, label, arrow = "neu", "Слабый спрос", "↓"

    return {
        "status":          status,
        "label":           label,
        "arrow":           arrow,
        "avg_btc":         round(avg_btc, 2),
        "long_btc":        round(long_btc, 2),
        "last_btc":        round(float(last["bid_to_cover"]), 2),
        "last_date":       last["дата"].strftime("%d.%m.%Y"),
        "last_code":       str(last["код_выпуска"]),
        "last_yield":      round(float(last["доходность_пct"]), 2),
        "last_demand_mln": int(last["спрос_млн"]),
        "yield_trend":     round(yield_trend, 2),
        "supply_pressure": sp,
        "pass_through":    pt,
        "entry_signal":    entry,
        "description": (
            f"BTC {avg_btc:.2f}× за {last_n_weeks} нед · "
            f"последний {last['код_выпуска']} {last['доходность_пct']:.2f}%"
        ),
    }


def enrich_auction_cache(cached: dict) -> dict:
    """Дополняет минимальный кэш до полного формата API."""
    if cached.get("status"):
        return cached

    avg_btc = cached.get("avg_btc", 0.5)
    sp, pt  = _supply_metrics(avg_btc)
    if avg_btc >= 1.5:
        status, label, arrow = "bull", "Сильный спрос", "↑"
    elif avg_btc >= 1.0:
        status, label, arrow = "neu", "Умеренный спрос", "→"
    else:
        status, label, arrow = "neu", "Слабый спрос", "↓"

    return {
        **cached,
        "status":          status,
        "label":           label,
        "arrow":           arrow,
        "long_btc":        cached.get("long_btc", avg_btc),
        "last_code":       cached.get("last_code", "—"),
        "last_demand_mln": cached.get("last_demand_mln", 0),
        "supply_pressure": cached.get("supply_pressure", sp),
        "pass_through":    cached.get("pass_through", pt),
        "entry_signal":    cached.get("entry_signal", avg_btc >= BTC_NORMAL),
        "description":     cached.get(
            "description",
            f"BTC {avg_btc:.2f}× · тренд доходности "
            f"{cached.get('yield_trend', 0):+.2f}%",
        ),
    }


# ─────────────────────────────────────────────
# 3. СИГНАЛ — ПИРАМИДА МИНТО
# ─────────────────────────────────────────────

def _classify_term(лет):
    """Участок кривой по сроку до погашения."""
    if лет <= 2:
        return "короткий"
    elif лет <= 7:
        return "средний"
    else:
        return "длинный"


def _btc_label(btc):
    """Простая интерпретация bid-to-cover для читателя."""
    if btc >= 2.5:
        return "ажиотажный спрос"
    elif btc >= 1.5:
        return "высокий спрос"
    elif btc >= 1.0:
        return "умеренный спрос"
    elif btc >= 0.5:
        return "слабый спрос"
    else:
        return "спрос ниже предложения"


def analyze_auctions(df, last_n_weeks=4):
    """
    Сигнал для дайджеста: хочет ли рынок покупать ОФЗ?
    Структура вывода: пирамида Минто.
    """
    # Берём аукционы за последние N недель
    cutoff = df["дата"].max() - pd.Timedelta(weeks=last_n_weeks)
    recent = df[df["дата"] >= cutoff].copy()

    if recent.empty:
        print("Нет данных за последние недели")
        return

    # Последний аукцион
    last = recent.iloc[0]

    # Средний bid-to-cover за период
    avg_btc  = recent["bid_to_cover"].mean()
    last_btc = last["bid_to_cover"]

    # Разбивка по участкам
    long_auctions  = recent[recent["лет_до_погашения"] > 7]
    short_auctions = recent[recent["лет_до_погашения"] <= 2]

    avg_btc_long  = long_auctions["bid_to_cover"].mean()
    avg_btc_short = short_auctions["bid_to_cover"].mean()

    # ── УРОВЕНЬ 1: ВЫВОД ──────────────────────────────────────────
    print(f"\n{'█'*55}")

    if avg_btc >= 1.5:
        вывод = "Рынок активно покупает ОФЗ — спрос устойчивый"
        рекомендация = ("Аукционы проходят успешно. "
                        "Подтверждает сигнал кривой на снижение ставки")
    elif avg_btc >= 1.0:
        вывод = "Рынок умеренно покупает ОФЗ — спрос есть, но без ажиотажа"
        рекомендация = "Нейтральный сигнал. Смотри на другие индикаторы"
    else:
        вывод = "Рынок слабо покупает ОФЗ — спрос ниже предложения"
        рекомендация = ("Осторожно с длинными ОФЗ. "
                        "Рынок не верит в скорое снижение ставки")

    print(f"  {вывод}")
    print(f"{'█'*55}")
    print(f"\n  ЧТО ОЗНАЧАЕТ: {рекомендация}")

    # ── УРОВЕНЬ 2: ПОЧЕМУ ─────────────────────────────────────────
    print(f"\n  ПОЧЕМУ — 3 сигнала аукционов:")

    print(f"\n  ① Последний аукцион ({last['дата'].strftime('%d.%m.%Y')}):")
    print(f"    {last['код_выпуска']} | срок {last['лет_до_погашения']} лет"
          f" | доходность {last['доходность_пct']:.2f}%")
    print(f"    Спрос/Предложение = {last_btc:.2f}x — {_btc_label(last_btc)}")

    print(f"\n  ② Средний bid-to-cover за {last_n_weeks} недели: {avg_btc:.2f}x")
    if not pd.isna(avg_btc_long):
        print(f"    Длинные ОФЗ (от 7 лет): {avg_btc_long:.2f}x")
    if not pd.isna(avg_btc_short):
        print(f"    Короткие ОФЗ (до 2 лет): {avg_btc_short:.2f}x")

    # Тренд доходности на аукционах
    if len(recent) >= 3:
        yield_trend = (recent["доходность_пct"].iloc[0]
                     - recent["доходность_пct"].iloc[-1])
        направление = "падает" if yield_trend < -0.1 else (
                      "растёт"  if yield_trend >  0.1 else "стабильна")
        print(f"\n  ③ Доходность на аукционах за {last_n_weeks} недели"
              f" {направление}: "
              f"{recent['доходность_пct'].iloc[-1]:.2f}%"
              f" → {recent['доходность_пct'].iloc[0]:.2f}%")
    
    # Сигнал противоречия с кривой
    if avg_btc < 1.0:
        print(f"\n  ⚡ ПРОТИВОРЕЧИЕ С КРИВОЙ:")
        print(f"    G-кривая: рынок ждёт снижения КС")
        print(f"    Аукционы: покупать по текущим ценам не спешат")
        print(f"    → Рынок ждёт лучшего момента входа")
        print(f"       или не верит в скорость снижения")

    # ── УРОВЕНЬ 3: ДЕТАЛИ ─────────────────────────────────────────
    print(f"\n  {'─'*55}")
    print(f"  ДЕТАЛИ — все аукционы за {last_n_weeks} недели")
    print(f"  {'─'*55}")
    print(f"  {'Дата':<12} {'Выпуск':<14} {'Срок':>5}"
          f" {'Дох%':>6} {'Спрос':>10} {'Разм':>10} {'BTC':>5}")
    print(f"  {'─'*55}")

    for _, row in recent.iterrows():
        участок = _classify_term(row["лет_до_погашения"])
        print(f"  {row['дата'].strftime('%d.%m.%Y'):<12}"
              f" {row['код_выпуска']:<14}"
              f" {row['лет_до_погашения']:>4.1f}л"
              f" {row['доходность_пct']:>6.2f}%"
              f" {row['спрос_млн']:>9.0f}м"
              f" {row['размещено_млн']:>9.0f}м"
              f" {row['bid_to_cover']:>5.2f}x"
              f"  [{участок}]")

    # Сохраняем
    recent.to_csv("data/auctions_recent.csv", index=False)
    df.to_csv("data/auctions_all.csv", index=False)
    print(f"\n  ✓ Сохранено: data/auctions_recent.csv, data/auctions_all.csv")


# ─────────────────────────────────────────────
# 4. ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Загружаем данные аукционов Минфина...\n")

    url      = get_latest_file_url()
    print(f"Файл: {url}\n")

    file_obj = download_xlsx(url)
    df       = parse_auctions(file_obj)

    print(f"✓ Загружено {len(df)} аукционов")
    print(f"  Период: {df['дата'].min().strftime('%d.%m.%Y')}"
          f" — {df['дата'].max().strftime('%d.%m.%Y')}\n")

    analyze_auctions(df, last_n_weeks=4)