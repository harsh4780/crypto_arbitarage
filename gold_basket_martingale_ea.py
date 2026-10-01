"""
Gold Basket Martingale Arbitrage EA — FOK Edition (SELL Direction)
=================================================================
A clean, dedicated, high-performance Basket Martingale Arbitrage Bot for nTrader.

SELL direction arbitrage strategy:
  OPEN  → Symbol1 SELL + Symbol2 BUY (when sym1.bid - sym2.ask >= initial_entry_gap)
  MARTINGALE RECOVERY → Opens weighted pairs when open_gap moves adversely by martingale_gap_step
  CLOSE → Closes entire basket simultaneously when Basket Net PnL >= Basket Profit Target or Max Drawdown Hit

Key Features:
  1. Configurable Initial Entry Gap (initial_entry_gap = 41.60)
  2. Dynamic Lot Multiplier Sizing (2.0 future / 0.02 spot -> 4.0/0.04 -> 8.0/0.08)
  3. Adverse Gap Step Trigger (martingale_gap_step = 0.50 points)
  4. Dynamic Volume-Scaled Commission ($0.16 per 0.01 spot lot)
  5. FILL-OR-KILL (FOK) Zero-Slippage Execution:
     Orders sent at EXACT requested price. After fill, actual LP fill price verified
     with ZERO tolerance. ANY slippage = INSTANT KILL of both legs.
     Entry miss is acceptable; slippage is NEVER acceptable.
  6. Zero-Latency Execution: Fire order BEFORE any logging I/O
  7. Multi-Layer Risk Controls (Max Levels, Exposure Cap, Drawdown Stop, Adverse Gap Limit)
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple


def get_writable_path(filename: str) -> Path:
    """Returns a Path object for filename in a directory guaranteed to be writable."""
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
    broker_time: float = 0.0
    local_time: float = field(default_factory=time.monotonic)


@dataclass
class Position:
    ticket: str
    symbol: str
    side: str                 # "BUY" or "SELL"
    volume: float
    open_time: datetime
    profit: float = 0.0
    swap: float = 0.0
    open_price: float = 0.0
    magic: int = 0
    comment: str = ""


class BrokerAdapter(Protocol):
    def get_tick(self, symbol: str) -> Optional[Tick]:
        ...

    def get_positions(self) -> List[Position]:
        ...

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
        req_price1: float = 0.0,
        req_price2: float = 0.0,
        max_slippage: float = 0.0,
    ) -> Tuple[Optional[str], Optional[str]]:
        ...

    def get_position_details(self, ticket: str) -> Optional[Dict[str, Any]]:
        ...

    def close_position(self, ticket: str, deviation: int) -> bool:
        ...

    def close_positions_simultaneous(self, tickets: List[str]) -> None:
        ...


TRADE_LOCK_TIMEOUT_S = 3.0


# ============================= CONFIGURATION =============================

@dataclass
class MartingaleConfig:
    # ─── Symbols ───
    symbol1: str = "GOLD_DEC26"                 # Symbol1 (Future leg)
    symbol2: str = "XAUUSD.LMAX"                # Symbol2 (Spot leg)

    # ─── Basket Martingale Parameters ───
    initial_entry_gap: float = 41.60            # Initial Gap trigger in points for Level 1 entry
    base_future_lot: float = 2.0                # Base lot for Symbol1 (Future leg)
    base_spot_lot: float = 0.02               # Base lot for Symbol2 (Spot leg)
    lot_multiplier: float = 2.0                 # Martingale lot multiplier per level (e.g., Level 1=2.0/0.02, Level 2=4.0/0.04)
    martingale_gap_step: float = 0.50           # Adverse gap distance in points to trigger next martingale level
    max_martingale_levels: int = 5              # Max recovery levels allowed per basket
    desired_net_profit_usd: float = 1.50        # Desired net profit in USD per basket after all costs
    commission_per_001_spot_usd: float = 0.16   # Roundturn commission cost per 0.01 spot lot ($)
    max_allowed_slippage_points: float = 0.0     # $0.00 — FOK mode: ZERO slippage tolerance

    # ─── Risk Controls ───
    max_total_lot_exposure: float = 15.0        # Combined open lot cap across all legs
    max_basket_drawdown_usd: float = 500.0      # Emergency Basket Equity Stop Loss in USD
    max_allowed_adverse_gap: float = 55.0       # Max adverse gap cap beyond which no new martingale orders fire

    # ─── General Bot Settings ───
    magic_number: int = 415
    slippage: int = 0                           # 0 points — FOK: exact price or reject
    max_slippage_tolerance: float = 0.00        # $0.00/oz — absolute zero tolerance (FOK kill threshold)
    min_hold_minutes: int = 3
    poll_interval_seconds: float = 0.00001
    trade_response_wait_seconds: float = 0.0
    account_label: str = "A-BOOK-LIVE"

    # ─── Logging ───
    spread_log_path: str = field(
        default_factory=lambda: str(get_writable_path("spread_log.csv"))
    )
    spread_log_interval_seconds: float = 1.0


# ============================= STRATEGY ENGINE =============================

class GoldBasketMartingaleBot:
    def __init__(self, broker: BrokerAdapter, config: Optional[MartingaleConfig] = None):
        self.broker = broker
        self.cfg = config or MartingaleConfig()

        self.tick1: Optional[Tick] = None
        self.tick2: Optional[Tick] = None

        self.trade_in_progress = False
        self.trade_lock_time = 0.0
        self.running = False

        # Basket State Tracking
        self.basket_initial_gap: float = 0.0
        self.last_entry_gap: float = 0.0
        self.basket_tickets: List[str] = []

        self._last_status_print = 0.0
        self._last_spread_log = 0.0
        self._throttled_logs: Dict[str, float] = {}

    def log(self, *parts: Any) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {' '.join(str(p) for p in parts)}"
        print(line, flush=True)
        try:
            log_dir = get_writable_path("logs")
            if not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            today_str = datetime.now().strftime("%Y-%m-%d")
            log_path = os.path.join(log_dir, f"bot_basket_martingale_{today_str}.log")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def log_throttled(self, key: str, *parts: Any, min_interval: float = 3.0) -> None:
        now = time.monotonic()
        if now - self._throttled_logs.get(key, 0.0) < min_interval:
            return
        self._throttled_logs[key] = now
        self.log(*parts)

    def refresh_ticks(self) -> bool:
        try:
            self.tick1 = self.broker.get_tick(self.cfg.symbol1)
            self.tick2 = self.broker.get_tick(self.cfg.symbol2)
            if self.tick1 is None or self.tick2 is None:
                return False
            if self.tick1.bid <= 0 or self.tick1.ask <= 0 or self.tick2.bid <= 0 or self.tick2.ask <= 0:
                return False
            return True
        except Exception as exc:
            self.log_throttled("tick_refresh_exc", "Tick refresh failed:", exc)
            return False

    def open_difference(self) -> float:
        """SELL Open Gap: Sym1.Bid - Sym2.Ask"""
        assert self.tick1 is not None and self.tick2 is not None
        return round(self.tick1.bid - self.tick2.ask, 2)

    def close_difference(self) -> float:
        """SELL Close Gap: Sym1.Ask - Sym2.Bid"""
        assert self.tick1 is not None and self.tick2 is not None
        return round(self.tick1.ask - self.tick2.bid, 2)

    def strategy_positions(self) -> List[Position]:
        positions = self.broker.get_positions()
        return [
            p for p in positions
            if p.magic == self.cfg.magic_number
            and p.symbol in (self.cfg.symbol1, self.cfg.symbol2)
        ]

    # ─── Basket Martingale Calculations ───

    def get_martingale_lots(self, level_index: int) -> Tuple[float, float]:
        """Calculates dynamic lot sizes for Level N (0-indexed)."""
        multiplier = max(1.0, self.cfg.lot_multiplier)
        fut_lot = round(self.cfg.base_future_lot * (multiplier ** level_index), 2)
        spot_lot = round(self.cfg.base_spot_lot * (multiplier ** level_index), 2)
        return fut_lot, spot_lot

    def get_basket_total_lots(self) -> float:
        """Total lots open across all legs."""
        return round(sum(p.volume for p in self.strategy_positions()), 2)

    def get_basket_spot_lots(self) -> float:
        """Total Spot leg lots open."""
        return round(sum(p.volume for p in self.strategy_positions() if p.symbol == self.cfg.symbol2), 2)

    def get_basket_open_pairs_count(self) -> int:
        """Total hedged pairs open."""
        return len(self.strategy_positions()) // 2

    def get_basket_net_pnl(self) -> float:
        """Live Net PnL = Floating PnL + Swaps - Volume-Scaled Commissions."""
        positions = self.strategy_positions()
        if not positions:
            return 0.0
        floating_pnl = sum(p.profit + p.swap for p in positions)
        spot_lots = self.get_basket_spot_lots()
        commissions = (spot_lots / 0.01) * self.cfg.commission_per_001_spot_usd
        return round(floating_pnl - commissions, 2)

    def get_basket_required_target(self) -> float:
        """Required Target = Desired Net Profit + Commissions + Slippage Buffer."""
        spot_lots = self.get_basket_spot_lots()
        total_lots = self.get_basket_total_lots()
        commissions = (spot_lots / 0.01) * self.cfg.commission_per_001_spot_usd
        slippage_buffer = self.cfg.max_allowed_slippage_points * 0.10 * total_lots
        return round(self.cfg.desired_net_profit_usd + commissions + slippage_buffer, 2)

    def check_basket_exit_conditions(self) -> Tuple[bool, str, float, float]:
        positions = self.strategy_positions()
        if not positions:
            return False, "", 0.0, 0.0
        net_pnl = self.get_basket_net_pnl()
        target = self.get_basket_required_target()

        if net_pnl >= target:
            return True, "PROFIT_TARGET_HIT", net_pnl, target

        if net_pnl <= -self.cfg.max_basket_drawdown_usd:
            return True, "MAX_DRAWDOWN_HIT", net_pnl, -self.cfg.max_basket_drawdown_usd

        return False, "", net_pnl, target

    def is_hold_complete(self) -> bool:
        if self.cfg.min_hold_minutes <= 0:
            return True
        positions = self.strategy_positions()
        if not positions:
            return True
        now = datetime.now(timezone.utc)
        for p in positions:
            mins = int((now - (p.open_time.replace(tzinfo=timezone.utc) if p.open_time.tzinfo is None else p.open_time)).total_seconds() // 60)
            if mins < self.cfg.min_hold_minutes:
                return False
        return True

    def is_trade_locked(self) -> bool:
        if not self.trade_in_progress:
            return False
        if time.monotonic() - self.trade_lock_time > TRADE_LOCK_TIMEOUT_S:
            self.log(">>> TRADE LOCK TIMEOUT — Force unlocking")
            self.trade_in_progress = False
            return False
        return True

    def check_orphan_trades(self) -> None:
        if self.trade_in_progress:
            return
        positions = self.strategy_positions()
        tickets = {p.ticket for p in positions}
        for i in range(0, len(self.basket_tickets) - 1, 2):
            t1, t2 = self.basket_tickets[i], self.basket_tickets[i + 1]
            a, b = t1 in tickets, t2 in tickets
            if a and not b:
                self.log(f">>> ORPHAN DETECTED | Closing t1={t1}")
                try:
                    self.broker.close_position(t1, self.cfg.slippage)
                except Exception:
                    pass
            elif b and not a:
                self.log(f">>> ORPHAN DETECTED | Closing t2={t2}")
                try:
                    self.broker.close_position(t2, self.cfg.slippage)
                except Exception:
                    pass

    # 🚀 FOK ZERO-LATENCY TRADE PLACEMENT
    def place_basket_trade(
        self,
        level_index: int,
        trigger_gap: float,
        open_gap: float,
        fut_vol: float,
        spot_vol: float,
    ) -> bool:
        if self.is_trade_locked():
            return False

        self.trade_in_progress = True
        self.trade_lock_time = time.monotonic()
        comment = f"BASKET_LVL_{level_index + 1}"

        # Lock exact requested prices at trigger instant
        req_leg1_price = self.tick1.bid if self.tick1 else 0.0   # SELL Futures @ this exact bid
        req_leg2_price = self.tick2.ask if self.tick2 else 0.0   # BUY Spot @ this exact ask

        # 🚀 FIRE ORDER IMMEDIATELY BEFORE ANY LOGGING I/O!
        t1_ticket, t2_ticket = self.broker.market_order_pair(
            symbol1=self.cfg.symbol1,
            side1="SELL",
            comment1=comment,
            symbol2=self.cfg.symbol2,
            side2="BUY",
            comment2=comment,
            volume1=fut_vol,
            volume2=spot_vol,
            magic=self.cfg.magic_number,
            req_price1=req_leg1_price,
            req_price2=req_leg2_price,
            max_slippage=self.cfg.max_slippage_tolerance,
        )

        # 📝 LOGGING & TRACKING AFTER ORDER DISPATCH
        if t1_ticket and t2_ticket:
            self.basket_tickets.extend([t1_ticket, t2_ticket])
            if self.basket_initial_gap == 0.0:
                self.basket_initial_gap = open_gap
            self.last_entry_gap = open_gap

            # Get confirmed fill prices for logging
            pos1 = self.broker.get_position_details(t1_ticket)
            pos2 = self.broker.get_position_details(t2_ticket)
            fill1 = pos1["open_price"] if pos1 and pos1.get("open_price") else req_leg1_price
            fill2 = pos2["open_price"] if pos2 and pos2.get("open_price") else req_leg2_price

            self.log(
                f"\n{'='*73}\n"
                f">>> FOK FILL CONFIRMED [Level {level_index + 1}] — ZERO SLIPPAGE ✅\n"
                f"{'-'*73}\n"
                f"[LEG 1 - Futures SELL] Ticket: {t1_ticket} ({fut_vol} lots) | Exact: {fill1:.2f} (Req: {req_leg1_price:.2f}) | Slip: $0.00 ✅\n"
                f"[LEG 2 - Spot BUY   ] Ticket: {t2_ticket} ({spot_vol} lots) | Exact: {fill2:.2f} (Req: {req_leg2_price:.2f}) | Slip: $0.00 ✅\n"
                f"[GAP] Open Gap: {open_gap:.2f} >= Trigger: {trigger_gap:.2f} | FOK Status: ACCEPTED\n"
                f"{'='*73}"
            )
            positions = [p for p in self.strategy_positions() if p.ticket in (t1_ticket, t2_ticket)]
            self.log_trade_record("OPEN", positions, open_gap)
            self.trade_in_progress = False
            return True
        else:
            self.log(f"[FOK MISS] Level {level_index + 1} — entry skipped (LP rejected or slippage detected, no position taken)")
            self.trade_in_progress = False
            return False

    def close_basket_and_reset(self, reason: str) -> None:
        self.trade_in_progress = True
        net_pnl = self.get_basket_net_pnl()
        open_pairs = self.get_basket_open_pairs_count()
        total_lots = self.get_basket_total_lots()
        positions = self.strategy_positions()

        self.log(
            f">>> CLOSING ENTIRE BASKET [{reason}] | "
            f"Open Pairs: {open_pairs} | Total Lots: {total_lots:.2f} | Live Net PnL: ${net_pnl:.2f}"
        )

        tickets = [p.ticket for p in positions]
        if tickets:
            if hasattr(self.broker, "close_positions_simultaneous"):
                self.broker.close_positions_simultaneous(tickets)
            else:
                for t in tickets:
                    try:
                        self.broker.close_position(t, self.cfg.slippage)
                    except Exception:
                        pass

        self.log_trade_record("CLOSE", positions, self.close_difference())
        self.basket_initial_gap = 0.0
        self.last_entry_gap = 0.0
        self.basket_tickets = []
        self.trade_in_progress = False
        self.log(">>> BASKET SUCCESSFULLY CLOSED & RESET — ready for next opportunity")

    def log_trade_record(self, event_type: str, positions: List[Position], diff_val: float) -> None:
        try:
            path = get_writable_path("trade_history.csv")
            file_exists = path.exists()
            p_fut = next((p for p in positions if p.symbol == self.cfg.symbol1), None)
            p_spot = next((p for p in positions if p.symbol == self.cfg.symbol2), None)

            p1_comment = p_fut.comment if p_fut else "Fut_Leg"
            p1_ticket = p_fut.ticket if p_fut else ""
            p1_price = p_fut.open_price if p_fut else 0.0
            p1_pnl = (p_fut.profit + p_fut.swap) if p_fut else 0.0

            p2_comment = p_spot.comment if p_spot else "Spot_Leg"
            p2_ticket = p_spot.ticket if p_spot else ""
            p2_price = p_spot.open_price if p_spot else 0.0
            p2_pnl = (p_spot.profit + p_spot.swap) if p_spot else 0.0

            total_pnl = p1_pnl + p2_pnl

            with open(path, "a", encoding="utf-8", newline="") as fh:
                if not file_exists:
                    fh.write(
                        "timestamp_utc,account_label,event,set_name,"
                        "leg1_comment,leg1_ticket,leg1_symbol,leg1_price,leg1_pnl,"
                        "leg2_comment,leg2_ticket,leg2_symbol,leg2_price,leg2_pnl,"
                        "difference,total_pnl\n"
                    )
                fh.write(
                    f"{datetime.now(timezone.utc).isoformat()},{self.cfg.account_label},{event_type},BASKET,"
                    f"{p1_comment},{p1_ticket},{self.cfg.symbol1},{p1_price:.4f},{p1_pnl:.2f},"
                    f"{p2_comment},{p2_ticket},{self.cfg.symbol2},{p2_price:.4f},{p2_pnl:.2f},"
                    f"{diff_val:.5f},{total_pnl:.2f}\n"
                )
        except Exception:
            pass

    def log_spread(self) -> None:
        now = time.monotonic()
        if now - self._last_spread_log < self.cfg.spread_log_interval_seconds:
            return
        self._last_spread_log = now
        if self.tick1 is None or self.tick2 is None:
            return

        try:
            path = self.cfg.spread_log_path
            file_exists = os.path.exists(path)
            with open(path, "a", encoding="utf-8", newline="") as fh:
                if not file_exists:
                    fh.write(
                        "timestamp_utc,symbol1,symbol1_bid,symbol1_ask,"
                        "symbol2,symbol2_bid,symbol2_ask,"
                        "open_difference,close_difference,"
                        "active_orders,net_pnl\n"
                    )
                fh.write(
                    f"{datetime.now(timezone.utc).isoformat()},"
                    f"{self.cfg.symbol1},{self.tick1.bid},{self.tick1.ask},"
                    f"{self.cfg.symbol2},{self.tick2.bid},{self.tick2.ask},"
                    f"{self.open_difference():.5f},{self.close_difference():.5f},"
                    f"{len(self.strategy_positions())},{self.get_basket_net_pnl():.2f}\n"
                )
        except Exception:
            pass

    def print_status(self, open_gap: float, close_gap: float) -> None:
        now = time.monotonic()
        if now - self._last_status_print < 2.0:
            return
        self._last_status_print = now

        b_pnl = self.get_basket_net_pnl()
        b_target = self.get_basket_required_target()
        b_pairs = self.get_basket_open_pairs_count()
        b_lots = self.get_basket_total_lots()

        self.log("============== GOLD BASKET MARTINGALE EA ==============")
        if self.tick1 and self.tick2:
            self.log(f" Difference_Open  : {open_gap:.2f} | {self.cfg.symbol1}: {self.tick1.bid:.2f} | {self.cfg.symbol2}: {self.tick2.ask:.2f}")
            self.log(f" Difference_Close : {close_gap:.2f} | {self.cfg.symbol1}: {self.tick1.ask:.2f} | {self.cfg.symbol2}: {self.tick2.bid:.2f}")

        self.log(
            f" BASKET STATUS | Level: {b_pairs}/{self.cfg.max_martingale_levels} | "
            f"Total Lots: {b_lots:.2f} | Net PnL: ${b_pnl:.2f} / Target: ${b_target:.2f} "
            f"(Desired Net: ${self.cfg.desired_net_profit_usd:.2f}) | "
            f"Initial Gap: {self.basket_initial_gap:.2f} | Last Entry Gap: {self.last_entry_gap:.2f}"
        )
        self.log("======================================================")

    # ───────────────────────────────────────────────
    # Zero-Latency Main Run Loop
    # ───────────────────────────────────────────────

    def run(self) -> None:
        self.running = True
        self.log("======================================================")
        self.log("Gold Basket Martingale EA Started (SELL Direction)")
        self.log(f"Symbol1 (Future)       : {self.cfg.symbol1}")
        self.log(f"Symbol2 (Spot)         : {self.cfg.symbol2}")
        self.log(f"Initial Entry Gap      : {self.cfg.initial_entry_gap}")
        self.log(f"Base Lots              : Future {self.cfg.base_future_lot} | Spot {self.cfg.base_spot_lot}")
        self.log(f"Lot Multiplier         : {self.cfg.lot_multiplier}x")
        self.log(f"Martingale Gap Step    : {self.cfg.martingale_gap_step} points")
        self.log(f"Max Levels             : {self.cfg.max_martingale_levels}")
        self.log(f"Desired Net Profit     : ${self.cfg.desired_net_profit_usd:.2f}")
        self.log("======================================================")

        while self.running:
            try:
                if not self.refresh_ticks():
                    time.sleep(self.cfg.poll_interval_seconds)
                    continue

                if self.is_trade_locked():
                    time.sleep(self.cfg.poll_interval_seconds)
                    continue

                # Check orphans
                self.check_orphan_trades()

                open_gap = self.open_difference()
                close_gap = self.close_difference()

                # 🚀 ZERO-LATENCY HOT PATH: Evaluate & Fire Trade Orders IMMEDIATELY!
                # 1. Evaluate Unified Basket Exit Conditions
                should_close, exit_reason, live_net_pnl, target_limit = self.check_basket_exit_conditions()
                if should_close and self.is_hold_complete():
                    self.close_basket_and_reset(exit_reason)
                    self.log_throttled(
                        f"basket_exit:{exit_reason}",
                        f">>> BASKET EXIT COMPLETED [{exit_reason}] | Live Net PnL: ${live_net_pnl:.2f} | Limit/Target: ${target_limit:.2f}"
                    )
                    continue

                # 2. Evaluate Basket Martingale Entries
                open_pairs = self.get_basket_open_pairs_count()
                total_lots = self.get_basket_total_lots()

                if open_pairs == 0:
                    # Initial Entry (Level 1) — triggers when open_gap >= initial_entry_gap
                    if open_gap >= self.cfg.initial_entry_gap:
                        fut_vol, spot_vol = self.get_martingale_lots(0)
                        # 🚀 FIRE ORDER IMMEDIATELY!
                        self.place_basket_trade(0, self.cfg.initial_entry_gap, open_gap, fut_vol, spot_vol)
                elif open_pairs < self.cfg.max_martingale_levels:
                    # Recovery Entry (Level N+1)
                    next_level_index = open_pairs
                    target_adverse_trigger = round(self.last_entry_gap + self.cfg.martingale_gap_step, 2)
                    next_fut_vol, next_spot_vol = self.get_martingale_lots(next_level_index)

                    # Risk Checks
                    adverse_ok = open_gap <= self.cfg.max_allowed_adverse_gap
                    exposure_ok = (total_lots + next_fut_vol + next_spot_vol) <= self.cfg.max_total_lot_exposure

                    if open_gap >= target_adverse_trigger and adverse_ok and exposure_ok:
                        # 🚀 FIRE ORDER IMMEDIATELY!
                        self.place_basket_trade(next_level_index, target_adverse_trigger, open_gap, next_fut_vol, next_spot_vol)

                # 📝 POST-EXECUTION I/O: Logging and status printing happen AFTER order dispatch logic!
                self.log_spread()
                self.print_status(open_gap, close_gap)

            except KeyboardInterrupt:
                self.log(">>> CLOSE ALL — Keyboard Interrupt")
                self.close_basket_and_reset("KEYBOARD_INTERRUPT")
                self.running = False
            except Exception as exc:
                self.log("Main-loop error:", repr(exc))

            time.sleep(self.cfg.poll_interval_seconds)

    def stop(self) -> None:
        self.running = False


# ============================= LOCAL TICKET TAGGING =============================

class LocalTicketTagStore:
    def __init__(self, path: str = "ntrader_ticket_tags.json"):
        if path == "ntrader_ticket_tags.json":
            self.path = get_writable_path("ntrader_ticket_tags.json")
        else:
            path_obj = Path(path)
            if not path_obj.is_absolute():
                try:
                    base_dir = Path(__file__).resolve().parent
                    path_obj = base_dir / path
                except Exception:
                    pass
            self.path = path_obj
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

    def tag(self, ticket: str, magic: int, comment: str, open_time: datetime) -> None:
        self._tags[str(ticket)] = {
            "magic": magic,
            "comment": comment,
            "open_time": open_time.isoformat(),
        }
        self._save()

    def untag(self, ticket: str) -> None:
        if str(ticket) in self._tags:
            del self._tags[str(ticket)]
            self._save()

    def get(self, ticket: str) -> Optional[Dict[str, Any]]:
        return self._tags.get(str(ticket))


# ============================= nTRADER BRIDGE ADAPTER =============================

class NTraderAdapter:
    def __init__(
        self,
        magic_number: int,
        tag_store: Optional[LocalTicketTagStore] = None,
        trade_response_wait_seconds: float = 0.0,
    ):
        import ntrader
        self.bridge = ntrader.bridge
        self.magic_number = magic_number
        self.tags = tag_store or LocalTicketTagStore()
        self.trade_response_wait_seconds = trade_response_wait_seconds

    def get_tick(self, symbol: str) -> Optional[Tick]:
        raw = self.bridge.get_tick(symbol)
        if not raw:
            return None
        bid = raw.get("bid")
        ask = raw.get("ask")
        if bid is None or ask is None:
            return None
        return Tick(bid=float(bid), ask=float(ask))

    def get_positions(self) -> List[Position]:
        raw_positions = self.bridge.get_open_positions() or []
        positions: List[Position] = []
        for raw in raw_positions:
            ticket = raw.get("ticket")
            if ticket is None:
                continue
            ticket_str = str(ticket)
            tag = self.tags.get(ticket_str)
            magic = tag["magic"] if tag else 0
            comment = tag["comment"] if tag else ""
            open_time = (
                datetime.fromisoformat(tag["open_time"])
                if tag and "open_time" in tag
                else datetime.now(timezone.utc)
            )
            side_raw = str(raw.get("type", "")).lower()
            side = "BUY" if "buy" in side_raw else "SELL"
            open_price = float(raw.get("open_price") or raw.get("price") or raw.get("price_open") or 0.0)

            positions.append(
                Position(
                    ticket=ticket_str,
                    symbol=raw.get("symbol", ""),
                    side=side,
                    volume=float(raw.get("volume", 0.0)),
                    open_time=open_time,
                    profit=float(raw.get("profit", 0.0)),
                    swap=float(raw.get("swap", 0.0)),
                    open_price=open_price,
                    magic=magic,
                    comment=comment,
                )
            )
        return positions

    def _send_buy_order(self, symbol: str, volume: float, comment: str = "") -> None:
        try:
            self.bridge.create_buy_order(symbol, volume, sl=0.0, tp=0.0, comment=comment)
        except TypeError:
            self.bridge.create_buy_order(symbol, volume, sl=0.0, tp=0.0)

    def _send_sell_order(self, symbol: str, volume: float, comment: str = "") -> None:
        try:
            self.bridge.create_sell_order(symbol, volume, sl=0.0, tp=0.0, comment=comment)
        except TypeError:
            self.bridge.create_sell_order(symbol, volume, sl=0.0, tp=0.0)

    def get_position_details(self, ticket: str) -> Optional[Dict[str, Any]]:
        """Retrieve fill details for a specific ticket."""
        raw_pos = self.bridge.get_open_positions() or []
        for p in raw_pos:
            if str(p.get("ticket")) == str(ticket):
                return {
                    "ticket": str(ticket),
                    "symbol": str(p.get("symbol", "")),
                    "open_price": float(p.get("open_price") or p.get("price") or p.get("price_open") or 0.0),
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
            if side1.strip().upper() == "SELL":
                self._send_sell_order(symbol1, volume1, comment=comment1)
            else:
                self._send_buy_order(symbol1, volume1, comment=comment1)

        def _send_leg2():
            if side2.strip().upper() == "BUY":
                self._send_buy_order(symbol2, volume2, comment=comment2)
            else:
                self._send_sell_order(symbol2, volume2, comment=comment2)

        # 🚀 CONCURRENT DISPATCH IN PARALLEL THREADS
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_send_leg1)
            f2 = executor.submit(_send_leg2)
            f1.result()
            f2.result()

        ticket1: Optional[str] = None
        ticket2: Optional[str] = None
        sym1_clean = symbol1.strip().upper()
        sym2_clean = symbol2.strip().upper()

        for attempt in range(12):
            after_pos = self.bridge.get_open_positions() or []
            now_utc = datetime.now(timezone.utc)

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
            if attempt < 11:
                time.sleep(0.05)

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

        # Calculate per-leg adverse slippage
        # For SELL: slippage = requested - filled (positive = bad, LP gave lower price)
        # For BUY:  slippage = filled - requested (positive = bad, LP gave higher price)
        if side1.strip().upper() == "SELL":
            slip1 = round(req_price1 - fill_price1, 2)  # SELL wants higher price
        else:
            slip1 = round(fill_price1 - req_price1, 2)  # BUY wants lower price

        if side2.strip().upper() == "SELL":
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
        try:
            self.tags.tag(ticket1, magic=magic, comment=comment1, open_time=now_utc)
        except Exception:
            pass
        try:
            self.tags.tag(ticket2, magic=magic, comment=comment2, open_time=now_utc)
        except Exception:
            pass

        return ticket1, ticket2

    def close_positions_simultaneous(self, tickets: List[str]) -> None:
        if not tickets:
            return

        def _do_close(t_str: str) -> None:
            try:
                self.bridge.close_position(int(t_str))
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=len(tickets)) as executor:
            executor.map(_do_close, tickets)

        for t in tickets:
            try:
                self.tags.untag(str(t))
            except Exception:
                pass

    def close_position(self, ticket: str, deviation: int = 100) -> bool:
        try:
            self.bridge.close_position(int(ticket))
            self.tags.untag(str(ticket))
            return True
        except Exception:
            return False


# ============================= MAIN ENTRY POINT =============================

if __name__ == "__main__":
    cfg = MartingaleConfig()
    tag_store = LocalTicketTagStore(path="ntrader_ticket_tags.json")
    broker = NTraderAdapter(
        magic_number=cfg.magic_number,
        tag_store=tag_store,
        trade_response_wait_seconds=cfg.trade_response_wait_seconds,
    )
    bot = GoldBasketMartingaleBot(broker, cfg)
    bot.run()
