"""
Live Price Fetcher: MetaTrader 5 (Spot) vs Delta Exchange (Futures)
===================================================================
Fetches and streams real-time prices for any asset (BTCUSD, ETHUSD, etc.)
Usage:
    python fetch_price.py BTCUSD
    python fetch_price.py ETHUSD
"""

import time
import sys
import argparse
from config import config
from mt5_connector import MT5Connector
from delta_connector import DeltaConnector
from binance_connector import BinanceConnector

def main():
    parser = argparse.ArgumentParser(description="Live Price Fetcher MT5 Spot vs Delta vs Binance Futures")
    parser.add_argument("symbol", nargs="?", default=config.DELTA_SYMBOL, help="Symbol to monitor (e.g. BTCUSD, ETHUSD)")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    print("=" * 80)
    print(f"   LIVE PRICE MONITOR: {symbol} (MT5 Spot | Delta | Binance Futures)")
    print("=" * 80)
    print("Press Ctrl+C to stop.\n")

    mt5_conn = MT5Connector(symbol=symbol)
    delta_conn = DeltaConnector(symbol=symbol, base_url=config.DELTA_BASE_URL)
    binance_conn = BinanceConnector()

    binance_coin_symbol = config.BINANCE_COIN_CONTRACT if "ETH" in symbol else "BTCUSD_261225"

    if not mt5_conn.connect():
        print(f"[WARN] Could not initialize MT5 symbol '{symbol}'. Ensure symbol is in Market Watch.")

    try:
        while True:
            mt5_tick = mt5_conn.get_latest_tick()
            delta_tick = delta_conn.get_latest_tick()
            b_coin_tick = binance_conn.get_coinm_tick(binance_coin_symbol)

            now_str = time.strftime("%H:%M:%S")

            mt5_str = f"${mt5_tick['mid']:8.2f}" if mt5_tick else "N/A"
            delta_str = f"${delta_tick['mid']:8.2f}" if delta_tick else "N/A"
            b_coin_str = f"${b_coin_tick['last']:8.2f} (B:{b_coin_tick['bid']:.2f}/A:{b_coin_tick['ask']:.2f})" if b_coin_tick else "N/A"

            spread_info = ""
            if b_coin_tick and mt5_tick:
                spread = b_coin_tick['last'] - mt5_tick['mid']
                spread_pct = (spread / mt5_tick['mid']) * 100.0 if mt5_tick['mid'] > 0 else 0.0
                spread_info = f" | Sprd(Binance-MT5): {spread:+7.2f} ({spread_pct:+6.3f}%)"

            print(
                f"[{now_str}] {symbol} -> "
                f"MT5: {mt5_str} | "
                f"Delta: {delta_str} | "
                f"Binance Qtly ({binance_coin_symbol}): {b_coin_str}"
                f"{spread_info}"
            )

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n[INFO] Stopped price monitoring.")
    finally:
        mt5_conn.disconnect()

if __name__ == "__main__":
    main()
