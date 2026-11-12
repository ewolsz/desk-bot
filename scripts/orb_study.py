#!/usr/bin/env python3
"""Backtest of the desk bot's rule on Alpaca 5-minute history, with the pass mark
written down before any test (config/desk_bot.json -> study.pass_mark).

    python3 scripts/orb_study.py --explore        # the older years: look as much as you like
    python3 scripts/orb_study.py --final          # the locked-away years: run ONCE

It uses the same functions as the live bot (bot/rules.py), so the backtest and
the bot cannot drift apart. The account is simulated the way the bot trades:
$5,000 budget, up to 3 trades a day taken in the order they trigger (most
traded first within a bar), sized by the same four limits and checked by the
same risk gate, costs on.

What it cannot fix, said plainly:
  - The stock list is today's large companies. Some are on it BECAUSE they went
    up (NVDA, PLTR, ...), which flatters any buy-side test. The "same minute,
    every stock" comparison shares that bias, so check 3 is the cleanest one.
  - History uses the full-market feed; the live bot sees one exchange (IEX).
  - Fills are assumed at the bar's close plus slippage. Real fills vary.
  - Running --final more than once, or after changing a setting because of what
    --final showed, turns the test into curve-fitting.

Not financial advice.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import statistics
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
import rules  # noqa: E402
from rules import ET, hhmm  # noqa: E402

SEED = 19


# ---------------------------------------------------------------------------
# one stock, one day
# ---------------------------------------------------------------------------

def net_r(entry: float, exit_px: float, risk: float, slip: float) -> float:
    return (exit_px * (1 - slip) - entry * (1 + slip)) / risk


def analyze_day(sym: str, day: str, bars: list, cfg: dict, slip: float, gap_fills: bool = True):
    """(trade or None, {bar_close_time: placebo R}) for one session."""
    bm = cfg["bar_minutes"]
    step = timedelta(minutes=bm)
    sb = rules.session_bars(bars, bm)
    if not sb or sb[-1]["t"].time() < hhmm("15:30"):
        return None, {}                                # an early close or a broken day: the bot sits out
    setup = rules.day_setup(sb, cfg)
    if setup is None:
        return None, {}
    flat_by = hhmm(cfg["flat_by"])
    d = sb[0]["t"].date()
    first_ok = datetime.combine(d, hhmm(cfg["no_entries_before"]), tzinfo=ET)
    last_ok = datetime.combine(d, hhmm(cfg["no_entries_after"]), tzinfo=ET)
    range_end = datetime.combine(d, rules.OPEN, tzinfo=ET) + timedelta(minutes=cfg["or_minutes"])

    # placebo: buy at this bar's close whether or not it broke out, same stop distance scale
    placebo = {}
    half = round((setup["or_high"] - setup["or_low"]) / 2, 2)
    for i, b in enumerate(sb):
        closes_at = b["t"] + step
        if b["t"] < range_end or not (first_ok <= closes_at <= last_ok):
            continue
        e = round(b["c"], 2)
        if half <= 0 or half < e * cfg.get("min_risk_pct", 0.05) / 100.0:
            continue
        px, _, _ = rules.simulate_exit(sb[i + 1:], e, e - half, e + cfg["target_r"] * half, flat_by, bm, gap_fills)
        placebo[closes_at.strftime("%H:%M")] = net_r(e, px, half, slip)

    trade = None
    if setup["status"] == "triggered":
        plan, bar = setup["plan"], setup["bar"]
        i = sb.index(bar)
        px, reason, when = rules.simulate_exit(sb[i + 1:], plan["entry"], plan["stop"], plan["target"], flat_by, bm, gap_fills)
        closes_at = bar["t"] + step
        trade = {
            "symbol": sym, "date": day, "time": closes_at.strftime("%H:%M"),
            "entry": plan["entry"], "stop": plan["stop"], "target": plan["target"], "risk": plan["risk"],
            "exit": px, "reason": reason, "exit_time": when.strftime("%H:%M") if when else closes_at.strftime("%H:%M"),
            "R": net_r(plan["entry"], px, plan["risk"], slip),
            "dollar_vol": sum(x["c"] * x["v"] for x in sb[:i + 1]),
        }
    return trade, placebo


def by_day(bars: list) -> dict:
    out = {}
    for b in bars:
        out.setdefault(b["t"].strftime("%Y-%m-%d"), []).append(b)
    return out


# ---------------------------------------------------------------------------
# the account, traded the way the bot trades
# ---------------------------------------------------------------------------

def simulate_account(trades: list, days: list, cfg: dict, slip: float):
    equity = float(cfg["budget"])
    curve, taken, halted_on = [], [], None
    per_day = {}
    for t in trades:
        per_day.setdefault(t["date"], []).append(t)
    floor = cfg["budget"] * (1 - cfg["halt_drawdown_pct"] / 100.0)
    for day in days:
        if equity < floor and halted_on is None:
            halted_on = day
        if halted_on is None:
            cands = sorted(per_day.get(day, []), key=lambda t: (t["time"], -t["dollar_vol"], t["symbol"]))
            open_pos, realized = [], 0.0               # (exit_time, value, pnl)
            cash = equity
            today = {"trades": 0, "risk_used": 0.0, "day_pnl": 0.0, "equity": equity}
            for t in cands:
                still = []
                for xt, value, pnl in open_pos:        # exits before this entry free their cash
                    if xt <= t["time"]:
                        cash += value + pnl
                        realized += pnl
                    else:
                        still.append((xt, value, pnl))
                open_pos = still
                today["day_pnl"] = realized
                deployed = sum(v for _, v, _ in open_pos)
                qty, _, _ = rules.size_order(t["entry"], t["stop"], cfg, cash, deployed, equity)
                if rules.risk_check(qty, t["entry"], t["stop"], t["target"], cfg, today):
                    continue
                value = qty * t["entry"]
                pnl = qty * (t["exit"] * (1 - slip) - t["entry"] * (1 + slip))
                cash -= value
                open_pos.append((t["exit_time"], value, pnl))
                today["trades"] += 1
                today["risk_used"] += qty * t["risk"]
                taken.append({**t, "qty": qty, "pnl": pnl})
            equity += realized + sum(p for _, _, p in open_pos)
        curve.append((day, equity))
    return curve, taken, halted_on


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def boot_ci(groups: dict, reps: int = 2000, seed: int = SEED):
    """Mean of all values and its 95% range, resampling whole days."""
    keys = sorted(groups)
    sums = [(sum(groups[k]), len(groups[k])) for k in keys]
    total_n = sum(n for _, n in sums)
    if not total_n:
        return None, None, None
    mean = sum(s for s, _ in sums) / total_n
    rng = random.Random(seed)
    k = len(sums)
    means = []
    for _ in range(reps):
        s = n = 0
        for _ in range(k):
            a, b = sums[rng.randrange(k)]
            s += a
            n += b
        if n:
            means.append(s / n)
    means.sort()
    return mean, means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1]


def max_drawdown(values: list) -> float:
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def cagr(first: float, last: float, d0: str, d1: str) -> float:
    years = max((date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25, 1e-9)
    return (last / first) ** (1 / years) - 1 if first > 0 and last > 0 else -1.0


def grouped(rows: list, key: str, val: str) -> dict:
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r[val])
    return out


def evaluate(signals: list, placebo: dict, days: list, cfg: dict, slip: float, bench: list):
    """All the numbers and the five checks. `bench` is [(date, close)] or []."""
    curve, taken, halted = simulate_account(signals, days, cfg, slip)
    res = {"days": len(days), "signals": len(signals), "taken": len(taken), "halted_on": halted}
    res["acct_R"] = boot_ci(grouped(taken, "date", "R"))
    res["acct_pnl_avg"] = statistics.mean(t["pnl"] for t in taken) if taken else None
    res["win_rate"] = sum(1 for t in taken if t["pnl"] > 0) / len(taken) if taken else None
    res["reasons"] = {k: sum(1 for t in taken if t["reason"] == k) for k in ("target", "stop", "flat")}
    res["sig_R"] = boot_ci(grouped(signals, "date", "R"))

    diffs = []
    for s in signals:
        peers = placebo.get((s["date"], s["time"]), [])
        if peers:
            diffs.append({"date": s["date"], "d": s["R"] - statistics.mean(peers)})
    res["edge_vs_placebo"] = boot_ci(grouped(diffs, "date", "d"))
    all_placebo = [v for vs in placebo.values() for v in vs]
    res["placebo_R"] = statistics.mean(all_placebo) if all_placebo else None

    vals = [v for _, v in curve]
    res["final_equity"] = vals[-1] if vals else cfg["budget"]
    res["acct_cagr"] = cagr(cfg["budget"], res["final_equity"], days[0], days[-1]) if days else 0.0
    res["acct_dd"] = max_drawdown([cfg["budget"]] + vals) if vals else 0.0
    res["by_year"] = {}
    for t in taken:
        y = res["by_year"].setdefault(t["date"][:4], {"n": 0, "pnl": 0.0, "R": 0.0})
        y["n"] += 1
        y["pnl"] += t["pnl"]
        y["R"] += t["R"]
    if bench:
        b = [c for d, c in bench if days[0] <= d <= days[-1]]
        res["bench_cagr"] = cagr(b[0], b[-1], days[0], days[-1]) if len(b) > 1 else None
        res["bench_dd"] = max_drawdown(b) if len(b) > 1 else None
    else:
        res["bench_cagr"] = res["bench_dd"] = None

    m, lo, _ = res["acct_R"]
    em, elo, _ = res["edge_vs_placebo"]
    res["checks"] = [
        ("At least 300 trades", res["taken"] >= 300, f"{res['taken']} trades"),
        ("Average trade above zero after costs, 95% range above zero",
         m is not None and m > 0 and lo > 0,
         "no trades" if m is None else f"average {m:+.3f} R, 95% range {lo:+.3f} to {res['acct_R'][2]:+.3f}"),
        ("Breakouts beat same-minute entries in every stock",
         em is not None and em > 0 and elo > 0,
         "no data" if em is None else f"difference {em:+.3f} R, 95% range {elo:+.3f} to {res['edge_vs_placebo'][2]:+.3f}"),
        ("Worst drop smaller than the benchmark's",
         res["bench_dd"] is not None and res["acct_dd"] > res["bench_dd"],
         f"bot {res['acct_dd']:.1%}" + ("" if res["bench_dd"] is None else f" vs benchmark {res['bench_dd']:.1%}")),
        ("Yearly return beats holding the benchmark",
         res["bench_cagr"] is not None and res["acct_cagr"] > res["bench_cagr"],
         f"bot {res['acct_cagr']:+.2%} a year" + ("" if res["bench_cagr"] is None else f" vs benchmark {res['bench_cagr']:+.2%}")),
    ]
    res["passed"] = all(ok for _, ok, _ in res["checks"])
    return res


def render(res: dict, cfg: dict, label: str, d0: str, d1: str) -> str:
    L = [f"## Desk bot backtest: {label} ({d0} to {d1})", ""]
    L.append(f"**{'PASS' if res['passed'] else 'FAIL'}** against the pass mark set in advance.")
    L.append("")
    L.append("| check | result | detail |")
    L.append("| --- | --- | --- |")
    for name, ok, detail in res["checks"]:
        L.append(f"| {name} | {'pass' if ok else 'FAIL'} | {detail} |")
    L.append("")
    L.append(f"- Trading days: {res['days']}. Breakouts found: {res['signals']}. Taken by the account "
             f"(max {cfg['max_trades_per_day']} a day): {res['taken']}.")
    if res["taken"]:
        r = res["reasons"]
        L.append(f"- Winners: {res['win_rate']:.1%}. Ended at the target {r['target']}, at the stop {r['stop']}, "
                 f"sold before the close {r['flat']}.")
        L.append(f"- Average trade: {res['acct_pnl_avg']:+.2f} USD after costs.")
    L.append(f"- Account: ${cfg['budget']:,.0f} became ${res['final_equity']:,.2f}. Worst drop {res['acct_dd']:.1%}.")
    if res["halted_on"]:
        L.append(f"- The {cfg['halt_drawdown_pct']}% drawdown halt tripped on {res['halted_on']}; no trades after that.")
    if res["sig_R"][0] is not None:
        L.append(f"- Every breakout (not only the ones taken): average {res['sig_R'][0]:+.3f} R. "
                 f"Same-minute entries in every stock: {res['placebo_R']:+.3f} R.")
    if res["by_year"]:
        L += ["", "| year | trades | result USD | average R |", "| --- | --- | --- | --- |"]
        for y in sorted(res["by_year"]):
            v = res["by_year"][y]
            L.append(f"| {y} | {v['n']} | {v['pnl']:+.2f} | {v['R'] / v['n']:+.3f} |")
    L += ["", f"Rule: first 5-minute close above the first {cfg['or_minutes']} minutes' high, between "
          f"{cfg['no_entries_before']} and {cfg['no_entries_after']} New York time; stop at the range midpoint; "
          f"target {cfg['target_r']:g}x the risk; out by {cfg['flat_by']}. Costs: {cfg['study']['slippage_bps_per_side']} "
          "hundredths of a percent each way. 1 R = the planned loss at the stop.",
          "", "The stock list is today's large companies, which flatters buy-side results. Past results do not "
          "predict future ones. Not financial advice."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_yahoo(path: str) -> list:
    """A saved Yahoo chart JSON (.json or .json.gz), for offline checks."""
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as f:
        r = json.load(f)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(r["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        out.append({"t": datetime.fromtimestamp(ts, timezone.utc).astimezone(ET), "o": round(q["open"][i], 2),
                    "h": round(q["high"][i], 2), "l": round(q["low"][i], 2), "c": round(q["close"][i], 2),
                    "v": q["volume"][i] or 0})
    return out


def run_study(symbol_bars, cfg: dict, d0: str, d1: str, bench: list, gap_fills: bool = True):
    """symbol_bars: iterable of (symbol, bars). Returns the evaluate() result."""
    slip = cfg["study"]["slippage_bps_per_side"] / 10000.0
    signals, placebo, days = [], {}, set()
    for sym, bars in symbol_bars:
        for day, rows in by_day(bars).items():
            if not (d0 <= day <= d1):
                continue
            trade, pl = analyze_day(sym, day, rows, cfg, slip, gap_fills)
            if pl or trade:
                days.add(day)
            for hm, r in pl.items():
                placebo.setdefault((day, hm), []).append(r)
            if trade:
                signals.append(trade)
    days = sorted(days)
    if not days:
        sys.exit("no usable days in that window")
    return evaluate(signals, placebo, days, cfg, slip, bench)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest the desk bot's rule.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--explore", action="store_true")
    g.add_argument("--final", action="store_true")
    p.add_argument("--out", default="study")
    args = p.parse_args(argv)

    sys.path.insert(0, str(ROOT / "bot"))
    import desk_bot
    cfg = desk_bot.load_cfg()
    st = cfg["study"]
    yesterday = (desk_bot.now_et() - timedelta(days=1)).strftime("%Y-%m-%d")
    d0, d1, label = ((st["explore_from"], st["explore_until"], "explore years") if args.explore
                     else (st["final_test_from"], yesterday, "FINAL test, locked-away years"))
    api = desk_bot.connect()
    start = datetime.combine(date.fromisoformat(d0), rules.OPEN, tzinfo=ET) - timedelta(hours=6)
    end = datetime.combine(date.fromisoformat(d1), rules.CLOSE, tzinfo=ET) + timedelta(hours=1)

    def feed():
        for n, sym in enumerate(cfg["universe"], 1):
            t0 = _time.time()
            bars = api.bars([sym], start, end, "sip").get(sym, [])
            print(f"[{n}/{len(cfg['universe'])}] {sym}: {len(bars):,} bars in {_time.time() - t0:.0f}s", flush=True)
            yield sym, bars

    b = api.bars([st["benchmark"]], start, end, "sip", timeframe="1Day", adjustment="all").get(st["benchmark"], [])
    bench = [(x["t"].astimezone(timezone.utc).strftime("%Y-%m-%d"), x["c"]) for x in b]
    res = run_study(feed(), cfg, d0, d1, bench)
    md = render(res, cfg, label, d0, d1)
    desk_bot.summary(md)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / ("explore.md" if args.explore else "final.md")).write_text(md + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
