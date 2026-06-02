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

def get_latest_file_url():
    url     = ("https://minfin.gov.ru/ru/perfomance/"
               "public_debt/internal/operations/ofz/auction/")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    html    = requests.get(url, headers=headers).text
    soup    = BeautifulSoup(html, "html.parser")

    links = sorted([
        a["href"] for a in soup.find_all("a", href=True)
        if "Auction_Results_rus" in a["href"]
        and a["href"].endswith(".xlsx")
    ], reverse=True)

    return BASE_URL + links[0] if links else None


def download_xlsx(url):
    headers  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return BytesIO(response.content)


# ─────────────────────────────────────────────
# 2. ПАРСИНГ
# ─────────────────────────────────────────────

def parse_auctions(file_obj):
    df = pd.read_excel(file_obj, sheet_name="Лист1", header=5)
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