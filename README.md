# algo-trading-prop-firm-ema

A 9/20 EMA trend-pullback-and-resumption day-trading strategy, operated under the same simulated prop-firm compliance rulebook as [`algo-trading-prop-firm`](https://github.com/esteedugal/algo-trading-prop-firm) (5-min Opening Range Breakout) — same $25k tier, same daily-loss/drawdown/consistency/volume rules, run against its own separate Alpaca paper account so the two signals can be compared head-to-head under identical constraints. It's the closest variant of an already-built sibling in this project family: the compliance layer (`tools/trading/rules_engine.py`, `order_manager.py`, `agents/orchestrator/reconcile.py`, `tools/market_data/intraday_bars.py`, `earnings_data.py`, `config/universe.py`) is ported byte-for-byte unchanged from the ORB sibling — the real design work here is entirely in the new signal logic.

## Strategy

1. **Trend**: 9 EMA vs 20 EMA on 5-minute bars. 9 above 20 = uptrend (long bias); below = downtrend (short bias).
2. **Clean-trend filter**: the relationship must have held for at least 12 consecutive 5-min bars (1 trading hour) AND the spread between the two EMAs must be at least 0.5× ATR(14) — self-scales per name's own volatility rather than a fixed dollar threshold. Stand aside on flat/tangled/choppy days, per the rulebook.
3. **Entry**: wait for price to pull back and trade through the 9 or 20 EMA, then enter on a **close** beyond the high (long) or low (short) of that pullback bar.
4. **Stop**: the tighter of (the EMA just used) or (the nearest swing low/high within the current trend's own lifetime), pushed slightly further out so noise doesn't immediately re-trigger it.
5. **Target**: the prior swing high/low, computed from a **wider, independent lookback window** — not bounded to the current trend's own start like the stop is (see Design Notes for why this distinction matters). A setup is skipped if the resulting reward is less than 1.5× the risk.
6. **Multiple setups per ticker per day**: unlike the ORB sibling (one measured range, one shot, done for the day), this strategy continuously tracks trend state — a ticker can trend, pull back, resume, close, and set up again later the same session.
7. **Exit**: target or stop, whichever hits first; flatten 10 minutes before close regardless.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config/.env.example config/.env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY / ANTHROPIC_API_KEY
```

## Usage

```bash
venv/bin/python agents/orchestrator/tick.py                 # one tick (also what cron invokes every trading minute)
venv/bin/python agents/orchestrator/eod_report.py            # today's EOD report
venv/bin/python backtest/run.py --days 60                     # backtest, validates the clean-trend thresholds
venv/bin/python backtest/run.py --days 60 --sweep             # + compares looser/stricter threshold alternatives

venv/bin/python tests/test_ema_trend_signal.py   # signal math regression tests
venv/bin/python tests/test_rules_engine.py       # compliance engine tests (ported verbatim, should pass unchanged)
```

## What's ported verbatim from the ORB sibling (zero changes)

`tools/trading/rules_engine.py`, `order_manager.py`, `agents/orchestrator/reconcile.py`, `tools/market_data/intraday_bars.py`, `earnings_data.py`, `config/universe.py`, and the `account_state`/`daily_state`/`positions`/`breaches`/`eod_reports` tables (plus all their CRUD) in `position_store.py`. None of it ever referenced ORB-specific concepts — it operates on generic `stop_price`/`entry_trigger` dicts regardless of what produced them. Same $25k tier, same $500 daily loss limit / $1,500 max drawdown with the same one-way 3× ratchet — confirmed to reuse the ORB sibling's already-user-confirmed figures rather than re-litigating them.

## Design notes

- **Two separate tables replace ORB's single write-once `candidates`**: `trend_state` (one row per ticker per day, continuously mutated as new 5-min bars arrive — phase: `no_trend → clean_trend → watching_resumption → position_open → back to clean_trend/no_trend`) and `trend_setups` (one row per distinct pullback→resumption cycle — deliberately no `UNIQUE(trading_date, ticker)` constraint, since a ticker can generate several in one day). The `positions` table's own partial unique index already only blocks *concurrent* open positions per ticker, never same-day sequential re-entry, so nothing there needed to change.
- **The stop's swing lookback is bounded by the current trend's own start** (`trend_started_at`) — a swing point from before the trend began isn't a real reference level for a stop. **The target's lookback is deliberately NOT bounded the same way** — it uses a wider, independent window (`TARGET_LOOKBACK_BARS=60` vs the stop's `SWING_LOOKBACK_BARS=20`). This distinction was learned the hard way: applying the same trend-start bound to both was the initial implementation, and it produced a target that collapses to something trivially close to entry early in a fresh trend (there's no "prior" level yet within such a short window) — this alone drove an implausible 68.9% backtest win rate on the first real run. Fixed by decoupling the two lookbacks and adding a `MIN_TARGET_RISK_REWARD=1.5` safety net (skip any setup where the resulting target isn't at least 1.5× the stop distance away) as a second line of defense.
- **Entry trigger uses a 1-minute bar's CLOSE**, not a raw latest-trade tick like the ORB sibling — agent.md's literal wording is "enter on a close beyond...," and this project takes that literally rather than reusing ORB's tick-level trigger unmodified.
- **`_flatten_all()` and the whole force-close path are ported unchanged**, including every fix found live in the ORB sibling on its first trading day: the settle-delay before flattening (cancelling a bracket leg doesn't instantly release Alpaca's qty hold), the retry-vs-still-pending distinction (retrying a genuinely-failed order is safe; retrying an order that's just slow to fill risks a `client_order_id` collision or a double-submission), and never marking a position closed without a confirmed fill.

## Backtest — built for v1, with real caveats

Unlike the ORB sibling (skipped a backtest — its "stocks in play" volume screener has a real hindsight-bias problem when replayed historically), this strategy has no daily selection step: the universe is fixed every day, same as live, and every signal function is already pure over bar data — `backtest/engine.py` replays them bar-by-bar with no look-ahead, reusing `rules_engine.py`'s sizing/loss-limit/drawdown functions directly so backtest and live logic can't silently drift apart.

**Known simplifications** (stated plainly, not hidden): the resumption-breakout check uses the next 5-min bar's close rather than a 1-min close (avoids fetching a full 1-min history for the whole backtest window); fill price is assumed to be exactly the entry trigger (no slippage modeled — this is somewhat optimistic in both directions, since a real fill on a confirmed breakout typically happens at or past the trigger, not exactly at it); a same-bar stop-and-target overlap assumes the stop hit first (conservative); the 5% volume cap uses bar-volume/5 as a proxy for "prior one-minute volume."

**A first 20-day run surfaced a real bug**: after fixing the target-lookback issue above, the win rate dropped from an implausible 68.9% to a much more believable 47.4%, with a realistic 2.7:1 average win/loss ratio — consistent with a genuine trend-following "cut losses, let winners run" profile rather than an artifact. P&L was reasonably diversified (the top 10 of 104 tickers accounted for only ~40% of total profit, not a single-name fluke). **But every one of the 13 trading days in that window was profitable**, and the largest contributors (KLAC, MU, INTC, TSLA, ON, ASML, AMD, TSM) point to a genuine semiconductor-sector trending rally during that exact stretch — a favorable market regime, not a repeatable guarantee. Thirteen trading days is too small a sample to treat the aggregate return as an expectation. Treat this backtest as confirmation that the mechanics behave sensibly after the fix, not as a forecast — the real test is forward paper-trading, same as every sibling project's actual validation method.

## Known limitations

- **IEX-only volume** (same free-tier limitation as every sibling project) — the 5%-of-volume cap is conservative relative to true market liquidity.
- **No halt-status API** — the same self-computed >10%-move-in-5-minutes proxy as the ORB sibling, with exchange-level order rejection as the real backstop.
- **~60-second breakout-detection lag**, inherent to 1-minute cron cadence.
- **Consistency rule has no pre-trade lever** — same as the ORB sibling, a fixed target means there's no mechanism to cap a single winner's share of total profit before the fact; only monitored and self-reported after.
- **Two genuinely arbitrary thresholds** (`CLEAN_TREND_MIN_BARS_SINCE_CROSS=12`, `CLEAN_TREND_MIN_SPREAD_ATR_MULT=0.5`) — validated by the backtest above only in the loose sense of "producing a sane trade profile after the target-lookback fix," not tuned/optimized. `backtest/run.py --sweep` compares looser and stricter alternatives if that comparison becomes useful later.
