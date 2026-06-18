import asyncio

from core import trader


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_fetch_live_portfolio_uses_funder_and_value_endpoint(monkeypatch):
    monkeypatch.setattr(trader.config, "FUNDER", "0xabc")
    monkeypatch.setattr(
        trader,
        "_live_portfolio_cache",
        {"portfolio": 0.0, "ts": 0.0, "ok": False, "error": ""},
    )
    session = _Session([{"user": "0xabc", "value": 12.345}])

    result = asyncio.run(trader.fetch_live_portfolio(session))

    assert result["ok"] is True
    assert result["portfolio"] == 12.35
    assert session.calls[0][0].endswith("/value")
    assert session.calls[0][1]["params"] == {"user": "0xabc"}


def test_fetch_live_portfolio_rejects_missing_funder(monkeypatch):
    monkeypatch.setattr(trader.config, "FUNDER", "")
    monkeypatch.setattr(
        trader,
        "_live_portfolio_cache",
        {"portfolio": 0.0, "ts": 0.0, "ok": False, "error": ""},
    )

    result = asyncio.run(trader.fetch_live_portfolio(_Session([])))

    assert result["ok"] is False
    assert "FUNDER" in result["error"]
