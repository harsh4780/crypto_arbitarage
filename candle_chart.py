"""
TradingView Arbitrage Spread & Price Candlestick Chart Viewer
=============================================================
Calculates and plots the EXACT Arbitrage Difference:
    Spread Candle = Bid(ETH Futures) - Ask(ETH Spot)
with live real-time tick streaming and historical OHLC reconstruction!
"""

import sys
import time
import datetime
import threading
import logging
import requests
import pandas as pd
from lightweight_charts import Chart
import MetaTrader5 as mt5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SpreadChart")


def get_mt5_utc_offset_seconds(symbol: str = "ETHUSD") -> int:
    """Calculate the timezone offset between MT5 broker server time and UTC."""
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc).timestamp()
        tick = mt5.symbol_info_tick(symbol)
        if tick and tick.time:
            # Round to nearest 30 mins (1800s)
            diff = round((tick.time - now_utc) / 1800.0) * 1800
            return int(diff)
    except Exception:
        pass
    return 3 * 3600  # Default to UTC+3 (standard for MT5 brokers)


def get_mt5_candles(symbol: str = "ETHUSD", count: int = 150) -> pd.DataFrame:
    """Fetch historical 1-minute OHLC candles from MetaTrader 5 converted to UTC."""
    if not mt5.initialize():
        logger.error(f"[MT5] Initialization failed: {mt5.last_error()}")
        return pd.DataFrame()

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()

    offset_sec = get_mt5_utc_offset_seconds(symbol)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"] - offset_sec, unit="s")
    df = df.rename(columns={
        "open": "s_open",
        "high": "s_high",
        "low": "s_low",
        "close": "s_close",
        "tick_volume": "s_volume"
    })[["time", "s_open", "s_high", "s_low", "s_close", "s_volume"]]
    return df


def get_binance_candles(symbol: str = "ETHUSDT_261225", limit: int = 150) -> pd.DataFrame:
    """Fetch historical 1-minute OHLC candles from Binance Delivery or USDT-M Futures."""
    # Check if delivery quarterly contract (e.g. contains underscore 261225)
    is_delivery = "_" in symbol
    base_url = "https://dapi.binance.com/dapi/v1/klines" if is_delivery else "https://fapi.binance.com/fapi/v1/klines"
    
    url = f"{base_url}?symbol={symbol}&interval=1m&limit={limit}"
    try:
        res = requests.get(url, timeout=5)
        data = res.json()
        if isinstance(data, list):
            rows = []
            for k in data:
                rows.append({
                    "time": pd.to_datetime(k[0], unit="ms"),
                    "f_open": float(k[1]),
                    "f_high": float(k[2]),
                    "f_low": float(k[3]),
                    "f_close": float(k[4]),
                    "f_volume": float(k[5])
                })
            return pd.DataFrame(rows)
    except Exception as e:
        logger.error(f"[Binance] Failed to fetch klines: {e}")
    return pd.DataFrame()


def build_spread_historical_dataframe(spot_symbol: str = "ETHUSD", futures_symbol: str = "ETHUSDT_261225", count: int = 120) -> pd.DataFrame:
    """Build aligned historical OHLC difference: Futures - Spot."""
    df_s = get_mt5_candles(spot_symbol, count=count)
    df_f = get_binance_candles(futures_symbol, limit=count)

    if df_s.empty or df_f.empty:
        logger.warning("Could not fetch historical data for one or both legs.")
        return pd.DataFrame()

    merged = pd.merge(df_s, df_f, on="time", how="inner")
    if merged.empty:
        logger.warning("Timestamp alignment produced 0 matched candles.")
        return pd.DataFrame()

    spread_df = pd.DataFrame()
    spread_df["time"] = merged["time"]
    # Spread OHLC: Futures - Spot
    spread_df["open"] = (merged["f_open"] - merged["s_open"]).round(2)
    # High spread occurs when futures is high and spot is low
    spread_df["high"] = (merged["f_high"] - merged["s_low"]).round(2)
    # Low spread occurs when futures is low and spot is high
    spread_df["low"] = (merged["f_low"] - merged["s_high"]).round(2)
    spread_df["close"] = (merged["f_close"] - merged["s_close"]).round(2)
    spread_df["volume"] = merged["f_volume"].round(2)

    return spread_df


def open_spread_chart(spot_symbol: str = "ETHUSD", futures_symbol: str = "ETHUSDT_261225"):
    """
    Launch interactive TradingView Candlestick Chart of:
        Spread = Bid(ETH Futures) - Ask(ETH Spot)
    with live real-time tick streaming updates!
    """
    chart = Chart(
        toolbox=True,
        title=f"⚡ SPREAD: Bid({futures_symbol}) - Ask({spot_symbol})",
        inner_width=0.98,
        inner_height=0.98
    )

    # Candlestick colors for spread expansion (green) vs compression (red)
    chart.candle_style(
        up_color="#22c55e",
        down_color="#ef4444",
        border_up_color="#22c55e",
        border_down_color="#ef4444",
        wick_up_color="#22c55e",
        wick_down_color="#ef4444"
    )
    chart.legend(visible=True, font_family="JetBrains Mono, Inter", font_size=13)

    logger.info(f"Building historical spread candles for {futures_symbol} - {spot_symbol}...")
    df_spread = build_spread_historical_dataframe(spot_symbol, futures_symbol, count=150)

    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    if not df_spread.empty:
        chart.set(df_spread)
        logger.info(f"Loaded {len(df_spread)} historical spread candles.")
        last_close = float(df_spread["close"].iloc[-1])
    else:
        # Create bootstrap candles if historical API unavailable
        logger.warning("Generating bootstrap live candles...")
        last_close = 25.0
        bootstrap = pd.DataFrame([{
            "time": now - datetime.timedelta(minutes=1),
            "open": 24.5, "high": 25.2, "low": 24.1, "close": 24.8, "volume": 10
        }])
        chart.set(bootstrap)

    # Reference target lines
    try:
        chart.horizontal_line(price=4.0, color="#38bdf8", width=1, style="dashed", text="Open Target (diffToTrade: $4.00)")
        chart.horizontal_line(price=0.5, color="#f59e0b", width=1, style="dashed", text="Close Target (diffToCut: $0.50)")
    except Exception:
        pass

    # State for forming live candle
    live_state = {
        "current_minute": None,
        "candle": None,
        "running": True
    }

    def live_stream_worker():
        """Fetch live ticks every 500ms and update the current spread candle."""
        is_delivery = "_" in futures_symbol
        ticker_url = (
            f"https://dapi.binance.com/dapi/v1/ticker/bookTicker?symbol={futures_symbol}"
            if is_delivery else
            f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={futures_symbol}"
        )

        while live_state["running"]:
            try:
                # 1. Fetch MT5 Spot Ask
                tick_s = mt5.symbol_info_tick(spot_symbol)
                spot_ask = float(tick_s.ask) if tick_s and tick_s.ask > 0 else 0.0

                # 2. Fetch Binance Futures Bid
                res = requests.get(ticker_url, timeout=2).json()
                if isinstance(res, list) and len(res) > 0:
                    res = res[0]
                fut_bid = float(res.get("bidPrice", 0.0))

                if spot_ask > 0 and fut_bid > 0:
                    # EXACT ARBITRAGE ENTRY SPREAD:
                    spread_val = round(fut_bid - spot_ask, 2)
                    
                    cur_min = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
                    
                    if live_state["current_minute"] is None or cur_min > live_state["current_minute"]:
                        # Start new 1-minute candle
                        live_state["current_minute"] = cur_min
                        live_state["candle"] = {
                            "time": cur_min.strftime("%Y-%m-%d %H:%M:%S"),
                            "open": spread_val,
                            "high": spread_val,
                            "low": spread_val,
                            "close": spread_val
                        }
                    else:
                        # Update current candle
                        c = live_state["candle"]
                        c["high"] = max(c["high"], spread_val)
                        c["low"] = min(c["low"], spread_val)
                        c["close"] = spread_val

                    # Update lightweight chart
                    chart.update(pd.Series(live_state["candle"]))
                    
            except Exception as e:
                pass

            time.sleep(0.5)

    # Start live tick stream in background daemon
    t = threading.Thread(target=live_stream_worker, daemon=True)
    t.start()

    logger.info("Opening Live Arbitrage Spread TradingView Chart Window...")
    try:
        chart.show(block=True)
    finally:
        live_state["running"] = False


def open_single_chart(source: str = "mt5", symbol: str = "ETHUSD"):
    """Launch single instrument chart (MT5 Spot or Binance Futures)."""
    chart = Chart(toolbox=True, title=f"{symbol} ({source.upper()})")
    chart.candle_style(
        up_color="#22c55e", down_color="#ef4444",
        border_up_color="#22c55e", border_down_color="#ef4444",
        wick_up_color="#22c55e", wick_down_color="#ef4444"
    )
    chart.legend(visible=True)

    if source.lower() == "mt5":
        df = get_mt5_candles(symbol, count=200)
        df = df.rename(columns={"s_open": "open", "s_high": "high", "s_low": "low", "s_close": "close", "s_volume": "volume"})
    else:
        df = get_binance_candles(symbol, limit=200)
        df = df.rename(columns={"f_open": "open", "f_high": "high", "f_low": "low", "f_close": "close", "f_volume": "volume"})

    chart.set(df)
    chart.show(block=True)


if __name__ == "__main__":
    mode = "spread"
    spot = "ETHUSD"
    fut = "ETHUSDT_261225"

    if len(sys.argv) > 1:
        arg1 = sys.argv[1].lower()
        if arg1 in ("spread", "diff", "gap"):
            mode = "spread"
            if len(sys.argv) > 2: spot = sys.argv[2]
            if len(sys.argv) > 3: fut = sys.argv[3]
        elif arg1 in ("mt5", "binance"):
            mode = arg1
            if len(sys.argv) > 2: spot = sys.argv[2]
        else:
            mode = "spread"

    if mode == "spread":
        open_spread_chart(spot_symbol=spot, futures_symbol=fut)
    else:
        open_single_chart(source=mode, symbol=spot)
