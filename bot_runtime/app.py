"""Entrypoint runtime for the BTC end-window bot."""
from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import core.config as config
import core.market as mkt
import core.safety as safety
import core.state as st
import core.trader as trader
from analysis.market_context import analyze_market_context
from bot_runtime import end_window_runner as end_window
from bot_runtime import notifications as tg
from bot_runtime.logging_setup import log, log_buf as _log_buf
from bot_runtime.process import acquire_instance_lock, release_instance_lock
from core.ws_feed import WsFeed, clear_clob_tokens, get_cache, set_clob_tokens
from strategies import enabled_strategies
from strategies import end_window as end_window_strategy


TICK_INTERVAL: float = float(os.getenv("TICK_INTERVAL", "0.01"))
SAVE_STATE_INTERVAL: float = float(os.getenv("SAVE_STATE_INTERVAL", "0.5"))
SNAPSHOT_INTERVAL: float = float(os.getenv("SNAPSHOT_INTERVAL", "1.0"))
FAST_REFRESH_SECS_LEFT: float = float(os.getenv("FAST_REFRESH_SECS_LEFT", "8.0"))
FAST_REFRESH_TIMEOUT_SECS: float = float(os.getenv("FAST_REFRESH_TIMEOUT_SECS", "0.08"))
MARKET_REFRESH_TIMEOUT_SECS: float = float(os.getenv("MARKET_REFRESH_TIMEOUT_SECS", "0.06"))
BTC_REST_INTERVAL: float = float(os.getenv("BTC_REST_INTERVAL", "0.5"))
BTC_HOT_CACHE_MAX_AGE_SECS: float = float(os.getenv("BTC_HOT_CACHE_MAX_AGE_SECS", "0.35"))
PRICE_SOURCE_TIMEOUT_SECS: float = float(os.getenv("PRICE_SOURCE_TIMEOUT_SECS", "0.5"))
OFFICIAL_RESOLVE_RETRIES: int = int(float(os.getenv("END_WINDOW_OFFICIAL_RESOLVE_RETRIES", "8")))
OFFICIAL_RESOLVE_DELAY_SECS: float = float(os.getenv("END_WINDOW_OFFICIAL_RESOLVE_DELAY_SECS", "0.5"))
OFFICIAL_RETRY_INTERVAL_SECS: float = float(os.getenv("END_WINDOW_OFFICIAL_RETRY_INTERVAL_SECS", "2.0"))
EXCHANGE_PRICE_MAX_AGE_SECS: float = float(os.getenv("EXCHANGE_PRICE_MAX_AGE_SECS", "3.0"))
CHAINLINK_PRICE_MAX_AGE_SECS: float = float(os.getenv("CHAINLINK_PRICE_MAX_AGE_SECS", "10.0"))
# FIX #2: re-sync target resmi (priceToBeat) Polymarket selama window.
TARGET_RESYNC_SECS: float = float(os.getenv("END_WINDOW_TARGET_RESYNC_SECS", "3.0"))
# Saat target resmi belum terkonfirmasi DAN |btc_now-btc_open| <= ambang ini
# (near-the-money), bot menahan trade agar tidak salah arah. 0 = nonaktif.
TARGET_NEAR_STRIKE_USD: float = float(os.getenv("END_WINDOW_TARGET_NEAR_STRIKE_USD", "5.0"))


class Bot:
    def __init__(self):
        self.running = True
        self._session: aiohttp.ClientSession | None = None
        self._ws_feed = WsFeed()
        self._last_save_state = 0.0
        self._last_snapshot = 0.0
        self._last_directional_orphan_resolve_check = 0.0
        self._session_pnl = 0.0
        self._close_btc: dict[int, float] = {}
        self._last_btc_rest_ts = 0.0
        self._last_btc_rest_price = 0.0
        self._last_btc_source = ""
        self._last_chainlink_age = 999.0
        self._last_exchange_age = 999.0
        self._context_inflight = False  # FIX #1: regime analysis off hot path
        self._target_locked_official = False  # FIX #2: priceToBeat re-sync state
        self._last_target_resync = 0.0
        self._window_settings: st.BotSettings | None = None
        self._telegram_task: asyncio.Task | None = None

        # MOCK mode: preserve trading_enabled across restarts so bot
        # resumes automatically.  LIVE mode: always require explicit
        # /trading command for safety.
        if not config.MOCK_MODE:
            st.set_trading_enabled(False)
        bal = st.load_balance()
        cfg = st.load_settings()
        _restored_trading = st.get_trading_enabled() if config.MOCK_MODE else False
        self.state = st.BotState(
            started_at=time.time(),
            mock_mode=config.MOCK_MODE,
            balance=float(bal.get("balance", 0.0) or 0.0),
            initial_balance=float(bal.get("initial", 0.0) or 0.0),
            max_trades_per_window=cfg.max_trades_per_window,
            trading_enabled=_restored_trading,
            bot_status="scanning" if _restored_trading else "waiting",
            status="scanning" if _restored_trading else "waiting",
            active_settings=st.asdict(cfg),
        )
        self._sync_stats()
        self._save(force=True)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = await mkt.make_session()
        return self._session

    def _load_cfg(self) -> st.BotSettings:
        return st.load_settings()

    def _apply_safety_gate(self, status: str) -> bool:
        gate = safety.startup_safety_report(self._load_cfg())
        if gate.ok:
            return True
        label = "Startup" if status.startswith("startup") else "Runtime"
        log.error("%s safety blocked trading: %s", label, gate.reason)
        st.set_trading_enabled(False)
        self.state.trading_enabled = False
        self.state.circuit_breaker_active = True
        self.state.circuit_breaker_reason = gate.reason
        self.state.status = self.state.bot_status = status
        self._save(force=True)
        return False

    def _stop_trading(self, status: str, reason: str) -> None:
        log.info("%s", reason)
        st.set_trading_enabled(False)
        self.state.trading_enabled = False
        self.state.status = self.state.bot_status = status
        self._sync_stats()
        self.state.trading_enabled = False
        self._save(force=True)

    def _sync_stats(self) -> None:
        trades = st.load_trades()
        stats = st.calc_stats(trades)
        bal = st.load_balance()
        cfg = self._load_cfg()
        self.state.balance = float(bal.get("balance", 0.0) or 0.0)
        self.state.initial_balance = float(bal.get("initial", 0.0) or 0.0)
        self.state.trades_total = int(stats.get("trades_total", 0) or 0)
        self.state.wins = int(stats.get("wins", 0) or 0)
        self.state.losses = int(stats.get("losses", 0) or 0)
        self.state.total_pnl = float(stats.get("total_pnl", 0.0) or 0.0)
        self.state.win_rate = float(stats.get("win_rate", 0.0) or 0.0)
        self.state.total_wagered = float(stats.get("total_wagered", 0.0) or 0.0)
        self.state.recent_win_rate = float(stats.get("recent_win_rate", 0.0) or 0.0)
        daily = st.load_daily_pnl()
        self.state.daily_pnl = float(daily.get("pnl", 0.0) or 0.0)
        self.state.daily_trades = int(daily.get("trades", 0) or 0)
        self.state.daily_halted = bool(daily.get("halted", False))
        self.state.daily_halt_reason = str(daily.get("halt_reason", "") or "")
        self.state.daily_start_balance = float(daily.get("start_balance", 0.0) or 0.0)
        self.state.daily_profit_stop_usd = float(getattr(cfg, "daily_profit_stop_usd", 0.0) or 0.0)
        self.state.daily_profit_stop_amount = self._daily_profit_stop_amount(cfg, daily)
        if config.MOCK_MODE:
            ledger = st.calc_trade_ledger_balance(trades, bal)
            self.state.ledger_balance = ledger["ledger_balance"]
            self.state.ledger_balance_drift = ledger["ledger_balance_drift"]
            self.state.ledger_balance_ok = ledger["ledger_balance_ok"]
        self._update_open_legs(trades)

    @staticmethod
    def _daily_profit_stop_amount(cfg: st.BotSettings, daily: dict) -> float:
        del daily
        amount = float(getattr(cfg, "daily_profit_stop_usd", 0.0) or 0.0)
        if amount <= 0:
            return 0.0
        return round(amount, 4)

    def _apply_daily_profit_halt(self, cfg: st.BotSettings | None = None) -> bool:
        cfg = cfg or self._load_cfg()
        daily = st.load_daily_pnl()
        stop_amount = self._daily_profit_stop_amount(cfg, daily)
        daily_pnl = float(daily.get("pnl", 0.0) or 0.0)
        if stop_amount <= 0 or daily_pnl < stop_amount:
            if bool(daily.get("halted", False)):
                st.set_daily_halted(False)
            return False

        reason = (
            f"Daily profit target reached: ${daily_pnl:.2f} "
            f">= ${stop_amount:.2f}"
        )
        if not bool(daily.get("halted", False)) or daily.get("halt_reason") != reason:
            log.info("%s. Pausing entries until next trading day.", reason)
            st.set_daily_halted(True, reason)
            asyncio.create_task(tg.send(f"⚠️ **DAILY PROFIT STOP**\n{reason}\nBot will resume on the next trading day."))
        self.state.status = self.state.bot_status = "daily_profit_stop"
        self._sync_stats()
        self._save(force=True)
        return True

    def _update_open_legs(self, trades: list[dict] | None = None) -> None:
        trades = trades if trades is not None else st.load_trades()
        current_window = int(self.state.current_window or 0)
        out: list[dict] = []
        for trade in trades:
            if str(trade.get("trigger") or "").upper() != "END_WINDOW":
                continue
            if trade.get("resolved") or trade.get("exited_early"):
                continue
            if current_window and int(trade.get("window_ts") or 0) != current_window:
                continue
            leg = dict(trade)
            outcome = str(leg.get("outcome") or "").upper()
            px = self.state.up_price if outcome == "UP" else self.state.down_price
            entry = float(leg.get("entry_price", 0.0) or 0.0)
            shares = float(leg.get("shares", 0.0) or 0.0)
            leg["current_price"] = px
            leg["unrealized_pnl"] = (
                round((float(px or 0.0) - entry) * shares, 4)
                if px and entry > 0 and shares > 0
                else None
            )
            out.append(leg)
        self.state.open_legs = out
        self.state.has_open_position = bool(out)
        if out:
            first = out[0]
            self.state.open_outcome = str(first.get("outcome") or "")
            self.state.open_entry_price = float(first.get("entry_price", 0.0) or 0.0)
            self.state.open_shares = float(first.get("shares", 0.0) or 0.0)
            self.state.open_trigger = str(first.get("trigger") or "")
            self.state.open_amount_usd = float(first.get("amount_usd", 0.0) or 0.0)
            self.state.open_unrealized_pnl = float(first.get("unrealized_pnl") or 0.0)
        else:
            self.state.open_outcome = ""
            self.state.open_entry_price = 0.0
            self.state.open_shares = 0.0
            self.state.open_trigger = ""
            self.state.open_amount_usd = 0.0
            self.state.open_unrealized_pnl = 0.0

    def _save(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save_state < SAVE_STATE_INTERVAL:
            return
        self._last_save_state = now
        self.state.log_lines = _log_buf[-50:]
        self.state.mock_mode = config.MOCK_MODE
        active = self._window_settings or self._load_cfg()
        self.state.active_settings = st.asdict(active)
        st.save_state(self.state)

    async def _sleep_until(self, target_ts: float) -> None:
        while self.running:
            delay = target_ts - time.time()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 1.0))

    async def _update_market_context_bg(self, now: float) -> None:
        """FIX #1: hitung regime/context di executor + background task.

        Tidak di-await oleh tick loop, jadi tidak mempengaruhi latency_ms.
        Guard ``_context_inflight`` mencegah penumpukan task.
        """
        if self._context_inflight:
            return
        self._context_inflight = True
        try:
            loop = asyncio.get_running_loop()
            snapshots = await loop.run_in_executor(None, st.load_snapshots)
            context = await loop.run_in_executor(
                None, lambda: analyze_market_context(snapshots, now=now)
            )
            self.state.current_regime = context["regime"]
            self.state.regime_reason = context["reason"]
            self.state.market_context_source = context["source"]
            self.state.market_context_confidence = context["confidence"]
            self.state.market_context_coverage_20m = context["coverage_20m"]
            self.state.delta_10s = context["delta_10s"]
            self.state.delta_10s_avg_20m = context["avg_signed_delta_10s_20m"]
            self.state.delta_10s_abs_avg_20m = context["avg_abs_delta_10s_20m"]
            self.state.delta_10s_abs_p90_20m = context["p90_abs_delta_10s_20m"]
            self.state.trend_net_move_20m = context["net_move_20m"]
            self.state.trend_slope_per_min_20m = context["slope_per_min_20m"]
            self.state.trend_efficiency_20m = context["efficiency_20m"]
            self.state.saturation_avg_secs_30m = context["saturation_avg_secs_30m"]
            self.state.saturation_samples_30m = context["saturation_samples_30m"]
            self.state.locked_avg_secs_30m = context["locked_avg_secs_30m"]
            self.state.locked_samples_30m = context["locked_samples_30m"]
            self.state.saturation_completed_windows_30m = context["completed_windows_30m"]
        except Exception as exc:
            log.debug("market context bg failed: %s", exc)
        finally:
            self._context_inflight = False

    async def _resync_official_target(
        self,
        session: aiohttp.ClientSession,
        market: mkt.BTCMarket,
        window_ts: int,
        market_interval: int,
        btc_open: float,
    ) -> tuple[float, mkt.BTCMarket]:
        """FIX #2: adopsi priceToBeat resmi Polymarket saat sudah terbit.

        ``btc_open`` awal bisa berasal dari snapshot Chainlink bot sendiri kalau
        priceToBeat belum tersedia di awal window. Method ini re-fetch market
        secara berkala (gated TARGET_RESYNC_SECS) sampai priceToBeat resmi muncul,
        lalu mengunci ``btc_open`` ke nilai resmi tersebut. Return (btc_open, market).
        """
        if self._target_locked_official or market.target_price > 0 and self._target_locked_official:
            return btc_open, market
        now = time.time()
        # Jika btc_open sudah == priceToBeat resmi yang ada di market, kunci.
        if market.target_price > 0 and abs(float(market.target_price) - float(btc_open)) <= 1e-6:
            self._target_locked_official = True
            return btc_open, market
        if now - self._last_target_resync < TARGET_RESYNC_SECS:
            return btc_open, market
        self._last_target_resync = now
        try:
            fresh = await asyncio.wait_for(
                mkt.fetch_market(session, "BTC", interval_secs=market_interval),
                timeout=PRICE_SOURCE_TIMEOUT_SECS,
            )
        except Exception as exc:
            log.debug("[FIX#2] target re-fetch failed: %s", exc)
            return btc_open, market
        if not fresh or int(getattr(fresh, "window_ts", 0) or 0) != int(window_ts):
            return btc_open, market
        official = float(getattr(fresh, "target_price", 0.0) or 0.0)
        if official > 0:
            if abs(official - float(btc_open)) > 1e-6:
                log.warning(
                    "[FIX#2] btc_open re-synced ke priceToBeat resmi: %.2f -> %.2f (drift %.2f)",
                    float(btc_open), official, official - float(btc_open),
                )
            market.target_price = official
            self._target_locked_official = True
            return official, market
        return btc_open, market

    async def _btc_now(self, session: aiohttp.ClientSession) -> float:
        cache = get_cache()
        now = time.time()
        source_price = getattr(cache, "source_btc", None)
        source_age = getattr(cache, "source_btc_age", None)
        chainlink = source_price("chainlink", CHAINLINK_PRICE_MAX_AGE_SECS) if source_price else None
        coinbase = source_price("coinbase", EXCHANGE_PRICE_MAX_AGE_SECS) if source_price else None
        gateio = source_price("gateio", EXCHANGE_PRICE_MAX_AGE_SECS) if source_price else None
        if chainlink and float(chainlink) > 0:
            self._last_btc_source = "chainlink"
            self._last_chainlink_age = float(source_age("chainlink")) if source_age else 0.0
            exchange_ages = [
                float(source_age(name))
                for name, price in (("coinbase", coinbase), ("gateio", gateio))
                if price and source_age
            ]
            self._last_exchange_age = min(exchange_ages, default=999.0)
            return float(chainlink)
            
        if coinbase and float(coinbase) > 0:
            self._last_btc_source = "coinbase (fallback)"
            self._last_chainlink_age = float(source_age("coinbase")) if source_age else 0.0
            exchange_ages = [
                float(source_age(name))
                for name, price in (("coinbase", coinbase), ("gateio", gateio))
                if price and source_age
            ]
            self._last_exchange_age = min(exchange_ages, default=999.0)
            return float(coinbase)
            
        if gateio and float(gateio) > 0:
            self._last_btc_source = "gateio (fallback)"
            self._last_chainlink_age = float(source_age("gateio")) if source_age else 0.0
            exchange_ages = [
                float(source_age(name))
                for name, price in (("coinbase", coinbase), ("gateio", gateio))
                if price and source_age
            ]
            self._last_exchange_age = min(exchange_ages, default=999.0)
            return float(gateio)

        self._last_btc_source = "chainlink-unavailable"
        self._last_chainlink_age = float(source_age("chainlink")) if source_age else 999.0
        exchange_ages = [
            float(source_age(name))
            for name, price in (("coinbase", coinbase), ("gateio", gateio))
            if price and source_age
        ]
        self._last_exchange_age = min(exchange_ages, default=float(cache.btc_age))
        return 0.0

    async def _btc_open(self, session: aiohttp.ClientSession, market: mkt.BTCMarket, window_ts: int) -> tuple[float, str]:
        if market.target_price > 0:
            return float(market.target_price), "Polymarket priceToBeat"
        source_at_time = getattr(get_cache(), "source_btc_at_time", None)
        snap = source_at_time("chainlink", float(window_ts), max_drift=2.0) if source_at_time else None
        if snap:
            price, _ts, drift = snap
            return float(price), f"Polymarket Chainlink open drift={float(drift):.2f}s"
        if config.MOCK_MODE and source_at_time:
            max_drift = min(60.0, max(2.0, time.time() - float(window_ts or 0)))
            snap = source_at_time("chainlink", float(window_ts), max_drift=max_drift)
            if snap:
                price, _ts, drift = snap
                return float(price), f"MOCK Chainlink open fallback drift={float(drift):.2f}s"
        if config.MOCK_MODE:
            source_price = getattr(get_cache(), "source_btc", None)
            chainlink = source_price("chainlink", 60.0) if source_price else None
            if chainlink and float(chainlink) > 0:
                return float(chainlink), "MOCK Chainlink current fallback"
        return 0.0, "missing Polymarket priceToBeat"

    async def _refresh_market(self, session: aiohttp.ClientSession, market: mkt.BTCMarket, secs_left: float) -> mkt.BTCMarket:
        cache = get_cache()
        refreshed_any = False
        for label, token_id in (("up", market.up_token), ("down", market.down_token)):
            bid_ask = cache.get_clob_full(token_id)
            if bid_ask:
                bid, ask = bid_ask
                if label == "up":
                    if bid > 0:
                        market.up_price = bid
                    if ask > 0:
                        market.up_ask = ask
                else:
                    if bid > 0:
                        market.down_price = bid
                    if ask > 0:
                        market.down_ask = ask
                refreshed_any = True
            depth = cache.get_depth(token_id)
            if label == "up":
                market.up_bid_depth = depth.get("bids", [])
                market.up_ask_depth = depth.get("asks", [])
            else:
                market.down_bid_depth = depth.get("bids", [])
                market.down_ask_depth = depth.get("asks", [])
        if refreshed_any:
            market.book_ts = time.time()
        if market.up_ask > 0 and market.down_ask > 0:
            return market
        try:
            coro = mkt.refresh_prices(session, market)
            timeout = min(
                FAST_REFRESH_TIMEOUT_SECS if secs_left <= FAST_REFRESH_SECS_LEFT else MARKET_REFRESH_TIMEOUT_SECS,
                0.01,
            )
            return await asyncio.wait_for(coro, timeout=max(0.01, timeout))
        except Exception as exc:
            log.debug("refresh_prices failed: %s", exc)
            return market

    async def _fetch_window_close_btc(self, session: aiohttp.ClientSession, window_ts: int) -> float | None:
        cached = self._close_btc.pop(window_ts, None)
        if cached and cached > 0:
            return cached
        price = await mkt.get_btc_close_price(session)
        return float(price) if price and price > 0 else None

    async def _fetch_official_outcome(self, session: aiohttp.ClientSession, slug: str, source: str) -> str | None:
        if not slug:
            return None
        attempts = max(1, OFFICIAL_RESOLVE_RETRIES if source == "window-close" else 1)
        for idx in range(attempts):
            actual = await mkt.fetch_resolved_outcome(session, slug, allow_implied=False)
            if actual:
                return actual
            if idx < attempts - 1:
                await asyncio.sleep(max(0.1, OFFICIAL_RESOLVE_DELAY_SECS))
        return None

    async def _fetch_gamma_resolution(self, session: aiohttp.ClientSession, slug: str, source: str) -> dict | None:
        if not slug:
            return None
        attempts = max(1, OFFICIAL_RESOLVE_RETRIES if source == "window-close" else 1)
        for idx in range(attempts):
            resolution = await mkt.fetch_resolution(session, slug, allow_implied=False)
            if resolution:
                return resolution
            if idx < attempts - 1:
                await asyncio.sleep(max(0.1, OFFICIAL_RESOLVE_DELAY_SECS))
        return await mkt.fetch_resolution(session, slug, allow_implied=True)

    async def _watch_window_without_target(
        self,
        session: aiohttp.ClientSession,
        market: mkt.BTCMarket,
        window_ts: int,
        close_ts: int,
        source: str,
    ) -> tuple[float, str, mkt.BTCMarket] | None:
        """Keep dashboard state live when Polymarket target metadata is missing."""
        self.state.market_question = market.question
        self.state.btc_open = 0.0
        self.state.btc_distance = 0.0
        self.state.status = self.state.bot_status = "missing_target"
        log.warning("%s for %s, watching without trading until target is available.", source, market.slug)
        while self.running:
            tick_started = time.perf_counter()
            now = time.time()
            secs_left = close_ts - now
            secs_elapsed = now - window_ts
            if secs_left <= 0:
                break
            btc_now = await self._btc_now(session)
            market = await self._refresh_market(session, market, secs_left)
            recovered_open, recovered_source = await self._btc_open(session, market, window_ts)
            if recovered_open > 0:
                log.info(
                    "[END_WINDOW] recovered btc_open for %s: %.2f (%s)",
                    market.slug,
                    recovered_open,
                    recovered_source,
                )
                return recovered_open, recovered_source, market
            cache = get_cache()
            up_age = cache.clob_age(market.up_token)
            down_age = cache.clob_age(market.down_token)

            self.state.seconds_left = secs_left
            self.state.secs_elapsed = secs_elapsed
            self.state.btc_price = btc_now
            self.state.btc_distance = 0.0
            self.state.up_price = market.up_price
            self.state.down_price = market.down_price
            self.state.up_ask = market.up_ask
            self.state.down_ask = market.down_ask
            self.state.up_spread = market.up_spread
            self.state.down_spread = market.down_spread
            self.state.up_ask_depth = list(market.up_ask_depth or [])
            self.state.down_ask_depth = list(market.down_ask_depth or [])
            self.state.chainlink_age_secs = float(self._last_chainlink_age)
            self.state.exchange_age_secs = float(self._last_exchange_age)
            self.state.clob_age_secs = float(min(up_age, down_age))
            self.state.price_feed_source = str(self._last_btc_source or cache.btc_source or "unknown")
            self.state.leading = market.leading_outcome
            self.state.balance = st.get_balance()
            self.state.trading_enabled = st.get_trading_enabled()
            self._update_open_legs()
            self._save()
            elapsed = time.perf_counter() - tick_started
            await asyncio.sleep(max(TICK_INTERVAL, 0.5 - elapsed))
        return None

    async def _resolve_directional_window(
        self,
        session: aiohttp.ClientSession | None,
        window_ts: int,
        source: str = "window-close",
    ) -> list[dict]:
        session = session or await self.get_session()
        open_trades = [
            t for t in st.load_trades()
            if int(t.get("window_ts") or 0) == int(window_ts)
            and str(t.get("trigger") or "").upper() == "END_WINDOW"
            and not t.get("resolved")
            and not t.get("exited_early")
        ]
        if not open_trades:
            return []
        if time.time() < window_ts + mkt.INTERVAL:
            return []

        groups: dict[str, list[dict]] = {}
        for trade in open_trades:
            groups.setdefault(str(trade.get("market_slug") or ""), []).append(trade)

        resolved: list[dict] = []
        for slug in groups:
            resolution = await self._fetch_gamma_resolution(session, slug, source)
            actual = str((resolution or {}).get("actual") or "").upper()
            if actual not in ("UP", "DOWN"):
                log.warning(
                    "[END_WINDOW] resolve deferred: official Gamma outcome unavailable for %s source=%s",
                    slug or window_ts,
                    source,
                )
                continue
            close_px = float((resolution or {}).get("final_price") or 0.0)
            if close_px <= 0:
                close_px = float(await self._fetch_window_close_btc(session, window_ts) or 0.0)
            price_to_beat = float((resolution or {}).get("price_to_beat") or 0.0)
            resolution_source = str((resolution or {}).get("source") or "gamma")
            group = st.update_directional_results(
                window_ts,
                float(close_px),
                market_slug=slug,
                triggers=("END_WINDOW",),
                actual=actual,
                price_to_beat=price_to_beat,
                resolution_source=resolution_source,
            )
            resolved.extend(group)

        if not resolved:
            return []

        returned = round(sum(float(t.get("balance_returned", 0.0) or 0.0) for t in resolved), 4)
        total_pnl = round(sum(float(t.get("pnl", 0.0) or 0.0) for t in resolved), 4)
        if returned > 0:
            st.add_balance(returned)
        for trade in resolved:
            st.update_daily_pnl(float(trade.get("pnl", 0.0) or 0.0), trade.get("won"))
        if not config.MOCK_MODE:
            trader.invalidate_live_balance_cache()

        self._session_pnl = round(self._session_pnl + total_pnl, 4)
        self.state.session_pnl = self._session_pnl
        self._sync_stats()
        self._save(force=True)

        cfg = st.load_settings()
        self._apply_daily_profit_halt(cfg)
        wins = sum(1 for t in resolved if t.get("won") is True)
        losses = sum(1 for t in resolved if t.get("won") is False)
        log.info(
            "[END_WINDOW] resolved window=%d legs=%d W:%d L:%d pnl=$%+.4f returned=$%.4f",
            window_ts,
            len(resolved),
            wins,
            losses,
            total_pnl,
            returned,
        )
        try:
            context = analyze_market_context(st.load_snapshots(), now=time.time())
            await tg.send(tg.format_trading_result(resolved, context, self.state))
        except Exception as exc:
            log.warning("[END_WINDOW] telegram result notification failed: %s", exc)
        return resolved

    async def _resolve_closed_directional_orphans(self, session: aiohttp.ClientSession, current_window_ts: int) -> None:
        now = time.time()
        if now - self._last_directional_orphan_resolve_check < max(1.0, OFFICIAL_RETRY_INTERVAL_SECS):
            return
        self._last_directional_orphan_resolve_check = now
        windows = sorted({
            int(t.get("window_ts") or 0)
            for t in st.load_trades()
            if str(t.get("trigger") or "").upper() == "END_WINDOW"
            and not t.get("resolved")
            and not t.get("exited_early")
            and int(t.get("window_ts") or 0) > 0
            and int(t.get("window_ts") or 0) < current_window_ts
        })
        for old_window in windows:
            await self._resolve_directional_window(session, old_window, source="retry-official")

    @staticmethod
    def _set_env_values(updates: dict[str, str]) -> None:
        path = Path(__file__).resolve().parent.parent / ".env"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            key, sep, _value = line.partition("=")
            if sep and key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                out.append(line)
        for key, value in updates.items():
            if key not in seen:
                out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")

    async def _handle_telegram_command(self, text: str) -> str | None:
        parts = text.strip().split()
        if not parts:
            return None
        command = parts[0].split("@", 1)[0].lower()
        if command == "/trading":
            st.set_emergency_stop(False)
            if not self._apply_safety_gate("telegram_safety_block"):
                reason = str(getattr(self.state, "circuit_breaker_reason", "") or "safety blocked")
                return f"Trading not started: {reason}"
            st.set_trading_enabled(True)
            self.state.trading_enabled = True
            self.state.status = self.state.bot_status = "telegram_start"
            self._save(force=True)
            mode = "MOCK" if config.MOCK_MODE else "LIVE"
            return f"Trading started\nStatus trading: {mode}"
        if command == "/stop":
            st.set_trading_enabled(False)
            self.state.trading_enabled = False
            self.state.status = self.state.bot_status = "waiting"
            self._save(force=True)
            return "Trading stopped"
        if command == "/open":
            return tg.format_open_positions(st.load_trades(), self.state)
        if command == "/screenshot":
            path: Path | None = None
            try:
                path = await tg.capture_dashboard_screenshot()
                await tg.send_photo(path, "END_WINDOW Bot dashboard")
                return None
            except Exception as exc:
                return f"Screenshot failed: {exc}"
            finally:
                if path:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
        if command == "/status":
            if len(parts) >= 2:
                mode = parts[1].strip().lower()
                if mode == "mock":
                    self._set_env_values({"MOCK_MODE": "true"})
                    return (
                        "MOCK mode saved for next restart.\n"
                        f"Current running mode: {'MOCK' if config.MOCK_MODE else 'LIVE'}"
                    )
                if mode == "live":
                    if len(parts) < 3 or parts[2] != safety.LIVE_CONFIRM_TEXT:
                        return (
                            "LIVE mode not changed.\n"
                            f"Use: /status live {safety.LIVE_CONFIRM_TEXT}\n"
                            "Restart is required after changing mode."
                        )
                    self._set_env_values({
                        "MOCK_MODE": "false",
                        "LIVE_TRADING_CONFIRM": safety.LIVE_CONFIRM_TEXT,
                    })
                    return (
                        "LIVE mode saved for next restart.\n"
                        "Restart bot/dashboard before trading live."
                    )
            return tg.format_runtime_status(self.state)
        return "\n".join([
            "Unknown command.",
            "/trading - start trading",
            "/stop - stop trading",
            "/open - open positions",
            "/screenshot - dashboard screenshot",
            "/status - trading mode/status",
        ])

    async def _telegram_command_loop(self) -> None:
        offset: int | None = None
        try:
            old_updates = await tg.get_updates(offset=-1, timeout=0)
            if old_updates:
                offset = max(int(update.get("update_id", 0)) for update in old_updates) + 1
        except Exception as exc:
            log.debug("telegram command offset init failed: %s", exc)
        while self.running:
            try:
                updates = await tg.get_updates(offset=offset, timeout=20)
                for update in updates:
                    update_id = int(update.get("update_id", 0) or 0)
                    offset = max(offset or 0, update_id + 1)
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    if str(chat.get("id") or "") != str(config.TG_CHAT):
                        continue
                    text = str(message.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    response = await self._handle_telegram_command(text)
                    if response:
                        await tg.send(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("telegram command loop failed: %s", exc)
                await asyncio.sleep(3.0)

    @staticmethod
    def _on_bg_task_done(task: asyncio.Task) -> None:
        """Log unhandled exceptions from background tasks instead of losing them."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Background task %s crashed: %s", task.get_name(), exc, exc_info=exc)

    async def run(self):
        mode = "MOCK" if config.MOCK_MODE else "LIVE"
        label = "+".join(sorted(enabled_strategies())) or config.STRATEGY_MODE
        log.info("=" * 58)
        log.info("BOT [%s] strategies=%s", mode, label)
        log.info("Dashboard: http://localhost:%s", os.getenv("DASH_PORT", "5004"))
        if self.state.trading_enabled:
            log.info("Trading RESTORED from previous session")
        else:
            log.info("Trading disabled — send /trading to start")
        log.info("=" * 58)

        if not self._apply_safety_gate("startup_safety_block"):
            return

        await self._ws_feed.start()
        for task in self._ws_feed._tasks:
            task.add_done_callback(self._on_bg_task_done)
        if config.TG_TOKEN and config.TG_CHAT:
            self._telegram_task = asyncio.create_task(self._telegram_command_loop())
            self._telegram_task.add_done_callback(self._on_bg_task_done)
        try:
            while self.running:
                if st.get_emergency_stop():
                    self.state.trading_enabled = False
                    self.state.status = self.state.bot_status = "emergency_stop"
                    st.set_trading_enabled(False)
                    self._save(force=True)
                    await asyncio.sleep(1.0)
                    continue

                if not st.get_trading_enabled():
                    self.state.trading_enabled = False
                    self.state.status = self.state.bot_status = "waiting"
                    self._sync_stats()
                    self._save()
                    await asyncio.sleep(1.0)
                    continue

                if self._apply_daily_profit_halt():
                    await asyncio.sleep(30.0)
                    continue

                self.state.trading_enabled = True
                self._sync_stats()
                if not self._apply_safety_gate("runtime_safety_block"):
                    await asyncio.sleep(1.0)
                    continue
                self.state.status = self.state.bot_status = "scanning"
                await self.trade_window()
        finally:
            await self.shutdown()

    async def trade_window(self):
        session = await self.get_session()
        trades_this_window = 0

        cfg = self._load_cfg()
        market_interval = mkt.INTERVAL
        window_ts = mkt.current_window_ts(market_interval)
        close_ts = window_ts + market_interval
        self._window_settings = cfg
        strategy_cfg = end_window_strategy.EndWindowConfig.from_settings(cfg)
        self.state.max_trades_per_window = cfg.max_trades_per_window
        self.state.current_window = window_ts
        self.state.market_interval_secs = market_interval
        self.state.market_interval_label = mkt.market_interval_label(market_interval)
        self.state.trades_this_window = 0
        self.state.window_snapshots = []
        self.state.status = self.state.bot_status = "scanning"
        self._sync_stats()
        self._save(force=True)

        await self._resolve_closed_directional_orphans(session, window_ts)

        health = await mkt.check_api_health(session)
        if health.get("maintenance"):
            log.warning("API maintenance: %s", health.get("message", ""))
            self.state.status = self.state.bot_status = "idle"
            self._save(force=True)
            await self._sleep_until(close_ts + 5)
            return

        market = await mkt.fetch_market(session, "BTC", interval_secs=market_interval)
        if market is None:
            log.warning("Market not found, skip window.")
            self.state.status = self.state.bot_status = "idle"
            self._save(force=True)
            await self._sleep_until(close_ts + 5)
            return

        set_clob_tokens(market.up_token, market.down_token)
        btc_open, source = await self._btc_open(session, market, window_ts)
        if btc_open <= 0:
            log.warning("%s for %s, skip trades: delta rules require a valid target.", source, market.slug)
            recovered = await self._watch_window_without_target(session, market, window_ts, close_ts, source)
            if recovered is None:
                clear_clob_tokens()
                self.state.status = self.state.bot_status = "idle"
                self._save(force=True)
                return
            btc_open, source, market = recovered
        self.state.market_question = market.question
        self.state.btc_open = btc_open
        # FIX #2: reset state re-sync target tiap window. Kunci segera hanya jika
        # btc_open sudah berasal dari priceToBeat resmi Polymarket.
        self._target_locked_official = source.startswith("Polymarket priceToBeat")
        self._last_target_resync = 0.0
        log.info(
            "Window %d | closes %s UTC | btc_open=$%.2f (%s) | balance=$%.2f",
            window_ts,
            datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime("%H:%M:%S"),
            btc_open,
            source,
            st.get_balance(),
        )

        self.state.status = self.state.bot_status = "watching"
        self._save(force=True)

        while self.running:
            tick_started = time.perf_counter()
            now = time.time()
            secs_left = close_ts - now
            secs_elapsed = now - window_ts
            if secs_left <= 0:
                break

            await self._resolve_closed_directional_orphans(session, window_ts)
            if self._apply_daily_profit_halt(cfg):
                break
            if not st.get_trading_enabled():
                self.state.trading_enabled = False
                self.state.status = self.state.bot_status = "waiting"
                self._sync_stats()
                self._save(force=True)
                break
            btc_now = await self._btc_now(session)
            market = await self._refresh_market(session, market, secs_left)
            # FIX #2: adopsi priceToBeat resmi begitu terbit; kunci btc_open.
            btc_open, market = await self._resync_official_target(
                session, market, window_ts, market_interval, btc_open
            )
            self.state.btc_open = btc_open
            target_unconfirmed = not self._target_locked_official
            delta = (
                float(btc_now or 0.0) - float(btc_open or 0.0)
                if btc_open > 0
                else 0.0
            )
            distance = abs(delta)
            cache = get_cache()
            up_age = cache.clob_age(market.up_token)
            down_age = cache.clob_age(market.down_token)

            self.state.seconds_left = secs_left
            self.state.secs_elapsed = secs_elapsed
            self.state.btc_price = btc_now
            self.state.btc_distance = delta
            self.state.up_price = market.up_price
            self.state.down_price = market.down_price
            self.state.up_ask = market.up_ask
            self.state.down_ask = market.down_ask
            self.state.up_spread = market.up_spread
            self.state.down_spread = market.down_spread
            self.state.up_ask_depth = list(market.up_ask_depth or [])
            self.state.down_ask_depth = list(market.down_ask_depth or [])
            self.state.chainlink_age_secs = float(self._last_chainlink_age)
            self.state.exchange_age_secs = float(self._last_exchange_age)
            self.state.clob_age_secs = float(min(up_age, down_age))
            self.state.price_feed_source = str(self._last_btc_source or cache.btc_source or "unknown")
            self.state.leading = "UP" if delta > 0 else "DOWN" if delta < 0 else market.leading_outcome
            self.state.balance = st.get_balance()
            self.state.trading_enabled = st.get_trading_enabled()
            self.state.trades_this_window = trades_this_window
            self._update_open_legs()

            if now - self._last_snapshot >= SNAPSHOT_INTERVAL:
                self._last_snapshot = now
                st.save_snapshot(
                    window_ts,
                    secs_left,
                    secs_elapsed,
                    market.up_price,
                    market.down_price,
                    btc_now,
                    btc_open,
                    delta,
                    self.state.leading,
                    up_spread=market.up_spread,
                    down_spread=market.down_spread,
                )
                # FIX #1: jalankan analisis regime (load snapshots + hitung 20-30m)
                # di background task agar tidak menggelembungkan latency_ms tick.
                asyncio.create_task(self._update_market_context_bg(now))

            try:
                buy1_exits = await end_window.try_buy1_exits(
                    market=market,
                    secs_left=secs_left,
                    session=session,
                    settings=cfg,
                )
                if buy1_exits:
                    self._sync_stats()
                    self._save(force=True)
                    if not config.MOCK_MODE:
                        trader.invalidate_live_balance_cache()
                near_strike = (
                    TARGET_NEAR_STRIKE_USD > 0
                    and abs(delta) <= TARGET_NEAR_STRIKE_USD
                )
                if target_unconfirmed and near_strike:
                    # FIX #2 (resync_and_guard): target resmi belum terkonfirmasi
                    # dan harga dekat strike — arah UP/DOWN belum bisa dipercaya.
                    # Tahan entry baru (exit di atas tetap jalan).
                    self.state.status = self.state.bot_status = "target_unconfirmed"
                    if int(now) % 5 == 0:
                        log.warning(
                            "[FIX#2] entry ditahan: priceToBeat resmi belum konfirmasi "
                            "& near-strike (delta=$%.2f <= $%.2f)",
                            abs(delta), TARGET_NEAR_STRIKE_USD,
                        )
                    records = []
                else:
                    records = await end_window.try_all_end_window(
                        market=market,
                        btc_open=btc_open,
                        btc_now=btc_now,
                        secs_elapsed=secs_elapsed,
                        secs_left=secs_left,
                        bankroll_usd=st.get_balance(),
                        session=session,
                        cfg=strategy_cfg,
                        settings=cfg,
                    )
            except Exception as exc:
                log.warning("[END_WINDOW] dispatcher exception: %s", exc)
                records = []

            if records:
                trades_this_window += len(records)
                self.state.trades_this_window = trades_this_window
                self.state.last_trade_time = time.time()
                self._sync_stats()
                self._save(force=True)
                if not config.MOCK_MODE:
                    trader.invalidate_live_balance_cache()
                for rec in records:
                    await tg.send(
                        f"END_WINDOW {rec.outcome} BUY\n"
                        f"{rec.trigger_reason}\n"
                        f"Window {rec.window_ts} | @{rec.entry_price:.4f} "
                        f"shares={rec.shares:.2f} ${rec.amount_usd:.2f}\n"
                        f"Balance ${st.get_balance():.2f}"
                    )

            elapsed = time.perf_counter() - tick_started
            self.state.latency_ms = round(elapsed * 1000.0, 1)
            self._save()
            await asyncio.sleep(max(0.0, TICK_INTERVAL - elapsed))

        self._close_btc[window_ts] = await self._btc_now(session)
        await self._resolve_directional_window(session, window_ts, source="window-close")
        clear_clob_tokens()
        self.state.status = self.state.bot_status = "idle"
        self._window_settings = None
        self._sync_stats()
        self._save(force=True)
        await self._sleep_until(close_ts + 5)

    async def shutdown(self):
        self.running = False
        clear_clob_tokens()
        if self._telegram_task:
            self._telegram_task.cancel()
            try:
                await self._telegram_task
            except asyncio.CancelledError:
                pass
        try:
            await self._ws_feed.stop()
        except Exception:
            pass
        if self._session and not self._session.closed:
            await self._session.close()
        self.state.status = self.state.bot_status = "stopped"
        self._save(force=True)


def main():
    if not acquire_instance_lock(log):
        return

    bot = Bot()

    def _stop(_sig, _frame):
        bot.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Bot stopped by user (Ctrl+C).")
    except Exception:
        log.exception("Bot crashed with unhandled exception")
    finally:
        release_instance_lock()


if __name__ == "__main__":
    main()
