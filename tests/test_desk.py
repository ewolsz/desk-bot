"""The approval desk: the Time Machine, the alert, the replies, the log. No network."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(ROOT / "scripts"))
import desk  # noqa: E402
import rules  # noqa: E402
import orb_study  # noqa: E402
from rules import ET  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
CFG = json.loads((ROOT / "config" / "desk_bot.json").read_text())
DAY = "2026-10-19"


class TimeMachine(unittest.TestCase):
    """The skill's published run for TSLA on 2026-10-19 (1R target, to 4:00 PM, seed 19):
    target first 29.4 %, stop first 25.5 %, neither 45.1 %, average R +0.034, and the
    history 33 trades: 13 target, 15 stop, 5 neither, average R -0.062."""

    @classmethod
    def setUpClass(cls):
        cls.hist = orb_study.load_yahoo(FIX / "TSLA-60d5m.json.gz")
        cls.today = rules.session_bars(orb_study.load_yahoo(FIX / "TSLA-2026-10-19-5m.json"))

    def test_same_numbers_as_the_skill(self):
        cfg = copy.deepcopy(CFG)
        cfg.update({"target_r": 1.0, "no_entries_after": "16:00", "flat_by": "16:00"})
        setup = rules.day_setup(self.today, cfg)
        r = desk.replay(self.hist, setup["plan"], setup["bar"], cfg, DAY)
        self.assertTrue(r["ok"])
        mc = r["mc"]
        self.assertEqual((mc["p_target"], mc["p_stop"], mc["p_neither"], mc["avg_R"]), (0.2944, 0.2551, 0.4505, 0.034))
        self.assertEqual(r["sessions"], 59)
        self.assertEqual(r["verdict"], "Close to a coin flip: target first 29.4 % vs stop first 25.5 %.")
        h = r["history"]
        self.assertEqual((h["trades"], h["target"], h["stop"], h["flat"], h["avg_r"]), (33, 13, 15, 5, -0.062))

    def test_the_bots_own_plan_is_scored_to_its_flat_by_time(self):
        setup = rules.day_setup(self.today, CFG)                          # 2R target, flat by 15:55
        r = desk.replay(self.hist, setup["plan"], setup["bar"], CFG, DAY, replays=2000)
        mc = r["mc"]
        self.assertEqual(mc["until"], "15:55")
        self.assertEqual(mc["bars_left"], 68)
        self.assertEqual(mc["r_at_target"], 2.0)
        self.assertAlmostEqual(mc["p_target"] + mc["p_stop"] + mc["p_neither"], 1.0, places=3)
        self.assertLess(mc["p_target"], 0.2944)                           # a target twice as far is hit less often
        self.assertIn("at 2R the target has to come first in 33 %", r["verdict"])
        self.assertLessEqual(r["history"]["trades"], 33)                  # the bot's entry window is shorter

    def test_same_seed_same_answer(self):
        setup = rules.day_setup(self.today, CFG)
        a = desk.replay(self.hist, setup["plan"], setup["bar"], CFG, DAY, replays=500)
        b = desk.replay(self.hist, setup["plan"], setup["bar"], CFG, DAY, replays=500)
        self.assertEqual(a["mc"], b["mc"])

    def test_today_is_never_in_the_pool(self):
        setup = rules.day_setup(self.today, CFG)
        r = desk.replay(self.hist, setup["plan"], setup["bar"], CFG, DAY, replays=100)
        self.assertLess(r["to"], DAY)

    def test_too_little_history_gives_no_score(self):
        setup = rules.day_setup(self.today, CFG)
        few = [b for b in self.hist if b["t"].strftime("%Y-%m-%d") >= "2026-10-01"]
        r = desk.replay(few, setup["plan"], setup["bar"], CFG, DAY)
        self.assertFalse(r["ok"])
        self.assertIn("full past sessions", r["why"])
        self.assertIsNone(desk.likelihood(r))

    def test_volume_against_a_normal_morning(self):
        setup = rules.day_setup(self.today, CFG)
        rv = desk.rel_volume(self.today, self.hist, setup["bar"], DAY)
        self.assertEqual(desk.likelihood(desk.replay(self.hist, setup["plan"], setup["bar"], CFG, DAY)), 50)
        self.assertGreater(rv, 0.2)
        self.assertLess(rv, 5)
        double = [{**b, "v": 2 * b["v"]} for b in self.today]
        self.assertAlmostEqual(desk.rel_volume(double, self.hist, setup["bar"], DAY), 2 * rv, delta=0.02)
        self.assertIsNone(desk.rel_volume(self.today, self.hist[-300:], setup["bar"], DAY))


def tmres(p_t=0.24, p_s=0.50, avg=0.02, trades=20, hist_avg=-0.2):
    mc = {"replays": 10000, "seed": 19, "p_target": p_t, "p_stop": p_s, "p_neither": round(1 - p_t - p_s, 4),
          "p_green": 0.41, "avg_R": avg, "r_at_target": 2.0, "until": "15:55"}
    return {"ok": True, "mc": mc, "sessions": 58, "verdict": desk.tm.verdict(mc), "notes": desk.tm.verdict_notes(mc),
            "lean": desk.tm.lean(mc),
            "history": {"trades": trades, "target": 5, "stop": 11, "flat": 4, "avg_r": hist_avg, "sessions": 58}}


class Alert(unittest.TestCase):
    order = {"symbol": "AAPL", "qty": 14, "ref": 101.5, "entry": 101.5, "stop": 100.0, "target": 104.5,
             "risk_usd": 21.0, "triggered": "10:15"}

    def test_news_must_name_the_company(self):
        items = [{"headline": "Chip stocks rally after Jensen Huang remarks", "symbols": ["NVDA", "AMD", "INTC"]},
                 {"headline": "AMD wins a new data-center customer", "symbols": ["AMD"]},
                 {"headline": "Nvidia supplier lifts its outlook", "symbols": ["NVDA", "AMD"]},
                 {"headline": "Ten stocks moving at midday", "symbols": ["NVDA", "AMD", "F", "T"]},
                 {"headline": "Ford recalls trucks; AT&T adds subscribers", "symbols": ["F", "T"]}]
        self.assertEqual(desk.about("NVDA", items), ["Chip stocks rally after Jensen Huang remarks", "Nvidia supplier lifts its outlook"])
        self.assertEqual(desk.about("AMD", items), ["AMD wins a new data-center customer"])
        self.assertEqual(desk.about("INTC", items), [])                   # tagged, but not about Intel
        self.assertEqual(desk.about("F", items), ["Ford recalls trucks; AT&T adds subscribers"])
        self.assertEqual(desk.about("T", items), ["Ford recalls trucks; AT&T adds subscribers"])
        self.assertEqual(desk.about("C", [{"headline": "C-suite shake-up at a retailer"}]), [])   # one-letter tickers never match
        self.assertEqual(desk.about("NVDA", None), [])
        self.assertLessEqual(set(desk.NAMES), set(CFG["universe"]))
        self.assertEqual(len(CFG["universe"]), len(set(CFG["universe"])))
        # stocks without a hand-written name use the broker's company name
        self.assertEqual(desk.name_word("Rivian Automotive, Inc. Class A Common Stock"), "Rivian")
        self.assertIsNone(desk.name_word("The Trade Desk, Inc."))
        self.assertIsNone(desk.name_word("Snap Inc."))
        self.assertIsNone(desk.name_word(None))
        news = [{"headline": "Rivian recalls trucks"}, {"headline": "EV stocks slide"}]
        self.assertEqual(desk.about("RIVN", news, ["Rivian"]), ["Rivian recalls trucks"])
        self.assertEqual(desk.about("RIVN", news, [None]), [])

    def test_key_points(self):
        pts = desk.key_points(self.order, tmres(), 1.8, -0.4, ["Apple cuts guidance"])
        self.assertEqual(pts, ["Breakout: first close above the opening 15-minute high, at 10:15.",
                               "Same setup on AAPL, last 58 days: 20 trades, 5 hit the target, 11 hit the stop.",
                               "News: Apple cuts guidance",
                               "Volume: 1.8x the usual for this time of day (heavy)."])
        pts = desk.key_points(self.order, tmres(), 0.5, 0.3, [])
        self.assertEqual(pts[2:], ["Volume: 0.5x the usual for this time of day (light).", "Market (SPY): +0.30% since the open."])
        pts = desk.key_points(self.order, {"ok": False, "why": "it could not run"}, None, None, None)
        self.assertEqual(pts, ["Breakout: first close above the opening 15-minute high, at 10:15.",
                               "No Time Machine score: it could not run."])
        self.assertIn("News: no headline about this company", desk.key_points(self.order, tmres(), None, None, [])[-1])

    def test_alert_text(self):
        expires = datetime(2026, 11, 10, 10, 25, tzinfo=ET)
        pts = desk.key_points(self.order, tmres(), 1.8, -0.4, [])
        title, body = desk.alert_text(self.order, tmres(), pts, expires, CFG)
        self.assertEqual(title, "Desk: buy AAPL? Tap by 10:25 ET")
        title.encode("ascii")
        self.assertEqual(body.split("\n")[:6], ["Buy at: 101.50 (14 shares)", "Stop loss at: 100.00", "Sell at: 104.50",
                                                "Success likelihood: 41%", "", "Key points:"])
        self.assertEqual(len(body.split("\n")), 6 + len(pts))
        self.assertLessEqual(len(pts), 4)
        _, body = desk.alert_text(self.order, {"ok": False, "why": "x"}, [], expires, CFG)
        self.assertIn("Success likelihood: not available", body)

    def test_chase_limit_is_where_reward_risk_hits_the_minimum(self):
        lim = desk.chase_limit(100.0, 104.5, CFG)
        self.assertEqual(lim, 102.25)
        self.assertEqual(rules.risk_check(14, lim, 100.0, 104.5, CFG, {"trades": 0, "risk_used": 0, "equity": 5000}), [])
        self.assertTrue(rules.risk_check(14, lim + 0.01, 100.0, 104.5, CFG, {"trades": 0, "risk_used": 0, "equity": 5000}))


class Replies(unittest.TestCase):
    def test_parse_reply(self):
        self.assertEqual(desk.parse_reply("approve TSLA 20261112"), ("approve", "TSLA", "2026-11-12"))
        self.assertEqual(desk.parse_reply(" Skip tsla 20261112\n"), ("skip", "TSLA", "2026-11-12"))
        for bad in ("", "approve", "buy TSLA 20261112", "approve TSLA", "approve TSLA 2026-11-12", "approve TSLA 20261112 now"):
            self.assertIsNone(desk.parse_reply(bad))

    def test_parse_poll(self):
        text = "\n".join([
            json.dumps({"id": "b", "time": 20, "event": "message", "topic": "t-ok", "message": "skip MSFT 20261110"}),
            json.dumps({"id": "k", "time": 5, "event": "keepalive"}),
            "not json",
            json.dumps({"id": "a", "time": 10, "event": "message", "topic": "t-ok", "message": "approve AAPL 20261110"}),
        ])
        self.assertEqual([e["id"] for e in desk.parse_poll(text)], ["a", "b"])
        self.assertEqual(desk.parse_poll(""), [])


class Log(unittest.TestCase):
    def row(self, sym, decision, entry=100.0, stop=99.0, target=102.0, triggered="10:15"):
        o = {"symbol": sym, "qty": 10, "ref": entry, "entry": entry, "stop": stop, "target": target,
             "risk_usd": 10.0, "triggered": triggered}
        r = desk.log_row("2026-11-10", o, tmres(), 1.2, 0.1, ["Apple does a thing"])
        r["decision"] = decision
        return r

    def bars(self, rows, until="15:55"):
        t = datetime(2026, 11, 10, 9, 30, tzinfo=ET)
        out, i = [], 0
        while t.strftime("%H:%M") < until:
            o, h, lo, c = rows[i] if i < len(rows) else rows[-1]
            out.append({"t": t, "o": o, "h": h, "l": lo, "c": c, "v": 1})
            t += timedelta(minutes=5)
            i += 1
        return out

    def test_round_trip(self):
        path = Path(tempfile.mkdtemp()) / "log" / "alerts.csv"
        rows = [self.row("AAPL", "bought"), self.row("MSFT", "expired")]
        desk.save_log(path, rows)
        back = desk.load_log(path)
        self.assertEqual([r["symbol"] for r in back], ["AAPL", "MSFT"])
        self.assertEqual((back[0]["tm_target_pct"], back[0]["tm_green_pct"], back[0]["hist_trades"], back[0]["headlines"]),
                         ("24.0", "41.0", "20", "1"))
        self.assertEqual(desk.load_log(path.parent / "missing.csv"), [])

    def test_score(self):
        flat = [(100, 100.2, 99.8, 100)] * 9                              # 9:30 to 10:15
        up = self.bars(flat + [(100, 101, 99.5, 101), (101, 102.5, 100.5, 102)])
        self.assertEqual(desk.score_row(self.row("AAPL", "bought"), up, CFG), ("target", 2.0))
        down = self.bars(flat + [(100, 100.1, 98.5, 99)])
        self.assertEqual(desk.score_row(self.row("AAPL", "skipped"), down, CFG), ("stop", -1.0))
        drift = self.bars(flat + [(100, 100.6, 99.6, 100.5)])
        self.assertEqual(desk.score_row(self.row("AAPL", "expired"), drift, CFG), ("flat", 0.5))
        self.assertIsNone(desk.score_row(self.row("AAPL", "bought"), self.bars(flat, until="12:00"), CFG))  # day not over

    def test_compare(self):
        rows = [self.row("AAPL", "bought"), self.row("MSFT", "expired"), self.row("NVDA", "skipped"), self.row("AMD", "pending")]
        for r, (oc, R) in zip(rows, (("target", "2.000"), ("stop", "-1.000"), ("stop", "-1.000"))):
            r["outcome"], r["outcome_r"] = oc, R
        md = desk.compare(rows)
        self.assertIn("| Every alert | 3 | 1 | 2 | 0 | +0.00 |", md)
        self.assertIn("| You approved (bought) | 1 | 1 | 0 | 0 | +2.00 |", md)
        self.assertIn("| Not bought | 2 | 0 | 2 | 0 | -1.00 |", md)
        self.assertIn("1 alert(s) not scored yet", md)
        self.assertIn("mostly noise", md)
        self.assertIn("| Every alert | 0 |", desk.compare([]))
        self.assertNotIn("not sent", md)
        short, quiet = self.row("TSLA", "bought"), self.row("AMD", "filtered")
        short["side"] = "short"
        short["outcome"], short["outcome_r"], quiet["outcome"], quiet["outcome_r"] = "target", "2.000", "stop", "-1.000"
        md = desk.compare(rows[:3] + [short, quiet])
        self.assertIn("| Every alert | 4 | 2 | 2 | 0 | +0.50 |", md)          # the unsent trigger is not an alert
        self.assertIn("| Alerts to short | 1 | 1 | 0 | 0 | +2.00 |", md)
        self.assertIn("| Triggers not sent (weak record) | 1 | 0 | 1 | 0 | -1.00 |", md)


if __name__ == "__main__":
    unittest.main()
