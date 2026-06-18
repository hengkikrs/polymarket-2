import unittest

import core.ws_feed as ws_feed


class LatencyConfigTests(unittest.TestCase):
    def test_chainlink_price_extracts_latest_btc_value(self):
        price = ws_feed._chainlink_price({
            "payload": {
                "symbol": "btc/usd",
                "data": [{"timestamp": 1, "value": 62_345.67}],
            },
        })
        self.assertEqual(price, 62_345.67)

    def test_coinbase_price_extracts_btc_usd_ticker(self):
        price = ws_feed._coinbase_price({
            "channel": "ticker",
            "events": [{
                "tickers": [
                    {"product_id": "ETH-USD", "price": "2000"},
                    {"product_id": "BTC-USD", "price": "62350.25"},
                ],
            }],
        })
        self.assertEqual(price, 62_350.25)

    def test_clob_poll_interval_respects_target_rpm(self):
        interval = ws_feed._clob_poll_interval(
            token_count=14,
            target_rpm=1200,
            min_sweep_interval=0.05,
        )
        self.assertAlmostEqual(interval, 0.7)

    def test_clob_poll_interval_has_minimum_floor(self):
        interval = ws_feed._clob_poll_interval(
            token_count=1,
            target_rpm=60000,
            min_sweep_interval=0.05,
        )
        self.assertEqual(interval, 0.05)


if __name__ == "__main__":
    unittest.main()
