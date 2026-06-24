"""
state.py — Shared state + settings store (v2 hardened)
=======================================================
- Atomic file writes (write to .tmp, then os.replace)
- Safe JSON reads with corruption fallback
- Win rate decay monitoring (recent 50 trades)
- BotSettings runtime config dari dashboard
"""
import json, time, logging, threading, os, tempfile
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

log = logging.getLogger("state")

# Runtime state files live under <repo>/runtime_data/.
_ROOT_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT_DIR / "runtime_data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE    = _DATA_DIR / "state.json"
TRADES_FILE   = _DATA_DIR / "trades.json"
SNAPSHOTS_FILE= _DATA_DIR / "snapshots.json"
PRICE_EXTREMES_FILE = _DATA_DIR / "price_extremes.json"
LOW_PRICE_WIN_THRESHOLD = 0.10
BALANCE_FILE  = _DATA_DIR / "balance.json"
SETTINGS_FILE = _DATA_DIR / "bot_settings.json"
CUM_STATS_FILE= _DATA_DIR / "cumulative_stats.json"
SNAPSHOT_MAX_ROWS = max(100, int(float(os.getenv("SNAPSHOT_MAX_ROWS", "500"))))
PRICE_EXTREME_LEVELS = (0.97, 0.98, 0.99)
PRICE_EXTREME_MAX_WINDOWS = max(100, int(float(os.getenv("PRICE_EXTREME_MAX_WINDOWS", "2000"))))

_json_cache_lock = threading.Lock()
_json_cache: dict[Path, tuple[int, int, object]] = {}


def _clone_json(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return [dict(item) if isinstance(item, dict) else item for item in value]
    return value


def _cached_read_json(path: Path, default=None):
    """Read JSON once per file version; repeated hot-loop reads only stat."""
    try:
        stat = path.stat()
    except OSError:
        return default
    key = (stat.st_mtime_ns, stat.st_size)
    with _json_cache_lock:
        cached = _json_cache.get(path)
        if cached and cached[0] == key[0] and cached[1] == key[1]:
            return _clone_json(cached[2])
    data = _safe_read_json(path, default)
    with _json_cache_lock:
        _json_cache[path] = (key[0], key[1], _clone_json(data))
    return _clone_json(data)


def _remember_json(path: Path, data):
    try:
        stat = path.stat()
    except OSError:
        return
    with _json_cache_lock:
        _json_cache[path] = (stat.st_mtime_ns, stat.st_size, _clone_json(data))


# ─────────────────────────────────────────────────────────────────────────────
#  ATOMIC FILE I/O
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: str):
    """Write to temp file then atomic rename — prevents corruption on crash.
    Windows-safe: retry on PermissionError (reader holds handle briefly)."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}_", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Windows: os.replace dapat gagal kalau dashboard/tracker/AV baru saja
        # open target file. Keep retrying long enough to avoid losing a filled leg.
        last_err = None
        for delay in (0.0, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp_path, str(path))
                return
            except PermissionError as e:
                last_err = e
        # Semua retry habis — re-raise error asli
        raise last_err
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _safe_read_json(path: Path, default=None):
    """Read JSON with fallback on corruption. Handles UTF-8 BOM + binary garbage."""
    try:
        if path.exists():
            # utf-8-sig auto-strip BOM; errors='replace' tolerate byte corruption
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if text.strip():
                return json.loads(text)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log.warning("Corrupt file %s: %s — using default", path.name, e)
    return default


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BotSettings:
    strategy_settings_version: int = 1
    market_5m_enabled: bool       = True
    market_15m_enabled: bool      = False
    arb5_enabled: bool            = False
    arb5_price: float             = 0.43
    arb5_trade_usd: float         = 100.0
    arb15_enabled: bool           = False
    arb15_price: float            = 0.43
    arb15_trade_usd: float        = 100.0
    max_trades_per_window: int   = 9
    trade_amount: float          = 100.0
    max_loss_per_trade: float    = 10.0
    profit_stop_pct: float       = 100.0
    t1_enabled: bool             = True
    t2_enabled: bool             = True
    t3_enabled: bool             = True
    t4_enabled: bool             = True
    t5_enabled: bool             = True
    t6_enabled: bool             = True
    time1_enabled: bool          = True
    time2_enabled: bool          = True
    time3_enabled: bool          = True
    time4_enabled: bool          = True
    time5_enabled: bool          = True
    time6_enabled: bool          = True
    time1_price: float           = 0.98
    time2_price: float           = 0.99
    time3_price: float           = 0.97
    time4_price: float           = 0.96
    time5_price: float           = 0.95
    time6_price: float           = 0.94
    time1_min_secs_left: float   = 3.0
    time2_min_secs_left: float   = 3.0
    time3_min_secs_left: float   = 3.0
    time4_min_secs_left: float   = 3.0
    time5_min_secs_left: float   = 3.0
    time6_min_secs_left: float   = 3.0
    time1_max_secs_left: float   = 299.0
    time2_max_secs_left: float   = 299.0
    time3_max_secs_left: float   = 299.0
    time4_max_secs_left: float   = 299.0
    time5_max_secs_left: float   = 299.0
    time6_max_secs_left: float   = 299.0
    time1_trade_usd: float       = 100.0
    time2_trade_usd: float       = 100.0
    time3_trade_usd: float       = 100.0
    time4_trade_usd: float       = 100.0
    time5_trade_usd: float       = 100.0
    time6_trade_usd: float       = 100.0
    time1_min_delta_usd: float   = 3.0
    time2_min_delta_usd: float   = 3.0
    time3_min_delta_usd: float   = 3.0
    time4_min_delta_usd: float   = 3.0
    time5_min_delta_usd: float   = 3.0
    time6_min_delta_usd: float   = 3.0
    buy1_enabled: bool           = True
    buy1_trade_usd: float        = 25.0
    buy1_min_price: float        = 0.50
    buy1_max_price: float        = 0.60
    buy1_sell_min_price: float   = 0.80
    buy1_sell_max_price: float   = 0.90
    buy1_min_delta_usd: float    = 8.0
    buy1_max_secs_left: float    = 260.0
    buy1_min_secs_left: float    = 20.0
    buy1_max_open_positions: int = 1
    t1_seconds_max: float        = 25.0
    t1_seconds_min: float        = 20.0
    t1_delta_min: float          = 70.0
    t1_min_price: float          = 0.50
    t1_max_price: float          = 0.90
    t2_seconds_max: float        = 20.0
    t2_seconds_min: float        = 15.0
    t2_delta_min: float          = 55.0
    t2_min_price: float          = 0.50
    t2_max_price: float          = 0.92
    t3_seconds_max: float        = 15.0
    t3_seconds_min: float        = 10.0
    t3_delta_min: float          = 35.0
    t3_min_price: float          = 0.50
    t3_max_price: float          = 0.94
    t4_seconds_max: float        = 10.0
    t4_seconds_min: float        = 7.0
    t4_delta_min: float          = 35.0
    t4_min_price: float          = 0.50
    t4_max_price: float          = 0.95
    t5_seconds_max: float        = 7.0
    t5_seconds_min: float        = 5.0
    t5_delta_min: float          = 25.0
    t5_min_price: float          = 0.50
    t5_max_price: float          = 0.95
    t6_seconds_max: float        = 5.0
    t6_seconds_min: float        = 4.0
    t6_delta_min: float          = 12.0
    t6_min_price: float          = 0.50
    t6_max_price: float          = 0.99
    t7_delta_min: float          = 20.0
    tp_t1: float                 = 0.68
    tp_t2: float                 = 0.75
    tp_t3: float                 = 0.82
    tp_t4: float                 = 0.86
    tp_t5: float                 = 0.92
    tp_t6: float                 = 0.70
    tp_t7: float                 = 0.95
    tp_tx: float                 = 0.99
    trail_activate_usd: float    = 0.10
    trail_drop_price: float      = 0.04
    force_exit_secs: float       = 15.0
    max_session_loss: float      = 50.0
    max_consecutive_losses: int  = 5
    # ── Daily loss circuit breaker (lebih ketat dari session_loss) ────────
    # Reset setiap UTC midnight. Halt trading kalau daily DD exceeded.
    max_daily_loss: float        = 20.0
    # ── Recovery mode (informational only — tidak ada sizing scale-down):
    # entered setelah X consecutive losses, exited setelah Y wins.
    # Tetap di-track untuk dashboard awareness, tapi tidak ubah trade_amount.
    recovery_after_losses: int   = 3
    recovery_size_pct: float     = 0.5    # legacy field, no-op
    recovery_exit_wins: int      = 2
    # ── Min TP margin (tick distance), mencegah trade dengan margin < cost ─
    min_tp_margin: float         = 0.04
    # ── Circuit Breaker toggles (granular ON/OFF) ─────────────────────────
    # Master switch: false = SEMUA CB off (session/daily/cons_loss/api_err/recovery)
    cb_master_enabled: bool      = True
    # Per-CB toggles (hanya berlaku kalau master=true)
    cb_session_loss_enabled: bool    = True   # halt jika session_pnl <= -max_session_loss
    cb_daily_loss_enabled: bool      = True   # halt jika daily_pnl <= -max_daily_loss
    cb_consec_loss_enabled: bool     = True   # halt jika consec_losses >= max_consecutive_losses
    cb_api_error_enabled: bool       = True   # halt jika error rate >30%
    cb_recovery_enabled: bool        = True   # auto scale-down setelah loss streak
    cb_loss_cooldown_enabled: bool   = True   # 15s cooldown setelah loss


def _apply_env_setting_overrides(s: BotSettings) -> BotSettings:
    """Keep env-owned sizing fields consistent with config.py."""
    try:
        import core.config as config

        if "TRADE_AMOUNT" in os.environ:
            s.trade_amount = float(config.TRADE_AMOUNT)
        if "MAX_TRADES_PER_WINDOW" in os.environ:
            s.max_trades_per_window = int(config.MAX_TRADES_PER_WINDOW)
    except Exception as e:
        log.warning("settings env override error: %s", e)
    s.market_5m_enabled = True
    s.market_15m_enabled = False
    s.arb5_enabled = False
    s.arb15_enabled = False
    return s


def load_settings() -> BotSettings:
    try:
        d = _cached_read_json(SETTINGS_FILE)
        if d and isinstance(d, dict):
            s = BotSettings()
            legacy_strategy_settings = int(d.get("strategy_settings_version", 0) or 0) < 1
            for k, v in d.items():
                if not hasattr(s, k):
                    continue
                if legacy_strategy_settings and k in {
                    f"t{i}_delta_min" for i in range(1, 7)
                }:
                    continue
                target_type = type(getattr(s, k))
                # Bool fields: handle string "true"/"false" properly
                # (Python bool("false") = True due to non-empty string)
                if target_type is bool:
                    if isinstance(v, bool):
                        setattr(s, k, v)
                    elif isinstance(v, (int, float)):
                        setattr(s, k, bool(v))
                    elif isinstance(v, str):
                        setattr(s, k, v.strip().lower() in ("true", "1", "yes", "on"))
                else:
                    setattr(s, k, target_type(v))
            return _apply_env_setting_overrides(s)
    except Exception as e:
        log.warning("load_settings error: %s — pakai default", e)
    return _apply_env_setting_overrides(BotSettings())


def save_settings(s: BotSettings):
    data = asdict(s)
    _atomic_write(SETTINGS_FILE, json.dumps(data, indent=2))
    _remember_json(SETTINGS_FILE, data)


def update_settings(data: dict) -> BotSettings:
    s = load_settings()
    _VALID = {
        "strategy_settings_version": (1, 1),
        "max_trades_per_window": (1, 50),
        "trade_amount":          (1.0, 10000.0),
        "max_loss_per_trade":    (0.5, 1000.0),
        "t1_delta_min":          (1.0, 500.0),
        "t2_delta_min":          (1.0, 500.0),
        "t3_delta_min":          (1.0, 500.0),
        "t4_delta_min":          (1.0, 500.0),
        "t5_delta_min":          (1.0, 500.0),
        "t6_delta_min":          (0.0, 500.0),
        **{f"t{i}_seconds_max": (0.1, 300.0) for i in range(1, 7)},
        **{f"t{i}_seconds_min": (0.0, 299.9) for i in range(1, 7)},
        **{f"t{i}_min_price": (0.01, 0.99) for i in range(1, 7)},
        **{f"t{i}_max_price": (0.01, 0.99) for i in range(1, 7)},
        **{f"time{i}_price": (0.01, 0.99) for i in range(1, 7)},
        **{f"time{i}_min_secs_left": (0.0, 299.9) for i in range(1, 7)},
        **{f"time{i}_max_secs_left": (0.1, 300.0) for i in range(1, 7)},
        **{f"time{i}_trade_usd": (1.0, 10000.0) for i in range(1, 7)},
        **{f"time{i}_min_delta_usd": (0.0, 500.0) for i in range(1, 7)},
        "buy1_trade_usd":        (1.0, 10000.0),
        "buy1_min_price":        (0.01, 0.99),
        "buy1_max_price":        (0.01, 0.99),
        "buy1_sell_min_price":   (0.01, 0.99),
        "buy1_sell_max_price":   (0.01, 0.99),
        "buy1_min_delta_usd":    (0.0, 500.0),
        "buy1_max_secs_left":    (0.1, 300.0),
        "buy1_min_secs_left":    (0.0, 299.9),
        "buy1_max_open_positions": (1, 9),
        "t7_delta_min":          (1.0, 500.0),
        "tp_t1":                 (0.01, 0.99),
        "tp_t2":                 (0.01, 0.99),
        "tp_t3":                 (0.01, 0.99),
        "tp_t4":                 (0.01, 0.99),
        "tp_t5":                 (0.01, 0.99),
        "tp_t6":                 (0.01, 0.99),
        "tp_t7":                 (0.01, 0.99),
        "tp_tx":                 (0.01, 0.99),
        "trail_activate_usd":    (0.01, 100.0),
        "trail_drop_price":      (0.005, 0.50),
        "force_exit_secs":       (3.0, 120.0),
        "max_session_loss":      (5.0, 10000.0),
        "max_consecutive_losses":(2, 50),
        "max_daily_loss":        (1.0, 10000.0),
        "recovery_after_losses": (2, 20),
        "recovery_size_pct":     (0.1, 1.0),
        "recovery_exit_wins":    (1, 10),
        "min_tp_margin":         (0.01, 0.30),
        # bool fields validated separately below (skip range)
    }
    _BOOL_FIELDS = {
        "t1_enabled", "t2_enabled", "t3_enabled",
        "t4_enabled", "t5_enabled", "t6_enabled",
        "time1_enabled", "time2_enabled", "time3_enabled",
        "time4_enabled", "time5_enabled", "time6_enabled",
        "buy1_enabled",
        "market_5m_enabled", "market_15m_enabled", "arb5_enabled", "arb15_enabled",
        "cb_master_enabled", "cb_session_loss_enabled",
        "cb_daily_loss_enabled", "cb_consec_loss_enabled",
        "cb_api_error_enabled", "cb_recovery_enabled",
        "cb_loss_cooldown_enabled",
    }
    for k, v in data.items():
        if not hasattr(s, k):
            continue
        try:
            # Bool fields: accept true/false/1/0/"true"/"false" — strict cast
            if k in _BOOL_FIELDS:
                if isinstance(v, bool):
                    typed_v = v
                elif isinstance(v, (int, float)):
                    typed_v = bool(v)
                elif isinstance(v, str):
                    typed_v = v.strip().lower() in ("true", "1", "yes", "on")
                else:
                    log.warning("settings: %s=%r invalid bool, skip", k, v)
                    continue
                setattr(s, k, typed_v)
                continue
            typed_v = type(getattr(s, k))(v)
            if k in _VALID:
                lo, hi = _VALID[k]
                if not (lo <= typed_v <= hi):
                    log.warning("settings: %s=%r out of range [%s-%s], skip", k, typed_v, lo, hi)
                    continue
            setattr(s, k, typed_v)
        except (ValueError, TypeError) as e:
            log.warning("settings: skip %s=%r (%s)", k, v, e)
    s.market_5m_enabled = True
    s.market_15m_enabled = False
    s.arb5_enabled = False
    s.arb15_enabled = False
    for i in range(1, 7):
        time_min_secs = float(getattr(s, f"time{i}_min_secs_left"))
        time_max_secs = float(getattr(s, f"time{i}_max_secs_left"))
        if time_min_secs >= time_max_secs:
            raise ValueError(f"TIME-{i}: end seconds must be below buy-before seconds")
        seconds_max = float(getattr(s, f"t{i}_seconds_max"))
        seconds_min = float(getattr(s, f"t{i}_seconds_min"))
        min_price = float(getattr(s, f"t{i}_min_price"))
        max_price = float(getattr(s, f"t{i}_max_price"))
        if seconds_min >= seconds_max:
            raise ValueError(f"T{i}: end seconds must be below start seconds")
        if min_price > max_price:
            raise ValueError(f"T{i}: minimum price must not exceed maximum price")
    if float(s.buy1_min_secs_left) >= float(s.buy1_max_secs_left):
        raise ValueError("Buy-1: end seconds must be below start seconds")
    if float(s.buy1_min_price) > float(s.buy1_max_price):
        raise ValueError("Buy-1: minimum buy price must not exceed maximum buy price")
    if float(s.buy1_sell_min_price) > float(s.buy1_sell_max_price):
        raise ValueError("Buy-1: minimum sell price must not exceed maximum sell price")
    save_settings(s)
    log.info("Settings updated: %s", {k: v for k, v in data.items() if hasattr(s, k)})
    return _apply_env_setting_overrides(s)


# ─────────────────────────────────────────────────────────────────────────────
#  BALANCE (atomic writes + lock)
# ─────────────────────────────────────────────────────────────────────────────

_balance_lock = threading.Lock()


def load_balance() -> dict:
    data = _cached_read_json(BALANCE_FILE)
    if data and isinstance(data, dict) and "balance" in data:
        return data
    initial = float(os.getenv("INITIAL_BALANCE", "200.0"))
    data = {"balance": initial, "initial": initial,
            "total_deposited": initial, "last_updated": time.time()}
    save_balance(data)
    return data


def save_balance(data: dict):
    data["last_updated"] = time.time()
    _atomic_write(BALANCE_FILE, json.dumps(data, indent=2))
    _remember_json(BALANCE_FILE, data)


def get_balance() -> float:
    return load_balance().get("balance", 0.0)


def deduct_balance(amount: float) -> float:
    with _balance_lock:
        data = load_balance()
        used = min(amount, data["balance"])
        data["balance"] = round(data["balance"] - used, 4)
        save_balance(data)
        return used


def add_balance(amount: float) -> float:
    with _balance_lock:
        data = load_balance()
        data["balance"] = round(data["balance"] + amount, 4)
        save_balance(data)
        return data["balance"]


def deposit_balance(amount: float) -> float:
    with _balance_lock:
        data = load_balance()
        data["balance"]         = round(data["balance"] + amount, 4)
        data["total_deposited"] = round(data.get("total_deposited", 0) + amount, 4)
        save_balance(data)
        log.info("Deposit $%.2f | balance $%.2f", amount, data["balance"])
        return data["balance"]


# ─────────────────────────────────────────────────────────────────────────────
#  DAILY PNL TRACKER (UTC-based, auto-reset at midnight)
# ─────────────────────────────────────────────────────────────────────────────

DAILY_PNL_FILE = _DATA_DIR / "daily_pnl.json"


def _today_utc() -> str:
    """Return today's UTC date as YYYY-MM-DD string."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def load_daily_pnl() -> dict:
    """Load today's PnL. Auto-reset jika date berubah (UTC midnight crossing)."""
    data = _safe_read_json(DAILY_PNL_FILE)
    today = _today_utc()
    if not data or not isinstance(data, dict) or data.get("date") != today:
        data = {"date": today, "pnl": 0.0, "trades": 0,
                "wins": 0, "losses": 0, "halted": False}
        _atomic_write(DAILY_PNL_FILE, json.dumps(data, indent=2))
    return data


def update_daily_pnl(pnl: float, won: Optional[bool]) -> dict:
    """Append PnL ke daily tally. Return updated state."""
    data = load_daily_pnl()
    data["pnl"] = round(data["pnl"] + pnl, 4)
    data["trades"] += 1
    if won is True:
        data["wins"] += 1
    elif won is False:
        data["losses"] += 1
    _atomic_write(DAILY_PNL_FILE, json.dumps(data, indent=2))
    return data


def set_daily_halted(halted: bool):
    data = load_daily_pnl()
    data["halted"] = halted
    _atomic_write(DAILY_PNL_FILE, json.dumps(data, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
#  POSITION RECOVERY FILE (separate from trades.json)
# ─────────────────────────────────────────────────────────────────────────────
# Critical: jika bot crash mid-position, position file punya record lengkap
# untuk recovery saat startup. Sebelumnya hanya bergantung pada trades.json.

POSITION_FILE = _DATA_DIR / "open_position.json"


def save_open_position(rec_dict: dict):
    """Persist current open position untuk crash recovery."""
    if not rec_dict:
        clear_open_position()
        return
    _atomic_write(POSITION_FILE, json.dumps(rec_dict, indent=2))


def load_open_position() -> Optional[dict]:
    return _safe_read_json(POSITION_FILE)


def clear_open_position():
    if POSITION_FILE.exists():
        try:
            POSITION_FILE.unlink()
        except OSError:
            _atomic_write(POSITION_FILE, "{}")


# ─────────────────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    timestamp: float = 0.0
    window_ts: int = 0
    asset: str = "BTC"
    market_slug: str = ""
    condition_id: str = ""
    outcome: str = ""
    entry_price: float = 0.0
    shares: float = 0.0
    amount_usd: float = 0.0
    btc_open: float = 0.0
    btc_at_entry: float = 0.0
    btc_at_close: Optional[float] = None
    secs_elapsed: float = 0.0
    secs_left: float = 0.0
    btc_distance: float = 0.0
    trigger: str = ""
    trigger_reason: str = ""
    order_id: str = ""
    mock: bool = True
    exited_early: bool = False
    exit_price: float = 0.0
    exit_reason: str = ""
    exit_secs_left: float = 0.0
    won: Optional[bool] = None
    pnl: float = 0.0
    resolved: bool = False
    balance_returned: float = 0.0
    # ADAPTIVE: regime profile yang berlaku saat entry (untuk effective TP calc)
    entry_regime: str = ""
    tp_multiplier: float = 1.0
    c_component: str = ""
    pair_total_cost: float = 0.0
    target_shares: float = 0.0
    profit_if_up: float = 0.0
    profit_if_down: float = 0.0
    confidence: float = 0.0
    winner_side: str = ""
    loser_side: str = ""
    sell_loser_reason: str = ""
    mandatory_buy_reason: str = ""
    latency_ms: float = 0.0
    spread: float = 0.0
    liquidity: float = 0.0
    partial_fill: bool = False
    failed_fill: bool = False
    one_side_exposure: bool = False
    realized_pnl: float = 0.0
    unresolved_pnl: float = 0.0


@dataclass
class BotState:
    trading_enabled: bool = False
    bot_status: str = "waiting"
    status: str = "waiting"
    current_window: int = 0
    market_interval_secs: int = 300
    market_interval_label: str = "5m"
    seconds_left: float = 0.0
    secs_elapsed: float = 0.0
    btc_price: float = 0.0
    btc_open: float = 0.0
    btc_distance: float = 0.0
    up_price: float = 0.0
    down_price: float = 0.0
    # UPGRADE A: best_ask + spread untuk transparansi execution quality
    up_ask: float = 0.0
    down_ask: float = 0.0
    up_spread: float = 0.0      # ask - bid (0 = unknown)
    down_spread: float = 0.0    # ask - bid (0 = unknown)
    up_ask_depth: list = field(default_factory=list)
    down_ask_depth: list = field(default_factory=list)
    latency_ms: float = 0.0
    chainlink_age_secs: float = 0.0
    exchange_age_secs: float = 0.0
    clob_age_secs: float = 0.0
    price_feed_source: str = ""
    leading: str = ""
    market_question: str = ""
    mock_mode: bool = True
    balance: float = 0.0
    initial_balance: float = 0.0
    has_open_position: bool = False
    open_outcome: str = ""
    open_entry_price: float = 0.0
    open_shares: float = 0.0
    open_trigger: str = ""
    open_unrealized_pnl: float = 0.0
    open_amount_usd: float = 0.0
    open_peak_price: float = 0.0
    open_trail_active: bool = False
    # List of currently-open legs for dashboard display.
    # Each entry: {outcome, trigger, entry_price, shares, amount_usd, current_price,
    #              unrealized_pnl, window_ts, mock}
    open_legs: list = field(default_factory=list)
    trades_total: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    total_wagered: float = 0.0
    trades_this_window: int = 0
    max_trades_per_window: int = 9
    started_at: float = 0.0
    last_update: float = 0.0
    last_trade_time: float = 0.0
    session_pnl: float = 0.0
    consecutive_losses: int = 0
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str = ""
    recent_win_rate: float = 0.0
    edge_decay_alert: bool = False
    active_settings: dict = field(default_factory=dict)
    log_lines: list = field(default_factory=list)
    window_snapshots: list = field(default_factory=list)
    # EDGE: per-trigger expectancy (rolling) untuk dashboard visibility
    trigger_expectancy: dict = field(default_factory=dict)
    # ADAPTIVE: current regime info (untuk dashboard panel)
    current_regime: str = "UNKNOWN"
    regime_reason: str = ""
    regime_size_mult: float = 1.0
    regime_tp_mult: float = 1.0
    regime_allowed_triggers: list = field(default_factory=list)
    market_context_source: str = ""
    market_context_confidence: float = 0.0
    market_context_coverage_20m: float = 0.0
    delta_10s: float = 0.0
    delta_10s_avg_20m: float = 0.0
    delta_10s_abs_avg_20m: float = 0.0
    delta_10s_abs_p90_20m: float = 0.0
    trend_net_move_20m: float = 0.0
    trend_slope_per_min_20m: float = 0.0
    trend_efficiency_20m: float = 0.0
    saturation_avg_secs_30m: float | None = None
    saturation_samples_30m: int = 0
    locked_avg_secs_30m: float | None = None
    locked_samples_30m: int = 0
    saturation_completed_windows_30m: int = 0
    # Live balance (Polymarket account) — only used when MOCK_MODE=false
    # V2 collateral = pUSD (was USDC.e in V1, migrated 2026-04-22)
    live_cash: float = 0.0          # pUSD cash available on Polymarket V2
    live_portfolio: float = 0.0     # value of open positions (shares × price)
    live_total: float = 0.0         # cash + portfolio
    live_balance_ok: bool = False   # True if last fetch succeeded
    live_portfolio_ok: bool = False
    live_portfolio_source: str = ""
    ledger_balance: float = 0.0
    ledger_balance_drift: float = 0.0
    ledger_balance_ok: bool = True
    # ── Risk metrics (8/10 readiness) ───────────────────────────────────
    daily_pnl: float = 0.0          # today's PnL (UTC-reset at midnight)
    daily_trades: int = 0           # today's trade count
    daily_halted: bool = False      # True if max_daily_loss reached
    daily_halt_reason: str = ""
    in_recovery: bool = False       # post-loss-streak (informational)
    recovery_streak_wins: int = 0   # consecutive wins towards exit recovery
    api_error_rate: float = 0.0     # rolling % API errors (last 50 calls)


# ─────────────────────────────────────────────────────────────────────────────
#  STATE I/O
# ─────────────────────────────────────────────────────────────────────────────

def save_state(state: BotState):
    try:
        state.last_update = time.time()
        data = asdict(state)
        _atomic_write(STATE_FILE, json.dumps(data, indent=2))
        _remember_json(STATE_FILE, data)
    except Exception as e:
        log.debug("save_state error: %s", e)


def load_state() -> dict:
    data = _cached_read_json(STATE_FILE)
    if data and isinstance(data, dict):
        return data
    return asdict(BotState())


# ─────────────────────────────────────────────────────────────────────────────
#  CONTROL
# ─────────────────────────────────────────────────────────────────────────────

_CONTROL_FILE = _DATA_DIR / "bot_control.json"

def set_trading_enabled(enabled: bool):
    data = {"trading_enabled": enabled, "ts": time.time()}
    _atomic_write(_CONTROL_FILE, json.dumps(data))
    _remember_json(_CONTROL_FILE, data)

def get_trading_enabled() -> bool:
    data = _cached_read_json(_CONTROL_FILE)
    if data and isinstance(data, dict):
        return data.get("trading_enabled", False)
    return False


_EMERGENCY_FILE = _DATA_DIR / "emergency_stop.json"

def set_emergency_stop(active: bool):
    """Flag emergency stop. Bot akan force-close pending + halt."""
    data = {"active": active, "ts": time.time()}
    _atomic_write(_EMERGENCY_FILE, json.dumps(data))
    _remember_json(_EMERGENCY_FILE, data)

def get_emergency_stop() -> bool:
    data = _cached_read_json(_EMERGENCY_FILE)
    if data and isinstance(data, dict):
        return data.get("active", False)
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  CUMULATIVE STATS
# ─────────────────────────────────────────────────────────────────────────────

def _default_cum_stats() -> dict:
    return {
        "trades_total": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "total_wagered": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0,
        "early_exits": 0,
    }

def load_cum_stats() -> dict:
    defaults = _default_cum_stats()
    data = _safe_read_json(CUM_STATS_FILE)
    if data and isinstance(data, dict):
        defaults.update(data)
    return defaults

def save_cum_stats(cs: dict):
    _atomic_write(CUM_STATS_FILE, json.dumps(cs, indent=2))

def record_cum_trade(trade: dict):
    cs = load_cum_stats()
    cs["trades_total"] += 1
    cs["total_wagered"] = round(cs["total_wagered"] + trade.get("amount_usd", 0), 2)
    if trade.get("resolved"):
        pnl = trade.get("pnl", 0)
        cs["total_pnl"] = round(cs["total_pnl"] + pnl, 4)
        if trade.get("won") is True:
            cs["wins"] += 1
        elif trade.get("won") is False:
            cs["losses"] += 1
        # won=None (draw/reconcile) → tidak masuk wins/losses
        cs["best_trade"]  = max(cs["best_trade"], pnl)
        cs["worst_trade"] = min(cs["worst_trade"], pnl)
        if trade.get("exited_early"):
            cs["early_exits"] += 1
    save_cum_stats(cs)


def rebuild_cum_stats(trades: list[dict] | None = None) -> dict:
    """Rebuild cumulative stats from trades.json after manual corrections."""
    trades = trades if trades is not None else load_trades()
    cs = _default_cum_stats()
    for t in trades:
        amount = float(t.get("amount_usd") or 0.0)
        cs["trades_total"] += 1
        cs["total_wagered"] = round(cs["total_wagered"] + amount, 2)
        if t.get("resolved"):
            pnl = float(t.get("pnl") or 0.0)
            cs["total_pnl"] = round(cs["total_pnl"] + pnl, 4)
            if t.get("won") is True:
                cs["wins"] += 1
            elif t.get("won") is False:
                cs["losses"] += 1
            cs["best_trade"] = max(cs["best_trade"], pnl)
            cs["worst_trade"] = min(cs["worst_trade"], pnl)
            if t.get("exited_early"):
                cs["early_exits"] += 1
    save_cum_stats(cs)
    return cs


def rebuild_daily_pnl(trades: list[dict] | None = None) -> dict:
    """Rebuild today's daily PnL from resolved trades after corrections."""
    trades = trades if trades is not None else load_trades()
    today = _today_utc()
    data = {"date": today, "pnl": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "halted": False}
    old = _safe_read_json(DAILY_PNL_FILE)
    if isinstance(old, dict) and old.get("date") == today:
        data["halted"] = bool(old.get("halted", False))
    for t in trades:
        if not t.get("resolved"):
            continue
        ts = float(t.get("timestamp") or 0.0)
        if ts <= 0 or time.strftime("%Y-%m-%d", time.gmtime(ts)) != today:
            continue
        won = t.get("won")
        data["pnl"] = round(data["pnl"] + float(t.get("pnl") or 0.0), 4)
        data["trades"] += 1
        if won is True:
            data["wins"] += 1
        elif won is False:
            data["losses"] += 1
    _atomic_write(DAILY_PNL_FILE, json.dumps(data, indent=2))
    return data


def calc_trade_ledger_balance(trades: list[dict] | None = None,
                              balance: dict | None = None) -> dict:
    """Expected mock cash from trade ledger.

    In MOCK mode, balance.json must equal initial cash minus every opened stake
    plus every resolved return. This catches file/cache/process drift without
    changing live-account accounting.
    """
    trades = trades if trades is not None else load_trades()
    balance = balance if balance is not None else load_balance()
    initial = float(balance.get("initial", 0.0) or 0.0)
    deposited = float(balance.get("total_deposited", initial) or initial)
    cash = float(balance.get("balance", 0.0) or 0.0)
    total_amount = round(sum(float(t.get("amount_usd") or 0.0) for t in trades), 4)
    returned = round(
        sum(float(t.get("balance_returned") or 0.0)
            for t in trades if t.get("resolved")),
        4,
    )
    expected = round(deposited - total_amount + returned, 4)
    drift = round(cash - expected, 4)
    open_amount = round(
        sum(float(t.get("amount_usd") or 0.0)
            for t in trades if not t.get("resolved")),
        4,
    )
    return {
        "initial": initial,
        "total_deposited": deposited,
        "balance": cash,
        "ledger_balance": expected,
        "ledger_balance_drift": drift,
        "ledger_balance_ok": abs(drift) <= 0.01,
        "open_amount": open_amount,
        "total_amount": total_amount,
        "returned": returned,
    }


def reconcile_mock_balance_from_trades(reason: str = "manual") -> dict:
    """Repair mock balance.json from trades.json and return before/after."""
    trades = load_trades()
    data = load_balance()
    ledger = calc_trade_ledger_balance(trades, data)
    old_balance = float(data.get("balance", 0.0) or 0.0)
    data["balance"] = ledger["ledger_balance"]
    save_balance(data)
    log.warning(
        "Mock balance reconciled from trades (%s): $%.4f -> $%.4f drift=$%+.4f",
        reason, old_balance, ledger["ledger_balance"], ledger["ledger_balance_drift"],
    )
    ledger["old_balance"] = old_balance
    ledger["new_balance"] = ledger["ledger_balance"]
    return ledger


def _hold_close_values(t: dict, actual: str) -> tuple[bool, float, float]:
    actual = str(actual or "").upper()
    won = str(t.get("outcome") or "").upper() == actual
    entry = float(t.get("entry_price") or 0.0)
    shares = float(t.get("shares") or 0.0)
    amount = float(t.get("amount_usd") or 0.0)
    if won:
        pnl = round((1.0 - entry) * shares, 4)
        returned = round(amount + pnl, 4)
    else:
        pnl = round(-amount, 4)
        returned = 0.0
    return won, pnl, returned


def apply_gamma_actual_correction(window_ts: int, market_slug: str,
                                  actual: str, source: str = "gamma-refresh",
                                  triggers: tuple[str, ...] = ("END_WINDOW",),
                                  final_price: float = 0.0,
                                  price_to_beat: float = 0.0) -> dict:
    """Correct hold-close records using Gamma official/implied outcome.

    Returns summary including balance delta. Early-sold legs keep realized sell PnL.
    """
    actual = str(actual or "").upper()
    if actual not in ("UP", "DOWN"):
        return {"changed": 0, "resolved": 0, "balance_delta": 0.0,
                "error": f"invalid actual={actual}"}
    valid_triggers = {str(t or "").upper() for t in triggers}
    trades = load_trades()
    changed = 0
    resolved_now = 0
    metadata_updated = 0
    balance_delta = 0.0
    details = []
    for t in trades:
        trig = str(t.get("trigger") or "").upper()
        if (int(t.get("window_ts") or 0) != int(window_ts)
                or trig not in valid_triggers
                or str(t.get("market_slug") or "") != str(market_slug or "")):
            continue
        old_actual = str(t.get("actual") or "").upper()
        old_won = t.get("won")
        old_pnl = float(t.get("pnl") or 0.0)
        old_returned = float(t.get("balance_returned") or 0.0)
        old_close = float(t.get("btc_at_close") or 0.0)
        old_target = float(t.get("resolution_price_to_beat") or 0.0)
        old_source = str(t.get("resolution_source") or "")
        t["actual"] = actual
        if t.get("exited_early"):
            # Sold positions are already realized at sell price; only annotate outcome.
            if old_actual != actual:
                t["correction_note"] = f"{source}: actual={actual}; early sell pnl unchanged"
                changed += 1
                details.append({"outcome": t.get("outcome"), "early": True,
                                "old_actual": old_actual, "new_actual": actual,
                                "pnl_delta": 0.0, "return_delta": 0.0})
            continue

        won, pnl, returned = _hold_close_values(t, actual)
        if not t.get("resolved"):
            resolved_now += 1
            t["resolved_ts"] = time.time()
        t["resolved"] = True
        t["won"] = won
        t["pnl"] = pnl
        t["balance_returned"] = returned
        if final_price and final_price > 0:
            t["btc_at_close"] = float(final_price)
        if price_to_beat and price_to_beat > 0:
            t["resolution_price_to_beat"] = float(price_to_beat)
        t["resolution_source"] = source
        if (
            (final_price > 0 and old_close != float(final_price))
            or (price_to_beat > 0 and old_target != float(price_to_beat))
            or old_source != source
        ):
            metadata_updated += 1
        t["exit_price"] = 0.0
        t["exit_reason"] = f"hold_close actual={actual}"
        t["correction_note"] = (
            f"{source}: actual {old_actual or '?'}->{actual}; "
            f"pnl {old_pnl:+.4f}->{pnl:+.4f}"
        )
        return_delta = returned - old_returned
        balance_delta = round(balance_delta + return_delta, 4)
        if old_actual != actual or old_won is not won or round(old_pnl, 4) != pnl:
            changed += 1
        details.append({"outcome": t.get("outcome"), "early": False,
                        "old_actual": old_actual, "new_actual": actual,
                        "old_pnl": old_pnl, "new_pnl": pnl,
                        "pnl_delta": round(pnl - old_pnl, 4),
                        "return_delta": round(return_delta, 4)})

    if changed or resolved_now or metadata_updated:
        save_trades(trades)
        if balance_delta:
            add_balance(balance_delta)
        rebuild_cum_stats(trades)
        rebuild_daily_pnl(trades)
    return {"changed": changed, "resolved": resolved_now,
            "metadata_updated": metadata_updated,
            "balance_delta": round(balance_delta, 4), "details": details}


# ─────────────────────────────────────────────────────────────────────────────
#  TRADE I/O
# ─────────────────────────────────────────────────────────────────────────────

def save_trade(trade: TradeRecord):
    trades = load_trades()
    trades.append(asdict(trade))
    if len(trades) > 50000:
        trades = trades[-50000:]
    _atomic_write(TRADES_FILE, json.dumps(trades, indent=2))
    _remember_json(TRADES_FILE, trades)


def save_trades(trades: list):
    """Atomic write seluruh trades list — prevents corruption on crash.
    Replaces direct TRADES_FILE.write_text() calls in main.py."""
    _atomic_write(TRADES_FILE, json.dumps(trades, indent=2))
    _remember_json(TRADES_FILE, trades)


def load_trades() -> list[dict]:
    data = _cached_read_json(TRADES_FILE)
    if data and isinstance(data, list):
        return data
    return []

def resolve_trade_immediately(window_ts: int, exit_price: float,
                               exit_reason: str, secs_left: float = 0.0) -> dict | None:
    trades = load_trades()
    resolved_trade = None
    for t in trades:
        if t.get("window_ts") == window_ts and not t.get("resolved") and not t.get("exited_early"):
            t["exited_early"]   = True
            t["exit_price"]     = exit_price
            t["exit_reason"]    = exit_reason
            t["exit_secs_left"] = secs_left
            t["resolved"]       = True
            t["resolved_ts"]    = time.time()
            raw_pnl = (exit_price - t["entry_price"]) * t["shares"]
            t["pnl"] = round(raw_pnl, 4)
            t["won"] = t["pnl"] > 0
            returned = round(t["amount_usd"] + raw_pnl, 4)
            t["balance_returned"] = max(returned, 0.0)
            resolved_trade = t
            record_cum_trade(t)
            tag = "WIN" if t["won"] else "LOSS"
            log.info("Resolved: window=%d %s pnl=$%.4f returned=$%.4f",
                     window_ts, tag, t["pnl"], t["balance_returned"])
            break
    _atomic_write(TRADES_FILE, json.dumps(trades, indent=2))
    _remember_json(TRADES_FILE, trades)
    return resolved_trade


def resolve_specific_leg(window_ts: int, outcome: str, exit_price: float,
                          exit_reason: str, secs_left: float = 0.0,
                          market_slug: str = "",
                          order_id: str = "") -> dict | None:
    """Resolve one specific leg by outcome in a window."""
    trades = load_trades()
    resolved_trade = None
    for t in trades:
        if (t.get("window_ts") == window_ts
                and t.get("outcome") == outcome
                and (not market_slug or t.get("market_slug") == market_slug)
                and (not order_id or t.get("order_id") == order_id)
                and not t.get("resolved")
                and not t.get("exited_early")):
            t["exited_early"]   = True
            t["exit_price"]     = exit_price
            t["exit_reason"]    = exit_reason
            t["exit_secs_left"] = secs_left
            t["resolved"]       = True
            t["resolved_ts"]    = time.time()
            raw_pnl = (exit_price - t["entry_price"]) * t["shares"]
            t["pnl"] = round(raw_pnl, 4)
            t["won"] = t["pnl"] > 0
            returned = round(t["amount_usd"] + raw_pnl, 4)
            t["balance_returned"] = max(returned, 0.0)
            resolved_trade = t
            record_cum_trade(t)
            tag = "WIN" if t["won"] else "LOSS"
            log.info("Resolved leg: window=%d %s %s pnl=$%.4f returned=$%.4f",
                     window_ts, outcome, tag, t["pnl"], t["balance_returned"])
            break
    _atomic_write(TRADES_FILE, json.dumps(trades, indent=2))
    _remember_json(TRADES_FILE, trades)
    return resolved_trade

def update_trade_exit(window_ts: int, exit_price: float,
                      exit_reason: str, secs_left: float = 0.0):
    return resolve_trade_immediately(window_ts, exit_price, exit_reason, secs_left)

def _actual_outcome(btc_open: float, btc_close: float) -> str:
    return "UP" if btc_close >= btc_open else "DOWN"

def _apply_hold_close_result(t: dict, won: bool, btc_close: float,
                             exit_reason: str = "", actual: str = ""):
    t["btc_at_close"] = btc_close
    t["resolved"]     = True
    t["resolved_ts"]  = time.time()
    actual = str(actual or "").upper()
    if not actual and "actual=" in str(exit_reason or ""):
        actual = str(exit_reason).split("actual=", 1)[1].split()[0].strip().upper()
    if actual in ("UP", "DOWN"):
        t["actual"] = actual
    if exit_reason:
        t["exit_reason"] = exit_reason
    if not t.get("exited_early"):
        t["won"] = won
        if won:
            raw_pnl = round((1.0 - t["entry_price"]) * t["shares"], 4)
            t["pnl"] = raw_pnl
            t["balance_returned"] = round(t["amount_usd"] + raw_pnl, 4)
        else:
            t["pnl"] = round(-t["amount_usd"], 4)
            t["balance_returned"] = 0.0

def update_trade_result(window_ts: int, won: bool, btc_close: float):
    """Resolve trade at window close (hold-to-close path).

    CRITICAL FIX: NO LOSS CAP on hold-close.
    On Polymarket binary markets, a LOSING position resolves to $0 per share —
    you lose the FULL amount_usd you put in. The previous "cap at max_loss_per_trade"
    was creating phantom cash: it pretended loss was -$0.50 when actual loss was -$5.50,
    causing balance_returned to add $5.00 of fake money per losing hold-close.
    SL cap only meaningful on EARLY EXIT (where bot can sell). Hold-close = full loss, period.
    """
    trades = load_trades()
    changed = False
    # Skip directional entries; those are resolved with full binary settlement.
    _self_resolved_triggers = {"END_WINDOW"}
    for t in trades:
        if t.get("window_ts") == window_ts and not t.get("resolved"):
            if str(t.get("trigger") or "").upper() in _self_resolved_triggers:
                continue
            _apply_hold_close_result(t, won, btc_close)
            changed = True
            tag = "WIN" if t.get("won") else "LOSS"
            log.info("Hold-to-close: window=%d %s pnl=$%.4f returned=$%.4f",
                     window_ts, tag, t.get("pnl", 0), t.get("balance_returned", 0))
            record_cum_trade(t)
    if changed:
        _atomic_write(TRADES_FILE, json.dumps(trades, indent=2))
        _remember_json(TRADES_FILE, trades)
    return trades

def update_directional_results(window_ts: int, btc_close: float,
                               market_slug: str = "",
                               triggers: tuple[str, ...] = ("END_WINDOW",),
                               actual: str = "",
                               price_to_beat: float = 0.0,
                               resolution_source: str = "") -> list[dict]:
    """Resolve open directional (single-side) legs at window close.

    End-window trades are single-side and tagged with their strategy code; if
    ``update_trade_result`` handled them, a losing hold-close could use legacy
    cap-bounded loss logic instead of full binary settlement loss.

    ``actual`` overrides the close-price-derived outcome when an official
    UP/DOWN label is available. ``price_to_beat`` records Polymarket's
    Chainlink target when Gamma event metadata provides it.
    """
    trades = load_trades()
    resolved: list[dict] = []
    changed = False
    valid_triggers = {str(t).upper() for t in (triggers or ())}
    actual_norm = str(actual or "").upper()
    for t in trades:
        trig = str(t.get("trigger") or "").upper()
        if (t.get("window_ts") == window_ts
                and trig in valid_triggers
                and (not market_slug or t.get("market_slug") == market_slug)
                and not t.get("resolved")
                and not t.get("exited_early")):
            outcome_actual = (
                actual_norm if actual_norm in ("UP", "DOWN")
                else _actual_outcome(float(t.get("btc_open", 0) or 0), btc_close)
            )
            won = (str(t.get("outcome") or "").upper() == outcome_actual)
            _apply_hold_close_result(
                t, won, btc_close, f"hold_close actual={outcome_actual}", outcome_actual)
            if price_to_beat and price_to_beat > 0:
                t["resolution_price_to_beat"] = float(price_to_beat)
            if resolution_source:
                t["resolution_source"] = str(resolution_source)
            changed = True
            resolved.append(dict(t))
            tag = "WIN" if won else "LOSS"
            log.info("Directional close: window=%d trig=%s %s %s pnl=$%.4f returned=$%.4f",
                     window_ts, trig, t.get("outcome", "?"), tag,
                     t.get("pnl", 0), t.get("balance_returned", 0))
            record_cum_trade(t)
    if changed:
        _atomic_write(TRADES_FILE, json.dumps(trades, indent=2))
        _remember_json(TRADES_FILE, trades)
    return resolved


def calc_stats(trades: list[dict]) -> dict:
    """Return cumulative stats + recent win rate for decay monitoring."""
    cs = load_cum_stats()
    resolved = cs["wins"] + cs["losses"]

    # Gunakan parameter trades, bukan load ulang dari disk
    recent_resolved = [t for t in trades if t.get("resolved") and t.get("won") is not None][-50:]
    recent_wins = sum(1 for t in recent_resolved if t.get("won"))
    recent_total = len(recent_resolved)
    recent_wr = (recent_wins / recent_total * 100) if recent_total >= 10 else 0.0

    return {
        "trades_total":  cs["trades_total"],
        "resolved":      resolved,
        "wins":          cs["wins"],
        "losses":        cs["losses"],
        "win_rate":      cs["wins"] / resolved * 100 if resolved else 0,
        "total_pnl":     round(cs["total_pnl"], 2),
        "total_wagered": round(cs["total_wagered"], 2),
        "avg_pnl":       round(cs["total_pnl"] / resolved, 4) if resolved else 0,
        "best_trade":    cs["best_trade"],
        "worst_trade":   cs["worst_trade"],
        "early_exits":   cs["early_exits"],
        "recent_win_rate": round(recent_wr, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SNAPSHOTS
# ─────────────────────────────────────────────────────────────────────────────

def save_snapshot(window_ts: int, secs_left: float, secs_elapsed: float,
                  up_price: float, down_price: float,
                  btc_price: float, btc_open: float,
                  btc_distance: float, leading: str,
                  up_spread: float | None = None,
                  down_spread: float | None = None):
    snaps = load_snapshots()
    leading_spread = up_spread if str(leading).upper() == "UP" else down_spread
    snaps.append({
        "window_ts": window_ts, "secs_left": round(secs_left, 1),
        "secs_elapsed": round(secs_elapsed, 1),
        "up_price": round(up_price, 4), "down_price": round(down_price, 4),
        "btc_price": round(btc_price, 2), "btc_open": round(btc_open, 2),
        "btc_distance": round(btc_distance, 2), "leading": leading,
        "leading_price": round(max(up_price, down_price), 4),
        "up_spread": round(up_spread, 4) if up_spread and up_spread > 0 else None,
        "down_spread": round(down_spread, 4) if down_spread and down_spread > 0 else None,
        "leading_spread": (
            round(leading_spread, 4)
            if leading_spread and leading_spread > 0 else None
        ),
        "timestamp": time.time(),
    })
    if len(snaps) > SNAPSHOT_MAX_ROWS:
        snaps = snaps[-SNAPSHOT_MAX_ROWS:]
    _atomic_write(SNAPSHOTS_FILE, json.dumps(snaps))
    _remember_json(SNAPSHOTS_FILE, snaps)
    record_price_extremes(window_ts, up_price, down_price)

def load_snapshots() -> list[dict]:
    data = _cached_read_json(SNAPSHOTS_FILE)
    if data and isinstance(data, list):
        return data
    return []


def load_price_extremes() -> dict:
    data = _cached_read_json(PRICE_EXTREMES_FILE)
    if isinstance(data, dict) and isinstance(data.get("windows"), list):
        return data
    return {"price_source": "best_bid", "sample_interval_secs": 1.0, "windows": []}


def _update_price_extreme_window(windows: list[dict], window_ts: int,
                                 up_price: float, down_price: float,
                                 observed_ts: float) -> dict:
    window = next(
        (row for row in windows if int(row.get("window_ts") or 0) == int(window_ts)),
        None,
    )
    if window is None:
        window = {
            "window_ts": int(window_ts),
            "market_slug": f"btc-updown-5m-{int(window_ts)}",
            "max_up_bid": 0.0,
            "max_down_bid": 0.0,
            "min_up_bid": 0.0,
            "min_down_bid": 0.0,
            "first_observed_ts": observed_ts,
            "last_observed_ts": observed_ts,
            "hits": {},
            "low_hits": {
                f"{LOW_PRICE_WIN_THRESHOLD:.2f}": {
                    "up_ts": None,
                    "down_ts": None,
                },
            },
        }
        windows.append(window)
    up = max(0.0, float(up_price or 0.0))
    down = max(0.0, float(down_price or 0.0))
    window["max_up_bid"] = round(max(float(window.get("max_up_bid") or 0.0), up), 4)
    window["max_down_bid"] = round(max(float(window.get("max_down_bid") or 0.0), down), 4)
    current_min_up = float(window.get("min_up_bid") or 0.0)
    current_min_down = float(window.get("min_down_bid") or 0.0)
    if up > 0:
        window["min_up_bid"] = round(min(current_min_up, up) if current_min_up > 0 else up, 4)
    if down > 0:
        window["min_down_bid"] = round(min(current_min_down, down) if current_min_down > 0 else down, 4)
    window["last_observed_ts"] = observed_ts
    hits = window.setdefault("hits", {})
    for level in PRICE_EXTREME_LEVELS:
        key = f"{level:.2f}"
        hit = hits.setdefault(key, {"up_ts": None, "down_ts": None})
        if up >= level and hit.get("up_ts") is None:
            hit["up_ts"] = observed_ts
        if down >= level and hit.get("down_ts") is None:
            hit["down_ts"] = observed_ts
    low_hit = window.setdefault("low_hits", {}).setdefault(
        f"{LOW_PRICE_WIN_THRESHOLD:.2f}",
        {"up_ts": None, "down_ts": None},
    )
    if 0 < up <= LOW_PRICE_WIN_THRESHOLD and low_hit.get("up_ts") is None:
        low_hit["up_ts"] = observed_ts
    if 0 < down <= LOW_PRICE_WIN_THRESHOLD and low_hit.get("down_ts") is None:
        low_hit["down_ts"] = observed_ts
    return window


def record_price_extremes(window_ts: int, up_price: float, down_price: float,
                          observed_ts: float | None = None) -> dict:
    """Persist per-window extreme bid hits without retaining every tick."""
    ts = float(observed_ts or time.time())
    data = load_price_extremes()
    windows = data["windows"]
    if not windows and not PRICE_EXTREMES_FILE.exists():
        for snapshot in load_snapshots():
            _update_price_extreme_window(
                windows,
                int(snapshot.get("window_ts") or 0),
                float(snapshot.get("up_price") or 0.0),
                float(snapshot.get("down_price") or 0.0),
                float(snapshot.get("timestamp") or ts),
            )
    window = _update_price_extreme_window(windows, window_ts, up_price, down_price, ts)

    if len(windows) > PRICE_EXTREME_MAX_WINDOWS:
        data["windows"] = windows[-PRICE_EXTREME_MAX_WINDOWS:]
    data["price_source"] = "best_bid"
    data["sample_interval_secs"] = float(os.getenv("SNAPSHOT_INTERVAL", "1.0"))
    _atomic_write(PRICE_EXTREMES_FILE, json.dumps(data, indent=2))
    _remember_json(PRICE_EXTREMES_FILE, data)
    return window
