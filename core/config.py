"""config.py — Load settings from .env (CTF Exchange V2)"""
import os
from pathlib import Path
from dotenv import load_dotenv

# .env lives at repo root; this file is in <root>/core/, so go up one.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

def _g(k, d=""): return os.getenv(k, d)
def _f(k, d=0.0): return float(_g(k, str(d)))
def _b(k, d="true"): return _g(k, d).lower() in ("true","1","yes")

# ── Polymarket ──────────────────────────────────────────────────────────────
API_KEY       = _g("POLYMARKET_API_KEY")
API_SECRET    = _g("POLYMARKET_API_SECRET")
PASSPHRASE    = _g("POLYMARKET_PASSPHRASE")
PRIVATE_KEY   = _g("POLYMARKET_PRIVATE_KEY").strip().strip("'\"")
FUNDER        = _g("POLYMARKET_FUNDER_ADDRESS").strip().strip("'\"")
_raw_sig      = _g("POLYMARKET_SIGNATURE_TYPE", "").strip()
if _raw_sig:
    SIG_TYPE = int(_raw_sig)
else:
    SIG_TYPE = 1 if FUNDER else 0

# ── Telegram ────────────────────────────────────────────────────────────────
TG_TOKEN      = _g("TELEGRAM_BOT_TOKEN")
TG_CHAT       = _g("TELEGRAM_CHAT_ID")

# ── OpenRouter AI advisor ──────────────────────────────────────────────────
OPENROUTER_API_KEY = _g("OPENROUTER_API_KEY").strip()
OPENROUTER_MODEL   = _g("OPENROUTER_MODEL", "tencent/hy3-preview:free").strip()
OPENROUTER_JSON_MODE = _b("OPENROUTER_JSON_MODE", "false")
OPENROUTER_MAX_TOKENS = int(_f("OPENROUTER_MAX_TOKENS", 768))
OPENROUTER_REASONING_EFFORT = _g("OPENROUTER_REASONING_EFFORT", "none").strip().lower()
OPENROUTER_SITE_URL = _g("OPENROUTER_SITE_URL", "http://localhost:5004").strip()
OPENROUTER_APP_NAME = _g("OPENROUTER_APP_NAME", "polymarket-btc-bot").strip()
AI_ADVISOR_ENABLED = _b("AI_ADVISOR_ENABLED", "false")
AI_AUTO_EXECUTE    = _b("AI_AUTO_EXECUTE", "false")
AI_MIN_CONFIDENCE  = _f("AI_MIN_CONFIDENCE", 0.70)
AI_DEFAULT_AMOUNT_USD = _f("AI_DEFAULT_AMOUNT_USD", 25.0)
AI_MIN_AMOUNT_USD     = _f("AI_MIN_AMOUNT_USD", 5.0)
AI_MAX_AMOUNT_USD     = _f("AI_MAX_AMOUNT_USD", 25.0)
AI_MAX_PRICE          = _f("AI_MAX_PRICE", 0.75)
AI_TP_PRICE           = _f("AI_TP_PRICE", 0.75)
AI_MIN_INTERVAL_SECS  = _f("AI_MIN_INTERVAL_SECS", 5.0)
AI_TIMEOUT_SECS       = _f("AI_TIMEOUT_SECS", 8.0)
AI_TEMPERATURE        = _f("AI_TEMPERATURE", 0.1)

# ── Trading ─────────────────────────────────────────────────────────────────
TRADE_AMOUNT          = _f("TRADE_AMOUNT", 5.0)
MAX_TRADES_PER_WINDOW = int(_f("MAX_TRADES_PER_WINDOW", 5))
INITIAL_BALANCE       = _f("INITIAL_BALANCE", 200.0)

# ═══════════════════════════════════════════════════════════════════════════
#  TRIGGER ON/OFF — set true/false di .env untuk enable/disable trigger
# ═══════════════════════════════════════════════════════════════════════════
T1_ENABLED = _b("T1_ENABLED", "false")  # Sniper (5-50s) — OFF: too early, no BTC signal
T2_ENABLED = _b("T2_ENABLED", "true")   # Early (40-100s) — primary winner (75% WR data)
T3_ENABLED = _b("T3_ENABLED", "true")   # Momentum (80-165s) — strict filter via cfg
T4_ENABLED = _b("T4_ENABLED", "false")  # Mid (140-220s) — OFF: reversal death zone
T5_ENABLED = _b("T5_ENABLED", "false")  # Late (200-265s) — OFF: 0% WR
T7_ENABLED = _b("T7_ENABLED", "true")   # Scalp (250-280s) — last-second momentum
TX_ENABLED = _b("TX_ENABLED", "false")  # Last-second — OFF: no edge, no time for SL

# Helper: get enabled triggers as set
def get_enabled_triggers() -> set:
    enabled = set()
    if T1_ENABLED: enabled.add("T1")
    if T2_ENABLED: enabled.add("T2")
    if T3_ENABLED: enabled.add("T3")
    if T4_ENABLED: enabled.add("T4")
    if T5_ENABLED: enabled.add("T5")
    if T7_ENABLED: enabled.add("T7")
    if TX_ENABLED: enabled.add("TX")
    return enabled

# ═══════════════════════════════════════════════════════════════════════════
#  T1: Sniper (5-50s) — harga masih murah, R:R terbaik
# ═══════════════════════════════════════════════════════════════════════════
T1_ELAPSED_MIN = _f("T1_ELAPSED_MIN", 5.0)
T1_ELAPSED_MAX = _f("T1_ELAPSED_MAX", 50.0)
T1_PRICE_MIN   = _f("T1_PRICE_MIN",   0.50)
T1_PRICE_MAX   = _f("T1_PRICE_MAX",   0.62)
T1_DELTA_MIN   = _f("T1_DELTA_MIN",   8.0)

# ═══════════════════════════════════════════════════════════════════════════
#  T2: Early (40-100s) — konfirmasi awal arah BTC
# ═══════════════════════════════════════════════════════════════════════════
T2_ELAPSED_MIN = _f("T2_ELAPSED_MIN", 40.0)
T2_ELAPSED_MAX = _f("T2_ELAPSED_MAX", 100.0)
T2_PRICE_MIN   = _f("T2_PRICE_MIN",   0.55)
T2_PRICE_MAX   = _f("T2_PRICE_MAX",   0.68)   # was 0.70 — data: lost at 0.70+
T2_DELTA_MIN   = _f("T2_DELTA_MIN",   10.0)

# ═══════════════════════════════════════════════════════════════════════════
#  T3: Momentum (80-165s) — BTC maintain arah, momentum terbangau
# ═══════════════════════════════════════════════════════════════════════════
T3_ELAPSED_MIN = _f("T3_ELAPSED_MIN", 80.0)
T3_ELAPSED_MAX = _f("T3_ELAPSED_MAX", 130.0)   # was 165 — data: 132-176s = loss zone
T3_PRICE_MIN   = _f("T3_PRICE_MIN",   0.58)
T3_PRICE_MAX   = _f("T3_PRICE_MAX",   0.70)    # was 0.78 — data: lost at 0.74-0.75
T3_DELTA_MIN   = _f("T3_DELTA_MIN",   10.0)

# ═══════════════════════════════════════════════════════════════════════════
#  T4: Mid-window (140-220s) — harga sudah refleksi arah
# ═══════════════════════════════════════════════════════════════════════════
T4_ELAPSED_MIN = _f("T4_ELAPSED_MIN", 140.0)
T4_ELAPSED_MAX = _f("T4_ELAPSED_MAX", 220.0)
T4_PRICE_MIN   = _f("T4_PRICE_MIN",   0.58)
T4_PRICE_MAX   = _f("T4_PRICE_MAX",   0.82)
T4_DELTA_MIN   = _f("T4_DELTA_MIN",   15.0)

# ═══════════════════════════════════════════════════════════════════════════
#  T5: Late confirm (200-265s) — BTC sudah dominan, delta ketat
# ═══════════════════════════════════════════════════════════════════════════
T5_ELAPSED_MIN = _f("T5_ELAPSED_MIN", 200.0)
T5_ELAPSED_MAX = _f("T5_ELAPSED_MAX", 265.0)
T5_PRICE_MIN   = _f("T5_PRICE_MIN",   0.65)
T5_PRICE_MAX   = _f("T5_PRICE_MAX",   0.88)
T5_DELTA_MIN   = _f("T5_DELTA_MIN",   20.0)


# ═══════════════════════════════════════════════════════════════════════════
#  T7: Scalp (250-280s) — entry cepat di akhir window
# ═══════════════════════════════════════════════════════════════════════════
T7_ELAPSED_MIN = _f("T7_ELAPSED_MIN", 250.0)
T7_ELAPSED_MAX = _f("T7_ELAPSED_MAX", 280.0)
T7_PRICE_MIN   = _f("T7_PRICE_MIN",   0.70)
T7_PRICE_MAX   = _f("T7_PRICE_MAX",   0.93)
T7_DELTA_MIN   = _f("T7_DELTA_MIN",   20.0)

# ═══════════════════════════════════════════════════════════════════════════
#  TX: Last-second (297-300s) — WAJIB beli jika enabled, 1 trade/window
# ═══════════════════════════════════════════════════════════════════════════
TX_ELAPSED_MIN = _f("TX_ELAPSED_MIN", 297.0)
TX_ELAPSED_MAX = _f("TX_ELAPSED_MAX", 300.0)
TX_PRICE_MIN   = _f("TX_PRICE_MIN",   0.50)
TX_PRICE_MAX   = _f("TX_PRICE_MAX",   0.99)
TX_DELTA_MIN   = _f("TX_DELTA_MIN",   0.0)

# ═══════════════════════════════════════════════════════════════════════════
#  EXIT — Take Profit per trigger
# ═══════════════════════════════════════════════════════════════════════════
TP_T1 = _f("TP_T1", 0.58)
TP_T2 = _f("TP_T2", 0.75)
TP_T3 = _f("TP_T3", 0.82)
TP_T4 = _f("TP_T4", 0.86)
TP_T5 = _f("TP_T5", 0.92)
TP_T6 = _f("TP_T6", 0.70)
TP_T7 = _f("TP_T7", 0.95)
TP_TX = _f("TP_TX", 0.99)

TRAIL_ACTIVATE_USD  = _f("TRAIL_ACTIVATE_USD", 0.10)
TRAIL_DROP_PRICE    = _f("TRAIL_DROP_PRICE",   0.04)
FORCE_EXIT_SECS     = _f("FORCE_EXIT_SECS",    25.0)

# ── Fee & Bot ───────────────────────────────────────────────────────────────
# NOTE V2: protocol-handled fees at match time (formula C × r × p × (1 - p)).
# FEE_MULTIPLIER tetap dipakai untuk size estimation (over-estimate aman).
FEE_MULTIPLIER = _f("FEE_MULTIPLIER", 0.9993)
MOCK_MODE      = _b("MOCK_MODE", "true")
LOG_LEVEL      = _g("LOG_LEVEL", "INFO")

# ── MOCK LIVE SIMULATION (V2 realistic costs in MOCK_MODE) ──────────────────
# Apply realistic V2 trading costs ke mock orders supaya P&L cermin live.
# Set MOCK_LIVE_SIM=false untuk pure logic test (no costs, like sebelumnya).
MOCK_LIVE_SIM       = _b("MOCK_LIVE_SIM", "true")
# Bid-ask spread di ticks (1 tick = 0.01 untuk BTC 5-min binary)
# BUY pays half-spread up, SELL receives half-spread down. 2 ticks = 2¢ typical.
MOCK_SPREAD_TICKS   = _f("MOCK_SPREAD_TICKS", 2.0)
# Extra slippage di ticks, uniform random 0..N (FOK reaches deeper book).
MOCK_SLIPPAGE_TICKS = _f("MOCK_SLIPPAGE_TICKS", 1.0)
# V2 protocol fee coefficient. Effective fee = shares * r * p * (1-p);
# at p=0.5, r=0.072 is about 1.8% of notional per leg.
MOCK_FEE_RATE       = _f("MOCK_FEE_RATE", 0.072)
# FOK BUY kill probability: probability liquidity at price+1tick insufficient.
MOCK_FOK_KILL_PCT   = _f("MOCK_FOK_KILL_PCT", 0.05)
# GTC SELL partial fill probability (fill 50-95% of size).
MOCK_PARTIAL_PCT    = _f("MOCK_PARTIAL_PCT", 0.03)
# Random seed: 0 = nondeterministic (variance across runs), >0 = reproducible.
MOCK_RANDOM_SEED    = int(_g("MOCK_RANDOM_SEED", "0"))

# Strategy switch.
STRATEGY_MODE = _g("STRATEGY_MODE", "END_WINDOW")

# ── API endpoints ───────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
# CTF Exchange V2 — production URL took over from V1 at 2026-04-22 cutover
CLOB_API  = _g("CLOB_API", "https://clob.polymarket.com")

# ── CTF Exchange V2 (live since 2026-04-22) ─────────────────────────────────
# CLOB_API_V2 retained for backward-compat with market.py probe; defaults sama
CLOB_API_V2 = _g("CLOB_API_V2", "https://clob.polymarket.com")
PREFER_V2   = _b("PREFER_V2", "true")   # V1 dead — default true
# Optional builder code (bytes32 hex). Empty = no builder attribution.
BUILDER_CODE = _g("BUILDER_CODE", "").strip()
