"""Order-book normalization shared by REST and background polling."""
from __future__ import annotations

from typing import Any


def top_depth(
    levels: list[Any],
    *,
    side: str,
    limit: int = 5,
) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    for level in levels or []:
        try:
            if isinstance(level, dict):
                price = float(level.get("price", 0.0) or 0.0)
                size = float(level.get("size", 0.0) or 0.0)
            else:
                price = float(level[0])
                size = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and size > 0:
            parsed.append((price, size))

    side = str(side or "").lower()
    if side not in {"bid", "ask"}:
        raise ValueError(f"invalid order-book side: {side}")
    parsed.sort(key=lambda row: row[0], reverse=side == "bid")
    return parsed[:max(0, int(limit))]
