"""
core/pulse.py — Ежедневный «пульс рынка»: компактная утренняя сводка.

build_pulse(overview, today) → строка (Markdown) для отправки в бот.
overview — словарь из GET /api/overview.
Все обращения к полям защищены .get — пульс не должен падать на неполных данных.
"""

from datetime import date

from core.events import get_upcoming_events

MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _nearest_event(today):
    """Ближайшее значимое событие (high/med), иначе любое ближайшее."""
    evs = get_upcoming_events(today, days=30)
    for e in evs:
        if e["importance"] in ("high", "med"):
            return e
    return evs[0] if evs else None


def build_pulse(overview: dict, today=None) -> str:
    today  = today or date.today()
    ov     = overview or {}
    regime = ov.get("regime", {}) or {}
    sigs   = ov.get("signals", {}) or {}
    cur    = sigs.get("curve", {}) or {}
    auc    = sigs.get("auctions", {}) or {}
    bnk    = sigs.get("banks", {}) or {}
    infl   = sigs.get("inflation", {}) or {}
    rec    = ov.get("recommendation", {}) or {}

    kr = ov.get("key_rate")
    lines = [
        f"📊 *Пульс рынка* · {today.day} {MONTHS[today.month]}",
        "",
        f"{regime.get('emoji', '🔵')} Режим: *{regime.get('name', '—')}*"
        + (f"  ·  КС {kr}%" if kr is not None else ""),
    ]

    # Сигналы — по одной компактной строке
    sig_lines = []
    if cur:
        ec = cur.get("exp_cut")
        sig_lines.append(
            f"  📈 Кривая: {cur.get('label', '—')} {cur.get('arrow', '')}".rstrip()
            + (f" (−{ec}% к КС)" if ec else "")
        )
    if auc:
        btc = auc.get("avg_btc")
        sig_lines.append(f"  🏦 Аукционы: BTC {btc}×" if btc is not None
                         else "  🏦 Аукционы: —")
    if infl:
        rr = infl.get("real_rate")
        obs = infl.get("observed")
        s = f"  📉 Инфляция: {infl.get('infl_yoy', '—')}% г/г"
        if rr is not None:
            s += f"  ·  реальная ставка {rr} п.п."
        if obs is not None:
            s += f"  ·  наблюдаемая {obs}%"
        sig_lines.append(s)
    if bnk:
        tb = bnk.get("total_bln")
        sig_lines.append(f"  🏛 Банки: {bnk.get('label', '—')}"
                         + (f" (+₽{tb} млрд)" if tb else ""))
    if sig_lines:
        lines.append("")
        lines.extend(sig_lines)

    # Рекомендация
    asset = rec.get("asset")
    if asset:
        pnl = rec.get("pnl_base")
        lines.append("")
        lines.append(f"🎯 Идея: *{asset}*"
                     + (f"  ·  +{pnl}% в базовом сценарии" if pnl is not None else ""))

    # Ближайшее событие
    ev = _nearest_event(today)
    if ev:
        du = (ev["date"] - today).days
        when = "сегодня" if du == 0 else "завтра" if du == 1 else f"через {du} дн."
        mark = "" if ev["confirmed"] else " (ожидается)"
        lines.append("")
        lines.append(f"📅 Ближайшее: {ev['emoji']} {ev['title']} — {when}{mark}")

    # Вердикт
    action = ov.get("action") or ov.get("verdict")
    if action:
        lines.append("")
        lines.append(f"→ {action}")

    return "\n".join(lines)
