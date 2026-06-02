"""
scripts/build_form101_history.py

Скачивает и парсит Форму 101 за последние 36 месяцев.
Строит историю позиций банков в долговых ЦБ.
Считает сигнал «N месяцев подряд наращивания».

Запуск: python scripts/build_form101_history.py
Время: ~20–30 минут (36 файлов по ~350KB)
"""

import sys
import json
import time
import subprocess
import shutil
import tempfile
import os
import re
import pandas as pd
import dbfread
import requests
from pathlib import Path
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_DIR  = Path("data")
CACHE_DIR = DATA_DIR / "form101_cache"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

BASE_URL = "https://www.cbr.ru/vfs/credit/forms"

SEVENZIP_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "7z", "7za",
]

# Счета долговых ЦБ (Положение 809-П)
DEBT_ACCOUNTS = {"501", "502", "504"}

# Хардкод банков которых нет в N1.dbf
BANKS_FALLBACK = {
    354:  "ГАЗПРОМБАНК",
    436:  "БАНК «САНКТ-ПЕТЕРБУРГ»",
    121:  "ПРОМСВЯЗЬБАНК",
    963:  "ЮНИКРЕДИТ БАНК",
    328:  "РОСЭКСИМБАНК",
    841:  "ОТП БАНК",
    705:  "КРЕДИТ ЕВРОПА БАНК",
    3255: "ПСБ",
}


# ─────────────────────────────────────────────
# УТИЛИТЫ
# ─────────────────────────────────────────────

def find_7zip():
    for path in SEVENZIP_PATHS:
        try:
            r = subprocess.run([path, "i"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def month_to_rar_date(year, month):
    """
    Данные за месяц M публикуются в RAR файле с датой 1-го числа M+1.
    Апрель 2026 → 20260501
    """
    d = date(year, month, 1) + relativedelta(months=1)
    return d.strftime("%Y%m%d")


def get_months_range(months_back=36):
    """Генерирует список (год, месяц) за последние N месяцев."""
    today = date.today()
    result = []
    for i in range(1, months_back + 1):
        d = date(today.year, today.month, 1) - relativedelta(months=i)
        result.append((d.year, d.month))
    return result


def download_rar(url, dest_path):
    """Скачивает RAR во временный файл."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp    = requests.get(url, headers=headers, timeout=60)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    return True


def extract_rar(rar_path, extract_dir, sevenzip):
    """Распаковывает RAR через 7-Zip."""
    result = subprocess.run(
        [sevenzip, "e", str(rar_path), f"-o{extract_dir}", "-y"],
        capture_output=True, text=True
    )
    return result.returncode == 0


def find_file(directory, pattern):
    for f in os.listdir(directory):
        if pattern.upper() in f.upper():
            return os.path.join(directory, f)
    return None


def read_bank_names(n1_path):
    names = {}
    try:
        for rec in dbfread.DBF(n1_path, encoding="cp866", raw=True):
            try:
                regn_raw = rec.get(b"REGN") or rec.get("REGN")
                name_raw = rec.get(b"NAME_B") or rec.get("NAME_B")
                if not regn_raw or not name_raw:
                    continue
                regn_str = (regn_raw.decode("ascii", errors="ignore")
                            if isinstance(regn_raw, bytes)
                            else str(regn_raw)).strip()
                name_str = (name_raw.decode("cp866", errors="replace")
                            if isinstance(name_raw, bytes)
                            else str(name_raw)).strip()
                if regn_str.isdigit() and name_str:
                    names[int(regn_str)] = name_str
            except Exception:
                continue
    except Exception:
        pass
    return names


def parse_b1(b1_path):
    """Парсит B1.dbf → DataFrame с позициями банков."""
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
        if not iitg or float(iitg) <= 0:
            continue
        rows.append({
            "bank_id": regn,
            "balance_mln": float(iitg) / 1000,
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    return (df.groupby("bank_id")["balance_mln"]
              .sum()
              .reset_index()
              .rename(columns={"balance_mln": "debt_mln"}))


# ─────────────────────────────────────────────
# ОСНОВНОЙ ПРОЦЕСС
# ─────────────────────────────────────────────

def process_month(year, month, sevenzip):
    """
    Обрабатывает один месяц: скачивает, парсит.
    Использует кэш — повторно не скачивает.
    Возвращает DataFrame или None.
    """
    rar_date  = month_to_rar_date(year, month)
    cache_csv = CACHE_DIR / f"form101_{rar_date}.csv"

    # Читаем из кэша если уже обработано
    if cache_csv.exists():
        return pd.read_csv(cache_csv)

    url      = f"{BASE_URL}/101-{rar_date}.rar"
    rar_path = CACHE_DIR / f"101-{rar_date}.rar"

    # Скачиваем
    if not download_rar(url, rar_path):
        return None  # файл не существует

    # Распаковываем
    extract_dir = tempfile.mkdtemp()
    try:
        if not extract_rar(rar_path, extract_dir, sevenzip):
            return None

        b1_path    = find_file(extract_dir, "B1.DBF")
        n1_path    = find_file(extract_dir, "N1.DBF")
        names_path = find_file(extract_dir, "NAMES.DBF")

        if not b1_path:
            return None

        df = parse_b1(b1_path)
        if df is None:
            return None

        # Имена банков
        names = {}
        if n1_path:
            names = read_bank_names(n1_path)
        names.update({k: v for k, v in BANKS_FALLBACK.items()
                      if k not in names})

        df["bank_name"]   = df["bank_id"].map(names).fillna(
            df["bank_id"].apply(lambda x: f"REGN {int(x)}")
        )
        df["report_month"] = f"{year:04d}-{month:02d}"

        # Кэшируем
        df.to_csv(cache_csv, index=False)
        return df

    finally:
        rar_path.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# АНАЛИЗ: СИГНАЛ «N МЕСЯЦЕВ ПОДРЯД»
# ─────────────────────────────────────────────

def build_signal(history_df):
    """
    Для каждого месяца считаем:
    - суммарный прирост позиций топ-20 банков
    - идёт ли рост N-й месяц подряд
    """
    months = sorted(history_df["report_month"].unique())

    signal_rows = []
    for i, month in enumerate(months):
        month_df = history_df[history_df["report_month"] == month]

        # Топ-20 по объёму
        top20 = month_df.nlargest(20, "debt_mln")

        # Суммарная позиция топ-20
        total = top20["debt_mln"].sum()

        signal_rows.append({
            "month":     month,
            "total_mln": round(total),
            "n_banks":   len(top20),
        })

    signal = pd.DataFrame(signal_rows)
    signal = signal.sort_values("month").reset_index(drop=True)

    # Изменение месяц к месяцу
    signal["change_mln"] = signal["total_mln"].diff()
    signal["growing"]    = signal["change_mln"] > 0

    # Стрик: сколько месяцев подряд растёт
    streak = []
    current = 0
    for growing in signal["growing"]:
        if growing:
            current += 1
        else:
            current = 0
        streak.append(current)
    signal["streak"] = streak

    return signal


def format_signal_output(signal, history_df):
    """Форматирует вывод для дайджеста."""
    if signal.empty:
        return "Данных недостаточно"

    last     = signal.iloc[-1]
    prev     = signal.iloc[-2] if len(signal) > 1 else None
    streak   = int(last["streak"])
    change   = last["change_mln"]

    lines = []
    W = 63

    lines += [
        "",
        "═" * W,
        "  УМНЫЕ ДЕНЬГИ — ИСТОРИЯ ПОЗИЦИЙ",
        "═" * W,
    ]

    # Главный сигнал
    if streak >= 3:
        lines.append(
            f"\n  ⚡ Топ-20 банков наращивают долговые ЦБ "
            f"{streak}-й месяц подряд"
        )
    elif streak == 2:
        lines.append(
            f"\n  Топ-20 банков наращивают долговые ЦБ "
            f"2-й месяц подряд"
        )
    elif streak == 1:
        lines.append(
            f"\n  Топ-20 банков нарастили позицию в этом месяце"
        )
    else:
        lines.append(
            f"\n  Топ-20 банков сократили позицию в этом месяце"
        )

    # Динамика за последние 6 месяцев
    recent = signal.tail(6)
    lines += [
        "",
        f"  {'Месяц':<10} {'Позиция топ-20':>16} {'Изменение':>14} {'Стрик':>8}",
        f"  {'─' * (W-2)}",
    ]
    for _, row in recent.iterrows():
        arrow  = "↑" if row["growing"] else "↓"
        streak_str = (f"{int(row['streak'])} мес ↑"
                      if row["streak"] > 0 else "—")
        chg = row["change_mln"]
        chg_str = f"{arrow}{chg/1000:>+.0f} млрд"
        lines.append(
            f"  {row['month']:<10} "
            f"{row['total_mln']/1000:>14.0f} млрд"
            f" {chg_str:>14}"
            f" {streak_str:>10}"
        )

    # Топ-5 банков текущего месяца с изменением
    last_month = signal.iloc[-1]["month"]
    prev_month = signal.iloc[-2]["month"] if len(signal) > 1 else None

    last_df = history_df[history_df["report_month"] == last_month]
    if prev_month:
        prev_df = history_df[history_df["report_month"] == prev_month]
        merged  = last_df.merge(
            prev_df[["bank_id", "debt_mln"]],
            on="bank_id", suffixes=("_now", "_prev"), how="left"
        )
        merged["change_mln"] = (merged["debt_mln_now"]
                               - merged["debt_mln_prev"].fillna(0))
        top_buyers = (merged[merged["change_mln"] > 0]
                      .nlargest(5, "change_mln"))
    else:
        top_buyers = last_df.nlargest(5, "debt_mln")
        top_buyers = top_buyers.assign(change_mln=None)

    lines += [
        f"\n  Топ-5 банков нарастивших позицию ({last_month}):",
        f"  {'─' * (W-2)}",
    ]
    for _, row in top_buyers.iterrows():
        name   = str(row.get("bank_name", f"REGN {int(row['bank_id'])}"))[:30]
        chg    = row.get("change_mln", 0) or 0
        lines.append(f"  · {name:<32} {chg/1000:>+.1f} млрд")

    lines.append("═" * W)
    return "\n".join(lines)


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    try:
        from dateutil.relativedelta import relativedelta
    except ImportError:
        import subprocess as _sp
        _sp.run(["pip", "install", "python-dateutil",
                 "--break-system-packages"], capture_output=True)
        from dateutil.relativedelta import relativedelta

    print("=" * 60)
    print("FORM 101 — ИСТОРИЯ ЗА 36 МЕСЯЦЕВ")
    print("=" * 60)

    sevenzip = find_7zip()
    if not sevenzip:
        print("7-Zip не найден → https://7-zip.org")
        sys.exit(1)
    print(f"✓ 7-Zip: {sevenzip}\n")

    months = get_months_range(months_back=36)
    print(f"Обрабатываем {len(months)} месяцев: "
          f"{months[-1][0]}-{months[-1][1]:02d} → "
          f"{months[0][0]}-{months[0][1]:02d}\n")

    all_dfs  = []
    success  = 0
    failed   = 0

    for i, (year, month) in enumerate(reversed(months), 1):
        month_str = f"{year}-{month:02d}"
        print(f"[{i:02d}/{len(months)}] {month_str}...", end=" ", flush=True)

        try:
            df = process_month(year, month, sevenzip)
            if df is not None:
                all_dfs.append(df)
                success += 1
                banks_count = len(df)
                total_trln  = df["debt_mln"].sum() / 1_000_000
                print(f"✓ {banks_count} банков, ₽{total_trln:.1f} трлн")
            else:
                failed += 1
                print("нет данных")
        except Exception as e:
            failed += 1
            print(f"ошибка: {e}")

        time.sleep(0.2)  # пауза чтобы не нагружать сервер

    print(f"\nИтого: {success} успешно, {failed} не получено\n")

    if not all_dfs:
        print("Нет данных для анализа")
        sys.exit(1)

    # Объединяем историю
    history = pd.concat(all_dfs, ignore_index=True)
    out_path = DATA_DIR / "form101_history.csv"
    history.to_csv(out_path, index=False)
    print(f"✓ История сохранена: {out_path}")
    print(f"  Строк: {len(history)}, "
          f"месяцев: {history['report_month'].nunique()}, "
          f"банков: {history['bank_id'].nunique()}\n")

    # Строим сигнал
    signal = build_signal(history)
    signal.to_csv(DATA_DIR / "form101_signal.csv", index=False)

    # Выводим результат
    output = format_signal_output(signal, history)
    print(output)

    # Сохраняем JSON для дайджеста
    last = signal.iloc[-1]
    signal_json = {
        "generated_at":  datetime.now().isoformat(),
        "last_month":    last["month"],
        "streak_months": int(last["streak"]),
        "total_mln":     float(last["total_mln"]),
        "change_mln":    float(last["change_mln"])
                         if not pd.isna(last["change_mln"]) else 0,
        "signal":        ("strong_buy"  if last["streak"] >= 3
                          else "buy"    if last["streak"] >= 2
                          else "neutral" if last["streak"] == 1
                          else "reduce"),
    }

    with open(DATA_DIR / "form101_signal.json", "w", encoding="utf-8") as f:
        json.dump(signal_json, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Сигнал: {signal_json['signal'].upper()} "
          f"(стрик {signal_json['streak_months']} мес)")
    print(f"✓ Сохранено: data/form101_signal.json")
    print("\nСледующий шаг: добавить сигнал в digest.py")