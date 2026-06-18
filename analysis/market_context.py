"""Deterministic rolling BTC and Polymarket context analysis."""
from __future__ import annotations

import statistics
import time
from collections import defaultdict


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return float(ordered[index])


def _resample_last(rows: list[dict], interval_secs: int = 10) -> list[dict]:
    buckets: dict[int, dict] = {}
    for row in rows:
        timestamp = float(row.get("timestamp") or 0.0)
        if timestamp <= 0:
            continue
        buckets[int(timestamp // interval_secs)] = row
    return [buckets[key] for key in sorted(buckets)]


def _gamma_delta_stats(rows: list[dict], now: float, window_secs: float, suffix: str) -> dict:
    window_rows = [
        row for row in rows
        if 0.0 <= now - (row["window_ts"] + 300) <= window_secs
    ]
    deltas = [row["delta"] for row in window_rows]
    abs_deltas = [abs(value) for value in deltas]
    avg_target = (
        statistics.fmean(row["price_to_beat"] for row in window_rows)
        if window_rows else 0.0
    )
    avg_signed = statistics.fmean(deltas) if deltas else 0.0
    avg_abs = statistics.fmean(abs_deltas) if abs_deltas else 0.0
    avg_abs_bps = avg_abs / avg_target * 10000.0 if avg_target > 0 else 0.0
    return {
        f"samples_{suffix}": len(window_rows),
        f"avg_signed_delta_{suffix}": round(avg_signed, 2),
        f"avg_abs_delta_{suffix}": round(avg_abs, 2),
        f"p90_abs_delta_{suffix}": round(_percentile(abs_deltas, 0.90), 2),
        f"avg_signed_delta_per_10s_{suffix}": round(avg_signed / 30.0, 2),
        f"avg_abs_delta_per_10s_{suffix}": round(avg_abs / 30.0, 2),
        f"avg_abs_delta_bps_{suffix}": round(avg_abs_bps, 2),
    }


def _side_spread_unavailable(row: dict, min_price: float) -> bool:
    if float(row.get("leading_price") or 0.0) < float(min_price):
        return False
    leading = str(row.get("leading") or "").upper()
    if "leading_spread" in row and row.get("leading_spread") is None:
        return True
    if leading == "UP" and "up_spread" in row and row.get("up_spread") is None:
        return True
    if leading == "DOWN" and "down_spread" in row and row.get("down_spread") is None:
        return True
    return False


def analyze_market_context(
    snapshots: list[dict],
    *,
    now: float | None = None,
    saturation_price: float = 0.94,
    min_completed_windows: int = 5,
) -> dict:
    now = float(now or time.time())
    valid = sorted(
        (
            row for row in snapshots
            if float(row.get("timestamp") or 0.0) > 0
            and float(row.get("btc_price") or 0.0) > 0
        ),
        key=lambda row: float(row["timestamp"]),
    )
    rows_20m = [row for row in valid if now - float(row["timestamp"]) <= 1200.0]
    rows_30m = [row for row in valid if now - float(row["timestamp"]) <= 1800.0]
    samples = _resample_last(rows_20m)
    span_secs = (
        float(samples[-1]["timestamp"]) - float(samples[0]["timestamp"])
        if len(samples) >= 2 else 0.0
    )
    coverage_20m = min(1.0, span_secs / 1200.0)

    changes_10s = [
        float(current["btc_price"]) - float(previous["btc_price"])
        for previous, current in zip(samples, samples[1:])
    ]
    latest_delta_10s = changes_10s[-1] if changes_10s else 0.0
    avg_signed_10s = statistics.fmean(changes_10s) if changes_10s else 0.0
    abs_changes = [abs(value) for value in changes_10s]
    avg_abs_10s = statistics.fmean(abs_changes) if abs_changes else 0.0
    p90_abs_10s = _percentile(abs_changes, 0.90)

    regime = "UNKNOWN"
    reason = "Need at least 15 minutes of rolling BTC samples"
    slope_per_min = 0.0
    net_move = 0.0
    efficiency = 0.0
    confidence = 0.0
    if len(samples) >= 30 and span_secs >= 900.0:
        start_ts = float(samples[0]["timestamp"])
        xs = [(float(row["timestamp"]) - start_ts) / 60.0 for row in samples]
        ys = [float(row["btc_price"]) for row in samples]
        x_mean = statistics.fmean(xs)
        y_mean = statistics.fmean(ys)
        denominator = sum((x - x_mean) ** 2 for x in xs)
        slope_per_min = (
            sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
            if denominator > 0 else 0.0
        )
        net_move = ys[-1] - ys[0]
        path = sum(abs(current - previous) for previous, current in zip(ys, ys[1:]))
        efficiency = abs(net_move) / path if path > 0 else 0.0
        move_threshold = max(30.0, avg_abs_10s * 3.0)
        trending = (
            abs(net_move) >= move_threshold
            and abs(slope_per_min) >= 2.5
            and efficiency >= 0.20
            and (slope_per_min > 0) == (net_move > 0)
        )
        regime = (
            "TRENDING_UP" if trending and net_move > 0
            else "TRENDING_DOWN" if trending
            else "SIDEWAYS"
        )
        direction_strength = min(1.0, abs(net_move) / max(move_threshold, 1.0))
        efficiency_strength = min(1.0, efficiency / 0.35)
        confidence = min(
            1.0,
            coverage_20m * (
                0.45 * direction_strength
                + 0.35 * efficiency_strength
                + 0.20 * min(1.0, abs(slope_per_min) / 5.0)
            ),
        )
        if regime == "SIDEWAYS":
            confidence = min(1.0, coverage_20m * (1.0 - min(0.8, efficiency)))
        reason = (
            f"net={net_move:+.1f} slope={slope_per_min:+.1f}/min "
            f"efficiency={efficiency:.2f} coverage={coverage_20m:.0%}"
        )

    min_completed_windows = max(1, int(min_completed_windows or 1))
    all_by_window: dict[int, list[dict]] = defaultdict(list)
    completed_rows = [
        row for row in valid
        if int(row.get("window_ts") or 0) > 0
        and int(row.get("window_ts") or 0) + 300 <= now
    ]
    for row in completed_rows:
        all_by_window[int(row.get("window_ts") or 0)].append(row)
    by_window: dict[int, list[dict]] = defaultdict(list)
    for row in rows_30m:
        window_ts = int(row.get("window_ts") or 0)
        if window_ts > 0 and window_ts + 300 <= now:
            by_window[window_ts].append(row)
    if len(by_window) < min_completed_windows:
        recent_window_ts = sorted(
            {int(row.get("window_ts") or 0) for row in completed_rows},
            reverse=True,
        )[:min_completed_windows]
        by_window = defaultdict(list)
        allowed_windows = set(recent_window_ts)
        for row in completed_rows:
            window_ts = int(row.get("window_ts") or 0)
            if window_ts in allowed_windows:
                by_window[window_ts].append(row)

    def _timing_pairs(grouped: dict[int, list[dict]]) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        saturation_pairs: list[tuple[int, float]] = []
        locked_pairs: list[tuple[int, float]] = []
        for window_ts, rows in sorted(grouped.items()):
            ordered_rows = sorted(rows, key=lambda item: float(item.get("timestamp") or 0.0))
            hit = next(
                (
                    float(row.get("secs_left") or 0.0)
                    for row in ordered_rows
                    if float(row.get("leading_price") or 0.0) >= saturation_price
                ),
                None,
            )
            if hit is not None:
                saturation_pairs.append((window_ts, hit))
            locked_hit = next(
                (
                    float(row.get("secs_left") or 0.0)
                    for row in ordered_rows
                    if float(row.get("up_price") or 0.0) >= 0.999
                    or float(row.get("down_price") or 0.0) >= 0.999
                    or float(row.get("leading_price") or 0.0) >= 0.999
                    or _side_spread_unavailable(row, saturation_price)
                ),
                None,
            )
            if locked_hit is not None:
                locked_pairs.append((window_ts, locked_hit))
        return saturation_pairs, locked_pairs

    saturation_pairs, locked_pairs = _timing_pairs(by_window)
    all_saturation_pairs, all_locked_pairs = _timing_pairs(all_by_window)
    if len(saturation_pairs) < min_completed_windows:
        saturation_pairs = all_saturation_pairs[-min_completed_windows:]
    if len(locked_pairs) < min_completed_windows:
        locked_pairs = all_locked_pairs[-min_completed_windows:]
    saturation_seconds = [hit for _window_ts, hit in saturation_pairs]
    locked_seconds = [hit for _window_ts, hit in locked_pairs]
    timing_windows = {
        window_ts
        for window_ts, _hit in [*saturation_pairs, *locked_pairs]
    }

    return {
        "regime": regime,
        "reason": reason,
        "confidence": round(confidence, 4),
        "source": "btc_feed+clob",
        "samples_20m": len(samples),
        "coverage_20m": round(coverage_20m, 4),
        "delta_10s": round(latest_delta_10s, 2),
        "avg_signed_delta_10s_20m": round(avg_signed_10s, 2),
        "avg_abs_delta_10s_20m": round(avg_abs_10s, 2),
        "p90_abs_delta_10s_20m": round(p90_abs_10s, 2),
        "net_move_20m": round(net_move, 2),
        "slope_per_min_20m": round(slope_per_min, 2),
        "efficiency_20m": round(efficiency, 4),
        "saturation_price": float(saturation_price),
        "saturation_avg_secs_30m": (
            round(statistics.fmean(saturation_seconds), 1)
            if saturation_seconds else None
        ),
        "saturation_samples_30m": len(saturation_seconds),
        "locked_avg_secs_30m": (
            round(statistics.fmean(locked_seconds), 1)
            if locked_seconds else None
        ),
        "locked_samples_30m": len(locked_seconds),
        "completed_windows_30m": max(len(by_window), len(timing_windows)),
    }


def analyze_gamma_resolutions(
    resolutions: list[dict],
    *,
    now: float | None = None,
    sideways_bps: float = 2.0,
    volatile_bps: float = 5.0,
) -> dict:
    """Summarize official Gamma target/final metadata without bot trade data."""
    now = float(now or time.time())
    valid = []
    for row in resolutions:
        try:
            window_ts = int(row.get("window_ts") or 0)
            target = float(row.get("price_to_beat") or 0.0)
            final = float(row.get("final_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if window_ts <= 0 or target <= 0 or final <= 0:
            continue
        valid.append({
            "window_ts": window_ts,
            "price_to_beat": target,
            "final_price": final,
            "delta": final - target,
        })
    valid.sort(key=lambda row: row["window_ts"])
    delta_stats = {}
    for suffix, window_secs in (
        ("3h", 10800.0),
        ("2h", 7200.0),
        ("1h", 3600.0),
        ("30m", 1800.0),
    ):
        delta_stats.update(_gamma_delta_stats(valid, now, window_secs, suffix))
    rows_3h_count = int(delta_stats["samples_3h"])
    avg_abs_bps_3h = float(delta_stats["avg_abs_delta_bps_3h"])

    rows_20m = [
        row for row in valid
        if 0.0 <= now - (row["window_ts"] + 300) <= 1200.0
    ]
    counts = {"UPTREND": 0, "DOWNTREND": 0, "SIDEWAYS": 0}
    for row in rows_20m:
        threshold = row["price_to_beat"] * max(0.0, sideways_bps) / 10000.0
        if row["delta"] > threshold:
            counts["UPTREND"] += 1
        elif row["delta"] < -threshold:
            counts["DOWNTREND"] += 1
        else:
            counts["SIDEWAYS"] += 1
    sample_count = len(rows_20m)
    percentages = {
        key: round(value / sample_count * 100.0, 1) if sample_count else 0.0
        for key, value in counts.items()
    }
    regime = max(
        ("UPTREND", "DOWNTREND", "SIDEWAYS"),
        key=lambda key: (counts[key], key == "SIDEWAYS"),
    ) if sample_count else "UNKNOWN"
    market_volatility = "VOLATILE" if avg_abs_bps_3h >= volatile_bps else "NON_VOLATILE"
    start_recommendation = (
        "START"
        if rows_3h_count >= 24 and sample_count >= 3 and market_volatility == "VOLATILE"
        else "WAIT"
    )

    return {
        "source": "gamma_event_metadata",
        "updated_at": now,
        **delta_stats,
        "market_volatility": market_volatility,
        "start_recommendation": start_recommendation,
        "start_reason": (
            f"{market_volatility}; Gamma samples={rows_3h_count}; "
            f"20m coverage={min(1.0, sample_count / 4.0):.0%}"
        ),
        "regime": regime,
        "regime_percentages_20m": percentages,
        "regime_samples_20m": sample_count,
        "regime_coverage_20m": round(min(1.0, sample_count / 4.0), 4),
        "sideways_threshold_bps": float(sideways_bps),
    }


__all__ = ["analyze_gamma_resolutions", "analyze_market_context"]
