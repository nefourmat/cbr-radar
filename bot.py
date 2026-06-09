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
import requests
from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
from datetime import datetime, date

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, Bot, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.helpers import escape_markdown
from telegram.error import Forbidden, TelegramError, BadRequest
from apscheduler.schedulers.background import BackgroundScheduler

# Добавляем корень проекта
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.subscriptions import (
    NOTIFICATION_TYPES, type_meta,
    register, get_subs, toggle, subscribers_for, remove,
    unsubscribe_all, count as subs_count,
)
from core.events import get_upcoming_events, format_calendar
from core.pulse import build_pulse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-app.railway.app")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")   # ID публичного канала (опционально)
# Базовый URL API для получения свежих данных (по умолчанию = Mini App URL)
API_BASE   = os.getenv("API_BASE", WEBAPP_URL).rstrip("/")

DATA_DIR     = ROOT / "data"
# Персистентное состояние (подписки/дедуп) — на смонтированном томе, если задан STATE_DIR
STATE_DIR    = Path(os.getenv("STATE_DIR") or DATA_DIR)
NOTIFY_STATE = STATE_DIR / "notify_state.json"


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


def fetch_overview() -> dict:
    """Свежие данные: сначала живой API, затем локальный кэш-файл."""
    try:
        r = requests.get(f"{API_BASE}/api/overview", timeout=15)
        if r.ok:
            return r.json()
    except Exception as e:
        log.warning(f"overview API недоступен: {e}")
    return load_overview()


# ── notify-state: защита от повторной отправки одного и того же события ──
def _notify_state() -> dict:
    if NOTIFY_STATE.exists():
        try:
            with open(NOTIFY_STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _notify_state_set(key: str, value) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st = _notify_state()
    st[key] = value
    tmp = NOTIFY_STATE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NOTIFY_STATE)


async def broadcast(bot: Bot, chat_ids, text: str, markup=None) -> int:
    """
    Рассылка с троттлингом (~20 msg/s) и очисткой мёртвых подписчиков.
    Возвращает число успешных отправок.
    """
    sent = 0
    dead = []
    for cid in chat_ids:
        try:
            try:
                await bot.send_message(chat_id=cid, text=text,
                                       parse_mode="Markdown", reply_markup=markup)
            except BadRequest:
                # Сломалась Markdown-разметка — шлём как обычный текст, не теряем сообщение
                await bot.send_message(chat_id=cid, text=text, reply_markup=markup)
            sent += 1
            await asyncio.sleep(0.05)        # лимит Telegram ~30 msg/s
        except Forbidden:
            # Пользователь заблокировал бота — пометим на удаление
            log.info(f"chat {cid} заблокировал бота — удаляю")
            dead.append(cid)
        except TelegramError as e:
            log.warning(f"Не доставлено {cid}: {e}")
        except Exception as e:
            log.warning(f"Ошибка отправки {cid}: {e}")
    if dead:
        # Удаляем мёртвых подписчиков одним заходом вне event loop
        await asyncio.to_thread(lambda: [remove(c) for c in dead])
    return sent


def settings_keyboard(chat_id) -> InlineKeyboardMarkup:
    """Клавиатура настроек: тумблер на каждый тип уведомления."""
    subs = get_subs(chat_id)
    rows = []
    for t in NOTIFICATION_TYPES:
        mark = "✅" if subs.get(t["key"]) else "⬜️"
        rows.append([InlineKeyboardButton(
            f"{mark} {t['emoji']} {t['title']}",
            callback_data=f"sub:{t['key']}",
        )])
    rows.append([InlineKeyboardButton("📊 Открыть терминал",
                                      web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(rows)


def format_signal_short(overview: dict) -> str:
    """Короткое сообщение для алертов и дайджеста."""
    regime = overview.get("regime", {})
    rec    = overview.get("recommendation", {})
    sigs   = overview.get("signals", {})
    cur    = sigs.get("auctions", {}).get("avg_btc", 0)
    banks  = sigs.get("banks", {})

    em = regime.get("emoji", "🔵")
    # Экранируем все динамические строки (могут содержать _ * ` [ из внешних данных)
    nm    = escape_markdown(regime.get("name", "Нормализация"))
    desc  = escape_markdown(regime.get("desc", ""))
    asset = escape_markdown(str(rec.get("asset", "—")))
    clabel = escape_markdown(sigs.get("curve", {}).get("label", "—"))
    carrow = sigs.get("curve", {}).get("arrow", "")
    bdesc = escape_markdown(banks.get("description", "—"))

    lines = [
        f"{em} *Режим рынка: {nm}*",
        f"_{desc}_",
        "",
        f"*Рекомендация недели*",
        f"Актив: `{asset}`",
        f"Доходность: *+{rec.get('pnl_base', 0):.1f}%* при КС→13%",
        f"Вероятность: *{rec.get('probability', 0)}%* ({rec.get('win_rate', 0)}% win rate)",
        "",
        f"*Три сигнала*",
        f"① Кривая: {clabel} {carrow}",
        f"② Аукционы: BTC *{cur:.2f}×*",
        f"③ Банки: {bdesc}",
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
    """Приветствие + регистрация подписчика + кнопка открытия Mini App."""
    user = update.effective_user
    # Регистрируем с подписками по умолчанию (заседания/сигнал входа/дайджест)
    register(update.effective_chat.id, user.first_name if user else "")
    # Экранируем имя: символы _ * ` [ ломают Markdown-разметку
    name = escape_markdown(user.first_name if user else "Инвестор")

    text = (
        f"Привет, {name}! 👋\n\n"
        f"*ЦБ-Радар* — система раннего обнаружения переломов "
        f"процентного цикла.\n\n"
        f"Что внутри:\n"
        f"• Режим рынка: Нормализация / Смягчение / Перегрев / Паника\n"
        f"• Лучшая ставка недели с расчётом доходности\n"
        f"• Что делают крупнейшие банки с ОФЗ\n"
        f"• Вероятности снижения КС по заседаниям\n\n"
        f"*Команды:*\n"
        f"/pulse — пульс рынка сейчас\n"
        f"/calendar — календарь событий\n"
        f"/signal — текущий сигнал\n"
        f"/digest — полный дайджест\n"
        f"/settings — настройка уведомлений\n"
        f"/stop — отписаться от всех уведомлений\n\n"
        f"Я уже подписал тебя на ключевые уведомления (заседания ЦБ, "
        f"сигнал входа, недельный дайджест) — настроить можно в /settings.\n\n"
        f"_Храню только твой chat ID, чтобы доставлять уведомления. "
        f"Отписаться — /stop._"
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

    # Бэктики в тексте сломали бы code-fence — заменяем на апострофы
    digest = digest.replace("`", "'")

    # Telegram ограничение 4096 символов
    if len(digest) > 4000:
        await update.message.reply_text(
            f"```\n{digest[:3900]}\n```\n_[продолжение в терминале]_",
            parse_mode="Markdown",
            reply_markup=webapp_keyboard("Открыть полный анализ"),
        )
    else:
        await update.message.reply_text(
            f"```\n{digest}\n```",
            parse_mode="Markdown",
            reply_markup=webapp_keyboard(),
        )


async def cmd_pulse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пульс рынка по запросу."""
    ov = await asyncio.to_thread(fetch_overview)   # блокирующий HTTP — в тред
    if not ov:
        await update.message.reply_text("Данные загружаются... Попробуй через минуту.")
        return
    await update.message.reply_text(
        build_pulse(ov),
        parse_mode="Markdown",
        reply_markup=webapp_keyboard("Открыть терминал"),
    )


async def cmd_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Календарь предстоящих событий ДКП."""
    await update.message.reply_text(
        format_calendar(days=21),
        parse_mode="Markdown",
        reply_markup=webapp_keyboard("Вероятности по заседаниям"),
    )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Настройка подписок на уведомления."""
    register(update.effective_chat.id,
             update.effective_user.first_name if update.effective_user else "")
    lines = ["⚙️ *Уведомления*", "", "Нажми, чтобы включить/выключить:"]
    for t in NOTIFICATION_TYPES:
        lines.append(f"{t['emoji']} *{t['title']}* — _{t['desc']}_")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=settings_keyboard(update.effective_chat.id),
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отписка от всех уведомлений (запись остаётся, можно включить заново)."""
    unsubscribe_all(update.effective_chat.id)
    await update.message.reply_text(
        "🔕 Ты отписан от всех уведомлений.\n"
        "Включить нужные обратно — в /settings.",
        reply_markup=settings_keyboard(update.effective_chat.id),
    )


async def on_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия на тумблер уведомления."""
    q = update.callback_query
    key = q.data.split(":", 1)[1]
    try:
        new_val = toggle(update.effective_chat.id, key)
    except KeyError:
        await q.answer("Неизвестная настройка")
        return
    meta = type_meta(key) or {"title": key}
    await q.answer(f"{'✅ Включено' if new_val else '⬜️ Выключено'}: {meta['title']}")
    try:
        await q.edit_message_reply_markup(
            reply_markup=settings_keyboard(update.effective_chat.id)
        )
    except TelegramError:
        pass  # сообщение не изменилось / устарело — не критично


# ─────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────

async def _deliver(bot: Bot, sub_key: str, text: str, btn_label: str):
    """Отправляет text подписчикам sub_key и (если задан) в канал."""
    markup = webapp_keyboard(btn_label)
    subs = subscribers_for(sub_key)
    sent = await broadcast(bot, subs, text, markup)
    log.info(f"[{sub_key}] доставлено {sent}/{len(subs)} подписчикам")
    if CHANNEL_ID:
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=text,
                                   parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            log.warning(f"Канал: {e}")


async def job_weekly_digest(bot: Bot):
    """Пятница 09:00 — еженедельный дайджест (подписчики + канал)."""
    log.info("Готовим еженедельный дайджест...")

    def _prep():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "refresh_data.py")],
                       check=True, timeout=300, cwd=str(ROOT))
        subprocess.run([sys.executable, str(ROOT / "digest.py")],
                       check=True, timeout=120, cwd=str(ROOT))

    try:
        await asyncio.to_thread(_prep)   # блокирующие subprocess — в тред
    except Exception as e:
        log.error(f"Подготовка дайджеста: {e}")

    overview = await asyncio.to_thread(fetch_overview)
    issue_n  = datetime.now().strftime("Неделя %U · %d.%m.%Y")
    text     = f"📊 *ЦБ-Радар · {issue_n}*\n\n" + format_signal_short(overview)
    await _deliver(bot, "weekly_digest", text, "Открыть полный анализ")


async def job_auction_alert(bot: Bot):
    """Среда 13:10 — итоги аукциона / сигнал входа (подписчики + канал)."""
    log.info("Проверяем данные аукциона...")
    overview = await asyncio.to_thread(fetch_overview)
    auctions = (overview.get("signals", {}) or {}).get("auctions", {})
    if not auctions:
        log.warning("Нет данных аукциона — алерт пропущен")
        return

    btc   = auctions.get("avg_btc", 0)
    entry = auctions.get("entry_signal", False)

    if entry:
        text = (
            "🚨 *СИГНАЛ ВХОДА В ОФЗ*\n\n"
            f"Bid-to-cover вырос до *{btc:.2f}×* (норма ≥ 1.5×)\n\n"
            "Рынок готов поглощать предложение.\n"
            "Рекомендация: рассмотреть вход в длинные ОФЗ."
        )
        await _deliver(bot, "entry_signal", text, "Открыть скринер")
    elif 0 < btc < 0.5:
        text = (
            "📉 *Слабый аукцион*\n\n"
            f"Bid-to-cover *{btc:.2f}×* — в {1.5/btc:.1f} раза ниже нормы.\n\n"
            "Сигнал входа ещё не пришёл. Ждём BTC > 1.5×."
        )
        await _deliver(bot, "auctions", text, "Открыть скринер")
    else:
        log.info(f"Аукцион нейтральный (BTC {btc:.2f}×), алерт не нужен")


async def job_daily_pulse(bot: Bot):
    """Ежедневно 09:00 — пульс рынка подписчикам daily_pulse."""
    subs = subscribers_for("daily_pulse")
    if not subs:
        return
    text = build_pulse(await asyncio.to_thread(fetch_overview))
    sent = await broadcast(bot, subs, text, webapp_keyboard("Открыть терминал"))
    log.info(f"[daily_pulse] доставлено {sent}/{len(subs)}")


async def job_meeting_reminders(bot: Bot):
    """Ежедневно 09:00 — напоминания о заседании ЦБ за 3/1/0 дней."""
    subs = subscribers_for("meetings")
    if not subs:
        return
    today = date.today()
    for e in get_upcoming_events(today, days=3):
        if e["kind"] != "meeting":
            continue
        du = (e["date"] - today).days
        if du not in (3, 1, 0):
            continue
        when = "сегодня" if du == 0 else "завтра" if du == 1 else f"через {du} дня"
        text = (
            f"🏛 *Заседание ЦБ {when}*\n\n"
            f"{e['title']} — {e['date'].strftime('%d.%m.%Y')}.\n"
            "Смотри вероятности по сценариям в терминале."
        )
        sent = await broadcast(bot, subs, text, webapp_keyboard("Вероятности по заседаниям"))
        log.info(f"[meetings] заседание {when}: доставлено {sent}/{len(subs)}")


async def job_inflation_check(bot: Bot):
    """Ежедневно — если вышли новые данные по инфляции, уведомляем подписчиков."""
    subs = subscribers_for("inflation")
    if not subs:
        return
    ov = await asyncio.to_thread(fetch_overview)
    infl = (ov.get("signals", {}) or {}).get("inflation", {})
    period = infl.get("date")
    if not period:
        return
    if _notify_state().get("inflation_period") == period:
        return  # уже уведомляли об этом периоде
    obs = infl.get("observed")
    text = (
        "📈 *Обновление по инфляции*\n\n"
        f"Официальная: *{infl.get('infl_yoy', '—')}%* г/г (цель 4%)\n"
        f"Реальная ставка: *{infl.get('real_rate', '—')} п.п.*"
        + (f"\nНаблюдаемая (инФОМ): *{obs}%*" if obs is not None else "")
        + f"\n\n{infl.get('description', '')}"
    )
    sent = await broadcast(bot, subs, text, webapp_keyboard("Открыть терминал"))
    _notify_state_set("inflation_period", period)
    log.info(f"[inflation] период {period}: доставлено {sent}/{len(subs)}")


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────

# Главный event loop бота (для запуска корутин из потоков APScheduler)
MAIN_LOOP: "asyncio.AbstractEventLoop | None" = None


async def _post_init(app: Application):
    """Меню команд + запоминаем event loop для планировщика."""
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    await app.bot.set_my_commands([
        BotCommand("start",    "Запуск и информация"),
        BotCommand("pulse",    "Пульс рынка сейчас"),
        BotCommand("calendar", "Календарь событий"),
        BotCommand("signal",   "Текущий сигнал"),
        BotCommand("digest",   "Полный дайджест"),
        BotCommand("settings", "Настройки уведомлений"),
        BotCommand("stop",     "Отписаться от всех уведомлений"),
    ])
    log.info(f"Бот запущен · подписчиков: {subs_count()}")


def _fire(job_coro, bot: Bot):
    """
    Запускает корутину job_coro(bot) на ГЛАВНОМ loop бота из потока APScheduler.
    Нельзя использовать asyncio.run() — у app.bot httpx-клиент привязан к loop
    polling'а; новый loop приведёт к 'Event loop is closed'.
    """
    if MAIN_LOOP is None or not MAIN_LOOP.is_running():
        log.warning("event loop ещё не готов — джоба пропущена")
        return
    fut = asyncio.run_coroutine_threadsafe(job_coro(bot), MAIN_LOOP)
    try:
        fut.result(timeout=600)
    except Exception as e:
        log.error(f"Джоба {getattr(job_coro, '__name__', job_coro)}: {e}")


def _build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    # Команды
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("signal",   cmd_signal))
    app.add_handler(CommandHandler("digest",   cmd_digest))
    app.add_handler(CommandHandler("pulse",    cmd_pulse))
    app.add_handler(CommandHandler("calendar", cmd_calendar))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    # Тумблеры уведомлений
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^sub:"))
    return app


def _start_scheduler(app: Application) -> BackgroundScheduler:
    # Планировщик (Europe/Moscow) — джобы исполняются на главном loop бота
    scheduler = BackgroundScheduler(timezone="Europe/Moscow")
    scheduler.add_job(lambda: _fire(job_daily_pulse, app.bot),
                      "cron", hour=9, minute=0, id="daily_pulse")
    scheduler.add_job(lambda: _fire(job_meeting_reminders, app.bot),
                      "cron", hour=9, minute=5, id="meeting_reminders")
    scheduler.add_job(lambda: _fire(job_inflation_check, app.bot),
                      "cron", hour=12, minute=0, id="inflation_check")
    scheduler.add_job(lambda: _fire(job_weekly_digest, app.bot),
                      "cron", day_of_week="fri", hour=9, minute=0, id="weekly_digest")
    scheduler.add_job(lambda: _fire(job_auction_alert, app.bot),
                      "cron", day_of_week="wed", hour=13, minute=10, id="auction_alert")
    scheduler.start()
    log.info("Планировщик запущен")
    return scheduler


def token_ok() -> bool:
    return bool(BOT_TOKEN) and BOT_TOKEN != "YOUR_TOKEN_HERE"


def run():
    """Запуск как отдельного процесса (python bot.py)."""
    if not token_ok():
        log.error("Установи BOT_TOKEN в переменных окружения!")
        return
    app = _build_application()
    _start_scheduler(app)
    log.info(f"Mini App URL: {WEBAPP_URL} · API: {API_BASE}")
    app.run_polling(drop_pending_updates=True)


def run_in_thread():
    """
    Запуск бота из фонового потока (встраивание в веб-сервис).
    Нужен собственный event loop и stop_signals=None — обработчики сигналов
    можно ставить только в главном потоке.
    """
    if not token_ok():
        log.warning("BOT_TOKEN не задан — Telegram-бот не запущен")
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = _build_application()
    _start_scheduler(app)
    log.info(f"Telegram-бот: polling в фоновом потоке · API: {API_BASE}")
    app.run_polling(drop_pending_updates=True, stop_signals=None, close_loop=False)


if __name__ == "__main__":
    run()
