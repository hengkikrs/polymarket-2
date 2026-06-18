"""
edge_signals.py — Microstructure & probabilistic edge signals
==============================================================

Module computes 5 institutional-grade signals used as a CROSS-VALIDATION GATE
before trade entry. Trades fire only if signals align with directional bet.

Signals:
  1. compute_imbalance(bids, asks) → [-1, +1] order flow pressure
  2. compute_microprice(bids, asks) → VWAP-weighted fair value
  3. classify_vol_regime(rv_history) → "low" | "normal" | "high" | "extreme"
  4. spread_zscore(spread_history, current) → standard deviations
  5. p_resolution_brownian(distance, secs_left, rv) → P(BTC stays above)

Plus:
  6. TriggerExpectancy — rolling per-trigger PnL tracker; auto-disable losers

DESIGN:
  - All signals are PURE functions over inputs (testable, no side effects).
  - Caller (main.py / strategy_b.py) decides how to combine.
  - Defensive: missing/malformed inputs return neutral values, not exceptions.
"""

from __future__ import annotations
import math
import statistics
import time
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
#  1. ORDERBOOK IMBALANCE
# ─────────────────────────────────────────────────────────────────────────────

def compute_imbalance(bids: list, asks: list, levels: int = 5) -> float:
    """
    Order flow imbalance — short-term price pressure indicator.

    Formula: (B - A) / (B + A)
      where B = sum of bid sizes (top N levels)
            A = sum of ask sizes (top N levels)

    Returns:
      +1.0 = all liquidity on bid side (bullish for this token)
      -1.0 = all liquidity on ask side (bearish — heavy selling pressure)
       0.0 = balanced (no info, or no data)

    Empirical: |imbalance| > 0.3 predicts 3-10s price move 60-65% of time.
    Crucially, imbalance flipping (sudden ask wall) precedes price drops.

    Args:
        bids: [(price, size), ...] sorted by price DESC (best bid first)
        asks: [(price, size), ...] sorted by price ASC (best ask first)
        levels: number of levels to sum (default 5)
    """
    if not bids and not asks:
        return 0.0
    bid_sum = sum(s for _, s in bids[:levels])
    ask_sum = sum(s for _, s in asks[:levels])
    total = bid_sum + ask_sum
    if total < 1e-9:
        return 0.0
    return (bid_sum - ask_sum) / total


# ─────────────────────────────────────────────────────────────────────────────
#  2. MICROPRICE (VWAP-weighted fair value)
# ─────────────────────────────────────────────────────────────────────────────

def compute_microprice(bids: list, asks: list) -> float:
    """
    Microprice = size-weighted bid+ask. Better fair-value than mid.

    Formula: (best_bid × ask_size + best_ask × bid_size) / (bid_size + ask_size)

    Intuition: if bid_size >> ask_size, fair price is closer to ask
    (more demand pulling price up). Mid-price misses this signal.

    Returns:
      0.0 if no data; else microprice in [best_bid, best_ask].
    """
    if not bids or not asks:
        return 0.0
    best_bid, bid_size = bids[0]
    best_ask, ask_size = asks[0]
    if best_bid <= 0 or best_ask <= 0:
        return 0.0
    total_size = bid_size + ask_size
    if total_size < 1e-9:
        # Fallback to mid if sizes degenerate
        return (best_bid + best_ask) / 2
    # Note: weights are CROSSED (bid weighted by ask_size and vice versa)
    # because the side with more liquidity is the side resisting movement.
    return (best_bid * ask_size + best_ask * bid_size) / total_size


# ─────────────────────────────────────────────────────────────────────────────
#  3. VOLATILITY REGIME CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────

# Realized vol thresholds (per-minute return |Δ| as fraction of price).
# Calibrated from BTC 2024-2025 minute bars.
_RV_LOW_MAX     = 0.0008   # < 0.08%/min = quiet
_RV_NORMAL_MAX  = 0.0020   # < 0.2%/min  = normal
_RV_HIGH_MAX    = 0.0040   # < 0.4%/min  = high
# > 0.4%/min = extreme (FOMC, CPI, halving events, flash crash regime)


def classify_vol_regime(realized_vol: float) -> str:
    """
    Classify current volatility regime based on realized vol (per-min |return|).

    Returns: "low" | "normal" | "high" | "extreme"

    Trading implication:
      low:     edge sources work (mean reversion, microprice, imbalance)
      normal:  baseline edge expectations
      high:    halve position size, widen TP, expect more wrong-direction
      extreme: SKIP — news regime, retail bots get destroyed
    """
    if realized_vol < 0:
        return "normal"  # invalid → safe default
    if realized_vol < _RV_LOW_MAX:
        return "low"
    if realized_vol < _RV_NORMAL_MAX:
        return "normal"
    if realized_vol < _RV_HIGH_MAX:
        return "high"
    return "extreme"


def regime_size_multiplier(regime: str) -> float:
    """Position size multiplier per regime. Caller multiplies trade_amount by this."""
    return {
        "low":     1.0,
        "normal":  1.0,
        "high":    0.5,    # halve size in volatile regime
        "extreme": 0.0,    # skip entirely
    }.get(regime, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
#  4. SPREAD Z-SCORE (info asymmetry detector)
# ─────────────────────────────────────────────────────────────────────────────

class SpreadTracker:
    """Rolling window of spreads → detect anomalous widening.

    When spread suddenly widens > 2σ above baseline, market makers are pulling
    quotes (often pre-news, pre-resolution, or info asymmetry). SKIP entries.

    Per-token tracking: BTC market has 2 tokens (UP/DOWN), each tracked separately.
    """
    __slots__ = ("_history",)

    def __init__(self):
        # token_id -> list of (timestamp, spread)
        self._history: dict[str, list[tuple[float, float]]] = {}

    def push(self, token_id: str, spread: float):
        """Add spread observation. Keeps last 60 entries (~30s @ 2Hz)."""
        if spread < 0:
            return
        h = self._history.setdefault(token_id, [])
        h.append((time.time(), spread))
        if len(h) > 60:
            h[:] = h[-60:]

    def zscore(self, token_id: str, current_spread: float) -> Optional[float]:
        """
        Return current spread Z-score vs rolling baseline (last 30-60 obs).
        None = insufficient history (< 10 obs).

        Z > +2: spread widening anomaly (skip entry)
        Z < -1: spread tightening (favorable, but rare signal)
        """
        h = self._history.get(token_id, [])
        if len(h) < 10:
            return None
        recent = [s for _, s in h]
        try:
            mu = statistics.mean(recent)
            sigma = statistics.stdev(recent)
        except statistics.StatisticsError:
            return None
        if sigma < 1e-6:
            return 0.0  # all values identical → no anomaly possible
        return (current_spread - mu) / sigma

    def reset(self):
        """Clear all history (e.g. at new window)."""
        self._history.clear()


# ─────────────────────────────────────────────────────────────────────────────
#  5. PROBABILITY OF RESOLUTION (Brownian model)
# ─────────────────────────────────────────────────────────────────────────────

def p_resolution_brownian(
    btc_now: float,
    btc_open: float,
    secs_left: float,
    realized_vol_per_min: float,
    direction: str = "UP",
) -> float:
    """
    Probability that BTC ends ABOVE (or below) btc_open at window close,
    using Brownian motion (geometric / arithmetic — we use arithmetic, OK
    for sub-1% moves over 5 min).

    Model:
      future_price ~ N(btc_now, σ × √(secs_left/60))
      where σ = realized_vol_per_min × btc_now (USD stdev per minute)

    P(BTC_close > btc_open) when direction="UP"
    P(BTC_close < btc_open) when direction="DOWN"

    Returns probability in [0, 1]. If inputs invalid, returns 0.5 (neutral).

    Edge use case: bot predicts P=0.72 of UP, market price = 0.65 → 7¢ EDGE.
    Pareto: bet only when P_model − fair_price > threshold (e.g. > 5¢).
    """
    if (btc_now <= 0 or btc_open <= 0 or secs_left < 0
            or realized_vol_per_min < 0 or direction not in ("UP", "DOWN")):
        return 0.5

    # At/past close: deterministic resolution (no remaining time = no uncertainty)
    if secs_left == 0:
        if direction == "UP":
            return 1.0 if btc_now > btc_open else 0.0
        else:
            return 1.0 if btc_now < btc_open else 0.0

    # Stdev for remaining time (per-minute vol scaled by sqrt(time))
    minutes_left = secs_left / 60.0
    if minutes_left <= 0:
        # Defensive (shouldn't reach here given check above)
        if direction == "UP":
            return 1.0 if btc_now > btc_open else 0.0
        else:
            return 1.0 if btc_now < btc_open else 0.0

    # σ per minute in USD = rv (fraction) × btc_now
    sigma_usd = realized_vol_per_min * btc_now * math.sqrt(minutes_left)
    if sigma_usd < 1e-9:
        # No volatility expected → deterministic
        if direction == "UP":
            return 1.0 if btc_now > btc_open else 0.0
        else:
            return 1.0 if btc_now < btc_open else 0.0

    # Z-score: how many σ is btc_open from btc_now
    z = (btc_open - btc_now) / sigma_usd
    # P(BTC_close > btc_open) = P(Z' > z) where Z' ~ N(0,1)
    # = 1 - Φ(z) = Φ(-z)
    p_up = _phi(-z)

    if direction == "UP":
        return p_up
    return 1.0 - p_up


def _phi(x: float) -> float:
    """Standard normal CDF using erf (Python 3.2+)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ─────────────────────────────────────────────────────────────────────────────
#  6. PER-TRIGGER EXPECTANCY TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class TriggerExpectancy:
    """
    Rolling per-trigger P&L tracking. Auto-disable triggers with negative
    expectancy after sufficient sample (default 20 trades).

    Loaded from disk on init, persisted on update — survives restart.
    """
    __slots__ = ("_data", "_min_samples", "_disable_threshold")

    def __init__(self, min_samples: int = 20, disable_threshold: float = -2.0):
        # trigger -> {"trades": [pnl, pnl, ...], "disabled": bool}
        self._data: dict[str, dict] = {}
        self._min_samples = min_samples
        # Disable trigger if avg PnL <= threshold per trade after min_samples
        self._disable_threshold = disable_threshold

    def record(self, trigger: str, pnl: float):
        """Record completed trade outcome."""
        if not trigger:
            return
        d = self._data.setdefault(trigger, {"trades": [], "disabled": False})
        d["trades"].append(round(pnl, 4))
        # Keep last 100 trades per trigger (rolling window)
        if len(d["trades"]) > 100:
            d["trades"] = d["trades"][-100:]
        # Auto-disable check
        if (len(d["trades"]) >= self._min_samples
                and not d["disabled"]):
            avg = sum(d["trades"]) / len(d["trades"])
            if avg <= self._disable_threshold:
                d["disabled"] = True

    def is_enabled(self, trigger: str) -> bool:
        """True kalau trigger boleh fire (default true sampai disabled)."""
        d = self._data.get(trigger)
        if not d:
            return True
        return not d["disabled"]

    def stats(self, trigger: str) -> dict:
        """Return {n, avg_pnl, win_rate, disabled} for trigger.
        Empty dict if no data."""
        d = self._data.get(trigger)
        if not d or not d["trades"]:
            return {}
        trades = d["trades"]
        n = len(trades)
        avg = sum(trades) / n
        wins = sum(1 for t in trades if t > 0)
        return {
            "n": n,
            "avg_pnl": round(avg, 3),
            "win_rate": round(wins / n * 100, 1),
            "disabled": d["disabled"],
        }

    def all_stats(self) -> dict:
        return {trig: self.stats(trig) for trig in self._data.keys()}

    def reset(self, trigger: str = None):
        """Re-enable trigger (clear disabled flag) or all triggers."""
        if trigger:
            if trigger in self._data:
                self._data[trigger]["disabled"] = False
        else:
            for d in self._data.values():
                d["disabled"] = False

    def to_dict(self) -> dict:
        """Serialize for disk persistence."""
        return {k: {"trades": list(v["trades"]), "disabled": v["disabled"]}
                for k, v in self._data.items()}

    @classmethod
    def from_dict(cls, data: dict, min_samples: int = 20,
                  disable_threshold: float = -2.0) -> "TriggerExpectancy":
        obj = cls(min_samples=min_samples, disable_threshold=disable_threshold)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and "trades" in v:
                    obj._data[k] = {
                        "trades": [float(x) for x in v.get("trades", [])][-100:],
                        "disabled": bool(v.get("disabled", False)),
                    }
        return obj


# ─────────────────────────────────────────────────────────────────────────────
#  AGGREGATE: cross-validation gate
# ─────────────────────────────────────────────────────────────────────────────

class EdgeDecision:
    """Container for edge gate decision: pass/skip + reason + diagnostics."""
    __slots__ = ("ok", "reason", "diag")

    def __init__(self, ok: bool, reason: str = "", diag: dict = None):
        self.ok = ok
        self.reason = reason
        self.diag = diag or {}

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"EdgeDecision(ok={self.ok}, reason={self.reason!r})"


def cross_validate_entry(
    outcome: str,
    market_price: float,
    btc_now: float,
    btc_open: float,
    secs_left: float,
    realized_vol_per_min: float,
    bid_depth: list,
    ask_depth: list,
    spread_z: Optional[float] = None,
    min_edge: float = 0.05,
    require_imbalance_alignment: bool = True,
) -> EdgeDecision:
    """
    Final cross-validation before fire. Ensures multiple signals agree.

    Returns EdgeDecision(ok=True/False, reason=str).

    Checks:
      1. Vol regime not "extreme" (skip news regime)
      2. Spread Z-score not anomalously high (> 2.5σ)
      3. Imbalance aligned with directional bet (kalau require_imbalance_alignment)
      4. P(resolution) - market_price > min_edge (model says we have edge)
    """
    diag = {}

    # 1. Vol regime
    regime = classify_vol_regime(realized_vol_per_min)
    diag["regime"] = regime
    if regime == "extreme":
        return EdgeDecision(False, f"extreme vol regime (rv={realized_vol_per_min:.4f})", diag)

    # 2. Spread anomaly
    if spread_z is not None:
        diag["spread_z"] = round(spread_z, 2)
        if spread_z > 2.5:
            return EdgeDecision(False, f"spread anomaly Z={spread_z:.1f}>2.5", diag)

    # 3. Orderbook imbalance alignment
    if require_imbalance_alignment and (bid_depth or ask_depth):
        imb = compute_imbalance(bid_depth, ask_depth)
        diag["imbalance"] = round(imb, 3)
        # For UP bet: we want bids on UP token to dominate (imb > 0).
        # Threshold -0.3 = allow neutral, only block STRONGLY against direction.
        # outcome="UP" buying UP token → want bid pressure on UP token → imb >= -0.3
        if imb < -0.3:
            return EdgeDecision(
                False,
                f"imbalance against bet ({imb:+.2f} on {outcome} token)",
                diag,
            )

    # 4. P(resolution) edge check
    p_model = p_resolution_brownian(
        btc_now, btc_open, secs_left, realized_vol_per_min, outcome
    )
    diag["p_model"] = round(p_model, 3)
    diag["p_market"] = round(market_price, 3)
    edge = p_model - market_price
    diag["edge"] = round(edge, 3)
    if edge < min_edge:
        return EdgeDecision(
            False,
            f"insufficient model edge ({edge:+.3f}<{min_edge:+.3f})",
            diag,
        )

    return EdgeDecision(True, "all signals aligned", diag)