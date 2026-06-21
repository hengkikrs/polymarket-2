"""
tracker.py — Trigger Tracker + Research Dashboard (localhost:5005)
==================================================================
Versi intra-window: analisis T1, T2 + exit stats

Usage:  python tracker.py
        http://localhost:5005
"""
import json, asyncio, time, logging
from datetime import datetime
from aiohttp import web
import core.state as st
from strategies import end_window

log = logging.getLogger("tracker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")

PORT = 5005

def build_end_window_meta(settings: st.BotSettings) -> dict:
    colors = {
        "REVERSE": "#f97316",
        "BUY-1": "#22c55e",
        "TIME-1": "#d946ef", "TIME-2": "#ec4899", "TIME-3": "#c084fc",
        "TIME-4": "#8b5cf6", "TIME-5": "#6366f1", "TIME-6": "#0ea5e9",
        "T1": "#3fb950", "T2": "#58a6ff", "T3": "#f0a000",
        "T4": "#a371f7", "T5": "#ff7b72", "T6": "#ffa657",
    }
    meta = {}
    meta["REVERSE"] = {
        "name": "REVERSE",
        "time": "After delta crosses target",
        "price": "<= 0.20",
        "req": "Opposite ask liquidity >= $50",
        "risk": "emergency",
        "color": colors["REVERSE"],
        "desc": "Maximum two opposite-side emergency entries per window",
    }
    meta["BUY-1"] = {
        "name": "BUY-1",
        "time": f"{settings.buy1_max_secs_left:g}-{settings.buy1_min_secs_left:g}s left",
        "price": f"buy {settings.buy1_min_price:.2f}-{settings.buy1_max_price:.2f}",
        "req": f"BTC delta >= ${settings.buy1_min_delta_usd:g}; sell {settings.buy1_sell_min_price:.2f}-{settings.buy1_sell_max_price:.2f}",
        "risk": "medium",
        "color": colors["BUY-1"],
        "desc": "Quick buy / quick sell momentum scalp",
    }
    for i in range(1, 7):
        trigger = f"TIME-{i}"
        meta[trigger] = {
            "name": trigger,
            "time": f"Any time above {getattr(settings, f'time{i}_min_secs_left'):g}s",
            "price": f"exactly {getattr(settings, f'time{i}_price'):.2f}",
            "req": (
                f"BTC delta >= ${getattr(settings, f'time{i}_min_delta_usd'):g}; "
                f"visible ask liquidity >= ${getattr(settings, f'time{i}_trade_usd'):g}"
            ),
            "risk": "highest",
            "color": colors[trigger],
            "desc": "Configurable exact-price FOK entry with aligned BTC delta gate",
        }
    cfg = end_window.EndWindowConfig.from_settings(settings)
    for layer in sorted(cfg.layers, key=lambda item: item.seconds_left_max, reverse=True):
        meta[layer.name] = {
            "name": layer.name,
            "time": f"{layer.seconds_left_max:g}-{layer.seconds_left_min:g}s left",
            "price": f"{layer.min_price:.2f}-{layer.max_price:.2f}",
            "req": f"BTC delta >= ${layer.min_distance_usd:g}",
            "risk": "highest" if layer.name == "T6" else "high" if layer.name in {"T4", "T5"} else "medium",
            "color": colors[layer.name],
            "desc": "Configurable end-window confirmation layer",
        }
    return meta


END_WINDOW_META = build_end_window_meta(st.load_settings())

TRIGGER_REASON_LAYERS = (
    ("BUY-1", "BUY-1"),
    ("REVERSE", "REVERSE"),
    ("TIME-6", "TIME-6"),
    ("TIME-5", "TIME-5"),
    ("TIME-4", "TIME-4"),
    ("TIME-3", "TIME-3"),
    ("TIME-2", "TIME-2"),
    ("TIME-1", "TIME-1"),
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
)


def trade_layer(trade: dict) -> str:
    reason = str(trade.get("trigger_reason") or "")
    for marker, layer in TRIGGER_REASON_LAYERS:
        if marker in reason:
            return layer
    trigger = str(trade.get("trigger") or "").upper()
    if trigger in END_WINDOW_META:
        return trigger
    try:
        secs_left = float(trade.get("secs_left") or 0.0)
    except (TypeError, ValueError):
        return ""
    for upper, lower, layer in (
        (25.0, 20.0, "T1"),
        (20.0, 15.0, "T2"),
        (15.0, 10.0, "T3"),
        (10.0, 7.0, "T4"),
        (7.0, 5.0, "T5"),
        (5.0, 4.0, "T6"),
    ):
        if lower < secs_left <= upper:
            return layer
    return ""


def build_trigger_calendar(trades: list[dict]) -> dict:
    days: dict[str, dict] = {}
    for trade in trades:
        if not trade.get("resolved"):
            continue
        layer = trade_layer(trade)
        if layer not in END_WINDOW_META:
            continue
        try:
            timestamp = float(trade.get("resolved_ts") or trade.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            continue
        if timestamp <= 0:
            continue
        date_key = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        day = days.setdefault(date_key, {
            "date": date_key,
            "resolved": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "triggers": {
                trigger: {"resolved": 0, "wins": 0, "losses": 0, "win_rate": 0.0}
                for trigger in END_WINDOW_META
            },
        })
        won = trade.get("won")
        trigger_stats = day["triggers"][layer]
        day["resolved"] += 1
        trigger_stats["resolved"] += 1
        if won is True:
            day["wins"] += 1
            trigger_stats["wins"] += 1
        elif won is False:
            day["losses"] += 1
            trigger_stats["losses"] += 1

    for day in days.values():
        day["win_rate"] = (
            round(day["wins"] / day["resolved"] * 100, 1)
            if day["resolved"] else 0.0
        )
        for trigger_stats in day["triggers"].values():
            trigger_stats["win_rate"] = (
                round(trigger_stats["wins"] / trigger_stats["resolved"] * 100, 1)
                if trigger_stats["resolved"] else 0.0
            )
    return {"days": [days[key] for key in sorted(days)]}

TRIGGER_META = {
    "T1": {
        "name":"T1", "time":"5-50s (sniper)", "price":"0.50-0.62",
        "req":"D>=$8 + BTC confirm + momentum", "risk":"medium", "color":"#3fb950",
        "desc":"Sniper — harga masih murah, R:R terbaik",
        "tp":"0.58", "sl":"-$7",
    },
    "T2": {
        "name":"T2", "time":"40-100s (early)", "price":"0.52-0.70",
        "req":"D>=$10 + BTC confirm + momentum", "risk":"safe", "color":"#58a6ff",
        "desc":"Early — konfirmasi awal arah BTC",
        "tp":"0.75", "sl":"-$7",
    },
    "T3": {
        "name":"T3", "time":"80-165s (momentum)", "price":"0.55-0.78",
        "req":"D>=$10 + BTC confirm + momentum", "risk":"safe", "color":"#f0a000",
        "desc":"Momentum — BTC maintain arah, momentum terbangau",
        "tp":"0.82", "sl":"-$7",
    },
    "T4": {
        "name":"T4", "time":"140-220s (mid)", "price":"0.58-0.82",
        "req":"D>=$15 + BTC confirm + momentum", "risk":"medium", "color":"#a371f7",
        "desc":"Mid-window — harga sudah refleksi arah",
        "tp":"0.86", "sl":"-$7",
    },
    "T5": {
        "name":"T5", "time":"200-265s (confirm)", "price":"0.65-0.88",
        "req":"D>=$20 + BTC confirm", "risk":"medium", "color":"#ff7b72",
        "desc":"Late confirm — BTC sudah dominan, delta ketat",
        "tp":"0.92", "sl":"-$7",
    },
    "T6": {
        "name":"T6", "time":"100-150s (reversal)", "price":"0.50-0.62",
        "req":"D>=$5 + BTC REVERSE", "risk":"high", "color":"#ffa657",
        "desc":"Reversal — counter-trend, harga sisi baru murah",
        "tp":"0.70", "sl":"-$7",
    },
    "T7": {
        "name":"T7", "time":"250-280s (scalp)", "price":"0.70-0.93",
        "req":"D>=$20 + BTC confirm", "risk":"high", "color":"#79c0ff",
        "desc":"Scalp — entry cepat di akhir window",
        "tp":"0.95", "sl":"-$7",
    },
    "TX": {
        "name":"TX", "time":"297-300s (last)", "price":">0.50",
        "req":"NONE (wajib)", "risk":"highest", "color":"#f85149",
        "desc":"Last-second — WAJIB beli, guaranteed 1 trade/window",
        "tp":"0.99", "sl":"-$7",
    },
}

# ── Strategy B trigger metadata (matches strategy_b.py B1-B5 spec v2) ───────
STRATEGY_B_META = {
    "B1": {
        "name":"B1", "time":"5-15s left (late)", "price":"0.85-0.93",
        "req":"D>=$15 + velocity 5s >= 0.003/s",
        "risk":"high", "color":"#ff6ec7",
        "desc":"Late Momentum Sweep — vel 5s + price band tinggi",
        "tp":"hold/0.99", "sl":"cfg",
    },
    "B2": {
        "name":"B2", "time":"20-40s left (vel 3s)", "price":"0.78-0.90",
        "req":"D>=$25 + velocity 3s >= 0.004/s",
        "risk":"high", "color":"#56d4dd",
        "desc":"Short-velocity Entry — vel 3s sensitif",
        "tp":"hold/0.99", "sl":"cfg",
    },
    "B3": {
        "name":"B3", "time":"25-50s left (vel)", "price":"0.85-0.95",
        "req":"D>=$40 + velocity 5s + retrace <40%",
        "risk":"medium", "color":"#bb86fc",
        "desc":"Velocity Confirmation — strong delta, momentum",
        "tp":"hold/0.99", "sl":"cfg",
    },
    "B4": {
        "name":"B4", "time":"15-45s left (arb)", "price":"0.50-0.80",
        "req":"D>=$25 + edge (fair_p - implied) >= 0.07",
        "risk":"medium", "color":"#ffd166",
        "desc":"Probabilistic Arbitrage — logistic regression mispricing",
        "tp":"hold/0.99", "sl":"cfg",
    },
    "B5": {
        "name":"B5", "time":"0-5s left (mandatory)", "price":"any",
        "req":"WAJIB 1 trade/window — outcome auto from BTC vs open",
        "risk":"highest", "color":"#06d6a0",
        "desc":"Mandatory Last-Second — abaikan price, ikuti BTC direction",
        "tp":"hold-to-close", "sl":"cfg",
    },
}

RISK_EMOJI = {"safest":"🟢","safe":"🟢","medium":"🟡","high":"🟠","highest":"🔴"}

HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>END_WINDOW Trigger Performance</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e17;color:#e0e6ed;font-family:'SF Mono','Fira Code',monospace;font-size:13px}
.hdr{background:linear-gradient(135deg,#1a1f2e,#0d1117);padding:14px 24px;border-bottom:1px solid #1e2d3d;display:flex;justify-content:space-between;align-items:center}
.hdr h1{font-size:18px;color:#58a6ff}.hdr .sub{color:#8b949e;font-size:12px}
.ct{padding:16px 24px}

.timer-bar{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:16px;margin-bottom:16px;display:flex;align-items:center;gap:20px}
.timer-bar .clock{font-size:36px;font-weight:700;font-variant-numeric:tabular-nums;min-width:80px}
.elapsed-tag{font-size:11px;color:#8b949e;margin-top:2px}
.timer-bar .pbar{flex:1;height:6px;background:#1e2d3d;border-radius:3px;overflow:hidden}
.timer-bar .pfill{height:100%;background:linear-gradient(90deg,#3fb950,#f0a000,#f85149);transition:width 1s linear}
.timer-bar .ebar{flex:1;height:3px;background:#1e2d3d;border-radius:3px;overflow:hidden;margin-top:3px}
.timer-bar .efill{height:100%;background:linear-gradient(90deg,#3fb950,#f0a000,#f85149);transition:width 1s linear}
.timer-bar .info{color:#8b949e;font-size:11px;text-align:right;min-width:160px}
.timer-bar .market{color:#58a6ff;font-size:11px}

.summary{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:12px;margin-bottom:16px}
.scard{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:12px 16px;min-width:0;text-align:left}
.scard .l{font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
.scard .v{font-size:20px;font-weight:700;margin-top:2px}
.scard .note{font-size:9px;color:#8b949e;margin-top:3px}
.green{color:#3fb950}.red{color:#f85149}.blue{color:#58a6ff}.yellow{color:#f0a000}.purple{color:#a371f7}

.trigs{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.trigs{grid-template-columns:repeat(2,1fr)}}
@media(max-width:500px){.trigs{grid-template-columns:1fr}}
.tc{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:16px;position:relative;overflow:hidden}
.tc .tn{font-size:20px;font-weight:700}.tc .td{font-size:10px;color:#8b949e;margin:4px 0 6px;line-height:1.5}
.tc .risk{font-size:10px;margin-bottom:8px}
.tc .hits{font-size:32px;font-weight:700}.tc .stats{font-size:11px;margin-top:4px}
.tc .stat-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px;margin-top:9px}.tc .stat-item{background:#0a0e17;border:1px solid #1e2d3d;border-radius:5px;padding:6px}.tc .stat-item .k{font-size:8px;color:#8b949e;text-transform:uppercase}.tc .stat-item .n{font-size:13px;font-weight:700;margin-top:2px}
.tc .exits{font-size:10px;color:#a371f7;margin-top:4px}
.tc .bar{position:absolute;bottom:0;left:0;height:3px;transition:width .5s}
.trigger-toggle{margin-top:8px;width:100%;border:1px solid #334155;background:#0a0e17;color:#e0e6ed;border-radius:5px;padding:6px;cursor:pointer}.trigger-toggle.on{border-color:#3fb950;color:#3fb950}.trigger-toggle.off{border-color:#f85149;color:#f85149}
.tp-sl{font-size:10px;color:#8b949e;margin-top:6px;border-top:1px solid #1e2d3d;padding-top:6px}

.trigger-calendar{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:14px;margin-bottom:16px}
.calendar-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.calendar-head h3{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px}.calendar-actions{display:flex;align-items:center;gap:8px}.calendar-actions button{border:1px solid #1e2d3d;background:#0a0e17;color:#e0e6ed;border-radius:5px;padding:6px 9px;cursor:pointer}.calendar-actions button:hover{border-color:#58a6ff}.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(120px,1fr));gap:7px;overflow:auto}.calendar-dow{font-size:9px;color:#8b949e;text-align:center;text-transform:uppercase;padding:3px}.calendar-day{min-height:130px;border:1px solid #1e2d3d;border-radius:6px;background:#0a0e17;padding:8px;color:#e0e6ed;font:inherit;text-align:left}.calendar-day:not(.empty){cursor:pointer}.calendar-day:not(.empty):hover{border-color:#58a6ff}.calendar-day.selected{border-color:#58a6ff;box-shadow:0 0 0 1px #58a6ff}.calendar-day.out{opacity:.28}.calendar-day.empty{min-height:70px}.calendar-date{display:flex;align-items:center;justify-content:space-between;gap:5px;margin-bottom:6px}.calendar-date strong{font-size:13px}.calendar-overall{font-size:9px;color:#8b949e}.calendar-triggers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:3px}.calendar-trigger{display:flex;justify-content:space-between;gap:3px;border:1px solid #1e2d3d;border-radius:4px;padding:4px;font-size:9px}.calendar-trigger.none{color:#4b5563}.calendar-trigger.win{color:#3fb950}.calendar-trigger.mid{color:#f0a000}.calendar-trigger.loss{color:#f85149}.calendar-modal{display:none;position:fixed;inset:0;z-index:1000;align-items:center;justify-content:center;padding:24px;background:rgba(3,7,18,.68);backdrop-filter:blur(7px)}.calendar-modal.open{display:flex}.calendar-dialog{position:relative;width:min(760px,calc(100vw - 48px));max-height:calc(100vh - 48px);overflow:auto;background:#111827;border:1px solid #334155;border-radius:8px;padding:24px 68px;box-shadow:0 24px 80px rgba(0,0,0,.55)}.calendar-close,.calendar-focus-nav{border:1px solid #334155;background:#0a0e17;color:#e0e6ed;cursor:pointer}.calendar-close:hover,.calendar-focus-nav:hover{border-color:#58a6ff;color:#58a6ff}.calendar-close{position:absolute;right:14px;top:14px;width:34px;height:34px;border-radius:5px;font-size:18px}.calendar-focus-nav{position:absolute;top:50%;transform:translateY(-50%);width:42px;height:54px;border-radius:6px;font-size:24px}.calendar-focus-nav.prev{left:12px}.calendar-focus-nav.next{right:12px}.calendar-focus-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:16px;padding-right:26px}.calendar-focus-head h4{font-size:20px}.calendar-focus-overall{text-align:right}.calendar-focus-overall strong{display:block;font-size:28px}.calendar-focus-overall span{font-size:10px;color:#8b949e}.calendar-focus-grid{display:grid;grid-template-columns:repeat(3,minmax(140px,1fr));gap:10px}.calendar-focus-card{background:#0a0e17;border:1px solid #1e2d3d;border-radius:6px;padding:16px}.calendar-focus-card .name{font-size:12px;color:#8b949e}.calendar-focus-card .rate{font-size:28px;font-weight:700;margin:5px 0}.calendar-focus-card .record{font-size:10px;color:#8b949e}@media(max-width:600px){.calendar-modal{padding:12px}.calendar-dialog{width:calc(100vw - 24px);max-height:calc(100vh - 24px);padding:58px 18px 20px}.calendar-close{right:12px;top:12px}.calendar-focus-nav{top:16px;transform:none;width:42px;height:34px}.calendar-focus-nav.prev{left:12px}.calendar-focus-nav.next{left:60px;right:auto}.calendar-focus-head{align-items:flex-start;flex-direction:column;padding-right:0}.calendar-focus-overall{text-align:left}.calendar-focus-grid{grid-template-columns:repeat(2,minmax(110px,1fr))}}

.research{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
@media(max-width:800px){.research{grid-template-columns:1fr}}
.rcard{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:14px}
.rcard h3{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
canvas{width:100%!important;height:180px!important}

.snaps{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:14px;margin-bottom:16px}
.snaps h3{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.snap-grid{display:flex;flex-wrap:wrap;gap:4px}
.snap-dot{width:36px;height:22px;border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:700;color:#fff;cursor:default}
.snap-dot:hover{outline:2px solid #58a6ff}

.tl{background:#111827;border:1px solid #1e2d3d;border-radius:8px;padding:14px}
.tl h3{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.tr{display:flex;align-items:center;padding:5px 0;border-bottom:1px solid #0d1117;font-size:11px;gap:6px}
.tr:last-child{border:none}
.tr .tt{width:70px;color:#8b949e}
.tr .tg{width:28px;font-weight:700}
.tr .sd{width:38px}
.tr .ep{width:55px}
.tr .xp{width:55px;color:#f0a000}
.tr .dl{width:50px}
.tr .rs{width:40px}
.tr .pn{width:55px}
.tr .lt{width:58px;color:#58a6ff}
.tr .xr{flex:1;color:#8b949e;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:1100px){.summary{grid-template-columns:repeat(3,minmax(120px,1fr))}}
@media(max-width:650px){.summary{grid-template-columns:repeat(2,minmax(120px,1fr))}}
</style></head><body>

<div class="hdr">
  <h1>END_WINDOW Trigger Performance</h1>
  <div class="sub">Auto-refresh 2s | <span id="totalT">0</span> trades</div>
</div>

<div class="ct">

<div class="timer-bar">
  <div>
    <div class="clock" id="clock">--:--</div>
    <div class="elapsed-tag" id="elapsedTag">0s elapsed</div>
  </div>
  <div style="flex:1">
    <div class="market" id="mktQ">Waiting...</div>
    <div class="pbar"><div class="pfill" id="pfill" style="width:0%"></div></div>
    <div class="ebar"><div class="efill" id="efill" style="width:0%"></div></div>
  </div>
  <div class="info">
    BTC: <span class="blue" id="btcP">--</span><br>
    Δ: <span id="btcD">--</span> | <span id="lead">--</span><br>
    <span style="color:#a371f7" id="posInfo">no position</span>
  </div>
</div>

<div class="summary">
  <div class="scard"><div class="l">All Trades</div><div class="v blue" id="smTotal">0</div><div class="note">including open trades</div></div>
  <div class="scard"><div class="l">Resolved</div><div class="v" id="smResolved">0</div><div class="note" id="smRecord">0W / 0L</div></div>
  <div class="scard"><div class="l">Overall Win Rate</div><div class="v yellow" id="smWr">0%</div><div class="note">resolved trades only</div></div>
  <div class="scard"><div class="l">Total P&L</div><div class="v" id="smPnl">$0</div><div class="note">resolved trades only</div></div>
  <div class="scard"><div class="l">Best Win Rate</div><div class="v green" id="smBest">--</div><div class="note" id="smBestNote">no resolved trades</div></div>
  <div class="scard"><div class="l">Runtime Latency</div><div class="v blue" id="smLatency">N/A</div><div class="note">latest bot tick</div></div>
</div>

<div class="trigs" id="trigCards"></div>

<div class="trigger-calendar">
  <div class="calendar-head">
    <h3>Trigger Win Rate Calendar</h3>
    <div class="calendar-actions">
      <button onclick="shiftCalendar(-1)" aria-label="Previous month">&lt;</button>
      <strong id="calendarTitle">--</strong>
      <button onclick="shiftCalendar(1)" aria-label="Next month">&gt;</button>
    </div>
  </div>
  <div class="calendar-grid" id="triggerCalendar"></div>
</div>

<div class="calendar-modal" id="calendarModal" onclick="closeCalendarFocus(event)">
  <div class="calendar-dialog" role="dialog" aria-modal="true" aria-labelledby="calendarFocusTitle">
    <button type="button" class="calendar-close" onclick="closeCalendarFocus()" aria-label="Close focus calendar">&times;</button>
    <button type="button" class="calendar-focus-nav prev" onclick="shiftFocusedDate(-1)" aria-label="Previous date">&lt;</button>
    <div id="calendarFocus"></div>
    <button type="button" class="calendar-focus-nav next" onclick="shiftFocusedDate(1)" aria-label="Next date">&gt;</button>
  </div>
</div>

<div class="research">
  <div class="rcard">
    <h3>📊 UP / DOWN price — current window</h3>
    <canvas id="priceChart"></canvas>
  </div>
  <div class="rcard">
    <h3>📈 BTC Δ$ — current window</h3>
    <canvas id="deltaChart"></canvas>
  </div>
</div>

<div class="snaps">
  <h3>🔬 Leading price heatmap — current window (hijau=tinggi, merah=rendah)</h3>
  <div class="snap-grid" id="snapGrid"></div>
</div>

<div class="tl">
  <h3>📋 Trade Timeline</h3>
  <div style="font-size:10px;color:#8b949e;margin-bottom:6px;display:flex;gap:8px">
    <span>TIME</span><span style="width:28px">TRIG</span><span style="width:38px">SIDE</span>
    <span style="width:55px">ENTRY</span><span style="width:55px;color:#f0a000">EXIT</span>
    <span style="width:50px">BTC Δ</span><span style="width:40px">RES</span>
    <span style="width:55px">P&L</span><span style="width:58px">LATENCY</span><span>EXIT REASON</span>
  </div>
  <div id="tlBody"></div>
</div>

</div>

<script>
const $=id=>document.getElementById(id);
const BUY_TRIGS=['BUY-1'];
const TIME_TRIGS=['TIME-1','TIME-2','TIME-3','TIME-4','TIME-5','TIME-6'];
const LAYER_TRIGS=['T1','T2','T3','T4','T5','T6'];
let TRIGS=[...BUY_TRIGS,...TIME_TRIGS,...LAYER_TRIGS];
const COL={
  'BUY-1':'#22c55e',
  'TIME-1':'#d946ef',
  'TIME-2':'#ec4899',
  'TIME-3':'#c084fc',
  'TIME-4':'#8b5cf6',
  'TIME-5':'#6366f1',
  'TIME-6':'#0ea5e9',
  T1:'#3fb950',T2:'#58a6ff',T3:'#f0a000',T4:'#a371f7',
  T5:'#ff7b72',T6:'#ffa657',T7:'#79c0ff',TX:'#f85149',
  B1:'#ff6ec7',B2:'#56d4dd',B3:'#bb86fc',B4:'#ffd166',B5:'#06d6a0'
};
const RISK_E={safest:'[safe]',safe:'[safe]',medium:'[med]',high:'[high]',highest:'[!!]'};
let calendarDate=new Date();
let calendarDays=[];
let focusedCalendarDate='';

function toggleTimeTrigger(trigger,enabled){
  const key=trigger==='BUY-1'?'buy1_enabled':'time'+trigger.split('-')[1]+'_enabled';
  fetch('/api/settings',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({[key]:enabled})})
    .then(()=>update()).catch(()=>{});
}

function shiftCalendar(delta){
  calendarDate=new Date(calendarDate.getFullYear(),calendarDate.getMonth()+delta,1);
  renderTriggerCalendar();
}

function focusCalendarDate(date){
  focusedCalendarDate=date;
  renderTriggerCalendar();
  $('calendarModal').classList.add('open');
  document.body.style.overflow='hidden';
}

function closeCalendarFocus(event){
  if(event&&event.target!==$('calendarModal'))return;
  $('calendarModal').classList.remove('open');
  document.body.style.overflow='';
}

function shiftFocusedDate(delta){
  const date=new Date(focusedCalendarDate+'T00:00:00');
  date.setDate(date.getDate()+delta);
  focusedCalendarDate=date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0');
  calendarDate=new Date(date.getFullYear(),date.getMonth(),1);
  renderTriggerCalendar();
}

function renderCalendarFocus(byDate){
  const day=byDate.get(focusedCalendarDate);
  const date=new Date(focusedCalendarDate+'T00:00:00');
  const title=date.toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'long',year:'numeric'});
  const cards=TRIGS.map(trigger=>{
    const stats=day?.triggers?.[trigger];
    const resolved=Number(stats?.resolved||0);
    const wr=Number(stats?.win_rate||0);
    const cls=!resolved?'none':wr>=60?'win':wr>=40?'mid':'loss';
    return `<div class="calendar-focus-card ${cls}"><div class="name">${trigger}</div><div class="rate">${resolved?wr.toFixed(1)+'%':'--'}</div><div class="record">${resolved} resolved | ${Number(stats?.wins||0)}W / ${Number(stats?.losses||0)}L</div></div>`;
  }).join('');
  const resolved=Number(day?.resolved||0);
  $('calendarFocus').innerHTML=`<div class="calendar-focus-head"><h4 id="calendarFocusTitle">${title}</h4><div class="calendar-focus-overall"><strong class="${resolved?'yellow':''}">${resolved?Number(day.win_rate).toFixed(1)+'%':'No trades'}</strong><span>${resolved} resolved | ${Number(day?.wins||0)}W / ${Number(day?.losses||0)}L</span></div></div><div class="calendar-focus-grid">${cards}</div>`;
}

function renderTriggerCalendar(){
  const year=calendarDate.getFullYear(),month=calendarDate.getMonth();
  $('calendarTitle').textContent=calendarDate.toLocaleDateString(undefined,{month:'long',year:'numeric'});
  const byDate=new Map(calendarDays.map(day=>[day.date,day]));
  const monthPrefix=year+'-'+String(month+1).padStart(2,'0')+'-';
  if(!focusedCalendarDate.startsWith(monthPrefix)){
    const monthDays=calendarDays.filter(day=>day.date.startsWith(monthPrefix));
    focusedCalendarDate=monthDays.length?monthDays[monthDays.length-1].date:monthPrefix+'01';
  }
  const firstDay=new Date(year,month,1);
  const mondayOffset=(firstDay.getDay()+6)%7;
  const daysInMonth=new Date(year,month+1,0).getDate();
  const labels=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const cells=labels.map(label=>`<div class="calendar-dow">${label}</div>`);
  for(let i=0;i<mondayOffset;i++)cells.push('<div class="calendar-day empty out"></div>');
  for(let dayNum=1;dayNum<=daysInMonth;dayNum++){
    const key=year+'-'+String(month+1).padStart(2,'0')+'-'+String(dayNum).padStart(2,'0');
    const day=byDate.get(key);
    const triggerRows=TRIGS.map(trigger=>{
      const stats=day?.triggers?.[trigger];
      const resolved=Number(stats?.resolved||0);
      const wr=Number(stats?.win_rate||0);
      const cls=!resolved?'none':wr>=60?'win':wr>=40?'mid':'loss';
      return `<div class="calendar-trigger ${cls}"><span>${trigger}</span><strong>${resolved?wr.toFixed(0)+'%':'--'}</strong></div>`;
    }).join('');
    const overall=day?`${day.win_rate.toFixed(1)}% | ${day.wins}W/${day.losses}L`:'No trades';
    const selected=key===focusedCalendarDate?' selected':'';
    cells.push(`<button type="button" class="calendar-day${selected}" onclick="focusCalendarDate('${key}')" aria-label="Focus ${key}"><div class="calendar-date"><strong>${dayNum}</strong><span class="calendar-overall">${overall}</span></div><div class="calendar-triggers">${triggerRows}</div></button>`);
  }
  $('triggerCalendar').innerHTML=cells.join('');
  renderCalendarFocus(byDate);
}

document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&$('calendarModal').classList.contains('open'))closeCalendarFocus();
});

function drawLine(cid,labels,datasets){
  const c=$(cid),ctx=c.getContext('2d');
  const W=c.width=c.offsetWidth,H=c.height=180;
  ctx.clearRect(0,0,W,H);
  if(!labels.length)return;
  datasets.forEach(ds=>{
    const vals=ds.data,col=ds.color;
    const mn=Math.min(...vals),mx=Math.max(...vals),range=mx-mn||1;
    ctx.beginPath();ctx.strokeStyle=col;ctx.lineWidth=2;
    vals.forEach((v,i)=>{
      const x=20+i/(vals.length-1||1)*(W-40);
      const y=H-20-(v-mn)/range*(H-40);
      i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
    });
    ctx.stroke();
    ctx.fillStyle=col;ctx.font='10px monospace';ctx.textAlign='left';
    ctx.fillText(ds.label+' '+vals[vals.length-1].toFixed(ds.decimals||2),5,datasets.indexOf(ds)*14+14);
  });
  if(labels.length>1){
    ctx.fillStyle='#8b949e';ctx.font='9px monospace';
    ctx.textAlign='left';ctx.fillText(labels[0]+'s',5,H-4);
    ctx.textAlign='right';ctx.fillText(labels[labels.length-1]+'s',W-5,H-4);
  }
}

function priceColor(p){
  if(p>=0.85)return'#3fb950';if(p>=0.72)return'#58a6ff';if(p>=0.60)return'#f0a000';
  if(p>=0.50)return'#f85149';return'#8b949e';
}

function update(){
  fetch('/api/data').then(r=>r.json()).then(d=>{
    const{triggers:tg,trades:tr,summary:sm,meta:mt,state:ss,snapshots:snaps,trigger_calendar:tc,settings}=d;
    TRIGS=[
      ...BUY_TRIGS,
      ...TIME_TRIGS.sort((a,b)=>{
        const ai=a.split('-')[1],bi=b.split('-')[1];
        return Number(settings?.[`time${ai}_price`]||0)-Number(settings?.[`time${bi}_price`]||0)||Number(ai)-Number(bi);
      }),
      ...LAYER_TRIGS,
    ];
    calendarDays=tc?.days||[];
    renderTriggerCalendar();

    // Timer
    const sl=Math.max(0,Math.round(ss.seconds_left||0));
    const se=Math.max(0,Math.round(ss.secs_elapsed||0));
    $('clock').textContent=Math.floor(sl/60)+':'+String(sl%60).padStart(2,'0');
    $('elapsedTag').textContent=se+'s elapsed';
    $('pfill').style.width=Math.max(0,Math.min(100,(1-sl/300)*100))+'%';
    $('efill').style.width=Math.max(0,Math.min(100,(se/300)*100))+'%';
    $('mktQ').textContent=ss.market_question||'Waiting...';
    $('btcP').textContent=ss.btc_price?'$'+ss.btc_price.toLocaleString(undefined,{maximumFractionDigits:0}):'--';
    $('btcD').textContent='$'+(ss.btc_distance||0).toFixed(1);
    $('lead').textContent=(ss.leading||'--')+' '+((ss.up_price>ss.down_price?ss.up_price:ss.down_price)||0).toFixed(4);
    $('posInfo').textContent=ss.has_open_position
      ? ss.open_trigger+' '+ss.open_outcome+' @'+ss.open_entry_price.toFixed(4)+' unrealized: $'+(ss.open_unrealized_pnl||0).toFixed(3)
      : 'no position';

    // Summary
    $('totalT').textContent=sm.total;
    $('smTotal').textContent=sm.total;
    $('smResolved').textContent=sm.resolved||0;
    $('smRecord').textContent=(sm.wins||0)+'W / '+(sm.losses||0)+'L';
    $('smPnl').textContent='$'+(sm.pnl>=0?'+':'')+sm.pnl.toFixed(2);
    $('smPnl').className='v '+(sm.pnl>=0?'green':'red');
    $('smWr').textContent=(sm.win_rate||0).toFixed(1)+'%';
    $('smBest').textContent=sm.best_trigger||'--';
    $('smBestNote').textContent=sm.best_trigger&&sm.best_trigger!=='--'
      ? (sm.best_trigger_win_rate||0).toFixed(1)+'% from '+(sm.best_trigger_resolved||0)+' resolved'
      : 'no resolved trades';
    $('smLatency').textContent=Number(ss.latency_ms||0)>0
      ? Number(ss.latency_ms).toFixed(1)+' ms'
      : 'N/A';

    const maxH=Math.max(...TRIGS.map(t=>(tg[t]||{}).count||0),1);
    $('trigCards').innerHTML=TRIGS.map(t=>{
      const s=tg[t]||{count:0,resolved:0,open:0,wins:0,losses:0,pnl:0,wr:0,avg_price:0,avg_delta:0,avg_latency_ms:null};
      const m=mt[t]||{};
      const re=RISK_E[m.risk]||'[risk]';
      const wrC=s.wr>=60?'green':s.wr>=40?'yellow':'red';
      const pC=s.pnl>=0?'green':'red';
      const latency=Number(s.avg_latency_ms||0)>0?s.avg_latency_ms.toFixed(1)+' ms':'N/A';
      const settingKey=t==='BUY-1'?'buy1_enabled':t.startsWith('TIME-')?'time'+t.split('-')[1]+'_enabled':'';
      const triggerEnabled=settingKey?settings?.[settingKey]!==false:false;
      const toggle=settingKey?`<button class="trigger-toggle ${triggerEnabled?'on':'off'}" onclick="toggleTimeTrigger('${t}',${!triggerEnabled})">${t} ${triggerEnabled?'ON':'OFF'}</button>`:'';
      return `<div class="tc">
        <div class="tn" style="color:${COL[t]}">${t}</div>
        <div class="td">${m.time||''}<br>${m.price||''} | ${m.req||''}</div>
        <div style="font-size:10px;color:#8b949e;margin-bottom:6px">${m.desc||''}</div>
        <div class="risk">${re} ${m.risk||''}</div>
        <div class="hits">${s.count}</div>
        <div class="stats">
          <span class="${wrC}">${s.resolved?(s.wr.toFixed(1)+'% WR'):'N/A WR'}</span> |
          <span class="${pC}">$${s.pnl>=0?'+':''}${s.pnl.toFixed(2)}</span>
        </div>
        <div class="stat-grid">
          <div class="stat-item"><div class="k">Resolved</div><div class="n">${s.resolved}</div></div>
          <div class="stat-item"><div class="k">Open</div><div class="n">${s.open}</div></div>
          <div class="stat-item"><div class="k">Record</div><div class="n">${s.wins}W / ${s.losses}L</div></div>
          <div class="stat-item"><div class="k">Avg Latency</div><div class="n">${latency}</div></div>
          <div class="stat-item"><div class="k">Avg Entry</div><div class="n">${s.avg_price.toFixed(3)}</div></div>
          <div class="stat-item"><div class="k">Avg BTC Delta</div><div class="n">$${Math.abs(s.avg_delta).toFixed(1)}</div></div>
        </div>
        ${toggle}
        <div class="bar" style="width:${s.count/maxH*100}%;background:${COL[t]}"></div>
      </div>`;
    }).join('');

    // Charts
    const curSnaps=snaps.filter(s=>s.window_ts===ss.current_window);
    if(curSnaps.length>1){
      const lbls=curSnaps.map(s=>Math.round(s.secs_elapsed));
      drawLine('priceChart',lbls,[
        {data:curSnaps.map(s=>s.up_price),  color:'#3fb950',label:'UP',  decimals:4},
        {data:curSnaps.map(s=>s.down_price), color:'#f85149',label:'DOWN',decimals:4},
      ]);
      drawLine('deltaChart',lbls,[
        {data:curSnaps.map(s=>s.btc_distance),color:'#58a6ff',label:'Δ$',decimals:1},
      ]);
    }

    // Heatmap
    $('snapGrid').innerHTML=curSnaps.map(s=>{
      const p=s.leading_price;
      const bg=priceColor(p);
      return `<div class="snap-dot" style="background:${bg}"
        title="${Math.round(s.secs_elapsed)}s | ${s.leading} ${p.toFixed(2)} | Δ$${s.btc_distance.toFixed(0)}">
        ${Math.round(s.secs_elapsed)}</div>`;
    }).join('');

    // Timeline
    const recent=tr.slice(-30).reverse();
    $('tlBody').innerHTML=recent.map(t=>{
      const tm=new Date(t.timestamp*1000).toLocaleTimeString();
      const trig=t.fire_layer||t.trigger||'?';
      const col=COL[trig]||'#8b949e';
      const won=t.won===true;

      // Result: teks styled, bukan emoji
      let resHtml;
      if(!t.resolved) resHtml='<span style="color:#8b949e">OPEN</span>';
      else if(won)    resHtml='<span style="color:#3fb950;font-weight:700">WIN</span>';
      else            resHtml='<span style="color:#f85149;font-weight:700">LOSS</span>';

      const pnl=t.resolved?((t.pnl>=0?'+':'')+t.pnl.toFixed(2)):'--';
      const pC=t.pnl>=0?'green':'red';
      const exitP=t.exited_early?(t.exit_price||0).toFixed(4):(t.resolved?'close':'--');
      const latency=Number(t.latency_ms||0)>0?Number(t.latency_ms).toFixed(1)+'ms':'N/A';

      // Strip semua non-ASCII (emoji, dsb) dari exit reason
      const rawR=t.exited_early?(t.exit_reason||''):(t.resolved?'hold-to-close':'');
      const exitR=rawR.replace(/[^\x20-\x7E]/g,'').replace(/[ \t]+/g,' ').trim().slice(0,25);

      return `<div class="tr">
        <span class="tt">${tm}</span>
        <span class="tg" style="color:${col}">${trig}</span>
        <span class="sd ${t.outcome==='UP'?'green':'red'}">${t.outcome}</span>
        <span class="ep">${(t.entry_price||0).toFixed(4)}</span>
        <span class="xp">${exitP}</span>
        <span class="dl">$${(t.btc_distance||0).toFixed(0)}</span>
        <span class="rs">${resHtml}</span>
        <span class="pn ${t.resolved?pC:''}">${pnl}</span>
        <span class="lt">${latency}</span>
        <span class="xr">${exitR}</span>
      </div>`;
    }).join('');
  }).catch(()=>{});
}

update();setInterval(update,1000);
</script></body></html>"""


def analyze(trades, snapshots):
    trigs = {
        trigger: {
            "count": 0, "resolved": 0, "open": 0,
            "wins": 0, "losses": 0, "pnl": 0.0, "wr": 0.0,
            "avg_price": 0.0, "avg_secs_elapsed": 0.0,
            "avg_delta": 0.0, "avg_latency_ms": None,
            "prices": [], "elapsed": [], "deltas": [], "latencies": [],
        }
        for trigger in END_WINDOW_META
    }

    analyzed_trades = []
    for source_trade in trades:
        trade = dict(source_trade)
        trigger = trade_layer(trade)
        if trigger not in trigs:
            continue
        trade["fire_layer"] = trigger
        analyzed_trades.append(trade)

        stats = trigs[trigger]
        stats["count"] += 1
        stats["prices"].append(float(trade.get("entry_price") or 0.0))
        stats["elapsed"].append(float(trade.get("secs_elapsed") or 0.0))
        stats["deltas"].append(float(trade.get("btc_distance") or 0.0))
        latency = float(trade.get("latency_ms") or 0.0)
        if latency > 0:
            stats["latencies"].append(latency)

        if trade.get("resolved"):
            stats["resolved"] += 1
            if trade.get("won") is True:
                stats["wins"] += 1
            elif trade.get("won") is False:
                stats["losses"] += 1
            stats["pnl"] += float(trade.get("pnl") or 0.0)
        else:
            stats["open"] += 1

    for stats in trigs.values():
        stats["wr"] = (
            stats["wins"] / stats["resolved"] * 100
            if stats["resolved"] else 0.0
        )
        stats["pnl"] = round(stats["pnl"], 2)
        stats["avg_price"] = (
            round(sum(stats["prices"]) / len(stats["prices"]), 4)
            if stats["prices"] else 0.0
        )
        stats["avg_secs_elapsed"] = (
            round(sum(stats["elapsed"]) / len(stats["elapsed"]), 1)
            if stats["elapsed"] else 0.0
        )
        stats["avg_delta"] = (
            round(sum(stats["deltas"]) / len(stats["deltas"]), 1)
            if stats["deltas"] else 0.0
        )
        stats["avg_latency_ms"] = (
            round(sum(stats["latencies"]) / len(stats["latencies"]), 1)
            if stats["latencies"] else None
        )
        del stats["prices"], stats["elapsed"], stats["deltas"], stats["latencies"]

    resolved_trades = [trade for trade in analyzed_trades if trade.get("resolved")]
    total_wins = sum(1 for trade in resolved_trades if trade.get("won") is True)
    total_losses = sum(1 for trade in resolved_trades if trade.get("won") is False)
    resolved_active = [
        (trigger, stats)
        for trigger, stats in trigs.items()
        if stats["resolved"] > 0
    ]
    best = max(
        resolved_active,
        key=lambda item: (item[1]["wr"], item[1]["resolved"], item[1]["pnl"]),
        default=("--", {}),
    )

    return {
        "triggers": trigs,
        "trades": analyzed_trades,
        "summary": {
            "total": len(analyzed_trades),
            "resolved": len(resolved_trades),
            "wins": total_wins,
            "losses": total_losses,
            "pnl": round(sum(float(t.get("pnl") or 0.0) for t in resolved_trades), 2),
            "win_rate": (
                total_wins / len(resolved_trades) * 100
                if resolved_trades else 0.0
            ),
            "best_trigger": best[0],
            "best_trigger_win_rate": best[1].get("wr", 0.0),
            "best_trigger_resolved": best[1].get("resolved", 0),
        },
        "meta": END_WINDOW_META,
        "snapshots": snapshots,
        "trigger_calendar": build_trigger_calendar(analyzed_trades),
    }


async def index(req):
    return web.Response(text=HTML, content_type='text/html')

async def api_data(req):
    global END_WINDOW_META
    END_WINDOW_META = build_end_window_meta(st.load_settings())
    trades    = st.load_trades()
    snapshots = st.load_snapshots()
    state     = st.load_state()
    result    = analyze(trades, snapshots)
    # Inject active strategy label for dashboard/tracker filtering.
    try:
        import core.config as config
        from strategies import enabled_strategies
        _enabled = enabled_strategies()
        if _enabled:
            state["strategy_mode"] = "+".join(sorted(_enabled))
        else:
            state["strategy_mode"] = getattr(config, "STRATEGY_MODE", "END_WINDOW")
    except Exception:
        state["strategy_mode"] = "END_WINDOW"
    result["state"] = state
    result["settings"] = st.asdict(st.load_settings())
    return web.json_response(result)


async def api_settings(req):
    data = await req.json()
    updates = {}
    if "buy1_enabled" in data:
        updates["buy1_enabled"] = bool(data["buy1_enabled"])
    for index in range(1, 7):
        key = f"time{index}_enabled"
        if key in data:
            updates[key] = bool(data[key])
    settings = st.update_settings(updates)
    return web.json_response({"success": True, "settings": st.asdict(settings)})

def main():
    app = web.Application()
    app.router.add_get('/',         index)
    app.router.add_get('/api/data', api_data)
    app.router.add_post('/api/settings', api_settings)
    log.info("Trigger Tracker → http://localhost:%d", PORT)
    web.run_app(app, host='0.0.0.0', port=PORT, print=lambda _: None)

if __name__ == '__main__':
    main()
