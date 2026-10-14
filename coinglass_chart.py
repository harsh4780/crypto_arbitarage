"""
Coinglass-Style Arbitrage Spikes & Price Overlay Visualizer
==========================================================
Visualizes:
1. Spikes / Spread Difference as Vibrant Green (+ve) & Red (-ve) vertical histogram bars around 0.
2. Underlying Asset Price as an overlaid smooth white line.
3. Dual Y-axis (Left: Spread %, Right: Price $).
4. Bottom draggable timeline zoom slider.

Usage:
  python coinglass_chart.py          # Opens interactive Coinglass Web View in browser
  python coinglass_chart.py --native # Opens native Matplotlib desktop window
"""

import sys
import os
import time
import argparse
import webbrowser
import threading
import requests
import pandas as pd
import numpy as np

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


def launch_browser_view():
    """Launch the interactive Coinglass web app."""
    url = "http://localhost:8000/static/coinglass.html"
    print("\n" + "="*65)
    print("🚀 Opening Coinglass Style Spike & Price Chart in your browser...")
    print(f"👉 URL: {url}")
    print("="*65 + "\n")
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Please open your browser and navigate to: {url}")


def run_native_matplotlib(csv_file: str = "eth_arbitrage_log.csv", limit: int = 250):
    """Run native Matplotlib version with Green/Red bars + White price line."""
    import matplotlib.pyplot as plt

    plt.style.use("dark_background")
    fig, ax_spread = plt.subplots(figsize=(12, 6.5))
    fig.patch.set_facecolor("#07090e")
    ax_spread.set_facecolor("#07090e")

    ax_price = ax_spread.twinx()

    df = None
    if os.path.exists(csv_file):
        try:
            df = pd.read_csv(csv_file)
            df = df.tail(limit)
        except Exception as e:
            print(f"Error loading {csv_file}: {e}")

    if df is not None and not df.empty:
        x = np.arange(len(df))
        spread = df["Spread_USD"].values
        price = df.get("MT5_Spot_Mid", df.get("MT5_Spot_Ask", pd.Series(np.zeros(len(df))))).values

        # Green for positive, Red for negative
        colors = ["#10b981" if s >= 0 else "#ef4444" for s in spread]

        # 1. Bar spikes around 0
        bars = ax_spread.bar(x, spread, color=colors, width=0.8, alpha=0.9, label="Spread Spike ($)")
        ax_spread.axhline(0, color="#475569", linestyle="-", linewidth=1.2)

        # 2. White Price line
        line_price = ax_price.plot(x, price, color="#ffffff", linewidth=1.8, label="Price ($)")

        ax_spread.set_ylabel("SPREAD SPIKE ($)", color="#10b981", fontweight="bold", fontsize=11)
        ax_price.set_ylabel("ASSET PRICE ($)", color="#ffffff", fontweight="bold", fontsize=11)
        ax_spread.set_xlabel("SEQUENCE / TICK", color="#94a3b8", fontweight="bold", fontsize=11)

        ax_spread.grid(True, linestyle=":", color="#1f2937", alpha=0.6)
        plt.title("Coinglass Style: Arbitrage Spikes (Green/Red) & Price Overlay", color="#f8fafc", fontsize=13, fontweight="bold", pad=14)

        # Combined legend
        lines1, labels1 = ax_spread.get_legend_handles_labels()
        lines2, labels2 = ax_price.get_legend_handles_labels()
        ax_spread.legend(lines1 + lines2, labels1 + labels2, loc="upper left", facecolor="#111827", edgecolor="#374151")

        plt.tight_layout()
        print("Displaying native Coinglass chart...")
        plt.show()
    else:
        print("No CSV data found. Run without --native to view the live streaming web chart.")


def main():
    parser = argparse.ArgumentParser(description="Coinglass Style Spike Visualizer")
    parser.add_argument("--native", action="store_true", help="Launch native Matplotlib desktop window")
    parser.add_argument("--file", default="eth_arbitrage_log.csv", help="CSV log file to render in native mode")
    parser.add_argument("--limit", type=int, default=300, help="Number of ticks in native mode")
    args = parser.parse_args()

    if args.native:
        run_native_matplotlib(csv_file=args.file, limit=args.limit)
    else:
        launch_browser_view()


if __name__ == "__main__":
    main()
