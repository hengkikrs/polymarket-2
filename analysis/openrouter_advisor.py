"""OpenRouter advisory layer for local trade decisions.

OpenRouter does not execute orders. It returns one strict JSON recommendation;
the bot validates the recommendation and performs the actual trade through the
existing local execution/risk path.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

import core.config as config

log = logging.getLogger("openrouter")


@dataclass
class AdvisorDecision:
    action: str = "HOLD"
    side: str = ""
    confidence: float = 0.0
    max_price: float = 0.0
    amount_usd: float = 0.0
    reason: str = ""
    raw: dict[str, Any] | None = None


def is_enabled() -> bool:
    return bool(config.OPENROUTER_API_KEY and config.AI_ADVISOR_ENABLED)


def _extract_text(resp: dict[str, Any]) -> str:
    # OpenRouter normalizes responses to the OpenAI Chat Completions shape.
    msg = resp.get("choices", [{}])[0].get("message", {})
    content = msg.get("content", "")
    if isinstance(content, list):
        texts = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ]
        return "\n".join(t for t in texts if t).strip()
    return str(content or "").strip()


def _parse_json_text(text: str) -> dict[str, Any]:
    if not text:
        raise ValueError("empty OpenRouter response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback for accidental fenced/plain prose responses.
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"OpenRouter response has no JSON object: {text[:160]}")
    return json.loads(m.group(0))


def _coerce_decision(data: dict[str, Any]) -> AdvisorDecision:
    action = str(data.get("action", "HOLD")).upper().strip()
    side = str(data.get("side", "")).upper().strip()
    if action not in ("BUY", "HOLD"):
        action = "HOLD"
    if side not in ("UP", "DOWN"):
        side = ""

    def _float(key: str, default: float = 0.0) -> float:
        try:
            return float(data.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    reason = str(data.get("reason", "") or "")[:240]
    return AdvisorDecision(
        action=action,
        side=side,
        confidence=max(0.0, min(1.0, _float("confidence", 0.0))),
        max_price=max(0.0, min(1.0, _float("max_price", 0.0))),
        amount_usd=max(0.0, _float("amount_usd", 0.0)),
        reason=reason,
        raw=data,
    )


def validate_decision(decision: AdvisorDecision, snapshot: dict[str, Any]) -> tuple[bool, str]:
    if decision.action == "HOLD":
        return (False, decision.reason or "OpenRouter HOLD")
    if decision.action != "BUY":
        return (False, f"unsupported OpenRouter action={decision.action}")
    if decision.side not in ("UP", "DOWN"):
        return (False, f"invalid OpenRouter side={decision.side}")
    if decision.confidence < config.AI_MIN_CONFIDENCE:
        return (False, f"confidence {decision.confidence:.2f}<{config.AI_MIN_CONFIDENCE:.2f}")

    prices = snapshot.get("prices", {}) or {}
    price = float(prices.get(decision.side, 0.0) or 0.0)
    if price <= 0:
        return (False, f"{decision.side} price unavailable")
    max_price = decision.max_price or config.AI_MAX_PRICE
    max_price = min(max_price, config.AI_MAX_PRICE)
    if price > max_price:
        return (False, f"{decision.side} price {price:.4f}>{max_price:.4f}")

    amount = decision.amount_usd or config.AI_DEFAULT_AMOUNT_USD
    amount = min(amount, config.AI_MAX_AMOUNT_USD)
    if amount < config.AI_MIN_AMOUNT_USD:
        return (False, f"amount ${amount:.2f}<${config.AI_MIN_AMOUNT_USD:.2f}")
    return (True, "ok")


def build_prompt(snapshot: dict[str, Any]) -> str:
    compact = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True)
    return (
        "You are an advisory signal for a 5-minute Polymarket crypto up/down bot. "
        "Return exactly one JSON object and no prose. You cannot execute trades. "
        "The local bot validates your recommendation and may reject it.\n"
        "Allowed schema: {\"action\":\"BUY|HOLD\",\"side\":\"UP|DOWN\","
        "\"confidence\":0.0,\"max_price\":0.0,\"amount_usd\":0.0,"
        "\"reason\":\"short reason\"}.\n"
        "Rules: choose HOLD if the edge is unclear, market data is stale, spread is wide, "
        "seconds left are too low for a sane fill, or confidence is below threshold. "
        "If BUY, side must match the likely resolver outcome: UP only if close is likely "
        "above open; DOWN otherwise. Keep max_price conservative.\n"
        f"Snapshot JSON: {compact}"
    )


async def recommend(session: aiohttp.ClientSession, snapshot: dict[str, Any]) -> AdvisorDecision:
    if not is_enabled():
        return AdvisorDecision(action="HOLD", reason="OpenRouter disabled or missing API key")

    model = config.OPENROUTER_MODEL.strip() or "tencent/hy3-preview:free"
    url = "https://openrouter.ai/api/v1/chat/completions"
    prompt = build_prompt(snapshot)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You return only strict JSON. No markdown, no prose. "
                    "You are an advisory signal; the local bot validates and executes."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": config.AI_TEMPERATURE,
        "max_tokens": config.OPENROUTER_MAX_TOKENS,
    }
    if config.OPENROUTER_REASONING_EFFORT:
        payload["reasoning"] = {
            "effort": config.OPENROUTER_REASONING_EFFORT,
            "exclude": True,
        }
    if config.OPENROUTER_JSON_MODE:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": config.OPENROUTER_SITE_URL,
        "X-Title": config.OPENROUTER_APP_NAME,
    }
    try:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=config.AI_TIMEOUT_SECS),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                log.warning("OpenRouter HTTP %s: %s", resp.status, text[:240])
                return AdvisorDecision(action="HOLD", reason=f"OpenRouter HTTP {resp.status}")
            data = json.loads(text)
    except Exception as e:
        err = f"{type(e).__name__}: {repr(e)}"
        log.warning("OpenRouter request failed: %s", err)
        return AdvisorDecision(action="HOLD", reason=f"OpenRouter error: {err}")

    try:
        content = _extract_text(data)
        if not content:
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            detail = {
                "model": data.get("model"),
                "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
                "usage": data.get("usage"),
                "message_keys": sorted(msg.keys()) if isinstance(msg, dict) else [],
            }
            log.warning("OpenRouter empty content: %s", json.dumps(detail, separators=(",", ":"), ensure_ascii=True)[:500])
        raw_decision = _parse_json_text(content)
        return _coerce_decision(raw_decision)
    except Exception as e:
        log.warning("OpenRouter parse failed: %s", e)
        return AdvisorDecision(action="HOLD", reason=f"OpenRouter parse error: {e}")
