"""
scripts/refresh_data.py

Обновляет все JSON кэши из живых источников.
Запускается по расписанию на Railway (каждый день 08:00).
Можно запускать вручную: python scripts/refresh_data.py

Порядок:
  1. G-кривая + КС → gcurve signal
  2. Аукционы → auction signal
  3. Вероятности заседаний ЦБ → cbr_probabilities
  4. Инфляция → inflation_latest
  5. Скринер ОФЗ → bond_screener
  6. Form 101 → form101_latest (если не старше 25 дней)
  7. Дайджест → digest_latest
  8. Сборный API кэш → api_overview
"""

import sys
import os
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
        from parsers.minfin import (
            get_latest_file_url, download_xlsx, parse_auctions,
            build_auction_signal,
        )

        url  = get_latest_file_url()
        if not url:
            raise ValueError("Минфин не вернул URL файла")
        xlsx = download_xlsx(url)
        df   = parse_auctions(xlsx)
        assert df is not None and not df.empty, "Пустые данные аукционов"

        signal = build_auction_signal(df)
        result = {"generated_at": datetime.now().isoformat(), **signal}
        _write("auctions_latest.json", result)
        _write_csv(df, DATA_DIR / "auctions_all.csv")
        log.info(
            f"  BTC = {signal['avg_btc']:.2f}×, "
            f"тренд доходности = {signal['yield_trend']:+.2f}%"
        )


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
# 4. ИНФЛЯЦИЯ
# ─────────────────────────────────────────────

def refresh_inflation():
    with step("Инфляция"):
        from parsers.inflation import get_inflation_data, build_inflation_signal
        from parsers.inflation_expectations import get_inflation_expectations
        from parsers.gcurve import get_key_rate

        rows = get_inflation_data()
        assert rows, "Не удалось получить данные по инфляции"

        # инФОМ (наблюдаемая/ожидаемая) — опционально, не роняем шаг при сбое
        try:
            expectations = get_inflation_expectations()
        except Exception as e:
            log.warning(f"  инФОМ недоступен: {e}")
            expectations = None

        key_rate = get_key_rate() or 14.5
        signal   = build_inflation_signal(rows, key_rate=key_rate,
                                          expectations=expectations)
        result   = {"generated_at": datetime.now().isoformat(), **signal}
        _write("inflation_latest.json", result)
        obs = signal.get("observed")
        log.info(
            f"  инфляция = {signal['infl_yoy']}% г/г, "
            f"реальная ставка = {signal['real_rate']} п.п."
            + (f", наблюдаемая = {obs}%" if obs is not None else "")
        )


# ─────────────────────────────────────────────
# 5. СКРИНЕР ОФЗ
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
        from parsers.gcurve import get_key_rate
        key_rate = get_key_rate() or 14.5
        results, rate_scenarios = mod.run_screener(supply, key_rate)

        out = {
            "generated_at":   datetime.now().isoformat(),
            "key_rate":       key_rate,
            "rate_scenarios": rate_scenarios,
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
                    "base_scenario":      r["base_scenario"],
                    "pnl_base_adjusted":  float(r["pnl_base_adjusted"]),
                    "pnl_mid_adjusted":   float(r["pnl_mid_adjusted"]),
                    "pnl_deep_adjusted":  float(r["pnl_deep_adjusted"]),
                    "pnl_flat":           float(r["pnl_flat"]),
                    "pnl_13_adjusted":    float(r["pnl_13_adjusted"]),
                    "pnl_11_adjusted":    float(r["pnl_11_adjusted"]),
                }
                for r in results
            ],
        }
        _write("bond_screener.json", out)
        log.info(f"  {len(results)} ОФЗ проанализировано")


# ─────────────────────────────────────────────
# 6. FORM 101 (только если данные устарели)
# ─────────────────────────────────────────────

def _form101_cache_valid(path) -> bool:
    """Проверяет, что CSV-кэш Form 101 читается, непустой и содержит нужные колонки."""
    try:
        import pandas as pd
        df = pd.read_csv(path)
        return not df.empty and "bank_id" in df.columns
    except Exception:
        return False


def refresh_form101():
    """
    Форма 101 обновляется раз в месяц (ЦБ публикует ~5-го числа).
    Не запускаем если кэш свежее 25 дней и валиден.
    """
    cache = DATA_DIR / "form101_latest.csv"
    if cache.exists():
        age_days = (datetime.now().timestamp() - cache.stat().st_mtime) / 86400
        if age_days < 25 and _form101_cache_valid(cache):
            log.info(f"  Form 101: кэш свежий ({age_days:.0f} дней), пропускаем")
            RESULTS["Form 101"] = f"SKIP (кэш {age_days:.0f}д)"
            return
        if age_days < 25:
            log.warning("  Form 101: кэш свежий по дате, но повреждён/пуст — обновляем")

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
# 7. ДАЙДЖЕСТ
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
# 8. СБОРНЫЙ API КЭШ
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
    # Атомарная запись: временный файл в той же папке → os.replace
    path = DATA_DIR / filename
    tmp  = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _write_csv(df, path):
    """Атомарная запись DataFrame в CSV (временный файл → os.replace)."""
    path = Path(path)
    tmp  = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


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
    refresh_inflation()
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
