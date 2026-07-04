import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot_runtime import end_window_runner
from bot_runtime.app import Bot
from core.market import BTCMarket
from strategies import end_window


def _utc_ts(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc).timestamp()


def _bangkok_date(ts: float | None = None) -> str:
    if ts is None:
        return "2026-06-28"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(
        timezone(timedelta(hours=7))
    ).strftime("%Y-%m-%d")


def _market(**overrides):
    data = dict(
        slug="btc-updown-5m-1800000000",
        window_ts=1800000000,
        close_ts=int(time.time()) + 8,
        condition_id="condition",
        up_token="up_token_1234567890",
        down_token="down_token_1234567890",
        up_price=0.88,
        down_price=0.10,
        up_ask=0.90,
        down_ask=0.11,
        up_ask_depth=[(0.90, 200.0)],
        down_ask_depth=[(0.11, 1000.0)],
        question="Bitcoin Up or Down",
        end_date="",
    )
    data.update(overrides)
    return BTCMarket(**data)


def _layer_settings():
    return SimpleNamespace(
        time1_enabled=False,
        time2_enabled=False,
        time3_enabled=False,
        t1_enabled=True,
        t2_enabled=True,
        t3_enabled=True,
        t4_enabled=True,
        t5_enabled=True,
        t6_enabled=True,
    )


def test_end_window_fires_with_requested_layer():
    market = _market()
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=190.0,
            secs_elapsed=275.0,
            secs_left=25.0,
            bankroll_usd=100.0,
            cfg=cfg,
            settings=_layer_settings(),
        ))

    assert out is fake_record
    buy.assert_awaited_once()
    assert buy.await_args.kwargs["outcome"] == "UP"
    assert buy.await_args.kwargs["price"] == 0.90
    assert buy.await_args.kwargs["amount_usd"] == 100.0
    assert " T1:" in buy.await_args.kwargs["reason"]


def test_end_window_fires_down_when_btc_below_target():
    market = _market(up_ask=0.11, down_ask=0.90, up_price=0.10, down_price=0.88)
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=190.0,
            btc_now=100.0,
            secs_elapsed=275.0,
            secs_left=25.0,
            bankroll_usd=100.0,
            cfg=cfg,
            settings=_layer_settings(),
        ))

    assert out is fake_record
    buy.assert_awaited_once()
    assert buy.await_args.kwargs["outcome"] == "DOWN"
    assert buy.await_args.kwargs["price"] == 0.90
    assert " T1:" in buy.await_args.kwargs["reason"]


def test_end_window_final_retry_never_exceeds_layer_price_cap():
    market = _market(up_ask=0.90, up_ask_depth=[(0.90, 200.0)])
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        force_trade=True,
        force_retry_attempts=1,
        force_final_price_cap=0.98,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=170.0,
            secs_elapsed=275.0,
            secs_left=25.0,
            bankroll_usd=100.0,
            cfg=cfg,
            settings=_layer_settings(),
        ))

    assert out is fake_record
    assert buy.await_args.kwargs["price"] == 0.90


@pytest.mark.parametrize(
    "source_reason",
    [
        "END_WINDOW UP TIME-1: exact",
        "END_WINDOW UP TIME-2: exact",
        "END_WINDOW UP TIME-3: exact",
        "END_WINDOW UP T1: configurable",
        "END_WINDOW UP T2: configurable",
        "END_WINDOW UP T3: configurable",
        "END_WINDOW UP T4: configurable",
        "END_WINDOW UP T5: configurable",
        "END_WINDOW UP T6: configurable",
    ],
)
def test_removed_reverse_hedge_does_not_buy_opposite_side_for_existing_strategy(source_reason):
    market = _market(
        up_ask=0.91,
        down_ask=0.40,
        down_ask_depth=[(0.40, 1000.0)],
        close_ts=int(time.time()) + 12,
    )
    initial = {
        "timestamp": 1.0,
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "outcome": "UP",
        "entry_price": 0.97,
        "btc_distance": 20.0,
        "trigger_reason": source_reason,
        "resolved": False,
        "exited_early": False,
    }
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        max_trades_per_window=4,

    )
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=90.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_removed_reverse_hedge_does_not_use_extra_slot_after_normal_trade_cap():
    market = _market(
        up_ask=0.91,
        down_ask=0.40,
        down_ask_depth=[(0.40, 2000.0)],
        close_ts=int(time.time()) + 4,
    )
    initial = {
        "timestamp": 1.0,
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "outcome": "UP",
        "entry_price": 0.97,
        "btc_distance": 20.0,
        "trigger_reason": "END_WINDOW UP TIME-3: exact",
        "resolved": False,
        "exited_early": False,
    }
    normal_trades = [
        dict(initial, timestamp=float(index), trigger_reason=f"END_WINDOW UP TIME-{index}: exact")
        for index in range(1, 5)
    ]
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        max_trades_per_window=9,

    )
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=normal_trades),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=90.0,
            secs_elapsed=297.0,
            secs_left=3.0,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_end_window_does_not_buy_reverse_1_until_direction_flips():
    market = _market(
        up_ask=0.91,
        down_ask=0.10,
        down_ask_depth=[(0.10, 100.0)],
    )
    initial = {
        "timestamp": 1.0,
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "outcome": "UP",
        "entry_price": 0.97,
        "btc_distance": 20.0,
        "trigger_reason": "END_WINDOW UP TIME-3: exact",
        "resolved": False,
        "exited_early": False,
    }
    reverse = dict(
        initial,
        timestamp=2.0,
        outcome="DOWN",
        btc_distance=-10.0,
        trigger_reason="END_WINDOW DOWN REVERSE: initial=UP",
    )
    cfg = end_window.EndWindowConfig(enabled=True, max_trades_per_window=4)
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial, reverse]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=90.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=200.0,
            cfg=cfg,
        ))
    assert out is None
    buy.assert_not_awaited()


def test_removed_reverse_hedge_does_not_buy_when_direction_flips_back():
    market = _market(
        up_ask=0.40,
        up_ask_depth=[(0.40, 1000.0)],
        down_ask=0.91,
        close_ts=int(time.time()) + 12,
    )
    initial = {
        "timestamp": 1.0,
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "outcome": "UP",
        "entry_price": 0.99,
        "btc_distance": 20.0,
        "trigger_reason": "END_WINDOW UP TIME-1: exact",
        "order_id": "initial-up",
        "resolved": False,
        "exited_early": False,
    }
    reverse = dict(
        initial,
        timestamp=2.0,
        outcome="DOWN",
        entry_price=0.40,
        btc_distance=-10.0,
        trigger_reason="END_WINDOW DOWN REVERSE: initial=UP source=TIME-1 source_ref=order-initial-up",
        order_id="reverse-down",
    )
    cfg = end_window.EndWindowConfig(
        enabled=True,
        max_trades_per_window=4,

    )
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial, reverse]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=110.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=200.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_removed_reverse_hedge_does_not_batch_reverse_orders_per_window():
    market = _market(
        up_ask=0.91,
        down_ask=0.40,
        down_ask_depth=[(0.40, 1500.0)],
        close_ts=int(time.time()) + 12,
    )
    ledger = [
        {
            "timestamp": float(index),
            "window_ts": market.window_ts,
            "market_slug": market.slug,
            "trigger": "END_WINDOW",
            "outcome": "UP",
            "entry_price": price,
            "btc_distance": 20.0 + index,
            "trigger_reason": f"END_WINDOW UP {source}: exact",
            "order_id": f"source-{index}",
            "resolved": False,
            "exited_early": False,
        }
        for index, (source, price) in enumerate(
            (("TIME-3", 0.97), ("TIME-1", 0.98), ("TIME-2", 0.99)),
            start=1,
        )
    ]
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        max_trades_per_window=9,

    )

    async def execute(**kwargs):
        rec = SimpleNamespace(
            trigger="END_WINDOW",
            trigger_reason=kwargs["reason"],
            outcome=kwargs["outcome"],
            amount_usd=kwargs["amount_usd"],
        )
        ledger.append({
            "timestamp": time.time(),
            "window_ts": market.window_ts,
            "market_slug": market.slug,
            "trigger": "END_WINDOW",
            "outcome": kwargs["outcome"],
            "btc_distance": -10.0,
            "trigger_reason": kwargs["reason"],
            "resolved": False,
            "exited_early": False,
        })
        return rec

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", side_effect=lambda: ledger),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(side_effect=execute)) as buy,
    ):
        records = asyncio.run(end_window_runner.try_all_end_window(
            market=market,
            btc_open=100.0,
            btc_now=90.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=300.0,
            cfg=cfg,
        ))

    assert records == []
    buy.assert_not_awaited()


def test_removed_reverse_hedge_ignores_above_cap_opposite_price():
    market = _market(
        up_ask=0.88,
        down_ask=0.41,
        down_ask_depth=[(0.41, 300.0)],
    )
    initial = {
        "timestamp": 1.0,
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "outcome": "UP",
        "entry_price": 0.97,
        "btc_distance": 20.0,
        "trigger_reason": "END_WINDOW UP TIME-3: exact",
        "resolved": False,
        "exited_early": False,
    }
    cfg = end_window.EndWindowConfig(enabled=True, max_trades_per_window=4)

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=90.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=200.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_removed_reverse_hedge_ignores_below_cap_opposite_price():
    market = _market(
        up_ask=0.88,
        down_ask=0.39,
        down_ask_depth=[(0.39, 300.0)],
        close_ts=int(time.time()) + 12,
    )
    initial = {
        "timestamp": 1.0,
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "outcome": "UP",
        "entry_price": 0.97,
        "btc_distance": 20.0,
        "trigger_reason": "END_WINDOW UP TIME-3: exact",
        "resolved": False,
        "exited_early": False,
    }
    cfg = end_window.EndWindowConfig(enabled=True, max_trades_per_window=4)
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=90.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=200.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_btc_15m_market_is_ignored_after_arb_removal():
    market = _market(
        slug="btc-updown-15m-1800000000",
        close_ts=1800000900,
        up_ask=0.43,
        down_ask=0.43,
        up_ask_depth=[(0.43, 300.0)],
        down_ask_depth=[(0.43, 300.0)],
    )
    settings = end_window_runner.st.BotSettings(
        market_5m_enabled=False,
    )
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        records = asyncio.run(end_window_runner.try_all_end_window(
            market=market,
            btc_open=100.0,
            btc_now=100.0,
            secs_elapsed=20.0,
            secs_left=880.0,
            bankroll_usd=200.0,
            settings=settings,
        ))

    assert records == []
    buy.assert_not_awaited()


def test_btc_5m_removed_arb_settings_do_not_fire_arbitrage():
    market = _market(
        close_ts=1800000300,
        up_ask=0.43,
        down_ask=0.44,
        up_ask_depth=[(0.43, 10.0)],
        down_ask_depth=[(0.44, 300.0)],
    )
    settings = end_window_runner.st.BotSettings(
        market_5m_enabled=True,
        time1_enabled=False,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=100.0,
            secs_elapsed=120.0,
            secs_left=180.0,
            bankroll_usd=100.0,
            settings=settings,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_removed_arb_settings_fall_through_to_time():
    market = _market(
        close_ts=1800000300,
        up_ask=0.90,
        down_ask=0.60,
        up_ask_depth=[(0.90, 300.0)],
        down_ask_depth=[(0.60, 300.0)],
    )
    settings = end_window_runner.st.BotSettings(
        market_5m_enabled=True,
        time1_enabled=True,
        time1_price=0.90,
        time1_trade_usd=100.0,
        time1_min_secs_left=3.0,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
    )
    fake_record = SimpleNamespace(
        trigger="END_WINDOW",
        trigger_reason="END_WINDOW UP TIME-1",
        outcome="UP",
        amount_usd=100.0,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=200.0,
            secs_elapsed=270.0,
            secs_left=20.0,
            bankroll_usd=100.0,
            settings=settings,
        ))

    assert out is fake_record
    assert buy.await_args.kwargs["outcome"] == "UP"
    assert "TIME-1" in buy.await_args.kwargs["reason"]


def test_legacy_arb_leg_does_not_block_time_after_arb_removal():
    market = _market(
        close_ts=1800000300,
        up_ask=0.90,
        down_ask=0.60,
        up_ask_depth=[(0.90, 300.0)],
        down_ask_depth=[(0.60, 300.0)],
    )
    prior = {
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP ARB5-UP",
        "outcome": "UP",
        "resolved": False,
        "exited_early": False,
    }
    settings = end_window_runner.st.BotSettings(
        market_5m_enabled=True,
        time1_enabled=True,
        time1_price=0.90,
        time1_trade_usd=100.0,
        time1_min_secs_left=3.0,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[prior]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=200.0,
            secs_elapsed=270.0,
            secs_left=20.0,
            bankroll_usd=100.0,
            settings=settings,
        ))

    assert out is not None
    assert "TIME-1" in buy.await_args.kwargs["reason"]


def test_end_window_disabled_layer_does_not_execute():
    market = _market()
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        t1_enabled=False,
        t2_enabled=True,
        t3_enabled=True,
        t4_enabled=True,
        t5_enabled=True,
        t6_enabled=True,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=190.0,
            secs_elapsed=275.0,
            secs_left=25.0,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time1_fires_early_with_aligned_three_dollar_delta_and_full_depth():
    market = _market(
        up_ask=0.98,
        down_ask=0.03,
        up_price=0.97,
        down_price=0.02,
        up_ask_depth=[(0.98, 103.0), (0.99, 50.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True,
        time2_enabled=True,
        t1_enabled=True, t2_enabled=True, t3_enabled=True,
        t4_enabled=True, t5_enabled=True, t6_enabled=True,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_003.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is fake_record
    assert buy.await_args.kwargs["outcome"] == "UP"
    assert buy.await_args.kwargs["price"] == 0.98
    assert buy.await_args.kwargs["amount_usd"] == 100.0
    assert buy.await_args.kwargs["strict_price"] is True
    assert buy.await_args.kwargs["skip_preflight"] is False
    assert "TIME-1" in buy.await_args.kwargs["reason"]


@pytest.mark.parametrize("delta", [-2.99, 0.0, 2.99])
def test_time_triggers_reject_delta_below_three_dollars(delta):
    market = _market(
        up_ask=0.97,
        down_ask=0.03,
        up_price=0.96,
        down_price=0.02,
        up_ask_depth=[(0.97, 104.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True, time3_enabled=True,
        t1_enabled=False, t2_enabled=False, t3_enabled=False,
        t4_enabled=False, t5_enabled=False, t6_enabled=False,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_000.0 + delta,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
            settings=settings,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time_trigger_rejects_exact_price_on_side_opposite_delta():
    market = _market(
        up_ask=0.03,
        down_ask=0.97,
        up_price=0.02,
        down_price=0.96,
        down_ask_depth=[(0.97, 104.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True, time3_enabled=True,
        t1_enabled=False, t2_enabled=False, t3_enabled=False,
        t4_enabled=False, t5_enabled=False, t6_enabled=False,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_003.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
            settings=settings,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time_scan_priority_is_time3_then_time1_then_time2():
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True,
        time2_enabled=True,
        time3_enabled=True,
        time1_price=0.98,
        time2_price=0.99,
        time3_price=0.97,
        time1_trade_usd=100.0,
        time2_trade_usd=100.0,
        time3_trade_usd=100.0,
        time1_min_secs_left=3.0,
        time2_min_secs_left=3.0,
        time3_min_secs_left=3.0,
        t1_enabled=False,
        t2_enabled=False,
        t3_enabled=True,
        t4_enabled=True,
        t5_enabled=True,
        t6_enabled=True,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    time3_market = _market(
        up_ask=0.97,
        down_ask=0.99,
        up_price=0.96,
        down_price=0.98,
        up_ask_depth=[(0.97, 104.0)],
        down_ask_depth=[(0.99, 102.0)],
    )
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch(
            "bot_runtime.end_window_runner._execute_buy",
            AsyncMock(return_value=fake_record),
        ) as buy,
    ):
        out = asyncio.run(
            end_window_runner.try_end_window_market(
                market=time3_market,
                btc_open=64_000.0,
                btc_now=64_003.0,
                secs_elapsed=100.0,
                secs_left=200.0,
                bankroll_usd=1_000.0,
                cfg=cfg,
                settings=settings,
            )
        )

    assert out is fake_record
    assert "TIME-3" in buy.await_args.kwargs["reason"]

    time1_market = _market(
        up_ask=0.98,
        down_ask=0.99,
        up_price=0.97,
        down_price=0.98,
        up_ask_depth=[(0.98, 103.0)],
        down_ask_depth=[(0.99, 102.0)],
    )
    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch(
            "bot_runtime.end_window_runner._execute_buy",
            AsyncMock(return_value=fake_record),
        ) as buy,
    ):
        out = asyncio.run(
            end_window_runner.try_end_window_market(
                market=time1_market,
                btc_open=64_000.0,
                btc_now=64_003.0,
                secs_elapsed=100.0,
                secs_left=200.0,
                bankroll_usd=1_000.0,
                cfg=cfg,
                settings=settings,
            )
        )

    assert out is fake_record
    assert "TIME-1" in buy.await_args.kwargs["reason"]


def test_time4_and_time6_both_fire_at_exact_096_in_same_tick():
    market = _market(
        up_ask=0.96,
        down_ask=0.05,
        up_ask_depth=[(0.96, 500.0)],
        close_ts=int(time.time()) + 12,
    )
    settings = SimpleNamespace(
        time1_enabled=False,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=True,
        time5_enabled=False,
        time6_enabled=True,
        time4_price=0.96,
        time6_price=0.96,
        time4_trade_usd=100.0,
        time6_trade_usd=100.0,
        time4_min_secs_left=3.0,
        time6_min_secs_left=3.0,
        t1_enabled=False,
        t2_enabled=False,
        t3_enabled=False,
        t4_enabled=False,
        t5_enabled=False,
        t6_enabled=False,
    )
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        max_trades_per_window=9,
    )

    async def execute(**kwargs):
        return SimpleNamespace(
            trigger="END_WINDOW",
            trigger_reason=kwargs["reason"],
            outcome=kwargs["outcome"],
            amount_usd=kwargs["amount_usd"],
        )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(side_effect=execute)) as buy,
    ):
        records = asyncio.run(end_window_runner.try_all_end_window(
            market=market,
            btc_open=100.0,
            btc_now=110.0,
            secs_elapsed=288.0,
            secs_left=12.0,
            bankroll_usd=200.0,
            cfg=cfg,
            settings=settings,
        ))

    assert len(records) == 2
    assert "TIME-4" in buy.await_args_list[0].kwargs["reason"]
    assert "TIME-6" in buy.await_args_list[1].kwargs["reason"]
    assert [call.kwargs["amount_usd"] for call in buy.await_args_list] == [100.0, 100.0]


def test_buy1_fires_on_aligned_mid_price_momentum():
    market = _market(
        close_ts=int(time.time()) + 200,
        up_ask=0.55,
        up_price=0.54,
        down_ask=0.47,
        down_price=0.46,
        up_ask_depth=[(0.55, 100.0)],
    )
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        max_trades_per_window=9,
    )
    settings = SimpleNamespace(
        buy1_enabled=True,
        buy1_trade_usd=25.0,
        buy1_min_price=0.50,
        buy1_max_price=0.60,
        buy1_sell_min_price=0.80,
        buy1_sell_max_price=0.90,
        buy1_min_delta_usd=8.0,
        buy1_max_secs_left=260.0,
        buy1_min_secs_left=20.0,
        buy1_max_open_positions=1,
        time1_enabled=False,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
        t1_enabled=False,
        t2_enabled=False,
        t3_enabled=False,
        t4_enabled=False,
        t5_enabled=False,
        t6_enabled=False,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW", trigger_reason="BUY-1 UP")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=112.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=100.0,
            cfg=cfg,
            settings=settings,
        ))

    assert out is fake_record
    assert buy.await_args.kwargs["price"] == 0.55
    assert buy.await_args.kwargs["amount_usd"] == 25.0
    assert "BUY-1 UP" in buy.await_args.kwargs["reason"]


def test_buy1_rejects_price_above_configured_buy_range():
    market = _market(
        close_ts=int(time.time()) + 200,
        up_ask=0.61,
        up_price=0.60,
        up_ask_depth=[(0.61, 100.0)],
    )
    settings = SimpleNamespace(
        buy1_enabled=True,
        buy1_trade_usd=25.0,
        buy1_min_price=0.50,
        buy1_max_price=0.60,
        buy1_min_delta_usd=8.0,
        buy1_max_secs_left=260.0,
        buy1_min_secs_left=20.0,
        buy1_max_open_positions=1,
        time1_enabled=False,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
        t1_enabled=False,
        t2_enabled=False,
        t3_enabled=False,
        t4_enabled=False,
        t5_enabled=False,
        t6_enabled=False,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=112.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=100.0,
            cfg=end_window.EndWindowConfig(enabled=True),
            settings=settings,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_buy1_exit_sells_and_resolves_matching_leg():
    market = _market(
        up_price=0.85,
        up_ask=0.86,
        close_ts=int(time.time()) + 120,
    )
    trade = {
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "trigger_reason": "BUY-1 UP: quick buy",
        "outcome": "UP",
        "order_id": "entry-1",
        "shares": 10.0,
        "entry_price": 0.55,
        "amount_usd": 5.5,
        "resolved": False,
        "exited_early": False,
    }
    closed = dict(trade, resolved=True, pnl=3.0, won=True, balance_returned=8.5)
    settings = SimpleNamespace(buy1_sell_min_price=0.80, buy1_sell_max_price=0.90)

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[trade]),
        patch(
            "bot_runtime.end_window_runner.trader.execute_sell",
            AsyncMock(return_value=SimpleNamespace(success=True, price=0.85, size=10.0, size_matched=10.0)),
        ) as sell,
        patch("bot_runtime.end_window_runner.st.resolve_specific_leg", return_value=closed) as resolve,
        patch("bot_runtime.end_window_runner.st.add_balance") as add_balance,
        patch("bot_runtime.end_window_runner.st.update_daily_pnl") as update_daily,
    ):
        exits = asyncio.run(end_window_runner.try_buy1_exits(
            market=market,
            secs_left=120.0,
            settings=settings,
        ))

    assert exits == [closed]
    assert sell.await_args.args[:4] == (market.up_token, "UP", 0.85, 10.0)
    resolve.assert_called_once_with(
        market.window_ts,
        "UP",
        0.85,
        "BUY-1 quick sell bid=0.8500 target=0.80-0.90",
        secs_left=120.0,
        market_slug=market.slug,
        order_id="entry-1",
    )
    add_balance.assert_called_once_with(8.5)
    update_daily.assert_called_once_with(3.0, True)


def test_time_screening_rules_are_sorted_by_buy_price_then_trigger_number():
    settings = SimpleNamespace(
        time1_price=0.98,
        time2_price=0.99,
        time3_price=0.97,
        time4_price=0.96,
        time5_price=0.95,
        time6_price=0.96,
    )

    rules = end_window_runner._time_screening_rules(
        settings,
        {index: True for index in range(1, 7)},
    )

    assert [rule[0] for rule in rules] == [
        "TIME-5", "TIME-4", "TIME-6", "TIME-3", "TIME-1", "TIME-2",
    ]
    assert [rule[2] for rule in rules] == [0.95, 0.96, 0.96, 0.97, 0.98, 0.99]


def test_disabled_t1_t2_falls_through_to_t3():
    market = _market(
        close_ts=int(time.time()) + 15,
        up_ask=0.94,
        down_ask=0.07,
        up_price=0.93,
        down_price=0.06,
        up_ask_depth=[(0.94, 200.0)],
        down_ask_depth=[(0.07, 500.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True,
        time2_enabled=True,
        time3_enabled=True,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
        t1_enabled=False,
        t2_enabled=False,
        t3_enabled=True,
        t4_enabled=True,
        t5_enabled=True,
        t6_enabled=True,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch(
            "bot_runtime.end_window_runner._execute_buy",
            AsyncMock(return_value=fake_record),
        ) as buy,
    ):
        out = asyncio.run(
            end_window_runner.try_end_window_market(
                market=market,
                btc_open=100.0,
                btc_now=135.0,
                secs_elapsed=285.0,
                secs_left=15.0,
                bankroll_usd=1_000.0,
                cfg=cfg,
                settings=settings,
            )
        )

    assert out is fake_record
    assert " T3:" in buy.await_args.kwargs["reason"]


def test_time1_rejects_early_entry_when_depth_cannot_fill_100_usd():
    market = _market(
        up_ask=0.98,
        down_ask=0.03,
        up_price=0.97,
        down_price=0.02,
        up_ask_depth=[(0.98, 50.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True,
        time2_enabled=True,
        t1_enabled=True, t2_enabled=True, t3_enabled=True,
        t4_enabled=True, t5_enabled=True, t6_enabled=True,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_001.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time1_toggle_off_prevents_early_entry():
    market = _market(
        up_ask=0.98,
        up_price=0.97,
        up_ask_depth=[(0.98, 120.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=False,
        time2_enabled=True,
        t1_enabled=True, t2_enabled=True, t3_enabled=True,
        t4_enabled=True, t5_enabled=True, t6_enabled=True,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_001.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time2_fires_at_exact_099_with_its_own_slot():
    market = _market(
        up_ask=0.99,
        down_ask=0.02,
        up_price=0.98,
        down_price=0.01,
        up_ask_depth=[(0.99, 102.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True,
        t1_enabled=True, t2_enabled=True, t3_enabled=True,
        t4_enabled=True, t5_enabled=True, t6_enabled=True,
    )
    prior_time1 = {
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-1: ask=0.9800",
        "resolved": False,
    }
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[prior_time1]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_003.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is fake_record
    assert buy.await_args.kwargs["price"] == 0.99
    assert buy.await_args.kwargs["amount_usd"] == 100.0
    assert "TIME-2" in buy.await_args.kwargs["reason"]


def test_time_triggers_do_not_trade_in_last_three_seconds():
    market = _market(
        up_ask=0.99,
        up_price=0.98,
        up_ask_depth=[(0.99, 102.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True,
        t1_enabled=False, t2_enabled=False, t3_enabled=False,
        t4_enabled=False, t5_enabled=False, t6_enabled=False,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_003.0,
            secs_elapsed=297.0,
            secs_left=3.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time_min_delta_setting_blocks_exact_price_entry():
    market = _market(
        up_ask=0.99,
        up_price=0.98,
        up_ask_depth=[(0.99, 102.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True,
        time1_min_delta_usd=3.0, time2_min_delta_usd=10.0,
        t1_enabled=False, t2_enabled=False, t3_enabled=False,
        t4_enabled=False, t5_enabled=False, t6_enabled=False,
    )
    prior_time1 = {
        "window_ts": market.window_ts,
        "market_slug": market.slug,
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-1: ask=0.9800",
        "resolved": False,
    }

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[prior_time1]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_003.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_time3_fires_at_exact_097_after_other_time_slots():
    market = _market(
        up_ask=0.97,
        down_ask=0.04,
        up_price=0.96,
        down_price=0.03,
        up_ask_depth=[(0.97, 104.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True, time3_enabled=True,
        time1_price=0.98, time2_price=0.99, time3_price=0.97,
        time1_trade_usd=100.0, time2_trade_usd=100.0, time3_trade_usd=100.0,
        time1_min_secs_left=3.0, time2_min_secs_left=3.0, time3_min_secs_left=3.0,
        t1_enabled=True, t2_enabled=True, t3_enabled=True,
        t4_enabled=True, t5_enabled=True, t6_enabled=True,
    )
    prior_trades = [
        {
            "window_ts": market.window_ts, "market_slug": market.slug,
            "trigger": "END_WINDOW", "trigger_reason": f"END_WINDOW UP TIME-{i}: exact",
            "resolved": False,
        }
        for i in (1, 2)
    ]
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=prior_trades),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=64_003.0,
            secs_elapsed=100.0,
            secs_left=200.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
            settings=settings,
        ))

    assert out is fake_record
    assert buy.await_args.kwargs["price"] == 0.97
    assert "TIME-3" in buy.await_args.kwargs["reason"]


def test_layer_slot_remains_available_after_time1_and_time2():
    market = _market()
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)
    settings = SimpleNamespace(
        time1_enabled=True, time2_enabled=True,
        t1_enabled=True, t2_enabled=True, t3_enabled=True,
        t4_enabled=True, t5_enabled=True, t6_enabled=True,
    )
    prior_trades = [
        {
            "window_ts": market.window_ts,
            "market_slug": market.slug,
            "trigger": "END_WINDOW",
            "trigger_reason": "END_WINDOW UP TIME-1: ask=0.9800",
            "resolved": False,
        },
        {
            "window_ts": market.window_ts,
            "market_slug": market.slug,
            "trigger": "END_WINDOW",
            "trigger_reason": "END_WINDOW UP TIME-2: ask=0.9900",
            "resolved": False,
        },
    ]
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_settings", return_value=settings),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=prior_trades),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=190.0,
            secs_elapsed=275.0,
            secs_left=25.0,
            bankroll_usd=1_000.0,
            cfg=cfg,
        ))

    assert out is fake_record
    assert " T1:" in buy.await_args.kwargs["reason"]


def test_end_window_rejects_low_priced_selected_side():
    market = _market(up_ask=0.96, down_ask=0.01, up_price=0.95, down_price=0.01)
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=62717.80,
            btc_now=62687.99,
            secs_elapsed=290.0,
            secs_left=9.7,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_end_window_rejects_quote_without_real_ask_depth():
    market = _market(
        up_ask=0.01,
        down_ask=0.99,
        up_ask_depth=[],
        down_ask_depth=[],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=63_800.0,
            secs_elapsed=293.0,
            secs_left=7.0,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_end_window_rejects_depth_below_trade_amount():
    market = _market(
        down_ask=0.99,
        down_ask_depth=[(0.99, 10.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=64_000.0,
            btc_now=63_800.0,
            secs_elapsed=293.0,
            secs_left=7.0,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_mock_fok_fill_consumes_real_depth_and_returns_vwap():
    market = _market(
        up_ask_depth=[(0.80, 50.0), (0.90, 100.0)],
    )

    fill = end_window_runner._mock_fok_fill(
        market,
        "UP",
        amount_usd=100.0,
        max_price=0.90,
    )

    assert fill is not None
    price, shares = fill
    assert price == 0.8571
    assert shares == 115.64


def test_mock_fok_fill_rejects_depth_above_limit_price():
    market = _market(
        up_ask_depth=[(0.80, 50.0), (0.96, 100.0)],
    )

    fill = end_window_runner._mock_fok_fill(
        market,
        "UP",
        amount_usd=100.0,
        max_price=0.90,
    )

    assert fill is None


def test_end_window_retry_stops_under_four_seconds():
    market = _market(close_ts=int(time.time()) + 3)
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        force_trade=True,
        force_retry_attempts=3,
        force_retry_delay_secs=0.0,
    )

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=170.0,
            secs_elapsed=296.5,
            secs_left=3.5,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_closed_directional_orphan_retry_picks_up_pending_end_window_trade():
    bot = Bot.__new__(Bot)
    bot._last_directional_orphan_resolve_check = 0.0
    current_window = 1800000300
    pending_window = 1800000000
    trades = [
        {
            "window_ts": pending_window,
            "trigger": "END_WINDOW",
            "resolved": False,
            "exited_early": False,
        }
    ]

    with (
        patch("bot_runtime.app.time.time", return_value=current_window + 10),
        patch("bot_runtime.app.st.load_trades", return_value=trades),
        patch.object(bot, "_resolve_directional_window", AsyncMock(return_value=[])) as resolve_window,
    ):
        asyncio.run(bot._resolve_closed_directional_orphans(None, current_window))

    resolve_window.assert_awaited_once_with(None, pending_window, source="retry-official")


def test_btc_now_fallbacks_to_exchange_price_without_chainlink():
    bot = Bot.__new__(Bot)
    bot._last_btc_rest_ts = 0.0
    bot._last_btc_rest_price = 0.0
    prices = {"gateio": 100.0}
    cache = SimpleNamespace(
        btc_age=0.2, 
        btc_price=100.0, 
        btc_source="gateio",
        source_btc=lambda source, _max_age: prices.get(source),
        source_btc_age=lambda _source: 0.2,
    )

    with (
        patch.dict(os.environ, {"GATEIO_WS_ENABLED": "true", "COINBASE_WS_ENABLED": "false"}),
        patch("bot_runtime.app.get_cache", return_value=cache),
        patch("bot_runtime.app.mkt.get_btc_price", AsyncMock(return_value=99.0)) as fetch_price,
    ):
        price = asyncio.run(bot._btc_now(None))

    assert price == 100.0
    assert bot._last_btc_source == "gateio (fallback)"
    fetch_price.assert_not_awaited()


def test_btc_now_fallbacks_to_cached_exchange_price_without_chainlink():
    bot = Bot.__new__(Bot)
    bot._last_btc_rest_ts = time.time()
    bot._last_btc_rest_price = 99.0
    prices = {"gateio": 100.0}
    cache = SimpleNamespace(
        btc_age=0.1, 
        btc_price=100.0, 
        btc_source="gateio",
        source_btc=lambda source, _max_age: prices.get(source),
        source_btc_age=lambda _source: 0.1,
    )

    with (
        patch.dict(os.environ, {"GATEIO_WS_ENABLED": "true", "COINBASE_WS_ENABLED": "false"}),
        patch("bot_runtime.app.get_cache", return_value=cache),
        patch("bot_runtime.app.mkt.get_btc_price", AsyncMock(return_value=99.0)) as fetch_price,
    ):
        price = asyncio.run(bot._btc_now(None))

    assert price == 100.0
    assert bot._last_btc_source == "gateio (fallback)"
    fetch_price.assert_not_awaited()


def test_btc_now_chainlink_only_does_not_use_gateio_when_disabled():
    bot = Bot.__new__(Bot)
    prices = {"gateio": 100.0}
    cache = SimpleNamespace(
        btc_age=0.1,
        btc_price=100.0,
        btc_source="gateio",
        source_btc=lambda source, _max_age: prices.get(source),
        source_btc_age=lambda source: 999.0 if source == "chainlink" else 0.1,
    )

    with (
        patch.dict(os.environ, {"GATEIO_WS_ENABLED": "false", "COINBASE_WS_ENABLED": "false"}),
        patch("bot_runtime.app.get_cache", return_value=cache),
        patch("bot_runtime.app.mkt.get_btc_price", AsyncMock()) as fetch_price,
    ):
        price = asyncio.run(bot._btc_now(None))

    assert price == 0.0
    assert bot._last_btc_source == "chainlink-unavailable"
    assert bot._last_chainlink_age == 999.0
    assert bot._last_exchange_age == 999.0
    fetch_price.assert_not_awaited()


def test_btc_now_uses_chainlink_directly_when_fresh():
    bot = Bot.__new__(Bot)
    bot._last_btc_rest_ts = time.time()
    bot._last_btc_rest_price = 100.0
    prices = {"chainlink": 101.0, "coinbase": 100.0, "gateio": 100.8}
    cache = SimpleNamespace(
        btc_age=0.1,
        btc_price=100.8,
        btc_source="gateio",
        source_btc=lambda source, _max_age: prices.get(source),
        source_btc_age=lambda _source: 0.1,
    )

    with (
        patch("bot_runtime.app.get_cache", return_value=cache),
        patch("bot_runtime.app.mkt.get_btc_price", AsyncMock()) as fetch_price,
    ):
        price = asyncio.run(bot._btc_now(None))

    assert price == 101.0
    assert bot._last_btc_source == "chainlink"
    fetch_price.assert_not_awaited()


def test_btc_open_rejects_missing_polymarket_price_to_beat():
    bot = Bot.__new__(Bot)
    market = _market()
    market.target_price = 0.0

    with patch("bot_runtime.app.get_cache", return_value=SimpleNamespace()):
        price, source = asyncio.run(bot._btc_open(None, market, market.window_ts))

    assert price == 0.0
    assert source == "missing Polymarket priceToBeat"


def test_btc_open_uses_chainlink_window_open_when_gamma_target_is_missing():
    bot = Bot.__new__(Bot)
    market = _market()
    market.target_price = 0.0
    cache = SimpleNamespace(
        source_btc_at_time=lambda source, timestamp, max_drift, **_kwargs: (64_123.45, timestamp + 0.2, 0.2)
    )

    with patch("bot_runtime.app.get_cache", return_value=cache):
        price, source = asyncio.run(bot._btc_open(None, market, market.window_ts))

    assert price == 64_123.45
    assert source == "Polymarket Chainlink open drift=0.20s"


def test_btc_open_uses_mock_chainlink_open_fallback_after_restart():
    bot = Bot.__new__(Bot)
    market = _market()
    market.target_price = 0.0
    cache = SimpleNamespace(
        source_btc_at_time=lambda source, timestamp, max_drift, **_kwargs: (
            (64_123.45, timestamp + 25.0, 25.0) if max_drift >= 25.0 else None
        )
    )

    with (
        patch("bot_runtime.app.config.MOCK_MODE", True),
        patch("bot_runtime.app.time.time", return_value=market.window_ts + 30.0),
        patch("bot_runtime.app.get_cache", return_value=cache),
    ):
        price, source = asyncio.run(bot._btc_open(None, market, market.window_ts))

    assert price == 64_123.45
    assert source == "MOCK Chainlink open fallback drift=25.00s"


def test_apply_safety_gate_blocks_trading_loudly():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(trading_enabled=True)

    with (
        patch.object(bot, "_load_cfg", return_value=SimpleNamespace()),
        patch("bot_runtime.app.safety.startup_safety_report", return_value=SimpleNamespace(ok=False, reason="bad live config")),
        patch("bot_runtime.app.st.set_trading_enabled") as set_trading,
        patch.object(bot, "_save") as save_state,
    ):
        ok = bot._apply_safety_gate("startup_safety_block")

    assert not ok
    assert bot.state.trading_enabled is False
    assert bot.state.circuit_breaker_active is True
    assert bot.state.circuit_breaker_reason == "bad live config"
    assert bot.state.status == "startup_safety_block"
    set_trading.assert_called_once_with(False)
    save_state.assert_called_once_with(force=True)


def test_daily_profit_halt_pauses_entries_until_next_day():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot.state = SimpleNamespace(trading_enabled=True)
        cfg = SimpleNamespace(daily_profit_stop_usd=80.0)
        daily = {"pnl": 82.0, "start_balance": 200.0, "halted": False, "halt_reason": ""}

        with (
            patch("bot_runtime.app.st.load_daily_pnl", return_value=daily),
            patch("bot_runtime.app.st.set_daily_halted") as set_halted,
            patch("bot_runtime.app.st.set_trading_enabled") as set_trading,
            patch.object(bot, "_sync_stats") as sync_stats,
            patch.object(bot, "_save") as save_state,
            patch("bot_runtime.app.tg.send", AsyncMock()) as send_notification,
        ):
            halted = bot._apply_daily_profit_halt(cfg)
            await asyncio.sleep(0)

        assert halted is True
        assert bot.state.trading_enabled is True
        assert bot.state.status == "daily_profit_stop"
        set_halted.assert_called_once()
        set_trading.assert_not_called()
        assert "Daily profit target reached" in set_halted.call_args.args[1]
        sync_stats.assert_called_once()
        save_state.assert_called_once_with(force=True)
        send_notification.assert_awaited_once()

    asyncio.run(run_check())


def test_daily_profit_halt_ignores_below_threshold():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(trading_enabled=True)
    cfg = SimpleNamespace(daily_profit_stop_usd=80.0)
    daily = {"pnl": 79.99, "start_balance": 200.0, "halted": False, "halt_reason": ""}

    with (
        patch("bot_runtime.app.st.load_daily_pnl", return_value=daily),
        patch("bot_runtime.app.st.set_daily_halted") as set_halted,
        patch.object(bot, "_sync_stats") as sync_stats,
        patch.object(bot, "_save") as save_state,
    ):
        halted = bot._apply_daily_profit_halt(cfg)

    assert halted is False
    set_halted.assert_not_called()
    sync_stats.assert_not_called()
    save_state.assert_not_called()


def test_live_daily_profit_halt_ignores_mock_daily_file():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(trading_enabled=True, daily_halted=True, daily_halt_reason="mock halt")
    cfg = SimpleNamespace(daily_profit_stop_usd=80.0)
    mock_daily = {
        "pnl": 110.0,
        "trades": 3,
        "wins": 3,
        "losses": 0,
        "start_balance": 200.0,
        "halted": True,
        "halt_reason": "Daily profit target reached",
    }

    with (
        patch("bot_runtime.app.config.MOCK_MODE", False),
        patch("bot_runtime.app.st.load_daily_pnl", return_value=mock_daily),
        patch("bot_runtime.app.st.load_trades", return_value=[]),
        patch("bot_runtime.app.st.set_daily_halted") as set_halted,
        patch.object(bot, "_sync_stats") as sync_stats,
        patch.object(bot, "_save") as save_state,
    ):
        halted = bot._apply_daily_profit_halt(cfg)

    assert halted is False
    assert bot.state.daily_pnl == 0.0
    assert bot.state.daily_halted is False
    assert bot.state.daily_halt_reason == ""
    set_halted.assert_not_called()
    sync_stats.assert_not_called()
    save_state.assert_not_called()


def test_live_daily_pnl_uses_trading_timezone_boundary():
    bot = Bot.__new__(Bot)
    daily = {
        "pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "start_balance": 200.0,
        "halted": False,
        "halt_reason": "",
    }
    trades = [
        {"timestamp": _utc_ts(2026, 6, 27, 16, 30), "resolved": True, "won": True, "pnl": 99.0, "mock": False},
        {"timestamp": _utc_ts(2026, 6, 27, 18, 30), "resolved": True, "won": True, "pnl": 2.0, "mock": False},
        {"timestamp": _utc_ts(2026, 6, 27, 18, 30), "resolved": True, "won": True, "pnl": 999.0, "mock": True},
    ]

    with (
        patch("bot_runtime.app.config.MOCK_MODE", False),
        patch("bot_runtime.app.st.load_daily_pnl", return_value=daily),
        patch("bot_runtime.app.st._daily_date", side_effect=_bangkok_date),
        patch("bot_runtime.app.time.time", return_value=_utc_ts(2026, 6, 27, 19, 0)),
    ):
        out = bot._daily_pnl_for_mode(trades)

    assert out["pnl"] == 2.0
    assert out["trades"] == 1
    assert out["wins"] == 1
    assert out["losses"] == 0
    assert out["halted"] is False


def test_live_balance_gate_blocks_before_market_scan():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot.state = SimpleNamespace(
            live_cash=0.29,
            live_balance_ok=True,
            live_balance_error="",
            status="waiting",
            bot_status="waiting",
            trading_enabled=True,
            circuit_breaker_reason="",
        )
        cfg = SimpleNamespace()

        with (
            patch("bot_runtime.app.config.MOCK_MODE", False),
            patch("bot_runtime.app.end_window_strategy.EndWindowConfig.from_settings", return_value=SimpleNamespace(live_trade_usd=1.0)),
            patch.object(bot, "_schedule_live_account_refresh") as schedule_refresh,
            patch.object(bot, "_sync_stats") as sync_stats,
            patch.object(bot, "_save") as save_state,
        ):
            blocked = await bot._apply_live_balance_gate(object(), cfg)

        assert blocked is True
        assert bot.state.status == "insufficient_balance"
        assert "cash $0.29 < min order $1.00" in bot.state.circuit_breaker_reason
        schedule_refresh.assert_called_once()
        sync_stats.assert_called_once()
        save_state.assert_called_once_with(force=True)

    asyncio.run(run_check())


def test_live_account_refresh_keeps_last_cash_when_balance_fetch_fails():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot._last_live_account_refresh = 0.0
        bot.state = SimpleNamespace(
            live_cash=6.2,
            live_portfolio=1.0,
            live_total=7.2,
            live_balance_ok=True,
            live_balance_error="",
            live_balance_last_ok_ts=123.0,
            live_portfolio_ok=True,
            live_portfolio_error="",
            live_portfolio_source="",
        )

        with (
            patch("bot_runtime.app.config.MOCK_MODE", False),
            patch(
                "bot_runtime.app.trader.fetch_live_balance",
                AsyncMock(return_value={"ok": False, "cash": 0.0, "error": "connection refused"}),
            ),
            patch(
                "bot_runtime.app.trader.fetch_live_portfolio",
                AsyncMock(return_value={"ok": True, "portfolio": 1.5}),
            ),
        ):
            await bot._refresh_live_account(object(), force=True)

        assert bot.state.live_cash == 6.2
        assert bot.state.live_balance_ok is False
        assert bot.state.live_balance_error == "connection refused"
        assert bot.state.live_portfolio == 1.5
        assert bot.state.live_total == 7.7

    asyncio.run(run_check())


def test_live_balance_gate_allows_scan_when_balance_fetch_is_unavailable():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot.state = SimpleNamespace(
            live_cash=6.2,
            live_balance_ok=False,
            live_balance_error="balance fetch timeout",
            status="waiting",
            bot_status="waiting",
            trading_enabled=True,
            circuit_breaker_reason="live balance unavailable: balance fetch timeout",
        )
        cfg = SimpleNamespace()

        with (
            patch("bot_runtime.app.config.MOCK_MODE", False),
            patch("bot_runtime.app.end_window_strategy.EndWindowConfig.from_settings", return_value=SimpleNamespace(live_trade_usd=5.0)),
            patch.object(bot, "_schedule_live_account_refresh") as schedule_refresh,
        ):
            blocked = await bot._apply_live_balance_gate(object(), cfg)

        assert blocked is False
        assert bot.state.live_cash == 6.2
        assert bot.state.status == "waiting"
        assert bot.state.circuit_breaker_reason == ""
        schedule_refresh.assert_called_once()

    asyncio.run(run_check())


def test_live_balance_gate_clears_stale_balance_reason_after_recovery():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot.state = SimpleNamespace(
            live_cash=6.2,
            live_balance_ok=True,
            live_balance_error="",
            status="balance_unavailable",
            bot_status="balance_unavailable",
            trading_enabled=True,
            circuit_breaker_reason="live balance unavailable: balance fetch timeout",
        )
        cfg = SimpleNamespace()

        with (
            patch("bot_runtime.app.config.MOCK_MODE", False),
            patch("bot_runtime.app.end_window_strategy.EndWindowConfig.from_settings", return_value=SimpleNamespace(live_trade_usd=5.0)),
            patch.object(bot, "_schedule_live_account_refresh") as schedule_refresh,
        ):
            blocked = await bot._apply_live_balance_gate(object(), cfg)

        assert blocked is False
        assert bot.state.circuit_breaker_reason == ""
        schedule_refresh.assert_called_once()

    asyncio.run(run_check())


def test_live_account_refresh_scheduler_does_not_await_network_refresh():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot._last_live_account_refresh = 0.0
        bot._live_account_refresh_inflight = False
        started = asyncio.Event()
        release = asyncio.Event()

        async def refresh(_session, force=False):
            started.set()
            await release.wait()

        with (
            patch("bot_runtime.app.config.MOCK_MODE", False),
            patch("bot_runtime.app.time.time", return_value=100.0),
            patch.object(bot, "_refresh_live_account", AsyncMock(side_effect=refresh)) as refresh_live,
            patch.object(bot, "_on_bg_task_done"),
        ):
            bot._schedule_live_account_refresh(object())
            assert bot._live_account_refresh_inflight is True
            await asyncio.wait_for(started.wait(), timeout=1.0)
            refresh_live.assert_awaited_once()
            assert bot._live_account_refresh_inflight is True
            release.set()
            await asyncio.sleep(0)
            assert bot._live_account_refresh_inflight is False

    asyncio.run(run_check())


def test_entry_scan_due_only_inside_enabled_windows():
    cfg = SimpleNamespace(
        buy1_enabled=False,
        time1_enabled=True,
        time1_min_secs_left=3.0,
        time1_max_secs_left=40.0,
        time2_enabled=False,
        time3_enabled=False,
        time4_enabled=False,
        time5_enabled=False,
        time6_enabled=False,
        t1_enabled=False,
        t2_enabled=False,
        t3_enabled=False,
        t4_enabled=False,
        t5_enabled=False,
        t6_enabled=False,
    )
    strategy_cfg = SimpleNamespace(layers=(), fast_open_enabled=False)

    assert Bot._entry_scan_due(cfg, strategy_cfg, 140.0) is False
    assert Bot._entry_scan_due(cfg, strategy_cfg, 20.0) is True


def test_refresh_market_uses_configured_timeout_instead_of_10ms_clamp():
    async def run_check():
        bot = Bot.__new__(Bot)
        market = _market(up_ask=0.0, down_ask=0.0)

        class EmptyCache:
            def get_clob_full(self, _token_id):
                return None

            def get_depth(self, _token_id):
                return {"bids": [], "asks": []}

        async def refresh_prices(_session, current):
            await asyncio.sleep(0.02)
            current.up_ask = 0.91
            current.down_ask = 0.12
            return current

        with (
            patch("bot_runtime.app.get_cache", return_value=EmptyCache()),
            patch("bot_runtime.app.mkt.refresh_prices", AsyncMock(side_effect=refresh_prices)),
            patch("bot_runtime.app.MARKET_REFRESH_TIMEOUT_SECS", 0.08),
            patch("bot_runtime.app.FAST_REFRESH_TIMEOUT_SECS", 0.05),
        ):
            refreshed = await bot._refresh_market(object(), market, secs_left=20.0)

        assert refreshed.up_ask == 0.91
        assert refreshed.down_ask == 0.12

    asyncio.run(run_check())


def test_refresh_market_can_skip_network_when_entry_scan_not_due():
    async def run_check():
        bot = Bot.__new__(Bot)
        market = _market(up_ask=0.0, down_ask=0.0)

        class EmptyCache:
            def get_clob_full(self, _token_id):
                return None

            def get_depth(self, _token_id):
                return {"bids": [], "asks": []}

        with (
            patch("bot_runtime.app.get_cache", return_value=EmptyCache()),
            patch("bot_runtime.app.mkt.refresh_prices", AsyncMock()) as refresh_prices,
        ):
            refreshed = await bot._refresh_market(
                object(),
                market,
                secs_left=200.0,
                allow_network=False,
            )

        assert refreshed.up_ask == 0.0
        assert refreshed.down_ask == 0.0
        refresh_prices.assert_not_called()

    asyncio.run(run_check())


def test_market_pending_watch_recovers_current_window_market():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot.running = True
        bot.state = SimpleNamespace()
        bot._last_chainlink_age = 0.1
        bot._last_exchange_age = 0.2
        bot._last_btc_source = "chainlink"
        window_ts = int(time.time()) - 10
        market = _market(window_ts=window_ts, close_ts=window_ts + 300)

        with (
            patch.object(bot, "_btc_now", AsyncMock(return_value=62500.0)),
            patch("bot_runtime.app.mkt.fetch_market", AsyncMock(side_effect=[None, market])),
            patch("bot_runtime.app.MARKET_DISCOVERY_RETRY_INTERVAL_SECS", 0.0),
            patch("bot_runtime.app.MARKET_DISCOVERY_ATTEMPT_TIMEOUT_SECS", 0.5),
            patch.object(bot, "_update_open_legs"),
            patch.object(bot, "_save") as save_state,
        ):
            recovered = await bot._watch_window_without_market(
                object(),
                window_ts,
                window_ts + 300,
                300,
            )

        assert recovered is market
        assert bot.state.status == "market_pending"
        save_state.assert_called()

    asyncio.run(run_check())


def test_missing_target_watch_refetches_gamma_metadata():
    async def run_check():
        bot = Bot.__new__(Bot)
        bot.running = True
        bot.state = SimpleNamespace()
        bot._last_chainlink_age = 0.1
        bot._last_exchange_age = 0.2
        bot._last_btc_source = "chainlink"
        window_ts = int(time.time()) - 10
        market = _market(window_ts=window_ts, close_ts=window_ts + 300, target_price=0.0)
        fresh = _market(window_ts=window_ts, close_ts=window_ts + 300, target_price=62490.0)

        with (
            patch.object(bot, "_btc_now", AsyncMock(return_value=62500.0)),
            patch.object(bot, "_refresh_market", AsyncMock(return_value=market)),
            patch("bot_runtime.app.mkt.fetch_market", AsyncMock(return_value=fresh)),
            patch("bot_runtime.app.MISSING_TARGET_REFETCH_SECS", 0.0),
            patch("bot_runtime.app.MISSING_TARGET_FETCH_TIMEOUT_SECS", 0.5),
            patch("bot_runtime.app.set_clob_tokens"),
        ):
            recovered_open, recovered_source, recovered_market = await bot._watch_window_without_target(
                object(),
                market,
                window_ts,
                window_ts + 300,
                "missing Polymarket priceToBeat",
            )

        assert recovered_open == 62490.0
        assert recovered_source == "Polymarket priceToBeat"
        assert recovered_market is fresh

    asyncio.run(run_check())


def test_btc_now_gateio_fallback_preserves_chainlink_age():
    class FakeCache:
        btc_age = 0.2
        btc_source = "gateio"

        def source_btc(self, source, _max_age):
            return 62500.0 if source == "gateio" else None

        def source_btc_age(self, source):
            return {"chainlink": 999.0, "gateio": 0.2}.get(source, 999.0)

    async def run_check():
        bot = Bot.__new__(Bot)
        with (
            patch.dict(os.environ, {"GATEIO_WS_ENABLED": "true", "COINBASE_WS_ENABLED": "false"}),
            patch("bot_runtime.app.get_cache", return_value=FakeCache()),
        ):
            price = await bot._btc_now(object())

        assert price == 62500.0
        assert bot._last_btc_source == "gateio (fallback)"
        assert bot._last_chainlink_age == 999.0
        assert bot._last_exchange_age == 0.2

    asyncio.run(run_check())


def test_daily_profit_halt_clears_when_target_is_raised():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(trading_enabled=False)
    cfg = SimpleNamespace(daily_profit_stop_usd=250.0)
    daily = {
        "pnl": 110.39,
        "start_balance": 200.0,
        "halted": True,
        "halt_reason": "Daily profit target reached",
    }

    with (
        patch("bot_runtime.app.st.load_daily_pnl", return_value=daily),
        patch("bot_runtime.app.st.set_daily_halted") as set_halted,
        patch.object(bot, "_sync_stats") as sync_stats,
        patch.object(bot, "_save") as save_state,
    ):
        stopped = bot._apply_daily_profit_halt(cfg)

    assert stopped is False
    set_halted.assert_called_once_with(False)
    sync_stats.assert_not_called()
    save_state.assert_not_called()


def test_daily_profit_halt_ignores_cumulative_profit():
    bot = Bot.__new__(Bot)
    bot._session_pnl = 25.0
    bot.state = SimpleNamespace(
        trading_enabled=True,
        status="scanning",
        bot_status="scanning",
        total_pnl=10000.0,
        session_pnl=25.0,
    )
    daily = {"pnl": 499.99, "start_balance": 200.0, "halted": False, "halt_reason": ""}

    with (
        patch("bot_runtime.app.st.load_daily_pnl", return_value=daily),
        patch("bot_runtime.app.st.set_daily_halted") as set_halted,
        patch.object(bot, "_sync_stats") as sync_stats,
        patch.object(bot, "_save") as save_state,
    ):
        stopped = bot._apply_daily_profit_halt(SimpleNamespace(daily_profit_stop_usd=500.0))

    assert stopped is False
    assert bot.state.trading_enabled is True
    set_halted.assert_not_called()
    sync_stats.assert_not_called()
    save_state.assert_not_called()


def test_live_final_force_attempt_keeps_preflight_enabled():
    market = _market(
        close_ts=int(time.time()) + 8,
        book_ts=time.time(),
        up_ask_depth=[(0.90, 200.0)],
        down_ask_depth=[(0.11, 200.0)],
    )
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        min_trade_usd=100.0,
        force_trade=True,
        force_retry_attempts=1,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.config.MOCK_MODE", False),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner.safety.validate_market_for_entry", return_value=SimpleNamespace(ok=True, reason="ok")) as gate,
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=170.0,
            secs_elapsed=295.0,
            secs_left=5.0,
            bankroll_usd=100.0,
            cfg=cfg,
            settings=_layer_settings(),
        ))

    assert out is fake_record
    gate.assert_called_once()
    assert gate.call_args.args[2] == {"UP": 1.0}
    assert buy.await_args.kwargs["amount_usd"] == 1.0
    assert buy.await_args.kwargs["skip_preflight"] is False
    assert "FORCE_FINAL_FOK" in buy.await_args.kwargs["reason"]


def test_live_time_trigger_uses_live_budget_without_changing_mock_budget():
    market = _market(
        close_ts=int(time.time()) + 30,
        book_ts=time.time(),
        up_ask=0.98,
        up_price=0.97,
        up_ask_depth=[(0.98, 2.0)],
    )
    cfg = end_window.EndWindowConfig(
        enabled=True,
        trade_usd=100.0,
        live_trade_usd=1.0,
        min_trade_usd=100.0,
    )
    settings = SimpleNamespace(
        time1_enabled=True,
        time2_enabled=True,
        time3_enabled=True,
        time1_price=0.98,
        time2_price=0.99,
        time3_price=0.97,
        time1_trade_usd=100.0,
        time2_trade_usd=100.0,
        time3_trade_usd=100.0,
        time1_min_secs_left=3.0,
        time2_min_secs_left=3.0,
        time3_min_secs_left=3.0,
        t1_enabled=True,
        t2_enabled=True,
        t3_enabled=True,
        t4_enabled=True,
        t5_enabled=True,
        t6_enabled=True,
    )
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.config.MOCK_MODE", False),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch(
            "bot_runtime.end_window_runner.safety.validate_market_for_entry",
            return_value=SimpleNamespace(ok=True, reason="ok"),
        ),
        patch(
            "bot_runtime.end_window_runner._execute_buy",
            AsyncMock(return_value=fake_record),
        ) as buy,
    ):
        out = asyncio.run(
            end_window_runner.try_end_window_market(
                market=market,
                btc_open=100.0,
                btc_now=110.0,
                secs_elapsed=270.0,
                secs_left=30.0,
                bankroll_usd=100.0,
                cfg=cfg,
                settings=settings,
            )
        )

    assert out is fake_record
    assert buy.await_args.kwargs["amount_usd"] == 1.0
    assert "TIME-1" in buy.await_args.kwargs["reason"]


def test_live_safety_reject_prevents_order():
    market = _market(
        book_ts=time.time(),
        up_ask_depth=[(0.90, 200.0)],
        down_ask_depth=[(0.11, 200.0)],
    )
    cfg = end_window.EndWindowConfig(enabled=True, trade_usd=100.0, min_trade_usd=100.0)

    with (
        patch("bot_runtime.end_window_runner.config.MOCK_MODE", False),
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[]),
        patch("bot_runtime.end_window_runner.safety.validate_market_for_entry", return_value=SimpleNamespace(ok=False, reason="bad book")),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock()) as buy,
    ):
        out = asyncio.run(end_window_runner.try_end_window_market(
            market=market,
            btc_open=100.0,
            btc_now=170.0,
            secs_elapsed=275.0,
            secs_left=25.0,
            bankroll_usd=100.0,
            cfg=cfg,
        ))

    assert out is None
    buy.assert_not_awaited()


def test_directional_resolver_prefers_gamma_metadata_resolution():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(balance=0.0, session_pnl=0.0)
    bot._session_pnl = 0.0
    bot._close_btc = {}
    bot._last_save_state = 0.0
    window_ts = 1800000000
    trades = [
        {
            "window_ts": window_ts,
            "trigger": "END_WINDOW",
            "asset": "BTC",
            "market_slug": "btc-updown-5m-1800000000",
            "resolved": False,
            "exited_early": False,
        }
    ]
    resolved = [{"balance_returned": 12.0, "pnl": 7.0, "won": True}]

    with (
        patch("bot_runtime.app.time.time", return_value=window_ts + 301),
        patch("bot_runtime.app.st.load_trades", return_value=trades),
        patch.object(bot, "_fetch_gamma_resolution", AsyncMock(return_value={
            "actual": "DOWN",
            "final_price": 90.0,
            "price_to_beat": 100.0,
            "source": "gamma_event_metadata",
        })),
        patch.object(bot, "_fetch_window_close_btc", AsyncMock(return_value=110.0)),
        patch("bot_runtime.app.st.update_directional_results", return_value=resolved) as update_results,
        patch("bot_runtime.app.st.add_balance", return_value=112.0),
        patch("bot_runtime.app.st.update_daily_pnl"),
        patch.object(bot, "_sync_stats"),
        patch.object(bot, "_save"),
        patch("bot_runtime.app.st.load_snapshots", return_value=[]),
        patch("bot_runtime.app.analyze_market_context", return_value={
            "saturation_avg_secs_30m": 106.3,
            "locked_avg_secs_30m": 34.1,
        }),
        patch("bot_runtime.app.tg.send", AsyncMock()) as send_notification,
    ):
        out = asyncio.run(bot._resolve_directional_window(SimpleNamespace(), window_ts))

    assert out == resolved
    update_results.assert_called_once_with(
        window_ts,
        90.0,
        market_slug="btc-updown-5m-1800000000",
        triggers=("END_WINDOW",),
        actual="DOWN",
        price_to_beat=100.0,
        resolution_source="gamma_event_metadata",
    )
    send_notification.assert_awaited_once()
    assert "Trading result:" in send_notification.await_args.args[0]
    assert "30m Saturation 0.94: 106.3s" in send_notification.await_args.args[0]


def test_directional_resolver_does_not_stop_on_window_or_cumulative_profit():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(
        balance=0.0,
        session_pnl=999.0,
        total_pnl=999.0,
        trading_enabled=True,
        status="scanning",
        bot_status="scanning",
    )
    bot._session_pnl = 999.0
    bot._close_btc = {}
    bot._last_save_state = 0.0
    window_ts = 1800000000
    trades = [
        {
            "window_ts": window_ts,
            "trigger": "END_WINDOW",
            "asset": "BTC",
            "market_slug": "btc-updown-5m-1800000000",
            "resolved": False,
            "exited_early": False,
        }
    ]
    resolved = [{"balance_returned": 2.0, "pnl": 1.5, "won": True}]

    with (
        patch("bot_runtime.app.time.time", return_value=window_ts + 301),
        patch("bot_runtime.app.st.load_trades", return_value=trades),
        patch.object(bot, "_fetch_gamma_resolution", AsyncMock(return_value={
            "actual": "DOWN",
            "final_price": 90.0,
            "price_to_beat": 100.0,
            "source": "gamma_event_metadata",
        })),
        patch.object(bot, "_fetch_window_close_btc", AsyncMock(return_value=110.0)),
        patch("bot_runtime.app.st.update_directional_results", return_value=resolved),
        patch("bot_runtime.app.st.add_balance"),
        patch("bot_runtime.app.st.update_daily_pnl"),
        patch("bot_runtime.app.st.load_settings", return_value=SimpleNamespace(
            daily_profit_stop_usd=0.0,
            trade_amount=100.0,
            max_trades_per_window=9,
        )),
        patch.object(bot, "_apply_daily_profit_halt", return_value=False) as daily_halt,
        patch.object(bot, "_sync_stats"),
        patch.object(bot, "_save"),
        patch("bot_runtime.app.st.load_snapshots", return_value=[]),
        patch("bot_runtime.app.analyze_market_context", return_value={}),
        patch("bot_runtime.app.tg.send", AsyncMock()),
    ):
        out = asyncio.run(bot._resolve_directional_window(SimpleNamespace(), window_ts))

    assert out == resolved
    assert bot.state.session_pnl == 1000.5
    assert bot.state.trading_enabled is True
    daily_halt.assert_called_once()


def test_directional_resolver_defers_when_gamma_outcome_is_unavailable():
    bot = Bot.__new__(Bot)
    window_ts = 1800000000
    trades = [
        {
            "window_ts": window_ts,
            "trigger": "END_WINDOW",
            "market_slug": "btc-updown-5m-1800000000",
            "resolved": False,
            "exited_early": False,
        }
    ]

    with (
        patch("bot_runtime.app.time.time", return_value=window_ts + 301),
        patch("bot_runtime.app.st.load_trades", return_value=trades),
        patch.object(bot, "_fetch_gamma_resolution", AsyncMock(return_value=None)),
        patch.object(bot, "_fetch_window_close_btc", AsyncMock(return_value=90.0)) as fetch_close,
        patch("bot_runtime.app.st.update_directional_results") as update_results,
    ):
        out = asyncio.run(bot._resolve_directional_window(SimpleNamespace(), window_ts))

    assert out == []
    fetch_close.assert_not_awaited()
    update_results.assert_not_called()


def test_directional_resolver_uses_chainlink_close_after_gamma_delay():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(balance=0.0, session_pnl=0.0)
    bot._session_pnl = 0.0
    bot._close_btc = {1800000000: 105.0}
    bot._last_save_state = 0.0
    window_ts = 1800000000
    trades = [
        {
            "window_ts": window_ts,
            "trigger": "END_WINDOW",
            "market_slug": "btc-updown-5m-1800000000",
            "outcome": "UP",
            "btc_open": 100.0,
            "resolved": False,
            "exited_early": False,
        }
    ]
    resolved = [{"balance_returned": 11.0, "pnl": 1.0, "won": True}]

    with (
        patch("bot_runtime.app.time.time", return_value=window_ts + 370),
        patch("bot_runtime.app.get_cache", return_value=SimpleNamespace(source_btc_at_time=lambda *_args, **_kwargs: None)),
        patch("bot_runtime.app.st.load_trades", return_value=trades),
        patch.object(bot, "_fetch_gamma_resolution", AsyncMock(return_value=None)),
        patch("bot_runtime.app.st.update_directional_results", return_value=resolved) as update_results,
        patch("bot_runtime.app.st.add_balance"),
        patch("bot_runtime.app.st.update_daily_pnl"),
        patch("bot_runtime.app.st.load_settings", return_value=SimpleNamespace(daily_profit_stop_usd=0.0)),
        patch.object(bot, "_apply_daily_profit_halt", return_value=False),
        patch.object(bot, "_sync_stats"),
        patch.object(bot, "_save"),
        patch("bot_runtime.app.st.load_snapshots", return_value=[]),
        patch("bot_runtime.app.analyze_market_context", return_value={}),
        patch("bot_runtime.app.tg.send", AsyncMock()),
    ):
        out = asyncio.run(bot._resolve_directional_window(SimpleNamespace(), window_ts))

    assert out == resolved
    update_results.assert_called_once_with(
        window_ts,
        105.0,
        market_slug="btc-updown-5m-1800000000",
        triggers=("END_WINDOW",),
        actual="UP",
        price_to_beat=100.0,
        resolution_source="chainlink_close_fallback",
    )


def test_gamma_resolution_falls_back_to_implied_after_official_metadata_miss():
    bot = Bot.__new__(Bot)
    implied = {
        "actual": "UP",
        "final_price": 0.0,
        "price_to_beat": 0.0,
        "source": "gamma_outcome_prices",
    }

    with patch(
        "bot_runtime.app.mkt.fetch_resolution",
        AsyncMock(side_effect=[None, implied]),
    ) as fetch_resolution:
        out = asyncio.run(
            bot._fetch_gamma_resolution(
                SimpleNamespace(),
                "btc-updown-5m-1800000000",
                "retry-official",
            )
        )

    assert out == implied
    assert fetch_resolution.await_args_list[0].kwargs["allow_implied"] is False
    assert fetch_resolution.await_args_list[1].kwargs["allow_implied"] is True


def test_telegram_trading_command_starts_with_safety_gate():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(trading_enabled=False, status="waiting", bot_status="waiting")

    with (
        patch.object(bot, "_apply_safety_gate", return_value=True) as safety_gate,
        patch("bot_runtime.app.st.set_emergency_stop") as emergency,
        patch("bot_runtime.app.st.set_trading_enabled") as set_trading,
        patch.object(bot, "_save") as save_state,
        patch("bot_runtime.app.config.MOCK_MODE", True),
    ):
        response = asyncio.run(bot._handle_telegram_command("/trading"))

    assert response == "Trading started\nStatus trading: MOCK"
    emergency.assert_called_once_with(False)
    safety_gate.assert_called_once_with("telegram_safety_block")
    set_trading.assert_called_once_with(True)
    assert bot.state.trading_enabled is True
    save_state.assert_called_once_with(force=True)


def test_telegram_stop_command_stops_trading():
    bot = Bot.__new__(Bot)
    bot.state = SimpleNamespace(trading_enabled=True, status="watching", bot_status="watching")

    with (
        patch("bot_runtime.app.st.set_trading_enabled") as set_trading,
        patch.object(bot, "_save") as save_state,
    ):
        response = asyncio.run(bot._handle_telegram_command("/stop"))

    assert response == "Trading stopped"
    set_trading.assert_called_once_with(False)
    assert bot.state.trading_enabled is False
    assert bot.state.status == "waiting"
    save_state.assert_called_once_with(force=True)
