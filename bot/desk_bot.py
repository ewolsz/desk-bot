#!/usr/bin/env python3
"""Desk bot: the Day Trader skill's opening-range breakout on an Alpaca PAPER
account, where nothing is bought until a person taps Approve on their phone.

    python3 bot/desk_bot.py status               # account, positions, today's trades
    python3 bot/desk_bot.py test-alert           # send a sample alert and wait for a tap (no orders)
    python3 bot/desk_bot.py run                  # the trading session
    python3 bot/desk_bot.py run --once --dry-run # one look right now, nothing sent
    python3 bot/desk_bot.py flatten              # out of everything before the close
    python3 bot/desk_bot.py report               # results so far, and approved vs every alert

What it does each trading day:
  1. From 9:45 to 10:30 New York time it checks ~50 large stocks after every
     5-minute bar closes. A stock triggers on its first 5-minute close above the
     first 15 minutes' high. Stop (range midpoint) and target (2x the risk) are
     fixed first, the size is the smallest of four limits, and every risk check
     must pass. This part is the day bot, unchanged.
  2. Instead of buying, it runs the skill's Time Machine on the plan and sends a
     phone alert: the plan, the odds, the lines for and against, and two
     buttons, Approve and Skip.
  3. A tap on Approve within the time limit makes it look again: fresh price,
     fresh account, every risk check a second time. Only then does it send one
     bracket order (buy + stop-loss + take-profit). No tap, no trade.
  4. Every alert is written to log/alerts.csv, tapped or not, and scored later.
  5. `flatten` sells whatever is still open a few minutes before the close.

Safety, in order:
  - Paper only. The trading URL is Alpaca's paper endpoint and there is no
    setting that changes it. Going live means editing this file on purpose.
  - No order without an Approve tap for that stock, that day, inside the limit.
  - Kill switch: while a file called STOP sits in the top folder, no alerts and
    no orders are sent. The bot ships switched off: delete STOP to switch it on.
  - Wrong-account guard: it refuses to run in an account that holds or traded
    anything outside its own stock list (for example another bot's account).
  - Every order passes rules.risk_check twice; a block is final for that order.
  - Alpaca is its memory for trades. Orders are tagged desk-YYYYMMDD-SYMBOL-rCENTS.

The Approve button posts to a second ntfy topic (your topic + "-ok"). Anyone who
knows the topic name could approve a PAPER trade that already passed every risk
check. That is acceptable on paper and not acceptable with real money.

Keys: environment variables ALPACA_KEY and ALPACA_SECRET (the desk bot's OWN
paper account). Phone alerts and approvals: NTFY_TOPIC (required for `run`).

Not financial advice. Most people who day trade lose money. This breakout rule
failed its 2019-2022 backtest: the trigger alone showed no edge.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import desk  # noqa: E402
import rules  # noqa: E402
from rules import ET, hhmm  # noqa: E402

TRADING_URL = "https://paper-api.alpaca.markets"     # paper. Do not change casually.
DATA_URL = "https://data.alpaca.markets"
CONFIG = ROOT / "config" / "desk_bot.json"
STOP_FILE = ROOT / "STOP"
LOG = ROOT / "log" / "alerts.csv"
POLL_SECONDS = 10           # how often to look for an Approve tap while one is awaited
BENCH = "SPY"
EARLY_START_MINUTES = 12     # a run may start this long before the open and wait; earlier ones exit
EARLY_FLATTEN_MINUTES = 75   # a flatten run may start this long before it acts and wait
TAG = "desk"
SETTLE_SECONDS = 20          # wait this long after a bar closes before reading it
DEAD = {"canceled", "rejected", "expired"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def load_cfg(path: Path = CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text())
    if cfg.get("paper") is not True or cfg.get("allow_short") or cfg.get("allow_margin") or cfg.get("allow_options"):
        sys.exit("config: this bot is paper-only, long-only, no margin, no options")
    return cfg


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "." in s:                                   # trim nanoseconds to microseconds
        head, rest = s.split(".", 1)
        tz_at = max(rest.find("+"), rest.find("-"))
        frac, tz = (rest[:tz_at], rest[tz_at:]) if tz_at >= 0 else (rest, "")
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).astimezone(ET)


def parse_bar(raw: dict) -> dict:
    return {"t": parse_ts(raw["t"]), "o": float(raw["o"]), "h": float(raw["h"]),
            "l": float(raw["l"]), "c": float(raw["c"]), "v": float(raw.get("v") or 0)}


def notify(title: str, body: str = "", actions: str = "", priority: str = "") -> bool:
    print(f"[alert] {title}" + (f"\n{body}" if body else ""), flush=True)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "replace").decode()}
    if actions:
        headers["Actions"] = actions
    if priority:
        headers["Priority"] = priority
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=(body or title).encode(),
                                     headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:                          # an alert must never stop the bot
        print(f"[alert not sent: {e}]", flush=True)
        return False


def reply_topic() -> str:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    return f"{topic}-ok" if topic else ""


def buttons(symbol: str, day: str, style: str = "http") -> str:
    """The Approve and Skip buttons: each sends one short line to the reply topic.
    "http" posts it straight from the notification. "view" opens a ntfy web link
    that does the same, for phones where http buttons do not work."""
    base = f"https://ntfy.sh/{reply_topic()}"
    out = []
    for label, act in (("Approve buy", "approve"), ("Skip", "skip")):
        text = desk.reply_text(act, symbol, day)
        if style == "view":
            out.append(f"view, {label}, {base}/publish?message={urllib.parse.quote_plus(text)}, clear=true")
        else:
            out.append(f"http, {label}, {base}, method=POST, body={text}, clear=true")
    return "; ".join(out)


def poll_replies(since) -> list:
    """Taps since `since` (unix seconds, or the id of the last one read). Empty on
    any trouble: no tap, no trade."""
    topic = reply_topic()
    if not topic:
        return []
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}/json?poll=1&since={since}",
                                     headers={"User-Agent": "desk-bot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return desk.parse_poll(r.read().decode(errors="replace"))
    except Exception as e:
        print(f"[could not read replies: {e}]", flush=True)
        return []


def summary(md: str) -> None:
    """Show text on the GitHub run page (and in the log)."""
    print(md, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def make_tag(day: str, symbol: str, risk_usd: float) -> str:
    return f"{TAG}-{day.replace('-', '')}-{symbol}-r{int(round(risk_usd * 100))}"


def parse_tag(cid: str):
    """desk-20261110-TSLA-r4688 -> {"day": "2026-11-10", "symbol": "TSLA", "risk": 46.88}"""
    parts = (cid or "").split("-")
    if len(parts) != 4 or parts[0] != TAG or len(parts[1]) != 8 or not parts[3].startswith("r"):
        return None
    try:
        d = parts[1]
        return {"day": f"{d[:4]}-{d[4:6]}-{d[6:]}", "symbol": parts[2], "risk": int(parts[3][1:]) / 100.0}
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------

class Alpaca:
    def __init__(self, key: str, secret: str):
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                  "Accept": "application/json", "User-Agent": "desk-bot/1.0"}

    def _call(self, base: str, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.h)
        if data:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as r:
                    text = r.read().decode()
                    return json.loads(text) if text.strip() else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:400]
                last = f"alpaca {method} {path}: {e.code} {text}"
                # never resend an order blindly: a POST is only retried when rate-limited
                if e.code == 429 or (e.code >= 500 and method == "GET"):
                    _time.sleep(2 + 3 * attempt)
                    continue
                raise RuntimeError(last)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = f"alpaca {method} {path}: {e}"
                if method != "GET":
                    raise RuntimeError(last)
                _time.sleep(2 + 3 * attempt)
        raise RuntimeError(last or f"alpaca {method} {path}: failed")

    # --- trading
    def account(self):
        return self._call(TRADING_URL, "GET", "/v2/account")

    def calendar(self, day: str):
        return self._call(TRADING_URL, "GET", "/v2/calendar", {"start": day, "end": day})

    def positions(self):
        return self._call(TRADING_URL, "GET", "/v2/positions")

    def orders(self, after: str, status: str = "all"):
        return self._call(TRADING_URL, "GET", "/v2/orders",
                          {"status": status, "after": after, "limit": 500, "nested": "true", "direction": "asc"})

    def submit(self, order: dict):
        return self._call(TRADING_URL, "POST", "/v2/orders", body=order)

    def cancel_all_orders(self):
        return self._call(TRADING_URL, "DELETE", "/v2/orders")

    def close_all_positions(self):
        return self._call(TRADING_URL, "DELETE", "/v2/positions", {"cancel_orders": "true"})

    def close_position(self, symbol: str):
        return self._call(TRADING_URL, "DELETE", f"/v2/positions/{symbol}")

    def fills(self, after: str):
        out, token = [], None
        while True:
            page = self._call(TRADING_URL, "GET", "/v2/account/activities/FILL",
                              {"after": after, "direction": "asc", "page_size": 100, "page_token": token})
            if not page:
                return out
            out += page
            if len(page) < 100:
                return out
            token = page[-1]["id"]

    # --- market data
    def bars(self, symbols: list, start: datetime, end: datetime | None, feed: str,
             timeframe: str = "5Min", adjustment: str = "split") -> dict:
        out, token = {s: [] for s in symbols}, None
        while True:
            page = self._call(DATA_URL, "GET", "/v2/stocks/bars", {
                "symbols": ",".join(symbols), "timeframe": timeframe,
                "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
                "limit": 10000, "adjustment": adjustment, "feed": feed, "sort": "asc", "page_token": token})
            for sym, rows in (page.get("bars") or {}).items():
                out.setdefault(sym, []).extend(parse_bar(r) for r in rows)
            token = page.get("next_page_token")
            if not token:
                return out

    def latest_trades(self, symbols: list, feed: str) -> dict:
        page = self._call(DATA_URL, "GET", "/v2/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": feed})
        return {s: float(t["p"]) for s, t in (page.get("trades") or {}).items()}

    def news(self, symbol: str, start: datetime, limit: int = 30) -> list:
        page = self._call(DATA_URL, "GET", "/v1beta1/news", {
            "symbols": symbol, "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit, "sort": "desc"})
        return [{"headline": n["headline"], "symbols": n.get("symbols") or []}
                for n in (page.get("news") or []) if n.get("headline")]


def connect() -> Alpaca:
    key, secret = os.environ.get("ALPACA_KEY", "").strip(), os.environ.get("ALPACA_SECRET", "").strip()
    if not key or not secret:
        sys.exit("no Alpaca keys: set ALPACA_KEY and ALPACA_SECRET (the desk bot's own paper account)")
    return Alpaca(key, secret)


# ---------------------------------------------------------------------------
# reading the account
# ---------------------------------------------------------------------------

def session_today(api, day: str):
    """(open_dt, close_dt) for today, or None when the market is closed all day."""
    for row in api.calendar(day) or []:
        if row.get("date") == day:
            d = datetime.strptime(day, "%Y-%m-%d").date()
            return (datetime.combine(d, hhmm(row["open"]), tzinfo=ET),
                    datetime.combine(d, hhmm(row["close"]), tzinfo=ET))
    return None


def wrong_account(cfg: dict, positions: list, orders: list):
    """A reason to refuse, or None. Everything in this account must be the bot's."""
    allowed = set(cfg["universe"])
    strangers = sorted({p["symbol"] for p in positions if p["symbol"] not in allowed} |
                       {o["symbol"] for o in orders if o["symbol"] not in allowed})
    if strangers:
        return ("this account holds or traded " + ", ".join(strangers[:6]) +
                ", which are not on the desk bot's list. It looks like another bot's account. "
                "Use the keys of the desk bot's own paper account.")
    return None


def todays_entries(orders: list, day: str) -> list:
    out = []
    for o in orders:
        tag = parse_tag(o.get("client_order_id", ""))
        if not tag or tag["day"] != day or o.get("side") != "buy":
            continue
        if o.get("status") in DEAD and float(o.get("filled_qty") or 0) == 0:
            continue
        out.append({**tag, "qty": float(o.get("qty") or 0), "status": o.get("status")})
    return out


def read_state(api, cfg: dict, day: str) -> dict:
    acct = api.account()
    positions = api.positions()
    since = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
    orders = api.orders(since)
    entries = todays_entries(orders, day)
    equity = float(acct["equity"])
    return {
        "equity": equity, "cash": float(acct["cash"]),
        "day_pnl": equity - float(acct.get("last_equity") or equity),
        "positions": positions, "orders": orders, "entries": entries,
        "deployed": sum(abs(float(p["market_value"])) for p in positions),
        "trades": len(entries), "risk_used": sum(e["risk"] for e in entries),
        "symbols": {e["symbol"] for e in entries} | {p["symbol"] for p in positions},
        "wrong": wrong_account(cfg, positions, orders),
    }


# ---------------------------------------------------------------------------
# the decision: pure, no network, fully testable
# ---------------------------------------------------------------------------

def decide(cfg: dict, bars: dict, latest: dict, state: dict, now: datetime) -> dict:
    """What to do after the bar that just closed.

    bars:   symbol -> today's 5-minute bars so far (dicts from parse_bar)
    latest: symbol -> latest trade price (may be missing)
    state:  from read_state (equity, cash, deployed, trades, risk_used, symbols, day_pnl)
    Returns {"orders": [...], "notes": [...], "done": bool}.
    """
    bm = cfg["bar_minutes"]
    step = timedelta(minutes=bm)
    notes, cands, usable = [], [], 0
    for sym in cfg["universe"]:
        sb = rules.completed(rules.session_bars(bars.get(sym, []), bm), now, bm)
        setup = rules.day_setup(sb, cfg)
        if setup is not None:
            usable += 1
        if setup is None or setup["status"] in ("waiting", "expired"):
            continue
        closes_at = setup["bar"]["t"] + step
        when = closes_at.strftime("%H:%M")
        if setup["status"] == "refused":
            if now - closes_at <= step:
                notes.append(f"{sym}: triggered {when} but no plan: {setup['why']}")
            continue
        if sym in state["symbols"]:
            continue                                   # already traded today
        if now - closes_at > step:
            continue                                   # an old trigger is a different trade
        dollar_vol = sum(b["c"] * b["v"] for b in sb)
        cands.append((dollar_vol, sym, setup, when))

    cands.sort(key=lambda c: (-c[0], c[1]))            # most traded first, like the scanner
    orders = []
    cash, deployed = state["cash"], state["deployed"]
    today = {"trades": state["trades"], "risk_used": state["risk_used"],
             "day_pnl": state["day_pnl"], "equity": state["equity"]}
    day = now.strftime("%Y-%m-%d")
    for _, sym, setup, when in cands:
        plan = setup["plan"]
        ref = max(plan["entry"], latest.get(sym, plan["entry"]))   # a price that ran up means more risk
        qty, which, limits = rules.size_order(ref, plan["stop"], cfg, cash, deployed, state["equity"])
        why = rules.risk_check(qty, ref, plan["stop"], plan["target"], cfg, today)
        if why:
            notes.append(f"{sym}: triggered {when} at {plan['entry']:.2f}, BLOCKED: " + "; ".join(why))
            continue
        risk_usd = qty * (ref - plan["stop"])
        orders.append({
            "symbol": sym, "qty": qty, "ref": ref, "entry": plan["entry"], "stop": plan["stop"],
            "target": plan["target"], "risk_usd": risk_usd, "bound_by": which, "triggered": when,
            "bar": setup["bar"],
            "request": {
                "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "gtc",
                "order_class": "bracket",
                "take_profit": {"limit_price": f"{plan['target']:.2f}"},
                "stop_loss": {"stop_price": f"{plan['stop']:.2f}"},
                "client_order_id": make_tag(day, sym, risk_usd),
            },
        })
        cash -= qty * ref
        deployed += qty * ref
        today["trades"] += 1
        today["risk_used"] += risk_usd
    done = today["trades"] >= cfg["max_trades_per_day"]
    return {"orders": orders, "notes": notes, "done": done, "usable": usable, "fresh": len(cands)}


def approve(cfg: dict, prop: dict, latest, state: dict, now: datetime):
    """The second look, after the tap. (order request, summary line, None) or
    (None, None, why not). Pure: no network.

    prop:   {"symbol", "entry", "stop", "target", "expires"}
    latest: the stock's latest trade price, or None
    state:  from read_state, read AFTER the tap
    """
    sym = prop["symbol"]
    if now > prop["expires"]:
        return None, None, f"the approval came after {prop['expires']:%H:%M} ET, too late"
    if sym in state["symbols"]:
        return None, None, "already traded today"
    if latest is None:
        return None, None, "no current price to check the plan against"
    if latest <= prop["stop"]:
        return None, None, f"the price ({latest:.2f}) is already at or under the stop ({prop['stop']:.2f})"
    ref = max(prop["entry"], latest)                   # a price that ran up means more risk
    qty, which, _ = rules.size_order(ref, prop["stop"], cfg, state["cash"], state["deployed"], state["equity"])
    today = {"trades": state["trades"], "risk_used": state["risk_used"],
             "day_pnl": state["day_pnl"], "equity": state["equity"]}
    why = rules.risk_check(qty, ref, prop["stop"], prop["target"], cfg, today)
    if why:
        return None, None, "; ".join(why)
    risk_usd = qty * (ref - prop["stop"])
    request = {
        "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "gtc",
        "order_class": "bracket",
        "take_profit": {"limit_price": f"{prop['target']:.2f}"},
        "stop_loss": {"stop_price": f"{prop['stop']:.2f}"},
        "client_order_id": make_tag(now.strftime("%Y-%m-%d"), sym, risk_usd),
    }
    line = (f"BUY {qty} {sym} near {ref:.2f} | stop {prop['stop']:.2f} | target {prop['target']:.2f} | "
            f"max loss ${risk_usd:.2f} (size set by {which})")
    return request, line, None


class Session:
    """One morning: the alerts sent, the taps seen, the log."""

    def __init__(self, day: str, log_path: Path | None = None, since: int | None = None):
        self.day, self.log_path = day, Path(log_path or LOG)
        self.rows = desk.load_log(self.log_path)
        self.pending = {}                                  # symbol -> proposal awaiting a tap
        self.seen = set()                                  # reply ids already handled
        self.since = int(_time.time()) if since is None else since
        self.hist = {}                                     # symbol -> (bars for the replay, bars for volume)

    def proposed(self) -> set:
        return {r["symbol"] for r in self.rows if r["date"] == self.day}

    def row(self, symbol: str):
        for r in self.rows:
            if r["date"] == self.day and r["symbol"] == symbol:
                return r
        return None

    def decide_row(self, symbol: str, decision: str, now: datetime, note: str = "", buy_ref: str = "") -> None:
        r = self.row(symbol)
        if r is not None:
            r.update({"decision": decision, "decided": now.strftime("%H:%M:%S"), "note": note, "buy_ref": buy_ref})
        self.pending.pop(symbol, None)
        self.save()

    def save(self) -> None:
        try:
            desk.save_log(self.log_path, self.rows)
        except OSError as e:
            print(f"[log not saved: {e}]", flush=True)


def fetch_history(api, cfg: dict, sym: str, now: datetime, sess: Session) -> tuple:
    """(bars for the Time Machine, bars for the volume comparison), today left out.
    The first is the full-market feed when the account allows it; the second is the
    live feed, so today's volume is compared with the same kind of number."""
    if sym in sess.hist:
        return sess.hist[sym]
    end = datetime.combine(now.date(), time(0, 0), tzinfo=ET)
    start = end - timedelta(days=cfg["desk"]["history_days"])
    live = []
    try:
        live = api.bars([sym], start, end, cfg["live_feed"]).get(sym, [])
    except RuntimeError as e:
        print(f"[no {cfg['live_feed']} history for {sym}: {e}]", flush=True)
    try:
        full = api.bars([sym], start, end, "sip").get(sym, [])
    except RuntimeError as e:
        print(f"[no full-market history for {sym}, using {cfg['live_feed']}: {e}]", flush=True)
        full = live
    sess.hist[sym] = (full or live, live)
    return sess.hist[sym]


def propose(api, cfg: dict, o: dict, today_bars: list, now: datetime, sess: Session, dry_run: bool = False) -> None:
    """Score one plan and ask the phone. Sends no order."""
    sym, day = o["symbol"], sess.day
    plan = {"entry": o["entry"], "stop": o["stop"], "target": o["target"]}
    try:
        full, live = fetch_history(api, cfg, sym, now, sess)
        tmres = desk.replay(full, plan, o["bar"], cfg, day, cfg["desk"]["replays"], cfg["desk"]["seed"])
        relvol = desk.rel_volume(today_bars, live, o["bar"], day)
    except Exception as e:                              # the score must never stop the alert
        print(f"[Time Machine failed for {sym}: {e}]", flush=True)
        tmres, relvol = {"ok": False, "why": "it could not run"}, None
    spy_pct = headlines = None
    try:
        start = datetime.combine(now.date(), rules.OPEN, tzinfo=ET)
        spy_pct = desk.market_move(api.bars([BENCH], start, None, cfg["live_feed"]).get(BENCH, []), now)
    except Exception as e:
        print(f"[no market read: {e}]", flush=True)
    try:
        back = 3 if now.weekday() == 0 else 1                 # on Monday, look back over the weekend
        since = datetime.combine(now.date(), rules.CLOSE, tzinfo=ET) - timedelta(days=back)
        headlines = desk.about(sym, api.news(sym, since))
    except Exception as e:
        print(f"[no news for {sym}: {e}]", flush=True)
    points = desk.key_points(o, tmres, relvol, spy_pct, headlines)
    expires = now + timedelta(minutes=cfg["desk"]["approval_minutes"])
    title, body = desk.alert_text(o, tmres, points, expires, cfg)
    if dry_run:
        print("[dry run, nothing sent] " + title + "\n" + body, flush=True)
        return
    sess.rows.append(desk.log_row(day, o, tmres, relvol, spy_pct, headlines))
    sent = notify(title, body, actions=buttons(sym, day, cfg["desk"]["button_style"]), priority="high")
    if not sent:
        sess.decide_row(sym, "not sent", now, "the alert could not be sent")
        return
    sess.pending[sym] = {**plan, "symbol": sym, "expires": expires}
    sess.save()


def cycle(api, cfg: dict, now: datetime, sess: Session, dry_run: bool = False) -> dict:
    """One look at the market: read, decide, ask. Returns decide()'s result."""
    day = now.strftime("%Y-%m-%d")
    state = read_state(api, cfg, day)
    if state["wrong"]:
        raise RuntimeError(state["wrong"])
    if state["trades"] >= cfg["max_trades_per_day"]:
        return {"orders": [], "notes": [f"{state['trades']} trades already placed today"], "done": True,
                "usable": 0, "fresh": 0}
    start = datetime.combine(now.date(), rules.OPEN, tzinfo=ET)
    bars = api.bars(cfg["universe"], start, None, cfg["live_feed"])
    latest_bar = max((b["t"] for rows in bars.values() for b in rows), default=None)
    expected = now.replace(second=0, microsecond=0)
    expected -= timedelta(minutes=expected.minute % cfg["bar_minutes"] + cfg["bar_minutes"])
    if latest_bar is None or latest_bar < expected:     # the newest bar is not in yet: one more try
        _time.sleep(0 if dry_run else 15)
        bars = api.bars(cfg["universe"], start, None, cfg["live_feed"])
    try:
        latest = api.latest_trades(cfg["universe"], cfg["live_feed"])
    except RuntimeError as e:
        latest = {}
        print(f"[no latest prices: {e}]", flush=True)
    result = decide(cfg, bars, latest, state, now)
    result["done"] = False                              # an alert is not a trade: only real buys end the morning
    print(f"{now:%H:%M:%S} ET: {result['usable']} of {len(cfg['universe'])} charts usable, "
          f"{result['fresh']} new breakouts, {len(result['orders'])} passed the risk checks.", flush=True)
    for note in result["notes"]:
        print(note, flush=True)
    for o in result["orders"]:
        if o["symbol"] in sess.proposed():
            continue                                       # asked once already today
        if len(sess.proposed()) >= cfg["desk"]["max_alerts_per_day"]:
            print(f"{o['symbol']}: not asked, {cfg['desk']['max_alerts_per_day']} alerts already sent today", flush=True)
            continue
        propose(api, cfg, o, bars.get(o["symbol"], []), now, sess, dry_run=dry_run)
    return result


def handle_replies(api, cfg: dict, sess: Session, now: datetime, replies: list) -> None:
    """Act on the taps: Skip is recorded, Approve gets the second look and then the order."""
    for ev in replies:
        if ev["id"] in sess.seen:
            continue
        sess.seen.add(ev["id"])
        parsed = desk.parse_reply(ev["message"])
        if not parsed:
            continue
        action, sym, day = parsed
        prop = sess.pending.get(sym)
        if day != sess.day or prop is None:
            continue                                       # not today's, or already decided
        if action == "skip":
            sess.decide_row(sym, "skipped", now)
            print(f"{sym}: skipped.", flush=True)
            continue
        try:
            state = read_state(api, cfg, sess.day)
            if state["wrong"]:
                raise RuntimeError(state["wrong"])
            latest = api.latest_trades([sym], cfg["live_feed"]).get(sym)
            request, line, why = approve(cfg, prop, latest, state, now)
            if request is None:
                sess.decide_row(sym, "refused", now, why)
                notify(f"Desk did NOT buy {sym}", f"You approved, but: {why}.")
                continue
            api.submit(request)
            sess.decide_row(sym, desk.BOUGHT, now, buy_ref=f"{max(prop['entry'], latest):.2f}")
            notify(f"Desk bought {sym}", line + "\nApproved by you. Paper account.")
        except RuntimeError as e:
            sess.decide_row(sym, "refused", now, str(e)[:200])
            notify(f"Desk: order for {sym} failed", str(e))


def expire(sess: Session, now: datetime) -> None:
    for sym, prop in list(sess.pending.items()):
        if now > prop["expires"]:
            sess.decide_row(sym, "expired", now, "no tap in time")
            print(f"{sym}: no tap by {prop['expires']:%H:%M}, expired.", flush=True)


def score_log(api, cfg: dict, rows: list, today: str) -> int:
    """Fill in what each past alert's plan went on to do. Returns how many were scored."""
    n = 0
    for r in rows:
        if r.get("outcome") or r["date"] >= today or not r.get("entry"):
            continue
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        start = datetime.combine(d, rules.OPEN, tzinfo=ET)
        end = datetime.combine(d, rules.CLOSE, tzinfo=ET) + timedelta(minutes=5)
        try:
            day_bars = api.bars([r["symbol"]], start, end, "sip").get(r["symbol"], [])
        except RuntimeError:
            day_bars = api.bars([r["symbol"]], start, end, cfg["live_feed"]).get(r["symbol"], [])
        res = desk.score_row(r, day_bars, cfg)
        if res:
            r["outcome"], r["outcome_r"] = res[0], f"{res[1]:.3f}"
            n += 1
    return n


# ---------------------------------------------------------------------------
# timing: pure, testable
# ---------------------------------------------------------------------------

def run_gate(cfg: dict, now: datetime, open_dt: datetime, close_dt: datetime):
    """A reason not to start the trading session, or None."""
    last_entry = datetime.combine(now.date(), hhmm(cfg["no_entries_after"]), tzinfo=ET)
    if cfg.get("skip_early_close_days") and close_dt.time() < rules.CLOSE:
        return f"early close at {close_dt:%H:%M}. The bot sits out early-close days."
    if now < open_dt - timedelta(minutes=EARLY_START_MINUTES):
        return f"{now:%H:%M} ET is too early (an early schedule slot; a later one takes over)."
    if now > last_entry:
        return f"{now:%H:%M} ET is past the last entry time {cfg['no_entries_after']}."
    return None


def next_check(cfg: dict, now: datetime):
    """When to look next (a bar close plus a settle delay), or None when the window is over."""
    bm = cfg["bar_minutes"]
    first = datetime.combine(now.date(), hhmm(cfg["no_entries_before"]), tzinfo=ET) + timedelta(minutes=bm)
    last_entry = datetime.combine(now.date(), hhmm(cfg["no_entries_after"]), tzinfo=ET)
    nxt = now.replace(second=0, microsecond=0)
    nxt += timedelta(minutes=bm - nxt.minute % bm)     # the next bar close
    nxt = max(nxt, first)
    if nxt > last_entry:
        return None
    return nxt + timedelta(seconds=SETTLE_SECONDS)


def flatten_plan(cfg: dict, now: datetime, close_dt: datetime):
    """("early" | "missed" | "go", when_to_act)."""
    flat_dt = min(datetime.combine(now.date(), hhmm(cfg["flat_by"]), tzinfo=ET), close_dt - timedelta(minutes=5))
    act_at = flat_dt - timedelta(minutes=3)
    if now >= close_dt:
        return "missed", act_at
    if now < act_at - timedelta(minutes=EARLY_FLATTEN_MINUTES):
        return "early", act_at
    return "go", act_at


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def flatten_now(api, pause: float = 5.0) -> bool:
    """Cancel everything and sell everything. True when the account ends flat."""
    try:
        api.close_all_positions()
    except RuntimeError as e:
        print(f"[close-all failed, trying one by one: {e}]", flush=True)
    for attempt in range(6):
        _time.sleep(pause)
        left = api.positions()
        if not left:
            return True
        if attempt == 1:                               # fallback: cancel, wait, close each
            try:
                api.cancel_all_orders()
                _time.sleep(pause)
                for p in left:
                    api.close_position(p["symbol"])
            except RuntimeError as e:
                print(f"[fallback close failed: {e}]", flush=True)
    return False


def wait_and_listen(api, cfg: dict, sess: Session, until: datetime) -> None:
    """Sleep until `until`, checking for taps every few seconds while one is awaited."""
    while True:
        now = now_et()
        if sess.pending:
            replies = poll_replies(sess.since)
            handle_replies(api, cfg, sess, now, replies)
            if replies:
                sess.since = replies[-1]["id"]             # next time, only what is newer
            expire(sess, now_et())
        left = (until - now_et()).total_seconds()
        if left <= 0:
            return
        _time.sleep(min(POLL_SECONDS, left) if sess.pending else left)


def cmd_run(args) -> int:
    cfg = load_cfg()
    if STOP_FILE.exists() and not args.dry_run:
        print("STOP file found: the bot is switched off and sends no alerts and no orders. "
              "Delete the file called STOP to switch it on.")
        return 0
    if not reply_topic() and not args.dry_run:
        print("NTFY_TOPIC is not set. Without it there are no alerts and no way to approve, so nothing would "
              "ever be bought. Add the secret and run again.")
        return 1
    api = connect()
    now = now_et()
    day = now.strftime("%Y-%m-%d")
    sess_hours = session_today(api, day)
    if sess_hours is None:
        print(f"{day}: the market is closed today.")
        return 0
    open_dt, close_dt = sess_hours

    state = read_state(api, cfg, day)
    if state["wrong"]:
        notify("Desk bot stopped: wrong account", state["wrong"])
        return 1
    sess = Session(day)

    if args.once:
        if not (open_dt <= now < close_dt):
            print(f"{now:%H:%M} ET: the market is not open, nothing to look at.")
            return 0
        cycle(api, cfg, now, sess, dry_run=True)       # one look never sends anything
        return 0

    reason = run_gate(cfg, now, open_dt, close_dt)
    if reason:
        print(f"{day}: {reason} Exiting.")
        return 0

    try:                                               # yesterday's alerts: what did their plans do?
        if score_log(api, cfg, sess.rows, day) and not args.dry_run:
            sess.save()
    except Exception as e:
        print(f"[could not score earlier alerts: {e}]", flush=True)

    # anything still held from an earlier day should never be there: get out first
    if state["positions"] and not state["entries"] and not args.dry_run:
        wait = (open_dt + timedelta(minutes=1) - now_et()).total_seconds()
        if wait > 0:
            _time.sleep(wait)
        names = ", ".join(p["symbol"] for p in state["positions"])
        ok = flatten_now(api)
        notify("Desk bot: closed positions left over from an earlier day",
               f"{names}. " + ("All closed." if ok else "SOME ARE STILL OPEN. Check the Alpaca paper account."))

    print(f"{day}: watching {len(cfg['universe'])} stocks until {cfg['no_entries_after']} ET. "
          f"Each alert waits {cfg['desk']['approval_minutes']} minutes for a tap.", flush=True)
    full = False
    while True:
        when = None if full else next_check(cfg, now_et())
        if when is None:
            if not sess.pending:
                break
            when = max(p["expires"] for p in sess.pending.values()) + timedelta(seconds=POLL_SECONDS)
            wait_and_listen(api, cfg, sess, when)
            continue
        wait_and_listen(api, cfg, sess, when)
        try:
            result = cycle(api, cfg, now_et(), sess, dry_run=args.dry_run)
        except RuntimeError as e:
            notify("Desk bot error", str(e))
            if "not on the desk bot's list" in str(e):
                return 1
            continue
        if result["done"]:
            print("Daily trade limit reached. No more alerts today.")
            full = True
            for sym in list(sess.pending):
                sess.decide_row(sym, "expired", now_et(), "daily trade limit reached")
    alerts = [r for r in sess.rows if r["date"] == day]
    bought = sum(1 for r in alerts if r["decision"] == desk.BOUGHT)
    summary(f"## Desk bot, {day}\n- Alerts sent: {len(alerts)}. Bought on your approval: {bought}.\n" +
            "\n".join(f"- {r['symbol']} {r['triggered']}: {r['decision']}" + (f" ({r['note']})" if r["note"] else "")
                      for r in alerts))
    print("Session over. Open trades are protected by their stop and target; "
          "the flatten job closes what is left before the bell.")
    return 0


def cmd_test_alert(args) -> int:
    """Send a sample alert with both buttons and wait for a tap. Places no order and
    needs no Alpaca keys. Run it once after setup to prove the round trip works."""
    if not reply_topic():
        print("NTFY_TOPIC is not set. Add the secret first.")
        return 1
    now = now_et()
    day = now.strftime("%Y-%m-%d")
    since = int(_time.time())
    sent = notify("Desk TEST: tap Approve or Skip",
                  "This is a test. Nothing is bought.\nTap a button below within "
                  f"{args.wait} minutes so the bot can confirm it hears you.",
                  actions=buttons("TEST", day, load_cfg()["desk"]["button_style"]), priority="high")
    if not sent:
        summary("## Desk test alert\n- FAILED: the alert could not be sent. Check the NTFY_TOPIC secret.")
        return 1
    deadline = _time.time() + 60 * args.wait
    while _time.time() < deadline:
        for ev in poll_replies(since):
            parsed = desk.parse_reply(ev["message"])
            if parsed and parsed[1] == "TEST":
                summary(f"## Desk test alert\n- PASSED: the bot heard your tap ({parsed[0]}).")
                notify("Desk TEST passed", f"The bot heard your tap ({parsed[0]}). Approvals work.")
                return 0
        _time.sleep(POLL_SECONDS)
    summary(f"## Desk test alert\n- FAILED: the alert was sent but no tap came back in {args.wait} minutes. "
            "If you did tap, the button could not reach ntfy. Tell Claude.")
    return 1


def cmd_flatten(args) -> int:
    cfg = load_cfg()
    api = connect()
    now = now_et()
    day = now.strftime("%Y-%m-%d")
    sess = session_today(api, day)
    if sess is None:
        print(f"{day}: the market is closed today.")
        return 0
    _, close_dt = sess
    missed = False
    if not args.now:
        verdict, act_at = flatten_plan(cfg, now, close_dt)
        if verdict == "early":
            print(f"{now:%H:%M} ET: too early to flatten (an early schedule slot; a later one takes over). Exiting.")
            return 0
        missed = verdict == "missed"
        wait = (act_at - now).total_seconds()
        if wait > 0 and not missed:                    # wait first, so a buy made after this run started is still sold
            print(f"{now:%H:%M} ET: waiting until {act_at:%H:%M} to flatten.", flush=True)
            _time.sleep(wait)
    state = read_state(api, cfg, day)
    if state["wrong"]:
        notify("Desk bot stopped: wrong account", state["wrong"])
        return 1
    if not state["positions"]:
        print("Nothing open. Nothing to do.")
        return 0
    names = ", ".join(p["symbol"] for p in state["positions"])
    if missed:
        notify("Desk bot: MISSED the close",
               f"Still holding {names} overnight. Their stop and target orders stay in place, "
               "and the bot sells them at the next open.")
        return 1
    ok = flatten_now(api)
    acct = api.account()
    pnl = float(acct["equity"]) - float(acct.get("last_equity") or acct["equity"])
    if ok:
        notify("Desk bot: flat for the day", f"Sold {names} before the close. Today: {pnl:+.2f} USD. "
               f"Account {float(acct['equity']):,.2f}. Paper account.")
        return 0
    notify("Desk bot: COULD NOT close everything", f"Was holding {names}. Check the Alpaca paper account.")
    return 1


def cmd_status(args) -> int:
    cfg = load_cfg()
    api = connect()
    now = now_et()
    day = now.strftime("%Y-%m-%d")
    state = read_state(api, cfg, day)
    sess = session_today(api, day)
    lines = [
        "## Desk bot status",
        f"- Time: {now:%a %Y-%m-%d %H:%M} New York. " +
        (f"Market hours today {sess[0]:%H:%M} to {sess[1]:%H:%M}." if sess else "Market closed today."),
        f"- Paper account: equity ${state['equity']:,.2f}, cash ${state['cash']:,.2f}, today {state['day_pnl']:+.2f}.",
        f"- Bot budget: ${cfg['budget']:,.0f}. Trades placed today: {state['trades']} of {cfg['max_trades_per_day']}. "
        f"At risk today: ${state['risk_used']:.2f} of ${cfg['max_daily_loss_usd']:.0f}.",
        "- Open positions: " + (", ".join(f"{p['symbol']} x{p['qty']}" for p in state["positions"]) or "none") + ".",
        f"- Kill switch (STOP file): {'ON' if STOP_FILE.exists() else 'off'}.",
        f"- Approvals: each alert waits {cfg['desk']['approval_minutes']} minutes for your tap; at most "
        f"{cfg['desk']['max_alerts_per_day']} alerts a day. Phone alerts are {'set up' if reply_topic() else 'NOT set up (no NTFY_TOPIC)'}.",
    ]
    if state["wrong"]:
        lines.append(f"- **WRONG ACCOUNT:** {state['wrong']}")
    if state["equity"] > 5 * cfg["budget"]:
        lines.append(f"- Note: the account is much bigger than the ${cfg['budget']:,.0f} budget. "
                     "The bot only uses the budget, but a paper account opened at that size is easier to read.")
    summary("\n".join(lines))
    return 1 if state["wrong"] else 0


def build_report(cfg: dict, fills: list, orders: list, equity: float) -> tuple:
    """(markdown, trade rows) from the account's fills. One row per stock per day."""
    risk = {}
    for o in orders:
        tag = parse_tag(o.get("client_order_id", ""))
        if tag:
            risk[(tag["day"], tag["symbol"])] = tag["risk"]
    groups = {}
    for f in fills:
        d = parse_ts(f["transaction_time"]).strftime("%Y-%m-%d")
        g = groups.setdefault((d, f["symbol"]), {"bought": 0.0, "cost": 0.0, "sold": 0.0, "proceeds": 0.0})
        q, px = float(f["qty"]), float(f["price"])
        if f["side"] == "buy":
            g["bought"] += q
            g["cost"] += q * px
        else:
            g["sold"] += q
            g["proceeds"] += q * px
    rows = []
    for (d, sym), g in sorted(groups.items()):
        if g["bought"] == 0 or abs(g["bought"] - g["sold"]) > 1e-9:
            continue                                   # still open, or not a round trip
        pnl = g["proceeds"] - g["cost"]
        r = risk.get((d, sym))
        rows.append({"date": d, "symbol": sym, "shares": g["bought"], "avg_buy": g["cost"] / g["bought"],
                     "avg_sell": g["proceeds"] / g["sold"], "pnl": pnl,
                     "risk_usd": r, "R": (pnl / r) if r else None})
    n = len(rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    total = sum(r["pnl"] for r in rows)
    rs = [r["R"] for r in rows if r["R"] is not None]
    days = len({r["date"] for r in rows})
    md = ["## Desk bot report (paper account)", ""]
    if n == 0:
        md.append("No completed trades yet.")
    else:
        md += [
            f"- Completed trades: **{n}** over {days} trading days with a trade "
            f"(first {rows[0]['date']}, last {rows[-1]['date']}).",
            f"- Winners: {wins} of {n} ({100 * wins / n:.0f}%).",
            f"- Total result: **{total:+.2f} USD** on a ${cfg['budget']:,.0f} budget ({100 * total / cfg['budget']:+.2f}%).",
            f"- Average trade: {total / n:+.2f} USD" + (f", {sum(rs) / len(rs):+.2f} R (1 R = the planned loss at the stop)." if rs else "."),
            f"- Account equity now: ${equity:,.2f}.",
            "",
            "Under about 100 trades these numbers are mostly noise." if n < 100 else "",
            "",
            "| date | stock | shares | buy | sell | result | R |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in rows[-40:]:
            md.append(f"| {r['date']} | {r['symbol']} | {r['shares']:.0f} | {r['avg_buy']:.2f} | {r['avg_sell']:.2f} | "
                      f"{r['pnl']:+.2f} | {'' if r['R'] is None else format(r['R'], '+.2f')} |")
    return "\n".join(md), rows


def cmd_report(args) -> int:
    cfg = load_cfg()
    api = connect()
    since = (now_et() - timedelta(days=args.days)).strftime("%Y-%m-%dT00:00:00Z")
    md, rows = build_report(cfg, api.fills(since), api.orders(since), float(api.account()["equity"]))
    log = desk.load_log(LOG)
    try:
        score_log(api, cfg, log, now_et().strftime("%Y-%m-%d"))
    except Exception as e:
        print(f"[could not score the alert log: {e}]", flush=True)
    md += "\n\n" + desk.compare(log)
    summary(md)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(md + "\n")
    desk.save_log(out / "alerts.csv", log)
    with open(out / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "symbol", "shares", "avg_buy", "avg_sell", "pnl", "risk_usd", "R"])
        w.writeheader()
        w.writerows(rows)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Desk bot: opening-range breakouts on an Alpaca paper account, bought only on your approval.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the trading session")
    r.add_argument("--dry-run", action="store_true", help="decide but send no orders")
    r.add_argument("--once", action="store_true", help="one look right now instead of the whole morning")
    r.set_defaults(fn=cmd_run)
    f = sub.add_parser("flatten", help="sell everything before the close")
    f.add_argument("--now", action="store_true", help="do it immediately instead of waiting for the flat-by time")
    f.set_defaults(fn=cmd_flatten)
    sub.add_parser("status", help="account, positions, today's trades").set_defaults(fn=cmd_status)
    t = sub.add_parser("test-alert", help="send a sample alert and wait for a tap; no orders")
    t.add_argument("--wait", type=int, default=5, help="minutes to wait for the tap")
    t.set_defaults(fn=cmd_test_alert)
    rep = sub.add_parser("report", help="results so far")
    rep.add_argument("--days", type=int, default=400)
    rep.add_argument("--out", default="report")
    rep.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
