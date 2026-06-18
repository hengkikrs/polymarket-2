import core.state as st


def test_bot_state_serializes_orderbook_depth_for_dashboard():
    state = st.BotState(
        up_ask_depth=[(0.40, 250.0)],
        down_ask_depth=[(0.39, 300.0)],
    )

    data = st.asdict(state)

    assert data["up_ask_depth"] == [(0.40, 250.0)]
    assert data["down_ask_depth"] == [(0.39, 300.0)]


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
