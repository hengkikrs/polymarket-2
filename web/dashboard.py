"""Live web dashboard for the END_WINDOW runtime."""
from __future__ import annotations

import asyncio
import calendar
import hmac
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

import core.config as config
import core.market as mkt
import core.state as st
from analysis.market_context import analyze_gamma_resolutions, analyze_market_context
from bot_runtime.end_window_runner import (
    TIME_MIN_DELTA_USD,
)
from strategies import end_window, enabled_strategies

log = logging.getLogger("dash")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")

PORT = int(os.getenv("DASH_PORT", "5004"))
HOST = os.getenv("DASH_HOST", "127.0.0.1")
AUTH_TOKEN = os.getenv("DASH_TOKEN", "").strip()
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
STATE_RECENT_TRADE_LIMIT = max(50, int(float(os.getenv("DASH_STATE_RECENT_TRADE_LIMIT", "250"))))
STATE_PNL_HISTORY_LIMIT = max(50, int(float(os.getenv("DASH_STATE_PNL_HISTORY_LIMIT", "250"))))
STATE_RESEARCH_LIMIT = max(10, int(float(os.getenv("DASH_STATE_RESEARCH_LIMIT", "50"))))
STATE_CONTEXT_SNAPSHOT_LIMIT = max(500, int(float(os.getenv("DASH_STATE_CONTEXT_SNAPSHOT_LIMIT", "2500"))))
_gamma_context = analyze_gamma_resolutions([])


def _auth_ok(request: web.Request) -> bool:
    if not AUTH_TOKEN:
        return True
    supplied = request.headers.get("X-Auth-Token") or request.rel_url.query.get("token") or ""
    return hmac.compare_digest(supplied, AUTH_TOKEN)


def _strategy_label() -> str:
    enabled = enabled_strategies()
    return "+".join(sorted(enabled)) if enabled else getattr(config, "STRATEGY_MODE", "END_WINDOW")


def _set_env_values(updates: dict[str, str]) -> None:
    try:
        lines = ENV_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    tmp = ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_FILE)
    os.environ.update(updates)


def _trade_calendar_ts(trade: dict) -> float:
    for key in ("resolved_ts", "exit_ts", "timestamp"):
        try:
            ts = float(trade.get(key) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
    return 0.0


def _build_pnl_calendar(trades: list[dict], year: int | None = None, month: int | None = None) -> dict:
    now = datetime.fromtimestamp(time.time())
    year = int(year or now.year)
    month = int(month or now.month)
    if year < 1970 or year > 2100 or month < 1 or month > 12:
        raise ValueError("invalid calendar month")

    days: dict[str, dict] = {}
    for trade in trades:
        if not trade.get("resolved"):
            continue
        ts = _trade_calendar_ts(trade)
        if ts <= 0:
            continue
        dt = datetime.fromtimestamp(ts)
        if dt.year != year or dt.month != month:
            continue
        key = dt.strftime("%Y-%m-%d")
        bucket = days.setdefault(
            key,
            {"date": key, "pnl": 0.0, "trade_count": 0, "wins": 0, "losses": 0},
        )
        pnl = float(trade.get("pnl") or 0.0)
        bucket["pnl"] = round(bucket["pnl"] + pnl, 4)
        bucket["trade_count"] += 1
        won = trade.get("won")
        if won is True:
            bucket["wins"] += 1
        elif won is False:
            bucket["losses"] += 1

    first_weekday, days_in_month = calendar.monthrange(year, month)
    cells: list[dict] = []
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    prev_days = calendar.monthrange(prev_year, prev_month)[1]

    for idx in range(first_weekday):
        day = prev_days - first_weekday + idx + 1
        key = f"{prev_year:04d}-{prev_month:02d}-{day:02d}"
        cells.append({"date": key, "day": day, "in_month": False, "pnl": 0.0, "trade_count": 0, "wins": 0, "losses": 0})
    for day in range(1, days_in_month + 1):
        key = f"{year:04d}-{month:02d}-{day:02d}"
        cells.append({"date": key, "day": day, "in_month": True, **days.get(key, {"pnl": 0.0, "trade_count": 0, "wins": 0, "losses": 0})})
    next_day = 1
    while len(cells) % 7:
        key = f"{next_year:04d}-{next_month:02d}-{next_day:02d}"
        cells.append({"date": key, "day": next_day, "in_month": False, "pnl": 0.0, "trade_count": 0, "wins": 0, "losses": 0})
        next_day += 1

    month_pnl = round(sum(float(day["pnl"]) for day in days.values()), 4)
    trading_days = sum(1 for day in days.values() if int(day["trade_count"]) > 0)
    winning_days = sum(1 for day in days.values() if float(day["pnl"]) > 0)
    losing_days = sum(1 for day in days.values() if float(day["pnl"]) < 0)
    today_key = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    return {
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "weeks": [cells[i:i + 7] for i in range(0, len(cells), 7)],
        "month_pnl": month_pnl,
        "trading_days": trading_days,
        "winning_days": winning_days,
        "losing_days": losing_days,
        "today": days.get(today_key, {"date": today_key, "pnl": 0.0, "trade_count": 0, "wins": 0, "losses": 0}),
    }


def _ui_layer_name(seconds_left_max: float) -> str:
    mapping = {
        25.0: "T1",
        20.0: "T2",
        15.0: "T3",
        10.0: "T4",
        7.0: "T5",
        5.0: "T6",
    }
    return mapping.get(float(seconds_left_max), f"T{int(seconds_left_max)}")


def _trade_fire_layer(trade: dict) -> str:
    reason = str(trade.get("trigger_reason") or "")
    if "BUY-1" in reason.upper():
        return "BUY-1"
    if "TIME-6" in reason:
        return "TIME-6"
    if "TIME-5" in reason:
        return "TIME-5"
    if "TIME-4" in reason:
        return "TIME-4"
    if "TIME-3" in reason:
        return "TIME-3"
    if "TIME-2" in reason:
        return "TIME-2"
    if "TIME-1" in reason:
        return "TIME-1"
    for raw, label in (
        (" T1:", "T1"),
        (" T2:", "T2"),
        (" T3:", "T3"),
        (" T4:", "T4"),
        (" T5:", "T5"),
        (" T6:", "T6"),
        ("T25_D90_P95", "T1"),
        ("T25_D70_P90", "T1"),
        ("T20_D55_P92", "T2"),
        ("T15_D35_P94", "T3"),
        ("T10_D35_P95", "T4"),
        ("T10_D30_P98", "T4"),
        ("T10_D22_P96", "T4"),
        ("T7_D25_P95", "T5"),
        ("T7_D20_P99", "T5"),
        ("T7_D14_P97", "T5"),
        ("T5_D12_P99", "T6"),
        ("T5_D10_P99", "T6"),
        ("T5_D10_P98", "T6"),
    ):
        if raw in reason:
            return label
    try:
        secs_left = float(trade.get("secs_left") or 0.0)
    except (TypeError, ValueError):
        return "N/A"
    if 20.0 < secs_left <= 25.0:
        return "T1"
    if 15.0 < secs_left <= 20.0:
        return "T2"
    if 10.0 < secs_left <= 15.0:
        return "T3"
    if 7.0 < secs_left <= 10.0:
        return "T4"
    if 5.0 < secs_left <= 7.0:
        return "T5"
    if 4.0 < secs_left <= 5.0:
        return "T6"
    return "N/A"


def _pnl_summary(trades: list[dict], stats: dict, balance: dict) -> dict:
    entries = [float(t.get("entry_price") or 0.0) for t in trades if float(t.get("entry_price") or 0.0) > 0]
    avg_entry = round(sum(entries) / len(entries), 4) if entries else 0.0
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda t: float(t.get("timestamp") or 0.0)):
        if not trade.get("resolved"):
            continue
        equity += float(trade.get("pnl") or 0.0)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return {
        "total_pnl": stats["total_pnl"],
        "win_rate": stats["win_rate"],
        "total_trades": stats["trades_total"],
        "avg_entry": avg_entry,
        "max_drawdown": round(max_drawdown, 4),
        "current_capital": float(balance.get("balance", 0.0) or 0.0),
        "initial_capital": float(balance.get("initial", 0.0) or 0.0),
        "total_capital": float(os.getenv("DASHBOARD_TOTAL_CAPITAL_USD", "1000.0")),
    }


def _first_spread_na_by_window(snapshots: list[dict]) -> dict[int, dict]:
    grouped: dict[int, list[dict]] = {}
    for snapshot in snapshots:
        window_ts = int(snapshot.get("window_ts") or 0)
        if window_ts <= 0:
            continue
        grouped.setdefault(window_ts, []).append(snapshot)

    out: dict[int, dict] = {}
    for window_ts, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: float(row.get("timestamp") or 0.0))
        price_ready = {"UP": False, "DOWN": False}
        for row in ordered:
            candidates = []
            for outcome, price_key, spread_key in (
                ("UP", "up_price", "up_spread"),
                ("DOWN", "down_price", "down_spread"),
            ):
                if not price_ready[outcome] and float(row.get(price_key) or 0.0) >= 0.99:
                    price_ready[outcome] = True
                if price_ready[outcome] and spread_key in row and row.get(spread_key) is None:
                    candidates.append(outcome)
            if candidates:
                out[window_ts] = {
                    "secs_left": float(row.get("secs_left") or 0.0),
                    "timestamp": float(row.get("timestamp") or 0.0),
                    "side": "/".join(candidates),
                    "sides": candidates,
                    }
                break
    return out


def _recent_trades(
    trades: list[dict],
    snapshots: list[dict] | None = None,
    *,
    limit: int = 1000,
) -> list[dict]:
    spread_na_by_window = _first_spread_na_by_window(
        snapshots if snapshots is not None else st.load_snapshots()
    )
    rows: list[dict] = []
    for trade in reversed(trades[-limit:]):
        btc_open = float(trade.get("btc_open") or 0.0)
        btc_at_entry = float(trade.get("btc_at_entry") or 0.0)
        raw_delta = float(trade.get("btc_distance") or 0.0)
        resolution_target = float(trade.get("resolution_price_to_beat") or btc_open or 0.0)
        if resolution_target > 0 and btc_at_entry > 0:
            btc_delta = round(btc_at_entry - resolution_target, 2)
        elif str(trade.get("outcome") or "").upper() == "DOWN" and raw_delta > 0:
            btc_delta = round(-raw_delta, 2)
        else:
            btc_delta = round(raw_delta, 2)
        btc_at_close = float(trade.get("btc_at_close") or 0.0)
        btc_delta_resolved = (
            round(btc_at_close - resolution_target, 2)
            if trade.get("resolved") and btc_at_close > 0 and resolution_target > 0
            else None
        )
        rows.append({
            "timestamp": trade.get("timestamp", 0.0),
            "window_ts": trade.get("window_ts", 0),
            "market_slug": trade.get("market_slug", ""),
            "outcome": trade.get("outcome", ""),
            "entry_price": trade.get("entry_price", 0.0),
            "amount_usd": trade.get("amount_usd", 0.0),
            "shares": trade.get("shares", 0.0),
            "btc_open": btc_open,
            "resolution_price_to_beat": trade.get("resolution_price_to_beat"),
            "btc_at_entry": btc_at_entry,
            "btc_at_close": btc_at_close or None,
            "secs_left": trade.get("secs_left", 0.0),
            "btc_distance": btc_delta,
            "btc_delta_entry": btc_delta,
            "btc_delta_resolved": btc_delta_resolved,
            "trigger": trade.get("trigger", ""),
            "fire_layer": _trade_fire_layer(trade),
            "trigger_reason": trade.get("trigger_reason", ""),
            "actual": trade.get("actual", ""),
            "resolved": trade.get("resolved", False),
            "won": trade.get("won"),
            "pnl": trade.get("pnl", 0.0),
            "mock": trade.get("mock", False),
            "resolution_source": trade.get("resolution_source", ""),
            "first_spread_na": spread_na_by_window.get(int(trade.get("window_ts") or 0), {}),
        })
    return rows


def _pnl_history(trades: list[dict], *, limit: int | None = None) -> list[dict]:
    rows: list[dict] = []
    cumulative = 0.0
    ordered = sorted(trades, key=lambda item: float(item.get("timestamp") or 0.0))
    if limit is not None and limit > 0:
        ordered = ordered[-limit:]
    for trade in ordered:
        if not trade.get("resolved"):
            continue
        pnl = float(trade.get("pnl") or 0.0)
        cumulative += pnl
        rows.append({
            "timestamp": trade.get("timestamp", 0.0),
            "pnl": round(pnl, 4),
            "cumulative": round(cumulative, 4),
            "layer": _trade_fire_layer(trade),
        })
    return rows


def _end_window_rules(settings: st.BotSettings | None = None) -> list[dict]:
    settings = settings or st.load_settings()
    cfg = end_window.EndWindowConfig.from_settings(settings)
    rules = sorted(cfg.layers, key=lambda layer: layer.seconds_left_max, reverse=True)
    return [
        {
            "name": layer.name,
            "display_name": layer.name,
            "label": f"{int(layer.seconds_left_max)}s layer",
            "seconds_left_max": layer.seconds_left_max,
            "seconds_left_min": layer.seconds_left_min,
            "min_distance_usd": layer.min_distance_usd,
            "min_price": layer.min_price,
            "max_price": layer.max_price,
            "enabled": bool(getattr(settings, f"{layer.name.lower()}_enabled", True)),
        }
        for layer in rules
    ]


def _end_window_settings(settings: st.BotSettings | None = None) -> dict:
    settings = settings or st.load_settings()
    cfg = end_window.EndWindowConfig.from_settings(settings)
    return {
        "market_5m_enabled": bool(getattr(settings, "market_5m_enabled", True)),
        "trade_usd": cfg.trade_usd,
        "min_trade_usd": cfg.min_trade_usd,
        "max_trades_per_window": cfg.max_trades_per_window,
        "time_min_delta_usd": TIME_MIN_DELTA_USD,
        "max_spread": cfg.max_spread,
        "force_retry_attempts": cfg.force_retry_attempts,
        "force_retry_delay_secs": cfg.force_retry_delay_secs,
        "force_final_price_cap": cfg.force_final_price_cap,
        "min_reasonable_price": cfg.min_reasonable_price,
        "min_side_price_edge": cfg.min_side_price_edge,
        **{
            f"time{index}_{field}": (
                bool(getattr(settings, f"time{index}_enabled", True))
                if field == "enabled"
                else getattr(settings, f"time{index}_{field}")
            )
            for index in range(1, 7)
            for field in ("enabled", "price", "trade_usd", "min_secs_left", "max_secs_left", "min_delta_usd")
        },
        **{
            f"buy1_{field}": getattr(settings, f"buy1_{field}")
            for field in (
                "enabled",
                "trade_usd",
                "min_price",
                "max_price",
                "sell_min_price",
                "sell_max_price",
                "min_delta_usd",
                "max_secs_left",
                "min_secs_left",
                "max_open_positions",
            )
        },
        "order_type": "Market FOK",
    }


def _settings_from_dict(data: dict | None) -> st.BotSettings:
    values = st.asdict(st.BotSettings())
    if isinstance(data, dict):
        values.update({key: value for key, value in data.items() if key in values})
    return st.BotSettings(**values)


def _price_extreme_stats() -> dict:
    data = st.load_price_extremes()
    windows = data.get("windows", []) if isinstance(data, dict) else []
    levels: dict[str, dict] = {}
    for row in windows:
        hits = row.get("hits", {}) if isinstance(row, dict) else {}
        if not isinstance(hits, dict):
            continue
        for level, sides in hits.items():
            bucket = levels.setdefault(
                str(level),
                {"level": float(level), "up_hits": 0, "down_hits": 0, "windows": 0},
            )
            if isinstance(sides, dict):
                up_hit = sides.get("up_ts") is not None
                down_hit = sides.get("down_ts") is not None
                bucket["up_hits"] += 1 if up_hit else 0
                bucket["down_hits"] += 1 if down_hit else 0
                bucket["windows"] += 1 if up_hit or down_hit else 0
    return {
        "price_source": data.get("price_source", "") if isinstance(data, dict) else "",
        "sample_interval_secs": data.get("sample_interval_secs", 0.0) if isinstance(data, dict) else 0.0,
        "recorded_windows": len(windows),
        "levels": sorted(levels.values(), key=lambda item: item["level"]),
    }


def _low_price_winner_stats(trades: list[dict]) -> dict:
    threshold = float(st.LOW_PRICE_WIN_THRESHOLD)
    actual_by_window: dict[int, str] = {}
    for trade in trades:
        if not trade.get("resolved"):
            continue
        actual = str(trade.get("actual") or "").upper()
        window_ts = int(trade.get("window_ts") or 0)
        if window_ts > 0 and actual in ("UP", "DOWN"):
            actual_by_window[window_ts] = actual

    windows = st.load_price_extremes().get("windows", [])
    observed: dict[int, dict] = {}
    for row in windows:
        window_ts = int(row.get("window_ts") or 0)
        if window_ts <= 0:
            continue
        observed[window_ts] = {
            "market_slug": row.get("market_slug") or f"btc-updown-5m-{window_ts}",
            "min_up_bid": float(row.get("min_up_bid") or 0.0),
            "min_down_bid": float(row.get("min_down_bid") or 0.0),
            "up_ts": float(((row.get("low_hits") or {}).get(f"{threshold:.2f}") or {}).get("up_ts") or 0.0),
            "down_ts": float(((row.get("low_hits") or {}).get(f"{threshold:.2f}") or {}).get("down_ts") or 0.0),
        }
    resolved_observed = 0
    winner_low_count = 0
    recent = []
    for window_ts, row in observed.items():
        actual = actual_by_window.get(window_ts)
        if actual not in ("UP", "DOWN"):
            continue
        min_price = float(row.get(f"min_{actual.lower()}_bid") or 0.0)
        if min_price <= 0:
            continue
        resolved_observed += 1
        if min_price > threshold:
            continue
        winner_low_count += 1
        recent.append({
            "window_ts": window_ts,
            "market_slug": row.get("market_slug") or f"btc-updown-5m-{window_ts}",
            "winner": actual,
            "min_price": min_price,
            "hit_ts": float(row.get(f"{actual.lower()}_ts") or 0.0),
        })
    recent.sort(key=lambda row: row["hit_ts"], reverse=True)
    return {
        "threshold": threshold,
        "winner_low_count": winner_low_count,
        "resolved_observed_windows": resolved_observed,
        "recorded_windows": len(observed),
        "rate_pct": round(winner_low_count / resolved_observed * 100, 2) if resolved_observed else 0.0,
        "recent": recent,
    }


def _state_payload() -> dict:
    state = st.load_state()
    snapshots = st.load_snapshots()
    context_snapshots = snapshots[-STATE_CONTEXT_SNAPSHOT_LIMIT:]
    context = analyze_market_context(context_snapshots)
    state.update({
        "current_regime": context["regime"],
        "regime_reason": context["reason"],
        "market_context_source": context["source"],
        "market_context_confidence": context["confidence"],
        "market_context_coverage_20m": context["coverage_20m"],
        "delta_10s": context["delta_10s"],
        "delta_10s_avg_20m": context["avg_signed_delta_10s_20m"],
        "delta_10s_abs_avg_20m": context["avg_abs_delta_10s_20m"],
        "delta_10s_abs_p90_20m": context["p90_abs_delta_10s_20m"],
        "trend_net_move_20m": context["net_move_20m"],
        "trend_slope_per_min_20m": context["slope_per_min_20m"],
        "trend_efficiency_20m": context["efficiency_20m"],
        "saturation_avg_secs_30m": context["saturation_avg_secs_30m"],
        "saturation_samples_30m": context["saturation_samples_30m"],
        "locked_avg_secs_30m": context["locked_avg_secs_30m"],
        "locked_samples_30m": context["locked_samples_30m"],
        "saturation_completed_windows_30m": context["completed_windows_30m"],
        "gamma_market_context": dict(_gamma_context),
    })
    saved_settings = st.load_settings()
    active_settings = _settings_from_dict(state.get("active_settings"))
    has_window = int(state.get("current_window") or 0) > 0 and float(state.get("seconds_left") or 0) > 0
    if not has_window:
        active_settings = saved_settings
    strategy_keys = [
        key for key in st.asdict(saved_settings)
        if (
            key.startswith("t")
            or key.startswith("time")
            or key == "market_5m_enabled"
            or key == "max_trades_per_window"
        )
    ]
    pending = any(
        getattr(active_settings, key) != getattr(saved_settings, key)
        for key in strategy_keys
    )
    trades = st.load_trades()
    bal = st.load_balance()
    stats = st.calc_stats(trades)
    pnl_calendar = _build_pnl_calendar(trades)
    today_pnl = pnl_calendar["today"]
    low_price_winner_stats = _low_price_winner_stats(trades)
    low_price_winner_stats["recent"] = low_price_winner_stats.get("recent", [])[:STATE_RESEARCH_LIMIT]
    state.update({
        "strategy_mode": _strategy_label(),
        "balance": bal.get("balance", 0.0),
        "initial_balance": bal.get("initial", 0.0),
        "active_settings": st.asdict(active_settings),
        "saved_settings": st.asdict(saved_settings),
        "strategy_settings_pending": pending,
        "trades_total": stats["trades_total"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "low_price_winner_stats": low_price_winner_stats,
        "total_pnl": stats["total_pnl"],
        "daily_pnl": today_pnl["pnl"],
        "daily_trades": today_pnl["trade_count"],
        "daily_wins": today_pnl["wins"],
        "daily_losses": today_pnl["losses"],
        "win_rate": stats["win_rate"],
        "recent_trades": _recent_trades(
            trades,
            context_snapshots,
            limit=STATE_RECENT_TRADE_LIMIT,
        ),
        "end_window_rules": _end_window_rules(active_settings),
        "end_window_settings": _end_window_settings(active_settings),
        "saved_end_window_rules": _end_window_rules(saved_settings),
        "saved_end_window_settings": _end_window_settings(saved_settings),
        "pnl_summary": _pnl_summary(trades, stats, bal),
        "pnl_history": _pnl_history(trades, limit=STATE_PNL_HISTORY_LIMIT),
        "pnl_calendar": pnl_calendar,
        "trading_enabled": st.get_trading_enabled(),
        "emergency_stop": st.get_emergency_stop(),
    })
    current_window = int(state.get("current_window") or 0)
    state["market_interval_secs"] = 300
    state["market_interval_label"] = "5m"
    current_slug = f"btc-updown-5m-{current_window}" if current_window > 0 else ""
    state["market_slug"] = current_slug
    state["market_url"] = f"https://polymarket.com/event/{current_slug}" if current_slug else ""
    if config.MOCK_MODE:
        ledger = st.calc_trade_ledger_balance(trades, bal)
        state.update({
            "ledger_balance": ledger["ledger_balance"],
            "ledger_balance_drift": ledger["ledger_balance_drift"],
            "ledger_balance_ok": ledger["ledger_balance_ok"],
        })
    return state


async def index(_request: web.Request) -> web.Response:
    html = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>END_WINDOW Bot</title>
<style>
*{box-sizing:border-box}
:root{--bg:#070b12;--panel:#111827;--panel2:#151b26;--line:#263244;--text:#f8fafc;--muted:#94a3b8;--up:#22c55e;--down:#f97316;--wait:#3b82f6;--no:#ef4444;--warn:#facc15;--mock:#a855f7;--live:#10b981;--emergency:#dc2626}
body{margin:0;background:radial-gradient(circle at top left,rgba(59,130,246,.12),transparent 34%),var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.45}
button,input,select{font:inherit}
button{border:1px solid var(--line);background:#182235;color:var(--text);border-radius:6px;padding:9px 12px;cursor:pointer}
button:hover{border-color:#3b4a64;background:#202d44}
button.primary{background:#12351f;border-color:#1f8a4c;color:#dcffe9}
button.danger{background:#3b1014;border-color:var(--emergency);color:#ffe4e6}
button.ghost{background:transparent}
button.mini{padding:5px 8px;font-size:12px}
input,select{width:100%;border:1px solid var(--line);background:#0b111c;color:var(--text);border-radius:6px;padding:8px}
.page{max-width:1540px;margin:0 auto;padding:20px}
.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:16px;flex-wrap:wrap}
.title h1{font-size:28px;margin:0 0 4px}
.subtitle{color:var(--muted);font-size:13px}
.actions,.badges{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.toolbar{display:flex;gap:10px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted);background:#0b111c;font-size:12px;font-weight:700}
.badge.wait{border-color:var(--wait);color:#bfdbfe}.badge.no{border-color:var(--no);color:#fecaca}.badge.up{border-color:var(--up);color:#bbf7d0}.badge.down{border-color:var(--down);color:#fed7aa}.badge.mock{border-color:var(--mock);color:#e9d5ff}.badge.live{border-color:var(--live);color:#bbf7d0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.dot.on{background:var(--up);box-shadow:0 0 0 3px rgba(34,197,94,.15)}.dot.off{background:var(--no)}
.health-dot.ok{background:var(--up)}.health-dot.bad{background:var(--no)}.health-dot.warn{background:var(--warn)}
.grid-top{display:grid;grid-template-columns:minmax(340px,1.35fr) minmax(280px,.85fr) minmax(320px,1fr);gap:14px;margin-bottom:14px}
.grid-mid{display:grid;grid-template-columns:minmax(330px,.9fr) minmax(360px,1.15fr) minmax(280px,.8fr);gap:14px;margin-bottom:14px}
.grid-log{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:14px}
.bottom{display:grid;grid-template-columns:minmax(260px,.65fr) minmax(460px,1.35fr);gap:14px;margin-bottom:18px}.settings-panel{grid-column:1/-1}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.panel{background:linear-gradient(180deg,rgba(255,255,255,.025),transparent),var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 16px 40px rgba(0,0,0,.22);overflow:hidden}.market-context-panel{margin-bottom:14px}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-bottom:1px solid var(--line);background:var(--panel2);flex-wrap:wrap}
.panel-head h2{font-size:14px;margin:0;text-transform:uppercase;letter-spacing:.06em;color:#dbeafe}
.panel-body{padding:14px}
.decision-card{min-height:282px;border-width:1px}.decision-card.wait{border-color:var(--wait);box-shadow:0 0 0 1px rgba(59,130,246,.2),0 16px 42px rgba(59,130,246,.08)}.decision-card.no{border-color:var(--no);box-shadow:0 0 0 1px rgba(239,68,68,.2),0 16px 42px rgba(239,68,68,.08)}.decision-card.up{border-color:var(--up);box-shadow:0 0 0 1px rgba(34,197,94,.22),0 16px 42px rgba(34,197,94,.08)}.decision-card.down{border-color:var(--down);box-shadow:0 0 0 1px rgba(249,115,22,.22),0 16px 42px rgba(249,115,22,.08)}
.decision-main{font-size:42px;font-weight:900;line-height:1;letter-spacing:.01em}.decision-main.wait{color:var(--wait)}.decision-main.no{color:var(--no)}.decision-main.up{color:var(--up)}.decision-main.down{color:var(--down)}
.reason-line{font-size:16px;font-weight:700;margin-top:10px}.action-line{margin-top:10px;color:var(--muted);font-weight:700}
.decision-checklist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:12px}
.decision-checklist .check{padding:7px 8px;font-size:12px}
.metric-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:16px}.market-context-strip{display:flex;gap:10px;overflow-x:auto;padding:14px}.market-context-strip .metric{flex:0 0 172px}.market-context-strip .metric.wide{flex-basis:210px}
.metric{background:#0b111c;border:1px solid rgba(148,163,184,.18);border-radius:7px;padding:10px;min-width:0}
.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em}.value{font-size:22px;font-weight:800;margin-top:3px}.mono{font-family:Consolas,Menlo,monospace}.hint{color:var(--muted);font-size:12px;margin-top:4px}.muted{color:var(--muted)}.good{color:var(--up)}.bad{color:var(--no)}.warn{color:var(--warn)}.info{color:var(--wait)}.down-text{color:var(--down)}
.bar{height:9px;border-radius:999px;background:#0b111c;border:1px solid rgba(148,163,184,.2);overflow:hidden;margin-top:10px}.bar>span{display:block;height:100%;width:0;background:var(--wait);transition:width .25s ease}.bar>span.good{background:var(--up)}.bar>span.warn{background:var(--warn)}.bar>span.bad{background:var(--no)}
.book-row{display:grid;grid-template-columns:72px repeat(4,minmax(0,1fr));gap:8px;align-items:center;padding:10px;border:1px solid rgba(148,163,184,.14);border-radius:7px;margin-bottom:8px;background:#0b111c}.book-row.lead-up{border-color:rgba(34,197,94,.65);background:rgba(34,197,94,.08)}.book-row.lead-down{border-color:rgba(249,115,22,.65);background:rgba(249,115,22,.08)}
.checklist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.check{display:flex;align-items:center;gap:8px;border:1px solid rgba(148,163,184,.14);border-radius:7px;background:#0b111c;padding:9px}.check .mark{width:20px;height:20px;display:grid;place-items:center;border-radius:50%;font-size:12px;font-weight:900}.check.ok .mark{background:rgba(34,197,94,.17);color:var(--up)}.check.bad .mark{background:rgba(239,68,68,.17);color:var(--no)}.check.warn .mark{background:rgba(250,204,21,.16);color:var(--warn)}
.layers{display:grid;gap:8px}.layer{display:grid;grid-template-columns:52px minmax(0,1fr) 82px;gap:10px;align-items:center;border:1px solid rgba(148,163,184,.14);border-radius:7px;background:#0b111c;padding:9px}.layer.active{border-color:var(--warn);box-shadow:0 0 0 1px rgba(250,204,21,.15)}.layer.next{border-color:var(--wait)}.layer.expired{opacity:.48}.layer.no-trade{border-color:var(--no);background:rgba(239,68,68,.08)}
.status-pill{justify-self:end;border:1px solid var(--line);border-radius:999px;padding:4px 8px;font-size:11px;font-weight:800}.status-pill.active{border-color:var(--warn);color:var(--warn)}.status-pill.next{border-color:var(--wait);color:#bfdbfe}.status-pill.expired{color:var(--muted)}.status-pill.locked{color:var(--muted)}.status-pill.no{border-color:var(--no);color:#fecaca}
.health-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.health-item{border:1px solid rgba(148,163,184,.14);border-radius:7px;background:#0b111c;padding:9px}
.reason-list{display:grid;gap:6px;max-height:380px;overflow:auto}.reason-item{border-left:3px solid var(--wait);background:#0b111c;border-radius:5px;padding:6px 8px;font-size:11px}.reason-item.no{border-left-color:var(--no)}.reason-item.up{border-left-color:var(--up)}.reason-item.down{border-left-color:var(--down)}.reason-item.wait{border-left-color:var(--wait)}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:1120px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid rgba(148,163,184,.12);vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;background:#0b111c;position:sticky;top:0}.reason{max-width:460px;color:var(--muted);font-size:12px}
.trade-scroll{max-height:570px}.runtime-log{max-height:145px}
.market-link{display:inline-grid;place-items:center;width:28px;height:28px;border:1px solid var(--line);border-radius:6px;background:#0b111c;color:#bfdbfe;text-decoration:none;font-size:17px;line-height:1}.market-link:hover{border-color:var(--wait);background:rgba(59,130,246,.16);color:#fff}.market-link-cell{width:34px;padding-right:2px}
.current-market-link{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;padding:10px;border:1px solid rgba(59,130,246,.45);border-radius:7px;background:rgba(59,130,246,.09);color:#bfdbfe;text-decoration:none}.current-market-link:hover{background:rgba(59,130,246,.16);border-color:var(--wait);color:#fff}.current-market-link strong{font-size:12px}.current-market-link span{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.liquidity-strip{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.liquidity-box,.extreme-level{padding:10px;border:1px solid var(--line);border-radius:7px;background:#0b111c}.extreme-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.extreme-level .price{font-size:20px;font-weight:800}.extreme-events{margin-top:10px;max-height:190px;overflow:auto}
.trade-tools{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}.trade-filters,.pagination{display:flex;align-items:center;gap:5px;flex-wrap:wrap}.trade-filters button,.pagination button{min-width:34px;padding:6px 9px;background:#0b111c}.trade-filters button.active,.pagination button.active{border-color:var(--wait);color:#dbeafe;background:rgba(59,130,246,.16)}.trade-footer{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 14px;border-top:1px solid var(--line);background:var(--panel2);flex-wrap:wrap}
.summary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.calendar-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(34px,1fr));gap:6px}.dow{color:var(--muted);font-size:11px;text-align:center;padding:3px}.day{min-height:62px;border:1px solid var(--line);border-radius:6px;padding:6px;background:#0b111c}.day.out{opacity:.34}.day.empty{background:#0d1218}.day.win{border-color:rgba(34,197,94,.58);background:rgba(34,197,94,.12)}.day.loss{border-color:rgba(239,68,68,.58);background:rgba(239,68,68,.12)}.day-num{font-size:12px;color:var(--muted)}.day-pnl{font-weight:800;margin-top:3px}.day-meta{font-size:11px;color:var(--muted)}
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.settings-section{border:1px solid rgba(148,163,184,.14);background:#0b111c;border-radius:7px;padding:10px}.settings-section h3{font-size:12px;text-transform:uppercase;letter-spacing:.07em;margin:0 0 8px;color:#dbeafe}.form-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}.full{grid-column:1/-1}
.layer-toggle{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid rgba(148,163,184,.12)}.layer-toggle:last-child{border-bottom:0}.switch{position:relative;width:42px;height:22px;display:inline-block;flex:none}.switch input{position:absolute;inset:0;z-index:2;opacity:0;width:100%;height:100%;cursor:pointer}.slider{position:absolute;inset:0;border-radius:999px;background:#334155;cursor:pointer;transition:.2s}.slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;border-radius:50%;background:#fff;transition:.2s}.switch input:checked+.slider{background:var(--up)}.switch input:checked+.slider:before{transform:translateX(20px)}
.rule-editor{padding:9px 0;border-bottom:1px solid rgba(148,163,184,.12)}.rule-editor:last-of-type{border-bottom:0}.rule-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}.rule-fields{display:grid;grid-template-columns:repeat(5,minmax(62px,1fr));gap:6px}.rule-fields.time{grid-template-columns:repeat(3,minmax(70px,1fr))}.rule-fields label{font-size:9px;color:var(--muted)}.rule-fields input{margin-top:3px;padding:6px;font-size:11px}
.chart-wrap{height:360px;padding:10px 12px;position:relative}.chart-wrap svg{width:100%;height:100%;display:block}.chart-empty{height:100%;display:grid;place-items:center;color:var(--muted)}.chart-tooltip{position:absolute;display:none;pointer-events:none;z-index:3;padding:6px 8px;border:1px solid var(--line);border-radius:5px;background:#050914;color:var(--text);font:11px Consolas,monospace;box-shadow:0 8px 24px rgba(0,0,0,.4);white-space:nowrap}
.toast{position:fixed;right:16px;bottom:16px;background:#0b111c;border:1px solid var(--line);border-radius:7px;padding:10px 12px;color:var(--text);display:none;max-width:360px;z-index:20}
@media (max-width:1180px){.grid-top,.grid-mid,.grid-log,.bottom,.chart-grid{grid-template-columns:1fr}.page{padding:14px}.decision-main{font-size:34px}.settings-grid{grid-template-columns:1fr}}
@media (max-width:760px){.metric-grid,.checklist,.health-grid,.summary-grid,.form-row{grid-template-columns:1fr}.book-row{grid-template-columns:70px repeat(2,minmax(0,1fr))}.book-row .depth,.book-row .cap{display:none}table{min-width:1040px}}
</style></head><body>
<div class="page">
  <header class="topbar">
    <div class="title">
      <h1>END_WINDOW Bot</h1>
      <div class="subtitle">BTC Up/Down | <span id="market">Market window N/A</span> | <span id="updated">Last updated N/A</span></div>
    </div>
    <div class="toolbar">
      <div class="badges">
        <span class="badge"><span id="runDot" class="dot"></span><span id="runText">STOPPED</span></span>
        <span id="modeBadge" class="badge mock">MOCK</span>
        <span id="apiBadge" class="badge"><span class="dot health-dot warn"></span>API N/A</span>
        <span id="chainBadge" class="badge"><span class="dot health-dot warn"></span>Chainlink N/A</span>
        <span id="clobBadge" class="badge"><span class="dot health-dot warn"></span>CLOB N/A</span>
      </div>
      <div class="actions">
        <button class="primary" onclick="control(true)">START</button>
        <button onclick="control(false)">STOP</button>
        <button class="danger" onclick="emergencyStop()">EMERGENCY</button>
        <button onclick="refreshPendingGamma()">Gamma</button>
        <button class="ghost" onclick="refresh()">REFRESH</button>
      </div>
    </div>
  </header>

  <section class="grid-top">
    <article id="decisionCard" class="panel decision-card wait">
      <div class="panel-head">
        <h2>Current Decision</h2>
        <span id="decisionBadge" class="badge wait">WAIT</span>
      </div>
      <div class="panel-body">
        <div id="decisionText" class="decision-main wait">WAIT</div>
        <div id="decisionReason" class="reason-line">Loading decision...</div>
        <div id="decisionAction" class="action-line">No order will be placed</div>
        <div id="decisionChecklist" class="decision-checklist"></div>
        <div class="metric-grid">
          <div class="metric"><div class="label">Layer</div><div id="decisionLayer" class="value mono">N/A</div></div>
          <div class="metric"><div class="label">Seconds Left</div><div id="decisionSeconds" class="value mono">N/A</div></div>
          <div class="metric"><div class="label">Required Delta</div><div id="decisionRequired" class="value mono">N/A</div></div>
          <div class="metric"><div class="label">Current Delta</div><div id="decisionDelta" class="value mono">N/A</div></div>
        </div>
      </div>
    </article>

    <article class="panel">
      <div class="panel-head"><h2>BTC Delta</h2><span id="leadingBadge" class="badge wait">N/A</span></div>
      <div class="panel-body">
        <div class="metric-grid">
          <div class="metric"><div class="label">BTC Price</div><div id="btcPrice" class="value mono">N/A</div></div>
          <div class="metric"><div class="label">Open / Beat</div><div id="btcOpen" class="value mono">N/A</div></div>
          <div class="metric full"><div class="label">Delta</div><div id="btcDelta" class="value mono">N/A</div><div id="btcDeltaHint" class="hint">N/A</div><div class="bar"><span id="deltaBar"></span></div></div>
          <div class="metric"><div class="label">Required</div><div id="requiredDelta" class="value mono">N/A</div></div>
          <div class="metric"><div class="label">Progress</div><div id="deltaProgress" class="value mono">N/A</div></div>
        </div>
      </div>
    </article>

    <article class="panel">
      <div class="panel-head"><h2>Orderbook</h2><span id="priceBadge" class="badge wait">N/A</span></div>
      <div class="panel-body">
        <div class="book-row" id="upBook"><strong class="good">UP</strong><div><div class="label">Ask</div><div id="upAsk" class="mono">N/A</div></div><div><div class="label">Bid</div><div id="upBid" class="mono">N/A</div></div><div><div class="label">Spread</div><div id="upSpread" class="mono">N/A</div></div><div class="depth"><div class="label">Depth</div><div class="mono">N/A</div></div></div>
        <div class="book-row" id="downBook"><strong class="down-text">DOWN</strong><div><div class="label">Ask</div><div id="downAsk" class="mono">N/A</div></div><div><div class="label">Bid</div><div id="downBid" class="mono">N/A</div></div><div><div class="label">Spread</div><div id="downSpread" class="mono">N/A</div></div><div class="depth"><div class="label">Depth</div><div class="mono">N/A</div></div></div>
        <div class="metric-grid">
          <div class="metric"><div class="label">Active Ask</div><div id="activeAsk" class="value mono">N/A</div></div>
          <div class="metric"><div class="label">Max Price</div><div id="maxPrice" class="value mono">N/A</div></div>
          <div class="metric full"><div id="orderbookWarning" class="hint">N/A</div></div>
        </div>
        <div class="liquidity-strip">
          <div class="liquidity-box"><div class="label">UP max trade liquidity</div><div id="upLiquidity" class="value mono">N/A</div><div class="muted">visible top-5 asks</div></div>
          <div class="liquidity-box"><div class="label">DOWN max trade liquidity</div><div id="downLiquidity" class="value mono">N/A</div><div class="muted">visible top-5 asks</div></div>
        </div>
        <a id="currentMarketLink" class="current-market-link" href="#" target="_blank" rel="noopener noreferrer"><strong>Open current BTC market</strong><span id="currentMarketSlug">N/A</span></a>
      </div>
    </article>
  </section>

  <section class="panel market-context-panel">
    <div class="panel-head"><h2>Market Context</h2><span class="muted">Gamma official + CLOB + bot spread fallback</span></div>
    <div class="market-context-strip">
      <div class="metric"><div class="label">Start Decision</div><div id="marketVolatility" class="value mono">WAIT</div><div id="marketVolatilityHint" class="hint">Gamma official 3h</div></div>
      <div class="metric"><div class="label">Gamma 3h Avg Delta</div><div id="gammaAvgDelta3h" class="value mono">N/A</div><div id="gammaAvgDelta3hHint" class="hint">Average |final - target|</div></div>
      <div class="metric"><div class="label">Gamma 2h Avg Delta</div><div id="gammaAvgDelta2h" class="value mono">N/A</div><div id="gammaAvgDelta2hHint" class="hint">Average |final - target|</div></div>
      <div class="metric"><div class="label">Gamma 1h Avg Delta</div><div id="gammaAvgDelta1h" class="value mono">N/A</div><div id="gammaAvgDelta1hHint" class="hint">Average |final - target|</div></div>
      <div class="metric"><div class="label">Gamma 30m Avg Delta</div><div id="gammaAvgDelta30m" class="value mono">N/A</div><div id="gammaAvgDelta30mHint" class="hint">Average |final - target|</div></div>
      <div class="metric wide"><div class="label">20m Regime</div><div id="marketRegime" class="value mono">UNKNOWN</div><div id="marketRegimeHint" class="hint">Gamma completed 5m markets</div></div>
      <div class="metric wide"><div class="label">Gamma Avg Delta / 10s</div><div id="delta10s" class="value mono">N/A</div><div id="delta10sStats" class="hint">Derived from official 5m target/final</div></div>
      <div class="metric wide"><div class="label">30m Saturation 0.94</div><div id="saturationTiming" class="value mono">N/A</div><div id="saturationHint" class="hint">First leading bid >= 0.94</div></div>
      <div class="metric wide"><div class="label">30m Locked / N/A</div><div id="lockedTiming" class="value mono">N/A</div><div id="lockedHint" class="hint">First price 1.00 or leading spread N/A</div></div>
    </div>
  </section>

  <section class="grid-mid">
    <article class="panel">
      <div class="panel-head"><h2>Entry Checklist</h2><span id="checklistBadge" class="badge wait">N/A</span></div>
      <div class="panel-body"><div id="entryChecklist" class="checklist"></div></div>
    </article>

    <article class="panel">
      <div class="panel-head"><h2>Layer Progress</h2><span id="layerSummary" class="badge wait">N/A</span></div>
      <div class="panel-body">
        <div id="layerHint" class="hint" style="margin-bottom:10px">N/A</div>
        <div id="layers" class="layers"></div>
      </div>
    </article>

    <article class="panel">
      <div class="panel-head"><h2>Data Health</h2><span id="dataBadge" class="badge wait">N/A</span></div>
      <div class="panel-body"><div id="dataHealth" class="health-grid"></div></div>
    </article>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Low Price Winner Research</h2><span id="lowWinnerWindowCount" class="muted">0 windows</span></div>
    <div class="panel-body">
      <div id="lowWinnerSummary" class="extreme-grid"></div>
      <div class="muted" style="margin-top:8px">Counted once per resolved window when the winning side's best bid was at or below 0.10.</div>
      <div id="lowWinnerEvents" class="extreme-events"></div>
      <div class="trade-footer"><span id="lowWinnerPageInfo" class="muted">Page 1 / 1</span><div id="lowWinnerPagination" class="pagination"></div></div>
    </div>
  </section>

  <section class="grid-log">
    <article class="panel">
      <div class="panel-head">
        <h2>Recent Trades</h2>
        <div class="trade-tools">
          <div class="trade-filters">
            <button id="tradeFilterAll" class="active" onclick="setTradeFilter('all')">All</button>
            <button id="tradeFilterProfit" onclick="setTradeFilter('profit')">Profit</button>
            <button id="tradeFilterLoss" onclick="setTradeFilter('loss')">Loss</button>
          </div>
          <span id="tradeCount" class="muted">N/A</span>
        </div>
      </div>
      <div class="table-wrap trade-scroll">
        <table>
          <thead><tr><th aria-label="Market">Market</th><th>Time</th><th>Fire T</th><th>Side</th><th>Entry</th><th>Size</th><th>BTC Entry</th><th>BTC Close</th><th>BTC Delta Entry</th><th>BTC Delta Resolved</th><th>Spread N/A</th><th>Result</th><th>PnL</th><th>Gamma</th></tr></thead>
          <tbody id="recentTrades"><tr><td colspan="14" class="muted">No trades yet</td></tr></tbody>
        </table>
      </div>
      <div class="trade-footer"><span id="tradePageInfo" class="muted">Page 1 / 1</span><div id="tradePagination" class="pagination"></div></div>
    </article>
  </section>

  <section class="chart-grid">
    <article class="panel">
      <div class="panel-head"><h2>PnL Per Trade</h2><span class="muted">X: USD | Y: time</span></div>
      <div id="pnlBars" class="chart-wrap"></div>
    </article>
    <article class="panel">
      <div class="panel-head"><h2>PnL Journey</h2><span id="pnlJourneyTotal" class="muted">from bot start</span></div>
      <div id="pnlJourney" class="chart-wrap"></div>
    </article>
  </section>

  <section class="bottom">
    <article class="panel">
      <div class="panel-head"><h2>PnL Summary</h2><span id="pnlMode" class="badge mock">MOCK</span></div>
      <div class="panel-body"><div id="pnlSummary" class="summary-grid"></div></div>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h2>PnL Calendar</h2>
        <div class="actions"><button onclick="shiftMonth(-1)">Prev</button><button onclick="shiftMonth(1)">Next</button></div>
      </div>
      <div class="panel-body">
        <div class="calendar-head"><strong id="calTitle">N/A</strong><span id="calSummary" class="muted">N/A</span></div>
        <div class="calendar-grid" id="calendar"></div>
      </div>
    </article>

    <article class="panel settings-panel">
      <div class="panel-head"><h2>Settings Panel</h2><span id="settingsSaved" class="muted">N/A</span></div>
      <div class="panel-body">
        <div class="settings-grid">
          <section class="settings-section">
            <h3>A. Trading Settings</h3>
            <div class="form-row"><div><span class="label">BTC 5m</span><label class="switch" aria-label="BTC 5m"><input id="market5m" type="checkbox" checked disabled><span class="slider"></span></label></div></div>
            <div class="form-row"><label><span class="label">Trade USD</span><input id="tradeAmount" type="number" min="1" step="1"></label><label><span class="label">Max/window</span><input id="maxTrades" type="number" min="1" step="1"></label><label><span class="label">Profit Stop %</span><input id="profitStopPct" type="number" min="1" step="1" value="100"></label></div>
            <div class="form-row"><label><span class="label">Order type</span><input id="orderType" value="FOK" disabled></label><label><span class="label">Max buy price</span><input id="maxBuyPrice" value="N/A" disabled></label></div>
            <button onclick="saveSettings()">Save Trading Settings</button>
          </section>
          <section class="settings-section">
            <h3>B. Risk Settings</h3>
            <div class="form-row"><label><span class="label">Risk/trade</span><input id="riskTrade" value="N/A" disabled></label><label><span class="label">Max daily loss</span><input id="maxDailyLoss" value="N/A" disabled></label></div>
            <div class="form-row"><label><span class="label">Max loss streak</span><input id="maxLossStreak" value="N/A" disabled></label><label><span class="label">Emergency</span><input id="emergencyStatus" value="N/A" disabled></label></div>
          </section>
          <section class="settings-section">
            <h3>C. Data Settings</h3>
            <div class="form-row"><label><span class="label">Max Chainlink age</span><input id="maxChainlinkAge" value="10s" disabled></label><label><span class="label">Max exchange age</span><input id="maxExchangeAge" value="10s" disabled></label></div>
            <div class="form-row"><label><span class="label">Max latency</span><input id="maxLatency" value="10ms" disabled></label><label><span class="label">Max spread</span><input id="maxSpreadSetting" value="N/A" disabled></label></div>
          </section>
          <section class="settings-section">
            <h3>D. Layer Settings</h3>
            <div id="layerSettings"></div>
            <button style="margin-top:10px" onclick="saveLayerSettings()">Save For Next Window</button>
          </section>
          <section class="settings-section full">
            <h3>Tools</h3>
            <div class="actions"><button onclick="refreshPendingGamma()">Reconcile Gamma</button><button onclick="resetData()">Reset mock data</button><button onclick="downloadReport('xlsx')">Excel</button><button onclick="downloadReport('pdf')">PDF</button></div>
          </section>
        </div>
      </div>
    </article>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Runtime Log</h2><span class="muted">latest 12</span></div>
    <div class="panel-body"><div id="logs" class="reason-list runtime-log"></div></div>
  </section>
</div>
<div id="toast" class="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let lastState = {};
let calDate = new Date();
const calendarCache = new Map();
let tradeFilter = 'all';
let tradePage = 1;
let lowWinnerPage = 1;
const TRADE_PAGE_SIZE = 100;
const RESEARCH_PAGE_SIZE = 10;
let layerEditorSignature = '';
let screenWakeLock = null;

async function keepScreenAwake(){
 if(!('wakeLock' in navigator) || document.visibilityState!=='visible' || screenWakeLock)return;
 try{
  screenWakeLock=await navigator.wakeLock.request('screen');
  screenWakeLock.addEventListener('release',()=>{screenWakeLock=null},{once:true});
 }catch(e){
  console.warn('Screen wake lock unavailable:',e.message);
 }
}

document.addEventListener('visibilitychange',()=>{
 if(document.visibilityState==='visible')keepScreenAwake();
});
document.addEventListener('pointerdown',keepScreenAwake,{once:true});
document.addEventListener('keydown',keepScreenAwake,{once:true});

function esc(v){return String(v ?? 'N/A').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function val(v, fallback='N/A'){return v===undefined||v===null||v===''?fallback:v}
function money(v, digits=2){if(v===undefined||v===null||v==='')return 'N/A';const n=Number(v||0);return (n<0?'-$':'$')+Math.abs(n).toFixed(digits)}
function signedMoney(v, digits=1){if(v===undefined||v===null||v==='')return 'N/A';const n=Number(v||0);return (n>0?'+':'')+(n<0?'-$':'$')+Math.abs(n).toFixed(digits)}
function pct(v){if(v===undefined||v===null||v==='')return 'N/A';return Number(v||0).toFixed(1)+'%'}
function num(v,d=2){if(v===undefined||v===null||v==='')return 'N/A';return Number(v||0).toFixed(d)}
function bookValue(v,d=2){const n=Number(v||0);return n>0?n.toFixed(d):'N/A'}
function clsBy(v){return Number(v||0)>0?'good':Number(v||0)<0?'bad':''}
function deltaClass(v){return Number(v||0)<0?'bad':'good'}
function decisionClass(decision){if(String(decision).includes('UP'))return 'up';if(String(decision).includes('DOWN'))return 'down';if(decision==='NO_TRADE')return 'no';return 'wait'}
function depthCapacity(rows,maxPrice){return (Array.isArray(rows)?rows:[]).reduce((total,row)=>{const p=Number(row?.[0]||0),size=Number(row?.[1]||0);return p>0&&size>0&&p<=Number(maxPrice||0)?total+p*size:total},0)}
function triggerLayer(reason,windows){return Object.keys(windows||{}).find((layer)=>String(reason||'').includes(layer))||''}
function toast(msg){const el=$('toast');el.textContent=msg;el.style.display='block';setTimeout(()=>el.style.display='none',2600)}
function fmtTime(ts){if(!ts)return 'N/A';return new Date(Number(ts)*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function stateAge(s){const ts=Number(s.last_update||0);return ts>0?Math.max(0,Date.now()/1000-ts):null}
function apiPct(s){const raw=Number(s.api_error_rate||0);return raw<=1?raw*100:raw}
function setBadge(id, label, kind){const el=$(id); if(!el)return; el.className='badge '+(kind||''); el.innerHTML=label}
function feedAge(s, key, fallback=null){const v=Number(s[key]); if(Number.isFinite(v)&&v>=0)return v; return fallback}
function calendarKey(y,m){return `${Number(y)}-${String(Number(m)).padStart(2,'0')}`}
async function post(url, body={}){
 const r=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
 let data={}; try{data=await r.json()}catch(_){}
 if(!r.ok || data.success===false) throw new Error(data.error||('HTTP '+r.status));
 await refresh(); return data;
}
async function control(on){try{await post('/api/control',{trading_enabled:on});toast(on?'Trading started':'Trading stopped')}catch(e){toast(e.message)}}
async function emergencyStop(){try{await post('/api/emergency-stop',{active:true});toast('Emergency stop active')}catch(e){toast(e.message)}}
async function resetData(){if(!confirm('Reset mock trades, balance, and snapshots?'))return;try{await post('/api/reset',{confirm:true});toast('Mock data reset');await loadCalendar()}catch(e){toast(e.message)}}
async function saveSettings(){
 try{
  const body={
   market_5m_enabled:true,
   trade_amount:Number($('tradeAmount').value||0),
   max_trades_per_window:Number($('maxTrades').value||1),
   profit_stop_pct:Number($('profitStopPct').value||100)
  };
  await post('/api/settings', body); $('settingsSaved').textContent='Saved'; toast('Settings saved');
 }catch(e){toast(e.message)}
}
async function saveLayerSettings(){
 try{
  const body={};
  document.querySelectorAll('#layerSettings [data-setting]').forEach((input)=>{
   body[input.dataset.setting]=input.type==='checkbox'?input.checked:Number(input.value);
  });
  await post('/api/settings',body);
  $('settingsSaved').textContent='Queued for next window';
  toast('Strategy settings saved for next window');
 }catch(e){toast(e.message)}
}
async function toggleLayer(layer, enabled){
 try{
  const timeMatch=String(layer).match(/^TIME-(\\d)$/);
  const key=timeMatch?`time${timeMatch[1]}_enabled`:`${String(layer).toLowerCase()}_enabled`;
  await post('/api/settings',{[key]:!!enabled});
  toast(`${layer} ${enabled?'enabled':'disabled'}`);
 }catch(e){toast(e.message)}
}
function downloadReport(fmt){window.location.href='/api/report?format='+fmt+'&hours=24'}
async function refreshGamma(windowTs, marketSlug){
 try{
  if(!windowTs || !marketSlug) throw new Error('window_ts / market slug missing');
  const data=await post('/api/gamma-refresh',{window_ts:Number(windowTs),market_slug:String(marketSlug)});
  toast(`Gamma refreshed: ${data.actual||'updated'}`);
  await loadCalendar();
 }catch(e){toast('Gamma refresh: '+e.message)}
}
async function refreshPendingGamma(){
 const rows=(lastState.recent_trades||[]).filter((t)=>(!t.resolved||t.resolution_source==='local_close')&&t.window_ts&&t.market_slug);
 if(!rows.length){toast('No pending/local Gamma trades in recent rows');return}
 let ok=0, fail=0;
 for(const t of rows){
  try{await post('/api/gamma-refresh',{window_ts:Number(t.window_ts),market_slug:String(t.market_slug)});ok++}
  catch(_){fail++}
 }
 toast(`Gamma refresh done: ${ok} updated, ${fail} pending`);
 await refresh(); await loadCalendar();
}
document.addEventListener('click',(ev)=>{
  const btn=ev.target.closest('button.gamma-refresh');
  if(!btn)return;
  refreshGamma(Number(btn.dataset.windowTs||0), btn.dataset.marketSlug||'');
});
window.selectMarketMode=function(){
 const m5=$('market5m');
 if(m5)m5.checked=true;
};

function deriveDecision(s){
 const rules=s.end_window_rules||[];
 const enabledRules=rules.filter((r)=>r.enabled!==false);
 const ew=s.end_window_settings||{};
 const secs=Number(s.seconds_left ?? s.secs_left ?? 0);
 const btcPrice=Number(s.btc_price||0);
 const btcOpen=Number(s.btc_open||0);
 const hasBtcOpen=btcPrice>0&&btcOpen>0;
 const delta=hasBtcOpen?btcPrice-btcOpen:0;
 const absDelta=Math.abs(delta);
 const side=delta>0?'UP':delta<0?'DOWN':String(s.leading||'').toUpperCase()||'N/A';
 const upAsk=Number(s.up_ask||0), downAsk=Number(s.down_ask||0), upBid=Number(s.up_price||0), downBid=Number(s.down_price||0);
 const upDepth=Array.isArray(s.up_ask_depth)?s.up_ask_depth:[], downDepth=Array.isArray(s.down_ask_depth)?s.down_ask_depth:[];
 const activeAsk=side==='DOWN'?downAsk:upAsk;
 const activeSpread=side==='DOWN'?Number(s.down_spread||0):Number(s.up_spread||0);
 const maxSpread=Number(ew.max_spread ?? 1.0);
 const tradeUsd=Number(ew.trade_usd||s.active_settings?.trade_amount||0);
 const active=enabledRules.find((r)=>secs<=Number(r.seconds_left_max)&&secs>Number(r.seconds_left_min));
 const maxLayerSeconds=Math.max(...enabledRules.map(r=>Number(r.seconds_left_max||0)),0);
 const next=secs>maxLayerSeconds?enabledRules[0]:enabledRules.find((r)=>secs>Number(r.seconds_left_max))||null;
 const layer=active||next||enabledRules[enabledRules.length-1]||null;
 const required=active?Number(active.min_distance_usd||0):(next?Number(next.min_distance_usd||0):0);
 const maxPrice=active?Number(active.max_price||0):(next?Number(next.max_price||0):Number(ew.force_final_price_cap||0));
 const minPrice=active?Math.max(Number(active.min_price||0),Number(ew.min_reasonable_price||0)):(next?Math.max(Number(next.min_price||0),Number(ew.min_reasonable_price||0)):Number(ew.min_reasonable_price||0));
 const activeDepth=side==='DOWN'?downDepth:upDepth;
 const activeCapacity=depthCapacity(activeDepth,maxPrice);
 const oppositeAsk=side==='DOWN'?upAsk:downAsk;
 const sideEdge=Number(ew.min_side_price_edge||0);
 const age=stateAge(s);
 const priceSource=String(s.price_feed_source||'').trim();
 const chainlinkAge=priceSource?feedAge(s,'chainlink_age_secs',null):null;
 const exchangeAge=feedAge(s,'exchange_age_secs',age);
 const clobAge=feedAge(s,'clob_age_secs',null);
 const latencyMs=Number(s.latency_ms||0);
 const maxFeedAge=10.0;
 const maxLatencyMs=10.0;
 const stateFresh=age!==null&&age<=15;
 const running=!!s.trading_enabled&&!s.emergency_stop&&stateFresh;
 const balanceOk=tradeUsd<=0||Number(s.balance||0)>=tradeUsd;
 const windowCapOk=Number(s.trades_this_window||0)<Number(ew.max_trades_per_window||s.max_trades_per_window||1);
 const riskOk=!s.circuit_breaker_active&&!s.daily_halted;
 const chainlinkFresh=chainlinkAge===null||chainlinkAge<=maxFeedAge;
 const exchangeFresh=exchangeAge!==null&&exchangeAge<=maxFeedAge;
 const latencyOk=!latencyMs||latencyMs<=maxLatencyMs;
 const clobOk=activeAsk>0;
 const spreadKnown=activeSpread>0;
 const spreadOk=!spreadKnown||activeSpread<=maxSpread;
 const priceOk=active?activeAsk>0&&activeAsk>=minPrice&&activeAsk<=maxPrice:false;
 const sidePriceOk=active?(!oppositeAsk||activeAsk+sideEdge>=oppositeAsk):false;
 const deltaOk=active?hasBtcOpen&&absDelta>=required:false;
 const orderbookOk=active?clobOk&&activeCapacity+1e-9>=tradeUsd:false;
 const upAvailable=upAsk>0||upBid>0, downAvailable=downAsk>0||downBid>0;
 const saturated=upAvailable!==downAvailable;
 const openLegs=(Array.isArray(s.open_legs)?s.open_legs:[]).filter((leg)=>!s.current_window||Number(leg.window_ts||0)===Number(s.current_window||0));
 const layerLegs=openLegs.filter((leg)=>!String(leg.trigger_reason||'').includes('TIME-'));
 const slotMode=Number(ew.max_trades_per_window||9)>=3;
 const layerSlotOk=!slotMode||layerLegs.length===0;
 const timeDefaults={1:.98,2:.99,3:.97,4:.96,5:.95,6:.94};
 const timeStates=[1,2,3,4,5,6].map((index)=>{
  const enabled=ew[`time${index}_enabled`]!==false;
  const used=openLegs.some((leg)=>String(leg.trigger_reason||'').includes(`TIME-${index}`));
  const timeOk=secs<=Number(ew[`time${index}_max_secs_left`]||299)&&secs>Number(ew[`time${index}_min_secs_left`]||3);
  const price=Number(ew[`time${index}_price`]||timeDefaults[index]);
  const amount=Number(ew[`time${index}_trade_usd`]||100);
  const minDelta=Number(ew[`time${index}_min_delta_usd`]||ew.time_min_delta_usd||3);
  const deltaOkForTime=hasBtcOpen&&absDelta>=minDelta;
  const quotes=[
   {side:'UP',ask:upAsk,capacity:depthCapacity(upDepth,price)},
   {side:'DOWN',ask:downAsk,capacity:depthCapacity(downDepth,price)},
  ].filter(row=>Math.abs(row.ask-price)<0.000001);
  const candidates=quotes.filter(row=>row.side===side&&row.capacity+1e-9>=amount).sort((a,b)=>b.capacity-a.capacity);
  const candidate=candidates[0]||null;
  const scanning=enabled&&!used&&timeOk;
  const ready=scanning&&deltaOkForTime&&!!candidate&&riskOk&&Number(s.balance||0)>=amount&&windowCapOk;
  return {index,label:`TIME-${index}`,enabled,used,timeOk,price,amount,minDelta,deltaOkForTime,quotes,candidates,candidate,scanning,ready};
 }).sort((a,b)=>a.price-b.price||a.index-b.index);
 const readyTime=timeStates.find(row=>row.ready)||null;
 const scanningTime=timeStates.find(row=>row.scanning)||null;
  let decision='WAIT', reason='Outside end-window', action='No order will be placed';
  if(!running){decision='WAIT';reason=s.emergency_stop?'Emergency stop active':(!stateFresh&&s.trading_enabled?'Bot state stale':'Bot stopped');}
  else if(readyTime){
  decision=`BUY ${readyTime.label} ${readyTime.candidate.side}`;
  reason=`${readyTime.candidate.side} ask reached exactly ${num(readyTime.candidate.ask,2)} with ${money(readyTime.candidate.capacity)} liquidity`;
  action=`Place ${readyTime.candidate.side} FOK order ${money(readyTime.amount,0)} at max ${num(readyTime.price,2)}`;
 }
 else if(secs>maxLayerSeconds){decision='WAIT';reason='Outside end-window';}
 else if(secs<=4){decision='NO_TRADE';reason='Retry disabled below 4s';}
 else if(!active){decision='NO_TRADE';reason='No active layer';}
 else if(!riskOk){decision='NO_TRADE';reason=s.daily_halted?'Daily halt active':'Circuit breaker active';}
 else if(!windowCapOk){decision='NO_TRADE';reason='Max trades per window reached';}
 else if(!layerSlotOk){decision='NO_TRADE';reason='T1-T6 slot already used in this window';}
 else if(!balanceOk){decision='NO_TRADE';reason='Balance below trade size';}
 else if(chainlinkAge!==null&&!chainlinkFresh){decision='NO_TRADE';reason='Chainlink stale';}
 else if(!exchangeFresh){decision='NO_TRADE';reason='Exchange price stale';}
 else if(!latencyOk){decision='NO_TRADE';reason='Latency too high';}
 else if(!clobOk){decision='NO_TRADE';reason='Selected orderbook unavailable';}
 else if(!spreadOk){decision='NO_TRADE';reason='Spread unsafe';}
 else if(!deltaOk){decision='NO_TRADE';reason='Delta below threshold';}
 else if(!priceOk){decision='NO_TRADE';reason=activeAsk<minPrice?'Price unrealistically low':'Price too expensive';}
 else if(!sidePriceOk){decision='NO_TRADE';reason='Selected side not leading orderbook';}
 else if(!orderbookOk){decision='NO_TRADE';reason=`Orderbook capacity ${money(activeCapacity)} below ${money(tradeUsd)}`;}
 else {decision=side==='DOWN'?'BUY_DOWN':'BUY_UP';reason=`${side} delta valid, price sane, data fresh, spread safe`;action=`Place ${decision} order`;}
 if(decision==='WAIT'&&secs>maxLayerSeconds&&next){action='No order will be placed';}
 if(decision==='NO_TRADE')action='No order will be placed';
 return {rules,ew,secs,delta,absDelta,side,upAsk,downAsk,upBid,downBid,upDepth,downDepth,activeAsk,oppositeAsk,activeSpread,maxSpread,tradeUsd,active,next,layer,required,minPrice,maxPrice,maxLayerSeconds,sideEdge,age,stateFresh,priceSource,chainlinkAge,exchangeAge,clobAge,latencyMs,maxFeedAge,maxLatencyMs,running,balanceOk,windowCapOk,layerSlotOk,riskOk,chainlinkFresh,exchangeFresh,latencyOk,clobOk,spreadOk,priceOk,sidePriceOk,deltaOk,orderbookOk,activeCapacity,saturated,openLegs,timeStates,readyTime,scanningTime,decision,reason,action};
}

function renderDecision(s,d){
 const kind=decisionClass(d.decision);
 $('decisionCard').className='panel decision-card '+kind;
 $('decisionText').className='decision-main '+kind;
 $('decisionText').textContent=d.decision;
 setBadge('decisionBadge', d.decision, kind);
 $('decisionReason').textContent=d.reason;
 $('decisionAction').textContent=d.action;
 $('decisionLayer').textContent=d.readyTime?d.readyTime.label:d.active?`${d.active.display_name} ${d.active.label}`:(d.scanningTime?`${d.scanningTime.label} scanning`:d.next?`Next ${d.next.display_name}`:'N/A');
 $('decisionSeconds').textContent=d.secs?num(d.secs,1)+'s':'N/A';
 $('decisionRequired').textContent=d.required?money(d.required,0):'N/A';
 $('decisionDelta').textContent=signedMoney(d.delta,1);
 $('decisionDelta').className='value mono '+deltaClass(d.delta);
 renderDecisionChecklist(d);
}

function renderDecisionChecklist(d){
 let checks=[];
 if(d.readyTime||(!d.active&&d.scanningTime)){
  const time=d.readyTime||d.scanningTime;
  checks=[
   [`More than ${num(d.ew[`time${time.index}_min_secs_left`]||3,1)}s remaining`,time.timeOk],
   [`Aligned BTC delta >= ${money(time.minDelta||d.ew.time_min_delta_usd||3,0)}`,time.deltaOkForTime&&!!time.candidate],
   [`${d.side} ask exactly ${num(time.price,2)}`,time.quotes.length>0],
   [`Orderbook >= ${money(time.amount,0)}`,time.candidates.length>0],
   [`${time.label} slot unused`,!time.used],
  ];
 }else{
  const scan=d.active||d.next;
  const scanMin=Number(scan?.min_price||0),scanMax=Number(scan?.max_price||0);
  const scanAsk=d.activeAsk;
  const scanDepth=d.side==='DOWN'?d.downDepth:d.upDepth;
  const scanCapacity=depthCapacity(scanDepth,scanMax);
  checks=[
   [`${scan?.display_name||'Layer'} time ${num(scan?.seconds_left_max,0)}-${num(scan?.seconds_left_min,0)}s`,!!d.active],
   [`Delta >= ${money(scan?.min_distance_usd||0,0)}`,!!scan&&d.absDelta>=Number(scan.min_distance_usd||0)],
   [`Price ${num(scanMin,2)}-${num(scanMax,2)}`,!!scan&&scanAsk>=scanMin&&scanAsk<=scanMax],
   [`Orderbook >= ${money(d.tradeUsd,0)}`,!!scan&&scanAsk>0&&scanCapacity+1e-9>=d.tradeUsd],
  ];
 }
 $('decisionChecklist').innerHTML=checks.map(([label,ok])=>`<div class="check ${ok?'ok':'bad'}"><span class="mark">${ok?'✓':'×'}</span><span>${esc(label)}</span></div>`).join('');
}

function renderTopBar(s,d){
 const running=d.running;
 $('runDot').className='dot '+(running?'on':'off');
 $('runText').textContent=running?'RUNNING':(!d.stateFresh&&s.trading_enabled?'STALE':'STOPPED');
 setBadge('modeBadge', s.mock_mode?'MOCK':'LIVE', s.mock_mode?'mock':'live');
 $('market').textContent=val(s.market_question||s.current_window||s.market_slug);
 $('updated').textContent='Last updated '+fmtTime(s.last_update);
 const apiOk=apiPct(s)<=5;
 setBadge('apiBadge', `<span class="dot health-dot ${apiOk?'ok':'bad'}"></span>API ${apiOk?'OK':'WARN'}`, apiOk?'up':'no');
 setBadge('chainBadge', `<span class="dot health-dot ${d.chainlinkAge===null?'warn':d.chainlinkFresh?'ok':'bad'}"></span>Chainlink ${d.chainlinkAge===null?'N/A':num(d.chainlinkAge,1)+'s'}`, d.chainlinkAge===null?'wait':d.chainlinkFresh?'up':'no');
 setBadge('clobBadge', `<span class="dot health-dot ${d.clobOk?'ok':'bad'}"></span>CLOB ${d.clobOk?'OK':'BAD'}`, d.clobOk?'up':'no');
}

function renderBtcDelta(s,d){
 const progress=d.required?Math.min(100,(d.absDelta/d.required)*100):0;
 const bar=$('deltaBar');
 $('leadingBadge').textContent=d.side==='DOWN'?'DOWN leading':d.side==='UP'?'UP leading':'N/A';
 $('leadingBadge').className='badge '+(d.side==='DOWN'?'down':d.side==='UP'?'up':'wait');
 $('btcPrice').textContent=money(s.btc_price,1);
 $('btcOpen').textContent=money(s.btc_open,1);
 $('btcDelta').textContent=signedMoney(d.delta,1);
 $('btcDelta').className='value mono '+deltaClass(d.delta);
 $('btcDeltaHint').textContent=d.deltaOk?'Delta threshold met':'Delta below threshold';
 $('requiredDelta').textContent=d.required?money(d.required,0):'N/A';
 $('deltaProgress').textContent=d.required?pct(progress):'N/A';
 const g=s.gamma_market_context||{};
 const regimePct=g.regime_percentages_20m||{};
 $('marketVolatility').textContent=String(g.start_recommendation||'WAIT');
 $('marketVolatility').className='value mono '+(g.start_recommendation==='START'?'good':'warn');
 $('marketVolatilityHint').textContent=`${String(g.market_volatility||'UNKNOWN').replaceAll('_',' ')} | avg |Δ| ${num(g.avg_abs_delta_bps_3h||0,1)} bps | ${Number(g.samples_3h||0)} Gamma markets`;
 const setGammaAvgDelta=(suffix,idSuffix)=>{
  const samples=Number(g[`samples_${suffix}`]||0);
  $(idSuffix).textContent=samples?signedMoney(Number(g[`avg_abs_delta_${suffix}`]||0),1):'N/A';
  $(`${idSuffix}Hint`).textContent=samples?`signed ${signedMoney(Number(g[`avg_signed_delta_${suffix}`]||0),1)} | p90 ${money(g[`p90_abs_delta_${suffix}`]||0,1)} | n=${samples}`:'Average |final - target|';
 };
 setGammaAvgDelta('3h','gammaAvgDelta3h');
 setGammaAvgDelta('2h','gammaAvgDelta2h');
 setGammaAvgDelta('1h','gammaAvgDelta1h');
 setGammaAvgDelta('30m','gammaAvgDelta30m');
 $('marketRegime').textContent=String(g.regime||'UNKNOWN').replaceAll('_',' ');
 $('marketRegimeHint').textContent=`UP ${pct(regimePct.UPTREND||0)} | DOWN ${pct(regimePct.DOWNTREND||0)} | SIDE ${pct(regimePct.SIDEWAYS||0)} | n=${Number(g.regime_samples_20m||0)}`;
 $('delta10s').textContent=g.samples_3h?signedMoney(Number(g.avg_signed_delta_per_10s_3h||0),2):'N/A';
 $('delta10s').className='value mono '+deltaClass(Number(g.avg_signed_delta_per_10s_3h||0));
 $('delta10sStats').textContent=`derived avg |Δ| ${money(g.avg_abs_delta_per_10s_3h||0,2)} | Gamma 5m metadata`;
 const hasGammaTiming=g.completed_windows_30m!==undefined&&g.completed_windows_30m!==null;
 const saturationAvg=hasGammaTiming?g.saturation_avg_secs_30m:s.saturation_avg_secs_30m;
 const saturationSamples=hasGammaTiming?g.saturation_samples_30m:s.saturation_samples_30m;
 const saturationWindows=hasGammaTiming?g.completed_windows_30m:s.saturation_completed_windows_30m;
 const saturationSource=hasGammaTiming?'Gamma+CLOB':'bot snapshots';
 const hasGammaLocked=g.locked_avg_secs_30m!==undefined&&g.locked_avg_secs_30m!==null;
 const hasLocalLocked=Number(s.locked_samples_30m||0)>0;
 const lockedAvg=hasGammaLocked?g.locked_avg_secs_30m:(hasLocalLocked?s.locked_avg_secs_30m:null);
 const lockedSamples=hasGammaLocked?g.locked_samples_30m:(hasLocalLocked?s.locked_samples_30m:g.locked_samples_30m);
 const lockedWindows=hasGammaLocked?g.completed_windows_30m:(hasLocalLocked?s.saturation_completed_windows_30m:saturationWindows);
 const lockedSource=hasGammaLocked?'Gamma price >= 1.00':hasLocalLocked?'bot spread N/A fallback':'Gamma+CLOB';
 $('saturationTiming').textContent=saturationAvg===null||saturationAvg===undefined?'N/A':`~${num(saturationAvg,1)}s`;
 $('saturationHint').textContent=`first touch price >= 0.94 | sample n=${Number(saturationSamples||0)} | ${saturationSource}`;
 $('lockedTiming').textContent=lockedAvg===null||lockedAvg===undefined?'N/A':`~${num(lockedAvg,1)}s`;
 $('lockedHint').textContent=`first touch price >= 1.00 or leading buy N/A | sample n=${Number(lockedSamples||0)} | ${lockedSource}`;
 bar.style.width=progress+'%';
 bar.className=d.deltaOk?'good':progress>70?'warn':'';
}

function renderOrderbook(s,d){
 $('upBook').className='book-row '+(d.side==='UP'?'lead-up':'');
 $('downBook').className='book-row '+(d.side==='DOWN'?'lead-down':'');
 $('upAsk').textContent=bookValue(d.upAsk,2); $('upBid').textContent=bookValue(d.upBid,2); $('upSpread').textContent=bookValue(s.up_spread,3);
 $('downAsk').textContent=bookValue(d.downAsk,2); $('downBid').textContent=bookValue(d.downBid,2); $('downSpread').textContent=bookValue(s.down_spread,3);
 $('activeAsk').textContent=bookValue(d.activeAsk,2);
 $('maxPrice').textContent=d.maxPrice?num(d.maxPrice,2):'N/A';
 $('upLiquidity').textContent=d.upDepth.length?money(depthCapacity(d.upDepth,0.99)):'N/A';
 $('downLiquidity').textContent=d.downDepth.length?money(depthCapacity(d.downDepth,0.99)):'N/A';
 let warning='Entry valid';
 let kind='up';
 if(d.saturated){warning='Market saturated: unavailable side shown as N/A';kind='wait'}
 else if(!d.clobOk){warning='Selected orderbook unavailable';kind='no'}
 else if(!d.spreadOk){warning='Spread unsafe';kind='no'}
 else if(d.active&&!d.priceOk){warning=d.activeAsk<d.minPrice?'Price unrealistically low':'Price too expensive';kind='no'}
 else if(d.active&&!d.sidePriceOk){warning='Selected side not leading';kind='no'}
 else if(d.active&&!d.orderbookOk){warning=`Depth ${money(d.activeCapacity)} below ${money(d.tradeUsd)}`;kind='no'}
 else if(!d.active){warning='Waiting for active layer';kind='wait'}
 $('orderbookWarning').textContent=warning;
 $('orderbookWarning').className='hint '+(kind==='no'?'bad':kind==='up'?'good':'info');
 setBadge('priceBadge', warning, kind);
 const link=$('currentMarketLink');
 const slug=String(s.market_slug||'');
 link.href=s.market_url||'#';
 link.style.pointerEvents=slug?'auto':'none';
 link.style.opacity=slug?'1':'.5';
 $('currentMarketSlug').textContent=slug||'N/A';
}

function renderLowWinnerResearch(s){
 const stats=s.low_price_winner_stats||{}, rows=stats.recent||[];
 const totalPages=Math.max(1,Math.ceil(rows.length/RESEARCH_PAGE_SIZE));
 lowWinnerPage=Math.min(Math.max(1,lowWinnerPage),totalPages);
 const pageRows=rows.slice((lowWinnerPage-1)*RESEARCH_PAGE_SIZE,lowWinnerPage*RESEARCH_PAGE_SIZE);
 $('lowWinnerWindowCount').textContent=`${Number(stats.recorded_windows||0)} windows recorded`;
 $('lowWinnerSummary').innerHTML=[
  ['Winner <= '+num(stats.threshold,2),Number(stats.winner_low_count||0),'qualifying windows'],
  ['Hit rate',pct(stats.rate_pct),`of ${Number(stats.resolved_observed_windows||0)} resolved observed`],
  ['Resolved sample',Number(stats.resolved_observed_windows||0),`${Number(stats.recorded_windows||0)} total windows recorded`],
 ].map(([label,value,detail])=>`<div class="extreme-level"><div class="price">${esc(value)}</div><div><strong>${esc(label)}</strong></div><div class="muted">${esc(detail)}</div></div>`).join('');
 $('lowWinnerEvents').innerHTML=pageRows.length?pageRows.map(row=>`<div class="reason-item wait"><div><strong>${esc(row.winner)} won after <= ${num(stats.threshold,2)}</strong><div class="muted">${esc(row.market_slug)} | minimum winning bid ${num(row.min_price,2)}</div></div><div class="mono">${fmtTime(row.hit_ts)}</div></div>`).join(''):'<div class="muted">No winning side has been recorded at or below 0.10 yet.</div>';
 renderPagination('lowWinner',lowWinnerPage,totalPages,rows.length);
}

function renderPagination(kind,page,totalPages,totalRows){
 const info=$(kind+'PageInfo'), controls=$(kind+'Pagination');
 info.textContent=`Page ${page} / ${totalPages} | ${totalRows} rows`;
 controls.innerHTML=`<button onclick="setPage('${kind}',${page-1})" ${page<=1?'disabled':''}>Prev</button><button class="active" disabled>${page}</button><button onclick="setPage('${kind}',${page+1})" ${page>=totalPages?'disabled':''}>Next</button>`;
}

function setPage(kind,page){
 if(!lastState){return}
 if(kind==='trade'){tradePage=page;renderTrades(lastState);return}
 if(kind==='lowWinner'){lowWinnerPage=page;renderLowWinnerResearch(lastState)}
}

function renderChecklist(s,d){
 const checks=[
  ['Market open', d.secs>4, d.secs<=0?'warn':null],
  ['Token UP/DOWN valid', (d.upAsk>0||d.upBid>0)&&(d.downAsk>0||d.downBid>0)],
  ['Chainlink fresh', d.chainlinkAge===null?null:d.chainlinkFresh],
  ['Exchange price fresh', d.exchangeFresh],
  ['Latency valid', d.latencyMs?d.latencyOk:null],
  ['Spread <= max spread', d.spreadOk],
  ['Liquidity enough', d.active?d.orderbookOk:null],
  ['Price within min/max', d.active?d.priceOk:null],
  ['Selected side leads book', d.active?d.sidePriceOk:null],
  ['Seconds left in window', !!d.active],
  ['Delta enough', d.active?d.deltaOk:null],
 ];
 let valid=0, invalid=0;
 $('entryChecklist').innerHTML=checks.map(([label,ok])=>{
  const state=ok===null?'warn':ok?'ok':'bad';
  if(state==='ok')valid++; if(state==='bad')invalid++;
  const mark=state==='ok'?'OK':state==='bad'?'X':'!';
  return `<div class="check ${state}"><span class="mark">${mark}</span><span>${esc(label)}</span></div>`;
 }).join('');
 setBadge('checklistBadge', invalid?`${invalid} invalid`:`${valid} valid`, invalid?'no':'up');
}

function renderLayers(s,d){
 let hint='Outside end-window';
 if(d.active)hint=`Active layer: ${d.active.display_name} ${d.active.label}`;
 else if(d.secs>d.maxLayerSeconds)hint=`Next trigger in ${num(d.secs-d.maxLayerSeconds,1)} seconds`;
 const finalCutoff=Math.min(...d.rules.map(r=>Number(r.seconds_left_min||0)),4);
 if(d.secs<=finalCutoff)hint='NO_TRADE zone: retry disabled';
 $('layerHint').textContent=hint;
 setBadge('layerSummary', d.active?`${d.active.display_name} ACTIVE`:d.secs<=finalCutoff?'NO_TRADE':'WAIT', d.active?'up':d.secs<=finalCutoff?'no':'wait');
 const rows=d.rules.map((r)=>{
  let status='LOCKED', cls='locked';
  if(r.enabled===false){status='OFF';cls='expired'}
  else if(d.secs<=Number(r.seconds_left_max)&&d.secs>Number(r.seconds_left_min)){status='ACTIVE';cls='active'}
  else if(d.secs<=Number(r.seconds_left_min)){status='EXPIRED';cls='expired'}
  else if(!d.active&&d.next&&d.next.name===r.name){status='NEXT';cls='next'}
  return `<div class="layer ${cls}"><strong>${esc(r.display_name)}</strong><div><div>${esc(r.label)}: t <= ${num(r.seconds_left_max,0)}s and > ${num(r.seconds_left_min,0)}s</div><div class="hint">delta >= ${money(r.min_distance_usd,0)} | price ${num(r.min_price,2)}-${num(r.max_price,2)}</div></div><span class="status-pill ${cls}">${status}</span></div>`;
 }).join('');
 $('layers').innerHTML=rows+`<div class="layer no-trade"><strong>&lt;${num(finalCutoff,1)}s</strong><div><div>NO TRADE</div><div class="hint">retry disabled</div></div><span class="status-pill no">${d.secs<=finalCutoff?'ACTIVE':'LOCKED'}</span></div>`;
 const savedRules=s.saved_end_window_rules||d.rules;
 const saved=s.saved_end_window_settings||d.ew;
 const timeEditors=[1,2,3,4,5,6].map(i=>{
  const enabled=saved[`time${i}_enabled`]!==false;
  return `<div class="rule-editor"><div class="rule-head"><strong>TIME-${i}</strong><label class="switch"><input data-setting="time${i}_enabled" type="checkbox" ${enabled?'checked':''}><span class="slider"></span></label></div><div class="rule-fields time"><label>Exact price<input data-setting="time${i}_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(saved[`time${i}_price`],2)}"></label><label>Buy when <= (s)<input data-setting="time${i}_max_secs_left" type="number" min="0.1" max="300" step="0.1" value="${num(saved[`time${i}_max_secs_left`]||299,1)}"></label><label>Stop below (s)<input data-setting="time${i}_min_secs_left" type="number" min="0" max="299" step="0.1" value="${num(saved[`time${i}_min_secs_left`],1)}"></label><label>Liquidity / USD<input data-setting="time${i}_trade_usd" type="number" min="1" step="1" value="${num(saved[`time${i}_trade_usd`],0)}"></label><label>Min delta $<input data-setting="time${i}_min_delta_usd" type="number" min="0" step="1" value="${num(saved[`time${i}_min_delta_usd`]||saved.time_min_delta_usd||3,1)}"></label></div></div>`;
 }).join('');
 const buy1Enabled=saved.buy1_enabled!==false;
 const buy1Editor=`<div class="rule-editor"><div class="rule-head"><strong>BUY-1</strong><label class="switch"><input data-setting="buy1_enabled" type="checkbox" ${buy1Enabled?'checked':''}><span class="slider"></span></label></div><div class="rule-fields"><label>Trade USD<input data-setting="buy1_trade_usd" type="number" min="1" step="1" value="${num(saved.buy1_trade_usd,0)}"></label><label>Buy min<input data-setting="buy1_min_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(saved.buy1_min_price,2)}"></label><label>Buy max<input data-setting="buy1_max_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(saved.buy1_max_price,2)}"></label><label>Sell min<input data-setting="buy1_sell_min_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(saved.buy1_sell_min_price,2)}"></label><label>Sell max<input data-setting="buy1_sell_max_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(saved.buy1_sell_max_price,2)}"></label><label>Delta $<input data-setting="buy1_min_delta_usd" type="number" min="0" step="1" value="${num(saved.buy1_min_delta_usd,1)}"></label><label>Start (s)<input data-setting="buy1_max_secs_left" type="number" min="0.1" max="300" step="0.1" value="${num(saved.buy1_max_secs_left,1)}"></label><label>End (s)<input data-setting="buy1_min_secs_left" type="number" min="0" max="299" step="0.1" value="${num(saved.buy1_min_secs_left,1)}"></label><label>Max open<input data-setting="buy1_max_open_positions" type="number" min="1" max="9" step="1" value="${num(saved.buy1_max_open_positions,0)}"></label></div></div>`;
 const layerEditors=savedRules.map(r=>{
  const key=String(r.display_name).toLowerCase();
  return `<div class="rule-editor"><div class="rule-head"><strong>${esc(r.display_name)}</strong><label class="switch"><input data-setting="${key}_enabled" type="checkbox" ${r.enabled===false?'':'checked'}><span class="slider"></span></label></div><div class="rule-fields"><label>Start (s)<input data-setting="${key}_seconds_max" type="number" min="0.1" max="300" step="0.1" value="${num(r.seconds_left_max,1)}"></label><label>End (s)<input data-setting="${key}_seconds_min" type="number" min="0" max="299" step="0.1" value="${num(r.seconds_left_min,1)}"></label><label>Delta $<input data-setting="${key}_delta_min" type="number" min="0" step="1" value="${num(r.min_distance_usd,1)}"></label><label>Min price<input data-setting="${key}_min_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(r.min_price,2)}"></label><label>Max price<input data-setting="${key}_max_price" type="number" min="0.01" max="0.99" step="0.01" value="${num(r.max_price,2)}"></label></div></div>`;
 }).join('');
 const editorHtml=timeEditors+buy1Editor+layerEditors;
 const editorSignature=JSON.stringify({rules:savedRules,settings:saved});
 if(!document.querySelector('#layerSettings:focus-within')&&editorSignature!==layerEditorSignature){
  $('layerSettings').innerHTML=editorHtml;
  layerEditorSignature=editorSignature;
 }
 $('settingsSaved').textContent=s.strategy_settings_pending?'Pending next window':'Active';
}

function renderHealth(s,d){
 const api=apiPct(s);
 const items=[
  ['Chainlink age',d.chainlinkAge===null?'N/A':num(d.chainlinkAge,1)+'s',d.chainlinkAge===null?'warn':d.chainlinkFresh?'ok':'bad'],
  ['Exchange age',d.exchangeAge===null?'N/A':num(d.exchangeAge,1)+'s',d.exchangeFresh?'ok':'bad'],
  ['Latency',d.latencyMs?num(d.latencyMs,1)+'ms':'N/A',d.latencyMs?d.latencyOk?'ok':'bad':'warn'],
  ['API error rate',pct(api),api<=5?'ok':'bad'],
  ['CLOB status',d.clobOk?`OK (${d.clobAge===null?'N/A':num(d.clobAge,1)+'s'})`:'BAD',d.clobOk?'ok':'bad'],
  ['Price source',d.priceSource||'N/A',d.priceSource?'ok':'warn'],
  ['Last update',fmtTime(s.last_update),d.age!==null&&d.age<=10?'ok':'bad'],
 ];
 $('dataHealth').innerHTML=items.map(([label,value,state])=>`<div class="health-item"><div class="label">${esc(label)}</div><div class="${state==='ok'?'good':state==='bad'?'bad':'warn'} mono">${esc(value)}</div></div>`).join('');
 const bad=items.some((i)=>i[2]==='bad');
 setBadge('dataBadge', bad?'Data stale - NO_TRADE':'Data OK', bad?'no':'up');
}

function renderReasonLog(s,d){
 const now=fmtTime(Date.now()/1000);
 const current=`${now} ${d.decision} - ${d.reason}${d.active?` (${d.active.display_name})`:''}`;
 const raw=(s.log_lines||[]).filter((line)=>/END_WINDOW|NO_TRADE|FIRE|skip|stale|spread/i.test(line)).slice(-14).reverse();
 const lines=[current,...raw].slice(0,14);
 $('reasonLog').innerHTML=lines.map((line)=>{
  const text=String(line);
  const kind=/BUY_DOWN|DOWN BUY|FIRE DOWN/.test(text)?'down':/BUY_UP|UP BUY|FIRE UP/.test(text)?'up':/NO_TRADE|skip|failed|stale|unsafe|below/i.test(text)?'no':'wait';
  return `<div class="reason-item ${kind}"><div class="mono">${esc(text)}</div></div>`;
 }).join('');
 setBadge('reasonLogBadge', d.decision, decisionClass(d.decision));
}

function renderTrades(s){
 const allRows=s.recent_trades||[];
 const filteredRows=allRows.filter((t)=>{
  const pnl=Number(t.pnl||0);
  if(tradeFilter==='profit')return t.resolved&&pnl>0;
  if(tradeFilter==='loss')return t.resolved&&pnl<0;
  return true;
 });
 const totalPages=Math.max(1,Math.ceil(filteredRows.length/TRADE_PAGE_SIZE));
 tradePage=Math.min(Math.max(1,tradePage),totalPages);
 const rows=filteredRows.slice((tradePage-1)*TRADE_PAGE_SIZE,tradePage*TRADE_PAGE_SIZE);
 $('tradeCount').textContent=`${filteredRows.length} / ${allRows.length} trades`;
 ['all','profit','loss'].forEach((name)=>{
  const id='tradeFilter'+name.charAt(0).toUpperCase()+name.slice(1);
  $(id).classList.toggle('active',tradeFilter===name);
 });
 if(!rows.length){
  $('recentTrades').innerHTML='<tr><td colspan="13" class="muted">No trades match this filter</td></tr>';
  renderPagination('trade',tradePage,totalPages,filteredRows.length);
  return;
 }
 $('recentTrades').innerHTML=rows.map(t=>{
  const result=t.resolved?(t.won===true?'<span class="badge up">WIN</span>':t.won===false?'<span class="badge no">LOSS</span>':'<span class="badge wait">FLAT</span>'):'<span class="badge wait">OPEN</span>';
  const gamma=`<button class="mini gamma-refresh" data-window-ts="${Number(t.window_ts||0)}" data-market-slug="${esc(t.market_slug||'')}">${t.resolved?'Correct Gamma':'Refresh Gamma'}</button>`;
  const marketLink=t.market_slug?`<a class="market-link" href="https://polymarket.com/event/${encodeURIComponent(String(t.market_slug))}" target="_blank" rel="noopener noreferrer" title="Open on Polymarket" aria-label="Open ${esc(t.market_slug)} on Polymarket">&#8599;</a>`:'<span class="muted">N/A</span>';
  const closeText=t.btc_at_close?money(t.btc_at_close,1):'<span class="muted">waiting close</span>';
  const target=Number(t.resolution_price_to_beat||t.btc_open||0);
  const resolvedDelta=t.btc_delta_resolved===null||t.btc_delta_resolved===undefined?'<span class="muted">waiting resolved</span>':signedMoney(t.btc_delta_resolved,1);
  const spreadNa=t.first_spread_na||{};
  const spreadNaSecs=spreadNa.secs_left;
  const spreadNaSide=String(spreadNa.side||'');
  const spreadNaClass=spreadNaSide==='UP'?'up':spreadNaSide==='DOWN'?'no':'wait';
  const spreadNaText=spreadNaSecs!==undefined&&spreadNaSecs!==null
    ? `<span class="badge ${spreadNaClass}">${num(spreadNaSecs,1)}s ${esc(spreadNaSide)}</span>`
    : '<span class="muted">waiting 0.99/N/A</span>';
  return `<tr><td class="market-link-cell">${marketLink}</td><td>${fmtTime(t.timestamp)}</td><td><span class="badge wait">${esc(t.fire_layer||'N/A')}</span></td><td><strong class="${t.outcome==='DOWN'?'down-text':'good'}">${esc(t.outcome||'N/A')}</strong><div class="muted">${t.mock?'MOCK':'LIVE'}</div></td><td class="mono">${num(t.entry_price,4)}<div class="muted">${num(t.secs_left,1)}s left</div></td><td class="mono">${money(t.amount_usd)}<div class="muted">${num(t.shares,2)} sh</div></td><td class="mono">${t.btc_at_entry?money(t.btc_at_entry,1):'N/A'}<div class="muted">target ${target?money(target,1):'N/A'}</div></td><td class="mono">${closeText}<div class="muted">1s->0s close</div></td><td class="mono ${deltaClass(t.btc_delta_entry)}">${signedMoney(t.btc_delta_entry,1)}</td><td class="mono ${deltaClass(t.btc_delta_resolved)}">${resolvedDelta}</td><td class="mono">${spreadNaText}</td><td>${result}</td><td class="mono ${clsBy(t.pnl)}">${t.resolved?money(t.pnl):'N/A'}</td><td>${gamma}</td></tr>`
 }).join('');
 renderPagination('trade',tradePage,totalPages,filteredRows.length);
}

function setTradeFilter(filter){
 tradeFilter=['all','profit','loss'].includes(filter)?filter:'all';
 tradePage=1;
 renderTrades(lastState);
}

function renderPnlCharts(s){
 const history=(s.pnl_history||[]).map((row)=>({...row,pnl:Number(row.pnl||0),cumulative:Number(row.cumulative||0)}));
 const recent=history.slice(-24);
 if(!recent.length){
  $('pnlBars').innerHTML='<div class="chart-empty">No resolved trades</div>';
  $('pnlJourney').innerHTML='<div class="chart-empty">No PnL history</div>';
  $('pnlJourneyTotal').textContent='from bot start';
  return;
 }
 const width=700,height=340,left=76,right=24,top=24,bottom=30;
 const maxAbs=Math.max(1,...recent.map((row)=>Math.abs(row.pnl)));
 const plotWidth=width-left-right,zeroX=left+plotWidth/2,rowHeight=(height-top-bottom)/recent.length;
 const ticks=[-maxAbs,-maxAbs/2,0,maxAbs/2,maxAbs];
 const grid=ticks.map((tick)=>{
  const x=left+((tick+maxAbs)/(maxAbs*2))*plotWidth;
  return `<line x1="${x.toFixed(1)}" y1="${top}" x2="${x.toFixed(1)}" y2="${height-bottom}" stroke="#334155" stroke-width="1" stroke-dasharray="${tick===0?'0':'3 4'}"/><text x="${x.toFixed(1)}" y="${height-8}" text-anchor="middle" fill="#94a3b8" font-size="10">${tick<0?'-$':'$'}${Math.abs(tick).toFixed(0)}</text>`;
 }).join('');
 const bars=recent.map((row,index)=>{
  const w=Math.max(2,Math.abs(row.pnl)/maxAbs*(plotWidth/2));
  const x=row.pnl>=0?zeroX:zeroX-w;
  const y=top+index*rowHeight+2;
  const color=row.pnl>=0?'#22c55e':'#ef4444';
  return `<text x="${left-8}" y="${(y+rowHeight*.55).toFixed(1)}" text-anchor="end" fill="#94a3b8" font-size="10">${fmtTime(row.timestamp)}</text><rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(3,rowHeight-4).toFixed(1)}" rx="2" fill="${color}" opacity=".9"><title>${fmtTime(row.timestamp)} | ${esc(row.layer)} | ${money(row.pnl)}</title></rect>`;
 }).join('');
 $('pnlBars').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="PnL bars by time">${grid}${bars}</svg>`;

 const values=history.map((row)=>row.cumulative);
 const min=Math.min(0,...values),max=Math.max(0,...values),range=Math.max(1,max-min);
 const pad=22;
 const points=history.map((row,index)=>{
  const x=pad+(history.length===1?0:index/(history.length-1))*(width-pad*2);
  const y=pad+(max-row.cumulative)/range*(height-pad*2);
  return `${x.toFixed(1)},${y.toFixed(1)}`;
 }).join(' ');
 const final=values[values.length-1]||0;
 const color=final>=0?'#22c55e':'#ef4444';
 const zeroLine=pad+(max/range)*(height-pad*2);
 $('pnlJourney').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Cumulative PnL journey"><defs><linearGradient id="pnlGlow" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><line x1="${pad}" y1="${zeroLine.toFixed(1)}" x2="${width-pad}" y2="${zeroLine.toFixed(1)}" stroke="#475569" stroke-dasharray="4 4"/><polygon points="${pad},${height-pad} ${points} ${width-pad},${height-pad}" fill="url(#pnlGlow)"/><polyline points="${points}" fill="none" stroke="${color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/><circle id="pnlHoverDot" cx="${pad}" cy="${pad}" r="5" fill="${color}" stroke="#fff" stroke-width="2" visibility="hidden"/><rect id="pnlHoverArea" x="${pad}" y="${pad}" width="${width-pad*2}" height="${height-pad*2}" fill="transparent"/><text x="${pad}" y="14" fill="#94a3b8" font-size="11">${history.length} resolved trades</text></svg><div id="pnlTooltip" class="chart-tooltip"></div>`;
 const chart=$('pnlJourney'),area=$('pnlHoverArea'),dot=$('pnlHoverDot'),tip=$('pnlTooltip');
 const showPnlTooltip=(event)=>{
  const rect=area.getBoundingClientRect();
  const ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width));
  const index=Math.round(ratio*(history.length-1));
  const row=history[index];
  const x=pad+(history.length===1?0:index/(history.length-1))*(width-pad*2);
  const y=pad+(max-row.cumulative)/range*(height-pad*2);
  dot.setAttribute('cx',x); dot.setAttribute('cy',y); dot.setAttribute('visibility','visible');
  tip.textContent=`${fmtTime(row.timestamp)} | Trade ${money(row.pnl)} | Total ${money(row.cumulative)}`;
  tip.style.display='block'; tip.style.left=Math.min(chart.clientWidth-210,event.clientX-chart.getBoundingClientRect().left+12)+'px'; tip.style.top=Math.max(8,event.clientY-chart.getBoundingClientRect().top-34)+'px';
 };
 area.addEventListener('pointermove',showPnlTooltip);
 area.addEventListener('mousemove',showPnlTooltip);
 area.addEventListener('mouseleave',()=>{dot.setAttribute('visibility','hidden');tip.style.display='none'});
 $('pnlJourneyTotal').textContent=`Total ${money(final)}`;
 $('pnlJourneyTotal').className=clsBy(final);
}

function renderPnlSummary(s){
 const p=s.pnl_summary||{};
 const totalPnl = p.total_pnl ?? s.total_pnl ?? 0;
 const totalCap = p.total_capital || 1000;
 const profitPct = totalCap > 0 ? (totalPnl / totalCap * 100) : 0;
 const currentModal = totalCap + totalPnl;
 const items=[
  ['Modal saat ini',money(currentModal),'mono'],
  ['Total modal',money(totalCap),'mono'],
  ['Total PnL',money(totalPnl),'mono '+clsBy(totalPnl)],
  ['Profit (%)',pct(profitPct),'mono '+clsBy(profitPct)],
  ['Today PnL',money(s.daily_pnl),'mono '+clsBy(s.daily_pnl)],
  ['Win rate',pct(p.win_rate??s.win_rate),'mono'],
  ['Total trades',String(p.total_trades??s.trades_total??0),'mono'],
  ['Average entry',num(p.avg_entry,4),'mono'],
  ['Max drawdown',money(p.max_drawdown),'mono bad'],
 ];
 $('pnlSummary').innerHTML=items.map(([label,value,klass])=>`<div class="metric"><div class="label">${esc(label)}</div><div class="value ${klass}">${esc(value)}</div></div>`).join('');
 setBadge('pnlMode', s.mock_mode?'MOCK':'LIVE', s.mock_mode?'mock':'live');
}

function renderState(s){
 lastState=s;
 const d=deriveDecision(s);
 renderTopBar(s,d); renderDecision(s,d); renderBtcDelta(s,d); renderOrderbook(s,d);
 renderChecklist(s,d); renderLayers(s,d); renderHealth(s,d); renderLowWinnerResearch(s); renderTrades(s); renderPnlSummary(s); renderPnlCharts(s);
 if(s.pnl_calendar){
  const key=calendarKey(s.pnl_calendar.year,s.pnl_calendar.month);
  calendarCache.set(key,s.pnl_calendar);
  if(key===calendarKey(calDate.getFullYear(),calDate.getMonth()+1))renderCalendar(s.pnl_calendar);
 }
 const settings=s.end_window_settings||{}, active=s.active_settings||{};
 if(document.activeElement!==$('market5m'))$('market5m').checked=settings.market_5m_enabled!==false;
 if(document.activeElement!==$('tradeAmount'))$('tradeAmount').value=Number(settings.trade_usd||active.trade_amount||100).toFixed(0);
 if(document.activeElement!==$('maxTrades'))$('maxTrades').value=Number(settings.max_trades_per_window||active.max_trades_per_window||1);
 if(document.activeElement!==$('profitStopPct'))$('profitStopPct').value=Number(active.profit_stop_pct||100).toFixed(0);
 $('orderType').value=settings.order_type||'FOK';
 $('maxBuyPrice').value=d.maxPrice?num(d.maxPrice,2):'N/A';
 $('maxSpreadSetting').value=settings.max_spread!==undefined?num(settings.max_spread,3):'N/A';
 $('maxChainlinkAge').value=num(d.maxFeedAge,0)+'s';
 $('maxExchangeAge').value=num(d.maxFeedAge,0)+'s';
 $('maxLatency').value=num(d.maxLatencyMs,0)+'ms';
 const noLossCap=s.mock_mode&&active.cb_master_enabled===false;
 $('riskTrade').value=noLossCap?'No cap (mock)':active.max_loss_per_trade?money(active.max_loss_per_trade):'N/A';
 $('maxDailyLoss').value=noLossCap?'No cap (mock)':active.max_daily_loss?money(active.max_daily_loss):'N/A';
 $('maxLossStreak').value=noLossCap?'No cap':active.max_consecutive_losses||'N/A';
 $('emergencyStatus').value=s.emergency_stop?'ACTIVE':'clear';
 const raw=(s.log_lines||[]).slice(-12).reverse();
 $('logs').innerHTML=raw.length?raw.map(line=>`<div class="reason-item wait"><div class="mono">${esc(line)}</div></div>`).join(''):'<div class="muted">No logs yet</div>';
}

async function refresh(){
 try{const r=await fetch('/api/state'); renderState(await r.json())}catch(e){console.error('state render error',e);toast('state error: '+e.message)}
}

function renderCalendar(c){
 if(!c)return;
 $('calTitle').textContent=`${c.month_name} ${c.year}`;
 $('calSummary').textContent=`${money(c.month_pnl)} | ${c.trading_days} days`;
 const heads=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(d=>`<div class="dow">${d}</div>`).join('');
 const cells=(c.weeks||[]).flat().map(d=>{
  const traded=Number(d.trade_count||0)>0; const sign=Number(d.pnl||0)>0?'win':Number(d.pnl||0)<0?'loss':'';
  const body=traded?`<div class="day-pnl ${clsBy(d.pnl)}">${money(d.pnl)}</div><div class="day-meta">${d.trade_count} trades</div>`:'';
  return `<div class="day ${d.in_month?'':'out'} ${traded?sign:'empty'}"><div class="day-num">${d.day}</div>${body}</div>`
 }).join('');
 $('calendar').innerHTML=heads+cells;
}
async function loadCalendar(force=false){
 const y=calDate.getFullYear(), m=calDate.getMonth()+1, key=calendarKey(y,m);
 if(!force&&calendarCache.has(key))renderCalendar(calendarCache.get(key));
 try{
  const r=await fetch(`/api/pnl-calendar?year=${y}&month=${m}`);
  const c=await r.json();
  calendarCache.set(key,c);
  if(key===calendarKey(calDate.getFullYear(),calDate.getMonth()+1))renderCalendar(c);
 }catch(e){if(!calendarCache.has(key))toast('calendar error: '+e.message)}
}
function shiftMonth(n){calDate.setMonth(calDate.getMonth()+n);loadCalendar()}

setInterval(refresh,200); refresh(); loadCalendar(); keepScreenAwake();
</script></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def stream(_request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(_request)
    try:
        while True:
            await response.write(f"data: {json.dumps(_state_payload())}\n\n".encode("utf-8"))
            await asyncio.sleep(1.0)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    return response


async def api_state(_request: web.Request) -> web.Response:
    return web.json_response(_state_payload())


async def api_pnl_calendar(request: web.Request) -> web.Response:
    year = request.rel_url.query.get("year")
    month = request.rel_url.query.get("month")
    data = _build_pnl_calendar(
        st.load_trades(),
        int(year) if year else None,
        int(month) if month else None,
    )
    return web.json_response(data)


async def api_control(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    data = await request.json()
    enabled = bool(data.get("trading_enabled", data.get("enabled", False)))
    st.set_trading_enabled(enabled)
    if enabled:
        st.set_emergency_stop(False)
    return web.json_response({"success": True, "trading_enabled": enabled})


async def api_settings_get(_request: web.Request) -> web.Response:
    data = st.asdict(st.load_settings())
    data.update({
        "strategy_settings_effective": "next_window",
        "end_window_trade_usd": float(os.getenv("END_WINDOW_TRADE_USD", data.get("trade_amount", 0.0))),
        "end_window_max_trades_per_window": int(float(os.getenv("END_WINDOW_MAX_TRADES_PER_WINDOW", "9"))),
    })
    return web.json_response(data)


async def api_health(_request: web.Request) -> web.Response:
    state = st.load_state()
    return web.json_response({
        "ok": True,
        "service": "poly-v3-dashboard",
        "mock_mode": bool(config.MOCK_MODE),
        "trading_enabled": bool(st.get_trading_enabled()),
        "emergency_stop": bool(st.get_emergency_stop()),
        "last_update": state.get("last_update"),
        "current_window": state.get("current_window"),
    })


async def api_settings_post(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    data = await request.json()
    env_updates: dict[str, str] = {}
    if "trade_amount" in data:
        amount = max(1.0, float(data["trade_amount"]))
        env_updates["TRADE_AMOUNT"] = f"{amount:.2f}"
        env_updates["END_WINDOW_TRADE_USD"] = f"{amount:.2f}"
        env_updates["END_WINDOW_MIN_TRADE_USD"] = f"{amount:.2f}"
    if "max_trades_per_window" in data:
        max_trades = min(9, max(1, int(data["max_trades_per_window"])))
        env_updates["MAX_TRADES_PER_WINDOW"] = str(max_trades)
        env_updates["END_WINDOW_MAX_TRADES_PER_WINDOW"] = str(max_trades)
    if env_updates:
        _set_env_values(env_updates)
    strategy_keys = {
        key for key in st.asdict(st.BotSettings())
        if (
            key.startswith("t")
            or key.startswith("time")
            or key.startswith("buy1_")
            or key == "market_5m_enabled"
            or key == "profit_stop_pct"
        )
    }
    try:
        st.update_settings({key: data[key] for key in strategy_keys if key in data})
    except ValueError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=400)
    settings = st.asdict(st.load_settings())
    state_data = st.load_state()
    state_data["active_settings"] = settings
    state_data["market_interval_secs"] = 300
    state_data["market_interval_label"] = "5m"
    st._atomic_write(st.STATE_FILE, json.dumps(state_data, indent=2))
    st._remember_json(st.STATE_FILE, state_data)
    settings.update({
        "end_window_trade_usd": float(os.getenv("END_WINDOW_TRADE_USD", settings.get("trade_amount", 0.0))),
        "end_window_max_trades_per_window": int(float(os.getenv("END_WINDOW_MAX_TRADES_PER_WINDOW", "9"))),
    })
    return web.json_response({"success": True, "settings": settings})


async def api_deposit(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    if not config.MOCK_MODE:
        return web.json_response({"success": False, "error": "deposit disabled in live mode"}, status=400)
    data = await request.json()
    balance = st.deposit_balance(float(data.get("amount", 0.0) or 0.0))
    return web.json_response({"success": True, "balance": balance})


async def api_reset(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    if not config.MOCK_MODE:
        return web.json_response({"success": False, "error": "reset disabled in live mode"}, status=400)
    initial = float(os.getenv("MOCK_RESET_BALANCE_USD", "1000.0"))
    st.save_trades([])
    st.save_balance({"balance": initial, "initial": initial, "total_deposited": initial})
    st._atomic_write(st.SNAPSHOTS_FILE, "[]")
    st._atomic_write(st.CUM_STATS_FILE, json.dumps(st._default_cum_stats()))
    st.rebuild_daily_pnl([])
    st.set_daily_halted(False)
    st.clear_open_position()
    st._atomic_write(st.STATE_FILE, json.dumps(st.asdict(st.BotState(mock_mode=config.MOCK_MODE, balance=initial, initial_balance=initial))))
    return web.json_response({"success": True, "balance": initial})


async def api_emergency_stop(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    data = await request.json()
    active = bool(data.get("active", True))
    st.set_emergency_stop(active)
    if active:
        st.set_trading_enabled(False)
    return web.json_response({"success": True, "active": active})


async def api_clear_daily_halt(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    st.set_daily_halted(False)
    return web.json_response({"success": True})


async def api_cb_toggle(_request: web.Request) -> web.Response:
    return web.json_response({"success": True, "note": "runtime circuit breaker toggles are not used by END_WINDOW-only mode"})


async def api_gamma_refresh(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"success": False, "error": "unauthorized"}, status=401)
    data = await request.json()
    window_ts = int(data.get("window_ts", 0) or 0)
    slug = str(data.get("market_slug") or "")
    if not window_ts or not slug:
        return web.json_response({"success": False, "error": "window_ts and market_slug required"}, status=400)
    async with await mkt.make_session() as session:
        resolution = await mkt.fetch_resolution(session, slug, allow_implied=True)
    if not resolution or not resolution.get("actual"):
        return web.json_response({"success": False, "error": "official outcome unavailable"}, status=404)
    actual = str(resolution["actual"])
    correction = st.apply_gamma_actual_correction(
        window_ts,
        slug,
        actual,
        source=str(resolution.get("source") or "gamma-refresh"),
        triggers=("END_WINDOW",),
        final_price=float(resolution.get("final_price") or 0.0),
        price_to_beat=float(resolution.get("price_to_beat") or 0.0),
    )
    return web.json_response({"success": True, "actual": actual, "resolution": resolution, "correction": correction})


async def api_report(request: web.Request) -> web.Response:
    fmt = request.rel_url.query.get("format", "xlsx").lower()
    try:
        hours = max(1, min(168, int(request.rel_url.query.get("hours", "24"))))
    except ValueError:
        hours = 24
    try:
        import web.report_gen as rg
        data = rg.get_report_data(hours=hours)
        if fmt == "pdf":
            content = await asyncio.get_event_loop().run_in_executor(None, rg.generate_pdf, data)
            content_type = "application/pdf"
            filename = f"laporan_end_window_{hours}h.pdf"
        else:
            content = await asyncio.get_event_loop().run_in_executor(None, rg.generate_excel, data)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            filename = f"laporan_end_window_{hours}h.xlsx"
        return web.Response(
            body=content,
            content_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        log.error("api_report error: %s", exc, exc_info=True)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def _gamma_context_worker(app: web.Application):
    global _gamma_context
    session = await mkt.make_session()
    try:
        while True:
            try:
                rows = await mkt.fetch_recent_btc_resolutions(session, hours=3)
                if rows:
                    context = analyze_gamma_resolutions(rows)
                    context.update(
                        await mkt.fetch_recent_clob_saturation(
                            session,
                            rows,
                            minutes=30,
                        )
                    )
                    _gamma_context = context
            except Exception as exc:
                log.warning("Gamma market context refresh failed: %s", exc)
            await asyncio.sleep(60.0)
    finally:
        await session.close()


async def _gamma_context_cleanup(app: web.Application):
    task = app.get("gamma_context_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _gamma_context_startup(app: web.Application):
    app["gamma_context_task"] = asyncio.create_task(_gamma_context_worker(app))


def main():
    app = web.Application()
    app.on_startup.append(_gamma_context_startup)
    app.on_cleanup.append(_gamma_context_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/stream", stream)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/pnl-calendar", api_pnl_calendar)
    app.router.add_post("/api/control", api_control)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_post("/api/settings", api_settings_post)
    app.router.add_post("/api/deposit", api_deposit)
    app.router.add_post("/api/reset", api_reset)
    app.router.add_post("/api/emergency-stop", api_emergency_stop)
    app.router.add_post("/api/clear-daily-halt", api_clear_daily_halt)
    app.router.add_post("/api/cb-toggle", api_cb_toggle)
    app.router.add_post("/api/gamma-refresh", api_gamma_refresh)
    app.router.add_get("/api/report", api_report)
    log.info("Dashboard -> http://%s:%d (auth=%s)", HOST, PORT, "on" if AUTH_TOKEN else "off")
    web.run_app(app, host=HOST, port=PORT, print=lambda _: None)


if __name__ == "__main__":
    main()
