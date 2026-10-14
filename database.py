"""
SQLite Database Module for Trade History & Execution Telemetry
==============================================================
Manages high-performance trade persistence with asynchronous, non-blocking
writing to ensure zero latency on the critical trading execution path.
"""

import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "trades.db")

class TradeDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_db_dir()
        self._init_schema()
        # Non-blocking async queue for deferred writes
        self._write_queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None

    def _ensure_db_dir(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        """Create trade_history table and performance indexes."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    leg_id INTEGER NOT NULL,
                    symbol_spot TEXT NOT NULL,
                    symbol_fut TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    trade_size REAL NOT NULL,
                    entry_spot_price REAL NOT NULL,
                    entry_fut_price REAL NOT NULL,
                    entry_gap REAL NOT NULL,
                    exit_spot_price REAL,
                    exit_fut_price REAL,
                    exit_gap REAL,
                    open_time TEXT NOT NULL,
                    close_time TEXT,
                    duration_sec REAL DEFAULT 0.0,
                    pnl_gross REAL DEFAULT 0.0,
                    pnl_net REAL DEFAULT 0.0,
                    fee_estimated REAL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    exit_reason TEXT,
                    mt5_ticket INTEGER,
                    fut_order_id TEXT,
                    execution_json TEXT
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS spread_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    spot_price REAL NOT NULL,
                    fut_price REAL NOT NULL,
                    raw_spread REAL NOT NULL,
                    spread_pct REAL NOT NULL,
                    deviation REAL DEFAULT 0.0
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_leg ON trade_history(leg_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trade_history(status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_open_time ON trade_history(open_time);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticks_curr_id ON spread_ticks(currency, id DESC);")
            conn.commit()
            logger.info(f"[DB] SQLite database initialized at {self.db_path}")

    def set_config(self, key: str, value: Any):
        """Persist system configuration or leg parameters safely to SQLite."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO system_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (key, json.dumps(value, ensure_ascii=False), datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"[DB] Failed to save config {key}: {e}", exc_info=True)

    def get_config(self, key: str, default: Any = None) -> Any:
        """Retrieve persisted system configuration or leg parameters."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM system_config WHERE key = ? LIMIT 1", (key,))
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as e:
            logger.error(f"[DB] Failed to read config {key}: {e}", exc_info=True)
        return default

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Retrieve all currently OPEN trades persisted in the SQLite database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, trade_id, leg_id, symbol_spot, symbol_fut, exchange,
                       action_type, trade_size, entry_spot_price, entry_fut_price, entry_gap,
                       open_time, status, mt5_ticket, fut_order_id, execution_json
                FROM trade_history
                WHERE status = 'OPEN'
                ORDER BY id ASC
            """)
            rows = cursor.fetchall()
            trades = []
            for r in rows:
                item = dict(r)
                try:
                    item["execution_json"] = json.loads(item["execution_json"]) if item.get("execution_json") else {}
                except Exception:
                    item["execution_json"] = {}
                trades.append(item)
            return trades

    def start_async_worker(self):
        """Starts the background worker queue in the current running asyncio event loop."""
        if self._worker_task is None or self._worker_task.done():
            self._write_queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(self._process_queue())
            logger.info("[DB] Asynchronous trade write worker started.")

    async def _process_queue(self):
        """Background worker that commits writes to SQLite asynchronously without blocking orders."""
        while True:
            try:
                task_item = await self._write_queue.get()
                op_type = task_item.get("op")
                data = task_item.get("data")
                
                # Execute in thread to avoid blocking the event loop
                await asyncio.to_thread(self._execute_db_op, op_type, data)
                self._write_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DB] Error in async write worker: {e}", exc_info=True)

    def _execute_db_op(self, op_type: str, data: Dict[str, Any]):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if op_type == "INSERT_OPEN":
                    cursor.execute("""
                        INSERT OR REPLACE INTO trade_history (
                            trade_id, leg_id, symbol_spot, symbol_fut, exchange,
                            action_type, trade_size, entry_spot_price, entry_fut_price, entry_gap,
                            open_time, status, mt5_ticket, fut_order_id, execution_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        data.get("trade_id"),
                        data.get("leg_id", 1),
                        data.get("symbol_spot"),
                        data.get("symbol_fut"),
                        data.get("exchange", "FUTURES"),
                        data.get("action_type"),
                        data.get("trade_size", 0.1),
                        data.get("entry_spot_price", 0.0),
                        data.get("entry_fut_price", 0.0),
                        data.get("entry_gap", 0.0),
                        data.get("open_time", datetime.now().isoformat()),
                        data.get("status", "OPEN"),
                        data.get("mt5_ticket"),
                        str(data.get("fut_order_id") or ""),
                        json.dumps(data.get("execution_json") or {}, ensure_ascii=False)
                    ))
                elif op_type == "UPDATE_CLOSE":
                    cursor.execute("""
                        UPDATE trade_history SET
                            exit_spot_price = ?,
                            exit_fut_price = ?,
                            exit_gap = ?,
                            close_time = ?,
                            duration_sec = ?,
                            pnl_gross = ?,
                            pnl_net = ?,
                            fee_estimated = ?,
                            status = ?,
                            exit_reason = ?,
                            execution_json = ?
                        WHERE trade_id = ?
                    """, (
                        data.get("exit_spot_price", 0.0),
                        data.get("exit_fut_price", 0.0),
                        data.get("exit_gap", 0.0),
                        data.get("close_time", datetime.now().isoformat()),
                        data.get("duration_sec", 0.0),
                        data.get("pnl_gross", 0.0),
                        data.get("pnl_net", 0.0),
                        data.get("fee_estimated", 0.0),
                        data.get("status", "CLOSED"),
                        data.get("exit_reason", "MANUAL"),
                        json.dumps(data.get("execution_json") or {}, ensure_ascii=False),
                        data.get("trade_id")
                    ))
                conn.commit()
        except Exception as e:
            logger.error(f"[DB] Error executing DB operation {op_type}: {e}", exc_info=True)

    def record_trade_open_async(self, trade_data: Dict[str, Any]):
        """Fast non-blocking dispatch of trade open to SQLite queue."""
        if self._write_queue:
            try:
                self._write_queue.put_nowait({"op": "INSERT_OPEN", "data": trade_data})
            except Exception:
                # If loop queue is full, write directly in threadpool
                asyncio.create_task(asyncio.to_thread(self._execute_db_op, "INSERT_OPEN", trade_data))
        else:
            # Fallback if queue worker is not initialized
            self._execute_db_op("INSERT_OPEN", trade_data)

    def record_trade_close_async(self, trade_data: Dict[str, Any]):
        """Fast non-blocking dispatch of trade close to SQLite queue."""
        if self._write_queue:
            try:
                self._write_queue.put_nowait({"op": "UPDATE_CLOSE", "data": trade_data})
            except Exception:
                asyncio.create_task(asyncio.to_thread(self._execute_db_op, "UPDATE_CLOSE", trade_data))
        else:
            self._execute_db_op("UPDATE_CLOSE", trade_data)

    def get_history(self, limit: int = 50, offset: int = 0, leg_id: Optional[int] = None, 
                    status: Optional[str] = None, search: Optional[str] = None) -> Dict[str, Any]:
        """Fetch historical trades with filters and pagination."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            where_clauses = []
            params: List[Any] = []

            if leg_id is not None and leg_id > 0:
                where_clauses.append("leg_id = ?")
                params.append(leg_id)

            if status and status.upper() != "ALL":
                where_clauses.append("status = ?")
                params.append(status.upper())

            if search:
                where_clauses.append("(trade_id LIKE ? OR symbol_spot LIKE ? OR symbol_fut LIKE ? OR exit_reason LIKE ?)")
                term = f"%{search}%"
                params.extend([term, term, term, term])

            where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            # Count total matching
            count_query = f"SELECT COUNT(*) FROM trade_history {where_str}"
            cursor.execute(count_query, params)
            total_count = cursor.fetchone()[0]

            # Fetch rows
            data_query = f"""
                SELECT id, trade_id, leg_id, symbol_spot, symbol_fut, exchange,
                       action_type, trade_size, entry_spot_price, entry_fut_price, entry_gap,
                       exit_spot_price, exit_fut_price, exit_gap, open_time, close_time,
                       duration_sec, pnl_gross, pnl_net, fee_estimated, status, exit_reason,
                       mt5_ticket, fut_order_id, execution_json
                FROM trade_history
                {where_str}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(data_query, params + [limit, offset])
            rows = cursor.fetchall()

            trades = []
            for r in rows:
                item = dict(r)
                # Parse execution_json for convenience
                try:
                    if item.get("execution_json"):
                        item["execution_json"] = json.loads(item["execution_json"])
                    else:
                        item["execution_json"] = {}
                except Exception:
                    item["execution_json"] = {}
                trades.append(item)

            return {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "trades": trades
            }

    def get_trade_by_id(self, trade_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trade_history WHERE trade_id = ? LIMIT 1", (trade_id,))
            row = cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            try:
                item["execution_json"] = json.loads(item["execution_json"]) if item.get("execution_json") else {}
            except Exception:
                item["execution_json"] = {}
            return item

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate summary statistics across all trades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) as closed_trades,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) as open_trades,
                    SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as win_trades,
                    SUM(CASE WHEN pnl_net < 0 THEN 1 ELSE 0 END) as loss_trades,
                    SUM(pnl_gross) as total_gross_pnl,
                    SUM(pnl_net) as total_net_pnl,
                    SUM(fee_estimated) as total_fees,
                    AVG(duration_sec) as avg_duration_sec,
                    AVG(trade_size) as avg_trade_size
                FROM trade_history
            """)
            row = cursor.fetchone()
            res = dict(row) if row else {}
            
            total_closed = res.get("closed_trades") or 0
            wins = res.get("win_trades") or 0
            res["win_rate_pct"] = round((wins / total_closed * 100.0), 2) if total_closed > 0 else 0.0
            res["total_net_pnl"] = round(res.get("total_net_pnl") or 0.0, 2)
            res["total_gross_pnl"] = round(res.get("total_gross_pnl") or 0.0, 2)
            res["total_fees"] = round(res.get("total_fees") or 0.0, 2)
            res["avg_duration_sec"] = round(res.get("avg_duration_sec") or 0.0, 1)
            return res

    def save_spread_tick(self, currency: str, spot_price: float, fut_price: float, raw_spread: float, spread_pct: float, deviation: float = 0.0, timestamp_str: Optional[str] = None):
        """Append a real-time spread tick for background persistence with NaN/Inf guards."""
        import math
        def clean(v):
            try:
                f = float(v)
                return 0.0 if (math.isnan(f) or math.isinf(f)) else f
            except Exception:
                return 0.0

        now_dt = datetime.utcnow()
        today = now_dt.strftime("%Y-%m-%d")
        ts = timestamp_str or now_dt.strftime("%H:%M:%S")
        sp = clean(spot_price)
        fp = clean(fut_price)
        rs = clean(raw_spread)
        pct = clean(spread_pct)
        dev = clean(deviation)
        curr = currency.upper()

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO spread_ticks (date, timestamp, currency, spot_price, fut_price, raw_spread, spread_pct, deviation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (today, ts, curr, sp, fp, rs, pct, dev))

                # Upsert into daily_gap_summary
                cursor.execute("""
                    INSERT INTO daily_gap_summary (date, currency, max_gap, max_gap_time, min_gap, min_gap_time, avg_gap, gap_range, total_ticks, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, 1, ?)
                    ON CONFLICT(date, currency) DO UPDATE SET
                        max_gap = MAX(daily_gap_summary.max_gap, excluded.max_gap),
                        max_gap_time = CASE WHEN excluded.max_gap >= daily_gap_summary.max_gap THEN excluded.max_gap_time ELSE daily_gap_summary.max_gap_time END,
                        min_gap = MIN(daily_gap_summary.min_gap, excluded.min_gap),
                        min_gap_time = CASE WHEN excluded.min_gap <= daily_gap_summary.min_gap THEN excluded.min_gap_time ELSE daily_gap_summary.min_gap_time END,
                        gap_range = MAX(daily_gap_summary.max_gap, excluded.max_gap) - MIN(daily_gap_summary.min_gap, excluded.min_gap),
                        total_ticks = daily_gap_summary.total_ticks + 1,
                        updated_at = excluded.updated_at
                """, (today, curr, rs, ts, rs, ts, rs, today))
                conn.commit()
        except Exception as e:
            logger.error(f"[DB] Error saving spread tick: {e}")

    def get_recent_spread_ticks(self, currency: str = "ETH", limit: int = 1000) -> List[Dict[str, Any]]:
        """Retrieve recent historical spread ticks sorted chronologically."""
        import math
        def clean(v):
            try:
                f = float(v)
                return 0.0 if (math.isnan(f) or math.isinf(f)) else f
            except Exception:
                return 0.0

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, timestamp, currency, spot_price, fut_price, raw_spread, spread_pct, deviation FROM (
                        SELECT id, timestamp, currency, spot_price, fut_price, raw_spread, spread_pct, deviation
                        FROM spread_ticks
                        WHERE currency = ?
                        ORDER BY id DESC
                        LIMIT ?
                    ) ORDER BY id ASC
                """, (currency.upper(), limit))
                results = []
                for r in cursor.fetchall():
                    item = dict(r)
                    item["spot_price"] = clean(item.get("spot_price", 0.0))
                    item["fut_price"] = clean(item.get("fut_price", 0.0))
                    item["raw_spread"] = clean(item.get("raw_spread", 0.0))
                    item["spread_pct"] = clean(item.get("spread_pct", 0.0))
                    item["deviation"] = clean(item.get("deviation", 0.0))
                    results.append(item)
                return results
        except Exception as e:
            logger.error(f"[DB] Error fetching spread ticks: {e}")
            return []

    def get_spread_high_low(self, currency: str = "ETH") -> Dict[str, Any]:
        """Get all-time high, low, and average raw spread for a currency."""
        import math
        def clean(v):
            try:
                f = float(v)
                return 0.0 if (math.isnan(f) or math.isinf(f)) else f
            except Exception:
                return 0.0

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        MAX(raw_spread) as max_spread,
                        MIN(raw_spread) as min_spread,
                        AVG(raw_spread) as avg_spread,
                        COUNT(*) as count
                    FROM spread_ticks
                    WHERE currency = ?
                """, (currency.upper(),))
                row = cursor.fetchone()
                if row and row["count"] and row["count"] > 0 and row["max_spread"] is not None:
                    return {
                        "max_spread": round(clean(row["max_spread"]), 2),
                        "min_spread": round(clean(row["min_spread"]), 2),
                        "avg_spread": round(clean(row["avg_spread"]), 2),
                        "count": int(row["count"])
                    }
        except Exception as e:
            logger.error(f"[DB] Error getting spread high/low: {e}")
        return {"max_spread": 0.0, "min_spread": 0.0, "avg_spread": 0.0, "count": 0}

    def get_today_gap_high_low(self, currency: str = "ETH") -> Dict[str, Any]:
        """Get TODAY'S maximum, minimum, and average gap for a currency."""
        import math
        def clean(v):
            try:
                f = float(v)
                return 0.0 if (math.isnan(f) or math.isinf(f)) else f
            except Exception:
                return 0.0

        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # First check daily_gap_summary table
                cursor.execute("""
                    SELECT max_gap, min_gap, avg_gap, gap_range, total_ticks, date
                    FROM daily_gap_summary
                    WHERE currency = ? AND date = ?
                """, (currency.upper(), today))
                row = cursor.fetchone()
                if row:
                    return {
                        "date": row["date"],
                        "max_gap": round(clean(row["max_gap"]), 2),
                        "min_gap": round(clean(row["min_gap"]), 2),
                        "avg_gap": round(clean(row["avg_gap"]), 2),
                        "gap_range": round(clean(row["gap_range"]), 2),
                        "count": int(row["total_ticks"])
                    }

                # Direct query fallback from spread_ticks
                cursor.execute("""
                    SELECT 
                        MAX(raw_spread) as max_spread,
                        MIN(raw_spread) as min_spread,
                        AVG(raw_spread) as avg_spread,
                        COUNT(*) as count
                    FROM spread_ticks
                    WHERE currency = ? AND (date = ? OR date = '')
                """, (currency.upper(), today))
                r = cursor.fetchone()
                if r and r["count"] and r["count"] > 0 and r["max_spread"] is not None:
                    mx = round(clean(r["max_spread"]), 2)
                    mn = round(clean(r["min_spread"]), 2)
                    return {
                        "date": today,
                        "max_gap": mx,
                        "min_gap": mn,
                        "avg_gap": round(clean(r["avg_gap"]), 2),
                        "gap_range": round(mx - mn, 2),
                        "count": int(r["count"])
                    }
        except Exception as e:
            logger.error(f"[DB] Error getting today's gap high/low: {e}")
        return {"date": today, "max_gap": 0.0, "min_gap": 0.0, "avg_gap": 0.0, "gap_range": 0.0, "count": 0}

    def get_daily_gap_history(self, currency: str = "ETH", limit: int = 30) -> List[Dict[str, Any]]:
        """Retrieve day-by-day historical high/low gap summary records."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT date, currency, max_gap, min_gap, avg_gap, gap_range, total_ticks
                    FROM daily_gap_summary
                    WHERE currency = ?
                    ORDER BY date DESC
                    LIMIT ?
                """, (currency.upper(), limit))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[DB] Error fetching daily gap history: {e}")
            return []


# Global database singleton
db = TradeDatabase()


