"""
Net PnL Auto-Close Script for MetaTrader 5 (MT5)
=================================================

Description:
  This script monitors your open positions in MetaTrader 5 (MT5) and automatically 
  closes ALL open trades when your total Net P&L (Profit + Swap) reaches your target 
  or max loss threshold.

Conditions Handled:
  1. Recovery Target (e.g. Current PnL = -40, Target = -18):
     - Trigger: Net PnL >= -18 (Triggers when PnL recovers/improves to -18).
  2. Max Loss Limit (e.g. Current PnL = -10, Max Loss = -18):
     - Trigger: Net PnL <= -18 (Triggers when loss hits -18).
  3. Profit Target (e.g. Target = +20):
     - Trigger: Net PnL >= 20 (Triggers when target profit +20 is reached).
"""

import sys
import time
import argparse
from datetime import datetime
from typing import List, Optional, Tuple, Dict

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] MetaTrader5 package is not installed. Run: pip install MetaTrader5")
    sys.exit(1)


def get_symbol_filling_mode(symbol: str) -> int:
    """Determine supported order filling type for the given symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC

    filling_mode = info.filling_mode
    # Bitmask checks for filling modes supported by broker
    if filling_mode & mt5.SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    elif filling_mode & mt5.SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    else:
        return mt5.ORDER_FILLING_RETURN


def close_single_position(pos, deviation: int = 20, max_retries: int = 3) -> bool:
    """Close a single open MT5 position with retry logic and filling mode detection."""
    symbol = pos.symbol
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"[WARN] Could not get tick for {symbol} to close ticket {pos.ticket}")
        return False

    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    filling = get_symbol_filling_mode(symbol)

    for attempt in range(1, max_retries + 1):
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol": symbol,
            "volume": pos.volume,
            "type": order_type,
            "price": price,
            "deviation": deviation,
            "magic": pos.magic,
            "comment": "Net PnL Auto Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"[SUCCESS] Closed Ticket #{pos.ticket} ({symbol} {pos.volume} lots) @ {result.price} | RetCode: {result.retcode}")
            return True

        # Handle filling mode errors by trying alternative filling types
        if result is not None and result.retcode in (10030, 10013):  # Unsupported filling / Invalid request
            for alt_filling in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]:
                if alt_filling == filling:
                    continue
                request["type_filling"] = alt_filling
                res_alt = mt5.order_send(request)
                if res_alt is not None and res_alt.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"[SUCCESS] Closed Ticket #{pos.ticket} with alt filling mode ({symbol}) @ {res_alt.price}")
                    return True

        err_msg = result.comment if result else "No response"
        retcode = result.retcode if result else "N/A"
        print(f"[RETRY {attempt}/{max_retries}] Failed to close Ticket #{pos.ticket}. Code: {retcode}, Msg: {err_msg}")

        # Refresh tick price for next retry attempt
        time.sleep(0.1)
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

    return False


def close_all_positions(magic_filter: Optional[int] = None, symbol_filter: Optional[str] = None) -> Tuple[int, int]:
    """Close all open positions (filtered optionally by magic or symbol). Returns (closed_count, total_count)."""
    positions = mt5.positions_get()
    if positions is None or len(positions) == 0:
        return 0, 0

    to_close = []
    for pos in positions:
        if magic_filter is not None and pos.magic != magic_filter:
            continue
        if symbol_filter is not None and pos.symbol.upper() != symbol_filter.upper():
            continue
        to_close.append(pos)

    total_count = len(to_close)
    if total_count == 0:
        return 0, 0

    print(f"\n==================================================")
    print(f"  [ACTION] TRIPLE-TRIGGER: CLOSING ALL {total_count} POSITIONS NOW!")
    print(f"==================================================")

    closed_count = 0
    for pos in to_close:
        if close_single_position(pos):
            closed_count += 1

    # Verify if any positions remain and retry once more if needed
    time.sleep(0.2)
    remaining = mt5.positions_get()
    if remaining and len(remaining) > 0:
        print("[CHECK] Re-verifying open positions...")
        for pos in remaining:
            if magic_filter is not None and pos.magic != magic_filter:
                continue
            if symbol_filter is not None and pos.symbol.upper() != symbol_filter.upper():
                continue
            print(f"[RETRY] Closing remaining ticket #{pos.ticket}")
            if close_single_position(pos):
                closed_count += 1

    print(f"==================================================")
    print(f"  [SUMMARY] Successfully Closed {closed_count} / {total_count} Positions.")
    print(f"==================================================\n")
    return closed_count, total_count


def calculate_net_pnl(magic_filter: Optional[int] = None, symbol_filter: Optional[str] = None) -> Tuple[float, List[dict]]:
    """Calculate total Net P&L (Floating Profit + Swap) across all open positions."""
    positions = mt5.positions_get()
    if positions is None:
        return 0.0, []

    total_pnl = 0.0
    pos_details = []

    for pos in positions:
        if magic_filter is not None and pos.magic != magic_filter:
            continue
        if symbol_filter is not None and pos.symbol.upper() != symbol_filter.upper():
            continue

        net_pos_pnl = pos.profit + pos.swap
        total_pnl += net_pos_pnl

        side_str = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
        pos_details.append({
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "side": side_str,
            "volume": pos.volume,
            "profit": pos.profit,
            "swap": pos.swap,
            "net_pnl": net_pos_pnl,
        })

    return total_pnl, pos_details


def parse_arguments():
    parser = argparse.ArgumentParser(description="Auto close all MT5 positions on Net PnL target or max loss.")
    parser.add_argument("--target", type=float, default=None, help="Target/Threshold PnL to trigger auto close (e.g. -18 or +20)")
    parser.add_argument("--max-loss", type=float, default=None, help="Max loss limit PnL (e.g. -18 or -50)")
    parser.add_argument("--take-profit", type=float, default=None, help="Take profit target PnL (e.g. +20)")
    parser.add_argument("--magic", type=int, default=None, help="Optional Magic Number filter")
    parser.add_argument("--symbol", type=str, default=None, help="Optional Symbol filter (e.g. GOLD_DEC26 or XAUUSD)")
    parser.add_argument("--interval", type=float, default=0.2, help="Monitoring polling interval in seconds (default 0.2s)")
    return parser.parse_args()


def main():
    args = parse_arguments()

    print("==================================================")
    print("   MT5 NET PnL AUTO CLOSE MANAGER")
    print("==================================================")

    # Initialize MT5 Connection
    if not mt5.initialize():
        print(f"[ERROR] MT5 Initialization failed: {mt5.last_error()}")
        print("Please make sure MetaTrader 5 terminal is open and logged in.")
        return

    account_info = mt5.account_info()
    if account_info is not None:
        print(f" Connected Account: {account_info.login} ({account_info.company})")
        print(f" Balance: {account_info.balance:.2f} {account_info.currency} | Equity: {account_info.equity:.2f} {account_info.currency}")
    else:
        print(" Connected to MT5 Terminal.")

    # Calculate current PnL
    initial_pnl, positions = calculate_net_pnl(magic_filter=args.magic, symbol_filter=args.symbol)
    print(f"\n Current Open Positions Count: {len(positions)}")
    print(f" Current Net P&L: {initial_pnl:+.2f}")

    if len(positions) == 0:
        print("\n[INFO] No open positions found right now.")

    # Determine Targets & Conditions
    target_recovery: Optional[float] = None
    target_profit: Optional[float] = None
    max_loss: Optional[float] = None

    if args.take_profit is not None:
        target_profit = args.take_profit

    if args.max_loss is not None:
        max_loss = args.max_loss

    if args.target is not None:
        # User supplied a single --target value (e.g. -18 or +20)
        target_val = args.target
        if target_val > initial_pnl:
            # e.g., current = -40, target = -18 (Recovery Target)
            # OR current = -10, target = +20 (Profit Target)
            target_recovery = target_val
            print(f"[AUTO-DETECT] Entered target {target_val:+.2f} is HIGHER than current PnL ({initial_pnl:+.2f}).")
            print(f"              => Set as TARGET: Close trades when Net PnL >= {target_val:+.2f}")
        else:
            # e.g., current = -10, target = -18 (Max Loss Limit)
            max_loss = target_val
            print(f"[AUTO-DETECT] Entered target {target_val:+.2f} is LOWER than current PnL ({initial_pnl:+.2f}).")
            print(f"              => Set as MAX LOSS: Close trades when Net PnL <= {target_val:+.2f}")

    # If no parameters provided via CLI, prompt interactively
    if target_recovery is None and target_profit is None and max_loss is None:
        print("\n--- Enter Target / Max Loss Settings ---")
        print("Examples:")
        print("  - If current PnL is -40 and you enter -18 -> Closes when PnL improves to -18")
        print("  - If current PnL is -10 and you enter -18 -> Closes when loss drops to -18")
        print("  - If you enter +20 -> Closes when target profit +20 is reached")
        
        user_input = input("\nEnter Target / Max Loss PnL value (e.g. -18 or 20): ").strip()
        if user_input:
            try:
                target_val = float(user_input)
                if target_val > initial_pnl:
                    target_recovery = target_val
                    print(f" -> Set TARGET: Close when Net PnL >= {target_recovery:+.2f}")
                else:
                    max_loss = target_val
                    print(f" -> Set MAX LOSS: Close when Net PnL <= {max_loss:+.2f}")
            except ValueError:
                print("[ERROR] Invalid number entered. Exiting.")
                mt5.shutdown()
                return

        # Option for dual targets
        dual_ans = input("Do you also want to add a separate Stop Loss or Profit Target? (y/n): ").strip().lower()
        if dual_ans in ('y', 'yes'):
            if max_loss is None:
                ml_input = input("Enter Max Loss PnL threshold (e.g. -50): ").strip()
                if ml_input:
                    try:
                        max_loss = float(ml_input)
                        print(f" -> Set MAX LOSS: Close when Net PnL <= {max_loss:+.2f}")
                    except ValueError:
                        pass
            if target_recovery is None and target_profit is None:
                tp_input = input("Enter Take Profit PnL target (e.g. +20): ").strip()
                if tp_input:
                    try:
                        target_profit = float(tp_input)
                        print(f" -> Set TAKE PROFIT: Close when Net PnL >= {target_profit:+.2f}")
                    except ValueError:
                        pass

    # Summary of Active Conditions
    print("\n==================================================")
    print("              ACTIVE AUTO-CLOSE RULES")
    print("==================================================")
    if target_recovery is not None:
        print(f" [RULE 1] Recovery Target  : Close ALL when Net PnL >= {target_recovery:+.2f}")
    if target_profit is not None:
        print(f" [RULE 2] Take Profit      : Close ALL when Net PnL >= {target_profit:+.2f}")
    if max_loss is not None:
        print(f" [RULE 3] Max Loss Limit   : Close ALL when Net PnL <= {max_loss:+.2f}")
    print("==================================================\n")

    if target_recovery is None and target_profit is None and max_loss is None:
        print("[ERROR] No target or max loss rule specified. Exiting.")
        mt5.shutdown()
        return

    print("Monitoring Live P&L... (Press Ctrl+C to stop script)\n")

    last_print_time = 0.0

    try:
        while True:
            current_pnl, pos_list = calculate_net_pnl(magic_filter=args.magic, symbol_filter=args.symbol)
            pos_count = len(pos_list)

            # Check trigger conditions
            should_close = False
            trigger_reason = ""

            if target_recovery is not None and current_pnl >= target_recovery:
                should_close = True
                trigger_reason = f"Recovery Target Reached! (Current PnL: {current_pnl:+.2f} >= Target: {target_recovery:+.2f})"

            elif target_profit is not None and current_pnl >= target_profit:
                should_close = True
                trigger_reason = f"Take Profit Target Reached! (Current PnL: {current_pnl:+.2f} >= Target: {target_profit:+.2f})"

            elif max_loss is not None and current_pnl <= max_loss:
                should_close = True
                trigger_reason = f"Max Loss Limit Hit! (Current PnL: {current_pnl:+.2f} <= Max Loss: {max_loss:+.2f})"

            # Execute Auto Close if triggered
            if should_close:
                print(f"\n[TRIGGER] {trigger_reason}")
                close_all_positions(magic_filter=args.magic, symbol_filter=args.symbol)
                print("[DONE] Target reached and position close task complete. Monitoring finished.")
                break

            # Live Status Update every 1 second
            now = time.time()
            if now - last_print_time >= 1.0:
                last_print_time = now
                timestamp_str = datetime.now().strftime("%H:%M:%S")

                # Build live status bar
                rules_str_parts = []
                if target_recovery is not None:
                    rules_str_parts.append(f"Recovery Target: {target_recovery:+.2f}")
                if target_profit is not None:
                    rules_str_parts.append(f"TP: {target_profit:+.2f}")
                if max_loss is not None:
                    rules_str_parts.append(f"Max Loss: {max_loss:+.2f}")

                rules_summary = " | ".join(rules_str_parts)
                print(f"[{timestamp_str}] Open Positions: {pos_count:2d} | Net P&L: {current_pnl:+8.2f} | Rules: [{rules_summary}]", end="\r", flush=True)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\n[STOP] Script stopped by user.")
    finally:
        mt5.shutdown()
        print("[INFO] MT5 connection closed.")


if __name__ == "__main__":
    main()
