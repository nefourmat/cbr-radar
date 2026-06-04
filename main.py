"""
main.py — FastAPI бэкенд ЦБ-Радар

Включает:
  - Pydantic response models (валидация + Swagger /docs)
  - Структурированное логирование (logging, не print)
  - APScheduler: refresh_data.py каждый день 08:00 + среда 13:30
  - CORS middleware
  - Кэширование JSON файлов

Запуск: uvicorn main:app --reload --port 8000
"""

import json
import logging
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))

from parsers.gcurve import get_last_gcurve, get_key_rate
from parsers.minfin import get_latest_file_url, download_xlsx, parse_auctions

# ─────────────────────────────────────────────
# ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cbr_radar.api")


# ─────────────────────────────────────────────
# PYDANTIC МОДЕЛИ
# ─────────────────────────────────────────────

class RegimeModel(BaseModel):
    name:  str = Field(..., description="Нормализация | Смягчение | Перегрев | Паника")
    color: str = Field(..., description="green | amber | red | blue")
    emoji: str
    desc:  str

class CurveSignal(BaseModel):
    status:      str
    label:       str
    arrow:       str
    exp_cut:     float = Field(..., description="Ожидаемое снижение КС в %")
    y1:          float
    y2:          float
    y10:         float
    slope_2_10:  float
    min_yield:   float
    date:        str
    description: str

class AuctionSignal(BaseModel):
    status:          str
    label:           str
    arrow:           str
    avg_btc:         float
    long_btc:        float
    last_btc:        float
    last_date:       str
    last_code:       str
    last_yield:      float
    last_demand_mln: int
    yield_trend:     float
    supply_pressure: float
    pass_through:    float
    entry_signal:    bool
    description:     str

class BankBuyer(BaseModel):
    name:        str
    change_bln:  float

class BanksSignal(BaseModel):
    status:     str
    label:      str
    arrow:      str
    total_bln:  float
    streak:     int
    buyers:     List[BankBuyer]
    description: str

class Signals(BaseModel):
    curve:    CurveSignal
    auctions: AuctionSignal
    banks:    BanksSignal

class PayoutScenario(BaseModel):
    scenario: str
    rub:      int
    pct:      float
    base:     bool = False

class Recommendation(BaseModel):
    asset:         str
    secid:         Optional[str]
    matdate:       Optional[str]
    coupon:        Optional[float]
    ytm:           Optional[float]
    duration:      Optional[float]
    pnl_base:      float
    pnl_flat:      float
    probability:   int
    win_rate:      int
    win_rate_n:    int = 3
    win_rate_d:    int = 4
    pass_through:  float
    supply_pressure: float
    entry_signal:  bool
    entry_condition: str
    invalidation:  str
    payout:        List[PayoutScenario]

class OverviewResponse(BaseModel):
    generated_at:    str
    key_rate:        float
    key_rate_str:    str
    verdict:         str
    action:          str
    regime:          RegimeModel
    signals:         Signals
    recommendation:  Recommendation

class MeetingScenarios(BaseModel):
    hold:    int
    cut_50:  int
    cut_100: int

class MeetingModel(BaseModel):
    date:             str
    type:             str
    days_ahead:       int
    implied_ks:       float
    meeting_cut_bps:  float
    prob_cut:         int
    scenarios:        MeetingScenarios

class MeetingsResponse(BaseModel):
    generated_at: str
    key_rate:     float
    curve_date:   str
    meetings:     List[MeetingModel]

class BondModel(BaseModel):
    secid:              str
    shortname:          str
    matdate:            str
    duration:           float
    price_pct:          float
    coupon_pct:         float
    ytm:                float
    pnl_13_theoretical: float
    pnl_13_adjusted:    float
    pnl_11_adjusted:    float
    pnl_flat:           float

class SupplyMetrics(BaseModel):
    btc_current:     float
    btc_normal:      float
    supply_pressure: float
    pass_through:    float
    overhang_active: bool
    entry_signal:    bool

class ScreenerResponse(BaseModel):
    generated_at:   str
    supply_metrics: SupplyMetrics
    bonds:          List[BondModel]

class HealthResponse(BaseModel):
    status: str
    time:   str


# ─────────────────────────────────────────────
# ПРИЛОЖЕНИЕ
# ─────────────────────────────────────────────

app = FastAPI(
    title="ЦБ-Радар API",
    description="Bloomberg для рублёвых облигаций",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────
# КЭШИРОВАНИЕ
# ─────────────────────────────────────────────

def read_cache(filename: str, max_age_hours: float = 24) -> Optional[dict]:
    path = DATA_DIR / filename
    if not path.exists():
        return None
    age_h = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
    if age_h > max_age_hours:
        log.debug(f"Кэш {filename} устарел ({age_h:.1f}ч > {max_age_hours}ч)")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_cache(filename: str, data: dict):
    with open(DATA_DIR / filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    log.debug(f"Кэш записан: {filename}")


# ─────────────────────────────────────────────
# APSCHEDULER — автообновление данных
# ─────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="Europe/Moscow")


def run_refresh():
    """Запускаем refresh_data.py в отдельном процессе."""
    log.info("Scheduler: запуск refresh_data.py...")
    try:
        result = subprocess.run(
            [sys.executable, "scripts/refresh_data.py"],
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
        )
        if result.returncode == 0:
            log.info("Scheduler: refresh_data.py завершён успешно")
        else:
            log.error(
                f"Scheduler: refresh_data.py вернул код {result.returncode}\n"
                f"{result.stderr[:500]}"
            )
    except subprocess.TimeoutExpired:
        log.error("Scheduler: refresh_data.py превысил таймаут 300с")
    except Exception as e:
        log.error(f"Scheduler: ошибка запуска refresh_data.py: {e}")


@app.on_event("startup")
def startup_event():
    log.info("Запуск ЦБ-Радар API...")

    # Ежедневное обновление 08:00 МСК
    scheduler.add_job(
        run_refresh,
        trigger="cron",
        hour=8, minute=0,
        id="daily_refresh",
        replace_existing=True,
    )

    # Среда 13:30 МСК — после окончания аукционов
    scheduler.add_job(
        run_refresh,
        trigger="cron",
        day_of_week="wed",
        hour=13, minute=30,
        id="auction_refresh",
        replace_existing=True,
    )

    scheduler.start()
    log.info("APScheduler запущен: обновление в 08:00 и Ср 13:30 МСК")


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    log.info("APScheduler остановлен")


# ─────────────────────────────────────────────
# СИГНАЛЫ (вычисление)
# ─────────────────────────────────────────────

def compute_curve_signal(key_rate: float) -> dict:
    log.debug("Вычисляем сигнал G-кривой")
    df, date_str = get_last_gcurve()
    if df is None:
        log.warning("G-кривая недоступна")
        return {}

    min_yield = df["доходность_пct"].min()
    min_срок  = float(df.loc[df["доходность_пct"].idxmin(), "срок_лет"])

    def _y(срок):
        row = df[df["срок_лет"] == срок]["доходность_пct"]
        return float(row.values[0]) if not row.empty else 0.0

    y1 = _y(1.0); y2 = _y(2.0); y10 = _y(10.0)
    exp_cut = round(key_rate - min_yield, 2)
    status  = "bull" if exp_cut > 0.5 else "neu"

    return {
        "status":      status,
        "label":       "Бычья" if status == "bull" else "Нейтральная",
        "arrow":       "↑" if status == "bull" else "→",
        "exp_cut":     exp_cut,
        "y1":          round(y1, 2),
        "y2":          round(y2, 2),
        "y10":         round(y10, 2),
        "slope_2_10":  round(y10 - y2, 2),
        "min_yield":   round(float(min_yield), 2),
        "min_срок":    min_срок,
        "date":        date_str,
        "description": f"1Y = {y1:.2f}% при КС {key_rate}% · ожид. снижение до ~{min_yield:.1f}%",
    }


def compute_auction_signal() -> dict:
    log.debug("Вычисляем аукционный сигнал")

    # Пробуем свежий кэш
    cached = read_cache("auctions_latest.json", max_age_hours=12)
    if cached:
        return cached

    try:
        url  = get_latest_file_url()
        xlsx = download_xlsx(url)
        df   = parse_auctions(xlsx)
        if df is None or df.empty:
            return {}

        cutoff = df["дата"].max() - pd.Timedelta(weeks=4)
        recent = df[df["дата"] >= cutoff].copy()

        avg_btc  = float(recent["bid_to_cover"].mean())
        long_df  = recent[recent["лет_до_погашения"] > 7]
        long_btc = float(long_df["bid_to_cover"].mean()) if not long_df.empty else avg_btc
        y_trend  = float(
            recent["доходность_пct"].iloc[0] - recent["доходность_пct"].iloc[-1]
        )

        sp    = max(0.0, min(1.0, 1 - avg_btc / 1.5))
        pt    = round(1.0 - sp * 0.5, 2)
        last  = recent.iloc[0]
        status = "bear" if avg_btc < 1.0 else "bull"

        result = {
            "status":          status,
            "label":           "Слабые" if status == "bear" else "Сильные",
            "arrow":           "↓" if status == "bear" else "↑",
            "avg_btc":         round(avg_btc, 2),
            "long_btc":        round(long_btc, 2),
            "last_btc":        round(float(last["bid_to_cover"]), 2),
            "last_date":       last["дата"].strftime("%d.%m.%Y"),
            "last_code":       last["код_выпуска"],
            "last_yield":      float(last["доходность_пct"]),
            "last_demand_mln": round(float(last["спрос_млн"])),
            "yield_trend":     round(y_trend, 2),
            "supply_pressure": round(sp, 2),
            "pass_through":    pt,
            "entry_signal":    avg_btc >= 1.5,
            "description":     (
                f"BTC {avg_btc:.2f}× · pass-through {pt:.0%} · "
                f"{'⚡ сигнал входа!' if avg_btc >= 1.5 else 'ждём BTC > 1.5×'}"
            ),
        }
        write_cache("auctions_latest.json", result)
        return result

    except Exception as e:
        log.error(f"Ошибка аукционного сигнала: {e}")
        return {}


def compute_banks_signal() -> dict:
    log.debug("Читаем сигнал банков (Form 101)")
    path = DATA_DIR / "form101_latest.csv"
    if not path.exists():
        log.warning("form101_latest.csv не найден")
        return {"status": "neu", "label": "Нет данных", "arrow": "→",
                "total_bln": 0, "streak": 0, "buyers": [], "description": "—"}

    df  = pd.read_csv(path)
    col = "change_mln" if "change_mln" in df.columns else "debt_mln"
    buyers_df = (
        df[df[col] > 0].sort_values(col, ascending=False).head(5)
        if col in df.columns else df.head(5)
    )
    total_bln = round(float(df[col].clip(lower=0).sum()) / 1000, 1) if col in df.columns else 0

    streak = 0
    sig_path = DATA_DIR / "form101_signal.json"
    if sig_path.exists():
        with open(sig_path, encoding="utf-8") as f:
            streak = json.load(f).get("streak_months", 0)

    buyers = [
        {
            "name":       str(row.get("bank_name", f"REGN {int(row['bank_id'])}")),
            "change_bln": round(float(row[col]) / 1000, 1) if col in row else 0,
        }
        for _, row in buyers_df.iterrows()
    ]

    return {
        "status":      "bull" if total_bln > 0 else "neu",
        "label":       "Покупают" if total_bln > 0 else "Нейтральные",
        "arrow":       "↑" if total_bln > 0 else "→",
        "total_bln":   total_bln,
        "streak":      streak,
        "buyers":      buyers,
        "description": f"+₽{total_bln:.0f} млрд за месяц · стрик {streak} мес",
    }


def compute_regime(curve: dict, auctions: dict) -> dict:
    exp_cut = curve.get("exp_cut", 0)
    btc     = auctions.get("avg_btc", 1)

    if exp_cut > 1.5 and btc >= 1.2:
        return {"name": "Смягчение",    "color": "green",
                "emoji": "🟢", "desc": "Рынок готовится к снижению КС и активно покупает"}
    elif exp_cut > 0.5 and btc < 1.0:
        return {"name": "Нормализация", "color": "blue",
                "emoji": "🔵", "desc": "Рынок ждёт снижения КС — сигнал ещё не пришёл"}
    elif exp_cut < 0 and btc > 1.5:
        return {"name": "Перегрев",     "color": "amber",
                "emoji": "🟡", "desc": "Рынок переоценён — повышенный риск коррекции"}
    else:
        return {"name": "Паника",       "color": "red",
                "emoji": "🔴", "desc": "Высокая неопределённость — защитная позиция"}


def compute_recommendation(key_rate: float, auctions: dict, banks: dict) -> dict:
    scr_path = DATA_DIR / "bond_screener.json"
    pass_t   = auctions.get("pass_through", 0.66)
    sp       = auctions.get("supply_pressure", 0.67)
    entry    = auctions.get("entry_signal", False)

    if scr_path.exists():
        with open(scr_path, encoding="utf-8") as f:
            scr = json.load(f)
        bonds = scr.get("bonds", [])
        if bonds:
            best = bonds[0]
            pnl  = best.get("pnl_13_adjusted", 0)
            return {
                "asset":         best["shortname"],
                "secid":         best["secid"],
                "matdate":       best["matdate"],
                "coupon":        best.get("coupon_pct"),
                "ytm":           best.get("ytm"),
                "duration":      best.get("duration"),
                "pnl_base":      pnl,
                "pnl_flat":      best.get("pnl_flat", 0),
                "probability":   67,
                "win_rate":      75,
                "win_rate_n":    3,
                "win_rate_d":    4,
                "pass_through":  pass_t,
                "supply_pressure": sp,
                "entry_signal":  entry,
                "entry_condition": f"BTC > 1.5× · сейчас {auctions.get('avg_btc', 0):.2f}×",
                "invalidation":  "ИПЦ за май > 10.5% г/г",
                "payout": [
                    {"scenario": "КС без изменений",
                     "rub": round(100000 * (1 + best["pnl_flat"]/100)),
                     "pct": best["pnl_flat"], "base": False},
                    {"scenario": "КС → 13.0%", "base": True,
                     "rub": round(100000 * (1 + pnl/100)), "pct": pnl},
                    {"scenario": "КС → 12.0%",
                     "rub": round(100000 * (1 + best.get("pnl_cut250", pnl+5)/100)),
                     "pct": round(best.get("pnl_cut250", pnl+5), 1), "base": False},
                    {"scenario": "КС → 11.0%",
                     "rub": round(100000 * (1 + best["pnl_11_adjusted"]/100)),
                     "pct": best["pnl_11_adjusted"], "base": False},
                ],
            }

    # Fallback
    return {
        "asset": "ОФЗ 26254", "secid": None, "matdate": "2040-10-03",
        "coupon": 13.0, "ytm": 14.85, "duration": 6.4,
        "pnl_base": 20.5, "pnl_flat": 14.1,
        "probability": 67, "win_rate": 75, "win_rate_n": 3, "win_rate_d": 4,
        "pass_through": pass_t, "supply_pressure": sp, "entry_signal": entry,
        "entry_condition": "BTC > 1.5× на аукционе",
        "invalidation": "ИПЦ > 10.5% г/г",
        "payout": [
            {"scenario": "КС без изменений", "rub": 114100, "pct": 14.1, "base": False},
            {"scenario": "КС → 13.0%",       "rub": 120500, "pct": 20.5, "base": True},
            {"scenario": "КС → 12.0%",       "rub": 129200, "pct": 29.2, "base": False},
            {"scenario": "КС → 11.0%",       "rub": 138000, "pct": 38.0, "base": False},
        ],
    }


# ─────────────────────────────────────────────
# ЭНДПОИНТЫ
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/health", response_model=HealthResponse)
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/api/overview", response_model=OverviewResponse)
async def get_overview():
    cached = read_cache("api_overview.json", max_age_hours=1)
    if cached:
        log.debug("Возвращаем кэш /api/overview")
        return JSONResponse(cached)

    log.info("Вычисляем /api/overview...")
    key_rate = get_key_rate() or 14.5
    curve    = compute_curve_signal(key_rate)
    auctions = compute_auction_signal()
    banks    = compute_banks_signal()
    regime   = compute_regime(curve, auctions)
    rec      = compute_recommendation(key_rate, auctions, banks)

    entry = auctions.get("entry_signal", False)
    if entry:
        verdict = "Сигнал входа пришёл"
        action  = f"Рассмотреть покупку {rec['asset']}"
    elif curve.get("status") == "bull":
        verdict = "Ждать сигнала входа в ОФЗ"
        action  = "Уведомим когда BTC > 1.5×"
    else:
        verdict = "Рынок в ожидании"
        action  = "Держать текущую позицию"

    result = {
        "generated_at": datetime.now().isoformat(),
        "key_rate":     key_rate,
        "key_rate_str": f"{key_rate}%",
        "verdict":      verdict,
        "action":       action,
        "regime":       regime,
        "signals":      {"curve": curve, "auctions": auctions, "banks": banks},
        "recommendation": rec,
    }

    write_cache("api_overview.json", result)
    log.info(f"Overview: КС={key_rate}%, режим={regime['name']}, "
             f"BTC={auctions.get('avg_btc','?')}×")
    return JSONResponse(result)


@app.get("/api/meetings", response_model=MeetingsResponse)
async def get_meetings():
    cached = read_cache("cbr_probabilities.json", max_age_hours=6)
    if cached:
        return JSONResponse(cached)
    log.warning("/api/meetings: кэш не найден — запусти scripts/refresh_data.py")
    return JSONResponse({"generated_at": datetime.now().isoformat(),
                         "key_rate": 14.5, "curve_date": "—", "meetings": []})


@app.get("/api/screener", response_model=ScreenerResponse)
async def get_screener():
    cached = read_cache("bond_screener.json", max_age_hours=6)
    if cached:
        return JSONResponse(cached)
    log.warning("/api/screener: кэш не найден — запусти scripts/refresh_data.py")
    return JSONResponse({"generated_at": datetime.now().isoformat(),
                         "supply_metrics": {}, "bonds": []})


@app.get("/api/banks")
async def get_banks():
    cached = read_cache("api_banks.json", max_age_hours=24)
    if cached:
        return JSONResponse(cached)
    banks = compute_banks_signal()
    write_cache("api_banks.json", banks)
    return JSONResponse(banks)


@app.get("/api/digest")
async def get_digest():
    path = DATA_DIR / "digest_latest.txt"
    if path.exists():
        return {"text": path.read_text(encoding="utf-8")}
    log.warning("/api/digest: файл не найден")
    return {"text": "Дайджест не найден. Запусти python digest.py"}