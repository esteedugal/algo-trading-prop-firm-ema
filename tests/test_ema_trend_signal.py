"""Unit tests for ema_trend/signal.py — pure functions, synthetic data, no live API."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from ema_trend.signal import (
    compute_ema,
    compute_atr,
    classify_trend,
    bars_since_cross,
    compute_clean_trend,
    detect_pullback_touch,
    detect_trend_failure,
    compute_swing_reference,
    compute_stop_target,
    detect_breakout,
    detect_halt_proxy,
)


def _bars(rows, start="2026-07-14 09:30", freq="5min"):
    """rows: list of (open, high, low, close) tuples."""
    idx = pd.date_range(start=start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    return df


def _trending_bars(n, start_price, per_bar_move, noise=0.0):
    """Synthetic bars with a constant per-bar drift, optionally with tiny
    high/low noise around each close, for a deterministic, clean trend."""
    rows = []
    price = start_price
    for i in range(n):
        o = price
        c = price + per_bar_move
        hi = max(o, c) + noise
        lo = min(o, c) - noise
        rows.append((o, hi, lo, c))
        price = c
    return _bars(rows)


def test_compute_ema_constant_price_converges_to_price():
    bars = _bars([(50.0, 50.0, 50.0, 50.0)] * 30)
    ema = compute_ema(bars, span=9)
    assert abs(ema.iloc[-1] - 50.0) < 1e-9


def test_compute_ema_fast_span_reacts_faster_than_slow_span():
    # A sudden jump in close -- the 9-span EMA should have moved further
    # toward the new level than the 20-span EMA after the same N bars.
    rows = [(100.0, 100.0, 100.0, 100.0)] * 20 + [(100.0, 120.0, 100.0, 120.0)] * 10
    bars = _bars(rows)
    ema9 = compute_ema(bars, span=9)
    ema20 = compute_ema(bars, span=20)
    assert ema9.iloc[-1] > ema20.iloc[-1]


def test_compute_atr_constant_range_bars():
    # Every bar has a true range of exactly 2.0 (high-low), no gaps.
    rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(20)]
    bars = _bars(rows)
    atr = compute_atr(bars, period=14)
    assert abs(atr.iloc[-1] - 2.0) < 0.5  # gap-adjusted TR is close to but not exactly 2.0 given the drift


def test_classify_trend_long_short_none():
    assert classify_trend(ema9=105.0, ema20=100.0) == "long"
    assert classify_trend(ema9=95.0, ema20=100.0) == "short"
    assert classify_trend(ema9=100.0, ema20=100.0) == "none"


def test_bars_since_cross_counts_consistent_run():
    ema9 = pd.Series([10, 10, 10, 5, 6, 7, 7.5])   # cross at index 3 (goes below ema20's 8s)
    ema20 = pd.Series([8, 8, 8, 8, 8, 8, 8])
    # index 0-2: ema9(10) > ema20(8) -> sign +1
    # index 3-6: ema9 < ema20 -> sign -1 (cross happens at index 3)
    assert bars_since_cross(ema9, ema20) == 4  # indices 3,4,5,6 all sign -1


def test_compute_clean_trend_true_for_sustained_wide_spread():
    bars = _trending_bars(40, start_price=100.0, per_bar_move=0.5)
    ema9 = compute_ema(bars, span=9)
    ema20 = compute_ema(bars, span=20)
    atr = compute_atr(bars, period=14)
    result = compute_clean_trend(ema9, ema20, atr, bars["close"], min_bars_since_cross=12, min_spread_atr_mult=0.5)
    assert result["is_clean"] is True


def test_compute_clean_trend_false_for_choppy_flat_series():
    # Oscillating price -> EMAs stay close together, low/no sustained spread.
    rows = []
    price = 100.0
    for i in range(40):
        price += 0.3 if i % 2 == 0 else -0.3
        rows.append((price, price + 0.1, price - 0.1, price))
    bars = _bars(rows)
    ema9 = compute_ema(bars, span=9)
    ema20 = compute_ema(bars, span=20)
    atr = compute_atr(bars, period=14)
    result = compute_clean_trend(ema9, ema20, atr, bars["close"], min_bars_since_cross=12, min_spread_atr_mult=0.5)
    assert result["is_clean"] is False


def test_detect_pullback_touch_prefers_ema9_when_both_touched():
    bar = {"low": 99.0, "high": 101.0}
    result = detect_pullback_touch(bar, ema9=100.0, ema20=100.5, direction="long")
    assert result["touched"] is True
    assert result["ema_used"] == "ema9"


def test_detect_pullback_touch_ema20_only():
    # bar's range includes ema20 (97.0) but not ema9 (100.0) -- should
    # credit ema20 specifically, not just report "no touch."
    bar = {"low": 96.5, "high": 97.5}
    result = detect_pullback_touch(bar, ema9=100.0, ema20=97.0, direction="long")
    assert result["touched"] is True
    assert result["ema_used"] == "ema20"


def test_detect_pullback_touch_no_touch_returns_false():
    bar = {"low": 105.0, "high": 106.0}
    result = detect_pullback_touch(bar, ema9=100.0, ema20=99.0, direction="long")
    assert result["touched"] is False
    assert result["ema_used"] is None


def test_detect_pullback_touch_none_direction_never_touches():
    bar = {"low": 99.0, "high": 101.0}
    result = detect_pullback_touch(bar, ema9=100.0, ema20=100.0, direction="none")
    assert result["touched"] is False


def test_detect_trend_failure_long_breaks_both_emas():
    assert detect_trend_failure(bar_close=95.0, ema9=100.0, ema20=98.0, direction="long") is True
    assert detect_trend_failure(bar_close=99.0, ema9=100.0, ema20=98.0, direction="long") is False  # only broke ema9


def test_detect_trend_failure_short_breaks_both_emas():
    assert detect_trend_failure(bar_close=105.0, ema9=100.0, ema20=102.0, direction="short") is True
    assert detect_trend_failure(bar_close=101.0, ema9=100.0, ema20=102.0, direction="short") is False


def test_compute_swing_reference_swing_low():
    bars = _bars([
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 102.0, 100.0, 101.5),
        (101.5, 102.5, 97.0, 98.0),   # swing low here
        (98.0, 99.0, 97.5, 98.5),
    ])
    result = compute_swing_reference(bars, kind="swing_low", lookback_n=20)
    assert result["price"] == 97.0


def test_compute_swing_reference_bounded_by_floor_ts():
    bars = _bars([
        (100.0, 101.0, 90.0, 100.5),   # very low here, but BEFORE the trend started
        (100.5, 102.0, 100.0, 101.5),
        (101.5, 102.5, 97.0, 98.0),    # swing low within the trend window
        (98.0, 99.0, 97.5, 98.5),
    ])
    floor_ts = bars.index[1]  # trend "started" at the second bar
    result = compute_swing_reference(bars, kind="swing_low", lookback_n=20, floor_ts=floor_ts)
    assert result["price"] == 97.0  # NOT 90.0 -- that bar is before floor_ts


def test_compute_stop_target_uses_tighter_of_ema_or_swing_long():
    # ema stop is closer (tighter) than swing stop for a long
    result = compute_stop_target(
        entry_trigger=100.0, ema_stop_price=99.0, swing_stop_price=95.0,
        swing_target_price=110.0, direction="long", stop_buffer_pct=0.0,
    )
    assert result["stop_basis"] == "ema"
    assert result["stop_price"] == 99.0
    assert result["target_price"] == 110.0
    assert result["risk_per_share"] == 1.0


def test_compute_stop_target_uses_tighter_of_ema_or_swing_short():
    # swing stop is closer (tighter) than ema stop for a short
    result = compute_stop_target(
        entry_trigger=100.0, ema_stop_price=105.0, swing_stop_price=102.0,
        swing_target_price=90.0, direction="short", stop_buffer_pct=0.0,
    )
    assert result["stop_basis"] == "swing"
    assert result["stop_price"] == 102.0
    assert result["target_price"] == 90.0


def test_compute_stop_target_applies_buffer_beyond_the_level():
    result = compute_stop_target(
        entry_trigger=100.0, ema_stop_price=99.0, swing_stop_price=95.0,
        swing_target_price=110.0, direction="long", stop_buffer_pct=0.01,
    )
    assert result["stop_price"] == 99.0 * 0.99  # pushed further below for a long stop


def test_compute_stop_target_rejects_target_below_entry_for_long():
    # swing "target" is below entry_trigger -- no real profit room, must raise
    try:
        compute_stop_target(
            entry_trigger=100.0, ema_stop_price=99.0, swing_stop_price=95.0,
            swing_target_price=99.5, direction="long",
        )
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_compute_stop_target_rejects_target_above_entry_for_short():
    try:
        compute_stop_target(
            entry_trigger=100.0, ema_stop_price=101.0, swing_stop_price=103.0,
            swing_target_price=100.5, direction="short",
        )
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_compute_stop_target_rejects_target_below_min_risk_reward():
    # risk = 1.0 (entry 100 -> stop 99), reward = 1.2 (target 101.2) -> ratio 1.2, below 1.5 min
    try:
        compute_stop_target(
            entry_trigger=100.0, ema_stop_price=99.0, swing_stop_price=95.0,
            swing_target_price=101.2, direction="long", stop_buffer_pct=0.0,
            min_target_risk_reward=1.5,
        )
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_compute_stop_target_accepts_target_meeting_min_risk_reward():
    # risk = 1.0, reward = 2.0 -> ratio 2.0, meets 1.5 min
    result = compute_stop_target(
        entry_trigger=100.0, ema_stop_price=99.0, swing_stop_price=95.0,
        swing_target_price=102.0, direction="long", stop_buffer_pct=0.0,
        min_target_risk_reward=1.5,
    )
    assert result["target_price"] == 102.0


def test_compute_stop_target_rejects_none_direction():
    try:
        compute_stop_target(100.0, 99.0, 95.0, 110.0, direction="none")
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_detect_breakout_long_boundary_values():
    assert detect_breakout(close_price=100.0, entry_trigger=100.0, direction="long") is False
    assert detect_breakout(close_price=100.01, entry_trigger=100.0, direction="long") is True
    assert detect_breakout(close_price=99.99, entry_trigger=100.0, direction="long") is False


def test_detect_breakout_short_boundary_values():
    assert detect_breakout(close_price=100.0, entry_trigger=100.0, direction="short") is False
    assert detect_breakout(close_price=99.99, entry_trigger=100.0, direction="short") is True
    assert detect_breakout(close_price=100.01, entry_trigger=100.0, direction="short") is False


def test_detect_halt_proxy_trips_above_threshold():
    bars = _bars([(100.0, 101.0, 99.5, 100.5), (100.5, 112.0, 100.0, 111.0)])
    result = detect_halt_proxy(bars, move_pct_threshold=0.10)
    assert result["is_halt_proxy"] is True


def test_detect_halt_proxy_does_not_trip_below_threshold():
    bars = _bars([(100.0, 101.0, 99.5, 100.5), (100.5, 105.0, 100.0, 104.0)])
    result = detect_halt_proxy(bars, move_pct_threshold=0.10)
    assert result["is_halt_proxy"] is False


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
