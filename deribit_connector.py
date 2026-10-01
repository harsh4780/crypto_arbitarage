"""
Deribit API v2 Connector
========================
Client connector for Deribit v2 API (JSON-RPC 2.0).
Supports fetching live Dated 3-Month Futures (e.g. BTC-25DEC26) & Perpetuals,
calculating Cash & Carry yield, and authenticated order execution.
"""

import time
import json
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class DeribitConnector:
    def __init__(self, currency: str = "BTC", testnet: bool = False,
                 client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.currency = currency.upper()
        self.base_url = "https://test.deribit.com/api/v2" if testnet else "https://www.deribit.com/api/v2"
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.access_token: Optional[str] = None
        self.token_expiry: float = 0.0

    def set_currency(self, currency: str):
        """Update active trading currency (BTC or ETH)."""
        self.currency = currency.upper()
        logger.info(f"[Deribit] Currency changed to {self.currency}")

    def set_credentials(self, client_id: str, client_secret: str):
        """Set or update Deribit API credentials."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = None
        logger.info("[Deribit] Credentials set.")

    def authenticate(self) -> bool:
        """Authenticate with Deribit API v2 using client_credentials."""
        if not self.client_id or not self.client_secret:
            logger.warning("[Deribit] Cannot authenticate: client_id or client_secret missing.")
            return False

        path = f"{self.base_url}/public/auth"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }

        try:
            res = requests.get(path, params=params, timeout=5)
            data = res.json()
            if "result" in data:
                result = data["result"]
                self.access_token = result.get("access_token")
                expires_in = result.get("expires_in", 900)
                self.token_expiry = time.time() + expires_in - 30
                logger.info("[Deribit] Authenticated successfully.")
                return True
            else:
                logger.error(f"[Deribit] Auth Error: {data.get('error')}")
                return False
        except Exception as e:
            logger.error(f"[Deribit] Connection error during auth: {e}")
            return False

    def _ensure_authenticated(self) -> bool:
        if self.access_token and time.time() < self.token_expiry:
            return True
        return self.authenticate()

    def get_dated_futures(self) -> List[Dict[str, Any]]:
        """Fetch all available dated futures instruments for configured currency."""
        path = f"{self.base_url}/public/get_instruments"
        params = {"currency": self.currency, "kind": "future"}
        try:
            res = requests.get(path, params=params, timeout=5)
            data = res.json()
            instruments = data.get("result", [])
            # Filter for dated futures (excluding perpetual)
            dated = [i for i in instruments if "PERPETUAL" not in i.get("instrument_name", "")]
            return dated
        except Exception as e:
            logger.error(f"[Deribit] Error fetching dated instruments: {e}")
            return []

    def get_latest_ticker(self, instrument_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch ticker book summary for a specific dated future or perpetual."""
        if not instrument_name:
            # Default to perpetual or next quarterly contract
            instruments = self.get_dated_futures()
            if instruments:
                instrument_name = instruments[-1].get("instrument_name") # furthest dated contract
            else:
                instrument_name = f"{self.currency}-PERPETUAL"

        path = f"{self.base_url}/public/get_book_summary_by_instrument"
        params = {"instrument_name": instrument_name}

        try:
            res = requests.get(path, params=params, timeout=5)
            data = res.json()
            result = data.get("result", [])
            if not result:
                return None

            item = result[0]
            best_bid = float(item.get("bid_price") or item.get("mark_price") or 0.0)
            best_ask = float(item.get("ask_price") or item.get("mark_price") or 0.0)
            mark_price = float(item.get("mark_price") or 0.0)
            underlying = float(item.get("underlying_price") or mark_price)
            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else mark_price

            premium_usd = mark_price - underlying
            premium_pct = (premium_usd / underlying * 100.0) if underlying > 0 else 0.0

            return {
                "exchange": "Deribit (Dated Futures)",
                "symbol": instrument_name,
                "currency": self.currency,
                "bid": round(best_bid, 2),
                "ask": round(best_ask, 2),
                "mid": round(mid, 2),
                "mark_price": round(mark_price, 2),
                "spot_index": round(underlying, 2),
                "premium_usd": round(premium_usd, 2),
                "premium_pct": round(premium_pct, 3),
                "timestamp": int(time.time() * 1000)
            }
        except Exception as e:
            logger.error(f"[Deribit] Error fetching ticker for {instrument_name}: {e}")
            return None

    def get_account_summary(self) -> Dict[str, Any]:
        """Fetch account summary balance (Requires Credentials)."""
        if not self._ensure_authenticated():
            return {"success": False, "error": "Unauthenticated. Check Client ID & Secret."}

        path = f"{self.base_url}/private/get_account_summary"
        params = {"currency": self.currency}
        headers = {"Authorization": f"Bearer {self.access_token}"}

        try:
            res = requests.get(path, params=params, headers=headers, timeout=5)
            data = res.json()
            if "result" in data:
                return {"success": True, "result": data["result"]}
            else:
                return {"success": False, "error": data.get("error")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def place_order(self, instrument_name: str, amount: float, side: str = "buy",
                    order_type: str = "limit", price: Optional[float] = None) -> Dict[str, Any]:
        """Place order on Deribit (Requires Credentials)."""
        if not self._ensure_authenticated():
            return {"success": False, "error": "Unauthenticated."}

        action = "buy" if side.lower() == "buy" else "sell"
        path = f"{self.base_url}/private/{action}"
        params = {
            "instrument_name": instrument_name,
            "amount": amount,
            "type": order_type
        }
        if price and order_type == "limit":
            params["price"] = price

        headers = {"Authorization": f"Bearer {self.access_token}"}

        try:
            res = requests.get(path, params=params, headers=headers, timeout=5)
            data = res.json()
            if "result" in data:
                return {"success": True, "result": data["result"]}
            else:
                return {"success": False, "error": data.get("error")}
        except Exception as e:
            return {"success": False, "error": str(e)}
