import pytest

from bot_runtime.notifications import (
    format_open_positions,
    format_runtime_status,
    format_trading_result,
)


def test_format_trading_result_uses_window_group_totals():
    trades = [
        {
            "timestamp": 2.0,
            "market_slug": "btc-updown-5m-1781574000",
            "entry_price": 0.95,
            "pnl": 2.0,
        },
        {
            "timestamp": 1.0,
            "market_slug": "btc-updown-5m-1781574000",
            "entry_price": 0.94,
            "pnl": 8.7,
        },
    ]
    context = {
        "saturation_avg_secs_30m": 106.3,
        "locked_avg_secs_30m": 34.1,
    }
    state = {
        "mock_mode": True,
        "total_pnl": 1127.7,
        "daily_pnl": 30.7,
    }

    message = format_trading_result(trades, context, state)

    assert message == "\n".join([
        "Trading result:",
        "Link Window BTC: https://polymarket.com/id/event/btc-updown-5m-1781574000",
        "Status trading: MOCK",
        "Trade: 2 (0.94, 0.95)",
        "Status Result: WIN",
        "PnL: $10.7",
        "Total PnL: $1127.7",
        "Today PnL: $30.7",
        "30m Saturation 0.94: 106.3s",
        "30m Locked N/A: 34.1s",
    ])


def test_format_trading_result_requires_trades():
    with pytest.raises(ValueError):
        format_trading_result([])


def test_format_open_positions_lists_unresolved_end_window_trades():
    message = format_open_positions([
        {
            "trigger": "END_WINDOW",
            "resolved": False,
            "exited_early": False,
            "timestamp": 1.0,
            "window_ts": 1781574000,
            "market_slug": "btc-updown-5m-1781574000",
            "outcome": "UP",
            "entry_price": 0.94,
            "amount_usd": 100.0,
            "shares": 106.38,
        }
    ], {"current_window": 1781574000, "up_price": 0.97})

    assert "Open position:" in message
    assert "UP | entry 0.94 | amount $100 | shares 106.38 | now 0.97" in message
    assert "https://polymarket.com/id/event/btc-updown-5m-1781574000" in message


def test_format_runtime_status_shows_mode_and_pnl():
    message = format_runtime_status({
        "mock_mode": True,
        "trading_enabled": True,
        "status": "watching",
        "total_pnl": 1127.7,
        "daily_pnl": 30.7,
    })

    assert "Status trading: MOCK" in message
    assert "Trading: RUNNING" in message
    assert "Total PnL: $1127.7" in message
    assert "Today PnL: $30.7" in message
