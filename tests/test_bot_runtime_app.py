import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot_runtime import end_window_runner
from bot_runtime.app import Bot
from core.market import BTCMarket
from strategies import end_window


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
def test_end_window_buys_hundred_dollar_reverse_at_forty_cents_for_every_strategy(source_reason):
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
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
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

    assert out is fake_record
    assert buy.await_args.kwargs["outcome"] == "DOWN"
    assert buy.await_args.kwargs["amount_usd"] == 100.0
    assert buy.await_args.kwargs["price"] == 0.40
    assert "REVERSE:" in buy.await_args.kwargs["reason"]


def test_end_window_reverse_is_extra_slot_after_normal_trade_cap():
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
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=normal_trades),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
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

    assert out is fake_record
    assert buy.await_args.kwargs["price"] == 0.40
    assert buy.await_args.kwargs["amount_usd"] == 100.0
    assert buy.await_args.kwargs["strict_price"] is True


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


def test_end_window_buys_reverse_1_when_direction_flips_back():
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
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial, reverse]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
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

    assert out is fake_record
    assert buy.await_args.kwargs["outcome"] == "UP"
    assert buy.await_args.kwargs["amount_usd"] == 100.0
    assert buy.await_args.kwargs["price"] == 0.40
    assert "REVERSE-1:" in buy.await_args.kwargs["reason"]
    assert "source=REVERSE" in buy.await_args.kwargs["reason"]
    assert "source_ref=order-reverse-down" in buy.await_args.kwargs["reason"]


def test_end_window_buys_only_one_first_reverse_per_window():
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

    assert len(records) == 1
    assert buy.await_count == 1
    assert [call.kwargs["amount_usd"] for call in buy.await_args_list] == [50.0]
    reasons = [call.kwargs["reason"] for call in buy.await_args_list]
    assert "source_ref=order-source-1" in reasons[0]


def test_end_window_reverse_rejects_price_above_forty_cents():
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
        patch("bot_runtime.end_window_runner._log_reverse_skip") as reverse_skip,
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
    reverse_skip.assert_called_once_with(
        market.slug,
        "ask_above_cap: outcome=DOWN ask=0.4100 cap=0.4000",
    )


def test_end_window_buys_reverse_below_forty_cents():
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
    fake_record = SimpleNamespace(trigger="END_WINDOW")

    with (
        patch("bot_runtime.end_window_runner.st.load_trades", return_value=[initial]),
        patch("bot_runtime.end_window_runner._execute_buy", AsyncMock(return_value=fake_record)) as buy,
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

    assert out is fake_record
    assert buy.await_args.kwargs["outcome"] == "DOWN"
    assert buy.await_args.kwargs["price"] == 0.39
    assert "cap=0.4000" in buy.await_args.kwargs["reason"]


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
        market_15m_enabled=True,
        arb15_enabled=True,
        arb15_price=0.43,
        arb15_trade_usd=100.0,
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


def test_btc_5m_arb_settings_do_not_fire_arbitrage():
    market = _market(
        close_ts=1800000300,
        up_ask=0.43,
        down_ask=0.44,
        up_ask_depth=[(0.43, 10.0)],
        down_ask_depth=[(0.44, 300.0)],
    )
    settings = end_window_runner.st.BotSettings(
        market_5m_enabled=True,
        market_15m_enabled=False,
        arb5_enabled=True,
        arb5_price=0.43,
        arb5_trade_usd=100.0,
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


def test_btc_5m_arb_settings_fall_through_to_time():
    market = _market(
        close_ts=1800000300,
        up_ask=0.90,
        down_ask=0.60,
        up_ask_depth=[(0.90, 300.0)],
        down_ask_depth=[(0.60, 300.0)],
    )
    settings = end_window_runner.st.BotSettings(
        market_5m_enabled=True,
        market_15m_enabled=False,
        arb5_enabled=True,
        arb5_price=0.43,
        arb5_trade_usd=100.0,
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
        market_15m_enabled=False,
        arb5_enabled=True,
        arb5_price=0.43,
        arb5_trade_usd=100.0,
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
        patch("bot_runtime.app.get_cache", return_value=cache),
        patch("bot_runtime.app.mkt.get_btc_price", AsyncMock(return_value=99.0)) as fetch_price,
    ):
        price = asyncio.run(bot._btc_now(None))

    assert price == 100.0
    assert bot._last_btc_source == "gateio (fallback)"
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
        source_btc_at_time=lambda source, timestamp, max_drift: (64_123.45, timestamp + 0.2, 0.2)
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
        source_btc_at_time=lambda source, timestamp, max_drift: (
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
