import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import core.state as st
import web.dashboard as dash
from web.dashboard import (
    _build_pnl_calendar,
    _end_window_settings,
    _low_price_winner_stats,
    _pnl_history,
    _price_extreme_stats,
    _recent_trades,
    _state_payload,
    _trade_fire_layer,
    api_control,
    api_health,
    api_pnl_calendar,
    api_settings_post,
    index,
)


def _ts(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, 0, 0).timestamp()


def _utc_ts(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc).timestamp()


def _bangkok_date(ts: float | None = None) -> str:
    if ts is None:
        return "2026-06-28"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(
        timezone(timedelta(hours=7))
    ).strftime("%Y-%m-%d")


def _cell(calendar_data: dict, date_key: str) -> dict:
    for week in calendar_data["weeks"]:
        for day in week:
            if day["date"] == date_key:
                return day
    raise AssertionError(f"missing calendar cell {date_key}")


def _empty_stats() -> dict:
    return {
        "trades_total": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "win_rate": 0.0,
        "total_wagered": 0.0,
        "recent_win_rate": 0.0,
    }


def test_pnl_calendar_groups_resolved_pnl_by_day_and_ignores_open_trades():
    trades = [
        {"timestamp": _ts(2026, 5, 15), "resolved": True, "won": True, "pnl": 4.52},
        {"timestamp": _ts(2026, 5, 15, 13), "resolved": True, "won": False, "pnl": -1.0},
        {"timestamp": _ts(2026, 5, 15, 14), "resolved": False, "won": True, "pnl": 99.0},
        {"timestamp": _ts(2026, 5, 21), "resolved": True, "won": False, "pnl": -92.13},
        {"timestamp": _ts(2026, 4, 30), "resolved": True, "won": True, "pnl": 50.0},
        {"timestamp": _ts(2026, 6, 1), "resolved": True, "won": True, "pnl": 25.0},
    ]

    cal = _build_pnl_calendar(trades, 2026, 5)

    assert cal["month_pnl"] == -88.61
    assert cal["trading_days"] == 2
    assert cal["winning_days"] == 1
    assert cal["losing_days"] == 1

    may_15 = _cell(cal, "2026-05-15")
    assert may_15["pnl"] == 3.52
    assert may_15["trade_count"] == 2
    assert may_15["wins"] == 1
    assert may_15["losses"] == 1

    may_21 = _cell(cal, "2026-05-21")
    assert may_21["pnl"] == -92.13
    assert may_21["trade_count"] == 1

    apr_30 = _cell(cal, "2026-04-30")
    assert apr_30["in_month"] is False
    assert apr_30["trade_count"] == 0


def test_pnl_calendar_counts_flat_trading_day_without_win_or_loss_day():
    trades = [
        {"timestamp": _ts(2026, 5, 22), "resolved": True, "won": None, "pnl": 0.0},
    ]

    cal = _build_pnl_calendar(trades, 2026, 5)

    assert cal["month_pnl"] == 0.0
    assert cal["trading_days"] == 1
    assert cal["winning_days"] == 0
    assert cal["losing_days"] == 0
    assert _cell(cal, "2026-05-22")["trade_count"] == 1


def test_pnl_calendar_includes_today_summary_for_share_card():
    trades = [
        {"timestamp": _ts(2026, 5, 23, 9), "resolved": True, "won": True, "pnl": 12.5},
        {"timestamp": _ts(2026, 5, 23, 10), "resolved": True, "won": False, "pnl": -2.0},
    ]

    with patch("web.dashboard.time.time", return_value=_ts(2026, 5, 23, 11)):
        cal = _build_pnl_calendar(trades, 2026, 5)

    assert cal["today"] == {
        "date": "2026-05-23",
        "pnl": 10.5,
        "trade_count": 2,
        "wins": 1,
        "losses": 1,
    }


def test_pnl_calendar_uses_trading_timezone_for_day_boundary(monkeypatch):
    monkeypatch.setenv("POLY_DAILY_TIMEZONE", "Asia/Bangkok")
    trades = [
        {"timestamp": _utc_ts(2026, 6, 27, 16, 30), "resolved": True, "won": True, "pnl": 99.0},
        {"timestamp": _utc_ts(2026, 6, 27, 18, 30), "resolved": True, "won": True, "pnl": 2.0},
    ]

    with (
        patch("web.dashboard.st._daily_date", side_effect=_bangkok_date),
        patch("web.dashboard.time.time", return_value=_utc_ts(2026, 6, 27, 19, 0)),
    ):
        cal = _build_pnl_calendar(trades, 2026, 6)

    assert _cell(cal, "2026-06-27")["pnl"] == 99.0
    assert _cell(cal, "2026-06-28")["pnl"] == 2.0
    assert cal["today"] == {
        "date": "2026-06-28",
        "pnl": 2.0,
        "trade_count": 1,
        "wins": 1,
        "losses": 0,
    }


def test_recent_trades_returns_latest_thousand_newest_first():
    trades = [
        {"timestamp": float(index), "pnl": float(index), "resolution_source": "gamma"}
        for index in range(1175)
    ]

    rows = _recent_trades(trades)

    assert len(rows) == 1000
    assert rows[0]["timestamp"] == 1174.0
    assert rows[-1]["timestamp"] == 175.0
    assert rows[0]["resolution_source"] == "gamma"


def test_recent_trades_exposes_entry_and_resolved_btc_delta():
    rows = _recent_trades([{
        "timestamp": 1.0,
        "btc_open": 64_000.0,
        "btc_at_entry": 64_025.0,
        "btc_at_close": 63_990.0,
        "resolution_price_to_beat": 64_005.0,
        "btc_distance": 25.0,
        "resolved": True,
    }])

    assert rows[0]["btc_delta_entry"] == 20.0
    assert rows[0]["btc_delta_resolved"] == -15.0


def test_recent_trades_prefers_official_target_for_entry_delta():
    rows = _recent_trades([{
        "timestamp": 1.0,
        "btc_open": 64_000.0,
        "btc_at_entry": 64_025.0,
        "resolution_price_to_beat": 64_005.0,
        "btc_distance": 25.0,
    }])

    assert rows[0]["btc_delta_entry"] == 20.0


def test_recent_trades_reports_first_spread_na_side_after_price_gate():
    trades = [{
        "timestamp": 10.0,
        "window_ts": 1000,
        "outcome": "UP",
        "resolved": False,
    }]
    snapshots = [
        {"window_ts": 1000, "timestamp": 1.0, "secs_left": 9.0, "up_price": 0.98, "up_spread": None},
        {"window_ts": 1000, "timestamp": 2.0, "secs_left": 8.0, "up_price": 0.99, "up_spread": 0.001},
        {"window_ts": 1000, "timestamp": 3.0, "secs_left": 7.0, "up_price": 0.99, "up_spread": None},
        {"window_ts": 1000, "timestamp": 4.0, "secs_left": 6.0, "down_price": 1.0, "down_spread": None},
    ]

    rows = _recent_trades(trades, snapshots)

    assert rows[0]["first_spread_na"] == {
        "secs_left": 7.0,
        "timestamp": 3.0,
        "side": "UP",
        "sides": ["UP"],
    }


def test_recent_trades_labels_removed_reverse_by_source_strategy():
    trade = {
        "trigger_reason": "END_WINDOW DOWN REVERSE: initial=UP source=TIME-3",
        "secs_left": 2.0,
    }

    assert _trade_fire_layer(trade) == "TIME-3"


def test_recent_trades_labels_buy1_strategy():
    trade = {
        "trigger_reason": "BUY-1 UP: quick buy ask=0.5500",
        "secs_left": 120.0,
    }

    assert _trade_fire_layer(trade) == "BUY-1"


def test_recent_trades_treats_legacy_arb_as_plain_layer():
    trade = {
        "trigger_reason": "END_WINDOW DOWN ARB5-DOWN: leg=2/2",
        "secs_left": 120.0,
    }

    assert _trade_fire_layer(trade) == "N/A"


def test_dashboard_exposes_dynamic_market_link():
    settings = _end_window_settings(st.BotSettings())
    html = asyncio.run(index(None)).text

    assert settings["market_5m_enabled"] is True
    assert settings["max_trades_per_window"] == 9
    assert "const TRADE_PAGE_SIZE = 100;" in html
    assert "arb15_price" not in html
    assert "arb5_price" not in html
    assert "BTC 15m" not in html
    assert "Buy when <= (s)" in html
    assert "Min delta $" in html
    assert settings["time_min_delta_usd"] == 3.0
    assert settings["time1_enabled"] is True
    assert settings["time1_price"] == 0.98
    assert settings["time1_min_delta_usd"] == 3.0
    assert settings["time2_enabled"] is True
    assert settings["time2_price"] == 0.99
    assert settings["time1_min_secs_left"] == 3.0
    assert settings["time3_enabled"] is True
    assert settings["time3_price"] == 0.97
    assert settings["time4_enabled"] is True
    assert settings["time4_price"] == 0.96
    assert settings["time5_enabled"] is True
    assert settings["time5_price"] == 0.95
    assert settings["time6_enabled"] is True
    assert settings["time6_price"] == 0.94
    assert settings["buy1_enabled"] is True
    assert settings["buy1_min_price"] == 0.50
    assert settings["buy1_max_price"] == 0.60
    assert settings["buy1_sell_min_price"] == 0.80
    assert settings["buy1_sell_max_price"] == 0.90
    assert "BUY-1" in html
    assert "buy1_min_price" in html
    assert "currentMarketLink" in html
    assert "upLiquidity" in html
    assert "Low Price Winner Research" in html
    assert 'id="marketRegime"' in html
    assert 'id="delta10s"' in html
    assert 'id="saturationTiming"' in html
    assert "label:`TIME-${index}`" in html
    assert "const timeDefaults={1:.98,2:.99,3:.97,4:.96,5:.95,6:.94};" in html
    assert "decisionChecklist" in html
    assert "const timeStates=[1,2,3,4,5,6]" in html
    assert ".sort((a,b)=>a.price-b.price||a.index-b.index)" in html
    assert "const timeEditors=[1,2,3,4,5,6]" in html
    assert "Market saturated: unavailable side shown as N/A" in html
    assert "navigator.wakeLock.request('screen')" in html
    assert "visibilitychange" in html
    assert "pointerdown',keepScreenAwake" in html
    assert "const TRADE_PAGE_SIZE = 100;" in html
    assert "const RESEARCH_PAGE_SIZE = 10" in html
    assert 'id="tradePagination"' in html
    assert 'id="lowWinnerPagination"' in html


def test_health_endpoint_exposes_non_secret_runtime_status():
    with (
        patch("web.dashboard.st.load_state", return_value={"last_update": 123.0, "current_window": 456}),
        patch("web.dashboard.st.get_trading_enabled", return_value=True),
        patch("web.dashboard.st.get_emergency_stop", return_value=False),
        patch("web.dashboard.config.FUNDER", ""),
        patch("web.dashboard.config.PRIVATE_KEY", "0x" + "1".zfill(64)),
    ):
        response = asyncio.run(api_health(None))

    data = json.loads(response.text)
    assert data["ok"] is True
    assert data["service"] == "poly-v3-dashboard"
    assert data["trading_enabled"] is True
    assert data["emergency_stop"] is False
    assert data["polymarket_account"].startswith("0x")
    assert "POLYMARKET_PRIVATE_KEY" not in response.text
    assert "0000000000000000000000000000000000000000000000000000000000000001" not in response.text


def test_removed_market_settings_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "SETTINGS_FILE", tmp_path / "settings.json")

    settings = st.update_settings({"market_15m_enabled": True})
    assert not hasattr(settings, "market_15m_enabled")
    assert settings.market_5m_enabled is True

    settings = st.update_settings({"market_5m_enabled": True})
    assert settings.market_5m_enabled is True
    assert not hasattr(settings, "market_15m_enabled")


def test_settings_post_syncs_market_choice_to_active_state(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(st, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(st, "STATE_FILE", state_file)
    st.save_state(st.BotState(active_settings=st.asdict(st.BotSettings())))

    async def _json():
        return {"market_15m_enabled": True, "daily_profit_stop_usd": 500.0}

    request = SimpleNamespace(headers={}, rel_url=SimpleNamespace(query={}), json=_json)
    response = asyncio.run(api_settings_post(request))
    payload = json.loads(response.text)
    state_data = st.load_state()

    assert payload["success"] is True
    assert "market_15m_enabled" not in payload["settings"]
    assert payload["settings"]["daily_profit_stop_usd"] == 500.0
    assert payload["state"]["saved_settings"]["daily_profit_stop_usd"] == 500.0
    assert payload["state"]["daily_profit_stop_usd"] == 500.0
    assert "market_15m_enabled" not in state_data["active_settings"]
    assert state_data["active_settings"]["daily_profit_stop_usd"] == 500.0
    assert state_data["active_settings"]["market_5m_enabled"] is True
    assert state_data["market_interval_label"] == "5m"
    assert state_data["market_interval_secs"] == 300


def test_settings_post_persists_mock_mode_to_env(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    state_file = tmp_path / "state.json"
    env_file = tmp_path / ".env"
    env_file.write_text("MOCK_MODE=true\n", encoding="utf-8")
    monkeypatch.setattr(st, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(st, "STATE_FILE", state_file)
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setenv("MOCK_MODE", "true")
    st.save_state(st.BotState(active_settings=st.asdict(st.BotSettings())))

    async def _json():
        return {"mock_mode": False}

    request = SimpleNamespace(headers={}, rel_url=SimpleNamespace(query={}), json=_json)
    with patch("web.dashboard.config.MOCK_MODE", True):
        response = asyncio.run(api_settings_post(request))
    payload = json.loads(response.text)

    assert payload["success"] is True
    assert payload["restart_required"] is True
    assert payload["settings"]["mock_mode_configured"] is False
    assert "MOCK_MODE=false" in env_file.read_text(encoding="utf-8")


def test_state_payload_switches_pnl_surfaces_to_live_mode(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "false")
    state = {
        "current_window": 1800000000,
        "seconds_left": 120.0,
        "active_settings": st.asdict(st.BotSettings()),
        "live_cash": 16.25,
        "live_portfolio": 3.75,
        "live_total": 20.0,
        "live_balance_ok": True,
        "live_portfolio_ok": True,
    }
    live_ts = _ts(2026, 6, 27)
    trades = [
        {"timestamp": live_ts, "resolved": True, "won": True, "pnl": 2.0, "amount_usd": 3.0, "mock": False},
        {"timestamp": live_ts, "resolved": True, "won": True, "pnl": 999.0, "amount_usd": 100.0, "mock": True},
    ]

    with (
        patch("web.dashboard.time.time", return_value=live_ts),
        patch("web.dashboard.st.load_state", return_value=state),
        patch("web.dashboard.st.load_daily_pnl", return_value={
            "pnl": 999.0,
            "trades": 1,
            "wins": 1,
            "losses": 0,
            "halted": True,
            "halt_reason": "mock daily halt",
            "start_balance": 1000.0,
        }),
        patch("web.dashboard.st.load_settings", return_value=st.BotSettings()),
        patch("web.dashboard.st.load_snapshots", return_value=[]),
        patch("web.dashboard.st.load_trades", return_value=trades),
        patch("web.dashboard.st.load_balance", return_value={"balance": 1000.0, "initial": 1000.0}),
        patch("web.dashboard.st.get_trading_enabled", return_value=True),
        patch("web.dashboard.st.get_emergency_stop", return_value=False),
        patch("web.dashboard.st.load_price_extremes", return_value={}),
    ):
        payload = _state_payload()

    assert payload["mock_mode"] is False
    assert payload["pnl_summary"]["source"] == "live_account"
    assert payload["pnl_summary"]["current_capital"] == 20.0
    assert payload["pnl_summary"]["total_pnl"] == 2.0
    assert "total_capital" not in payload["pnl_summary"]
    assert payload["trades_total"] == 1
    assert payload["total_pnl"] == 2.0
    assert payload["daily_pnl"] == 2.0
    assert payload["daily_halted"] is False
    assert payload["daily_halt_reason"] == ""
    assert payload["recent_trades"][0]["mock"] is False


def test_pnl_calendar_endpoint_filters_to_configured_live_mode(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "false")
    live_ts = _ts(2026, 6, 27)
    trades = [
        {"timestamp": live_ts, "resolved": True, "won": True, "pnl": 2.0, "mock": False},
        {"timestamp": live_ts, "resolved": True, "won": True, "pnl": 999.0, "mock": True},
    ]
    request = SimpleNamespace(rel_url=SimpleNamespace(query={"year": "2026", "month": "6"}))

    with (
        patch("web.dashboard.time.time", return_value=live_ts),
        patch("web.dashboard.st.load_trades", return_value=trades),
    ):
        response = asyncio.run(api_pnl_calendar(request))

    payload = json.loads(response.text)
    assert payload["month_pnl"] == 2.0
    assert payload["today"]["pnl"] == 2.0
    assert payload["today"]["trade_count"] == 1


def test_live_control_start_does_not_persist_mock_daily_halt(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(st, "STATE_FILE", state_file)
    monkeypatch.setenv("MOCK_MODE", "false")
    st.save_state(st.BotState(daily_halted=True, daily_halt_reason="old mock halt"))

    async def _json():
        return {"trading_enabled": True}

    request = SimpleNamespace(headers={}, rel_url=SimpleNamespace(query={}), json=_json)
    with (
        patch("web.dashboard.st.set_trading_enabled"),
        patch("web.dashboard.st.set_emergency_stop"),
        patch("web.dashboard._state_payload", return_value={"mock_mode": False}),
    ):
        response = asyncio.run(api_control(request))

    payload = json.loads(response.text)
    state_data = st.load_state()
    assert payload["success"] is True
    assert state_data["daily_halted"] is False
    assert state_data["daily_halt_reason"] == ""
    assert state_data["daily_pnl"] == 0.0


def test_state_payload_uses_saved_daily_profit_stop_not_active_window():
    saved = st.BotSettings(daily_profit_stop_usd=500.0)
    active = st.BotSettings(daily_profit_stop_usd=1000.0)
    state = {
        "current_window": 1800000000,
        "seconds_left": 120.0,
        "active_settings": st.asdict(active),
    }

    with (
        patch("web.dashboard.st.load_state", return_value=state),
        patch("web.dashboard.st.load_daily_pnl", return_value={
            "pnl": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "halted": False,
            "halt_reason": "",
            "start_balance": 1000.0,
        }),
        patch("web.dashboard.st.load_settings", return_value=saved),
        patch("web.dashboard.st.load_snapshots", return_value=[]),
        patch("web.dashboard.st.load_trades", return_value=[]),
        patch("web.dashboard.st.load_balance", return_value={"balance": 1000.0, "initial": 1000.0}),
        patch("web.dashboard.st.calc_stats", return_value={
            "trades_total": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "total_wagered": 0.0,
            "recent_win_rate": 0.0,
        }),
        patch("web.dashboard.st.get_trading_enabled", return_value=True),
        patch("web.dashboard.st.get_emergency_stop", return_value=False),
        patch("web.dashboard.st.load_price_extremes", return_value={}),
        patch("web.dashboard.st._atomic_write"),
        patch("web.dashboard.st._remember_json"),
    ):
        payload = _state_payload()

    assert payload["active_settings"]["daily_profit_stop_usd"] == 1000.0
    assert payload["saved_settings"]["daily_profit_stop_usd"] == 500.0
    assert payload["daily_profit_stop_usd"] == 500.0
    assert payload["daily_profit_stop_amount"] == 500.0


def test_state_payload_clears_stale_daily_halt_from_daily_store():
    state = {
        "daily_halted": True,
        "daily_halt_reason": "Daily profit target reached",
        "active_settings": st.asdict(st.BotSettings()),
    }
    daily = {
        "date": "2026-06-27",
        "pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "halted": False,
        "halt_reason": "",
        "start_balance": 1220.78,
    }

    with (
        patch("web.dashboard.st.load_state", return_value=state),
        patch("web.dashboard.st.load_daily_pnl", return_value=daily),
        patch("web.dashboard.st.load_settings", return_value=st.BotSettings(daily_profit_stop_usd=500.0)),
        patch("web.dashboard.st.load_snapshots", return_value=[]),
        patch("web.dashboard.st.load_trades", return_value=[]),
        patch("web.dashboard.st.load_balance", return_value={"balance": 1220.78, "initial": 1000.0}),
        patch("web.dashboard.st.calc_stats", return_value=_empty_stats()),
        patch("web.dashboard.st.get_trading_enabled", return_value=True),
        patch("web.dashboard.st.get_emergency_stop", return_value=False),
        patch("web.dashboard.st.load_price_extremes", return_value={}),
        patch("web.dashboard.st._atomic_write") as write_state,
        patch("web.dashboard.st._remember_json"),
    ):
        payload = _state_payload()

    assert payload["daily_halted"] is False
    assert payload["daily_halt_reason"] == ""
    assert payload["daily_pnl"] == 0.0
    write_state.assert_called_once()


def test_control_start_returns_daily_halt_cleared_state():
    state = {
        "daily_halted": True,
        "daily_halt_reason": "Daily profit target reached",
        "active_settings": st.asdict(st.BotSettings()),
    }
    daily = {
        "date": "2026-06-27",
        "pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "halted": False,
        "halt_reason": "",
        "start_balance": 1220.78,
    }

    async def _json():
        return {"trading_enabled": True}

    request = SimpleNamespace(headers={}, rel_url=SimpleNamespace(query={}), json=_json)

    with (
        patch("web.dashboard.st.set_trading_enabled") as set_trading,
        patch("web.dashboard.st.set_emergency_stop") as set_emergency,
        patch("web.dashboard.st.load_state", return_value=state),
        patch("web.dashboard.st.load_daily_pnl", return_value=daily),
        patch("web.dashboard.st.load_settings", return_value=st.BotSettings(daily_profit_stop_usd=500.0)),
        patch("web.dashboard.st.load_snapshots", return_value=[]),
        patch("web.dashboard.st.load_trades", return_value=[]),
        patch("web.dashboard.st.load_balance", return_value={"balance": 1220.78, "initial": 1000.0}),
        patch("web.dashboard.st.calc_stats", return_value=_empty_stats()),
        patch("web.dashboard.st.get_trading_enabled", return_value=True),
        patch("web.dashboard.st.get_emergency_stop", return_value=False),
        patch("web.dashboard.st.load_price_extremes", return_value={}),
        patch("web.dashboard.st._atomic_write"),
        patch("web.dashboard.st._remember_json"),
    ):
        response = asyncio.run(api_control(request))

    payload = json.loads(response.text)
    assert payload["success"] is True
    assert payload["state"]["daily_halted"] is False
    assert payload["state"]["daily_halt_reason"] == ""
    set_trading.assert_called_once_with(True)
    set_emergency.assert_called_once_with(False)


def test_price_extreme_stats_counts_each_level_independently():
    data = {
        "price_source": "best_bid",
        "sample_interval_secs": 1.0,
        "windows": [{
            "window_ts": 100,
            "market_slug": "btc-updown-5m-100",
            "max_up_bid": 0.98,
            "max_down_bid": 0.99,
            "hits": {
                "0.97": {"up_ts": 10.0, "down_ts": 20.0},
                "0.98": {"up_ts": 11.0, "down_ts": 21.0},
                "0.99": {"up_ts": None, "down_ts": 22.0},
            },
        }],
    }

    with patch("web.dashboard.st.load_price_extremes", return_value=data):
        stats = _price_extreme_stats()

    assert len(stats["levels"]) == 3





def test_low_price_winner_stats_counts_each_window_once():
    data = {
        "windows": [
            {
                "window_ts": 100,
                "market_slug": "btc-updown-5m-100",
                "min_up_bid": 0.08,
                "min_down_bid": 0.45,
                "last_observed_ts": 20.0,
                "low_hits": {"0.10": {"up_ts": 10.0, "down_ts": None}},
            },
            {
                "window_ts": 200,
                "market_slug": "btc-updown-5m-200",
                "min_up_bid": 0.20,
                "min_down_bid": 0.09,
                "last_observed_ts": 40.0,
                "low_hits": {"0.10": {"up_ts": None, "down_ts": 30.0}},
            },
        ],
    }
    trades = [
        {"window_ts": 100, "resolved": True, "actual": "UP"},
        {"window_ts": 100, "resolved": True, "actual": "UP"},
        {"window_ts": 200, "resolved": True, "actual": "UP"},
    ]

    with (
        patch("web.dashboard.st.load_price_extremes", return_value=data),
        patch("web.dashboard.st.load_snapshots", return_value=[]),
    ):
        stats = _low_price_winner_stats(trades)

    assert stats["winner_low_count"] == 1
    assert stats["resolved_observed_windows"] == 2
    assert stats["rate_pct"] == 50.0
    assert stats["recent"][0]["window_ts"] == 100


def test_low_price_winner_stats_retains_all_windows_for_pagination():
    windows = [
        {
            "window_ts": index,
            "market_slug": f"btc-updown-5m-{index}",
            "min_up_bid": 0.08,
            "min_down_bid": 0.45,
            "low_hits": {"0.10": {"up_ts": float(index), "down_ts": None}},
        }
        for index in range(1, 16)
    ]
    trades = [
        {"window_ts": index, "resolved": True, "actual": "UP"}
        for index in range(1, 16)
    ]

    with (
        patch("web.dashboard.st.load_price_extremes", return_value={"windows": windows}),
        patch("web.dashboard.st.load_snapshots", return_value=[]),
    ):
        stats = _low_price_winner_stats(trades)

    assert len(stats["recent"]) == 15


def test_low_price_winner_stats_reset_ignores_old_snapshots():
    trades = [{"window_ts": 100, "resolved": True, "actual": "UP"}]
    snapshots = [{
        "window_ts": 100,
        "timestamp": 110.0,
        "up_price": 0.08,
        "down_price": 0.45,
    }]

    with (
        patch("web.dashboard.st.load_price_extremes", return_value={"windows": []}),
        patch("web.dashboard.st.load_snapshots", return_value=snapshots),
    ):
        stats = _low_price_winner_stats(trades)

    assert stats["recorded_windows"] == 0
    assert stats["winner_low_count"] == 0
    assert stats["recent"] == []


def test_pnl_history_tracks_each_resolved_trade_and_cumulative_pnl():
    rows = _pnl_history([
        {"timestamp": 3.0, "resolved": True, "pnl": 4.0, "secs_left": 5.0},
        {"timestamp": 1.0, "resolved": True, "pnl": 10.0, "secs_left": 25.0},
        {"timestamp": 2.0, "resolved": False, "pnl": 99.0, "secs_left": 20.0},
        {"timestamp": 4.0, "resolved": True, "pnl": -6.0, "secs_left": 10.0},
    ])

    assert [row["pnl"] for row in rows] == [10.0, 4.0, -6.0]
    assert [row["cumulative"] for row in rows] == [10.0, 14.0, 8.0]
