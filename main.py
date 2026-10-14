

import sys
import time
import argparse
import logging
import threading
from typing import Any
import uvicorn

from config import config
from mt5_connector import MT5Connector
from delta_connector import DeltaConnector
from bybit_connector import BybitConnector
from binance_connector import BinanceConnector
from arbitrage_engine import ArbitrageEngine
from terminal_ui import TerminalUI
from rich.live import Live

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("eth_arbitrage_system.log"), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("main")

def run_terminal_mode(mt5_conn: MT5Connector, futures_conn: Any, engine: ArbitrageEngine, contract_name: str = ""):
    """Run Rich terminal live interactive view."""
    ui = TerminalUI()
    logger.info("Starting Terminal Dashboard... Press Ctrl+C to exit.")
    
    if not mt5_conn.connect():
        print("[WARNING] Could not connect to MT5. Ensure MT5 is running with symbol in Market Watch.")

    with Live(refresh_per_second=2, screen=False) as live:
        try:
            while True:
                mt5_tick = mt5_conn.get_latest_tick()
                if hasattr(futures_conn, "get_latest_ticker"):
                    futures_tick = futures_conn.get_latest_ticker(contract_name)
                else:
                    futures_tick = futures_conn.get_latest_tick()
                
                eval_data = engine.evaluate(mt5_tick, futures_tick)
                history = engine.get_history_list()

                layout = ui.render_layout(eval_data, history)
                live.update(layout)
                
                time.sleep(config.POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("Terminal UI stopped by user.")
        finally:
            mt5_conn.disconnect()

def run_web_server():
    """Run Uvicorn FastAPI server."""
    logger.info(f"Starting Web Server at http://{config.WEB_HOST}:{config.WEB_PORT}")
    uvicorn.run("server:app", host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")

def run_dual_mode(mt5_conn: MT5Connector, futures_conn: Any, engine: ArbitrageEngine, contract_name: str = ""):
    """Run Web Server in background thread and Terminal UI in foreground."""
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    time.sleep(1.0) # Allow web server time to bind port
    run_terminal_mode(mt5_conn, futures_conn, engine, contract_name)

def main():
    parser = argparse.ArgumentParser(description="Spot (MT5) vs Futures (Bybit / Delta / Deribit) Arbitrage Monitor")
    parser.add_argument("--mode", choices=["terminal", "web", "dual"], default="web",
                        help="Run mode: 'terminal' (CLI dashboard), 'web' (Web UI chart), or 'dual' (Both)")
    parser.add_argument("--exchange", choices=["binance", "bybit", "deribit", "delta"], default="binance",
                        help="Futures exchange source: 'binance' (Default Demo/Live), 'bybit', 'deribit', or 'delta'")
    parser.add_argument("--mt5-symbol", type=str, default=config.MT5_SYMBOL, help="MT5 Symbol (e.g. BTCUSD, ETHUSD)")
    parser.add_argument("--mt5-path", type=str, default=config.MT5_PATH, help="Full path to MT5 terminal64.exe (e.g. 'C:\\Program Files\\MetaTrader 5\\terminal64.exe')")
    parser.add_argument("--contract", type=str, default=config.BINANCE_COIN_CONTRACT or "ETHUSD_261225", help="Futures contract symbol (e.g. ETHUSD_261225)")
    parser.add_argument("--min-spread", type=float, default=config.MIN_SPREAD_USD, help="Min spread USD threshold")
    
    args = parser.parse_args()

    # Apply CLI args to config
    config.MT5_SYMBOL = args.mt5_symbol
    if args.mt5_path:
        config.MT5_PATH = args.mt5_path
    config.MIN_SPREAD_USD = args.min_spread

    mt5_conn = MT5Connector(symbol=config.MT5_SYMBOL, path=config.MT5_PATH)
    
    if args.exchange == "binance":
        futures_conn = BinanceConnector(testnet=config.BINANCE_TESTNET, api_key=config.BINANCE_API_KEY, api_secret=config.BINANCE_API_SECRET)
    elif args.exchange == "bybit":
        futures_conn = BybitConnector(currency="BTC" if "BTC" in args.mt5_symbol else "ETH", testnet=config.BYBIT_TESTNET, api_key=config.BYBIT_API_KEY, api_secret=config.BYBIT_API_SECRET)
    else:
        futures_conn = DeltaConnector(symbol=config.DELTA_SYMBOL, api_key=config.DELTA_API_KEY, api_secret=config.DELTA_API_SECRET)
        
    curr = "BTC" if "BTC" in args.mt5_symbol else "ETH"
    engine = ArbitrageEngine(currency=curr, diff_to_trade=config.MIN_SPREAD_USD)

    mode_tag = "Demo Account (Testnet)" if ((args.exchange == "binance" and config.BINANCE_TESTNET) or (args.exchange == "bybit" and config.BYBIT_TESTNET)) else "Live"
    print("=" * 75)
    print(f"  Crypto Spot (MT5) vs Futures ({args.exchange.upper()}) Arbitrage System")
    print(f"   Mode: {args.mode.upper()} | Futures Exchange: {args.exchange.upper()} ({mode_tag})")
    print(f"   MT5 Symbol: {config.MT5_SYMBOL} | Futures Contract: {args.contract}")
    print(f"   Min Spread Threshold: ${config.MIN_SPREAD_USD:.2f} USD")
    print(f"   Web Dashboard: http://{config.WEB_HOST}:{config.WEB_PORT}")
    print("=" * 75)

    if args.mode == "terminal":
        run_terminal_mode(mt5_conn, futures_conn, engine, args.contract)
    elif args.mode == "web":
        run_web_server()
    elif args.mode == "dual":
        run_dual_mode(mt5_conn, futures_conn, engine, args.contract)

if __name__ == "__main__":
    main()
