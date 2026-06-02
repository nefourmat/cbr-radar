"""
scripts/build_history.py

Собирает исторические данные для pattern engine:
  1. G-кривая ЦБ — 10 лет (ежедневно)
  2. Аукционы Минфина — 2016–2026 (все файлы по годам)
  3. История решений ЦБ по КС — с 2019 года

Запуск: python scripts/build_history.py
"""

import sys
import os
import time
import requests
import pandas as pd
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, date
from bs4 import BeautifulSoup

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent.parent))
from parsers.gcurve  import get_gcurve
from parsers.minfin  import parse_auctions

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

BASE_URL = "https://www.cbr.ru"


# ═══════════════════════════════════════════════════════
# 1. G-КРИВАЯ — 5 ЛЕТ
# ═══════════════════════════════════════════════════════

def build_gcurve_history(years=10):
    """
    Скачивает G-кривую за последние N лет по рабочим дням.
    Пропускает даты которые уже есть в файле.
    """
    out_path = DATA_DIR / "gcurve_history_full.csv"

    # Загружаем уже скачанные даты
    existing_dates = set()
    if out_path.exists():
        df_existing = pd.read_csv(out_path)
        existing_dates = set(df_existing["date"].unique())
        print(f"  Уже скачано: {len(existing_dates)} дат")

    today      = date.today()
    total_days = years * 365
    results    = []
    errors_row = 0

    print(f"  Скачиваем G-кривую за {years} лет...")

    for days_ago in range(1, total_days):
        d = today - timedelta(days=days_ago)

        # Пропускаем выходные
        if d.weekday() >= 5:
            continue

        date_str = d.strftime("%d.%m.%Y")

        # Пропускаем если уже есть
        if date_str in existing_dates:
            continue

        try:
            df = get_gcurve(date_str)
            if df is not None:
                results.append(df)
                errors_row = 0
                if len(results) % 100 == 0:
                    print(f"    {len(results) + len(existing_dates)} дат "
                          f"скачано... ({date_str})")
            else:
                errors_row += 1
        except Exception:
            errors_row += 1

        # Если 15 рабочих дней подряд без данных — конец архива
        if errors_row > 45:  # биржа закрывалась на ~30 дней в марте 2022
            print(f"    Конец доступных данных: {date_str}")
            break

        # Небольшая пауза чтобы не перегружать сервер ЦБ
        time.sleep(0.1)

    if results:
        df_new = pd.concat(results, ignore_index=True)

        if out_path.exists():
            df_old = pd.read_csv(out_path)
            df_all = pd.concat([df_old, df_new], ignore_index=True)
            df_all = df_all.drop_duplicates(subset=["date", "срок_лет"])
        else:
            df_all = df_new

        # Сортируем по дате
        df_all["_dt"] = pd.to_datetime(df_all["date"], format="%d.%m.%Y")
        df_all = df_all.sort_values("_dt", ascending=False).drop("_dt", axis=1)
        df_all.to_csv(out_path, index=False)

        total = len(df_all["date"].unique())
        print(f"  ✓ G-кривая: {total} торговых дней → {out_path}")
    else:
        print("  G-кривая: новых данных не найдено")


# ═══════════════════════════════════════════════════════
# 2. АУКЦИОНЫ МИНФИНА — ВСЕ ГОДЫ
# ═══════════════════════════════════════════════════════

def get_minfin_file_urls():
    """Находит все XLSX файлы аукционов на сайте Минфина."""
    url     = ("https://minfin.gov.ru/ru/perfomance/"
               "public_debt/internal/operations/ofz/auction/")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    soup    = BeautifulSoup(
        requests.get(url, headers=headers).text, "html.parser"
    )

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "Auction_Results_rus" in href and href.endswith(".xlsx"):
            # Извлекаем год из имени файла
            import re
            m = re.search(r"_(\d{4})_", href)
            year = int(m.group(1)) if m else None
            links.append({
                "year": year,
                "url":  ("https://minfin.gov.ru" + href
                         if href.startswith("/") else href),
            })

    links.sort(key=lambda x: x["year"] or 0)
    return links


def build_auctions_history():
    """Скачивает все годовые файлы аукционов и объединяет."""
    out_path = DATA_DIR / "auctions_all.csv"

    print("  Ищем файлы аукционов Минфина...")
    file_urls = get_minfin_file_urls()
    print(f"  Найдено файлов: {len(file_urls)}")
    for f in file_urls:
        print(f"    {f['year']}: {f['url']}")

    all_dfs = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for file_info in file_urls:
        year = file_info["year"]
        url  = file_info["url"]
        print(f"\n  Скачиваем {year}...")

        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            print(f"    Размер: {len(resp.content):,} байт")

            df = parse_auctions(BytesIO(resp.content))
            if df is not None and not df.empty:
                all_dfs.append(df)
                print(f"    ✓ {len(df)} аукционов")
            else:
                print(f"    ⚠ Данных не получено")

        except Exception as e:
            print(f"    ✗ Ошибка: {e}")

    if all_dfs:
        result = pd.concat(all_dfs, ignore_index=True)
        result = result.drop_duplicates(
            subset=["дата", "код_выпуска"]
        ).sort_values("дата", ascending=False)
        result.to_csv(out_path, index=False)
        print(f"\n  ✓ Аукционы: {len(result)} записей "
              f"за {result['дата'].dt.year.nunique()} лет → {out_path}")
    else:
        print("  ✗ Данных аукционов не получено")


# ═══════════════════════════════════════════════════════
# 3. ИСТОРИЯ РЕШЕНИЙ ЦБ ПО КС
# ═══════════════════════════════════════════════════════

def build_key_rate_history():
    """
    Полная история КС через POST-запрос с диапазоном дат.
    Аналогично G-кривой.
    """
    out_path = DATA_DIR / "cbr_decisions.csv"

    url     = "https://www.cbr.ru/hd_base/KeyRate/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # Запрашиваем всю историю с 2013 года
    data = {
        "UniDbQuery.Posted": "True",
        "UniDbQuery.From":   "14.09.2013",  # первое решение по КС
        "UniDbQuery.To":     datetime.today().strftime("%d.%m.%Y"),
    }

    print("  Скачиваем полную историю КС с 2013 года...")
    resp = requests.post(url, data=data, headers=headers)
    resp.raise_for_status()
    print(f"  Размер страницы: {len(resp.text):,} символов")

    soup  = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="data")

    if not table:
        print("  ✗ Таблица не найдена")
        return

    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        date_str = cells[0].get_text(strip=True)
        rate_str = cells[1].get_text(strip=True).replace(",", ".")
        try:
            rows.append({
                "decision_date": date_str,
                "rate_pct":      float(rate_str)
            })
        except ValueError:
            continue

    df = pd.DataFrame(rows)
    df["decision_date"] = pd.to_datetime(
        df["decision_date"], format="%d.%m.%Y", errors="coerce"
    )
    df = df.dropna().sort_values("decision_date", ascending=False)

    # Изменение ставки в базисных пунктах
    df["rate_change_bps"] = (
        (df["rate_pct"] - df["rate_pct"].shift(-1)) * 100
    ).round(0).fillna(0).astype(int)

    df["direction"] = df["rate_change_bps"].apply(
        lambda x: "cut" if x < 0 else ("hike" if x > 0 else "hold")
    )

    df.to_csv(out_path, index=False)
    print(f"  ✓ История КС: {len(df)} решений с "
          f"{df['decision_date'].min().date()} → {out_path}")

    # Показываем разбивку по направлениям
    cuts  = (df["direction"] == "cut").sum()
    hikes = (df["direction"] == "hike").sum()
    holds = (df["direction"] == "hold").sum()
    print(f"  Снижений: {cuts}, повышений: {hikes}, без изменений: {holds}")

    print(f"\n  Последние 10 решений:")
    print(f"  {'Дата':<14} {'КС%':>6} {'Δ бп':>7}  Направление")
    print(f"  {'─'*40}")
    for _, row in df.head(10).iterrows():
        arrow = "↓" if row["direction"] == "cut" else (
                "↑" if row["direction"] == "hike" else "→")
        print(f"  {str(row['decision_date'].date()):<14} "
              f"{row['rate_pct']:>5.2f}%  "
              f"{row['rate_change_bps']:>+5} бп  {arrow}")


# ═══════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("ПОСТРОЕНИЕ ИСТОРИЧЕСКОЙ БАЗЫ ДАННЫХ")
    print("Это займёт 15–30 минут. Не закрывай терминал.")
    print("=" * 60)

    # Шаг 1: История КС (быстро — одна страница)
    print("\n[1/3] История решений ЦБ по ключевой ставке")
    print("─" * 60)
    try:
        build_key_rate_history()
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")

    # Шаг 2: Аукционы (несколько файлов)
    print("\n[2/3] История аукционов ОФЗ (Минфин, все годы)")
    print("─" * 60)
    try:
        build_auctions_history()
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")

    # Шаг 3: G-кривая (самый долгий — много запросов)
    print("\n[3/3] G-кривая ЦБ — 10 лет")
    print("─" * 60)
    print("  Это займёт 10–20 минут (один запрос в день).")
    try:
        build_gcurve_history(years=10)
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")

    # Итог
    print("\n" + "=" * 60)
    print("ИТОГ")
    print("=" * 60)
    for fname in ["gcurve_history_full.csv",
                  "auctions_all.csv",
                  "cbr_decisions.csv"]:
        path = DATA_DIR / fname
        if path.exists():
            size = path.stat().st_size // 1024
            if fname.endswith(".csv"):
                rows = len(pd.read_csv(path))
                print(f"  ✓ {fname}: {rows} строк ({size} KB)")
        else:
            print(f"  ✗ {fname}: не создан")

    print("\nСледующий шаг: python scripts/pattern_engine.py")