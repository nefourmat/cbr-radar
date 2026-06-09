"""
main.py — FastAPI бэкенд ЦБ-Радар
"""
import json
import logging
import os
import sys
import subprocess
import threading
import hmac
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import pandas as pd
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler

sys.path.insert(0, str(Path(__file__).parent))

from parsers.gcurve import get_last_gcurve, get_key_rate
from parsers.minfin import (
    get_latest_file_url, download_xlsx, parse_auctions,
    build_auction_signal, enrich_auction_cache,
)
from parsers.inflation import get_inflation_data, build_inflation_signal
from parsers.inflation_expectations import get_inflation_expectations

# ─────────────────────────────────────────────
# ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cbr_radar")

# ─────────────────────────────────────────────
# ДИРЕКТОРИИ
# ─────────────────────────────────────────────
DATA_DIR   = Path("data")
STATIC_DIR = Path("static")
DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# JSON UTF-8
# ─────────────────────────────────────────────
def json_resp(data: dict, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False, default=str),
        status_code=status_code,
        media_type="application/json; charset=utf-8",
    )

# ─────────────────────────────────────────────
# КЭШИРОВАНИЕ
# ─────────────────────────────────────────────
def read_cache(filename: str, max_age_hours: float = 24) -> Optional[dict]:
    path = DATA_DIR / filename
    if not path.exists():
        return None
    age_h = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
    if age_h > max_age_hours:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Ошибка чтения кэша {filename}: {e}")
        return None

def write_cache(filename: str, data: dict):
    # Атомарная запись: пишем во временный файл в той же папке, затем os.replace
    try:
        path = DATA_DIR / filename
        tmp  = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        log.error(f"Ошибка записи кэша {filename}: {e}")

# ─────────────────────────────────────────────
# REFRESH — запускается через subprocess (безопасно)
# ─────────────────────────────────────────────
_refresh_lock = threading.Lock()

def run_refresh():
    """Запускает refresh_data.py как subprocess с правильным Python."""
    # Не запускаем параллельные refresh: если уже идёт — пропускаем
    if not _refresh_lock.acquire(blocking=False):
        log.warning("refresh уже выполняется — пропускаем")
        return
    log.info("Запуск refresh_data.py...")
    try:
        env = os.environ.copy()
        result = subprocess.run(
            [sys.executable, "scripts/refresh_data.py"],
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if result.returncode == 0:
            log.info("refresh_data.py завершён успешно")
        else:
            log.error(f"refresh_data.py ошибка:\n{result.stderr[:500]}")
    except subprocess.TimeoutExpired:
        log.error("refresh_data.py таймаут 300с")
    except Exception as e:
        log.error(f"refresh_data.py: {e}")
    finally:
        _refresh_lock.release()

def needs_refresh() -> bool:
    """Нужно ли обновить данные при старте?"""
    critical = ["gcurve_latest.json", "cbr_probabilities.json"]
    for f in critical:
        if not (DATA_DIR / f).exists():
            return True
        age_h = (datetime.now().timestamp() - (DATA_DIR / f).stat().st_mtime) / 3600
        if age_h > 12:
            return True
    return False

# ─────────────────────────────────────────────
# APSCHEDULER
# ─────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Europe/Moscow")

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Запуск ЦБ-Радар API...")

    # Обновляем данные при старте если их нет или они устарели
    if needs_refresh():
        log.info("Данных нет или устарели — запускаем refresh при старте...")
        t = threading.Thread(target=run_refresh, daemon=True)
        t.start()

    # Расписание
    scheduler.add_job(run_refresh, "cron",
        hour=8, minute=0, id="daily", replace_existing=True)
    scheduler.add_job(run_refresh, "cron",
        day_of_week="wed", hour=13, minute=30,
        id="wednesday", replace_existing=True)
    scheduler.start()
    log.info("APScheduler: 08:00 и Ср 13:30 МСК")
    yield
    scheduler.shutdown(wait=False)
    log.info("APScheduler остановлен")

# ─────────────────────────────────────────────
# ПРИЛОЖЕНИЕ
# ─────────────────────────────────────────────
app = FastAPI(
    title="ЦБ-Радар API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: список origin'ов настраивается через env CORS_ORIGINS (через запятую).
# Если не задан — по умолчанию "*" (поведение не меняется).
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],   # ← POST для /api/refresh и /api/cache/clear
    allow_headers=["*"],
)


def require_refresh_token(x_refresh_token: str = Header(default=None)):
    """
    Авторизация мутирующих эндпоинтов.
    Если env REFRESH_TOKEN задан — требуем совпадающий заголовок X-Refresh-Token.
    Если не задан — пропускаем (обратная совместимость, поведение не меняется).
    """
    expected = os.environ.get("REFRESH_TOKEN")
    if not expected:
        return
    if not x_refresh_token or not hmac.compare_digest(x_refresh_token, expected):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий токен")

if STATIC_DIR.exists() and any(STATIC_DIR.iterdir()):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ─────────────────────────────────────────────
# СИГНАЛЫ
# ─────────────────────────────────────────────
def compute_curve_signal(key_rate: float) -> dict:
    df, date_str = get_last_gcurve()
    if df is None:
        log.warning("G-кривая недоступна")
        return {
            "status": "neu", "label": "Нет данных", "arrow": "→",
            "exp_cut": 0, "y1": 0, "y2": 0, "y10": 0,
            "slope_2_10": 0, "min_yield": 0, "date": "—",
            "description": "G-кривая временно недоступна",
        }
    min_yield = float(df["доходность_пct"].min())
    def _y(t):
        r = df[df["срок_лет"] == t]["доходность_пct"]
        return float(r.values[0]) if not r.empty else 0.0
    y1, y2, y10 = _y(1.0), _y(2.0), _y(10.0)
    exp_cut = round(key_rate - min_yield, 2)
    return {
        "status":      "bull" if exp_cut > 0.5 else "neu",
        "label":       "Бычья" if exp_cut > 0.5 else "Нейтральная",
        "arrow":       "↑" if exp_cut > 0.5 else "→",
        "exp_cut":     exp_cut,
        "y1":          round(y1, 2),
        "y2":          round(y2, 2),
        "y10":         round(y10, 2),
        "slope_2_10":  round(y10 - y2, 2),
        "min_yield":   round(min_yield, 2),
        "date":        date_str,
        "description": f"1Y = {y1:.2f}% при КС {key_rate}% · ожид. снижение до ~{min_yield:.1f}%",
    }

def _neutral_auction() -> dict:
    return {
        "status": "neu", "label": "Нет данных", "arrow": "→",
        "avg_btc": 0.49, "long_btc": 0.45, "last_btc": 0.49,
        "last_date": "—", "last_code": "—", "last_yield": 0.0,
        "last_demand_mln": 0, "yield_trend": 0.0,
        "supply_pressure": 0.67, "pass_through": 0.66,
        "entry_signal": False,
        "description": "Данные аукционов обновляются...",
    }

def compute_auction_signal() -> dict:
    # Сначала кэш (любой возраст — лучше устаревшие данные чем ничего)
    cached = read_cache("auctions_latest.json", max_age_hours=36)
    if cached and "error" not in cached and cached.get("avg_btc"):
        return enrich_auction_cache(cached)

    try:
        url = get_latest_file_url()
        if not url:
            raise ValueError("Минфин не вернул URL файла")
        xlsx = download_xlsx(url)
        df   = parse_auctions(xlsx)
        if df is None or df.empty:
            raise ValueError("Пустые данные аукционов")
        signal = build_auction_signal(df)
        write_cache("auctions_latest.json", {
            "generated_at": datetime.now().isoformat(), **signal
        })
        df.to_csv(DATA_DIR / "auctions_all.csv", index=False)
        return signal
    except Exception as e:
        log.error(f"Аукционы: {e}")
        stale = read_cache("auctions_latest.json", max_age_hours=9999)
        return enrich_auction_cache(stale) if stale else _neutral_auction()

def compute_banks_signal() -> dict:
    path = DATA_DIR / "form101_latest.csv"
    if not path.exists():
        return {
            "status": "neu", "label": "Нет данных", "arrow": "→",
            "total_bln": 0, "streak": 0, "buyers": [],
            "description": "Форма 101 ещё не загружена",
        }
    try:
        df  = pd.read_csv(path)
        col = "change_mln" if "change_mln" in df.columns else "debt_mln"
        buyers_df = df[df[col] > 0].sort_values(col, ascending=False).head(5)
        total_bln = round(float(df[col].clip(lower=0).sum()) / 1000, 1)
        streak = 0
        sig_path = DATA_DIR / "form101_signal.json"
        if sig_path.exists():
            with open(sig_path, encoding="utf-8") as f:
                streak = json.load(f).get("streak_months", 0)
        buyers = [
            {"name": str(row.get("bank_name", f"REGN {int(row['bank_id'])}")),
             "change_bln": round(float(row[col]) / 1000, 1)}
            for _, row in buyers_df.iterrows()
        ]

        why_now = ""
        try:
            from scripts.narrative import generate_banks_narrative
            narr    = generate_banks_narrative(
                streak=streak,
                total_bln=total_bln,
            )
            why_now = narr.get("why_now", "")
        except Exception as ne:
            log.warning(f"Narrative: {ne}")


        return {
            "status":      "bull" if total_bln > 0 else "neu",
            "label":       "Покупают" if total_bln > 0 else "Нейтральные",
            "arrow":       "↑" if total_bln > 0 else "→",
            "total_bln":   total_bln, "streak": streak, "buyers": buyers,
            "description": f"+₽{total_bln:.0f} млрд за месяц · стрик {streak} мес",
            "why_now":     why_now,   # ← новое поле
        }
    except Exception as e:
        log.error(f"Form 101: {e}")
        return {"status": "neu", "label": "Ошибка", "arrow": "→",
                "total_bln": 0, "streak": 0, "buyers": [],
                "description": "Ошибка чтения Form 101"}

def compute_inflation_signal(key_rate: float) -> dict:
    """
    Инфляция + реальная ставка (КС − инфляция).
    Сначала кэш (любой возраст лучше, чем ничего), затем живой источник ЦБ.
    """
    cached = read_cache("inflation_latest.json", max_age_hours=24)
    if cached and "error" not in cached and cached.get("infl_yoy"):
        return cached

    try:
        rows = get_inflation_data()
        if not rows:
            raise ValueError("ЦБ не вернул данные по инфляции")
        # инФОМ (наблюдаемая/ожидаемая) — опционально, не должно ронять сигнал
        try:
            expectations = get_inflation_expectations()
        except Exception as ee:
            log.warning(f"инФОМ: {ee}")
            expectations = None
        signal = build_inflation_signal(rows, key_rate=key_rate,
                                        expectations=expectations)
        write_cache("inflation_latest.json", {
            "generated_at": datetime.now().isoformat(), **signal
        })
        return signal
    except Exception as e:
        log.error(f"Инфляция: {e}")
        stale = read_cache("inflation_latest.json", max_age_hours=9999)
        if stale and stale.get("infl_yoy"):
            return stale
        return build_inflation_signal(None, key_rate=key_rate)

def compute_regime(curve: dict, auctions: dict, banks: dict) -> dict:
    exp_cut = curve.get("exp_cut", 0)
    btc     = auctions.get("avg_btc", 1.0)
    sm_bull = banks.get("status") == "bull"
    cycle   = 0
    try:
        dec = pd.read_csv(DATA_DIR / "cbr_decisions.csv")
        dec["decision_date"] = pd.to_datetime(dec["decision_date"])
        last3 = (dec[dec["rate_change_bps"] != 0]
                 .sort_values("decision_date").tail(3)["rate_change_bps"])
        if len(last3) >= 3 and (last3 < 0).all():
            cycle = +1
        elif len(last3) >= 3 and (last3 > 0).all():
            cycle = -1
    except Exception:
        pass

    if cycle == +1 and exp_cut > 0.5 and sm_bull:
        return {"name": "Смягчение", "color": "green", "emoji": "🟢",
                "desc": "ЦБ снижает ставку · рынок и банки подтверждают тренд"}
    elif cycle == +1 and exp_cut > 0.5:
        return {"name": "Нормализация", "color": "blue", "emoji": "🔵",
                "desc": "Цикл снижения идёт · рынок ждёт следующего шага ЦБ"}
    elif cycle == -1 or exp_cut < -0.5:
        return {"name": "Перегрев", "color": "amber", "emoji": "🟡",
                "desc": "Ставка растёт · длинные ОФЗ под давлением"}
    elif btc < 0.3 and exp_cut < 0:
        return {"name": "Паника", "color": "red", "emoji": "🔴",
                "desc": "Рынок в стрессе · аукционы проваливаются"}
    else:
        return {"name": "Нормализация", "color": "blue", "emoji": "🔵",
                "desc": "Рынок ждёт снижения КС — сигнал ещё не пришёл"}

def compute_recommendation(key_rate: float, auctions: dict, banks: dict = None) -> dict:
    pt  = auctions.get("pass_through", 0.66)
    sp  = auctions.get("supply_pressure", 0.67)
    btc = auctions.get("avg_btc", 0.49)

    # Подтверждают ли «умные деньги» (банки наращивают позицию в долговых ЦБ)
    banks = banks or {}
    sm_confirms = (banks.get("status") == "bull"
                   or (banks.get("total_bln") or 0) > 0)

    scr_path = DATA_DIR / "bond_screener.json"
    if scr_path.exists():
        try:
            with open(scr_path, encoding="utf-8") as f:
                scr = json.load(f)
            bonds = scr.get("bonds", [])
            if bonds:
                # Лучшая бумага по базовому сценарию (динамический base, fallback на legacy)
                def _base_pnl(b):
                    return b.get("pnl_base_adjusted", b.get("pnl_13_adjusted", 0))
                best = max(bonds, key=_base_pnl)
                pnl  = round(_base_pnl(best), 1)
                p11  = round(best.get("pnl_11_adjusted", 0), 1)
                flat = round(best.get("pnl_flat", 0), 1)
                pcut = round(pnl + 8, 1)
                base_label = best.get("base_scenario", "КС → 13.0%")
                return {
                    "asset":         best["shortname"],
                    "secid":         best.get("secid"),
                    "matdate":       best.get("matdate"),
                    "coupon":        best.get("coupon_pct"),
                    "ytm":           best.get("ytm"),
                    "duration":      best.get("duration"),
                    "pnl_base":      pnl,
                    "pnl_flat":      flat,
                    "base_scenario": base_label,
                    "probability":   67,
                    "win_rate":      75,
                    "win_rate_n":    3,
                    "win_rate_d":    4,
                    "pass_through":  pt,
                    "supply_pressure": sp,
                    "entry_signal":  btc >= 1.5,
                    "entry_condition": f"BTC > 1.5× · сейчас {btc:.2f}×",
                    "invalidation":  "ИПЦ > 10.5% г/г",
                    "smart_money_confirms": sm_confirms,
                    "payout": [
                        {"scenario": "КС без изменений",
                         "rub": int(100000*(1+flat/100)), "pct": flat},
                        {"scenario": base_label, "base": True,
                         "rub": int(100000*(1+pnl/100)), "pct": pnl},
                        {"scenario": "КС → 12.0%",
                         "rub": int(100000*(1+pcut/100)), "pct": pcut},
                        {"scenario": "КС → 11.0%",
                         "rub": int(100000*(1+p11/100)), "pct": p11},
                    ],
                }
        except Exception as e:
            log.error(f"Скринер: {e}")

    # Fallback
    return {
        "asset": "ОФЗ 26254", "secid": None,
        "matdate": "2040-10-03", "coupon": 13.0,
        "ytm": 14.85, "duration": 6.4,
        "pnl_base": 20.5, "pnl_flat": 14.1,
        "base_scenario": "КС → 13.0%",
        "probability": 67, "win_rate": 75, "win_rate_n": 3, "win_rate_d": 4,
        "pass_through": pt, "supply_pressure": sp,
        "entry_signal": btc >= 1.5,
        "entry_condition": f"BTC > 1.5× · сейчас {btc:.2f}×",
        "invalidation": "ИПЦ > 10.5% г/г",
        "smart_money_confirms": sm_confirms,
        "payout": [
            {"scenario": "КС без изменений", "rub": 114100, "pct": 14.1},
            {"scenario": "КС → 13.0%", "rub": 120500, "pct": 20.5, "base": True},
            {"scenario": "КС → 12.0%", "rub": 129200, "pct": 29.2},
            {"scenario": "КС → 11.0%", "rub": 138000, "pct": 38.0},
        ],
    }

# ─────────────────────────────────────────────
# ЭНДПОИНТЫ
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    idx = STATIC_DIR / "index.html"
    return FileResponse(str(idx)) if idx.exists() else Response("Mini App не найден")

@app.get("/health")
async def health():
    files = {f.name: True for f in DATA_DIR.glob("*.json")}
    files.update({f.name: True for f in DATA_DIR.glob("*.csv")})
    return json_resp({"status": "ok", "time": datetime.now().isoformat(),
                      "data": files})

@app.post("/api/refresh")
async def trigger_refresh(background_tasks: BackgroundTasks,
                          _auth: None = Depends(require_refresh_token)):
    """Запускает обновление данных через subprocess в фоне."""
    background_tasks.add_task(run_refresh)
    return json_resp({"status": "started",
                      "note": "Данные обновятся через 30-120 секунд"})

@app.post("/api/cache/clear")
async def cache_clear(_auth: None = Depends(require_refresh_token)):
    cleared = []
    for f in list(DATA_DIR.glob("api_*.json")) + [DATA_DIR / "auctions_latest.json"]:
        if f.exists():
            f.unlink()
            cleared.append(f.name)
    log.info(f"Кэш очищен: {cleared}")
    return json_resp({"cleared": cleared})

@app.get("/api/overview")
async def get_overview():
    cached = read_cache("api_overview.json", max_age_hours=1)
    if cached:
        return json_resp(cached)

    log.info("Вычисляем /api/overview...")
    key_rate  = get_key_rate() or 14.5
    curve     = compute_curve_signal(key_rate)
    auctions  = compute_auction_signal()
    banks     = compute_banks_signal()
    inflation = compute_inflation_signal(key_rate)
    regime    = compute_regime(curve, auctions, banks)
    rec       = compute_recommendation(key_rate, auctions, banks)

    entry = auctions.get("entry_signal", False)
    verdict = ("Сигнал входа пришёл" if entry
               else "Ждать сигнала входа в ОФЗ" if curve.get("status") == "bull"
               else "Рынок в ожидании")
    if entry:
        action = f"Рассмотреть покупку {rec['asset']}"
    elif inflation.get("status") == "bull" and inflation.get("real_rate", 0) >= 4:
        action = (f"Реальная ставка {inflation['real_rate']:.1f} п.п. · "
                  f"дезинфляция → ждём сигнал входа (BTC > 1.5×)")
    else:
        action = "Уведомим когда BTC > 1.5×"

    result = {
        "generated_at": datetime.now().isoformat(),
        "key_rate":     key_rate,
        "key_rate_str": f"{key_rate}%",
        "verdict":      verdict,
        "action":       action,
        "regime":       regime,
        "signals":      {"curve": curve, "auctions": auctions,
                          "banks": banks, "inflation": inflation},
        "recommendation": rec,
    }
    write_cache("api_overview.json", result)
    log.info(f"Overview: КС={key_rate}%, режим={regime['name']}, BTC={auctions.get('avg_btc','?')}×")
    return json_resp(result)

@app.get("/api/meetings")
async def get_meetings():
    cached = read_cache("cbr_probabilities.json", max_age_hours=6)
    if cached:
        return json_resp(cached)
    return json_resp({"generated_at": datetime.now().isoformat(),
                      "key_rate": 14.5, "curve_date": "—", "meetings": []})

@app.get("/api/screener")
async def get_screener():
    cached = read_cache("bond_screener.json", max_age_hours=6)
    if cached:
        return json_resp(cached)
    return json_resp({"generated_at": datetime.now().isoformat(),
                      "supply_metrics": {
                          "btc_current": 0.49, "btc_normal": 1.5,
                          "supply_pressure": 0.67, "pass_through": 0.66,
                          "overhang_active": True, "entry_signal": False,
                      }, "bonds": []})

@app.get("/api/banks")
async def get_banks():
    cached = read_cache("api_banks.json", max_age_hours=24)
    if cached:
        return json_resp(cached)
    banks = compute_banks_signal()
    write_cache("api_banks.json", banks)
    return json_resp(banks)

@app.get("/api/inflation")
async def get_inflation():
    cached = read_cache("inflation_latest.json", max_age_hours=24)
    if cached:
        return json_resp(cached)
    key_rate = get_key_rate() or 14.5
    return json_resp(compute_inflation_signal(key_rate))

@app.get("/api/digest")
async def get_digest():
    path = DATA_DIR / "digest_latest.txt"
    return {"text": path.read_text(encoding="utf-8") if path.exists()
            else "Дайджест не найден"}

