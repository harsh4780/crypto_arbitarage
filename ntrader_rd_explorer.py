"""
nTrader API R&D Explorer & Tester Tool
======================================
Interactive / Automated R&D script to inspect nTrader bridge functions,
call API methods, and print formatted responses for debugging & research.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def print_header(title: str) -> None:
    print("\n" + "=" * 70)
    print(f" 🚀 {title}")
    print("=" * 70)


def print_json(data: Any, label: str = "RESPONSE") -> None:
    print(f"\n--- [{label} @ {datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ---")
    print(f"Type: {type(data).__name__}")
    try:
        print(json.dumps(data, indent=2, default=str))
    except Exception:
        print(repr(data))
    print("-" * 50)


def load_ntrader_bridge():
    try:
        import ntrader
        print("[SUCCESS] Imported `ntrader` module successfully.")
        return ntrader, ntrader.bridge
    except ImportError as exc:
        print(f"[ERROR] Could not import `ntrader` module: {exc}")
        print("Please ensure nTrader environment / package is active.")
        return None, None
    except AttributeError as exc:
        print(f"[ERROR] `ntrader` module has no `bridge` attribute: {exc}")
        return None, None


def inspect_bridge(ntrader_module: Any, bridge: Any) -> None:
    print_header("NTRADER MODULE & BRIDGE INSPECTION")

    print("\n📦 `ntrader` Module Attributes:")
    for attr in sorted(dir(ntrader_module)):
        if not attr.startswith("__"):
            val = getattr(ntrader_module, attr, None)
            print(f"  - {attr} ({type(val).__name__})")

    print("\n🌉 `ntrader.bridge` Methods & Attributes:")
    for attr in sorted(dir(bridge)):
        if not attr.startswith("__"):
            val = getattr(bridge, attr, None)
            doc = getattr(val, "__doc__", "") or ""
            doc_first_line = doc.strip().split("\n")[0] if doc else ""
            print(f"  - {attr:28s} | Type: {type(val).__name__:12s} | {doc_first_line}")


def test_get_tick(bridge: Any, symbol: Optional[str] = None) -> None:
    if not symbol:
        symbol = input("Enter symbol to fetch tick (e.g. GOLD_DEC26, XAUUSD.LMAX, XAUUSD.c): ").strip()
    if not symbol:
        symbol = "XAUUSD.LMAX"

    print(f"\n>>> Calling `bridge.get_tick('{symbol}')`...")
    try:
        tick = bridge.get_tick(symbol)
        print_json(tick, f"GET TICK ({symbol})")
    except Exception as exc:
        print(f"[EXCEPTION] get_tick('{symbol}') failed:", exc)


def test_get_open_positions(bridge: Any) -> None:
    print("\n>>> Calling `bridge.get_open_positions()`...")
    try:
        positions = bridge.get_open_positions()
        print_json(positions, "GET OPEN POSITIONS")
        if isinstance(positions, list):
            print(f"Total Open Positions Count: {len(positions)}")
    except Exception as exc:
        print("[EXCEPTION] get_open_positions() failed:", exc)


def test_get_last_trade_response(bridge: Any) -> None:
    print("\n>>> Calling `bridge.get_last_trade_response()`...")
    try:
        response = bridge.get_last_trade_response()
        print_json(response, "LAST TRADE RESPONSE")
    except Exception as exc:
        print("[EXCEPTION] get_last_trade_response() failed:", exc)


def test_create_order(bridge: Any, side: str) -> None:
    symbol = input("Enter Symbol (e.g. XAUUSD.LMAX): ").strip() or "XAUUSD.LMAX"
    vol_str = input("Enter Volume/Lot (e.g. 0.01): ").strip() or "0.01"
    try:
        volume = float(vol_str)
    except ValueError:
        print("[ERROR] Invalid volume number!")
        return

    sl_str = input("Enter SL price (default 0.0): ").strip() or "0.0"
    tp_str = input("Enter TP price (default 0.0): ").strip() or "0.0"
    comment = input("Enter Comment (default 'RD_TEST'): ").strip() or "RD_TEST"

    sl = float(sl_str)
    tp = float(tp_str)

    print(f"\n>>> Sending {side.upper()} order: {symbol} {volume} lots (SL={sl}, TP={tp}, Comment='{comment}')...")

    try:
        start_t = time.monotonic()
        if side.upper() == "BUY":
            try:
                bridge.create_buy_order(symbol, volume, sl=sl, tp=tp, comment=comment)
            except TypeError:
                bridge.create_buy_order(symbol, volume, sl=sl, tp=tp)
        else:
            try:
                bridge.create_sell_order(symbol, volume, sl=sl, tp=tp, comment=comment)
            except TypeError:
                bridge.create_sell_order(symbol, volume, sl=sl, tp=tp)

        dispatch_time_ms = (time.monotonic() - start_t) * 1000
        print(f"Order function executed in {dispatch_time_ms:.2f} ms")

        # Wait a short pause and check trade response
        time.sleep(0.3)
        test_get_last_trade_response(bridge)

        # Check open positions to verify fill
        time.sleep(0.2)
        test_get_open_positions(bridge)

    except Exception as exc:
        print(f"[EXCEPTION] create_{side.lower()}_order failed:", exc)


def test_close_position(bridge: Any) -> None:
    ticket_str = input("Enter Ticket ID to Close: ").strip()
    if not ticket_str:
        print("[ERROR] Ticket ID is required!")
        return

    try:
        ticket = int(ticket_str)
    except ValueError:
        ticket = ticket_str  # Keep string if string ticket

    print(f"\n>>> Calling `bridge.close_position({ticket})`...")
    try:
        start_t = time.monotonic()
        bridge.close_position(ticket)
        dispatch_time_ms = (time.monotonic() - start_t) * 1000
        print(f"Close function executed in {dispatch_time_ms:.2f} ms")

        time.sleep(0.3)
        test_get_last_trade_response(bridge)

        time.sleep(0.2)
        test_get_open_positions(bridge)
    except Exception as exc:
        print(f"[EXCEPTION] close_position({ticket}) failed:", exc)


def run_full_diagnostic(ntrader_module: Any, bridge: Any) -> None:
    print_header("RUNNING FULL AUTOMATED DIAGNOSTIC")
    inspect_bridge(ntrader_module, bridge)

    print("\n--- 1. Testing Default Symbols Tick Fetch ---")
    for sym in ["XAUUSD.LMAX", "GOLD_DEC26", "XAUUSD.c", "XAUUSD"]:
        test_get_tick(bridge, sym)

    print("\n--- 2. Fetching Current Open Positions ---")
    test_get_open_positions(bridge)

    print("\n--- 3. Fetching Last Trade Response ---")
    test_get_last_trade_response(bridge)


def main_menu():
    ntrader_module, bridge = load_ntrader_bridge()
    if bridge is None:
        print("\nExiting. Make sure `ntrader` package is installed and available.")
        return

    while True:
        print_header("NTRADER API R&D EXPLORER MENU")
        print("1. Inspect `ntrader` & `bridge` available methods (dir)")
        print("2. Get Tick for a Symbol (`get_tick`)")
        print("3. Get Open Positions (`get_open_positions`)")
        print("4. Get Last Trade Response (`get_last_trade_response`)")
        print("5. Send BUY Order (`create_buy_order`)")
        print("6. Send SELL Order (`create_sell_order`)")
        print("7. Close Position (`close_position`)")
        print("8. Run Full Diagnostic Scan")
        print("0. Exit")

        choice = input("\nSelect option (0-8): ").strip()

        if choice == "1":
            inspect_bridge(ntrader_module, bridge)
        elif choice == "2":
            test_get_tick(bridge)
        elif choice == "3":
            test_get_open_positions(bridge)
        elif choice == "4":
            test_get_last_trade_response(bridge)
        elif choice == "5":
            test_create_order(bridge, "BUY")
        elif choice == "6":
            test_create_order(bridge, "SELL")
        elif choice == "7":
            test_close_position(bridge)
        elif choice == "8":
            run_full_diagnostic(ntrader_module, bridge)
        elif choice == "0":
            print("\nExiting nTrader R&D Explorer. Goodbye!")
            break
        else:
            print("[INVALID CHOICE] Please select 0 to 8.")


if __name__ == "__main__":
    # If run with '--diag' flag, run non-interactive full diagnostic
    if "--diag" in sys.argv:
        m, b = load_ntrader_bridge()
        if b:
            run_full_diagnostic(m, b)
    else:
        main_menu()
