from analysis.market_context import analyze_gamma_resolutions, analyze_market_context


def _snapshot(timestamp, price, window_ts, leading_price=0.60):
    secs_elapsed = float(timestamp - window_ts)
    return {
        "timestamp": float(timestamp),
        "btc_price": float(price),
        "window_ts": int(window_ts),
        "secs_elapsed": secs_elapsed,
        "secs_left": max(0.0, 300.0 - secs_elapsed),
        "leading_price": float(leading_price),
    }


def test_market_context_detects_trending_up_and_ten_second_delta():
    now = 10_000.0
    rows = [
        _snapshot(now - 1200 + index * 10, 60_000 + index * 4, 9_900)
        for index in range(121)
    ]

    result = analyze_market_context(rows, now=now)

    assert result["regime"] == "TRENDING_UP"
    assert result["delta_10s"] == 4.0
    assert result["coverage_20m"] == 1.0
    assert result["confidence"] > 0.5


def test_market_context_reports_sideways_and_average_saturation_second():
    now = 20_000.0
    rows = []
    for window_ts, hit_second in (
        (18_600, None),
        (18_900, 210),
        (19_200, 240),
        (19_500, None),
    ):
        for second in range(0, 300, 10):
            leading = 0.95 if hit_second is not None and second >= hit_second else 0.70
            rows.append(_snapshot(window_ts + second, 60_000 + (second % 30) - 10, window_ts, leading))

    result = analyze_market_context(rows, now=now)

    assert result["regime"] == "SIDEWAYS"
    assert result["saturation_avg_secs_30m"] == 75.0
    assert result["saturation_samples_30m"] == 2
    assert result["completed_windows_30m"] == 4


def test_market_context_reports_locked_or_missing_spread_timing():
    now = 20_000.0
    rows = []
    for second in range(0, 300, 10):
        row = _snapshot(19_500 + second, 60_000 + second, 19_500, 0.99)
        row["leading_spread"] = None if second >= 250 else 0.01
        rows.append(row)

    result = analyze_market_context(rows, now=now)

    assert result["locked_avg_secs_30m"] == 50.0
    assert result["locked_samples_30m"] == 1


def test_market_context_ignores_missing_spread_before_saturation():
    now = 20_000.0
    rows = []
    for second in range(0, 300, 10):
        leading = 0.50 if second < 220 else 0.99
        row = _snapshot(19_500 + second, 60_000 + second, 19_500, leading)
        row["leading_spread"] = None if second in (0, 250) else 0.01
        rows.append(row)

    result = analyze_market_context(rows, now=now)

    assert result["saturation_avg_secs_30m"] == 80.0
    assert result["locked_avg_secs_30m"] == 50.0
    assert result["locked_samples_30m"] == 1


def test_market_context_expands_timing_sample_to_five_completed_windows():
    now = 20_000.0
    rows = []
    for window_ts in (17_000, 17_300, 17_600, 19_200, 19_500):
        for second in range(0, 300, 60):
            row = _snapshot(window_ts + second, 60_000 + second, window_ts, 0.95)
            row["leading_spread"] = None if second >= 120 else 0.01
            rows.append(row)

    result = analyze_market_context(rows, now=now)

    assert result["completed_windows_30m"] == 5
    assert result["saturation_samples_30m"] == 5
    assert result["locked_samples_30m"] == 5


def test_gamma_context_uses_official_target_final_rows_for_regime_and_delta():
    now = 20_000.0
    rows = [
        {
            "window_ts": 18_600 + index * 300,
            "price_to_beat": 60_000.0,
            "final_price": 60_000.0 + delta,
        }
        for index, delta in enumerate((60.0, -40.0, 5.0, 80.0))
    ]

    result = analyze_gamma_resolutions(rows, now=now)

    assert result["samples_3h"] == 4
    assert result["avg_abs_delta_3h"] == 46.25
    assert result["avg_abs_delta_2h"] == 46.25
    assert result["avg_abs_delta_1h"] == 46.25
    assert result["avg_abs_delta_30m"] == 46.25
    assert result["avg_signed_delta_per_10s_3h"] == 0.88
    assert result["regime"] == "UPTREND"
    assert result["start_recommendation"] == "WAIT"
    assert result["regime_percentages_20m"] == {
        "UPTREND": 50.0,
        "DOWNTREND": 25.0,
        "SIDEWAYS": 25.0,
    }


def test_gamma_context_reports_distinct_rolling_delta_windows():
    now = 20_000.0
    rows = []
    for window_ts, delta in (
        (9_200, 300.0),
        (12_800, 200.0),
        (16_400, 100.0),
        (18_200, 50.0),
        (19_400, 10.0),
    ):
        rows.append({
            "window_ts": window_ts,
            "price_to_beat": 60_000.0,
            "final_price": 60_000.0 + delta,
        })

    result = analyze_gamma_resolutions(rows, now=now)

    assert result["samples_3h"] == 5
    assert result["avg_abs_delta_3h"] == 132.0
    assert result["samples_2h"] == 4
    assert result["avg_abs_delta_2h"] == 90.0
    assert result["samples_1h"] == 3
    assert result["avg_abs_delta_1h"] == 53.33
    assert result["samples_30m"] == 2
    assert result["avg_abs_delta_30m"] == 30.0
