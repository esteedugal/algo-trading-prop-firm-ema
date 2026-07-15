"""
Backtest Engine
------------------
Replays ema_trend/signal.py's exact pure functions bar-by-bar against
historical 5-min data, with no look-ahead (at bar i, only bars[:i+1] are
ever passed to any signal function). Reuses tools/trading/rules_engine.py's
position-sizing/loss-limit/drawdown functions directly — the same
functions the live system calls — so there's no risk of backtest logic
silently drifting from live logic.

Primary purpose: validate CLEAN_TREND_MIN_BARS_SINCE_CROSS and
CLEAN_TREND_MIN_SPREAD_ATR_MULT (the two genuinely arbitrary defaults in
config/settings.py) against real historical data before risking paper
capital on unvalidated numbers — not to produce a headline return figure.

Simplifications, stated plainly rather than hidden:
- The resumption-breakout check uses the NEXT 5-min bar's close, not a
  1-min bar's close like the live system (avoids needing to also fetch/
  store a full 1-min bar history for the whole backtest window). Same
  underlying detect_breakout function and strict-inequality convention.
- Fill price on a triggered breakout is assumed to be exactly the
  entry_trigger price (no slippage modeled).
- Bracket-fill resolution: if a single bar's [low, high] range contains
  BOTH the stop and target, the STOP is assumed to hit first (conservative).
- Volume cap uses the 5-min bar's own volume / 5 as a proxy for "prior
  one-minute volume" (the live system's literal input) — an approximation.
- The drawdown ratchet is tracked but not exercised through
  apply_drawdown_ratchet's persistence path (in-memory only, no DB) —
  fine for a backtest, which never writes to position_store.
"""

from typing import Optional
import pandas as pd

from ema_trend.signal import (
    compute_ema, compute_atr, classify_trend, compute_clean_trend,
    detect_pullback_touch, detect_trend_failure, compute_swing_reference,
    compute_stop_target, detect_breakout,
)
from tools.trading.rules_engine import (
    compute_position_size, compute_final_qty, check_daily_loss_limit, check_max_drawdown,
)
from config.settings import (
    EMA_FAST_SPAN, EMA_SLOW_SPAN, ATR_PERIOD,
    CLEAN_TREND_MIN_BARS_SINCE_CROSS, CLEAN_TREND_MIN_SPREAD_ATR_MULT,
    SWING_LOOKBACK_BARS, TARGET_LOOKBACK_BARS, MIN_TARGET_RISK_REWARD,
    STOP_BUFFER_PCT, VOLUME_CAP_PCT, RISK_PER_TRADE_PCT,
    TIER_INITIAL_BALANCE, TIER_INTRADAY_BUYING_POWER, DAILY_LOSS_LIMIT_DOLLARS,
    MAX_DRAWDOWN_DOLLARS, DRAWDOWN_RATCHET_MULTIPLE,
)


class _TickerState:
    """In-memory equivalent of a trend_state row — no DB I/O, for backtest speed."""
    __slots__ = ("phase", "direction", "trend_started_at", "active_setup", "cycles_today")

    def __init__(self):
        self.phase = "no_trend"
        self.direction = None
        self.trend_started_at = None
        self.active_setup = None
        self.cycles_today = 0


def _target_for(bars, direction, lookback_n):
    """Deliberately UNBOUNDED by trend_started_at, unlike the stop -- see
    config/settings.py's TARGET_LOOKBACK_BARS docstring for why."""
    kind = "swing_high" if direction == "long" else "swing_low"
    return compute_swing_reference(bars, kind, lookback_n, floor_ts=None)


def _stop_for(bars, direction, floor_ts, lookback_n):
    kind = "swing_low" if direction == "long" else "swing_high"
    return compute_swing_reference(bars, kind, lookback_n, floor_ts)


def _build_setup(bars_so_far, latest_bar, direction, ema_used, ema9, ema20, trend_started_at,
                  swing_lookback_n, target_lookback_n, stop_buffer_pct, min_target_risk_reward):
    entry_trigger = float(latest_bar["high"]) if direction == "long" else float(latest_bar["low"])
    ema_stop_price = ema9 if ema_used == "ema9" else ema20
    floor_ts = pd.Timestamp(trend_started_at) if trend_started_at is not None else None
    swing_stop = _stop_for(bars_so_far, direction, floor_ts, swing_lookback_n)
    swing_target = _target_for(bars_so_far, direction, target_lookback_n)
    return compute_stop_target(
        entry_trigger=entry_trigger, ema_stop_price=ema_stop_price,
        swing_stop_price=swing_stop["price"], swing_target_price=swing_target["price"],
        direction=direction, stop_buffer_pct=stop_buffer_pct, min_target_risk_reward=min_target_risk_reward,
    )


def run_backtest_for_ticker(
    ticker: str,
    bars: pd.DataFrame,
    account_state: dict,
    daily_states: dict,
    min_bars_since_cross: int = CLEAN_TREND_MIN_BARS_SINCE_CROSS,
    min_spread_atr_mult: float = CLEAN_TREND_MIN_SPREAD_ATR_MULT,
    swing_lookback_n: int = SWING_LOOKBACK_BARS,
    target_lookback_n: int = TARGET_LOOKBACK_BARS,
    stop_buffer_pct: float = STOP_BUFFER_PCT,
    min_target_risk_reward: float = MIN_TARGET_RISK_REWARD,
) -> list:
    """
    Returns a list of trade dicts for this ticker. account_state and
    daily_states are mutated in place so a multi-ticker backtest shares
    one running compliance ledger, same as live.
    """
    trades = []
    state = _TickerState()
    open_position = None

    if bars.empty or len(bars) < ATR_PERIOD + 2:
        return trades

    ema9_series = compute_ema(bars, EMA_FAST_SPAN)
    ema20_series = compute_ema(bars, EMA_SLOW_SPAN)
    atr_series = compute_atr(bars, ATR_PERIOD)
    warmup = ATR_PERIOD + 1

    for i in range(warmup, len(bars)):
        bar = bars.iloc[i]
        bar_ts = bars.index[i]
        trading_date = bar_ts.strftime("%Y-%m-%d")

        ema9, ema20 = float(ema9_series.iloc[i]), float(ema20_series.iloc[i])
        direction = classify_trend(ema9, ema20)

        # ---- manage an already-open position first ----
        if open_position is not None:
            low, high = float(bar["low"]), float(bar["high"])
            stop, target = open_position["stop"], open_position["target"]
            exit_price, exit_reason = None, None

            if open_position["direction"] == "long":
                stop_hit, target_hit = low <= stop, high >= target
            else:
                stop_hit, target_hit = high >= stop, low <= target

            if stop_hit:
                exit_price, exit_reason = stop, "stop_hit"   # conservative: stop wins on same-bar ties
            elif target_hit:
                exit_price, exit_reason = target, "target_hit"

            is_last_bar_of_day = (i + 1 >= len(bars)) or (bars.index[i + 1].strftime("%Y-%m-%d") != trading_date)
            if exit_price is None and is_last_bar_of_day:
                exit_price, exit_reason = float(bar["close"]), "eod_flatten"

            if exit_price is not None:
                direction_sign = 1.0 if open_position["direction"] == "long" else -1.0
                pnl_dollar = (exit_price - open_position["entry_price"]) * open_position["qty"] * direction_sign
                trades.append({
                    "ticker": ticker, "trading_date": open_position["trading_date"],
                    "direction": open_position["direction"], "entry_price": open_position["entry_price"],
                    "exit_price": exit_price, "exit_reason": exit_reason, "qty": open_position["qty"],
                    "pnl_dollar": pnl_dollar, "cycle": open_position["cycle"],
                })
                ds = daily_states[open_position["trading_date"]]
                ds["realized_pnl_running"] += pnl_dollar
                account_state["cumulative_realized_pnl"] += pnl_dollar
                open_position = None
                state.phase = "clean_trend" if direction in ("long", "short") else "no_trend"
                state.active_setup = None
            else:
                continue  # still open -- nothing else to evaluate for this ticker this bar

        # ---- trend-state machine, mirrors tick.py's _update_trend_states ----
        flipped = state.direction not in (None, "none") and direction not in (None, "none") and direction != state.direction
        if flipped:
            state.phase = "no_trend"
            state.trend_started_at = bar_ts
            state.active_setup = None
        if state.trend_started_at is None or direction != state.direction:
            state.trend_started_at = bar_ts
        state.direction = direction

        if state.phase in ("no_trend", "clean_trend") and direction in ("long", "short"):
            clean = compute_clean_trend(
                ema9_series.iloc[:i + 1], ema20_series.iloc[:i + 1],
                atr_series.iloc[:i + 1], bars["close"].iloc[:i + 1],
                min_bars_since_cross, min_spread_atr_mult,
            )
            state.phase = "clean_trend" if clean["is_clean"] else "no_trend"
        elif direction not in ("long", "short"):
            state.phase = "no_trend"

        bars_so_far = bars.iloc[:i + 1]

        if state.phase == "clean_trend":
            touch = detect_pullback_touch(bar, ema9, ema20, direction)
            if touch["touched"]:
                try:
                    st = _build_setup(bars_so_far, bar, direction, touch["ema_used"], ema9, ema20,
                                       state.trend_started_at, swing_lookback_n, target_lookback_n,
                                       stop_buffer_pct, min_target_risk_reward)
                    state.cycles_today += 1
                    state.active_setup = {**st, "direction": direction, "cycle": state.cycles_today}
                    state.phase = "watching_resumption"
                except ValueError:
                    pass  # no valid stop/target this bar -- stay in clean_trend, try again next bar

        elif state.phase == "watching_resumption" and state.active_setup is not None:
            bar_close = float(bar["close"])
            if detect_trend_failure(bar_close, ema9, ema20, direction):
                state.phase = "no_trend"
                state.active_setup = None
            else:
                touch = detect_pullback_touch(bar, ema9, ema20, direction)
                if touch["touched"]:
                    try:
                        st = _build_setup(bars_so_far, bar, direction, touch["ema_used"], ema9, ema20,
                                           state.trend_started_at, swing_lookback_n, target_lookback_n,
                                           stop_buffer_pct, min_target_risk_reward)
                        state.active_setup.update(st)
                    except ValueError:
                        pass

                if detect_breakout(bar_close, state.active_setup["entry_trigger"], direction):
                    daily_states.setdefault(trading_date, {
                        "realized_pnl_running": 0.0,
                        "start_of_day_equity": account_state["initial_balance"] + account_state["cumulative_realized_pnl"],
                    })
                    ds = daily_states[trading_date]
                    current_equity = account_state["initial_balance"] + account_state["cumulative_realized_pnl"]
                    daily_check = check_daily_loss_limit(ds, current_equity, DAILY_LOSS_LIMIT_DOLLARS)
                    drawdown_check = check_max_drawdown(account_state, current_equity)

                    stop_distance = abs(state.active_setup["entry_trigger"] - state.active_setup["stop_price"])
                    risk_qty = compute_position_size(DAILY_LOSS_LIMIT_DOLLARS, RISK_PER_TRADE_PCT, stop_distance)
                    prior_minute_volume = float(bar["volume"]) / 5.0
                    sizing = compute_final_qty(risk_qty, prior_minute_volume, VOLUME_CAP_PCT, TIER_INTRADAY_BUYING_POWER, bar_close)
                    qty = sizing["final_qty"]
                    risk_dollars = qty * stop_distance

                    approved = (
                        qty > 0 and not daily_check["breached"] and not drawdown_check["breached"]
                        and risk_dollars <= daily_check["remaining_budget"]
                        and risk_dollars <= drawdown_check["buffer_remaining"]
                    )
                    if approved:
                        open_position = {
                            "entry_price": state.active_setup["entry_trigger"], "stop": state.active_setup["stop_price"],
                            "target": state.active_setup["target_price"], "direction": direction, "qty": qty,
                            "trading_date": trading_date, "cycle": state.active_setup["cycle"],
                        }
                        state.phase = "position_open"
                    else:
                        state.phase = "clean_trend"
                        state.active_setup = None

    return trades


def run_backtest(bars_by_ticker: dict, initial_balance: float = TIER_INITIAL_BALANCE, **kwargs) -> dict:
    """Runs all tickers against ONE shared account_state/daily_states ledger
    (same compliance budget contention as live), returns {trades, final_pnl, account_state}."""
    account_state = {
        "initial_balance": initial_balance,
        "cumulative_realized_pnl": 0.0,
        "max_drawdown_dollars": MAX_DRAWDOWN_DOLLARS,
        "drawdown_ratchet_multiple": DRAWDOWN_RATCHET_MULTIPLE,
        "is_ratcheted": False,
    }
    daily_states: dict = {}
    all_trades = []
    for ticker, bars in bars_by_ticker.items():
        trades = run_backtest_for_ticker(ticker, bars, account_state, daily_states, **kwargs)
        all_trades.extend(trades)
    all_trades.sort(key=lambda t: (t["trading_date"], t["ticker"]))
    return {"trades": all_trades, "final_pnl": account_state["cumulative_realized_pnl"], "account_state": account_state}
