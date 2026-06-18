import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import core.trader as trader


def test_market_info_uses_token_orderbook_minimum():
    client = SimpleNamespace(
        get_order_book=lambda token_id: {
            "tick_size": "0.01",
            "min_order_size": "5",
        }
    )
    trader._market_info_cache.clear()

    info = asyncio.run(trader._get_market_info(client, "token-up"))

    assert info == {"tick_size": "0.01", "min_order_size": 5.0}


def test_live_buy_uses_dollar_market_order_path():
    result = trader.TradeResult(success=True, size=1.0, size_matched=1.0)

    with (
        patch.object(trader.config, "MOCK_MODE", False),
        patch.object(trader, "HAS_SDK", True),
        patch.object(trader, "_get_clob_client", return_value=object()),
        patch.object(
            trader,
            "_get_market_info",
            AsyncMock(return_value={"tick_size": "0.01", "min_order_size": 1.0}),
        ),
        patch.object(trader, "_buy_sdk_fok", AsyncMock(return_value=result)) as buy,
    ):
        out = asyncio.run(
            trader.execute_buy(
                "token-up",
                "UP",
                0.99,
                1.0,
                "condition",
                allow_partial=False,
            )
        )

    assert out is result
    buy.assert_awaited_once()


def test_live_market_buy_is_not_blocked_by_limit_order_share_minimum():
    result = trader.TradeResult(success=True, size=1.01, size_matched=1.01)

    with (
        patch.object(trader.config, "MOCK_MODE", False),
        patch.object(trader, "HAS_SDK", True),
        patch.object(trader, "_get_clob_client", return_value=object()),
        patch.object(
            trader,
            "_get_market_info",
            AsyncMock(return_value={"tick_size": "0.01", "min_order_size": 5.0}),
        ),
        patch.object(trader, "_buy_sdk_fok", AsyncMock(return_value=result)) as buy,
    ):
        out = asyncio.run(
            trader.execute_buy(
                "token-up",
                "UP",
                0.99,
                1.0,
                "condition",
                allow_partial=False,
            )
        )

    assert out is result
    buy.assert_awaited_once()


def test_live_buy_builds_market_order_with_dollar_amount_and_price_cap():
    client = SimpleNamespace(
        calculate_market_price=Mock(return_value=0.99),
        create_and_post_market_order=Mock(
            return_value={
                "success": True,
                "status": "matched",
                "orderID": "order-1",
                "makingAmount": "1.00",
                "takingAmount": "1.0101",
            }
        ),
    )

    result = asyncio.run(
        trader._buy_sdk_fok(
            client,
            "token-up",
            "UP",
            0.99,
            1.0,
            "0.01",
            strict_price=True,
            allow_partial=False,
        )
    )

    assert result.success
    assert result.size == 1.0101
    assert round(result.price, 4) == 0.99
    order_args = client.create_and_post_market_order.call_args.kwargs["order_args"]
    assert order_args.amount == 1.0
    assert order_args.price == 0.99
    assert order_args.side == trader.Side.BUY


def test_mock_buy_does_not_use_live_minimum_lookup():
    with (
        patch.object(trader.config, "MOCK_MODE", True),
        patch.object(trader, "_get_market_info", AsyncMock()) as market_info,
    ):
        out = asyncio.run(
            trader.execute_buy(
                "mock_up",
                "UP",
                0.99,
                1.0,
                mock_fill=(0.99, 1.0),
            )
        )

    assert out.success
    assert out.mock
    assert out.size == 1.0
    market_info.assert_not_awaited()
