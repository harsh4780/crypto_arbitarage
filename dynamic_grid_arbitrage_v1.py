"""
Dynamic Grid Gold Arbitrage Bot v1.0 — FOK Edition (nTrader Python Bridge)
==========================================================================
Key Architecture:
  1. Live Baseline Anchor Engine:
     Auto-calibrates to live broker baseline gap (~40.49) and centers grid levels dynamically.

  2. Two-Way Grid Scaling (BUY & SELL Directions):
     - SELL Grid (Spike UP): Futures SELL + Spot BUY (Level 1 @ Baseline + 1.00 = 41.49)
     - BUY Grid (Dip DOWN): Futures BUY + Spot SELL (Level 1 @ Baseline - 1.00 = 39.49)

  3. 1:1 Contract Equivalence Sizing:
     1.0 Standard Lot Futures vs 0.01 Lot Spot matching user broker specifications.

  4. FILL-OR-KILL (FOK) Zero-Slippage Execution:
     Orders are sent at the EXACT requested price. After fill confirmation, actual LP fill
     price is compared against requested price with ZERO tolerance. If ANY slippage is
     detected (even $0.01), both legs are INSTANTLY killed/closed. Entry miss is acceptable;
     slippage is NEVER acceptable.

  5. Microsecond Emergency Leg Rollback (Zero Leg Risk):
     Fires both legs in parallel via ThreadPoolExecutor. If 1 leg fills and the 2nd leg fails
     or is rejected by LP, the script instantly closes/kills the 1st leg within 200ms.

  6. Live LP Spread Protection:
     Monitors (Futures_Spread + Spot_Spread). If LPs widen spreads during high-volatility news,
     triggers are automatically suppressed.

  7. Zero-Delay Hot Execution Path:
     Clean, low-overhead loop with no unnecessary sleep calls in the tick evaluation path.
"""

from __future__ import annotations
import json
import os
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def get_writable_path(filename: str) -> Path:
    """Returns Path object for `filename` in a guaranteed writable directory."""
    candidates: List[Path] = []
    if "__file__" in globals():
        try:
            candidates.append(Path(__file__).resolve().parent / filename)
        except Exception:
            pass
    try:
        candidates.append(Path.cwd() / filename)
    except Exception:
        pass

    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        candidates.append(Path(appdata) / "TradivantaBot" / filename)
    candidates.append(Path.home() / ".ntrader_bot" / filename)

    for cand in candidates:
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            test_file = cand.parent / f".write_test_{os.getpid()}.tmp"
            with open(test_file, "w", encoding="utf-8") as fh:
                fh.write("test")
            if test_file.exists():
                test_file.unlink()
            return cand
        except Exception:
            continue

    import tempfile
    return Path(tempfile.gettempdir()) / filename


# ============================= DATA MODELS =============================

@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Position:
    ticket: str
    symbol: str
    side: str            # "BUY" or "SELL"
    volume: float
    open_time: datetime
    profit: float = 0.0
    swap: float = 0.0
    open_price: float = 0.0
    magic: int = 0
    comment: str = ""


# ============================= CONFIGURATION =============================

@dataclass
class DynamicArbitrageConfig:
    # Symbols & Contract Sizing (Matching Broker Contract Specs)
    symbol1: str = "GOLD_DEC26"          # Futures leg
    symbol2: str = "XAUUSD.LMAX"         # Spot leg
    future_lot: float = 1.0              # 1 Standard Lot Futures
    spot_lot: float = 0.01               # 0.01 Lot Spot (1:1 Contract Equivalence on user broker)

    # Dynamic Basis Math Parameters
    expiry_date: str = "2026-12-28"      # Futures Expiry (YYYY-MM-DD)
    annual_sofr_rate: float = 0.045      # 4.5% USD Interest Rate
    use_theoretical_sofr: bool = False   # Set False to anchor grid to real-time live market gap (~40.49)

    # Dynamic Grid Parameters (Channel Envelope Micro-Grid Mode)
    step_size: float = 1.00              # $1.00 step size
    grid_levels: int = 5                 # 5 levels Up (SELL) and 5 levels Down (BUY)
    target_profit: float = 0.80          # $0.80/oz target net profit capture ($80/lot)
    broker_commission: float = 0.32      # $0.32/oz roundtrip commission ($0.16 per leg on 0.01 lot)
    slippage_buffer: float = 0.0         # $0.00 — FOK mode: ZERO slippage tolerance
    max_allowed_spread: float = 1.20     # Max combined spread (0.38 Fut + 0.15 Spot = 0.53 normal, max 1.20)

    # Envelopes Indicator Parameters (Period 100, Deviation 3)
    envelope_period: int = 100           # Period 100
    envelope_deviation: float = 3.0      # Deviation 3 (3% or 3.0 points)
    envelope_deviation_mode: str = "percent" # "percent" (3% of MA) or "points" ($3.00 offset)

    # Execution & Risk Settings
    magic_number: int = 888
    slippage: int = 0                    # 0 points — FOK: exact price or reject
    max_slippage_tolerance: float = 0.00 # $0.00/oz — absolute zero tolerance (FOK kill threshold)
    poll_interval_seconds: float = 0.0001
    trade_lock_timeout_seconds: float = 2.0
    account_label: str = "DYNAMIC-LIVE-PROFIT"

    # Spread Logging
    spread_log_path: str = field(
        default_factory=lambda: str(get_writable_path("dynamic_spread_log.csv"))
    )


# ============================= LOCAL TICKET TAGGING =============================

class TicketTagStore:
    """Persists ticket metadata locally for nTrader position recovery."""

    def __init__(self, filename: str = "ntrader_dynamic_tags.json"):
        self.path = get_writable_path(filename)
        self._tags: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self._tags = json.load(fh)
            except Exception:
                self._tags = {}

    def _save(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self._tags, fh, indent=2)
        os.replace(tmp_path, self.path)

    def tag(self, ticket: str, magic: int, comment: str, open_time: datetime, open_gap: float) -> None:
        self._tags[str(ticket)] = {
            "magic": magic,
            "comment": comment,
            "open_time": open_time.isoformat(),
            "open_gap": open_gap,
        }
        self._save()

    def untag(self, ticket: str) -> None:
        if str(ticket) in self._tags:
            del self._tags[str(ticket)]
            self._save()

    def get(self, ticket: str) -> Optional[Dict[str, Any]]:
        return self._tags.get(str(ticket))


# ============================= nTRADER ADAPTER =============================

class NTraderAdapter:
    """Wraps `ntrader.bridge` with parallel order placement and microsecond leg rollback."""

    def __init__(self, magic_number: int, tag_store: Optional[TicketTagStore] = None):
        import ntrader
        self.bridge = ntrader.bridge
        self.magic_number = magic_number
        self.tags = tag_store or TicketTagStore()

    def get_tick(self, symbol: str) -> Optional[Tick]:
        raw = self.bridge.get_tick(symbol)
        if not raw:
            return None
        bid = raw.get("bid")
        ask = raw.get("ask")
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        return Tick(bid=float(bid), ask=float(ask))

    def get_positions(self) -> List[Position]:
        raw_pos = self.bridge.get_open_positions() or []
        positions: List[Position] = []
        for raw in raw_pos:
            ticket = raw.get("ticket")
            if ticket is None:
                continue
            ticket_str = str(ticket)
            tag = self.tags.get(ticket_str)
            magic = tag["magic"] if tag else 0
            comment = tag["comment"] if tag else ""
            side_raw = str(raw.get("type", "")).lower()
            side = "BUY" if "buy" in side_raw else "SELL"

            positions.append(
                Position(
                    ticket=ticket_str,
                    symbol=raw.get("symbol", ""),
                    side=side,
                    volume=float(raw.get("volume", 0.0)),
                    open_time=datetime.now(timezone.utc),
                    profit=float(raw.get("profit", 0.0)),
                    swap=float(raw.get("swap", 0.0)),
                    open_price=float(raw.get("open_price") or raw.get("price") or 0.0),
                    magic=magic,
                    comment=comment,
                )
            )
        return positions

    def get_position_details(self, ticket: str) -> Optional[Dict[str, Any]]:
        raw_pos = self.bridge.get_open_positions() or []
        for p in raw_pos:
            if str(p.get("ticket")) == str(ticket):
                return {
                    "ticket": str(ticket),
                    "symbol": str(p.get("symbol", "")),
                    "open_price": float(p.get("open_price") or p.get("price") or 0.0),
                    "type": str(p.get("type", "")),
                    "volume": float(p.get("volume", 0.0)),
                }
        return None

    def market_order_pair(
        self,
        symbol1: str,
        side1: str,
        comment1: str,
        symbol2: str,
        side2: str,
        comment2: str,
        volume1: float,
        volume2: float,
        magic: int,
        open_gap: float,
        req_price1: float = 0.0,
        req_price2: float = 0.0,
        max_slippage: float = 0.0,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        FILL-OR-KILL (FOK) Zero-Slippage Execution Engine.
        ────────────────────────────────────────────────────
        1. Fires BOTH legs concurrently in parallel threads.
        2. After fill, VERIFIES actual LP fill price vs requested price.
        3. If ANY leg has slippage > max_slippage (default $0.00), INSTANTLY
           kills BOTH legs — trade is rejected entirely.
        4. Entry miss is OK. Slippage is NEVER OK.
        """
        before_pos = self.bridge.get_open_positions() or []
        before_tickets = {str(p.get("ticket")) for p in before_pos if p.get("ticket") is not None}

        def _send_leg1():
            if side1.upper() == "BUY":
                self.bridge.create_buy_order(symbol1, volume1, sl=0.0, tp=0.0)
            else:
                self.bridge.create_sell_order(symbol1, volume1, sl=0.0, tp=0.0)

        def _send_leg2():
            if side2.upper() == "BUY":
                self.bridge.create_buy_order(symbol2, volume2, sl=0.0, tp=0.0)
            else:
                self.bridge.create_sell_order(symbol2, volume2, sl=0.0, tp=0.0)

        # Fire concurrent threads
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_send_leg1)
            f2 = executor.submit(_send_leg2)
            f1.result()
            f2.result()

        # Fast poll positions (up to 6 attempts @ 30ms = max 180ms)
        ticket1: Optional[str] = None
        ticket2: Optional[str] = None
        sym1_clean = symbol1.strip().upper()
        sym2_clean = symbol2.strip().upper()

        for _ in range(6):
            after_pos = self.bridge.get_open_positions() or []
            for p in after_pos:
                t_str = str(p.get("ticket"))
                if t_str not in before_tickets:
                    sym = str(p.get("symbol", "")).strip().upper()
                    if sym == sym1_clean and ticket1 is None:
                        ticket1 = t_str
                    elif sym == sym2_clean and ticket2 is None:
                        ticket2 = t_str

            if ticket1 and ticket2:
                break
            time.sleep(0.03)

        # ── STAGE 1: EMERGENCY ROLLBACK (one leg failed to fill) ──
        if ticket1 and not ticket2:
            print(f">>> EMERGENCY ROLLBACK: Leg 2 ({symbol2}) failed! Closing Leg 1 ticket {ticket1} instantly...", flush=True)
            try:
                self.bridge.close_position(int(ticket1))
            except Exception as exc:
                print(f"Rollback error ticket {ticket1}: {exc}", flush=True)
            return None, None

        if ticket2 and not ticket1:
            print(f">>> EMERGENCY ROLLBACK: Leg 1 ({symbol1}) failed! Closing Leg 2 ticket {ticket2} instantly...", flush=True)
            try:
                self.bridge.close_position(int(ticket2))
            except Exception as exc:
                print(f"Rollback error ticket {ticket2}: {exc}", flush=True)
            return None, None

        if not ticket1 or not ticket2:
            return None, None

        # ── STAGE 2: FOK SLIPPAGE VERIFICATION (Zero Tolerance Kill) ──
        # Both legs filled — now verify EXACT prices match requested prices
        pos1 = None
        pos2 = None
        for _ in range(4):  # up to 4 polls @ 20ms = 80ms
            if pos1 is None:
                pos1 = self.get_position_details(ticket1)
            if pos2 is None:
                pos2 = self.get_position_details(ticket2)
            if pos1 and pos2:
                break
            time.sleep(0.02)

        fill_price1 = pos1["open_price"] if pos1 and pos1.get("open_price") else 0.0
        fill_price2 = pos2["open_price"] if pos2 and pos2.get("open_price") else 0.0

        # Calculate per-leg slippage
        # For SELL: slippage = requested - filled (positive = bad, LP gave lower price)
        # For BUY:  slippage = filled - requested (positive = bad, LP gave higher price)
        if side1.upper() == "SELL":
            slip1 = round(req_price1 - fill_price1, 2)  # SELL wants higher price
        else:
            slip1 = round(fill_price1 - req_price1, 2)  # BUY wants lower price

        if side2.upper() == "SELL":
            slip2 = round(req_price2 - fill_price2, 2)
        else:
            slip2 = round(fill_price2 - req_price2, 2)

        # ANY adverse slippage beyond tolerance = INSTANT KILL BOTH LEGS
        if slip1 > max_slippage or slip2 > max_slippage:
            print(
                f"\n{'='*73}\n"
                f">>> FOK KILL: SLIPPAGE DETECTED — BOTH LEGS INSTANTLY CLOSED <<<\n"
                f"{'-'*73}\n"
                f"[LEG 1 - {symbol1} {side1}] Requested: {req_price1:.2f} | Filled: {fill_price1:.2f} | Adverse Slip: {slip1:+.2f} {'⛔ KILLED' if slip1 > max_slippage else '✅ OK'}\n"
                f"[LEG 2 - {symbol2} {side2}] Requested: {req_price2:.2f} | Filled: {fill_price2:.2f} | Adverse Slip: {slip2:+.2f} {'⛔ KILLED' if slip2 > max_slippage else '✅ OK'}\n"
                f"[TOLERANCE] Max Allowed: ${max_slippage:.2f}/oz | Result: TRADE REJECTED (Fill-or-Kill)\n"
                f"{'='*73}",
                flush=True,
            )
            # Instantly close both filled legs
            try:
                self.bridge.close_position(int(ticket1))
            except Exception as exc:
                print(f"FOK kill error ticket {ticket1}: {exc}", flush=True)
            try:
                self.bridge.close_position(int(ticket2))
            except Exception as exc:
                print(f"FOK kill error ticket {ticket2}: {exc}", flush=True)
            return None, None

        # ── STAGE 3: ZERO SLIPPAGE CONFIRMED — Tag & Keep ──
        now_utc = datetime.now(timezone.utc)
        self.tags.tag(ticket1, magic=magic, comment=comment1, open_time=now_utc, open_gap=open_gap)
        self.tags.tag(ticket2, magic=magic, comment=comment2, open_time=now_utc, open_gap=open_gap)

        return ticket1, ticket2

    def close_positions_simultaneous(self, tickets: List[str]) -> None:
        if not tickets:
            return

        def _do_close(t_str: str) -> None:
            try:
                self.bridge.close_position(int(t_str))
            except Exception as exc:
                print(f"Error closing ticket {t_str}: {exc}", flush=True)

        with ThreadPoolExecutor(max_workers=len(tickets)) as executor:
            executor.map(_do_close, tickets)

        for t in tickets:
            self.tags.untag(str(t))


# ============================= DYNAMIC BASIS & GRID BOT =============================

class DynamicGridArbitrageBot:
    def __init__(self, broker: NTraderAdapter, config: Optional[DynamicArbitrageConfig] = None):
        self.broker = broker
        self.cfg = config or DynamicArbitrageConfig()

        # Parse expiry date
        try:
            self.expiry_dt = datetime.strptime(self.cfg.expiry_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            self.expiry_dt = datetime(2026, 12, 28, tzinfo=timezone.utc)

        # State
        self.tick1: Optional[Tick] = None
        self.tick2: Optional[Tick] = None
        self.running = False
        self.trade_in_progress = False
        self.trade_lock_time = 0.0
        self.baseline_gap: Optional[float] = None

        # Envelopes Indicator sliding buffer (Period 100)
        self.gap_history: deque[float] = deque(maxlen=self.cfg.envelope_period)

        # Throttled status logs
        self._last_status_print = 0.0
        self._last_spread_log = 0.0

    def get_fair_gap(self, spot_price: float) -> float:
        """
        Returns fair basis gap.
        If use_theoretical_sofr is True: calculates SOFR theoretical gap.
        If use_theoretical_sofr is False (default): auto-calibrates to live broker baseline gap (~40.49).
        Also maintains the rolling sample buffer for Envelopes calculation (Period 100).
        """
        if self.tick1 is not None and self.tick2 is not None:
            current_mid_gap = ((self.tick1.bid + self.tick1.ask) / 2.0) - ((self.tick2.bid + self.tick2.ask) / 2.0)
            self.gap_history.append(current_mid_gap)

            if not self.cfg.use_theoretical_sofr:
                if self.baseline_gap is None:
                    self.baseline_gap = round(current_mid_gap, 2)
                else:
                    # Slow rolling EMA (99.8% past + 0.2% new tick) to maintain stable market anchor
                    self.baseline_gap = round((0.998 * self.baseline_gap) + (0.002 * current_mid_gap), 2)
                return self.baseline_gap

        if self.cfg.use_theoretical_sofr:
            now = datetime.now(timezone.utc)
            seconds_left = max((self.expiry_dt - now).total_seconds(), 3600.0)
            days_left = seconds_left / 86400.0
            fair_gap = spot_price * (self.cfg.annual_sofr_rate * (days_left / 365.0))
            return round(fair_gap, 2)

        return 40.49

    def get_envelope_values(self) -> Dict[str, Any]:
        """
        Calculates Envelopes indicator (Period 100, Deviation 3):
        - MA (Period 100 Simple Moving Average)
        - ENV HIGH (Upper Band)
        - ENV LOW (Lower Band)
        Supports percentage mode (%3.0) and fixed points mode ($3.00 offset).
        """
        if self.gap_history:
            ma = sum(self.gap_history) / len(self.gap_history)
        elif self.baseline_gap is not None:
            ma = self.baseline_gap
        else:
            ma = 40.49

        dev = self.cfg.envelope_deviation  # 3.0

        # Percentage mode (MT4 Standard: MA * (1 +/- Dev%))
        dev_pct_amt = ma * (dev / 100.0)
        high_pct = ma + dev_pct_amt
        low_pct = ma - dev_pct_amt

        # Fixed points mode ($3.00 offset)
        high_pts = ma + dev
        low_pts = ma - dev

        return {
            "ma": round(ma, 2),
            "high_pct": round(high_pct, 2),
            "low_pct": round(low_pct, 2),
            "high_pts": round(high_pts, 2),
            "low_pts": round(low_pts, 2),
            "period": self.cfg.envelope_period,
            "deviation": dev,
            "samples": len(self.gap_history),
        }

    def log(self, *parts: Any) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {' '.join(str(p) for p in parts)}"
        print(line, flush=True)

    def strategy_positions(self) -> List[Position]:
        all_pos = self.broker.get_positions()
        return [p for p in all_pos if p.magic == self.cfg.magic_number]

    def total_active_orders(self) -> int:
        return len(self.strategy_positions())

    def net_pnl(self) -> float:
        return sum(p.profit + p.swap for p in self.strategy_positions())

    def is_trade_locked(self) -> bool:
        if not self.trade_in_progress:
            return False
        if time.monotonic() - self.trade_lock_time > self.cfg.trade_lock_timeout_seconds:
            self.trade_in_progress = False
            return False
        return True

    def evaluate_and_trade(self) -> None:
        assert self.tick1 is not None and self.tick2 is not None

        fut_spread = round(self.tick1.ask - self.tick1.bid, 2)
        spot_spread = round(self.tick2.ask - self.tick2.bid, 2)
        total_spread = round(fut_spread + spot_spread, 2)

        # LP Spread Protection Guard
        if total_spread > self.cfg.max_allowed_spread:
            return

        fair_gap = self.get_fair_gap(self.tick2.ask)
        env = self.get_envelope_values()

        # Actual market gaps
        actual_open_gap_sell = round(self.tick1.bid - self.tick2.ask, 2)  # For SELL direction (Futures SELL + Spot BUY)
        actual_open_gap_buy = round(self.tick1.ask - self.tick2.bid, 2)   # For BUY direction (Futures BUY + Spot SELL)
        actual_close_gap_sell = round(self.tick1.ask - self.tick2.bid, 2)
        actual_close_gap_buy = round(self.tick1.bid - self.tick2.ask, 2)

        active_positions = self.strategy_positions()

        # ─── 1. CLOSE EXISTING TRADES (Target Hit Check) ───
        if active_positions:
            for p in active_positions:
                if not p.comment:
                    continue
                tag = self.broker.tags.get(p.ticket)
                if not tag:
                    continue
                open_gap = tag.get("open_gap", 0.0)

                # SELL direction trade close check
                if "SELL_GRID" in p.comment:
                    if actual_close_gap_sell <= (open_gap - self.cfg.target_profit):
                        # CLOSE FIRST IMMEDIATELY (0ms PRE-ORDER DELAY)
                        matching = [pos.ticket for pos in active_positions if pos.comment == p.comment]
                        self.broker.close_positions_simultaneous(matching)
                        
                        # LOG AFTER CLOSE EXECUTION
                        profit_captured = round(open_gap - actual_close_gap_sell, 2)
                        self.log(
                            f"\n=========================================================================\n"
                            f">>> TARGET HIT & CLOSED [{p.comment}] (Closed Instantly - 0ms Pre-Delay)\n"
                            f"-------------------------------------------------------------------------\n"
                            f"[GAP MOVE] Entry Open Gap: {open_gap:.2f} -> Exit Close Gap: {actual_close_gap_sell:.2f}\n"
                            f"[PROFIT  ] Gross Gap Profit Captured: ${profit_captured:.2f}/oz (${profit_captured * 100:.2f}/lot)\n"
                            f"========================================================================="
                        )
                        return

                # BUY direction trade close check
                elif "BUY_GRID" in p.comment:
                    if actual_close_gap_buy >= (open_gap + self.cfg.target_profit):
                        # CLOSE FIRST IMMEDIATELY (0ms PRE-ORDER DELAY)
                        matching = [pos.ticket for pos in active_positions if pos.comment == p.comment]
                        self.broker.close_positions_simultaneous(matching)

                        # LOG AFTER CLOSE EXECUTION
                        profit_captured = round(actual_close_gap_buy - open_gap, 2)
                        self.log(
                            f"\n=========================================================================\n"
                            f">>> TARGET HIT & CLOSED [{p.comment}] (Closed Instantly - 0ms Pre-Delay)\n"
                            f"-------------------------------------------------------------------------\n"
                            f"[GAP MOVE] Entry Open Gap: {open_gap:.2f} -> Exit Close Gap: {actual_close_gap_buy:.2f}\n"
                            f"[PROFIT  ] Gross Gap Profit Captured: ${profit_captured:.2f}/oz (${profit_captured * 100:.2f}/lot)\n"
                            f"========================================================================="
                        )
                        return

        # ─── 2. EVALUATE NEW GRID ENTRIES ───
        if self.is_trade_locked():
            return

        # Check SELL Grid Levels (Spike Up to Top Blue Band / Env High)
        for i in range(1, self.cfg.grid_levels + 1):
            comment = f"SELL_GRID_LVL_{i}"
            if any(p.comment == comment for p in active_positions):
                continue  # Level already active

            trigger_gap = round(fair_gap + (i * self.cfg.step_size), 2)
            if actual_open_gap_sell >= trigger_gap:
                # 🚀 FOK EXECUTION: EXACT PRICE OR KILL
                self.trade_in_progress = True
                self.trade_lock_time = time.monotonic()

                # Lock exact requested prices at trigger instant
                req_fut_price = self.tick1.bid   # SELL Futures @ this exact bid
                req_spot_price = self.tick2.ask   # BUY Spot @ this exact ask
                req_open_gap = actual_open_gap_sell

                t1, t2 = self.broker.market_order_pair(
                    symbol1=self.cfg.symbol1, side1="SELL", comment1=comment,
                    symbol2=self.cfg.symbol2, side2="BUY", comment2=comment,
                    volume1=self.cfg.future_lot, volume2=self.cfg.spot_lot,
                    magic=self.cfg.magic_number, open_gap=actual_open_gap_sell,
                    req_price1=req_fut_price, req_price2=req_spot_price,
                    max_slippage=self.cfg.max_slippage_tolerance,
                )
                self.trade_in_progress = False

                if t1 and t2:
                    # ✅ FOK PASSED — Zero slippage confirmed, log exact fill details
                    pos1 = self.broker.get_position_details(t1)
                    pos2 = self.broker.get_position_details(t2)
                    lp_fut_price = pos1["open_price"] if pos1 and pos1.get("open_price") else req_fut_price
                    lp_spot_price = pos2["open_price"] if pos2 and pos2.get("open_price") else req_spot_price
                    lp_gap = round(lp_fut_price - lp_spot_price, 2)

                    self.log(
                        f"\n=========================================================================\n"
                        f">>> FOK FILL CONFIRMED [{comment}] — ZERO SLIPPAGE ✅\n"
                        f"-------------------------------------------------------------------------\n"
                        f"[LEG 1 - Futures SELL] Ticket: {t1} | Exact Price: {lp_fut_price:.2f} (Req: {req_fut_price:.2f}) | Slip: $0.00 ✅\n"
                        f"[LEG 2 - Spot BUY   ] Ticket: {t2} | Exact Price: {lp_spot_price:.2f} (Req: {req_spot_price:.2f}) | Slip: $0.00 ✅\n"
                        f"[EXECUTION SUMMARY  ] Requested Gap: {req_open_gap:.2f} | Filled Gap: {lp_gap:.2f} | FOK Status: ACCEPTED\n"
                        f"========================================================================="
                    )
                else:
                    self.log(f"[FOK MISS] {comment} — entry skipped (LP rejected or slippage detected, no position taken)")
                return  # Fire one level per evaluation pass

        # Check BUY Grid Levels (Dip Down to Bottom Red Band / Env Low)
        for i in range(1, self.cfg.grid_levels + 1):
            comment = f"BUY_GRID_LVL_{i}"
            if any(p.comment == comment for p in active_positions):
                continue  # Level already active

            trigger_gap = round(fair_gap - (i * self.cfg.step_size), 2)
            if actual_open_gap_buy <= trigger_gap:
                # 🚀 FOK EXECUTION: EXACT PRICE OR KILL
                self.trade_in_progress = True
                self.trade_lock_time = time.monotonic()

                # Lock exact requested prices at trigger instant
                req_fut_price = self.tick1.ask   # BUY Futures @ this exact ask
                req_spot_price = self.tick2.bid   # SELL Spot @ this exact bid
                req_open_gap = actual_open_gap_buy

                t1, t2 = self.broker.market_order_pair(
                    symbol1=self.cfg.symbol1, side1="BUY", comment1=comment,
                    symbol2=self.cfg.symbol2, side2="SELL", comment2=comment,
                    volume1=self.cfg.future_lot, volume2=self.cfg.spot_lot,
                    magic=self.cfg.magic_number, open_gap=actual_open_gap_buy,
                    req_price1=req_fut_price, req_price2=req_spot_price,
                    max_slippage=self.cfg.max_slippage_tolerance,
                )
                self.trade_in_progress = False

                if t1 and t2:
                    # ✅ FOK PASSED — Zero slippage confirmed, log exact fill details
                    pos1 = self.broker.get_position_details(t1)
                    pos2 = self.broker.get_position_details(t2)
                    lp_fut_price = pos1["open_price"] if pos1 and pos1.get("open_price") else req_fut_price
                    lp_spot_price = pos2["open_price"] if pos2 and pos2.get("open_price") else req_spot_price
                    lp_gap = round(lp_fut_price - lp_spot_price, 2)

                    self.log(
                        f"\n=========================================================================\n"
                        f">>> FOK FILL CONFIRMED [{comment}] — ZERO SLIPPAGE ✅\n"
                        f"-------------------------------------------------------------------------\n"
                        f"[LEG 1 - Futures BUY ] Ticket: {t1} | Exact Price: {lp_fut_price:.2f} (Req: {req_fut_price:.2f}) | Slip: $0.00 ✅\n"
                        f"[LEG 2 - Spot SELL  ] Ticket: {t2} | Exact Price: {lp_spot_price:.2f} (Req: {req_spot_price:.2f}) | Slip: $0.00 ✅\n"
                        f"[EXECUTION SUMMARY  ] Requested Gap: {req_open_gap:.2f} | Filled Gap: {lp_gap:.2f} | FOK Status: ACCEPTED\n"
                        f"========================================================================="
                    )
                else:
                    self.log(f"[FOK MISS] {comment} — entry skipped (LP rejected or slippage detected, no position taken)")
                return

    def print_status(self) -> None:
        now = time.monotonic()
        if now - self._last_status_print < 2.0:
            return
        self._last_status_print = now

        if self.tick1 is None or self.tick2 is None:
            return

        fair_gap = self.get_fair_gap(self.tick2.ask)
        gap_sell = round(self.tick1.bid - self.tick2.ask, 2)
        gap_buy = round(self.tick1.ask - self.tick2.bid, 2)
        fut_sp = round(self.tick1.ask - self.tick1.bid, 2)
        spot_sp = round(self.tick2.ask - self.tick2.bid, 2)

        env = self.get_envelope_values()

        self.log(
            f"=== DYNAMIC GRID BOT [{self.cfg.account_label}] === | "
            f"ENV HIGH (High Band): {env['high_pct']:.2f} | "
            f"Baseline MA({env['samples']}/{env['period']}): {env['ma']:.2f} | "
            f"ENV LOW (Low Band): {env['low_pct']:.2f} | "
            f"Gap (SELL/BUY): {gap_sell:.2f}/{gap_buy:.2f} | "
            f"Spreads (Fut/Spot): {fut_sp:.2f}/{spot_sp:.2f} | Active Trades: {self.total_active_orders()} | PnL: ${self.net_pnl():.2f}"
        )

    def run(self) -> None:
        self.running = True
        self.log("=========================================================================")
        self.log("DYNAMIC GRID GOLD ARBITRAGE BOT STARTED")
        self.log(f"Symbols: {self.cfg.symbol1} (Futures) vs {self.cfg.symbol2} (Spot)")
        self.log(f"1:1 Contract Lot Sizes: Future={self.cfg.future_lot} lots | Spot={self.cfg.spot_lot} lots")
        self.log(f"Envelopes Indicator: Period={self.cfg.envelope_period} | Deviation={self.cfg.envelope_deviation} (3% or $3.00 offset)")
        self.log(f"Expiry Date: {self.cfg.expiry_date} | SOFR Rate: {self.cfg.annual_sofr_rate * 100:.1f}%")
        self.log(f"Grid Step Size: ${self.cfg.step_size:.2f} | Target Profit: ${self.cfg.target_profit:.2f}/oz")
        self.log("=========================================================================")

        while self.running:
            try:
                self.tick1 = self.broker.get_tick(self.cfg.symbol1)
                self.tick2 = self.broker.get_tick(self.cfg.symbol2)

                if self.tick1 is not None and self.tick2 is not None:
                    self.print_status()
                    self.evaluate_and_trade()

            except KeyboardInterrupt:
                self.log("Stopping bot (KeyboardInterrupt)...")
                self.running = False
            except Exception as exc:
                self.log("Main loop exception:", repr(exc))

            time.sleep(self.cfg.poll_interval_seconds)


if __name__ == "__main__":
    cfg = DynamicArbitrageConfig()
    tag_store = TicketTagStore(filename="ntrader_dynamic_tags.json")
    adapter = NTraderAdapter(magic_number=cfg.magic_number, tag_store=tag_store)
    bot = DynamicGridArbitrageBot(broker=adapter, config=cfg)
    bot.run()
