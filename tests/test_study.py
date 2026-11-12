"""The backtest's account must obey the same limits as the live bot."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(ROOT / "scripts"))
import orb_study  # noqa: E402
import rules  # noqa: E402

CFG = json.loads((ROOT / "config" / "desk_bot.json").read_text())
SLIP = CFG["study"]["slippage_bps_per_side"] / 10000.0


def sig(sym, day, hm, entry=100.0, stop=99.0, exit_px=102.0, exit_time="11:00", vol=1.0):
    return {"symbol": sym, "date": day, "time": hm, "entry": entry, "stop": stop, "target": entry + 2 * (entry - stop),
            "risk": entry - stop, "exit": exit_px, "reason": "target", "exit_time": exit_time,
            "R": orb_study.net_r(entry, exit_px, entry - stop, SLIP), "dollar_vol": vol}


class Account(unittest.TestCase):
    def test_three_a_day_in_trigger_order_most_traded_first(self):
        day = "2024-01-02"
        sigs = [sig("A", day, "10:05", vol=1), sig("B", day, "09:50", vol=1), sig("C", day, "10:05", vol=9),
                sig("D", day, "10:10"), sig("E", day, "10:20")]
        curve, taken, halted = orb_study.simulate_account(sigs, [day], CFG, SLIP)
        self.assertEqual([t["symbol"] for t in taken], ["B", "C", "A"])
        self.assertIsNone(halted)
        for t in taken:
            self.assertLessEqual(t["qty"] * t["entry"], CFG["budget"] * CFG["max_position_pct"] / 100)
            self.assertLessEqual(t["qty"] * t["risk"], CFG["max_loss_per_trade_usd"])
        self.assertAlmostEqual(curve[0][1], 5000 + sum(t["pnl"] for t in taken))

    def test_costs_come_off(self):
        day = "2024-01-02"
        _, taken, _ = orb_study.simulate_account([sig("A", day, "10:05", exit_px=100.0)], [day], CFG, SLIP)
        self.assertLess(taken[0]["pnl"], 0)                       # flat price, still a small loss after costs

    def test_halts_after_the_drawdown_limit(self):
        days = [f"2024-01-{d:02d}" for d in range(2, 5)]
        _, taken, halted = orb_study.simulate_account([sig("A", d, "10:05") for d in days], days,
                                                       {**CFG, "budget": 5000, "halt_drawdown_pct": 0.0001}, SLIP)
        self.assertIsNone(halted)                                 # winners never trip it
        losers = [sig("A", d, "10:05", exit_px=99.0) for d in days]
        _, taken, halted = orb_study.simulate_account(losers, days, {**CFG, "halt_drawdown_pct": 0.1}, SLIP)
        self.assertEqual((len(taken), halted), (1, days[1]))

    def test_statistics(self):
        m, lo, hi = orb_study.boot_ci({"d1": [1.0, 1.0], "d2": [1.0], "d3": [1.0]})
        self.assertEqual((m, lo, hi), (1.0, 1.0, 1.0))
        self.assertAlmostEqual(orb_study.max_drawdown([100, 120, 90, 130]), -0.25)
        self.assertAlmostEqual(orb_study.cagr(100, 121, "2020-01-01", "2022-01-01"), 0.1, places=3)


class EndToEnd(unittest.TestCase):
    def test_runs_on_the_sample_data_and_reports_every_check(self):
        bars = orb_study.load_yahoo(ROOT / "tests" / "fixtures" / "TSLA-60d5m.json.gz")
        days = sorted(orb_study.by_day(bars))
        bench = [(d, rules.session_bars(r)[-1]["c"]) for d, r in sorted(orb_study.by_day(bars).items())]
        res = orb_study.run_study([("TSLA", bars)], CFG, days[0], days[-1], bench)
        self.assertEqual(len(res["checks"]), len(CFG["study"]["pass_mark"]))
        self.assertFalse(res["passed"])                           # one stock for 60 days cannot pass: too few trades
        md = orb_study.render(res, CFG, "sample", days[0], days[-1])
        self.assertIn("FAIL", md)
        self.assertLessEqual(res["taken"], res["days"] * CFG["max_trades_per_day"])


if __name__ == "__main__":
    unittest.main()
