"""
scripts/refresh_data.py

Обновляет все JSON кэши из живых источников.
Запускается по расписанию на Railway (каждый день 08:00).
Можно запускать вручную: python scripts/refresh_data.py

Порядок:
  1. G-кривая + КС → gcurve signal
  2. Аукционы → auction signal
  3. Вероятности заседаний ЦБ → cbr_probabilities
  4. Скринер ОФЗ → bond_screener
  5. Form 101 → form101_latest (если не старше 25 дней)
  6. Дайджест → digest_latest
  7. Сборный API кэш → api_overview
"""

import sys
import json
import logging
import traceback
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("refresh")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

RESULTS = {}  # собираем итог для отчёта


def step(name: str):
    """Декоратор-контекстменеджер для шагов."""
    class Step:
        def __enter__(self):
            log.info(f"▶ {name}...")
            return self
        def __exit__(self, exc_type, exc_val, tb):
            if exc_type:
                log.error(f"✗ {name}: {exc_val}")
                RESULTS[name] = f"ERROR: {exc_val}"
                return True  # не пробрасываем
            log.info(f"✓ {name}")
            RESULTS[name] = "OK"
    return Step()


# ─────────────────────────────────────────────
# 1. G-КРИВАЯ И КС
# ─────────────────────────────────────────────

def refresh_gcurve():
    with step("G-кривая"):
        from parsers.gcurve import get_last_gcurve, get_key_rate

        key_rate = get_key_rate()
        assert key_rate is not None, "Не удалось получить КС"

        df, date_str = get_last_gcurve()
        assert df is not None, "Не удалось получить G-кривую"

        result = {
            "generated_at": datetime.now().isoformat(),
            "key_rate":     key_rate,
            "curve_date":   date_str,
            "yields":       df[["срок_лет","доходность_пct"]].to_dict("records"),
        }
        _write("gcurve_latest.json", result)
        log.info(f"  КС = {key_rate}%, кривая = {date_str}, точек = {len(df)}")


# ─────────────────────────────────────────────
# 2. АУКЦИОНЫ
# ─────────────────────────────────────────────

def refresh_auctions():
    with step("Аукционы Минфина"):
        import pandas as pd
        from parsers.minfin import get_latest_file_url, download_xlsx, parse_auctions

        url  = get_latest_file_url()
        xlsx = download_xlsx(url)
        df   = parse_auctions(xlsx)
        assert df is not None and not df.empty, "Пустые данные аукционов"

        cutoff = df["дата"].max() - pd.Timedelta(weeks=4)
        recent = df[df["дата"] >= cutoff]

        avg_btc     = float(recent["bid_to_cover"].mean())
        yield_trend = float(
            recent["доходность_пct"].iloc[0] - recent["доходность_пct"].iloc[-1]
        )

        result = {
            "generated_at":  datetime.now().isoformat(),
            "avg_btc":       round(avg_btc, 2),
            "yield_trend":   round(yield_trend, 2),
            "last_date":     recent.iloc[0]["дата"].strftime("%d.%m.%Y"),
            "last_btc":      round(float(recent.iloc[0]["bid_to_cover"]), 2),
            "last_yield":    float(recent.iloc[0]["доходность_пct"]),
            "entry_signal":  avg_btc >= 1.5,
        }
        _write("auctions_latest.json", result)
        log.info(f"  BTC = {avg_btc:.2f}×, тренд доходности = {yield_trend:+.2f}%")


# ─────────────────────────────────────────────
# 3. ВЕРОЯТНОСТИ ЗАСЕДАНИЙ ЦБ
# ─────────────────────────────────────────────

def refresh_probabilities():
    with step("Вероятности ЦБ"):
        from parsers.gcurve import get_last_gcurve, get_key_rate
        from scripts.cbr_probabilities import (
            calc_meeting_probabilities, interpolate_spot
        )

        key_rate      = get_key_rate() or 14.5
        gcurve_df, dt = get_last_gcurve()
        assert gcurve_df is not None

        results = calc_meeting_probabilities(gcurve_df, key_rate, dt)
        signal  = {
            "generated_at": datetime.now().isoformat(),
            "key_rate":     key_rate,
            "curve_date":   dt,
            "meetings": [
                {
                    "date":           r["date"].isoformat(),
                    "type":           r["type"],
                    "days_ahead":     r["days_ahead"],
                    "implied_ks":     r["implied_ks"],
                    "meeting_cut_bps": r["meeting_cut_bps"],
                    "prob_cut":       round(r["meeting_prob"] * 100),
                    "scenarios": {
                        "hold":    round(r["split"]["hold"]    * 100),
                        "cut_50":  round(r["split"]["cut_50"]  * 100),
                        "cut_100": round(r["split"]["cut_100"] * 100),
                    },
                }
                for r in results
            ],
        }
        _write("cbr_probabilities.json", signal)
        log.info(f"  {len(results)} заседаний рассчитано")


# ─────────────────────────────────────────────
# 4. СКРИНЕР ОФЗ
# ─────────────────────────────────────────────

def refresh_screener():
    with step("Скринер ОФЗ"):
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location(
            "bond_screener",
            Path(__file__).parent / "bond_screener.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        supply  = mod.calc_supply_metrics()
        results = mod.run_screener(supply)

        out = {
            "generated_at":   datetime.now().isoformat(),
            "supply_metrics": {k: (bool(v) if isinstance(v, bool) else float(v) if isinstance(v, (int,float)) else v) for k,v in supply.items()},
            "bonds": [
                {
                    "secid":              r["secid"],
                    "shortname":          r["shortname"],
                    "matdate":            r["matdate"].isoformat(),
                    "duration":           float(r["duration"]),
                    "price_pct":          float(r["price_pct"]),
                    "coupon_pct":         float(r["coupon_pct"]),
                    "ytm":                float(r["ytm"]),
                    "pnl_13_theoretical": float(r["scenarios"]["КС → 13.0%"]["theoretical_pct"]),
                    "pnl_13_adjusted":    float(r["scenarios"]["КС → 13.0%"]["adjusted_pct"]),
                    "pnl_11_adjusted":    float(r["scenarios"]["КС → 11.0%"]["adjusted_pct"]),
                    "pnl_flat":           float(r["scenarios"]["Flat (hold)"]["adjusted_pct"]),
                }
                for r in results
            ],
        }
        _write("bond_screener.json", out)
        log.info(f"  {len(results)} ОФЗ проанализировано")


# ─────────────────────────────────────────────
# 5. FORM 101 (только если данные устарели)
# ─────────────────────────────────────────────

def refresh_form101():
    """
    Форма 101 обновляется раз в месяц (ЦБ публикует ~5-го числа).
    Не запускаем если кэш свежее 25 дней.
    """
    cache = DATA_DIR / "form101_latest.csv"
    if cache.exists():
        age_days = (datetime.now().timestamp() - cache.stat().st_mtime) / 86400
        if age_days < 25:
            log.info(f"  Form 101: кэш свежий ({age_days:.0f} дней), пропускаем")
            RESULTS["Form 101"] = f"SKIP (кэш {age_days:.0f}д)"
            return

    with step("Form 101"):
        import subprocess
        result = subprocess.run(
            [sys.executable, "parsers/form101.py"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[:200])
        log.info("  Form 101 обновлена")


# ─────────────────────────────────────────────
# 6. ДАЙДЖЕСТ
# ─────────────────────────────────────────────

def refresh_digest():
    with step("Дайджест"):
        import subprocess, os
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
        result = subprocess.run(
            [sys.executable, "digest.py"],
            capture_output=True, text=True,
            timeout=60, encoding='utf-8', env=env
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[:200])


# ─────────────────────────────────────────────
# 7. СБОРНЫЙ API КЭШ
# ─────────────────────────────────────────────

def refresh_api_overview():
    """Инвалидируем кэш /api/overview чтобы он пересчитался при следующем запросе."""
    with step("API overview cache"):
        path = DATA_DIR / "api_overview.json"
        if path.exists():
            path.unlink()
            log.info("  Кэш инвалидирован — пересчитается при следующем запросе")
        else:
            log.info("  Кэша не было")


# ─────────────────────────────────────────────
# УТИЛИТЫ
# ─────────────────────────────────────────────

def _write(filename: str, data: dict):
    path = DATA_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ─────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    started = datetime.now()
    log.info("=" * 50)
    log.info("ОБНОВЛЕНИЕ ДАННЫХ ЦБ-РАДАР")
    log.info("=" * 50)

    # Выполняем шаги
    refresh_gcurve()
    refresh_auctions()
    refresh_probabilities()
    refresh_screener()
    refresh_form101()
    refresh_digest()
    refresh_api_overview()

    # Итог
    elapsed = (datetime.now() - started).seconds
    log.info("=" * 50)
    log.info(f"ГОТОВО за {elapsed}с")
    for name, status in RESULTS.items():
        icon = "✓" if status == "OK" else ("→" if status.startswith("SKIP") else "✗")
        log.info(f"  {icon} {name}: {status}")
    log.info("=" * 50)

    # Если есть ошибки — выходим с кодом 1 (Railway заметит)
    errors = [k for k, v in RESULTS.items() if v.startswith("ERROR")]
    if errors:
        log.error(f"Ошибки: {errors}")
        sys.exit(1)
