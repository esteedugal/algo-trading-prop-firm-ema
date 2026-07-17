"""
9/20 EMA Trend Pullback Signal Math
--------------------------------------
Pure functions, no I/O — mirrors the sibling ORB project's orb/signal.py
in shape and naming conventions (strict-inequality breakout convention,
stop/target frozen once computed, halt-proxy logic copy-pasted unchanged
since it was already strategy-agnostic there).

Unlike ORB's single measured opening range, trend state here evolves
continuously through the day: classify_trend/compute_clean_trend get
re-evaluated once per new 5-min bar, and a ticker can cycle through
pullback -> resumption -> position -> back to watching multiple times in
one session (see agents/orchestrator/tick.py's phase state machine).
"""

from typing import Literal, Optional
import pandas as pd

Direction = Literal["long", "short", "none"]


def compute_ema(bars_5min: pd.DataFrame, span: int) -> pd.Series:
    return bars_5min["close"].ewm(span=span, adjust=False).mean()


def compute_atr(bars_5min: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range: rolling mean of the true range (max of the
    three standard high-low / high-prevclose / low-prevclose spans)."""
    high = bars_5min["high"]
    low = bars_5min["low"]
    prev_close = bars_5min["close"].shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def classify_trend(ema9: float, ema20: float) -> Direction:
    if ema9 > ema20:
        return "long"
    if ema9 < ema20:
        return "short"
    return "none"


def bars_since_cross(ema9_series: pd.Series, ema20_series: pd.Series) -> int:
    """
    Number of consecutive trailing bars the 9/20 relationship's sign has
    held (long vs short). Returns the full series length if the
    relationship has been consistent for the entire available window
    (i.e. no cross found in the data given).
    """
    diff = ema9_series - ema20_series
    sign = diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    n = len(sign)
    if n == 0:
        return 0
    current = sign.iloc[-1]
    count = 0
    for i in range(n - 1, -1, -1):
        if sign.iloc[i] != current:
            break
        count += 1
    return count


def compute_clean_trend(
    ema9_series: pd.Series,
    ema20_series: pd.Series,
    atr_series: pd.Series,
    close_series: pd.Series,
    min_bars_since_cross: int = 12,
    min_spread_atr_mult: float = 0.5,
) -> dict:
    """
    agent.md: "stand aside during choppy, directionless days where the
    EMAs are flat or repeatedly crossing." Two conditions, both required:
    (1) the 9/20 relationship has held its current sign for at least
    min_bars_since_cross bars (not a fresh/unstable cross), and (2) the
    spread between them, scaled by ATR(14), exceeds min_spread_atr_mult
    (self-adjusts per name's own volatility, unlike a fixed $ spread
    threshold that would be too strict for a $500 stock and too loose for
    a $15 one). Both threshold defaults are genuinely arbitrary and
    flagged for the backtest to validate before going live.
    """
    ema9 = ema9_series.iloc[-1]
    ema20 = ema20_series.iloc[-1]
    atr = atr_series.iloc[-1]
    spread = abs(ema9 - ema20)
    bsc = bars_since_cross(ema9_series, ema20_series)

    if atr is None or pd.isna(atr) or atr <= 0:
        return {"is_clean": False, "bars_since_cross": bsc, "spread": spread, "atr": atr, "spread_atr_ratio": None}

    spread_atr_ratio = spread / atr
    is_clean = bool(bsc >= min_bars_since_cross and spread_atr_ratio >= min_spread_atr_mult)
    return {
        "is_clean": is_clean,
        "bars_since_cross": bsc,
        "spread": spread,
        "atr": atr,
        "spread_atr_ratio": spread_atr_ratio,
    }


def detect_pullback_touch(bar: dict, ema9: float, ema20: float, direction: Direction) -> dict:
    """
    True if the bar's [low, high] range traded through either EMA (price
    pulled back INTO it, not necessarily closing beyond it). Prefers ema9
    (the faster/nearer line) when both are touched in the same bar.
    """
    if direction not in ("long", "short"):
        return {"touched": False, "ema_used": None}
    low, high = bar["low"], bar["high"]
    if low <= ema9 <= high:
        return {"touched": True, "ema_used": "ema9"}
    if low <= ema20 <= high:
        return {"touched": True, "ema_used": "ema20"}
    return {"touched": False, "ema_used": None}


def detect_trend_failure(bar_close: float, ema9: float, ema20: float, direction: Direction) -> bool:
    """
    True if the bar's CLOSE breaks beyond BOTH EMAs against the trend —
    deeper than a normal pullback (which only touches one EMA within the
    bar's high/low range). This is itself a signal to abandon the setup,
    not a non-event.
    """
    if direction == "long":
        return bar_close < min(ema9, ema20)
    if direction == "short":
        return bar_close > max(ema9, ema20)
    return False


def compute_swing_reference(
    bars_5min: pd.DataFrame,
    kind: Literal["swing_low", "swing_high"],
    lookback_n: int = 20,
    floor_ts: Optional[pd.Timestamp] = None,
) -> dict:
    """
    Lowest low (kind='swing_low') or highest high (kind='swing_high') over
    a bounded window: min(lookback_n bars, bars since floor_ts). floor_ts
    should be the current trend's own start (trend_started_at) — a swing
    point from before the trend began isn't a real reference level for it.
    """
    window = bars_5min
    if floor_ts is not None:
        window = window[window.index >= floor_ts]
    window = window.tail(lookback_n)

    if window.empty:
        raise ValueError("compute_swing_reference: no bars available in the bounded lookback window")

    if kind == "swing_low":
        idx = window["low"].idxmin()
        price = window.loc[idx, "low"]
    elif kind == "swing_high":
        idx = window["high"].idxmax()
        price = window.loc[idx, "high"]
    else:
        raise ValueError(f"kind must be 'swing_low' or 'swing_high', got {kind!r}")

    return {"price": float(price), "ts": idx}


def compute_stop_target(
    entry_trigger: float,
    ema_stop_price: float,
    swing_stop_price: float,
    swing_target_price: float,
    direction: Direction,
    stop_buffer_pct: float = 0.0050,
    min_target_risk_reward: float = 0.0,
) -> dict:
    """
    Stop = tighter-of(EMA-based, swing-based) by distance from entry,
    pushed stop_buffer_pct further out ("just beyond" the level, not
    exactly on it — avoids noise re-touching an exact-level stop). Target
    is the swing reference price, frozen as given (computed once at setup
    time by the caller, from a pre-pullback swing point that doesn't move
    while the pullback is still forming).

    IMPORTANT: swing_target_price must come from a lookback window that is
    NOT bounded by the current trend's own start (unlike the stop's
    window) — early in a fresh trend, a trend-start-bounded search has no
    real "prior" level to find and collapses to something trivially close
    to entry. This produced an implausible 68.9% backtest win rate before
    being caught and fixed (see config/settings.py's TARGET_LOOKBACK_BARS
    docstring). min_target_risk_reward is the safety net for the residual
    case where even a wider window still lands a real swing point close by.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"compute_stop_target requires direction in ('long','short'), got {direction!r}")

    if direction == "long" and swing_target_price <= entry_trigger:
        raise ValueError(
            f"swing target {swing_target_price} is not above entry_trigger {entry_trigger} for a long setup "
            "-- no real profit room within the lookback window, skip this setup"
        )
    if direction == "short" and swing_target_price >= entry_trigger:
        raise ValueError(
            f"swing target {swing_target_price} is not below entry_trigger {entry_trigger} for a short setup "
            "-- no real profit room within the lookback window, skip this setup"
        )

    ema_distance = abs(entry_trigger - ema_stop_price)
    swing_distance = abs(entry_trigger - swing_stop_price)

    if swing_distance < ema_distance:
        raw_stop, stop_basis = swing_stop_price, "swing"
    else:
        raw_stop, stop_basis = ema_stop_price, "ema"

    stop_price = raw_stop * (1 - stop_buffer_pct) if direction == "long" else raw_stop * (1 + stop_buffer_pct)

    risk_per_share = abs(entry_trigger - stop_price)
    if risk_per_share <= 0:
        raise ValueError(f"Non-positive risk_per_share (entry={entry_trigger}, stop={stop_price})")

    reward_per_share = abs(swing_target_price - entry_trigger)
    if min_target_risk_reward > 0 and reward_per_share < min_target_risk_reward * risk_per_share:
        raise ValueError(
            f"target reward {reward_per_share:.4f} is less than {min_target_risk_reward}x "
            f"the risk {risk_per_share:.4f} -- not enough edge for this setup, skip it"
        )

    return {
        "entry_trigger": entry_trigger,
        "stop_price": stop_price,
        "stop_basis": stop_basis,
        "target_price": swing_target_price,
        "risk_per_share": risk_per_share,
    }


def detect_breakout(close_price: float, entry_trigger: float, direction: Direction) -> bool:
    """
    Strict inequality — same convention as orb/signal.py's detect_breakout
    — but fed a 1-minute bar's CLOSE, not a raw latest-trade tick, per
    agent.md's literal "enter on a close beyond..." wording.
    """
    if direction == "long":
        return close_price > entry_trigger
    if direction == "short":
        return close_price < entry_trigger
    return False


def detect_halt_proxy(bars_trailing, move_pct_threshold: float = 0.10) -> dict:
    """Copy-pasted unchanged from orb/signal.py — no ORB-specific logic in
    it already. No halt-status API exists anywhere in Alpaca's SDK; this
    is a self-computed >=10%-move-in-5-minutes proxy, with actual
    exchange-level order rejection as the real backstop."""
    first_open = float(bars_trailing["open"].iloc[0])
    last_close = float(bars_trailing["close"].iloc[-1])
    move_pct = (last_close - first_open) / first_open
    return {
        "is_halt_proxy": abs(move_pct) >= move_pct_threshold,
        "move_pct": move_pct,
    }
