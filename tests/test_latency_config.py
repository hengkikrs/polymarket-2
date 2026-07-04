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

    def test_chainlink_sample_preserves_official_timestamp(self):
        sample = ws_feed._chainlink_sample({
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": 1_800_000_000, "value": 62_300.0},
                    {"timestamp": 1_800_000_001, "value": 62_345.67},
                ],
            },
        })
        self.assertEqual(sample, (62_345.67, 1_800_000_001.0))

    def test_chainlink_samples_preserve_full_history_batch(self):
        samples = ws_feed._chainlink_samples({
            "payload": {
                "data": [
                    {"timestamp": 1_800_000_000_000, "value": 62_300.0},
                    {"timestamp": 1_800_000_001_000, "value": 62_345.67},
                ],
            },
        })

        self.assertEqual(samples, [
            (62_300.0, 1_800_000_000.0),
            (62_345.67, 1_800_000_001.0),
        ])

    def test_source_btc_at_time_uses_feed_timestamp(self):
        cache = ws_feed.PriceCache()
        cache.set_source_btc(62_300.0, "chainlink", timestamp=1_800_000_000.0)
        cache.set_source_btc(62_345.67, "chainlink", timestamp=1_800_000_003.0)

        sample = cache.source_btc_at_time("chainlink", 1_800_000_000.0, max_drift=0.5)

        self.assertEqual(sample, (62_300.0, 1_800_000_000.0, 0.0))

    def test_source_btc_at_time_can_prefer_open_sample_not_future_tick(self):
        cache = ws_feed.PriceCache()
        cache.set_source_btc(62_300.0, "chainlink", timestamp=1_800_000_000.0)
        cache.set_source_btc(62_350.0, "chainlink", timestamp=1_800_000_000.2)

        sample = cache.source_btc_at_time(
            "chainlink",
            1_800_000_000.1,
            max_drift=0.5,
            prefer_at_or_before=True,
        )

        self.assertEqual(sample[:2], (62_300.0, 1_800_000_000.0))
        self.assertAlmostEqual(sample[2], 0.1, places=6)

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
