"""
main.py — FastAPI бэкенд ЦБ-Радар

Эндпоинты:
  GET /              → Mini App HTML
  GET /api/overview  → все сигналы для главного экрана
  GET /api/meetings  → вероятности заседаний ЦБ
  GET /api/screener  → скринер ОФЗ
  GET /api/banks     → умные деньги (Form 101)
  GET /api/digest    → текстовый дайджест

Запуск локально:
  uvicorn main:app --reload --port 8000

Deploy Railway:
  railway up
"""

import json
import sys
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, date
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).parent))

from parsers.gcurve import get_last_gcurve, get_key_rate
from parsers.minfin import get_latest_file_url, download_xlsx, parse_auctions

DATA_DIR   = Path("data")
STATIC_DIR = Path("static")

app = FastAPI(title="ЦБ-Радар API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Отдаём Mini App
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")


# ─────────────────────────────────────────────
# КЭШИРОВАНИЕ
# ─────────────────────────────────────────────

def read_cache(filename: str, max_age_hours: float = 24):
    """Читаем кэш если он свежий."""
    path = DATA_DIR / filename
    if not path.exists():
        return None
    age_h = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
    if age_h > max_age_hours:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_cache(filename: str, data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ─────────────────────────────────────────────
# СИГНАЛЫ
# ─────────────────────────────────────────────

def compute_curve_signal(key_rate: float) -> dict:
    """G-кривая → сигнал."""
    df, date_str = get_last_gcurve()
    if df is None:
        return {}

    min_yield = df["доходность_пct"].min()
    min_срок  = float(df.loc[df["доходность_пct"].idxmin(), "срок_лет"])
    y1  = float(df.loc[df["срок_лет"] == 1.0,  "доходность_пct"].values[0])
    y2  = float(df.loc[df["срок_лет"] == 2.0,  "доходность_пct"].values[0])
    y10 = float(df.loc[df["срок_лет"] == 10.0, "доходность_пct"].values[0])

    exp_cut = round(key_rate - min_yield, 2)
    slope   = round(y10 - y2, 2)

    status = "bull" if exp_cut > 0.5 else "neu"

    return {
        "status":      status,
        "label":       "Бычья" if status == "bull" else "Нейтральная",
        "arrow":       "↑" if status == "bull" else "→",
        "exp_cut":     exp_cut,
        "y1":          round(y1, 2),
        "y2":          round(y2, 2),
        "y10":         round(y10, 2),
        "slope_2_10":  slope,
        "min_yield":   round(float(min_yield), 2),
        "min_срок":    min_срок,
        "date":        date_str,
        "description": f"1Y ОФЗ = {y1:.2f}% при КС {key_rate}% · рынок ждёт снижения до ~{min_yield:.1f}%",
    }


def compute_auction_signal() -> dict:
    """Аукционы Минфина → сигнал."""
    try:
        url      = get_latest_file_url()
        file_obj = download_xlsx(url)
        df       = parse_auctions(file_obj)

        cutoff = df["дата"].max() - pd.Timedelta(weeks=4)
        recent = df[df["дата"] >= cutoff].copy()
        if recent.empty:
            return {}

        last     = recent.iloc[0]
        avg_btc  = float(recent["bid_to_cover"].mean())
        long_df  = recent[recent["лет_до_погашения"] > 7]
        long_btc = float(long_df["bid_to_cover"].mean()) if not long_df.empty else avg_btc
        yield_tr = float(recent["доходность_пct"].iloc[0]
                         - recent["доходность_пct"].iloc[-1])

        норма    = 1.5
        ratio    = avg_btc / норма
        supply_p = max(0, min(1, 1 - ratio))
        pass_thr = round(1 - supply_p * 0.5, 2)

        status = "bear" if avg_btc < 1.0 else "bull"

        return {
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
            "yield_trend":     round(yield_tr, 2),
            "supply_pressure": round(supply_p, 2),
            "pass_through":    pass_thr,
            "entry_signal":    avg_btc >= 1.5,
            "description": (
                f"BTC {avg_btc:.2f}× при норме 1.5× · "
                f"pass-through {pass_thr:.0%} · "
                f"{'сигнал входа пришёл ✓' if avg_btc >= 1.5 else 'сигнал входа ещё не пришёл'}"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def compute_banks_signal() -> dict:
    """Form 101 → умные деньги."""
    path = DATA_DIR / "form101_latest.csv"
    if not path.exists():
        return {"buyers": [], "streak": 0}

    df = pd.read_csv(path)
    if df.empty:
        return {"buyers": [], "streak": 0}

    col = "change_mln" if "change_mln" in df.columns else "debt_mln"
    buyers = (
        df[df[col] > 0]
        .sort_values(col, ascending=False)
        .head(5)
    )

    total_change = float(df[col].clip(lower=0).sum()) / 1000  # млрд

    # Стрик из сигнала
    streak = 0
    sig_path = DATA_DIR / "form101_signal.json"
    if sig_path.exists():
        with open(sig_path, encoding="utf-8") as f:
            sig = json.load(f)
        streak = sig.get("streak_months", 0)

    return {
        "status":       "bull" if total_change > 0 else "neu",
        "label":        "Покупают" if total_change > 0 else "Нейтральные",
        "arrow":        "↑" if total_change > 0 else "→",
        "total_bln":    round(total_change, 1),
        "streak":       streak,
        "buyers": [
            {
                "name":   row.get("bank_name", f"REGN {int(row['bank_id'])}"),
                "change_bln": round(float(row[col]) / 1000, 1),
            }
            for _, row in buyers.iterrows()
        ],
        "description": (
            f"+₽{total_change:.0f} млрд за месяц · "
            f"стрик {streak} мес"
        ),
    }


def compute_regime(curve: dict, auctions: dict, banks: dict) -> dict:
    """
    Режим рынка из трёх сигналов.
    Нормализация / Смягчение / Перегрев / Паника
    """
    exp_cut  = curve.get("exp_cut", 0)
    btc      = auctions.get("avg_btc", 1)
    sm_bull  = banks.get("status") == "bull"

    if exp_cut > 1.5 and btc >= 1.2:
        return {"name": "Смягчение",    "color": "green",  "emoji": "🟢",
                "desc": "Рынок готовится к снижению КС и активно покупает"}
    elif exp_cut > 0.5 and btc < 1.0:
        return {"name": "Нормализация", "color": "blue",   "emoji": "🔵",
                "desc": "Рынок ждёт снижения КС — сигнал ещё не пришёл"}
    elif exp_cut < 0 and btc > 1.5:
        return {"name": "Перегрев",     "color": "amber",  "emoji": "🟡",
                "desc": "Рынок переоценён — повышенный риск коррекции"}
    else:
        return {"name": "Паника",       "color": "red",    "emoji": "🔴",
                "desc": "Высокая неопределённость — защитная позиция"}


def compute_recommendation(key_rate: float, curve: dict,
                            auctions: dict, banks: dict) -> dict:
    """Конкретная инвестиционная рекомендация."""
    # Загружаем скринер если есть
    scr_path = DATA_DIR / "bond_screener.json"
    if scr_path.exists():
        with open(scr_path, encoding="utf-8") as f:
            scr = json.load(f)
        bonds  = scr.get("bonds", [])
        supply = scr.get("supply_metrics", {})
        pass_t = supply.get("pass_through", 0.7)
        if bonds:
            best = bonds[0]
            return {
                "asset":       best["shortname"],
                "secid":       best["secid"],
                "matdate":     best["matdate"],
                "coupon":      best.get("coupon_pct", 0),
                "ytm":         best.get("ytm", 0),
                "duration":    best.get("duration", 0),
                "pnl_base":    best.get("pnl_13_adjusted", 0),
                "pnl_optimistic": best.get("pnl_11_adjusted", 0),
                "pnl_flat":    best.get("pnl_flat", 0),
                "probability": 67,
                "win_rate":    68,
                "pass_through": pass_t,
                "supply_pressure": supply.get("supply_pressure", 0.67),
                "entry_signal": auctions.get("entry_signal", False),
                "entry_condition": "BTC > 1.5× на аукционе длинных ОФЗ",
                "invalidation": "ИПЦ май > 10.5% г/г → 05.06.2026",
                "payout": [
                    {"scenario": "КС без изменений",
                     "rub": round(100000 * (1 + best["pnl_flat"]/100)),
                     "pct": best["pnl_flat"]},
                    {"scenario": "КС → 13.0%", "base": True,
                     "rub": round(100000 * (1 + best["pnl_13_adjusted"]/100)),
                     "pct": best["pnl_13_adjusted"]},
                    {"scenario": "КС → 12.0%",
                     "rub": round(100000 * (1 + best.get("pnl_cut250",26)/100)),
                     "pct": round(best.get("pnl_cut250", 26), 1)},
                    {"scenario": "КС → 11.0%",
                     "rub": round(100000 * (1 + best["pnl_11_adjusted"]/100)),
                     "pct": best["pnl_11_adjusted"]},
                ],
            }

    # Fallback — фиксированные значения
    return {
        "asset": "ОФЗ 26254", "secid": "SU26254RMFS0",
        "pnl_base": 20.5, "pnl_flat": 14.1, "probability": 67,
        "win_rate": 68,
        "payout": [
            {"scenario":"КС без изменений","rub":114100,"pct":14.1},
            {"scenario":"КС → 13%","rub":120500,"pct":20.5,"base":True},
            {"scenario":"КС → 12%","rub":129200,"pct":29.2},
            {"scenario":"КС → 11%","rub":138000,"pct":38.0},
        ],
    }


# ─────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/api/overview")
async def get_overview():
    """Все сигналы для главного экрана. Кэш 1 час."""
    cached = read_cache("api_overview.json", max_age_hours=1)
    if cached:
        return JSONResponse(cached)

    key_rate = get_key_rate() or 14.5
    curve    = compute_curve_signal(key_rate)
    auctions = compute_auction_signal()
    banks    = compute_banks_signal()
    regime   = compute_regime(curve, auctions, banks)
    rec      = compute_recommendation(key_rate, curve, auctions, banks)

    # Вердикт
    if auctions.get("entry_signal"):
        verdict = "Сигнал входа пришёл"
        action  = f"Покупать {rec['asset']} по рынку"
    elif curve.get("status") == "bull" and not auctions.get("entry_signal"):
        verdict = "Ждать сигнала входа в ОФЗ"
        action  = f"Уведомим когда BTC > 1.5×"
    else:
        verdict = "Рынок в ожидании"
        action  = "Держать текущую позицию"

    result = {
        "generated_at": datetime.now().isoformat(),
        "key_rate":     key_rate,
        "verdict":      verdict,
        "action":       action,
        "regime":       regime,
        "signals": {
            "curve":    curve,
            "auctions": auctions,
            "banks":    banks,
        },
        "recommendation": rec,
    }

    write_cache("api_overview.json", result)
    return JSONResponse(result)


@app.get("/api/meetings")
async def get_meetings():
    """Вероятности заседаний ЦБ. Кэш 6 часов."""
    cached = read_cache("cbr_probabilities.json", max_age_hours=6)
    if cached:
        return JSONResponse(cached)
    return JSONResponse({"meetings": [], "error": "Запустите scripts/cbr_probabilities.py"})


@app.get("/api/screener")
async def get_screener():
    """Скринер ОФЗ. Кэш 6 часов."""
    cached = read_cache("bond_screener.json", max_age_hours=6)
    if cached:
        return JSONResponse(cached)
    return JSONResponse({"bonds": [], "error": "Запустите scripts/bond_screener.py"})


@app.get("/api/banks")
async def get_banks():
    """Умные деньги. Кэш 24 часа."""
    cached = read_cache("api_banks.json", max_age_hours=24)
    if cached:
        return JSONResponse(cached)

    banks = compute_banks_signal()
    write_cache("api_banks.json", banks)
    return JSONResponse(banks)


@app.get("/api/digest")
async def get_digest():
    """Текстовый дайджест. Кэш 12 часов."""
    path = DATA_DIR / "digest_latest.txt"
    if path.exists():
        return {"text": path.read_text(encoding="utf-8")}
    return {"text": "Дайджест не найден. Запустите python digest.py"}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
