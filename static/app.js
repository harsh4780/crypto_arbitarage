// MQL EA Arbitrage Dashboard Application

let socket = null;
let chart = null;
let currentChartMode = 'price'; // 'price' or 'spread'
let activeExchange = 'binance';
let isTestnet = true;
let activeCurrency = 'ETH';
let activeContract = 'ETHUSDT_261225';
let autoTradeEnabled = false;
const MAX_CHART_POINTS = 50;

// Data arrays for Chart.js
const chartLabels = [];
const mt5Prices = [];
const deltaPrices = [];
const spreadData = [];

document.addEventListener('DOMContentLoaded', () => {
    initClock();
    initChart();
    loadAvailableContracts();
    connectWebSocket();
});

function initClock() {
    setInterval(() => {
        const now = new Date();
        document.getElementById('liveClock').textContent = now.toTimeString().split(' ')[0];
    }, 1000);
}

function initChart() {
    const ctx = document.getElementById('liveChart').getContext('2d');
    chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: chartLabels,
            datasets: [
                {
                    label: 'MT5 Spot Price',
                    borderColor: '#38bdf8',
                    backgroundColor: 'rgba(56, 189, 248, 0.1)',
                    data: mt5Prices,
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.2,
                    yAxisID: 'yPrice'
                },
                {
                    label: 'Futures Price',
                    borderColor: '#fbbf24',
                    backgroundColor: 'rgba(251, 191, 36, 0.1)',
                    data: deltaPrices,
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.2,
                    yAxisID: 'yPrice'
                },
                {
                    label: 'Live Gap ($)',
                    borderColor: '#c084fc',
                    backgroundColor: 'rgba(192, 132, 252, 0.1)',
                    data: spreadData,
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.2,
                    yAxisID: 'ySpread',
                    hidden: true
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
                x: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#8b949e', maxTicksLimit: 8 }
                },
                yPrice: {
                    type: 'linear',
                    position: 'left',
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#8b949e' }
                },
                ySpread: {
                    type: 'linear',
                    position: 'right',
                    grid: { drawOnChartArea: false },
                    ticks: { color: '#c084fc' }
                }
            },
            plugins: {
                legend: {
                    labels: { color: '#f0f6fc', font: { family: 'Inter' } }
                }
            }
        }
    });
}

let candlestickChart = null;
let spreadCandleSeries = null;
let currentBarTimeSec = 0;
let spreadCandle = null;

function initCandlestickChart() {
    const container = document.getElementById('candlestickContainer');
    if (!container || candlestickChart) return;

    try {
        candlestickChart = LightweightCharts.createChart(container, {
            width: container.clientWidth || 600,
            height: 280,
            layout: {
                background: { color: 'transparent' },
                textColor: '#8b949e',
                fontFamily: 'Inter, sans-serif'
            },
            grid: {
                vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
                horzLines: { color: 'rgba(255, 255, 255, 0.05)' }
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
            },
            rightPriceScale: {
                borderColor: 'rgba(255, 255, 255, 0.1)',
            },
            timeScale: {
                borderColor: 'rgba(255, 255, 255, 0.1)',
                timeVisible: true,
                secondsVisible: true
            }
        });

        // Arbitrage Spread Candlestick Series: Bid(Fut) - Ask(Spot)
        spreadCandleSeries = candlestickChart.addCandlestickSeries({
            upColor: '#22c55e',
            downColor: '#ef4444',
            borderUpColor: '#22c55e',
            borderDownColor: '#ef4444',
            wickUpColor: '#22c55e',
            wickDownColor: '#ef4444',
            title: '⚡ Gap ($): Bid(Fut) - Ask(Spot)'
        });

        window.addEventListener('resize', () => {
            if (candlestickChart && container) {
                candlestickChart.applyOptions({ width: container.clientWidth });
            }
        });
    } catch (e) {
        console.error("Failed to initialize TradingView Lightweight Charts:", e);
    }
}

function updateCandlesticks(spotPrice, futuresPrice, timestampMs, spreadVal) {
    if (spreadVal === undefined || spreadVal === null) return;
    if (!candlestickChart) initCandlestickChart();

    const barIntervalSec = 5; // 5-second OHLC candles for fast intraday visualization
    const timeSec = Math.floor((timestampMs || Date.now()) / 1000);
    const barTime = Math.floor(timeSec / barIntervalSec) * barIntervalSec;

    if (spreadCandle === null || barTime > currentBarTimeSec) {
        currentBarTimeSec = barTime;
        
        spreadCandle = {
            time: barTime,
            open: spreadVal,
            high: spreadVal,
            low: spreadVal,
            close: spreadVal
        };
    } else {
        spreadCandle.high = Math.max(spreadCandle.high, spreadVal);
        spreadCandle.low = Math.min(spreadCandle.low, spreadVal);
        spreadCandle.close = spreadVal;
    }

    if (spreadCandleSeries && spreadCandle) {
        spreadCandleSeries.update(spreadCandle);
    }
}

function showChart(mode) {
    currentChartMode = mode;
    document.getElementById('btnPriceChart').classList.toggle('active', mode === 'price');
    document.getElementById('btnSpreadChart').classList.toggle('active', mode === 'spread');
    document.getElementById('btnCandleChart').classList.toggle('active', mode === 'candle');

    const canvasEl = document.getElementById('liveChart');
    const candleEl = document.getElementById('candlestickContainer');

    if (mode === 'candle') {
        canvasEl.style.display = 'none';
        candleEl.style.display = 'block';
        if (!candlestickChart) {
            initCandlestickChart();
        }
        if (candlestickChart && candleEl) {
            candlestickChart.applyOptions({ width: candleEl.clientWidth, height: 280 });
        }
    } else {
        canvasEl.style.display = 'block';
        candleEl.style.display = 'none';

        chart.data.datasets[0].hidden = (mode === 'spread');
        chart.data.datasets[1].hidden = (mode === 'spread');
        chart.data.datasets[2].hidden = (mode === 'price');

        chart.options.scales.yPrice.display = (mode === 'price');
        chart.options.scales.ySpread.display = (mode === 'spread');
        chart.update('none');
    }
}

async function switchCurrency(currency) {
    activeCurrency = currency.toUpperCase();
    document.getElementById('btnCurrencyBTC').classList.toggle('active', activeCurrency === 'BTC');
    document.getElementById('btnCurrencyETH').classList.toggle('active', activeCurrency === 'ETH');

    try {
        const response = await fetch('/api/select-currency', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ currency: activeCurrency })
        });
        const data = await response.json();
        
        activeContract = data.active_contract || '';
        const select = document.getElementById('selectContractDropdown');
        select.innerHTML = '';
        
        if (data.contracts) {
            data.contracts.forEach(contractName => {
                const opt = document.createElement('option');
                opt.value = contractName;
                opt.textContent = contractName;
                if (contractName === activeContract) opt.selected = true;
                select.appendChild(opt);
            });
        }

        // Adjust default gap inputs for ETH vs BTC
        if (activeCurrency === 'ETH') {
            document.getElementById('inputDiffToTrade').value = 4.0;
            document.getElementById('inputDiffToCut').value = 0.5;
            document.getElementById('dispDiffToTrade').textContent = '$4.00';
            document.getElementById('dispDiffToCut').textContent = '$0.50';
        } else {
            document.getElementById('inputDiffToTrade').value = 50.0;
            document.getElementById('inputDiffToCut').value = 5.0;
            document.getElementById('dispDiffToTrade').textContent = '$50.00';
            document.getElementById('dispDiffToCut').textContent = '$5.00';
        }

        chartLabels.length = 0;
        mt5Prices.length = 0;
        deltaPrices.length = 0;
        spreadData.length = 0;
        chart.update();
    } catch (e) {
        console.error('Error switching currency:', e);
    }
}

async function loadAvailableContracts() {
    try {
        const res = await fetch('/api/available-contracts');
        const data = await res.json();
        const select = document.getElementById('selectContractDropdown');
        select.innerHTML = '';
        
        activeContract = data.active_contract || '';
        if (data.currency) {
            activeCurrency = data.currency;
            document.getElementById('btnCurrencyBTC').classList.toggle('active', activeCurrency === 'BTC');
            document.getElementById('btnCurrencyETH').classList.toggle('active', activeCurrency === 'ETH');
        }

        if (data.exchange) {
            activeExchange = data.exchange.toLowerCase();
            const btnBinance = document.getElementById('btnExchangeBinance');
            const btnBybit = document.getElementById('btnExchangeBybit');
            const btnDeribit = document.getElementById('btnExchangeDeribit');
            const btnDelta = document.getElementById('btnExchangeDelta');
            if (btnBinance) btnBinance.classList.toggle('active', activeExchange === 'binance');
            if (btnBybit) btnBybit.classList.toggle('active', activeExchange === 'bybit');
            if (btnDeribit) btnDeribit.classList.toggle('active', activeExchange === 'deribit');
            if (btnDelta) btnDelta.classList.toggle('active', activeExchange === 'delta');

            const testnetBox = document.getElementById('testnetToggleBox');
            if (testnetBox) testnetBox.style.display = (activeExchange === 'bybit' || activeExchange === 'binance') ? 'flex' : 'none';
        }

        if (data.is_testnet !== undefined) {
            isTestnet = data.is_testnet;
            const btn = document.getElementById('btnToggleTestnet');
            if (btn) {
                if (isTestnet) {
                    btn.className = 'testnet-btn active';
                    btn.textContent = '🟡 Testnet (Demo)';
                } else {
                    btn.className = 'testnet-btn live';
                    btn.textContent = '🔴 Mainnet (Live)';
                }
            }
        }

        if (data.contracts && data.contracts.length > 0) {
            data.contracts.forEach(contractName => {
                const opt = document.createElement('option');
                opt.value = contractName;
                opt.textContent = contractName;
                if (contractName === activeContract) {
                    opt.selected = true;
                }
                select.appendChild(opt);
            });
        }
    } catch (e) {
        console.error('Error loading contracts:', e);
    }
}

async function toggleTestnetMode() {
    isTestnet = !isTestnet;
    try {
        const res = await fetch('/api/toggle-testnet', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ testnet: isTestnet })
        });
        const data = await res.json();
        const btn = document.getElementById('btnToggleTestnet');
        if (btn) {
            if (isTestnet) {
                btn.className = 'testnet-btn active';
                btn.textContent = '🟡 Testnet (Demo)';
            } else {
                btn.className = 'testnet-btn live';
                btn.textContent = '🔴 Mainnet (Live)';
            }
        }
        await loadAvailableContracts();
    } catch (e) {
        console.error('Error toggling testnet:', e);
    }
}

async function onContractSelected() {
    const select = document.getElementById('selectContractDropdown');
    const chosenContract = select.value;
    if (!chosenContract) return;

    try {
        await fetch('/api/select-contract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ contract: chosenContract })
        });
        
        activeContract = chosenContract;
        chartLabels.length = 0;
        mt5Prices.length = 0;
        deltaPrices.length = 0;
        spreadData.length = 0;
        chart.update();
    } catch (e) {
        console.error('Error selecting contract:', e);
    }
}

async function switchExchangeSource(source) {
    activeExchange = source.toLowerCase();
    const btnBinance = document.getElementById('btnExchangeBinance');
    const btnBybit = document.getElementById('btnExchangeBybit');
    const btnDeribit = document.getElementById('btnExchangeDeribit');
    const btnDelta = document.getElementById('btnExchangeDelta');
    if (btnBinance) btnBinance.classList.toggle('active', activeExchange === 'binance');
    if (btnBybit) btnBybit.classList.toggle('active', activeExchange === 'bybit');
    if (btnDeribit) btnDeribit.classList.toggle('active', activeExchange === 'deribit');
    if (btnDelta) btnDelta.classList.toggle('active', activeExchange === 'delta');

    const testnetBox = document.getElementById('testnetToggleBox');
    if (testnetBox) testnetBox.style.display = (activeExchange === 'bybit' || activeExchange === 'binance') ? 'flex' : 'none';

    const contractBox = document.getElementById('contractSelectorBox');
    if (activeExchange === 'binance' || activeExchange === 'bybit' || activeExchange === 'deribit') {
        contractBox.style.display = 'flex';
        await loadAvailableContracts();
    } else {
        contractBox.style.display = 'none';
    }

    try {
        await fetch('/api/switch-exchange', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ exchange: source })
        });
        
        chartLabels.length = 0;
        mt5Prices.length = 0;
        deltaPrices.length = 0;
        spreadData.length = 0;
        chart.update();
    } catch (e) {
        console.error('Error switching exchange:', e);
    }
}

// EA Settings & Fire Actions
async function applyEaSettings() {
    const diffToTrade = parseFloat(document.getElementById('inputDiffToTrade').value) || (activeCurrency === 'ETH' ? 4.0 : 50.0);
    const diffToCut = parseFloat(document.getElementById('inputDiffToCut').value) || (activeCurrency === 'ETH' ? 0.5 : 5.0);
    const tradeSize = parseFloat(document.getElementById('inputTradeSize').value) || 0.1;
    const targetProfit = parseFloat(document.getElementById('inputTargetProfit')?.value) || 1.0;

    try {
        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                diff_to_trade: diffToTrade,
                diff_to_cut: diffToCut,
                trade_size: tradeSize,
                auto_trade: autoTradeEnabled,
                target_profit_usd: targetProfit
            })
        });
        document.getElementById('dispDiffToTrade').textContent = `$${diffToTrade.toFixed(2)}`;
        document.getElementById('dispDiffToCut').textContent = `$${diffToCut.toFixed(2)}`;
    } catch (e) {
        console.error('Error applying EA settings:', e);
    }
}

async function toggleAutoTrade() {
    autoTradeEnabled = !autoTradeEnabled;
    const btn = document.getElementById('btnAutoTrade');
    btn.textContent = autoTradeEnabled ? 'ON (AUTO)' : 'OFF';
    btn.className = `auto-trade-toggle ${autoTradeEnabled ? 'on' : ''}`;
    await applyEaSettings();
}

async function fireManualOrder() {
    try {
        const response = await fetch('/api/fire-order', { method: 'POST' });
        const res = await response.json();
        alert(`⚡ ${res.message}`);
    } catch (e) {
        alert(`Error firing order: ${e.message}`);
    }
}

async function closeAllPositions() {
    try {
        const response = await fetch('/api/close-positions', { method: 'POST' });
        const res = await response.json();
        alert(`🔴 ${res.message}`);
    } catch (e) {
        alert(`Error closing positions: ${e.message}`);
    }
}

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
        document.getElementById('wsDot').className = 'dot pulse-green';
        document.getElementById('wsStatus').textContent = 'Connected';
    };

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === 'INIT_HISTORY') {
            if (data.exchange_source) {
                activeExchange = data.exchange_source.toLowerCase();
                document.getElementById('btnExchangeDeribit').classList.toggle('active', activeExchange === 'deribit');
                document.getElementById('btnExchangeDelta').classList.toggle('active', activeExchange === 'delta');
            }
            if (data.active_currency) {
                activeCurrency = data.active_currency;
                document.getElementById('btnCurrencyBTC').classList.toggle('active', activeCurrency === 'BTC');
                document.getElementById('btnCurrencyETH').classList.toggle('active', activeCurrency === 'ETH');
            }
            if (data.active_contract) {
                activeContract = data.active_contract;
                const select = document.getElementById('selectContractDropdown');
                if (select) select.value = activeContract;
            }
            if (data.diff_to_trade) {
                document.getElementById('inputDiffToTrade').value = data.diff_to_trade;
                document.getElementById('dispDiffToTrade').textContent = `$${data.diff_to_trade.toFixed(2)}`;
            }
            if (data.diff_to_cut) {
                document.getElementById('inputDiffToCut').value = data.diff_to_cut;
                document.getElementById('dispDiffToCut').textContent = `$${data.diff_to_cut.toFixed(2)}`;
            }
            if (data.auto_trade !== undefined) {
                autoTradeEnabled = data.auto_trade;
                const btn = document.getElementById('btnAutoTrade');
                btn.textContent = autoTradeEnabled ? 'ON (AUTO)' : 'OFF';
                btn.className = `auto-trade-toggle ${autoTradeEnabled ? 'on' : ''}`;
            }
            data.data.forEach(item => updateDashboard(item, false));
            chart.update();
            return;
        }

        updateDashboard(data, true);
    };

    socket.onclose = () => {
        document.getElementById('wsDot').className = 'dot';
        document.getElementById('wsStatus').textContent = 'Disconnected (Reconnecting...)';
        setTimeout(connectWebSocket, 2000);
    };

    socket.onerror = (err) => {
        console.error('WebSocket Error:', err);
    };
}

function updateDashboard(data, redrawChart = true) {
    if (!data) return;

    const mt5 = data.mt5 || {};
    const delta = data.delta || {};
    const exSource = (data.exchange_source || activeExchange).toUpperCase();
    const contract = data.active_contract || activeContract || delta.symbol || 'FUTURES';
    const isDemo = data.is_testnet;

    // Update Header Tag & Badges
    const badgeText = (exSource === 'BYBIT' || exSource === 'BINANCE') ? (isDemo ? `${exSource} DEMO FUTURES` : `${exSource} LIVE FUTURES`) : `${exSource} FUTURES`;
    const headerTag = document.getElementById('headerExchangeTag');
    if (headerTag) headerTag.textContent = `${contract} (${badgeText})`;
    const futBadge = document.getElementById('futuresBadge');
    if (futBadge) futBadge.textContent = badgeText;
    const chartSub = document.getElementById('chartSubtext');
    if (chartSub) chartSub.textContent = `Comparing live tick updates from MT5 & ${contract} on ${badgeText}`;

    // Compute Spreads for both pairs
    let mt5Spread = 0;
    if (data.mt5_spread !== undefined && data.mt5_spread !== null) {
        mt5Spread = data.mt5_spread;
    } else if (mt5.ask && mt5.bid) {
        mt5Spread = Math.max(0, mt5.ask - mt5.bid);
    }

    let deltaSpread = 0;
    if (data.futures_spread !== undefined && data.futures_spread !== null) {
        deltaSpread = data.futures_spread;
    } else if (delta.ask && delta.bid) {
        deltaSpread = Math.max(0, delta.ask - delta.bid);
    }

    // Update MT5 UI
    if (mt5.mid) document.getElementById('mt5MidPrice').textContent = `$${mt5.mid.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
    if (mt5.bid) document.getElementById('mt5Bid').textContent = `$${mt5.bid.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
    if (mt5.ask) document.getElementById('mt5Ask').textContent = `$${mt5.ask.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
    const elMt5Spread = document.getElementById('mt5Spread');
    if (elMt5Spread) elMt5Spread.textContent = `$${mt5Spread.toFixed(2)}`;
    if (mt5.symbol) document.getElementById('mt5Symbol').textContent = mt5.symbol;

    // Update Futures UI
    if (delta.mid) document.getElementById('deltaMidPrice').textContent = `$${delta.mid.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
    if (delta.bid) document.getElementById('deltaBid').textContent = `$${delta.bid.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
    if (delta.ask) document.getElementById('deltaAsk').textContent = `$${delta.ask.toLocaleString('en-US', {minimumFractionDigits: 2})}`;
    const elDeltaSpread = document.getElementById('deltaSpread');
    if (elDeltaSpread) elDeltaSpread.textContent = `$${deltaSpread.toFixed(2)}`;
    if (delta.symbol) document.getElementById('deltaSymbol').textContent = delta.symbol;

    // Update Metrics
    const spreadUsd = data.spread_usd || 0;
    const spreadPct = data.spread_pct || 0;

    const elSpreadUsd = document.getElementById('spreadUsd');
    if (elSpreadUsd) elSpreadUsd.textContent = `${spreadUsd >= 0 ? '+' : ''}$${spreadUsd.toFixed(2)}`;
    const elSpreadPct = document.getElementById('spreadPct');
    if (elSpreadPct) elSpreadPct.textContent = `${spreadPct >= 0 ? '+' : ''}${spreadPct.toFixed(3)}%`;
    const elSpreadBadge = document.getElementById('spreadBadge');
    if (elSpreadBadge) elSpreadBadge.textContent = `${spreadPct >= 0 ? '+' : ''}${spreadPct.toFixed(2)}% Basis`;
    const elSignalLiveGap = document.getElementById('signalLiveGap');
    if (elSignalLiveGap) elSignalLiveGap.textContent = `${spreadUsd >= 0 ? '+' : ''}$${spreadUsd.toFixed(2)}`;
    
    const elMetricSpotSpread = document.getElementById('metricSpotSpread');
    if (elMetricSpotSpread) elMetricSpotSpread.textContent = `$${mt5Spread.toFixed(2)}`;
    const elMetricFuturesSpread = document.getElementById('metricFuturesSpread');
    if (elMetricFuturesSpread) elMetricFuturesSpread.textContent = `$${deltaSpread.toFixed(2)}`;

    const buySpotEl = document.getElementById('spreadBuySpot');
    if (buySpotEl) {
        const netExecGap = (data.spread_buy_spot_sell_fut !== undefined) 
            ? data.spread_buy_spot_sell_fut 
            : ((delta.bid && mt5.ask) ? (delta.bid - mt5.ask) : 0);
        buySpotEl.textContent = `${netExecGap >= 0 ? '+' : ''}$${netExecGap.toFixed(2)}`;
    }
    const sellSpotEl = document.getElementById('spreadSellSpot');
    if (sellSpotEl) sellSpotEl.textContent = `$${(data.spread_sell_spot_buy_fut || 0).toFixed(2)}`;

    // Update Active Positions Count & Table
    const activePositions = data.active_positions || [];
    const elActivePairsCount = document.getElementById('activePairsCount');
    if (elActivePairsCount) elActivePairsCount.textContent = `${activePositions.length} Pairs`;
    updatePositionsTable(activePositions, spreadUsd);

    // Update Banner
    const banner = document.getElementById('signalBanner');
    const signalTitle = document.getElementById('signalTitle');
    const signalDetail = document.getElementById('signalDetail');
    const signalIcon = document.getElementById('signalIcon');

    if (data.is_opportunity) {
        banner.className = 'signal-banner opportunity';
        signalIcon.textContent = '🚀';
        signalTitle.textContent = `TARGET GAP REACHED: ${data.signal.replace(/_/g, ' ')}`;
        signalDetail.textContent = data.action_detail || `Live gap ${spreadUsd >= 0 ? '+' : ''}$${spreadUsd.toFixed(2)} hit target open threshold!`;
    } else {
        banner.className = 'signal-banner neutral';
        signalIcon.textContent = '⏳';
        signalTitle.textContent = 'MONITORING LIVE GAP POINTS';
        signalDetail.textContent = `Live running gap for ${contract} is ${spreadUsd >= 0 ? '+' : ''}$${spreadUsd.toFixed(2)}. Open Target (diffToTrade) is $${(data.diff_to_trade || 50).toFixed(2)}.`;
    }

    // Chart update
    if (data.time_str && mt5.mid && delta.mid) {
        chartLabels.push(data.time_str);
        mt5Prices.push(mt5.mid);
        deltaPrices.push(delta.mid);
        spreadData.push(spreadUsd);

        if (chartLabels.length > MAX_CHART_POINTS) {
            chartLabels.shift();
            mt5Prices.shift();
            deltaPrices.shift();
            spreadData.shift();
        }

        // Live Candlestick update: Bid(Fut) - Ask(Spot)
        const netExecSpread = (delta.bid && mt5.ask) ? (delta.bid - mt5.ask) : spreadUsd;
        updateCandlesticks(mt5.mid, delta.mid, data.timestamp, netExecSpread);

        if (redrawChart) {
            chart.update('none');
        }
    }
}

function updatePositionsTable(positions, currentGap = 0) {
    const tbody = document.getElementById('logsTableBody');
    document.getElementById('logCount').textContent = `${positions.length} active positions`;

    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No active open positions... Click "⚡ FIRE ORDER NOW" or enable Auto-Trade to enter when target gap is hit!</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    positions.forEach(pos => {
        const row = document.createElement('tr');
        const entryGap = pos.entry_gap || 0;
        const size = pos.trade_size || 0.1;
        // If entered long spot & short futures (positive entry gap), profit when gap narrows
        const capturedGap = entryGap >= 0 ? (entryGap - currentGap) : (currentGap - entryGap);
        const livePnl = capturedGap * size;
        const pnlClass = livePnl >= 0 ? 'text-green' : 'text-red';
        const pnlSign = livePnl >= 0 ? '+' : '';

        row.innerHTML = `
            <td>${pos.open_time}<br><span class="text-muted">${pos.id}</span></td>
            <td>$${pos.spot_price ? pos.spot_price.toFixed(2) : '-'}</td>
            <td>$${pos.fut_price ? pos.fut_price.toFixed(2) : '-'}</td>
            <td>
                <span class="text-amber">Entry: ${entryGap >= 0 ? '+' : ''}$${entryGap.toFixed(2)}</span><br>
                <span class="${pnlClass}" style="font-weight: 700;">PnL: ${pnlSign}$${livePnl.toFixed(2)} (${pnlSign}$${capturedGap.toFixed(2)} Gap)</span>
            </td>
            <td><span class="badge-blue">${pos.signal}</span></td>
            <td><span class="badge-amber">${pos.status}</span></td>
        `;
        tbody.appendChild(row);
    });
}

// Modal functions
function openCredentialsModal() {
    document.getElementById('credentialsModal').classList.remove('hidden');
    onExchangeCredChange();
}

function closeCredentialsModal() {
    document.getElementById('credentialsModal').classList.add('hidden');
}

function onExchangeCredChange() {
    const exchange = document.getElementById('inputExchangeType').value;
    const bybitInfo = document.getElementById('bybitCredInfo');
    const lblKey = document.getElementById('lblApiKey');
    const lblSecret = document.getElementById('lblApiSecret');
    const inputKey = document.getElementById('inputApiKey');
    const inputSecret = document.getElementById('inputApiSecret');

    if (exchange === 'binance') {
        if (bybitInfo) {
            bybitInfo.style.display = 'block';
            bybitInfo.innerHTML = '<span>💡 Binance Futures Testnet Demo API Key from <a href="https://testnet.binancefuture.com" target="_blank" style="color: #f0b90b; text-decoration: underline; font-weight: 600;">testnet.binancefuture.com</a>.</span>';
        }
        if (lblKey) lblKey.textContent = 'Binance API Key (Testnet / Live)';
        if (lblSecret) lblSecret.textContent = 'Binance API Secret';
        if (inputKey) inputKey.placeholder = 'e.g. icTuw7Telt1SbjAK...';
        if (inputSecret) inputSecret.placeholder = 'e.g. LRBCamWl8CXumqzc...';
    } else if (exchange === 'bybit') {
        if (bybitInfo) {
            bybitInfo.style.display = 'block';
            bybitInfo.innerHTML = '<span>💡 For free Bybit Demo Trading, generate API Key on <a href="https://testnet.bybit.com/en/profile/api-management" target="_blank" style="color: #f7a600; text-decoration: underline; font-weight: 600;">testnet.bybit.com</a> with <strong>Read-Write</strong> & <strong>Orders</strong> permissions.</span>';
        }
        if (lblKey) lblKey.textContent = 'Bybit API Key (Testnet / Live)';
        if (lblSecret) lblSecret.textContent = 'Bybit API Secret';
        if (inputKey) inputKey.placeholder = 'e.g. gNc6EV3kHDZ1E2rF...';
        if (inputSecret) inputSecret.placeholder = 'e.g. iql86ce5I4R93nc...';
    } else if (exchange === 'deribit') {
        if (bybitInfo) bybitInfo.style.display = 'none';
        if (lblKey) lblKey.textContent = 'Deribit Client ID';
        if (lblSecret) lblSecret.textContent = 'Deribit Client Secret';
        if (inputKey) inputKey.placeholder = 'e.g. ylpXJHz_';
        if (inputSecret) inputSecret.placeholder = 'e.g. jcgIBLUUIywvxrN...';
    } else {
        if (bybitInfo) bybitInfo.style.display = 'none';
        if (lblKey) lblKey.textContent = 'Delta Exchange API Key';
        if (lblSecret) lblSecret.textContent = 'Delta Exchange API Secret';
        if (inputKey) inputKey.placeholder = 'e.g. delta_key_...';
        if (inputSecret) inputSecret.placeholder = 'e.g. delta_secret_...';
    }
}

async function saveCredentials() {
    const exchange = document.getElementById('inputExchangeType').value;
    const key = document.getElementById('inputApiKey').value.trim();
    const secret = document.getElementById('inputApiSecret').value.trim();
    const resBox = document.getElementById('credResult');

    if (!key || !secret) {
        resBox.className = 'alert-box text-red';
        resBox.textContent = 'Please fill out both Client ID/Key and Secret.';
        resBox.classList.remove('hidden');
        return;
    }

    try {
        const response = await fetch('/api/credentials', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ exchange: exchange, api_key: key, api_secret: secret })
        });
        const res = await response.json();
        if (res.status === 'success') {
            resBox.className = 'alert-box text-green';
            let detail = '';
            if (res.account && res.account.total_equity) {
                detail = ` | Verified Equity: $${parseFloat(res.account.total_equity).toLocaleString()} USDT (${res.is_testnet ? 'Testnet Demo' : 'Live'})`;
            }
            resBox.textContent = `✅ ${exchange.toUpperCase()} credentials connected successfully!${detail}`;
            resBox.classList.remove('hidden');
            setTimeout(closeCredentialsModal, 2000);
        } else {
            resBox.className = 'alert-box text-red';
            resBox.textContent = `Connection warning: ${res.account?.error || 'Could not verify balance'}`;
            resBox.classList.remove('hidden');
        }
    } catch (e) {
        resBox.className = 'alert-box text-red';
        resBox.textContent = `Error saving credentials: ${e.message}`;
        resBox.classList.remove('hidden');
    }
}
