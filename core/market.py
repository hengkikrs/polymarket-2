"""
market.py — BTC 5-min market discovery (V2-aware)
=================================================
Market slug deterministik: btc-updown-5m-{timestamp}
Timestamp = floor(now / 300) * 300

Gamma API → token IDs + prices
CLOB API  → order book best bid/ask (V2 production endpoint)
Gate.io/Binance → current BTC spot price

V2 (live since 2026-04-22): CLOB_API URL is V2 backend by default.
DNS: Uses custom resolver (8.8.8.8) to bypass ISP blocks.
"""
import asyncio
import time, json, logging, os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import aiohttp
from aiohttp import TCPConnector
import core.config as config
from core.orderbook import top_depth

log = logging.getLogger("market")


async def make_session() -> aiohttp.ClientSession:
    """Create session with custom DNS resolver + proper SSL."""
    from core.ssl_helper import make_connector
    connector = make_connector(use_custom_dns=True)

    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=10),
    )

INTERVAL = 300  # 5 minutes in seconds
INTERVAL_15M = 900
ASSET_ALIASES = {
    "HYPERLIQUID": "HYPE",
}
COINBASE_SPOT_ASSETS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
BINANCE_SPOT_ASSETS = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"}
PRICE_SOURCE_TIMEOUT_SECS = float(os.getenv("PRICE_SOURCE_TIMEOUT_SECS", "0.8"))
GAMMA_FETCH_TIMEOUT_SECS = float(os.getenv("GAMMA_FETCH_TIMEOUT_SECS", "4.0"))


def normalize_asset(asset: str = "BTC") -> str:
    """Normalize user-facing asset names to Polymarket/exchange tickers."""
    symbol = str(asset or "BTC").upper().strip()
    return ASSET_ALIASES.get(symbol, symbol)

# ─── Maintenance detection ───────────────────────────────────────────────────
_api_version: str = "v2"            # post-cutover default
_last_health_check: float = 0.0
_maintenance_until: float = 0.0     # timestamp akhir maintenance mode
HEALTH_CHECK_INTERVAL = 60.0        # cek health setiap 60 detik
MAINTENANCE_CODES = {503, 502, 429}
MAINTENANCE_MESSAGES = {"maintenance", "service unavailable", "rate limit",
                        "temporarily unavailable", "upgrading"}


async def check_api_health(session: aiohttp.ClientSession) -> dict:
    """
    Probe API health.
    V2: production URL (clob.polymarket.com) sudah V2 sejak 2026-04-22.
    Tidak perlu probe URL terpisah — cukup ping /time + cek HTTP code.
    """
    global _api_version, _last_health_check, _maintenance_until

    now = time.time()
    if now - _last_health_check < HEALTH_CHECK_INTERVAL:
        return {
            "healthy": _maintenance_until < now,
            "version": _api_version,
            "maintenance": _maintenance_until >= now,
        }

    _last_health_check = now
    result = {"healthy": True, "version": _api_version,
              "maintenance": False, "message": ""}

    try:
        url = f"{config.CLOB_API}/time"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                _maintenance_until = 0
                _api_version = "v2"
                result["healthy"] = True
                result["version"] = "v2"
            elif resp.status in MAINTENANCE_CODES:
                _maintenance_until = now + 120  # assume 2 min maintenance
                result["healthy"] = False
                result["maintenance"] = True
                result["message"] = f"API maintenance (HTTP {resp.status})"
                log.warning("API maintenance detected — pausing for 120s")
            else:
                try:
                    body = await resp.text()
                    body_lower = body.lower()
                    if any(m in body_lower for m in MAINTENANCE_MESSAGES):
                        _maintenance_until = now + 120
                        result["healthy"] = False
                        result["maintenance"] = True
                        result["message"] = f"Maintenance: {body[:100]}"
                except Exception:
                    pass
    except Exception as e:
        result["healthy"] = False
        result["message"] = f"Health check failed: {e}"

    return result


def get_active_clob_api() -> str:
    """Return CLOB API URL. V2 production sejak 2026-04-22."""
    return config.CLOB_API


def is_maintenance() -> bool:
    """True jika API sedang maintenance."""
    return time.time() < _maintenance_until


@dataclass
class BTCMarket:
    """Active BTC 5-min market.

    Pricing: up_price/down_price = best BID (untuk SELL referensi).
             up_ask/down_ask    = best ASK (untuk BUY referensi).
             Real spread = ask - bid per token.
    """
    slug: str
    window_ts: int          # window open timestamp
    close_ts: int           # window close timestamp
    condition_id: str
    up_token: str
    down_token: str
    up_price: float         # best bid for UP
    down_price: float       # best bid for DOWN
    question: str
    end_date: str
    asset: str = "BTC"
    target_price: float = 0.0  # BTC target dari Polymarket question (oracle price)
    active: bool = True
    closed: bool = False
    archived: bool = False
    accepting_orders: bool = True
    book_ts: float = 0.0
    # Best ask per token (dari order book asks). Default 0 = unknown.
    up_ask: float   = 0.0
    down_ask: float = 0.0
    # EDGE UPGRADE: top-5 depth per side, list of (price, size) tuples.
    # Used by edge_signals.compute_imbalance() and microprice().
    # Empty list = no depth data (cache miss / book empty).
    up_bid_depth:   list = field(default_factory=list)   # [(price, size), ...]
    up_ask_depth:   list = field(default_factory=list)
    down_bid_depth: list = field(default_factory=list)
    down_ask_depth: list = field(default_factory=list)

    @property
    def seconds_left(self) -> float:
        return max(0, self.close_ts - time.time())

    @property
    def leading_outcome(self) -> str:
        return "UP" if self.up_price > self.down_price else "DOWN"

    @property
    def leading_price(self) -> float:
        return max(self.up_price, self.down_price)

    @property
    def leading_token(self) -> str:
        return self.up_token if self.up_price > self.down_price else self.down_token

    @property
    def up_spread(self) -> float:
        """Spread UP token: best_ask - best_bid. 0 = unknown (ask not fetched)."""
        if self.up_ask > 0 and self.up_price > 0 and self.up_ask >= self.up_price:
            return round(self.up_ask - self.up_price, 4)
        return 0.0

    @property
    def down_spread(self) -> float:
        """Spread DOWN token. 0 = unknown."""
        if self.down_ask > 0 and self.down_price > 0 and self.down_ask >= self.down_price:
            return round(self.down_ask - self.down_price, 4)
        return 0.0

    def spread_for(self, outcome: str) -> float:
        """Get spread untuk outcome ('UP' atau 'DOWN'). 0 = unknown."""
        return self.up_spread if outcome == "UP" else self.down_spread


def _parse_target_from_question(question: str) -> float:
    """
    Extract BTC target price dari Polymarket question text.
    Format umum: "Will Bitcoin be above $77,032.54 at 3:30 AM ET on April 29?"
    Atau: "Bitcoin Up or Down ... target $77,032.54"
    Return 0.0 jika gagal parse.
    """
    import re
    if not question:
        return 0.0
    # Match $XX,XXX.XX or $XXXXX.XX or $XX,XXX
    m = re.search(r'\$\s*([\d,]+(?:\.\d+)?)', question)
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            return 0.0
    return 0.0


def _parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _bool_field(payload: dict, names: tuple[str, ...], default: bool) -> bool:
    for name in names:
        if name not in payload:
            continue
        value = payload.get(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def _parse_ts(value) -> float:
    """Parse Gamma timestamps. Return 0 when the field is absent/unknown."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _normalize_outcome_name(value: str) -> str:
    name = str(value or "").strip().upper()
    if name in ("UP", "YES"):
        return "UP"
    if name in ("DOWN", "NO"):
        return "DOWN"
    return name


def _resolved_outcome_from_market_payload(
    mkt: dict,
    *,
    allow_implied: bool = True,
) -> Optional[str]:
    """Return official resolved UP/DOWN from Gamma market payload if final."""
    if not isinstance(mkt, dict):
        return None
    status = str(mkt.get("umaResolutionStatus") or "").lower()
    is_final = bool(mkt.get("closed")) or status == "resolved"
    outcomes = _parse_json_list(mkt.get("outcomes", []))
    prices = _parse_json_list(mkt.get("outcomePrices", []))
    if len(outcomes) < 2 or len(prices) < 2:
        return None

    numeric_prices = []
    for p in prices:
        try:
            numeric_prices.append(float(p))
        except (TypeError, ValueError):
            numeric_prices.append(0.0)
    best_idx = max(range(len(numeric_prices)), key=lambda i: numeric_prices[i])
    best_px = numeric_prices[best_idx]
    other = [p for i, p in enumerate(numeric_prices) if i != best_idx]

    implied_min = float(os.getenv("GAMMA_IMPLIED_RESOLVE_MIN", "0.97"))
    implied_other_max = float(os.getenv("GAMMA_IMPLIED_RESOLVE_OTHER_MAX", "0.03"))
    implied_final = (
        allow_implied
        and best_px >= implied_min
        and all(p <= implied_other_max for p in other)
    )

    if not is_final and not implied_final:
        return None
    return _normalize_outcome_name(outcomes[best_idx])


def _resolution_from_event_payload(event: dict) -> Optional[dict]:
    """Return Chainlink finalPrice/priceToBeat resolution from a Gamma event."""
    if not isinstance(event, dict):
        return None
    metadata = event.get("eventMetadata") or {}
    if not isinstance(metadata, dict):
        return None
    try:
        final_price = float(metadata.get("finalPrice") or 0.0)
        price_to_beat = float(metadata.get("priceToBeat") or 0.0)
    except (TypeError, ValueError):
        return None
    if final_price <= 0 or price_to_beat <= 0:
        return None
    actual = "UP" if final_price >= price_to_beat else "DOWN"
    result = {
        "actual": actual,
        "final_price": final_price,
        "price_to_beat": price_to_beat,
        "source": "gamma_event_metadata",
    }
    markets = event.get("markets", [])
    market_payload = markets[0] if markets and isinstance(markets[0], dict) else {}
    outcomes = _parse_json_list(market_payload.get("outcomes", []))
    token_ids = _parse_json_list(market_payload.get("clobTokenIds", []))
    for index, outcome in enumerate(outcomes):
        if _normalize_outcome_name(outcome) == actual and index < len(token_ids):
            result["winner_token"] = str(token_ids[index])
            break
    return result


def _price_to_beat_from_event_payload(event: dict) -> float:
    if not isinstance(event, dict):
        return 0.0
    metadata = event.get("eventMetadata") or {}
    if not isinstance(metadata, dict):
        return 0.0
    try:
        price_to_beat = float(metadata.get("priceToBeat") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return price_to_beat if price_to_beat > 0 else 0.0


def market_interval_label(interval_secs: int = INTERVAL) -> str:
    interval = int(interval_secs or INTERVAL)
    return "15m" if interval == INTERVAL_15M else "5m"


def current_window_ts(interval_secs: int = INTERVAL) -> int:
    """Calculate the current market window start timestamp."""
    now = int(time.time())
    interval = int(interval_secs or INTERVAL)
    return now - (now % interval)


def current_slug(asset: str = "BTC", interval_secs: int = INTERVAL) -> str:
    """Generate the slug for the current crypto market."""
    return (
        f"{normalize_asset(asset).lower()}-updown-"
        f"{market_interval_label(interval_secs)}-{current_window_ts(interval_secs)}"
    )


async def fetch_market(session: aiohttp.ClientSession,
                       asset: str = "BTC",
                       interval_secs: int = INTERVAL) -> Optional[BTCMarket]:
    """
    Find the current BTC 5-min market via Gamma API.
    Returns None if market not found, maintenance, or not accepting orders.
    """
    if is_maintenance():
        log.warning("API maintenance — skipping fetch_market")
        return None

    interval = int(interval_secs or INTERVAL)
    wts = current_window_ts(interval)
    asset = normalize_asset(asset)
    slug = f"{asset.lower()}-updown-{market_interval_label(interval)}-{wts}"

    url = f"{config.GAMMA_API}/events/slug/{slug}"

    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=GAMMA_FETCH_TIMEOUT_SECS),
        ) as resp:
            if resp.status in MAINTENANCE_CODES:
                global _maintenance_until
                _maintenance_until = time.time() + 120
                log.warning("Gamma API %d (maintenance) for %s", resp.status, slug)
                return None
            if resp.status != 200:
                log.debug("Gamma API %d for %s", resp.status, slug)
                return None
            data = await resp.json()

        if not data:
            log.debug("No event for slug %s", slug)
            return None

        event = data[0] if isinstance(data, list) else data
        markets = event.get("markets", [])
        if not markets:
            log.debug("No markets in event %s", slug)
            return None

        mkt = markets[0]
        active = _bool_field(mkt, ("active", "isActive"), True)
        closed = _bool_field(mkt, ("closed", "isClosed"), False)
        archived = _bool_field(mkt, ("archived", "isArchived"), False)
        accepting_orders = _bool_field(
            mkt,
            ("acceptingOrders", "accepting_orders", "enableOrderBook", "orderBookEnabled"),
            active and not closed and not archived,
        )
        end_date = mkt.get("endDate", mkt.get("end_date_iso", ""))
        end_ts = _parse_ts(end_date)
        if closed or archived or not active or not accepting_orders:
            log.info(
                "Market %s rejected status active=%s closed=%s archived=%s accepting=%s",
                slug, active, closed, archived, accepting_orders,
            )
            return None
        if end_ts and end_ts <= time.time():
            log.info("Market %s rejected expired endDate=%s", slug, end_date)
            return None

        # Parse token IDs
        token_ids = mkt.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)

        outcomes = mkt.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        # Parse prices
        prices = mkt.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)

        if len(token_ids) < 2 or len(outcomes) < 2:
            log.debug("Incomplete market data for %s", slug)
            return None

        # Map UP/DOWN tokens
        up_idx = 0
        down_idx = 1
        for i, name in enumerate(outcomes):
            if name.lower() in ("up", "yes"):
                up_idx = i
            elif name.lower() in ("down", "no"):
                down_idx = i

        up_price = float(prices[up_idx]) if len(prices) > up_idx else 0.5
        down_price = float(prices[down_idx]) if len(prices) > down_idx else 0.5

        question_text = mkt.get("question", slug)
        target_price = _price_to_beat_from_event_payload(event)
        if target_price <= 0:
            target_price = _parse_target_from_question(question_text)

        return BTCMarket(
            slug=slug,
            asset=asset,
            window_ts=wts,
            close_ts=wts + interval,
            condition_id=mkt.get("conditionId", mkt.get("condition_id", "")),
            up_token=str(token_ids[up_idx]),
            down_token=str(token_ids[down_idx]),
            up_price=up_price,
            down_price=down_price,
            question=question_text,
            end_date=end_date,
            target_price=target_price,
            active=active,
            closed=closed,
            archived=archived,
            accepting_orders=accepting_orders,
            book_ts=0.0,
        )

    except Exception as e:
        log.warning(
            "fetch_market %s error: %s: %r",
            asset,
            type(e).__name__,
            e,
        )
        return None


async def fetch_markets(session: aiohttp.ClientSession,
                        assets: list[str]) -> dict[str, BTCMarket]:
    """Fetch current 5m markets for multiple assets."""
    import asyncio as _asyncio
    norm_assets = list(dict.fromkeys(normalize_asset(a) for a in assets if str(a or "").strip()))
    tasks = [fetch_market(session, a) for a in norm_assets]
    results = await _asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, BTCMarket] = {}
    for asset, result in zip(norm_assets, results):
        if isinstance(result, BTCMarket):
            out[result.asset.upper()] = result
        elif isinstance(result, Exception):
            log.debug("fetch_markets %s error: %s", asset, result)
    return out


async def fetch_resolved_outcome(session: aiohttp.ClientSession,
                                 slug: str,
                                 *,
                                 allow_implied: bool = True) -> Optional[str]:
    """Fetch official resolved UP/DOWN for a market slug from Gamma API."""
    if not slug:
        return None
    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        event = data[0] if isinstance(data, list) and data else data
        if not isinstance(event, dict):
            return None
        resolution = _resolution_from_event_payload(event)
        if resolution:
            return resolution["actual"]
        markets = event.get("markets", [])
        if not markets:
            return None
        return _resolved_outcome_from_market_payload(
            markets[0],
            allow_implied=allow_implied,
        )
    except Exception as e:
        log.debug("fetch_resolved_outcome %s error: %s", slug, e)
        return None


async def fetch_resolution(session: aiohttp.ClientSession,
                           slug: str,
                           *,
                           allow_implied: bool = True) -> Optional[dict]:
    """Fetch resolved UP/DOWN plus Chainlink final/target prices when available."""
    if not slug:
        return None
    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        event = data[0] if isinstance(data, list) and data else data
        if not isinstance(event, dict):
            return None
        resolution = _resolution_from_event_payload(event)
        if resolution:
            return resolution
        markets = event.get("markets", [])
        if not markets:
            return None
        actual = _resolved_outcome_from_market_payload(
            markets[0],
            allow_implied=allow_implied,
        )
        if not actual:
            return None
        return {
            "actual": actual,
            "final_price": 0.0,
            "price_to_beat": 0.0,
            "source": "gamma_outcome_prices",
        }
    except Exception as e:
        log.debug("fetch_resolution %s error: %s", slug, e)
        return None


async def fetch_recent_btc_resolutions(
    session: aiohttp.ClientSession,
    *,
    hours: int = 3,
    now: float | None = None,
) -> list[dict]:
    """Fetch completed BTC 5m target/final values directly from Gamma."""
    now = float(now or time.time())
    current_window = int(now) - (int(now) % INTERVAL)
    window_count = max(1, int(hours * 3600 / INTERVAL))
    window_timestamps = [
        current_window - INTERVAL * offset
        for offset in range(1, window_count + 1)
    ]

    async def _fetch(window_ts: int) -> Optional[dict]:
        slug = f"btc-updown-5m-{window_ts}"
        resolution = await fetch_resolution(session, slug, allow_implied=False)
        if not resolution:
            return None
        final_price = float(resolution.get("final_price") or 0.0)
        price_to_beat = float(resolution.get("price_to_beat") or 0.0)
        if final_price <= 0 or price_to_beat <= 0:
            return None
        return {
            "window_ts": window_ts,
            "market_slug": slug,
            "final_price": final_price,
            "price_to_beat": price_to_beat,
            "winner_token": str(resolution.get("winner_token") or ""),
            "source": str(resolution.get("source") or "gamma_event_metadata"),
        }

    rows = await asyncio.gather(*(_fetch(window_ts) for window_ts in window_timestamps))
    return sorted((row for row in rows if row), key=lambda row: row["window_ts"])


async def fetch_recent_clob_saturation(
    session: aiohttp.ClientSession,
    resolutions: list[dict],
    *,
    minutes: int = 30,
    now: float | None = None,
    saturation_price: float = 0.94,
    min_completed_windows: int = 5,
) -> dict:
    """Estimate official intrawindow saturation timing from CLOB price history."""
    now = float(now or time.time())
    cutoff = now - max(1, int(minutes)) * 60
    min_completed_windows = max(1, int(min_completed_windows or 1))
    recent = [
        row for row in resolutions
        if cutoff <= int(row.get("window_ts") or 0) + INTERVAL <= now
        and str(row.get("winner_token") or "")
    ]
    candidate_rows = sorted(
        (
            row for row in resolutions
            if int(row.get("window_ts") or 0) + INTERVAL <= now
            and str(row.get("winner_token") or "")
        ),
        key=lambda row: int(row.get("window_ts") or 0),
    )
    if len(recent) < min_completed_windows:
        recent = sorted(
            (
                row for row in resolutions
                if int(row.get("window_ts") or 0) + INTERVAL <= now
                and str(row.get("winner_token") or "")
            ),
            key=lambda row: int(row.get("window_ts") or 0),
            reverse=True,
        )[:min_completed_windows]
        recent = sorted(recent, key=lambda row: int(row.get("window_ts") or 0))

    async def _fetch(row: dict) -> dict:
        window_ts = int(row["window_ts"])
        try:
            async with session.get(
                f"{config.CLOB_API}/prices-history",
                params={
                    "market": str(row["winner_token"]),
                    "startTs": window_ts,
                    "endTs": window_ts + INTERVAL,
                    "fidelity": 1,
                },
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as resp:
                if resp.status != 200:
                    return {"window_ts": window_ts}
                payload = await resp.json()
        except Exception:
            return {"window_ts": window_ts}
        history = sorted(
            (
                point for point in payload.get("history", [])
                if float(point.get("p") or 0.0) > 0
                and window_ts <= int(point.get("t") or 0) <= window_ts + INTERVAL
            ),
            key=lambda point: int(point.get("t") or 0),
        )
        saturation_hit = next(
            (
                window_ts + INTERVAL - int(point["t"])
                for point in history
                if float(point["p"]) >= saturation_price
            ),
            None,
        )
        locked_hit = next(
            (
                window_ts + INTERVAL - int(point["t"])
                for point in history
                if float(point["p"]) >= 0.999
            ),
            None,
        )
        return {
            "window_ts": window_ts,
            "saturation_secs": saturation_hit,
            "locked_secs": locked_hit,
        }

    samples = await asyncio.gather(*(_fetch(row) for row in candidate_rows))
    saturation_seconds = [
        float(row["saturation_secs"])
        for row in samples if row.get("saturation_secs") is not None
    ][-min_completed_windows:]
    locked_seconds = [
        float(row["locked_secs"])
        for row in samples if row.get("locked_secs") is not None
    ][-min_completed_windows:]
    return {
        "saturation_source": "gamma_tokens+clob_price_history",
        "saturation_granularity_secs": 60,
        "saturation_avg_secs_30m": (
            round(sum(saturation_seconds) / len(saturation_seconds), 1)
            if saturation_seconds else None
        ),
        "saturation_samples_30m": len(saturation_seconds),
        "locked_avg_secs_30m": (
            round(sum(locked_seconds) / len(locked_seconds), 1)
            if locked_seconds else None
        ),
        "locked_samples_30m": len(locked_seconds),
        "completed_windows_30m": max(len(recent), len(saturation_seconds), len(locked_seconds)),
    }


async def refresh_prices(session: aiohttp.ClientSession, mkt: BTCMarket) -> BTCMarket:
    """Refresh token prices (best_bid + best_ask). WS cache first, REST fallback.

    Note: REST hanya di-fallback untuk token yang TIDAK ter-cache (atau stale).
    Bug pre-existing yang sudah di-fix: dulu jika SALAH SATU token cached,
    return early — token lain tidak ter-update.

    UPGRADE A: now fetches best_ask too → real spread = ask - bid populated.
    Cache stores (bid, ask) tuple per token.
    """
    if is_maintenance():
        return mkt

    cached = {"up": False, "down": False}
    refreshed_any = False

    # 1. WS cache (0ms)
    try:
        from core.ws_feed import get_cache
        cache = get_cache()
        cache_max_age = max(0.05, float(os.getenv("CLOB_CACHE_MAX_AGE_SECS", "0.25")))
        for label, token_id in [("up", mkt.up_token), ("down", mkt.down_token)]:
            if cache.clob_age(token_id) < cache_max_age:
                bid_ask = cache.get_clob_full(token_id)
                if bid_ask and (bid_ask[0] > 0 or bid_ask[1] > 0):
                    bid, ask = bid_ask
                    if label == "up":
                        if bid > 0:
                            mkt.up_price = bid
                        if ask > 0:
                            mkt.up_ask = ask
                    else:
                        if bid > 0:
                            mkt.down_price = bid
                        if ask > 0:
                            mkt.down_ask = ask
                    cached[label] = True
                    refreshed_any = True
                # Populate depth (separate from price cache hit logic)
                depth = cache.get_depth(token_id)
                if label == "up":
                    mkt.up_bid_depth = depth.get("bids", [])
                    mkt.up_ask_depth = depth.get("asks", [])
                else:
                    mkt.down_bid_depth = depth.get("bids", [])
                    mkt.down_ask_depth = depth.get("asks", [])
    except ImportError:
        pass

    # 2. REST fallback HANYA untuk yang belum cached
    if cached["up"] and cached["down"]:
        if refreshed_any:
            mkt.book_ts = time.time()
        return mkt

    clob_url = get_active_clob_api()
    try:
        book_timeout = max(0.05, float(os.getenv("CLOB_BOOK_TIMEOUT_SECS", "0.75")))
    except ValueError:
        book_timeout = 0.75
    for label, token_id in [("up", mkt.up_token), ("down", mkt.down_token)]:
        if cached[label]:
            continue
        try:
            url = f"{clob_url}/book"
            async with session.get(
                url,
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=book_timeout),
            ) as resp:
                if resp.status in MAINTENANCE_CODES:
                    global _maintenance_until
                    _maintenance_until = time.time() + 120
                    log.warning("CLOB API maintenance during price refresh")
                    return mkt
                if resp.status != 200:
                    continue
                book = await resp.json()

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = max((float(b.get("price", 0)) for b in bids), default=0.0)
            # Best ask = LOWEST sell offer (asks sorted ascending in V2)
            best_ask = min((float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0),
                           default=0.0)

            if label == "up":
                if best_bid > 0:
                    mkt.up_price = best_bid
                if best_ask > 0:
                    mkt.up_ask = best_ask
            else:
                if best_bid > 0:
                    mkt.down_price = best_bid
                if best_ask > 0:
                    mkt.down_ask = best_ask

            # EDGE: populate top-5 depth from REST too
            try:
                _bd = top_depth(bids, side="bid")
                _ad = top_depth(asks, side="ask")
                if label == "up":
                    mkt.up_bid_depth = _bd
                    mkt.up_ask_depth = _ad
                else:
                    mkt.down_bid_depth = _bd
                    mkt.down_ask_depth = _ad
                refreshed_any = True
                # Also populate cache for next tick
                try:
                    from core.ws_feed import get_cache
                    _cache = get_cache()
                    if best_bid > 0 or best_ask > 0:
                        _cache.set_clob(token_id, best_bid, best_ask)
                    _cache.set_depth(token_id, _bd, _ad)
                except ImportError:
                    pass
            except (ValueError, TypeError):
                pass
        except Exception as e:
            log.debug("Price refresh error (%s): %s", label, e)

    if refreshed_any:
        mkt.book_ts = time.time()
    return mkt


async def get_crypto_price(session: aiohttp.ClientSession,
                           asset: str = "BTC") -> Optional[float]:
    """Get current USD spot for BTC/ETH/SOL/XRP with Coinbase primary."""
    asset = normalize_asset(asset)
    coinbase_pair = f"{asset}-USD"
    usdt_pair = f"{asset}_USDT"
    binance_symbol = f"{asset}USDT"

    if asset in COINBASE_SPOT_ASSETS:
        try:
            url = f"https://api.coinbase.com/v2/prices/{coinbase_pair}/spot"
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=PRICE_SOURCE_TIMEOUT_SECS),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = float(data["data"]["amount"])
                    if price > 0:
                        return price
        except Exception:
            pass

    try:
        url = "https://api.gateio.ws/api/v4/spot/tickers"
        async with session.get(
            url,
            params={"currency_pair": usdt_pair},
            timeout=aiohttp.ClientTimeout(total=PRICE_SOURCE_TIMEOUT_SECS),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    return float(data[0]["last"])
    except Exception:
        pass

    if asset in BINANCE_SPOT_ASSETS:
        try:
            url = "https://api.binance.com/api/v3/ticker/price"
            async with session.get(
                url,
                params={"symbol": binance_symbol},
                timeout=aiohttp.ClientTimeout(total=PRICE_SOURCE_TIMEOUT_SECS),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["price"])
        except Exception:
            pass

    return None


async def get_crypto_close_price(session: aiohttp.ClientSession,
                                 asset: str = "BTC") -> Optional[float]:
    """Close-price helper for C multi-market settlement."""
    return await get_crypto_price(session, asset)


async def get_btc_price(session: aiohttp.ClientSession) -> Optional[float]:
    """Get BTC price. Source priority: Coinbase BTC-USD (closest to Polymarket
    Chainlink oracle) > WS cache (Gate.io/Binance) > Gate.io REST > Binance REST.

    CRITICAL: Polymarket BTC 5-min markets resolve via Chainlink BTC/USD oracle
    (aggregated, but Coinbase BTC-USD is largest weight). Gate.io and Binance
    pakai BTC/USDT yang bisa diverge $5-50 dari BTC/USD di moments tertentu —
    cukup untuk flip outcome di window dengan delta tipis.

    Untuk akurasi maksimal di LIVE mode, set USE_CHAINLINK_WS=true dan provide
    POLYMARKET_RTDS credentials (subscribe ke crypto_prices_chainlink topic).
    """
    # 1. Coinbase BTC-USD spot (PRIMARY — closest to Chainlink oracle source)
    # Coinbase adalah salah satu data feed Chainlink BTC/USD aggregator,
    # dan harga BTC-USD (bukan BTC-USDT) match dengan oracle resolusi Polymarket.
    def _cache_price(price: float, source: str) -> None:
        try:
            from core.ws_feed import get_cache
            get_cache().set_btc(float(price), source)
        except ImportError:
            pass

    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = float(data["data"]["amount"])
                if price > 0:
                    _cache_price(price, "coinbase")
                    return price
    except Exception:
        pass

    # 2. WS cache (Gate.io/Binance — fast tapi BTC/USDT, ada divergence risk)
    try:
        from core.ws_feed import get_cache
        cache = get_cache()
        if cache.btc_age < 15:
            p = cache.btc_price
            if p > 0:
                return p
    except ImportError:
        pass

    # 3. Gate.io REST (BTC/USDT — fallback)
    try:
        url = "https://api.gateio.ws/api/v4/spot/tickers"
        async with session.get(url, params={"currency_pair": "BTC_USDT"}) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    price = float(data[0]["last"])
                    if price > 0:
                        _cache_price(price, "gateio-rest")
                    return price
    except Exception:
        pass

    # 4. Binance REST (BTC/USDT — fallback)
    try:
        url = "https://api.binance.com/api/v3/ticker/price"
        async with session.get(url, params={"symbol": "BTCUSDT"}) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = float(data["price"])
                if price > 0:
                    _cache_price(price, "binance-rest")
                return price
    except Exception:
        pass

    return None


async def get_btc_price_probe(session: aiohttp.ClientSession) -> dict:
    """Fetch BTC from the three spot sources used by runtime guards.

    This is an audit/consensus helper. Callers should decide whether a wide
    source spread is acceptable for their strategy.
    """
    import asyncio as _asyncio

    async def _coinbase():
        try:
            url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d["data"]["amount"])
        except Exception:
            return None

    async def _gateio():
        try:
            url = "https://api.gateio.ws/api/v4/spot/tickers"
            async with session.get(url, params={"currency_pair": "BTC_USDT"},
                                   timeout=aiohttp.ClientTimeout(total=2.0)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d:
                        return float(d[0]["last"])
        except Exception:
            return None

    async def _binance():
        try:
            url = "https://api.binance.com/api/v3/ticker/price"
            async with session.get(url, params={"symbol": "BTCUSDT"},
                                   timeout=aiohttp.ClientTimeout(total=2.0)) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d["price"])
        except Exception:
            return None

    coinbase, gateio, binance = await _asyncio.gather(
        _coinbase(), _gateio(), _binance(), return_exceptions=False)
    prices = {"coinbase": coinbase, "gateio": gateio, "binance": binance}
    valid = {k: float(v) for k, v in prices.items() if v and float(v) > 0}
    spread = (max(valid.values()) - min(valid.values())) if len(valid) >= 2 else 0.0
    return {
        "prices": prices,
        "valid_prices": valid,
        "spread": round(float(spread or 0.0), 4),
        "source_count": len(valid),
    }


async def get_btc_close_price(session: aiohttp.ClientSession) -> Optional[float]:
    """Get BTC price untuk RESOLUSI (close_ts) dengan multi-source consensus.

    Berbeda dari get_btc_price() yang prefer speed, fungsi ini prefer ACCURACY:
    fetch dari Coinbase + Gate.io + Binance bersamaan, return MEDIAN price.
    Mitigasi divergence: kalau 1 exchange anomali, median masih representatif.

    KRITIS: dipakai di _resolve_pending untuk hold-to-close P&L. Mismatch
    sumber price = bot resolve berbeda dari Polymarket Chainlink oracle =
    balance.json drift dari actual cash di LIVE mode.
    """
    import asyncio as _asyncio

    async def _coinbase():
        try:
            url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d["data"]["amount"])
        except Exception:
            return None

    async def _gateio():
        try:
            url = "https://api.gateio.ws/api/v4/spot/tickers"
            async with session.get(url, params={"currency_pair": "BTC_USDT"},
                                   timeout=aiohttp.ClientTimeout(total=3.0)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d:
                        return float(d[0]["last"])
        except Exception:
            return None

    async def _binance():
        try:
            url = "https://api.binance.com/api/v3/ticker/price"
            async with session.get(url, params={"symbol": "BTCUSDT"},
                                   timeout=aiohttp.ClientTimeout(total=3.0)) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d["price"])
        except Exception:
            return None

    results = await _asyncio.gather(_coinbase(), _gateio(), _binance(),
                                    return_exceptions=False)
    valid = [p for p in results if p and p > 0]
    if not valid:
        return None

    # Coinbase prioritas (closest to Chainlink oracle)
    coinbase_price = results[0]
    if coinbase_price and coinbase_price > 0:
        # Check spread vs lainnya — kalau divergence > $50, log warning
        if len(valid) >= 2:
            spread = max(valid) - min(valid)
            if spread > 50:
                log.warning(
                    "BTC source spread=$%.2f at resolve (Coinbase=%.2f Gate=%s Bin=%s) — "
                    "possible oracle divergence risk",
                    spread, coinbase_price,
                    f"{results[1]:.2f}" if results[1] else "N/A",
                    f"{results[2]:.2f}" if results[2] else "N/A",
                )
        return coinbase_price

    # Fallback: median dari source yang valid
    valid.sort()
    n = len(valid)
    return valid[n // 2] if n % 2 == 1 else (valid[n//2 - 1] + valid[n//2]) / 2
