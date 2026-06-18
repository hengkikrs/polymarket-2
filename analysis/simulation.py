"""Read-only stress simulation for local trade history.

The simulator does not call Polymarket and does not place orders. It re-prices
resolved local trades under configurable cost assumptions to make mock/PnL
claims harder to over-trust.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


@dataclass
class StressAssumptions:
    fee_rate: float = 0.072
    buy_slippage_ticks: float = 1.0
    sell_slippage_ticks: float = 1.0
    tick_size: float = 0.01
    failed_fill_pct: float = 0.05
    partial_fill_pct: float = 0.03
    partial_fill_min: float = 0.5
    partial_fill_max: float = 0.95
    seed: int = 7


def _fee(shares: float, price: float, fee_rate: float) -> float:
    return max(0.0, shares * fee_rate * price * (1.0 - price))


def _drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    equity = 0.0
    for pnl in values:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 4)


def metrics(pnls: list[float]) -> dict:
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    breakeven = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 0.0
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
        "total_pnl": round(sum(pnls), 4),
        "expectancy": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "max_drawdown": _drawdown(pnls),
        "breakeven_win_rate": round(breakeven * 100, 2),
        "worst_trade": round(min(pnls), 4) if pnls else 0.0,
        "best_trade": round(max(pnls), 4) if pnls else 0.0,
    }


def simulate_trades(trades: Iterable[dict], assumptions: StressAssumptions) -> dict:
    rng = random.Random(assumptions.seed)
    stressed: list[float] = []
    skipped_failed = 0
    partials = 0

    for trade in trades:
        if not trade.get("resolved") or trade.get("exited_early"):
            continue
        entry = float(trade.get("entry_price", 0.0) or 0.0)
        shares = float(trade.get("shares", 0.0) or 0.0)
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        if entry <= 0 or shares <= 0:
            continue

        if rng.random() < assumptions.failed_fill_pct:
            skipped_failed += 1
            continue

        size_mult = 1.0
        if rng.random() < assumptions.partial_fill_pct:
            size_mult = rng.uniform(assumptions.partial_fill_min, assumptions.partial_fill_max)
            partials += 1

        adjusted_shares = shares * size_mult
        buy_slip = assumptions.buy_slippage_ticks * assumptions.tick_size * adjusted_shares
        # A resolved hold-to-expiry winner has no sell, but fee/slippage still
        # punishes entry. Early exits would need separate order-book replay.
        fee = _fee(adjusted_shares, min(max(entry, 0.01), 0.99), assumptions.fee_rate)
        stressed.append(round(pnl * size_mult - buy_slip - fee, 4))

    return {
        "assumptions": asdict(assumptions),
        "skipped_failed_fills": skipped_failed,
        "partial_fills": partials,
        "metrics": metrics(stressed),
    }


def walk_forward(trades: list[dict], assumptions: StressAssumptions) -> dict:
    resolved = [t for t in trades if t.get("resolved") and not t.get("exited_early")]
    mid = len(resolved) // 2
    return {
        "in_sample": simulate_trades(resolved[:mid], assumptions)["metrics"],
        "out_of_sample": simulate_trades(resolved[mid:], assumptions)["metrics"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    default_trades = Path(__file__).resolve().parent.parent / "runtime_data" / "trades.json"
    parser.add_argument("--trades", default=str(default_trades))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    trades = json.loads(Path(args.trades).read_text(encoding="utf-8"))
    assumptions = StressAssumptions()
    result = simulate_trades(trades, assumptions)
    result["walk_forward"] = walk_forward(trades, assumptions)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Stress simulation (read-only, no live orders)")
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
