"""
Backtest Runner
------------------
Fetches historical 5-min bars for the universe over a date range and
runs backtest/engine.py, reporting results. Primary purpose: validate
CLEAN_TREND_MIN_BARS_SINCE_CROSS and CLEAN_TREND_MIN_SPREAD_ATR_MULT
(the two genuinely arbitrary defaults in config/settings.py) before
going live — not to produce a headline return figure. Same no-look-ahead
discipline as the momentum sibling project's own backtest.

Usage:
  python backtest/run.py --days 60
  python backtest/run.py --days 60 --sweep   # also compares alternate threshold values
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv('config/.env')

import argparse
from datetime import datetime, timedelta
import pytz

from config.universe import UNIVERSE
from config.settings import CLEAN_TREND_MIN_BARS_SINCE_CROSS, CLEAN_TREND_MIN_SPREAD_ATR_MULT
from tools.market_data.intraday_bars import get_bars, FIVE_MIN_BAR
from backtest.engine import run_backtest

ET = pytz.timezone("America/New_York")


def fetch_universe_bars(days: int) -> dict:
    end = datetime.now(ET)
    start = end - timedelta(days=days)
    print(f"Fetching {days} days of 5-min bars for {len(UNIVERSE)} tickers...")
    bars_by_ticker = get_bars(UNIVERSE, start.astimezone(pytz.utc), end.astimezone(pytz.utc), timeframe=FIVE_MIN_BAR)
    print(f"Got data for {len(bars_by_ticker)} tickers\n")
    return bars_by_ticker


def summarize(result: dict, label: str) -> None:
    trades = result["trades"]
    total_pnl = result["final_pnl"]
    wins = [t for t in trades if t["pnl_dollar"] > 0]
    losses = [t for t in trades if t["pnl_dollar"] <= 0]

    print(f"=== {label} ===")
    if trades:
        print(f"Trades: {len(trades)}  Wins: {len(wins)}  Losses: {len(losses)}  "
              f"Win rate: {len(wins) / len(trades) * 100:.1f}%")
        avg_win = sum(t["pnl_dollar"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t["pnl_dollar"] for t in losses) / len(losses) if losses else 0.0
        print(f"Avg win: ${avg_win:,.2f}  Avg loss: ${avg_loss:,.2f}")
        reasons: dict = {}
        for t in trades:
            reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        print(f"Exit reasons: {reasons}")
    else:
        print("Trades: 0")
    print(f"Total P&L: ${total_pnl:,.2f}\n")


def main():
    parser = argparse.ArgumentParser(description="EMA Trend Pullback Backtest")
    parser.add_argument("--days", type=int, default=60, help="Calendar days of history to backtest")
    parser.add_argument("--sweep", action="store_true", help="Also compare alternate clean-trend thresholds")
    args = parser.parse_args()

    bars_by_ticker = fetch_universe_bars(args.days)

    baseline = run_backtest(bars_by_ticker)
    summarize(baseline, f"BASELINE (min_bars_since_cross={CLEAN_TREND_MIN_BARS_SINCE_CROSS}, "
                         f"min_spread_atr_mult={CLEAN_TREND_MIN_SPREAD_ATR_MULT})")

    if args.sweep:
        looser = run_backtest(bars_by_ticker, min_bars_since_cross=6, min_spread_atr_mult=0.3)
        summarize(looser, "LOOSER (min_bars_since_cross=6, min_spread_atr_mult=0.3)")

        stricter = run_backtest(bars_by_ticker, min_bars_since_cross=20, min_spread_atr_mult=0.8)
        summarize(stricter, "STRICTER (min_bars_since_cross=20, min_spread_atr_mult=0.8)")


if __name__ == "__main__":
    main()
