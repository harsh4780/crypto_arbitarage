"""
Delta Exchange Connector
========================
Fetches live ETH Futures / Perpetual prices from Delta Exchange API.
Supports HMAC-SHA256 authenticated REST calls for live trade execution when credentials are set.
"""

import time
import hmac
import hashlib
import json
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class DeltaConnector:
    def __init__(self, symbol: str = "ETHUSDT", base_url: str = "https://api.delta.exchange", 
                 api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self.symbol = symbol
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.last_tick: Optional[Dict[str, Any]] = None
        self.product_id: Optional[int] = None

    def _generate_signature(self, method: str, path: str, payload_str: str, timestamp: str) -> str:
        """Generate HMAC-SHA256 signature for Delta Exchange API authentication."""
        signature_data = method.upper() + timestamp + path + payload_str
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_data.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _get_auth_headers(self, method: str, path: str, payload_dict: Optional[Dict] = None) -> Dict[str, str]:
        """Construct authentication headers for Delta Exchange REST API."""
        timestamp = str(int(time.time()))
        payload_str = json.dumps(payload_dict) if payload_dict else ""
        signature = self._generate_signature(method, path, payload_str, timestamp)
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "Content-Type": "application/json",
            "User-Agent": "ETH-Arbitrage-Bot/1.0"
        }

    def set_credentials(self, api_key: str, api_secret: str):
        """Update API key and secret dynamically."""
        self.api_key = api_key
        self.api_secret = api_secret
        logger.info("[Delta] Credentials updated.")

    def get_latest_tick(self) -> Optional[Dict[str, Any]]:
        """Fetch latest market ticker for configured futures product (e.g. ETHUSDT)."""
        try:
            url = f"{self.base_url}/v2/tickers"
            response = requests.get(url, timeout=5)
            if response.status_code != 200:
                logger.error(f"[Delta] API HTTP error {response.status_code}: {response.text}")
                return None
            
            data = response.json()
            tickers: List[Dict[str, Any]] = data.get("result", [])
            
            # Find symbol match
            target = None
            for t in tickers:
                if t.get("symbol") == self.symbol:
                    target = t
                    break
            
            # Fallback search if exact symbol match not found
            if not target:
                base_coin = self.symbol.upper().replace("USDT", "").replace("USD", "")
                for t in tickers:
                    if base_coin in t.get("symbol", "").upper() and t.get("contract_type") == "perpetual_futures":
                        target = t
                        break

            if not target:
                logger.warning(f"[Delta] Could not find futures ticker for {self.symbol}")
                return None

            quotes = target.get("quotes", {})
            best_bid = float(quotes.get("best_bid") or target.get("close") or 0.0)
            best_ask = float(quotes.get("best_ask") or target.get("close") or 0.0)
            mark_price = float(target.get("mark_price") or 0.0)
            spot_price = float(target.get("spot_price") or 0.0)
            
            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else mark_price

            tick_data = {
                "exchange": "Delta Exchange (Futures)",
                "symbol": target.get("symbol", self.symbol),
                "contract_type": target.get("contract_type", "futures"),
                "bid": best_bid,
                "ask": best_ask,
                "mid": round(mid, 2),
                "mark_price": round(mark_price, 2),
                "spot_price_index": round(spot_price, 2),
                "last": float(target.get("close") or mid),
                "product_id": target.get("product_id"),
                "timestamp": int(time.time() * 1000)
            }

            self.last_tick = tick_data
            return tick_data

        except Exception as e:
            logger.error(f"[Delta] Error fetching ticker: {e}")
            return None

    def get_wallet_balances(self) -> Optional[Dict[str, Any]]:
        """Fetch account wallet balance (Requires API credentials)."""
        if not self.api_key or not self.api_secret:
            return {"status": "unauthenticated", "message": "API credentials missing."}

        path = "/v2/wallet/balances"
        headers = self._get_auth_headers("GET", path)
        try:
            res = requests.get(f"{self.base_url}{path}", headers=headers, timeout=5)
            if res.status_code == 200:
                return res.json()
            else:
                return {"status": "error", "code": res.status_code, "detail": res.text}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def place_order(self, symbol: str, size: int, side: str, order_type: str = "limit_order", price: Optional[float] = None) -> Dict[str, Any]:
        """Place live order on Delta Exchange (Requires API credentials)."""
        if not self.api_key or not self.api_secret:
            return {"success": False, "error": "API Key & Secret not configured yet."}

        path = "/v2/orders"
        payload = {
            "product_symbol": symbol,
            "size": size,
            "side": side.lower(), # "buy" or "sell"
            "order_type": order_type # "limit_order" or "market_order"
        }
        if price and order_type == "limit_order":
            payload["limit_price"] = str(price)

        headers = self._get_auth_headers("POST", path, payload)
        try:
            res = requests.post(f"{self.base_url}{path}", headers=headers, json=payload, timeout=5)
            data = res.json()
            if res.status_code in [200, 201]:
                return {"success": True, "result": data.get("result")}
            else:
                return {"success": False, "error": data.get("error", {}).get("message", res.text)}
        except Exception as e:
            return {"success": False, "error": str(e)}
