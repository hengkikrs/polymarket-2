from strategies import end_window
from core.state import BotSettings


def test_end_window_layers_match_requested_thresholds():
    cfg = end_window.EndWindowConfig(enabled=True)
    cases = [
        (25.0, 70.0, 0.90, "T1"),
        (20.0, 55.0, 0.92, "T2"),
        (15.0, 35.0, 0.94, "T3"),
        (10.0, 35.0, 0.95, "T4"),
        (7.0, 25.0, 0.95, "T5"),
        (5.0, 12.0, 0.99, "T6"),
    ]

    for seconds_left, distance, price, layer in cases:
        decision = end_window.evaluate_entry(
            seconds_left=seconds_left,
            distance_usd=distance,
            price=price,
            side="YES",
            cfg=cfg,
        )

        assert decision.ok, (seconds_left, distance, price, decision.reason)
        assert decision.layer == layer


def test_end_window_rejects_above_price_or_below_delta():
    cfg = end_window.EndWindowConfig(enabled=True)

    high_price = end_window.evaluate_entry(
        seconds_left=20.0,
        distance_usd=55.0,
        price=0.93,
        side="YES",
        cfg=cfg,
    )
    low_delta = end_window.evaluate_entry(
        seconds_left=20.0,
        distance_usd=54.99,
        price=0.92,
        side="YES",
        cfg=cfg,
    )

    assert not high_price.ok
    assert not low_delta.ok


def test_changed_layers_enforce_new_delta_and_price_caps():
    cfg = end_window.EndWindowConfig(enabled=True)
    cases = [
        (25.0, 69.99, 0.90),
        (25.0, 70.0, 0.901),
        (10.0, 34.99, 0.95),
        (10.0, 35.0, 0.951),
        (7.0, 24.99, 0.95),
        (7.0, 25.0, 0.951),
    ]
    for seconds_left, distance, price in cases:
        decision = end_window.evaluate_entry(
            seconds_left=seconds_left,
            distance_usd=distance,
            price=price,
            side="YES",
            cfg=cfg,
        )
        assert not decision.ok


def test_end_window_rejects_unreasonable_low_price():
    cfg = end_window.EndWindowConfig(enabled=True)

    decision = end_window.evaluate_entry(
        seconds_left=10.0,
        distance_usd=29.82,
        price=0.01,
        side="NO",
        cfg=cfg,
        opposite_price=0.96,
    )

    assert not decision.ok
    assert decision.reason.startswith("unreasonable_price")


def test_end_window_rejects_selected_side_not_leading_book():
    cfg = end_window.EndWindowConfig(enabled=True)

    decision = end_window.evaluate_entry(
        seconds_left=10.0,
        distance_usd=29.82,
        price=0.55,
        side="NO",
        cfg=cfg,
        opposite_price=0.80,
    )

    assert not decision.ok
    assert decision.reason.startswith("side_price_not_leading")


def test_end_window_rejects_before_25s_and_under_4s():
    cfg = end_window.EndWindowConfig(enabled=True)

    before_window = end_window.evaluate_entry(
        seconds_left=25.1,
        distance_usd=500.0,
        price=0.50,
        side="YES",
        cfg=cfg,
    )
    under_four = end_window.evaluate_entry(
        seconds_left=3.99,
        distance_usd=500.0,
        price=0.50,
        side="YES",
        cfg=cfg,
    )

    assert not before_window.ok
    assert not under_four.ok


def test_end_window_rejects_missing_direction_and_wide_spread():
    cfg = end_window.EndWindowConfig(enabled=True, max_spread=0.05)

    no_direction = end_window.evaluate_entry(
        seconds_left=7.0,
        distance_usd=80.0,
        price=0.76,
        side="",
        cfg=cfg,
    )
    wide_spread = end_window.evaluate_entry(
        seconds_left=7.0,
        distance_usd=80.0,
        price=0.76,
        side="YES",
        cfg=cfg,
        spread=0.06,
    )

    assert no_direction.reason == "no_direction"
    assert wide_spread.reason.startswith("spread_too_wide")


def test_end_window_t6_requires_twelve_dollar_delta_at_099():
    cfg = end_window.EndWindowConfig(enabled=True)

    decision = end_window.evaluate_entry(
        seconds_left=5.0,
        distance_usd=12.0,
        price=0.99,
        side="YES",
        cfg=cfg,
        opposite_price=0.01,
    )

    assert decision.ok
    assert decision.layer == "T6"

    below_delta = end_window.evaluate_entry(
        seconds_left=5.0,
        distance_usd=11.99,
        price=0.99,
        side="YES",
        cfg=cfg,
        opposite_price=0.01,
    )
    assert not below_delta.ok


def test_end_window_config_uses_saved_custom_layer_values():
    settings = BotSettings(
        t1_seconds_max=30.0,
        t1_seconds_min=21.0,
        t1_delta_min=80.0,
        t1_min_price=0.60,
        t1_max_price=0.88,
    )
    cfg = end_window.EndWindowConfig.from_settings(settings)
    t1 = next(layer for layer in cfg.layers if layer.name == "T1")

    assert (t1.seconds_left_max, t1.seconds_left_min) == (30.0, 21.0)
    assert t1.min_distance_usd == 80.0
    assert (t1.min_price, t1.max_price) == (0.60, 0.88)
