import asyncio
import json

from core import market


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def _event_payload(slug):
    return {
        "slug": slug,
        "markets": [{
            "question": "Bitcoin Up or Down",
            "conditionId": "condition",
            "endDate": "2099-01-01T00:00:00Z",
            "active": True,
            "closed": False,
            "archived": False,
            "acceptingOrders": True,
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.45", "0.55"]',
            "clobTokenIds": '["up-token", "down-token"]',
        }],
    }


def test_fetch_market_uses_direct_slug_endpoint(monkeypatch):
    window_ts = 1_800_000_000
    slug = f"btc-updown-5m-{window_ts}"
    monkeypatch.setattr(market, "current_window_ts", lambda interval_secs=300: window_ts)
    session = _Session(_event_payload(slug))

    result = asyncio.run(market.fetch_market(session, "BTC"))

    assert result is not None
    assert result.slug == slug
    assert result.up_token == "up-token"
    assert session.calls[0][0].endswith(f"/events/slug/{slug}")
    assert "params" not in session.calls[0][1]


def test_fetch_market_can_use_btc_15m_slug(monkeypatch):
    window_ts = 1_800_000_900
    slug = f"btc-updown-15m-{window_ts}"
    monkeypatch.setattr(market, "current_window_ts", lambda interval_secs=300: window_ts)
    session = _Session(_event_payload(slug))

    result = asyncio.run(market.fetch_market(session, "BTC", interval_secs=900))

    assert result is not None
    assert result.slug == slug
    assert result.window_ts == window_ts
    assert result.close_ts == window_ts + 900
    assert session.calls[0][0].endswith(f"/events/slug/{slug}")


def test_fetch_market_prefers_event_metadata_price_to_beat(monkeypatch):
    window_ts = 1_800_000_000
    slug = f"btc-updown-5m-{window_ts}"
    payload = _event_payload(slug)
    payload["eventMetadata"] = {"priceToBeat": 62345.67}
    monkeypatch.setattr(market, "current_window_ts", lambda interval_secs=300: window_ts)
    session = _Session(payload)

    result = asyncio.run(market.fetch_market(session, "BTC"))

    assert result is not None
    assert result.target_price == 62345.67


def test_fetch_resolved_outcome_uses_direct_slug_endpoint():
    slug = "btc-updown-5m-1800000000"
    payload = _event_payload(slug)
    payload["markets"][0]["closed"] = True
    payload["markets"][0]["outcomePrices"] = '["1", "0"]'
    session = _Session(payload)

    result = asyncio.run(market.fetch_resolved_outcome(session, slug))

    assert result == "UP"
    assert session.calls[0][0].endswith(f"/events/slug/{slug}")
    assert "params" not in session.calls[0][1]


def test_fetch_resolution_prefers_event_metadata_prices():
    slug = "btc-updown-5m-1800000000"
    payload = _event_payload(slug)
    payload["eventMetadata"] = {
        "finalPrice": 62775.70177044554,
        "priceToBeat": 62794.962049925874,
    }
    payload["markets"][0]["closed"] = True
    payload["markets"][0]["outcomePrices"] = '["1", "0"]'
    session = _Session(payload)

    result = asyncio.run(market.fetch_resolution(session, slug))

    assert result == {
        "actual": "DOWN",
        "final_price": 62775.70177044554,
        "price_to_beat": 62794.962049925874,
        "source": "gamma_event_metadata",
        "winner_token": "down-token",
    }


def test_fetch_recent_btc_resolutions_reads_completed_gamma_windows(monkeypatch):
    now = 1_800_000_600.0

    async def fake_fetch_resolution(_session, slug, *, allow_implied=True):
        window_ts = int(slug.rsplit("-", 1)[1])
        return {
            "actual": "UP",
            "final_price": float(window_ts + 25),
            "price_to_beat": float(window_ts),
            "source": "gamma_event_metadata",
        }

    monkeypatch.setattr(market, "fetch_resolution", fake_fetch_resolution)

    result = asyncio.run(
        market.fetch_recent_btc_resolutions(object(), hours=1, now=now)
    )

    assert len(result) == 12
    assert result[-1]["window_ts"] == 1_800_000_300
    assert result[-1]["final_price"] - result[-1]["price_to_beat"] == 25.0


def test_fetch_resolution_includes_official_winner_token():
    slug = "btc-updown-5m-1800000000"
    payload = _event_payload(slug)
    payload["eventMetadata"] = {
        "finalPrice": 60_050.0,
        "priceToBeat": 60_000.0,
    }
    session = _Session(payload)

    result = asyncio.run(market.fetch_resolution(session, slug))

    assert result["winner_token"] == "up-token"


def test_fetch_recent_clob_saturation_uses_official_history():
    class _HistorySession:
        def get(self, _url, **_kwargs):
            return _Response({
                "history": [
                    {"t": 18_605, "p": 0.55},
                    {"t": 18_845, "p": 0.96},
                ],
            })

    rows = [{
        "window_ts": 18_600,
        "winner_token": "winner-token",
    }]

    result = asyncio.run(
        market.fetch_recent_clob_saturation(
            _HistorySession(),
            rows,
            now=19_000.0,
        )
    )

    assert result["saturation_avg_secs_30m"] == 55.0
    assert result["locked_avg_secs_30m"] is None
    assert result["completed_windows_30m"] == 1


def test_fetch_recent_clob_saturation_expands_to_five_completed_windows():
    class _HistorySession:
        def get(self, _url, **kwargs):
            start_ts = int(kwargs["params"]["startTs"])
            return _Response({
                "history": [
                    {"t": start_ts + 245, "p": 0.96},
                ],
            })

    rows = [
        {"window_ts": window_ts, "winner_token": f"winner-{window_ts}"}
        for window_ts in (16_200, 16_500, 16_800, 17_100, 17_400, 17_700)
    ]

    result = asyncio.run(
        market.fetch_recent_clob_saturation(
            _HistorySession(),
            rows,
            now=18_000.0,
            minutes=5,
        )
    )

    assert result["completed_windows_30m"] == 5
    assert result["saturation_samples_30m"] == 5
    assert result["saturation_avg_secs_30m"] == 55.0
