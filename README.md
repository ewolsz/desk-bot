# Desk bot

The Day Trader skill's opening-range breakout on an Alpaca **paper** account, where nothing is bought until you tap **Approve** on your phone. It runs from GitHub, costs nothing while the repository is **public**, and needs no computer switched on.

It is a third, separate experiment. It must have its own paper account, not the rotation bot's or the sized-rotation bot's.

**It ships switched off.** A file called `STOP` blocks all alerts and orders until you delete it.

## Read this first

- The breakout rule this bot uses **failed its 2019 to 2022 backtest** in the day-bot repo. Buying on the breakout did no better than buying any stock on the list at the same minute, and after costs it lost about a tenth of the risk per trade.
- This bot does not fix that rule. It tests a different question: **do the trades you approve do better than taking every alert?** The log answers that after about 100 alerts.
- **"Success likelihood"** is the Time Machine's share of 10,000 replays in which the trade ends the day in profit: the target was hit, or the price is above your buy price at 3:55 PM. It is a simulation built from the stock's recent price moves, not a prediction, and it does not measure whether the breakout has an edge. Expect it to sit near 50% for most stocks. The full odds (target first, stop first, average R) are in `log/alerts.csv`.

## What it does each trading day

1. **9:45 AM to 3:00 PM New York:** after every 5-minute bar closes it checks 50 large stocks. A stock triggers on its first 5-minute close above the high of its first 15 minutes. Stop at the middle of that range, target at 2x the risk, size from the smallest of four limits, every risk rule checked. This part is the day bot, unchanged.
2. **Time Machine:** for each trigger it runs the skill's own Time Machine (10,000 replays, seed 19) on that stock's last ~60 sessions, and replays this bot's rule on each of those days.
3. **Phone alert:** four lines (buy at, stop loss at, sell at, success likelihood), up to four key points, and two buttons: **Approve buy** and **Skip**. At most 6 alerts a day.
4. **Your tap:** Approve within 10 minutes and it looks again (fresh price, fresh account, every risk check a second time) and only then sends one bracket order: buy + stop-loss + take-profit. At most 3 buys a day.
5. **No tap, no trade.** The alert expires. A tap after the limit is refused. So is a tap after the price ran more than half the risk past the plan, or fell to the stop.
6. **3:52 PM:** a second job sells anything still open. Nothing is held overnight.
7. **The log:** every alert goes into `log/alerts.csv` with its score and what you did. The next morning each one is scored the same way (bought at the trigger price, held to stop, target or 3:55 PM), whether you bought it or not.

## The alert

```
Desk: buy TSLA? Tap by 10:25 ET
Buy at: 364.18 (4 shares)
Stop loss at: 358.32
Sell at: 375.90
Success likelihood: 50%

Key points:
- Breakout: first close above the opening 15-minute high, at 10:15.
- Same setup on TSLA, last 59 days: 33 trades, 3 hit the target, 17 hit the stop.
- News: (a headline, only when it names the company itself)
- Volume: 1.1x the usual for this time of day (normal).
```

The numbers are real, from the TSLA data that ships with the skill (Oct 19 2026). Key points are facts, good or bad: the breakout, this setup's recent record on the stock, a headline, volume, and the market's direction. At most four are shown.

**News:** a headline is shown only when it names the company (or its ticker). Articles that are merely tagged with the ticker, such as round-ups or a story about a rival, are left out. News does not change the success likelihood.

On an iPhone, press and hold the notification to see the buttons.

## Settings (`config/desk_bot.json`)

Trading rules and risk limits are the day bot's, except the last entry time (3:00 PM instead of 10:30 AM): $5,000 budget, $50 max loss per trade, $150 per day, 3 trades a day, 30% biggest position, long only, no margin, flat by 3:55 PM.

| desk setting | value | meaning |
| --- | --- | --- |
| `approval_minutes` | 10 | how long an alert waits for your tap |
| `max_alerts_per_day` | 6 | busy mornings do not flood your phone |
| `replays`, `seed` | 10,000, 19 | the Time Machine's defaults |
| `history_days` | 90 | calendar days of 5-minute bars, about 60 sessions |
| `button_style` | `http` | change to `view` if the buttons do nothing on your phone |

Do not change settings while a test is running.

## Setup (about 15 minutes, once)

**1. A third paper account in Alpaca**
1. Log in at alpaca.markets. Click the account name at the top left.
2. **Open New Paper Account.** Name it `desk-bot`, starting amount **5000**.
3. Switch to it, find **API Keys**, **Generate**, and copy the Key and the Secret. The Secret is shown once.

**2. A new private repository on GitHub**
1. github.com, **+**, **New repository**. Name `desk-bot`. **Private.** Create it.
2. Click **uploading an existing file**.
3. Unzip `desk-bot.zip`, open the `desk-bot` folder, press **Cmd + Shift + .** so `.github` shows.
4. Select everything inside and drag it onto the page. **Commit changes.**
5. The main page should show: `.github/workflows`, `bot`, `config`, `log`, `scripts`, `tests`, `README.md`, `STOP`.

**3. Three secrets** (Settings, Secrets and variables, Actions, New repository secret)
- `ALPACA_KEY` and `ALPACA_SECRET`: the new account's.
- `NTFY_TOPIC`: the same topic your other bots use.

**4. Check the connection**
Actions, **Desk bot**, Run workflow, mode **status**. It should show equity $5,000.00 and no positions.

**5. Test the buttons**
Actions, **Desk bot**, Run workflow, mode **test-alert**. Your phone gets a test alert. Tap **Approve buy** within 5 minutes. A second alert, "Desk TEST passed", means the round trip works. If the run says FAILED, tell Claude before going further. Nothing is bought.

**6. Make the repository public**
Settings, General, scroll to the Danger Zone, **Change repository visibility**, Public. Watching until 3:00 PM takes about 7,000 GitHub minutes a month; public repositories are free, private ones share 2,000. Your keys and phone topic are secrets and stay hidden. The code, the run pages and the paper alert log become visible to anyone.

**7. Switch it on (paper)**
Delete the file `STOP`. From the next trading day you get alerts between 9:50 AM and 3:00 PM.

## Living with it

- **Pause:** add a file called `STOP` again.
- **Sell everything now:** Actions, Desk bot flatten, Run workflow, tick the box.
- **Results:** Actions, Desk bot report. It shows your real paper trades and the table "approved vs every alert".
- **Judge it after about 100 alerts,** not before.
- **Free minutes:** free only while the repository is public. If it is ever made private again, set `no_entries_after` back to `10:30` first, or it will use up the minutes your other bots need within a week.
- **Most alerts still come early.** Each stock can trigger once a day, and most do it in the first hour.

## What has and has not been checked

Checked offline by the 76 tests in `tests/`:
- The Time Machine gives the skill's own published numbers for TSLA on Oct 19 2026 (target first 29.4%, stop first 25.5%, neither 45.1%, average R +0.034; history 33 trades, 13 target, 15 stop, 5 neither).
- No tap means no order. A late tap, a tap after the price ran, a tap after the stop was hit, a tap with no current price, and a tap over the daily limits are all refused. One tap buys once.
- The whole morning loop on a fake clock, including an alert that still gets its 10 minutes at the end of the window.
- Everything the day bot already checked: sizing, risk limits, the wrong-account guard, flattening, the report.

Not checked, because it needs your phone, your keys and a live market:
- **The buttons on your iPhone.** Step 5 is the test.
- The first real conversation with Alpaca, including the news lookup and the 60-day history download. If any of those fail the alert still goes out, with "not checked" in place of the missing line.
- GitHub can start scheduled jobs late. A late start only misses early bars.
- Saving `log/alerts.csv` back to the repository. If the step "Save the alert log" fails on the first morning, tell Claude.

Known limits:
- The longer window was not backtested. The backtest that failed used the 10:30 AM cutoff.
- The Approve button works through your ntfy topic. Anyone who knew the topic name could approve a paper trade that had already passed every risk check. Fine on paper. Not good enough for real money.
- Live prices come from one exchange (IEX); the Time Machine's history is full-market when the account allows it.
- Your approvals are scored at the trigger price. A real fill a few minutes later is usually a little worse.

Not financial advice. Most people who day trade lose money.

## Files

```
bot/rules.py            the trading rules (the day bot's, unchanged)
bot/desk_bot.py         run, test-alert, flatten, status, report
bot/desk.py             the Time Machine wrapper, the alert, the replies, the log
bot/timemachine.py      the skill's Time Machine, unchanged (MIT, Jake Schincariol)
config/desk_bot.json    every setting and the stock list
log/alerts.csv          every alert, what you did, and what the plan went on to do
scripts/orb_study.py    the backtest code (used by the tests; the rule already failed it)
tests/                  76 offline tests
.github/workflows/      the three schedules
STOP                    the off switch
```
