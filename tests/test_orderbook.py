import pytest

from core.orderbook import top_depth


def test_top_depth_normalizes_polymarket_book_order():
    bids = [
        {"price": "0.01", "size": "10"},
        {"price": "0.42", "size": "3"},
        {"price": "0.40", "size": "4"},
    ]
    asks = [
        {"price": "0.99", "size": "10"},
        {"price": "0.45", "size": "4"},
        {"price": "0.43", "size": "3"},
    ]

    assert top_depth(bids, side="bid", limit=2) == [(0.42, 3.0), (0.4, 4.0)]
    assert top_depth(asks, side="ask", limit=2) == [(0.43, 3.0), (0.45, 4.0)]


def test_top_depth_rejects_invalid_side():
    with pytest.raises(ValueError):
        top_depth([], side="middle")
