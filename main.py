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
import os
import sys
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import pandas as pd
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))

from parsers.gcurve import get_last_gcurve, get_key_rate
from parsers.minfin import (
    get_latest_file_url, download_xlsx, parse_auctions,
    build_auction_signal, enrich_auction_cache,
)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Запуск ЦБ-Радар API...")
    scheduler.add_job(
        run_refresh,
        trigger="cron",
        hour=8, minute=0,
        id="daily_refresh",
        replace_existing=True,
    )
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
    yield
    scheduler.shutdown(wait=False)
    log.info("APScheduler остановлен")


# ─────────────────────────────────────────────
# ПРИЛОЖЕНИЕ
# ─────────────────────────────────────────────

app = FastAPI(
    title="ЦБ-Радар API",
    description="Bloomberg для рублёвых облигаций",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)



class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content, ensure_ascii=False, indent=2, default=str
        ).encode("utf-8")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

CACHE_CLEAR_TOKEN = os.getenv("CACHE_CLEAR_TOKEN", "")


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


def _neutral_auction_signal() -> dict:
    return {
        "status": "neu", "label": "Нет данных", "arrow": "→",
        "avg_btc": 0.49, "long_btc": 0.45,
        "last_btc": 0.26, "last_date": "—", "last_code": "—",
        "last_yield": 0.0, "last_demand_mln": 0,
        "yield_trend": 0.0, "supply_pressure": 0.67,
        "pass_through": 0.66, "entry_signal": False,
        "description": "Данные аукционов временно недоступны",
    }


def compute_auction_signal() -> dict:
    log.debug("Вычисляем аукционный сигнал")

    cached = read_cache("auctions_latest.json", max_age_hours=12)
    if cached:
        return enrich_auction_cache(cached)

    try:
        url = get_latest_file_url()
        if url is None:
            raise ValueError("Минфин не вернул URL файла")
        xlsx = download_xlsx(url)
        df   = parse_auctions(xlsx)
        if df is None or df.empty:
            raise ValueError("Пустые данные аукционов")

        signal = build_auction_signal(df)
        write_cache("auctions_latest.json", {
            "generated_at": datetime.now().isoformat(),
            **signal,
        })
        df.to_csv(DATA_DIR / "auctions_all.csv", index=False)
        return signal
    except Exception as e:
        log.error(f"Аукционы недоступны: {e}")
        return _neutral_auction_signal()


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


def compute_regime(curve: dict, auctions: dict, banks: dict) -> dict:
    exp_cut = curve.get("exp_cut", 0)
    # Если аукционы с ошибкой — берём нейтральное значение
    btc = auctions.get("avg_btc", 1.0) if "error" not in auctions else 1.0
    sm_bull = banks.get("status") == "bull"

    # Читаем направление цикла из истории решений ЦБ
    cycle = 0
    try:
        dec = pd.read_csv(DATA_DIR / "cbr_decisions.csv")
        dec["decision_date"] = pd.to_datetime(dec["decision_date"])
        last3 = (dec[dec["rate_change_bps"] != 0]
                 .sort_values("decision_date")
                 .tail(3)["rate_change_bps"])
        if (last3 < 0).all():
            cycle = +1   # устойчивый цикл снижения
        elif (last3 > 0).all():
            cycle = -1   # устойчивый цикл повышения
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


def _load_meeting_probability() -> tuple[int, str]:
    """Вероятность снижения на ближайшем заседании из кэша."""
    path = DATA_DIR / "cbr_probabilities.json"
    if not path.exists():
        return 50, "—"
    try:
        with open(path, encoding="utf-8") as f:
            meetings = json.load(f).get("meetings", [])
        if not meetings:
            return 50, "—"
        m = meetings[0]
        dt = m.get("date", "")[:10]
        return int(m.get("prob_cut", 50)), dt
    except Exception:
        return 50, "—"


def _load_invalidation() -> str:
    path = DATA_DIR / "hypotheses.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                for h in json.load(f).get("hypotheses", []):
                    if h.get("status") == "open" and h.get("invalidation"):
                        inv = h["invalidation"]
                        if h.get("invalidation_date"):
                            inv += f" → {h['invalidation_date']}"
                        return inv
        except Exception:
            pass
    return "ИПЦ существенно выше прогноза ЦБ"


def _best_bond(bonds: list) -> dict:
    return max(
        bonds,
        key=lambda b: b.get(
            "pnl_base_adjusted",
            b.get("pnl_13_adjusted", 0),
        ),
    )


def _build_payout(best: dict, rate_scenarios: list | None) -> list:
    """Сценарии выплат из bond + rate_scenarios."""
    flat_pct = best.get("pnl_flat", 0)
    rows = [{
        "scenario": "КС без изменений",
        "rub": round(100000 * (1 + flat_pct / 100)),
        "pct": flat_pct,
        "base": False,
    }]
    cuts = [s for s in (rate_scenarios or []) if s.get("id") != "flat"]
    pnl_map = {
        "cut_50":  best.get("pnl_base_adjusted", best.get("pnl_13_adjusted", 0)),
        "cut_100": best.get("pnl_mid_adjusted", 0),
        "cut_150": best.get("pnl_deep_adjusted", best.get("pnl_11_adjusted", 0)),
    }
    for i, sc in enumerate(cuts):
        pct = pnl_map.get(sc["id"], 0)
        rows.append({
            "scenario": sc["label"],
            "rub": round(100000 * (1 + pct / 100)),
            "pct": pct,
            "base": i == 0,
        })
    if len(rows) == 1:
        base = best.get("pnl_base_adjusted", best.get("pnl_13_adjusted", 0))
        rows.append({
            "scenario": best.get("base_scenario", "Базовый сценарий"),
            "rub": round(100000 * (1 + base / 100)),
            "pct": base,
            "base": True,
        })
    return rows


def _build_why_text(curve: dict, auctions: dict, banks: dict, entry: bool) -> str:
    parts = []
    if curve.get("status") == "bull":
        parts.append(
            f"G-кривая: рынок ждёт снижения КС до ~{curve.get('min_yield', '—')}%"
        )
    if banks.get("total_bln", 0) > 0:
        parts.append(
            f"банки нарастили позиции на ₽{banks['total_bln']:.0f} млрд"
        )
    btc = auctions.get("avg_btc", 0)
    if entry:
        parts.append(f"аукционы подтверждают спрос (BTC {btc:.2f}×)")
    else:
        parts.append(
            f"на аукционах спрос слабый (BTC {btc:.2f}×) — ждём BTC > 1.5×"
        )
    return ". ".join(parts).capitalize() + "."


def compute_recommendation(
    key_rate: float,
    auctions: dict,
    banks: dict,
    curve: dict | None = None,
) -> dict:
    scr_path = DATA_DIR / "bond_screener.json"
    pass_t   = auctions.get("pass_through", 0.66)
    sp       = auctions.get("supply_pressure", 0.67)
    entry    = auctions.get("entry_signal", False)
    prob, meet_dt = _load_meeting_probability()
    invalidation = _load_invalidation()

    if scr_path.exists():
        with open(scr_path, encoding="utf-8") as f:
            scr = json.load(f)
        bonds = scr.get("bonds", [])
        rate_scenarios = scr.get("rate_scenarios")
        if bonds:
            best = _best_bond(bonds)
            pnl  = best.get("pnl_base_adjusted", best.get("pnl_13_adjusted", 0))
            base_label = best.get("base_scenario", f"КС → {key_rate - 0.5:.1f}%")
            return {
                "asset":           best["shortname"],
                "secid":           best["secid"],
                "matdate":         best["matdate"],
                "coupon":          best.get("coupon_pct"),
                "ytm":             best.get("ytm"),
                "duration":        best.get("duration"),
                "pnl_base":        pnl,
                "pnl_flat":        best.get("pnl_flat", 0),
                "probability":     prob,
                "probability_note": f"рынок на заседание {meet_dt}",
                "win_rate":        prob,
                "win_rate_n":      prob,
                "win_rate_d":      100,
                "pass_through":    pass_t,
                "supply_pressure": sp,
                "entry_signal":    entry,
                "entry_condition": f"BTC > 1.5× · сейчас {auctions.get('avg_btc', 0):.2f}×",
                "invalidation":    invalidation,
                "base_scenario":   base_label,
                "why_text":        _build_why_text(curve or {}, auctions, banks, entry),
                "payout":          _build_payout(best, rate_scenarios),
            }

    base_target = round((key_rate - 0.5) * 2) / 2
    return {
        "asset": "—", "secid": None, "matdate": None,
        "coupon": None, "ytm": None, "duration": None,
        "pnl_base": 0, "pnl_flat": 0,
        "probability": prob,
        "probability_note": f"рынок на заседание {meet_dt}",
        "win_rate": prob, "win_rate_n": prob, "win_rate_d": 100,
        "pass_through": pass_t, "supply_pressure": sp, "entry_signal": entry,
        "entry_condition": "BTC > 1.5× на аукционе",
        "invalidation": invalidation,
        "base_scenario": f"КС → {base_target:.1f}%",
        "why_text": "Данные скринера загружаются — запустите refresh_data.py",
        "payout": [],
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
        return UTF8JSONResponse(cached)

    log.info("Вычисляем /api/overview...")
    key_rate = get_key_rate() or 14.5
    curve    = compute_curve_signal(key_rate)
    auctions = compute_auction_signal()
    banks    = compute_banks_signal()
    regime   = compute_regime(curve, auctions, banks)
    rec      = compute_recommendation(key_rate, auctions, banks, curve)

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
    return UTF8JSONResponse(result, media_type="application/json; charset=utf-8")


@app.get("/api/meetings", response_model=MeetingsResponse)
async def get_meetings():
    cached = read_cache("cbr_probabilities.json", max_age_hours=6)
    if cached:
        return UTF8JSONResponse(cached)
    log.warning("/api/meetings: кэш не найден — запусти scripts/refresh_data.py")
    return UTF8JSONResponse({"generated_at": datetime.now().isoformat(),
                         "key_rate": 14.5, "curve_date": "—", "meetings": []})


@app.get("/api/screener", response_model=ScreenerResponse)
async def get_screener():
    cached = read_cache("bond_screener.json", max_age_hours=6)
    if cached:
        return UTF8JSONResponse(cached)
    log.warning("/api/screener: кэш не найден — запусти scripts/refresh_data.py")
    return UTF8JSONResponse({"generated_at": datetime.now().isoformat(),
                         "supply_metrics": {}, "bonds": []})


@app.get("/api/banks")
async def get_banks():
    cached = read_cache("api_banks.json", max_age_hours=24)
    if cached:
        return UTF8JSONResponse(cached)
    banks = compute_banks_signal()
    write_cache("api_banks.json", banks)
    return UTF8JSONResponse(banks)


@app.get("/api/digest")
async def get_digest():
    path = DATA_DIR / "digest_latest.txt"
    if path.exists():
        return {"text": path.read_text(encoding="utf-8")}
    log.warning("/api/digest: файл не найден")
    return {"text": "Дайджест не найден. Запусти python digest.py"}


@app.post("/api/cache/clear")
async def clear_cache(x_cache_token: str = Header(default="", alias="X-Cache-Token")):
    if CACHE_CLEAR_TOKEN and x_cache_token != CACHE_CLEAR_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    for f in DATA_DIR.glob("api_*.json"):
        f.unlink()
    log.info("Кэш очищен")
    return {"cleared": True}