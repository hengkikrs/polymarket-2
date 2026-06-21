from datetime import datetime

from analysis.tracker import analyze, build_end_window_meta, build_trigger_calendar, trade_layer
from core.state import BotSettings


def test_trade_layer_uses_reason_then_secs_left_fallback():
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-1: ask=0.9800",
    }) == "TIME-1"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-2: ask=0.9900",
    }) == "TIME-2"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-3: ask=0.9700",
    }) == "TIME-3"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-4: ask=0.9600",
    }) == "TIME-4"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW DOWN TIME-5: ask=0.9500",
    }) == "TIME-5"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP TIME-6: ask=0.9600",
    }) == "TIME-6"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW DOWN REVERSE: initial=UP source=TIME-3",
    }) == "REVERSE"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "BUY-1 UP: quick buy ask=0.5500",
    }) == "BUY-1"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP T1: configurable",
    }) == "T1"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW UP T25_D70_P90",
    }) == "T1"
    assert trade_layer({
        "trigger": "END_WINDOW",
        "trigger_reason": "END_WINDOW DOWN T5_D12_P99",
    }) == "T6"
    assert trade_layer({"trigger": "END_WINDOW", "secs_left": 12.0}) == "T3"


def test_tracker_metadata_reflects_saved_settings():
    settings = BotSettings(
        time3_price=0.96,
        time3_min_secs_left=4.5,
        time3_min_delta_usd=14.0,
        t6_delta_min=14.0,
    )
    meta = build_end_window_meta(settings)

    assert meta["TIME-3"]["price"] == "exactly 0.96"
    assert meta["TIME-3"]["time"] == "Any time above 4.5s"
    assert "BTC delta >= $14" in meta["TIME-3"]["req"]
    assert meta["T6"]["req"] == "BTC delta >= $14"
    assert meta["BUY-1"]["price"] == "buy 0.50-0.60"
    assert "sell 0.80-0.90" in meta["BUY-1"]["req"]


def test_analyze_reports_win_rate_and_latency_per_fire_layer():
    trades = [
        {
            "trigger": "END_WINDOW",
            "trigger_reason": "END_WINDOW UP T25_D70_P90",
            "resolved": True, "won": True, "pnl": 5.0,
            "entry_price": 0.8, "secs_elapsed": 276.0,
            "btc_distance": 80.0, "latency_ms": 12.0,
        },
        {
            "trigger": "END_WINDOW",
            "trigger_reason": "END_WINDOW DOWN T25_D70_P90",
            "resolved": True, "won": False, "pnl": -100.0,
            "entry_price": 0.9, "secs_elapsed": 278.0,
            "btc_distance": -75.0, "latency_ms": 18.0,
        },
        {
            "trigger": "END_WINDOW",
            "trigger_reason": "END_WINDOW UP T5_D12_P99",
            "resolved": False, "won": None, "pnl": 0.0,
            "entry_price": 0.99, "secs_elapsed": 295.0,
            "btc_distance": 20.0, "latency_ms": 0.0,
        },
    ]

    result = analyze(trades, [])

    assert result["triggers"]["T1"]["count"] == 2
    assert result["triggers"]["T1"]["resolved"] == 2
    assert result["triggers"]["T1"]["wins"] == 1
    assert result["triggers"]["T1"]["losses"] == 1
    assert result["triggers"]["T1"]["wr"] == 50.0
    assert result["triggers"]["T1"]["avg_latency_ms"] == 15.0
    assert result["triggers"]["T6"]["open"] == 1
    assert result["triggers"]["T6"]["avg_latency_ms"] is None
    assert result["summary"]["total"] == 3
    assert result["summary"]["resolved"] == 2
    assert result["summary"]["win_rate"] == 50.0
    assert all(t["fire_layer"] in {"T1", "T6"} for t in result["trades"])


def test_trigger_calendar_groups_each_layer_win_rate_by_date():
    day_one = datetime(2026, 6, 11, 12).timestamp()
    day_two = datetime(2026, 6, 12, 12).timestamp()
    trades = [
        {"timestamp": day_one, "trigger_reason": "T25_D70_P90", "resolved": True, "won": True},
        {"timestamp": day_one + 60, "trigger_reason": "T25_D70_P90", "resolved": True, "won": False},
        {"timestamp": day_one + 120, "trigger_reason": "T5_D12_P99", "resolved": True, "won": True},
        {"timestamp": day_two, "trigger_reason": "T20_D55_P92", "resolved": True, "won": True},
        {"timestamp": day_two + 60, "trigger_reason": "T20_D55_P92", "resolved": False, "won": None},
    ]

    calendar = build_trigger_calendar(trades)

    assert len(calendar["days"]) == 2
    first = calendar["days"][0]
    assert first["date"] == "2026-06-11"
    assert first["win_rate"] == 66.7
    assert first["triggers"]["T1"]["win_rate"] == 50.0
    assert first["triggers"]["T6"]["win_rate"] == 100.0
    second = calendar["days"][1]
    assert second["triggers"]["T2"]["resolved"] == 1
    assert second["triggers"]["T2"]["win_rate"] == 100.0
