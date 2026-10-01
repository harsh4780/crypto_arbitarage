"""
Terminal Dashboard UI
=====================
Interactive Rich terminal display with live prices, orderbooks,
arbitrage signal alerts, and real-time ASCII spread chart.
"""

import os
import time
from typing import Dict, Any, List
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich import box

class TerminalUI:
    def __init__(self):
        self.console = Console()

    def generate_ascii_chart(self, history: List[Dict[str, Any]], width: int = 60, height: int = 7) -> str:
        """Render a mini ASCII line chart of the spread history."""
        if not history:
            return "No spread history recorded yet..."

        spreads = [h["spread_usd"] for h in history[-width:]]
        if not spreads:
            return ""

        min_val = min(spreads)
        max_val = max(spreads)
        rng = (max_val - min_val) if max_val != min_val else 1.0

        # Create canvas
        rows = [[" " for _ in range(len(spreads))] for _ in range(height)]

        for col, val in enumerate(spreads):
            normalized = int(((val - min_val) / rng) * (height - 1))
            normalized = max(0, min(height - 1, normalized))
            # 0 is top row, height-1 is bottom row
            row_idx = (height - 1) - normalized
            rows[row_idx][col] = "█" if val >= 0 else "▄"

        chart_lines = []
        for r_idx, row in enumerate(rows):
            val_at_row = max_val - (r_idx / (height - 1)) * rng
            prefix = f"{val_at_row:+6.2f} │ "
            chart_lines.append(prefix + "".join(row))

        chart_lines.append("       └" + "─" * len(spreads))
        chart_lines.append(f"       Min: ${min_val:+.2f} | Max: ${max_val:+.2f} | Current: ${spreads[-1]:+.2f}")
        return "\n".join(chart_lines)

    def render_layout(self, data: Dict[str, Any], history: List[Dict[str, Any]]) -> Layout:
        """Build Rich Layout structure."""
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )

        # Header
        header_text = Text("⚡ MT5 ETH SPOT vs DELTA FUTURES ARBITRAGE MONITOR ⚡", style="bold cyan")
        time_str = data.get("time_str", time.strftime("%H:%M:%S"))
        header_panel = Panel(
            Text.assemble((header_text), ("  │  Time: ", "bold yellow"), (time_str, "bold white")),
            style="bold white on blue", box=box.ROUNDED
        )
        layout["header"].update(header_panel)

        # Main content splits
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1)
        )

        # Left Column: Market Prices & Arbitrage Metrics
        mt5 = data.get("mt5", {}) or {}
        delta = data.get("delta", {}) or {}

        price_table = Table(title="📊 Real-Time Market Feeds", box=box.SIMPLE_HEAD, expand=True)
        price_table.add_column("Exchange", style="bold magenta")
        price_table.add_column("Symbol", style="cyan")
        price_table.add_column("Bid ($)", style="bold green", justify="right")
        price_table.add_column("Ask ($)", style="bold red", justify="right")
        price_table.add_column("Mid ($)", style="bold yellow", justify="right")

        price_table.add_row(
            "MetaTrader 5", str(mt5.get("symbol", "ETHUSD")),
            f"{mt5.get('bid', 0.0):,.2f}", f"{mt5.get('ask', 0.0):,.2f}", f"{mt5.get('mid', 0.0):,.2f}"
        )
        price_table.add_row(
            "Delta Exchange", str(delta.get("symbol", "ETHUSDT")),
            f"{delta.get('bid', 0.0):,.2f}", f"{delta.get('ask', 0.0):,.2f}", f"{delta.get('mid', 0.0):,.2f}"
        )

        # Signal Box
        signal = data.get("signal", "NEUTRAL")
        is_opp = data.get("is_opportunity", False)
        spread_usd = data.get("spread_usd", 0.0)
        spread_pct = data.get("spread_pct", 0.0)

        if signal == "BUY_SPOT_SELL_FUTURES":
            sig_style = "bold green"
            sig_box_style = "green"
            sig_icon = "🚀 OPPORTUNITY: BUY SPOT (MT5) / SELL FUTURES (DELTA)"
        elif signal == "SELL_SPOT_BUY_FUTURES":
            sig_style = "bold green"
            sig_box_style = "green"
            sig_icon = "🚀 OPPORTUNITY: SELL SPOT (MT5) / BUY FUTURES (DELTA)"
        else:
            sig_style = "bold yellow"
            sig_box_style = "dim cyan"
            sig_icon = "⏳ SEARCHING / NEUTRAL (Spread below threshold)"

        metrics_table = Table(box=box.MINIMAL, expand=True)
        metrics_table.add_column("Metric", style="bold white")
        metrics_table.add_column("Value", style="bold yellow", justify="right")

        metrics_table.add_row("Futures - Spot Spread ($)", f"${spread_usd:+.2f}")
        metrics_table.add_row("Basis Percentage (%)", f"{spread_pct:+.3f}%")
        metrics_table.add_row("Buy Spot / Sell Fut Spread", f"${data.get('spread_buy_spot_sell_fut', 0.0):+.2f}")
        metrics_table.add_row("Sell Spot / Buy Fut Spread", f"${data.get('spread_sell_spot_buy_fut', 0.0):+.2f}")
        metrics_table.add_row("Trade Size", f"{data.get('trade_size_eth', 0.1)} ETH")
        metrics_table.add_row("Est. Gross PnL", f"${data.get('gross_pnl_usd', 0.0):+.2f}")
        metrics_table.add_row("Est. Roundtrip Fee", f"${data.get('estimated_fee_usd', 0.0):.2f}")
        metrics_table.add_row("Est. Net PnL", f"${data.get('net_pnl_usd', 0.0):+.2f}", style="bold green" if data.get("net_pnl_usd", 0) > 0 else "white")

        left_layout = Layout()
        left_layout.split(
            Layout(Panel(price_table, box=box.ROUNDED), size=7),
            Layout(Panel(metrics_table, title="💰 Arbitrage Opportunity Matrix", box=box.ROUNDED), ratio=1)
        )
        layout["left"].update(left_layout)

        # Right Column: Real-Time ASCII Spread Chart & Signal Panel
        chart_str = self.generate_ascii_chart(history)
        chart_panel = Panel(Text(chart_str, style="cyan"), title="📈 Live Real-Time Spread Chart (Delta Futures - MT5 Spot)", box=box.ROUNDED)
        
        signal_panel = Panel(
            Text(f"{sig_icon}\n{data.get('action_detail', '')}", style=sig_style, justify="center"),
            title="🎯 Signal Status", style=sig_box_style, box=box.ROUNDED
        )

        right_layout = Layout()
        right_layout.split(
            Layout(chart_panel, ratio=2),
            Layout(signal_panel, size=4)
        )
        layout["right"].update(right_layout)

        # Footer
        footer_text = Text(
            "Press Ctrl+C to exit  │  Web Dashboard: http://127.0.0.1:8000  │  CSV Logged to eth_arbitrage_log.csv",
            style="dim white", justify="center"
        )
        layout["footer"].update(Panel(footer_text, box=box.ROUNDED))

        return layout
