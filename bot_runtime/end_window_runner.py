"""Runtime dispatcher for the end-window directional strategy."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import replace
from typing import Optional

import aiohttp

import core.config as config
import core.market as market_api
import core.safety as safety
import core.state as st
import core.trader as trader
from core.market import BTCMarket
from core.orderbook import top_depth
from strategies import end_window

log = logging.getLogger("end_window")
_last_skip_log: dict[tuple[str, str], float] = {}
DIRECT_BOOK_TIMEOUT_SECS = max(0.05, float(os.getenv("END_WINDOW_DIRECT_BOOK_TIMEOUT_SECS", "0.20")))
DIRECT_BOOK_CACHE_MAX_AGE_SECS = max(0.05, float(os.getenv("END_WINDOW_DIRECT_BOOK_CACHE_MAX_AGE_SECS", "1.0")))
TIME_MIN_DELTA_USD = 3.0
REVERSAL_MAX_PRICE = max(
    0.01,
    float(os.getenv("END_WINDOW_REVERSAL_MAX_PRICE", os.getenv("END_WINDOW_REVERSAL_TRIGGER_PRICE", "0.40"))),
)
MAX_REVERSALS_PER_WINDOW = 2
TIME_DEFAULT_PRICES = {1: 0.98, 2: 0.99, 3: 0.97, 4: 0.96, 5: 0.95, 6: 0.94}


def _log_forced_skip(market_slug: str, reason: str, secs_left: float, cfg: end_window.EndWindowConfig) -> None:
    max_layer_secs = max((layer.seconds_left_max for layer in cfg.layers), default=10.0)
    if secs_left > max_layer_secs:
        log.debug("[END_WINDOW] forced skip %s: %s", market_slug, reason)
        return
    reason_key = str(reason or "").split(":", 1)[0]
    key = (str(market_slug or ""), reason_key)
    now = time.time()
    if now - _last_skip_log.get(key, 0.0) < 1.0:
        return
    _last_skip_log[key] = now
    log.info("[END_WINDOW] forced skip %s: %s", market_slug, reason)


def _log_reverse_skip(market_slug: str, reason: str) -> None:
    reason_key = str(reason or "").split(":", 1)[0]
    key = (str(market_slug or ""), f"reverse_{reason_key}")
    now = time.time()
    if now - _last_skip_log.get(key, 0.0) < 1.0:
        return
    _last_skip_log[key] = now
    log.info("[END_WINDOW] reverse skip %s: %s", market_slug, reason)


def _open_strategy_legs(window_ts: int, market_slug: str) -> list[dict]:
    return [
        t for t in st.load_trades()
        if int(t.get("window_ts") or 0) == int(window_ts)
        and str(t.get("market_slug") or "") == str(market_slug or "")
        and str(t.get("trigger") or "").upper() == "END_WINDOW"
        and not t.get("resolved")
        and not t.get("exited_early")
    ]


def _count_strategy_trades(window_ts: int, market_slug: str) -> int:
    return sum(
        1 for t in st.load_trades()
        if int(t.get("window_ts") or 0) == int(window_ts)
        and str(t.get("market_slug") or "") == str(market_slug or "")
        and str(t.get("trigger") or "").upper() == "END_WINDOW"
    )


def _strategy_trades(window_ts: int, market_slug: str) -> list[dict]:
    return [
        t for t in st.load_trades()
        if int(t.get("window_ts") or 0) == int(window_ts)
        and str(t.get("market_slug") or "") == str(market_slug or "")
        and str(t.get("trigger") or "").upper() == "END_WINDOW"
    ]


def _trade_slot(trade: dict) -> str:
    reason = str(trade.get("trigger_reason") or "").upper()
    if "ARB5-DOWN" in reason:
        return "ARB5-DOWN"
    if "ARB5-UP" in reason:
        return "ARB5-UP"
    if "ARB5" in reason:
        return "ARB5"
    if "ARB15-DOWN" in reason:
        return "ARB15-DOWN"
    if "ARB15-UP" in reason:
        return "ARB15-UP"
    if "ARB15" in reason:
        return "ARB15"
    if "BUY-1" in reason:
        return "BUY-1"
    if "REVERSE-1" in reason:
        return "REVERSE-1"
    if "REVERSE" in reason:
        return "REVERSE"
    if "TIME-6" in reason:
        return "TIME-6"
    if "TIME-5" in reason:
        return "TIME-5"
    if "TIME-4" in reason:
        return "TIME-4"
    if "TIME-3" in reason:
        return "TIME-3"
    if "TIME-1" in reason:
        return "TIME-1"
    if "TIME-2" in reason:
        return "TIME-2"
    return "LAYER"


def _delta_outcome(btc_open: float, btc_now: float, min_delta: float = 0.0) -> str:
    if float(btc_open or 0.0) <= 0 or float(btc_now or 0.0) <= 0:
        return ""
    delta = float(btc_now or 0.0) - float(btc_open or 0.0)
    threshold = max(0.0, float(min_delta or 0.0))
    if delta > 0 and delta >= threshold:
        return "UP"
    if delta < 0 and delta <= -threshold:
        return "DOWN"
    return ""


def _entry_delta_aligned(trade: dict) -> bool:
    outcome = str(trade.get("outcome") or "").upper()
    delta = float(trade.get("btc_distance") or 0.0)
    return (outcome == "UP" and delta > 0) or (outcome == "DOWN" and delta < 0)


def _trade_reference(trade: dict) -> str:
    order_id = str(trade.get("order_id") or "").strip()
    if order_id:
        return f"order-{order_id.replace(' ', '_')}"
    return f"ts-{float(trade.get('timestamp') or 0.0):.6f}"


def _reversed_source_refs(strategy_trades: list[dict]) -> set[str]:
    refs: set[str] = set()
    for trade in strategy_trades:
        if _trade_slot(trade) not in {"REVERSE", "REVERSE-1"}:
            continue
        reason = str(trade.get("trigger_reason") or "")
        marker = "source_ref="
        if marker not in reason:
            refs.add("*")
            continue
        refs.add(reason.split(marker, 1)[1].split()[0].rstrip(";"))
    return refs


def _next_reverse_source(
    open_legs: list[dict],
    current_outcome: str,
    used_slots: set[str],
    reversed_source_refs: set[str] | None = None,
) -> tuple[dict, str] | None:
    if current_outcome not in {"UP", "DOWN"}:
        return None
    reversed_source_refs = reversed_source_refs or set()
    if "*" in reversed_source_refs:
        return None
    if "REVERSE-1" in used_slots:
        return None
    source_slot = "REVERSE" if "REVERSE" in used_slots else ""
    next_slot = "REVERSE-1" if source_slot == "REVERSE" else "REVERSE"
    candidates = [
        trade for trade in open_legs
        if (
            (_trade_slot(trade) == source_slot)
            if source_slot
            else _trade_slot(trade) not in {"REVERSE", "REVERSE-1"}
        )
        and str(trade.get("outcome") or "").upper() != current_outcome
        and _entry_delta_aligned(trade)
        and _trade_reference(trade) not in reversed_source_refs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda trade: float(trade.get("timestamp") or 0.0)), next_slot


def _side_price(market: BTCMarket, btc_open: float, btc_now: float) -> tuple[str, str, str, float, float]:
    if btc_now > btc_open:
        return "YES", "UP", market.up_token, float(market.up_ask or 0.0), float(market.up_spread or 0.0)
    if btc_now < btc_open:
        return "NO", "DOWN", market.down_token, float(market.down_ask or 0.0), float(market.down_spread or 0.0)
    if float(market.up_price or 0.0) > float(market.down_price or 0.0):
        return "YES", "UP", market.up_token, float(market.up_ask or 0.0), float(market.up_spread or 0.0)
    if float(market.down_price or 0.0) > float(market.up_price or 0.0):
        return "NO", "DOWN", market.down_token, float(market.down_ask or 0.0), float(market.down_spread or 0.0)
    if float(market.up_ask or 0.0) > 0 and (
        float(market.down_ask or 0.0) <= 0 or float(market.up_ask or 0.0) <= float(market.down_ask or 0.0)
    ):
        return "YES", "UP", market.up_token, float(market.up_ask or 0.0), float(market.up_spread or 0.0)
    if float(market.down_ask or 0.0) > 0:
        return "NO", "DOWN", market.down_token, float(market.down_ask or 0.0), float(market.down_spread or 0.0)
    return "", "", "", 0.0, 0.0


def _opposite_ask(market: BTCMarket, outcome: str) -> float:
    if outcome == "UP":
        return float(market.down_ask or 0.0)
    if outcome == "DOWN":
        return float(market.up_ask or 0.0)
    return 0.0


def _selected_book_age(market: BTCMarket, outcome: str) -> float:
    token_id = market.up_token if outcome == "UP" else market.down_token
    if not token_id:
        return 999999.0
    try:
        from core.ws_feed import get_cache
        return float(get_cache().clob_age(token_id))
    except Exception:
        return 999999.0


def _selected_ask_depth(market: BTCMarket, outcome: str) -> list:
    if outcome == "UP":
        return list(getattr(market, "up_ask_depth", []) or [])
    if outcome == "DOWN":
        return list(getattr(market, "down_ask_depth", []) or [])
    return []


def _ask_capacity_usd(market: BTCMarket, outcome: str, max_price: float) -> float:
    total = 0.0
    for row in _selected_ask_depth(market, outcome):
        try:
            price = float(row[0])
            size = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0 or size <= 0 or price > float(max_price or 0.0):
            continue
        total += price * size
    return round(total, 4)


def _market_interval_secs(market: BTCMarket) -> int:
    return max(0, int(getattr(market, "close_ts", 0) or 0) - int(getattr(market, "window_ts", 0) or 0))


def _time_candidate(
    market: BTCMarket,
    target_price: float,
    amount_usd: float,
    required_outcome: str,
) -> tuple[str, str, str, float, float] | None:
    candidates = []
    for side, outcome, token, ask, spread in (
        ("YES", "UP", market.up_token, float(market.up_ask or 0.0), float(market.up_spread or 0.0)),
        ("NO", "DOWN", market.down_token, float(market.down_ask or 0.0), float(market.down_spread or 0.0)),
    ):
        if outcome != required_outcome:
            continue
        if abs(ask - target_price) > 1e-9:
            continue
        capacity = _ask_capacity_usd(market, outcome, target_price)
        if capacity + 1e-9 >= float(amount_usd or 0.0):
            candidates.append((side, outcome, token, ask, spread, capacity))
    if not candidates:
        return None
    side, outcome, token, ask, spread, _capacity = max(
        candidates,
        key=lambda row: (row[3], row[5], row[1]),
    )
    return side, outcome, token, ask, spread


def _buy1_candidate(
    market: BTCMarket,
    btc_open: float,
    btc_now: float,
    secs_left: float,
    bankroll_usd: float,
    settings: st.BotSettings,
    cfg: end_window.EndWindowConfig,
    used_slots: set[str],
    strategy_trades: list[dict],
) -> tuple[str, str, str, float, float, float] | None:
    if not bool(getattr(settings, "buy1_enabled", True)):
        return None
    if "BUY-1" in used_slots:
        return None
    max_open = max(1, int(getattr(settings, "buy1_max_open_positions", 1) or 1))
    open_buy1 = [
        trade for trade in strategy_trades
        if _trade_slot(trade) == "BUY-1"
        and not trade.get("resolved")
        and not trade.get("exited_early")
    ]
    if len(open_buy1) >= max_open:
        return None
    min_secs = float(getattr(settings, "buy1_min_secs_left", 20.0) or 0.0)
    max_secs = float(getattr(settings, "buy1_max_secs_left", 260.0) or 0.0)
    if not (min_secs < float(secs_left or 0.0) <= max_secs):
        return None
    outcome = _delta_outcome(
        btc_open,
        btc_now,
        float(getattr(settings, "buy1_min_delta_usd", 8.0) or 0.0),
    )
    if not outcome:
        return None
    side, current_outcome, token, ask, spread = _side_price(market, btc_open, btc_now)
    if current_outcome != outcome or ask <= 0:
        return None
    min_price = float(getattr(settings, "buy1_min_price", 0.50) or 0.50)
    max_price = float(getattr(settings, "buy1_max_price", 0.60) or 0.60)
    if not (min_price <= ask <= max_price):
        return None
    requested_amount = float(getattr(settings, "buy1_trade_usd", 25.0) or 0.0)
    if not config.MOCK_MODE:
        requested_amount = min(requested_amount, float(cfg.live_trade_usd or requested_amount))
    amount = min(requested_amount, float(bankroll_usd or 0.0))
    if amount + 1e-9 < requested_amount:
        return None
    capacity = _ask_capacity_usd(market, outcome, ask)
    if capacity + 1e-9 < amount:
        return None
    return side, outcome, token, ask, spread, amount


def _buy1_open_legs(window_ts: int, market_slug: str) -> list[dict]:
    return [
        trade for trade in _open_strategy_legs(window_ts, market_slug)
        if _trade_slot(trade) == "BUY-1"
    ]


def _side_bid(market: BTCMarket, outcome: str) -> tuple[str, str, float]:
    if outcome == "UP":
        return market.up_token, "UP", float(market.up_price or 0.0)
    if outcome == "DOWN":
        return market.down_token, "DOWN", float(market.down_price or 0.0)
    return "", "", 0.0


def _time_screening_rules(settings: st.BotSettings, time_enabled: dict[int, bool]) -> list[tuple]:
    rules = [
        (
            f"TIME-{index}",
            time_enabled[index],
            float(getattr(settings, f"time{index}_price", TIME_DEFAULT_PRICES[index])),
            float(getattr(settings, f"time{index}_trade_usd", 100.0)),
            float(getattr(settings, f"time{index}_min_secs_left", 3.0)),
            float(getattr(settings, f"time{index}_max_secs_left", 299.0)),
        )
        for index in range(1, 7)
    ]
    return sorted(rules, key=lambda rule: (rule[2], int(rule[0].split("-")[1])))


def _mock_fok_fill(
    market: BTCMarket,
    outcome: str,
    amount_usd: float,
    max_price: float,
    skip_usd: float = 0.0,
) -> tuple[float, float] | None:
    remaining = float(amount_usd or 0.0)
    skip_remaining = max(0.0, float(skip_usd or 0.0))
    total_shares = 0.0
    total_cost = 0.0
    for row in _selected_ask_depth(market, outcome):
        try:
            price = float(row[0])
            available_shares = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0 or available_shares <= 0 or price > float(max_price or 0.0):
            continue
        level_cost = price * available_shares
        skipped_cost = min(skip_remaining, level_cost)
        skip_remaining -= skipped_cost
        level_cost -= skipped_cost
        if level_cost <= 0:
            continue
        used_cost = min(remaining, level_cost)
        total_cost += used_cost
        total_shares += used_cost / price
        remaining -= used_cost
        if remaining <= 1e-9:
            break
    if remaining > 1e-9 or total_shares <= 0:
        return None
    average_price = total_cost / total_shares
    fee_pct = float(config.MOCK_FEE_RATE or 0.0) * average_price * (1.0 - average_price)
    filled_shares = total_shares * max(0.0, 1.0 - fee_pct)
    return round(average_price, 4), round(filled_shares, 2)


async def _refresh_side_book(
    session: aiohttp.ClientSession | None,
    market: BTCMarket,
    outcome: str,
) -> BTCMarket:
    if session is None:
        return market
    token_id = market.up_token if outcome == "UP" else market.down_token
    if not token_id:
        return market
    try:
        url = f"{market_api.get_active_clob_api()}/book"
        async with session.get(
            url,
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=DIRECT_BOOK_TIMEOUT_SECS),
        ) as resp:
            if resp.status != 200:
                log.debug("[END_WINDOW] direct book refresh %s HTTP %s", outcome, resp.status)
                return market
            book = await resp.json()
    except Exception as exc:
        log.debug("[END_WINDOW] direct book refresh %s failed: %s", outcome, exc)
        return market

    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_depth = top_depth(bids, side="bid")
    ask_depth = top_depth(asks, side="ask")
    best_bid = bid_depth[0][0] if bid_depth else 0.0
    best_ask = ask_depth[0][0] if ask_depth else 0.0
    if outcome == "UP":
        market.up_price = best_bid
        market.up_ask = best_ask
        market.up_bid_depth = bid_depth
        market.up_ask_depth = ask_depth
    else:
        market.down_price = best_bid
        market.down_ask = best_ask
        market.down_bid_depth = bid_depth
        market.down_ask_depth = ask_depth
    market.book_ts = time.time()
    return market


async def _execute_buy(
    *,
    market: BTCMarket,
    side: str,
    outcome: str,
    token: str,
    price: float,
    amount_usd: float,
    reason: str,
    spread: float,
    secs_elapsed: float,
    secs_left: float,
    btc_open: float,
    btc_now: float,
    strict_price: bool = True,
    skip_preflight: bool = False,
    mock_fill: tuple[float, float] | None = None,
    ignore_slippage: bool = False,
) -> Optional[st.TradeRecord]:
    spend = round(float(amount_usd or 0.0), 2)
    if spend <= 0:
        log.info("[END_WINDOW] skip: zero spend")
        return None
    if st.get_balance() < spend:
        log.info("[END_WINDOW] skip: balance $%.2f < $%.2f", st.get_balance(), spend)
        return None

    st.deduct_balance(spend)
    try:
        result = await trader.execute_buy(
            token,
            outcome,
            float(price),
            spend,
            market.condition_id,
            strict_price=strict_price,
            allow_partial=False,
            skip_preflight=skip_preflight,
            mock_fill=mock_fill,
            ignore_slippage=ignore_slippage,
        )
    except Exception as exc:
        st.add_balance(spend)
        log.error("[END_WINDOW] %s buy exception: %s", outcome, exc)
        return None

    fill_status = str(getattr(result, "fill_status", "") or "")
    filled_size = float(getattr(result, "size", 0.0) or getattr(result, "size_matched", 0.0) or 0.0)
    positive_failed_fill = (not result.success) and filled_size > 0
    if not result.success and not positive_failed_fill:
        st.add_balance(spend)
        log.warning("[END_WINDOW] %s buy failed: %s", outcome, getattr(result, "error", ""))
        return None

    fill_price = float(getattr(result, "price", 0.0) or price)
    is_partial = fill_status in {"partial", "fak_partial_unsellable", "partial_unsellable"} or positive_failed_fill
    actual_used = spend
    if is_partial and filled_size > 0 and fill_price > 0:
        actual_used = min(round(filled_size * fill_price / config.FEE_MULTIPLIER, 4), spend)
        refund = round(spend - actual_used, 4)
        if refund > 0:
            st.add_balance(refund)

    rec = st.TradeRecord(
        timestamp=time.time(),
        window_ts=int(market.window_ts),
        asset=str(getattr(market, "asset", "BTC") or "BTC").upper(),
        market_slug=str(getattr(market, "slug", "") or ""),
        condition_id=str(market.condition_id or ""),
        outcome=outcome,
        entry_price=fill_price,
        shares=filled_size,
        amount_usd=actual_used,
        btc_open=float(btc_open or 0.0),
        btc_at_entry=float(btc_now or 0.0),
        secs_elapsed=float(secs_elapsed or 0.0),
        secs_left=float(secs_left or 0.0),
        btc_distance=float(btc_now or 0.0) - float(btc_open or 0.0),
        trigger="END_WINDOW",
        trigger_reason=reason,
        order_id=str(getattr(result, "order_id", "") or ""),
        mock=bool(getattr(result, "mock", False)),
        spread=float(spread or 0.0),
        liquidity=0.0,
        confidence=0.0,
        partial_fill=is_partial,
        failed_fill=is_partial,
        one_side_exposure=True,
    )
    st.save_trade(rec)
    log.info(
        "[END_WINDOW] FIRE %s @ %.4f $%.2f %s window=%d",
        outcome,
        rec.entry_price,
        rec.amount_usd,
        side,
        rec.window_ts,
    )
    return rec


async def try_buy1_exits(
    *,
    market: BTCMarket,
    secs_left: float,
    session: aiohttp.ClientSession | None = None,
    settings: st.BotSettings | None = None,
) -> list[dict]:
    settings = settings or st.load_settings()
    market_slug = str(getattr(market, "slug", "") or "")
    legs = _buy1_open_legs(int(market.window_ts), market_slug)
    if not legs:
        return []
    sell_min = float(getattr(settings, "buy1_sell_min_price", 0.80) or 0.80)
    sell_max = float(getattr(settings, "buy1_sell_max_price", 0.90) or 0.90)
    resolved: list[dict] = []
    for trade in legs:
        outcome = str(trade.get("outcome") or "").upper()
        token, _, bid = _side_bid(market, outcome)
        if bid <= 0 or _selected_book_age(market, outcome) > DIRECT_BOOK_CACHE_MAX_AGE_SECS:
            market = await _refresh_side_book(session, market, outcome)
            token, _, bid = _side_bid(market, outcome)
        if not token or bid < sell_min:
            continue
        sell_price = min(max(bid, sell_min), sell_max)
        shares = float(trade.get("shares") or 0.0)
        if shares <= 0:
            continue
        try:
            result = await trader.execute_sell(
                token,
                outcome,
                sell_price,
                shares,
                str(getattr(market, "condition_id", "") or ""),
            )
        except Exception as exc:
            log.warning("[BUY-1] sell exception %s %s: %s", market_slug, outcome, exc)
            continue
        filled_size = float(
            getattr(result, "size", 0.0)
            or getattr(result, "size_matched", 0.0)
            or 0.0
        )
        if not getattr(result, "success", False):
            log.warning("[BUY-1] sell failed %s %s: %s", market_slug, outcome, getattr(result, "error", ""))
            continue
        if filled_size + 1e-6 < shares:
            log.warning(
                "[BUY-1] partial sell not ledger-resolved %s %s %.2f/%.2f",
                market_slug,
                outcome,
                filled_size,
                shares,
            )
            continue
        exit_price = float(getattr(result, "price", 0.0) or sell_price)
        closed = st.resolve_specific_leg(
            int(market.window_ts),
            outcome,
            exit_price,
            f"BUY-1 quick sell bid={bid:.4f} target={sell_min:.2f}-{sell_max:.2f}",
            secs_left=float(secs_left or 0.0),
            market_slug=market_slug,
            order_id=str(trade.get("order_id") or ""),
        )
        if not closed:
            continue
        st.add_balance(float(closed.get("balance_returned") or 0.0))
        st.update_daily_pnl(float(closed.get("pnl") or 0.0), closed.get("won"))
        resolved.append(closed)
        log.info(
            "[BUY-1] SOLD %s @ %.4f pnl=$%.4f window=%d",
            outcome,
            exit_price,
            float(closed.get("pnl") or 0.0),
            int(market.window_ts),
        )
    return resolved


async def try_end_window_market(
    *,
    market: BTCMarket,
    btc_open: float,
    btc_now: float,
    secs_elapsed: float,
    secs_left: float,
    bankroll_usd: float,
    session: aiohttp.ClientSession | None = None,
    cfg: end_window.EndWindowConfig | None = None,
    settings: st.BotSettings | None = None,
    book_reserved_usd: float = 0.0,
    reserved_slots: set[str] | None = None,
    reversed_source_refs: set[str] | None = None,
) -> Optional[st.TradeRecord]:
    settings = settings or st.load_settings()
    cfg = cfg or end_window.EndWindowConfig.from_settings(settings)
    if not cfg.enabled:
        return None
    enabled_layers = tuple(
        layer for layer in cfg.layers
        if bool(getattr(settings, f"{layer.name.lower()}_enabled", True))
    )
    time_enabled = {
        index: bool(getattr(settings, f"time{index}_enabled", True))
        for index in range(1, 7)
    }
    if enabled_layers != cfg.layers:
        cfg = replace(cfg, layers=enabled_layers)
    if btc_now <= 0:
        return None

    market_slug = str(getattr(market, "slug", "") or "")
    if _market_interval_secs(market) == 900:
        return None

    strategy_trades = _strategy_trades(int(market.window_ts), market_slug)
    open_legs = [
        trade for trade in strategy_trades
        if not trade.get("resolved") and not trade.get("exited_early")
    ]
    trade_count = len(strategy_trades)
    if trade_count >= max(1, cfg.max_trades_per_window):
        return None
    slot_mode = cfg.max_trades_per_window >= 3
    used_slots = {_trade_slot(trade) for trade in strategy_trades}
    used_slots.update(reserved_slots or set())
    delta_outcome = _delta_outcome(btc_open, btc_now)
    reversed_refs = _reversed_source_refs(strategy_trades)
    reversed_refs.update(reversed_source_refs or set())
    reverse_count = sum(1 for trade in strategy_trades if _trade_slot(trade) in {"REVERSE", "REVERSE-1"})
    reverse_candidate = _next_reverse_source(
        open_legs,
        delta_outcome,
        used_slots,
        reversed_refs,
    )
    if reverse_count >= MAX_REVERSALS_PER_WINDOW:
        reverse_candidate = None
    reverse_source, reverse_slot = reverse_candidate if reverse_candidate else (None, "")
    reversal = reverse_source is not None
    reversal_layer = _trade_slot(reverse_source) if reverse_source else ""
    ignore_slippage = False

    if reversal:
        side, outcome, token, price, spread = _side_price(market, btc_open, btc_now)
        if outcome != delta_outcome:
            _log_reverse_skip(
                market_slug,
                f"delta_changed: expected={delta_outcome or 'NONE'} current={outcome or 'NONE'}",
            )
            return None
        if (
            (not config.MOCK_MODE and book_reserved_usd > 0)
            or
            _selected_book_age(market, outcome) > DIRECT_BOOK_CACHE_MAX_AGE_SECS
            or not _selected_ask_depth(market, outcome)
        ):
            market = await _refresh_side_book(session, market, outcome)
            side, outcome, token, price, spread = _side_price(market, btc_open, btc_now)
        if outcome != delta_outcome:
            _log_reverse_skip(
                market_slug,
                f"delta_changed_after_refresh: expected={delta_outcome or 'NONE'} current={outcome or 'NONE'}",
            )
            return None
        if price <= 0:
            _log_reverse_skip(market_slug, f"ask_unavailable: outcome={outcome}")
            return None
        if price > REVERSAL_MAX_PRICE:
            _log_reverse_skip(
                market_slug,
                f"ask_above_cap: outcome={outcome} ask={price:.4f} cap={REVERSAL_MAX_PRICE:.4f}",
            )
            return None
        decision = end_window.EndWindowDecision(
            True,
            side=side,
            price=price,
            layer=reverse_slot,
            reason=(
                f"{reverse_slot}: initial={reverse_source.get('outcome')} "
                f"source={reversal_layer} "
                f"source_ref={_trade_reference(reverse_source)} "
                f"entry_delta={float(reverse_source.get('btc_distance') or 0.0):+.2f} "
                f"current_delta={float(btc_now or 0.0) - float(btc_open or 0.0):+.2f} "
                f"opposite_ask={price:.4f} cap={REVERSAL_MAX_PRICE:.4f}"
            ),
        )
    else:
        if (
            not enabled_layers
            and not any(time_enabled.values())
            and not bool(getattr(settings, "buy1_enabled", True))
        ):
            return None

    buy1 = None
    if not reversal:
        buy1 = _buy1_candidate(
            market,
            btc_open,
            btc_now,
            secs_left,
            bankroll_usd,
            settings,
            cfg,
            used_slots,
            strategy_trades,
        )
        if buy1:
            side, outcome, token, price, spread, amount = buy1
            if (
                _selected_book_age(market, outcome) > DIRECT_BOOK_CACHE_MAX_AGE_SECS
                or not _selected_ask_depth(market, outcome)
            ):
                market = await _refresh_side_book(session, market, outcome)
                buy1 = _buy1_candidate(
                    market,
                    btc_open,
                    btc_now,
                    secs_left,
                    bankroll_usd,
                    settings,
                    cfg,
                    used_slots,
                    strategy_trades,
                )
                if not buy1:
                    return None
                side, outcome, token, price, spread, amount = buy1
            if not config.MOCK_MODE:
                gate = safety.validate_market_for_entry(
                    market,
                    [outcome],
                    {outcome: amount},
                    live=True,
                    existing_window_exposure_usd=sum(
                        float(trade.get("amount_usd") or 0.0)
                        for trade in strategy_trades
                    ),
                )
                if not gate.ok:
                    log.warning("[BUY-1] live safety skip %s %s: %s", market_slug, outcome, gate.reason)
                    return None
            mock_fill = (
                _mock_fok_fill(market, outcome, amount, price)
                if config.MOCK_MODE
                else None
            )
            if config.MOCK_MODE and mock_fill is None:
                _log_forced_skip(market_slug, "BUY-1 mock_fok_unfilled", secs_left, cfg)
                return None
            reason = (
                f"BUY-1 {outcome}: quick buy ask={price:.4f} "
                f"buy_range={float(getattr(settings, 'buy1_min_price', 0.50)):.2f}-"
                f"{float(getattr(settings, 'buy1_max_price', 0.60)):.2f} "
                f"sell_target={float(getattr(settings, 'buy1_sell_min_price', 0.80)):.2f}-"
                f"{float(getattr(settings, 'buy1_sell_max_price', 0.90)):.2f} "
                f"delta={float(btc_now or 0.0) - float(btc_open or 0.0):+.2f} "
                f"target={btc_open:.2f} btc_now={btc_now:.2f}"
            )
            return await _execute_buy(
                market=market,
                side=side,
                outcome=outcome,
                token=token,
                price=price,
                amount_usd=amount,
                reason=reason,
                spread=spread,
                secs_elapsed=secs_elapsed,
                secs_left=secs_left,
                btc_open=btc_open,
                btc_now=btc_now,
                strict_price=True,
                mock_fill=mock_fill,
            )
    time_trigger = ""
    time_price = 0.0
    time_amount = 0.0
    time_min_secs_left = 0.0
    time_candidate = None
    time_outcome = _delta_outcome(btc_open, btc_now, TIME_MIN_DELTA_USD)
    if not reversal and time_outcome and (slot_mode or not open_legs):
        for trigger, enabled, target_price, requested_amount, min_secs_left, max_secs_left in _time_screening_rules(
            settings,
            time_enabled,
        ):
            if secs_left <= float(min_secs_left) or secs_left > float(max_secs_left):
                continue
            if not config.MOCK_MODE:
                requested_amount = min(float(requested_amount or 0.0), cfg.live_trade_usd)
            available_amount = min(requested_amount, float(bankroll_usd or 0.0))
            if not enabled or trigger in used_slots or available_amount < requested_amount:
                continue
            candidate = _time_candidate(market, target_price, available_amount, time_outcome)
            if candidate:
                time_trigger = trigger
                time_price = target_price
                time_amount = available_amount
                time_min_secs_left = float(min_secs_left)
                time_candidate = candidate
                break

    if reversal:
        pass
    elif time_candidate:
        side, outcome, token, observed_ask, spread = time_candidate
        if (
            (not config.MOCK_MODE and book_reserved_usd > 0)
            or
            _selected_book_age(market, outcome) > DIRECT_BOOK_CACHE_MAX_AGE_SECS
            or not _selected_ask_depth(market, outcome)
        ):
            market = await _refresh_side_book(session, market, outcome)
            time_candidate = _time_candidate(market, time_price, time_amount, time_outcome)
            if not time_candidate:
                return None
            side, outcome, token, observed_ask, spread = time_candidate
        price = time_price
    else:
        if slot_mode and "LAYER" in used_slots:
            return None
        if btc_open <= 0:
            return None
        side, outcome, token, price, spread = _side_price(market, btc_open, btc_now)
    max_layer_secs = max((layer.seconds_left_max for layer in cfg.layers), default=10.0)
    if (
        outcome
        and secs_left <= max_layer_secs + 1.0
        and (
            _selected_book_age(market, outcome) > DIRECT_BOOK_CACHE_MAX_AGE_SECS
            or not _selected_ask_depth(market, outcome)
        )
    ):
        market = await _refresh_side_book(session, market, outcome)
        side, outcome, token, price, spread = _side_price(market, btc_open, btc_now)

    if reversal:
        pass
    elif time_candidate:
        decision = end_window.EndWindowDecision(
            True,
            side=side,
            price=time_price,
            layer=time_trigger,
            reason=(
                f"{time_trigger}: ask={observed_ask:.4f} exact={time_price:.2f} "
                f"liquidity=${_ask_capacity_usd(market, outcome, time_price):.2f}"
            ),
        )
    else:
        decision = end_window.evaluate_entry(
            seconds_left=secs_left,
            distance_usd=abs(btc_now - btc_open),
            price=price,
            side=side,
            cfg=cfg,
            spread=spread,
            opposite_price=_opposite_ask(market, outcome),
        )
        if not decision.ok:
            _log_forced_skip(market_slug, decision.reason, secs_left, cfg)
            return None

    requested_amount = time_amount if time_candidate else cfg.reversal_trade_usd if reversal else cfg.trade_usd
    if not config.MOCK_MODE and not reversal:
        requested_amount = min(float(requested_amount or 0.0), cfg.live_trade_usd)
    amount = min(float(requested_amount or 0.0), float(bankroll_usd or 0.0))
    min_trade_usd = (
        cfg.reversal_trade_usd
        if reversal
        else cfg.min_trade_usd
        if config.MOCK_MODE
        else cfg.live_trade_usd
    )
    if amount < max(0.0, float(min_trade_usd or 0.0)):
        log.info("[END_WINDOW] skip: amount $%.2f below min $%.2f", amount, min_trade_usd)
        return None

    if not config.MOCK_MODE:
        existing_exposure = sum(float(trade.get("amount_usd") or 0.0) for trade in strategy_trades)
        gate = safety.validate_market_for_entry(
            market,
            [outcome],
            {outcome: amount},
            live=True,
            existing_window_exposure_usd=existing_exposure,
        )
        if not gate.ok:
            log.warning("[END_WINDOW] live safety skip %s %s: %s", market_slug, outcome, gate.reason)
            return None

    reason = (
        f"END_WINDOW {outcome} {decision.reason}; "
        f"target={btc_open:.2f} btc_now={btc_now:.2f}"
    )
    min_allowed_secs = (
        time_min_secs_left
        if time_candidate
        else 0.0
        if reversal
        else min((layer.seconds_left_min for layer in cfg.layers), default=0.0)
    )
    attempts = 1 if time_candidate or reversal else max(1, int(cfg.force_retry_attempts if cfg.force_trade else 1))
    delay = max(0.0, float(cfg.force_retry_delay_secs or 0.0))
    for attempt in range(attempts):
        remaining = max(0.0, float(getattr(market, "close_ts", 0) or 0) - time.time())
        if (time_candidate and remaining <= time_min_secs_left) or (
            not time_candidate and remaining < min_allowed_secs
        ):
            log.info("[END_WINDOW] retry stop %s: below minimum trade time %.1fs", market_slug, min_allowed_secs)
            break
        if attempt > 0 and remaining <= 0.25:
            break
        if attempt > 0:
            market = await _refresh_side_book(session, market, outcome)
            side, outcome, token, price, spread = _side_price(market, btc_open, btc_now)
            retry_decision = end_window.evaluate_entry(
                seconds_left=max(0.0, remaining),
                distance_usd=abs(btc_now - btc_open),
                price=price,
                side=side,
                cfg=cfg,
                spread=spread,
                opposite_price=_opposite_ask(market, outcome),
            )
            if retry_decision.ok:
                decision = retry_decision
                reason = (
                    f"END_WINDOW {outcome} {decision.reason}; "
                    f"target={btc_open:.2f} btc_now={btc_now:.2f}"
                )
            else:
                _log_forced_skip(market_slug, retry_decision.reason, max(0.0, remaining), cfg)
                break

        final_attempt = not time_candidate and not reversal and cfg.force_trade and attempt == attempts - 1
        trade_price = float(decision.price or price or 0.0)
        strict_price = True
        skip_preflight = False
        attempt_reason = reason
        if final_attempt:
            layer_max_price = next(
                (
                    float(layer.max_price)
                    for layer in cfg.layers
                    if layer.name == decision.layer
                ),
                0.99,
            )
            trade_price = min(
                max(float(cfg.force_final_price_cap or 0.99), trade_price),
                layer_max_price,
                0.99,
            )
            strict_price = True
            skip_preflight = bool(config.MOCK_MODE)
            attempt_reason = f"{reason}; FORCE_FINAL_FOK cap={trade_price:.2f}"
        reserved_capacity = float(book_reserved_usd or 0.0) if config.MOCK_MODE else 0.0
        capacity = max(0.0, _ask_capacity_usd(market, outcome, trade_price) - reserved_capacity)
        if capacity + 1e-9 < amount:
            skip_reason = f"insufficient_ask_depth: available=${capacity:.2f} required=${amount:.2f}"
            if reversal:
                _log_reverse_skip(market_slug, skip_reason)
            else:
                _log_forced_skip(market_slug, skip_reason, max(0.0, remaining), cfg)
            break
        mock_fill = (
            _mock_fok_fill(
                market,
                outcome,
                amount,
                trade_price,
                skip_usd=reserved_capacity,
            )
            if config.MOCK_MODE
            else None
        )
        if config.MOCK_MODE and mock_fill is None:
            skip_reason = "mock_fok_unfilled: depth changed before execution"
            if reversal:
                _log_reverse_skip(market_slug, skip_reason)
            else:
                _log_forced_skip(market_slug, skip_reason, max(0.0, remaining), cfg)
            break

        rec = await _execute_buy(
            market=market,
            side=side,
            outcome=outcome,
            token=token,
            price=trade_price,
            amount_usd=amount,
            reason=attempt_reason,
            spread=spread,
            secs_elapsed=secs_elapsed,
            secs_left=max(0.0, remaining) if remaining > 0 else secs_left,
            btc_open=btc_open,
            btc_now=btc_now,
            strict_price=strict_price,
            skip_preflight=skip_preflight,
            mock_fill=mock_fill,
            ignore_slippage=ignore_slippage,
        )
        if rec is not None:
            return rec
        if attempt < attempts - 1 and delay > 0:
            await asyncio.sleep(delay)
    return None


async def try_all_end_window(
    *,
    market: BTCMarket,
    btc_open: float,
    btc_now: float,
    secs_elapsed: float,
    secs_left: float,
    bankroll_usd: float,
    session: aiohttp.ClientSession | None = None,
    cfg: end_window.EndWindowConfig | None = None,
    settings: st.BotSettings | None = None,
) -> list[st.TradeRecord]:
    records: list[st.TradeRecord] = []
    remaining_bankroll = float(bankroll_usd or 0.0)
    reserved_by_outcome: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}
    reserved_slots: set[str] = set()
    reversed_source_refs: set[str] = set()
    active_cfg = cfg or end_window.EndWindowConfig.from_settings(settings or st.load_settings())
    max_attempts = max(1, int(active_cfg.max_trades_per_window))
    for _ in range(max_attempts):
        current_outcome = _delta_outcome(btc_open, btc_now)
        rec = await try_end_window_market(
            market=market,
            btc_open=btc_open,
            btc_now=btc_now,
            secs_elapsed=secs_elapsed,
            secs_left=secs_left,
            bankroll_usd=remaining_bankroll,
            session=session,
            cfg=active_cfg,
            settings=settings,
            book_reserved_usd=reserved_by_outcome.get(current_outcome, 0.0),
            reserved_slots=reserved_slots,
            reversed_source_refs=reversed_source_refs,
        )
        if rec is None:
            break
        records.append(rec)
        remaining_bankroll = max(0.0, remaining_bankroll - float(rec.amount_usd or 0.0))
        outcome = str(rec.outcome or "").upper()
        if outcome in reserved_by_outcome:
            reserved_by_outcome[outcome] += float(rec.amount_usd or 0.0)
        slot = _trade_slot({"trigger_reason": rec.trigger_reason})
        if slot in {"REVERSE", "REVERSE-1"}:
            marker = "source_ref="
            if marker in str(rec.trigger_reason or ""):
                reversed_source_refs.add(
                    str(rec.trigger_reason).split(marker, 1)[1].split()[0].rstrip(";")
                )
            reserved_slots.add(slot)
        else:
            reserved_slots.add(slot)
    return records


__all__ = ["try_all_end_window", "try_buy1_exits", "try_end_window_market"]
