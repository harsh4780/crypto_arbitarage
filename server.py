"""
FastAPI Server & WebSocket Broadcast Server
============================================
Serves the web dashboard, supports 5-Leg Multi-Tier EA parameters,
SQLite Trade History with telemetry JSON, Day-by-Day Rotating Log Viewer,
dynamic Currency switching (BTC / ETH), dynamic contract expiry selection,
and manual/automated order execution across Binance, Bybit, Deribit, and Delta.
"""

import time
import asyncio
import logging
from datetime import datetime
from typing import Set, Dict, Any, Optional, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from config import config
from database import db
from logger_setup import setup_system_logging, get_available_log_dates, read_log_file
from mt5_connector import MT5Connector
from delta_connector import DeltaConnector
from deribit_connector import DeribitConnector
from bybit_connector import BybitConnector
from binance_connector import BinanceConnector
from arbitrage_engine import ArbitrageEngine

# Initialize System Logger with daily file rotation
logger = setup_system_logging(logging.INFO)

# Active exchange provider ('bybit', 'deribit', 'delta', 'binance')
active_exchange_source = db.get_config("active_exchange", "binance")

# Active currency ('BTC' or 'ETH')
active_currency = db.get_config("active_currency", "ETH")

# Active Contract per exchange
active_bybit_contract = db.get_config("active_bybit_contract", config.BYBIT_CONTRACT or "BTCUSDT-02OCT26")
active_deribit_contract = db.get_config("active_deribit_contract", config.DERIBIT_CONTRACT or "")
active_binance_contract = db.get_config("active_binance_contract", config.BINANCE_USDT_CONTRACT or "ETHUSDT_261225")

# System instances
mt5_conn = MT5Connector(symbol=f"{active_currency}USD")
bybit_conn = BybitConnector(currency=active_currency, testnet=config.BYBIT_TESTNET, api_key=config.BYBIT_API_KEY, api_secret=config.BYBIT_API_SECRET, category=config.BYBIT_CATEGORY)
delta_conn = DeltaConnector(symbol=f"{active_currency}USD", base_url=config.DELTA_BASE_URL, api_key=config.DELTA_API_KEY, api_secret=config.DELTA_API_SECRET)
deribit_conn = DeribitConnector(currency=active_currency, client_id=config.DERIBIT_CLIENT_ID, client_secret=config.DERIBIT_CLIENT_SECRET)
binance_conn = BinanceConnector(testnet=config.BINANCE_TESTNET, api_key=config.BINANCE_API_KEY, api_secret=config.BINANCE_API_SECRET)
engine = ArbitrageEngine(currency=active_currency)

app = FastAPI(title="Crypto Arbitrage 5-Leg Terminal", version="4.0.0")

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
    contract: str

class SettingsRequest(BaseModel):
    diff_to_trade: float
    diff_to_cut: float
    trade_size: float
    auto_trade: bool
    target_profit_usd: Optional[float] = 1.0

class LegUpdateRequest(BaseModel):
    leg_id: int
    diff_to_trade: float
    diff_to_cut: float
    trade_size: float
    target_profit_usd: float
    enabled: bool

class BatchLegsUpdateRequest(BaseModel):
    legs: List[LegUpdateRequest]

class FireLegRequest(BaseModel):
    leg_id: Optional[int] = None # None means next available idle leg

class CloseLegRequest(BaseModel):
    leg_id: int

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

def get_active_futures_connector():
    if active_exchange_source == "binance":
        return binance_conn
    elif active_exchange_source == "bybit":
        return bybit_conn
    elif active_exchange_source == "deribit":
        return deribit_conn
    else:
        return delta_conn

@app.on_event("startup")
async def startup_event():
    # Start async database queue worker
    db.start_async_worker()
    mt5_conn.connect()
    asyncio.create_task(market_data_loop())
    logger.info("[Startup] Fast 5-Leg Multi-Tier Arbitrage Server Started.")

@app.on_event("shutdown")
async def shutdown_event():
    mt5_conn.disconnect()
    logger.info("[Shutdown] Server disconnected.")

async def market_data_loop():
    """Background task to fetch price data, evaluate 5 EA gap tiers, and broadcast via WebSockets."""
    global active_exchange_source, active_currency
    while True:
        try:
            mt5_tick = mt5_conn.get_latest_tick()
            futures_tick, target_sym = get_active_futures_ticker()

            active_fut_conn = get_active_futures_connector()
            evaluation = engine.evaluate(mt5_tick, futures_tick, mt5_conn=mt5_conn, futures_conn=active_fut_conn)
            evaluation["exchange_source"] = active_exchange_source.upper()
            evaluation["active_currency"] = active_currency
            evaluation["active_contract"] = target_sym
            evaluation["is_testnet"] = get_current_is_testnet()

            # Persist active currency tick to SQLite
            if evaluation.get("status") != "WAITING_FOR_DATA":
                mt5_mid = float(evaluation.get("mt5_mid", 0.0))
                fut_mid = float(evaluation.get("delta_mid", 0.0))
                spread_usd = float(evaluation.get("spread_usd", 0.0))
                spread_pct = float(evaluation.get("spread_pct", 0.0))
                t_str = evaluation.get("time_str", datetime.utcnow().strftime("%H:%M:%S"))
                if mt5_mid > 0 and fut_mid > 0:
                    db.save_spread_tick(active_currency, mt5_mid, fut_mid, spread_usd, spread_pct, timestamp_str=t_str)

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

            # Background Tick Logger for Alternate Currency (e.g. BTC if ETH is active, or ETH if BTC is active)
            alt_curr = "BTC" if active_currency == "ETH" else "ETH"
            try:
                import MetaTrader5 as mt5_raw
                alt_spot_tick = mt5_raw.symbol_info_tick(f"{alt_curr}USD")
                if alt_spot_tick and alt_spot_tick.ask > 0:
                    alt_spot_mid = (alt_spot_tick.bid + alt_spot_tick.ask) / 2.0
                    alt_fut_tick = None
                    if active_exchange_source == "binance":
                        alt_fut_tick = binance_conn.get_usdm_tick(f"{alt_curr}USDT")
                    elif active_exchange_source == "bybit":
                        alt_contract = config.BYBIT_CONTRACT if alt_curr == "BTC" else "ETHUSDT"
                        alt_fut_tick = bybit_conn.get_latest_ticker(alt_contract)
                    
                    if alt_fut_tick and alt_fut_tick.get("mid", 0) > 0:
                        alt_fut_mid = float(alt_fut_tick["mid"])
                        alt_spread = alt_fut_mid - alt_spot_mid
                        alt_pct = (alt_spread / alt_spot_mid * 100.0) if alt_spot_mid > 0 else 0.0
                        db.save_spread_tick(alt_curr, alt_spot_mid, alt_fut_mid, alt_spread, alt_pct)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[Loop] Error in market data loop: {e}", exc_info=True)

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
            "auto_trade": engine.auto_trade_enabled,
            "legs": list(engine.legs.values())
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
        "legs": list(engine.legs.values()),
        "active_positions": engine.active_positions
    }

# -------------------------------------------------------------
# 5-LEGS MULTI-TIER REST API
# -------------------------------------------------------------
@app.get("/api/legs")
async def get_legs():
    """Retrieve all 5 legs with their configuration and live state."""
    return {
        "status": "success",
        "currency": active_currency,
        "auto_trade": engine.auto_trade_enabled,
        "legs": list(engine.legs.values())
    }

@app.post("/api/legs/{leg_id}")
async def update_single_leg(leg_id: int, req: LegUpdateRequest):
    """Update settings for a single leg."""
    if leg_id not in engine.legs:
        raise HTTPException(status_code=404, detail=f"Leg {leg_id} not found.")
    
    updated_leg = engine.update_leg_config(
        leg_id=leg_id,
        diff_to_trade=req.diff_to_trade,
        diff_to_cut=req.diff_to_cut,
        trade_size=req.trade_size,
        target_profit_usd=req.target_profit_usd,
        enabled=req.enabled
    )
    return {"status": "success", "leg": updated_leg}

@app.post("/api/legs/batch")
async def update_batch_legs(req: BatchLegsUpdateRequest):
    """Update all 5 legs in a single call."""
    engine.batch_update_legs([leg.dict() for leg in req.legs])
    return {"status": "success", "legs": list(engine.legs.values())}

@app.post("/api/fire-leg")
async def fire_leg_endpoint(req: FireLegRequest):
    """Manually fire a specific leg immediately (or next idle leg)."""
    mt5_tick = mt5_conn.get_latest_tick()
    futures_tick, target_sym = get_active_futures_ticker()

    if not mt5_tick or not futures_tick:
        raise HTTPException(status_code=400, detail="Cannot fire leg: live market ticks unavailable.")

    active_fut_conn = get_active_futures_connector()
    res = engine.fire_manual_leg(
        leg_id=req.leg_id,
        mt5_tick=mt5_tick,
        futures_tick=futures_tick,
        mt5_conn=mt5_conn,
        futures_conn=active_fut_conn
    )

    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to fire leg"))

    return {"status": "success", "result": res}

@app.post("/api/close-leg")
async def close_leg_endpoint(req: CloseLegRequest):
    """Manually close a specific open leg."""
    mt5_tick = mt5_conn.get_latest_tick()
    futures_tick, _ = get_active_futures_ticker()

    active_fut_conn = get_active_futures_connector()
    res = engine.close_single_leg(
        leg_id=req.leg_id,
        mt5_tick=mt5_tick or {},
        futures_tick=futures_tick or {},
        mt5_conn=mt5_conn,
        futures_conn=active_fut_conn
    )

    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to close leg"))

    return {"status": "success", "result": res}

@app.post("/api/fire-order")
async def manual_fire_order():
    """Backward compatibility: fires next available idle leg."""
    mt5_tick = mt5_conn.get_latest_tick()
    futures_tick, _ = get_active_futures_ticker()
    if not mt5_tick or not futures_tick:
        raise HTTPException(status_code=400, detail="Cannot fire order: live ticks unavailable.")

    active_fut_conn = get_active_futures_connector()
    res = engine.fire_manual_leg(
        leg_id=None,
        mt5_tick=mt5_tick,
        futures_tick=futures_tick,
        mt5_conn=mt5_conn,
        futures_conn=active_fut_conn
    )

    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "No idle leg available."))
    return {"status": "success", "message": f"Fired Leg {res['position']['leg_id']}", "position": res["position"]}

@app.post("/api/close-positions")
async def close_all_positions():
    """Close all open arbitrage legs."""
    mt5_tick = mt5_conn.get_latest_tick()
    futures_tick, _ = get_active_futures_ticker()
    active_fut_conn = get_active_futures_connector()
    res = engine.close_all_positions(mt5_tick=mt5_tick or {}, futures_tick=futures_tick or {}, mt5_conn=mt5_conn, futures_conn=active_fut_conn)
    return {"status": "success", "message": f"Closed {res.get('closed_count', 0)} open leg(s)."}

# -------------------------------------------------------------
# SQLITE TRADE HISTORY REST API
# -------------------------------------------------------------
@app.get("/api/history")
async def get_history_route(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    leg_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None)
):
    """Fetch paginated historical trades from SQLite DB."""
    data = db.get_history(limit=limit, offset=offset, leg_id=leg_id, status=status, search=search)
    return data

@app.get("/api/history/{trade_id}")
async def get_trade_detail(trade_id: str):
    """Fetch complete trade record including execution JSON telemetry."""
    trade = db.get_trade_by_id(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found.")
    return {"status": "success", "trade": trade}

@app.get("/api/history-stats")
async def get_history_stats():
    """Summary KPI metrics from trade history."""
    stats = db.get_stats()
    return {"status": "success", "stats": stats}

# -------------------------------------------------------------
# DAY-BY-DAY ROTATING LOG VIEWER REST API
# -------------------------------------------------------------
@app.get("/api/logs/dates")
async def get_log_dates():
    """Retrieve available dates for daily log files."""
    dates = get_available_log_dates()
    return {"status": "success", "dates": dates}

@app.get("/api/logs")
async def get_logs(
    date: Optional[str] = None,
    limit: int = Query(500, ge=10, le=2000),
    level: Optional[str] = Query("ALL"),
    search: Optional[str] = Query(None)
):
    """Retrieve log lines for a specific day with filtering."""
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    result = read_log_file(date_str=target_date, max_lines=limit, level_filter=level, search=search)
    return result

# -------------------------------------------------------------
# EXCHANGE & CONTRACT CONTROLS
# -------------------------------------------------------------
@app.get("/api/available-contracts")
async def get_available_contracts():
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
    global active_currency, active_bybit_contract, active_deribit_contract, active_binance_contract
    curr = req.currency.upper()
    if curr in ["BTC", "ETH"]:
        active_currency = curr
        mt5_conn.set_symbol(f"{curr}USD")
        delta_conn.symbol = f"{curr}USD"
        bybit_conn.set_currency(curr)
        deribit_conn.set_currency(curr)
        
        # Scale 5-leg defaults for the selected currency
        engine.set_currency(curr)

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

        db.set_config("active_currency", active_currency)
        logger.info(f"[Server] Currency switched to {curr} | MT5: {curr}USD | Active: {chosen_active}")
        return {
            "status": "success",
            "currency": active_currency,
            "active_contract": chosen_active,
            "contracts": chosen_contracts,
            "legs": list(engine.legs.values())
        }
    raise HTTPException(status_code=400, detail="Invalid currency. Choose 'BTC' or 'ETH'.")

@app.post("/api/select-contract")
async def select_contract(req: ContractSelectRequest):
    global active_bybit_contract, active_deribit_contract, active_binance_contract
    contract = req.contract.strip()
    if active_exchange_source == "binance":
        active_binance_contract = contract
        db.set_config("active_binance_contract", contract)
    elif active_exchange_source == "bybit":
        active_bybit_contract = contract
        db.set_config("active_bybit_contract", contract)
    elif active_exchange_source == "deribit":
        active_deribit_contract = contract.upper()
        db.set_config("active_deribit_contract", contract.upper())
    logger.info(f"[Server] Selected {active_exchange_source.upper()} contract: {contract}")
    return {"status": "success", "active_contract": contract}

@app.post("/api/toggle-testnet")
async def toggle_testnet(req: TestnetToggleRequest):
    if active_exchange_source == "binance":
        binance_conn.set_testnet(req.testnet)
        mode_text = "Binance TESTNET (Demo Account)" if binance_conn.testnet else "Binance MAINNET (Live)"
        return {"status": "success", "is_testnet": binance_conn.testnet, "message": f"Switched to {mode_text}"}
    else:
        bybit_conn.set_testnet(req.testnet)
        mode_text = "Bybit TESTNET (Demo Account)" if bybit_conn.testnet else "Bybit MAINNET (Live)"
        return {"status": "success", "is_testnet": bybit_conn.testnet, "message": f"Switched to {mode_text}"}

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
    return {"status": "success", "message": f"EA Parameters Updated. Net TP Target: ${tp:.2f} USD."}

@app.post("/api/switch-exchange")
async def switch_exchange(req: ExchangeSwitchRequest):
    global active_exchange_source
    src = req.exchange.lower()
    if src in ["bybit", "deribit", "delta", "binance"]:
        active_exchange_source = src
        db.set_config("active_exchange", active_exchange_source)
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

class MT5PathRequest(BaseModel):
    path: str

@app.get("/api/config/mt5-path")
async def get_mt5_path():
    p = config.MT5_PATH or db.get_config("mt5_path", "")
    return {"path": p, "is_connected": mt5_conn.is_connected}

@app.post("/api/config/mt5-path")
async def set_mt5_path(req: MT5PathRequest):
    p = req.path.strip('\"\'')
    config.MT5_PATH = p
    db.set_config("mt5_path", p)
    reconnected = mt5_conn.set_path(p)
    return {
        "status": "success" if reconnected else "warning",
        "path": p,
        "is_connected": reconnected,
        "message": f"MT5 terminal path updated and {'connected successfully' if reconnected else 'could not connect, please check path'}"
    }


# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_dashboard():
    return FileResponse("static/index.html")

@app.get("/coinglass")
async def get_coinglass_chart():
    return FileResponse("static/coinglass.html")

@app.get("/api/coinglass-history")
async def get_coinglass_history(currency: str = "ETH", limit: int = 800):
    """Fetch historical price & spread data for the Coinglass overlay chart from SQLite or CSV."""
    import os
    import pandas as pd
    curr = currency.upper()
    items = []
    
    import math

    def clean_float(v, default=0.0):
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                return default
            return f
        except Exception:
            return default

    # 1. Fetch from SQLite database (Primary Source)
    db_ticks = db.get_recent_spread_ticks(currency=curr, limit=limit)
    today_hl = db.get_today_gap_high_low(currency=curr)

    if db_ticks and len(db_ticks) > 0:
        for row in db_ticks:
            items.append({
                "time": str(row.get("timestamp", "")),
                "price": clean_float(row.get("spot_price", 0.0)),
                "spread": round(clean_float(row.get("raw_spread", 0.0)), 2),
                "pct": round(clean_float(row.get("spread_pct", 0.0)), 4),
                "deviation": round(clean_float(row.get("deviation", 0.0)), 3)
            })
        return {
            "currency": curr,
            "items": items,
            "count": len(items),
            "today_high_low": today_hl
        }

    # 2. Fallback to CSV with strict NaN cleaning if SQLite has no ticks yet
    csv_file = "eth_arbitrage_log.csv"
    if os.path.exists(csv_file) and curr == "ETH":
        try:
            df = pd.read_csv(csv_file)
            if not df.empty and "Spread_USD" in df.columns:
                df_valid = df.dropna(subset=["Spread_USD", "MT5_Spot_Mid"])
                if not df_valid.empty:
                    df_tail = df_valid.tail(min(limit, len(df_valid)))
                    for _, row in df_tail.iterrows():
                        p = clean_float(row.get("MT5_Spot_Mid", 0.0))
                        s = clean_float(row.get("Spread_USD", 0.0))
                        pct = clean_float(row.get("Spread_PCT", 0.0))
                        ts = str(row.get("Timestamp", ""))
                        items.append({
                            "time": ts,
                            "price": p,
                            "spread": round(s, 2),
                            "pct": round(pct, 4),
                            "deviation": 0.0
                        })
                    
                    spreads = [i["spread"] for i in items if not math.isnan(i["spread"])]
                    if spreads:
                        today_hl = {
                            "date": datetime.utcnow().strftime("%Y-%m-%d"),
                            "max_gap": round(clean_float(max(spreads)), 2),
                            "min_gap": round(clean_float(min(spreads)), 2),
                            "avg_gap": round(clean_float(sum(spreads) / len(spreads)), 2),
                            "gap_range": round(clean_float(max(spreads) - min(spreads)), 2),
                            "count": len(spreads)
                        }

                    return {
                        "currency": curr,
                        "items": items,
                        "count": len(items),
                        "today_high_low": today_hl
                    }
        except Exception as e:
            logger.error(f"[CoinglassAPI] Error reading CSV: {e}")

    return {
        "currency": curr,
        "items": items,
        "count": 0,
        "today_high_low": today_hl
    }

@app.get("/api/daily-gap-history")
async def get_daily_gap_history(currency: str = "ETH", limit: int = 30):
    """Retrieve day-by-day historical records of Max/Min Gap, Range, and Volatility."""
    curr = currency.upper()
    days = db.get_daily_gap_history(currency=curr, limit=limit)
    today = db.get_today_gap_high_low(currency=curr)
    return {
        "currency": curr,
        "today": today,
        "days": days
    }




