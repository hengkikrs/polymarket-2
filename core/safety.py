"""Central safety and market validation helpers."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import core.config as config
import core.state as st


LIVE_CONFIRM_TEXT = "I_UNDERSTAND_THIS_IS_REAL_MONEY"


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


def _b(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class SafetyConfig:
    min_entry_secs_left: float = 4.0
    max_book_age_secs: float = 8.0
    max_spread: float = 0.04
    min_liquidity_mult: float = 1.0
    require_orderbook_in_paper: bool = True
    max_trades_per_window_live: int = 9
    max_live_trade_usd: float = 10.0
    max_live_window_exposure_usd: float = 20.0
    max_paper_window_exposure_usd: float = 500.0
    max_api_error_rate_pct: float = 30.0
    max_time_skew_secs: float = 2.0

    @classmethod
    def from_env(cls) -> "SafetyConfig":
        return cls(
            min_entry_secs_left=_f("SAFETY_MIN_ENTRY_SECS_LEFT", 4.0),
            max_book_age_secs=_f("SAFETY_MAX_BOOK_AGE_SECS", 8.0),
            max_spread=_f("SAFETY_MAX_SPREAD", 0.04),
            min_liquidity_mult=_f("SAFETY_MIN_LIQUIDITY_MULT", 1.0),
            require_orderbook_in_paper=_b("SAFETY_REQUIRE_ORDERBOOK_IN_PAPER", "true"),
            max_trades_per_window_live=_i("SAFETY_MAX_TRADES_PER_WINDOW_LIVE", 9),
            max_live_trade_usd=_f("SAFETY_MAX_LIVE_TRADE_USD", 10.0),
            max_live_window_exposure_usd=_f("SAFETY_MAX_LIVE_WINDOW_EXPOSURE_USD", 20.0),
            max_paper_window_exposure_usd=_f("SAFETY_MAX_PAPER_WINDOW_EXPOSURE_USD", 500.0),
            max_api_error_rate_pct=_f("SAFETY_MAX_API_ERROR_RATE_PCT", 30.0),
            max_time_skew_secs=_f("SAFETY_MAX_TIME_SKEW_SECS", 2.0),
        )


@dataclass
class GateDecision:
    ok: bool
    reason: str = ""
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "ok", **metrics: Any) -> "GateDecision":
        return cls(True, reason=reason, metrics=metrics)

    @classmethod
    def reject(
        cls,
        failures: Iterable[str],
        warnings: Iterable[str] = (),
        **metrics: Any,
    ) -> "GateDecision":
        fail_list = [str(f) for f in failures if str(f)]
        return cls(
            False,
            reason="; ".join(fail_list) if fail_list else "rejected",
            failures=fail_list,
            warnings=[str(w) for w in warnings if str(w)],
            metrics=metrics,
        )


def is_live_mode() -> bool:
    return not bool(getattr(config, "MOCK_MODE", True))


def startup_safety_report(
    cfg: st.BotSettings,
    scfg: SafetyConfig | None = None,
) -> GateDecision:
    """Validate dangerous live settings before the bot can trade."""
    scfg = scfg or SafetyConfig.from_env()
    failures: list[str] = []
    live = is_live_mode()

    if live and os.getenv("LIVE_TRADING_CONFIRM", "") != LIVE_CONFIRM_TEXT:
        failures.append("LIVE_TRADING_CONFIRM missing or invalid")
    if live and not bool(getattr(cfg, "cb_master_enabled", True)):
        failures.append("cb_master_enabled=false is forbidden in live")
    if live and int(getattr(cfg, "max_trades_per_window", 0) or 0) > scfg.max_trades_per_window_live:
        failures.append(f"max_trades_per_window>{scfg.max_trades_per_window_live} forbidden in live")
    live_trade_usd = _f(
        "END_WINDOW_LIVE_TRADE_USD",
        float(getattr(cfg, "trade_amount", 0.0) or 0.0),
    )
    if live and live_trade_usd > scfg.max_live_trade_usd:
        failures.append(f"live_trade_usd>${scfg.max_live_trade_usd:.2f} forbidden in live")

    if failures:
        return GateDecision.reject(failures, live=live)
    return GateDecision.allow("startup safety ok", live=live)


def _price_depth_usd(depth: Any, max_price: float) -> float:
    total = 0.0
    if not depth:
        return 0.0
    for row in depth:
        try:
            if isinstance(row, dict):
                price = float(row.get("price", 0.0) or 0.0)
                size = float(row.get("size", 0.0) or 0.0)
            else:
                price = float(row[0])
                size = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0 or size <= 0 or price > max_price:
            continue
        total += price * size
    return round(total, 4)


def validate_market_for_entry(
    market: Any,
    legs: list[str],
    amounts: dict[str, float],
    *,
    now: float | None = None,
    live: bool | None = None,
    scfg: SafetyConfig | None = None,
    existing_window_exposure_usd: float = 0.0,
) -> GateDecision:
    """Validate market status, executable prices, freshness, spread, and depth."""
    scfg = scfg or SafetyConfig.from_env()
    live = is_live_mode() if live is None else bool(live)
    now = time.time() if now is None else float(now)
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {}
    requested_exposure = sum(max(0.0, float(value or 0.0)) for value in amounts.values())
    total_window_exposure = max(0.0, float(existing_window_exposure_usd or 0.0)) + requested_exposure
    metrics["requested_exposure_usd"] = round(requested_exposure, 4)
    metrics["window_exposure_usd"] = round(total_window_exposure, 4)
    if live:
        for leg, amount in amounts.items():
            if float(amount or 0.0) > scfg.max_live_trade_usd:
                failures.append(
                    f"{str(leg).upper()} amount ${float(amount):.2f}>"
                    f"${scfg.max_live_trade_usd:.2f} live cap"
                )
        if total_window_exposure > scfg.max_live_window_exposure_usd:
            failures.append(
                f"window exposure ${total_window_exposure:.2f}>"
                f"${scfg.max_live_window_exposure_usd:.2f} live cap"
            )

    if market is None:
        return GateDecision.reject(["market missing"])

    slug = str(getattr(market, "slug", "") or "")
    if not slug:
        failures.append("market slug missing")
    if not bool(getattr(market, "active", True)):
        failures.append("market inactive")
    if bool(getattr(market, "closed", False)):
        failures.append("market closed")
    if bool(getattr(market, "archived", False)):
        failures.append("market archived")
    if not bool(getattr(market, "accepting_orders", True)):
        failures.append("market not accepting orders")

    window_ts = int(getattr(market, "window_ts", 0) or 0)
    close_ts = int(getattr(market, "close_ts", 0) or 0)
    window_interval = close_ts - window_ts if close_ts > 0 and window_ts > 0 else 0
    if window_ts <= 0 or window_ts % 300 != 0:
        failures.append("market window timestamp invalid")
    if close_ts <= now:
        failures.append("market expired")
    if window_ts > 0 and close_ts > 0 and all(
        abs(window_interval - expected) > scfg.max_time_skew_secs
        for expected in (300, 900)
    ):
        failures.append("market close timestamp does not match supported window")

    book_ts = float(getattr(market, "book_ts", 0.0) or 0.0)
    book_age = now - book_ts if book_ts > 0 else 999999.0
    metrics["book_age"] = round(book_age, 3)
    if book_ts <= 0:
        if live or scfg.require_orderbook_in_paper:
            failures.append("orderbook timestamp missing")
        else:
            warnings.append("orderbook timestamp missing in paper mode")
    elif book_age < -scfg.max_time_skew_secs:
        failures.append("orderbook timestamp is in the future")
    elif book_age > scfg.max_book_age_secs:
        failures.append(f"orderbook stale age={book_age:.1f}s")

    tokens = {
        "UP": str(getattr(market, "up_token", "") or ""),
        "DOWN": str(getattr(market, "down_token", "") or ""),
    }
    for leg in ("UP", "DOWN"):
        if len(tokens[leg]) < 10:
            failures.append(f"{leg} token invalid")

    for leg in [str(x).upper() for x in legs]:
        bid = float(getattr(market, "up_price" if leg == "UP" else "down_price", 0.0) or 0.0)
        ask = float(getattr(market, "up_ask" if leg == "UP" else "down_ask", 0.0) or 0.0)
        depth = getattr(market, "up_ask_depth" if leg == "UP" else "down_ask_depth", [])
        amount = float(amounts.get(leg, 0.0) or 0.0)
        if amount <= 0:
            failures.append(f"{leg} amount invalid")
        if ask <= 0:
            failures.append(f"{leg} ask missing")
            continue
        if bid <= 0:
            failures.append(f"{leg} bid missing")
            continue
        if not (0.01 <= bid <= 0.99 and 0.01 <= ask <= 0.99):
            failures.append(f"{leg} bid/ask outside valid range")
        if ask < bid:
            failures.append(f"{leg} ask below bid")
        spread = round(ask - bid, 4)
        metrics[f"{leg}_spread"] = spread
        if spread <= 0:
            failures.append(f"{leg} spread unknown")
        elif spread > scfg.max_spread:
            failures.append(f"{leg} spread {spread:.4f}>{scfg.max_spread:.4f}")

        depth_usd = _price_depth_usd(depth, ask)
        metrics[f"{leg}_ask_depth_usd"] = depth_usd
        needed = round(amount * scfg.min_liquidity_mult, 4)
        if live or scfg.require_orderbook_in_paper:
            if depth_usd <= 0:
                failures.append(f"{leg} orderbook missing")
            elif depth_usd < needed:
                failures.append(f"{leg} liquidity ${depth_usd:.2f}<${needed:.2f}")

    if failures:
        return GateDecision.reject(failures, warnings, **metrics)
    return GateDecision.allow("market entry valid", **metrics)
