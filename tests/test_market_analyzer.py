import time

from strategies.market_analyzer import AnalyzerConfig, analyze_market


def market(**overrides):
    now = time.time()
    base = {
        "MARKET_ID": "btc-updown-5m-1800000000",
        "TIME_REMAINING": 120,
        "TARGET_PRICE": 105000,
        "CURRENT_ASSET_PRICE": 105010,
        "UP_ASK_PRICE": 0.47,
        "DOWN_ASK_PRICE": 0.47,
        "UP_BID_PRICE": 0.46,
        "DOWN_BID_PRICE": 0.46,
        "AVAILABLE_CAPITAL": 500,
        "CAPITAL_PER_LEG": 100,
        "ORDERBOOK_LIQUIDITY_UP": 500,
        "ORDERBOOK_LIQUIDITY_DOWN": 500,
        "SPREAD_UP": 0.01,
        "SPREAD_DOWN": 0.01,
        "PRICE_FEED_TIMESTAMP": now,
        "CURRENT_TIME": now,
        "RECENT_VOLATILITY": 0.10,
        "RECENT_PRICE_DIRECTION": "UP",
    }
    base.update(overrides)
    return base


def locked_position(**overrides):
    base = {
        "POSITION_EXISTS": True,
        "INITIAL_UP_PRICE": 0.47,
        "INITIAL_DOWN_PRICE": 0.47,
        "SHARES_UP": 212.7659574468,
        "SHARES_DOWN": 212.7659574468,
        "CAPITAL_UP": 100,
        "CAPITAL_DOWN": 100,
        "CURRENT_UP_PRICE": 0.85,
        "CURRENT_DOWN_PRICE": 0.15,
        "CURRENT_UP_BID": 0.85,
        "CURRENT_DOWN_BID": 0.15,
    }
    base.update(overrides)
    return base


def test_entry_strong_buy_balances_to_smallest_share_count():
    result = analyze_market(market())

    assert result.recommended_action == "STRONG_BUY_BOTH_LEGS"
    assert result.total_price == 0.94
    assert result.shares_up == result.shares_down
    assert round(result.locked_profit, 3) == 12.766
    assert round(result.locked_roi, 4) == 0.0638


def test_entry_above_aggressive_cap_skips():
    result = analyze_market(market(UP_ASK_PRICE=0.49, DOWN_ASK_PRICE=0.49))

    assert result.recommended_action == "SKIP_ENTRY"
    assert "total price" in result.reason


def test_locked_position_holds_before_end_window():
    result = analyze_market(market(TIME_REMAINING=45), locked_position())

    assert result.recommended_action == "HOLD_LOCKED"
    assert "time remaining" in result.reason


def test_locked_position_sells_loss_leg_when_confidence_is_85():
    result = analyze_market(
        market(
            TIME_REMAINING=28,
            CURRENT_ASSET_PRICE=105045,
            RECENT_PRICE_DIRECTION="UP",
        ),
        locked_position(),
    )

    assert result.win_leg == "UP"
    assert result.loss_leg == "DOWN"
    assert result.confidence_score == 85
    assert result.recommended_action == "SELL_LOSS_ONLY"
    assert result.sell_loss_percent == 0.5


def test_locked_position_adds_win_leg_when_confidence_and_risk_allow():
    result = analyze_market(
        market(
            TIME_REMAINING=28,
            CURRENT_ASSET_PRICE=105065,
            RECENT_PRICE_DIRECTION="UP",
        ),
        locked_position(),
        AnalyzerConfig(max_worst_case_loss_ratio=1.10),
    )

    assert result.confidence_score >= 90
    assert result.recommended_action == "SELL_LOSS_AND_ADD_WIN"
    assert result.additional_capital > 0
