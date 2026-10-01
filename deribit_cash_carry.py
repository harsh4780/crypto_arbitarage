"""
Deribit 3-Month Cash & Carry Arbitrage Scanner
===============================================
Tracks live premiums and Annualized Yield (% p.a.) between
Spot prices and Deribit Dated 3-Month / 6-Month Expiry Futures.
"""

import time
from datetime import datetime
from config import config
from deribit_connector import DeribitConnector
from mt5_connector import MT5Connector

def calculate_annualized_yield(premium_pct: float, days_to_expiry: int) -> float:
    """Calculate annualized risk-free return percentage (% p.a.)."""
    if days_to_expiry <= 0:
        return 0.0
    return (premium_pct / days_to_expiry) * 365.0

def scan_cash_and_carry(currency: str = "BTC"):
    deribit = DeribitConnector(currency=currency, client_id=config.DERIBIT_CLIENT_ID, client_secret=config.DERIBIT_CLIENT_SECRET)
    mt5_sym = f"{currency}USD"
    mt5 = MT5Connector(symbol=mt5_sym)
    mt5.connect()

    print("=" * 75)
    print(f"  DERIBIT 3-MONTH CASH & CARRY ARBITRAGE MONITOR ({currency})")
    print("=" * 75)

    if deribit.authenticate():
        summary = deribit.get_account_summary()
        if summary.get("success"):
            print(f"  Deribit Account Connected | Equity: {summary['result']['equity']} {currency}")
    
    print("-" * 75)
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            mt5_tick = mt5.get_latest_tick()
            
            # Fetch Deribit dated futures
            dated_instruments = deribit.get_dated_futures()
            
            now_str = time.strftime("%H:%M:%S")
            print(f"\n[{now_str}] Market Snapshot ({currency}):")
            if mt5_tick:
                print(f"  MT5 Spot Mid Price: ${mt5_tick['mid']:,.2f}")

            # Check top 3 dated contracts
            for inst in dated_instruments[-3:]:
                symbol = inst.get("instrument_name")
                ticker = deribit.get_latest_ticker(symbol)
                
                if ticker:
                    spot_ref = mt5_tick["mid"] if mt5_tick else ticker["spot_index"]
                    future_mid = ticker["mid"]
                    premium_usd = future_mid - spot_ref
                    premium_pct = (premium_usd / spot_ref) * 100.0 if spot_ref > 0 else 0.0

                    print(
                        f"  Contract: {symbol:14s} | "
                        f"Future Price: ${future_mid:9,.2f} | "
                        f"Premium: {premium_usd:+7.2f} USD ({premium_pct:+6.3f}%)"
                    )

            time.sleep(2.5)

    except KeyboardInterrupt:
        print("\n[INFO] Cash & Carry monitor stopped.")
    finally:
        mt5.disconnect()

if __name__ == "__main__":
    scan_cash_and_carry("BTC")
