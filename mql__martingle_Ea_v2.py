"""
HRX Arbitrage EA v5 — Python port for N-Trader (SELL Direction)
================================================================
Converted from: HRX_2132639732_v5.mq5 (licecnse.mq5)

SELL direction arbitrage strategy:
  OPEN  → Symbol1 SELL + Symbol2 BUY  (when sym1.bid - sym2.ask > threshold + overshoot)
  CLOSE → When sym1.ask - sym2.bid < cut_threshold - overshoot

All MQL5 features ported 1:1:
  - 11 Sets (SET_0 to SET_10) with individual numberOfPairs, diffToTrade, diffToCut
  - Level Locking (permanent lock after close/cut, unlock only on restart)
  - Orphan Detection (single-leg auto-close)
  - Open Overshoot / Close Overshoot
  - Price Re-check before execution (avoid stale triggers)
  - condition_prev double-fire guard (prevents same-tick re-trigger)
  - Expected Profit target calculation (gap-capture based)
  - Trade Lock with 3s timeout (prevents concurrent trades)
  - Position Recovery on restart (rebuilds internal structure from open positions)
  - Min Hold Minutes (per-level hold time check)
  - tradeOnSameLevel option
  - Sequential set processing (one open per tick cycle, matching MQL5 return behavior)

nTrader bridge integration (unchanged from original differ_lot_sell_v3.py)
---------------------------------------------------------------------------
    import ntrader
    bridge = ntrader.bridge

    bridge.get_tick(symbol)                                   -> dict | None
    bridge.get_open_positions()                                -> list[dict]
    bridge.create_buy_order(symbol, volume, sl=0.0, tp=0.0)     -> None (async)
    bridge.create_sell_order(symbol, volume, sl=0.0, tp=0.0)    -> None (async)
    bridge.close_position(ticket)                               -> None (async)
    bridge.get_last_trade_response()                            -> dict (after sleep)

IMPORTANT GAP AND HOW IT'S HANDLED
-----------------------------------
The original MQL5 EA (and this Python port) identify which open positions
belong to which "Set" (SET_0..SET_10) using two broker-side fields on each
position: a `magic` number and a `comment` string like "SET_3".

The nTrader bridge API does NOT expose either of these:
  - create_buy_order / create_sell_order take no comment or magic parameter.
  - get_open_positions() dicts (per the docs sample) only contain
    ticket, symbol, type, volume, profit -- no comment, magic, swap, or
    open_time.

Since the broker can't tag orders for us, this file adds a local tagging
layer (`LocalTicketTagStore`) that:
  1. Records ticket -> (set_name, leg, magic, open_time) locally (JSON file)
     the moment an order is placed and its ticket is known.
  2. Re-attaches that metadata onto Position objects when positions are
     fetched, by matching on ticket number.

This means the strategy's existing logic (which filters by `magic` and
`comment` prefix) keeps working unmodified. The tradeoff: if the local
JSON file is lost or a position is opened outside this bot, it will show
up as an "untagged" position with magic=0 and empty comment, and will be
IGNORED by the strategy (not managed, not auto-closed).

TRADE RESPONSE SCHEMA -- CONFIRMED from a live log
-----------------------------------------------------
`bridge.get_last_trade_response()` returns a dict shaped like:

    {
        "error": "",
        "login": 5638,
        "msg": "Invalid volume",
        "res_code": 7,
        "res_ord": 0,
        "res_price": 0,
        "server_time": 1785757509,
        "sn": "",
        "status": 10014,
        "type": 0,
    }

Confirmed field meanings:
  - "res_ord"      -> the new position's ticket (0 / falsy when the order
                       was rejected).
  - "status"       -> reuses MT4/5-style trade return codes. 10009 =
                       TRADE_RETCODE_DONE (success). Anything else is a
                       rejection (seen so far: 10014 = INVALID_VOLUME).
  - "msg"          -> human-readable error text on rejection.
  - "error"        -> was empty even on a real rejection in the sample log,
                       so it is NOT a reliable failure signal on its own.
  - "res_price"    -> fill price.
  - "server_time"  -> epoch seconds, broker server time.
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
    """
    Returns a Path object for `filename` in a directory guaranteed to be writable.
    First tries the directory of this file / current working directory.
    If writing a temporary file fails (e.g., C:\\Program Files (x86)\\...),
    it falls back to %LOCALAPPDATA%\\TradivantaBot or ~/.ntrader_bot directory.
    """
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

    def market_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        deviation: int,
        magic: int,
        comment: str,
    ) -> str:
        """Place a market order and return its ticket/order ID."""
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
    ) -> Tuple[Optional[str], Optional[str]]:
        """Place two market orders simultaneously with individual volumes and return their tickets."""
        ...

    def close_position(self, ticket: str, deviation: int) -> bool:
        ...


# ============================= MQL5 MATCHING CONSTANTS =============================

ORDER_NUM = 100                  # MQL5: #define orderNum 100
TRADE_LOCK_TIMEOUT_S = 3.0      # MQL5: #define TRADE_LOCK_TIMEOUT_MS 3000


# ============================= ORDER DETAILS (matches MQL5 orderDetails struct) =============================

@dataclass
class OrderDetails:
    """
    MQL5 struct orderDetails {
        ulong tckts[];
        int   lvl;
        datetime openTime;
    };
    """
    tickets: List[str] = field(default_factory=list)
    level: int = -1
    open_time: Optional[datetime] = None


# ============================= CONFIGURATION =============================

@dataclass
class SetConfig:
    """
    One set's parameters — matches MQL5 inputs:
      numberOfPairsX, diffToTradeX, diffToCutX
    """
    name: str                   # "SET_0", "SET_1", ... "SET_10"
    number_of_pairs: int        # MQL5: numberOfPairs
    diff_to_trade: float        # MQL5: diffToTrade (0 = disabled)
    diff_to_cut: float          # MQL5: diffToCut   (0 = disabled)


@dataclass
class StrategyConfig:
    # ─── General Settings (matches MQL5 input parameters) ───
    symbol1: str = "GOLD_DEC26"         # MQL5: 
    symbol2: str = "XAUUSD.LMAX"        # MQL5: symbol2
    future_lot: float = 1.0             # Default Lot for Symbol1 (Future leg)
    spot_lot: float = 0.01              # Default Lot for Symbol2 (Spot leg)
    magic_number: int = 415             # MQL5: magic_no
    slippage: int = 100                  # MQL5: slippageAllowed
    stop_loss: float = 0.0              # MQL5: stopLoss (0 = off)
    take_profit: float = 0.0            # MQL5: takeProfit (0 = off)
    open_overshoot: float = 0.0         # MQL5: openOvershoot (all sets)
    cut_overshoot: float = 0.0         # MQL5: cutOvershoot (all sets)
    trade_on_same_level: bool = False   # MQL5: tradeOnSameLevel
    min_hold_minutes: int = 3           # MQL5: minHoldMinutes (0 = off)
    symbol_to_trade: int = 0            # MQL5: SymbolToTrade (0=Sym1, 1=Sym2)
    symbol_to_close: int = 0            # MQL5: SymbolToClose (0=Sym1, 1=Sym2)

    # ─── Basket Martingale & Recovery Settings ───
    enable_basket_martingale: bool = True       # Enable basket martingale recovery system
    initial_entry_gap: float = 41.60            # Initial entry gap trigger in points for Level 1
    base_future_lot: float = 2.0                # Base lot for Symbol1 (Future leg)
    base_spot_lot: float = 0.02               # Base lot for Symbol2 (Spot leg)
    lot_multiplier: float = 2.0                 # Martingale lot multiplier per level (e.g., Level 1=2.0/0.02, Level 2=4.0/0.04)
    martingale_gap_step: float = 0.50           # Adverse gap movement in points to trigger next martingale level
    max_martingale_levels: int = 5              # Max martingale recovery levels allowed
    desired_net_profit_usd: float = 1.50        # Actual net profit target in USD after all trading costs
    commission_per_pair_usd: float = 0.16       # Total roundturn commission cost per 0.01 spot lot ($)
    max_allowed_slippage_points: float = 5.0    # Allowed exit slippage buffer in points

    # ─── Basket Risk Controls ───
    max_total_lot_exposure: float = 15.0        # Combined open lot limit across all legs
    max_basket_drawdown_usd: float = 500.0      # Emergency Basket Equity Stop Loss in USD
    max_allowed_adverse_gap: float = 55.0       # Max adverse gap limit beyond which no new martingale orders fire

    # ─── Bot Settings ───
    poll_interval_seconds: float = 0.00001
    trade_response_wait_seconds: float = 0.0
    account_label: str = "A-BOOK-LIVE"

    # ─── Spread Logging ───
    spread_log_path: str = field(
        default_factory=lambda: str(get_writable_path("spread_log.csv"))
    )
    spread_log_interval_seconds: float = 1.0

    # ─── Set Parameters (SET_0 to SET_10) ───
    # Set open = 0.0 or pairs = 0 to disable a set

    # ── SET 0 ──
    set0_pairs: int = 1
    set0_open: float = 41.6
    set0_cut: float = 38.6

    # ── SET 1 ──
    set1_pairs: int = 1
    set1_open: float = 42.6
    set1_cut: float = 39.6

    # ── SET 2 ──
    set2_pairs: int = 1
    set2_open: float = 43.6
    set2_cut: float = 40.6

    # ── SET 3 ──
    set3_pairs: int = 1
    set3_open: float = 44.6
    set3_cut: float = 41.6

    # ── SET 4 ──
    set4_pairs: int = 1
    set4_open: float = 45.6
    set4_cut: float = 42.6

    # ── SET 5 ──
    set5_pairs: int = 0
    set5_open: float = 46.6
    set5_cut: float = 43.6

    # ── SET 6 ──
    set6_pairs: int = 0
    set6_open: float = 47.6
    set6_cut: float = 44.6

    # ── SET 7 ──
    set7_pairs: int = 0
    set7_open: float = 48.6
    set7_cut: float = 45.6

    # ── SET 8 ──
    set8_pairs: int = 0
    set8_open: float = 49.6
    set8_cut: float = 46.6

    # ── SET 9 ──
    set9_pairs: int = 0
    set9_open: float = 50.6
    set9_cut: float = 47.6

    # ── SET 10 ──
    set10_pairs: int = 0
    set10_open: float = 51.6
    set10_cut: float = 48.6

    sets: List[SetConfig] = field(default_factory=list)

    def __post_init__(self):
        if not self.sets:
            params = [
                (self.set0_pairs, self.set0_open, self.set0_cut),
                (self.set1_pairs, self.set1_open, self.set1_cut),
                (self.set2_pairs, self.set2_open, self.set2_cut),
                (self.set3_pairs, self.set3_open, self.set3_cut),
                (self.set4_pairs, self.set4_open, self.set4_cut),
                (self.set5_pairs, self.set5_open, self.set5_cut),
                (self.set6_pairs, self.set6_open, self.set6_cut),
                (self.set7_pairs, self.set7_open, self.set7_cut),
                (self.set8_pairs, self.set8_open, self.set8_cut),
                (self.set9_pairs, self.set9_open, self.set9_cut),
                (self.set10_pairs, self.set10_open, self.set10_cut),
            ]
            self.sets = [
                SetConfig(f"SET_{i}", p, o, c)
                for i, (p, o, c) in enumerate(params)
            ]


# ============================= STRATEGY (MQL5 1:1 PORT) =============================

class GoldArbitrageBot:
    """
    HRX Arbitrage EA v5 — Python port.
    Every method maps directly to an MQL5 function.
    SELL direction only: sym1 SELL + sym2 BUY on open.
    """

    def __init__(self, broker: BrokerAdapter, config: Optional[StrategyConfig] = None):
        self.broker = broker
        self.cfg = config or StrategyConfig()

        # Tick data
        self.tick1: Optional[Tick] = None
        self.tick2: Optional[Tick] = None

        # ─── MQL5 State Arrays (11 levels: SET_0 to SET_10) ───
        self.condition_prev   = [False] * 11   # MQL5: g_condition_prev — same-tick double fire guard
        self.actual_open_gap  = [0.0]   * 11   # MQL5: g_actual_open_gap — recorded gap at open
        self.level_filled     = [False] * 11   # MQL5: g_level_filled — all pairs filled for this level
        self.level_closing    = [False] * 11   # MQL5: g_level_closing — currently closing this level
        self.level_locked     = [False] * 11   # MQL5: g_level_locked — permanent lock after close/cut
        self.prev_diff_to_trade = [0.0] * 11   # MQL5: g_prev_diffToTrade — for detecting param changes

        # ─── Trade Lock ───
        self.trade_in_progress = False         # MQL5: g_trade_in_progress
        self.trade_lock_time   = 0.0           # MQL5: g_trade_lock_time

        # ─── Order Details Structure (MQL5: orderDetails od_1[100]) ───
        self.od = [OrderDetails() for _ in range(ORDER_NUM)]

        # ─── Run State ───
        self.running = False
        self.total_pairs = 0                   # MQL5: totalPairs (count of active sets)

        # ─── Missed Entry Tracking ───
        self.missed_entries_total = 0
        self.missed_entries_by_reason: Dict[str, int] = {}
        self.missed_entries_by_level = [0] * 11

        # ─── Throttling & Tracking ───

        self._last_status_print = 0.0
        self._last_spread_log = 0.0
        self._spread_log_header_written = False
        self._spread_log_error_logged = False
        self._throttled_logs: Dict[str, float] = {}

        # ─── Tick Feed Age & Staleness Tracking (Diagnostic only) ───
        self._last_tick1_price: Tuple[float, float] = (0.0, 0.0)
        self._last_tick1_change_time: float = time.monotonic()
        self._last_tick2_price: Tuple[float, float] = (0.0, 0.0)
        self._last_tick2_change_time: float = time.monotonic()

        # ─── Basket Martingale & Recovery State ───
        self.basket_initial_gap: float = 0.0
        self.last_entry_gap: float = 0.0
        self.basket_active_level_count: int = 0

    def log_missed_entry(self, level: int, reason: str, details: str) -> None:
        """
        Log a missed trade entry when open condition was met but trade failed or was skipped.
        Tracks total missed entries, level-wise count, and reason breakdown.
        """
        self.missed_entries_total += 1
        self.missed_entries_by_reason[reason] = self.missed_entries_by_reason.get(reason, 0) + 1
        self.missed_entries_by_level[level] += 1

        self.log_throttled(
            f"missed_entry:{level}:{reason}",
            f">>> MISSED ENTRY #{self.missed_entries_total} | Set_{level} | "
            f"Reason: [{reason}] -> {details} | "
            f"Total Missed: {self.missed_entries_total} (Set_{level}: {self.missed_entries_by_level[level]})",
            min_interval=1.0,
        )

    # ───────────────────────────────────────────────
    # Logging
    # ───────────────────────────────────────────────

    def log(self, *parts: Any) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {' '.join(str(p) for p in parts)}"
        print(line, flush=True)
        try:
            log_dir = get_writable_path("logs")
            if not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            today_str = datetime.now().strftime("%Y-%m-%d")
            log_path = os.path.join(log_dir, f"bot_sell_{today_str}.log")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def log_throttled(self, key: str, *parts: Any, min_interval: float = 3.0) -> None:
        now = time.monotonic()
        last = self._throttled_logs.get(key, 0.0)
        if now - last < min_interval:
            return
        self._throttled_logs[key] = now
        self.log(*parts)

    # ───────────────────────────────────────────────
    # Tick Management (with staleness checks)
    # ───────────────────────────────────────────────

    def refresh_ticks(self) -> bool:
        try:
            self.tick1 = self.broker.get_tick(self.cfg.symbol1)
            self.tick2 = self.broker.get_tick(self.cfg.symbol2)
            if self.tick1 is None:
                self.log_throttled(
                    f"tick_missing:{self.cfg.symbol1}",
                    f"Tick refresh failed: no data for symbol1={self.cfg.symbol1!r}",
                )
                return False
            if self.tick2 is None:
                self.log_throttled(
                    f"tick_missing:{self.cfg.symbol2}",
                    f"Tick refresh failed: no data for symbol2={self.cfg.symbol2!r}",
                )
                return False

            if self.tick1.bid <= 0 or self.tick1.ask <= 0 or self.tick2.bid <= 0 or self.tick2.ask <= 0:
                return False

            now = time.monotonic()
            if (self.tick1.bid, self.tick1.ask) != self._last_tick1_price:
                self._last_tick1_price = (self.tick1.bid, self.tick1.ask)
                self._last_tick1_change_time = now

            if (self.tick2.bid, self.tick2.ask) != self._last_tick2_price:
                self._last_tick2_price = (self.tick2.bid, self.tick2.ask)
                self._last_tick2_change_time = now

            return True
        except Exception as exc:
            self.log_throttled("tick_refresh_exc", "Tick refresh failed:", exc)
            return False

    def get_tick_ages(self) -> Tuple[float, float]:
        """Returns (tick1_age_seconds, tick2_age_seconds) for feed staleness diagnostics."""
        now = time.monotonic()
        t1_age = round(now - self._last_tick1_change_time, 2)
        t2_age = round(now - self._last_tick2_change_time, 2)
        return t1_age, t2_age

    # ─── Gap Calculations (SELL direction) ───

    def open_difference(self) -> float:
        """MQL5 SELL: dos = sym1.BID - sym2.ASK"""
        assert self.tick1 is not None and self.tick2 is not None
        return self.tick1.bid - self.tick2.ask

    def close_difference(self) -> float:
        """MQL5 SELL: dcs = sym1.ASK - sym2.BID"""
        assert self.tick1 is not None and self.tick2 is not None
        return self.tick1.ask - self.tick2.bid

    def future_spread(self) -> float:
        assert self.tick1 is not None
        return self.tick1.ask - self.tick1.bid

    def spot_spread(self) -> float:
        assert self.tick2 is not None
        return self.tick2.ask - self.tick2.bid

    # ───────────────────────────────────────────────
    # Position Management
    # ───────────────────────────────────────────────

    def strategy_positions(self) -> List[Position]:
        """All positions matching our magic number and symbols."""
        positions = self.broker.get_positions()
        return [
            p for p in positions
            if p.magic == self.cfg.magic_number
            and p.symbol in (self.cfg.symbol1, self.cfg.symbol2)
        ]

    def get_active_ticket_set(self) -> set:
        """Set of all active ticket strings for this strategy."""
        return {p.ticket for p in self.strategy_positions()}

    def net_profit_and_loss(self) -> float:
        """MQL5: getTotalPnL — sum profit+swap of all strategy positions."""
        return sum(p.profit + p.swap for p in self.strategy_positions())

    def total_active_orders(self) -> int:
        return len(self.strategy_positions())

    # ───────────────────────────────────────────────
    # Order Details Structure (MQL5 matching)
    # ───────────────────────────────────────────────

    def add_to_structure(self, level: int, ticket: str, open_time: datetime) -> None:
        """MQL5: level_trade_add_to_structure"""
        for i in range(ORDER_NUM):
            if self.od[i].level == -1 or self.od[i].level == level:
                self.od[i].level = level
                self.od[i].open_time = open_time
                self.od[i].tickets.append(ticket)
                break

    def get_active_count(self, tickets: List[str]) -> int:
        """MQL5: getActiveOrdersCount — count how many tickets are still open."""
        active = self.get_active_ticket_set()
        return sum(1 for t in tickets if t in active)

    def get_level_trade_pairs(self, level: int) -> int:
        """MQL5: get_level_trades_pair — count active pairs (tickets/2) on a level."""
        for i in range(ORDER_NUM):
            if self.od[i].level == level and len(self.od[i].tickets) != 0:
                return self.get_active_count(self.od[i].tickets) // 2
        return 0

    def check_trade_on_level(self, level: int) -> bool:
        """MQL5: check_trade_place_on_that_level"""
        for i in range(ORDER_NUM):
            if self.od[i].level == level and len(self.od[i].tickets) != 0:
                return True
        return False

    def clear_closed_tickets(self) -> None:
        """MQL5: clear_closed_tickets — cleanup structure entries with no active tickets."""
        active = self.get_active_ticket_set()
        for i in range(ORDER_NUM):
            if self.od[i].level == -1 or len(self.od[i].tickets) == 0:
                continue
            any_open = any(t in active for t in self.od[i].tickets)
            if not any_open:
                lv = self.od[i].level
                self.od[i].tickets = []
                self.od[i].level = -1
                self.od[i].open_time = None
                self.level_filled[lv] = False
                self.actual_open_gap[lv] = 0.0

    # ───────────────────────────────────────────────
    # Recovery (MQL5: recoverStructure)
    # ───────────────────────────────────────────────

    def recover_structure(self) -> None:
        """
        MQL5: recoverStructure
        Scan open positions and rebuild internal od structure.
        Also unlocks levels that have active positions (so close logic works).
        """
        positions = self.strategy_positions()
        for p in positions:
            # Extract level from comment "SET_X"
            level = self._extract_level_from_comment(p.comment)
            if level < 0 or level > 10:
                continue

            # Check if already in structure
            found = False
            for j in range(ORDER_NUM):
                if self.od[j].level != level:
                    continue
                if p.ticket in self.od[j].tickets:
                    found = True
                    break

            if not found:
                self.add_to_structure(level, p.ticket, p.open_time)
                self.log(f">>> RECOVERED ticket {p.ticket} SET_{level} ({p.symbol})")

            # Unlock level so closeTrade can work
            # condition_prev = true taaki dobara trigger na ho
            if self.level_locked[level]:
                self.level_locked[level] = False
                self.condition_prev[level] = True   # FIX: prevent re-trigger after recovery
                self.log(f">>> SET_{level} UNLOCKED for recovery")

    def scan_and_register_trades(self, level: int, open_time: datetime) -> None:
        """
        MQL5: scanAndRegisterTrades
        Scan open positions by comment and register to structure.
        Used after async open to reliably get tickets.
        """
        comment = f"SET_{level}"
        positions = self.strategy_positions()
        for p in positions:
            if p.comment != comment:
                continue
            # Check if already in structure
            found = False
            for j in range(ORDER_NUM):
                if self.od[j].level != level:
                    continue
                if p.ticket in self.od[j].tickets:
                    found = True
                    break
            if not found:
                self.add_to_structure(level, p.ticket, open_time)
                self.log(f">>> Registered ticket {p.ticket} for Set_{level}")

    @staticmethod
    def _extract_level_from_comment(comment: str) -> int:
        """Extract level number from comment like 'SET_3' -> 3"""
        if not comment or not comment.startswith("SET_"):
            return -1
        try:
            return int(comment[4:])
        except (ValueError, IndexError):
            return -1

    # ───────────────────────────────────────────────
    # PnL and Expected Profit (MQL5: getTotalPnL, getExpectedProfit)
    # ───────────────────────────────────────────────

    def get_total_pnl(self) -> float:
        """MQL5: getTotalPnL"""
        return sum(p.profit + p.swap for p in self.strategy_positions())

    def get_expected_profit(self) -> float:
        """
        MQL5: getExpectedProfit
        Calculate expected profit based on gap capture for all filled levels.
        Formula: capture = actual_open_gap - (diffToCut - cutOvershoot)
                 exp += capture * active_pairs * lot * 100
        """
        exp = 0.0
        for i, sc in enumerate(self.cfg.sets):
            ap = self.get_level_trade_pairs(i)
            if ap > 0 and self.actual_open_gap[i] > 0 and sc.diff_to_cut > 0:
                cut_level = sc.diff_to_cut - self.cfg.cut_overshoot
                capture = self.actual_open_gap[i] - cut_level
                if capture > 0:
                    exp += capture * ap * self.cfg.spot_lot * 100
        return round(exp, 2)

    # ───────────────────────────────────────────────
    # Basket Martingale Recovery Engine Methods
    # ───────────────────────────────────────────────

    def get_martingale_lots(self, level_index: int) -> Tuple[float, float]:
        """
        Calculate dynamic lot size for Level N (0-indexed).
        Example with base=0.51, multiplier=2.0:
          Level 0 (1st entry) -> 0.51 * (2.0^0) = 0.51 lot
          Level 1 (2nd entry) -> 0.51 * (2.0^1) = 1.02 lots
          Level 2 (3rd entry) -> 0.51 * (2.0^2) = 2.04 lots
          Level 3 (4th entry) -> 0.51 * (2.0^3) = 4.08 lots
        """
        multiplier = max(1.0, self.cfg.lot_multiplier)
        fut_lot = round(self.cfg.base_future_lot * (multiplier ** level_index), 2)
        spot_lot = round(self.cfg.base_spot_lot * (multiplier ** level_index), 2)
        return fut_lot, spot_lot

    def get_basket_total_lots(self) -> float:
        """Returns cumulative volume of all currently open strategy positions across both legs."""
        positions = self.strategy_positions()
        return round(sum(p.volume for p in positions), 2)

    def get_basket_open_pairs_count(self) -> int:
        """Returns the total number of hedged pairs currently open in the basket."""
        positions = self.strategy_positions()
        # Each pair consists of 2 positions (Leg1 + Leg2)
        return len(positions) // 2

    def get_basket_spot_lots(self) -> float:
        """Returns cumulative volume of all currently open Spot leg positions (Symbol 2)."""
        positions = [p for p in self.strategy_positions() if p.symbol == self.cfg.symbol2]
        return round(sum(p.volume for p in positions), 2)

    def get_basket_net_pnl(self) -> float:
        """
        Basket Net PnL Formula:
          Sum of floating profit/loss of ALL open legs
          + swaps
          - total roundturn commissions (scaled by spot volume: $0.16 per 0.01 lot)
        """
        positions = self.strategy_positions()
        if not positions:
            return 0.0
        floating_pnl = sum(p.profit + p.swap for p in positions)
        spot_lots = self.get_basket_spot_lots()
        # Commission rate: $0.16 per 0.01 spot lot ($16.0 per 1.0 spot lot)
        total_commissions = (spot_lots / 0.01) * self.cfg.commission_per_pair_usd
        net_pnl = floating_pnl - total_commissions
        return round(net_pnl, 2)

    def get_basket_required_target(self) -> float:
        """
        Required Basket Profit Target Formula:
          Total Trading Costs (Commissions + Exit Slippage Buffer) + Desired Net Profit
        """
        spot_lots = self.get_basket_spot_lots()
        total_lots = self.get_basket_total_lots()
        commissions = (spot_lots / 0.01) * self.cfg.commission_per_pair_usd
        # Slippage buffer: max_allowed_slippage_points converted to approx monetary value ($0.10/point/lot)
        slippage_buffer = self.cfg.max_allowed_slippage_points * 0.10 * total_lots
        required_target = self.cfg.desired_net_profit_usd + commissions + slippage_buffer
        return round(required_target, 2)

    def check_basket_exit_conditions(self) -> Tuple[bool, str, float, float]:
        """
        Evaluates basket exit conditions:
          1. Basket Net PnL >= Basket Profit Target (Target Hit)
          2. Basket Net PnL <= -Max Basket Drawdown (Emergency Stop Loss)
        Returns (should_close, reason, current_net_pnl, target_or_limit)
        """
        positions = self.strategy_positions()
        if not positions:
            return False, "", 0.0, 0.0

        net_pnl = self.get_basket_net_pnl()
        required_target = self.get_basket_required_target()

        if net_pnl >= required_target:
            return True, "PROFIT_TARGET_HIT", net_pnl, required_target

        if net_pnl <= -self.cfg.max_basket_drawdown_usd:
            return True, "MAX_DRAWDOWN_HIT", net_pnl, -self.cfg.max_basket_drawdown_usd

        return False, "", net_pnl, required_target

    # ───────────────────────────────────────────────
    # Hold Time (MQL5: isHoldTimeComplete, getHoldStatus, isAllHoldComplete)
    # ───────────────────────────────────────────────

    def is_hold_complete(self, level: int) -> bool:
        """MQL5: isHoldTimeComplete"""
        if self.cfg.min_hold_minutes <= 0:
            return True
        for i in range(ORDER_NUM):
            if self.od[i].level == level and self.od[i].open_time is not None:
                now = datetime.now(self.od[i].open_time.tzinfo or timezone.utc)
                mins = int((now - self.od[i].open_time).total_seconds() // 60)
                if mins < self.cfg.min_hold_minutes:
                    return False
                return True
        return True

    def get_hold_status(self, level: int) -> str:
        """MQL5: getHoldStatus"""
        if self.cfg.min_hold_minutes <= 0:
            return ""
        for i in range(ORDER_NUM):
            if self.od[i].level == level and self.od[i].open_time is not None:
                now = datetime.now(self.od[i].open_time.tzinfo or timezone.utc)
                mins = int((now - self.od[i].open_time).total_seconds() // 60)
                if mins < self.cfg.min_hold_minutes:
                    return f" [HOLD {mins}/{self.cfg.min_hold_minutes}min]"
        return ""

    def is_all_hold_complete(self) -> bool:
        """MQL5: isAllHoldComplete"""
        if self.cfg.min_hold_minutes <= 0:
            return True
        for i in range(11):
            if self.get_level_trade_pairs(i) > 0 and not self.is_hold_complete(i):
                return False
        return True

    # ───────────────────────────────────────────────
    # Trade Lock (MQL5: isTradeLocked)
    # ───────────────────────────────────────────────

    def is_trade_locked(self) -> bool:
        """MQL5: isTradeLocked — 3s timeout trade lock to prevent concurrent trades."""
        if not self.trade_in_progress:
            return False
        now = time.monotonic()
        if now - self.trade_lock_time > TRADE_LOCK_TIMEOUT_S:
            self.log(">>> TRADE LOCK TIMEOUT — Force unlocking")
            self.trade_in_progress = False
            return False
        return True

    # ───────────────────────────────────────────────
    # Orphan Detection (MQL5: checkOrphanTrades)
    # Agar 1 leg open aur 1 closed → orphan close karo
    # ───────────────────────────────────────────────

    def check_orphan_trades(self) -> None:
        """MQL5: checkOrphanTrades"""
        if self.trade_in_progress:
            return
        active = self.get_active_ticket_set()
        for i in range(ORDER_NUM):
            if self.od[i].level == -1:
                continue
            level = self.od[i].level
            if self.level_closing[level]:
                continue
            tickets = self.od[i].tickets
            if len(tickets) == 0:
                continue
            active_count = sum(1 for t in tickets if t in active)
            if active_count == 0:
                continue

            # Check tickets in pairs (j, j+1)
            for j in range(0, len(tickets) - 1, 2):
                t1 = tickets[j]
                t2 = tickets[j + 1]
                a = t1 in active
                b = t2 in active
                if a and b:
                    continue
                if not a and not b:
                    continue
                if a and not b:
                    self.log(f">>> ORPHAN | Closing t1={t1}")
                    try:
                        self.broker.close_position(t1, self.cfg.slippage)
                    except Exception as exc:
                        self.log(f"Error closing orphan {t1}: {exc}")
                else:
                    self.log(f">>> ORPHAN | Closing t2={t2}")
                    try:
                        self.broker.close_position(t2, self.cfg.slippage)
                    except Exception as exc:
                        self.log(f"Error closing orphan {t2}: {exc}")
                self.level_filled[level] = False
                self.actual_open_gap[level] = 0.0

    # ───────────────────────────────────────────────
    # Trade Placement (MQL5: placeTrade)
    #
    # v5 features:
    # - Async open: dono legs ek saath broker ko jaati hain
    # - Orphan checker handles single-leg situations
    # - condition_prev: same-tick double fire rokta hai
    # - After fill: agar aur pairs baaki hain toh reset for next pair
    # ───────────────────────────────────────────────

    def place_trade(
        self,
        number_of_pairs: int,
        diff_to_trade: float,
        level: int,
        open_gap: float,
        fut_vol: Optional[float] = None,
        spot_vol: Optional[float] = None,
    ) -> bool:
        """
        MQL5: placeTrade(int numberOfPair, double difftoTrade, int level, double dob, double dos)
        SELL direction: open_gap = dos = sym1.bid - sym2.ask
        Returns True if trade was placed.
        """
        v1 = fut_vol if fut_vol is not None else self.cfg.future_lot
        v2 = spot_vol if spot_vol is not None else self.cfg.spot_lot

        trigger = diff_to_trade + self.cfg.open_overshoot
        condition_met = open_gap > trigger

        # Fast zero-latency checks (NO logging delay in hot execution path)
        if self.level_locked[level]:
            return False

        if self.level_filled[level]:
            if self.get_level_trade_pairs(level) == 0:
                self.level_filled[level] = False
                self.condition_prev[level] = False
                self.actual_open_gap[level] = 0.0
            return False

        if not condition_met or self.condition_prev[level]:
            return False

        if self.get_level_trade_pairs(level) >= number_of_pairs:
            return False

        self.condition_prev[level] = True

        # Snapshot tick prices at trigger instant
        sent_leg1_price = self.tick1.bid if self.tick1 else 0.0
        sent_leg2_price = self.tick2.ask if self.tick2 else 0.0
        target_diff = diff_to_trade
        trigger_diff = open_gap

        # Trade lock
        self.trade_in_progress = True
        self.trade_lock_time = time.monotonic()

        comment = f"SET_{level}"

        # 🚀 ZERO-LATENCY: Send market order pair IMMEDIATELY before any logging I/O!
        t1_ticket, t2_ticket = self.broker.market_order_pair(
            symbol1=self.cfg.symbol1,
            side1="SELL",
            comment1=comment,
            symbol2=self.cfg.symbol2,
            side2="BUY",
            comment2=comment,
            volume1=v1,
            volume2=v2,
            magic=self.cfg.magic_number,
        )

        # 📝 LOGGING AFTER ORDER DISPATCH
        self.log(
            f">>> TRIGGER Set_{level} gap={open_gap} crossed {trigger} "
            f"({self.get_level_trade_pairs(level)}/{number_of_pairs})"
        )
        self.log(
            f"SIMULTANEOUS OPEN Set_{level} -> "
            f"Leg1 Future ({self.cfg.symbol1}, {v1} lots, SELL): ticket={t1_ticket} | "
            f"Leg2 Spot ({self.cfg.symbol2}, {v2} lots, BUY): ticket={t2_ticket}"
        )

        # Register tickets to structure
        now = datetime.now(timezone.utc)
        if t1_ticket:
            self.add_to_structure(level, t1_ticket, now)
        if t2_ticket:
            self.add_to_structure(level, t2_ticket, now)

        # Also scan positions (fallback, matching MQL5 scanAndRegisterTrades)
        self.scan_and_register_trades(level, now)

        active_pairs = self.get_level_trade_pairs(level)
        positions = [p for p in self.strategy_positions() if p.comment == comment]

        if active_pairs > 0:
            if self.actual_open_gap[level] == 0:
                self.actual_open_gap[level] = open_gap
            if self.basket_initial_gap == 0.0:
                self.basket_initial_gap = open_gap
            self.last_entry_gap = open_gap

            if active_pairs >= number_of_pairs:
                self.level_filled[level] = True
                self.condition_prev[level] = True
                self.log(f">>> Set_{level} FILLED ({active_pairs}/{number_of_pairs}) — waiting for cut/target")
            else:
                self.condition_prev[level] = False
                self.log(f">>> Set_{level} pair {active_pairs}/{number_of_pairs} done — waiting for next pair")

            # Log trade record & detailed slippage report AFTER order dispatch
            self.log_trade_record(f"SET_{level}", "OPEN", positions, open_gap)
            self.log_slippage_report(
                set_name=f"SET_{level}",
                event_type="OPEN",
                target_diff=target_diff,
                trigger_diff=trigger_diff,
                sent_leg1_price=sent_leg1_price,
                sent_leg2_price=sent_leg2_price,
                positions=positions,
            )

            self.trade_in_progress = False
            return True
        else:
            # No positions found — order failed, lock level
            self.log_missed_entry(
                level,
                "BROKER_REJECTION",
                f"Order sent to broker but no positions registered (broker rejected or margin insufficient)"
            )
            self.log(f">>> FAIL Set_{level} — no positions found after async open, LOCKING")
            self.log_slippage_report(
                set_name=f"SET_{level}",
                event_type="REJECTED",
                target_diff=target_diff,
                trigger_diff=trigger_diff,
                sent_leg1_price=sent_leg1_price,
                sent_leg2_price=sent_leg2_price,
                positions=[],
            )
            self.condition_prev[level] = False
            self.level_locked[level] = True
            self.trade_in_progress = False
            return False

    # ───────────────────────────────────────────────
    # Trade Closing (MQL5: closeTrade)
    # ───────────────────────────────────────────────

    def has_level_trades(self, level: int) -> bool:
        """Check if a specific level has active open tickets registered in structure."""
        for i in range(ORDER_NUM):
            if self.od[i].level == level and len(self.od[i].tickets) != 0:
                return True
        return False

    def close_trade(self, cut_trade: float, level: int, close_gap: float) -> None:
        """
        MQL5: closeTrade
        Close when closeGap < cutTrade - cutOvershoot (only if level has active trades)
        """
        if not self.has_level_trades(level):
            return
        if not self.is_hold_complete(level):
            return
        if close_gap < cut_trade - self.cfg.cut_overshoot:
            self.log(
                f">>> CUT Set_{level} | closeGap={close_gap} < "
                f"{cut_trade - self.cfg.cut_overshoot:.2f}"
            )
            self.close_level_trades(level)

    def close_level_trades(self, level: int) -> bool:
        """
        MQL5: close_trade_place_on_that_level
        Close all tickets on a specific level + LOCK level.
        """
        for i in range(ORDER_NUM):
            if self.od[i].level == level and len(self.od[i].tickets) != 0:
                self.log(f">>> CLOSING Set_{level}")
                self.level_closing[level] = True

                # Log trade record before closing
                comment = f"SET_{level}"
                positions = [p for p in self.strategy_positions() if p.comment == comment]
                close_diff = self.close_difference() if (self.tick1 and self.tick2) else 0.0
                set_pnl = sum(p.profit + p.swap for p in positions)
                sent_leg1_price = self.tick1.ask if self.tick1 else 0.0
                sent_leg2_price = self.tick2.bid if self.tick2 else 0.0
                target_cut_diff = self.cfg.sets[level].diff_to_cut if level < len(self.cfg.sets) else 0.0

                self.log(
                    f"TRADE CLOSED [SET_{level}] -> "
                    f"Total Set PnL: ${set_pnl:.2f} | CloseDiff={close_diff:.5f}"
                )
                self.log_trade_record(f"SET_{level}", "CLOSE", positions, close_diff)
                self.log_slippage_report(
                    set_name=f"SET_{level}",
                    event_type="CLOSE",
                    target_diff=target_cut_diff,
                    trigger_diff=close_diff,
                    sent_leg1_price=sent_leg1_price,
                    sent_leg2_price=sent_leg2_price,
                    positions=positions,
                )

                # Close all tickets simultaneously — sab ek saath close
                tickets_to_close = list(self.od[i].tickets)
                if hasattr(self.broker, "close_positions_simultaneous"):
                    self.log(f"SIMULTANEOUS CLOSE Set_{level} tickets: {tickets_to_close}")
                    self.broker.close_positions_simultaneous(tickets_to_close)
                else:
                    for t in tickets_to_close:
                        try:
                            active = self.get_active_ticket_set()
                            if t in active:
                                self.broker.close_position(t, self.cfg.slippage)
                                self.log(f"Closed: {t}")
                        except Exception as exc:
                            self.log(f"Err: {t} {exc}")

                # Update state (matching MQL5 exactly)
                self.level_closing[level] = False
                self.actual_open_gap[level] = 0.0
                self.level_filled[level] = False
                self.condition_prev[level] = True
                self.level_locked[level] = True     # LOCK after cut
                self.log(f">>> Set_{level} CUT & LOCKED — restart script to re-enable")

                if self.cfg.trade_on_same_level:
                    self.od[i].tickets = []
                    self.od[i].level = -1
                    self.od[i].open_time = None

                return True
        return False

    def close_all_trades(self) -> None:
        """MQL5: closeAllTrades — sab ek saath close"""
        positions = self.strategy_positions()
        tickets = [p.ticket for p in positions]
        if not tickets:
            return
        if hasattr(self.broker, "close_positions_simultaneous"):
            self.broker.close_positions_simultaneous(tickets)
        else:
            for p in positions:
                try:
                    self.broker.close_position(p.ticket, self.cfg.slippage)
                    self.log(f"Closed: {p.ticket}")
                except Exception as exc:
                    self.log(f"Error: {p.ticket} {exc}")

    def close_all_and_lock(self) -> None:
        """
        MQL5: closeAllTrades + lock all levels
        Used by: profit target hit, keyboard interrupt (Close All equivalent).
        """
        self.trade_in_progress = False
        self.close_all_trades()
        for i in range(11):
            self.level_filled[i] = False
            self.condition_prev[i] = True
            self.actual_open_gap[i] = 0.0
            self.level_locked[i] = True
        self.log(">>> ALL LEVELS LOCKED — restart script to re-enable")

    # ───────────────────────────────────────────────
    # Spread Logging (for spread_dashboard.html)
    # ───────────────────────────────────────────────

    def log_spread(self) -> None:
        """Append the current spread to a CSV file for spread_dashboard.html.
        Throttled to spread_log_interval_seconds so a fast poll loop doesn't
        flood the file."""
        now = time.monotonic()
        if now - self._last_spread_log < self.cfg.spread_log_interval_seconds:
            return
        self._last_spread_log = now

        if self.tick1 is None or self.tick2 is None:
            return

        path = self.cfg.spread_log_path
        if not os.path.isabs(path):
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                path = os.path.join(script_dir, path)
            except Exception:
                pass

        try:
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
                    f"{self.total_active_orders()},{self.net_profit_and_loss():.2f}\n"
                )
        except Exception as exc:
            if not self._spread_log_error_logged:
                self.log(f"Spread log write failed ({path}):", exc)
                self._spread_log_error_logged = True

    # ───────────────────────────────────────────────
    # Trade History CSV
    # ───────────────────────────────────────────────

    def log_trade_record(self, set_name: str, event_type: str, positions: List[Position], diff_val: float) -> None:
        """Appends trade execution/close record to trade_history.csv."""
        try:
            path = get_writable_path("trade_history.csv")
            file_exists = path.exists()
            p_fut = next((p for p in positions if p.symbol == self.cfg.symbol1), None)
            p_spot = next((p for p in positions if p.symbol == self.cfg.symbol2), None)

            p1_comment = p_fut.comment if p_fut else f"{set_name}_Leg1"
            p1_ticket = p_fut.ticket if p_fut else ""
            p1_price = p_fut.open_price if p_fut else 0.0
            p1_pnl = (p_fut.profit + p_fut.swap) if p_fut else 0.0

            p2_comment = p_spot.comment if p_spot else f"{set_name}_Leg2"
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
                    f"{datetime.now(timezone.utc).isoformat()},{self.cfg.account_label},{event_type},{set_name},"
                    f"{p1_comment},{p1_ticket},{self.cfg.symbol1},{p1_price:.4f},{p1_pnl:.2f},"
                    f"{p2_comment},{p2_ticket},{self.cfg.symbol2},{p2_price:.4f},{p2_pnl:.2f},"
                    f"{diff_val:.5f},{total_pnl:.2f}\n"
                )
        except Exception as exc:
            self.log(f"Failed to write trade_history.csv: {exc}")

    def log_slippage_report(
        self,
        set_name: str,
        event_type: str,
        target_diff: float,
        trigger_diff: float,
        sent_leg1_price: float,
        sent_leg2_price: float,
        positions: List[Position],
    ) -> None:
        """
        Logs detailed execution and slippage breakdown to console and appends to slippage_report.csv.
        Called strictly AFTER order execution to ensure ZERO latency during trade placement.
        """
        try:
            p_fut = next((p for p in positions if p.symbol == self.cfg.symbol1), None)
            p_spot = next((p for p in positions if p.symbol == self.cfg.symbol2), None)

            fill_leg1_price = p_fut.open_price if p_fut else 0.0
            fill_leg2_price = p_spot.open_price if p_spot else 0.0
            t1_ticket = p_fut.ticket if p_fut else "N/A"
            t2_ticket = p_spot.ticket if p_spot else "N/A"

            diff_spike = round(trigger_diff - target_diff, 4)
            sent_diff = round(sent_leg1_price - sent_leg2_price, 4)

            # Leg 1 (SELL): Sent - Fill (positive = adverse fill for SELL)
            if fill_leg1_price > 0.0 and sent_leg1_price > 0.0:
                leg1_slippage = round(sent_leg1_price - fill_leg1_price, 4)
            else:
                leg1_slippage = 0.0

            # Leg 2 (BUY): Fill - Sent (positive = adverse fill for BUY)
            if fill_leg2_price > 0.0 and sent_leg2_price > 0.0:
                leg2_slippage = round(fill_leg2_price - sent_leg2_price, 4)
            else:
                leg2_slippage = 0.0

            filled_diff = round(fill_leg1_price - fill_leg2_price, 4) if (fill_leg1_price > 0 and fill_leg2_price > 0) else 0.0
            total_diff_slippage = round(filled_diff - trigger_diff, 4) if filled_diff > 0 else 0.0

            # Feed Staleness Diagnostic
            t1_age, t2_age = self.get_tick_ages()
            is_stale = (t1_age > 1.0 or t2_age > 1.0)
            if is_stale:
                stale_parts = []
                if t1_age > 1.0:
                    stale_parts.append(f"Leg1 ({self.cfg.symbol1}) price unchanged for {t1_age:.2f}s")
                if t2_age > 1.0:
                    stale_parts.append(f"Leg2 ({self.cfg.symbol2}) price unchanged for {t2_age:.2f}s")
                feed_status = f"[STALE FEED WARNING] {', '.join(stale_parts)}. Gap spike may be caused by feed delay!"
                stale_flag = "STALE"
            else:
                feed_status = f"FRESH (Leg1 age: {t1_age:.2f}s, Leg2 age: {t2_age:.2f}s)"
                stale_flag = "FRESH"

            self.log(
                f"======================================== [ SLIPPAGE REPORT: {set_name} {event_type} ] ========================================\n"
                f">>> [1] TARGET vs TRIGGER : Target Diff: {target_diff:.2f} | Actual Trigger Diff: {trigger_diff:.2f} | Spike Overshoot: {diff_spike:+.2f}\n"
                f">>> [2] SENT TO BROKER   : Leg1 ({self.cfg.symbol1} SELL): {sent_leg1_price:.4f} | Leg2 ({self.cfg.symbol2} BUY): {sent_leg2_price:.4f} | Sent Diff: {sent_diff:.2f}\n"
                + (
                    f">>> [3] FILLED FROM LP   : Leg1 Ticket #{t1_ticket} Open: {fill_leg1_price:.4f} (Slippage: {leg1_slippage:+.4f}) | Leg2 Ticket #{t2_ticket} Open: {fill_leg2_price:.4f} (Slippage: {leg2_slippage:+.4f})\n"
                    f">>> [4] FINAL EXECUTION  : Filled Diff: {filled_diff:.2f} | Total Gap Slippage: {total_diff_slippage:+.2f} points\n"
                    if (fill_leg1_price > 0 or fill_leg2_price > 0)
                    else ">>> [3] FILLED FROM LP   : No filled positions registered yet (or order rejected by broker).\n"
                )
                + f">>> [5] TICK FEED STATUS : {feed_status}\n"
                + "================================================================================================================="
            )

            path = get_writable_path("slippage_report.csv")
            file_exists = path.exists()

            with open(path, "a", encoding="utf-8", newline="") as fh:
                if not file_exists:
                    fh.write(
                        "timestamp_utc,account_label,event_type,set_name,"
                        "target_diff,trigger_diff,overshoot_spike,"
                        "leg1_symbol,leg1_sent,leg1_fill,leg1_slippage,leg1_ticket,leg1_age_s,"
                        "leg2_symbol,leg2_sent,leg2_fill,leg2_slippage,leg2_ticket,leg2_age_s,"
                        "filled_diff,total_diff_slippage,feed_status\n"
                    )
                fh.write(
                    f"{datetime.now(timezone.utc).isoformat()},{self.cfg.account_label},{event_type},{set_name},"
                    f"{target_diff:.4f},{trigger_diff:.4f},{diff_spike:+.4f},"
                    f"{self.cfg.symbol1},{sent_leg1_price:.4f},{fill_leg1_price:.4f},{leg1_slippage:+.4f},{t1_ticket},{t1_age:.2f},"
                    f"{self.cfg.symbol2},{sent_leg2_price:.4f},{fill_leg2_price:.4f},{leg2_slippage:+.4f},{t2_ticket},{t2_age:.2f},"
                    f"{filled_diff:.4f},{total_diff_slippage:+.4f},{stale_flag}\n"
                )
        except Exception as exc:
            self.log(f"Failed to log slippage report: {exc}")

    # ───────────────────────────────────────────────
    # Status Display (matches MQL5 OnTick print format)
    # ───────────────────────────────────────────────

    def print_status(self, open_gap: float, close_gap: float) -> None:
        now = time.monotonic()
        if now - self._last_status_print < 2.0:
            return
        self._last_status_print = now

        self.log(f"============== HRX EA v5 [{self.cfg.account_label}] ==============")

        if self.tick1 is not None and self.tick2 is not None:
            # SELL direction prices (matching MQL5 print format)
            self.log(
                f" Difference_Open  : {open_gap} , "
                f"{self.cfg.symbol1}  {self.tick1.bid} , "
                f"{self.cfg.symbol2}  {self.tick2.ask}"
            )
            self.log(
                f" Difference_Close : {close_gap} , "
                f"{self.cfg.symbol1}  {self.tick1.ask} , "
                f"{self.cfg.symbol2}  {self.tick2.bid}"
            )

            fut_sp = self.future_spread()
            spot_sp = self.spot_spread()
            self.log(
                f"PRICES | {self.cfg.symbol1}: Bid={self.tick1.bid:.4f} Ask={self.tick1.ask:.4f} "
                f"(Spread: {fut_sp:.4f}) | "
                f"{self.cfg.symbol2}: Bid={self.tick2.bid:.4f} Ask={self.tick2.ask:.4f} "
                f"(Spread: {spot_sp:.4f})"
            )

        # Set status lines (matching MQL5 format exactly)
        for i, sc in enumerate(self.cfg.sets):
            pairs = self.get_level_trade_pairs(i)
            # State logic: CLOSE > OPEN > PENDING > blank
            if self.level_locked[i]:
                state = " [CLOSE]"
            elif pairs > 0:
                state = " [OPEN]"
            elif sc.diff_to_trade != 0:
                state = " [PENDING]"
            else:
                state = ""
            hold = self.get_hold_status(i)
            self.log(
                f" Set_{i:<2} : {pairs}/{sc.number_of_pairs} | "
                f"Open:{sc.diff_to_trade} Cut:{sc.diff_to_cut}{state}{hold}"
            )

        ep = self.get_expected_profit()
        pnl = self.get_total_pnl()
        self.log(f" ExpProfit: ${ep:.2f} | PnL: ${pnl:.2f} | Missed Entries: {self.missed_entries_total}")

        if self.cfg.enable_basket_martingale:
            b_pnl = self.get_basket_net_pnl()
            b_target = self.get_basket_required_target()
            b_pairs = self.get_basket_open_pairs_count()
            b_lots = self.get_basket_total_lots()
            self.log(
                f" BASKET MARTINGALE STATUS | Level: {b_pairs}/{self.cfg.max_martingale_levels} | "
                f"Total Lots: {b_lots:.2f} | Net PnL: ${b_pnl:.2f} / Target: ${b_target:.2f} "
                f"(Desired Net: ${self.cfg.desired_net_profit_usd:.2f}) | "
                f"Initial Gap: {self.basket_initial_gap:.2f} | Last Entry Gap: {self.last_entry_gap:.2f}"
            )
        self.log("===========================================")

    def close_basket_and_reset(self, reason: str) -> None:
        """
        Closes all open legs of the active basket simultaneously and resets basket state.
        Unlocks levels if trade_on_same_level is True, or resets condition_prev so a new basket can start.
        """
        self.trade_in_progress = False
        net_pnl = self.get_basket_net_pnl()
        open_pairs = self.get_basket_open_pairs_count()
        total_lots = self.get_basket_total_lots()
        self.log(
            f">>> CLOSING ENTIRE BASKET [{reason}] | "
            f"Open Pairs: {open_pairs} | Total Lots: {total_lots:.2f} | Live Net PnL: ${net_pnl:.2f}"
        )
        self.close_all_trades()

        # Reset structure and level flags for next basket cycle
        for i in range(11):
            self.level_filled[i] = False
            self.actual_open_gap[i] = 0.0
            self.level_closing[i] = False
            self.condition_prev[i] = False
            self.level_locked[i] = False

        self.od = [OrderDetails() for _ in range(ORDER_NUM)]
        self.basket_initial_gap = 0.0
        self.last_entry_gap = 0.0
        self.basket_active_level_count = 0
        self.log(">>> BASKET SUCCESSFULLY CLOSED & RESET — ready for next opportunity")

    # ───────────────────────────────────────────────
    # Initialization (MQL5: OnInit — fresh attach)
    # ───────────────────────────────────────────────

    def initialize(self) -> None:
        """MQL5: OnInit — fresh attach initialization."""
        self.trade_in_progress = False
        self.trade_lock_time = 0.0

        # Count active sets (diffToTrade != 0)
        self.total_pairs = sum(1 for sc in self.cfg.sets if sc.diff_to_trade != 0)

        # Fresh attach — all unlocked
        for i in range(11):
            self.level_filled[i] = False
            self.condition_prev[i] = False
            self.actual_open_gap[i] = 0.0
            self.level_closing[i] = False
            self.level_locked[i] = False

        # Store initial diffToTrade values (for future param change detection)
        for i, sc in enumerate(self.cfg.sets):
            self.prev_diff_to_trade[i] = sc.diff_to_trade

        # Recover any open positions into structure
        self.recover_structure()

        # Print startup info (matching MQL5 format)
        self.log("===========================================")
        self.log("HRX Arbitrage EA v5 (Python port for N-Trader)")
        self.log(f"Direction              : SELL")
        self.log(f"Symbol1                : {self.cfg.symbol1}")
        self.log(f"Symbol2                : {self.cfg.symbol2}")
        self.log(f"Future Lot (Symbol1)   : {self.cfg.future_lot}")
        self.log(f"Spot Lot (Symbol2)     : {self.cfg.spot_lot}")
        self.log(f"Magic Number           : {self.cfg.magic_number}")
        self.log(f"Open Overshoot         : {self.cfg.open_overshoot}")
        self.log(f"Close Overshoot        : {self.cfg.cut_overshoot}")
        self.log(f"Min Hold Minutes       : {self.cfg.min_hold_minutes}")
        self.log(f"Active Sets            : {self.total_pairs}")
        self.log(f"Account Label          : {self.cfg.account_label}")
        self.log("===========================================")

    # ───────────────────────────────────────────────
    # Main Loop (MQL5: OnTick equivalent)
    #
    # Flow order (matching MQL5 exactly):
    #   1. Trade lock check
    #   2. Recover structure + check orphans
    #   3. Calculate gaps
    #   4. Print status
    #   5. Profit target check → close all + lock if hit
    #   6. Place trades (sequential, one per cycle) → return if any fires
    #   7. Close trades (all sets checked if no open fired)
    # ───────────────────────────────────────────────

    def run(self) -> None:
        self.initialize()
        self.running = True
        self.log("Gold arbitrage bot started (SELL direction)")
        self.log(
            f"Configured Lot Sizes -> Future ({self.cfg.symbol1}): {self.cfg.future_lot} lots | "
            f"Spot ({self.cfg.symbol2}): {self.cfg.spot_lot} lots"
        )
        self.log(
            "Spread log ->",
            os.path.abspath(self.cfg.spread_log_path),
            "(open this in spread_dashboard.html)",
        )

        while self.running:
            try:
                if not self.refresh_ticks():
                    time.sleep(self.cfg.poll_interval_seconds)
                    continue

                # Trade lock check (MQL5: isTradeLocked)
                if self.is_trade_locked():
                    self.log_throttled("trade_lock", ">>> TRADE IN PROGRESS — waiting...")
                    time.sleep(self.cfg.poll_interval_seconds)
                    continue

                # Recover structure and check orphans (every cycle, matching MQL5 OnTick)
                self.recover_structure()
                self.check_orphan_trades()

                # Calculate gaps (SELL direction)
                open_gap = round(self.tick1.bid - self.tick2.ask, 2)
                close_gap = round(self.tick1.ask - self.tick2.bid, 2)

                # 🚀 ZERO-LATENCY HOT PATH: Evaluate & Fire Trade Orders IMMEDIATELY before any logging I/O!
                trade_executed = False

                # ─── Basket Martingale Mode vs Legacy Mode ───
                if self.cfg.enable_basket_martingale:
                    # 1. Evaluate Unified Basket Exit Conditions
                    should_close, exit_reason, live_net_pnl, target_limit = self.check_basket_exit_conditions()
                    if should_close and self.is_all_hold_complete():
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
                        # Initial Entry (Level 1) — check if open_gap reaches initial_entry_gap
                        entry_trigger_gap = self.cfg.initial_entry_gap if self.cfg.initial_entry_gap > 0 else (self.cfg.sets[0].diff_to_trade if len(self.cfg.sets) > 0 else 41.60)
                        if open_gap >= entry_trigger_gap:
                            fut_vol, spot_vol = self.get_martingale_lots(0)
                            # 🚀 FIRE ORDER IMMEDIATELY!
                            trade_executed = self.place_trade(1, entry_trigger_gap, 0, open_gap, fut_vol, spot_vol)
                    elif open_pairs < self.cfg.max_martingale_levels:
                        # Recovery Entry (Level N+1)
                        next_level_index = open_pairs
                        target_adverse_trigger = round(self.last_entry_gap + self.cfg.martingale_gap_step, 2)
                        next_fut_vol, next_spot_vol = self.get_martingale_lots(next_level_index)

                        # Risk Checks: Adverse Gap Limit & Total Exposure Limit
                        adverse_ok = open_gap <= self.cfg.max_allowed_adverse_gap
                        exposure_ok = (total_lots + next_fut_vol + next_spot_vol) <= self.cfg.max_total_lot_exposure

                        if open_gap >= target_adverse_trigger and adverse_ok and exposure_ok:
                            set_idx = min(next_level_index, len(self.cfg.sets) - 1)
                            # 🚀 FIRE ORDER IMMEDIATELY (Logging happens inside/after place_trade)!
                            trade_executed = self.place_trade(1, target_adverse_trigger, set_idx, open_gap, next_fut_vol, next_spot_vol)
                else:
                    # Legacy Set-by-Set Mode
                    # ─── Profit target check (MQL5: getExpectedProfit) ───
                    ep = self.get_expected_profit()
                    pnl = self.get_total_pnl()
                    if ep > 0 and pnl >= ep and self.is_all_hold_complete():
                        self.log(f">>> PROFIT TARGET HIT | PnL ${pnl:.2f} >= ${ep:.2f}")
                        self.close_all_and_lock()
                        continue

                    # ─── Place trades — check all satisfied sets (highest level first on spike) ───
                    for i in reversed(range(len(self.cfg.sets))):
                        sc = self.cfg.sets[i]
                        if sc.diff_to_trade != 0:
                            if self.place_trade(sc.number_of_pairs, sc.diff_to_trade, i, open_gap):
                                trade_executed = True

                    # ─── Close trades (MQL5: all sets checked, only if no open fired) ───
                    if not trade_executed:
                        for i, sc in enumerate(self.cfg.sets):
                            if sc.diff_to_cut != 0:
                                self.close_trade(sc.diff_to_cut, i, close_gap)

                # 📝 POST-EXECUTION I/O: Logging and status printing happen AFTER order dispatch logic!
                self.log_spread()
                self.print_status(open_gap, close_gap)

            except KeyboardInterrupt:
                self.log(">>> CLOSE ALL — keyboard interrupt (Ctrl+C)")
                self.close_all_and_lock()
                self.running = False
            except Exception as exc:
                self.log("Main-loop error:", repr(exc))

            time.sleep(self.cfg.poll_interval_seconds)

    def stop(self) -> None:
        self.running = False


# ============================= LOCAL TICKET TAGGING =============================

class LocalTicketTagStore:
    """
    Persists ticket -> {set_name, leg, magic, open_time} to a JSON file,
    since the nTrader bridge does not let us attach a comment/magic to an
    order or read one back from get_open_positions().

    This is the ONLY source of truth for which set/leg a position belongs
    to. If this file is deleted, the bot will treat all currently-open
    positions as untagged (not managed) rather than guessing -- that is
    intentional, to avoid the bot auto-closing something it doesn't
    actually understand.
    """

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


# ============================= nTRADER CONNECTION LAYER =============================

class NTraderAdapter:
    """
    Real nTrader bridge adapter.

    Wraps `ntrader.bridge` and adds the local tagging layer described above
    so GoldArbitrageBot's existing magic/comment-based logic keeps working.
    """

    def __init__(
        self,
        magic_number: int,
        tag_store: Optional[LocalTicketTagStore] = None,
        trade_response_wait_seconds: float = 0.0,
    ):
        import ntrader  # deferred import so this module can be unit-tested off-platform
        self.bridge = ntrader.bridge
        self.magic_number = magic_number
        self.tags = tag_store or LocalTicketTagStore()
        self.trade_response_wait_seconds = trade_response_wait_seconds
        self._last_tick_warning: Dict[str, float] = {}

    def _warn_throttled(self, key: str, message: str, min_interval: float = 3.0) -> None:
        now = time.monotonic()
        last = self._last_tick_warning.get(key, 0.0)
        if now - last < min_interval:
            return
        self._last_tick_warning[key] = now
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)

    # ---------- Ticks ----------

    def get_tick(self, symbol: str) -> Optional[Tick]:
        raw = self.bridge.get_tick(symbol)
        if not raw:
            self._warn_throttled(
                f"tick:{symbol}",
                f"get_tick({symbol!r}) returned {raw!r} -- symbol may not exist, "
                f"not be subscribed/in Market Watch, or 'get_tick' may not be the "
                f"right bridge method for this account (compare against "
                f"bridge.latest_tick if available).",
            )
            return None
        bid = raw.get("bid")
        ask = raw.get("ask")
        if bid is None or ask is None:
            self._warn_throttled(
                f"tick_fields:{symbol}",
                f"get_tick({symbol!r}) returned {raw!r} but bid/ask missing.",
            )
            return None

        broker_time = 0.0
        if isinstance(raw, dict):
            for t_key in ("time", "time_msc", "timestamp", "server_time"):
                if t_key in raw and raw[t_key]:
                    try:
                        t_val = float(raw[t_key])
                        if t_val > 1e11:
                            t_val /= 1000.0
                        broker_time = t_val
                        break
                    except (TypeError, ValueError):
                        pass

        return Tick(bid=float(bid), ask=float(ask), broker_time=broker_time)

    # ---------- Positions ----------

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
                self._parse_iso(tag["open_time"])
                if tag
                else datetime.now(timezone.utc)  # untagged position: no known open time
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

    # ---------- Orders ----------

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

    def market_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        deviation: int,
        magic: int,
        comment: str,
    ) -> str:
        side = side.strip().upper()
        if side == "BUY":
            self._send_buy_order(symbol, volume, comment=comment)
        elif side == "SELL":
            self._send_sell_order(symbol, volume, comment=comment)
        else:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")

        if self.trade_response_wait_seconds > 0:
            time.sleep(self.trade_response_wait_seconds)
        response = self.bridge.get_last_trade_response()

        if self._response_indicates_failure(response):
            reason = response.get("msg") if isinstance(response, dict) else None
            raise RuntimeError(
                f"nTrader rejected {side} {symbol} order"
                + (f": {reason}" if reason else "")
                + f" | raw={response!r}"
            )

        ticket = self._extract_ticket_from_response(response)
        if ticket is None:
            raise RuntimeError(
                f"Order for {side} {symbol} reported success but no ticket was found "
                f"in 'res_ord' (or fallback keys): {response!r}"
            )

        open_time = self._server_time_from_response(response) or datetime.now(timezone.utc)
        try:
            self.tags.tag(str(ticket), magic=magic, comment=comment, open_time=open_time)
        except Exception as exc:
            print(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Warning: Failed to save ticket tag for ticket={ticket}: {exc}",
                flush=True,
            )
        return str(ticket)

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
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Sends BOTH Leg 1 and Leg 2 orders to nTrader SIMULTANEOUSLY in parallel
        threads with 0ms delay, then verifies open positions and tags both.
        """
        before_pos = self.bridge.get_open_positions() or []
        before_tickets = {str(p.get("ticket")) for p in before_pos if p.get("ticket") is not None}

        def _send_leg1():
            side1_upper = side1.strip().upper()
            if side1_upper == "BUY":
                self._send_buy_order(symbol1, volume1, comment=comment1)
            elif side1_upper == "SELL":
                self._send_sell_order(symbol1, volume1, comment=comment1)

        def _send_leg2():
            side2_upper = side2.strip().upper()
            if side2_upper == "BUY":
                self._send_buy_order(symbol2, volume2, comment=comment2)
            elif side2_upper == "SELL":
                self._send_sell_order(symbol2, volume2, comment=comment2)

        # Fire BOTH Leg 1 and Leg 2 orders concurrently in parallel threads
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_send_leg1)
            f2 = executor.submit(_send_leg2)
            f1.result()
            f2.result()

        # Poll positions with retries to ensure new tickets are captured cleanly
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
                        try:
                            self.tags.tag(ticket1, magic=magic, comment=comment1, open_time=now_utc)
                        except Exception:
                            pass
                    elif sym == sym2_clean and ticket2 is None:
                        ticket2 = t_str
                        try:
                            self.tags.tag(ticket2, magic=magic, comment=comment2, open_time=now_utc)
                        except Exception:
                            pass

            if ticket1 and ticket2:
                break
            if attempt < 11:
                time.sleep(0.05)

        return ticket1, ticket2

    def close_positions_simultaneous(self, tickets: List[str]) -> None:
        if not tickets:
            return

        def _do_close(t_str: str) -> None:
            try:
                self.bridge.close_position(int(t_str))
            except Exception as exc:
                print(f"Error issuing close for ticket {t_str}: {exc}", flush=True)

        # Dispatch ALL close requests concurrently in parallel threads with 0ms delay
        with ThreadPoolExecutor(max_workers=len(tickets)) as executor:
            executor.map(_do_close, tickets)

        if self.trade_response_wait_seconds > 0:
            time.sleep(self.trade_response_wait_seconds)

        for t in tickets:
            try:
                self.tags.untag(str(t))
            except Exception:
                pass

    def close_position(self, ticket: str, deviation: int) -> bool:
        self.bridge.close_position(int(ticket))
        time.sleep(self.trade_response_wait_seconds)
        response = self.bridge.get_last_trade_response()

        if self._response_indicates_failure(response):
            reason = response.get("msg") if isinstance(response, dict) else None
            self.log_response_for_debug(
                f"close_position failed for ticket={ticket}" + (f" ({reason})" if reason else ""),
                response,
            )
            return False

        self.tags.untag(str(ticket))
        return True

    # ---------- Response parsing helpers (UNVERIFIED -- confirm against real responses) ----------

    #: Confirmed via live log: MT4/5-style TRADE_RETCODE_DONE.
    TRADE_RETCODE_DONE = 10009

    @staticmethod
    def _extract_ticket_from_response(response: Any) -> Optional[str]:
        if not isinstance(response, dict):
            return None
        # "res_ord" is the confirmed field (0/falsy on a rejected order).
        for key in ("res_ord", "ticket", "order", "order_id", "ticket_id", "deal", "deal_id"):
            value = response.get(key)
            if value:
                try:
                    return str(int(value))
                except (TypeError, ValueError):
                    return str(value)
        return None

    @classmethod
    def _response_indicates_failure(cls, response: Any) -> bool:
        if response is None:
            return True
        if not isinstance(response, dict):
            return False

        # Confirmed signal: status reuses MT4/5 trade return codes.
        # 10009 = TRADE_RETCODE_DONE. Anything else is a rejection.
        if "status" in response:
            try:
                status = int(response.get("status"))
            except (TypeError, ValueError):
                status = None
            if status is not None:
                return status != cls.TRADE_RETCODE_DONE

        # Fallback if "status" is ever missing
        if response.get("msg"):
            return True

        return False

    @staticmethod
    def _server_time_from_response(response: Any) -> Optional[datetime]:
        if not isinstance(response, dict):
            return None
        raw = response.get("server_time")
        if raw is None:
            return None
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _parse_iso(self, value: str) -> datetime:
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.now(timezone.utc)

    def log_response_for_debug(self, label: str, response: Any) -> None:
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), label, "->", repr(response), flush=True)


def debug_probe_trade_response() -> None:
    """
    Run this once, manually, on a DEMO account before trusting the adapter
    above. It places a tiny market order, prints the raw trade response
    object, and closes the position again.
    """
    import ntrader
    bridge = ntrader.bridge

    print("Placing tiny probe BUY order...")
    bridge.create_buy_order("XAUUSD.c", 0.01, sl=0.0, tp=0.0)
    time.sleep(1.5)
    response = bridge.get_last_trade_response()
    print("RAW get_last_trade_response() ->", repr(response))
    print("Type:", type(response))
    if isinstance(response, dict):
        print("Keys:", list(response.keys()))

    positions = bridge.get_open_positions() or []
    print("Open positions after probe order:", positions)

    if positions:
        ticket = positions[-1].get("ticket")
        print(f"Closing probe position, ticket={ticket} ...")
        bridge.close_position(int(ticket))
        time.sleep(1.5)
        close_response = bridge.get_last_trade_response()
        print("RAW close response ->", repr(close_response))


# ============================= START =============================

if __name__ == "__main__":
    cfg = StrategyConfig()
    tag_store = LocalTicketTagStore(path="ntrader_ticket_tags.json")
    broker = NTraderAdapter(
        magic_number=cfg.magic_number,
        tag_store=tag_store,
        trade_response_wait_seconds=cfg.trade_response_wait_seconds,
    )
    bot = GoldArbitrageBot(broker, cfg)
    bot.run()
