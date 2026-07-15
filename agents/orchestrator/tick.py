"""
Intraday Tick — Single Cron Entrypoint
------------------------------------------
Fired every trading minute (~9:25-15:59 ET) by cron. Stateless between
invocations: everything needed survives in SQLite
(tools/trading/position_store.py). Same execution model as the sibling
ORB project (algo-trading-prop-firm) — cron IS the scheduler, no daemon,
no --schedule flag.

Steps 1-6 and 10 are IDENTICAL to the ORB sibling's tick.py (trading-day
gate, virtual-equity computation, monitor_hard_stops, halt/terminate
check, reconcile/check_bracket_exits/audit, and _flatten_all at the
flatten deadline — including all of that project's settle-delay/retry/
never-falsely-close/stale-prior-day-position fixes, ported unchanged).
Steps 7-9 are new, replacing ORB's one-shot screener + opening-range
capture + single breakout-scan with continuous trend-state tracking:

  7. Once per 5-min bar close (not every tick): update each ticker's
     trend phase (no_trend -> clean_trend -> watching_resumption ->
     position_open -> back to clean_trend/no_trend), detect pullbacks,
     spawn/refresh trend_setups rows.
  8. Every tick, cheap: scan only the (typically small) set of tickers
     currently watching_resumption for a resumption breakout on the
     latest completed 1-minute close.
  9. After any position closes (via reconcile.check_bracket_exits or
     _flatten_all — both ported verbatim, never modified), release the
     linked trend_state row so the same ticker can generate a new setup
     later the same day.

Usage:
  python agents/orchestrator/tick.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import time
from datetime import datetime, timedelta, date
import pytz
import pandas as pd
import pandas_market_calendars as mcal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus
from alpaca.data.timeframe import TimeFrame

from config.settings import (
    TIER_INITIAL_BALANCE, TIER_INTRADAY_BUYING_POWER,
    DAILY_LOSS_LIMIT_DOLLARS, MAX_DRAWDOWN_DOLLARS, DRAWDOWN_RATCHET_MULTIPLE,
    EMA_FAST_SPAN, EMA_SLOW_SPAN, ATR_PERIOD,
    CLEAN_TREND_MIN_BARS_SINCE_CROSS, CLEAN_TREND_MIN_SPREAD_ATR_MULT,
    SWING_LOOKBACK_BARS, TARGET_LOOKBACK_BARS, MIN_TARGET_RISK_REWARD,
    STOP_BUFFER_PCT, MAX_CONCURRENT_SETUPS,
    HALT_PROXY_MOVE_PCT, HALT_PROXY_WINDOW_MINUTES,
    ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE, FLATTEN_MINUTES_BEFORE_CLOSE,
)
from config.universe import UNIVERSE

from tools.trading import position_store
from tools.trading import rules_engine
from tools.trading import order_manager
from tools.market_data.intraday_bars import get_bars, get_latest_prices, FIVE_MIN_BAR

from ema_trend.signal import (
    compute_ema, compute_atr, classify_trend, compute_clean_trend,
    detect_pullback_touch, detect_trend_failure, compute_swing_reference,
    compute_stop_target, detect_breakout, detect_halt_proxy,
)

from agents.orchestrator import reconcile

ET = pytz.timezone("America/New_York")


def _client() -> TradingClient:
    return TradingClient(
        api_key=os.getenv('ALPACA_API_KEY'),
        secret_key=os.getenv('ALPACA_SECRET_KEY'),
        paper=True,
    )


def is_market_open_today() -> bool:
    nyse = mcal.get_calendar('NYSE')
    today_str = date.today().isoformat()
    schedule = nyse.schedule(start_date=today_str, end_date=today_str)
    return not schedule.empty


def _market_close_et(now_et: datetime) -> datetime:
    """NYSE-calendar-aware close time — handles early closes (half days)."""
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=now_et.date().isoformat(), end_date=now_et.date().isoformat())
    close_utc = schedule.iloc[0]['market_close']
    return close_utc.tz_convert(ET)


# ── Step 1-6, 10: ported verbatim from the ORB sibling's tick.py ────────

def _flatten_all(client: TradingClient, trading_date: str, reason: str) -> None:
    """
    Cancel every resting bracket, then market-flatten whatever Alpaca
    actually still holds. Ported unchanged from algo-trading-prop-firm's
    tick.py, including its settle-delay, retry-vs-still-pending
    distinction, and never-falsely-close fixes — all found live and
    fixed against real order behavior on 2026-07-14; nothing here is
    ORB-specific.
    """
    open_positions = position_store.list_open_positions()

    for pos in open_positions:
        if pos.get("alpaca_order_id"):
            order_manager.cancel_bracket(pos["alpaca_order_id"], client)

    if open_positions:
        time.sleep(3)  # let Alpaca release the qty hold before attempting to flatten

    alpaca_positions = {p.symbol: p for p in client.get_all_positions()}

    for pos in open_positions:
        ticker = pos["ticker"]
        alpaca_pos = alpaca_positions.get(ticker)
        if alpaca_pos is None or abs(float(alpaca_pos.qty)) <= 0:
            continue

        held_qty = abs(float(alpaca_pos.qty))
        exit_price = None
        confirmed_closed = False
        still_pending_order_id = None

        for attempt in range(3):
            close_client_order_id = f"ema-flatten-{ticker}-{trading_date}-{pos['id']}-{attempt}"
            try:
                result = order_manager.close_position_market(ticker, held_qty, pos["direction"], close_client_order_id, client)
                order = reconcile.poll_for_fill(result["alpaca_order_id"], client, timeout_seconds=30)
            except Exception as e:
                print(f"  ⚠️  flatten order failed for {ticker} (attempt {attempt + 1}/3): {e}")
                time.sleep(3)
                continue

            if order.status == OrderStatus.FILLED and order.filled_avg_price:
                exit_price = float(order.filled_avg_price)
                confirmed_closed = True
                break
            elif order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                print(f"  ⚠️  flatten order for {ticker} was {order.status}, retrying ({attempt + 1}/3)")
                time.sleep(3)
                continue
            else:
                still_pending_order_id = result["alpaca_order_id"]
                print(f"  ⏳  flatten order for {ticker} still {order.status} after poll — "
                      f"leaving it live (order {still_pending_order_id}), not retrying")
                break

        if not confirmed_closed:
            detail = (f"order {still_pending_order_id} still live/pending" if still_pending_order_id
                       else "order rejected/cancelled after 3 attempts")
            rules_engine.record_breach(
                trading_date, "flatten_failed",
                f"{ticker}: {detail} (reason={reason})",
                "left position OPEN in DB for retry/monitoring -- STILL HELD AT ALPACA",
            )
            continue

        pnl_dollar = None
        if exit_price is not None and pos.get("fill_price") is not None:
            direction_sign = 1.0 if pos["direction"] == "long" else -1.0
            pnl_dollar = (exit_price - pos["fill_price"]) * pos["qty"] * direction_sign

        held_seconds = None
        if pos.get("filled_at"):
            try:
                held_seconds = (datetime.now() - datetime.fromisoformat(pos["filled_at"])).total_seconds()
            except Exception:
                pass

        valid = rules_engine.is_valid_trade(pnl_dollar, held_seconds)
        position_store.close_position(
            pos["id"], exit_reason=reason, exit_price=exit_price, exit_time=datetime.now().isoformat(),
            pnl_dollar=pnl_dollar, held_seconds=held_seconds, is_valid_trade=valid,
            close_client_order_id=close_client_order_id,
        )
        rules_engine.record_realized_pnl(trading_date, pnl_dollar)

    for pos in position_store.list_pending_positions(older_than_minutes=0):
        if pos.get("alpaca_order_id"):
            order_manager.cancel_bracket(pos["alpaca_order_id"], client)
        position_store.mark_position_failed(pos["id"], f"cancelled — {reason}", alpaca_order_id=pos.get("alpaca_order_id"))


# ── Step 7: continuous trend-state tracking, once per 5-min bar close ───

def _compute_target_for_direction(bars: pd.DataFrame, direction: str) -> dict:
    """
    Prior swing HIGH is the target for a long (resistance above); prior
    swing LOW is the target for a short (support below) -- opposite kind
    from the stop's own swing reference. Deliberately UNBOUNDED by
    trend_started_at (unlike the stop) and uses a wider TARGET_LOOKBACK_BARS
    window -- early in a fresh trend, a trend-start-bounded search has no
    real "prior" level to find and collapses to something trivially close
    to entry (the exact bug that produced an implausible 68.9% backtest
    win rate before being caught and fixed).
    """
    kind = "swing_high" if direction == "long" else "swing_low"
    return compute_swing_reference(bars, kind, TARGET_LOOKBACK_BARS, floor_ts=None)


def _compute_stop_for_direction(bars: pd.DataFrame, direction: str, floor_ts) -> dict:
    kind = "swing_low" if direction == "long" else "swing_high"
    return compute_swing_reference(bars, kind, SWING_LOOKBACK_BARS, floor_ts)


def _try_build_setup_levels(bars: pd.DataFrame, latest_bar, direction: str, ema_used: str,
                             ema9: float, ema20: float, trend_started_at) -> dict:
    entry_trigger = float(latest_bar["high"]) if direction == "long" else float(latest_bar["low"])
    ema_stop_price = ema9 if ema_used == "ema9" else ema20
    floor_ts = pd.Timestamp(trend_started_at) if trend_started_at else None

    swing_stop = _compute_stop_for_direction(bars, direction, floor_ts)
    swing_target = _compute_target_for_direction(bars, direction)

    return compute_stop_target(
        entry_trigger=entry_trigger, ema_stop_price=ema_stop_price,
        swing_stop_price=swing_stop["price"], swing_target_price=swing_target["price"],
        direction=direction, stop_buffer_pct=STOP_BUFFER_PCT, min_target_risk_reward=MIN_TARGET_RISK_REWARD,
    )


def _update_trend_states(trading_date: str, now_et: datetime) -> None:
    lookback_start = (now_et - timedelta(days=5)).astimezone(pytz.utc)  # ample bars for ATR/swing/clean-trend windows
    lookback_end = now_et.astimezone(pytz.utc)
    try:
        bars_by_ticker = get_bars(UNIVERSE, lookback_start, lookback_end, timeframe=FIVE_MIN_BAR)
    except Exception as e:
        print(f"  ⚠️  failed to fetch 5-min bars for trend update: {e}")
        return

    for ticker, bars in bars_by_ticker.items():
        if bars.empty or len(bars) < ATR_PERIOD + 1:
            continue

        latest_bar_ts = str(bars.index[-1])
        ts = position_store.get_or_create_trend_state(trading_date, ticker)
        if ts["last_bar_ts"] == latest_bar_ts:
            continue  # already processed this exact bar

        ema9_series = compute_ema(bars, EMA_FAST_SPAN)
        ema20_series = compute_ema(bars, EMA_SLOW_SPAN)
        atr_series = compute_atr(bars, ATR_PERIOD)
        ema9, ema20 = float(ema9_series.iloc[-1]), float(ema20_series.iloc[-1])
        direction = classify_trend(ema9, ema20)
        latest_bar = bars.iloc[-1]

        update = {"ema9": ema9, "ema20": ema20, "last_bar_ts": latest_bar_ts}

        # Genuine direction flip -- abandon any in-flight setup, reset fresh.
        flipped = ts["direction"] not in (None, "none") and direction not in (None, "none") and direction != ts["direction"]
        if flipped:
            if ts.get("active_setup_id"):
                position_store.update_setup(ts["active_setup_id"], status="abandoned_trend_flip")
            update.update({
                "phase": "no_trend", "direction": direction, "trend_started_at": latest_bar_ts,
                "clean_since_bar_ts": None, "pullback_bar_high": None, "pullback_bar_low": None,
                "pullback_bar_ts": None, "active_setup_id": None,
            })
            position_store.update_trend_state(trading_date, ticker, **update)
            continue

        if ts["trend_started_at"] is None or direction != ts.get("direction"):
            update["trend_started_at"] = latest_bar_ts
        update["direction"] = direction

        phase = ts["phase"]

        if phase in ("no_trend", "clean_trend") and direction in ("long", "short"):
            clean = compute_clean_trend(
                ema9_series, ema20_series, atr_series, bars["close"],
                CLEAN_TREND_MIN_BARS_SINCE_CROSS, CLEAN_TREND_MIN_SPREAD_ATR_MULT,
            )
            if clean["is_clean"]:
                if phase == "no_trend":
                    update["clean_since_bar_ts"] = latest_bar_ts
                phase = "clean_trend"
            else:
                phase = "no_trend"
            update["phase"] = phase
        elif direction not in ("long", "short"):
            phase = "no_trend"
            update["phase"] = phase

        trend_started_at = update.get("trend_started_at") or ts.get("trend_started_at")

        if phase == "clean_trend" and ts["cycles_today"] < MAX_CONCURRENT_SETUPS:
            touch = detect_pullback_touch(latest_bar, ema9, ema20, direction)
            if touch["touched"]:
                cycle_number = ts["cycles_today"] + 1
                setup_id = position_store.create_setup(trading_date, ticker, cycle_number, direction)
                try:
                    st = _try_build_setup_levels(bars, latest_bar, direction, touch["ema_used"], ema9, ema20, trend_started_at)
                    position_store.update_setup(
                        setup_id, ema_used=touch["ema_used"],
                        pullback_bar_high=float(latest_bar["high"]), pullback_bar_low=float(latest_bar["low"]),
                        pullback_bar_ts=latest_bar_ts,
                        entry_trigger=st["entry_trigger"], stop_price=st["stop_price"],
                        target_price=st["target_price"], risk_per_share=st["risk_per_share"],
                        stop_basis=st["stop_basis"],
                    )
                    update.update({
                        "phase": "watching_resumption", "active_setup_id": setup_id, "cycles_today": cycle_number,
                        "pullback_bar_high": float(latest_bar["high"]), "pullback_bar_low": float(latest_bar["low"]),
                        "pullback_bar_ts": latest_bar_ts,
                    })
                except ValueError as e:
                    position_store.update_setup(setup_id, status="skipped_swing_error")
                    print(f"  ⚠️  {ticker}: pullback setup skipped, no valid stop/target: {e}")

        elif phase == "watching_resumption":
            active_id = ts.get("active_setup_id")
            setup = position_store.get_setup(active_id) if active_id else None
            if setup is None:
                update["phase"] = "clean_trend"
            else:
                bar_close = float(latest_bar["close"])
                if detect_trend_failure(bar_close, ema9, ema20, direction):
                    position_store.update_setup(active_id, status="abandoned_trend_failure")
                    update.update({"phase": "no_trend", "active_setup_id": None})
                elif setup["status"] == "watching":
                    touch = detect_pullback_touch(latest_bar, ema9, ema20, direction)
                    if touch["touched"]:
                        try:
                            st = _try_build_setup_levels(bars, latest_bar, direction, touch["ema_used"], ema9, ema20, trend_started_at)
                            position_store.update_setup(
                                active_id, ema_used=touch["ema_used"],
                                pullback_bar_high=float(latest_bar["high"]), pullback_bar_low=float(latest_bar["low"]),
                                pullback_bar_ts=latest_bar_ts, entry_trigger=st["entry_trigger"],
                                stop_price=st["stop_price"], stop_basis=st["stop_basis"],
                                risk_per_share=st["risk_per_share"], target_price=st["target_price"],
                            )
                            update.update({
                                "pullback_bar_high": float(latest_bar["high"]), "pullback_bar_low": float(latest_bar["low"]),
                                "pullback_bar_ts": latest_bar_ts,
                            })
                        except ValueError:
                            pass  # keep the existing setup fields if a fresh swing calc fails this bar

        position_store.update_trend_state(trading_date, ticker, **update)


# ── Step 8: every tick, only for tickers currently watching_resumption ──

def _scan_resumption_breakouts(
    client: TradingClient, trading_date: str,
    account_state: dict, daily_state: dict, current_equity: float, now_et: datetime,
) -> None:
    watching = position_store.list_watching_setups(trading_date)
    if not watching:
        return

    tickers = [s["ticker"] for s in watching]
    try:
        latest_prices = get_latest_prices(tickers)
    except Exception as e:
        print(f"  ⚠️  failed to fetch latest prices: {e}")
        return

    trailing_start = (now_et - timedelta(minutes=HALT_PROXY_WINDOW_MINUTES)).astimezone(pytz.utc)
    trailing_end = now_et.astimezone(pytz.utc)
    try:
        trailing_bars = get_bars(tickers, trailing_start, trailing_end, timeframe=TimeFrame.Minute)
    except Exception:
        trailing_bars = {}

    remaining_intraday_risk_committed = 0.0
    buying_power = TIER_INTRADAY_BUYING_POWER  # simulated tier BP -- NEVER Alpaca's real (larger) account BP

    for setup in watching:
        ticker = setup["ticker"]

        bars_trailing = trailing_bars.get(ticker)
        if bars_trailing is not None and not bars_trailing.empty:
            halt = detect_halt_proxy(bars_trailing, HALT_PROXY_MOVE_PCT)
            if halt["is_halt_proxy"]:
                position_store.update_setup(setup["id"], status="invalidated_halt_proxy")
                continue
            # entry trigger check uses the latest COMPLETED 1-min bar's close,
            # per agent.md's literal "enter on a close beyond..." wording --
            # not a raw latest-trade tick like the ORB sibling uses.
            latest_close = float(bars_trailing["close"].iloc[-1])
        else:
            continue  # no 1-min bar data yet this tick -- try again next tick

        if not detect_breakout(latest_close, setup["entry_trigger"], setup["direction"]):
            continue

        if position_store.get_open_position_db(ticker) is not None:
            position_store.update_setup(setup["id"], status="triggered")
            continue

        if not order_manager.is_tradable(ticker, setup["direction"], client):
            position_store.update_setup(setup["id"], status="skipped_not_tradable")
            continue

        prior_minute_volume = float(bars_trailing["volume"].iloc[-1]) if "volume" in bars_trailing else 0.0

        check = rules_engine.pre_trade_check(
            setup, account_state, daily_state, current_equity,
            remaining_intraday_risk_committed, prior_minute_volume, buying_power,
        )
        if not check["approved"]:
            reason_tag = (check["reasons"][0][:50] if check["reasons"] else "not_approved").replace(" ", "_")
            position_store.update_setup(setup["id"], status=f"skipped_{reason_tag}"[:60])
            continue

        client_order_id = f"ema-open-{ticker}-{trading_date}-{setup['id']}"
        try:
            position_id = position_store.create_pending_position(
                setup["id"], trading_date, ticker, setup["direction"], check["final_qty"],
                setup["stop_price"], setup["target_price"], setup["risk_per_share"], check["risk_dollars"],
                client_order_id,
            )
        except Exception:
            continue  # already attempted this ticker today (idempotency guard)

        try:
            result = order_manager.open_bracket_position(
                ticker, setup["direction"], check["final_qty"], setup["stop_price"], setup["target_price"],
                latest_close, client_order_id, client,
            )
        except Exception as e:
            position_store.mark_position_failed(position_id, f"submission error: {e}")
            position_store.update_setup(setup["id"], status="skipped_order_error")
            continue

        order = reconcile.poll_for_fill(result["alpaca_order_id"], client, timeout_seconds=30)

        if order.status == OrderStatus.PARTIALLY_FILLED:
            order_manager.cancel_bracket(result["alpaca_order_id"], client)
            time.sleep(2)

        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            confirmed = reconcile.confirm_fill_via_position(ticker, client)
            if confirmed:
                filled_at = str(order.filled_at) if order.filled_at else datetime.now().isoformat()
                position_store.mark_position_open(
                    position_id, result["alpaca_order_id"], str(order.status),
                    fill_price=confirmed["avg_entry_price"], filled_qty=confirmed["qty"], filled_at=filled_at,
                )
                position_store.update_setup(setup["id"], status="triggered")
                position_store.update_trend_state(
                    trading_date, ticker, phase="position_open", active_setup_id=setup["id"],
                )
                remaining_intraday_risk_committed += check["risk_dollars"]
                print(f"  ✅ Opened {setup['direction']} {ticker} x{confirmed['qty']} @ {confirmed['avg_entry_price']:.2f} "
                      f"(cycle {setup['cycle_number']}, stop_basis={setup['stop_basis']})")
            else:
                position_store.mark_position_failed(position_id, "fill unconfirmed", alpaca_order_id=result["alpaca_order_id"])
                position_store.update_setup(setup["id"], status="skipped_fill_unconfirmed")
        elif order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            position_store.mark_position_failed(position_id, f"order {order.status}", alpaca_order_id=result["alpaca_order_id"])
            position_store.update_setup(setup["id"], status=f"skipped_order_{str(order.status).lower()}"[:60])
        else:
            order_manager.cancel_bracket(result["alpaca_order_id"], client)
            position_store.mark_position_failed(position_id, "poll timeout -- order cancelled", alpaca_order_id=result["alpaca_order_id"])
            position_store.update_setup(setup["id"], status="skipped_timeout")


# ── Step 9: release trend_state after a position closes ─────────────────

def _release_closed_setups(trading_date: str) -> None:
    """
    Runs every tick, after reconcile.check_bracket_exits()/_flatten_all()
    (both ported verbatim, never modified here) may have just closed a
    position. Idempotent scan over today's positions: any trend_state row
    still pointing at a now-closed setup gets released so the same ticker
    can be watched for a fresh setup later the same day.
    """
    for pos in position_store.list_positions_for_date(trading_date):
        if pos["status"] != "closed":
            continue
        trend_state = position_store.get_trend_state(trading_date, pos["ticker"])
        if trend_state is None or trend_state.get("active_setup_id") != pos["candidate_id"]:
            continue  # already released, or never was the active link

        # Provisional immediate reset: keep watching if the EMA relationship
        # still looks directional; the NEXT step-7 pass (within 5 minutes)
        # re-validates the full clean-trend criteria and demotes to
        # no_trend on its own if it's not actually clean anymore.
        ema9, ema20 = trend_state.get("ema9"), trend_state.get("ema20")
        still_directional = ema9 is not None and ema20 is not None and ema9 != ema20
        new_phase = "clean_trend" if still_directional else "no_trend"
        position_store.update_trend_state(
            trading_date, pos["ticker"], active_setup_id=None, phase=new_phase,
        )


def run_tick() -> None:
    now_et = datetime.now(ET)
    trading_date = now_et.date().isoformat()

    if not is_market_open_today():
        print(f"[{now_et}] Market closed today (holiday/weekend) — no-op.")
        return

    client = _client()

    position_store.init_account_state(
        initial_balance=TIER_INITIAL_BALANCE,
        daily_loss_limit_dollars=DAILY_LOSS_LIMIT_DOLLARS,
        max_drawdown_dollars=MAX_DRAWDOWN_DOLLARS,
        drawdown_ratchet_multiple=DRAWDOWN_RATCHET_MULTIPLE,
        evaluation_start_date=trading_date,
    )
    account_state = position_store.get_account_state()

    if account_state["terminated"]:
        print(f"[{now_et}] Account TERMINATED (max drawdown breach) — no further trading.")
        return

    open_positions_for_equity = position_store.list_open_positions()
    latest_prices_for_equity = {}
    if open_positions_for_equity:
        try:
            latest_prices_for_equity = get_latest_prices([p["ticker"] for p in open_positions_for_equity])
        except Exception:
            latest_prices_for_equity = {}
    current_equity = rules_engine.compute_virtual_equity(account_state, open_positions_for_equity, latest_prices_for_equity)

    daily_state = rules_engine.get_or_create_daily_state(trading_date, current_equity)

    stale_positions = [p for p in position_store.list_open_positions() if p["trading_date"] != trading_date]
    if stale_positions:
        print(f"  ⚠️  {len(stale_positions)} position(s) carried over from a prior trading day — "
              f"flattening immediately: {[p['ticker'] for p in stale_positions]}")
        for p in stale_positions:
            rules_engine.record_breach(
                trading_date, "unexpected_overnight_hold",
                f"{p['ticker']} (opened {p['trading_date']}) still open at start of {trading_date}",
                "force-flattening immediately",
            )
        _flatten_all(client, trading_date, reason="unexpected_overnight_hold")

    hard_stops = rules_engine.monitor_hard_stops(account_state, daily_state, current_equity)
    account_state = hard_stops["account_state"]

    if not daily_state["trading_halted"] and hard_stops["daily_loss_breached"]:
        _flatten_all(client, trading_date, reason="daily_loss_limit_breach")
        rules_engine.record_breach(
            trading_date, "daily_loss_limit",
            f"daily loss ${hard_stops['daily_loss_detail']['daily_loss']:.2f} breached limit "
            f"${daily_state['daily_loss_limit_snapshot']:.2f}",
            "closed all positions/orders, halted trading for the day",
        )
        position_store.update_daily_state(
            trading_date, trading_halted=1, halt_reason="daily_loss_limit", halted_at=now_et.isoformat()
        )
        daily_state = position_store.get_daily_state(trading_date)

    if not account_state["terminated"] and hard_stops["drawdown_breached"]:
        _flatten_all(client, trading_date, reason="max_drawdown_breach")
        rules_engine.record_breach(
            trading_date, "max_drawdown",
            f"equity ${current_equity:.2f} breached drawdown floor ${hard_stops['drawdown_detail']['floor']:.2f}",
            "closed all positions/orders, PERMANENT account termination",
        )
        position_store.update_account_state(
            terminated=1, terminated_at=now_et.isoformat(), terminated_reason="max_drawdown_breach"
        )
        account_state = position_store.get_account_state()

    if daily_state["trading_halted"] or account_state["terminated"]:
        print(f"[{now_et}] Trading halted/terminated — no further action this tick.")
        return

    # Step 6: reconcile + detect bracket-leg exits + audit.
    reconcile.reconcile_pending_positions(client)
    reconcile.check_bracket_exits(client)
    reconcile.audit_open_positions(client)

    # Step 9 (runs right after exits are detected, before the next scan).
    _release_closed_setups(trading_date)

    # Step 7: trend-state update, once per 5-min bar close.
    if now_et.minute % 5 == 0:
        _update_trend_states(trading_date, now_et)

    # Step 8: resumption-breakout scan + entry, every tick.
    close_et = _market_close_et(now_et)
    entry_cutoff = close_et - timedelta(minutes=ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE)
    if now_et < entry_cutoff:
        _scan_resumption_breakouts(client, trading_date, account_state, daily_state, current_equity, now_et)
    elif now_et.minute % 5 == 0:
        # Past the entry cutoff -- sweep any still-watching setups so the
        # EOD report doesn't show stale "watching" rows for the day.
        for setup in position_store.list_watching_setups(trading_date):
            position_store.update_setup(setup["id"], status="expired_eod")

    # Step 10: flatten deadline.
    flatten_deadline = close_et - timedelta(minutes=FLATTEN_MINUTES_BEFORE_CLOSE)
    if now_et >= flatten_deadline:
        _flatten_all(client, trading_date, reason="eod_flatten")

    print(f"[{now_et}] Tick complete.")


if __name__ == "__main__":
    run_tick()
