"""The trading rules: pure functions, no network, shared by the live bot and the backtest.

These follow the Day Trader skill (github.com/Jakeschincariol/day-trader-skill):

  trader-entry   opening range = the first 15 minutes; the trigger is the first
                 completed 5-minute bar after the range that CLOSES above the
                 range high. One trigger per stock per day.
  trader-exit    stop at the opening-range midpoint, rounded down to the cent;
                 target = entry + R x risk; refuse a risk under 0.05% of price.
  trader-budget  whole shares; size = the smallest of four limits (max loss,
                 position cap, max order, cash).
  trader-risk    max loss per trade, daily loss, trades per day, reward:risk,
                 entry window, duplicates.

Long side only. A bar is a dict: {"t": datetime (New York, bar start), "o", "h",
"l", "c", "v"}.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
CENT = Decimal("0.01")


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _d(x) -> Decimal:
    return Decimal(str(x))


def floor_cent(x) -> float:
    return float(_d(x).quantize(CENT, rounding=ROUND_DOWN))


def round_cent(x) -> float:
    return float(_d(x).quantize(CENT, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# bars
# ---------------------------------------------------------------------------

def session_bars(bars: list, bar_minutes: int = 5, close: time = CLOSE) -> list:
    """Regular-session bars on the bar grid, oldest first, one per slot."""
    out, seen = [], set()
    for b in sorted(bars, key=lambda b: b["t"]):
        t = b["t"].timetz().replace(tzinfo=None)
        if not (OPEN <= t < close):
            continue
        if b["t"].second or b["t"].minute % bar_minutes:
            continue
        if b["t"] in seen or b.get("c") is None:
            continue
        seen.add(b["t"])
        out.append(b)
    return out


def completed(bars: list, now: datetime, bar_minutes: int = 5) -> list:
    """Only bars that have closed by `now`."""
    step = timedelta(minutes=bar_minutes)
    return [b for b in bars if b["t"] + step <= now]


def opening_range(bars: list, or_minutes: int = 15, bar_minutes: int = 5):
    """(high, low) of the first `or_minutes`, or None unless every range bar is there."""
    need = or_minutes // bar_minutes
    if not bars:
        return None
    day = bars[0]["t"].date()
    start = datetime.combine(day, OPEN, tzinfo=ET)
    want = [start + timedelta(minutes=bar_minutes * i) for i in range(need)]
    by_t = {b["t"]: b for b in bars}
    if any(w not in by_t for w in want):
        return None
    rng = [by_t[w] for w in want]
    return max(b["h"] for b in rng), min(b["l"] for b in rng)


def find_trigger(bars: list, or_high: float, cfg: dict):
    """The first completed bar after the range that closes above the range high.

    Returns (bar, status): status is "triggered", "waiting" (no close above yet)
    or "expired" (the first close above came outside the entry window; later
    closes do not reset it).
    """
    bm = cfg["bar_minutes"]
    step = timedelta(minutes=bm)
    if not bars:
        return None, "waiting"
    day = bars[0]["t"].date()
    range_end = datetime.combine(day, OPEN, tzinfo=ET) + timedelta(minutes=cfg["or_minutes"])
    first_ok = datetime.combine(day, hhmm(cfg["no_entries_before"]), tzinfo=ET)
    last_ok = datetime.combine(day, hhmm(cfg["no_entries_after"]), tzinfo=ET)
    for b in bars:
        if b["t"] < range_end:
            continue
        if b["c"] > or_high:
            closes_at = b["t"] + step
            if first_ok <= closes_at <= last_ok:
                return b, "triggered"
            return b, "expired"
    return None, "waiting"


# ---------------------------------------------------------------------------
# the exit plan (trader-exit)
# ---------------------------------------------------------------------------

def plan_exit(entry: float, or_high: float, or_low: float, cfg: dict):
    """(plan, None) or (None, reason). The stop and target are fixed before entry."""
    rule = cfg.get("stop_rule", "or-mid")
    or_high, or_low = round_cent(or_high), round_cent(or_low)
    if rule == "or-mid":
        stop = floor_cent((_d(or_high) + _d(or_low)) / 2)
    elif rule == "or-low":
        stop = floor_cent(or_low)
    else:
        return None, f"unknown stop rule {rule!r}"
    entry = round_cent(entry)
    risk = round(entry - stop, 2)
    if risk <= 0:
        return None, "the stop is not below the entry"
    if risk < entry * cfg.get("min_risk_pct", 0.05) / 100.0:
        return None, "the stop is too close to the entry to survive the spread"
    target = round_cent(_d(entry) + _d(cfg["target_r"]) * _d(risk))
    return {"entry": entry, "stop": stop, "target": target, "risk": risk,
            "target_r": cfg["target_r"]}, None


def reward_risk(ref: float, stop: float, target: float) -> float:
    risk = ref - stop
    return (target - ref) / risk if risk > 0 else -1.0


# ---------------------------------------------------------------------------
# sizing (trader-budget) and the gate (trader-risk)
# ---------------------------------------------------------------------------

def size_order(ref: float, stop: float, cfg: dict, cash: float, deployed: float, equity: float):
    """Whole shares: the smallest of the four limits. Returns (qty, which_limit, detail)."""
    risk = ref - stop
    if risk <= 0:
        return 0, "stop", {}
    budget = min(cfg["budget"], equity)
    buffer_usd = budget * cfg["cash_buffer_pct"] / 100.0
    spendable = min(cash, budget - deployed) - buffer_usd
    limits = {
        "max loss": math.floor(cfg["max_loss_per_trade_usd"] / risk),
        "position cap": math.floor(budget * cfg["max_position_pct"] / 100.0 / ref),
        "max order": math.floor(cfg["max_order_usd"] / ref),
        "cash": math.floor(max(spendable, 0.0) / ref),
    }
    which = min(limits, key=lambda k: limits[k])
    return max(limits[which], 0), which, limits


def risk_check(qty: int, ref: float, stop: float, target: float, cfg: dict, today: dict):
    """PASS (empty list) or the reasons it is blocked.

    today: {"trades": n, "risk_used": usd, "symbols": set, "day_pnl": usd, "equity": usd}
    """
    why = []
    risk = ref - stop
    if qty <= 0:
        why.append("size is zero shares")
    if risk <= 0:
        why.append("the stop is not below the entry")
        return why
    if qty * ref < cfg["min_order_usd"]:
        why.append(f"order under the ${cfg['min_order_usd']:.0f} minimum")
    loss = qty * risk
    if loss > cfg["max_loss_per_trade_usd"] + 1e-9:
        why.append(f"loss at the stop ${loss:.2f} is over the ${cfg['max_loss_per_trade_usd']:.0f} max loss per trade")
    rr = reward_risk(ref, stop, target)
    if rr < cfg["min_reward_risk"] - 1e-9:
        why.append(f"reward:risk {rr:.2f} is under {cfg['min_reward_risk']:.2f} (the move has mostly happened)")
    if today["trades"] >= cfg["max_trades_per_day"]:
        why.append(f"{cfg['max_trades_per_day']} trades already placed today")
    if today["risk_used"] + loss > cfg["max_daily_loss_usd"] + 1e-9:
        why.append(f"daily loss limit: ${today['risk_used']:.2f} already at risk today, limit ${cfg['max_daily_loss_usd']:.0f}")
    if today.get("day_pnl", 0.0) <= -cfg["max_daily_loss_usd"]:
        why.append(f"the account is down ${-today['day_pnl']:.2f} today, at the daily loss limit")
    floor = cfg["budget"] * (1 - cfg["halt_drawdown_pct"] / 100.0)
    if today.get("equity", cfg["budget"]) < floor:
        why.append(f"account under ${floor:,.0f} ({cfg['halt_drawdown_pct']}% drawdown): halted until you review it")
    return why


# ---------------------------------------------------------------------------
# what happens after the entry (used by the backtest)
# ---------------------------------------------------------------------------

def simulate_exit(bars_after: list, entry: float, stop: float, target: float,
                  flat_by: time, bar_minutes: int = 5, gap_fills: bool = True):
    """Walk the bars after the entry bar. Stop is checked before the target; a
    bar that touches both counts as the stop. Otherwise out at the last close at
    or before `flat_by`. Returns (exit_price, reason, exit_time)."""
    step = timedelta(minutes=bar_minutes)
    last = None
    for b in bars_after:
        closes_at = b["t"] + step
        if closes_at.timetz().replace(tzinfo=None) > flat_by:
            break
        if b["l"] <= stop:
            px = min(stop, b["o"]) if gap_fills else stop
            return px, "stop", closes_at
        if b["h"] >= target:
            return target, "target", closes_at
        last = b
    if last is None:
        return entry, "flat", None
    return last["c"], "flat", last["t"] + step


def day_setup(bars: list, cfg: dict):
    """Everything the rules say about one stock on one day, from its session bars.

    Returns None when there is no usable opening range, else a dict with the
    range, the trigger status and (when triggered) the plan.
    """
    rng = opening_range(bars, cfg["or_minutes"], cfg["bar_minutes"])
    if rng is None:
        return None
    hi, lo = rng
    bar, status = find_trigger(bars, hi, cfg)
    out = {"or_high": hi, "or_low": lo, "status": status, "bar": bar, "plan": None, "why": None}
    if status == "triggered":
        plan, why = plan_exit(bar["c"], hi, lo, cfg)
        out["plan"], out["why"] = plan, why
        if plan is None:
            out["status"] = "refused"
    return out
