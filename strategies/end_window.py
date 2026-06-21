"""End-window directional buy strategy."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


def _b(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"true", "1", "yes", "on"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class EndWindowLayer:
    name: str
    seconds_left_max: float
    seconds_left_min: float
    min_distance_usd: float
    min_price: float
    max_price: float
    strict_distance: bool = False

    def matches(self, seconds_left: float, distance_usd: float, price: float) -> bool:
        if seconds_left > self.seconds_left_max:
            return False
        if seconds_left < self.seconds_left_min:
            return False
        if self.min_distance_usd <= 0:
            pass
        elif self.strict_distance:
            if distance_usd <= self.min_distance_usd:
                return False
        elif distance_usd < self.min_distance_usd:
            return False
        return self.min_price <= price <= self.max_price


@dataclass(frozen=True)
class EndWindowConfig:
    enabled: bool = False
    trade_usd: float = 5.0
    live_trade_usd: float = 1.0
    min_trade_usd: float = 5.0
    max_trades_per_window: int = 9
    max_spread: float = 1.0
    force_trade: bool = True
    force_retry_attempts: int = 8
    force_retry_delay_secs: float = 0.25
    force_final_price_cap: float = 0.99
    min_reasonable_price: float = 0.50
    min_side_price_edge: float = 0.03
    # FIX #3: fast-lane open. Saat ask sisi unggul >= fast_open_price dan
    # orderbook/spread lolos, eksekusi segera tanpa menunggu cadence layer.
    fast_open_enabled: bool = False
    fast_open_price: float = 0.90
    fast_open_max_price: float = 0.99
    layers: tuple[EndWindowLayer, ...] = (
        EndWindowLayer("T1", 25.0, 20.0, 70.0, 0.50, 0.90),
        EndWindowLayer("T2", 20.0, 15.0, 55.0, 0.50, 0.92),
        EndWindowLayer("T3", 15.0, 10.0, 35.0, 0.50, 0.94),
        EndWindowLayer("T4", 10.0, 7.0, 35.0, 0.50, 0.95),
        EndWindowLayer("T5", 7.0, 5.0, 25.0, 0.50, 0.95),
        EndWindowLayer("T6", 5.0, 4.0, 12.0, 0.50, 0.99),
    )

    @classmethod
    def from_env(cls) -> "EndWindowConfig":
        return cls(
            enabled=_b("END_WINDOW_ENABLED", "false"),
            trade_usd=_f("END_WINDOW_TRADE_USD", 5.0),
            live_trade_usd=_f("END_WINDOW_LIVE_TRADE_USD", 1.0),
            min_trade_usd=_f("END_WINDOW_MIN_TRADE_USD", 5.0),
            max_trades_per_window=_i("END_WINDOW_MAX_TRADES_PER_WINDOW", 9),
            max_spread=_f("END_WINDOW_MAX_SPREAD", 1.0),
            force_trade=_b("END_WINDOW_FORCE_TRADE", "true"),
            force_retry_attempts=_i("END_WINDOW_FORCE_RETRY_ATTEMPTS", 8),
            force_retry_delay_secs=_f("END_WINDOW_FORCE_RETRY_DELAY_SECS", 0.25),
            force_final_price_cap=_f("END_WINDOW_FORCE_FINAL_PRICE_CAP", 0.98),
            min_reasonable_price=_f("END_WINDOW_MIN_REASONABLE_PRICE", 0.50),
            min_side_price_edge=_f("END_WINDOW_MIN_SIDE_EDGE", 0.03),
            fast_open_enabled=_b("END_WINDOW_FAST_OPEN_ENABLED", "false"),
            fast_open_price=_f("END_WINDOW_FAST_OPEN_PRICE", 0.90),
            fast_open_max_price=_f("END_WINDOW_FAST_OPEN_MAX_PRICE", 0.99),
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "EndWindowConfig":
        base = cls.from_env()
        layers = tuple(
            EndWindowLayer(
                f"T{i}",
                float(getattr(settings, f"t{i}_seconds_max")),
                float(getattr(settings, f"t{i}_seconds_min")),
                float(getattr(settings, f"t{i}_delta_min")),
                float(getattr(settings, f"t{i}_min_price")),
                float(getattr(settings, f"t{i}_max_price")),
            )
            for i in range(1, 7)
        )
        return cls(
            enabled=base.enabled,
            trade_usd=base.trade_usd,
            live_trade_usd=base.live_trade_usd,
            min_trade_usd=base.min_trade_usd,
            max_trades_per_window=int(getattr(settings, "max_trades_per_window", 9)),
            max_spread=base.max_spread,
            force_trade=base.force_trade,
            force_retry_attempts=base.force_retry_attempts,
            force_retry_delay_secs=base.force_retry_delay_secs,
            force_final_price_cap=base.force_final_price_cap,
            min_reasonable_price=base.min_reasonable_price,
            min_side_price_edge=base.min_side_price_edge,
            fast_open_enabled=base.fast_open_enabled,
            fast_open_price=base.fast_open_price,
            fast_open_max_price=base.fast_open_max_price,
            layers=layers,
        )


@dataclass(frozen=True)
class EndWindowDecision:
    ok: bool
    side: str = ""
    price: float = 0.0
    layer: str = ""
    reason: str = ""


def evaluate_entry(
    *,
    seconds_left: float,
    distance_usd: float,
    price: float,
    side: str,
    cfg: EndWindowConfig,
    spread: float = 0.0,
    opposite_price: float = 0.0,
) -> EndWindowDecision:
    if not cfg.enabled:
        return EndWindowDecision(False, reason="disabled")
    side = str(side or "").upper()
    if side not in {"YES", "NO"}:
        return EndWindowDecision(False, reason="no_direction")
    if price <= 0:
        return EndWindowDecision(False, side=side, reason="missing_buy_price")

    seconds_left = max(0.0, float(seconds_left or 0.0))
    distance_usd = abs(float(distance_usd or 0.0))
    price = float(price or 0.0)
    spread = max(0.0, float(spread or 0.0))
    opposite_price = float(opposite_price or 0.0)
    if spread > max(0.0, float(cfg.max_spread or 0.0)):
        return EndWindowDecision(False, side=side, price=price, reason=f"spread_too_wide: {spread:.4f}")
    min_reasonable = max(0.0, float(cfg.min_reasonable_price or 0.0))
    if price < min_reasonable:
        return EndWindowDecision(False, side=side, price=price, reason=f"unreasonable_price: {price:.4f}<{min_reasonable:.4f}")
    min_edge = max(0.0, float(cfg.min_side_price_edge or 0.0))
    if opposite_price > 0 and price + min_edge < opposite_price:
        return EndWindowDecision(
            False,
            side=side,
            price=price,
            reason=f"side_price_not_leading: selected={price:.4f} opposite={opposite_price:.4f}",
        )

    for layer in cfg.layers:
        if layer.matches(seconds_left, distance_usd, price):
            return EndWindowDecision(
                True,
                side=side,
                price=price,
                layer=layer.name,
                reason=(
                    f"{layer.name}: t<={layer.seconds_left_max:.0f}s "
                    f"t>{layer.seconds_left_min:.0f}s "
                    f"distance=${distance_usd:.2f} price={price:.4f}"
                ),
            )
    return EndWindowDecision(
        False,
        side=side,
        price=price,
        reason=(
            f"no_layer: t={seconds_left:.1f}s distance=${distance_usd:.2f} "
            f"price={price:.4f}"
        ),
    )


_cached_cfg: Optional[EndWindowConfig] = None


def get_config() -> EndWindowConfig:
    global _cached_cfg
    if _cached_cfg is None:
        _cached_cfg = EndWindowConfig.from_env()
    return _cached_cfg


def reload_config() -> EndWindowConfig:
    global _cached_cfg
    _cached_cfg = EndWindowConfig.from_env()
    return _cached_cfg


def is_active() -> bool:
    return get_config().enabled


__all__ = [
    "EndWindowConfig",
    "EndWindowDecision",
    "EndWindowLayer",
    "evaluate_entry",
    "get_config",
    "is_active",
    "reload_config",
]
