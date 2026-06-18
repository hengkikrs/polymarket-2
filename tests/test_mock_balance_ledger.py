import core.state as st


def test_mock_ledger_balance_matches_initial_minus_stakes_plus_returns():
    trades = [
        {"amount_usd": 52.0, "resolved": True, "balance_returned": 0.0},
        {"amount_usd": 40.0, "resolved": True, "balance_returned": 98.962},
        {"amount_usd": 10.0, "resolved": False, "balance_returned": 0.0},
    ]
    balance = {"initial": 1000.0, "balance": 1048.962}

    ledger = st.calc_trade_ledger_balance(trades, balance)

    assert ledger["ledger_balance"] == 996.962
    assert ledger["ledger_balance_drift"] == 52.0
    assert ledger["ledger_balance_ok"] is False
    assert ledger["open_amount"] == 10.0


def test_mock_ledger_balance_accepts_exact_cash_match():
    trades = [
        {"amount_usd": 42.0, "resolved": True, "balance_returned": 0.0},
        {"amount_usd": 45.0, "resolved": True, "balance_returned": 99.021},
    ]
    balance = {"initial": 1000.0, "balance": 1012.021}

    ledger = st.calc_trade_ledger_balance(trades, balance)

    assert ledger["ledger_balance"] == 1012.021
    assert ledger["ledger_balance_drift"] == 0.0
    assert ledger["ledger_balance_ok"] is True


def test_gamma_actual_correction_can_resolve_directional_trigger(monkeypatch):
    trades = [
        {
            "window_ts": 1800000000,
            "market_slug": "btc-updown-5m-1800000000",
            "trigger": "MANDATORY",
            "outcome": "DOWN",
            "entry_price": 0.80,
            "shares": 10.0,
            "amount_usd": 8.0,
            "resolved": False,
            "exited_early": False,
            "pnl": 0.0,
            "balance_returned": 0.0,
        }
    ]
    saved = []
    added = []
    monkeypatch.setattr(st, "load_trades", lambda: trades)
    monkeypatch.setattr(st, "save_trades", lambda rows: saved.append(rows))
    monkeypatch.setattr(st, "add_balance", lambda amount: added.append(amount) or amount)
    monkeypatch.setattr(st, "rebuild_cum_stats", lambda rows=None: {})
    monkeypatch.setattr(st, "rebuild_daily_pnl", lambda rows=None: {})

    result = st.apply_gamma_actual_correction(
        1800000000,
        "btc-updown-5m-1800000000",
        "DOWN",
        triggers=("MANDATORY",),
    )

    assert result["resolved"] == 1
    assert result["balance_delta"] == 10.0
    assert saved
    assert added == [10.0]
    assert trades[0]["resolved"] is True
    assert trades[0]["won"] is True


def test_gamma_correction_persists_official_metadata_when_result_is_unchanged(monkeypatch):
    trades = [
        {
            "window_ts": 1800000000,
            "market_slug": "btc-updown-5m-1800000000",
            "trigger": "END_WINDOW",
            "outcome": "UP",
            "entry_price": 0.90,
            "shares": 10.0,
            "amount_usd": 9.0,
            "resolved": True,
            "exited_early": False,
            "actual": "UP",
            "won": True,
            "pnl": 1.0,
            "balance_returned": 10.0,
            "btc_at_close": 101.0,
            "resolution_source": "local_close",
        }
    ]
    saved = []
    monkeypatch.setattr(st, "load_trades", lambda: trades)
    monkeypatch.setattr(st, "save_trades", lambda rows: saved.append(rows))
    monkeypatch.setattr(st, "add_balance", lambda amount: amount)
    monkeypatch.setattr(st, "rebuild_cum_stats", lambda rows=None: {})
    monkeypatch.setattr(st, "rebuild_daily_pnl", lambda rows=None: {})

    result = st.apply_gamma_actual_correction(
        1800000000,
        "btc-updown-5m-1800000000",
        "UP",
        source="gamma_event_metadata",
        final_price=102.0,
        price_to_beat=100.0,
    )

    assert result["changed"] == 0
    assert result["metadata_updated"] == 1
    assert saved
    assert trades[0]["btc_at_close"] == 102.0
    assert trades[0]["resolution_price_to_beat"] == 100.0
    assert trades[0]["resolution_source"] == "gamma_event_metadata"


def test_end_window_directional_loss_is_full_stake(monkeypatch):
    trades = [
        {
            "window_ts": 1800000000,
            "market_slug": "btc-updown-5m-1800000000",
            "trigger": "END_WINDOW",
            "outcome": "UP",
            "entry_price": 0.95,
            "shares": 105.0,
            "amount_usd": 100.0,
            "btc_open": 100.0,
            "resolved": False,
            "exited_early": False,
            "pnl": 0.0,
            "balance_returned": 0.0,
        }
    ]
    writes = []
    monkeypatch.setattr(st, "load_trades", lambda: trades)
    monkeypatch.setattr(st, "_atomic_write", lambda path, text: writes.append((path, text)))
    monkeypatch.setattr(st, "_remember_json", lambda path, data: None)
    monkeypatch.setattr(st, "record_cum_trade", lambda trade: None)

    resolved = st.update_directional_results(
        1800000000,
        btc_close=90.0,
        market_slug="btc-updown-5m-1800000000",
        triggers=("END_WINDOW",),
        actual="DOWN",
    )

    assert resolved[0]["won"] is False
    assert resolved[0]["pnl"] == -100.0
    assert resolved[0]["balance_returned"] == 0.0
    assert trades[0]["pnl"] == -100.0
    assert writes


def test_end_window_directional_win_uses_btc_open_to_close(monkeypatch):
    trades = [
        {
            "window_ts": 1800000000,
            "market_slug": "btc-updown-5m-1800000000",
            "trigger": "END_WINDOW",
            "outcome": "UP",
            "entry_price": 0.57,
            "shares": 172.34,
            "amount_usd": 100.0,
            "btc_open": 62774.875,
            "resolved": False,
            "exited_early": False,
            "pnl": 0.0,
            "balance_returned": 0.0,
        }
    ]
    writes = []
    monkeypatch.setattr(st, "load_trades", lambda: trades)
    monkeypatch.setattr(st, "_atomic_write", lambda path, text: writes.append((path, text)))
    monkeypatch.setattr(st, "_remember_json", lambda path, data: None)
    monkeypatch.setattr(st, "record_cum_trade", lambda trade: None)

    resolved = st.update_directional_results(
        1800000000,
        btc_close=62796.005,
        market_slug="btc-updown-5m-1800000000",
        triggers=("END_WINDOW",),
    )

    assert resolved[0]["actual"] == "UP"
    assert resolved[0]["won"] is True
    assert resolved[0]["pnl"] == 74.1062
    assert resolved[0]["balance_returned"] == 174.1062
    assert trades[0]["btc_at_close"] == 62796.005
    assert writes
