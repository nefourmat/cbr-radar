"""
core/events.py — Календарь событий денежно-кредитной политики.

Объединяет:
  • Заседания ЦБ (точные даты — из scripts.cbr_probabilities.CBR_MEETINGS)
  • Резюме обсуждения ключевой ставки (~ через 7 дней после заседания)
  • Аукционы Минфина (еженедельно, среда)
  • Недельная инфляция Росстата (еженедельно, среда)
  • Месячный ИПЦ Росстата (~12 число)
  • Инфляционные ожидания инФОМ (~20 число)
  • Форма 101 банков (~5 число)

get_upcoming_events(today, days) → отсортированный список событий:
    {"date": date, "title": str, "emoji": str, "kind": str,
     "importance": "high"|"med"|"low", "confirmed": bool}

Точные события (заседания) — confirmed=True; периодические (аукционы,
публикации) — confirmed=False ("ожидается"), т.к. точная дата может сдвигаться.
"""

from datetime import date, timedelta
from calendar import monthrange


def _cbr_meetings():
    """Даты заседаний ЦБ из cbr_probabilities (мягкий импорт)."""
    try:
        from scripts.cbr_probabilities import CBR_MEETINGS
        return CBR_MEETINGS
    except Exception:
        return []


def _clamp_day(year, month, day):
    """Дата day-го числа месяца, не выходя за его границы."""
    last = monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _months_in_window(today, end):
    """Список (year, month) от месяца today до месяца end включительно."""
    out, y, m = [], today.year, today.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def get_upcoming_events(today=None, days: int = 30) -> list:
    today = today or date.today()
    end   = today + timedelta(days=days)
    events = []

    def add(d, title, emoji, kind, importance, confirmed):
        if today <= d <= end:
            events.append({
                "date": d, "title": title, "emoji": emoji,
                "kind": kind, "importance": importance, "confirmed": confirmed,
            })

    # 1. Заседания ЦБ + резюме обсуждения (точные)
    for m in _cbr_meetings():
        d = m.get("date")
        if not isinstance(d, date):
            continue
        typ = m.get("type", "заседание")
        add(d, f"Заседание ЦБ ({typ})", "🏛", "meeting", "high", True)
        # Резюме обсуждения КС публикуется примерно через неделю
        add(d + timedelta(days=7),
            "Резюме обсуждения ключевой ставки", "📝", "minutes", "med", False)

    # 2. Еженедельные события (среда): аукцион Минфина и недельная инфляция
    d = today
    while d <= end:
        if d.weekday() == 2:  # среда
            add(d, "Аукцион ОФЗ (Минфин)", "🏦", "auction", "low", False)
            add(d, "Недельная инфляция (Росстат)", "📈", "weekly_cpi", "low", False)
        d += timedelta(days=1)

    # 3. Ежемесячные публикации
    for (y, mo) in _months_in_window(today, end):
        add(_clamp_day(y, mo, 5),  "Форма 101 банков (ЦБ)", "🏦", "form101", "low", False)
        add(_clamp_day(y, mo, 12), "Месячный ИПЦ (Росстат)", "📊", "monthly_cpi", "high", False)
        add(_clamp_day(y, mo, 20), "Инфляционные ожидания (инФОМ)", "📋", "infom", "med", False)

    events.sort(key=lambda e: (e["date"], {"high": 0, "med": 1, "low": 2}[e["importance"]]))
    return events


def days_until(d, today=None) -> int:
    return (d - (today or date.today())).days


def format_calendar(today=None, days: int = 21) -> str:
    """Текстовый календарь для бота/дайджеста."""
    today  = today or date.today()
    events = get_upcoming_events(today, days)
    if not events:
        return "На ближайшие недели событий не запланировано."

    MONTHS = ["", "янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"]
    lines = [f"📅 *Календарь событий* (ближайшие {days} дн.)\n"]
    for e in events:
        d  = e["date"]
        du = (d - today).days
        when = "сегодня" if du == 0 else "завтра" if du == 1 else f"через {du} дн."
        mark = "" if e["confirmed"] else " (ожидается)"
        star = "❗" if e["importance"] == "high" else ""
        lines.append(
            f"{e['emoji']} {d.day} {MONTHS[d.month]} · {when}{mark}\n"
            f"   {star}{e['title']}"
        )
    return "\n".join(lines)
