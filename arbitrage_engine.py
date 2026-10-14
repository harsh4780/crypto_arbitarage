"""
Arbitrage Calculation & 5-Leg Multi-Tier Execution Engine
=========================================================
Computes real-time spreads, basis percentage, and directional signals.
Manages 5 independent execution legs (Leg 1 to Leg 5) with individual
Open Gap (diffToTrade), Close Gap (diffToCut), Lot Size, and Net TP targets.
Decouples trade execution from persistence for zero-latency execution.
"""

import os
import csv
import json
import time
import logging
from typing import Dict, Any, List, Optional
from collections import deque
from datetime import datetime
from config import config
from database import db

logger = logging.getLogger("arbitrage.engine")

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(CONFIG_DIR, exist_ok=True)

class ArbitrageEngine:
    def __init__(self, currency: str = "ETH", diff_to_trade: Optional[float] = None, **kwargs):
        self.currency = currency.upper()
        # Restore auto_trade state from SQLite config (default False)
        self.auto_trade_enabled = bool(db.get_config("auto_trade", False))
        self.trade_counter = 0

        # Historical buffer for charts & UI (max 200 points)
        self.history: deque = deque(maxlen=config.HISTORICAL_POINTS_MAX)
        self.signals_log: List[Dict[str, Any]] = []
        self.csv_file = config.CSV_LOG_FILE

        # 5-Leg Multi-Tier Matrix Configuration
        self.legs: Dict[int, Dict[str, Any]] = {}
        self._init_default_legs(self.currency)
        if diff_to_trade is not None and 1 in self.legs:
            self.legs[1]["diff_to_trade"] = float(diff_to_trade)
            self._save_legs_config()

        # Restore any active open trades from SQLite so no trade is ever lost
        self._restore_open_positions_from_db()
        self._init_csv()

    def _get_legs_config_file(self, currency: str) -> str:
        return os.path.join(CONFIG_DIR, f"legs_config_{currency.upper()}.json")

    def _save_legs_config(self):
        """Save current 5-leg parameters to disk JSON and SQLite for double-redundancy."""
        try:
            cfg_data = []
            for lid in sorted(self.legs.keys()):
                leg = self.legs[lid]
                cfg_data.append({
                    "leg_id": lid,
                    "enabled": leg["enabled"],
                    "diff_to_trade": leg["diff_to_trade"],
                    "diff_to_cut": leg["diff_to_cut"],
                    "trade_size": leg["trade_size"],
                    "target_profit_usd": leg["target_profit_usd"]
                })
            
            # 1. SQLite config persistence
            db.set_config(f"legs_{self.currency}", cfg_data)
            
            # 2. File persistence
            cfg_file = self._get_legs_config_file(self.currency)
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, indent=2)
            logger.info(f"[Engine] Persisted 5-Leg configuration for {self.currency} to DB and {cfg_file}")
        except Exception as e:
            logger.error(f"[Engine] Failed to persist leg configuration: {e}", exc_info=True)

    def _init_default_legs(self, currency: str):
        """Initialize 5 legs, restoring saved custom values from SQLite/file if available."""
        # 1. Check if saved configuration exists in SQLite or file
        saved_legs = db.get_config(f"legs_{currency}")
        if not saved_legs:
            cfg_file = self._get_legs_config_file(currency)
            if os.path.exists(cfg_file):
                try:
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        saved_legs = json.load(f)
                except Exception:
                    saved_legs = None

        if saved_legs and isinstance(saved_legs, list) and len(saved_legs) >= 5:
            logger.info(f"[Engine] Loading persisted 5-Leg parameters for {currency}")
            defaults = saved_legs
        else:
            defaults = [
                {"leg_id": lid, "enabled": False, "diff_to_trade": 0.0, "diff_to_cut": 0.0, "trade_size": 0.0, "target_profit_usd": 0.0}
                for lid in range(1, 6)
            ]

        for d in defaults:
            lid = int(d["leg_id"])
            existing_pos = self.legs.get(lid, {}).get("active_position")
            status = "OPEN" if existing_pos else "IDLE"
            self.legs[lid] = {
                "leg_id": lid,
                "enabled": bool(d.get("enabled", True)),
                "diff_to_trade": float(d["diff_to_trade"]),
                "diff_to_cut": float(d["diff_to_cut"]),
                "trade_size": float(d["trade_size"]),
                "target_profit_usd": float(d["target_profit_usd"]),
                "status": status,
                "active_position": existing_pos
            }
        logger.info(f"[Engine] Initialized 5-Leg Matrix for {currency} (Persisted settings applied)")

    def _restore_open_positions_from_db(self):
        """Restores any active OPEN trades from SQLite so positions survive server restarts."""
        try:
            open_trades = db.get_open_trades()
            if not open_trades:
                return

            for t in open_trades:
                lid = t.get("leg_id", 1)
                if lid not in self.legs:
                    continue

                ticket = t.get("mt5_ticket")
                fut_id = t.get("fut_order_id")
                trade_id = t.get("trade_id")
                open_iso = t.get("open_time")
                open_time_str = open_iso.split("T")[1][:8] if "T" in open_iso else open_iso

                restored_pos = {
                    "id": trade_id,
                    "trade_id": trade_id,
                    "leg_id": lid,
                    "open_time": open_time_str,
                    "open_iso": open_iso,
                    "open_perf_time": time.time(),
                    "symbol_spot": t.get("symbol_spot"),
                    "symbol_fut": t.get("symbol_fut"),
                    "entry_gap": float(t.get("entry_gap", 0.0)),
                    "spot_price": float(t.get("entry_spot_price", 0.0)),
                    "fut_price": float(t.get("entry_fut_price", 0.0)),
                    "trade_size": float(t.get("trade_size", self.legs[lid]["trade_size"])),
                    "status": f"OPEN (MT5 #{ticket} | {t.get('exchange', 'FUT')} #{fut_id})",
                    "signal": t.get("action_type", "BUY_SPOT_SELL_FUTURES"),
                    "ticket": ticket,
                    "fut_order": {"order_id": fut_id},
                    "execution_json": t.get("execution_json") or {}
                }

                self.legs[lid]["status"] = "OPEN"
                self.legs[lid]["active_position"] = restored_pos
                
                # Keep counter above restored IDs
                try:
                    num_part = int(trade_id.split("-")[-1])
                    if num_part >= self.trade_counter:
                        self.trade_counter = num_part
                except Exception:
                    pass

                logger.warning(f"[Engine] 🛡️ RESTORED OPEN TRADE from DB: {trade_id} on Leg {lid} (Ticket #{ticket})")
        except Exception as e:
            logger.error(f"[Engine] Error restoring open positions from DB: {e}", exc_info=True)

    def set_currency(self, currency: str):
        """Update currency and re-initialize default leg scales."""
        self.currency = currency.upper()
        self._init_default_legs(self.currency)
        self._restore_open_positions_from_db()

    def _init_csv(self):
        """Create CSV file with headers if not already created."""
        if not os.path.exists(self.csv_file):
            try:
                with open(self.csv_file, mode="w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Timestamp", "MT5_Spot_Mid", "Futures_Mid",
                        "Live_Gap_USD", "Gap_Pct", "Signal", "Open_Legs_Count"
                    ])
            except Exception as e:
                logger.error(f"[Engine] Failed to initialize CSV log: {e}")

    @property
    def diff_to_trade(self) -> float:
        """Backward compatibility: return Leg 1's diff_to_trade."""
        return self.legs.get(1, {}).get("diff_to_trade", 4.0)

    @property
    def diff_to_cut(self) -> float:
        """Backward compatibility: return Leg 1's diff_to_cut."""
        return self.legs.get(1, {}).get("diff_to_cut", 0.5)

    @property
    def trade_size(self) -> float:
        """Backward compatibility: return Leg 1's trade_size."""
        return self.legs.get(1, {}).get("trade_size", 0.1)

    @property
    def target_profit_usd(self) -> float:
        """Backward compatibility: return Leg 1's target_profit_usd."""
        return self.legs.get(1, {}).get("target_profit_usd", 1.0)

    @property
    def active_positions(self) -> List[Dict[str, Any]]:
        """Collect all currently open positions across all 5 legs."""
        positions = []
        for lid in sorted(self.legs.keys()):
            pos = self.legs[lid].get("active_position")
            if pos:
                positions.append(pos)
        return positions

    def update_leg_config(self, leg_id: int, diff_to_trade: float, diff_to_cut: float, 
                          trade_size: float, target_profit_usd: float, enabled: bool) -> Dict[str, Any]:
        """Update parameters for an individual leg."""
        if leg_id not in self.legs:
            raise ValueError(f"Invalid leg ID {leg_id}. Must be between 1 and 5.")

        leg = self.legs[leg_id]
        leg["diff_to_trade"] = float(diff_to_trade)
        leg["diff_to_cut"] = float(diff_to_cut)
        leg["trade_size"] = float(trade_size)
        leg["target_profit_usd"] = float(target_profit_usd)
        leg["enabled"] = bool(enabled)
        self._save_legs_config()
        logger.info(f"[Engine] Updated Leg {leg_id}: OpenGap={diff_to_trade}, CutGap={diff_to_cut}, Size={trade_size}, TP=${target_profit_usd:.2f}, Enabled={enabled}")
        return leg

    def batch_update_legs(self, legs_data: List[Dict[str, Any]]):
        """Update multiple or all 5 legs in a single call."""
        for item in legs_data:
            lid = int(item.get("leg_id", 0))
            if lid in self.legs:
                leg = self.legs[lid]
                leg["diff_to_trade"] = float(item.get("diff_to_trade", leg["diff_to_trade"]))
                leg["diff_to_cut"] = float(item.get("diff_to_cut", leg["diff_to_cut"]))
                leg["trade_size"] = float(item.get("trade_size", leg["trade_size"]))
                leg["target_profit_usd"] = float(item.get("target_profit_usd", leg["target_profit_usd"]))
                leg["enabled"] = bool(item.get("enabled", leg["enabled"]))
        self._save_legs_config()

    def update_parameters(self, diff_to_trade: float, diff_to_cut: float, trade_size: float, auto_trade: bool, target_profit_usd: float = 1.0):
        """Global update that applies to Leg 1 and sets auto-trade toggle."""
        self.auto_trade_enabled = auto_trade
        db.set_config("auto_trade", self.auto_trade_enabled)
        if 1 in self.legs:
            self.legs[1]["diff_to_trade"] = diff_to_trade
            self.legs[1]["diff_to_cut"] = diff_to_cut
            self.legs[1]["trade_size"] = trade_size
            self.legs[1]["target_profit_usd"] = target_profit_usd
        self._save_legs_config()
        logger.info(f"[Engine] Global Parameters updated: Leg1 OpenGap={diff_to_trade}, CutGap={diff_to_cut}, Size={trade_size}, AutoTrade={auto_trade}, TP=${target_profit_usd:.2f}")

    def evaluate(self, mt5_tick: Optional[Dict[str, Any]], futures_tick: Optional[Dict[str, Any]], 
                 mt5_conn: Any = None, futures_conn: Any = None) -> Dict[str, Any]:
        """
        Processes real-time market data ticks, evaluates trigger conditions across all 5 legs,
        and fires automated entries/cuts concurrently.
        Decoupled from DB writes to guarantee sub-millisecond execution.
        """
        now_ts = int(time.time() * 1000)
        formatted_time = time.strftime("%H:%M:%S")

        if not mt5_tick or not futures_tick:
            return {
                "timestamp": now_ts,
                "time_str": formatted_time,
                "status": "WAITING_FOR_DATA",
                "mt5_mid": mt5_tick.get("mid", 0.0) if mt5_tick else 0.0,
                "delta_mid": futures_tick.get("mid", 0.0) if futures_tick else 0.0,
                "spread_usd": 0.0,
                "spread_pct": 0.0,
                "auto_trade": self.auto_trade_enabled,
                "signal": "NO_DATA",
                "is_opportunity": False,
                "legs": list(self.legs.values()),
                "active_positions_count": len(self.active_positions)
            }

        mt5_mid = mt5_tick["mid"]
        futures_mid = futures_tick["mid"]

        # Calculate Live Gap: Futures - Spot
        spread_usd = futures_mid - mt5_mid
        spread_pct = (spread_usd / mt5_mid * 100.0) if mt5_mid > 0 else 0.0

        spot_ask = mt5_tick.get("ask", mt5_mid)
        spot_bid = mt5_tick.get("bid", mt5_mid)
        futures_bid = futures_tick.get("bid", futures_mid)
        futures_ask = futures_tick.get("ask", futures_mid)

        mt5_spread = max(0.0, spot_ask - spot_bid)
        futures_spread = max(0.0, futures_ask - futures_bid)
        spread_buy_spot_sell_fut = futures_bid - spot_ask
        spread_sell_spot_buy_fut = spot_bid - futures_ask

        # Estimated friction % (Binance taker ~0.08%, MT5 spread ~0.05%, slippage ~0.04% = ~0.17%)
        total_friction_pct = 0.17

        # -------------------------------------------------------------
        # STEP 1: EVALUATE & CLOSE TRIGGERED OPEN LEGS
        # -------------------------------------------------------------
        closed_legs_logs = []
        for lid in sorted(self.legs.keys()):
            leg = self.legs[lid]
            pos = leg.get("active_position")
            if not pos or leg.get("status") != "OPEN":
                continue

            entry_gap = pos.get("entry_gap", spread_usd)
            pos_size = pos.get("trade_size", leg["trade_size"])

            captured_gap = (entry_gap - spread_usd) if entry_gap >= 0 else (spread_usd - entry_gap)
            gross_pnl = captured_gap * pos_size
            friction_usd = (mt5_mid + futures_mid) * (total_friction_pct / 100.0) * pos_size
            net_pnl = gross_pnl - friction_usd

            # Update live position stats
            pos["current_gap"] = round(spread_usd, 2)
            pos["captured_gap"] = round(captured_gap, 2)
            pos["live_net_pnl"] = round(net_pnl, 2)
            pos["live_gross_pnl"] = round(gross_pnl, 2)
            pos["total_friction"] = round(friction_usd, 2)

            should_close = False
            close_reason = ""

            # Check Net TP Target
            if net_pnl >= leg["target_profit_usd"]:
                should_close = True
                close_reason = f"TP_REACHED (+${net_pnl:.2f} >= ${leg['target_profit_usd']:.2f})"
            # Check Cut Gap Points
            elif abs(spread_usd) <= leg["diff_to_cut"]:
                should_close = True
                close_reason = f"DIFF_TO_CUT (Gap ${spread_usd:+.2f} <= ${leg['diff_to_cut']:.2f})"

            if should_close and self.auto_trade_enabled:
                close_res = self._execute_leg_close(
                    leg_id=lid,
                    close_reason=close_reason,
                    mt5_tick=mt5_tick,
                    futures_tick=futures_tick,
                    mt5_conn=mt5_conn,
                    futures_conn=futures_conn
                )
                closed_legs_logs.append(f"Auto-Closed Leg {lid}: {close_reason} (Net PnL: ${net_pnl:+.2f})")

        # -------------------------------------------------------------
        # STEP 2: EVALUATE & OPEN TRIGGERED IDLE LEGS
        # -------------------------------------------------------------
        opened_legs_logs = []
        is_opportunity = False
        primary_signal = "NEUTRAL"
        primary_action_detail = ""

        # Check if spread is wide enough for any enabled leg
        for lid in sorted(self.legs.keys()):
            leg = self.legs[lid]
            if not leg["enabled"]:
                continue

            open_threshold = leg["diff_to_trade"]
            if open_threshold <= 0.0 or leg["trade_size"] <= 0.0:
                continue

            if abs(spread_usd) >= open_threshold:
                is_opportunity = True
                if spread_usd >= open_threshold:
                    signal_type = "BUY_SPOT_SELL_FUTURES"
                    detail = f"Leg {lid} Triggered: Buy Spot @ {spot_ask:.2f} | Sell Fut @ {futures_bid:.2f} (Gap ${spread_usd:+.2f} >= ${open_threshold:.2f})"
                else:
                    signal_type = "SELL_SPOT_BUY_FUTURES"
                    detail = f"Leg {lid} Triggered: Sell Spot @ {spot_bid:.2f} | Buy Fut @ {futures_ask:.2f} (Gap ${spread_usd:+.2f} <= -${open_threshold:.2f})"

                if primary_signal == "NEUTRAL":
                    primary_signal = signal_type
                    primary_action_detail = detail

                # If this leg is IDLE and auto-trade is enabled, FIRE it immediately!
                if leg.get("status") == "IDLE" and self.auto_trade_enabled:
                    open_res = self._execute_leg_open(
                        leg_id=lid,
                        spread_usd=spread_usd,
                        signal_type=signal_type,
                        mt5_tick=mt5_tick,
                        futures_tick=futures_tick,
                        mt5_conn=mt5_conn,
                        futures_conn=futures_conn
                    )
                    if open_res.get("success"):
                        opened_legs_logs.append(f"Auto-Opened Leg {lid} @ Gap ${spread_usd:+.2f} (Ticket #{open_res.get('ticket')})")
                    else:
                        opened_legs_logs.append(f"Leg {lid} Open Failed: {open_res.get('error')}")

        # Summary PnL across active positions
        total_live_net_pnl = sum(p.get("live_net_pnl", 0.0) for p in self.active_positions)
        total_live_gross_pnl = sum(p.get("live_gross_pnl", 0.0) for p in self.active_positions)
        total_friction = sum(p.get("total_friction", 0.0) for p in self.active_positions)

        auto_action_log = None
        if opened_legs_logs or closed_legs_logs:
            auto_action_log = " | ".join(opened_legs_logs + closed_legs_logs)

        result = {
            "timestamp": now_ts,
            "time_str": formatted_time,
            "status": "ACTIVE",
            "mt5": mt5_tick,
            "delta": futures_tick,
            "mt5_mid": mt5_mid,
            "delta_mid": futures_mid,
            "spread_usd": round(spread_usd, 2),
            "spread_pct": round(spread_pct, 4),
            "mt5_spread": round(mt5_spread, 2),
            "futures_spread": round(futures_spread, 2),
            "spread_buy_spot_sell_fut": round(spread_buy_spot_sell_fut, 2),
            "spread_sell_spot_buy_fut": round(spread_sell_spot_buy_fut, 2),
            "diff_to_trade": self.diff_to_trade,
            "diff_to_cut": self.diff_to_cut,
            "target_profit_usd": self.target_profit_usd,
            "trade_size": self.trade_size,
            "auto_trade": self.auto_trade_enabled,
            "signal": primary_signal,
            "is_opportunity": is_opportunity,
            "action_detail": primary_action_detail or f"Monitoring 5 Legs. Live Gap: ${spread_usd:+.2f}",
            "auto_action_log": auto_action_log,
            "legs": list(self.legs.values()),
            "active_positions": self.active_positions,
            "active_positions_count": len(self.active_positions),
            "gross_pnl_usd": round(total_live_gross_pnl, 2),
            "estimated_fee_usd": round(total_friction, 2),
            "net_pnl_usd": round(total_live_net_pnl, 2)
        }

        self.history.append(result)
        if is_opportunity:
            self.signals_log.append(result)
        self._write_csv(result)

        return result

    # -------------------------------------------------------------
    # FAST ORDER EXECUTION (DECOUPLED FROM DISK / DB I/O)
    # -------------------------------------------------------------
    def _execute_leg_open(self, leg_id: int, spread_usd: float, signal_type: str,
                           mt5_tick: Dict[str, Any], futures_tick: Dict[str, Any],
                           mt5_conn: Any, futures_conn: Any) -> Dict[str, Any]:
        """
        Executes order pair on MT5 and Futures exchange for a specific leg.
        Dispatches telemetry JSON to background SQLite worker after execution.
        """
        t_start = time.perf_counter()
        leg = self.legs[leg_id]
        leg_size = float(leg.get("trade_size", 0.0))
        if leg_size <= 0:
            default_sz = 0.01 if "BTC" in self.currency.upper() else 0.1
            logger.warning(f"[Engine] Leg {leg_id} configured trade_size was 0.0 lots! Applying safe default {default_sz} lots.")
            leg_size = default_sz

        spot_action = "BUY" if spread_usd >= 0 else "SELL"
        futures_action = "Sell" if spread_usd >= 0 else "Buy"

        mt5_symbol = mt5_tick.get("symbol", f"{self.currency}USD")
        fut_symbol = futures_tick.get("symbol", "")

        # 1. MT5 Execution
        t_mt5_sent = time.perf_counter()
        mt5_res = {"success": True, "ticket": 0, "comment": "Simulated"}
        if mt5_conn:
            mt5_res = mt5_conn.place_order(
                symbol=mt5_symbol,
                order_type=spot_action,
                volume=leg_size,
                comment=f"Leg{leg_id} Gap ${spread_usd:+.2f}"
            )
        t_mt5_done = time.perf_counter()

        if not mt5_res.get("success"):
            err_msg = mt5_res.get("error", "MT5 order rejected")
            logger.error(f"[Engine] Leg {leg_id} MT5 order failed: {err_msg}")
            return {"success": False, "error": err_msg}

        ticket = mt5_res.get("ticket", 0)

        # 2. Futures Hedge Execution
        t_fut_sent = time.perf_counter()
        fut_order_info = None
        ex_name = futures_conn.__class__.__name__.replace("Connector", "") if futures_conn else "Futures"
        fut_status = f"{ex_name}: Simulated / No Key"

        if futures_conn and getattr(futures_conn, "api_key", None):
            try:
                # Check for Coin-M or USD-M if Binance
                if hasattr(futures_conn, "place_coinm_order") and "_" in fut_symbol and "USDT" not in fut_symbol:
                    c_val = 10 if self.currency == "ETH" else 100
                    contracts_qty = max(1, int(round((leg_size * futures_tick["mid"]) / c_val)))
                    fut_order_info = futures_conn.place_coinm_order(symbol=fut_symbol, side=futures_action.upper(), quantity=contracts_qty)
                else:
                    fut_order_info = futures_conn.place_order(symbol=fut_symbol, side=futures_action, qty=leg_size, order_type="Market")

                if fut_order_info and fut_order_info.get("success"):
                    fut_status = f"{ex_name} Order #{fut_order_info.get('order_id')}"
                else:
                    fut_status = f"{ex_name} Err: {fut_order_info.get('error') if fut_order_info else 'Failed'}"
                    logger.error(f"[Engine] Leg {leg_id} Futures hedge failed: {fut_status}")
                    # Circuit breaker: close MT5 orphan
                    if mt5_conn and ticket:
                        logger.warning(f"[Engine] Circuit breaker: Closing orphan MT5 ticket #{ticket}")
                        mt5_conn.close_position(ticket)
                    return {"success": False, "error": fut_status}
            except Exception as e:
                logger.error(f"[Engine] Exception during Futures hedge: {e}", exc_info=True)
                if mt5_conn and ticket:
                    mt5_conn.close_position(ticket)
                return {"success": False, "error": str(e)}

        t_fut_done = time.perf_counter()
        total_latency_ms = (t_fut_done - t_start) * 1000.0

        self.trade_counter += 1
        trade_id = f"PAIR-LEG{leg_id}-{self.trade_counter}"
        open_iso = datetime.now().isoformat()
        open_time_str = time.strftime("%H:%M:%S")

        # Telemetry Execution JSON
        execution_json = {
            "trade_id": trade_id,
            "leg_id": leg_id,
            "timestamps": {
                "open_iso": open_iso,
                "mt5_latency_ms": round((t_mt5_done - t_mt5_sent) * 1000.0, 2),
                "futures_latency_ms": round((t_fut_done - t_fut_sent) * 1000.0, 2),
                "total_order_latency_ms": round(total_latency_ms, 2)
            },
            "entry_snapshot": {
                "spread_usd": round(spread_usd, 2),
                "spot_mid": mt5_tick.get("mid"),
                "spot_bid": mt5_tick.get("bid"),
                "spot_ask": mt5_tick.get("ask"),
                "fut_mid": futures_tick.get("mid"),
                "fut_bid": futures_tick.get("bid"),
                "fut_ask": futures_tick.get("ask"),
                "leg_target_open": leg["diff_to_trade"],
                "leg_target_cut": leg["diff_to_cut"]
            },
            "broker_responses": {
                "mt5": mt5_res,
                "futures": fut_order_info
            }
        }

        new_pos = {
            "id": trade_id,
            "trade_id": trade_id,
            "leg_id": leg_id,
            "open_time": open_time_str,
            "open_iso": open_iso,
            "open_perf_time": time.time(),
            "symbol_spot": mt5_symbol,
            "symbol_fut": fut_symbol,
            "entry_gap": round(spread_usd, 2),
            "spot_price": mt5_tick["mid"],
            "fut_price": futures_tick["mid"],
            "trade_size": leg_size,
            "status": f"OPEN (MT5 #{ticket} | {fut_status})",
            "signal": signal_type,
            "ticket": ticket,
            "fut_order": fut_order_info,
            "execution_json": execution_json
        }

        # Update in-memory leg state immediately
        leg["status"] = "OPEN"
        leg["active_position"] = new_pos

        # POST-EXECUTION: Asynchronously record trade open in SQLite
        db.record_trade_open_async({
            "trade_id": trade_id,
            "leg_id": leg_id,
            "symbol_spot": mt5_symbol,
            "symbol_fut": fut_symbol,
            "exchange": ex_name.upper(),
            "action_type": signal_type,
            "trade_size": leg_size,
            "entry_spot_price": mt5_tick["mid"],
            "entry_fut_price": futures_tick["mid"],
            "entry_gap": round(spread_usd, 2),
            "open_time": open_iso,
            "status": "OPEN",
            "mt5_ticket": ticket,
            "fut_order_id": (fut_order_info.get("order_id") if fut_order_info else ""),
            "execution_json": execution_json
        })

        logger.info(f"[Engine] Leg {leg_id} OPENED: {trade_id} @ Gap ${spread_usd:+.2f} | Latency: {total_latency_ms:.1f}ms")
        return {"success": True, "ticket": ticket, "trade_id": trade_id, "position": new_pos}

    def _execute_leg_close(self, leg_id: int, close_reason: str,
                           mt5_tick: Dict[str, Any], futures_tick: Dict[str, Any],
                           mt5_conn: Any, futures_conn: Any) -> Dict[str, Any]:
        """
        Closes position on MT5 and Futures exchange for a specific leg.
        Dispatches updated telemetry and PnL to SQLite asynchronously.
        """
        t_start = time.perf_counter()
        leg = self.legs.get(leg_id)
        if not leg or not leg.get("active_position"):
            return {"success": False, "error": f"No active position for Leg {leg_id}"}

        pos = leg["active_position"]
        ticket = pos.get("ticket")
        spot_sym = pos.get("symbol_spot")
        fut_sym = pos.get("symbol_fut")
        open_perf = pos.get("open_perf_time", time.time())
        duration_sec = round(time.time() - open_perf, 1)

        # 1. Close MT5 position
        if mt5_conn:
            if ticket:
                mt5_conn.close_position(ticket)
            else:
                mt5_conn.close_all_positions(symbol=spot_sym)

        # 2. Close Futures position
        if futures_conn and getattr(futures_conn, "api_key", None):
            try:
                futures_conn.close_all_positions(fut_sym)
            except Exception as e:
                logger.error(f"[Engine] Leg {leg_id} Futures close error: {e}")

        total_latency_ms = (time.perf_counter() - t_start) * 1000.0

        current_gap = round(futures_tick["mid"] - mt5_tick["mid"], 2)
        entry_gap = pos.get("entry_gap", 0.0)
        size = pos.get("trade_size", leg["trade_size"])
        captured_gap = round((entry_gap - current_gap) if entry_gap >= 0 else (current_gap - entry_gap), 2)
        gross_pnl = round(captured_gap * size, 2)
        fees = round((mt5_tick["mid"] + futures_tick["mid"]) * (0.17 / 100.0) * size, 2)
        net_pnl = round(gross_pnl - fees, 2)

        close_iso = datetime.now().isoformat()

        # Update telemetry JSON with exit snapshot
        exec_json = pos.get("execution_json") or {}
        exec_json["exit_snapshot"] = {
            "close_iso": close_iso,
            "exit_spot_mid": mt5_tick["mid"],
            "exit_fut_mid": futures_tick["mid"],
            "exit_gap": current_gap,
            "captured_gap": captured_gap,
            "gross_pnl": gross_pnl,
            "fees": fees,
            "net_pnl": net_pnl,
            "close_reason": close_reason,
            "close_latency_ms": round(total_latency_ms, 2)
        }

        # Clear in-memory leg state
        leg["status"] = "IDLE"
        leg["active_position"] = None

        # POST-EXECUTION: Asynchronously update SQLite record
        db.record_trade_close_async({
            "trade_id": pos["trade_id"],
            "exit_spot_price": mt5_tick["mid"],
            "exit_fut_price": futures_tick["mid"],
            "exit_gap": current_gap,
            "close_time": close_iso,
            "duration_sec": duration_sec,
            "pnl_gross": gross_pnl,
            "pnl_net": net_pnl,
            "fee_estimated": fees,
            "status": "CLOSED",
            "exit_reason": close_reason,
            "execution_json": exec_json
        })

        logger.info(f"[Engine] Leg {leg_id} CLOSED: {pos['trade_id']} | Reason: {close_reason} | Net PnL: ${net_pnl:+.2f} | Duration: {duration_sec}s")
        return {"success": True, "leg_id": leg_id, "net_pnl": net_pnl, "close_reason": close_reason}

    # -------------------------------------------------------------
    # MANUAL ACTIONS (USER UI BUTTONS)
    # -------------------------------------------------------------
    def fire_manual_leg(self, leg_id: Optional[int], mt5_tick: Dict[str, Any], futures_tick: Dict[str, Any],
                        mt5_conn: Any, futures_conn: Any) -> Dict[str, Any]:
        """Manually trigger entry for a specific leg (or first idle leg if leg_id is None)."""
        target_lid = leg_id
        if target_lid is None:
            # Pick first idle enabled leg
            for lid in sorted(self.legs.keys()):
                if self.legs[lid]["enabled"] and self.legs[lid]["status"] == "IDLE":
                    target_lid = lid
                    break

        if target_lid is None or target_lid not in self.legs:
            return {"success": False, "error": "No idle leg available to fire. Close an active leg first."}

        leg = self.legs[target_lid]
        if leg["status"] == "OPEN":
            return {"success": False, "error": f"Leg {target_lid} is already open. Close it first or select another leg."}

        spread_usd = futures_tick["mid"] - mt5_tick["mid"]
        signal_type = "BUY_SPOT_SELL_FUTURES" if spread_usd >= 0 else "SELL_SPOT_BUY_FUTURES"

        return self._execute_leg_open(
            leg_id=target_lid,
            spread_usd=spread_usd,
            signal_type=f"MANUAL_LEG_{target_lid}",
            mt5_tick=mt5_tick,
            futures_tick=futures_tick,
            mt5_conn=mt5_conn,
            futures_conn=futures_conn
        )

    def close_single_leg(self, leg_id: int, mt5_tick: Dict[str, Any], futures_tick: Dict[str, Any],
                         mt5_conn: Any, futures_conn: Any) -> Dict[str, Any]:
        """Manually close a specific open leg."""
        return self._execute_leg_close(
            leg_id=leg_id,
            close_reason="MANUAL_CLOSE",
            mt5_tick=mt5_tick,
            futures_tick=futures_tick,
            mt5_conn=mt5_conn,
            futures_conn=futures_conn
        )

    def close_all_positions(self, mt5_tick: Dict[str, Any], futures_tick: Dict[str, Any],
                            mt5_conn: Any, futures_conn: Any) -> Dict[str, Any]:
        """Closes all currently open legs."""
        closed_count = 0
        for lid in sorted(self.legs.keys()):
            if self.legs[lid]["status"] == "OPEN":
                self._execute_leg_close(
                    leg_id=lid,
                    close_reason="MANUAL_CLOSE_ALL",
                    mt5_tick=mt5_tick,
                    futures_tick=futures_tick,
                    mt5_conn=mt5_conn,
                    futures_conn=futures_conn
                )
                closed_count += 1
        return {"success": True, "closed_count": closed_count}

    def _write_csv(self, res: Dict[str, Any]):
        """Write record to CSV log."""
        try:
            mt5 = res.get("mt5", {})
            delta = res.get("delta", {})
            with open(self.csv_file, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    res["time_str"],
                    mt5.get("mid", 0), delta.get("mid", 0),
                    res["spread_usd"], res["spread_pct"], res["signal"],
                    res["active_positions_count"]
                ])
        except Exception:
            pass

    def get_history_list(self) -> List[Dict[str, Any]]:
        return list(self.history)
