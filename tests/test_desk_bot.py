"""The bot's decisions and its handling of the broker, with a fake Alpaca. No network."""

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
import desk  # noqa: E402
import desk_bot  # noqa: E402
import rules  # noqa: E402
from rules import ET  # noqa: E402

CFG = json.loads((ROOT / "config" / "desk_bot.json").read_text())
DAY = "2026-11-10"


def at(hm: str, sec: int = 0) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 11, 10, int(h), int(m), sec, tzinfo=ET)


def bar(hm, o, h, lo, c, v=1000):
    return {"t": at(hm), "o": o, "h": h, "l": lo, "c": c, "v": v}


def morning(breakout_at="10:10", base=100.0, close=101.5, vol=1000, until="10:10"):
    """Range 99.00 to 101.00 (stop 100.00), flat bars, then a close above at `breakout_at`."""
    rows = [bar("09:30", base, base + 1, base - 1, base, vol), bar("09:35", base, base + 0.8, base - 0.5, base + 0.2, vol),
            bar("09:40", base + 0.2, base + 0.9, base - 0.2, base + 0.5, vol)]
    t = at("09:45")
    while t <= at(until):
        hm = t.strftime("%H:%M")
        if hm == breakout_at:
            rows.append(bar(hm, base + 0.6, close + 0.1, base + 0.5, close, vol))
        elif hm > breakout_at:
            rows.append(bar(hm, close, close + 0.2, close - 0.2, close, vol))
        else:
            rows.append(bar(hm, base + 0.5, base + 0.9, base + 0.3, base + 0.6, vol))
        t += timedelta(minutes=5)
    return rows


def state(**kw):
    s = {"equity": 5000.0, "cash": 5000.0, "deployed": 0.0, "trades": 0, "risk_used": 0.0,
         "symbols": set(), "day_pnl": 0.0}
    s.update(kw)
    return s


class Decide(unittest.TestCase):
    def test_fresh_breakout_becomes_one_bracket_order(self):
        r = desk_bot.decide(CFG, {"AAPL": morning()}, {"AAPL": 101.5}, state(), at("10:15", 20))
        self.assertEqual(len(r["orders"]), 1)
        o = r["orders"][0]
        self.assertEqual((o["symbol"], o["stop"], o["target"]), ("AAPL", 100.0, 104.5))
        self.assertEqual(o["qty"], 14)                                   # $1,500 cap / 101.50
        req = o["request"]
        self.assertEqual(req["order_class"], "bracket")
        self.assertEqual(req["stop_loss"], {"stop_price": "100.00"})
        self.assertEqual(req["take_profit"], {"limit_price": "104.50"})
        self.assertEqual(req["client_order_id"], "desk-20261110-AAPL-r2100")
        self.assertEqual(desk_bot.parse_tag(req["client_order_id"]), {"day": DAY, "symbol": "AAPL", "risk": 21.0})

    def test_bar_still_forming_is_not_a_trigger(self):
        r = desk_bot.decide(CFG, {"AAPL": morning()}, {}, state(), at("10:13"))
        self.assertEqual(r["orders"], [])

    def test_old_trigger_is_skipped(self):
        r = desk_bot.decide(CFG, {"AAPL": morning(until="10:20")}, {}, state(), at("10:25", 20))
        self.assertEqual(r["orders"], [])

    def test_already_traded_today_is_skipped(self):
        r = desk_bot.decide(CFG, {"AAPL": morning()}, {}, state(symbols={"AAPL"}, trades=1), at("10:15", 20))
        self.assertEqual(r["orders"], [])

    def test_breakout_after_the_window_is_ignored(self):
        r = desk_bot.decide(MORNING_ONLY, {"AAPL": morning(breakout_at="10:30", until="10:30")}, {}, state(), at("10:35", 20))
        self.assertEqual(r["orders"], [])

    def test_price_that_ran_away_is_blocked(self):
        r = desk_bot.decide(CFG, {"AAPL": morning()}, {"AAPL": 103.2}, state(), at("10:15", 20))
        self.assertEqual(r["orders"], [])
        self.assertTrue(any("reward:risk" in n for n in r["notes"]))

    def test_most_traded_first_and_three_a_day(self):
        bars = {s: morning(vol=v) for s, v in (("AAPL", 100), ("MSFT", 900), ("NVDA", 500), ("AMZN", 300))}
        r = desk_bot.decide(CFG, bars, {}, state(), at("10:15", 20))
        self.assertEqual([o["symbol"] for o in r["orders"]], ["MSFT", "NVDA", "AMZN"])
        self.assertTrue(r["done"])
        spent = sum(o["qty"] * o["ref"] for o in r["orders"])
        self.assertLessEqual(spent, 5000 * 0.9)                           # the cash buffer survives

    def test_one_slot_left(self):
        bars = {s: morning() for s in ("AAPL", "MSFT")}
        r = desk_bot.decide(CFG, bars, {}, state(trades=2, risk_used=40.0, cash=2000.0, deployed=3000.0), at("10:15", 20))
        self.assertEqual(len(r["orders"]), 1)

    def test_daily_loss_and_halt_block_everything(self):
        for s in (state(day_pnl=-160.0, equity=4840.0), state(equity=4200.0, cash=4200.0)):
            r = desk_bot.decide(CFG, {"AAPL": morning()}, {}, s, at("10:15", 20))
            self.assertEqual(r["orders"], [])

    def test_never_spends_more_than_the_budget_in_a_big_account(self):
        r = desk_bot.decide(CFG, {"AAPL": morning()}, {}, state(equity=100000.0, cash=97000.0, deployed=3000.0), at("10:15", 20))
        self.assertEqual(r["orders"][0]["qty"], 14)
        r = desk_bot.decide(CFG, {"AAPL": morning()}, {}, state(equity=100000.0, cash=95400.0, deployed=4600.0), at("10:15", 20))
        self.assertEqual(r["orders"], [])


class Timing(unittest.TestCase):
    open_dt, close_dt = at("09:30"), at("16:00")

    def test_run_gate(self):
        self.assertIsNone(desk_bot.run_gate(CFG, at("09:43"), self.open_dt, self.close_dt))
        self.assertIsNone(desk_bot.run_gate(CFG, at("10:05"), self.open_dt, self.close_dt))       # a late start still works
        self.assertIn("too early", desk_bot.run_gate(CFG, at("08:40"), self.open_dt, self.close_dt))
        self.assertIn("too early", desk_bot.run_gate(CFG, at("09:17"), self.open_dt, self.close_dt))  # would run past GitHub's 6-hour limit
        self.assertIsNone(desk_bot.run_gate(CFG, at("09:18"), self.open_dt, self.close_dt))           # starts early and waits for the open
        self.assertIsNone(desk_bot.run_gate(CFG, at("10:40"), self.open_dt, self.close_dt))    # the window runs to 15:00
        self.assertIn("past", desk_bot.run_gate(CFG, at("15:01"), self.open_dt, self.close_dt))
        self.assertIn("early close", desk_bot.run_gate(CFG, at("09:43"), self.open_dt, at("13:00")))

    def test_next_check(self):
        self.assertEqual(desk_bot.next_check(CFG, at("09:41")), at("09:50", 20))    # first bar after the range
        self.assertEqual(desk_bot.next_check(CFG, at("09:52")), at("09:55", 20))
        self.assertEqual(desk_bot.next_check(CFG, at("10:25", 30)), at("10:30", 20))
        self.assertIsNone(desk_bot.next_check(MORNING_ONLY, at("10:30", 30)))
        self.assertEqual(desk_bot.next_check(CFG, at("14:55", 30)), at("15:00", 20))
        self.assertIsNone(desk_bot.next_check(CFG, at("15:00", 30)))

    def test_flatten_plan(self):
        self.assertEqual(desk_bot.flatten_plan(CFG, at("15:45"), self.close_dt), ("go", at("15:52")))
        self.assertEqual(desk_bot.flatten_plan(CFG, at("15:58"), self.close_dt)[0], "go")           # late: act at once
        self.assertEqual(desk_bot.flatten_plan(CFG, at("14:36"), self.close_dt)[0], "early")        # an early slot
        self.assertEqual(desk_bot.flatten_plan(CFG, at("14:45"), self.close_dt), ("go", at("15:52")))  # starts early and waits
        self.assertEqual(desk_bot.flatten_plan(CFG, at("16:42"), self.close_dt)[0], "missed")
        self.assertEqual(desk_bot.flatten_plan(CFG, at("12:40"), at("13:00")), ("go", at("12:52")))


class FakeAlpaca:
    def __init__(self, bars=None, positions=None, orders=None, equity=5000.0, cash=5000.0, stuck=False):
        self._bars, self._positions, self._orders = bars or {}, positions or [], orders or []
        self.equity, self.cash, self.stuck = equity, cash, stuck
        self.submitted, self.calls, self.latest = [], [], {}

    def account(self):
        return {"equity": str(self.equity), "cash": str(self.cash), "last_equity": "5000"}

    def calendar(self, day):
        return [{"date": day, "open": "09:30", "close": "16:00"}]

    def positions(self):
        return list(self._positions)

    def orders(self, after, status="all"):
        return list(self._orders)

    def bars(self, symbols, start, end, feed, **kw):
        return {s: self._bars.get(s, []) for s in symbols}

    def latest_trades(self, symbols, feed):
        return {s: self.latest[s] for s in symbols if s in self.latest}

    def news(self, symbol, start, limit=10):
        return [{"headline": "Chip stocks rally after Jensen Huang remarks", "symbols": ["NVDA", "AMD", symbol]},
                {"headline": "Apple unveils a thing", "symbols": ["AAPL"]}]

    def submit(self, order):
        self.submitted.append(order)
        return {"id": "x"}

    def close_all_positions(self):
        self.calls.append("close_all")
        if not self.stuck:
            self._positions = []

    def cancel_all_orders(self):
        self.calls.append("cancel_all")

    def close_position(self, symbol):
        self.calls.append(f"close {symbol}")
        self._positions = [p for p in self._positions if p["symbol"] != symbol]


def order(cid, symbol, side="buy", status="filled", filled="14", qty="14"):
    return {"client_order_id": cid, "symbol": symbol, "side": side, "status": status, "filled_qty": filled, "qty": qty}


def session(since=0):
    tmp = tempfile.mkdtemp()
    return desk_bot.Session(DAY, Path(tmp) / "alerts.csv", since=since)


def tap(action, symbol, day=DAY, id_="m1"):
    return {"id": id_, "time": 1, "message": desk.reply_text(action, symbol, day)}


class Broker(unittest.TestCase):
    def setUp(self):
        self._sleep, self._notify = desk_bot._time.sleep, desk_bot.notify
        desk_bot._time.sleep = lambda s: None
        self.alerts = []

        def fake_notify(title, body="", actions="", priority=""):
            self.alerts.append({"title": title, "body": body, "actions": actions})
            return True
        desk_bot.notify = fake_notify

    def tearDown(self):
        desk_bot._time.sleep, desk_bot.notify = self._sleep, self._notify

    def titles(self):
        return [a["title"] for a in self.alerts]

    # --- the alert

    def test_cycle_asks_and_buys_nothing(self):
        api, sess = FakeAlpaca(bars={"AAPL": morning()}), session()
        r = desk_bot.cycle(api, CFG, at("10:15", 20), sess)
        self.assertEqual(api.submitted, [])                               # no tap, no trade
        self.assertEqual(self.titles(), ["Desk: buy AAPL? Tap by 10:25 ET"])
        self.assertIn("approve AAPL 20261110", self.alerts[0]["actions"])
        self.assertIn("skip AAPL 20261110", self.alerts[0]["actions"])
        body = self.alerts[0]["body"]
        self.assertTrue(body.startswith("Buy at: 101.50 (14 shares)\nStop loss at: 100.00\nSell at: 104.50\nSuccess likelihood: "))
        self.assertIn("- News: Apple unveils a thing", body)             # the one that names the company
        self.assertNotIn("Jensen", body)                                  # only tagged with the ticker
        self.assertFalse(r["done"])
        self.assertEqual(set(sess.pending), {"AAPL"})
        row = desk.load_log(sess.log_path)[0]
        self.assertEqual((row["symbol"], row["decision"], row["entry"], row["triggered"]), ("AAPL", "pending", "101.50", "10:15"))

    def test_dry_run_sends_nothing(self):
        api, sess = FakeAlpaca(bars={"AAPL": morning()}), session()
        desk_bot.cycle(api, CFG, at("10:15", 20), sess, dry_run=True)
        self.assertEqual((api.submitted, self.alerts, sess.rows, sess.pending), ([], [], [], {}))

    def test_asks_once_per_stock_and_caps_the_alerts(self):
        api, sess = FakeAlpaca(bars={"AAPL": morning()}), session()
        desk_bot.cycle(api, CFG, at("10:15", 20), sess)
        desk_bot.cycle(api, CFG, at("10:15", 40), sess)
        self.assertEqual(len(self.alerts), 1)
        cfg = copy.deepcopy(CFG)
        cfg["desk"]["max_alerts_per_day"] = 2
        api = FakeAlpaca(bars={s: morning() for s in ("AAPL", "MSFT", "NVDA")})
        sess = session()
        desk_bot.cycle(api, cfg, at("10:15", 20), sess)
        self.assertEqual(len(sess.rows), 2)

    def test_alert_that_cannot_be_sent_is_not_pending(self):
        desk_bot.notify = lambda *a, **k: False
        api, sess = FakeAlpaca(bars={"AAPL": morning()}), session()
        desk_bot.cycle(api, CFG, at("10:15", 20), sess)
        self.assertEqual(sess.pending, {})
        self.assertEqual(sess.rows[0]["decision"], "not sent")

    # --- the tap

    def asked(self, latest=101.6, **kw):
        api, sess = FakeAlpaca(bars={"AAPL": morning()}, **kw), session()
        desk_bot.cycle(api, CFG, at("10:15", 20), sess)
        api.latest = {"AAPL": latest} if latest is not None else {}
        self.alerts.clear()
        return api, sess

    def test_approve_buys_with_a_bracket(self):
        api, sess = self.asked()
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("approve", "AAPL")])
        self.assertEqual(len(api.submitted), 1)
        req = api.submitted[0]
        self.assertEqual((req["symbol"], req["qty"], req["order_class"]), ("AAPL", "14", "bracket"))
        self.assertEqual((req["stop_loss"], req["take_profit"]), ({"stop_price": "100.00"}, {"limit_price": "104.50"}))
        self.assertEqual(desk_bot.parse_tag(req["client_order_id"])["symbol"], "AAPL")
        self.assertEqual(self.titles(), ["Desk bought AAPL"])
        self.assertEqual((sess.rows[0]["decision"], sess.rows[0]["buy_ref"], sess.pending), ("bought", "101.60", {}))

    def test_one_tap_buys_once(self):
        api, sess = self.asked()
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("approve", "AAPL"), tap("approve", "AAPL")])
        desk_bot.handle_replies(api, CFG, sess, at("10:19"), [tap("approve", "AAPL", id_="m2")])
        self.assertEqual(len(api.submitted), 1)

    def test_skip_buys_nothing(self):
        api, sess = self.asked()
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("skip", "AAPL")])
        desk_bot.handle_replies(api, CFG, sess, at("10:19"), [tap("approve", "AAPL", id_="m2")])   # too late to change your mind
        self.assertEqual(api.submitted, [])
        self.assertEqual(sess.rows[0]["decision"], "skipped")

    def test_taps_for_other_days_or_stocks_are_ignored(self):
        api, sess = self.asked()
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [
            tap("approve", "AAPL", day="2026-11-09", id_="a"), tap("approve", "MSFT", id_="b"),
            {"id": "c", "time": 1, "message": "buy everything"}])
        self.assertEqual((api.submitted, sess.rows[0]["decision"]), ([], "pending"))

    def test_late_approval_is_refused(self):
        api, sess = self.asked()
        desk_bot.handle_replies(api, CFG, sess, at("10:26"), [tap("approve", "AAPL")])
        self.assertEqual(api.submitted, [])
        self.assertEqual(sess.rows[0]["decision"], "refused")
        self.assertEqual(self.titles(), ["Desk did NOT buy AAPL"])

    def test_approval_after_the_price_ran_is_refused(self):
        api, sess = self.asked(latest=102.4)                              # over the 102.25 limit
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("approve", "AAPL")])
        self.assertEqual(api.submitted, [])
        self.assertIn("reward:risk", sess.rows[0]["note"])

    def test_approval_after_the_stop_was_hit_is_refused(self):
        api, sess = self.asked(latest=99.9)
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("approve", "AAPL")])
        self.assertEqual(api.submitted, [])
        self.assertIn("stop", sess.rows[0]["note"])

    def test_approval_without_a_price_is_refused(self):
        api, sess = self.asked(latest=None)
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("approve", "AAPL")])
        self.assertEqual(api.submitted, [])

    def test_approval_still_obeys_the_daily_limits(self):
        api, sess = self.asked()
        api._orders = [order(f"desk-20261110-{s}-r2000", s) for s in ("MSFT", "NVDA", "AMZN")]
        desk_bot.handle_replies(api, CFG, sess, at("10:18"), [tap("approve", "AAPL")])
        self.assertEqual(api.submitted, [])
        self.assertIn("trades already", sess.rows[0]["note"])

    def test_no_tap_expires(self):
        api, sess = self.asked()
        desk_bot.expire(sess, at("10:24"))
        self.assertEqual(sess.rows[0]["decision"], "pending")
        desk_bot.expire(sess, at("10:25", 30))
        self.assertEqual((sess.rows[0]["decision"], sess.pending, api.submitted), ("expired", {}, []))

    def test_second_run_the_same_day_does_not_ask_again(self):
        api = FakeAlpaca(bars={"AAPL": morning()}, orders=[order("desk-20261110-AAPL-r2100", "AAPL")],
                         positions=[{"symbol": "AAPL", "qty": "14", "market_value": "1421"}], cash=3579.0)
        desk_bot.cycle(api, CFG, at("10:15", 20), session())
        self.assertEqual((api.submitted, self.alerts), ([], []))

    def test_state_counts_todays_entries_only(self):
        api = FakeAlpaca(orders=[
            order("desk-20261110-AAPL-r2100", "AAPL"),
            order("desk-20261110-MSFT-r1800", "MSFT", status="canceled", filled="0"),   # never filled: not a trade
            order("desk-20261109-NVDA-r3000", "NVDA"),                                  # yesterday
            order("3f1c-broker-made-id", "AAPL", side="sell"),                         # a flatten sell
        ])
        s = desk_bot.read_state(api, CFG, DAY)
        self.assertEqual((s["trades"], s["risk_used"]), (1, 21.0))
        self.assertIsNone(s["wrong"])

    def test_refuses_the_rotation_bots_account(self):
        api = FakeAlpaca(bars={"AAPL": morning()}, positions=[{"symbol": "XLK", "qty": "10", "market_value": "2300"}])
        with self.assertRaises(RuntimeError):
            desk_bot.cycle(api, CFG, at("10:15", 20), session())
        self.assertEqual(api.submitted, [])
        api = FakeAlpaca(orders=[order("rot-20261101-EEM", "EEM")])
        self.assertIn("another bot", desk_bot.read_state(api, CFG, DAY)["wrong"])

    def test_flatten(self):
        api = FakeAlpaca(positions=[{"symbol": "AAPL", "qty": "14", "market_value": "1421"}])
        self.assertTrue(desk_bot.flatten_now(api, pause=0))
        self.assertEqual(api.calls, ["close_all"])

    def test_flatten_falls_back_to_one_by_one(self):
        api = FakeAlpaca(positions=[{"symbol": "AAPL", "qty": "14", "market_value": "1421"}], stuck=True)
        self.assertTrue(desk_bot.flatten_now(api, pause=0))
        self.assertEqual(api.calls, ["close_all", "cancel_all", "close AAPL"])

    def test_report(self):
        fills = [
            {"transaction_time": "2026-11-10T15:15:21.123456789Z", "symbol": "AAPL", "side": "buy", "qty": "14", "price": "101.50"},
            {"transaction_time": "2026-11-10T16:40:00Z", "symbol": "AAPL", "side": "sell", "qty": "14", "price": "104.50"},
            {"transaction_time": "2026-11-11T15:00:00Z", "symbol": "MSFT", "side": "buy", "qty": "3", "price": "400.00"},
            {"transaction_time": "2026-11-11T15:30:00Z", "symbol": "MSFT", "side": "sell", "qty": "3", "price": "398.00"},
            {"transaction_time": "2026-11-12T15:00:00Z", "symbol": "NVDA", "side": "buy", "qty": "5", "price": "200.00"},   # still open
        ]
        orders = [order("desk-20261110-AAPL-r2100", "AAPL"), order("desk-20261111-MSFT-r600", "MSFT")]
        md, rows = desk_bot.build_report(CFG, fills, orders, 5036.0)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["pnl"], 42.0)
        self.assertAlmostEqual(rows[0]["R"], 2.0)
        self.assertAlmostEqual(rows[1]["R"], -1.0)
        self.assertIn("Completed trades: **2**", md)
        self.assertIn("+36.00 USD", md)


MORNING_ONLY = {**CFG, "no_entries_after": "10:30"}


class Clock:
    def __init__(self, start):
        self.now = start

    def sleep(self, seconds):
        self.now += timedelta(seconds=max(seconds, 0))


class Morning(unittest.TestCase):
    """The whole morning loop on a fake clock: no real waiting, no network."""

    def run_morning(self, bars, taps):
        """taps: [(HH:MM, message)] -> (api, alert titles, log rows, the time the loop ended)."""
        import os
        import types
        clock = Clock(at("09:41"))
        api = FakeAlpaca(bars=bars)
        api.latest = {s: rows[-1]["c"] for s, rows in bars.items()}
        alerts = []
        tmp = Path(tempfile.mkdtemp())

        def fake_poll(since):
            return [{"id": f"m{i}", "time": i, "message": msg} for i, (hm, msg) in enumerate(taps) if clock.now >= at(hm)]

        def fake_notify(title, body="", actions="", priority=""):
            alerts.append(title)
            return True

        saved = {k: getattr(desk_bot, k) for k in ("now_et", "connect", "notify", "poll_replies", "STOP_FILE", "LOG", "summary")}
        saved_sleep, saved_topic = desk_bot._time.sleep, os.environ.get("NTFY_TOPIC")
        desk_bot.now_et, desk_bot.connect = (lambda: clock.now), (lambda: api)
        desk_bot.notify, desk_bot.poll_replies, desk_bot.summary = fake_notify, fake_poll, (lambda md: None)
        desk_bot.STOP_FILE, desk_bot.LOG = tmp / "STOP", tmp / "alerts.csv"
        desk_bot._time.sleep = clock.sleep
        os.environ["NTFY_TOPIC"] = "t"
        try:
            code = desk_bot.cmd_run(types.SimpleNamespace(dry_run=False, once=False))
        finally:
            for k, v in saved.items():
                setattr(desk_bot, k, v)
            desk_bot._time.sleep = saved_sleep
            if saved_topic is None:
                del os.environ["NTFY_TOPIC"]
            else:
                os.environ["NTFY_TOPIC"] = saved_topic
        self.assertEqual(code, 0)
        return api, alerts, desk.load_log(tmp / "alerts.csv"), clock.now

    def test_approve_in_time_buys_and_the_morning_ends_on_schedule(self):
        api, alerts, rows, end = self.run_morning({"AAPL": morning(until="10:30")},
                                                  [("10:17", "approve AAPL 20261110")])
        self.assertEqual([o["symbol"] for o in api.submitted], ["AAPL"])
        self.assertEqual(alerts, ["Desk: buy AAPL? Tap by 10:25 ET", "Desk bought AAPL"])
        self.assertEqual(rows[0]["decision"], "bought")
        self.assertLess(rows[0]["decided"], "10:17:30")                   # heard within one poll
        self.assertGreater(end, at("15:00"))                               # it keeps watching until the last entry time
        self.assertLess(end, at("15:01"))

    def test_no_tap_means_no_trade_and_a_late_alert_still_gets_its_ten_minutes(self):
        bars = {"AAPL": morning(until="10:30"), "MSFT": morning(breakout_at="10:25", until="10:30")}
        api, alerts, rows, end = self.run_morning(bars, [("10:44", "approve MSFT 20261110")])   # after the limit
        self.assertEqual(api.submitted, [])
        self.assertEqual({r["symbol"]: r["decision"] for r in rows}, {"AAPL": "expired", "MSFT": "expired"})
        self.assertEqual(alerts, ["Desk: buy AAPL? Tap by 10:25 ET", "Desk: buy MSFT? Tap by 10:40 ET"])
        self.assertLess(end, at("15:01"))

    def test_flatten_started_early_waits_and_sells_a_later_buy(self):
        import types
        clock = Clock(at("14:50"))
        api = FakeAlpaca()                                                # nothing held when the run starts
        alerts = []
        saved = desk_bot.now_et, desk_bot.connect, desk_bot.notify, desk_bot._time.sleep

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.now >= at("15:05") and not api._orders:               # a buy approved after this run started
                api._orders = [order("desk-20261110-AAPL-r2100", "AAPL")]
                api._positions = [{"symbol": "AAPL", "qty": "14", "market_value": "1421"}]
        desk_bot.now_et, desk_bot.connect = (lambda: clock.now), (lambda: api)
        desk_bot.notify = lambda title, body="", **k: alerts.append(title) or True
        desk_bot._time.sleep = sleep
        try:
            code = desk_bot.cmd_flatten(types.SimpleNamespace(now=False))
        finally:
            desk_bot.now_et, desk_bot.connect, desk_bot.notify, desk_bot._time.sleep = saved
        self.assertEqual(code, 0)
        self.assertGreaterEqual(clock.now, at("15:52"))
        self.assertEqual(alerts, ["Desk bot: flat for the day"])

    def test_stop_file_sends_nothing(self):
        import types
        api = FakeAlpaca(bars={"AAPL": morning()})
        saved = desk_bot.connect, desk_bot.STOP_FILE
        desk_bot.connect = lambda: api
        desk_bot.STOP_FILE = ROOT / "STOP"                                # the repo ships switched off
        try:
            self.assertTrue(desk_bot.STOP_FILE.exists())
            self.assertEqual(desk_bot.cmd_run(types.SimpleNamespace(dry_run=False, once=False)), 0)
        finally:
            desk_bot.connect, desk_bot.STOP_FILE = saved
        self.assertEqual(api.submitted, [])


class Config(unittest.TestCase):
    def test_shipped_config_is_paper_long_only(self):
        cfg = desk_bot.load_cfg()
        self.assertIs(cfg["paper"], True)
        self.assertEqual(desk_bot.TRADING_URL, "https://paper-api.alpaca.markets")
        self.assertEqual(len(cfg["universe"]), len(set(cfg["universe"])))
        self.assertLessEqual(cfg["max_trades_per_day"] * cfg["max_loss_per_trade_usd"], cfg["max_daily_loss_usd"])
        self.assertLessEqual(cfg["max_trades_per_day"] * cfg["max_position_pct"], 100 - cfg["cash_buffer_pct"])

    def test_buttons_post_to_the_reply_topic(self):
        import os
        old = os.environ.get("NTFY_TOPIC")
        os.environ["NTFY_TOPIC"] = "evan-topic"
        try:
            b = desk_bot.buttons("TSLA", DAY)
            v = desk_bot.buttons("TSLA", DAY, "view")
        finally:
            if old is None:
                del os.environ["NTFY_TOPIC"]
            else:
                os.environ["NTFY_TOPIC"] = old
        self.assertEqual(v, "view, Approve buy, https://ntfy.sh/evan-topic-ok/publish?message=approve+TSLA+20261110, clear=true; "
                            "view, Skip, https://ntfy.sh/evan-topic-ok/publish?message=skip+TSLA+20261110, clear=true")
        self.assertEqual(b, "http, Approve buy, https://ntfy.sh/evan-topic-ok, method=POST, body=approve TSLA 20261110, clear=true; "
                            "http, Skip, https://ntfy.sh/evan-topic-ok, method=POST, body=skip TSLA 20261110, clear=true")

    def test_live_config_is_refused(self):
        bad = copy.deepcopy(CFG)
        bad["paper"] = False
        p = ROOT / "tests" / "_bad.json"
        p.write_text(json.dumps(bad))
        try:
            with self.assertRaises(SystemExit):
                desk_bot.load_cfg(p)
        finally:
            p.unlink()


if __name__ == "__main__":
    unittest.main()
