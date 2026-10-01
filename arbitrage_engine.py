"""
Arbitrage Calculation & Execution Engine
==========================================
Computes real-time spreads, basis percentage, directional signals,
manages Set-based Open Gap (diffToTrade) & Close Gap (diffToCut) levels,
and handles automated trade firing matching MQL5 EA logic (mql_Ea_v1.py).
"""

import os
import csv
import time
import logging
from typing import Dict, Any, List, Optional
from collections import deque
from config import config

logger = logging.getLogger(__name__)

class ArbitrageEngine:
    def __init__(self, diff_to_trade: float = config.MIN_SPREAD_USD, 
                 diff_to_cut: float = 1.0,
                 trade_size: float = config.DEFAULT_TRADE_SIZE_ETH,
                 target_profit_usd: float = 1.0):
        
        # MQL5 EA Ported Parameters
        self.diff_to_trade = diff_to_trade   # Open Gap threshold (points/USD)
        self.diff_to_cut = diff_to_cut       # Close Cut Gap threshold (points/USD)
        self.trade_size = trade_size         # Trade Volume / Lot size
        self.target_profit_usd = target_profit_usd # Target Net Profit after all fees/slippage (e.g. $1.00 USD)
        self.auto_trade_enabled = False      # Toggle auto order firing
        
        # Track Active Trades (Set Pairs)
        self.active_positions: List[Dict[str, Any]] = []
        self.trade_counter = 0

        # Historical buffer for charts & UI (max 200 points)
        self.history: deque = deque(maxlen=config.HISTORICAL_POINTS_MAX)
        self.signals_log: List[Dict[str, Any]] = []
        self.csv_file = config.CSV_LOG_FILE

        self._init_csv()

    def _init_csv(self):
        """Create CSV file with headers if not already created."""
        if not os.path.exists(self.csv_file):
            try:
                with open(self.csv_file, mode="w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Timestamp", "MT5_Spot_Mid", "Futures_Mid",
                        "Live_Gap_USD", "Gap_Pct", "Signal", "Auto_Trade_Fired"
                    ])
            except Exception as e:
                logger.error(f"Failed to initialize CSV log: {e}")

    def update_parameters(self, diff_to_trade: float, diff_to_cut: float, trade_size: float, auto_trade: bool, target_profit_usd: float = 1.0):
        """Update EA settings dynamically from Web Dashboard."""
        self.diff_to_trade = diff_to_trade
        self.diff_to_cut = diff_to_cut
        self.trade_size = trade_size
        self.auto_trade_enabled = auto_trade
        self.target_profit_usd = target_profit_usd
        logger.info(f"[Engine] Parameters updated: OpenGap={diff_to_trade}, CloseGap={diff_to_cut}, Size={trade_size}, AutoTrade={auto_trade}, TargetProfit=${target_profit_usd:.2f}")

    def evaluate(self, mt5_tick: Optional[Dict[str, Any]], futures_tick: Optional[Dict[str, Any]], mt5_conn: Any = None, futures_conn: Any = None) -> Dict[str, Any]:
        """Process MT5 Spot & Futures ticks, compare against diffToTrade & diffToCut, handle auto-execution."""
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
                "diff_to_trade": self.diff_to_trade,
                "diff_to_cut": self.diff_to_cut,
                "auto_trade": self.auto_trade_enabled,
                "signal": "NO_DATA",
                "is_opportunity": False,
                "active_positions_count": len(self.active_positions)
            }

        mt5_mid = mt5_tick["mid"]
        futures_mid = futures_tick["mid"]

        # Calculate Live Gap: Futures - Spot
        spread_usd = futures_mid - mt5_mid
        spread_pct = (spread_usd / mt5_mid * 100.0) if mt5_mid > 0 else 0.0

        # Best Execution Gap: Futures Bid - Spot Ask
        spot_ask = mt5_tick.get("ask", mt5_mid)
        futures_bid = futures_tick.get("bid", futures_mid)

        # Best Execution Gap: Spot Bid - Futures Ask
        spot_bid = mt5_tick.get("bid", mt5_mid)
        futures_ask = futures_tick.get("ask", futures_mid)

        # Individual pair Bid/Ask spreads
        mt5_spread = max(0.0, spot_ask - spot_bid)
        futures_spread = max(0.0, futures_ask - futures_bid)
        spread_buy_spot_sell_fut = futures_bid - spot_ask
        spread_sell_spot_buy_fut = spot_bid - futures_ask

        signal = "NEUTRAL"
        is_opportunity = False
        action_detail = ""
        should_fire_open = False
        should_fire_close = False

        # OPEN TRIGGER CHECK (diffToTrade)
        if abs(spread_usd) >= self.diff_to_trade:
            is_opportunity = True
            if spread_usd >= self.diff_to_trade:
                signal = "BUY_SPOT_SELL_FUTURES"
                action_detail = f"OPEN: Buy Spot @ {spot_ask:.2f} | Sell Futures @ {futures_bid:.2f} (Gap: ${spread_usd:+.2f} >= ${self.diff_to_trade:.2f})"
                should_fire_open = True
            elif spread_usd <= -self.diff_to_trade:
                signal = "SELL_SPOT_BUY_FUTURES"
                action_detail = f"OPEN: Sell Spot @ {spot_bid:.2f} | Buy Futures @ {futures_ask:.2f} (Gap: ${spread_usd:+.2f} <= -${self.diff_to_trade:.2f})"
                should_fire_open = True

        # Estimated round-trip friction %:
        # Binance taker 0.10% + MT5 spread (~$1.50 = 0.05%) + slippage buffer (~$0.50 = 0.02%) = ~0.17%
        total_friction_pct = 0.17

        # CLOSE CUT CHECK (diffToCut & Target Net Profit) for active positions
        if self.active_positions:
            for pos in self.active_positions:
                entry_gap = pos.get("entry_gap", spread_usd)
                pos_size = pos.get("trade_size", self.trade_size)
                
                # If entered positive gap (bought spot, sold futures), profit when gap narrows
                captured_gap = (entry_gap - spread_usd) if entry_gap >= 0 else (spread_usd - entry_gap)
                gross_pnl = captured_gap * pos_size
                friction_usd = (mt5_mid + futures_mid) * (total_friction_pct / 100.0) * pos_size
                net_pnl = gross_pnl - friction_usd
                
                pos["current_gap"] = round(spread_usd, 2)
                pos["captured_gap"] = round(captured_gap, 2)
                pos["live_net_pnl"] = round(net_pnl, 2)
                pos["total_friction"] = round(friction_usd, 2)
                
                # Take profit triggers when net PnL hits target (e.g. $1.00 USD) OR gap narrows to diff_to_cut
                if net_pnl >= self.target_profit_usd:
                    should_fire_close = True
                    signal = "TAKE_PROFIT_REACHED"
                    action_detail = f"TAKE PROFIT: Net Profit reached +${net_pnl:.2f} (Target: +${self.target_profit_usd:.2f}) after all fees & slippage! (Gap compressed by ${captured_gap:.2f})"
                    break
                elif abs(spread_usd) <= self.diff_to_cut:
                    should_fire_close = True
                    signal = "CLOSE_ARBITRAGE_PAIR"
                    action_detail = f"CLOSE: Gap compressed to ${spread_usd:+.2f} (Target Cut: ${self.diff_to_cut:.2f}), Net PnL: ${net_pnl:+.2f}"
                    break

        # Handle Automated Firing if Auto-Trade Enabled
        auto_action_log = None
        if self.auto_trade_enabled:
            if should_fire_open and len(self.active_positions) == 0:
                mt5_symbol = mt5_tick.get("symbol", "ETHUSD")
                spot_action = "BUY" if spread_usd >= 0 else "SELL"
                futures_action = "Sell" if spread_usd >= 0 else "Buy"
                
                mt5_res = {"success": True, "comment": "Simulated"}
                if mt5_conn:
                    mt5_res = mt5_conn.place_order(symbol=mt5_symbol, order_type=spot_action, volume=self.trade_size, comment=f"Auto Gap ${spread_usd:+.2f}")
                
                if mt5_res.get("success"):
                    self.trade_counter += 1
                    mt5_status = f"MT5 Ticket #{mt5_res.get('ticket')}"
                    
                    # Also place hedge order on futures exchange if credentials configured
                    fut_order_info = None
                    ex_name = futures_conn.__class__.__name__.replace("Connector", "") if futures_conn else "Futures"
                    fut_status = f"{ex_name}: No API Key (Connect via 🔑 Connect API)"
                    if futures_conn and getattr(futures_conn, "api_key", None):
                        fut_sym = futures_tick.get("symbol")
                        try:
                            fut_order_info = futures_conn.place_order(symbol=fut_sym, side=futures_action, qty=self.trade_size, order_type="Market")
                            if fut_order_info.get("success"):
                                fut_status = f"{ex_name} Order #{fut_order_info.get('order_id')}"
                                logger.info(f"[Engine] {ex_name} order executed successfully: {fut_status}")
                            else:
                                fut_status = f"{ex_name} ERR: {fut_order_info.get('error')}"
                                logger.error(f"[Engine] {ex_name} order failed: {fut_status}")
                                # CIRCUIT BREAKER: Hedge leg failed! Close MT5 orphan position & pause auto-trade
                                if mt5_conn and mt5_res.get("ticket"):
                                    logger.warning(f"[Engine] Circuit breaker: Closing orphan MT5 ticket #{mt5_res.get('ticket')} due to hedge failure.")
                                    mt5_conn.close_position(mt5_res.get("ticket"))
                                self.auto_trade_enabled = False
                                auto_action_log = f"AUTO-TRADE PAUSED: {ex_name} hedge failed ({fut_order_info.get('error')})"
                                action_detail = f"Hedge Failed: {fut_order_info.get('error')}. Auto-trade paused for safety."
                                logger.warning(f"[Engine] {auto_action_log}")
                                return {
                                    "timestamp": now_ts,
                                    "time_str": formatted_time,
                                    "status": "CIRCUIT_BREAKER",
                                    "mt5": mt5_tick,
                                    "delta": futures_tick,
                                    "mt5_mid": mt5_mid,
                                    "delta_mid": futures_mid,
                                    "spread_usd": round(spread_usd, 2),
                                    "spread_pct": round(spread_pct, 4),
                                    "diff_to_trade": self.diff_to_trade,
                                    "diff_to_cut": self.diff_to_cut,
                                    "trade_size": self.trade_size,
                                    "auto_trade": False,
                                    "signal": signal,
                                    "is_opportunity": False,
                                    "action_detail": action_detail,
                                    "auto_action_log": auto_action_log,
                                    "active_positions": self.active_positions,
                                    "active_positions_count": len(self.active_positions),
                                    "gross_pnl_usd": 0.0,
                                    "estimated_fee_usd": 0.0,
                                    "net_pnl_usd": 0.0
                                }
                        except Exception as e:
                            logger.error(f"[Engine] Futures hedge exception: {e}")
                            fut_status = f"Bybit Ex: {e}"
                            if mt5_conn and mt5_res.get("ticket"):
                                mt5_conn.close_position(mt5_res.get("ticket"))
                            self.auto_trade_enabled = False
                    else:
                        logger.warning(f"[Engine] Bybit order skipped: No API credentials set. Connect via '🔑 Connect API' on dashboard or in .env.")

                    new_pos = {
                        "id": f"PAIR-{self.trade_counter}",
                        "open_time": formatted_time,
                        "symbol_spot": mt5_symbol,
                        "symbol_fut": futures_tick.get("symbol"),
                        "entry_gap": round(spread_usd, 2),
                        "spot_price": mt5_mid,
                        "fut_price": futures_mid,
                        "trade_size": self.trade_size,
                        "status": f"OPEN ({mt5_status} | {fut_status})",
                        "signal": signal,
                        "ticket": mt5_res.get("ticket", 0),
                        "fut_order": fut_order_info
                    }
                    self.active_positions.append(new_pos)
                    auto_action_log = f"AUTO-FIRED OPEN ORDER: {new_pos['id']} | {mt5_status}"
                    logger.info(auto_action_log)
                else:
                    err_msg = mt5_res.get("error", "Unknown MT5 error")
                    auto_action_log = f"AUTO-TRADE REJECTED: {err_msg}"
                    action_detail = f"MT5 ERROR: {err_msg} (Check MT5 Algo Trading button)"
                    logger.error(f"[Engine] {auto_action_log}")

            elif should_fire_close and len(self.active_positions) > 0:
                closed_pos = self.active_positions.pop(0)
                mt5_symbol = mt5_tick.get("symbol", "ETHUSD")
                if mt5_conn:
                    mt5_conn.close_all_positions(symbol=mt5_symbol)
                if futures_conn and getattr(futures_conn, "api_key", None):
                    try:
                        futures_conn.close_all_positions(futures_tick.get("symbol"))
                    except Exception as e:
                        logger.error(f"[Engine] Futures close error: {e}")
                auto_action_log = f"AUTO-CLOSED ORDER: {closed_pos['id']} @ Cut Gap ${spread_usd:+.2f}"
                logger.info(auto_action_log)

        # Estimated PnL
        gross_pnl_usd = abs(spread_usd) * self.trade_size if is_opportunity else 0.0
        roundtrip_fee_usd = (mt5_mid + futures_mid) * (config.ESTIMATED_FEE_PCT / 100.0) * self.trade_size
        net_pnl_usd = gross_pnl_usd - roundtrip_fee_usd if is_opportunity else 0.0

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
            "signal": signal,
            "is_opportunity": is_opportunity,
            "action_detail": action_detail,
            "auto_action_log": auto_action_log,
            "active_positions": self.active_positions,
            "active_positions_count": len(self.active_positions),
            "gross_pnl_usd": round(gross_pnl_usd, 2),
            "estimated_fee_usd": round(roundtrip_fee_usd, 2),
            "net_pnl_usd": round(net_pnl_usd, 2)
        }

        self.history.append(result)
        if is_opportunity:
            self.signals_log.append(result)
        self._write_csv(result)

        return result

    def _write_csv(self, res: Dict[str, Any]):
        """Write item to CSV log."""
        try:
            mt5 = res.get("mt5", {})
            delta = res.get("delta", {})
            with open(self.csv_file, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    res["time_str"],
                    mt5.get("mid", 0), delta.get("mid", 0),
                    res["spread_usd"], res["spread_pct"], res["signal"],
                    bool(res.get("auto_action_log"))
                ])
        except Exception as e:
            pass

    def get_history_list(self) -> List[Dict[str, Any]]:
        return list(self.history)
