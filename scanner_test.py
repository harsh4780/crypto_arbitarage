import time
from mt5_connector import MT5Connector
from delta_connector import DeltaConnector
from arbitrage_engine import ArbitrageEngine

def scan_btc():
    mt5_btc = MT5Connector("BTCUSD")
    delta_btc = DeltaConnector("BTCUSD", base_url="https://api.india.delta.exchange")

    if not mt5_btc.connect():
        print("[ERROR] Could not connect MT5 for BTCUSD. Ensure BTCUSD is in Market Watch.")
        return

    engine = ArbitrageEngine(min_spread_usd=35.0, min_spread_pct=0.04)

    print("=== LIVE BTC ARBITRAGE SCANNER (10 SAMPLES) ===")
    btc_spreads = []

    for i in range(10):
        t_m = mt5_btc.get_latest_tick()
        t_d = delta_btc.get_latest_tick()
        if t_m and t_d:
            mt5_mid = t_m['mid']
            delta_mid = t_d['mid']
            spread = delta_mid - mt5_mid
            spread_pct = (spread / mt5_mid) * 100.0 if mt5_mid > 0 else 0.0
            btc_spreads.append(spread)
            print(f"[Sample {i+1:2d}/10 BTC] MT5 Spot: ${mt5_mid:9.2f} (B:{t_m['bid']:9.2f}/A:{t_m['ask']:9.2f}) | Delta Fut: ${delta_mid:9.2f} (B:{t_d['bid']:9.2f}/A:{t_d['ask']:9.2f}) | Spread: {spread:+8.2f} USD ({spread_pct:+6.3f}%)")
        time.sleep(1.0)

    mt5_btc.disconnect()

    if btc_spreads:
        avg_s = sum(btc_spreads) / len(btc_spreads)
        min_s = min(btc_spreads)
        max_s = max(btc_spreads)
        print("\n=== SUMMARY STATS FOR BTC ===")
        print(f"BTC Avg Spread: ${avg_s:+8.2f} | Min Spread: ${min_s:+8.2f} | Max Spread: ${max_s:+8.2f}")

if __name__ == "__main__":
    scan_btc()
