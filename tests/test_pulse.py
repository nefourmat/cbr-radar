"""tests/test_pulse.py — сборка ежедневного пульса."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pulse import build_pulse, _md


class TestPulse:
    def test_empty_overview_does_not_crash(self):
        out = build_pulse({}, date(2026, 6, 9))
        assert isinstance(out, str) and "Пульс рынка" in out

    def test_partial_overview(self):
        out = build_pulse({"regime": {"name": "Паника", "emoji": "🔴"}}, date(2026, 6, 9))
        assert "Паника" in out

    def test_markdown_specials_escaped(self):
        ov = {"recommendation": {"asset": "ОФЗ*26238_x"},
              "regime": {"name": "Пере*грев"}}
        out = build_pulse(ov, date(2026, 6, 9))
        # сырых неэкранированных спецсимволов из данных быть не должно
        assert "ОФЗ\\*26238\\_x" in out
        assert "Пере\\*грев" in out

    def test_zero_values_are_shown_not_dropped(self):
        ov = {"signals": {"curve": {"label": "Нейтр", "arrow": "→", "exp_cut": 0},
                          "banks": {"label": "Нейтр", "total_bln": 0}}}
        out = build_pulse(ov, date(2026, 6, 9))
        assert "0% к КС" in out          # exp_cut==0 не потерян
        assert "+₽0 млрд" in out         # total_bln==0 не потерян

    def test_md_helper(self):
        assert _md("a*b_c`d[e") == "a\\*b\\_c\\`d\\[e"
