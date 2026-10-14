// ==========================================================================
// 5-Leg Crypto Arbitrage Terminal - Frontend Application Logic
// ==========================================================================

let socket = null;
let chart = null;
let currentChartMode = 'price'; // 'price', 'spread', 'candle'
let activeExchange = 'binance';
let isTestnet = true;
let activeCurrency = 'ETH';
let activeContract = 'ETHUSDT_261225';
let autoTradeEnabled = false;
let currentView = 'terminal';

// History Pagination & Filtering State
let histLimit = 20;
let histOffset = 0;
let histTotal = 0;
let histDebounceTimer = null;
let currentSelectedTradeJson = null;

// Log Viewer State
let logAutoRefreshTimer = null;
let logDebounceTimer = null;

// Chart.js Data Buffer
const MAX_CHART_POINTS = 50;
const chartLabels = [];
const mt5Prices = [];
const deltaPrices = [];
const spreadData = [];

// Lightweight TradingView Candlestick Chart
let candlestickChart = null;
let spreadCandleSeries = null;
let currentBarTimeSec = 0;
let spreadCandle = null;

// Cache of current 5 legs
let currentLegs = [];
let legDebounceTimers = {};

document.addEventListener('DOMContentLoaded', async () => {
    initClock();
    initChart();
    // 1. Immediately load local cached inputs to avoid blank/default flicker on refresh
    loadLegsFromLocalStorage();
    // 2. Authoritatively fetch server state (persisted settings & active trades)
    await fetchInitialStatus();
    // 3. Load contracts, connect WebSocket, and load stats
    loadAvailableContracts();
    connectWebSocket();
    loadHistoryStats();
});

function saveLegsToLocalStorage(legs) {
    if (!legs || !Array.isArray(legs) || legs.length === 0) return;
    try {
        localStorage.setItem(`crypto_arbi_legs_${activeCurrency}`, JSON.stringify(legs));
    } catch (e) {}
}

function loadLegsFromLocalStorage() {
    try {
        const cached = localStorage.getItem(`crypto_arbi_legs_${activeCurrency}`);
        if (cached) {
            const parsed = JSON.parse(cached);
            if (Array.isArray(parsed) && parsed.length >= 5) {
                currentLegs = parsed;
                renderLegsGrid(parsed);
                return true;
            }
        }
    } catch (e) {}
    renderLegsGrid();
    return false;
}

function showLegSaveFeedback(legId) {
    const card = document.getElementById(`legCard_${legId}`);
    if (card) {
        card.classList.add('saved-pulse');
        setTimeout(() => card.classList.remove('saved-pulse'), 700);
    }
}

async function fetchInitialStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data) {
            if (data.active_currency) {
                activeCurrency = data.active_currency;
                document.getElementById('btnCurrencyBTC')?.classList.toggle('active', activeCurrency === 'BTC');
                document.getElementById('btnCurrencyETH')?.classList.toggle('active', activeCurrency === 'ETH');
            }
            if (data.exchange_source) {
                activeExchange = data.exchange_source.toLowerCase();
                document.getElementById('btnExchangeBinance')?.classList.toggle('active', activeExchange === 'binance');
                document.getElementById('btnExchangeBybit')?.classList.toggle('active', activeExchange === 'bybit');
                document.getElementById('btnExchangeDeribit')?.classList.toggle('active', activeExchange === 'deribit');
                document.getElementById('btnExchangeDelta')?.classList.toggle('active', activeExchange === 'delta');
            }
            if (data.auto_trade !== undefined) {
                autoTradeEnabled = data.auto_trade;
                const btn = document.getElementById('btnAutoTrade');
                if (btn) {
                    btn.textContent = autoTradeEnabled ? 'ON (AUTO)' : 'OFF';
                    btn.className = `auto-trade-toggle ${autoTradeEnabled ? 'on' : ''}`;
                }
            }
            // Populate exact persisted leg settings from DB
            if (data.legs && data.legs.length > 0) {
                currentLegs = data.legs;
                renderLegsGrid(data.legs);
                saveLegsToLocalStorage(data.legs);
            }
            // Show any open positions immediately on page load / refresh
            const activePositions = data.active_positions || [];
            const currentGap = data.evaluation ? (data.evaluation.spread_usd || 0) : 0;
            updatePositionsTable(activePositions, currentGap);
            const elActivePairsCount = document.getElementById('activePairsCount');
            if (elActivePairsCount) elActivePairsCount.textContent = `${activePositions.length} / 5 Legs`;
        }
    } catch (e) {
        console.error('Error fetching initial status:', e);
    }
}

// ==========================================================================
// View Switching (Sidebar Navigation)
// ==========================================================================
function switchView(viewName) {
    currentView = viewName;
    
    // Update Sidebar Navigation buttons
    document.getElementById('navTerminal').classList.toggle('active', viewName === 'terminal');
    document.getElementById('navHistory').classList.toggle('active', viewName === 'history');
    document.getElementById('navLogs').classList.toggle('active', viewName === 'logs');

    // Update View Sections
    document.getElementById('viewTerminal').classList.toggle('active', viewName === 'terminal');
    document.getElementById('viewHistory').classList.toggle('active', viewName === 'history');
    document.getElementById('viewLogs').classList.toggle('active', viewName === 'logs');

    // Update Header Title
    const titleMap = {
        'terminal': 'Live 5-Leg Arbitrage Terminal',
        'history': 'Trade Execution History (SQLite Database)',
        'logs': 'Daily Rotating System Logs'
    };
    document.getElementById('pageTitle').textContent = titleMap[viewName] || 'Arbitrage Terminal';

    if (viewName === 'history') {
        loadTradeHistory();
        loadHistoryStats();
    } else if (viewName === 'logs') {
        loadLogDates();
    }
}

// ==========================================================================
// Clock & Charts
// ==========================================================================
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
                legend: { labels: { color: '#f0f6fc', font: { family: 'Inter' } } }
            }
        }
    });
}

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
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: { borderColor: 'rgba(255, 255, 255, 0.1)' },
            timeScale: {
                borderColor: 'rgba(255, 255, 255, 0.1)',
                timeVisible: true,
                secondsVisible: true
            }
        });

        spreadCandleSeries = candlestickChart.addCandlestickSeries({
            upColor: '#22c55e',
            downColor: '#ef4444',
            borderUpColor: '#22c55e',
            borderDownColor: '#ef4444',
            wickUpColor: '#22c55e',
            wickDownColor: '#ef4444',
            title: '⚡ Gap ($): Futures - Spot'
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

function updateCandlesticks(timestampMs, spreadVal) {
    if (spreadVal === undefined || spreadVal === null) return;
    if (!candlestickChart) initCandlestickChart();

    const barIntervalSec = 5;
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
        if (!candlestickChart) initCandlestickChart();
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

// ==========================================================================
// 5-LEG MATRIX UI RENDERING & CONTROLS
// ==========================================================================
function renderLegsGrid(legs = null) {
    const container = document.getElementById('legsContainer');
    if (!container) return;

    if (legs && legs.length > 0) {
        currentLegs = legs;
    } else if (currentLegs.length === 0) {
        // Fallback default 5 legs set to 0.0 for safety
        currentLegs = [
            { leg_id: 1, enabled: false, diff_to_trade: 0.0, diff_to_cut: 0.0, trade_size: 0.0, target_profit_usd: 0.0, status: 'IDLE' },
            { leg_id: 2, enabled: false, diff_to_trade: 0.0, diff_to_cut: 0.0, trade_size: 0.0, target_profit_usd: 0.0, status: 'IDLE' },
            { leg_id: 3, enabled: false, diff_to_trade: 0.0, diff_to_cut: 0.0, trade_size: 0.0, target_profit_usd: 0.0, status: 'IDLE' },
            { leg_id: 4, enabled: false, diff_to_trade: 0.0, diff_to_cut: 0.0, trade_size: 0.0, target_profit_usd: 0.0, status: 'IDLE' },
            { leg_id: 5, enabled: false, diff_to_trade: 0.0, diff_to_cut: 0.0, trade_size: 0.0, target_profit_usd: 0.0, status: 'IDLE' }
        ];
    }

    container.innerHTML = '';
    currentLegs.forEach(leg => {
        const lid = leg.leg_id;
        const isOpen = leg.status === 'OPEN';
        const pos = leg.active_position;
        const liveNetPnl = pos ? (pos.live_net_pnl || 0) : 0;
        const pnlColor = liveNetPnl >= 0 ? 'text-green' : 'text-red';
        const pnlSign = liveNetPnl >= 0 ? '+' : '';

        const card = document.createElement('div');
        card.className = `leg-card ${isOpen ? 'open-active' : ''}`;
        card.id = `legCard_${lid}`;

        card.innerHTML = `
            <div class="leg-header">
                <div class="leg-badge-box">
                    <input type="checkbox" id="legChk_${lid}" class="leg-enable-chk" ${leg.enabled ? 'checked' : ''} onchange="onLegEnableChange(${lid})">
                    <span class="leg-number-badge">LEG ${lid}</span>
                </div>
                <span class="leg-status-pill ${isOpen ? 'open' : 'idle'}" id="legStatus_${lid}">
                    ${isOpen ? 'OPEN 🟢' : 'IDLE ⚪'}
                </span>
            </div>

            <div class="leg-inputs-group">
                <div class="leg-field">
                    <label>Open Gap ($)</label>
                    <input type="number" step="0.5" id="legOpenGap_${lid}" value="${leg.diff_to_trade}" onchange="saveLegDebounced(${lid})">
                </div>
                <div class="leg-field">
                    <label>Cut Gap ($)</label>
                    <input type="number" step="0.25" id="legCutGap_${lid}" value="${leg.diff_to_cut}" onchange="saveLegDebounced(${lid})">
                </div>
                <div class="leg-field">
                    <label>Trade Size</label>
                    <input type="number" step="0.01" id="legSize_${lid}" value="${leg.trade_size}" onchange="saveLegDebounced(${lid})">
                </div>
                <div class="leg-field">
                    <label>Net TP ($)</label>
                    <input type="number" step="0.25" id="legTp_${lid}" value="${leg.target_profit_usd}" onchange="saveLegDebounced(${lid})">
                </div>
            </div>

            <div class="leg-live-stats" id="legStats_${lid}">
                <span class="stat-label">${isOpen ? `Ticket #${pos?.ticket || '-'}` : 'Waiting for trigger'}</span>
                <span class="stat-val ${isOpen ? pnlColor : 'text-muted'}" style="font-weight:700;">
                    ${isOpen ? `${pnlSign}$${liveNetPnl.toFixed(2)} Net` : '$0.00'}
                </span>
            </div>

            <div class="leg-actions">
                <button class="btn-leg-fire" onclick="fireSingleLeg(${lid})">⚡ Fire</button>
                <button class="btn-leg-save" id="btnSaveLeg_${lid}" onclick="saveSingleLegBtn(${lid})">💾 Save</button>
                <button class="btn-leg-cut" onclick="closeSingleLeg(${lid})" ${isOpen ? '' : 'style="opacity:0.4; cursor:not-allowed;"'}>🔴 Cut</button>
            </div>
        `;
        container.appendChild(card);
    });
}

function updateLegLiveStates(legs) {
    if (!legs) return;
    currentLegs = legs;

    legs.forEach(leg => {
        const lid = leg.leg_id;
        const isOpen = leg.status === 'OPEN';
        const card = document.getElementById(`legCard_${lid}`);
        const statusPill = document.getElementById(`legStatus_${lid}`);
        const statsBox = document.getElementById(`legStats_${lid}`);

        if (card) card.className = `leg-card ${isOpen ? 'open-active' : ''}`;
        if (statusPill) {
            statusPill.className = `leg-status-pill ${isOpen ? 'open' : 'idle'}`;
            statusPill.textContent = isOpen ? 'OPEN 🟢' : 'IDLE ⚪';
        }

        // Keep cut button active only if leg is open
        if (card) {
            const cutBtn = card.querySelector('.btn-leg-cut');
            if (cutBtn) {
                cutBtn.style.opacity = isOpen ? '1' : '0.4';
                cutBtn.style.cursor = isOpen ? 'pointer' : 'not-allowed';
            }
        }

        // Only sync inputs if user is NOT currently focusing an input in this card
        const activeEl = document.activeElement;
        const isEditingThisCard = activeEl && card && card.contains(activeEl);
        if (!isEditingThisCard) {
            const chk = document.getElementById(`legChk_${lid}`);
            const openGap = document.getElementById(`legOpenGap_${lid}`);
            const cutGap = document.getElementById(`legCutGap_${lid}`);
            const size = document.getElementById(`legSize_${lid}`);
            const tp = document.getElementById(`legTp_${lid}`);

            if (chk && chk.checked !== leg.enabled) chk.checked = leg.enabled;
            if (openGap && parseFloat(openGap.value) !== leg.diff_to_trade) openGap.value = leg.diff_to_trade;
            if (cutGap && parseFloat(cutGap.value) !== leg.diff_to_cut) cutGap.value = leg.diff_to_cut;
            if (size && parseFloat(size.value) !== leg.trade_size) size.value = leg.trade_size;
            if (tp && parseFloat(tp.value) !== leg.target_profit_usd) tp.value = leg.target_profit_usd;
        }

        if (statsBox) {
            const pos = leg.active_position;
            if (isOpen && pos) {
                const liveNetPnl = pos.live_net_pnl || 0;
                const pnlSign = liveNetPnl >= 0 ? '+' : '';
                const pnlClass = liveNetPnl >= 0 ? 'text-green' : 'text-red';
                statsBox.innerHTML = `
                    <span class="stat-label">Ticket #${pos.ticket || '-'}</span>
                    <span class="stat-val ${pnlClass}" style="font-weight:700;">${pnlSign}$${liveNetPnl.toFixed(2)} Net</span>
                `;
            } else {
                statsBox.innerHTML = `
                    <span class="stat-label">Waiting for trigger</span>
                    <span class="stat-val text-muted">$0.00</span>
                `;
            }
        }
    });
}

async function saveAllLegs() {
    const payload = [];
    for (let lid = 1; lid <= 5; lid++) {
        const chk = document.getElementById(`legChk_${lid}`);
        const openGap = document.getElementById(`legOpenGap_${lid}`);
        const cutGap = document.getElementById(`legCutGap_${lid}`);
        const size = document.getElementById(`legSize_${lid}`);
        const tp = document.getElementById(`legTp_${lid}`);

        if (chk && openGap && cutGap && size && tp) {
            payload.push({
                leg_id: lid,
                enabled: chk.checked,
                diff_to_trade: parseFloat(openGap.value) || 4.0,
                diff_to_cut: parseFloat(cutGap.value) || 0.5,
                trade_size: parseFloat(size.value) || 0.1,
                target_profit_usd: parseFloat(tp.value) || 1.0
            });
        }
    }

    // Save to local cache immediately
    saveLegsToLocalStorage(payload);

    try {
        const res = await fetch('/api/legs/batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ legs: payload })
        });
        const data = await res.json();
        if (data.status === 'success') {
            for (let lid = 1; lid <= 5; lid++) {
                showLegSaveFeedback(lid);
            }
            alert('✅ All 5 Leg settings securely saved to database!');
        }
    } catch (e) {
        console.error('Error saving legs:', e);
        alert('Error saving settings: ' + e.message);
    }
}

function saveLegDebounced(legId) {
    const chk = document.getElementById(`legChk_${legId}`);
    const openGap = document.getElementById(`legOpenGap_${legId}`);
    const cutGap = document.getElementById(`legCutGap_${legId}`);
    const size = document.getElementById(`legSize_${legId}`);
    const tp = document.getElementById(`legTp_${legId}`);

    if (!chk || !openGap || !cutGap || !size || !tp) return;

    // 1. Immediately update in-memory currentLegs & localStorage (never lost!)
    const target = currentLegs.find(l => l.leg_id === legId);
    if (target) {
        target.enabled = chk.checked;
        target.diff_to_trade = parseFloat(openGap.value) || target.diff_to_trade;
        target.diff_to_cut = parseFloat(cutGap.value) || target.diff_to_cut;
        target.trade_size = parseFloat(size.value) || target.trade_size;
        target.target_profit_usd = parseFloat(tp.value) || target.target_profit_usd;
    }
    saveLegsToLocalStorage(currentLegs);

    // 2. Debounce backend persistence
    clearTimeout(legDebounceTimers[legId]);
    legDebounceTimers[legId] = setTimeout(async () => {
        try {
            const res = await fetch(`/api/legs/${legId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    leg_id: legId,
                    enabled: chk.checked,
                    diff_to_trade: parseFloat(openGap.value) || 4.0,
                    diff_to_cut: parseFloat(cutGap.value) || 0.5,
                    trade_size: parseFloat(size.value) || 0.1,
                    target_profit_usd: parseFloat(tp.value) || 1.0
                })
            });
            const data = await res.json();
            if (data.status === 'success') {
                showLegSaveFeedback(legId);
            }
        } catch (e) {
            console.error(`Error saving leg ${legId}:`, e);
        }
    }, 400);
}

function onLegEnableChange(legId) {
    saveLegDebounced(legId);
}

async function saveSingleLegBtn(legId) {
    const chk = document.getElementById(`legChk_${legId}`);
    const openGap = document.getElementById(`legOpenGap_${legId}`);
    const cutGap = document.getElementById(`legCutGap_${legId}`);
    const size = document.getElementById(`legSize_${legId}`);
    const tp = document.getElementById(`legTp_${legId}`);

    if (!chk || !openGap || !cutGap || !size || !tp) return;

    const valOpenGap = parseFloat(openGap.value) || 0.0;
    const valCutGap = parseFloat(cutGap.value) || 0.0;
    const valSize = parseFloat(size.value) || 0.0;
    const valTp = parseFloat(tp.value) || 0.0;

    const target = currentLegs.find(l => l.leg_id === legId);
    if (target) {
        target.enabled = chk.checked;
        target.diff_to_trade = valOpenGap;
        target.diff_to_cut = valCutGap;
        target.trade_size = valSize;
        target.target_profit_usd = valTp;
    }
    saveLegsToLocalStorage(currentLegs);

    const btn = document.getElementById(`btnSaveLeg_${legId}`);
    if (btn) btn.textContent = '⏳ Saving';

    try {
        const res = await fetch(`/api/legs/${legId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                leg_id: legId,
                enabled: chk.checked,
                diff_to_trade: valOpenGap,
                diff_to_cut: valCutGap,
                trade_size: valSize,
                target_profit_usd: valTp
            })
        });
        const data = await res.json();
        if (data.status === 'success') {
            showLegSaveFeedback(legId);
            if (btn) {
                btn.textContent = '✅ Saved';
                btn.classList.add('saved-success');
                setTimeout(() => {
                    btn.textContent = '💾 Save';
                    btn.classList.remove('saved-success');
                }, 1200);
            }
        } else {
            if (btn) btn.textContent = '❌ Fail';
            alert(`Could not save Leg ${legId}: ${data.detail || data.error}`);
        }
    } catch (e) {
        console.error(`Error saving leg ${legId}:`, e);
        if (btn) btn.textContent = '❌ Err';
    }
}

async function zeroAllLegs() {
    if (!confirm('Are you sure you want to set all 5 legs to 0?')) return;
    const zeroPayload = [];
    for (let lid = 1; lid <= 5; lid++) {
        zeroPayload.push({
            leg_id: lid,
            enabled: false,
            diff_to_trade: 0.0,
            diff_to_cut: 0.0,
            trade_size: 0.0,
            target_profit_usd: 0.0
        });
        const chk = document.getElementById(`legChk_${lid}`);
        const openGap = document.getElementById(`legOpenGap_${lid}`);
        const cutGap = document.getElementById(`legCutGap_${lid}`);
        const size = document.getElementById(`legSize_${lid}`);
        const tp = document.getElementById(`legTp_${lid}`);
        if (chk) chk.checked = false;
        if (openGap) openGap.value = 0.0;
        if (cutGap) cutGap.value = 0.0;
        if (size) size.value = 0.0;
        if (tp) tp.value = 0.0;
    }
    currentLegs = zeroPayload;
    saveLegsToLocalStorage(zeroPayload);

    try {
        await fetch('/api/legs/batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ legs: zeroPayload })
        });
        for (let lid = 1; lid <= 5; lid++) {
            showLegSaveFeedback(lid);
        }
    } catch (e) {
        console.error('Error zeroing legs:', e);
    }
}

async function fireSingleLeg(legId) {
    try {
        const res = await fetch('/api/fire-leg', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ leg_id: legId })
        });
        const data = await res.json();
        if (data.status === 'success') {
            console.log(`Leg ${legId} fired successfully:`, data);
            loadHistoryStats();
        } else {
            alert(`Could not fire Leg ${legId}: ${data.detail || data.error}`);
        }
    } catch (e) {
        alert(`Error firing Leg ${legId}: ${e.message}`);
    }
}

async function closeSingleLeg(legId) {
    try {
        const res = await fetch('/api/close-leg', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ leg_id: legId })
        });
        const data = await res.json();
        if (data.status === 'success') {
            console.log(`Leg ${legId} closed:`, data);
            loadHistoryStats();
        } else {
            alert(`Could not close Leg ${legId}: ${data.detail || data.error}`);
        }
    } catch (e) {
        alert(`Error closing Leg ${legId}: ${e.message}`);
    }
}

async function fireManualOrder() {
    // Fires next idle leg
    try {
        const res = await fetch('/api/fire-order', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            console.log("Fired order:", data);
            loadHistoryStats();
        } else {
            alert(`Fire Order error: ${data.detail || data.message}`);
        }
    } catch (e) {
        alert(`Error firing order: ${e.message}`);
    }
}

async function closeAllPositions() {
    try {
        const res = await fetch('/api/close-positions', { method: 'POST' });
        const data = await res.json();
        alert(data.message || 'Closed all positions.');
        loadHistoryStats();
    } catch (e) {
        alert(`Error closing positions: ${e.message}`);
    }
}

async function toggleAutoTrade() {
    autoTradeEnabled = !autoTradeEnabled;
    const btn = document.getElementById('btnAutoTrade');
    btn.textContent = autoTradeEnabled ? 'ON (AUTO)' : 'OFF';
    btn.className = `auto-trade-toggle ${autoTradeEnabled ? 'on' : ''}`;

    try {
        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                diff_to_trade: currentLegs[0]?.diff_to_trade || 4.0,
                diff_to_cut: currentLegs[0]?.diff_to_cut || 0.5,
                trade_size: currentLegs[0]?.trade_size || 0.1,
                auto_trade: autoTradeEnabled,
                target_profit_usd: currentLegs[0]?.target_profit_usd || 1.0
            })
        });
    } catch (e) {
        console.error('Error toggling auto-trade:', e);
    }
}

// ==========================================================================
// Currency & Contract Management
// ==========================================================================
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

        if (data.legs) {
            renderLegsGrid(data.legs);
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
                if (contractName === activeContract) opt.selected = true;
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
    activeContract = select.value;
    try {
        await fetch('/api/select-contract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ contract: activeContract })
        });
    } catch (e) {
        console.error('Error selecting contract:', e);
    }
}

async function switchExchangeSource(exchange) {
    activeExchange = exchange.toLowerCase();
    try {
        await fetch('/api/switch-exchange', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ exchange: activeExchange })
        });
        await loadAvailableContracts();
    } catch (e) {
        console.error('Error switching exchange:', e);
    }
}

// ==========================================================================
// WebSocket Real-Time Connection
// ==========================================================================
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    socket.onopen = () => {
        document.getElementById('wsDot').className = 'dot pulse-green';
        document.getElementById('wsStatus').textContent = 'Live Connected';
    };

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'INIT_HISTORY') {
            if (data.exchange_source) activeExchange = data.exchange_source.toLowerCase();
            if (data.active_currency) activeCurrency = data.active_currency;
            if (data.active_contract) activeContract = data.active_contract;
            if (data.legs && data.legs.length > 0) {
                const isUserTyping = document.activeElement && document.activeElement.tagName === 'INPUT';
                if (!isUserTyping) {
                    renderLegsGrid(data.legs);
                    saveLegsToLocalStorage(data.legs);
                }
            }
            if (data.auto_trade !== undefined) {
                autoTradeEnabled = data.auto_trade;
                const btn = document.getElementById('btnAutoTrade');
                if (btn) {
                    btn.textContent = autoTradeEnabled ? 'ON (AUTO)' : 'OFF';
                    btn.className = `auto-trade-toggle ${autoTradeEnabled ? 'on' : ''}`;
                }
            }
            if (data.data) {
                data.data.forEach(item => updateDashboard(item, false));
                chart.update();
            }
            return;
        }

        updateDashboard(data, true);
    };

    socket.onclose = () => {
        document.getElementById('wsDot').className = 'dot';
        document.getElementById('wsStatus').textContent = 'Disconnected (Reconnecting...)';
        setTimeout(() => {
            fetchInitialStatus(); // Re-sync active positions and inputs on reconnect
            connectWebSocket();
        }, 2000);
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

    // Update Header Tag
    const badgeText = (exSource === 'BYBIT' || exSource === 'BINANCE') ? (isDemo ? `${exSource} DEMO` : `${exSource} LIVE`) : exSource;
    const headerTag = document.getElementById('headerExchangeTag');
    if (headerTag) headerTag.textContent = `${contract} (${badgeText})`;
    const futBadge = document.getElementById('futuresBadge');
    if (futBadge) futBadge.textContent = `${badgeText} FUTURES`;
    const chartSub = document.getElementById('chartSubtext');
    if (chartSub) chartSub.textContent = `Comparing live tick updates from MT5 & ${contract} on ${badgeText}`;

    // Compute Spreads
    const mt5Spread = (mt5.ask && mt5.bid) ? Math.max(0, mt5.ask - mt5.bid) : (data.mt5_spread || 0);
    const deltaSpread = (delta.ask && delta.bid) ? Math.max(0, delta.ask - delta.bid) : (data.futures_spread || 0);

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
        const netExecGap = (delta.bid && mt5.ask) ? (delta.bid - mt5.ask) : spreadUsd;
        buySpotEl.textContent = `${netExecGap >= 0 ? '+' : ''}$${netExecGap.toFixed(2)}`;
    }

    // Update 5 Legs Matrix Live States
    if (data.legs) {
        updateLegLiveStates(data.legs);
    }

    // Update Active Positions Count & Table
    const activePositions = data.active_positions || [];
    const elActivePairsCount = document.getElementById('activePairsCount');
    if (elActivePairsCount) elActivePairsCount.textContent = `${activePositions.length} / 5 Legs`;
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
        signalDetail.textContent = data.action_detail || `Live gap ${spreadUsd >= 0 ? '+' : ''}$${spreadUsd.toFixed(2)} met leg threshold!`;
    } else {
        banner.className = 'signal-banner neutral';
        signalIcon.textContent = '⏳';
        signalTitle.textContent = 'MONITORING 5-LEG GAP LEVELS';
        signalDetail.textContent = `Live running gap for ${contract} is ${spreadUsd >= 0 ? '+' : ''}$${spreadUsd.toFixed(2)}. ${activePositions.length} leg(s) active.`;
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

        updateCandlesticks(data.timestamp, spreadUsd);

        if (redrawChart) {
            chart.update('none');
        }
    }
}

function updatePositionsTable(positions, currentGap = 0) {
    const tbody = document.getElementById('logsTableBody');
    document.getElementById('logCount').textContent = `${positions.length} active positions`;

    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">No open legs active... Set Open Gap & click Fire Leg!</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    positions.forEach(pos => {
        const row = document.createElement('tr');
        const entryGap = pos.entry_gap || 0;
        const size = pos.trade_size || 0.1;
        const capturedGap = entryGap >= 0 ? (entryGap - currentGap) : (currentGap - entryGap);
        const liveNetPnl = pos.live_net_pnl !== undefined ? pos.live_net_pnl : (capturedGap * size);
        const pnlClass = liveNetPnl >= 0 ? 'text-green' : 'text-red';
        const pnlSign = liveNetPnl >= 0 ? '+' : '';
        const legId = pos.leg_id || 1;

        row.innerHTML = `
            <td><span class="badge-blue">LEG ${legId}</span><br><span class="text-muted" style="font-size:10px;">${pos.id}</span></td>
            <td>${pos.open_time}</td>
            <td>$${pos.spot_price ? pos.spot_price.toFixed(2) : '-'}</td>
            <td>$${pos.fut_price ? pos.fut_price.toFixed(2) : '-'}</td>
            <td><span class="text-amber">${entryGap >= 0 ? '+' : ''}$${entryGap.toFixed(2)}</span></td>
            <td>
                <span class="${pnlClass}" style="font-weight: 700;">${pnlSign}$${liveNetPnl.toFixed(2)}</span><br>
                <span class="text-muted" style="font-size:10px;">Gap: ${pnlSign}$${capturedGap.toFixed(2)}</span>
            </td>
            <td><span class="badge-amber">${pos.status}</span></td>
            <td>
                <button class="btn-leg-cut" style="padding:4px 8px; font-size:10px;" onclick="closeSingleLeg(${legId})">Close Leg</button>
            </td>
        `;
        tbody.appendChild(row);
    });
}

// ==========================================================================
// VIEW 2: SQLITE TRADE HISTORY & TELEMETRY JSON
// ==========================================================================
async function loadHistoryStats() {
    try {
        const res = await fetch('/api/history-stats');
        const data = await res.json();
        if (data.status === 'success') {
            const stats = data.stats;
            const net = stats.total_net_pnl || 0.0;
            const pnlEl = document.getElementById('kpiNetPnl');
            if (pnlEl) {
                pnlEl.textContent = `${net >= 0 ? '+' : ''}$${net.toFixed(2)}`;
                pnlEl.className = `kpi-val ${net >= 0 ? 'text-green' : 'text-red'}`;
            }
            const winEl = document.getElementById('kpiWinRate');
            if (winEl) winEl.textContent = `${stats.win_rate_pct || 0.0}%`;
            const closedEl = document.getElementById('kpiClosedTrades');
            if (closedEl) closedEl.textContent = `${stats.closed_trades || 0} Trades`;
            const durEl = document.getElementById('kpiAvgDuration');
            if (durEl) durEl.textContent = `${stats.avg_duration_sec || 0}s`;

            const badge = document.getElementById('sidebarTradesCount');
            if (badge) badge.textContent = stats.total_trades || 0;
        }
    } catch (e) {
        console.error('Error loading history stats:', e);
    }
}

async function loadTradeHistory() {
    const leg = document.getElementById('histFilterLeg')?.value || '';
    const status = document.getElementById('histFilterStatus')?.value || '';
    const search = document.getElementById('histSearchInput')?.value.trim() || '';

    let url = `/api/history?limit=${histLimit}&offset=${histOffset}`;
    if (leg) url += `&leg_id=${leg}`;
    if (status) url += `&status=${status}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        histTotal = data.total || 0;

        renderHistoryTable(data.trades || []);
        updateHistoryPagination();
    } catch (e) {
        console.error('Error fetching trade history:', e);
    }
}

function renderHistoryTable(trades) {
    const tbody = document.getElementById('historyTableBody');
    if (!tbody) return;

    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No trade history recorded yet. Execute trades in Live Terminal to view here!</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    trades.forEach(t => {
        const row = document.createElement('tr');
        const netPnl = t.pnl_net || 0.0;
        const pnlClass = netPnl >= 0 ? 'text-green' : 'text-red';
        const pnlSign = netPnl >= 0 ? '+' : '';
        const openTime = t.open_time ? t.open_time.split('T')[1]?.substring(0, 8) || t.open_time : '-';
        const closeTime = t.close_time ? t.close_time.split('T')[1]?.substring(0, 8) || t.close_time : '-';

        row.innerHTML = `
            <td><strong>${t.trade_id}</strong></td>
            <td><span class="badge-blue">LEG ${t.leg_id}</span></td>
            <td><span class="badge-purple">${t.action_type || '-'}</span></td>
            <td>${t.trade_size}</td>
            <td>${openTime}</td>
            <td>${closeTime}</td>
            <td>${t.entry_gap ? `$${t.entry_gap.toFixed(2)}` : '-'}</td>
            <td>${t.exit_gap ? `$${t.exit_gap.toFixed(2)}` : '-'}</td>
            <td><span class="${pnlClass}" style="font-weight:700;">${pnlSign}$${netPnl.toFixed(2)}</span></td>
            <td>
                <span class="${t.status === 'CLOSED' ? 'badge-green' : 'badge-amber'}">${t.status}</span>
                <span class="text-muted" style="font-size:10px; display:block;">${t.exit_reason || ''}</span>
            </td>
            <td>
                <button class="btn-telemetry" onclick='openTelemetryModal(${JSON.stringify(t.trade_id)})'>🔍 View JSON</button>
            </td>
        `;
        tbody.appendChild(row);
    });
}

function updateHistoryPagination() {
    const pageNum = Math.floor(histOffset / histLimit) + 1;
    const totalPages = Math.max(1, Math.ceil(histTotal / histLimit));
    document.getElementById('histPageInfo').textContent = `Page ${pageNum} of ${totalPages} (${histTotal} records)`;
    document.getElementById('btnHistPrev').disabled = (histOffset === 0);
    document.getElementById('btnHistNext').disabled = (histOffset + histLimit >= histTotal);
}

function histPrevPage() {
    if (histOffset >= histLimit) {
        histOffset -= histLimit;
        loadTradeHistory();
    }
}

function histNextPage() {
    if (histOffset + histLimit < histTotal) {
        histOffset += histLimit;
        loadTradeHistory();
    }
}

function debounceHistorySearch() {
    clearTimeout(histDebounceTimer);
    histDebounceTimer = setTimeout(() => {
        histOffset = 0;
        loadTradeHistory();
    }, 300);
}

// ==========================================================================
// TELEMETRY JSON MODAL
// ==========================================================================
async function openTelemetryModal(tradeId) {
    try {
        const res = await fetch(`/api/history/${tradeId}`);
        const data = await res.json();
        if (data.status === 'success' && data.trade) {
            currentSelectedTradeJson = data.trade;
            document.getElementById('telemetryTradeId').textContent = `${data.trade.trade_id} (Leg ${data.trade.leg_id})`;
            document.getElementById('telemetryStatusBadge').textContent = data.trade.status;
            document.getElementById('telemetryCodeBlock').textContent = JSON.stringify(data.trade, null, 2);
            document.getElementById('telemetryModal').classList.remove('hidden');
        }
    } catch (e) {
        alert(`Error opening telemetry JSON: ${e.message}`);
    }
}

function closeTelemetryModal() {
    document.getElementById('telemetryModal').classList.add('hidden');
}

function copyTelemetryJson() {
    if (currentSelectedTradeJson) {
        navigator.clipboard.writeText(JSON.stringify(currentSelectedTradeJson, null, 2))
            .then(() => alert('📋 Telemetry JSON copied to clipboard!'))
            .catch(err => alert('Failed to copy: ' + err));
    }
}

// ==========================================================================
// VIEW 3: DAILY SYSTEM LOGS VIEWER
// ==========================================================================
async function loadLogDates() {
    try {
        const res = await fetch('/api/logs/dates');
        const data = await res.json();
        const select = document.getElementById('selectLogDate');
        if (select && data.dates) {
            select.innerHTML = '';
            data.dates.forEach(d => {
                const opt = document.createElement('option');
                opt.value = d;
                opt.textContent = `📅 ${d}.log`;
                select.appendChild(opt);
            });
            fetchDayLogs();
        }
    } catch (e) {
        console.error('Error loading log dates:', e);
    }
}

async function fetchDayLogs() {
    const date = document.getElementById('selectLogDate')?.value;
    const level = document.getElementById('selectLogLevel')?.value || 'ALL';
    const search = document.getElementById('logSearchInput')?.value.trim() || '';

    let url = `/api/logs?level=${level}&limit=400`;
    if (date) url += `&date=${date}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        renderLogTerminal(data.lines || []);
    } catch (e) {
        console.error('Error fetching logs:', e);
    }
}

function renderLogTerminal(lines) {
    const win = document.getElementById('terminalLogWindow');
    if (!win) return;

    if (!lines || lines.length === 0) {
        win.innerHTML = '<div class="terminal-loading">No matching log records found for this date.</div>';
        return;
    }

    win.innerHTML = '';
    // Show newest first or in order
    lines.forEach(line => {
        const div = document.createElement('div');
        let cls = 'log-line';
        if (line.includes('[ERROR') || line.includes('ERR:') || line.includes('rejected') || line.includes('failed')) {
            cls += ' error';
        } else if (line.includes('[WARN')) {
            cls += ' warning';
        } else if (line.includes('OPENED:') || line.includes('CLOSED:')) {
            cls += ' success';
        } else if (line.includes('[INFO')) {
            cls += ' info';
        }
        div.className = cls;
        div.textContent = line;
        win.appendChild(div);
    });
}

function debounceLogSearch() {
    clearTimeout(logDebounceTimer);
    logDebounceTimer = setTimeout(fetchDayLogs, 300);
}

function toggleLogAutoRefresh() {
    const isChecked = document.getElementById('chkAutoRefreshLogs')?.checked;
    clearInterval(logAutoRefreshTimer);
    if (isChecked) {
        logAutoRefreshTimer = setInterval(() => {
            if (currentView === 'logs') fetchDayLogs();
        }, 3000);
    }
}

// Credentials Modal Functions
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
            bybitInfo.innerHTML = '<span>💡 Binance Futures Testnet API Key from <a href="https://testnet.binancefuture.com" target="_blank" style="color: #f0b90b; text-decoration: underline; font-weight: 600;">testnet.binancefuture.com</a>.</span>';
        }
        if (lblKey) lblKey.textContent = 'Binance API Key (Testnet / Live)';
        if (lblSecret) lblSecret.textContent = 'Binance API Secret';
        if (inputKey) inputKey.placeholder = 'e.g. icTuw7Telt1SbjAK...';
        if (inputSecret) inputSecret.placeholder = 'e.g. LRBCamWl8CXumqzc...';
    } else if (exchange === 'bybit') {
        if (bybitInfo) {
            bybitInfo.style.display = 'block';
            bybitInfo.innerHTML = '<span>💡 Bybit Demo API Key from <a href="https://testnet.bybit.com/en/profile/api-management" target="_blank" style="color: #f7a600; text-decoration: underline; font-weight: 600;">testnet.bybit.com</a>.</span>';
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
    } else if (exchange === 'mt5') {
        if (bybitInfo) {
            bybitInfo.style.display = 'block';
            bybitInfo.innerHTML = '<span>💡 Specify exact <code>terminal64.exe</code> path when running multiple MT5 terminals on a VPS.</span>';
        }
        if (lblKey) lblKey.textContent = 'MT5 terminal64.exe Executable Path';
        if (inputKey) inputKey.placeholder = 'e.g. C:\\Program Files\\MetaTrader 5\\terminal64.exe';
        if (lblSecret && lblSecret.parentElement) lblSecret.parentElement.style.display = 'none';

        // Fetch saved MT5 path to prefill
        fetch('/api/config/mt5-path')
            .then(r => r.json())
            .then(data => {
                if (data && data.path && inputKey) {
                    inputKey.value = data.path;
                }
            })
            .catch(() => {});
    } else {
        if (lblSecret && lblSecret.parentElement) lblSecret.parentElement.style.display = 'block';
        if (bybitInfo) bybitInfo.style.display = 'none';
        if (lblKey) lblKey.textContent = 'Delta Exchange API Key';
        if (lblSecret) lblSecret.textContent = 'Delta Exchange API Secret';
        if (inputKey) inputKey.placeholder = 'e.g. delta_key_...';
        if (inputSecret) inputSecret.placeholder = 'e.g. delta_secret_...';
    }

    if (exchange !== 'mt5' && lblSecret && lblSecret.parentElement) {
        lblSecret.parentElement.style.display = 'block';
    }
}

async function saveCredentials() {
    const exchange = document.getElementById('inputExchangeType').value;
    const key = document.getElementById('inputApiKey').value.trim();
    const secret = document.getElementById('inputApiSecret').value.trim();
    const resBox = document.getElementById('credResult');

    if (exchange === 'mt5') {
        if (!key) {
            resBox.className = 'alert-box text-red';
            resBox.textContent = 'Please enter the full path to terminal64.exe';
            resBox.classList.remove('hidden');
            return;
        }

        try {
            resBox.className = 'alert-box text-blue';
            resBox.textContent = 'Connecting to custom MT5 terminal...';
            resBox.classList.remove('hidden');

            const response = await fetch('/api/config/mt5-path', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: key })
            });
            const res = await response.json();
            if (res.status === 'success' || res.is_connected) {
                resBox.className = 'alert-box text-green';
                resBox.textContent = `✅ ${res.message} (Path: ${res.path})`;
            } else {
                resBox.className = 'alert-box text-red';
                resBox.textContent = `⚠️ ${res.message}`;
            }
        } catch (err) {
            resBox.className = 'alert-box text-red';
            resBox.textContent = `Connection error: ${err.message}`;
        }
        return;
    }

    if (!key || !secret) {
        resBox.className = 'alert-box text-red';
        resBox.textContent = 'Please fill out both API Key/Client ID and Secret.';
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
                detail = ` | Verified Equity: $${parseFloat(res.account.total_equity).toLocaleString()} USDT`;
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
