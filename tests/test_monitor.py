# -*- coding: utf-8 -*-

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor  # noqa: E402


class MarketHoursTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "runtime": {
                "monitor_24h": False,
                "market_open_cst": "21:30",
                "market_close_cst": "04:00",
                "winter_open_cst": "22:30",
                "winter_close_cst": "05:00",
            }
        }

    def test_friday_us_session_continues_after_beijing_midnight(self):
        now = dt.datetime(
            2026, 7, 4, 0, 30, tzinfo=monitor.BEIJING_TZ
        )
        self.assertTrue(monitor.in_market_hours(now, self.cfg))

    def test_saturday_after_close_is_not_market_hours(self):
        now = dt.datetime(
            2026, 7, 4, 4, 30, tzinfo=monitor.BEIJING_TZ
        )
        self.assertFalse(monitor.in_market_hours(now, self.cfg))

    def test_us_open_is_market_hours(self):
        now = dt.datetime(
            2026, 7, 6, 21, 30, tzinfo=monitor.BEIJING_TZ
        )
        self.assertTrue(monitor.in_market_hours(now, self.cfg))


class SinaParsingTests(unittest.TestCase):
    def test_hong_kong_open_and_previous_close_are_not_swapped(self):
        positions = [{"symbol": "HSTECH", "market": "hk"}]
        text = (
            'var hq_str_rt_hkHSTECH="HSTECH,恒生科技指数,'
            '4232.240,4249.620,4258.220,4216.340,4253.890,'
            '4.270,0.100";'
        )
        quote = monitor.parse_sina_response(text, positions)["HSTECH"]
        self.assertEqual(quote["open"], 4232.24)
        self.assertEqual(quote["prev_close"], 4249.62)
        self.assertEqual(quote["price"], 4253.89)

    def test_us_volume_and_previous_close(self):
        positions = [{"symbol": "AAPL", "market": "us"}]
        text = (
            'var hq_str_gb_aapl="Apple,336.2200,2.07,time,6.8200,'
            '330.8000,339.5000,330.1401,345.3400,242.8900,'
            '19898461";'
        )
        quote = monitor.parse_sina_response(text, positions)["AAPL"]
        self.assertAlmostEqual(quote["prev_close"], 329.4)
        self.assertEqual(quote["volume"], 19898461)


class PendingAlertTests(unittest.TestCase):
    def _config(self):
        return {
            "positions": [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "qty": 1,
                    "cost_price": 100,
                    "market": "us",
                }
            ],
            "alerts": {
                "daily_change_pct": 2,
                "cost_change_pct": 10,
                "portfolio_daily_pnl": 100,
                "volume_ratio": 2,
                "price_gap_pct": 2,
            },
            "runtime": {
                "log_file": "monitor.log",
                "state_file": "alerted_state.json",
            },
            "push": {},
        }

    def test_failed_alert_is_retried_and_only_cleared_after_success(self):
        quote = {
            "AAPL": {
                "name": "Apple",
                "price": 110.0,
                "change_pct": 10.0,
                "change_amt": 10.0,
                "prev_close": 100.0,
                "open": 100.0,
                "high": 111.0,
                "low": 99.0,
                "volume": 1000,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(monitor, "BASE_DIR", Path(tmp)),
                mock.patch.object(monitor, "load_config", return_value=self._config()),
                mock.patch.object(monitor, "setup_logging"),
                mock.patch.object(monitor, "in_market_hours", return_value=True),
                mock.patch.object(
                    monitor, "fetch_sina_realtime", return_value=quote
                ),
                mock.patch.object(monitor, "get_avg_volume", return_value=0),
                mock.patch.object(
                    monitor, "send_alerts", return_value=False
                ) as failed_send,
            ):
                monitor.main()
                state_path = Path(tmp) / "alerted_state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertTrue(state["pending_alerts"])
                self.assertEqual(failed_send.call_count, 1)

            with (
                mock.patch.object(monitor, "BASE_DIR", Path(tmp)),
                mock.patch.object(monitor, "load_config", return_value=self._config()),
                mock.patch.object(monitor, "setup_logging"),
                mock.patch.object(monitor, "in_market_hours", return_value=True),
                mock.patch.object(
                    monitor, "fetch_sina_realtime", return_value=quote
                ),
                mock.patch.object(monitor, "get_avg_volume", return_value=0),
                mock.patch.object(
                    monitor, "send_alerts", return_value=True
                ) as successful_send,
            ):
                monitor.main()
                state = json.loads(
                    (Path(tmp) / "alerted_state.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertFalse(state["pending_alerts"])
                self.assertEqual(successful_send.call_count, 1)
                sent_values = [
                    value
                    for key, value in state.items()
                    if key != "pending_alerts"
                    and key.endswith(("_daily", "_cost"))
                ]
                self.assertTrue(sent_values)
                self.assertTrue(all(value != "pending" for value in sent_values))


if __name__ == "__main__":
    unittest.main()
