"""
FastAPI Server & WebSocket Broadcast Server
============================================
Serves the web dashboard, supports diffToTrade / diffToCut EA parameters,
dynamic Currency switching (BTC / ETH), dynamic contract expiry selection,
and manual/automated order execution for Delta Exchange & Deribit.
"""

import time
import asyncio
import logging
from typing import Set, Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from config import config
from mt5_connector import MT5Connector
from delta_connector import DeltaConnector
from deribit_connector import DeribitConnector
from bybit_connector import BybitConnector
from binance_connector import BinanceConnector
from arbitrage_engine import ArbitrageEngine

logger = logging.getLogger(__name__)

# Active exchange provider ('bybit', 'deribit', 'delta', 'binance')
active_exchange_source = "binance"

# Active currency ('BTC' or 'ETH')
active_currency = "ETH"

# Active Contract per exchange
active_bybit_contract = config.BYBIT_CONTRACT or "BTCUSDT-02OCT26"
active_deribit_contract = config.DERIBIT_CONTRACT or ""
active_binance_contract = config.BINANCE_USDT_CONTRACT or "ETHUSDT_261225"

# System instances
mt5_conn = MT5Connector(symbol=f"{active_currency}USD")
bybit_conn = BybitConnector(currency=active_currency, testnet=config.BYBIT_TESTNET, api_key=config.BYBIT_API_KEY, api_secret=config.BYBIT_API_SECRET, category=config.BYBIT_CATEGORY)
delta_conn = DeltaConnector(symbol=f"{active_currency}USD", base_url=config.DELTA_BASE_URL, api_key=config.DELTA_API_KEY, api_secret=config.DELTA_API_SECRET)
deribit_conn = DeribitConnector(currency=active_currency, client_id=config.DERIBIT_CLIENT_ID, client_secret=config.DERIBIT_CLIENT_SECRET)
binance_conn = BinanceConnector(testnet=config.BINANCE_TESTNET, api_key=config.BINANCE_API_KEY, api_secret=config.BINANCE_API_SECRET)
engine = ArbitrageEngine(diff_to_trade=4.0, diff_to_cut=0.5)

app = FastAPI(title="Crypto Arbitrage EA Terminal (Bybit Futures Demo / Live)", version="3.1.0")

# WebSocket active connection set
active_websockets: Set[WebSocket] = set()

# Request Models
class CredentialsRequest(BaseModel):
    exchange: str
    api_key: str
    api_secret: str

class ExchangeSwitchRequest(BaseModel):
    exchange: str

class CurrencySelectRequest(BaseModel):
    currency: str # 'BTC' or 'ETH'

class ContractSelectRequest(BaseModel):
    contract: str # e.g. BTC-27NOV26, ETH-25DEC26

class SettingsRequest(BaseModel):
    diff_to_trade: float   # Open Gap threshold
    diff_to_cut: float     # Close Cut Gap threshold
    trade_size: float      # Lot size
    auto_trade: bool       # Auto execution toggle
    target_profit_usd: Optional[float] = 1.0 # Target Net Profit ($1.00 USD) after all fees & slippage

class TestnetToggleRequest(BaseModel):
    testnet: bool

def get_current_is_testnet() -> bool:
    if active_exchange_source == "bybit":
        return bybit_conn.testnet
    elif active_exchange_source == "binance":
        return binance_conn.testnet
    return False

def get_active_futures_ticker() -> tuple[Optional[Dict[str, Any]], str]:
    """Helper to fetch the current futures tick based on active exchange and currency."""
    global active_exchange_source, active_currency, active_bybit_contract, active_deribit_contract, active_binance_contract
    
    if active_exchange_source == "binance":
        if not active_binance_contract or not (active_currency in active_binance_contract):
            active_binance_contract = f"{active_currency}USD_261225"
        if "_" in active_binance_contract and "USDT" not in active_binance_contract:
            tick = binance_conn.get_coinm_tick(active_binance_contract)
        else:
            tick = binance_conn.get_usdm_tick(active_binance_contract)
        return tick, active_binance_contract
    elif active_exchange_source == "bybit":
        if not active_bybit_contract or not active_bybit_contract.startswith(active_currency):
            contracts = bybit_conn.get_contract_names()
            cfg_target = (config.BYBIT_CONTRACT or "").replace("2026", "26")
            matching = [c for c in contracts if cfg_target and (cfg_target in c or c.endswith(cfg_target.split("-")[-1]))]
            active_bybit_contract = matching[0] if matching else (contracts[0] if contracts else f"{active_currency}USDT-02OCT26")
        tick = bybit_conn.get_latest_ticker(active_bybit_contract)
        return tick, active_bybit_contract
    elif active_exchange_source == "deribit":
        if not active_deribit_contract or not active_deribit_contract.startswith(active_currency):
            dated_list = deribit_conn.get_dated_futures()
            active_deribit_contract = dated_list[-3]["instrument_name"] if len(dated_list) >= 3 else f"{active_currency}-25DEC26"
        tick = deribit_conn.get_latest_ticker(active_deribit_contract)
        return tick, active_deribit_contract
    else: # delta
        return delta_conn.get_latest_tick(), delta_conn.symbol

@app.on_event("startup")
async def startup_event():
    mt5_conn.connect()
    asyncio.create_task(market_data_loop())

@app.on_event("shutdown")
async def shutdown_event():
    mt5_conn.disconnect()

async def market_data_loop():
    """Background task to fetch price data, evaluate EA gap levels, and broadcast via WebSockets."""
    global active_exchange_source, active_currency
    while True:
        try:
            mt5_tick = mt5_conn.get_latest_tick()
            futures_tick, target_sym = get_active_futures_ticker()

            active_fut_conn = (
                binance_conn if active_exchange_source == "binance" else (
                    bybit_conn if active_exchange_source == "bybit" else (
                        deribit_conn if active_exchange_source == "deribit" else delta_conn
                    )
                )
            )
            evaluation = engine.evaluate(mt5_tick, futures_tick, mt5_conn=mt5_conn, futures_conn=active_fut_conn)
            evaluation["exchange_source"] = active_exchange_source.upper()
            evaluation["active_currency"] = active_currency
            evaluation["active_contract"] = target_sym
            evaluation["is_testnet"] = get_current_is_testnet()

            # Broadcast to connected WebSockets
            if active_websockets:
                stale_ws = set()
                for ws in active_websockets:
                    try:
                        await ws.send_json(evaluation)
                    except Exception:
                        stale_ws.add(ws)
                
                for ws in stale_ws:
                    active_websockets.remove(ws)

        except Exception as e:
            logger.error(f"[Loop] Error in market data loop: {e}")

        await asyncio.sleep(config.POLL_INTERVAL_SEC)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.add(websocket)
    try:
        history = engine.get_history_list()
        futures_tick, target_sym = get_active_futures_ticker()
        
        await websocket.send_json({
            "type": "INIT_HISTORY",
            "data": history,
            "exchange_source": active_exchange_source.upper(),
            "active_currency": active_currency,
            "active_contract": target_sym,
            "is_testnet": get_current_is_testnet(),
            "diff_to_trade": engine.diff_to_trade,
            "diff_to_cut": engine.diff_to_cut,
            "auto_trade": engine.auto_trade_enabled
        })
        
        # Instantly send current live evaluation tick on websocket connection
        mt5_tick = mt5_conn.get_latest_tick()
        latest_eval = engine.evaluate(mt5_tick, futures_tick)
        latest_eval["exchange_source"] = active_exchange_source.upper()
        latest_eval["active_currency"] = active_currency
        latest_eval["active_contract"] = target_sym
        latest_eval["is_testnet"] = get_current_is_testnet()
        await websocket.send_json(latest_eval)

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)
    except Exception:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

@app.get("/api/status")
async def get_status():
    mt5_tick = mt5_conn.get_latest_tick()
    futures_tick, target_sym = get_active_futures_ticker()
    eval_res = engine.evaluate(mt5_tick, futures_tick)
    return {
        "evaluation": eval_res,
        "exchange_source": active_exchange_source.upper(),
        "active_currency": active_currency,
        "active_contract": target_sym,
        "is_testnet": get_current_is_testnet(),
        "diff_to_trade": engine.diff_to_trade,
        "diff_to_cut": engine.diff_to_cut,
        "auto_trade": engine.auto_trade_enabled,
        "active_positions": engine.active_positions
    }

@app.get("/api/available-contracts")
async def get_available_contracts():
    """Fetch all active futures contracts for the currently selected exchange & currency."""
    global active_bybit_contract, active_binance_contract
    if active_exchange_source == "binance":
        curr = active_currency
        contract_list = [f"{curr}USDT_261225", f"{curr}USDT", f"{curr}USD_261225", f"{curr}USD_PERP"]
        current_active = active_binance_contract
        if not current_active or not (curr in current_active):
            current_active = contract_list[0]
            active_binance_contract = current_active
        return {
            "exchange": "binance",
            "currency": active_currency,
            "is_testnet": binance_conn.testnet,
            "active_contract": current_active,
            "contracts": contract_list
        }
    elif active_exchange_source == "bybit":
        contract_list = bybit_conn.get_contract_names()
        current_active = active_bybit_contract
        if not current_active or current_active not in contract_list:
            cfg_target = (config.BYBIT_CONTRACT or "").replace("2026", "26")
            matching = [c for c in contract_list if cfg_target and (cfg_target in c or c.endswith(cfg_target.split("-")[-1]))]
            current_active = matching[0] if matching else (contract_list[0] if contract_list else f"{active_currency}USDT-02OCT26")
            active_bybit_contract = current_active
        return {
            "exchange": "bybit",
            "currency": bybit_conn.currency,
            "is_testnet": bybit_conn.testnet,
            "active_contract": current_active,
            "contracts": contract_list
        }
    elif active_exchange_source == "deribit":
        instruments = deribit_conn.get_dated_futures()
        contract_list = [i.get("instrument_name") for i in instruments]
        perp_name = f"{deribit_conn.currency}-PERPETUAL"
        if perp_name not in contract_list:
            contract_list.insert(0, perp_name)
        
        current_active = active_deribit_contract
        if not current_active or not current_active.startswith(deribit_conn.currency):
            current_active = contract_list[-3] if len(contract_list) >= 3 else perp_name

        return {
            "exchange": "deribit",
            "currency": deribit_conn.currency,
            "is_testnet": False,
            "active_contract": current_active,
            "contracts": contract_list
        }
    else: # delta
        return {
            "exchange": "delta",
            "currency": active_currency,
            "is_testnet": False,
            "active_contract": delta_conn.symbol,
            "contracts": [delta_conn.symbol]
        }

@app.post("/api/select-currency")
async def select_currency(req: CurrencySelectRequest):
    """Switch active cryptocurrency currency (BTC or ETH)."""
    global active_currency, active_bybit_contract, active_deribit_contract, active_binance_contract
    curr = req.currency.upper()
    if curr in ["BTC", "ETH"]:
        active_currency = curr
        mt5_conn.set_symbol(f"{curr}USD")
        delta_conn.symbol = f"{curr}USD"
        bybit_conn.set_currency(curr)
        deribit_conn.set_currency(curr)
        
        # Determine active Binance contract for new currency
        active_binance_contract = f"{curr}USD_261225"
        binance_contracts = [f"{curr}USD_261225", f"{curr}USD_PERP", f"{curr}USDT_261225", f"{curr}USDT"]

        # Determine active Bybit contract for new currency
        bybit_contracts = bybit_conn.get_contract_names()
        cfg_target = (config.BYBIT_CONTRACT or "").replace("2026", "26")
        matching = [c for c in bybit_contracts if cfg_target and (c.endswith(cfg_target.split("-")[-1]))]
        active_bybit_contract = matching[0] if matching else (bybit_contracts[0] if bybit_contracts else f"{curr}USDT-02OCT26")

        # Determine active Deribit contract for new currency
        deribit_instruments = deribit_conn.get_dated_futures()
        deribit_contracts = [i.get("instrument_name") for i in deribit_instruments]
        perp_name = f"{curr}-PERPETUAL"
        if perp_name not in deribit_contracts:
            deribit_contracts.insert(0, perp_name)
        active_deribit_contract = deribit_contracts[-3] if len(deribit_contracts) >= 3 else perp_name

        if active_exchange_source == "binance":
            chosen_contracts = binance_contracts
            chosen_active = active_binance_contract
        elif active_exchange_source == "bybit":
            chosen_contracts = bybit_contracts
            chosen_active = active_bybit_contract
        elif active_exchange_source == "deribit":
            chosen_contracts = deribit_contracts
            chosen_active = active_deribit_contract
        else:
            chosen_contracts = [delta_conn.symbol]
            chosen_active = delta_conn.symbol

        logger.info(f"[Server] Currency switched to {curr} | MT5: {curr}USD | Active: {chosen_active}")
        return {
            "status": "success",
            "currency": active_currency,
            "active_contract": chosen_active,
            "contracts": chosen_contracts
        }
    raise HTTPException(status_code=400, detail="Invalid currency. Choose 'BTC' or 'ETH'.")

@app.post("/api/select-contract")
async def select_contract(req: ContractSelectRequest):
    global active_bybit_contract, active_deribit_contract, active_binance_contract
    contract = req.contract.strip()
    if active_exchange_source == "binance":
        active_binance_contract = contract
    elif active_exchange_source == "bybit":
        active_bybit_contract = contract
    elif active_exchange_source == "deribit":
        active_deribit_contract = contract.upper()
    logger.info(f"[Server] Selected {active_exchange_source.upper()} contract: {contract}")
    return {"status": "success", "active_contract": contract}

@app.post("/api/toggle-testnet")
async def toggle_testnet(req: TestnetToggleRequest):
    """Toggle Testnet (Demo Trading) vs Mainnet for Bybit or Binance."""
    if active_exchange_source == "binance":
        binance_conn.set_testnet(req.testnet)
        mode_text = "Binance TESTNET (Demo Account)" if binance_conn.testnet else "Binance MAINNET (Live)"
        return {
            "status": "success",
            "is_testnet": binance_conn.testnet,
            "message": f"Switched to {mode_text}"
        }
    else:
        bybit_conn.set_testnet(req.testnet)
        mode_text = "Bybit TESTNET (Demo Account)" if bybit_conn.testnet else "Bybit MAINNET (Live)"
        return {
            "status": "success",
            "is_testnet": bybit_conn.testnet,
            "message": f"Switched to {mode_text}"
        }

@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    tp = req.target_profit_usd if req.target_profit_usd is not None else 1.0
    engine.update_parameters(
        diff_to_trade=req.diff_to_trade,
        diff_to_cut=req.diff_to_cut,
        trade_size=req.trade_size,
        auto_trade=req.auto_trade,
        target_profit_usd=tp
    )
    return {"status": "success", "message": f"EA Arbitrage Parameters Updated. Net TP Target: ${tp:.2f} USD."}

@app.post("/api/fire-order")
async def manual_fire_order():
    """Manually fire a pair trade immediately on MT5 and Futures Exchange."""
    mt5_tick = mt5_conn.get_latest_tick()
    futures_tick, target_sym = get_active_futures_ticker()
    
    if not mt5_tick or not futures_tick:
        raise HTTPException(status_code=400, detail="Cannot fire order: live ticks unavailable.")

    mt5_symbol = mt5_tick.get("symbol", f"{active_currency}USD")
    spread_usd = futures_tick["mid"] - mt5_tick["mid"]
    spot_action = "BUY" if spread_usd >= 0 else "SELL"
    futures_action = "Sell" if spread_usd >= 0 else "Buy"

    # Execute MT5 Order
    mt5_res = mt5_conn.place_order(symbol=mt5_symbol, order_type=spot_action, volume=engine.trade_size, comment=f"Manual Gap ${spread_usd:+.2f}")

    if not mt5_res.get("success"):
        logger.error(f"[Server] Manual order MT5 failed: {mt5_res.get('error')}")
        raise HTTPException(status_code=500, detail=f"MT5 Order Execution Failed: {mt5_res.get('error')}")

    # If Binance is active, place Binance Demo/Live order
    binance_order_info = None
    if active_exchange_source == "binance" and binance_conn.api_key and binance_conn.api_secret:
        if "_" in target_sym and "USDT" not in target_sym:
            # COIN-M: contract value is $10 for ETH, $100 for BTC
            contracts_qty = max(1, int(round((engine.trade_size * futures_tick["mid"]) / (10 if active_currency == "ETH" else 100))))
            binance_order_info = binance_conn.place_coinm_order(
                symbol=target_sym,
                side=futures_action.upper(),
                quantity=contracts_qty,
                order_type="MARKET"
            )
        else:
            binance_order_info = binance_conn.place_usdm_order(
                symbol=target_sym,
                side=futures_action.upper(),
                quantity=engine.trade_size,
                order_type="MARKET"
            )

    # If Bybit is active and credentials are provided, place matching Demo/Live futures order
    bybit_order_info = None
    if active_exchange_source == "bybit" and bybit_conn.api_key and bybit_conn.api_secret:
        bybit_res = bybit_conn.place_order(
            symbol=target_sym,
            side=futures_action,
            qty=engine.trade_size,
            order_type="Market"
        )
        bybit_order_info = bybit_res

    engine.trade_counter += 1
    new_pos = {
        "id": f"PAIR-MANUAL-{engine.trade_counter}",
        "open_time": time.strftime("%H:%M:%S"),
        "symbol_spot": mt5_symbol,
        "symbol_fut": target_sym,
        "entry_gap": round(spread_usd, 2),
        "spot_price": mt5_tick["mid"],
        "fut_price": futures_tick["mid"],
        "trade_size": engine.trade_size,
        "status": f"OPEN (MT5 Ticket #{mt5_res.get('ticket')})",
        "signal": "MANUAL_FIRE",
        "ticket": mt5_res.get("ticket"),
        "futures_order": binance_order_info or bybit_order_info
    }
    engine.active_positions.append(new_pos)
    logger.info(f"[Manual Fire] Created {new_pos['id']} MT5 Ticket #{mt5_res.get('ticket')} @ Gap ${spread_usd:+.2f}")
    return {
        "status": "success",
        "message": f"Order Pair Fired! MT5 #{mt5_res.get('ticket')} {spot_action} | {active_exchange_source.upper()} {futures_action} {target_sym} @ Gap ${spread_usd:+.2f}",
        "position": new_pos
    }

@app.post("/api/close-positions")
async def close_all_positions():
    """Close all open arbitrage position pairs."""
    mt5_symbol = f"{active_currency}USD"
    close_res = mt5_conn.close_all_positions(symbol=mt5_symbol)
    
    # Close Binance positions if credentials configured
    if active_exchange_source == "binance" and binance_conn.api_key:
        binance_conn.close_all_positions(active_binance_contract)

    # Close Bybit positions if credentials configured
    if active_exchange_source == "bybit" and bybit_conn.api_key:
        bybit_conn.close_all_positions(active_bybit_contract)

    count = len(engine.active_positions)
    engine.active_positions.clear()
    logger.info(f"[Manual Close] Closed MT5 positions: {close_res}")
    return {"status": "success", "message": f"Closed {close_res.get('closed_count', count)} MT5 & {active_exchange_source.upper()} Position(s)."}

@app.post("/api/switch-exchange")
async def switch_exchange(req: ExchangeSwitchRequest):
    global active_exchange_source
    src = req.exchange.lower()
    if src in ["bybit", "deribit", "delta", "binance"]:
        active_exchange_source = src
        logger.info(f"[Server] Switched active exchange to: {src.upper()}")
        return {"status": "success", "active_exchange": active_exchange_source.upper()}
    raise HTTPException(status_code=400, detail="Invalid exchange. Choose 'binance', 'bybit', 'deribit', or 'delta'.")

@app.post("/api/credentials")
async def set_credentials(req: CredentialsRequest):
    ex = req.exchange.lower()
    if ex == "binance":
        binance_conn.set_credentials(req.api_key, req.api_secret)
        wallet = binance_conn.get_account_balance()
        usdm_balance = wallet.get("balances", {}).get("usdm", {}).get("totalWalletBalance", 0.0)
        return {
            "status": "success" if wallet.get("success") else "error",
            "exchange": "Binance",
            "is_testnet": binance_conn.testnet,
            "account": {"total_equity": usdm_balance, "details": wallet}
        }
    elif ex == "bybit":
        bybit_conn.set_credentials(req.api_key, req.api_secret)
        wallet = bybit_conn.get_wallet_balances()
        return {
            "status": "success" if wallet.get("success") else "error",
            "exchange": "Bybit",
            "is_testnet": bybit_conn.testnet,
            "account": wallet
        }
    elif ex == "deribit":
        deribit_conn.set_credentials(req.api_key, req.api_secret)
        authenticated = deribit_conn.authenticate()
        summary = deribit_conn.get_account_summary() if authenticated else {}
        return {"status": "success" if authenticated else "error", "exchange": "Deribit", "account": summary}
    else:
        delta_conn.set_credentials(req.api_key, req.api_secret)
        wallet = delta_conn.get_wallet_balances()
        return {"status": "success", "exchange": "Delta", "wallet": wallet}

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_dashboard():
    return FileResponse("static/index.html")
