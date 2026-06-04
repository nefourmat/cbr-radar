import requests
import subprocess
import dbfread
import tempfile
from datetime import datetime
import io
import shutil
import os
import re
import pandas as pd
from bs4 import BeautifulSoup

BASE_URL = "https://www.cbr.ru"

BANKS_FALLBACK = {
    354: "ГАЗПРОМБАНК",
    436: "БАНК «САНКТ-ПЕТЕРБУРГ»",
    121: "ПРОМСВЯЗЬБАНК",
    963: "ЮНИКРЕДИТ БАНК",
    328: "РОСЭКСИМБАНК",
    841: "ОТП БАНК",
    705: "КРЕДИТ ЕВРОПА БАНК",
}

SEVENZIP_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "/usr/bin/7z",
    "/usr/bin/7za",
    "7z", "7za",
]

# Счета долговых ценных бумаг в новом плане счетов (809-П, с 2019)
# 501 = FVPL (торговый портфель)
# 502 = FVOCI (справедливая стоимость через ПСД — здесь обычно ОФЗ)
# 504 = АС (амортизированная стоимость — тоже ОФЗ длинные)
DEBT_ACCOUNTS = {"501", "502", "504"}


# ─────────────────────────────────────────────
# УТИЛИТЫ
# ─────────────────────────────────────────────

def find_7zip():
    for path in SEVENZIP_PATHS:
        try:
            r = subprocess.run([path, "i"], capture_output=True, timeout=5)
            if r.returncode == 0:
                print(f"✓ 7-Zip: {path}")
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def get_rar_urls(limit=12):
    """Все RAR ссылки, свежие первыми. limit = сколько месяцев взять."""
    url     = f"{BASE_URL}/banking_sector/otchetnost-kreditnykh-organizaciy/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    soup    = BeautifulSoup(
        requests.get(url, headers=headers).text, "html.parser"
    )
    links = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"101-(\d{8})\.rar", a["href"])
        if m:
            links.append({"date": m.group(1), "url": BASE_URL + a["href"]})
    links.sort(key=lambda x: x["date"], reverse=True)
    return links[:limit]


def download_and_extract(rar_url, sevenzip):
    """Скачивает RAR и распаковывает. Возвращает (extract_dir, rar_path)."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp    = requests.get(rar_url, headers=headers)
    resp.raise_for_status()

    tmp_rar = tempfile.NamedTemporaryFile(suffix=".rar", delete=False)
    tmp_rar.write(resp.content)
    tmp_rar.close()

    extract_dir = tempfile.mkdtemp()
    result = subprocess.run(
        [sevenzip, "e", tmp_rar.name, f"-o{extract_dir}", "-y"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        shutil.rmtree(extract_dir)
        return None, tmp_rar.name

    return extract_dir, tmp_rar.name


def find_file(directory, pattern):
    for f in os.listdir(directory):
        if pattern.upper() in f.upper():
            return os.path.join(directory, f)
    return None


# ─────────────────────────────────────────────
# ЧТЕНИЕ DBF
# ─────────────────────────────────────────────

def read_bank_names(n1_path):
    """Читает справочник банков REGN → название."""
    names = {}
    try:
        for rec in dbfread.DBF(n1_path, encoding="cp866", raw=True):
            try:
                regn_raw = rec.get(b"REGN") or rec.get("REGN")
                name_raw = rec.get(b"NAME_B") or rec.get("NAME_B")

                if not regn_raw or not name_raw:
                    continue

                # REGN хранится как ASCII-строка с цифрами
                # b'1481' → '1481' → 1481
                regn_str = (
                    regn_raw.decode("ascii", errors="ignore")
                    if isinstance(regn_raw, bytes)
                    else str(regn_raw)
                ).strip()

                # NAME_B хранится в cp866
                name_str = (
                    name_raw.decode("cp866", errors="replace")
                    if isinstance(name_raw, bytes)
                    else str(name_raw)
                ).strip()

                if regn_str.isdigit() and name_str:
                    names[int(regn_str)] = name_str

            except Exception:
                continue

    except Exception as e:
        print(f"⚠ N1.dbf: {e}")

    print(f"  Загружено названий банков: {len(names)}")
    return names

def parse_b1(b1_path, report_date):
    """
    Читаем B1.dbf — позиции банков в долговых ЦБ.

    Нужные счета: 501, 502, 504 (долговые ЦБ по трём моделям учёта МСФО 9)
    Поле остатка: IITG (исходящий итог = баланс на конец месяца)
    Только актив: A_P = '1'
    """
    rows  = []
    table = dbfread.DBF(b1_path, encoding="cp866")

    for rec in table:
        num_sc = str(rec.get("NUM_SC", "")).strip()
        a_p    = str(rec.get("A_P",   "")).strip()
        iitg   = rec.get("IITG") or 0
        regn   = rec.get("REGN")

        if a_p != "1":
            continue
        if num_sc not in DEBT_ACCOUNTS:
            continue
        if not iitg or iitg <= 0:
            continue

        rows.append({
            "bank_id":    regn,
            "num_sc":     num_sc,
            "balance_mln": float(iitg) / 1000,  # тыс. руб. → млн руб.
        })

    if not rows:
        return None

    df = pd.DataFrame(rows)

    # Агрегируем по банку — сумма всех долговых ЦБ
    result = (
        df.groupby("bank_id")["balance_mln"]
        .sum()
        .reset_index()
        .rename(columns={"balance_mln": "debt_securities_mln"})
    )

    result["report_date"] = report_date
    result = result[result["debt_securities_mln"] > 0]
    result = result.sort_values("debt_securities_mln", ascending=False)
    return result


# ─────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА
# ─────────────────────────────────────────────

def process_one_month(rar_info, sevenzip):
    """Скачивает и парсит один месяц. Возвращает DataFrame."""
    extract_dir, rar_path = download_and_extract(
        rar_info["url"], sevenzip
    )
    if not extract_dir:
        return None
    
    try:
        b1_path = find_file(extract_dir, "B1.DBF")
        n1_path = find_file(extract_dir, "N1.DBF")

        if not b1_path:
            return None

        df    = parse_b1(b1_path, rar_info["date"])
        names = read_bank_names(n1_path) if n1_path else {}
        if len(names) < 100:   # N1 неполный — дополняем реестром
            registry = get_full_bank_registry()
            registry.update(names)  # N1 приоритетнее
            names = registry

        if df is not None and names:
            df["bank_name"] = df["bank_id"].map(names).fillna("Неизвестный")
        if df is not None:
            df["bank_name"] = df["bank_id"].map(names).fillna(
                df["bank_id"].map(BANKS_FALLBACK)
            ).fillna("Неизвестный")

        return df

    finally:
        os.unlink(rar_path)
        shutil.rmtree(extract_dir)


def calc_smart_money(df_current, df_prev):
    """
    Считаем изменение позиций банков за месяц.
    Умные деньги = топ банков по наращиванию долговых ЦБ.
    """
    merged = df_current.merge(
        df_prev[["bank_id", "debt_securities_mln"]],
        on="bank_id",
        suffixes=("_now", "_prev"),
        how="left"
    )
    merged["change_mln"] = (
        merged["debt_securities_mln_now"]
        - merged["debt_securities_mln_prev"].fillna(0)
    )
    merged["change_pct"] = (
        merged["change_mln"]
        / merged["debt_securities_mln_prev"].replace(0, float("nan"))
        * 100
    ).round(1)

    return merged.sort_values("change_mln", ascending=False)

def get_full_bank_registry():
    """
    Скачивает полный реестр КО с сайта ЦБ.
    Возвращает словарь REGN → название банка.
    Кэшируем в data/banks_registry.csv чтобы не скачивать каждый раз.
    """
    cache_path = "data/banks_registry.csv"

    # Возвращаем кэш если он свежий (обновляем раз в 30 дней)
    if os.path.exists(cache_path):
        mtime = os.path.getmtime(cache_path)
        if (datetime.now().timestamp() - mtime) < 30 * 86400:
            df = pd.read_csv(cache_path)
            return dict(zip(df["regn"], df["name"]))

    print("  Скачиваем реестр банков с ЦБ...")
    today = datetime.now().strftime("%m/%d/%Y")
    url = (
        "https://www.cbr.ru/Queries/UniDbQuery/DownloadExcel/98547"
        f"?FromDate={today.replace('/', '%2F')}&ToDate={today.replace('/', '%2F')}&posted=True"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        df = pd.read_excel(
            io.BytesIO(resp.content),
            header=1          # вторая строка — заголовки
        )

        # Находим нужные колонки
        print(f"  Колонки реестра: {list(df.columns)}")

        # ЦБ называет их примерно так — подберём по содержимому
        regn_col = next(
            (c for c in df.columns
             if "рег" in str(c).lower() or "номер" in str(c).lower()),
            df.columns[0]
        )
        name_col = next(
            (c for c in df.columns
             if "наименование" in str(c).lower()
             or "название" in str(c).lower()),
            df.columns[1]
        )

        result = df[[regn_col, name_col]].dropna()
        result.columns = ["regn", "name"]
        result["regn"] = pd.to_numeric(
            result["regn"], errors="coerce"
        ).dropna().astype(int)
        result = result.dropna()

        # Сохраняем кэш
        os.makedirs("data", exist_ok=True)
        result.to_csv(cache_path, index=False)
        print(f"  ✓ Реестр: {len(result)} банков → {cache_path}")

        return dict(zip(result["regn"], result["name"]))

    except Exception as e:
        print(f"  ⚠ Не удалось скачать реестр: {e}")
        return {}


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    sevenzip = find_7zip()
    if not sevenzip:
        print("7-Zip не найден → https://7-zip.org")
        exit(1)

    urls = get_rar_urls(limit=2)  # берём 2 месяца для сравнения
    if not urls:
        print("Файлы не найдены")
        exit(1)

    print(f"\nОбрабатываем {len(urls)} месяца...\n")

    # Текущий месяц
    print(f"[1/2] {urls[0]['date']}")
    df_now = process_one_month(urls[0], sevenzip)

    # Предыдущий месяц
    print(f"\n[2/2] {urls[1]['date']}")
    df_prev = process_one_month(urls[1], sevenzip)

    if df_now is None:
        print("Не удалось получить данные")
        exit(1)

    print(f"\n{'='*65}")
    print(f"УМНЫЕ ДЕНЬГИ — ДОЛГОВЫЕ ЦБ БАНКОВ (счета 501+502+504)")
    print(f"Данные на {urls[0]['date']}")
    print(f"{'='*65}")

    # Топ-20 по объёму
    top20 = df_now.head(20)
    print(f"\nТОП-20 БАНКОВ по объёму долговых ЦБ:")
    print(f"{'REGN':>6}  {'Название':<35} {'млрд руб':>10}")
    print(f"{'─'*60}")
    for _, row in top20.iterrows():
        name = row.get("bank_name", "—")[:35]
        val  = row["debt_securities_mln"] / 1000
        print(f"{int(row['bank_id']):>6}  {name:<35} {val:>10.1f}")

    # Изменения за месяц
    if df_prev is not None:
        smart = calc_smart_money(df_now, df_prev)
        top_buyers = smart[smart["change_mln"] > 0].head(10)

        print(f"\nТОП-10 НАРАЩИВАЮЩИХ ЦБ за месяц (сигнал):")
        print(f"{'REGN':>6}  {'Название':<30} {'было':>8} {'стало':>8} {'Δ млрд':>8}")
        print(f"{'─'*65}")
        for _, row in top_buyers.iterrows():
            name = row.get("bank_name", "—")[:30]
            prev = row["debt_securities_mln_prev"] / 1000
            now  = row["debt_securities_mln_now"]  / 1000
            chg  = row["change_mln"] / 1000
            print(f"{int(row['bank_id']):>6}  {name:<30} "
                  f"{prev:>8.1f} {now:>8.1f} {chg:>+8.1f}")

    # Сохраняем
    out_path = "data/form101_latest.csv"
    df_now.to_csv(out_path, index=False)
    # Добавляем change_mln если есть данные двух месяцев
    if df_prev is not None:
        smart = calc_smart_money(df_now, df_prev)
        smart.to_csv("data/form101_latest.csv", index=False)
    else:
        df_now.to_csv("data/form101_latest.csv", index=False)
    print(f"\n✓ Сохранено: {out_path}")