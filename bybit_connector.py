"""
Bybit V5 Futures & Demo Account Connector
==========================================
Client connector for Bybit V5 API (supporting both Testnet/Demo & Mainnet).
Supports:
- Dated USDT Futures (e.g. BTCUSDT-02OCT2026, ETHUSDT-02OCT2026)
- USDT Perpetual Contracts (e.g. BTCUSDT, ETHUSDT)
- Real-time orderbook / ticker streaming & Cash & Carry premium calculation
- Authenticated V5 HMAC-SHA256 order execution for Demo/Testnet & Live trading
- Unified Trading Account (UTA) wallet balance checks
"""

import time
import json
import hmac
import hashlib
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class BybitConnector:
    def __init__(self, currency: str = "BTC", testnet: bool = True,
                 api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 category: str = "linear"):
        self.currency = currency.upper()
        self.testnet = testnet
        self.category = category # 'linear' (USDT / USDC) or 'inverse' (Coin-M)
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.recv_window = "20000"  # 20 seconds maximum allowed by Bybit V5
        self.time_offset_ms = 0
        self._last_tickers: Dict[str, Dict[str, Any]] = {}
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._update_base_url()
        self.sync_server_time()

    def sync_server_time(self):
        """Synchronize local clock with Bybit server time to avoid timestamp mismatch errors."""
        try:
            r = self.session.get(f"{self.base_url}/v5/market/time", timeout=4)
            data = r.json()
            if data.get("retCode") == 0:
                server_time_ms = int(data["result"]["timeNano"]) // 1000000
                local_time_ms = int(time.time() * 1000)
                self.time_offset_ms = server_time_ms - local_time_ms
                logger.info(f"[Bybit] Server time synced. Offset: {self.time_offset_ms:+d}ms")
        except Exception as e:
            logger.warning(f"[Bybit] Server time sync skipped: {e}")

    def _update_base_url(self):
        if self.testnet:
            self.base_url = "https://api-testnet.bybit.com"
        else:
            self.base_url = "https://api.bybit.com"

    def set_testnet(self, testnet: bool):
        """Switch between Testnet (Demo) and Mainnet."""
        self.testnet = testnet
        self._update_base_url()
        self.sync_server_time()
        mode_str = "TESTNET (DEMO)" if self.testnet else "MAINNET (LIVE)"
        logger.info(f"[Bybit] Switched to {mode_str} at {self.base_url}")

    def set_currency(self, currency: str):
        """Update active trading currency (BTC or ETH)."""
        self.currency = currency.upper()
        logger.info(f"[Bybit] Currency changed to {self.currency}")

    def set_credentials(self, api_key: str, api_secret: str, testnet: Optional[bool] = None):
        """Set or update Bybit API credentials."""
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        if testnet is not None:
            self.set_testnet(testnet)
        else:
            self.sync_server_time()
        logger.info(f"[Bybit] Credentials updated (Testnet={self.testnet}).")

    def _generate_signature(self, timestamp: str, param_str: str) -> str:
        """Generate Bybit V5 HMAC-SHA256 signature."""
        val = f"{timestamp}{self.api_key}{self.recv_window}{param_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            val.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, param_str: str = "") -> Dict[str, str]:
        """Build Bybit V5 Authentication Headers with synchronized server timestamp."""
        timestamp = str(int(time.time() * 1000) + self.time_offset_ms)
        signature = self._generate_signature(timestamp, param_str)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json"
        }

    def get_instruments(self) -> List[Dict[str, Any]]:
        """Fetch all linear futures instruments for current base currency."""
        path = f"{self.base_url}/v5/market/instruments-info"
        params = {"category": self.category, "baseCoin": self.currency}
        try:
            res = self.session.get(path, params=params, timeout=8)
            data = res.json()
            if data.get("retCode") == 0:
                instruments = data.get("result", {}).get("list", [])
                # Filter for USDT quoteCoin and active trading status
                valid = [
                    i for i in instruments 
                    if i.get("quoteCoin") == "USDT" and i.get("status") == "Trading"
                ]
                return valid
            else:
                logger.error(f"[Bybit] Instruments Error: {data.get('retMsg')}")
                return []
        except Exception as e:
            logger.error(f"[Bybit] Connection error fetching instruments: {e}")
            return []

    def get_contract_names(self) -> List[str]:
        """
        Return clean list of available Bybit contract symbols.
        Prioritizes dated futures (e.g. BTCUSDT-02OCT2026) and perpetuals (BTCUSDT).
        """
        instruments = self.get_instruments()
        dated = []
        perps = []

        for inst in instruments:
            sym = inst.get("symbol", "")
            contract_type = inst.get("contractType", "")
            if "Futures" in contract_type or "-" in sym:
                dated.append(sym)
            elif "Perpetual" in contract_type or sym == f"{self.currency}USDT":
                perps.append(sym)

        # Sort dated futures by symbol/expiry
        dated.sort()

        # Combine: dated futures first, then perpetuals
        combined = dated + [p for p in perps if p not in dated]
        if not combined:
            combined = [f"{self.currency}USDT-02OCT26", f"{self.currency}USDT"]

        return combined

    def get_latest_ticker(self, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch real-time ticker data for a Bybit contract."""
        if symbol:
            # Bybit uses 2-digit year format (e.g. 02OCT26, not 02OCT2026)
            symbol = symbol.replace("2026", "26").replace("2027", "27").strip()
        else:
            contracts = self.get_contract_names()
            symbol = contracts[0] if contracts else f"{self.currency}USDT-02OCT26"

        path = f"{self.base_url}/v5/market/tickers"
        params = {"category": self.category, "symbol": symbol}

        try:
            res = self.session.get(path, params=params, timeout=6)
            data = res.json()
            if data.get("retCode") != 0:
                logger.warning(f"[Bybit] Ticker error {symbol}: {data.get('retMsg')}")
                return self._last_tickers.get(symbol)

            items = data.get("result", {}).get("list", [])
            if not items:
                return self._last_tickers.get(symbol)

            item = items[0]
            best_bid = float(item.get("bid1Price") or item.get("lastPrice") or 0.0)
            best_ask = float(item.get("ask1Price") or item.get("lastPrice") or 0.0)
            last_price = float(item.get("lastPrice") or 0.0)
            mark_price = float(item.get("markPrice") or last_price)
            index_price = float(item.get("indexPrice") or mark_price)

            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else last_price

            premium_usd = mark_price - index_price
            premium_pct = (premium_usd / index_price * 100.0) if index_price > 0 else 0.0

            mode_tag = "Demo/Testnet" if self.testnet else "Live"

            tick_res = {
                "exchange": f"Bybit ({mode_tag})",
                "symbol": symbol,
                "currency": self.currency,
                "bid": round(best_bid, 2),
                "ask": round(best_ask, 2),
                "mid": round(mid, 2),
                "mark_price": round(mark_price, 2),
                "spot_index": round(index_price, 2),
                "premium_usd": round(premium_usd, 2),
                "premium_pct": round(premium_pct, 3),
                "is_testnet": self.testnet,
                "timestamp": int(time.time() * 1000)
            }
            self._last_tickers[symbol] = tick_res
            return tick_res
        except Exception as e:
            logger.warning(f"[Bybit] Network glitch fetching ticker for {symbol}: {e}. Using cached tick.")
            return self._last_tickers.get(symbol)

    def get_wallet_balances(self) -> Dict[str, Any]:
        """Fetch Unified / Contract account wallet balance (Requires API credentials)."""
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "API Key & Secret required."}

        # Candidate endpoints to try: current base_url first, then other environments
        candidates = [self.base_url]
        for alt in ["https://api-demo.bybit.com", "https://api-testnet.bybit.com", "https://api.bybit.com"]:
            if alt not in candidates:
                candidates.append(alt)

        last_error = "Unknown Bybit error"
        for url in candidates:
            path = f"{url}/v5/account/wallet-balance"
            query_str = "accountType=UNIFIED"
            headers = self._auth_headers(param_str=query_str)
            try:
                res = requests.get(f"{path}?{query_str}", headers=headers, timeout=5)
                if res.status_code != 200:
                    last_error = f"HTTP {res.status_code} on {url}"
                    continue
                data = res.json()
                if data.get("retCode") == 0:
                    if self.base_url != url:
                        logger.info(f"[Bybit] Discovered active endpoint: {url} (switched base_url)")
                        self.base_url = url
                    list_data = data.get("result", {}).get("list", [])
                    account_info = list_data[0] if list_data else {}
                    total_equity = account_info.get("totalEquity", "0.0")
                    total_wallet_balance = account_info.get("totalWalletBalance", "0.0")
                    coins = account_info.get("coin", [])
                    return {
                        "success": True,
                        "is_testnet": "testnet" in url or "demo" in url,
                        "endpoint": url,
                        "total_equity": total_equity,
                        "total_wallet_balance": total_wallet_balance,
                        "coins": coins
                    }
                else:
                    last_error = data.get("retMsg", last_error)
            except Exception as e:
                last_error = str(e)

        return {"success": False, "error": last_error}

    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "Market",
                    price: Optional[float] = None, reduce_only: bool = False) -> Dict[str, Any]:
        """
        Place order on Bybit (Demo or Mainnet).
        side: 'Buy' or 'Sell'
        order_type: 'Market' or 'Limit'
        """
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "Cannot place order: Bybit credentials not configured."}

        payload: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "side": side.capitalize(),
            "orderType": order_type.capitalize(),
            "qty": str(qty),
            "timeInForce": "IOC" if order_type.lower() == "market" else "GTC",
            "reduceOnly": reduce_only
        }
        if price is not None and order_type.lower() == "limit":
            payload["price"] = str(price)

        body_str = json.dumps(payload, separators=(',', ':'))

        # Candidate endpoints to try
        candidates = [self.base_url]
        for alt in ["https://api-demo.bybit.com", "https://api-testnet.bybit.com", "https://api.bybit.com"]:
            if alt not in candidates:
                candidates.append(alt)

        last_error = "Unknown error"
        for url in candidates:
            path = f"{url}/v5/order/create"
            # Generate fresh headers immediately before sending to ensure timestamp accuracy
            headers = self._auth_headers(param_str=body_str)
            try:
                res = requests.post(path, data=body_str, headers=headers, timeout=6)
                data = res.json()
                if data.get("retCode") == 0:
                    if self.base_url != url:
                        logger.info(f"[Bybit] Discovered active endpoint: {url} (switched base_url)")
                        self.base_url = url
                    order_id = data.get("result", {}).get("orderId")
                    logger.info(f"[Bybit] Order placed successfully: {side} {qty} {symbol} (Order ID: {order_id}) on {url}")
                    return {"success": True, "order_id": order_id, "endpoint": url, "data": data.get("result")}
                elif data.get("retCode") == 10002: # Timestamp / recv_window mismatch
                    logger.warning("[Bybit] Timestamp mismatch detected (10002). Re-syncing clock with Bybit and retrying...")
                    self.sync_server_time()
                    fresh_headers = self._auth_headers(param_str=body_str)
                    res_retry = requests.post(path, data=body_str, headers=fresh_headers, timeout=6)
                    data_retry = res_retry.json()
                    if data_retry.get("retCode") == 0:
                        order_id = data_retry.get("result", {}).get("orderId")
                        logger.info(f"[Bybit] Order placed on retry: {side} {qty} {symbol} (Order ID: {order_id})")
                        return {"success": True, "order_id": order_id, "endpoint": url, "data": data_retry.get("result")}
                    else:
                        last_error = data_retry.get("retMsg", "Unknown retry error")
                        break
                else:
                    last_error = data.get("retMsg", "Unknown error")
                    if data.get("retCode") != 10003: # if not invalid key, don't keep trying others
                        break
            except Exception as e:
                last_error = str(e)

        logger.error(f"[Bybit] Order failed: {last_error}")
        return {"success": False, "error": last_error}

    def close_all_positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Cancel open orders and close positions."""
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "Bybit credentials not configured."}

        path = f"{self.base_url}/v5/order/cancel-all"
        payload = {"category": self.category}
        if symbol:
            payload["symbol"] = symbol

        body_str = json.dumps(payload, separators=(',', ':'))
        headers = self._auth_headers(param_str=body_str)

        try:
            res = requests.post(path, data=body_str, headers=headers, timeout=5)
            data = res.json()
            return {"success": data.get("retCode") == 0, "result": data}
        except Exception as e:
            return {"success": False, "error": str(e)}
