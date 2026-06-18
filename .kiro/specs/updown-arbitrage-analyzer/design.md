# Design: UP/DOWN Arbitrage Analyzer

## Overview

The UP/DOWN Arbitrage Analyzer is a pure-function decision engine that consumes a market snapshot (and optionally a position state) and emits a fixed-shape `AnalyzerDecision` record. It has no I/O, no side effects, and no dependency on network or disk. This keeps it trivially testable with property-based tests against the two simulation datasets that act as ground truth.

The engine plugs into the existing bot at the call site of `main.py` / `bot_runtime/app.py`. Those existing modules gather live market data from Polymarket (`market.py`), Gate/Binance (spot price), and the open orderbook, assemble a snapshot, call `analyze(...)`, and then route the returned action to the appropriate order-placement path. The runtime is also responsible for persisting decisions and wiring the Telegram / dashboard notifications.

Scope of this design is strictly the decision engine — its data model, pipeline, configuration, and test strategy. Order execution, broker clients, persistence, and UI integration are explicitly out of scope.

## Design Decisions

### D1 — Single flat module

The new module lives at the repo root: `arb_analyzer.py`. Existing bot modules are flat (`market.py`, `edge_signals.py`, `market_regime.py`, `config.py`). A package would fork convention without adding value at this size (one decision function, a config dataclass, three data classes, a confidence scorer, and helpers). Rule 11 — match the codebase's conventions.

### D2 — Pure function, total over inputs

`analyze(snapshot, position=None, cfg=None) -> AnalyzerDecision` never raises. On malformed input it returns a well-formed `AnalyzerDecision` with a conservative action (`SKIP_ENTRY`, `HOLD_LOCKED`, or `EMERGENCY_EXIT`) and a populated `REASON`. This satisfies Req 14 (fail-loud via explicit record, not via traceback) and Rule 12 (fail loud).

### D3 — Dataclasses, not dicts or pydantic

Input snapshots and the output decision are plain `@dataclass(frozen=True, slots=True)` structures. Matches `market.py`'s use of `@dataclass`, keeps typing strict, and avoids adding pydantic as a dependency. Frozen + slots also means snapshots are hashable and cheap to construct — useful for the property-based tests that generate many samples.

### D4 — Separate `EntryDecision` phase from `LockedDecision` phase

The top-level dispatch reads `POSITION_EXISTS` once and routes to one of two disjoint branches. There is no code path where both an entry action and an end-window action are evaluated in the same call. This makes Req 4.3 (no BUY when locked) and P4-no-entry-when-locked trivially true by construction.

### D5 — Confidence is a standalone function

`score_confidence(snapshot, win_leg_price, loss_leg_price, cfg) -> int` computes the 10-component score. It is exported as a separate function so tests can pin each component independently. It's also easier to audit — Rule 9 (tests verify intent).

### D6 — Downgrade ladder is explicit, not implicit

The worst-case enforcement from Req 11 is implemented as a small state machine that attempts `SELL_LOSS_AND_ADD_WIN` first, falls back to `SELL_LOSS_ONLY`, then `HOLD_LOCKED`, at each step checking the worst-case against the hard stop. The downgrade is logged in `REASON`. This is more verbose than a conditional but makes P11-no-silent-breach auditable.

### D7 — Configuration as a single immutable dataclass

All thresholds live in `AnalyzerConfig(frozen=True)`. `config.py` already loads env values into module-level constants, so the analyzer accepts an optional `AnalyzerConfig` arg and falls back to `AnalyzerConfig.default()` which reads sensible defaults. The runtime can override any field by constructing a custom config. No global mutable state.

### D8 — Existing files are not modified in this phase

Req integration: implementation adds only new files (`arb_analyzer.py`, tests). Call sites in `main.py` / `bot_runtime/app.py` are left untouched; wiring them up is a follow-up task. Rule 3 (surgical changes).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  bot_runtime / main.py                  │
│   (gathers live data, calls analyze, routes action)     │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │  arb_analyzer.analyze  │
              └────────────────────────┘
                           │
         ┌─────────────────┴─────────────────┐
         │                                   │
         ▼                                   ▼
 ┌───────────────┐                  ┌──────────────────┐
 │  _validate    │                  │  _decide_entry   │
 │  (Req 14)     │                  │  (Req 1,2,3)     │
 └───────┬───────┘                  └──────────────────┘
         │
         │ no position                       ┌──────────────────┐
         ├──────────────────────────────────▶│  _decide_entry   │
         │                                   └──────────────────┘
         │ position locked                   ┌──────────────────┐
         └──────────────────────────────────▶│  _decide_locked  │
                                             │  (Req 4–11)      │
                                             └────────┬─────────┘
                                                      │
                                                      ▼
                                             ┌──────────────────┐
                                             │  _assemble       │
                                             │  _decision       │
                                             │  (Req 12)        │
                                             └──────────────────┘
```

## Data Model

```python
@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    market_id: str
    time_remaining: float          # seconds until settlement
    target_price: float            # binary strike
    current_asset_price: float     # spot / feed price
    up_ask_price: float
    down_ask_price: float
    up_bid_price: float
    down_bid_price: float
    available_capital: float
    capital_per_leg: float
    orderbook_liquidity_up: float  # shares available within spread
    orderbook_liquidity_down: float
    spread_up: float               # best_ask - best_bid on UP leg
    spread_down: float
    price_feed_timestamp: float    # unix epoch seconds
    current_time: float            # unix epoch seconds
    recent_volatility: str         # "low" | "normal" | "high" | "extreme"
    recent_price_direction: str    # "toward_target" | "away_from_target" | "flat"

@dataclass(frozen=True, slots=True)
class PositionState:
    exists: bool
    initial_up_price: float
    initial_down_price: float
    shares_up: float
    shares_down: float
    capital_up: float
    capital_down: float
    current_up_price: float
    current_down_price: float
    current_up_bid: float
    current_down_bid: float

@dataclass(frozen=True, slots=True)
class AnalyzerConfig:
    max_total_price: float = 0.96
    strong_buy_cutoff: float = 0.94
    aggressive_cutoff: float = 0.97
    distance_threshold: float = 40.0
    time_remaining_threshold: float = 30.0
    min_win_leg_price_for_sell_loss: float = 0.85
    max_loss_leg_price_for_sell: float = 0.15
    min_win_leg_price_for_add: float = 0.70
    max_win_leg_price_for_add: float = 0.85
    never_add_above: float = 0.90
    max_additional_ratio: float = 0.20
    moderate_additional_ratio: float = 0.10
    hard_stop_loss: float = -25.0
    staleness_threshold_secs: float = 3.0
    max_spread_normal: float = 0.02
    max_spread_aggressive: float = 0.01
    liquidity_buffer: float = 1.2   # need 1.2x target_shares on book
    equal_shares_tol: float = 1e-6

    @staticmethod
    def default() -> "AnalyzerConfig": ...

@dataclass(frozen=True, slots=True)
class AnalyzerDecision:
    market_id: str
    market_status: str  # "no_position" | "locked" | "invalid"
    time_remaining: float
    target_price: float
    current_asset_price: float
    distance_from_target: float
    up_ask_price: float
    down_ask_price: float
    total_price: float
    gross_edge: float
    position_exists: bool
    shares_up: float
    shares_down: float
    total_initial_capital: float
    locked_profit: float
    locked_roi: float
    win_leg: str       # "UP" | "DOWN" | "UNDETERMINED"
    loss_leg: str      # "UP" | "DOWN" | "UNDETERMINED"
    win_leg_price: float
    loss_leg_price: float
    confidence_score: int
    recommended_action: str  # one of Action enum
    sell_loss_percent: float
    additional_capital: float
    expected_profit_if_correct: float
    worst_case_reversal_profit: float
    risk_level: str          # "LOW" | "MEDIUM" | "HIGH" | "DANGER"
    reason: str
```

The field order in `AnalyzerDecision` matches Req 12.1 exactly. Non-applicable fields use `0.0` for numerics and `""` for strings, with the documented sentinel captured in module-level constants for stability.

## Decision Pipeline

Ordered phases. Each phase can short-circuit by returning a decision; later phases never override earlier ones silently.

1. **Validate inputs** — `_validate(snapshot, position)` → returns optional fail-loud decision. Checks for NaN, infinite, negative where invalid, missing fields, price bounds. On failure, emits `SKIP_ENTRY` (no position) or `EMERGENCY_EXIT` (position corrupt). Req 13, Req 14.
2. **Branch on position** — if `position is None or not position.exists`: go to `_decide_entry`; else: go to `_decide_locked`. Req 4.
3. **Entry branch** (`_decide_entry`):
   - Detect staleness, thin book, wide spread → `SKIP_ENTRY` with cited reason. Req 3.
   - Compute `total_price`, `gross_edge`, `target_shares`, `locked_profit`. Req 1, Req 2.
   - Classify action by `total_price` band (aggressive band uses tighter safety thresholds). Req 1.2–1.5, Req 3.6.
   - Output: entry record.
4. **Locked branch** (`_decide_locked`):
   - Equal-shares check → `EMERGENCY_EXIT` if violated. Req 4.5, Req 13.
   - Detect `WIN_LEG` / `LOSS_LEG` from `current_asset_price` vs `target_price`. Req 5.
   - Compute `distance_from_target`. Req 5.6.
   - If `distance < distance_threshold` → `HOLD_LOCKED`. Req 6.
   - If `time_remaining > time_remaining_threshold` → `HOLD_LOCKED`. Req 7.
   - Compute `confidence_score`. Req 8.
   - If `confidence < 85` → `HOLD_LOCKED`. Req 8.3.
   - Evaluate candidate action ladder (Req 9, Req 10):
     - If `0.70 ≤ win_price ≤ 0.85` AND `loss_price ≤ 0.20` AND `confidence ≥ 90` AND `win_price ≤ 0.90` → candidate `SELL_LOSS_AND_ADD_WIN`.
     - Else if `win_price ≥ 0.85` AND `loss_price ≤ 0.15` AND `confidence ≥ 85` → candidate `SELL_LOSS_ONLY`.
     - Else → `HOLD_LOCKED`.
   - Compute worst case for the candidate. Req 9.7, Req 10.8.
   - If `worst_case < hard_stop`: downgrade ladder (`SELL_LOSS_AND_ADD_WIN` → `SELL_LOSS_ONLY` → `HOLD_LOCKED`), recomputing worst case at each step. Req 11.
   - Final action emitted.
5. **Assemble output** — `_assemble_decision(...)` populates every field in the Req 12 contract. Non-applicable fields use documented sentinels.

### Pseudocode

```python
def analyze(snapshot, position=None, cfg=None):
    cfg = cfg or AnalyzerConfig.default()

    invalid = _validate(snapshot, position, cfg)
    if invalid is not None:
        return invalid

    if position is None or not position.exists:
        return _decide_entry(snapshot, cfg)
    else:
        return _decide_locked(snapshot, position, cfg)
```

### Entry branch

```python
def _decide_entry(s, cfg):
    total = s.up_ask_price + s.down_ask_price
    gross = 1.0 - total
    target_shares = min(s.capital_per_leg / s.up_ask_price,
                        s.capital_per_leg / s.down_ask_price)
    total_capital = target_shares * (s.up_ask_price + s.down_ask_price)
    locked_profit = target_shares - total_capital
    locked_roi = locked_profit / total_capital if total_capital > 0 else 0.0

    guard = _entry_safety(s, target_shares, cfg, band_is_aggressive=(total > cfg.max_total_price))
    if guard is not None:
        return _assemble_entry(s, total, gross, target_shares, total_capital,
                               locked_profit, locked_roi,
                               action="SKIP_ENTRY", reason=guard)

    if total <= cfg.strong_buy_cutoff:
        action = "STRONG_BUY_BOTH_LEGS"
    elif total <= cfg.max_total_price:
        action = "BUY_BOTH_LEGS"
    elif total <= cfg.aggressive_cutoff:
        action = "AGGRESSIVE_BUY_ONLY_IF_EXECUTION_CLEAN"
    else:
        action = "SKIP_ENTRY"

    reason = _entry_reason(total, action, cfg)
    return _assemble_entry(s, total, gross, target_shares, total_capital,
                           locked_profit, locked_roi, action, reason)
```

### Locked branch

```python
def _decide_locked(s, p, cfg):
    if abs(p.shares_up - p.shares_down) > cfg.equal_shares_tol:
        return _emergency("unequal legs detected", s, p, cfg)

    shares_per_leg = p.shares_up
    total_initial = p.capital_up + p.capital_down
    locked_profit = shares_per_leg - total_initial
    locked_roi = locked_profit / total_initial if total_initial > 0 else 0.0

    win_leg, loss_leg = _classify_legs(s)
    distance = abs(s.current_asset_price - s.target_price)

    win_price, loss_price, loss_bid = _leg_prices(s, win_leg)

    # Hard gates (Req 6, Req 7)
    if distance < cfg.distance_threshold:
        return _hold(s, p, win_leg, loss_leg, win_price, loss_price,
                     shares_per_leg, total_initial, locked_profit, locked_roi,
                     confidence=0,
                     reason=f"distance={distance:.2f} < {cfg.distance_threshold}")
    if s.time_remaining > cfg.time_remaining_threshold:
        return _hold(..., reason=f"time_remaining={s.time_remaining} > {cfg.time_remaining_threshold}")

    # Confidence + action ladder
    conf = score_confidence(s, win_price, loss_price, cfg)
    if conf < 85:
        return _hold(..., confidence=conf, reason=f"confidence={conf} < 85")

    candidate = _choose_candidate(win_price, loss_price, conf, cfg)
    # candidate is one of: SELL_LOSS_AND_ADD_WIN, SELL_LOSS_ONLY, HOLD_LOCKED

    # Downgrade ladder (Req 11)
    final = _apply_worst_case_ladder(candidate, s, p, win_leg, loss_leg,
                                     win_price, loss_bid, conf, cfg)
    return _assemble_locked(final)
```

### Confidence scoring

```python
def score_confidence(s, win_price, loss_price, cfg) -> int:
    score = 0
    if s.time_remaining <= 30: score += 10
    if s.time_remaining <= 20: score += 10
    distance = abs(s.current_asset_price - s.target_price)
    if distance >= 40: score += 15
    if distance >= 60: score += 10
    if win_price >= 0.85: score += 15
    if loss_price <= 0.15: score += 15
    if s.recent_price_direction == "away_from_target": score += 10
    if s.recent_volatility == "low": score += 10
    if s.spread_up <= cfg.max_spread_normal and s.spread_down <= cfg.max_spread_normal: score += 5
    if (s.orderbook_liquidity_up >= cfg.liquidity_buffer * 1.0 and
        s.orderbook_liquidity_down >= cfg.liquidity_buffer * 1.0):
        score += 5
    return min(score, 100)
```

Each component is a one-liner — trivially unit-testable and matches Req 8.1 exactly.

### Downgrade ladder

```python
def _apply_worst_case_ladder(candidate, s, p, win_leg, loss_leg,
                             win_price, loss_bid, conf, cfg):
    trail = []
    for candidate in (candidate, *_ladder_below(candidate)):
        result = _evaluate(candidate, ...)
        if result.worst_case >= cfg.hard_stop_loss:
            result.reason = _with_trail(result.reason, trail)
            return result
        trail.append((candidate, result.worst_case))
    # Fell all the way through: HOLD_LOCKED
    return _hold(..., reason=f"worst case breaches hard stop in all candidates: {trail}")
```

`_ladder_below("SELL_LOSS_AND_ADD_WIN")` yields `("SELL_LOSS_ONLY", "HOLD_LOCKED")`; `_ladder_below("SELL_LOSS_ONLY")` yields `("HOLD_LOCKED",)`; `_ladder_below("HOLD_LOCKED")` yields `()`.

### Fail-loud (validation)

```python
def _validate(s, p, cfg):
    errors = []
    for name, val in _numeric_fields(s):
        if math.isnan(val) or math.isinf(val):
            errors.append(f"{name}={val}")
    if s.time_remaining < 0:
        errors.append(f"time_remaining={s.time_remaining}")
    if s.up_ask_price <= 0 or s.up_ask_price > 1:
        errors.append(f"up_ask_price={s.up_ask_price}")
    # ... similar checks
    if not errors:
        return None
    if p is not None and p.exists:
        return _emergency("; ".join(errors), s, p, cfg)
    return _skip_entry(s, cfg, reason="invalid inputs: " + "; ".join(errors))
```

## Integration

### Call site contract (for the runtime)

```python
from arb_analyzer import analyze, MarketSnapshot, PositionState, AnalyzerConfig

snapshot = MarketSnapshot(
    market_id=token_meta["slug"],
    time_remaining=settlement_ts - time.time(),
    target_price=target,
    current_asset_price=btc_spot,
    up_ask_price=book_up.best_ask,
    down_ask_price=book_down.best_ask,
    # … etc
)
decision = analyze(snapshot, position_state)
if decision.recommended_action in ("STRONG_BUY_BOTH_LEGS", "BUY_BOTH_LEGS"):
    place_dual_leg_order(decision)
elif decision.recommended_action == "SELL_LOSS_ONLY":
    …
```

This design does not change `main.py`, `bot_runtime/app.py`, `market.py`, or any other existing file. Wiring the call site is a separate follow-up task.

### Configuration

`AnalyzerConfig.default()` returns the dataclass with the defaults from Section 3 of the requirements. Env-driven overrides are the runtime's responsibility; the analyzer itself has no dependency on `os.environ`. This keeps test setup minimal — every test constructs its own config explicitly.

## Test Strategy

Tests live under `tests/arb_analyzer/` and are pure offline tests — no network, no disk outside of committed fixtures.

### Layer 1 — unit tests (pytest)

- `test_entry_bands.py` — one test per action band (STRONG_BUY / BUY / AGGRESSIVE / SKIP) using hand-picked prices at band boundaries.
- `test_equal_shares.py` — verifies `shares_up == shares_down` within `equal_shares_tol` across a handful of snapshots.
- `test_win_loss_detection.py` — 3 cases: above target, below target, equal.
- `test_confidence_components.py` — one test per confidence component, pinning each score delta.
- `test_fail_loud.py` — covers NaN, inf, negative time, invalid price, missing fields; every case returns a well-formed decision with a populated `reason` and an action in `{SKIP_ENTRY, HOLD_LOCKED, EMERGENCY_EXIT}`.

### Layer 2 — property-based tests (hypothesis)

- `test_properties.py` covers P1–P14 from requirements:
  - `P1-entry-monotone`: generate pairs of snapshots with identical safety signals; assert action ordering.
  - `P1-edge-sign`: generate any snapshot; if action ∈ {STRONG_BUY, BUY}, assert `gross_edge > 0`.
  - `P2-equal-shares`: generate any entry snapshot; assert `shares_up == shares_down` within tol.
  - `P2-capital-bound`: assert `total_initial_capital <= 2 * capital_per_leg`.
  - `P4-no-entry-when-locked`: generate any snapshot + locked position; assert action not in entry set.
  - `P6-near-target-hold`: generate any locked snapshot with `distance < 40`; assert `HOLD_LOCKED`.
  - `P7-pre-window-hold`: `time_remaining > 30` + locked ⇒ `HOLD_LOCKED`.
  - `P8-bounds`: `0 <= confidence <= 100` for any input.
  - `P8-monotone-time`, `P8-monotone-distance`: shrink time or grow distance, score never decreases.
  - `P10-never-above-0.90`: `win_price > 0.90` ⇒ action ≠ `SELL_LOSS_AND_ADD_WIN`.
  - `P10-capital-cap`: `additional_capital <= 0.20 * total_initial_capital`.
  - `P11-no-silent-breach`: action ∈ sell-variants ⇒ `worst_case >= hard_stop`.
  - `P12-schema-stable`, `P12-enum-closed`: assert field set and enum membership.
  - `P14-total-function`, `P14-reason-always-present`: for any (possibly malformed) input, call succeeds and `reason != ""`.

### Layer 3 — anchored tests against simulation datasets

- `tests/fixtures/arbitrage_sim.csv` — derived from `simulasi-arbitrase-polymarket-up-down-035-075.xlsx`. Columns: `up_price`, `down_price`, `expected_action`.
- `tests/fixtures/end_window_sim.csv` — derived from `simulasi-bot-sell-tambah-akhir-window.xlsx`. Columns: `time_remaining`, `distance`, `win_price`, `loss_price`, `expected_action`, `expected_profit`, `worst_case`.
- `test_sim_arbitrage.py` loops over every row of `arbitrage_sim.csv`, builds a minimal clean snapshot (all safety signals OK), asserts the analyzer agrees with the label.
- `test_sim_end_window.py` loops over every row of `end_window_sim.csv`, builds a locked snapshot with the parameterized win/loss prices, asserts the analyzer agrees with the labeled action and matches the expected / worst-case numbers within `0.05` absolute tolerance.

### Fixture generation

A small one-off script (`tests/fixtures/_generate.py`, committed) reads the xlsx files and emits the CSVs. It is run once and its output is committed. The script is not part of the test run — tests only consume the CSVs. Keeps tests fast and hermetic.

## Files Added

```
arb_analyzer.py
tests/arb_analyzer/__init__.py
tests/arb_analyzer/test_entry_bands.py
tests/arb_analyzer/test_equal_shares.py
tests/arb_analyzer/test_win_loss_detection.py
tests/arb_analyzer/test_confidence_components.py
tests/arb_analyzer/test_fail_loud.py
tests/arb_analyzer/test_properties.py
tests/arb_analyzer/test_sim_arbitrage.py
tests/arb_analyzer/test_sim_end_window.py
tests/fixtures/arbitrage_sim.csv
tests/fixtures/end_window_sim.csv
tests/fixtures/_generate.py
```

## Files Modified

None in this phase. Wiring into `main.py` / `bot_runtime/app.py` is a deliberate follow-up task so this phase can ship, get tested, and pass review independently. Rule 3 (surgical changes) — touch only what we must.

## Risks & Open Questions

1. **Sim-dataset labels use different safety-signal defaults** than the analyzer will see in production. The sim rows don't include spread, liquidity, or volatility — they assume "clean". The anchored tests therefore construct clean snapshots. This is fine for validating the core math but does not validate guard behavior. The property-based tests cover guard interactions separately.
2. **`RISK_LEVEL` thresholds** are mentioned in Req 11 but specific numeric cutoffs aren't pinned. Proposed defaults for the implementation:
   - `LOW` if `worst_case >= locked_profit`
   - `MEDIUM` if `0 <= worst_case < locked_profit`
   - `HIGH` if `hard_stop <= worst_case < 0`
   - `DANGER` if `worst_case < hard_stop`
   Confirm during task execution if these match intent.
3. **`DISTANCE_FROM_TARGET` units** — the sim dataset uses 40/60/100 thresholds. The requirement treats these as USD against the spot price. If your runtime passes a normalized distance (e.g. percent) the default thresholds need adjustment. Flag during integration.
