# Requirements: UP/DOWN Arbitrage Analyzer

## Introduction

The UP/DOWN Arbitrage Analyzer is a pure-function decision engine for Polymarket's 5-minute binary UP/DOWN markets. It consumes a snapshot of market state and, when applicable, an existing hedged position, and emits a fixed-shape decision record that the trading runtime can act on.

It has two operational modes that are mutually exclusive at any given moment:

1. **Entry mode** — no position exists. The analyzer evaluates whether a risk-free arbitrage is available by checking whether `UP_ASK_PRICE + DOWN_ASK_PRICE` is low enough to guarantee a positive payout regardless of outcome, and recommends one of the BUY actions (or SKIP).
2. **Post-entry / end-window mode** — a hedged position already exists with equal shares on both legs. The analyzer defaults to holding the locked profit, but in the final seconds of the window it may recommend selectively unwinding the losing leg (SELL_LOSS_ONLY) or unwinding and augmenting the winning leg (SELL_LOSS_AND_ADD_WIN) when confidence is high and directional signal is strong.

The analyzer is deliberately stateless and deterministic — all inputs are passed in; all outputs derive purely from those inputs. Execution side effects (placing orders, syncing to brokers) are explicitly out of scope and live in the runtime layer. This keeps the decision engine testable with property-based tests against the two simulation datasets that act as ground truth:

- An arbitrage-entry dataset of 1681 UP×DOWN price combinations labeling BUY / WATCH / SKIP / DANGER by total price.
- An end-window dataset of 80 scenarios parameterized by `(time_remaining, distance, win_leg_price, loss_leg_price)` labeling HOLD_LOCKED / SELL_LOSS_ONLY / SELL_LOSS+ADD_WIN with expected profit and worst-case reversal.

## Glossary

- **UP leg / DOWN leg**: The two binary outcome shares of a Polymarket UP/DOWN market. Exactly one pays $1 at settlement; the other pays $0.
- **TOTAL_PRICE**: `UP_ASK_PRICE + DOWN_ASK_PRICE`. When `< 1`, an arbitrage opportunity exists.
- **GROSS_EDGE**: `1 - TOTAL_PRICE`. Per-share profit at settlement ignoring fees.
- **LOCKED position**: A position holding equal shares on UP and DOWN after successful dual-leg fill. Payout at settlement equals `SHARES_PER_LEG` regardless of outcome.
- **LOCKED_PROFIT**: `SHARES_PER_LEG - TOTAL_INITIAL_CAPITAL`. The guaranteed profit of a LOCKED position held to settlement.
- **WIN_LEG / LOSS_LEG**: Classification based on whether `CURRENT_ASSET_PRICE` is above or below `TARGET_PRICE` at decision time. The WIN leg is the one expected to pay; the LOSS leg is expected to expire worthless.
- **DISTANCE_FROM_TARGET**: `abs(CURRENT_ASSET_PRICE - TARGET_PRICE)`. Used as a proxy for directional certainty.
- **CONFIDENCE_SCORE**: A 0–100 score combining 10 observable signals (time, distance, prices, direction, volatility, spread, liquidity, feed freshness). Gates which end-window actions are allowed.
- **End window**: The final `TIME_REMAINING_THRESHOLD` seconds of the 5-minute market (default 30 s). Only in this window may SELL_LOSS_ONLY or SELL_LOSS_AND_ADD_WIN be considered.
- **MAX_TOTAL_PRICE**: Entry cutoff, default `0.96`. Configurable per mode (conservative `0.94`, aggressive `0.97`).
- **SELL_LOSS_PERCENT**: Fraction of the LOSS_LEG to unwind in SELL_LOSS_ONLY (0.50, 0.75, or 1.00 depending on confidence band).
- **Equal-shares invariant**: After a successful entry, `SHARES_UP == SHARES_DOWN` within a small float tolerance.
- **Fail-loud**: On any invalid / missing input or internal inconsistency, the analyzer returns a conservative action (`HOLD_LOCKED` or `SKIP_ENTRY`) and records the precise violation in `REASON`; it never silently returns a permissive action.

---

## Requirements

### Requirement 1: Arbitrage Entry Decision

**User Story:** As a bot operator, I want the analyzer to classify an entry opportunity from raw UP/DOWN ask prices, so that the runtime only attempts entries with a positive guaranteed edge.

### Acceptance Criteria

1. WHEN `POSITION_EXISTS` is false THE analyzer SHALL compute `TOTAL_PRICE = UP_ASK_PRICE + DOWN_ASK_PRICE` and `GROSS_EDGE = 1 - TOTAL_PRICE`.
2. IF `TOTAL_PRICE <= 0.94` THEN THE `RECOMMENDED_ACTION` SHALL be `STRONG_BUY_BOTH_LEGS`.
3. IF `0.94 < TOTAL_PRICE <= 0.96` THEN THE `RECOMMENDED_ACTION` SHALL be `BUY_BOTH_LEGS`.
4. IF `0.96 < TOTAL_PRICE <= 0.97` THEN THE `RECOMMENDED_ACTION` SHALL be `AGGRESSIVE_BUY_ONLY_IF_EXECUTION_CLEAN`.
5. IF `TOTAL_PRICE > 0.97` THEN THE `RECOMMENDED_ACTION` SHALL be `SKIP_ENTRY`.
6. IF `TOTAL_PRICE >= 1.0` THEN THE `RECOMMENDED_ACTION` SHALL be `SKIP_ENTRY` AND THE `REASON` SHALL explicitly state that no arbitrage exists.
7. THE `GROSS_EDGE` field SHALL always be included in the output, even when the action is `SKIP_ENTRY`, so downstream observers can distinguish "no edge" from "edge exists but blocked by safety guard".

### Correctness Properties

- **P1-entry-monotone**: For any two snapshots `a` and `b` with identical safety signals, if `a.TOTAL_PRICE <= b.TOTAL_PRICE`, then the action class for `a` is at least as permissive as for `b` under the ordering `STRONG_BUY ≻ BUY ≻ AGGRESSIVE ≻ SKIP`.
- **P1-sim-anchors**: For every row in the arbitrage simulation dataset, the analyzer's classification agrees with the dataset's `Aksi Bot` label (BUY / WATCH / AGGRESSIVE / SKIP / DANGER) when safety signals are clean.
- **P1-edge-sign**: IF the action is in `{STRONG_BUY_BOTH_LEGS, BUY_BOTH_LEGS}` THEN `GROSS_EDGE > 0`.

---

### Requirement 2: Equal-Shares Sizing

**User Story:** As a bot operator, I want the analyzer to size both legs to the same share count, so that the post-entry position is a true hedge rather than a directional bet.

### Acceptance Criteria

1. WHEN the recommended action is any BUY variant THE analyzer SHALL compute `TARGET_SHARES = min(CAPITAL_PER_LEG / UP_ASK_PRICE, CAPITAL_PER_LEG / DOWN_ASK_PRICE)`.
2. THE analyzer SHALL compute `MODAL_UP = TARGET_SHARES * UP_ASK_PRICE` and `MODAL_DOWN = TARGET_SHARES * DOWN_ASK_PRICE`.
3. THE `TOTAL_INITIAL_CAPITAL` SHALL equal `MODAL_UP + MODAL_DOWN` and SHALL NOT exceed `2 * CAPITAL_PER_LEG`.
4. THE `SHARES_UP` and `SHARES_DOWN` output fields SHALL both equal `TARGET_SHARES`.
5. THE `LOCKED_PROFIT` output SHALL equal `TARGET_SHARES - TOTAL_INITIAL_CAPITAL`.
6. THE `LOCKED_ROI` output SHALL equal `LOCKED_PROFIT / TOTAL_INITIAL_CAPITAL`.

### Correctness Properties

- **P2-equal-shares**: For any entry action, `SHARES_UP == SHARES_DOWN` within `1e-9` absolute tolerance.
- **P2-capital-bound**: `TOTAL_INITIAL_CAPITAL <= 2 * CAPITAL_PER_LEG` always.
- **P2-locked-profit-sign**: IF action is `STRONG_BUY_BOTH_LEGS` or `BUY_BOTH_LEGS` THEN `LOCKED_PROFIT > 0`.
- **P2-sim-anchor-0.47**: For `UP=0.47`, `DOWN=0.47`, `CAPITAL_PER_LEG=100`, `TARGET_SHARES ≈ 212.766`, `LOCKED_PROFIT ≈ 12.766`, `LOCKED_ROI ≈ 6.38%` — matching the end-window simulation's initial state.

---

### Requirement 3: Entry Safety Guards

**User Story:** As a bot operator, I want the analyzer to reject entries with stale or illiquid book state, so that a nominally profitable arbitrage isn't wrecked by partial fills or price moves.

### Acceptance Criteria

1. IF `PRICE_FEED_TIMESTAMP` is older than a configured staleness threshold relative to `CURRENT_TIME` THEN THE action SHALL be `SKIP_ENTRY` regardless of `TOTAL_PRICE`.
2. IF `ORDERBOOK_LIQUIDITY_UP` is below the sizing needed for `TARGET_SHARES` THEN THE action SHALL be `SKIP_ENTRY`.
3. IF `ORDERBOOK_LIQUIDITY_DOWN` is below the sizing needed for `TARGET_SHARES` THEN THE action SHALL be `SKIP_ENTRY`.
4. IF `SPREAD_UP` or `SPREAD_DOWN` exceeds a configured maximum THEN THE action SHALL be `SKIP_ENTRY`.
5. WHEN any safety guard downgrades an action to `SKIP_ENTRY` THE `REASON` SHALL explicitly name the failing guard (stale feed, thin UP book, thin DOWN book, wide UP spread, wide DOWN spread).
6. WHERE `TOTAL_PRICE` is in the aggressive band `(0.96, 0.97]` THE analyzer SHALL require strictly tighter safety thresholds (smaller allowed spread, larger liquidity buffer) than in the normal band.

### Correctness Properties

- **P3-guard-dominates-edge**: A failing safety guard always overrides a positive `GROSS_EDGE`; there exists no input where `SKIP_ENTRY` is emitted but `REASON` does not cite a specific blocker.
- **P3-guard-idempotent**: Re-evaluating the same snapshot produces the same guard outcome (no randomness in safety decisions).

---

### Requirement 4: Locked-Position Management

**User Story:** As a bot operator, I want the default action on a LOCKED position to be HOLD_LOCKED, so that we never throw away a guaranteed profit to chase an incremental one.

### Acceptance Criteria

1. WHEN `POSITION_EXISTS` is true AND `SHARES_UP == SHARES_DOWN` (within tolerance) THE analyzer SHALL treat the position as LOCKED.
2. WHEN a position is LOCKED AND no end-window conditions (Req 7) are met THE `RECOMMENDED_ACTION` SHALL be `HOLD_LOCKED`.
3. WHEN a position is LOCKED THE analyzer SHALL NOT emit any BUY variant action.
4. THE `LOCKED_PROFIT` output SHALL equal `SHARES_PER_LEG - TOTAL_INITIAL_CAPITAL` computed from the position's initial state, not from current ask prices.
5. IF `SHARES_UP != SHARES_DOWN` by more than tolerance on an existing position THE analyzer SHALL emit `EMERGENCY_EXIT` AND THE `REASON` SHALL state "unequal legs detected".

### Correctness Properties

- **P4-no-entry-when-locked**: For any input with `POSITION_EXISTS == true`, the action is never in `{BUY_BOTH_LEGS, STRONG_BUY_BOTH_LEGS, AGGRESSIVE_BUY_ONLY_IF_EXECUTION_CLEAN}`.
- **P4-locked-profit-stable**: `LOCKED_PROFIT` depends only on `SHARES_PER_LEG` and `TOTAL_INITIAL_CAPITAL`; it is invariant under changes to current market prices.

---

### Requirement 5: Win/Loss Leg Detection

**User Story:** As a bot operator, I want the analyzer to classify which leg is winning based on the asset's position relative to target, so that end-window decisions operate on the correct leg.

### Acceptance Criteria

1. IF `CURRENT_ASSET_PRICE > TARGET_PRICE` THEN `WIN_LEG = UP` AND `LOSS_LEG = DOWN`.
2. IF `CURRENT_ASSET_PRICE < TARGET_PRICE` THEN `WIN_LEG = DOWN` AND `LOSS_LEG = UP`.
3. IF `CURRENT_ASSET_PRICE == TARGET_PRICE` THEN `WIN_LEG` and `LOSS_LEG` SHALL be classified as `UNDETERMINED` AND the action SHALL be `HOLD_LOCKED`.
4. THE `WIN_LEG_PRICE` SHALL be sourced from the current price of the winning leg (ask price for buys, bid price for sells, as appropriate to the action being evaluated).
5. THE `LOSS_LEG_PRICE` SHALL be sourced symmetrically from the losing leg.
6. THE `DISTANCE_FROM_TARGET` output SHALL equal `abs(CURRENT_ASSET_PRICE - TARGET_PRICE)`.

### Correctness Properties

- **P5-exhaustive**: For any valid `(CURRENT_ASSET_PRICE, TARGET_PRICE)`, exactly one of `WIN_LEG ∈ {UP, DOWN, UNDETERMINED}` holds.
- **P5-symmetry**: Swapping the meaning of UP/DOWN and flipping the target-comparison direction yields the same `DISTANCE_FROM_TARGET` and mirrored `WIN_LEG`.

---

### Requirement 6: Distance-from-Target Gating

**User Story:** As a bot operator, I want the analyzer to refuse to unlock the hedge when the asset is too close to the target price, so that we don't surrender a locked profit for a marginal directional bet.

### Acceptance Criteria

1. WHEN a position is LOCKED AND `DISTANCE_FROM_TARGET < 40` (configurable `DISTANCE_THRESHOLD`) THE action SHALL be `HOLD_LOCKED`.
2. THE `REASON` in that case SHALL explicitly cite `DISTANCE_FROM_TARGET` and its current value relative to the threshold.
3. THE `DISTANCE_THRESHOLD` SHALL be configurable but default to `40`.

### Correctness Properties

- **P6-near-target-hold**: For any LOCKED input with `DISTANCE_FROM_TARGET < DISTANCE_THRESHOLD`, the action is always `HOLD_LOCKED`, regardless of confidence.

---

### Requirement 7: End-Window Activation

**User Story:** As a bot operator, I want SELL_LOSS_ONLY and SELL_LOSS_AND_ADD_WIN to be considered only in the final seconds of the window, so that we don't take directional risk with time still to reverse.

### Acceptance Criteria

1. WHEN a position is LOCKED AND `TIME_REMAINING > 30` (configurable `TIME_REMAINING_THRESHOLD`) THE action SHALL be `HOLD_LOCKED`.
2. THE end-window maximizer (Req 9 and Req 10) SHALL only be evaluated when BOTH `TIME_REMAINING <= TIME_REMAINING_THRESHOLD` AND `DISTANCE_FROM_TARGET >= DISTANCE_THRESHOLD`.
3. THE `TIME_REMAINING_THRESHOLD` SHALL be configurable but default to `30` seconds.

### Correctness Properties

- **P7-pre-window-hold**: For any LOCKED input with `TIME_REMAINING > TIME_REMAINING_THRESHOLD`, the action is `HOLD_LOCKED`.
- **P7-sim-anchors**: For every row in the end-window simulation where `TIME_REMAINING == 45`, the action is `HOLD_LOCKED` (matches the dataset's ground-truth labels).

---

### Requirement 8: Confidence Scoring

**User Story:** As a bot operator, I want a transparent 0–100 confidence score combining the observable signals, so that end-window action selection is rule-based and auditable.

### Acceptance Criteria

1. THE analyzer SHALL compute `CONFIDENCE_SCORE` as the sum of component contributions capped at `100`:
   - `+10` if `TIME_REMAINING <= 30`
   - `+10` additional if `TIME_REMAINING <= 20`
   - `+15` if `DISTANCE_FROM_TARGET >= 40`
   - `+10` additional if `DISTANCE_FROM_TARGET >= 60`
   - `+15` if `WIN_LEG_PRICE >= 0.85`
   - `+15` if `LOSS_LEG_PRICE <= 0.15`
   - `+10` if `RECENT_PRICE_DIRECTION` is moving away from target
   - `+10` if `RECENT_VOLATILITY` is classified as low
   - `+5` if `SPREAD_UP` and `SPREAD_DOWN` are both below the tight-spread threshold
   - `+5` if `ORDERBOOK_LIQUIDITY_UP` and `_DOWN` both clear the adequate-liquidity threshold
2. THE `CONFIDENCE_SCORE` SHALL be output as an integer in `[0, 100]`.
3. IF `CONFIDENCE_SCORE < 85` THEN THE action SHALL be `HOLD_LOCKED` regardless of price conditions.
4. THE weighting of each component SHALL be configurable but default to the values above.

### Correctness Properties

- **P8-bounds**: `0 <= CONFIDENCE_SCORE <= 100` always.
- **P8-monotone-time**: Decreasing `TIME_REMAINING` (without crossing a threshold in the wrong direction) never decreases the score.
- **P8-monotone-distance**: Increasing `DISTANCE_FROM_TARGET` never decreases the score.
- **P8-low-confidence-hold**: For any LOCKED input with `CONFIDENCE_SCORE < 85`, the action is `HOLD_LOCKED`.

---

### Requirement 9: SELL_LOSS_ONLY Decision

**User Story:** As a bot operator, I want the analyzer to scale out of the losing leg when the market has strongly committed to a direction, so that we convert near-certain dead shares into cash.

### Acceptance Criteria

1. THE analyzer SHALL evaluate SELL_LOSS_ONLY only when ALL of the following hold: `POSITION_EXISTS`, `TIME_REMAINING <= 30`, `DISTANCE_FROM_TARGET >= 40`, `WIN_LEG_PRICE >= 0.85`, `LOSS_LEG_PRICE <= 0.15`, `CONFIDENCE_SCORE >= 85`, all safety guards pass.
2. IF `85 <= CONFIDENCE_SCORE < 90` THEN `SELL_LOSS_PERCENT = 0.50`.
3. IF `90 <= CONFIDENCE_SCORE < 95` THEN `SELL_LOSS_PERCENT = 0.75`.
4. IF `CONFIDENCE_SCORE >= 95` THEN `SELL_LOSS_PERCENT = 1.00`.
5. THE analyzer SHALL compute `SHARES_TO_SELL = SHARES_LOSS * SELL_LOSS_PERCENT`, `CASH_FROM_SELL_LOSS = SHARES_TO_SELL * LOSS_LEG_BID_PRICE`, `REMAINING_LOSS_SHARES = SHARES_LOSS - SHARES_TO_SELL`.
6. THE analyzer SHALL compute `EXPECTED_PROFIT_IF_CORRECT = SHARES_WIN + CASH_FROM_SELL_LOSS - TOTAL_INITIAL_CAPITAL` (REMAINING_LOSS_SHARES valued at 0).
7. THE analyzer SHALL compute `WORST_CASE_REVERSAL_PROFIT = REMAINING_LOSS_SHARES + CASH_FROM_SELL_LOSS - TOTAL_INITIAL_CAPITAL`.
8. IF `WORST_CASE_REVERSAL_PROFIT` is below the configured hard stop loss THEN THE action SHALL be `HOLD_LOCKED` AND THE `REASON` SHALL cite the worst-case breach.
9. WHEN all conditions are met AND worst case is within bounds THE `RECOMMENDED_ACTION` SHALL be `SELL_LOSS_ONLY`.

### Correctness Properties

- **P9-percent-band**: `SELL_LOSS_PERCENT ∈ {0.50, 0.75, 1.00}` exactly, and matches the confidence band.
- **P9-profit-gte-locked**: IF action is `SELL_LOSS_ONLY` THEN `EXPECTED_PROFIT_IF_CORRECT >= LOCKED_PROFIT` (otherwise unlocking is pointless).
- **P9-sim-anchors**: For end-window rows labeled `SELL_LOSS_ONLY` at `win=0.90, loss=0.10`, the analyzer produces `SELL_LOSS_ONLY` and `EXPECTED_PROFIT_IF_CORRECT ≈ 33.99`, `WORST_CASE ≈ -178.78`.

---

### Requirement 10: SELL_LOSS_AND_ADD_WIN Decision

**User Story:** As a bot operator, I want the analyzer to optionally augment the winning leg when confidence is very high and the winning price still has room to pay out, so that we capture more upside on high-conviction signals.

### Acceptance Criteria

1. THE analyzer SHALL evaluate SELL_LOSS_AND_ADD_WIN only when ALL of the following hold: `POSITION_EXISTS`, `TIME_REMAINING <= 30`, `DISTANCE_FROM_TARGET >= 40`, `0.70 <= WIN_LEG_PRICE <= 0.85`, `LOSS_LEG_PRICE <= 0.20`, `CONFIDENCE_SCORE >= 90`, `RECENT_VOLATILITY` not extreme, all safety guards pass.
2. IF `WIN_LEG_PRICE > 0.90` THEN the analyzer SHALL NOT recommend adding to the winning leg, regardless of confidence.
3. IF `90 <= CONFIDENCE_SCORE < 95` THEN `MAX_ADDITIONAL_CAPITAL = 0.10 * TOTAL_INITIAL_CAPITAL`.
4. IF `CONFIDENCE_SCORE >= 95` THEN `MAX_ADDITIONAL_CAPITAL = 0.20 * TOTAL_INITIAL_CAPITAL`.
5. THE `ADDITIONAL_CAPITAL` SHALL never exceed `0.20 * TOTAL_INITIAL_CAPITAL`.
6. THE analyzer SHALL compute `ADDITIONAL_SHARES = ADDITIONAL_CAPITAL / WIN_LEG_ASK_PRICE`, `TOTAL_WIN_SHARES = SHARES_WIN + ADDITIONAL_SHARES`, `CASH_FROM_SELL_LOSS = SHARES_TO_SELL * LOSS_LEG_BID_PRICE`, `TOTAL_CAPITAL_AFTER_ADD = TOTAL_INITIAL_CAPITAL + ADDITIONAL_CAPITAL`.
7. THE analyzer SHALL compute `EXPECTED_PROFIT_IF_CORRECT = TOTAL_WIN_SHARES + CASH_FROM_SELL_LOSS - TOTAL_CAPITAL_AFTER_ADD`.
8. THE analyzer SHALL compute `WORST_CASE_REVERSAL_PROFIT = CASH_FROM_SELL_LOSS + REMAINING_LOSS_SHARES - TOTAL_CAPITAL_AFTER_ADD`.
9. IF `WORST_CASE_REVERSAL_PROFIT` is below the configured hard stop loss THEN THE action SHALL be downgraded to `SELL_LOSS_ONLY` if that passes its own checks, else `HOLD_LOCKED`.
10. WHEN all conditions are met AND worst case is within bounds THE `RECOMMENDED_ACTION` SHALL be `SELL_LOSS_AND_ADD_WIN`.

### Correctness Properties

- **P10-never-above-0.90**: IF `WIN_LEG_PRICE > 0.90` THEN action is never `SELL_LOSS_AND_ADD_WIN`.
- **P10-capital-cap**: `ADDITIONAL_CAPITAL <= 0.20 * TOTAL_INITIAL_CAPITAL` always.
- **P10-confidence-required**: IF action is `SELL_LOSS_AND_ADD_WIN` THEN `CONFIDENCE_SCORE >= 90`.
- **P10-sim-anchors**: For end-window rows labeled `SELL_LOSS + ADD_WIN` at `win=0.85, loss=0.15`, the analyzer produces `SELL_LOSS_AND_ADD_WIN` and `EXPECTED_PROFIT_IF_CORRECT ≈ 51.66`, `WORST_CASE ≈ -208.16`.

---

### Requirement 11: Worst-Case Reversal Enforcement

**User Story:** As a bot operator, I want a hard-stop-loss guard on every unlocking action, so that no combination of signals can drive the position into catastrophic loss.

### Acceptance Criteria

1. THE analyzer SHALL always compute `WORST_CASE_REVERSAL_PROFIT` for any action in `{SELL_LOSS_ONLY, SELL_LOSS_AND_ADD_WIN}`.
2. WHEN `WORST_CASE_REVERSAL_PROFIT < HARD_STOP_LOSS` (configurable, default `-25 USDC`) THE action SHALL be downgraded per the following ladder: SELL_LOSS_AND_ADD_WIN → SELL_LOSS_ONLY → HOLD_LOCKED.
3. THE `RISK_LEVEL` output field SHALL reflect the worst-case magnitude: `LOW` when worst case `>=` locked profit, `MEDIUM` when worst case is between locked profit and hard stop, `HIGH` near the hard stop, `DANGER` if it would breach the hard stop.
4. WHEN a downgrade occurs THE `REASON` SHALL record both the original candidate action AND the worst-case value that triggered the downgrade.

### Correctness Properties

- **P11-no-silent-breach**: There is no input for which action is in `{SELL_LOSS_ONLY, SELL_LOSS_AND_ADD_WIN}` AND `WORST_CASE_REVERSAL_PROFIT < HARD_STOP_LOSS`.
- **P11-downgrade-monotone**: A downgrade never upgrades risk — `HOLD_LOCKED` has risk ≤ `SELL_LOSS_ONLY` ≤ `SELL_LOSS_AND_ADD_WIN` across any fixed snapshot.

---

### Requirement 12: Fixed Output Contract

**User Story:** As a runtime integrator, I want every analyzer call to produce a record with the same shape and field ordering, so that downstream logging, dashboarding, and order placement can rely on a stable schema.

### Acceptance Criteria

1. THE analyzer SHALL always return an output record containing exactly these fields, in this order:
   `MARKET_ID`, `MARKET_STATUS`, `TIME_REMAINING`, `TARGET_PRICE`, `CURRENT_ASSET_PRICE`, `DISTANCE_FROM_TARGET`, `UP_ASK_PRICE`, `DOWN_ASK_PRICE`, `TOTAL_PRICE`, `GROSS_EDGE`, `POSITION_EXISTS`, `SHARES_UP`, `SHARES_DOWN`, `TOTAL_INITIAL_CAPITAL`, `LOCKED_PROFIT`, `LOCKED_ROI`, `WIN_LEG`, `LOSS_LEG`, `WIN_LEG_PRICE`, `LOSS_LEG_PRICE`, `CONFIDENCE_SCORE`, `RECOMMENDED_ACTION`, `SELL_LOSS_PERCENT`, `ADDITIONAL_CAPITAL`, `EXPECTED_PROFIT_IF_CORRECT`, `WORST_CASE_REVERSAL_PROFIT`, `RISK_LEVEL`, `REASON`.
2. Fields not applicable to the chosen action (e.g. `SELL_LOSS_PERCENT` when action is `HOLD_LOCKED`) SHALL be set to a documented sentinel (e.g. `0.0` or `None`), not omitted.
3. THE `RECOMMENDED_ACTION` SHALL be exactly one of the enum values: `SKIP_ENTRY`, `BUY_BOTH_LEGS`, `STRONG_BUY_BOTH_LEGS`, `AGGRESSIVE_BUY_ONLY_IF_EXECUTION_CLEAN`, `HOLD_LOCKED`, `SELL_LOSS_ONLY`, `SELL_LOSS_AND_ADD_WIN`, `EMERGENCY_EXIT`.
4. THE `RISK_LEVEL` SHALL be one of: `LOW`, `MEDIUM`, `HIGH`, `DANGER`.
5. THE `REASON` SHALL be a human-readable string naming the dominant rule(s) that produced the action.
6. THE analyzer SHALL validate the output record before returning; if a field is missing, emit `EMERGENCY_EXIT` with a diagnostic `REASON` (fail-loud).

### Correctness Properties

- **P12-schema-stable**: For any valid input, the output record has exactly the fields listed above in Req 12.1, with the documented types.
- **P12-enum-closed**: `RECOMMENDED_ACTION` and `RISK_LEVEL` never take a value outside their respective enumerations.

---

### Requirement 13: Emergency Exit

**User Story:** As a bot operator, I want the analyzer to flag irrecoverable states clearly, so that the runtime can close the position or halt instead of operating on corrupt assumptions.

### Acceptance Criteria

1. WHEN `POSITION_EXISTS` is true AND `SHARES_UP != SHARES_DOWN` by more than the equal-shares tolerance THE action SHALL be `EMERGENCY_EXIT`.
2. WHEN `POSITION_EXISTS` is true AND `TOTAL_INITIAL_CAPITAL <= 0` THE action SHALL be `EMERGENCY_EXIT`.
3. WHEN any numeric input is `NaN`, infinite, or negative where negativity is semantically invalid (e.g. `TIME_REMAINING < 0`, `SHARES_UP < 0`) THE action SHALL be `EMERGENCY_EXIT`.
4. THE `REASON` for any `EMERGENCY_EXIT` SHALL list every detected violation, not just the first.

### Correctness Properties

- **P13-exhaustive-reason**: For every `EMERGENCY_EXIT` output, `REASON` is non-empty and mentions at least one concrete violation.

---

### Requirement 14: Fail-Loud on Invalid Inputs

**User Story:** As a bot operator, I want the analyzer to refuse to act on incomplete or contradictory inputs, so that silent fallbacks don't mask real integration bugs.

### Acceptance Criteria

1. IF any required input field is missing THEN THE action SHALL be `SKIP_ENTRY` (if no position) or `HOLD_LOCKED` (if position), AND `REASON` SHALL name the missing field(s).
2. IF `UP_ASK_PRICE <= 0` OR `DOWN_ASK_PRICE <= 0` OR either exceeds `1.0` THEN THE action SHALL be `SKIP_ENTRY` with a `REASON` citing the invalid price.
3. IF `TARGET_PRICE <= 0` THEN THE action SHALL be `SKIP_ENTRY` / `HOLD_LOCKED` as above.
4. THE analyzer SHALL NOT raise exceptions for invalid inputs; it SHALL always return a well-formed output record describing the failure (fail-loud via explicit record, not via traceback).
5. THE `REASON` field SHALL never be empty.

### Correctness Properties

- **P14-total-function**: For any input (including malformed), the analyzer returns an output record conforming to Req 12, never throws.
- **P14-reason-always-present**: `REASON` is a non-empty string on every output.
- **P14-conservative-on-ambiguity**: When inputs are invalid, the action is always in the conservative set `{SKIP_ENTRY, HOLD_LOCKED, EMERGENCY_EXIT}`.
