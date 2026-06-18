"""Runtime notifications.

Telegram is optional. Missing token/chat silently disables alerts so the bot
does not depend on a root-level helper module to run.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp

import core.config as config

log = logging.getLogger("notify")


def _fmt_price(value: float) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_money(value: float | None) -> str:
    try:
        return f"${float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "$0"


def _read_value(source, key: str, default=None):
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def format_trading_result(
    trades: list[dict],
    market_context: dict | None = None,
    bot_state=None,
) -> str:
    if not trades:
        raise ValueError("trades must not be empty")
    market_context = market_context or {}
    first = trades[0]
    slug = str(first.get("market_slug") or "").strip()
    link = f"https://polymarket.com/id/event/{slug}" if slug else "N/A"
    prices = ", ".join(
        _fmt_price(float(trade.get("entry_price") or 0.0))
        for trade in sorted(trades, key=lambda row: float(row.get("timestamp") or 0.0))
    )
    total_pnl = round(sum(float(trade.get("pnl") or 0.0) for trade in trades), 4)
    status = "WIN" if total_pnl > 0 else "LOSS" if total_pnl < 0 else "FLAT"
    mode = "MOCK" if bool(_read_value(bot_state, "mock_mode", config.MOCK_MODE)) else "LIVE"
    return "\n".join([
        "Trading result:",
        f"Link Window BTC: {html.escape(link)}",
        f"Status trading: {mode}",
        f"Trade: {len(trades)} ({html.escape(prices)})",
        f"Status Result: {status}",
        f"PnL: {_fmt_money(total_pnl)}",
        f"Total PnL: {_fmt_money(_read_value(bot_state, 'total_pnl', 0.0))}",
        f"Today PnL: {_fmt_money(_read_value(bot_state, 'daily_pnl', 0.0))}",
        f"30m Saturation 0.94: {_fmt_seconds(market_context.get('saturation_avg_secs_30m'))}",
        f"30m Locked N/A: {_fmt_seconds(market_context.get('locked_avg_secs_30m'))}",
    ])


def format_open_positions(trades: list[dict], state=None) -> str:
    open_rows = [
        trade for trade in trades
        if str(trade.get("trigger") or "").upper() == "END_WINDOW"
        and not trade.get("resolved")
        and not trade.get("exited_early")
    ]
    if not open_rows:
        return "Open position: none"
    lines = ["Open position:"]
    current_window = int(_read_value(state, "current_window", 0) or 0)
    up_price = float(_read_value(state, "up_price", 0.0) or 0.0)
    down_price = float(_read_value(state, "down_price", 0.0) or 0.0)
    for idx, trade in enumerate(sorted(open_rows, key=lambda row: float(row.get("timestamp") or 0.0)), 1):
        outcome = str(trade.get("outcome") or "N/A").upper()
        window_ts = int(trade.get("window_ts") or 0)
        current_price = 0.0
        if current_window and window_ts == current_window:
            current_price = up_price if outcome == "UP" else down_price
        bits = [
            f"{idx}. {outcome}",
            f"entry {_fmt_price(float(trade.get('entry_price') or 0.0))}",
            f"amount {_fmt_money(float(trade.get('amount_usd') or 0.0))}",
            f"shares {float(trade.get('shares') or 0.0):.2f}",
        ]
        if current_price > 0:
            bits.append(f"now {_fmt_price(current_price)}")
        slug = str(trade.get("market_slug") or "")
        if slug:
            bits.append(f"https://polymarket.com/id/event/{slug}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def format_runtime_status(state=None) -> str:
    mode = "MOCK" if bool(_read_value(state, "mock_mode", config.MOCK_MODE)) else "LIVE"
    trading = "RUNNING" if bool(_read_value(state, "trading_enabled", False)) else "STOPPED"
    status = str(_read_value(state, "status", "unknown") or "unknown")
    return "\n".join([
        "Trading status:",
        f"Status trading: {mode}",
        f"Trading: {trading}",
        f"Bot status: {status}",
        f"Total PnL: {_fmt_money(_read_value(state, 'total_pnl', 0.0))}",
        f"Today PnL: {_fmt_money(_read_value(state, 'daily_pnl', 0.0))}",
        "Mode command: /status mock or /status live",
        "Live mode requires restart and live safety confirmation.",
    ])


async def send(text: str) -> None:
    if not config.TG_TOKEN or not config.TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            await session.post(
                url,
                json={
                    "chat_id": config.TG_CHAT,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        log.debug("notification send failed: %s", exc)


async def send_photo(path: str | Path, caption: str = "") -> None:
    if not config.TG_TOKEN or not config.TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendPhoto"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            with Path(path).open("rb") as image_file:
                form = aiohttp.FormData()
                form.add_field("chat_id", str(config.TG_CHAT))
                if caption:
                    form.add_field("caption", caption)
                form.add_field(
                    "photo",
                    image_file,
                    filename=Path(path).name,
                    content_type="image/png",
                )
                await session.post(url, data=form)
    except Exception as exc:
        log.debug("photo notification send failed: %s", exc)


async def get_updates(offset: int | None = None, *, timeout: int = 20) -> list[dict]:
    if not config.TG_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/getUpdates"
    params = {
        "timeout": int(timeout),
        "allowed_updates": '["message"]',
    }
    if offset is not None:
        params["offset"] = int(offset)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=max(5, timeout + 5))
        ) as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
        if data.get("ok") and isinstance(data.get("result"), list):
            return data["result"]
    except Exception as exc:
        log.debug("telegram getUpdates failed: %s", exc)
    return []


def _browser_candidates() -> list[str]:
    configured = os.getenv("TELEGRAM_SCREENSHOT_BROWSER", "").strip()
    return [
        path for path in [
            configured,
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        if path
    ]


def _capture_dashboard_screenshot_sync(url: str) -> Path:
    browser = next((Path(path) for path in _browser_candidates() if Path(path).exists()), None)
    if browser is None:
        raise RuntimeError("browser headless not found")
    out = Path(tempfile.gettempdir()) / f"poly_dashboard_{os.getpid()}.png"
    profile_dir = Path(tempfile.mkdtemp(prefix="poly_dashboard_profile_"))
    cmd = [
        str(browser),
        "--headless",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--hide-scrollbars",
        f"--user-data-dir={profile_dir}",
        "--window-size=1365,2400",
        f"--screenshot={out}",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode != 0 or not out.exists():
            raise RuntimeError((result.stderr or result.stdout or "screenshot failed").strip())
        return out
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


async def capture_dashboard_screenshot(url: str | None = None) -> Path:
    target = url or os.getenv("TELEGRAM_DASHBOARD_URL", "http://127.0.0.1:5004")
    return await asyncio.to_thread(_capture_dashboard_screenshot_sync, target)
