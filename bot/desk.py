"""The approval desk: everything that turns a breakout into a question for a person.

Pure functions, no network. desk_bot.py does the talking to Alpaca and ntfy.

  replay()      the Day Trader skill's Time Machine (bot/timemachine.py, unchanged,
                MIT, Jake Schincariol) on one plan: 10,000 replays of the rest of
                the day built from the stock's own past sessions, plus the real
                record of this bot's rule on those sessions.
  context()     the lines for and against, from numbers only.
  alert_text()  what the phone shows.
  parse_*()     reading the tap that comes back.
  log + score   every alert is written down, tapped or not, and later scored the
                same way, so "did my approvals beat taking every alert?" has an answer.

The Time Machine's odds are a simulation from recent price moves. They are not a
prediction and they do not measure whether the breakout signal has an edge.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import rules
import timemachine as tm
from rules import ET, hhmm

LOG_FIELDS = [
    "date", "symbol", "triggered", "entry", "stop", "target", "qty", "risk_usd",
    "tm_target_pct", "tm_stop_pct", "tm_neither_pct", "tm_green_pct", "tm_avg_r", "tm_sessions",
    "hist_trades", "hist_target", "hist_stop", "hist_flat", "hist_avg_r",
    "rel_volume", "spy_pct", "headlines",
    "decision", "decided", "note", "buy_ref",
    "outcome", "outcome_r",
]
BOUGHT = "bought"


# ---------------------------------------------------------------------------
# the Time Machine
# ---------------------------------------------------------------------------

def _slot(t: datetime) -> int:
    return (t.hour * 60 + t.minute - tm.OPEN_MIN) // 5


def by_day(bars: list) -> dict:
    out = {}
    for b in bars:
        out.setdefault(b["t"].strftime("%Y-%m-%d"), []).append(b)
    return out


def tm_sessions(bars: list, before: str) -> list:
    """Our bars -> the Time Machine's full sessions (all 78 bars) strictly before `before`."""
    days = {}
    for b in rules.session_bars(bars, 5):
        d = b["t"].strftime("%Y-%m-%d")
        s = _slot(b["t"])
        days.setdefault(d, {})[s] = {"date": d, "slot": s, "start": b["t"].strftime("%H:%M"),
                                     "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
    full, _ = tm.full_sessions(days, before)
    return full


def rule_history(bars: list, cfg: dict, before: str, dates: set | None = None) -> dict:
    """This bot's own rule (same window, stop, target, flat-by) on each past session.
    R is +target_r at the target, -1 at the stop, else the distance at the flat-by time."""
    flat_by = hhmm(cfg["flat_by"])
    bm = cfg["bar_minutes"]
    out = {"trades": 0, "target": 0, "stop": 0, "flat": 0, "avg_r": None, "sessions": 0}
    rs = []
    for d, rows in sorted(by_day(bars).items()):
        if d >= before or (dates is not None and d not in dates):
            continue
        out["sessions"] += 1
        sb = rules.session_bars(rows, bm)
        setup = rules.day_setup(sb, cfg)
        if not setup or setup["status"] != "triggered":
            continue
        plan = setup["plan"]
        i = sb.index(setup["bar"])
        px, why, _ = rules.simulate_exit(sb[i + 1:], plan["entry"], plan["stop"], plan["target"], flat_by, bm,
                                         gap_fills=False)
        out["trades"] += 1
        out[why] += 1
        rs.append((px - plan["entry"]) / plan["risk"])
    if rs:
        out["avg_r"] = round(sum(rs) / len(rs), 3)
    return out


def replay(history_bars: list, plan: dict, trigger_bar: dict, cfg: dict, day: str,
           replays: int = 10000, seed: int = 19) -> dict:
    """The Time Machine for one plan. {"ok": False, "why": ...} when it cannot run."""
    if cfg["bar_minutes"] != 5:
        return {"ok": False, "why": "the Time Machine needs 5-minute bars"}
    sessions = tm_sessions(history_bars, day)
    if len(sessions) < tm.MIN_SESSIONS:
        return {"ok": False, "why": f"only {len(sessions)} full past sessions in the data; it needs {tm.MIN_SESSIONS}"}
    k = _slot(trigger_bar["t"])
    flat = hhmm(cfg["flat_by"])
    last = (flat.hour * 60 + flat.minute - tm.OPEN_MIN) // 5 - 1
    if last <= k:
        return {"ok": False, "why": "nothing left of the day to replay"}
    mc = tm.simulate(tm.build_pool(sessions), plan["entry"], plan["stop"], plan["target"], k, last, replays, seed)
    mc = {key: v for key, v in mc.items() if key not in ("bands", "sample_paths", "hist")}
    mc["until"] = cfg["flat_by"]
    return {
        "ok": True, "mc": mc, "sessions": len(sessions), "from": sessions[0][0], "to": sessions[-1][0],
        "verdict": tm.verdict(mc), "notes": tm.verdict_notes(mc), "lean": tm.lean(mc),
        "history": rule_history(history_bars, cfg, day, {d for d, _ in sessions}),
    }


# ---------------------------------------------------------------------------
# the lines for and against
# ---------------------------------------------------------------------------

def rel_volume(today_bars: list, history_bars: list, trigger_bar: dict, day: str, min_days: int = 10):
    """Today's volume from the open through the trigger bar, over the average of
    the same stretch on past days. Both must come from the same data feed."""
    cut = trigger_bar["t"].timetz().replace(tzinfo=None)
    today = sum(b["v"] for b in rules.session_bars(today_bars, 5) if b["t"].timetz().replace(tzinfo=None) <= cut)
    past = []
    for d, rows in by_day(history_bars).items():
        if d >= day:
            continue
        v = sum(b["v"] for b in rules.session_bars(rows, 5) if b["t"].timetz().replace(tzinfo=None) <= cut)
        if v > 0:
            past.append(v)
    if len(past) < min_days or today <= 0:
        return None
    return round(today / (sum(past) / len(past)), 2)


def market_move(spy_bars: list, now: datetime):
    """SPY's move from today's open to its last completed bar, in percent."""
    sb = rules.completed(rules.session_bars(spy_bars, 5), now, 5)
    if not sb or sb[0]["o"] <= 0:
        return None
    return round(100.0 * (sb[-1]["c"] / sb[0]["o"] - 1.0), 2)


def context(tmres: dict, relvol, spy_pct, headlines: list | None) -> dict:
    """{"for": [...], "against": [...], "note": [...]}. Numbers only, no opinions."""
    pro, con, note = [], [], []
    if tmres.get("ok"):
        mc, lv, h = tmres["mc"], tmres["lean"], tmres["history"]
        if lv is None:
            note.append("No replay reached the target or the stop.")
        else:
            d, share, even = lv
            line = f"target came first in {100 * share:.0f}% of the replays that hit a side (break-even is {100 * even:.0f}%)"
            if d >= tm.COIN_FLIP:
                pro.append("Replays lean to the target: " + line + ".")
            elif d <= -tm.COIN_FLIP:
                con.append("Replays lean to the stop: " + line + ".")
            else:
                con.append("Replays show no edge: " + line + ".")
        if h["trades"] < 10:
            note.append(f"Only {h['trades']} past trades of this rule on this stock: too few to judge.")
        elif h["avg_r"] >= 0.1:
            pro.append(f"This rule made money on this stock lately: average {h['avg_r']:+.2f} R over {h['trades']} trades.")
        elif h["avg_r"] <= -0.1:
            con.append(f"This rule lost money on this stock lately: average {h['avg_r']:+.2f} R over {h['trades']} trades.")
        else:
            con.append(f"This rule was flat on this stock lately: average {h['avg_r']:+.2f} R over {h['trades']} trades.")
    else:
        note.append("No Time Machine score: " + tmres.get("why", "not available") + ".")
    if relvol is not None:
        if relvol >= 1.5:
            pro.append(f"Heavy volume: {relvol:.1f}x a normal morning.")
        elif relvol < 0.8:
            con.append(f"Light volume: {relvol:.1f}x a normal morning. Quiet breakouts fail more often.")
    if spy_pct is not None:
        if spy_pct >= 0.15:
            pro.append(f"The market is up {spy_pct:+.2f}% since the open.")
        elif spy_pct <= -0.15:
            con.append(f"The market is down {spy_pct:+.2f}% since the open.")
    if headlines is None:
        note.append("News not checked.")
    elif headlines:
        first = headlines[0].strip().replace("\n", " ")
        if len(first) > 90:
            first = first[:87] + "..."
        note.append(f"{len(headlines)} headline(s) since yesterday's close. Latest: {first}")
    else:
        note.append("No headlines since yesterday's close.")
    return {"for": pro, "against": con, "note": note}


def chase_limit(stop: float, target: float, cfg: dict) -> float:
    """The highest price the bot will still buy at: above it, reward:risk is under the minimum."""
    m = cfg["min_reward_risk"]
    return math.floor((target + m * stop) / (1 + m) * 100 + 1e-6) / 100.0


def alert_text(o: dict, tmres: dict, ctx: dict, expires: datetime, cfg: dict) -> tuple:
    """(title, body) for the phone."""
    gain = o["qty"] * (o["target"] - o["ref"])
    title = f"Desk: buy {o['symbol']}? Tap by {expires:%H:%M} ET"
    lines = [
        f"Buy {o['qty']} {o['symbol']} near {o['ref']:.2f} | stop {o['stop']:.2f} | target {o['target']:.2f}",
        f"Max loss ${o['risk_usd']:.2f}, possible gain ${gain:.2f}. Will not buy above {chase_limit(o['stop'], o['target'], cfg):.2f}.",
        "",
    ]
    if tmres.get("ok"):
        mc, h = tmres["mc"], tmres["history"]
        lines += [
            f"TIME MACHINE ({mc['replays']:,} replays, seed {mc['seed']}, {tmres['sessions']} past sessions)",
            f"Target first {100 * mc['p_target']:.1f}% | stop first {100 * mc['p_stop']:.1f}% | "
            f"neither {100 * mc['p_neither']:.1f}%",
            f"Ends green {100 * mc['p_green']:.1f}% (target hit, or above your entry at {cfg['flat_by']}) | "
            f"average R {mc['avg_R']:+.3f}",
            tmres["verdict"],
        ] + list(tmres["notes"])
        if h["trades"]:
            lines.append(f"History: this rule fired on {h['trades']} of {h['sessions']} days: {h['target']} target, "
                         f"{h['stop']} stop, {h['flat']} neither. Average R {h['avg_r']:+.3f}.")
        else:
            lines.append(f"History: this rule did not fire on any of the {h['sessions']} past days.")
        lines.append("")
    for head, key, mark in (("FOR", "for", "+ "), ("AGAINST", "against", "- "), ("NOTE", "note", "* ")):
        if ctx[key]:
            lines.append(head)
            lines += [mark + x for x in ctx[key]]
    lines += ["", "Paper account. A simulation, not a prediction. This rule failed its 2019-2022 backtest."]
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# the tap that comes back
# ---------------------------------------------------------------------------

def reply_text(action: str, symbol: str, day: str) -> str:
    return f"{action} {symbol} {day.replace('-', '')}"


def parse_reply(text: str):
    """"approve TSLA 20261112" -> ("approve", "TSLA", "2026-11-12"), else None."""
    parts = (text or "").strip().split()
    if len(parts) != 3 or parts[0].lower() not in ("approve", "skip"):
        return None
    d = parts[2]
    if len(d) != 8 or not d.isdigit():
        return None
    return parts[0].lower(), parts[1].upper(), f"{d[:4]}-{d[4:6]}-{d[6:]}"


def parse_poll(text: str) -> list:
    """ntfy's poll output (one JSON object per line) -> [{"id", "time", "message"}], oldest first."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "message" and ev.get("id"):
            out.append({"id": ev["id"], "time": ev.get("time", 0), "message": ev.get("message", "")})
    return sorted(out, key=lambda e: e["time"])


# ---------------------------------------------------------------------------
# the log, and scoring it afterwards
# ---------------------------------------------------------------------------

def load_log(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return [{k: r.get(k, "") for k in LOG_FIELDS} for r in csv.DictReader(f)]


def save_log(path: Path, rows: list) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LOG_FIELDS})


def log_row(day: str, o: dict, tmres: dict, relvol, spy_pct, headlines) -> dict:
    row = {k: "" for k in LOG_FIELDS}
    row.update({"date": day, "symbol": o["symbol"], "triggered": o["triggered"], "entry": f"{o['entry']:.2f}",
                "stop": f"{o['stop']:.2f}", "target": f"{o['target']:.2f}", "qty": str(o["qty"]),
                "risk_usd": f"{o['risk_usd']:.2f}", "decision": "pending",
                "rel_volume": "" if relvol is None else str(relvol),
                "spy_pct": "" if spy_pct is None else str(spy_pct),
                "headlines": "" if headlines is None else str(len(headlines))})
    if tmres.get("ok"):
        mc, h = tmres["mc"], tmres["history"]
        row.update({"tm_target_pct": f"{100 * mc['p_target']:.1f}", "tm_stop_pct": f"{100 * mc['p_stop']:.1f}",
                    "tm_neither_pct": f"{100 * mc['p_neither']:.1f}",
                    "tm_green_pct": f"{100 * mc['p_green']:.1f}", "tm_avg_r": f"{mc['avg_R']:.3f}",
                    "tm_sessions": str(tmres["sessions"]), "hist_trades": str(h["trades"]),
                    "hist_target": str(h["target"]), "hist_stop": str(h["stop"]), "hist_flat": str(h["flat"]),
                    "hist_avg_r": "" if h["avg_r"] is None else f"{h['avg_r']:.3f}"})
    return row


def score_row(row: dict, day_bars: list, cfg: dict):
    """What the plan did that day if bought at the trigger close: (outcome, R), or
    None when the day's bars are not all there yet. The same yardstick for every
    alert, bought or not, so the two groups can be compared."""
    bm = cfg["bar_minutes"]
    sb = rules.session_bars(day_bars, bm)
    flat_by = hhmm(cfg["flat_by"])
    if not sb or (sb[-1]["t"] + timedelta(minutes=bm)).timetz().replace(tzinfo=None) < flat_by:
        return None
    d = sb[0]["t"].date()
    closes_at = datetime.combine(d, hhmm(row["triggered"]), tzinfo=ET)
    after = [b for b in sb if b["t"] >= closes_at]
    entry, stop, target = float(row["entry"]), float(row["stop"]), float(row["target"])
    px, why, _ = rules.simulate_exit(after, entry, stop, target, flat_by, bm)
    return why, round((px - entry) / (entry - stop), 3)


def compare(rows: list) -> str:
    """Markdown: every scored alert against the ones bought and the ones passed on."""
    scored = [r for r in rows if r.get("outcome")]

    def line(name, grp):
        n = len(grp)
        if n == 0:
            return f"| {name} | 0 | | | | |"
        rs = [float(r["outcome_r"]) for r in grp]
        c = {k: sum(1 for r in grp if r["outcome"] == k) for k in ("target", "stop", "flat")}
        return f"| {name} | {n} | {c['target']} | {c['stop']} | {c['flat']} | {sum(rs) / n:+.2f} |"

    bought = [r for r in scored if r["decision"] == BOUGHT]
    passed = [r for r in scored if r["decision"] != BOUGHT]
    md = [
        "## Did your approvals beat taking every alert?", "",
        "Each alert is scored the same way: bought at the trigger price, held to the stop, the target or "
        f"the flat-by time. R is the result in units of the planned loss.", "",
        "| group | alerts | target | stop | neither | average R |",
        "| --- | --- | --- | --- | --- | --- |",
        line("Every alert", scored), line("You approved (bought)", bought), line("Not bought", passed), "",
    ]
    waiting = len(rows) - len(scored)
    if waiting:
        md.append(f"{waiting} alert(s) not scored yet.")
    if len(scored) < 100:
        md.append(f"{len(scored)} scored alerts so far. Under about 100, the difference between the rows is mostly noise.")
    return "\n".join(md)
