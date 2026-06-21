"""Polymarket 5m UP/DOWN market analyzer.

This module is intentionally pure: it computes the recommended action and
metrics, but it never places orders.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Literal


Action = Literal[
    "SKIP_ENTRY",
    "BUY_BOTH_LEGS",
    "STRONG_BUY_BOTH_LEGS",
    "AGGRESSIVE_BUY_ONLY_IF_EXECUTION_CLEAN",
    "HOLD_LOCKED",
    "SELL_LOSS_ONLY",
    "SELL_LOSS_AND_ADD_WIN",
    "EMERGENCY_EXIT",
    "DO_NOT_TRADE",
]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AnalyzerConfig:
    max_total_price: float = 0.96
    aggressive_max_total_price: float = 0.97
    safe_total_price: float = 0.94
    share_tolerance: float = 1e-6
    max_spread: float = 0.04
    max_price_feed_age_seconds: float = 2.0
    extreme_volatility: float = 0.75
    time_remaining_threshold: float = 30.0
    distance_threshold: float = 40.0
    min_win_leg_price_for_sell_loss: float = 0.85
    max_loss_leg_price_for_sell: float = 0.15
    min_win_leg_price_for_add: float = 0.70
    max_win_leg_price_for_add: float = 0.85
    max_additional_ratio: float = 0.20
    max_worst_case_loss_ratio: float = 0.70


@dataclass(frozen=True)
class MarketInput:
    market_id: str
    time_remaining: float
    target_price: float
    current_asset_price: float
    up_ask_price: float
    down_ask_price: float
    up_bid_price: float
    down_bid_price: float
    available_capital: float
    capital_per_leg: float
    orderbook_liquidity_up: float
    orderbook_liquidity_down: float
    spread_up: float
    spread_down: float
    price_feed_timestamp: float
    current_time: float
    recent_volatility: float
    recent_price_direction: str
    market_status: str = "OPEN"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "MarketInput":
        upper = {str(k).upper(): v for k, v in data.items()}
        return cls(
            market_id=str(upper.get("MARKET_ID", "")),
            time_remaining=_num(upper.get("TIME_REMAINING")),
            target_price=_num(upper.get("TARGET_PRICE")),
            current_asset_price=_num(upper.get("CURRENT_ASSET_PRICE")),
            up_ask_price=_num(upper.get("UP_ASK_PRICE")),
            down_ask_price=_num(upper.get("DOWN_ASK_PRICE")),
            up_bid_price=_num(upper.get("UP_BID_PRICE")),
            down_bid_price=_num(upper.get("DOWN_BID_PRICE")),
            available_capital=_num(upper.get("AVAILABLE_CAPITAL")),
            capital_per_leg=_num(upper.get("CAPITAL_PER_LEG")),
            orderbook_liquidity_up=_num(upper.get("ORDERBOOK_LIQUIDITY_UP")),
            orderbook_liquidity_down=_num(upper.get("ORDERBOOK_LIQUIDITY_DOWN")),
            spread_up=_num(upper.get("SPREAD_UP")),
            spread_down=_num(upper.get("SPREAD_DOWN")),
            price_feed_timestamp=_num(upper.get("PRICE_FEED_TIMESTAMP")),
            current_time=_num(upper.get("CURRENT_TIME"), time.time()),
            recent_volatility=_num(upper.get("RECENT_VOLATILITY")),
            recent_price_direction=str(upper.get("RECENT_PRICE_DIRECTION", "")),
            market_status=str(upper.get("MARKET_STATUS", "OPEN")),
        )


@dataclass(frozen=True)
class PositionInput:
    position_exists: bool
    initial_up_price: float = 0.0
    initial_down_price: float = 0.0
    shares_up: float = 0.0
    shares_down: float = 0.0
    capital_up: float = 0.0
    capital_down: float = 0.0
    current_up_price: float = 0.0
    current_down_price: float = 0.0
    current_up_bid: float = 0.0
    current_down_bid: float = 0.0

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PositionInput":
        upper = {str(k).upper(): v for k, v in data.items()}
        return cls(
            position_exists=_bool(upper.get("POSITION_EXISTS")),
            initial_up_price=_num(upper.get("INITIAL_UP_PRICE")),
            initial_down_price=_num(upper.get("INITIAL_DOWN_PRICE")),
            shares_up=_num(upper.get("SHARES_UP")),
            shares_down=_num(upper.get("SHARES_DOWN")),
            capital_up=_num(upper.get("CAPITAL_UP")),
            capital_down=_num(upper.get("CAPITAL_DOWN")),
            current_up_price=_num(upper.get("CURRENT_UP_PRICE")),
            current_down_price=_num(upper.get("CURRENT_DOWN_PRICE")),
            current_up_bid=_num(upper.get("CURRENT_UP_BID")),
            current_down_bid=_num(upper.get("CURRENT_DOWN_BID")),
        )


@dataclass(frozen=True)
class MarketAnalysis:
    market_id: str
    market_status: str
    time_remaining: float
    target_price: float
    current_asset_price: float
    distance_from_target: float
    up_ask_price: float
    down_ask_price: float
    total_price: float
    gross_edge: float
    position_exists: bool
    shares_up: float
    shares_down: float
    total_initial_capital: float
    locked_profit: float
    locked_roi: float
    win_leg: str
    loss_leg: str
    win_leg_price: float
    loss_leg_price: float
    confidence_score: float
    recommended_action: Action
    sell_loss_percent: float
    additional_capital: float
    expected_profit_if_correct: float
    risk_level: str
    reason: str

    def to_output(self) -> dict[str, Any]:
        return {k.upper(): v for k, v in asdict(self).items()}


def analyze_market(
    market: MarketInput | dict[str, Any],
    position: PositionInput | dict[str, Any] | None = None,
    cfg: AnalyzerConfig | None = None,
) -> MarketAnalysis:
    cfg = cfg or AnalyzerConfig()
    market = MarketInput.from_mapping(market) if isinstance(market, dict) else market
    if position is None:
        position = PositionInput(position_exists=False)
    elif isinstance(position, dict):
        position = PositionInput.from_mapping(position)

    distance = abs(market.current_asset_price - market.target_price)
    total_price = round(market.up_ask_price + market.down_ask_price, 6)
    gross_edge = round(1.0 - total_price, 6)
    stale = _price_feed_stale(market, cfg)
    spread_wide = market.spread_up > cfg.max_spread or market.spread_down > cfg.max_spread
    volatility_extreme = market.recent_volatility >= cfg.extreme_volatility

    if not position.position_exists:
        return _analyze_entry(
            market,
            cfg,
            distance,
            total_price,
            gross_edge,
            stale,
            spread_wide,
            volatility_extreme,
        )
    return _analyze_locked_position(
        market,
        position,
        cfg,
        distance,
        total_price,
        gross_edge,
        stale,
        spread_wide,
        volatility_extreme,
    )


def _analyze_entry(
    market: MarketInput,
    cfg: AnalyzerConfig,
    distance: float,
    total_price: float,
    gross_edge: float,
    stale: bool,
    spread_wide: bool,
    volatility_extreme: bool,
) -> MarketAnalysis:
    reasons: list[str] = []
    raw_shares_up = market.capital_per_leg / market.up_ask_price if market.up_ask_price > 0 else 0.0
    raw_shares_down = market.capital_per_leg / market.down_ask_price if market.down_ask_price > 0 else 0.0
    target_shares = min(raw_shares_up, raw_shares_down)
    modal_up = target_shares * market.up_ask_price
    modal_down = target_shares * market.down_ask_price
    total_modal = modal_up + modal_down
    locked_profit = target_shares - total_modal
    locked_roi = locked_profit / total_modal if total_modal > 0 else 0.0
    liquidity_low = (
        market.orderbook_liquidity_up < target_shares
        or market.orderbook_liquidity_down < target_shares
    )
    enough_capital = market.available_capital + cfg.share_tolerance >= total_modal

    action: Action
    if total_price >= 1:
        action = "DO_NOT_TRADE"
        reasons.append("total price is >= 1")
    elif total_price > cfg.aggressive_max_total_price:
        action = "SKIP_ENTRY"
        reasons.append(f"total price {total_price:.4f}>{cfg.aggressive_max_total_price:.4f}")
    elif market.time_remaining <= cfg.time_remaining_threshold:
        action = "SKIP_ENTRY"
        reasons.append("market almost finished and no position exists")
    elif locked_profit <= 0:
        action = "SKIP_ENTRY"
        reasons.append("locked profit is not positive")
    elif target_shares <= 0:
        action = "SKIP_ENTRY"
        reasons.append("invalid ask price or target shares")
    elif not enough_capital:
        action = "SKIP_ENTRY"
        reasons.append("available capital is not enough for both legs")
    elif liquidity_low:
        action = "SKIP_ENTRY"
        reasons.append("one or both legs do not have enough liquidity")
    elif spread_wide:
        action = "SKIP_ENTRY"
        reasons.append("spread is too wide")
    elif stale:
        action = "SKIP_ENTRY"
        reasons.append("price feed is stale")
    elif volatility_extreme:
        action = "SKIP_ENTRY"
        reasons.append("recent volatility is extreme")
    elif total_price <= cfg.safe_total_price:
        action = "STRONG_BUY_BOTH_LEGS"
        reasons.append("total price is in strong buy range")
    elif total_price <= cfg.max_total_price:
        action = "BUY_BOTH_LEGS"
        reasons.append("total price is in normal buy range")
    else:
        action = "AGGRESSIVE_BUY_ONLY_IF_EXECUTION_CLEAN"
        reasons.append("total price is only acceptable in aggressive clean execution")

    return MarketAnalysis(
        market_id=market.market_id,
        market_status=market.market_status,
        time_remaining=round(market.time_remaining, 4),
        target_price=market.target_price,
        current_asset_price=market.current_asset_price,
        distance_from_target=round(distance, 6),
        up_ask_price=market.up_ask_price,
        down_ask_price=market.down_ask_price,
        total_price=total_price,
        gross_edge=gross_edge,
        position_exists=False,
        shares_up=round(target_shares, 6),
        shares_down=round(target_shares, 6),
        total_initial_capital=round(total_modal, 6),
        locked_profit=round(locked_profit, 6),
        locked_roi=round(locked_roi, 6),
        win_leg="",
        loss_leg="",
        win_leg_price=0.0,
        loss_leg_price=0.0,
        confidence_score=0.0,
        recommended_action=action,
        sell_loss_percent=0.0,
        additional_capital=0.0,
        expected_profit_if_correct=round(locked_profit, 6),
        risk_level=_risk_level(locked_profit, total_modal),
        reason="; ".join(reasons),
    )


def _analyze_locked_position(
    market: MarketInput,
    position: PositionInput,
    cfg: AnalyzerConfig,
    distance: float,
    total_price: float,
    gross_edge: float,
    stale: bool,
    spread_wide: bool,
    volatility_extreme: bool,
) -> MarketAnalysis:
    total_initial_capital = position.capital_up + position.capital_down
    shares_per_leg = min(position.shares_up, position.shares_down)
    locked_profit = shares_per_leg - total_initial_capital
    locked_roi = locked_profit / total_initial_capital if total_initial_capital > 0 else 0.0

    if abs(position.shares_up - position.shares_down) > cfg.share_tolerance:
        return _position_output(
            market, position, cfg, distance, total_price, gross_edge,
            "", "", 0.0, 0.0, 0.0, "EMERGENCY_EXIT", 0.0, 0.0,
            locked_profit, locked_profit, "HIGH",
            "UP and DOWN shares are not balanced",
        )

    win_leg = "UP" if market.current_asset_price > market.target_price else "DOWN"
    loss_leg = "DOWN" if win_leg == "UP" else "UP"
    win_leg_price = (
        position.current_up_bid or position.current_up_price
        if win_leg == "UP"
        else position.current_down_bid or position.current_down_price
    )
    loss_leg_price = (
        position.current_down_bid or position.current_down_price
        if loss_leg == "DOWN"
        else position.current_up_bid or position.current_up_price
    )
    confidence = compute_confidence_score(
        market=market,
        win_leg=win_leg,
        win_leg_price=win_leg_price,
        loss_leg_price=loss_leg_price,
        cfg=cfg,
    )
    hold_reasons = _hold_reasons(
        market, cfg, distance, confidence, win_leg, win_leg_price,
        loss_leg_price, stale, spread_wide, volatility_extreme,
    )
    if hold_reasons:
        return _position_output(
            market, position, cfg, distance, total_price, gross_edge,
            win_leg, loss_leg, win_leg_price, loss_leg_price, confidence,
            "HOLD_LOCKED", 0.0, 0.0, locked_profit, locked_profit,
            _risk_level(locked_profit, total_initial_capital),
            "; ".join(hold_reasons),
        )

    sell_percent = _sell_loss_percent(confidence)
    sell_decision = _sell_loss_values(
        position, loss_leg, loss_leg_price, sell_percent,
        total_initial_capital, cfg,
    )

    add_decision = None
    if (
        confidence >= 90.0
        and cfg.min_win_leg_price_for_add <= win_leg_price <= cfg.max_win_leg_price_for_add
    ):
        add_ratio = 0.20 if confidence >= 95.0 else 0.10
        add_capital = min(total_initial_capital * add_ratio,
                          total_initial_capital * cfg.max_additional_ratio)
        add_decision = _sell_loss_and_add_values(
            position, win_leg, loss_leg, win_leg_price, loss_leg_price,
            sell_percent, add_capital, total_initial_capital, cfg,
        )

    chosen = add_decision if add_decision and add_decision["risk_ok"] else sell_decision
    if not chosen["risk_ok"]:
        return _position_output(
            market, position, cfg, distance, total_price, gross_edge,
            win_leg, loss_leg, win_leg_price, loss_leg_price, confidence,
            "HOLD_LOCKED", sell_percent, 0.0, locked_profit,
            chosen["worst_case"], "HIGH",
            "risk/reward is not worth unlocking the hedge",
        )

    return _position_output(
        market, position, cfg, distance, total_price, gross_edge,
        win_leg, loss_leg, win_leg_price, loss_leg_price, confidence,
        chosen["action"], sell_percent, chosen["additional_capital"],
        chosen["expected"], chosen["worst_case"],
        _risk_level(chosen["worst_case"], total_initial_capital),
        chosen["reason"],
    )


def compute_confidence_score(
    *,
    market: MarketInput,
    win_leg: str,
    win_leg_price: float,
    loss_leg_price: float,
    cfg: AnalyzerConfig | None = None,
) -> float:
    cfg = cfg or AnalyzerConfig()
    score = 0.0
    distance = abs(market.current_asset_price - market.target_price)
    if market.time_remaining <= 30:
        score += 10
    if market.time_remaining <= 20:
        score += 10
    if distance >= cfg.distance_threshold:
        score += 15
    if distance >= 60:
        score += 10
    if win_leg_price >= cfg.min_win_leg_price_for_sell_loss:
        score += 15
    if loss_leg_price <= cfg.max_loss_leg_price_for_sell:
        score += 15
    if _direction_away_from_target(market.recent_price_direction, win_leg):
        score += 10
    if market.recent_volatility < cfg.extreme_volatility:
        score += 10
    if market.spread_up <= cfg.max_spread and market.spread_down <= cfg.max_spread:
        score += 5
    if market.orderbook_liquidity_up > 0 and market.orderbook_liquidity_down > 0:
        score += 5
    return round(min(score, 100.0), 2)


def _hold_reasons(
    market: MarketInput,
    cfg: AnalyzerConfig,
    distance: float,
    confidence: float,
    win_leg: str,
    win_leg_price: float,
    loss_leg_price: float,
    stale: bool,
    spread_wide: bool,
    volatility_extreme: bool,
) -> list[str]:
    reasons: list[str] = []
    if market.time_remaining > cfg.time_remaining_threshold:
        reasons.append("time remaining is above end-window threshold")
    if distance < cfg.distance_threshold:
        reasons.append("asset is too close to target")
    if confidence < 85:
        reasons.append("confidence score is below 85")
    if win_leg_price < cfg.min_win_leg_price_for_add:
        reasons.append("win leg price is below add threshold")
    if loss_leg_price > 0.20:
        reasons.append("loss leg price is too high")
    if stale:
        reasons.append("price feed is stale")
    if market.orderbook_liquidity_up <= 0 or market.orderbook_liquidity_down <= 0:
        reasons.append("orderbook liquidity is low")
    if spread_wide:
        reasons.append("spread is too wide")
    if volatility_extreme:
        reasons.append("recent volatility is extreme")
    if not _direction_away_from_target(market.recent_price_direction, win_leg):
        reasons.append("market direction is unclear")
    return reasons


def _sell_loss_values(
    position: PositionInput,
    loss_leg: str,
    loss_leg_bid: float,
    sell_percent: float,
    total_initial_capital: float,
    cfg: AnalyzerConfig,
) -> dict[str, Any]:
    shares_win = position.shares_up if loss_leg == "DOWN" else position.shares_down
    shares_loss = position.shares_down if loss_leg == "DOWN" else position.shares_up
    shares_to_sell = shares_loss * sell_percent
    cash = shares_to_sell * loss_leg_bid
    remaining_loss = shares_loss - shares_to_sell
    expected = shares_win + cash - total_initial_capital
    worst = remaining_loss + cash - total_initial_capital
    return {
        "action": "SELL_LOSS_ONLY",
        "expected": round(expected, 6),
        "worst_case": round(worst, 6),
        "additional_capital": 0.0,
        "risk_ok": _worst_case_ok(worst, total_initial_capital, cfg),
        "reason": f"sell loss leg {loss_leg} at {sell_percent * 100:.0f}%",
    }


def _sell_loss_and_add_values(
    position: PositionInput,
    win_leg: str,
    loss_leg: str,
    win_leg_ask: float,
    loss_leg_bid: float,
    sell_percent: float,
    additional_capital: float,
    total_initial_capital: float,
    cfg: AnalyzerConfig,
) -> dict[str, Any]:
    if win_leg_ask > 0.90:
        return {"risk_ok": False, "worst_case": -total_initial_capital}
    shares_win = position.shares_up if win_leg == "UP" else position.shares_down
    shares_loss = position.shares_down if loss_leg == "DOWN" else position.shares_up
    shares_to_sell = shares_loss * sell_percent
    remaining_loss = shares_loss - shares_to_sell
    cash = shares_to_sell * loss_leg_bid
    additional_shares = additional_capital / win_leg_ask if win_leg_ask > 0 else 0.0
    total_win_shares = shares_win + additional_shares
    total_capital_after_add = total_initial_capital + additional_capital
    expected = total_win_shares + cash - total_capital_after_add
    worst = cash + remaining_loss - total_capital_after_add
    return {
        "action": "SELL_LOSS_AND_ADD_WIN",
        "expected": round(expected, 6),
        "worst_case": round(worst, 6),
        "additional_capital": round(additional_capital, 6),
        "risk_ok": _worst_case_ok(worst, total_initial_capital, cfg),
        "reason": (
            f"sell loss leg {loss_leg} and add {win_leg} "
            f"with {additional_capital:.2f} capital"
        ),
    }


def _position_output(
    market: MarketInput,
    position: PositionInput,
    cfg: AnalyzerConfig,
    distance: float,
    total_price: float,
    gross_edge: float,
    win_leg: str,
    loss_leg: str,
    win_leg_price: float,
    loss_leg_price: float,
    confidence: float,
    action: Action,
    sell_loss_percent: float,
    additional_capital: float,
    expected_profit: float,
    worst_case: float,
    risk_level: str,
    reason: str,
) -> MarketAnalysis:
    total_initial_capital = position.capital_up + position.capital_down
    shares_per_leg = min(position.shares_up, position.shares_down)
    locked_profit = shares_per_leg - total_initial_capital
    locked_roi = locked_profit / total_initial_capital if total_initial_capital > 0 else 0.0
    return MarketAnalysis(
        market_id=market.market_id,
        market_status=market.market_status,
        time_remaining=round(market.time_remaining, 4),
        target_price=market.target_price,
        current_asset_price=market.current_asset_price,
        distance_from_target=round(distance, 6),
        up_ask_price=market.up_ask_price,
        down_ask_price=market.down_ask_price,
        total_price=total_price,
        gross_edge=gross_edge,
        position_exists=True,
        shares_up=round(position.shares_up, 6),
        shares_down=round(position.shares_down, 6),
        total_initial_capital=round(total_initial_capital, 6),
        locked_profit=round(locked_profit, 6),
        locked_roi=round(locked_roi, 6),
        win_leg=win_leg,
        loss_leg=loss_leg,
        win_leg_price=round(win_leg_price, 6),
        loss_leg_price=round(loss_leg_price, 6),
        confidence_score=round(confidence, 2),
        recommended_action=action,
        sell_loss_percent=round(sell_loss_percent, 4),
        additional_capital=round(additional_capital, 6),
        expected_profit_if_correct=round(expected_profit, 6),
        risk_level=risk_level,
        reason=reason,
    )


def _sell_loss_percent(confidence: float) -> float:
    if confidence >= 95.0:
        return 1.0
    if confidence >= 90.0:
        return 0.75
    if confidence >= 85.0:
        return 0.50
    return 0.0


def _worst_case_ok(worst_case: float, total_initial_capital: float, cfg: AnalyzerConfig) -> bool:
    if total_initial_capital <= 0:
        return False
    return worst_case >= -(total_initial_capital * cfg.max_worst_case_loss_ratio)


def _risk_level(worst_case: float, capital: float) -> str:
    if capital <= 0:
        return "HIGH"
    ratio = worst_case / capital
    if ratio >= 0:
        return "LOW"
    if ratio >= -0.35:
        return "MEDIUM"
    return "HIGH"


def _price_feed_stale(market: MarketInput, cfg: AnalyzerConfig) -> bool:
    if market.price_feed_timestamp <= 0:
        return True
    now = market.current_time or time.time()
    return max(0.0, now - market.price_feed_timestamp) > cfg.max_price_feed_age_seconds


def _direction_away_from_target(direction: str, win_leg: str) -> bool:
    text = str(direction or "").strip().lower()
    if text in {"away", "away_from_target", "menjauh"}:
        return True
    if win_leg == "UP":
        return text in {"up", "bull", "bullish", "naik"}
    if win_leg == "DOWN":
        return text in {"down", "bear", "bearish", "turun"}
    return False
