"""
9/20 EMA Trend Pullback Strategy Parameters
-----------------------------------------------
Trend-pullback-and-resumption entries on a fixed universe, operated under
the SAME simulated prop-firm compliance rulebook as the sibling
`algo-trading-prop-firm` (ORB) project (see agent.md) — same $25k tier,
same daily-loss/drawdown/consistency/volume rules. Only the signal
mechanics below differ from that sibling's settings.py.

Every $ threshold below is a literal module constant, not read from env
with a fallback default — there is deliberately no silent-default path.
These numbers are the entire basis of position sizing and the hard-stop
compliance checks; guessing here is the one mistake this file can't afford.

IMPORTANT: the Alpaca paper account backing this project has real equity/
buying power far larger than the tier below (standard Alpaca paper
default) — that is infrastructure only, so a real order is never blocked
by Alpaca's own buying power. The account this strategy is actually
managed against is a SIMULATED $25k prop-firm tier, tracked entirely in
our own `rules_engine` state (see tools/trading/rules_engine.py,
`account_state` table). Every compliance check uses TIER_* below, never
the real Alpaca account's actual equity or buying power.
"""

# ── Simulated prop-firm account tier (same as the ORB sibling) ─────────
TIER_INITIAL_BALANCE = 25_000.0
TIER_INTRADAY_BUYING_POWER = 25_000.0
TIER_OVERNIGHT_BUYING_POWER = 4_000.0

# ── Daily Loss Limit / Max Drawdown (same $25k-tier figures as the ORB
#    sibling — agent.md gives rule structure only, not the $ figures) ──
DAILY_LOSS_LIMIT_DOLLARS = 500.0
MAX_DRAWDOWN_DOLLARS = 1_500.0
DRAWDOWN_RATCHET_MULTIPLE = 3.0   # equity >= initial + 3x daily loss limit -> floor locks at initial

# ── Position sizing ─────────────────────────────────────────────────────
RISK_PER_TRADE_PCT = 0.30      # of DAILY_LOSS_LIMIT_DOLLARS, per agent.md's literal formula
VOLUME_CAP_PCT = 0.05          # new/added position <= this fraction of prior 1-min volume

# ── Trade validity / consistency rule ───────────────────────────────────
CONSISTENCY_MAX_PROFIT_SHARE = 0.30   # no single position > this share of total profit
MIN_VALID_PROFIT_CENTS = 10.0
MIN_VALID_HOLD_SECONDS = 60
MIN_VALID_TRADES = 20                 # over the evaluation period (monitored, never forced)

# ── 9/20 EMA trend mechanics ─────────────────────────────────────────────
EMA_FAST_SPAN = 9
EMA_SLOW_SPAN = 20
ATR_PERIOD = 14

# "Clean trend" filter (agent.md: stand aside during choppy/flat/tangled
# EMAs). Both of these are genuinely arbitrary defaults, flagged in the
# implementation plan for the backtest to validate before going live --
# not shipped as unverified assumptions.
CLEAN_TREND_MIN_BARS_SINCE_CROSS = 12    # 12 x 5-min bars = 1 trading hour of sustained direction
CLEAN_TREND_MIN_SPREAD_ATR_MULT = 0.5    # |ema9-ema20| >= 0.5x ATR(14) -- self-scales per name's own volatility

SWING_LOOKBACK_BARS = 20                 # STOP reference only -- bounded further by bars-since-trend-start
TARGET_LOOKBACK_BARS = 60                # TARGET reference -- deliberately NOT bounded by trend_started_at
                                          # (unlike the stop, a "prior swing high/low" target needs to look
                                          # further back than the current leg's own start, or it collapses
                                          # to something trivially close to entry early in a fresh trend --
                                          # found live via the backtest on 2026-07-14: this bug alone produced
                                          # an implausible 68.9% win rate / 2,476 trades in 20 days)
MIN_TARGET_RISK_REWARD = 1.5             # skip a setup if target distance < this x the stop distance --
                                          # safety net in case even the wider lookback still lands close
STOP_BUFFER_PCT = 0.0050                 # push stop meaningfully beyond the level, not just past it --
                                          # widened from 0.0005 on 2026-07-16 after a live replay of the
                                          # first 2 trading days' 27 stopped-out trades against real 1-min
                                          # bars showed the old buffer was getting clipped by ordinary
                                          # intraday noise (avg stop distance was 0.235% of entry price):
                                          # 0% win rate / -$1,034 at 0.05%, vs 37% win rate / -$434 at 0.50%
                                          # (position-sized to hold $ risk per trade constant in both cases).
                                          # Reward:risk gate (MIN_TARGET_RISK_REWARD) interaction with the
                                          # wider stop was not re-tested -- watch for a drop in trade
                                          # frequency if the wider stop now disqualifies more setups.

# Operational cap only (NOT a risk control -- pre_trade_check's own
# budget/volume/buying-power gating already bounds real exposure) --
# bounds the per-tick "fetch latest close for watched tickers" batch size.
MAX_CONCURRENT_SETUPS = 15

HALT_PROXY_MOVE_PCT = 0.10
HALT_PROXY_WINDOW_MINUTES = 5

# ── Session timing (all wall-clock ET) ──────────────────────────────────
ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE = 20   # stop looking for NEW entries this far before close
FLATTEN_MINUTES_BEFORE_CLOSE = 10        # force-close everything this far before close

# ── Order execution ──────────────────────────────────────────────────────
ENTRY_LIMIT_BUFFER = 1.005   # marketable-limit buffer on bracket entry (0.5%), bounds slippage

# ── EOD reporting ──────────────────────────────────────────────────────
EOD_REFLECTION_MODEL = "claude-haiku-4-5-20251001"
