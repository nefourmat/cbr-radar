"""Динамические сценарии КС от текущей ставки."""

CUT_STEPS_BPS = (50, 100, 150)


def build_rate_scenarios(key_rate: float) -> list[dict]:
    """
    Сценарии снижения КС от текущего уровня.
    Возвращает cut-сценарии + flat, округление целевой ставки до 0.5%.
    """
    scenarios = []
    for cut in CUT_STEPS_BPS:
        target = round((key_rate - cut / 100) * 2) / 2
        if target <= 0:
            continue
        scenarios.append({
            "id":       f"cut_{cut}",
            "label":    f"КС → {target:.1f}%",
            "target_rate": target,
            "cut_bps":  -cut,
        })
    scenarios.append({
        "id":          "flat",
        "label":       "КС без изменений",
        "target_rate": round(key_rate * 2) / 2,
        "cut_bps":     0,
    })
    return scenarios
