"""
MetaTrader 5 Price Connector
==============================
Fetches real-time ETH Spot price ticks from MetaTrader 5 (MT5).
"""

import time
import logging
from typing import Dict, Any, Optional
import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

class MT5Connector:
    def __init__(self, symbol: str = "ETHUSD"):
        self.symbol = symbol
        self.is_connected = False
        self.last_tick: Optional[Dict[str, Any]] = None

    def set_symbol(self, symbol: str):
        """Update target symbol and select in Market Watch."""
        self.symbol = symbol.upper()
        if self.is_connected:
            mt5.symbol_select(self.symbol, True)
        logger.info(f"[MT5] Symbol changed to {self.symbol}")

    def connect(self) -> bool:
        """Initialize connection to MetaTrader 5 terminal."""
        if not mt5.initialize():
            logger.error(f"[MT5] Initialization failed: {mt5.last_error()}")
            self.is_connected = False
            return False

        # Ensure symbol is selected in MarketWatch
        if not mt5.symbol_select(self.symbol, True):
            logger.warning(f"[MT5] Symbol '{self.symbol}' could not be selected in Market Watch.")
        
        self.is_connected = True
        logger.info(f"[MT5] Connected successfully to MT5 terminal for symbol {self.symbol}")
        return True

    def get_latest_tick(self) -> Optional[Dict[str, Any]]:
        """Fetch latest live price tick for the configured symbol."""
        if not self.is_connected:
            if not self.connect():
                return None

        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            # Try re-selecting symbol
            mt5.symbol_select(self.symbol, True)
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                logger.warning(f"[MT5] Failed to get tick for {self.symbol}")
                return None

        # Calculate mid price
        bid = float(tick.bid)
        ask = float(tick.ask)
        
        # Guard against zero bid/ask if tick isn't live yet
        if bid <= 0 or ask <= 0:
            info = mt5.symbol_info(self.symbol)
            if info:
                bid = info.bid if info.bid > 0 else bid
                ask = info.ask if info.ask > 0 else ask

        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask)

        tick_data = {
            "exchange": "MetaTrader 5 (Spot)",
            "symbol": self.symbol,
            "bid": bid,
            "ask": ask,
            "mid": round(mid, 2),
            "last": float(tick.last) if tick.last > 0 else mid,
            "volume": float(tick.volume_real if tick.volume_real > 0 else tick.volume),
            "timestamp": int(tick.time_msc) if tick.time_msc > 0 else int(time.time() * 1000)
        }
        
        self.last_tick = tick_data
        return tick_data

    def place_order(self, symbol: Optional[str] = None, order_type: str = "BUY", volume: float = 0.1, comment: str = "Arbitrage EA") -> Dict[str, Any]:
        """Send live market order (BUY/SELL) to MetaTrader 5 terminal."""
        target_symbol = (symbol or self.symbol).upper()
        if not self.is_connected and not self.connect():
            return {"success": False, "error": "MT5 not connected"}

        mt5.symbol_select(target_symbol, True)
        tick = mt5.symbol_info_tick(target_symbol)
        info = mt5.symbol_info(target_symbol)
        
        if not tick or not info:
            return {"success": False, "error": f"Symbol info unavailable for {target_symbol}"}

        # Determine price & order_type
        if order_type.upper() == "BUY":
            price = tick.ask
            action_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            action_type = mt5.ORDER_TYPE_SELL

        # Determine list of filling modes to try in order
        possible_filling_modes = []

        if hasattr(info, "filling_mode"):
            if info.filling_mode & mt5.ORDER_FILLING_FOK:
                possible_filling_modes.append(mt5.ORDER_FILLING_FOK)
            if info.filling_mode & mt5.ORDER_FILLING_IOC:
                possible_filling_modes.append(mt5.ORDER_FILLING_IOC)
            if info.filling_mode & mt5.ORDER_FILLING_RETURN:
                possible_filling_modes.append(mt5.ORDER_FILLING_RETURN)

        # Fallback list if bitmask is empty or failed
        for fallback in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]:
            if fallback not in possible_filling_modes:
                possible_filling_modes.append(fallback)

        last_error_res = None
        for fill_mode in possible_filling_modes:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": target_symbol,
                "volume": float(volume),
                "type": action_type,
                "price": price,
                "sl": 0.0,
                "tp": 0.0,
                "deviation": 20,
                "magic": 202609,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fill_mode,
            }

            logger.info(f"[MT5] Sending {order_type} order for {volume} lots of {target_symbol} @ {price} (fill_mode={fill_mode})")
            result = mt5.order_send(request)

            if result is None:
                err = f"MT5 order_send returned None: {mt5.last_error()}"
                logger.error(f"[MT5] {err}")
                last_error_res = {"success": False, "error": err}
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[MT5] Order SUCCESS: Ticket #{result.order}, deal #{result.deal}, price {result.price}")
                return {
                    "success": True,
                    "ticket": result.order,
                    "deal": result.deal,
                    "price": result.price,
                    "volume": result.volume,
                    "symbol": target_symbol,
                    "order_type": order_type.upper(),
                    "comment": result.comment
                }

            # If retcode == 10030 (Unsupported filling mode), try next fill mode!
            if result.retcode in [10030, getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)]:
                logger.warning(f"[MT5] Fill mode {fill_mode} unsupported (code=10030). Retrying next fill mode...")
                last_error_res = {"success": False, "error": f"MT5 order execution failed code={result.retcode} ({result.comment})", "retcode": result.retcode}
                continue
            else:
                err = f"MT5 order execution failed code={result.retcode} ({result.comment})"
                logger.error(f"[MT5] {err}")
                return {"success": False, "error": err, "retcode": result.retcode}

        return last_error_res or {"success": False, "error": "MT5 order failed all filling modes."}

    def close_all_positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Close all open positions in MT5 for specified or current symbol."""
        target_symbol = (symbol or self.symbol).upper()
        if not self.is_connected and not self.connect():
            return {"success": False, "error": "MT5 not connected", "closed_count": 0}

        positions = mt5.positions_get(symbol=target_symbol)
        if positions is None or len(positions) == 0:
            return {"success": True, "message": f"No open MT5 positions for {target_symbol}", "closed_count": 0}

        closed_count = 0
        errors = []

        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue

            # Opposite order type to close
            order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

            info = mt5.symbol_info(pos.symbol)
            possible_filling_modes = []
            if info and hasattr(info, "filling_mode"):
                if info.filling_mode & mt5.ORDER_FILLING_FOK:
                    possible_filling_modes.append(mt5.ORDER_FILLING_FOK)
                if info.filling_mode & mt5.ORDER_FILLING_IOC:
                    possible_filling_modes.append(mt5.ORDER_FILLING_IOC)
                if info.filling_mode & mt5.ORDER_FILLING_RETURN:
                    possible_filling_modes.append(mt5.ORDER_FILLING_RETURN)

            for fallback in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]:
                if fallback not in possible_filling_modes:
                    possible_filling_modes.append(fallback)

            success = False
            for fill_mode in possible_filling_modes:
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "volume": pos.volume,
                    "type": order_type,
                    "price": price,
                    "deviation": 20,
                    "magic": 202609,
                    "comment": f"Close #{pos.ticket}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": fill_mode,
                }

                res = mt5.order_send(request)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    closed_count += 1
                    logger.info(f"[MT5] Closed position #{pos.ticket} successfully.")
                    success = True
                    break
                elif res and res.retcode in [10030, getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)]:
                    logger.warning(f"[MT5 Close] Fill mode {fill_mode} unsupported (code=10030). Retrying next...")
                    continue
                else:
                    comment = res.comment if res else str(mt5.last_error())
                    err_msg = f"Failed to close position #{pos.ticket}: {comment}"
                    logger.error(f"[MT5] {err_msg}")
                    errors.append(err_msg)
                    break

        return {"success": len(errors) == 0, "closed_count": closed_count, "errors": errors}

    def close_position(self, ticket: int) -> Dict[str, Any]:
        """Close a specific open position in MT5 by ticket number."""
        if not self.is_connected and not self.connect():
            return {"success": False, "error": "MT5 not connected"}

        positions = mt5.positions_get(ticket=ticket)
        if positions is None or len(positions) == 0:
            return {"success": True, "message": f"Position #{ticket} already closed or not found"}

        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            return {"success": False, "error": f"Failed to get live tick for {pos.symbol}"}

        order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

        info = mt5.symbol_info(pos.symbol)
        possible_filling_modes = []
        if info and hasattr(info, "filling_mode"):
            if info.filling_mode & mt5.ORDER_FILLING_FOK:
                possible_filling_modes.append(mt5.ORDER_FILLING_FOK)
            if info.filling_mode & mt5.ORDER_FILLING_IOC:
                possible_filling_modes.append(mt5.ORDER_FILLING_IOC)
            if info.filling_mode & mt5.ORDER_FILLING_RETURN:
                possible_filling_modes.append(mt5.ORDER_FILLING_RETURN)

        for fallback in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]:
            if fallback not in possible_filling_modes:
                possible_filling_modes.append(fallback)

        for fill_mode in possible_filling_modes:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": pos.ticket,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": order_type,
                "price": price,
                "deviation": 20,
                "magic": 202609,
                "comment": f"Close #{pos.ticket}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fill_mode,
            }
            res = mt5.order_send(request)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[MT5] Successfully closed position #{ticket} (deal #{res.deal})")
                return {"success": True, "ticket": ticket, "deal": res.deal}
            if res and res.retcode in [10030, getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)]:
                continue
            return {"success": False, "error": f"Failed to close position #{ticket}: {res.comment if res else mt5.last_error()}"}

        return {"success": False, "error": f"Failed to close position #{ticket} across all filling modes"}

    def disconnect(self):
        """Disconnect MT5."""
        if self.is_connected:
            mt5.shutdown()
            self.is_connected = False
            logger.info("[MT5] Connection closed.")

