"""The rules must reproduce the Day Trader skill's own worked example, to the cent.

Fixtures are real Yahoo data shipped with the skill (MIT, Jake Schincariol):
TSLA on Monday 2026-10-19, and TSLA's 5-minute bars for the 60 days before.
"""

import copy
import json
import sys
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(ROOT / "scripts"))
import rules  # noqa: E402
from rules import ET  # noqa: E402
import orb_study  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
CFG = json.loads((ROOT / "config" / "desk_bot.json").read_text())
SKILL = {**CFG, "budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
         "min_order_usd": 5, "target_r": 1.0, "no_entries_after": "15:30"}   # the skill's example config


def at(hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, 19, int(h), int(m), tzinfo=ET)


class TheReel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = rules.session_bars(orb_study.load_yahoo(FIX / "TSLA-2026-10-19-5m.json"))

    def test_opening_range(self):
        self.assertEqual(rules.opening_range(self.bars), (362.18, 354.47))

    def test_waits_for_the_bar_to_close(self):
        sb = rules.completed(self.bars, at("10:12"))
        bar, status = rules.find_trigger(sb, 362.18, SKILL)
        self.assertEqual((bar, status), (None, "waiting"))

    def test_triggers_at_1015(self):
        sb = rules.completed(self.bars, at("10:15"))
        bar, status = rules.find_trigger(sb, 362.18, SKILL)
        self.assertEqual(status, "triggered")
        self.assertEqual(bar["t"], at("10:10"))
        self.assertAlmostEqual(bar["c"], 364.18, places=2)

    def test_exit_plan_1r_and_2r(self):
        plan, why = rules.plan_exit(364.18, 362.18, 354.47, SKILL)
        self.assertIsNone(why)
        self.assertEqual((plan["stop"], plan["risk"], plan["target"]), (358.32, 5.86, 370.04))
        plan2, _ = rules.plan_exit(364.18, 362.18, 354.47, CFG)          # the bot's 2R target
        self.assertEqual(plan2["target"], 375.90)

    def test_stop_rounds_down_but_not_past_an_exact_cent(self):
        plan, _ = rules.plan_exit(101.00, 100.40, 100.24, CFG)             # midpoint is exactly 100.32
        self.assertEqual(plan["stop"], 100.32)

    def test_refuses_a_stop_too_tight(self):
        plan, why = rules.plan_exit(364.18, 364.30, 364.00, CFG)
        self.assertIsNone(plan)
        self.assertIn("too close", why)

    def test_skill_sizing_8_shares(self):
        qty, which, limits = rules.size_order(364.18, 358.32, SKILL, cash=5000, deployed=0, equity=5000)
        self.assertEqual((qty, which), (8, "max loss"))
        self.assertEqual(limits, {"max loss": 8, "position cap": 10, "max order": 10, "cash": 12})

    def test_bot_sizing_is_capped_so_three_positions_fit(self):
        qty, which, _ = rules.size_order(364.18, 358.32, CFG, cash=5000, deployed=0, equity=5000)
        self.assertEqual((qty, which), (4, "position cap"))
        self.assertLessEqual(3 * qty * 364.18, 5000 * 0.9)

    def test_risk_gate(self):
        today = {"trades": 0, "risk_used": 0.0, "day_pnl": 0.0, "equity": 5000}
        self.assertEqual(rules.risk_check(8, 364.18, 358.32, 370.04, SKILL, today), [])
        self.assertTrue(any("max loss" in w for w in rules.risk_check(9, 364.18, 358.32, 370.04, SKILL, today)))
        # the skill's 10:55 case: the price ran to 366.48, reward:risk 0.44
        late = rules.risk_check(6, 366.48, 358.32, 370.04, SKILL, today)
        self.assertAlmostEqual(rules.reward_risk(366.48, 358.32, 370.04), 0.44, places=2)
        self.assertTrue(any("reward:risk" in w for w in late))

    def test_daily_limits(self):
        full = {"trades": 3, "risk_used": 30.0, "day_pnl": 0.0, "equity": 5000}
        self.assertTrue(any("trades already" in w for w in rules.risk_check(4, 364.18, 358.32, 375.90, CFG, full)))
        used = {"trades": 2, "risk_used": 140.0, "day_pnl": 0.0, "equity": 5000}
        self.assertTrue(any("daily loss" in w for w in rules.risk_check(4, 364.18, 358.32, 375.90, CFG, used)))
        down = {"trades": 1, "risk_used": 20.0, "day_pnl": -150.0, "equity": 4850}
        self.assertTrue(any("down" in w for w in rules.risk_check(4, 364.18, 358.32, 375.90, CFG, down)))
        halted = {"trades": 0, "risk_used": 0.0, "day_pnl": 0.0, "equity": 4200}
        self.assertTrue(any("halted" in w for w in rules.risk_check(4, 364.18, 358.32, 375.90, CFG, halted)))

    def test_bot_window_expires_a_late_first_breakout(self):
        cfg = {**CFG, "no_entries_after": "10:00"}
        bar, status = rules.find_trigger(self.bars, 362.18, cfg)
        self.assertEqual(status, "expired")

    def test_missing_range_bar_means_no_setup(self):
        bars = [b for b in self.bars if b["t"] != at("09:35")]
        self.assertIsNone(rules.day_setup(bars, CFG))


class Exits(unittest.TestCase):
    def bars(self, rows):
        t0 = at("10:15")
        return [{"t": t0 + timedelta(minutes=5 * i), "o": o, "h": h, "l": lo, "c": c, "v": 1}
                for i, (o, h, lo, c) in enumerate(rows)]

    def test_stop_before_target_in_the_same_bar(self):
        px, why, _ = rules.simulate_exit(self.bars([(100, 103, 98, 101)]), 100, 99, 102, time(15, 55))
        self.assertEqual((px, why), (99, "stop"))

    def test_gap_through_the_stop_fills_at_the_open(self):
        px, why, _ = rules.simulate_exit(self.bars([(100, 100.5, 99.5, 100), (97, 98, 96, 97)]), 100, 99, 102, time(15, 55))
        self.assertEqual((px, why), (97, "stop"))

    def test_target(self):
        px, why, when = rules.simulate_exit(self.bars([(100, 101, 99.5, 101), (101, 102.5, 100.5, 102)]), 100, 99, 102, time(15, 55))
        self.assertEqual((px, why, when.strftime("%H:%M")), (102, "target", "10:25"))

    def test_flat_by_uses_the_last_close_before_the_cutoff(self):
        rows = [(100, 100.5, 99.5, 100.2)] * 80                                # runs past 15:55
        px, why, when = rules.simulate_exit(self.bars(rows), 100, 99, 102, time(15, 55))
        self.assertEqual((px, why, when.strftime("%H:%M")), (100.2, "flat", "15:55"))


class SkillHistory(unittest.TestCase):
    """The skill's Time Machine reran its rule on TSLA's 59 full sessions before
    2026-10-19: it fired 33 times, 13 target, 15 stop, 5 neither, average R -0.062.
    The same rule through this repo's functions must give the same record."""

    def test_same_record_as_the_skill(self):
        cfg = copy.deepcopy(CFG)
        cfg.update({"target_r": 1.0, "no_entries_after": "16:00", "flat_by": "16:00"})
        days = orb_study.by_day(orb_study.load_yahoo(FIX / "TSLA-60d5m.json.gz"))
        trades = []
        for day, rows in sorted(days.items()):
            if day >= "2026-10-19" or len(rules.session_bars(rows)) != 78:
                continue
            trade, _ = orb_study.analyze_day("TSLA", day, rows, cfg, slip=0.0, gap_fills=False)
            if trade:
                trades.append(trade)
        count = {k: sum(1 for t in trades if t["reason"] == k) for k in ("target", "stop", "flat")}
        self.assertEqual(len(trades), 33)
        self.assertEqual(count, {"target": 13, "stop": 15, "flat": 5})
        self.assertAlmostEqual(sum(t["R"] for t in trades) / len(trades), -0.062, places=3)


if __name__ == "__main__":
    unittest.main()
