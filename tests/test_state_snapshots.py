import json
from datetime import datetime, timezone

import core.state as st


def test_bot_state_serializes_orderbook_depth_for_dashboard():
    state = st.BotState(
        up_ask_depth=[(0.40, 250.0)],
        down_ask_depth=[(0.39, 300.0)],
    )

    data = st.asdict(state)

    assert data["up_ask_depth"] == [(0.40, 250.0)]
    assert data["down_ask_depth"] == [(0.39, 300.0)]


def test_daily_date_defaults_to_bangkok_day(monkeypatch):
    monkeypatch.setenv("POLY_DAILY_TIMEZONE", "Asia/Bangkok")
    ts = datetime(2026, 6, 26, 18, 0, 0, tzinfo=timezone.utc).timestamp()

    assert st._daily_date(ts) == "2026-06-27"


def test_daily_pnl_resets_halt_on_new_trading_day(tmp_path, monkeypatch):
    daily_file = tmp_path / "daily_pnl.json"
    balance_file = tmp_path / "balance.json"
    daily_file.write_text(json.dumps({
        "date": "2026-06-26",
        "pnl": 110.0,
        "trades": 3,
        "wins": 3,
        "losses": 0,
        "halted": True,
        "halt_reason": "Daily profit target reached",
        "start_balance": 1000.0,
    }), encoding="utf-8")
    balance_file.write_text(json.dumps({"balance": 1110.0}), encoding="utf-8")
    monkeypatch.setattr(st, "DAILY_PNL_FILE", daily_file)
    monkeypatch.setattr(st, "BALANCE_FILE", balance_file)
    monkeypatch.setattr(st, "_daily_date", lambda ts=None: "2026-06-27")
    st._json_cache.clear()

    data = st.load_daily_pnl()

    assert data["date"] == "2026-06-27"
    assert data["pnl"] == 0.0
    assert data["trades"] == 0
    assert data["halted"] is False
    assert data["halt_reason"] == ""
    assert data["start_balance"] == 1110.0


def test_rebuild_daily_pnl_uses_trading_day_filter(tmp_path, monkeypatch):
    include_ts = 100.0
    skip_ts = 200.0
    monkeypatch.setattr(st, "DAILY_PNL_FILE", tmp_path / "daily_pnl.json")
    monkeypatch.setattr(st, "load_balance", lambda: {"balance": 1000.0})
    monkeypatch.setattr(
        st,
        "_daily_date",
        lambda ts=None: "2026-06-27" if ts is None or ts == include_ts else "2026-06-26",
    )
    st._json_cache.clear()

    data = st.rebuild_daily_pnl([
        {"resolved": True, "timestamp": include_ts, "pnl": 12.5, "won": True},
        {"resolved": True, "timestamp": skip_ts, "pnl": 99.0, "won": True},
    ])

    assert data["date"] == "2026-06-27"
    assert data["pnl"] == 12.5
    assert data["trades"] == 1
    assert data["wins"] == 1


def test_save_snapshot_trims_to_configured_limit(tmp_path, monkeypatch):
    snapshot_file = tmp_path / "snapshots.json"
    extremes_file = tmp_path / "price_extremes.json"
    monkeypatch.setattr(st, "SNAPSHOTS_FILE", snapshot_file)
    monkeypatch.setattr(st, "PRICE_EXTREMES_FILE", extremes_file)
    monkeypatch.setattr(st, "SNAPSHOT_MAX_ROWS", 2)
    st._json_cache.clear()

    for idx in range(3):
        st.save_snapshot(
            1_800_000_000,
            secs_left=100 - idx,
            secs_elapsed=idx,
            up_price=0.5,
            down_price=0.5,
            btc_price=62_000 + idx,
            btc_open=62_000,
            btc_distance=idx,
            leading="UP",
        )

    snapshots = st.load_snapshots()

    assert len(snapshots) == 2
    assert snapshots[0]["secs_elapsed"] == 1
    assert snapshots[1]["secs_elapsed"] == 2


def test_price_extremes_track_both_sides_per_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "PRICE_EXTREMES_FILE", tmp_path / "price_extremes.json")
    snapshots_file = tmp_path / "snapshots.json"
    snapshots_file.write_text("[]")
    monkeypatch.setattr(st, "SNAPSHOTS_FILE", snapshots_file)
    st._json_cache.clear()

    st.record_price_extremes(1_800_000_000, 0.98, 0.01, observed_ts=10.0)
    st.record_price_extremes(1_800_000_000, 0.02, 0.99, observed_ts=20.0)

    row = st.load_price_extremes()["windows"][0]
    assert row["hits"]["0.97"] == {"up_ts": 10.0, "down_ts": 20.0}
    assert row["hits"]["0.98"] == {"up_ts": 10.0, "down_ts": 20.0}
    assert row["hits"]["0.99"] == {"up_ts": None, "down_ts": 20.0}
    assert row["min_up_bid"] == 0.02
    assert row["min_down_bid"] == 0.01
    assert row["low_hits"]["0.10"] == {"up_ts": 20.0, "down_ts": 10.0}
