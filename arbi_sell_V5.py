"""
Gold arbitrage strategy converted from:
ARBITRAGE_BOT_EA_V11_No_Log_SHORT(1).mq5

nTrader bridge integration
---------------------------
This version wires the strategy to the real nTrader scripting API:

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
belong to which "Set" (Set1..Set5) using two broker-side fields on each
position: a `magic` number and a `comment` string like "Set1_Sell".

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

This means GoldArbitrageBot's existing strategy logic (which filters by
`magic` and `comment` prefix) keeps working unmodified. The tradeoff: if the
local JSON file is lost or a position is opened outside this bot, it will
show up as an "untagged" position with magic=0 and empty comment, and will
be IGNORED by the strategy (not managed, not auto-closed). That is a
deliberate safety choice -- silently adopting a stranger position and
closing it would be worse.

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
  - "msg"          -> human-readable error text on rejection (empty on
                       success, in principle -- reconfirm once you have a
                       successful fill logged).
  - "error"        -> was empty even on a real rejection in the sample log,
                       so it is NOT a reliable failure signal on its own.
  - "res_price"    -> fill price.
  - "server_time"  -> epoch seconds, broker server time.

This adapter now keys off "status" first (10009 = success) and reads the
ticket from "res_ord". The success behavior on a real fill (does res_ord
become nonzero, does msg stay empty) is inferred from MT4/5 convention and
the rejection sample -- reconfirm once you have a *successful* order log to
compare against.
"""

from __future__ import annotations

import json
import os
import time
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


# ============================= CONFIGURATION =============================

@dataclass
class SetConfig:
    name: str
    pairs: int
    open_difference: float
    close_difference: float
    enabled_to_open: bool = True
    slippage_after_open: float = 0.0
    slippage_after_close: float = 0.0
    op_ft_slip: float = 0.0   # Opening Future Slippage (opFt)
    op_sp_slip: float = 0.0   # Opening Spot Slippage (opSp)
    cl_ft_slip: float = 0.0   # Closing Future Slippage (clFt)
    cl_sp_slip: float = 0.0   # Closing Spot Slippage (clSp)


@dataclass
class StrategyConfig:
    # NOTE: USDJPY / XAUUSD is a TEST PAIR for exercising tick-fetch and
    # order plumbing only. They do not track the same underlying asset, so
    # (symbol1.bid - symbol2.ask) is not a meaningful arbitrage spread --
    # it will sit around -3000 to -3300 (USDJPY ~150 minus XAUUSD ~3400+)
    # and will never cross open_difference thresholds like 3.0. Swap back
    # to two same-asset symbols (e.g. a real gold future + XAUUSD) before
    # expecting process_set_open() to actually fire.
    symbol1: str = "GOLD_DEC26"
    symbol2: str = "XAUUSD.LMAX"
    future_lot: float = 1.0   # Lot size for Future leg (symbol1)
    spot_lot: float = 0.01     # Lot size for Spot leg (symbol2)
    slippage: int = 10
    magic_number: int = 415
    total_profit: float = 350.0
    total_loss: float = 200.0
    use_min_hold_minutes: bool = True
    min_hold_minutes: int = 5
    poll_interval_seconds: float = 0.10
    max_retries: int = 2
    trade_response_wait_seconds: float = 1.0
    account_label: str = "A-BOOK-LIVE"      # Label to distinguish accounts in logs (e.g. A-BOOK-LIVE / B-BOOK-DEMO)
    max_tick_age_seconds: float = 3.0       # Max allowed age of tick feed before marking stale
    max_static_tick_seconds: float = 5.0    # Warning threshold if one leg freezes while other moves
    max_future_spread: float = 2.0           # Max allowed Bid-Ask spread for Future leg to allow new trade open
    max_spot_spread: float = 2.0             # Max allowed Bid-Ask spread for Spot leg to allow new trade open
    

    # Spread logging for the HTML dashboard (see spread_dashboard.html).
    spread_log_path: str = field(
        default_factory=lambda: str(get_writable_path("spread_log.csv"))
    )
    spread_log_interval_seconds: float = 1.0

    # Individual Set parameters (Exposed to MT5 / nTrader GUI Parameters table)
    set1_enable: bool = True
    set1_pairs: int = 1
    set1_open_diff: float = 56.20
    set1_close_diff: float = 54.20

    set2_enable: bool = True
    set2_pairs: int = 1
    set2_open_diff: float = 56.30
    set2_close_diff: float = 54.20

    set3_enable: bool = True
    set3_pairs: int = 0
    set3_open_diff: float = 58.00
    set3_close_diff: float = 54.20

    set4_enable: bool = True
    set4_pairs: int = 1
    set4_open_diff: float = 57.40
    set4_close_diff: float = 54.30

    set5_enable: bool = True
    set5_pairs: int = 1
    set5_open_diff: float = 60.2
    set5_close_diff: float = 57.20

    sets: List[SetConfig] = field(default_factory=list)

    def __post_init__(self):
        if not self.sets:
            self.sets = [
                SetConfig("Set1", self.set1_pairs, self.set1_open_diff, self.set1_close_diff, enabled_to_open=self.set1_enable),
                SetConfig("Set2", self.set2_pairs, self.set2_open_diff, self.set2_close_diff, enabled_to_open=self.set2_enable),
                SetConfig("Set3", self.set3_pairs, self.set3_open_diff, self.set3_close_diff, enabled_to_open=self.set3_enable),
                SetConfig("Set4", self.set4_pairs, self.set4_open_diff, self.set4_close_diff, enabled_to_open=self.set4_enable),
                SetConfig("Set5", self.set5_pairs, self.set5_open_diff, self.set5_close_diff, enabled_to_open=self.set5_enable),
            ]


# ============================= STRATEGY (unchanged logic) =============================

class GoldArbitrageBot:
    def __init__(self, broker: BrokerAdapter, config: Optional[StrategyConfig] = None):
        self.broker = broker
        self.cfg = config or StrategyConfig()
        self.tick1: Optional[Tick] = None
        self.tick2: Optional[Tick] = None
        self.running = False
        self._last_status_print = 0.0
        self._last_spread_log = 0.0
        self._spread_log_header_written = False
        self._spread_log_error_logged = False
        self._last_price1: Optional[Tuple[float, float]] = None
        self._last_price2: Optional[Tuple[float, float]] = None
        self._last_price1_change_time: float = time.monotonic()
        self._last_price2_change_time: float = time.monotonic()

    def log(self, *parts: Any) -> None:
        prefix = f"[{self.cfg.account_label}]" if hasattr(self, "cfg") and hasattr(self.cfg, "account_label") else ""
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), prefix, *parts, flush=True)

    def log_throttled(self, key: str, *parts: Any, min_interval: float = 3.0) -> None:
        now = time.monotonic()
        if not hasattr(self, "_throttled_logs"):
            self._throttled_logs = {}
        last = self._throttled_logs.get(key, 0.0)
        if now - last < min_interval:
            return
        self._throttled_logs[key] = now
        self.log(*parts)

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

            now_mono = time.monotonic()
            now_utc_epoch = datetime.now(timezone.utc).timestamp()

            # Track price changes
            p1 = (self.tick1.bid, self.tick1.ask)
            if self._last_price1 is None or p1 != self._last_price1:
                self._last_price1 = p1
                self._last_price1_change_time = now_mono

            p2 = (self.tick2.bid, self.tick2.ask)
            if self._last_price2 is None or p2 != self._last_price2:
                self._last_price2 = p2
                self._last_price2_change_time = now_mono

            # Staleness Check 1: Broker timestamp check (if available)
            if self.tick1.broker_time > 0 and abs(now_utc_epoch - self.tick1.broker_time) > self.cfg.max_tick_age_seconds:
                self.log_throttled(
                    f"stale_broker_time:{self.cfg.symbol1}",
                    f"STALE FEED: {self.cfg.symbol1} broker timestamp is {abs(now_utc_epoch - self.tick1.broker_time):.1f}s old! Suppressing triggers.",
                )
                return False

            if self.tick2.broker_time > 0 and abs(now_utc_epoch - self.tick2.broker_time) > self.cfg.max_tick_age_seconds:
                self.log_throttled(
                    f"stale_broker_time:{self.cfg.symbol2}",
                    f"STALE FEED: {self.cfg.symbol2} broker timestamp is {abs(now_utc_epoch - self.tick2.broker_time):.1f}s old! Suppressing triggers.",
                )
                return False

            # Staleness Check 2: Frozen feed anomaly detection (one symbol moving while other is completely frozen)
            p1_age = now_mono - self._last_price1_change_time
            p2_age = now_mono - self._last_price2_change_time

            if p2_age < 2.0 and p1_age > self.cfg.max_static_tick_seconds and self.tick1.broker_time == 0:
                self.log_throttled(
                    f"frozen_tick:{self.cfg.symbol1}",
                    f"FROZEN FEED WARNING: {self.cfg.symbol1} price hasn't updated in {p1_age:.1f}s while {self.cfg.symbol2} is active. Suppressing triggers.",
                )
                return False

            if p1_age < 2.0 and p2_age > self.cfg.max_static_tick_seconds and self.tick2.broker_time == 0:
                self.log_throttled(
                    f"frozen_tick:{self.cfg.symbol2}",
                    f"FROZEN FEED WARNING: {self.cfg.symbol2} price hasn't updated in {p2_age:.1f}s while {self.cfg.symbol1} is active. Suppressing triggers.",
                )
                return False

            return True
        except Exception as exc:
            self.log_throttled("tick_refresh_exc", "Tick refresh failed:", exc)
            return False

    def strategy_positions(self) -> List[Position]:
        positions = self.broker.get_positions()
        return [
            p for p in positions
            if p.magic == self.cfg.magic_number
            and p.symbol in (self.cfg.symbol1, self.cfg.symbol2)
        ]

    def positions_for_set(self, set_name: str) -> List[Position]:
        prefix = set_name + "_"
        return [p for p in self.strategy_positions() if p.comment.startswith(prefix)]

    def count_all_sets(self) -> Dict[str, int]:
        return {s.name: len(self.positions_for_set(s.name)) for s in self.cfg.sets}

    def total_active_orders(self) -> int:
        return len(self.strategy_positions())

    def net_profit_and_loss(self) -> float:
        return sum(p.profit + p.swap for p in self.strategy_positions())

    def open_difference(self) -> float:
        assert self.tick1 is not None and self.tick2 is not None
        return self.tick1.bid - self.tick2.ask

    def close_difference(self) -> float:
        assert self.tick1 is not None and self.tick2 is not None
        return self.tick1.ask - self.tick2.bid

    def future_spread(self) -> float:
        assert self.tick1 is not None
        return self.tick1.ask - self.tick1.bid

    def spot_spread(self) -> float:
        assert self.tick2 is not None
        return self.tick2.ask - self.tick2.bid

    def minutes_since_last_trade(self, set_name: Optional[str] = None) -> int:
        positions = self.strategy_positions()
        if set_name is not None:
            prefix = set_name + "_"
            positions = [p for p in positions if p.comment.startswith(prefix)]
        if not positions:
            return -1
        newest = max(p.open_time for p in positions)
        now = datetime.now(newest.tzinfo or timezone.utc)
        return max(0, int((now - newest).total_seconds() // 60))

    def open_market_orders(self, symbol: str, side: str, count: int, comment: str, volume: Optional[float] = None) -> bool:
        opened_tickets: List[str] = []
        vol = volume if volume is not None else (self.cfg.future_lot if symbol == self.cfg.symbol1 else self.cfg.spot_lot)
        try:
            for index in range(count):
                ticket = self.broker.market_order(
                    symbol=symbol,
                    side=side,
                    volume=vol,
                    deviation=self.cfg.slippage,
                    magic=self.cfg.magic_number,
                    comment=comment,
                )
                if not ticket:
                    raise RuntimeError(f"No ticket returned for {side} {symbol}")
                opened_tickets.append(str(ticket))
                self.log(side, "opened", symbol, "ticket=", ticket, f"({index + 1}/{count})")
            return True
        except Exception as exc:
            self.log(side, "failed", symbol, "error=", exc)
            for ticket in opened_tickets:
                self.close_position_with_retry(ticket)
            return False

    def process_set_open(self, set_cfg: SetConfig) -> None:
        if set_cfg.pairs <= 0 or not set_cfg.enabled_to_open:
            return
        if self.positions_for_set(set_cfg.name):
            set_cfg.enabled_to_open = False
            return

        open_diff = self.open_difference()

        # Near-Miss Open Diagnostic: Price came close (within 0.50 pts) to open threshold
        if open_diff >= set_cfg.open_difference - 0.50 and open_diff < set_cfg.open_difference:
            gap = set_cfg.open_difference - open_diff
            self.log_throttled(
                f"near_miss_open:{set_cfg.name}",
                f"NEAR-MISS OPEN ALERT ({set_cfg.name}): OpenDiff reached {open_diff:.5f} (Target={set_cfg.open_difference:.5f}). Short by {gap:.4f} pts!",
            )

        if open_diff < set_cfg.open_difference:
            return

        # Check individual Bid-Ask spreads before opening
        fut_sp = self.future_spread()
        spot_sp = self.spot_spread()

        if fut_sp > self.cfg.max_future_spread:
            self.log_throttled(
                f"high_fut_spread:{set_cfg.name}",
                f"{set_cfg.name} OPEN BLOCKED: Future spread ({fut_sp:.4f}) exceeds max_future_spread ({self.cfg.max_future_spread:.4f})"
            )
            return

        if spot_sp > self.cfg.max_spot_spread:
            self.log_throttled(
                f"high_spot_spread:{set_cfg.name}",
                f"{set_cfg.name} OPEN BLOCKED: Spot spread ({spot_sp:.4f}) exceeds max_spot_spread ({self.cfg.max_spot_spread:.4f})"
            )
            return

        # Disable set IMMEDIATELY so it executes ONLY ONCE and never retries/loops
        set_cfg.enabled_to_open = False

        # Capture expected signal execution prices BEFORE order dispatch (0ms lag)
        exp_fut_bid = self.tick1.bid
        exp_spot_ask = self.tick2.ask

        self.log(
            set_cfg.name,
            "OPEN signal triggered (ONE-TIME EXECUTION):",
            f"difference={self.open_difference():.5f}",
            f"threshold={set_cfg.open_difference:.5f}",
        )

        all_ok = True
        for pair_idx in range(set_cfg.pairs):
            t1, t2 = self.broker.market_order_pair(
                symbol1=self.cfg.symbol1,
                side1="SELL",
                comment1=set_cfg.name + "_Sell",
                symbol2=self.cfg.symbol2,
                side2="BUY",
                comment2=set_cfg.name + "_Buy",
                volume1=self.cfg.future_lot,
                volume2=self.cfg.spot_lot,
                magic=self.cfg.magic_number,
            )
            self.log(
                f"SIMULTANEOUS OPEN ({pair_idx+1}/{set_cfg.pairs}) -> "
                f"Leg1 Future ({self.cfg.symbol1}, {self.cfg.future_lot} lots): ticket={t1} | Leg2 Spot ({self.cfg.symbol2}, {self.cfg.spot_lot} lots): ticket={t2}"
            )
            if not t1 or not t2:
                all_ok = False
                self.log("ERROR: One or both legs failed during simultaneous open! Performing emergency rollback...")
                if t1:
                    self.close_position_with_retry(t1)
                if t2:
                    self.close_position_with_retry(t2)
                break

        if all_ok:
            self.refresh_ticks()
            set_cfg.slippage_after_open = self.open_difference()

            # Calculate opening leg slippages after execution (without execution lag)
            pos_list = self.positions_for_set(set_cfg.name)
            p_fut = next((p for p in pos_list if p.symbol == self.cfg.symbol1 and p.open_price > 0), None)
            p_spot = next((p for p in pos_list if p.symbol == self.cfg.symbol2 and p.open_price > 0), None)

            fill_fut = p_fut.open_price if p_fut else self.tick1.bid
            fill_spot = p_spot.open_price if p_spot else self.tick2.ask

            set_cfg.op_ft_slip = exp_fut_bid - fill_fut       # SELL: expected_bid - fill_price
            set_cfg.op_sp_slip = fill_spot - exp_spot_ask       # BUY: fill_price - expected_ask
        else:
            self.log("Simultaneous open failed or incomplete; cleaning up set", set_cfg.name)
            self.close_set_orders(set_cfg.name)

    def check_sets_open(self) -> None:
        for set_cfg in self.cfg.sets:
            self.process_set_open(set_cfg)

    def close_position_with_retry(self, ticket: str) -> bool:
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                if self.broker.close_position(ticket, self.cfg.slippage):
                    self.log("Closed ticket=", ticket, "attempt=", attempt)
                    return True
            except Exception as exc:
                self.log("Close attempt failed ticket=", ticket, "attempt=", attempt, "error=", exc)
            if attempt < self.cfg.max_retries:
                time.sleep(0.05)
        self.log("FAILED to close ticket=", ticket)
        return False

    def close_set_orders(self, set_name: str) -> None:
        positions = list(self.positions_for_set(set_name))
        tickets = [p.ticket for p in positions]
        if not tickets:
            return
        if hasattr(self.broker, "close_positions_simultaneous"):
            self.log(f"SIMULTANEOUS CLOSE set {set_name} tickets: {tickets}")
            self.broker.close_positions_simultaneous(tickets)
        else:
            for position in positions:
                self.close_position_with_retry(position.ticket)

        # Atomic Verification: Ensure all positions in this set are closed.
        remaining = list(self.positions_for_set(set_name))
        if remaining:
            self.log(f"WARNING: {len(remaining)} position(s) remain open for set {set_name}! Retrying emergency close...")
            for pos in remaining:
                self.close_position_with_retry(pos.ticket)

    def close_all_orders(self) -> None:
        positions = list(self.strategy_positions())
        for position in positions:
            self.close_position_with_retry(position.ticket)

    def process_set_close(self, set_cfg: SetConfig) -> None:
        positions = self.positions_for_set(set_cfg.name)
        if not positions:
            return

        close_diff = self.close_difference()

        # Near-Miss Close Diagnostic: Price came close (within 0.50 pts) to cut threshold
        if close_diff <= set_cfg.close_difference + 0.50 and close_diff > set_cfg.close_difference:
            gap = close_diff - set_cfg.close_difference
            self.log_throttled(
                f"near_miss_close:{set_cfg.name}",
                f"NEAR-MISS CLOSE ALERT ({set_cfg.name}): CloseDiff reached {close_diff:.5f} (Target={set_cfg.close_difference:.5f}). Short by {gap:.4f} pts!",
            )

        if close_diff <= set_cfg.close_difference:
            exp_fut_ask = self.tick1.ask if self.tick1 else 0.0
            exp_spot_bid = self.tick2.bid if self.tick2 else 0.0

            self.log(
                set_cfg.name,
                "CLOSE signal:",
                f"difference={self.close_difference():.5f}",
                f"cut={set_cfg.close_difference:.5f}",
            )
            self.close_set_orders(set_cfg.name)
            self.refresh_ticks()
            set_cfg.slippage_after_close = self.close_difference()

            # Calculate closing leg slippages
            fill_fut_close = self.tick1.ask if self.tick1 else exp_fut_ask
            fill_spot_close = self.tick2.bid if self.tick2 else exp_spot_bid

            set_cfg.cl_ft_slip = fill_fut_close - exp_fut_ask    # BUY back: fill_price - expected_ask
            set_cfg.cl_sp_slip = exp_spot_bid - fill_spot_close   # SELL back: expected_bid - fill_price

    def check_sets_close(self) -> None:
        for set_cfg in self.cfg.sets:
            self.process_set_close(set_cfg)

    def close_manual_trades(self) -> None:
        for set_cfg in self.cfg.sets:
            if set_cfg.pairs <= 0:
                continue
            count = len(self.positions_for_set(set_cfg.name))
            expected = set_cfg.pairs * 2
            if 0 < count < expected:
                self.log(set_cfg.name, "partial/manual close detected:", count, "/", expected)
                self.close_set_orders(set_cfg.name)

    def initialize_set_states(self) -> None:
        for set_cfg in self.cfg.sets:
            count = len(self.positions_for_set(set_cfg.name))
            set_cfg.enabled_to_open = count == 0
            self.log(set_cfg.name, "enabled_to_open=", set_cfg.enabled_to_open)

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

    def print_status(self) -> None:
        now = time.monotonic()
        if now - self._last_status_print < 2.0:
            return
        self._last_status_print = now
        self.log("============== EA STATUS ==============")
        if self.tick1 is not None and self.tick2 is not None:
            open_diff = self.open_difference()
            close_diff = self.close_difference()
            now_m = time.monotonic()
            p1_age = now_m - self._last_price1_change_time if self._last_price1_change_time else 0.0
            p2_age = now_m - self._last_price2_change_time if self._last_price2_change_time else 0.0
            fut_sp = self.future_spread()
            spot_sp = self.spot_spread()
            self.log(
                f"PRICES | {self.cfg.symbol1}: Bid={self.tick1.bid:.4f} Ask={self.tick1.ask:.4f} (Spread: {fut_sp:.4f}, Age: {p1_age:.1f}s) | "
                f"{self.cfg.symbol2}: Bid={self.tick2.bid:.4f} Ask={self.tick2.ask:.4f} (Spread: {spot_sp:.4f}, Age: {p2_age:.1f}s)"
            )
            self.log(
                f"DIFF   | OpenDiff={open_diff:.5f} (Bid1-Ask2) | CloseDiff={close_diff:.5f} (Ask1-Bid2)"
            )
        for set_cfg in self.cfg.sets:
            active = len(self.positions_for_set(set_cfg.name))
            total = set_cfg.pairs * 2
            if set_cfg.enabled_to_open:
                state = "PENDING"
            elif active > 0:
                held = self.minutes_since_last_trade(set_cfg.name)
                state = (
                    f"ACTIVE | Hold:{held}/{self.cfg.min_hold_minutes}m"
                    if self.cfg.use_min_hold_minutes and held <= self.cfg.min_hold_minutes
                    else "ACTIVE"
                )
            else:
                state = "CLOSED"

            slip_parts = []
            if set_cfg.op_ft_slip != 0.0:
                slip_parts.append(f"opFt={set_cfg.op_ft_slip:+.2f}")
            if set_cfg.op_sp_slip != 0.0:
                slip_parts.append(f"opSp={set_cfg.op_sp_slip:+.2f}")
            if set_cfg.cl_ft_slip != 0.0:
                slip_parts.append(f"clFt={set_cfg.cl_ft_slip:+.2f}")
            if set_cfg.cl_sp_slip != 0.0:
                slip_parts.append(f"clSp={set_cfg.cl_sp_slip:+.2f}")

            slip_str = (" | " + " ".join(slip_parts)) if slip_parts else ""

            self.log(
                f"{set_cfg.name}: {active}/{total}",
                f"Open={set_cfg.open_difference:.4f}",
                f"Cut={set_cfg.close_difference:.4f}",
                state + slip_str,
            )
        self.log("=======================================")

    def run(self) -> None:
        self.initialize_set_states()
        self.running = True
        self.log("Gold arbitrage bot started")
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

                self.log_spread()

                active_orders = self.total_active_orders()

                if active_orders > 0:
                    can_check = True
                    if self.cfg.use_min_hold_minutes:
                        can_check = self.minutes_since_last_trade() >= self.cfg.min_hold_minutes

                    if can_check:
                        self.check_sets_close()
                        net_profit = self.net_profit_and_loss()
                        if net_profit > self.cfg.total_profit or net_profit <= -self.cfg.total_loss:
                            self.log("Closing all trades on P/L:", net_profit)
                            self.close_all_orders()
                            time.sleep(self.cfg.poll_interval_seconds)
                            continue
                    else:
                        held = self.minutes_since_last_trade()
                        self.log_throttled(
                            "hold_time_blocked",
                            f"HOLD TIME BLOCK: Active position exists but close check BLOCKED by min_hold_minutes (Held: {held}m / Req: {self.cfg.min_hold_minutes}m)",
                        )

                    self.close_manual_trades()

                self.check_sets_open()
                self.print_status()

            except KeyboardInterrupt:
                self.log("Stop requested by keyboard")
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
        trade_response_wait_seconds: float = 1.0,
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
            self.bridge.create_buy_order(symbol, volume, sl=0.0, tp=0.0)
        elif side == "SELL":
            self.bridge.create_sell_order(symbol, volume, sl=0.0, tp=0.0)
        else:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")

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
        Sends BOTH Leg 1 and Leg 2 orders to nTrader SIMULTANEOUSLY
        without sleeping between them, then verifies open positions and tags both.
        """
        before_pos = self.bridge.get_open_positions() or []
        before_tickets = {str(p.get("ticket")) for p in before_pos if p.get("ticket") is not None}

        # Send Order 1 (Future leg)
        side1_upper = side1.strip().upper()
        if side1_upper == "BUY":
            self.bridge.create_buy_order(symbol1, volume1, sl=0.0, tp=0.0)
        elif side1_upper == "SELL":
            self.bridge.create_sell_order(symbol1, volume1, sl=0.0, tp=0.0)

        # Send Order 2 IMMEDIATELY (0ms delay) (Spot leg)
        side2_upper = side2.strip().upper()
        if side2_upper == "BUY":
            self.bridge.create_buy_order(symbol2, volume2, sl=0.0, tp=0.0)
        elif side2_upper == "SELL":
            self.bridge.create_sell_order(symbol2, volume2, sl=0.0, tp=0.0)

        # Poll positions with retries to ensure new tickets are captured cleanly
        ticket1: Optional[str] = None
        ticket2: Optional[str] = None

        sym1_clean = symbol1.strip().upper()
        sym2_clean = symbol2.strip().upper()

        for attempt in range(3):
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
            if attempt < 2:
                time.sleep(0.3)

        return ticket1, ticket2

    def close_positions_simultaneous(self, tickets: List[str]) -> None:
        if not tickets:
            return
        for t in tickets:
            try:
                self.bridge.close_position(int(t))
            except Exception as exc:
                print(f"Error issuing close for ticket {t}: {exc}", flush=True)

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
        # Other keys kept as a fallback only, in case the shape ever varies
        # by order type or broker config.
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
        # 10009 = TRADE_RETCODE_DONE. Anything else observed so far
        # (e.g. 10014 = INVALID_VOLUME) is a rejection.
        if "status" in response:
            try:
                status = int(response.get("status"))
            except (TypeError, ValueError):
                status = None
            if status is not None:
                return status != cls.TRADE_RETCODE_DONE

        # Fallback if "status" is ever missing: a non-empty "msg" was the
        # human-readable error text on the one rejection we've observed.
        # Note "error" was empty even on that rejection, so it is NOT used
        # as a failure signal on its own.
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
    object, and closes the position again. Use the printed output to
    confirm/adjust _extract_ticket_from_response() and
    _response_indicates_failure().
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



