"""
Arbitrage System Configuration
================================
Centralized configuration for MetaTrader 5, Delta Exchange, and Deribit.
"""

import os
from dataclasses import dataclass
from typing import Optional

def load_dotenv_simple(filepath: str = ".env"):
    """Simple parser for .env files without requiring external packages."""
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

# Load env variables on module import
load_dotenv_simple()

@dataclass
class Config:
    # MetaTrader 5 Settings
    MT5_SYMBOL: str = os.getenv("MT5_SYMBOL", "BTCUSD")
    MT5_PATH: Optional[str] = os.getenv("MT5_PATH", "")
    MT5_RETRY_INTERVAL: float = 1.0
    
    # Delta Exchange Settings
    DELTA_SYMBOL: str = os.getenv("DELTA_SYMBOL", "BTCUSD")
    DELTA_BASE_URL: str = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
    DELTA_WS_URL: str = os.getenv("DELTA_WS_URL", "wss://socket.india.delta.exchange")
    DELTA_API_KEY: Optional[str] = os.getenv("DELTA_API_KEY", "gNc6EV3kHDZ1E2rFzwq7Z98JMU34W5")
    DELTA_API_SECRET: Optional[str] = os.getenv("DELTA_API_SECRET", "iql86ce5I4R93nce5JIdChLzCvpC8VYD4Yswdwizl9B0e5griXxELQF0QhYQ")
    
    # Deribit Settings
    DERIBIT_CLIENT_ID: Optional[str] = os.getenv("DERIBIT_CLIENT_ID", "ylpXJHz_")
    DERIBIT_CLIENT_SECRET: Optional[str] = os.getenv("DERIBIT_CLIENT_SECRET", "jcgIBLUUIywvxrN19t6TCPDoL_iguhFP0LgA1u4trhk")
    DERIBIT_CURRENCY: str = os.getenv("DERIBIT_CURRENCY", "BTC")
    DERIBIT_CONTRACT: Optional[str] = os.getenv("DERIBIT_CONTRACT", "") # e.g. BTC-27NOV26 (1-Month), BTC-25DEC26 (3-Month)

    # Bybit Settings (Demo / Testnet & Mainnet Futures)
    BYBIT_API_KEY: Optional[str] = os.getenv("BYBIT_API_KEY", "")
    BYBIT_API_SECRET: Optional[str] = os.getenv("BYBIT_API_SECRET", "")
    BYBIT_TESTNET: bool = os.getenv("BYBIT_TESTNET", "true").lower() in ("true", "1", "yes")
    BYBIT_CONTRACT: Optional[str] = os.getenv("BYBIT_CONTRACT", "BTCUSDT-02OCT26")
    BYBIT_CATEGORY: str = os.getenv("BYBIT_CATEGORY", "linear")

    # Binance Futures Settings (Demo / Testnet & Mainnet)
    BINANCE_API_KEY: Optional[str] = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET: Optional[str] = os.getenv("BINANCE_API_SECRET", "")
    BINANCE_TESTNET: bool = os.getenv("BINANCE_TESTNET", "true").lower() in ("true", "1", "yes")
    BINANCE_COIN_CONTRACT: str = os.getenv("BINANCE_COIN_CONTRACT", "ETHUSD_261225")
    BINANCE_USDT_CONTRACT: str = os.getenv("BINANCE_USDT_CONTRACT", "ETHUSDT_261225")
    BINANCE_BASE_URL: str = os.getenv("BINANCE_BASE_URL", "https://testnet.binancefuture.com")

    # Arbitrage Parameters
    MIN_SPREAD_USD: float = float(os.getenv("MIN_SPREAD_USD", "3.0"))  # Minimum spread in USD to trigger opportunity
    MIN_SPREAD_PCT: float = float(os.getenv("MIN_SPREAD_PCT", "0.15")) # Minimum spread percentage (0.15%)
    ESTIMATED_FEE_PCT: float = float(os.getenv("ESTIMATED_FEE_PCT", "0.08")) # Estimated round-trip taker fees %
    DEFAULT_TRADE_SIZE_ETH: float = float(os.getenv("DEFAULT_TRADE_SIZE_ETH", "0.1")) # Default ETH order size
    
    # Polling & Refresh Settings
    POLL_INTERVAL_SEC: float = float(os.getenv("POLL_INTERVAL_SEC", "0.5"))
    HISTORICAL_POINTS_MAX: int = 200
    
    # Web & Logging
    WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8000"))
    CSV_LOG_FILE: str = "eth_arbitrage_log.csv"

# Global Instance
config = Config()
