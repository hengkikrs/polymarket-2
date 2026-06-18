"""
ws_feed.py — WebSocket Price Feed
===================================
Persistent WebSocket connections untuk BTC price (Gate.io / Binance)
dan CLOB orderbook (Polymarket).

Arsitektur:
  - Background task yang jalan di event loop yang sama dengan bot
  - Harga disimpan di shared dict (in-memory, zero latency read)
  - Auto-reconnect dengan exponential backoff
  - Custom DNS resolver (8.8.8.8) untuk bypass ISP blocks
  - Fallback: jika WS mati, market.py tetap bisa REST polling

Usage di main.py:
  feed = WsFeed()
  await feed.start()       # mulai background tasks
  btc = feed.btc_price     # baca instant, no await
  up  = feed.clob_price("UP", token_id)
  await feed.stop()        # cleanup saat shutdown
"""
import asyncio, json, time, logging, os
from typing import Optional
import aiohttp
from aiohttp import TCPConnector
from core.orderbook import top_depth

log = logging.getLogger("ws_feed")


def _make_connector() -> TCPConnector:
    """Create connector with custom DNS + proper SSL."""
    from core.ssl_helper import make_connector
    return make_connector(use_custom_dns=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _clob_poll_interval(token_count: int, target_rpm: float, min_sweep_interval: float) -> float:
    token_count = max(1, int(token_count or 1))
    target_rpm = max(60.0, float(target_rpm or 60.0))
    return max(float(min_sweep_interval or 0.0), token_count * 60.0 / target_rpm)

# ─────────────────────────────────────────────────────────────────────────────
#  SHARED PRICE CACHE
# ─────────────────────────────────────────────────────────────────────────────

class PriceCache:
    """Thread-safe-ish price store. Writes from WS tasks, reads from bot loop.

    UPGRADE A: _clob now stores (bid, ask) tuple per token instead of just bid.
    UPGRADE EDGE: _depth stores top-5 (price, size) per side per token.
    Backward-compat: get_clob() still returns bid; new get_clob_full() / get_depth().
    """
    __slots__ = (
        '_btc', '_btc_ts', '_btc_source', '_btc_sources',
        '_clob', '_clob_ts', '_btc_history', '_btc_source_history', '_depth',
    )

    def __init__(self):
        self._btc: float = 0.0
        self._btc_ts: float = 0.0
        self._btc_source: str = ""
        self._btc_sources: dict[str, tuple[float, float]] = {}
        # token_id -> (best_bid, best_ask). ask=0.0 means unknown.
        self._clob: dict[str, tuple[float, float]] = {}
        self._clob_ts: dict[str, float] = {}
        # Depth per token: token_id -> {"bids": [(p,s),...], "asks": [(p,s),...]}
        # Top-5 per side. Updated by CLOB poller when fetching /book.
        self._depth: dict[str, dict] = {}
        # Ring buffer (timestamp, price) untuk historical lookup
        # 600 entries × ~1Hz = 10 menit window — cukup utk window_ts boundary lookup
        self._btc_history: list[tuple[float, float]] = []
        self._btc_source_history: dict[str, list[tuple[float, float]]] = {}

    @property
    def btc_price(self) -> float:
        return self._btc

    @property
    def btc_age(self) -> float:
        return time.time() - self._btc_ts if self._btc_ts else 999

    @property
    def btc_source(self) -> str:
        return self._btc_source

    def set_btc(self, price: float, source: str = ""):
        self._btc = price
        self._btc_ts = time.time()
        self._btc_source = source
        self.set_source_btc(price, source)
        # Tambah ke history; cap 600 entries (~10 menit)
        self._btc_history.append((self._btc_ts, price))
        if len(self._btc_history) > 600:
            self._btc_history = self._btc_history[-600:]

    def set_source_btc(self, price: float, source: str) -> None:
        key = str(source or "").strip().lower()
        if key and float(price or 0.0) > 0:
            timestamp = time.time()
            self._btc_sources[key] = (float(price), timestamp)
            history = self._btc_source_history.setdefault(key, [])
            history.append((timestamp, float(price)))
            if len(history) > 600:
                self._btc_source_history[key] = history[-600:]

    def source_btc(self, source: str, max_age: float = 999.0) -> Optional[float]:
        prefix = str(source or "").strip().lower()
        matches = [
            (timestamp, price)
            for key, (price, timestamp) in self._btc_sources.items()
            if key == prefix or key.startswith(prefix + "-")
        ]
        if not matches:
            return None
        timestamp, price = max(matches)
        return price if time.time() - timestamp <= max_age else None

    def source_btc_age(self, source: str) -> float:
        prefix = str(source or "").strip().lower()
        timestamps = [
            timestamp
            for key, (_price, timestamp) in self._btc_sources.items()
            if key == prefix or key.startswith(prefix + "-")
        ]
        return time.time() - max(timestamps) if timestamps else 999.0

    def btc_at_time(self, target_ts: float, max_drift: float = 2.0):
        """Ambil BTC price terdekat dgn target_ts dari history.

        max_drift = max tolerated time difference (detik). Return None kalau
        tidak ada data dalam toleransi (mis. bot baru start, history kosong).
        Return (price, actual_ts, drift_secs) kalau ada.
        """
        if not self._btc_history:
            return None
        best = None
        best_diff = float('inf')
        for ts, p in self._btc_history:
            d = abs(ts - target_ts)
            if d < best_diff:
                best_diff = d
                best = (p, ts, d)
        if best is None or best[2] > max_drift:
            return None
        return best

    def source_btc_at_time(self, source: str, target_ts: float, max_drift: float = 2.0):
        prefix = str(source or "").strip().lower()
        history = [
            sample
            for key, samples in self._btc_source_history.items()
            if key == prefix or key.startswith(prefix + "-")
            for sample in samples
        ]
        if not history:
            return None
        price_ts = min(history, key=lambda sample: abs(sample[0] - target_ts))
        drift = abs(price_ts[0] - target_ts)
        if drift > max_drift:
            return None
        return price_ts[1], price_ts[0], drift

    def get_clob(self, token_id: str) -> float:
        """Backward-compat: return best_bid only."""
        v = self._clob.get(token_id)
        return v[0] if v else 0.0

    def get_clob_full(self, token_id: str) -> Optional[tuple[float, float]]:
        """Return (best_bid, best_ask) tuple atau None kalau belum ter-cache."""
        return self._clob.get(token_id)

    def clob_age(self, token_id: str) -> float:
        ts = self._clob_ts.get(token_id, 0)
        return time.time() - ts if ts else 999

    def set_clob(self, token_id: str, best_bid: float, best_ask: float = 0.0):
        """Store (bid, ask). best_ask=0 = unknown (back-compat untuk caller lama)."""
        self._clob[token_id] = (best_bid, best_ask)
        self._clob_ts[token_id] = time.time()

    def set_depth(self, token_id: str, bids: list, asks: list):
        """Store top-N depth. bids/asks = list of (price, size) tuples.
        Used by edge_signals.compute_imbalance() and microprice()."""
        self._depth[token_id] = {"bids": bids, "asks": asks}

    def get_depth(self, token_id: str) -> dict:
        """Return {'bids': [(p,s),...], 'asks': [(p,s),...]}. Empty lists if missing."""
        return self._depth.get(token_id, {"bids": [], "asks": []})


# Singleton cache — diakses oleh market.py dan main.py
_cache = PriceCache()


def get_cache() -> PriceCache:
    return _cache


# ─────────────────────────────────────────────────────────────────────────────
#  GATE.IO WEBSOCKET (BTC/USDT)
# ─────────────────────────────────────────────────────────────────────────────

async def _ws_gateio(cache: PriceCache, stop_event: asyncio.Event):
    """
    Gate.io WebSocket v4: wss://api.gateio.ws/ws/v4/
    Channel: spot.tickers, pair: BTC_USDT
    """
    import aiohttp

    url = "wss://api.gateio.ws/ws/v4/"
    sub_msg = json.dumps({
        "time": int(time.time()),
        "channel": "spot.tickers",
        "event": "subscribe",
        "payload": ["BTC_USDT"]
    })

    backoff = 1
    while not stop_event.is_set():
        try:
            connector = _make_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(url, heartbeat=20, timeout=15) as ws:
                    log.info("Gate.io WS connected")
                    await ws.send_str(sub_msg)
                    backoff = 1  # reset on success

                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if data.get("channel") == "spot.tickers" and data.get("event") == "update":
                                    result = data.get("result", {})
                                    last = result.get("last")
                                    if last:
                                        cache.set_btc(float(last), "gateio")
                            except (ValueError, KeyError, TypeError):
                                pass
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("Gate.io WS error: %s — reconnect in %ds", e, backoff)

        if stop_event.is_set():
            return
        await asyncio.sleep(min(backoff, 30))
        backoff = min(backoff * 2, 30)


def _coinbase_price(message: dict) -> Optional[float]:
    if not isinstance(message, dict) or message.get("channel") != "ticker":
        return None
    for event in message.get("events") or []:
        for ticker in event.get("tickers") or []:
            if ticker.get("product_id") != "BTC-USD":
                continue
            try:
                price = float(ticker.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
    return None


async def _ws_coinbase(cache: PriceCache, stop_event: asyncio.Event):
    url = "wss://advanced-trade-ws.coinbase.com"
    ticker_subscription = json.dumps({
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channel": "ticker",
    })
    heartbeat_subscription = json.dumps({
        "type": "subscribe",
        "channel": "heartbeats",
    })
    backoff = 1
    while not stop_event.is_set():
        try:
            connector = _make_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(url, heartbeat=20, timeout=15) as ws:
                    await ws.send_str(ticker_subscription)
                    await ws.send_str(heartbeat_subscription)
                    log.info("Coinbase BTC-USD WS connected")
                    backoff = 1
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                price = _coinbase_price(json.loads(msg.data))
                                if price:
                                    cache.set_btc(price, "coinbase")
                            except (ValueError, TypeError):
                                pass
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Coinbase WS error: %s - reconnect in %ds", exc, backoff)
        if stop_event.is_set():
            return
        await asyncio.sleep(min(backoff, 30))
        backoff = min(backoff * 2, 30)


def _chainlink_price(message: dict) -> Optional[float]:
    payload = message.get("payload") if isinstance(message, dict) else None
    if not isinstance(payload, dict):
        return None
    symbol = str(payload.get("symbol") or "").lower()
    if symbol and symbol != "btc/usd":
        return None
    values = payload.get("data")
    if isinstance(values, list) and values:
        latest = values[-1]
        if isinstance(latest, dict):
            values = latest.get("value")
    else:
        values = payload.get("value")
    try:
        price = float(values or 0.0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


async def _ws_chainlink(cache: PriceCache, stop_event: asyncio.Event):
    url = "wss://ws-live-data.polymarket.com"
    subscription = json.dumps({
        "action": "subscribe",
        "subscriptions": [{
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": "{\"symbol\":\"btc/usd\"}",
        }],
    })
    backoff = 1
    while not stop_event.is_set():
        try:
            connector = _make_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(url, heartbeat=20, timeout=15) as ws:
                    await ws.send_str(subscription)
                    log.info("Polymarket Chainlink WS connected")
                    backoff = 1
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                price = _chainlink_price(json.loads(msg.data))
                                if price:
                                    cache.set_source_btc(price, "chainlink")
                            except (ValueError, TypeError):
                                pass
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Chainlink WS error: %s - reconnect in %ds", exc, backoff)
        if stop_event.is_set():
            return
        await asyncio.sleep(min(backoff, 30))
        backoff = min(backoff * 2, 30)


# ─────────────────────────────────────────────────────────────────────────────
#  BINANCE WEBSOCKET FALLBACK (BTC/USDT)
# ─────────────────────────────────────────────────────────────────────────────

async def _ws_binance(cache: PriceCache, stop_event: asyncio.Event):
    """
    Binance WS: wss://stream.binance.com:9443/ws/btcusdt@ticker
    Hanya aktif jika Gate.io belum punya data > 10 detik.
    """
    import aiohttp

    url = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    backoff = 1

    while not stop_event.is_set():
        # Cek apakah Gate.io sudah aktif
        if cache.btc_age < 5 and cache.btc_source != "binance":
            await asyncio.sleep(5)
            continue

        try:
            connector = _make_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(url, heartbeat=20, timeout=15) as ws:
                    log.info("Binance WS connected (fallback)")
                    backoff = 1

                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        # Jika Gate.io sudah recovery, pause Binance
                        if cache.btc_age < 3 and cache.btc_source != "binance":
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                last = data.get("c")  # Binance ticker: "c" = last price
                                if last:
                                    cache.set_btc(float(last), "binance")
                            except (ValueError, KeyError, TypeError):
                                pass
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

        except asyncio.CancelledError:
            return
        except Exception as e:
            log.debug("Binance WS: %s — retry in %ds", e, backoff)

        if stop_event.is_set():
            return
        await asyncio.sleep(min(backoff, 30))
        backoff = min(backoff * 2, 30)


# ─────────────────────────────────────────────────────────────────────────────
#  CLOB ORDERBOOK POLLING (lightweight async, bukan WS)
# ─────────────────────────────────────────────────────────────────────────────
#  Polymarket CLOB tidak punya public WebSocket untuk orderbook.
#  Tapi kita tetap bisa polling lebih cepat dari main loop (setiap 0.3s)
#  di background task terpisah, sehingga main loop baca dari cache (0ms).

async def _clob_poller(cache: PriceCache, stop_event: asyncio.Event):
    """
    Background poller: refresh CLOB best bids dari V2 endpoint.
    Token IDs di-set via set_clob_tokens() saat market ditemukan.

    V2 CLOB: same /book endpoint URL (clob.polymarket.com/book).
    Rate-limit safe: 0.5s × 2 tokens = ~240 req/min (V2 unauthenticated
    public endpoint accept ~500 req/min). Backoff on 429/5xx.
    """
    import aiohttp

    target_rpm = max(60.0, _env_float("CLOB_POLL_TARGET_RPM", 1200.0))
    min_sweep_interval = max(0.02, _env_float("CLOB_POLL_MIN_SWEEP_INTERVAL", 0.05))
    max_concurrency = max(1, _env_int("CLOB_POLL_MAX_CONCURRENCY", 8))
    book_timeout = max(0.05, _env_float("CLOB_BOOK_TIMEOUT_SECS", 0.15))
    backoff_until = 0.0          # epoch seconds
    consecutive_errors = 0
    last_miss_log = 0.0
    session = None

    try:
        connector = _make_connector()
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=book_timeout),
        )
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _fetch_one(
            label: str, token_id: str, clob_url: str
        ) -> tuple[bool, int | str]:
            async with semaphore:
                try:
                    url = f"{clob_url}/book"
                    async with session.get(
                        url,
                        params={"token_id": token_id},
                        timeout=aiohttp.ClientTimeout(total=book_timeout),
                    ) as resp:
                        if resp.status != 200:
                            return (False, resp.status)
                        book = await resp.json()
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    return (False, "timeout")
                except Exception as exc:
                    return (False, type(exc).__name__)

                bids = book.get("bids", [])
                asks = book.get("asks", [])
                best_bid = max(
                    (float(b.get("price", 0)) for b in bids), default=0.0
                )
                best_ask = min(
                    (float(a.get("price", 0)) for a in asks
                     if float(a.get("price", 0)) > 0),
                    default=0.0,
                )
                if best_bid > 0 or best_ask > 0:
                    cache.set_clob(token_id, best_bid, best_ask)

                try:
                    bid_depth = top_depth(bids, side="bid")
                    ask_depth = top_depth(asks, side="ask")
                    cache.set_depth(token_id, bid_depth, ask_depth)
                except (ValueError, TypeError) as _e:
                    log.debug("depth parse error: %s", _e)
                return (True, 200)

        while not stop_event.is_set():
            tokens = _clob_tokens.copy()
            if not tokens:
                await asyncio.sleep(1)
                continue

            # Honor backoff window (rate limit / maintenance)
            now = time.time()
            if now < backoff_until:
                await asyncio.sleep(min(1.0, backoff_until - now))
                continue

            # Skip during V2 maintenance (sync with market.py state)
            try:
                import core.market as _mkt
                if _mkt.is_maintenance():
                    await asyncio.sleep(2)
                    continue
                clob_url = _mkt.get_active_clob_api()
            except Exception:
                import core.config as config
                clob_url = config.CLOB_API

            token_count = max(1, len(tokens))
            poll_interval = _clob_poll_interval(token_count, target_rpm, min_sweep_interval)
            results = await asyncio.gather(
                *[_fetch_one(label, token_id, clob_url) for label, token_id in tokens.items()],
                return_exceptions=True,
            )
            had_error = False
            rate_limited_status = 0
            failed_statuses = {}
            for result in results:
                if isinstance(result, BaseException):
                    had_error = True
                    failed_statuses["exception"] = failed_statuses.get("exception", 0) + 1
                    continue
                ok, status = result
                if ok:
                    consecutive_errors = 0
                    continue
                had_error = True
                failed_statuses[status] = failed_statuses.get(status, 0) + 1
                if status in (429, 502, 503, 504):
                    rate_limited_status = status
            if rate_limited_status:
                consecutive_errors += 1
                log.warning("CLOB poller HTTP %d - backing off", rate_limited_status)
                backoff_until = time.time() + min(60, 5 * consecutive_errors)
            elif failed_statuses and time.time() - last_miss_log >= 15.0:
                last_miss_log = time.time()
                log.info("CLOB poller miss statuses=%s", failed_statuses)
            if not had_error:
                consecutive_errors = max(0, consecutive_errors - 1)
            await asyncio.sleep(poll_interval)
            continue

    except asyncio.CancelledError:
        return
    finally:
        if session and not session.closed:
            await session.close()


# Token registry — diset oleh main.py saat market ditemukan
_clob_tokens: dict[str, str] = {}


def set_clob_tokens(up_token: str, down_token: str):
    """Dipanggil oleh main.py saat market aktif ditemukan."""
    global _clob_tokens
    _clob_tokens = {"up": up_token, "down": down_token}


def set_clob_token_map(tokens: dict[str, str]):
    """Register multiple CLOB tokens, keyed by a stable label."""
    global _clob_tokens
    _clob_tokens = {str(k): str(v) for k, v in (tokens or {}).items() if v}


def clear_clob_tokens():
    """Reset saat window selesai."""
    global _clob_tokens
    _clob_tokens = {}


# ─────────────────────────────────────────────────────────────────────────────
#  FEED MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class WsFeed:
    """
    Lifecycle manager untuk semua WS connections.
    Start/stop dari Bot.run().
    """
    def __init__(self):
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._cache = _cache

    async def start(self):
        """Mulai semua background WS tasks."""
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(_ws_chainlink(self._cache, self._stop)),
            asyncio.create_task(_ws_coinbase(self._cache, self._stop)),
            asyncio.create_task(_ws_gateio(self._cache, self._stop)),
            asyncio.create_task(_ws_binance(self._cache, self._stop)),
            asyncio.create_task(_clob_poller(self._cache, self._stop)),
        ]
        log.info("WS feed started (Chainlink + Coinbase + Gate.io + Binance fallback + CLOB poller)")

    async def stop(self):
        """Stop semua tasks gracefully."""
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("WS feed stopped")

    @property
    def btc_price(self) -> Optional[float]:
        """BTC price dari cache. None jika stale > 30 detik."""
        if self._cache.btc_age > 30:
            return None
        return self._cache.btc_price or None

    def clob_price(self, label: str, token_id: str) -> Optional[float]:
        """CLOB best bid dari cache. None jika stale > 10 detik."""
        if self._cache.clob_age(token_id) > 10:
            return None
        p = self._cache.get_clob(token_id)
        return p if p > 0 else None

    @property
    def is_btc_live(self) -> bool:
        return self._cache.btc_age < 10

    @property
    def is_clob_live(self) -> bool:
        return any(self._cache.clob_age(t) < 10 for t in _clob_tokens.values())
