"""
Binance Futures & Demo Testnet Connector
========================================
Client connector for Binance USDⓈ-M and COIN-M Delivery Futures.
Supports:
- Live Price Fetching (e.g. ETHUSD_261225 COIN-M Quarterly, ETHUSDT Perpetual & Delivery)
- Bid/Ask Book Tickers and Mid-price calculation
- Real-time Account Balance & Position checks (Testnet & Mainnet)
- Authenticated HMAC-SHA256 Order Placement and Cancellation
"""

import time
import hmac
import hashlib
import logging
import threading
import json
import requests
import websocket
from typing import Dict, Any, Optional, List
from config import config

logger = logging.getLogger(__name__)

class BinanceConnector:
    def __init__(self, testnet: bool = True, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self.testnet = testnet if testnet is not None else config.BINANCE_TESTNET
        self.api_key = api_key or config.BINANCE_API_KEY or ""
        self.api_secret = api_secret or config.BINANCE_API_SECRET or ""
        
        self.recv_window = 10000
        self.time_offset_ms = 0
        
        # Real-time WebSocket cache & background workers
        self._ws_ticks: Dict[str, Dict[str, Any]] = {}
        self._ws_running = False
        
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        self._update_urls()
        self.sync_server_time()
        self._start_ws_threads()

    def _update_urls(self):
        if self.testnet:
            self.base_url = "https://testnet.binancefuture.com"
            self.fapi_url = "https://testnet.binancefuture.com/fapi/v1"
            self.dapi_url = "https://testnet.binancefuture.com/dapi/v1"
        else:
            self.base_url = "https://fapi.binance.com"
            self.fapi_url = "https://fapi.binance.com/fapi/v1"
            self.dapi_url = "https://dapi.binance.com/dapi/v1"

    def set_testnet(self, testnet: bool):
        """Switch between Testnet (Demo) and Mainnet."""
        self.testnet = testnet
        self._update_urls()
        self.sync_server_time()
        mode_str = "TESTNET (DEMO)" if self.testnet else "MAINNET (LIVE)"
        logger.info(f"[Binance] Switched to {mode_str} at {self.base_url}")

    def set_credentials(self, api_key: str, api_secret: str, testnet: Optional[bool] = None):
        """Set or update Binance API credentials dynamically."""
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        if testnet is not None:
            self.set_testnet(testnet)
        else:
            self.sync_server_time()
        logger.info(f"[Binance] Credentials updated (Testnet={self.testnet}).")

    def sync_server_time(self):
        """Synchronize local clock with Binance Futures server time to avoid timestamp offset errors."""
        try:
            url = f"{self.base_url}/fapi/v1/time"
            r = self.session.get(url, timeout=4)
            data = r.json()
            if "serverTime" in data:
                server_time_ms = int(data["serverTime"])
                local_time_ms = int(time.time() * 1000)
                self.time_offset_ms = server_time_ms - local_time_ms
                logger.info(f"[Binance] Server time synced. Offset: {self.time_offset_ms:+d}ms")
        except Exception as e:
            logger.warning(f"[Binance] Server time sync skipped: {e}")

    def _generate_signature(self, query_string: str) -> str:
        """Generate HMAC-SHA256 signature for Binance API authentication."""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _get_auth_headers(self) -> Dict[str, str]:
        """Headers required for authenticated Binance endpoints."""
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ETH-Arbitrage-Bot/1.0"
        }

        self._start_ws_threads()

    def _start_ws_threads(self):
        """Start background WebSocket streaming threads for ultra-low latency price updates (<10ms)."""
        if self._ws_running:
            return
        self._ws_running = True
        
        t_usdm = threading.Thread(target=self._run_ws_loop, args=("usdm",), daemon=True)
        t_usdm.start()
        
        t_coinm = threading.Thread(target=self._run_ws_loop, args=("coinm",), daemon=True)
        t_coinm.start()
        logger.info("[Binance] Live WebSocket stream threads initialized.")

    def _run_ws_loop(self, stream_type: str):
        """Background worker that continuously streams live bookTicker pushes from Binance."""
        while self._ws_running:
            try:
                if stream_type == "usdm":
                    base_ws = "wss://stream.binancefuture.com" if self.testnet else "wss://fstream.binance.com"
                    streams = "ethusdt_261225@bookTicker/ethusdt@bookTicker/btcusdt_261225@bookTicker/btcusdt@bookTicker"
                else:
                    base_ws = "wss://dstream.binancefuture.com" if self.testnet else "wss://dstream.binance.com"
                    streams = "ethusd_261225@bookTicker/ethusd_perp@bookTicker/btcusd_261225@bookTicker/btcusd_perp@bookTicker"

                url = f"{base_ws}/stream?streams={streams}"

                def on_msg(ws, message):
                    try:
                        msg_json = json.loads(message)
                        data = msg_json.get("data", msg_json)
                        if data and "s" in data and "b" in data and "a" in data:
                            sym = data["s"].upper()
                            bid = float(data["b"])
                            ask = float(data["a"])
                            mid = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else 0.0
                            self._ws_ticks[sym] = {
                                "exchange": "Binance USD-M Futures (WebSocket)" if stream_type == "usdm" else "Binance COIN-M Futures (WebSocket)",
                                "symbol": sym,
                                "contract_type": "quarterly_delivery" if "_" in sym else "perpetual",
                                "bid": bid,
                                "ask": ask,
                                "mid": mid,
                                "last": mid,
                                "timestamp": int(data.get("T", time.time() * 1000)),
                                "timestamp_sec": time.time(),
                                "is_ws": True
                            }
                    except Exception as e:
                        logger.debug(f"[Binance WS {stream_type}] Parse error: {e}")

                def on_err(ws, err):
                    logger.debug(f"[Binance WS {stream_type}] Connection notice: {err}")

                def on_close(ws, close_status_code, close_msg):
                    logger.debug(f"[Binance WS {stream_type}] Closed: {close_msg}")

                ws = websocket.WebSocketApp(
                    url,
                    on_message=on_msg,
                    on_error=on_err,
                    on_close=on_close
                )
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.debug(f"[Binance WS {stream_type}] Loop error: {e}")

            time.sleep(2) # Reconnect delay

    # ==========================================
    # MARKET DATA (WebSocket Primary + REST Fallback)
    # ==========================================

    def get_coinm_price(self, symbol: str = "ETHUSD_261225") -> Optional[float]:
        """Fetch COIN-M Futures Price."""
        sym = symbol.upper()
        cached = self._ws_ticks.get(sym)
        if cached and (time.time() - cached.get("timestamp_sec", 0)) < 2.5:
            return cached["last"]

        try:
            url = f"{self.base_url}/dapi/v1/ticker/price?symbol={sym}"
            res = self.session.get(url, timeout=4).json()
            if isinstance(res, list) and len(res) > 0 and "price" in res[0]:
                return float(res[0]["price"])
            elif isinstance(res, dict) and "price" in res:
                return float(res["price"])
            return None
        except Exception as e:
            logger.error(f"[Binance] Error fetching COIN-M price for {sym}: {e}")
            return None

    def get_coinm_tick(self, symbol: str = "ETHUSD_261225") -> Optional[Dict[str, Any]]:
        """
        Fetch real-time ticker for COIN-M Futures.
        Primary: Sub-millisecond WebSocket in-memory cache.
        Fallback: REST API.
        """
        sym = symbol.upper()
        # 1. Fast path: Ultra-low latency WebSocket push
        cached = self._ws_ticks.get(sym)
        if cached and (time.time() - cached.get("timestamp_sec", 0)) < 2.5:
            return cached

        # 2. Fallback: REST API
        try:
            book_url = f"{self.base_url}/dapi/v1/ticker/bookTicker?symbol={sym}"
            book_res = self.session.get(book_url, timeout=4).json()

            book_item = book_res[0] if isinstance(book_res, list) and len(book_res) > 0 else book_res
            bid = float(book_item.get("bidPrice", 0.0))
            ask = float(book_item.get("askPrice", 0.0))
            last_price = self.get_coinm_price(sym) or 0.0
            mid = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else last_price

            tick_data = {
                "exchange": "Binance COIN-M Futures (REST)",
                "symbol": sym,
                "contract_type": "quarterly_delivery",
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": last_price,
                "timestamp": int(time.time() * 1000),
                "is_ws": False
            }
            self._ws_ticks[sym] = {**tick_data, "timestamp_sec": time.time()}
            return tick_data
        except Exception as e:
            logger.error(f"[Binance] Error fetching COIN-M tick for {sym}: {e}")
            return None

    def get_usdm_price(self, symbol: str = "ETHUSDT") -> Optional[float]:
        """Fetch USD-M Futures Price."""
        sym = symbol.upper()
        cached = self._ws_ticks.get(sym)
        if cached and (time.time() - cached.get("timestamp_sec", 0)) < 2.5:
            return cached["last"]

        try:
            url = f"{self.base_url}/fapi/v1/ticker/price?symbol={sym}"
            res = self.session.get(url, timeout=4).json()
            if "price" in res:
                return float(res["price"])
            return None
        except Exception as e:
            logger.error(f"[Binance] Error fetching USD-M price for {sym}: {e}")
            return None

    def get_usdm_tick(self, symbol: str = "ETHUSDT") -> Optional[Dict[str, Any]]:
        """
        Fetch real-time ticker for USD-M Futures.
        Primary: Sub-millisecond WebSocket in-memory cache (<10ms latency).
        Fallback: REST API.
        """
        sym = symbol.upper()
        # 1. Fast path: Ultra-low latency WebSocket push
        cached = self._ws_ticks.get(sym)
        if cached and (time.time() - cached.get("timestamp_sec", 0)) < 2.5:
            return cached

        # 2. Fallback: REST API
        try:
            book_url = f"{self.base_url}/fapi/v1/ticker/bookTicker?symbol={sym}"
            book = self.session.get(book_url, timeout=4).json()
            
            bid = float(book.get("bidPrice", 0.0))
            ask = float(book.get("askPrice", 0.0))
            last_price = self.get_usdm_price(sym) or 0.0
            mid = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else last_price

            tick_data = {
                "exchange": "Binance USD-M Futures (REST)",
                "symbol": sym,
                "contract_type": "perpetual" if "_" not in sym else "quarterly_delivery",
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": last_price,
                "timestamp": int(time.time() * 1000),
                "is_ws": False
            }
            self._ws_ticks[sym] = {**tick_data, "timestamp_sec": time.time()}
            return tick_data
        except Exception as e:
            logger.error(f"[Binance] Error fetching USD-M tick for {sym}: {e}")
            return None

    # ==========================================
    # ACCOUNT & BALANCES (Authenticated)
    # ==========================================

    def get_account_balance(self) -> Dict[str, Any]:
        """
        Fetch Wallet Balances for both USD-M and COIN-M Futures accounts.
        """
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "API credentials missing."}

        timestamp = int(time.time() * 1000) + self.time_offset_ms
        query = f"recvWindow={self.recv_window}&timestamp={timestamp}"
        signature = self._generate_signature(query)
        headers = self._get_auth_headers()

        balances = {}
        # 1. USD-M Balance
        try:
            url_fapi = f"{self.base_url}/fapi/v2/account?{query}&signature={signature}"
            res_fapi = self.session.get(url_fapi, headers=headers, timeout=5).json()
            if "totalWalletBalance" in res_fapi:
                balances["usdm"] = {
                    "totalWalletBalance": float(res_fapi["totalWalletBalance"]),
                    "totalUnrealizedProfit": float(res_fapi.get("totalUnrealizedProfit", 0.0)),
                    "availableBalance": float(res_fapi.get("availableBalance", 0.0)),
                    "assets": [a for a in res_fapi.get("assets", []) if float(a.get("walletBalance", 0)) > 0]
                }
            else:
                balances["usdm_error"] = res_fapi
        except Exception as e:
            balances["usdm_error"] = str(e)

        # 2. COIN-M Balance
        try:
            timestamp = int(time.time() * 1000) + self.time_offset_ms
            query_dapi = f"recvWindow={self.recv_window}&timestamp={timestamp}"
            sig_dapi = self._generate_signature(query_dapi)
            url_dapi = f"{self.base_url}/dapi/v1/account?{query_dapi}&signature={sig_dapi}"
            res_dapi = self.session.get(url_dapi, headers=headers, timeout=5).json()
            if "assets" in res_dapi:
                balances["coinm"] = {
                    "assets": [a for a in res_dapi.get("assets", []) if float(a.get("walletBalance", 0)) > 0],
                    "positions": [p for p in res_dapi.get("positions", []) if float(p.get("positionAmt", 0)) != 0]
                }
            else:
                balances["coinm_error"] = res_dapi
        except Exception as e:
            balances["coinm_error"] = str(e)

        return {"success": True, "balances": balances}

    # ==========================================
    # ORDER EXECUTION (Authenticated)
    # ==========================================

    def place_coinm_order(self, symbol: str, side: str, quantity: int,
                          order_type: str = "LIMIT", price: Optional[float] = None) -> Dict[str, Any]:
        """
        Place an order on Binance COIN-M Futures (contracts quantity).
        :param symbol: Contract symbol (e.g. 'ETHUSD_261225')
        :param side: 'BUY' or 'SELL'
        :param quantity: Number of contracts (1 contract = $10 for ETH)
        :param order_type: 'LIMIT' or 'MARKET'
        :param price: Required for LIMIT orders
        """
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "API credentials missing."}

        timestamp = int(time.time() * 1000) + self.time_offset_ms
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
            "recvWindow": self.recv_window,
            "timestamp": timestamp
        }

        if order_type.upper() == "LIMIT":
            if not price:
                return {"success": False, "error": "Price is required for LIMIT order."}
            params["price"] = str(price)
            params["timeInForce"] = "GTC"

        query_str = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = self._generate_signature(query_str)

        try:
            url = f"{self.base_url}/dapi/v1/order"
            res = self.session.post(url, headers=self._get_auth_headers(), data=params, timeout=5)
            data = res.json()
            if res.status_code == 200 and "orderId" in data:
                return {"success": True, "order": data}
            return {"success": False, "error": data.get("msg", str(data))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def place_usdm_order(self, symbol: str, side: str, quantity: float,
                         order_type: str = "LIMIT", price: Optional[float] = None) -> Dict[str, Any]:
        """
        Place an order on Binance USD-M Futures.
        :param symbol: Contract symbol (e.g. 'ETHUSDT' or 'ETHUSDT_261225')
        :param side: 'BUY' or 'SELL'
        :param quantity: Quantity in ETH
        :param order_type: 'LIMIT' or 'MARKET'
        :param price: Required for LIMIT orders
        """
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "API credentials missing."}

        timestamp = int(time.time() * 1000) + self.time_offset_ms
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
            "recvWindow": self.recv_window,
            "timestamp": timestamp
        }

        if order_type.upper() == "LIMIT":
            if not price:
                return {"success": False, "error": "Price is required for LIMIT order."}
            params["price"] = str(price)
            params["timeInForce"] = "GTC"

        query_str = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = self._generate_signature(query_str)

        try:
            url = f"{self.base_url}/fapi/v1/order"
            res = self.session.post(url, headers=self._get_auth_headers(), data=params, timeout=5)
            data = res.json()
            if res.status_code == 200 and "orderId" in data:
                return {"success": True, "order": data, "order_id": data.get("orderId")}
            return {"success": False, "error": data.get("msg", str(data))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "Market", price: Optional[float] = None) -> Dict[str, Any]:
        """
        Unified order placement method compatible with ArbitrageEngine.
        Handles both COIN-M Delivery contracts (e.g. ETHUSD_261225) and USD-M Perpetual/Delivery contracts.
        """
        symbol_upper = symbol.upper()
        side_upper = side.upper()
        norm_type = "MARKET" if order_type.upper() == "MARKET" else "LIMIT"

        # Check if COIN-M Delivery (e.g. ETHUSD_261225)
        if "_" in symbol_upper and "USDT" not in symbol_upper:
            # 1 contract = $10 for ETH, $100 for BTC
            price_est = price or self.get_coinm_price(symbol_upper) or (2700.0 if "ETH" in symbol_upper else 85000.0)
            contract_mult = 10 if "ETH" in symbol_upper else 100
            contracts = max(1, int(round((qty * price_est) / contract_mult)))
            res = self.place_coinm_order(symbol=symbol_upper, side=side_upper, quantity=contracts, order_type=norm_type, price=price)
            if res.get("success"):
                res["order_id"] = res.get("order", {}).get("orderId")
            return res
        else:
            res = self.place_usdm_order(symbol=symbol_upper, side=side_upper, quantity=qty, order_type=norm_type, price=price)
            if res.get("success"):
                res["order_id"] = res.get("order", {}).get("orderId")
            return res

    def close_all_positions(self, symbol: str = "") -> Dict[str, Any]:
        """
        Close any active open position for the specified symbol or all symbols.
        """
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "API credentials missing."}

        closed_count = 0
        timestamp = int(time.time() * 1000) + self.time_offset_ms
        query = f"recvWindow={self.recv_window}&timestamp={timestamp}"
        signature = self._generate_signature(query)
        headers = self._get_auth_headers()

        # Check COIN-M positions
        try:
            url_coin = f"{self.base_url}/dapi/v1/positionRisk?{query}&signature={signature}"
            positions = self.session.get(url_coin, headers=headers, timeout=5).json()
            if isinstance(positions, list):
                for p in positions:
                    amt = float(p.get("positionAmt", 0.0))
                    p_sym = p.get("symbol")
                    if amt != 0 and (not symbol or symbol.upper() == p_sym):
                        close_side = "SELL" if amt > 0 else "BUY"
                        qty = abs(int(amt))
                        self.place_coinm_order(symbol=p_sym, side=close_side, quantity=qty, order_type="MARKET")
                        closed_count += 1
                        logger.info(f"[Binance] Closed COIN-M position {p_sym} {close_side} {qty} contracts")
        except Exception as e:
            logger.error(f"[Binance] Error closing COIN-M positions: {e}")

        # Check USD-M positions
        try:
            timestamp = int(time.time() * 1000) + self.time_offset_ms
            query = f"recvWindow={self.recv_window}&timestamp={timestamp}"
            sig_fapi = self._generate_signature(query)
            url_usdm = f"{self.base_url}/fapi/v2/positionRisk?{query}&signature={sig_fapi}"
            usdm_pos = self.session.get(url_usdm, headers=headers, timeout=5).json()
            if isinstance(usdm_pos, list):
                for p in usdm_pos:
                    amt = float(p.get("positionAmt", 0.0))
                    p_sym = p.get("symbol")
                    if amt != 0 and (not symbol or symbol.upper() == p_sym):
                        close_side = "SELL" if amt > 0 else "BUY"
                        qty = abs(amt)
                        self.place_usdm_order(symbol=p_sym, side=close_side, quantity=qty, order_type="MARKET")
                        closed_count += 1
                        logger.info(f"[Binance] Closed USD-M position {p_sym} {close_side} {qty}")
        except Exception as e:
            logger.error(f"[Binance] Error closing USD-M positions: {e}")

        return {"success": True, "closed_count": closed_count}



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 65)
    print("      BINANCE FUTURES CONNECTOR SELF-TEST")
    print("=" * 65)

    conn = BinanceConnector()

    # 1. Test COIN-M Quarterly 1225
    coin_symbol = config.BINANCE_COIN_CONTRACT
    coin_tick = conn.get_coinm_tick(coin_symbol)
    if coin_tick:
        print(f"\n[OK] COIN-M Quarterly ({coin_symbol}):")
        print(f"     Last: ${coin_tick['last']:.2f} | Bid: ${coin_tick['bid']:.2f} | Ask: ${coin_tick['ask']:.2f} | Mid: ${coin_tick['mid']:.2f}")
    else:
        print(f"\n[WARN] Failed to fetch COIN-M tick for {coin_symbol}")

    # 2. Test USD-M Perpetual
    usdt_tick = conn.get_usdm_tick("ETHUSDT")
    if usdt_tick:
        print(f"\n[OK] USD-M Perpetual (ETHUSDT):")
        print(f"     Last: ${usdt_tick['last']:.2f} | Bid: ${usdt_tick['bid']:.2f} | Ask: ${usdt_tick['ask']:.2f} | Mid: ${usdt_tick['mid']:.2f}")

    # 3. Test Account Balances
    bal = conn.get_account_balance()
    if bal.get("success"):
        usdm_bal = bal.get("balances", {}).get("usdm", {})
        print(f"\n[OK] Wallet Total Balance (USD-M): ${usdm_bal.get('totalWalletBalance', 0.0):.2f} USDT")
    else:
        print(f"\n[INFO] Balances check: {bal}")
    print("=" * 65)
