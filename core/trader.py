"""
trader.py — Order execution via py-clob-client-v2 (CTF Exchange V2)
====================================================================
V2 migration (live since 2026-04-22). V1 SDK is dead.

Live mode:
  - BUY  → Limit FAK (Fill-and-Kill / IOC) at price + 1 tick — partial OK
           Atomic: fully filled or rejected. No fill-polling needed.
  - SELL → Limit GTC at max(tick, price - 1 tick)
           Polling fill status. Partial fill accepted, remainder cancelled.

Mock mode: simulasi langsung dengan opsi live-cost simulation
(spread, slippage, fee, kill rate, partial fill) — diatur via config.MOCK_*.

Key V2 changes:
  - Package : py_clob_client_v2 (was py_clob_client)
  - API key : create_or_derive_api_key()  (was create_or_derive_api_creds)
  - Cancel  : cancel_order(OrderPayload(orderID=...))  (was cancel(order_id))
  - Side    : Side.BUY / Side.SELL  (enum)
  - Options : PartialCreateOrderOptions(tick_size="0.01")  (required)
  - Fees    : protocol-handled at match time (no feeRateBps in order)
  - Collateral: pUSD (was USDC.e) — wrap manual untuk API-only traders
  - REST fallback: REMOVED — V2 wajib EIP-712 signed order, hanya SDK.
"""
import asyncio, math, time, random, logging, os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional
import aiohttp
import core.config as config

log = logging.getLogger("trader")


# ─────────────────────────────────────────────────────────────────────────────
#  V2 SDK IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
try:
    from py_clob_client_v2 import (
        ClobClient,
        ApiCreds,
        OrderArgs,
        MarketOrderArgs,
        OrderType,
        OrderPayload,
        Side,
        PartialCreateOrderOptions,
        BalanceAllowanceParams,
        AssetType,
    )
    HAS_SDK = True
    BUY, SELL = Side.BUY, Side.SELL
    log.info("py-clob-client-v2 loaded OK (CTF Exchange V2)")
except ImportError:
    HAS_SDK = False
    BUY, SELL = "BUY", "SELL"
    log.error(
        "py-clob-client-v2 TIDAK terinstall! "
        "V1 sudah mati per 2026-04-22. Run: pip install py-clob-client-v2"
    )


# ─── Order fill verification config (untuk SELL/GTC saja) ────────────────────
FILL_POLL_INTERVAL  = 0.25     # poll setiap 0.25s
FILL_TIMEOUT_SELL   = 12.0     # max tunggu fill SELL GTC
FOK_REQUEST_TIMEOUT = 5.0      # FOK BUY adalah atomic — tidak ada polling
# Slippage: 0.02 = warn, 0.04 (2x) = hard abort
MAX_SLIPPAGE        = 0.02
LIVE_MIN_ORDER_SHARES = max(1.0, float(os.getenv("LIVE_MIN_ORDER_SHARES", "1.0")))


@dataclass
class TradeResult:
    success: bool
    order_id: str  = ""
    price: float   = 0.0
    size: float    = 0.0
    error: str     = ""
    mock: bool     = False
    fill_status: str = ""       # "full","partial","unfilled","cancelled","fak_killed","fak_partial_unsellable"
    size_matched: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  MOCK LIVE COST SIMULATION
#  Apply spread / slippage / V2 fee / kill rate untuk realistic mock P&L.
#  Formula V2 fee: notional × fee_rate × p × (1 - p)
# ─────────────────────────────────────────────────────────────────────────────

_mock_rng: Optional[random.Random] = None


def _get_mock_rng() -> random.Random:
    """Return seeded RNG (deterministic) jika MOCK_RANDOM_SEED > 0, else system."""
    global _mock_rng
    if _mock_rng is None:
        seed = config.MOCK_RANDOM_SEED
        _mock_rng = random.Random(seed) if seed > 0 else random.Random()
    return _mock_rng


def _v2_fee_pct(price: float) -> float:
    """V2 fee fraction: r × p × (1 - p). Bounded [0, r/4] (max at p=0.5)."""
    p = max(0.0, min(1.0, price))
    return config.MOCK_FEE_RATE * p * (1 - p)


def _simulate_live_buy(price: float, amount: float, tick: float = 0.01,
                       strict_price: bool = False) -> dict:
    """
    Simulate live V2 BUY costs untuk MOCK_LIVE_SIM mode.
    BUY pays: half-spread + slippage. Fee deducted from amount.
    Returns {"killed": bool, "fill_price": float, "size": float}
    """
    if not config.MOCK_LIVE_SIM:
        return {
            "killed": False,
            "fill_price": price,
            "size": round((amount / price) * config.FEE_MULTIPLIER, 2),
        }

    rng = _get_mock_rng()

    # FOK kill: simulate insufficient liquidity at price+1tick
    if rng.random() < config.MOCK_FOK_KILL_PCT:
        return {"killed": True, "fill_price": 0.0, "size": 0.0}

    half_spread = (config.MOCK_SPREAD_TICKS * tick) / 2
    slip = rng.uniform(0, config.MOCK_SLIPPAGE_TICKS * tick)
    fill_price = round(min(round(1 - tick, 4), price + half_spread + slip), 4)
    if strict_price:
        # A strict BUY is a hard limit order. The mock live-cost
        # model may reduce fill size via fees, but it must not cross the cap.
        fill_price = min(fill_price, price)

    fee_pct = _v2_fee_pct(fill_price)
    size = round((amount * (1 - fee_pct)) / fill_price, 2)
    return {"killed": False, "fill_price": fill_price, "size": size}


def _simulate_live_sell(price: float, shares: float, tick: float = 0.01) -> dict:
    """
    Simulate live V2 SELL costs untuk MOCK_LIVE_SIM mode.
    SELL receives: mid - half-spread - slippage. Fee deducted from proceeds.
    Returns {"fill_price": float, "size_filled": float, "fill_status": str}
    """
    if not config.MOCK_LIVE_SIM:
        return {"fill_price": price, "size_filled": shares, "fill_status": "full"}

    rng = _get_mock_rng()

    half_spread = (config.MOCK_SPREAD_TICKS * tick) / 2
    slip = rng.uniform(0, config.MOCK_SLIPPAGE_TICKS * tick)
    raw_price = round(max(tick, price - half_spread - slip), 4)

    # V2 fee baked into effective received price
    fee_pct = _v2_fee_pct(raw_price)
    fill_price = round(raw_price * (1 - fee_pct), 4)

    # Partial fill simulation
    if rng.random() < config.MOCK_PARTIAL_PCT:
        ratio = rng.uniform(0.5, 0.95)
        size_filled = round(shares * ratio, 2)
        return {"fill_price": fill_price, "size_filled": size_filled,
                "fill_status": "partial"}
    return {"fill_price": fill_price, "size_filled": shares, "fill_status": "full"}


# ─────────────────────────────────────────────────────────────────────────────
#  CLIENT INITIALIZATION (V2)
# ─────────────────────────────────────────────────────────────────────────────

_client_cache: Optional[object] = None


def _get_clob_client() -> Optional[object]:
    """Build V2 ClobClient. Cached singleton.
    V2 pattern: init with key → derive api_key → set creds.
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    if not HAS_SDK:
        return None
    if not config.PRIVATE_KEY:
        log.error("PRIVATE_KEY tidak diset di .env")
        return None

    try:
        from core.ssl_helper import configure_py_clob_httpx
        configure_py_clob_httpx()

        # Optional builder attribution (V2)
        builder_config = None
        if config.BUILDER_CODE:
            try:
                from py_clob_client_v2 import BuilderConfig
                builder_config = BuilderConfig(builder_code=config.BUILDER_CODE)
                log.info("Builder code attribution enabled")
            except Exception as e:
                log.warning("BUILDER_CODE invalid, ignored: %s", e)

        # Build kwargs — funder + signature_type optional (proxy/EOA modes)
        kwargs = dict(
            host=config.CLOB_API,
            chain_id=137,
            key=config.PRIVATE_KEY,
            use_server_time=True,    # avoid clock skew at L2 sign
            retry_on_error=True,     # auto-retry on V1↔V2 version mismatch
        )
        if config.FUNDER:
            kwargs["signature_type"] = config.SIG_TYPE
            kwargs["funder"] = config.FUNDER
        if builder_config is not None:
            kwargs["builder_config"] = builder_config

        # Step 1: build creds (from .env or derive via L1)
        if config.API_KEY and config.API_SECRET and config.PASSPHRASE:
            creds = ApiCreds(
                api_key=config.API_KEY,
                api_secret=config.API_SECRET,
                api_passphrase=config.PASSPHRASE,
            )
        else:
            bootstrap = ClobClient(**kwargs)
            creds = bootstrap.create_or_derive_api_key()
            log.info("Derived V2 API creds from L1 wallet signature")

        # Step 2: build full client with creds
        client = ClobClient(**kwargs, creds=creds)
        _client_cache = client
        log.info("V2 ClobClient ready (chain=137, sig_type=%s, funder=%s, builder=%s)",
                 config.SIG_TYPE, "set" if config.FUNDER else "EOA",
                 "yes" if builder_config else "no")
        return client
    except Exception as e:
        log.error("Gagal buat V2 ClobClient: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER STATUS / CANCEL VIA V2 SDK
# ─────────────────────────────────────────────────────────────────────────────

async def _check_order_status_sdk(client, order_id: str) -> dict:
    """V2 GET /order/{id} via SDK. Returns dict with status/size_matched/price."""
    if not order_id:
        return {"status": "unknown", "error": "empty order_id"}
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.get_order(order_id)),
            timeout=3.0,
        )
        if isinstance(result, dict):
            return result
        # Some SDK responses are objects — convert
        return vars(result) if hasattr(result, "__dict__") else {"raw": str(result)}
    except asyncio.TimeoutError:
        return {"status": "unknown", "error": "get_order timeout"}
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


async def _cancel_order_sdk(client, order_id: str) -> bool:
    """V2 cancel_order(OrderPayload). Returns True jika cancel reach exchange."""
    if not order_id:
        return False
    try:
        payload = OrderPayload(orderID=order_id)
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.cancel_order(payload)),
            timeout=5.0,
        )
        log.info("V2 cancel order %s OK", order_id)
        return True
    except Exception as e:
        log.error("V2 cancel error: %s", e)
        return False


async def cancel_all_orders() -> dict:
    """Best-effort cancel-all safety path for emergency handling."""
    if config.MOCK_MODE:
        return {"ok": True, "mock": True}
    client = _get_clob_client()
    if client is None:
        return {"ok": False, "error": "client unavailable"}
    try:
        loop = asyncio.get_running_loop()

        def _cancel_all():
            if hasattr(client, "cancel_all"):
                return client.cancel_all()
            if hasattr(client, "cancel_all_orders"):
                return client.cancel_all_orders()
            if hasattr(client, "cancel_orders"):
                return client.cancel_orders([])
            raise AttributeError("SDK client has no cancel-all method")

        result = await asyncio.wait_for(
            loop.run_in_executor(None, _cancel_all),
            timeout=5.0,
        )
        log.warning("V2 cancel-all submitted: %s", result)
        return {"ok": True, "result": result}
    except Exception as e:
        log.error("V2 cancel-all error: %s", e)
        return {"ok": False, "error": str(e)}


async def _wait_for_fill(client, order_id: str, expected_size: float,
                          timeout: float) -> dict:
    """
    Poll order status sampai filled, partial, atau timeout (untuk GTC SELL).
    Return: {fill_status, size_matched, avg_price}
    """
    if not order_id or config.MOCK_MODE:
        return {"fill_status": "full", "size_matched": expected_size, "avg_price": 0}

    start = time.time()
    last_status: dict = {}

    while time.time() - start < timeout:
        status = await _check_order_status_sdk(client, order_id)
        last_status = status

        order_status = (status.get("status") or
                        status.get("order_status") or "").upper()
        size_matched = float(status.get("size_matched") or
                             status.get("sizeMatched") or 0)

        # MATCHED / FILLED = fully filled
        if order_status in ("MATCHED", "FILLED"):
            avg_price = float(status.get("associate_trades_avg_price")
                              or status.get("price") or 0)
            log.info("Order %s FILLED: %.2f/%.2f shares @ %.4f",
                     order_id, size_matched or expected_size,
                     expected_size, avg_price)
            return {
                "fill_status": "full",
                "size_matched": size_matched or expected_size,
                "avg_price": avg_price,
            }

        # PARTIAL — tunggu sampai 70% timeout, lalu ambil partial
        if size_matched > 0 and size_matched < expected_size:
            if time.time() - start >= timeout * 0.7:
                log.warning("Partial fill: %.2f/%.2f — cancelling remainder",
                            size_matched, expected_size)
                await _cancel_order_sdk(client, order_id)
                avg_price = float(status.get("associate_trades_avg_price")
                                  or status.get("price") or 0)
                return {
                    "fill_status": "partial",
                    "size_matched": size_matched,
                    "avg_price": avg_price,
                }

        # CANCELLED oleh exchange — kalau ada partial fill, treat sebagai partial
        if order_status in ("CANCELLED", "CANCELED", "EXPIRED"):
            if size_matched > 0:
                avg_price = float(status.get("associate_trades_avg_price")
                                  or status.get("price") or 0)
                log.warning("Order %s CANCELLED with partial fill %.2f/%.2f",
                            order_id, size_matched, expected_size)
                return {
                    "fill_status": "partial",
                    "size_matched": size_matched,
                    "avg_price": avg_price,
                }
            return {"fill_status": "cancelled", "size_matched": 0, "avg_price": 0}

        await asyncio.sleep(FILL_POLL_INTERVAL)

    # TIMEOUT — cancel unfilled order
    log.warning("Fill timeout %.1fs for order %s — cancelling", timeout, order_id)
    size_matched = float(last_status.get("size_matched")
                         or last_status.get("sizeMatched") or 0)

    await _cancel_order_sdk(client, order_id)

    # Race condition guard: recheck status setelah cancel
    await asyncio.sleep(0.5)
    try:
        post_status = await _check_order_status_sdk(client, order_id)
        post_matched = float(post_status.get("size_matched")
                             or post_status.get("sizeMatched") or 0)
        if post_matched > size_matched:
            log.warning("Post-cancel re-check: %.2f shares FILLED (was %.2f). "
                        "Order completed before cancel reached exchange.",
                        post_matched, size_matched)
            size_matched = post_matched
            last_status = post_status
    except Exception as e:
        log.debug("Post-cancel status check failed: %s", e)

    if size_matched > 0:
        avg_price = float(last_status.get("associate_trades_avg_price")
                          or last_status.get("price") or 0)
        return {
            "fill_status": "partial" if size_matched < expected_size else "full",
            "size_matched": size_matched,
            "avg_price": avg_price,
        }
    return {"fill_status": "unfilled", "size_matched": 0, "avg_price": 0}


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET INFO (V2: tick_size + min_order_size from token orderbook)
# ─────────────────────────────────────────────────────────────────────────────

_market_info_cache: dict = {}
_MARKET_INFO_TTL = 300.0
_VALID_TICK_SIZES = ("0.1", "0.01", "0.001", "0.0001")


async def _get_market_info(client, token_id: str) -> dict:
    """Fetch and cache live tick size and exchange minimum for one token."""
    defaults = {"tick_size": "0.01", "min_order_size": LIVE_MIN_ORDER_SHARES}
    if not client or not token_id:
        return defaults

    cached = _market_info_cache.get(token_id)
    if cached and time.time() - cached["ts"] < _MARKET_INFO_TTL:
        return {
            "tick_size": cached["tick_size"],
            "min_order_size": cached["min_order_size"],
        }

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.get_order_book(token_id)),
            timeout=3.0,
        )
        if not isinstance(info, dict):
            info = vars(info) if hasattr(info, "__dict__") else {}

        ts_raw = str(info.get("tick_size", "0.01"))
        tick_size = ts_raw if ts_raw in _VALID_TICK_SIZES else "0.01"
        try:
            exchange_min = float(info.get("min_order_size") or LIVE_MIN_ORDER_SHARES)
        except (TypeError, ValueError):
            exchange_min = LIVE_MIN_ORDER_SHARES
        min_order = max(LIVE_MIN_ORDER_SHARES, exchange_min)

        _market_info_cache[token_id] = {
            "ts": time.time(),
            "tick_size": tick_size,
            "min_order_size": min_order,
        }
        return {"tick_size": tick_size, "min_order_size": min_order}
    except Exception as e:
        log.debug("get_order_book(%s) failed: %s - using live defaults", token_id, e)
        return defaults


# ─────────────────────────────────────────────────────────────────────────────
#  BUY — Limit FAK (Fill-and-Kill / IOC, partial fill allowed)
# ─────────────────────────────────────────────────────────────────────────────

def _quantize_binary_order(price: float, size: float) -> tuple[float, float]:
    """Return price/size that satisfy V2 binary amount precision limits."""
    d_price = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    max_p = Decimal("0.99")
    if d_price > max_p:
        d_price = max_p
    if d_price <= 0:
        return 0.0, 0.0

    price_cents = int(d_price * 100)
    g = math.gcd(price_cents, 100) if price_cents > 0 else 1
    size_step = Decimal(100 // g) / Decimal(100)
    raw_size = Decimal(str(size))
    n_steps = int(raw_size / size_step)
    size_dec = size_step * n_steps
    size_dec = size_dec.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return float(d_price), float(size_dec)


def quantize_buy_order(
    price: float,
    amount: float,
    *,
    fee_multiplier: float | None = None,
) -> tuple[float, float]:
    """Return the exact live order price and size for a USD buy budget."""
    if price <= 0 or amount <= 0:
        return 0.0, 0.0
    multiplier = config.FEE_MULTIPLIER if fee_multiplier is None else fee_multiplier
    d_price = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    d_price = min(d_price, Decimal("0.99"))
    if d_price <= 0:
        return 0.0, 0.0
    raw_size = (
        Decimal(str(amount))
        * Decimal(str(max(float(multiplier), 0.0001)))
        / d_price
    )
    return _quantize_binary_order(float(d_price), float(raw_size))


async def execute_buy(
    token_id: str,
    outcome: str,
    price: float,
    amount: float,
    condition_id: str = "",
    strict_price: bool = False,
    allow_partial: bool = True,
    skip_preflight: bool = False,
    mock_fill: tuple[float, float] | None = None,
    ignore_slippage: bool = False,
) -> TradeResult:
    """Entry point untuk BUY order (V2: FAK by default, FOK when partial is disabled)."""
    # Validate FIRST (sebelum mock branch — hindari ZeroDivisionError)
    if price <= 0 or amount <= 0:
        return TradeResult(success=False, error=f"Invalid price={price} amount={amount}")

    if config.MOCK_MODE:
        if mock_fill is not None:
            fill_price, fill_size = float(mock_fill[0]), float(mock_fill[1])
            if fill_price <= 0 or fill_size <= 0:
                return TradeResult(
                    success=False,
                    error="MOCK FOK unfilled from orderbook depth",
                    fill_status="fok_killed",
                    mock=True,
                )
            if strict_price and fill_price > price + 1e-9:
                return TradeResult(
                    success=False,
                    error=f"Strict price cap: book VWAP {fill_price:.4f} > limit {price:.4f}",
                    fill_status="strict_price_abort",
                    mock=True,
                )
            log.info(
                "[MOCK-BOOK] BUY %s limit=%.4f vwap=%.4f size=%.2f FOK full",
                outcome, price, fill_price, fill_size,
            )
            return TradeResult(
                success=True,
                order_id=f"mock_book_buy_{int(time.time()*1000)%10**9}",
                price=fill_price,
                size=fill_size,
                mock=True,
                fill_status="full",
                size_matched=fill_size,
            )
        sim = _simulate_live_buy(price, amount, strict_price=strict_price)
        if sim["killed"]:
            log.info("[MOCK-LIVE] BUY %s @ %.4f → FOK KILLED (sim insufficient liquidity)",
                     outcome, price)
            return TradeResult(
                success=False,
                error="MOCK FOK killed (live sim)",
                fill_status="fok_killed",
                mock=True,
            )
        if strict_price and sim["fill_price"] > price:
            return TradeResult(
                success=False,
                error=(f"Strict price cap: simulated fill {sim['fill_price']:.4f} "
                       f"> limit {price:.4f}"),
                fill_status="strict_price_abort",
                mock=True,
            )
        cost_cents = (sim["fill_price"] - price) * 100
        log.info("[MOCK-LIVE] BUY %s observed=%.4f fill=%.4f size=%.2f (cost=+%.2f¢)",
                 outcome, price, sim["fill_price"], sim["size"], cost_cents)
        return TradeResult(
            success=True,
            order_id=f"mock_buy_{int(time.time()*1000)%10**9}",
            price=sim["fill_price"],
            size=sim["size"],
            mock=True,
            fill_status="full",
            size_matched=sim["size"],
        )

    if not token_id or token_id in ("mock_up", "mock_down"):
        return TradeResult(success=False, error="Invalid token_id untuk Live mode")
    if not HAS_SDK:
        return TradeResult(success=False,
                           error="py-clob-client-v2 tidak terinstall — V2 wajib SDK.")

    client = _get_clob_client()
    if not client:
        return TradeResult(success=False, error="V2 ClobClient gagal init")

    info = await _get_market_info(client, token_id)
    tick_size = info["tick_size"]

    return await _buy_sdk_fok(
        client, token_id, outcome, price, amount, tick_size,
        strict_price=strict_price,
        allow_partial=allow_partial,
        skip_preflight=skip_preflight,
        ignore_slippage=ignore_slippage,
    )


async def _buy_sdk_fok(client, token_id: str, outcome: str,
                        price: float, amount: float, tick_size: str,
                        strict_price: bool = False,
                        allow_partial: bool = True,
                        skip_preflight: bool = False,
                        ignore_slippage: bool = False) -> TradeResult:
    """BUY via V2 SDK. FAK allows partial; FOK is all-or-kill."""
    try:
        tick = float(tick_size)
        order_type = OrderType.FAK if allow_partial else OrderType.FOK

        # ── Pre-flight: hitung harga eksak yg dibutuhkan untuk fill `amount`
        # via SDK calculate_market_price. Ini menelusuri orderbook & return
        # worst price yang akan dihit untuk fill seluruh amount.
        # Kalau likuiditas tak cukup, SDK raise exception → kita kill sebelum
        # POST /order yang pasti FOK-killed.
        loop = asyncio.get_running_loop()
        if skip_preflight:
            market_price = price
            log.warning(
                "BUY %s skip_preflight=true: sending direct %s cap %.4f",
                outcome,
                "FOK" if not allow_partial else "FAK",
                price,
            )
        else:
            try:
                market_price = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: client.calculate_market_price(
                            token_id, "BUY", float(amount), order_type
                        ),
                    ),
                    timeout=2.0,
                )
            except Exception as e:
                return TradeResult(
                    success=False,
                    error=f"Pre-flight likuiditas gagal (orderbook thin?): {e}",
                    fill_status="fak_killed",
                )

        # Guard slippage: market_price vs price snapshot
        slippage = (market_price - price) / price if price > 0 else 1.0
        if strict_price and market_price > price:
            return TradeResult(
                success=False,
                error=(f"Strict price cap: market {market_price:.4f} "
                       f"> limit {price:.4f}"),
                fill_status="strict_price_abort",
            )
        if not ignore_slippage and slippage > MAX_SLIPPAGE * 2:
            return TradeResult(
                success=False,
                error=f"Slippage {slippage*100:.2f}% > {MAX_SLIPPAGE*200:.1f}% "
                      f"(snapshot {price:.4f} vs market {market_price:.4f})",
                fill_status="slippage_abort",
            )
        if not ignore_slippage and slippage > MAX_SLIPPAGE:
            log.warning("BUY slippage %.2f%% (snap=%.4f, market=%.4f)",
                        slippage*100, price, market_price)

        # strict_price=True keeps the signed order's price at the caller cap.
        # the hard worst-price cap. Other strategies keep the old +1tick buffer.
        aggressive_price = price if strict_price else round(market_price + tick, 4)
        # Clamp ke max 1 - tick (Polymarket harga 0..1 exclusive)
        aggressive_price = min(aggressive_price, round(1 - tick, 4))

        aggressive_price = round(aggressive_price, 4)
        if aggressive_price <= 0:
            return TradeResult(success=False, error="price <= 0 setelah quantize",
                               fill_status="precision_error")
        expected_size = float(amount) / aggressive_price

        def _place_fak():
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(float(amount), 2),
                price=aggressive_price,
                side=Side.BUY,
                order_type=order_type,
            )
            return client.create_and_post_market_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size=tick_size),
                order_type=order_type,
            )

        loop = asyncio.get_running_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, _place_fak),
            timeout=FOK_REQUEST_TIMEOUT,
        )

        return _parse_fak_resp(
            resp,
            price,
            expected_size,
            outcome,
            allow_partial=allow_partial,
        )

    except asyncio.TimeoutError:
        return TradeResult(success=False,
                           error=f"FAK BUY timeout (>{FOK_REQUEST_TIMEOUT}s)")
    except Exception as e:
        log.error("V2 FAK BUY error: %s", e)
        return TradeResult(success=False, error=str(e))


def _parse_fak_resp(resp, orig_price: float, expected_size: float,
                    outcome: str, allow_partial: bool = True) -> TradeResult:
    """Parse V2 response untuk immediate-or-cancel BUY.
    FAK semantik: fill apa yang bisa, sisa di-cancel (no resting).
    FOK should fill all-or-kill; any partial is tracked so caller can unwind.
    """
    if resp is None:
        return TradeResult(success=False, error="SDK returned None",
                           fill_status="fak_killed")

    if not isinstance(resp, dict):
        resp = vars(resp) if hasattr(resp, "__dict__") else {"raw": str(resp)}

    # Error patterns
    err = resp.get("error") or resp.get("errorMsg") or resp.get("message")
    if err:
        log.warning("FAK BUY rejected: %s", err)
        return TradeResult(success=False, error=str(err),
                           fill_status="fak_killed")
    if resp.get("errorCode"):
        return TradeResult(success=False,
                           error=f"errorCode={resp['errorCode']}",
                           fill_status="fak_killed")

    if resp.get("success") is False:
        msg = resp.get("errorMsg") or "FAK rejected"
        log.warning("FAK BUY rejected: %s", msg)
        return TradeResult(success=False, error=str(msg),
                           fill_status="fak_killed")

    order_id = (resp.get("orderID") or resp.get("id") or
                resp.get("orderId") or "")
    status = (resp.get("status") or "").lower()

    # V2 status untuk FAK: "matched"=filled (full/partial), "unmatched"=zero fill
    if status == "unmatched":
        return TradeResult(success=False,
                           error="FAK unmatched (zero fill)",
                           order_id=str(order_id),
                           fill_status="fak_killed")

    if status == "live":
        # FAK seharusnya tidak rest. Kalau muncul = anomaly SDK.
        log.error("FAK returned status='live' (anomaly) — treating as kill")
        return TradeResult(success=False,
                           error="FAK anomaly: status=live",
                           order_id=str(order_id),
                           fill_status="fak_killed")

    # Parse size_matched / fill price
    making_amount = float(resp.get("makingAmount") or 0)
    taking_amount = float(resp.get("takingAmount") or 0)
    size_matched = float(
        resp.get("size_matched") or resp.get("sizeMatched") or
        taking_amount or 0
    )
    fill_price = float(
        resp.get("price") or resp.get("avgPrice") or
        resp.get("associate_trades_avg_price") or
        (making_amount / taking_amount if making_amount > 0 and taking_amount > 0 else 0) or
        orig_price
    )

    # Zero-fill guard (defensive — status check di atas seharusnya cukup)
    if size_matched <= 0:
        return TradeResult(success=False,
                           error="FAK zero size_matched",
                           order_id=str(order_id),
                           fill_status="fak_killed")

    # Partial fill detection
    is_partial = size_matched < expected_size * 0.99
    fill_pct = (size_matched / expected_size * 100) if expected_size > 0 else 0
    if is_partial and not allow_partial:
        log.error("FOK BUY returned partial %.2f/%.2f for %s; tracking so caller can unwind",
                  size_matched, expected_size, outcome)

    # Slippage observation
    slippage = abs(fill_price - orig_price)
    if slippage > MAX_SLIPPAGE:
        log.warning("FAK slippage: expected=%.4f actual=%.4f slip=%.4f",
                    orig_price, fill_price, slippage)

    fill_label = "PARTIAL" if is_partial else "FULL"
    log.info("FAK BUY %s: %s id=%s price=%.4f size=%.2f/%.2f (%.0f%%)",
             fill_label, outcome, order_id, fill_price,
             size_matched, expected_size, fill_pct)

    return TradeResult(
        success=True,
        order_id=str(order_id),
        price=fill_price,
        size=size_matched,
        fill_status="partial" if is_partial else "full",
        size_matched=size_matched,
    )


# Disabled — REST fallback tidak bisa sign EIP-712
async def _buy_rest(*args, **kwargs) -> TradeResult:
    return TradeResult(
        success=False,
        error="REST fallback disabled di V2: wajib EIP-712 via py-clob-client-v2.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SELL — Limit GTC (allow partial exit)
# ─────────────────────────────────────────────────────────────────────────────

async def execute_sell(
    token_id: str,
    outcome: str,
    price: float,
    shares: float,
    condition_id: str = "",
) -> TradeResult:
    """Entry point untuk SELL order (V2: Limit GTC + fill polling)."""
    # Validate FIRST
    if price <= 0 or shares <= 0:
        return TradeResult(success=False, error=f"Invalid price={price} shares={shares}")

    if config.MOCK_MODE:
        sim = _simulate_live_sell(price, shares)
        cost_cents = (price - sim["fill_price"]) * 100
        log.info("[MOCK-LIVE] SELL %s observed=%.4f fill=%.4f size=%.2f/%.2f (cost=-%.2f¢, %s)",
                 outcome, price, sim["fill_price"], sim["size_filled"], shares,
                 cost_cents, sim["fill_status"])
        return TradeResult(
            success=True,
            order_id=f"mock_sell_{int(time.time()*1000)%10**9}",
            price=sim["fill_price"],
            size=sim["size_filled"],
            mock=True,
            fill_status=sim["fill_status"],
            size_matched=sim["size_filled"],
        )

    if not token_id or token_id in ("mock_up", "mock_down"):
        return TradeResult(success=False, error="Invalid token_id untuk Live mode")
    if not HAS_SDK:
        return TradeResult(success=False,
                           error="py-clob-client-v2 tidak terinstall — V2 wajib SDK.")

    client = _get_clob_client()
    if not client:
        return TradeResult(success=False, error="V2 ClobClient gagal init")

    info = await _get_market_info(client, token_id)
    min_order = info["min_order_size"]
    tick_size = info["tick_size"]

    if shares < min_order:
        log.warning("SELL rejected: shares %.2f < min %.2f. "
                    "Position akan di-hold + redeem manual.",
                    shares, min_order)
        return TradeResult(
            success=False,
            error=f"Shares {shares:.2f} < min {min_order} — hold + manual redeem",
            fill_status="sell_min_size",
        )

    return await _sell_sdk_gtc(
        client,
        token_id,
        outcome,
        price,
        shares,
        tick_size,
        min_order_size=min_order,
    )


async def _sell_sdk_gtc(client, token_id: str, outcome: str,
                         price: float, shares: float, tick_size: str,
                         min_order_size: float = LIVE_MIN_ORDER_SHARES) -> TradeResult:
    """SELL via V2 SDK Limit GTC + fill verification."""
    try:
        tick = float(tick_size)
        aggressive_price = round(max(tick, price - tick), 4)
        aggressive_price, order_size = _quantize_binary_order(aggressive_price, shares)
        if aggressive_price <= 0 or order_size < min_order_size:
            return TradeResult(
                success=False,
                error=(f"SELL precision/min-size guard: price={aggressive_price:.4f} "
                       f"size={order_size:.4f}"),
                fill_status="precision_error",
            )

        def _place_gtc():
            order_args = OrderArgs(
                token_id=token_id,
                price=aggressive_price,
                size=order_size,
                side=Side.SELL,
            )
            return client.create_and_post_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size=tick_size),
                order_type=OrderType.GTC,
            )

        loop = asyncio.get_running_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, _place_gtc),
            timeout=8.0,
        )
        parsed = _parse_sdk_resp(resp, price, order_size, outcome)
        if not parsed.success:
            return parsed

        fill = await _wait_for_fill(client, parsed.order_id, order_size, FILL_TIMEOUT_SELL)
        return _apply_fill_result(parsed, fill, price, order_size)

    except asyncio.TimeoutError:
        return TradeResult(success=False, error="V2 SELL timeout (>8s)")
    except Exception as e:
        log.error("V2 SELL error: %s", e)
        return TradeResult(success=False, error=str(e))


# Disabled — REST fallback tidak bisa sign EIP-712
async def _sell_rest(*args, **kwargs) -> TradeResult:
    return TradeResult(
        success=False,
        error="REST fallback disabled di V2: wajib EIP-712 via py-clob-client-v2.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SHARED HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _apply_fill_result(parsed: TradeResult, fill: dict,
                        orig_price: float, expected_size: float) -> TradeResult:
    """Gabungkan parsed order result + fill verification (untuk GTC SELL)."""
    fill_status = fill.get("fill_status", "unknown")
    size_matched = fill.get("size_matched", 0)
    avg_price = fill.get("avg_price", 0)

    actual_price = avg_price if avg_price > 0 else parsed.price

    slippage = abs(actual_price - orig_price)
    if slippage > MAX_SLIPPAGE and actual_price > 0:
        log.warning("Slippage: expected=%.4f actual=%.4f slip=%.4f (max=%.4f)",
                    orig_price, actual_price, slippage, MAX_SLIPPAGE)
        if slippage > MAX_SLIPPAGE * 2:
            log.error("SLIPPAGE ABORT: %.4f > %.4f (2x max)",
                      slippage, MAX_SLIPPAGE * 2)
            # Phantom shares guard: kalau sudah fill, treat sebagai partial
            if fill_status in ("full", "partial") and size_matched > 0:
                log.warning("Slippage tinggi tapi order FILLED — track sebagai partial")
                return TradeResult(
                    success=True,
                    order_id=parsed.order_id,
                    price=actual_price,
                    size=size_matched,
                    fill_status="partial",
                    size_matched=size_matched,
                )
            return TradeResult(
                success=False,
                order_id=parsed.order_id,
                error=f"Slippage abort: {slippage:.4f} > {MAX_SLIPPAGE*2:.4f}",
                fill_status="slippage_abort",
                size_matched=0,
            )

    if fill_status == "full":
        return TradeResult(
            success=True,
            order_id=parsed.order_id,
            price=actual_price,
            size=size_matched or expected_size,
            fill_status="full",
            size_matched=size_matched or expected_size,
        )

    if fill_status == "partial" and size_matched > 0:
        log.warning("Partial fill: %.2f/%.2f shares — adjusting position",
                    size_matched, expected_size)
        return TradeResult(
            success=True,
            order_id=parsed.order_id,
            price=actual_price,
            size=size_matched,
            fill_status="partial",
            size_matched=size_matched,
        )

    log.error("Order %s not filled: %s", parsed.order_id, fill_status)
    return TradeResult(
        success=False,
        order_id=parsed.order_id,
        error=f"Order not filled: {fill_status}",
        fill_status=fill_status,
        size_matched=0,
    )


def _parse_sdk_resp(resp, price: float, size: float, outcome: str) -> TradeResult:
    """Parse response dari V2 create_and_post_order (untuk GTC path)."""
    if resp is None:
        return TradeResult(success=False, error="SDK returned None")

    if not isinstance(resp, dict):
        resp = vars(resp) if hasattr(resp, "__dict__") else {}

    if resp.get("error"):
        return TradeResult(success=False, error=str(resp["error"]))
    if resp.get("errorMsg"):
        return TradeResult(success=False, error=str(resp["errorMsg"]))
    if resp.get("status") in ("error", "failed"):
        return TradeResult(success=False, error=resp.get("message", "Unknown error"))
    if resp.get("errorCode"):
        return TradeResult(success=False, error=f"errorCode={resp['errorCode']}")
    if resp.get("success") is False:
        return TradeResult(success=False,
                           error=resp.get("errorMsg") or "post_order success=false")

    order_id = (resp.get("orderID") or resp.get("id") or
                resp.get("orderId") or "")

    actual_price = float(resp.get("price", price) or price)
    actual_size  = float(resp.get("size",  size)  or size)

    log.info("V2 order placed: %s id=%s price=%.4f size=%.2f",
             outcome, order_id, actual_price, actual_size)
    return TradeResult(
        success=True,
        order_id=str(order_id),
        price=actual_price,
        size=actual_size,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE BALANCE FETCH — V2 pUSD via SDK
# ─────────────────────────────────────────────────────────────────────────────

_live_bal_cache: dict = {"cash": 0.0, "ts": 0.0, "ok": False, "error": ""}
_live_portfolio_cache: dict = {
    "portfolio": 0.0,
    "ts": 0.0,
    "ok": False,
    "error": "",
}
_LIVE_BAL_TTL = 10.0   # cache 10s
_DATA_API = os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com").rstrip("/")


def invalidate_live_balance_cache():
    """FIX BUG #4: Force expire cache setelah BUY/SELL sukses.
    Tanpa ini, _check_balance bisa return stale cash 10s setelah trade,
    menyebabkan over-leverage di multi-trade window atau BUY error LIVE."""
    global _live_bal_cache
    _live_bal_cache["ts"] = 0.0
    _live_portfolio_cache["ts"] = 0.0


async def fetch_live_balance(session: aiohttp.ClientSession) -> dict:
    """Fetch real pUSD balance dari V2 via SDK.
    NOTE: V2 collateral = pUSD. User harus wrap USDC.e → pUSD via
    Collateral Onramp sebelum trading kalau API-only.
    """
    global _live_bal_cache

    if time.time() - _live_bal_cache["ts"] < _LIVE_BAL_TTL:
        return _live_bal_cache

    if not HAS_SDK:
        _live_bal_cache = {"cash": 0.0, "ts": time.time(),
                           "ok": False, "error": "no V2 SDK"}
        return _live_bal_cache

    client = _get_clob_client()
    if not client:
        _live_bal_cache = {"cash": 0.0, "ts": time.time(),
                           "ok": False, "error": "V2 client init failed"}
        return _live_bal_cache

    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.get_balance_allowance(params)),
            timeout=15.0,
        )

        if not isinstance(result, dict):
            result = vars(result) if hasattr(result, "__dict__") else {}

        # FIX: balance dulu, allowance kedua. Allowance bisa MaxUint256 setelah
        # approve → $1e71 nilai palsu kalau diambil duluan.
        raw = result.get("balance") or result.get("allowance") or "0"
        cash = float(raw) / 1e6   # pUSD = 6 decimals
        _live_bal_cache = {"cash": round(cash, 2), "ts": time.time(),
                           "ok": True, "error": ""}
        return _live_bal_cache
    except asyncio.TimeoutError:
        _live_bal_cache = {"cash": 0.0, "ts": time.time(),
                           "ok": False, "error": "balance fetch timeout"}
    except Exception as e:
        _live_bal_cache = {"cash": 0.0, "ts": time.time(),
                           "ok": False, "error": str(e)}

    return _live_bal_cache


async def fetch_live_portfolio(session: aiohttp.ClientSession) -> dict:
    """Fetch mark-to-market value of all positions for the live proxy wallet."""
    global _live_portfolio_cache

    if time.time() - _live_portfolio_cache["ts"] < _LIVE_BAL_TTL:
        return _live_portfolio_cache

    user = str(config.FUNDER or "").strip()
    if not user:
        _live_portfolio_cache = {
            "portfolio": 0.0,
            "ts": time.time(),
            "ok": False,
            "error": "POLYMARKET_FUNDER_ADDRESS is empty",
        }
        return _live_portfolio_cache

    try:
        async with session.get(
            f"{_DATA_API}/value",
            params={"user": user},
            timeout=aiohttp.ClientTimeout(total=5.0),
        ) as resp:
            if resp.status != 200:
                _live_portfolio_cache = {
                    "portfolio": 0.0,
                    "ts": time.time(),
                    "ok": False,
                    "error": f"Data API HTTP {resp.status}",
                }
                return _live_portfolio_cache
            payload = await resp.json()

        row = payload[0] if isinstance(payload, list) and payload else payload
        value = float(row.get("value", 0.0) or 0.0) if isinstance(row, dict) else 0.0
        _live_portfolio_cache = {
            "portfolio": round(max(value, 0.0), 2),
            "ts": time.time(),
            "ok": True,
            "error": "",
        }
    except asyncio.TimeoutError:
        _live_portfolio_cache = {
            "portfolio": 0.0,
            "ts": time.time(),
            "ok": False,
            "error": "portfolio fetch timeout",
        }
    except Exception as e:
        _live_portfolio_cache = {
            "portfolio": 0.0,
            "ts": time.time(),
            "ok": False,
            "error": str(e),
        }

    return _live_portfolio_cache
