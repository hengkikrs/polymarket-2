"""
market_regime.py — Adaptive Market Regime Classifier
======================================================

Classifies current market state into 7 regimes and emits adaptive parameters
(size multiplier, allowed triggers, TP/SL adjustments) so bot behavior changes
intelligently based on conditions.

Regimes:
  TRENDING_UP    — sustained upward drift, BTC distance growing
  TRENDING_DOWN  — sustained downward drift
  RANGING        — oscillating near open, low net move
  HIGH_VOL       — realized vol > threshold (volatile but unclear direction)
  LOW_VOL_DEAD   — near-zero realized vol, no movement
  NEWS_SPIKE     — sudden BTC tick swing (FOMC/CPI release)
  MANIPULATION   — orderbook depth wipe (spoofing detected)
  UNKNOWN        — insufficient data (bot baru start, < 30s of ticks)

Each regime emits a `RegimeProfile` dict consumed by main.py / strategy_b.py:
  - allowed_triggers: set of trigger codes that should fire in this regime
  - size_multiplier: scale trade_amount (0.0 = SKIP, 1.0 = full, 0.5 = half)
  - tp_multiplier:   widen/narrow TP (1.2 = wider, 0.8 = tighter)
  - max_entry_price: cap on entry price (skip expensive entries in unstable regime)
  - reason:          human-readable explanation for logging

Design:
  - Single-pass O(N) classification per tick from price history
  - Hysteresis: regime persists for `_regime_min_dwell` seconds to avoid flapping
  - All thresholds tunable via env (defaults calibrated for BTC 2025-2026 vol)
"""

from __future__ import annotations
import math
import os
import time
import statistics
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

REGIME_TRENDING_UP   = "TRENDING_UP"
REGIME_TRENDING_DOWN = "TRENDING_DOWN"
REGIME_RANGING       = "RANGING"
REGIME_HIGH_VOL      = "HIGH_VOL"
REGIME_LOW_VOL_DEAD  = "LOW_VOL_DEAD"
REGIME_NEWS_SPIKE    = "NEWS_SPIKE"
REGIME_MANIPULATION  = "MANIPULATION"
REGIME_UNKNOWN       = "UNKNOWN"

ALL_REGIMES = (
    REGIME_TRENDING_UP, REGIME_TRENDING_DOWN, REGIME_RANGING,
    REGIME_HIGH_VOL, REGIME_LOW_VOL_DEAD, REGIME_NEWS_SPIKE,
    REGIME_MANIPULATION, REGIME_UNKNOWN,
)


# Tunable thresholds (env overrideable)
def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (ValueError, TypeError):
        return default


# Trending detection: net BTC move over 60s relative to open
_TREND_DISTANCE_MIN     = _f("REGIME_TREND_DIST_MIN", 30.0)    # USD: BTC must be ≥30 from open
_TREND_VELOCITY_MIN     = _f("REGIME_TREND_VEL_MIN", 5.0)      # USD/min: directional drift

# Ranging: BTC oscillates within a range
_RANGE_DISTANCE_MAX     = _f("REGIME_RANGE_DIST_MAX", 15.0)    # USD: BTC stays within $15 from open
_RANGE_OSC_MIN          = _f("REGIME_RANGE_OSC_MIN", 5.0)      # USD: total oscillation > $5

# Vol thresholds (per-min realized vol as fraction of price)
_VOL_LOW_DEAD_MAX       = _f("REGIME_VOL_LOW_MAX", 0.0003)     # < 0.03%/min = dead market
_VOL_HIGH_MIN           = _f("REGIME_VOL_HIGH_MIN", 0.0030)    # > 0.3%/min = high vol
_VOL_NEWS_MIN           = _f("REGIME_VOL_NEWS_MIN", 0.0080)    # > 0.8%/min = news regime

# News spike: single-tick BTC swing magnitude
_NEWS_SPIKE_USD         = _f("REGIME_NEWS_SPIKE_USD", 50.0)    # > $50 in 5s = news event
_NEWS_LOCKOUT_SECS      = _f("REGIME_NEWS_LOCKOUT", 30.0)      # SKIP all entries N secs after spike

# Manipulation: depth wipe detection
_MANIP_DEPTH_DROP_PCT   = _f("REGIME_MANIP_DEPTH_DROP", 0.70)  # > 70% depth drop in 3s
_MANIP_LOCKOUT_SECS     = _f("REGIME_MANIP_LOCKOUT", 15.0)

# Hysteresis
_REGIME_MIN_DWELL_SECS  = _f("REGIME_MIN_DWELL", 5.0)          # regime sticky for ≥5s
_DATA_MIN_TICKS         = int(_f("REGIME_DATA_MIN_TICKS", 30))  # min ticks before classify


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME PROFILE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeProfile:
    """Adaptive parameters produced per regime. Consumed by main.py."""
    regime: str = REGIME_UNKNOWN
    # Set of trigger codes allowed in this regime ("ALL" = wildcard)
    allowed_triggers: set = field(default_factory=lambda: {"ALL"})
    size_multiplier: float = 1.0      # scale trade_amount
    tp_multiplier:   float = 1.0      # widen TP (>1) or tighten (<1)
    max_entry_price: float = 0.99     # cap entry price (avoid expensive entries)
    reason: str = ""

    def allows(self, trigger: str) -> bool:
        return "ALL" in self.allowed_triggers or trigger in self.allowed_triggers

    def __repr__(self):
        return (f"<{self.regime} size×{self.size_multiplier} "
                f"tp×{self.tp_multiplier} max=${self.max_entry_price} "
                f"trigs={'|'.join(sorted(self.allowed_triggers))}>")


# Per-regime profile templates
def _profile_for(regime: str, reason: str = "") -> RegimeProfile:
    """Build profile per regime. Centralizes adaptive parameter logic."""
    if regime == REGIME_TRENDING_UP:
        # BTC trending up: favor UP-side momentum triggers (T2/T3/T4/T5)
        # B5 mandatory still allowed. Size full. Wider TP for trend continuation.
        return RegimeProfile(
            regime=regime,
            allowed_triggers={"T1", "T2", "T3", "T4", "T5", "B1", "B2", "B5", "TX"},
            size_multiplier=1.0,
            tp_multiplier=1.1,           # 10% wider TP (let trend run)
            max_entry_price=0.85,        # avoid super-mature entries
            reason=reason or "BTC trending UP",
        )
    if regime == REGIME_TRENDING_DOWN:
        return RegimeProfile(
            regime=regime,
            allowed_triggers={"T1", "T2", "T3", "T4", "T5", "B1", "B2", "B5", "TX"},
            size_multiplier=1.0,
            tp_multiplier=1.1,
            max_entry_price=0.85,
            reason=reason or "BTC trending DOWN",
        )
    if regime == REGIME_RANGING:
        # Ranging: mean-reversion friendly. T6 (reversal) preferred. Tighter TP.
        return RegimeProfile(
            regime=regime,
            allowed_triggers={"T1", "T6", "T7", "B3", "B5"},
            size_multiplier=0.8,
            tp_multiplier=0.85,          # tighter TP (no breakout expected)
            max_entry_price=0.75,
            reason=reason or "BTC ranging near open",
        )
    if regime == REGIME_HIGH_VOL:
        # Volatile: halve size, mandatory only.
        return RegimeProfile(
            regime=regime,
            allowed_triggers={"B5", "TX"},
            size_multiplier=0.5,
            tp_multiplier=1.2,
            max_entry_price=0.80,
            reason=reason or "HIGH realized vol",
        )
    if regime == REGIME_LOW_VOL_DEAD:
        # Dead: no edge, skip A entries entirely. Only B5 mandatory.
        return RegimeProfile(
            regime=regime,
            allowed_triggers={"B5"},
            size_multiplier=0.7,         # smaller bets, less liquidity available
            tp_multiplier=1.0,
            max_entry_price=0.70,
            reason=reason or "DEAD market (low vol)",
        )
    if regime == REGIME_NEWS_SPIKE:
        # News: lockout all entries
        return RegimeProfile(
            regime=regime,
            allowed_triggers=set(),       # NO triggers
            size_multiplier=0.0,
            tp_multiplier=1.0,
            max_entry_price=0.0,
            reason=reason or "NEWS SPIKE — entries locked out",
        )
    if regime == REGIME_MANIPULATION:
        return RegimeProfile(
            regime=regime,
            allowed_triggers=set(),
            size_multiplier=0.0,
            tp_multiplier=1.0,
            max_entry_price=0.0,
            reason=reason or "MANIPULATION suspected (depth wipe)",
        )
    # UNKNOWN: full access (default safe behavior pre-classification)
    return RegimeProfile(
        regime=REGIME_UNKNOWN,
        allowed_triggers={"ALL"},
        size_multiplier=1.0,
        tp_multiplier=1.0,
        max_entry_price=0.99,
        reason=reason or "insufficient data",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME CLASSIFIER STATE
# ─────────────────────────────────────────────────────────────────────────────

class RegimeClassifier:
    """
    Stateful classifier — pushes BTC ticks + market state, emits regime + profile.
    Maintains hysteresis so regime doesn't flap (changes only if new regime
    persists for `_REGIME_MIN_DWELL_SECS` seconds, except news/manipulation
    which are immediate lockouts).

    Usage:
        cls = RegimeClassifier()
        cls.push_tick(btc_now=100050, ts=time.time())
        cls.push_depth(token_id, total_depth=200.0)  # for manip detection
        profile = cls.classify(btc_open=100000, realized_vol=0.001, secs_elapsed=120)
        if profile.allows("T2"): ...
    """
    __slots__ = (
        "_ticks", "_depth_history",
        "_news_lockout_until", "_manip_lockout_until",
        "_current_regime", "_regime_set_at",
        "_pending_regime", "_pending_set_at",
    )

    def __init__(self):
        # (ts, btc_price) — last 300s of ticks (~300 entries @ 1Hz, more @ 5Hz)
        self._ticks: list[tuple[float, float]] = []
        # token_id -> list of (ts, total_depth_size) for manipulation detection
        self._depth_history: dict[str, list[tuple[float, float]]] = {}
        # Lockout timestamps
        self._news_lockout_until: float = 0.0
        self._manip_lockout_until: float = 0.0
        # Regime state with hysteresis
        self._current_regime: str = REGIME_UNKNOWN
        self._regime_set_at: float = 0.0
        # Pending: regime that wants to become current (waiting for dwell)
        self._pending_regime: Optional[str] = None
        self._pending_set_at: float = 0.0

    def push_tick(self, btc_now: float, ts: Optional[float] = None):
        """Push BTC price observation."""
        if btc_now is None or btc_now <= 0:
            return
        if ts is None:
            ts = time.time()
        # News spike detection: BTC swing > $X in 5s
        if self._ticks:
            recent5 = [p for t, p in self._ticks if ts - t <= 5.0]
            if recent5:
                lo, hi = min(recent5), max(recent5)
                # current tick included
                lo = min(lo, btc_now); hi = max(hi, btc_now)
                if hi - lo >= _NEWS_SPIKE_USD:
                    self._news_lockout_until = ts + _NEWS_LOCKOUT_SECS
        self._ticks.append((ts, btc_now))
        # Cap to 300s (defensive)
        if len(self._ticks) > 0:
            cutoff = ts - 300.0
            self._ticks = [t for t in self._ticks if t[0] >= cutoff]

    def push_depth(self, token_id: str, total_depth: float, ts: Optional[float] = None):
        """Push aggregate depth (sum of bid sizes top-5) for manipulation detection."""
        if total_depth < 0:
            return
        if ts is None:
            ts = time.time()
        h = self._depth_history.setdefault(token_id, [])
        # Manipulation: depth drops > 70% in 3s vs prior baseline
        if h:
            recent3 = [s for t, s in h if ts - t <= 3.0]
            if recent3:
                baseline = max(recent3)  # peak depth in last 3s
                if baseline > 10 and total_depth < baseline * (1 - _MANIP_DEPTH_DROP_PCT):
                    self._manip_lockout_until = ts + _MANIP_LOCKOUT_SECS
        h.append((ts, total_depth))
        # Cap to 60s
        cutoff = ts - 60.0
        h[:] = [x for x in h if x[0] >= cutoff]

    def reset(self):
        """Reset on new window (regime is per-window context)."""
        self._ticks.clear()
        self._depth_history.clear()
        self._news_lockout_until = 0.0
        self._manip_lockout_until = 0.0
        self._current_regime = REGIME_UNKNOWN
        self._regime_set_at = 0.0
        self._pending_regime = None
        self._pending_set_at = 0.0

    def _classify_raw(self, btc_open: float, realized_vol: float,
                      secs_elapsed: float, ts: float) -> tuple[str, str]:
        """Inner classifier: return (regime, reason). No hysteresis applied."""
        # Hard lockouts override everything
        if ts < self._news_lockout_until:
            secs_until = round(self._news_lockout_until - ts)
            return REGIME_NEWS_SPIKE, f"news lockout {secs_until}s remain"
        if ts < self._manip_lockout_until:
            secs_until = round(self._manip_lockout_until - ts)
            return REGIME_MANIPULATION, f"manip lockout {secs_until}s remain"

        # Need data for classification
        if len(self._ticks) < _DATA_MIN_TICKS or btc_open <= 0:
            return REGIME_UNKNOWN, "insufficient ticks"

        btc_now = self._ticks[-1][1]
        btc_dist_signed = btc_now - btc_open
        btc_dist = abs(btc_dist_signed)

        # Velocity: BTC change over last 60s (USD/min)
        velocity = self._velocity_60s(ts)

        # Oscillation: range of BTC in last 60s
        recent60 = [p for t, p in self._ticks if ts - t <= 60.0]
        oscillation = (max(recent60) - min(recent60)) if recent60 else 0.0

        # Vol regime classification
        if realized_vol >= _VOL_NEWS_MIN:
            # Treat extreme vol as news (overlap with news_lockout if recent spike)
            return REGIME_NEWS_SPIKE, f"vol={realized_vol:.4f}>news_min"

        if realized_vol >= _VOL_HIGH_MIN:
            return REGIME_HIGH_VOL, f"vol={realized_vol:.4f}>high"

        # LOW VOL DEAD: very low vol AND minimal movement
        # Only classify after sufficient elapsed time (early-window has natural low movement)
        if (realized_vol < _VOL_LOW_DEAD_MAX
                and secs_elapsed >= 60
                and btc_dist < 5.0
                and oscillation < 5.0):
            return REGIME_LOW_VOL_DEAD, (
                f"vol={realized_vol:.5f} dist=${btc_dist:.0f} osc=${oscillation:.0f}"
            )

        # TRENDING: directional drift + sustained distance
        if (btc_dist >= _TREND_DISTANCE_MIN
                and abs(velocity) >= _TREND_VELOCITY_MIN
                # Velocity sign must match distance sign (consistent trend)
                and (velocity > 0) == (btc_dist_signed > 0)):
            if btc_dist_signed > 0:
                return REGIME_TRENDING_UP, (
                    f"dist=+${btc_dist:.0f} vel=+${velocity:.1f}/min"
                )
            else:
                return REGIME_TRENDING_DOWN, (
                    f"dist=-${btc_dist:.0f} vel={velocity:.1f}/min"
                )

        # RANGING: low net distance but some oscillation
        if btc_dist <= _RANGE_DISTANCE_MAX and oscillation >= _RANGE_OSC_MIN:
            return REGIME_RANGING, f"dist=${btc_dist:.0f} osc=${oscillation:.0f}"

        # Default fallback: unknown / transitional
        return REGIME_UNKNOWN, (
            f"dist=${btc_dist:.0f} vel=${velocity:.1f}/min osc=${oscillation:.0f}"
        )

    def _velocity_60s(self, ts: float) -> float:
        """BTC velocity over last 60s, in USD/minute."""
        if len(self._ticks) < 2:
            return 0.0
        recent = [(t, p) for t, p in self._ticks if ts - t <= 60.0]
        if len(recent) < 2:
            return 0.0
        t_start, p_start = recent[0]
        t_end,   p_end   = recent[-1]
        dt = t_end - t_start
        if dt < 1.0:
            return 0.0
        # Scale to per-minute
        return (p_end - p_start) * (60.0 / dt)

    def classify(self, btc_open: float, realized_vol: float,
                 secs_elapsed: float, ts: Optional[float] = None) -> RegimeProfile:
        """
        Classify current regime + return adaptive profile (with hysteresis).

        Hysteresis: regime change only after candidate persists ≥ _REGIME_MIN_DWELL_SECS,
        EXCEPT news/manipulation lockouts which are immediate (safety overrides).
        """
        if ts is None:
            ts = time.time()

        new_regime, reason = self._classify_raw(btc_open, realized_vol, secs_elapsed, ts)

        # Lockouts are immediate (no hysteresis — safety override)
        if new_regime in (REGIME_NEWS_SPIKE, REGIME_MANIPULATION):
            self._current_regime = new_regime
            self._regime_set_at = ts
            self._pending_regime = None
            return _profile_for(new_regime, reason)

        # No change → keep current
        if new_regime == self._current_regime:
            self._pending_regime = None
            return _profile_for(self._current_regime, reason)

        # Different from current — check pending dwell
        if self._pending_regime != new_regime:
            # Start new pending
            self._pending_regime = new_regime
            self._pending_set_at = ts
            return _profile_for(self._current_regime,
                                f"hysteresis: pending→{new_regime}")

        # Same pending → check dwell time
        dwell = ts - self._pending_set_at
        if dwell >= _REGIME_MIN_DWELL_SECS:
            # Promote pending → current
            self._current_regime = new_regime
            self._regime_set_at = ts
            self._pending_regime = None
            return _profile_for(new_regime, reason)

        # Still in dwell, keep current
        return _profile_for(self._current_regime,
                            f"hysteresis: pending→{new_regime} ({dwell:.1f}s/{_REGIME_MIN_DWELL_SECS:.0f}s)")

    @property
    def current_regime(self) -> str:
        return self._current_regime

    def diagnostics(self) -> dict:
        """Return diagnostic snapshot (untuk dashboard / logging)."""
        ts = time.time()
        return {
            "regime": self._current_regime,
            "pending": self._pending_regime,
            "ticks_n": len(self._ticks),
            "news_lockout_remain": max(0, self._news_lockout_until - ts),
            "manip_lockout_remain": max(0, self._manip_lockout_until - ts),
        }