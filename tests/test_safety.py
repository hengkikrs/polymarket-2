import time
from types import SimpleNamespace
from unittest.mock import patch

import core.config as config
import core.safety as safety


def cfg(**overrides):
    base = dict(
        max_trades_per_window=1,
        trade_amount=100.0,
        cb_master_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def market(**overrides):
    data = dict(
        slug="btc-updown-5m-1800000000",
        window_ts=1800000000,
        close_ts=1800000300,
        up_token="1" * 30,
        down_token="2" * 30,
        up_price=0.50,
        down_price=0.49,
        up_ask=0.51,
        down_ask=0.50,
        up_ask_depth=[(0.51, 300.0)],
        down_ask_depth=[(0.50, 300.0)],
        active=True,
        closed=False,
        archived=False,
        accepting_orders=True,
        book_ts=time.time(),
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_startup_safety_allows_mock_high_trade_size():
    scfg = safety.SafetyConfig(max_live_trade_usd=5.0)
    with patch.object(config, "MOCK_MODE", True):
        decision = safety.startup_safety_report(cfg(trade_amount=100.0), scfg)

    assert decision.ok


def test_startup_safety_blocks_live_without_confirm():
    scfg = safety.SafetyConfig(max_live_trade_usd=5.0)
    with (
        patch.object(config, "MOCK_MODE", False),
        patch.dict(
            "os.environ",
            {"LIVE_TRADING_CONFIRM": "", "END_WINDOW_LIVE_TRADE_USD": "100"},
        ),
    ):
        decision = safety.startup_safety_report(cfg(trade_amount=100.0), scfg)

    assert not decision.ok
    assert "LIVE_TRADING_CONFIRM" in decision.reason
    assert "live_trade_usd" in decision.reason


def test_validate_market_rejects_stale_or_missing_orderbook():
    scfg = safety.SafetyConfig(require_orderbook_in_paper=True, max_book_age_secs=8.0)

    decision = safety.validate_market_for_entry(
        market(book_ts=time.time() - 60),
        ["UP"],
        {"UP": 100.0},
        live=False,
        scfg=scfg,
    )

    assert not decision.ok
    assert "orderbook stale" in decision.reason


def test_validate_market_rejects_thin_liquidity():
    scfg = safety.SafetyConfig(require_orderbook_in_paper=True)

    decision = safety.validate_market_for_entry(
        market(up_ask_depth=[(0.51, 1.0)]),
        ["UP"],
        {"UP": 100.0},
        live=False,
        scfg=scfg,
    )

    assert not decision.ok
    assert "liquidity" in decision.reason


def test_validate_market_accepts_clean_book():
    scfg = safety.SafetyConfig(require_orderbook_in_paper=True, max_spread=0.05)

    decision = safety.validate_market_for_entry(
        market(),
        ["UP"],
        {"UP": 100.0},
        live=False,
        scfg=scfg,
    )

    assert decision.ok, decision.reason


def test_validate_market_accepts_clean_btc_15m_book():
    scfg = safety.SafetyConfig(require_orderbook_in_paper=True, max_spread=0.05)

    decision = safety.validate_market_for_entry(
        market(
            slug="btc-updown-15m-1800000000",
            close_ts=1800000900,
        ),
        ["UP"],
        {"UP": 100.0},
        live=False,
        scfg=scfg,
    )

    assert decision.ok, decision.reason


def test_live_entry_enforces_trade_and_window_exposure_caps():
    scfg = safety.SafetyConfig(
        max_live_trade_usd=5.0,
        max_live_window_exposure_usd=11.0,
        max_spread=0.05,
    )

    oversized = safety.validate_market_for_entry(
        market(), ["UP"], {"UP": 100.0}, live=True, scfg=scfg,
    )
    cumulative = safety.validate_market_for_entry(
        market(), ["UP"], {"UP": 5.0}, live=True, scfg=scfg,
        existing_window_exposure_usd=10.0,
    )

    assert not oversized.ok
    assert "live cap" in oversized.reason
    assert not cumulative.ok
    assert "window exposure" in cumulative.reason
