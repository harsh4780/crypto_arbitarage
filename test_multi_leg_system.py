"""
Comprehensive Verification Test for 5-Leg Multi-Tier Arbitrage System
====================================================================
Tests database persistence, execution telemetry, daily rotating logs,
5-leg matrix engine evaluation, and FastAPI server endpoints.
"""

import os
import json
import time
import asyncio
import unittest
from fastapi.testclient import TestClient

from database import TradeDatabase
from logger_setup import setup_system_logging, get_available_log_dates, read_log_file
from arbitrage_engine import ArbitrageEngine
from server import app

class TestMultiLegSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = os.path.join(os.path.dirname(__file__), "data", "test_trades.db")
        cls.test_cfg_path = os.path.join(os.path.dirname(__file__), "data", "legs_config_ETH.json")
        if os.path.exists(cls.test_db_path):
            try:
                os.remove(cls.test_db_path)
            except Exception:
                pass
        if os.path.exists(cls.test_cfg_path):
            try:
                os.remove(cls.test_cfg_path)
            except Exception:
                pass
        from database import db as global_db
        global_db.set_config("legs_ETH", None)
        cls.db = TradeDatabase(db_path=cls.test_db_path)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.test_db_path):
            try:
                os.remove(cls.test_db_path)
            except Exception:
                pass

    def test_01_database_operations(self):
        """Test database trade insertion, closing, querying, and stats."""
        test_trade = {
            "trade_id": "TEST-LEG1-001",
            "leg_id": 1,
            "symbol_spot": "ETHUSD",
            "symbol_fut": "ETHUSDT_261225",
            "exchange": "BINANCE",
            "action_type": "BUY_SPOT_SELL_FUTURES",
            "trade_size": 0.10,
            "entry_spot_price": 2450.00,
            "entry_fut_price": 2455.00,
            "entry_gap": 5.00,
            "open_time": "2026-10-14T12:00:00",
            "status": "OPEN",
            "mt5_ticket": 123456,
            "fut_order_id": "987654",
            "execution_json": {
                "trade_id": "TEST-LEG1-001",
                "leg_id": 1,
                "entry_snapshot": {"spread_usd": 5.0}
            }
        }
        self.db._execute_db_op("INSERT_OPEN", test_trade)

        trade = self.db.get_trade_by_id("TEST-LEG1-001")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["status"], "OPEN")
        self.assertEqual(trade["leg_id"], 1)
        self.assertEqual(trade["entry_gap"], 5.0)
        self.assertIn("entry_snapshot", trade["execution_json"])

        # Update close
        close_data = {
            "trade_id": "TEST-LEG1-001",
            "exit_spot_price": 2452.00,
            "exit_fut_price": 2452.50,
            "exit_gap": 0.50,
            "close_time": "2026-10-14T12:05:00",
            "duration_sec": 300.0,
            "pnl_gross": 0.45,
            "pnl_net": 0.35,
            "fee_estimated": 0.10,
            "status": "CLOSED",
            "exit_reason": "DIFF_TO_CUT",
            "execution_json": {
                "trade_id": "TEST-LEG1-001",
                "exit_snapshot": {"exit_gap": 0.50, "close_reason": "DIFF_TO_CUT"}
            }
        }
        self.db._execute_db_op("UPDATE_CLOSE", close_data)

        closed_trade = self.db.get_trade_by_id("TEST-LEG1-001")
        self.assertEqual(closed_trade["status"], "CLOSED")
        self.assertEqual(closed_trade["pnl_net"], 0.35)
        self.assertEqual(closed_trade["exit_reason"], "DIFF_TO_CUT")

        # History pagination & filters
        hist = self.db.get_history(limit=10, offset=0, leg_id=1, status="CLOSED")
        self.assertEqual(hist["total"], 1)
        self.assertEqual(len(hist["trades"]), 1)

        # Stats
        stats = self.db.get_stats()
        self.assertEqual(stats["total_trades"], 1)
        self.assertEqual(stats["closed_trades"], 1)
        self.assertEqual(stats["win_trades"], 1)
        self.assertEqual(stats["win_rate_pct"], 100.0)

    def test_02_logging_system(self):
        """Test daily log file creation and reading."""
        dates = get_available_log_dates()
        self.assertTrue(len(dates) > 0)
        
        today = dates[0]
        log_res = read_log_file(date_str=today, max_lines=50)
        self.assertIn("lines", log_res)
        self.assertIn("date", log_res)

    def test_03_arbitrage_engine_5_legs(self):
        """Test 5-leg initialization, independent configuration, and evaluate logic."""
        eng = ArbitrageEngine(currency="ETH")
        self.assertEqual(len(eng.legs), 5)

        # Verify initial default values are 0.0 for safety
        self.assertEqual(eng.legs[1]["diff_to_trade"], 0.0)
        self.assertEqual(eng.legs[1]["diff_to_cut"], 0.0)
        self.assertEqual(eng.legs[2]["diff_to_trade"], 0.0)
        self.assertEqual(eng.legs[3]["diff_to_trade"], 0.0)
        self.assertEqual(eng.legs[4]["diff_to_trade"], 0.0)
        self.assertEqual(eng.legs[5]["diff_to_trade"], 0.0)

        # Configure Leg 1 and Leg 2
        eng.update_leg_config(leg_id=1, diff_to_trade=4.0, diff_to_cut=0.5, trade_size=0.10, target_profit_usd=1.0, enabled=True)
        eng.update_leg_config(leg_id=2, diff_to_trade=5.5, diff_to_cut=0.8, trade_size=0.12, target_profit_usd=2.5, enabled=True)
        self.assertEqual(eng.legs[2]["diff_to_trade"], 5.5)
        self.assertEqual(eng.legs[2]["diff_to_cut"], 0.8)
        self.assertEqual(eng.legs[2]["trade_size"], 0.12)
        self.assertEqual(eng.legs[2]["target_profit_usd"], 2.5)

        # Mock market ticks: MT5 spot = 2450.00, Futures = 2454.50 -> Gap = +4.50
        # Leg 1 diff_to_trade = 4.0 -> Should trigger Leg 1 opportunity
        mt5_tick = {"mid": 2450.00, "bid": 2449.90, "ask": 2450.10, "symbol": "ETHUSD"}
        futures_tick = {"mid": 2454.50, "bid": 2454.40, "ask": 2454.60, "symbol": "ETHUSDT_261225"}

        eval_res = eng.evaluate(mt5_tick, futures_tick)
        self.assertEqual(eval_res["spread_usd"], 4.50)
        self.assertTrue(eval_res["is_opportunity"])
        self.assertEqual(eval_res["signal"], "BUY_SPOT_SELL_FUTURES")

        # Test manual firing of Leg 1 (with mock connections)
        class MockMT5:
            def place_order(self, symbol, order_type, volume, comment):
                return {"success": True, "ticket": 888101}
            def close_position(self, ticket):
                return {"success": True}
        class MockFutures:
            api_key = "mock_key"
            def place_order(self, symbol, side, qty, order_type):
                return {"success": True, "order_id": "FUT-999"}
            def close_all_positions(self, symbol):
                return {"success": True}

        mock_mt5 = MockMT5()
        mock_fut = MockFutures()

        # Fire Leg 1 manually
        fire_res = eng.fire_manual_leg(leg_id=1, mt5_tick=mt5_tick, futures_tick=futures_tick, mt5_conn=mock_mt5, futures_conn=mock_fut)
        self.assertTrue(fire_res["success"])
        self.assertEqual(eng.legs[1]["status"], "OPEN")
        self.assertEqual(len(eng.active_positions), 1)

        # Close Leg 1 manually
        close_res = eng.close_single_leg(leg_id=1, mt5_tick=mt5_tick, futures_tick=futures_tick, mt5_conn=mock_mt5, futures_conn=mock_fut)
        self.assertTrue(close_res["success"])
        self.assertEqual(eng.legs[1]["status"], "IDLE")
        self.assertEqual(len(eng.active_positions), 0)

    def test_04_fastapi_server_endpoints(self):
        """Test REST API routes for 5-Leg Matrix, History, and Logs."""
        client = TestClient(app)

        # 1. GET /api/legs
        r_legs = client.get("/api/legs")
        self.assertEqual(r_legs.status_code, 200)
        data = r_legs.json()
        self.assertEqual(len(data["legs"]), 5)

        # 2. POST /api/legs/1
        r_upd = client.post("/api/legs/1", json={
            "leg_id": 1,
            "diff_to_trade": 4.5,
            "diff_to_cut": 0.6,
            "trade_size": 0.15,
            "target_profit_usd": 1.5,
            "enabled": True
        })
        self.assertEqual(r_upd.status_code, 200)
        self.assertEqual(r_upd.json()["leg"]["diff_to_trade"], 4.5)

        # 3. GET /api/status
        r_status = client.get("/api/status")
        self.assertEqual(r_status.status_code, 200)
        self.assertIn("legs", r_status.json())

        # 4. GET /api/history
        r_hist = client.get("/api/history?limit=10")
        self.assertEqual(r_hist.status_code, 200)
        self.assertIn("trades", r_hist.json())

        # 5. GET /api/history-stats
        r_stats = client.get("/api/history-stats")
        self.assertEqual(r_stats.status_code, 200)
        self.assertIn("stats", r_stats.json())

        # 6. GET /api/logs/dates
        r_dates = client.get("/api/logs/dates")
        self.assertEqual(r_dates.status_code, 200)
        self.assertIn("dates", r_dates.json())

        # 7. GET /api/logs
        r_logs = client.get("/api/logs?limit=50")
        self.assertEqual(r_logs.status_code, 200)
        self.assertIn("lines", r_logs.json())

    def test_05_persistence_and_open_trade_recovery(self):
        """Verify that customized leg inputs and active open trades survive engine reboots."""
        from database import db as global_db
        # 1. Update Leg 3 with distinct custom values
        eng1 = ArbitrageEngine(currency="ETH")
        eng1.update_leg_config(leg_id=3, diff_to_trade=14.25, diff_to_cut=1.75, trade_size=0.45, target_profit_usd=8.5, enabled=True)

        # 2. Insert an active OPEN trade for Leg 2 into DB
        open_trade = {
            "trade_id": "REBOOT-LEG2-99",
            "leg_id": 2,
            "symbol_spot": "ETHUSD",
            "symbol_fut": "ETHUSDT_261225",
            "exchange": "BINANCE",
            "action_type": "BUY_SPOT_SELL_FUTURES",
            "trade_size": 0.25,
            "entry_spot_price": 2400.0,
            "entry_fut_price": 2408.0,
            "entry_gap": 8.0,
            "open_time": "2026-10-14T13:30:00",
            "status": "OPEN",
            "mt5_ticket": 777333,
            "fut_order_id": "ORD-555"
        }
        global_db._execute_db_op("INSERT_OPEN", open_trade)

        # 3. Re-instantiate a fresh ArbitrageEngine (simulates server restart or page refresh)
        eng_rebooted = ArbitrageEngine(currency="ETH")

        # 4. Verify Leg 3 retained customized inputs
        self.assertEqual(eng_rebooted.legs[3]["diff_to_trade"], 14.25)
        self.assertEqual(eng_rebooted.legs[3]["diff_to_cut"], 1.75)
        self.assertEqual(eng_rebooted.legs[3]["trade_size"], 0.45)
        self.assertEqual(eng_rebooted.legs[3]["target_profit_usd"], 8.5)

        # 5. Verify Leg 2 has recovered the active open trade
        self.assertEqual(eng_rebooted.legs[2]["status"], "OPEN")
        self.assertIsNotNone(eng_rebooted.legs[2]["active_position"])
        self.assertEqual(eng_rebooted.legs[2]["active_position"]["trade_id"], "REBOOT-LEG2-99")
        self.assertEqual(eng_rebooted.legs[2]["active_position"]["ticket"], 777333)
        self.assertTrue(len(eng_rebooted.active_positions) >= 1)

        # Clean up test open trade
        global_db._execute_db_op("UPDATE_CLOSE", {
            "trade_id": "REBOOT-LEG2-99",
            "exit_spot_price": 2402.0, "exit_fut_price": 2402.5, "exit_gap": 0.5,
            "close_time": "2026-10-14T13:35:00", "duration_sec": 300,
            "pnl_gross": 1.5, "pnl_net": 1.2, "fee_estimated": 0.3,
            "status": "CLOSED", "exit_reason": "TEST_CLEANUP", "execution_json": {}
        })

if __name__ == "__main__":
    unittest.main()
