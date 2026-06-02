"""
bot.py — Telegram Bot для ЦБ-Радар

Что умеет:
  /start   → Welcome + кнопка «Открыть терминал»
  /digest  → Последний текстовый дайджест
  /signal  → Текущий сигнал (коротко)

Автоматически:
  Пятница 09:00 → еженедельный дайджест
  Среда 13:10   → алерт после аукциона (если BTC изменился)
  При BTC > 1.5× → экстренный алерт «Сигнал входа!»

Переменные окружения:
  BOT_TOKEN     — токен от @BotFather
  WEBAPP_URL    — URL Mini App (https://your-app.railway.app)
  CHANNEL_ID    — ID канала (например -1001234567890)
"""

import os
import sys
import json
import asyncio
import logging
from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, Bot
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)
from apscheduler.schedulers.background import BackgroundScheduler

# Добавляем корень проекта
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-app.railway.app")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")   # ID публичного канала

DATA_DIR = Path("data")


# ─────────────────────────────────────────────
# УТИЛИТЫ
# ─────────────────────────────────────────────

def webapp_keyboard(label="📊 Открыть терминал"):
    """Кнопка открытия Mini App."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


def load_overview() -> dict:
    path = DATA_DIR / "api_overview.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_digest() -> str:
    path = DATA_DIR / "digest_latest.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def format_signal_short(overview: dict) -> str:
    """Короткое сообщение для алертов и дайджеста."""
    regime = overview.get("regime", {})
    rec    = overview.get("recommendation", {})
    sigs   = overview.get("signals", {})
    cur    = sigs.get("auctions", {}).get("avg_btc", 0)
    banks  = sigs.get("banks", {})

    em = regime.get("emoji", "🔵")
    nm = regime.get("name", "Нормализация")

    lines = [
        f"{em} *Режим рынка: {nm}*",
        f"_{regime.get('desc', '')}_",
        "",
        f"*Рекомендация недели*",
        f"Актив: `{rec.get('asset', '—')}`",
        f"Доходность: *+{rec.get('pnl_base', 0):.1f}%* при КС→13%",
        f"Вероятность: *{rec.get('probability', 0)}%* ({rec.get('win_rate', 0)}% win rate)",
        "",
        f"*Три сигнала*",
        f"① Кривая: {sigs.get('curve', {}).get('label', '—')} {sigs.get('curve', {}).get('arrow', '')}",
        f"② Аукционы: BTC *{cur:.2f}×*",
        f"③ Банки: {banks.get('description', '—')}",
    ]

    if rec.get("entry_signal"):
        lines = [
            "🚨 *СИГНАЛ ВХОДА ПРИШЁЛ*",
            "",
        ] + lines

    return "\n".join(lines)


# ─────────────────────────────────────────────
# КОМАНДЫ
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приветствие + кнопка открытия Mini App."""
    user = update.effective_user
    name = user.first_name if user else "Инвестор"

    text = (
        f"Привет, {name}! 👋\n\n"
        f"*ЦБ-Радар* — система раннего обнаружения переломов "
        f"процентного цикла.\n\n"
        f"Что внутри:\n"
        f"• Режим рынка: Нормализация / Смягчение / Перегрев / Паника\n"
        f"• Лучшая ставка недели с расчётом доходности\n"
        f"• Что делают крупнейшие банки с ОФЗ\n"
        f"• Вероятности снижения КС по заседаниям\n\n"
        f"Нажми кнопку ниже чтобы открыть терминал 👇"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=webapp_keyboard("📊 Открыть терминал"),
    )


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Текущий сигнал коротко."""
    overview = load_overview()
    if not overview:
        await update.message.reply_text(
            "Данные загружаются... Попробуй через минуту.",
            reply_markup=webapp_keyboard(),
        )
        return

    text = format_signal_short(overview)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=webapp_keyboard(),
    )


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Последний полный дайджест."""
    digest = load_digest()
    if not digest:
        await update.message.reply_text("Дайджест ещё не готов.")
        return

    # Telegram ограничение 4096 символов
    if len(digest) > 4000:
        await update.message.reply_text(
            digest[:4000] + "\n\n_[продолжение в терминале]_",
            parse_mode="Markdown",
            reply_markup=webapp_keyboard("Открыть полный анализ"),
        )
    else:
        await update.message.reply_text(
            f"```\n{digest}\n```",
            parse_mode="Markdown",
            reply_markup=webapp_keyboard(),
        )


# ─────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────

async def job_weekly_digest(bot: Bot):
    """Пятница 09:00 — еженедельный дайджест."""
    if not CHANNEL_ID:
        log.warning("CHANNEL_ID не установлен, пропускаем рассылку")
        return

    log.info("Отправляем еженедельный дайджест...")

    try:
        # Обновляем данные
        import subprocess
        subprocess.run(["python", "digest.py"], check=True, timeout=120)

        overview = load_overview()
        text     = format_signal_short(overview)

        issue_n = datetime.now().strftime("Неделя %U · %d.%m.%Y")
        header  = f"📊 *ЦБ-Радар · {issue_n}*\n\n"

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=header + text,
            parse_mode="Markdown",
            reply_markup=webapp_keyboard("Открыть полный анализ"),
        )
        log.info("Еженедельный дайджест отправлен")
    except Exception as e:
        log.error(f"Ошибка дайджеста: {e}")


async def job_auction_alert(bot: Bot):
    """Среда 13:10 — алерт после аукциона."""
    if not CHANNEL_ID:
        return

    log.info("Проверяем данные аукциона...")

    try:
        # Обновляем данные аукционов
        overview = load_overview()
        sigs     = overview.get("signals", {})
        auctions = sigs.get("auctions", {})
        btc      = auctions.get("avg_btc", 0)
        entry    = auctions.get("entry_signal", False)

        if entry:
            # 🚨 Сигнал входа!
            text = (
                "🚨 *СИГНАЛ ВХОДА В ОФЗ*\n\n"
                f"Bid-to-cover вырос до *{btc:.2f}×* "
                f"(норма ≥ 1.5×)\n\n"
                f"Рынок готов поглощать предложение.\n"
                f"Рекомендация: рассмотреть вход в длинные ОФЗ."
            )
        elif btc < 0.5:
            # Слабый аукцион — информационный алерт
            text = (
                f"📉 *Слабый аукцион*\n\n"
                f"Bid-to-cover *{btc:.2f}×* — в "
                f"{1.5/btc:.1f} раза ниже нормы.\n\n"
                f"Сигнал входа ещё не пришёл. Ждём BTC > 1.5×."
            )
        else:
            # Нейтральный — не отправляем
            log.info(f"Аукцион нейтральный (BTC {btc:.2f}×), алерт не нужен")
            return

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=webapp_keyboard("Открыть скринер"),
        )
        log.info("Аукционный алерт отправлен")
    except Exception as e:
        log.error(f"Ошибка аукционного алерта: {e}")


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────

def run():
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        log.error("Установи BOT_TOKEN в переменных окружения!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("digest", cmd_digest))

    # Планировщик
    scheduler = BackgroundScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        lambda: asyncio.run(job_weekly_digest(app.bot)),
        "cron",
        day_of_week="fri",
        hour=9, minute=0,
        id="weekly_digest",
    )
    scheduler.add_job(
        lambda: asyncio.run(job_auction_alert(app.bot)),
        "cron",
        day_of_week="wed",
        hour=13, minute=10,
        id="auction_alert",
    )

    scheduler.start()
    log.info("Планировщик запущен")
    log.info(f"Mini App URL: {WEBAPP_URL}")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
