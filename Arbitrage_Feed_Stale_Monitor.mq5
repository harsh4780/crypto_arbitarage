//+------------------------------------------------------------------+
//|                                  Arbitrage_Feed_Stale_Monitor.mq5|
//|                                  Copyright 2026, Antigravity AI  |
//|                                       https://www.mql5.com       |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026"
#property link      "https://www.mql5.com"
#property version   "1.00"
#property description "Arbitrage Feed Stale / Freeze Detector EA"
#property description "Monitors 2 Arbitrage Feeds and logs stale/freeze duration in milliseconds."

//--- Input Parameters
input string   InpSymbolA           = "EURUSD";      // Feed A Symbol (Local/Slow Feed)
input string   InpSymbolB           = "EURUSD_fast"; // Feed B Symbol (Fast/External Feed)
input int      InpTimerIntervalMs   = 100;           // Monitor Check Frequency (ms)
input int      InpLogIntervalMs     = 1000;          // Log Frequency Interval (ms)
input ulong    InpStaleThresholdMs  = 3000;          // Freeze Warning Threshold (ms) -> 3000 ms = 3 Seconds
input bool     InpOnlyLogWhenStale  = true;          // Log ONLY when feed freeze >= Threshold (3s)

//--- Global Variables
ulong    g_last_tick_time_a_msc = 0;   // Tick time_msc from broker for Feed A
ulong    g_last_tick_time_b_msc = 0;   // Tick time_msc from broker for Feed B

ulong    g_last_local_update_a  = 0;   // Local GetTickCount64() timestamp when Feed A last updated
ulong    g_last_local_update_b  = 0;   // Local GetTickCount64() timestamp when Feed B last updated

double   g_last_bid_a           = 0;
double   g_last_ask_a           = 0;
double   g_last_bid_b           = 0;
double   g_last_ask_b           = 0;

ulong    g_last_log_print_time  = 0;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   // 1. Ensure Feed A is selected in Market Watch
   if(!SymbolSelect(InpSymbolA, true))
     {
      PrintFormat("[ERROR] Symbol A '%s' could not be added to Market Watch!", InpSymbolA);
      return(INIT_FAILED);
     }

   // 2. Ensure Feed B is selected in Market Watch
   if(!SymbolSelect(InpSymbolB, true))
     {
      PrintFormat("[ERROR] Symbol B '%s' could not be added to Market Watch!", InpSymbolB);
      return(INIT_FAILED);
     }

   // 3. Initialize Timestamps
   ulong current_time_ms = GetTickCount64();
   g_last_local_update_a = current_time_ms;
   g_last_local_update_b = current_time_ms;
   g_last_log_print_time = current_time_ms;

   // Initial Tick Fetch
   MqlTick tick_a, tick_b;
   if(SymbolInfoTick(InpSymbolA, tick_a))
     {
      g_last_tick_time_a_msc = tick_a.time_msc;
      g_last_bid_a = tick_a.bid;
      g_last_ask_a = tick_a.ask;
     }
   if(SymbolInfoTick(InpSymbolB, tick_b))
     {
      g_last_tick_time_b_msc = tick_b.time_msc;
      g_last_bid_b = tick_b.bid;
      g_last_ask_b = tick_b.ask;
     }

   // 4. Set High-Frequency Millisecond Timer
   if(!EventSetMillisecondTimer(InpTimerIntervalMs))
     {
      Print("[ERROR] Failed to set millisecond timer!");
      return(INIT_FAILED);
     }

   PrintFormat("[INIT] Arbitrage Feed Monitor Started | Feed A: '%s' | Feed B: '%s' | Check Timer: %d ms",
               InpSymbolA, InpSymbolB, InpTimerIntervalMs);

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   // Kill Timer & Clear Chart Comment
   EventKillTimer();
   Comment("");
   Print("[DEINIT] Arbitrage Feed Monitor Stopped.");
  }

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
  {
   // Process feed check on tick event
   CheckFeeds();
  }

//+------------------------------------------------------------------+
//| Timer function                                                   |
//+------------------------------------------------------------------+
void OnTimer()
  {
   // Process feed check on high-frequency timer event
   CheckFeeds();
  }

//+------------------------------------------------------------------+
//| Core Function: Check Feeds and Calculate Freeze Durations        |
//+------------------------------------------------------------------+
void CheckFeeds()
  {
   ulong now_ms = GetTickCount64();
   
   MqlTick tick_a, tick_b;
   bool has_a = SymbolInfoTick(InpSymbolA, tick_a);
   bool has_b = SymbolInfoTick(InpSymbolB, tick_b);

   // --- Check Feed A Updates ---
   if(has_a)
     {
      if(tick_a.time_msc != g_last_tick_time_a_msc || tick_a.bid != g_last_bid_a || tick_a.ask != g_last_ask_a)
        {
         g_last_tick_time_a_msc = tick_a.time_msc;
         g_last_bid_a = tick_a.bid;
         g_last_ask_a = tick_a.ask;
         g_last_local_update_a = now_ms;
        }
     }

   // --- Check Feed B Updates ---
   if(has_b)
     {
      if(tick_b.time_msc != g_last_tick_time_b_msc || tick_b.bid != g_last_bid_b || tick_b.ask != g_last_ask_b)
        {
         g_last_tick_time_b_msc = tick_b.time_msc;
         g_last_bid_b = tick_b.bid;
         g_last_ask_b = tick_b.ask;
         g_last_local_update_b = now_ms;
        }
     }

   // --- Calculate Elapsed Stale / Freeze Duration (in milliseconds & seconds) ---
   ulong freeze_time_a_ms = (now_ms >= g_last_local_update_a) ? (now_ms - g_last_local_update_a) : 0;
   ulong freeze_time_b_ms = (now_ms >= g_last_local_update_b) ? (now_ms - g_last_local_update_b) : 0;

   double freeze_time_a_sec = freeze_time_a_ms / 1000.0;
   double freeze_time_b_sec = freeze_time_b_ms / 1000.0;

   bool is_a_stale = (freeze_time_a_ms >= InpStaleThresholdMs);
   bool is_b_stale = (freeze_time_b_ms >= InpStaleThresholdMs);

   // --- Print structured log to Experts Log at specified log interval ---
   if(now_ms - g_last_log_print_time >= (ulong)InpLogIntervalMs)
     {
      // If InpOnlyLogWhenStale is true, log ONLY when at least one feed is stale (>= 3 sec)
      if(!InpOnlyLogWhenStale || is_a_stale || is_b_stale)
        {
         g_last_log_print_time = now_ms;

         string status_a = is_a_stale ? "⚠️[FREEZE/STALE]" : "[OK]";
         string status_b = is_b_stale ? "⚠️[FREEZE/STALE]" : "[OK]";

         PrintFormat("FEED LOG | %s %s: Freeze=%llu ms (%.2fs) | Bid:%.5f Ask:%.5f || %s %s: Freeze=%llu ms (%.2fs) | Bid:%.5f Ask:%.5f",
                     InpSymbolA, status_a, freeze_time_a_ms, freeze_time_a_sec, tick_a.bid, tick_a.ask,
                     InpSymbolB, status_b, freeze_time_b_ms, freeze_time_b_sec, tick_b.bid, tick_b.ask);
        }
     }

   // --- Display Live Visual Status on Chart Screen ---
   string chart_msg = "========================================================\n" +
                      "            ARBITRAGE FEED STALE / FREEZE MONITOR        \n" +
                      "========================================================\n" +
                      StringFormat("Feed A [%s]:\n", InpSymbolA) +
                      StringFormat("  - Freeze Time : %llu ms (%.2f sec) %s\n", freeze_time_a_ms, freeze_time_a_sec, is_a_stale ? "⚠️ STALE!" : "✅ OK") +
                      StringFormat("  - Last Bid/Ask: %.5f / %.5f\n\n", tick_a.bid, tick_a.ask) +
                      StringFormat("Feed B [%s]:\n", InpSymbolB) +
                      StringFormat("  - Freeze Time : %llu ms (%.2f sec) %s\n", freeze_time_b_ms, freeze_time_b_sec, is_b_stale ? "⚠️ STALE!" : "✅ OK") +
                      StringFormat("  - Last Bid/Ask: %.5f / %.5f\n", tick_b.bid, tick_b.ask) +
                      "========================================================\n" +
                      StringFormat("Stale Alert Threshold: %llu ms | Timer: %d ms", InpStaleThresholdMs, InpTimerIntervalMs);

   Comment(chart_msg);
  }
//+------------------------------------------------------------------+
