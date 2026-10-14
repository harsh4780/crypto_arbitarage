"""
Real-Time Arbitrage Spike Visualizer & Monitor
=============================================
Visualizes spread spikes tick-by-tick (Sequence Number vs Spread Difference).
Matches clean sequence line plot format (No complex candlesticks needed).

Features:
- Live streaming from MetaTrader 5 (Spot) and Binance Futures (or Delta/Bybit)
- Sequence Number on X-axis, Spread Difference on Y-axis
- Prominent baseline and spike threshold levels
- Automatic spike detection with visual peak markers
- Maintains and logs detected spikes to 'spike_events.csv'
- Offline replay mode: can plot historical CSV logs (e.g. eth_arbitrage_log.csv)
"""

import sys
import os
import time
import argparse
import datetime
import threading
from collections import deque
import requests
import pandas as pd
import numpy as np

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.widgets import Button, TextBox

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


class SpikeDataMaintainer:
    """Maintains real-time tick sequences, baseline calculations, and spike logging."""

    def __init__(self, max_points: int = 150, spike_threshold: float = 1.5, log_file: str = "spike_events.csv"):
        self.max_points = max_points
        self.spike_threshold = spike_threshold
        self.log_file = log_file

        self.seq_counter = 0
        self.sequence_numbers = deque(maxlen=max_points)
        self.spread_values = deque(maxlen=max_points)
        self.timestamps = deque(maxlen=max_points)

        # Baseline
        self.baseline = 0.0
        self.baseline_initialized = False

        # Spike detection
        self.spike_x = []
        self.spike_y = []
        self.spike_types = []  # 'high' or 'low'

        # Current live stats
        self.current_spread = 0.0
        self.max_spike_high = -999999.0
        self.min_spike_low = 999999.0
        self.total_spikes_count = 0
        self.paused = False

        # Ensure CSV header exists
        if not os.path.exists(self.log_file):
            try:
                with open(self.log_file, "w", encoding="utf-8") as f:
                    f.write("timestamp,sequence_num,spread,baseline,deviation,spike_type\n")
            except Exception:
                pass

    def add_tick(self, spread: float, timestamp_str: str = None):
        """Append a new tick and detect spikes."""
        if self.paused:
            return

        self.seq_counter += 1
        seq = self.seq_counter
        ts = timestamp_str or datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

        self.current_spread = spread
        self.sequence_numbers.append(seq)
        self.spread_values.append(spread)
        self.timestamps.append(ts)

        # Update baseline (exponential moving average or rolling mean)
        if not self.baseline_initialized:
            self.baseline = spread
            self.baseline_initialized = True
        else:
            # Smooth EMA for baseline
            self.baseline = 0.95 * self.baseline + 0.05 * spread

        # Update high/low records
        if spread > self.max_spike_high or self.max_spike_high == -999999.0:
            self.max_spike_high = spread
        if spread < self.min_spike_low or self.min_spike_low == 999999.0:
            self.min_spike_low = spread

        # Check for spike relative to baseline
        deviation = spread - self.baseline
        if abs(deviation) >= self.spike_threshold:
            spike_type = "HIGH" if deviation > 0 else "LOW"
            self.total_spikes_count += 1
            self.spike_x.append(seq)
            self.spike_y.append(spread)
            self.spike_types.append(spike_type)

            # Keep only spikes within the current visible sequence range
            min_seq = self.sequence_numbers[0] if self.sequence_numbers else 0
            while self.spike_x and self.spike_x[0] < min_seq:
                self.spike_x.pop(0)
                self.spike_y.pop(0)
                self.spike_types.pop(0)

            # Persist spike event to CSV
            self._log_spike_event(ts, seq, spread, self.baseline, deviation, spike_type)

    def _log_spike_event(self, ts, seq, spread, baseline, dev, s_type):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"{ts},{seq},{spread:.3f},{baseline:.3f},{dev:.3f},{s_type}\n")
        except Exception:
            pass


class SpikeVisualizerApp:
    """Interactive GUI window matching the clean sequence spike graph style."""

    def __init__(self, maintainer: SpikeDataMaintainer, title: str = "Arbitrage Spike Visualizer", theme: str = "classic"):
        self.dm = maintainer
        self.title = title
        self.theme = theme

        # Setup Matplotlib Figure
        if theme == "dark":
            plt.style.use("dark_background")
            self.bg_color = "#111827"
            self.line_color = "#38bdf8"
            self.baseline_color = "#f59e0b"
            self.text_color = "#f3f4f6"
            self.grid_color = "#374151"
        else:
            plt.style.use("default")
            self.bg_color = "#ffffff"
            self.line_color = "#0000ff"       # Matching user's sample blue line
            self.baseline_color = "#0000aa"   # Baseline color
            self.text_color = "#000000"
            self.grid_color = "#d1d5db"

        self.fig, self.ax = plt.subplots(figsize=(10, 6))
        self.fig.patch.set_facecolor(self.bg_color)
        self.ax.set_facecolor(self.bg_color)

        # Plot components
        (self.line_spread,) = self.ax.plot([], [], color=self.line_color, lw=1.6, label="Spread Difference")
        (self.line_baseline,) = self.ax.plot([], [], color=self.baseline_color, lw=1.2, ls="--", alpha=0.8, label="Baseline (Mean)")
        self.scatter_high = self.ax.scatter([], [], color="#dc2626", s=55, zorder=5, marker="^", label="High Spike (Sell Fut / Buy Spot)")
        self.scatter_low = self.ax.scatter([], [], color="#16a34a", s=55, zorder=5, marker="v", label="Low Dip (Buy Fut / Sell Spot)")

        # Axes labels & Title
        self.ax.set_title(self.title, fontsize=13, fontweight="bold", color=self.text_color, pad=12)
        self.ax.set_xlabel("SEQUENCE NUMBER", fontsize=11, fontweight="bold", color=self.text_color, labelpad=8)
        self.ax.set_ylabel("SPREAD / YIELD ($)", fontsize=11, fontweight="bold", color=self.text_color, labelpad=8)
        self.ax.grid(True, linestyle=":", alpha=0.6, color=self.grid_color)

        # Stats text annotation
        self.stats_text = self.ax.text(
            0.02, 0.95, "",
            transform=self.ax.transAxes,
            fontsize=9.5,
            family="monospace",
            verticalalignment="top",
            bbox=dict(boxstyle="round,pad=0.5", facecolor=self.bg_color, edgecolor=self.grid_color, alpha=0.9)
        )

        # Key bindings
        self.fig.canvas.mpl_connect("key_press_event", self.on_key_press)

    def on_key_press(self, event):
        """Keyboard shortcuts."""
        if event.key == " ":
            self.dm.paused = not self.dm.paused
            state = "PAUSED" if self.dm.paused else "RUNNING"
            print(f"[UI] State changed: {state}")
        elif event.key in ("c", "C"):
            self.dm.sequence_numbers.clear()
            self.dm.spread_values.clear()
            self.dm.spike_x.clear()
            self.dm.spike_y.clear()
            self.dm.spike_types.clear()
            print("[UI] Buffer cleared.")
        elif event.key in ("s", "S"):
            filename = f"spike_snapshot_{int(time.time())}.png"
            self.fig.savefig(filename, dpi=150)
            print(f"[UI] Snapshot saved to {filename}")

    def update_frame(self, frame):
        """Animation update function called periodically."""
        if not self.dm.sequence_numbers:
            return self.line_spread, self.line_baseline, self.stats_text

        x = list(self.dm.sequence_numbers)
        y = list(self.dm.spread_values)

        # Update line data
        self.line_spread.set_data(x, y)

        # Baseline horizontal data across the visible sequence
        baseline_y = [self.dm.baseline] * len(x)
        self.line_baseline.set_data(x, baseline_y)

        # Update Spikes Scatter
        high_x = [sx for sx, st in zip(self.dm.spike_x, self.dm.spike_types) if st == "HIGH"]
        high_y = [sy for sy, st in zip(self.dm.spike_y, self.dm.spike_types) if st == "HIGH"]
        low_x = [sx for sx, st in zip(self.dm.spike_x, self.dm.spike_types) if st == "LOW"]
        low_y = [sy for sy, st in zip(self.dm.spike_y, self.dm.spike_types) if st == "LOW"]

        if high_x:
            self.scatter_high.set_offsets(np.c_[high_x, high_y])
        else:
            self.scatter_high.set_offsets(np.empty((0, 2)))

        if low_x:
            self.scatter_low.set_offsets(np.c_[low_x, low_y])
        else:
            self.scatter_low.set_offsets(np.empty((0, 2)))

        # Auto-adjust limits with margin
        min_x, max_x = x[0], x[-1]
        if max_x - min_x < 20:
            max_x = min_x + 20
        self.ax.set_xlim(min_x, max_x + 1)

        min_y = min(y)
        max_y = max(y)
        y_range = max(max_y - min_y, 2.0)
        margin = y_range * 0.15
        self.ax.set_ylim(min_y - margin, max_y + margin)

        # Update HUD Text
        paused_badge = " [PAUSED (Space)]" if self.dm.paused else ""
        hud = (
            f"Seq: #{x[-1]} | Live Spread: ${self.dm.current_spread:.2f}{paused_badge}\n"
            f"Baseline: ${self.dm.baseline:.2f} | Threshold: ±${self.dm.spike_threshold:.2f}\n"
            f"Peak High: ${self.dm.max_spike_high:.2f} | Peak Low: ${self.dm.min_spike_low:.2f}\n"
            f"Spikes Logged: {self.dm.total_spikes_count} -> spike_events.csv"
        )
        self.stats_text.set_text(hud)

        return self.line_spread, self.line_baseline, self.stats_text


def live_tick_collector(maintainer: SpikeDataMaintainer, spot_symbol: str = "ETHUSD", futures_symbol: str = "ETHUSDT_261225", poll_interval: float = 0.25, mt5_path: str = None):
    """Background worker collecting live ticks from MT5 and Binance."""
    print(f"[Collector] Starting tick collector: MT5({spot_symbol}) vs Binance({futures_symbol})...")

    if MT5_AVAILABLE:
        init_ok = False
        if mt5_path and os.path.exists(mt5_path.strip('\"\'')):
            clean_path = os.path.abspath(mt5_path.strip('\"\''))
            print(f"[Collector] Initializing with custom MT5 path: {clean_path}")
            init_ok = mt5.initialize(path=clean_path)
        else:
            init_ok = mt5.initialize()

        if not init_ok:
            print(f"[Collector] MT5 init error: {mt5.last_error()}. Trying to proceed anyway...")
        else:
            mt5.symbol_select(spot_symbol, True)
            print(f"[Collector] MT5 initialized for {spot_symbol}")
    else:
        print("[Collector] MetaTrader5 package not available. Using simulated spot fallback if needed.")

    # Determine Binance endpoint (Delivery or Perpetual)
    is_delivery = "_" in futures_symbol
    binance_url = (
        f"https://dapi.binance.com/dapi/v1/ticker/bookTicker?symbol={futures_symbol}"
        if is_delivery else
        f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={futures_symbol}"
    )

    last_spot_ask = 0.0

    while True:
        try:
            # 1. MT5 Spot Ask
            spot_ask = 0.0
            if MT5_AVAILABLE:
                tick_s = mt5.symbol_info_tick(spot_symbol)
                if tick_s and tick_s.ask > 0:
                    spot_ask = float(tick_s.ask)
                    last_spot_ask = spot_ask

            if spot_ask <= 0 and last_spot_ask > 0:
                spot_ask = last_spot_ask

            # 2. Binance Futures Bid
            res = requests.get(binance_url, timeout=2).json()
            if isinstance(res, list) and len(res) > 0:
                res = res[0]
            fut_bid = float(res.get("bidPrice", 0.0))

            if spot_ask > 0 and fut_bid > 0:
                # Spread = Futures Bid - Spot Ask (Exact Arbitrage Spread)
                spread = round(fut_bid - spot_ask, 2)
                maintainer.add_tick(spread)

        except Exception as e:
            time.sleep(0.5)

        time.sleep(poll_interval)


def replay_from_csv(maintainer: SpikeDataMaintainer, csv_path: str, speed_delay: float = 0.05):
    """Replay ticks from an existing CSV log (e.g. eth_arbitrage_log.csv or spread_log.csv)."""
    print(f"[Replay] Loading CSV data from: {csv_path}...")
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"[Replay] Error reading CSV: {e}")
        return

    # Identify spread column
    col = None
    for candidate in ["Spread_USD", "open_difference", "spread", "Spread", "spread_usd"]:
        if candidate in df.columns:
            col = candidate
            break

    if not col:
        print(f"[Replay] Could not find spread column in columns: {list(df.columns)}")
        return

    print(f"[Replay] Found {len(df)} rows. Streaming column '{col}'...")

    for idx, row in df.iterrows():
        while maintainer.paused:
            time.sleep(0.1)

        val = float(row[col])
        maintainer.add_tick(val)
        time.sleep(speed_delay)


def main():
    parser = argparse.ArgumentParser(description="Real-Time Arbitrage Spike Visualizer")
    parser.add_argument("--spot", default="ETHUSD", help="MT5 Spot symbol (default: ETHUSD)")
    parser.add_argument("--fut", default="ETHUSDT_261225", help="Binance Futures symbol (default: ETHUSDT_261225 or ETHUSDT)")
    parser.add_argument("--threshold", type=float, default=1.5, help="Spike deviation threshold ($)")
    parser.add_argument("--points", type=int, default=120, help="Number of visible sequence ticks on X-axis (default: 120)")
    parser.add_argument("--file", default=None, help="Replay from CSV log file (e.g. eth_arbitrage_log.csv)")
    parser.add_argument("--theme", default="classic", choices=["classic", "dark"], help="Theme: 'classic' (like user image) or 'dark'")
    parser.add_argument("--interval", type=float, default=0.25, help="Tick polling interval in seconds (default: 0.25)")
    parser.add_argument("--mt5-path", default=None, help="Custom MT5 terminal64.exe executable path")

    args = parser.parse_args()

    maintainer = SpikeDataMaintainer(max_points=args.points, spike_threshold=args.threshold)

    # Start data collector thread
    if args.file:
        t = threading.Thread(target=replay_from_csv, args=(maintainer, args.file, args.interval), daemon=True)
    else:
        t = threading.Thread(target=live_tick_collector, args=(maintainer, args.spot, args.fut, args.interval, args.mt5_path), daemon=True)
    t.start()

    # Title
    title = f"Arbitrage Spike Sequence Graph: {args.fut} - {args.spot}"
    if args.file:
        title += f" [Replaying {os.path.basename(args.file)}]"

    app = SpikeVisualizerApp(maintainer, title=title, theme=args.theme)

    # Animate at ~30 FPS (every 35ms)
    ani = animation.FuncAnimation(app.fig, app.update_frame, interval=40, blit=False, cache_frame_data=False)

    print("\n" + "="*60)
    print("🚀 Spike Visualizer is active!")
    print("X-Axis: Sequence Number | Y-Axis: Spread ($)")
    print("Key controls:")
    print("   [Space] : Pause / Resume graph")
    print("   [C]     : Clear sequence history")
    print("   [S]     : Save PNG snapshot")
    print("="*60 + "\n")

    plt.show()


if __name__ == "__main__":
    main()
