import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coldmath_live_alert as live


def topic_address(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def encode_uints(*values: int) -> str:
    return "0x" + "".join(f"{value:064x}" for value in values)


def synthetic_log(*, maker: str, taker: str, data: str) -> dict:
    return {
        "address": live.EXCHANGES["ctf_exchange_v2"],
        "topics": [
            live.ORDER_FILLED_TOPIC,
            "0x" + "1".zfill(64),
            topic_address(maker),
            topic_address(taker),
        ],
        "data": data,
        "blockNumber": hex(123),
        "transactionHash": "0x" + "2" * 64,
        "logIndex": "0x5",
    }


class SideInferenceTests(unittest.TestCase):
    def test_maker_buy(self):
        action = live.infer_coldmath_action("maker", 0, 987, 25_000_000, 100_000_000)
        self.assertEqual(action["inferred_action"], "BUY")
        self.assertEqual(action["asset_id"], "987")
        self.assertEqual(action["estimated_usdc_notional"], 25)
        self.assertEqual(action["size"], 100)
        self.assertEqual(action["price"], 0.25)

    def test_maker_sell(self):
        action = live.infer_coldmath_action("maker", 987, 0, 100_000_000, 25_000_000)
        self.assertEqual(action["inferred_action"], "SELL")
        self.assertEqual(action["asset_id"], "987")

    def test_taker_buy(self):
        action = live.infer_coldmath_action("taker", 987, 0, 100_000_000, 25_000_000)
        self.assertEqual(action["inferred_action"], "BUY")
        self.assertEqual(action["asset_id"], "987")
        self.assertEqual(action["estimated_usdc_notional"], 25)
        self.assertEqual(action["size"], 100)

    def test_taker_sell(self):
        action = live.infer_coldmath_action("taker", 0, 987, 25_000_000, 100_000_000)
        self.assertEqual(action["inferred_action"], "SELL")
        self.assertEqual(action["asset_id"], "987")

    def test_decode_log_uses_topics_and_amounts(self):
        log = synthetic_log(
            maker=live.COLDMATH_WALLET,
            taker="0x0000000000000000000000000000000000000abc",
            data=encode_uints(0, 12345, 1_000_000, 100_000_000, 0),
        )
        decoded = live.decode_log(log)
        self.assertEqual(decoded.event_id, f"{log['transactionHash']}:5")
        self.assertEqual(decoded.inferred_action, "BUY")
        self.assertEqual(decoded.asset_id, "12345")
        self.assertEqual(decoded.price, 0.01)


class MetadataAndStoreTests(unittest.TestCase):
    def test_local_metadata_cache_resolves_weather_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "asset": "123",
                            "conditionId": "0xabc",
                            "outcome": "Yes",
                            "title": "Will the highest temperature in NYC be 90°F?",
                            "slug": "highest-temperature-in-nyc-90f",
                            "eventSlug": "highest-temperature-in-nyc",
                        },
                        {
                            "asset": "999",
                            "conditionId": "0xdef",
                            "outcome": "Yes",
                            "title": "Will Finland be in the top 5 at Eurovision?",
                            "slug": "finland-top-5-eurovision",
                            "eventSlug": "eurovision-top-5",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            logger = live.DaemonLogger(Path(tmp) / "daemon.log")
            cache = live.MetadataCache(logger, local_cache=path)
            cache.load_local_cache()
            self.assertEqual(cache.resolve("123")["condition_id"], "0xabc")
            self.assertIsNone(cache.resolve("999"))

    def test_event_store_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(live, "LIVE_DIR", Path(tmp)), patch.object(
                live, "BUY_EVENTS_JSONL", Path(tmp) / "events.jsonl"
            ), patch.object(live, "LATENCY_CSV", Path(tmp) / "latency.csv"), patch.object(
                live, "STATE_FILE", Path(tmp) / "state.json"
            ):
                logger = live.DaemonLogger(Path(tmp) / "daemon.log")
                store = live.EventStore(logger)
                event = {field: "" for field in live.CSV_FIELDS}
                event["event_id"] = "tx:1"
                event["tx_hash"] = "tx"
                self.assertTrue(store.record_event(event))
                self.assertFalse(store.record_event(event))
                self.assertEqual(len(store.events), 1)


if __name__ == "__main__":
    unittest.main()
