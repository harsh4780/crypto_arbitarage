//+------------------------------------------------------------------+
//|                                          HRX_2132639732_v5.mq5  |
//|                                  Copyright 2025, MetaQuotes Ltd. |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024, MQL Development"
#property link      "https://www.mqldevelopment.com/"
#property version   "9.00"
#property description "HRX Arbitrage EA v5"

#include <Trade\Trade.mqh>
CTrade trade;

#define orderNum      100
#define BTN_CLOSE_ALL "btn_close_all"

// SECURITY SETTINGS
#define ALLOWED_ACCOUNT {{ account_id }}
#define EXPIRY_DATE D'{{ expiry_date }}'

struct orderDetails
  {
   ulong    tckts[];
   int      lvl;
   datetime openTime;
            orderDetails() { ArrayResize(tckts,0); lvl=-1; openTime=0; }
  };
orderDetails od_1[orderNum];

enum frstsecn { Sym1, Sym2 };
enum tradetyp { buy, sell   };

// ─────────────────────────────────────────────
// Trade Trigger Settings
// ─────────────────────────────────────────────
input string  settings1         = " ------------- Set 0 ------------- ";//_
input int     numberOfPairs     = 0;    // Set_0  | Number Of Pairs
input double  diffToTrade       = 0;    // Set_0  | Difference To Trade
input double  diffToCut         = 0;    // Set_0  | Difference To Cut

input string  settings_1        = " ------------- Set 1 ------------- ";//_
input int     numberOfPairs1    = 0;    // Set_1  | Number Of Pairs
input double  diffToTrade1      = 0;    // Set_1  | Difference To Trade
input double  diffToCut1        = 0;    // Set_1  | Difference To Cut

input string  settings_2        = " ------------- Set 2 ------------- ";//_
input int     numberOfPairs2    = 0;    // Set_2  | Number Of Pairs
input double  diffToTrade2      = 0;    // Set_2  | Difference To Trade
input double  diffToCut2        = 0;    // Set_2  | Difference To Cut

input string  settings_3        = " ------------- Set 3 ------------- ";//_
input int     numberOfPairs3    = 0;    // Set_3  | Number Of Pairs
input double  diffToTrade3      = 0;    // Set_3  | Difference To Trade
input double  diffToCut3        = 0;    // Set_3  | Difference To Cut

input string  settings_4        = " ------------- Set 4 ------------- ";//_
input int     numberOfPairs4    = 0;    // Set_4  | Number Of Pairs
input double  diffToTrade4      = 0;    // Set_4  | Difference To Trade
input double  diffToCut4        = 0;    // Set_4  | Difference To Cut

input string  settings_5        = " ------------- Set 5 ------------- ";//_
input int     numberOfPairs5    = 0;    // Set_5  | Number Of Pairs
input double  diffToTrade5      = 0;    // Set_5  | Difference To Trade
input double  diffToCut5        = 0;    // Set_5  | Difference To Cut

input string  settings_6        = " ------------- Set 6 ------------- ";//_
input int     numberOfPairs6    = 0;    // Set_6  | Number Of Pairs
input double  diffToTrade6      = 0;    // Set_6  | Difference To Trade
input double  diffToCut6        = 0;    // Set_6  | Difference To Cut

input string  settings_7        = " ------------- Set 7 ------------- ";//_
input int     numberOfPairs7    = 0;    // Set_7  | Number Of Pairs
input double  diffToTrade7      = 0;    // Set_7  | Difference To Trade
input double  diffToCut7        = 0;    // Set_7  | Difference To Cut

input string  settings_8        = " ------------- Set 8 ------------- ";//_
input int     numberOfPairs8    = 0;    // Set_8  | Number Of Pairs
input double  diffToTrade8      = 0;    // Set_8  | Difference To Trade
input double  diffToCut8        = 0;    // Set_8  | Difference To Cut

input string  settings_9        = " ------------- Set 9 ------------- ";//_
input int     numberOfPairs9    = 0;    // Set_9  | Number Of Pairs
input double  diffToTrade9      = 0;    // Set_9  | Difference To Trade
input double  diffToCut9        = 0;    // Set_9  | Difference To Cut

input string  settings_10       = " ------------- Set 10 ------------- ";//_
input int     numberOfPairs10   = 0;    // Set_10 | Number Of Pairs
input double  diffToTrade10     = 0;    // Set_10 | Difference To Trade
input double  diffToCut10       = 0;    // Set_10 | Difference To Cut

// ─────────────────────────────────────────────
// General Settings
// ─────────────────────────────────────────────
input string   settings         = " ------------- General Settings ------------- ";//_
input int      magic_no         = 123;               // Magic No
input tradetyp tradeType        = buy;               // Open Direction
input string   symbol1          = "XAUUSD.ins";      // Symbol 1
input string   symbol2          = "GOLD.APR2026.r";  // Symbol 2
input double   initialLot       = 0.1;               // Initial Lot
input frstsecn SymbolToTrade    = Sym1;              // Symbol To Trade First
input frstsecn SymbolToClose    = Sym1;              // Symbol To Close First
input double   stopLoss         = 0;                 // StopLoss Pips
input double   takeProfit       = 0;                 // TakeProfit Pips
input ulong    slippageAllowed  = 50;                // Slippage Points
input bool     tradeOnSameLevel = false;             // Enable Trade On Same Level
input int      minHoldMinutes   = 0;                 // Min Hold Minutes (0=off)
input double   openOvershoot    = 1.0;               // Open Overshoot (all sets)
input double   cutOvershoot     = 0.20;              // Close Overshoot (all sets)

// ─────────────────────────────────────────────
// Global Variables
// ─────────────────────────────────────────────
int    totalPairs             = 0;
int    reason_deint           = -1;
ulong  g_last_tick_time       = 0;
int    g_process_interval     = 50;

bool   g_condition_prev[11]   = {false};
double g_actual_open_gap[11]  = {0};
bool   g_level_filled[11]     = {false};
bool   g_level_closing[11]    = {false};

// Permanent lock per level
// Reset ONLY when: diffToTrade changes (OnInit reason=5)
// Close All / Cut / Fail: LOCKS — does NOT unlock automatically
bool   g_level_locked[11]     = {false};

bool   g_trade_in_progress    = false;
ulong  g_trade_lock_time      = 0;
#define TRADE_LOCK_TIMEOUT_MS  3000

double g_prev_diffToTrade[11] = {0};

//+------------------------------------------------------------------+
//| Auto Filling Mode — per symbol                                    |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetFillingMode(string symbol)
  {
   uint filling = (uint)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
  }

//+------------------------------------------------------------------+
//| License Check                                                     |
//+------------------------------------------------------------------+
bool CheckLicense()
  {
   long allowed_acc = ALLOWED_ACCOUNT;
   if(allowed_acc != 0 && AccountInfoInteger(ACCOUNT_LOGIN) != allowed_acc)
     { Alert("ERROR: Licensed for account ", allowed_acc, " only!"); return false; }
   if(TimeCurrent() > EXPIRY_DATE)
     { Alert("ERROR: License expired!"); return false; }
   return true;
  }

//+------------------------------------------------------------------+
//| Dashboard                                                         |
//+------------------------------------------------------------------+
void CreateDashboard()
  {
   if(ObjectFind(0, BTN_CLOSE_ALL) < 0)
     {
      ObjectCreate(0, BTN_CLOSE_ALL, OBJ_BUTTON, 0, 0, 0);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_XDISTANCE,    10);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_YDISTANCE,    10);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_XSIZE,        120);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_YSIZE,        30);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_CORNER,       CORNER_LEFT_UPPER);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_COLOR,        clrWhite);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_BGCOLOR,      clrCrimson);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_BORDER_COLOR, clrDarkRed);
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_FONTSIZE,     9);
      ObjectSetString (0, BTN_CLOSE_ALL, OBJPROP_TEXT,         "CLOSE ALL");
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_SELECTABLE,   false);
      ChartRedraw(0);
     }
  }

void RemoveDashboard()
  {
   ObjectDelete(0, BTN_CLOSE_ALL);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| OnChartEvent                                                      |
//| Close All → LOCK all levels (does NOT unlock)                    |
//| To re-enable: change diffToTrade value                           |
//+------------------------------------------------------------------+
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id == CHARTEVENT_OBJECT_CLICK && sparam == BTN_CLOSE_ALL)
     {
      Print(">>> CLOSE ALL BUTTON CLICKED");
      g_trade_in_progress = false;
      closeAllTrades();
      for(int i = 0; i < 11; i++)
        {
         g_level_filled[i]    = false;
         g_condition_prev[i]  = true;
         g_actual_open_gap[i] = 0;
         g_level_locked[i]    = true;
        }
      Print(">>> ALL LEVELS LOCKED — change diffToTrade to re-enable any set");
      ObjectSetInteger(0, BTN_CLOSE_ALL, OBJPROP_STATE, false);
      ChartRedraw(0);
     }
  }

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(!CheckLicense()) return(INIT_FAILED);

   g_trade_in_progress = false;
   g_trade_lock_time   = 0;

   totalPairs = 0;
   if(diffToTrade   != 0) totalPairs++;  if(diffToTrade1  != 0) totalPairs++;
   if(diffToTrade2  != 0) totalPairs++;  if(diffToTrade3  != 0) totalPairs++;
   if(diffToTrade4  != 0) totalPairs++;  if(diffToTrade5  != 0) totalPairs++;
   if(diffToTrade6  != 0) totalPairs++;  if(diffToTrade7  != 0) totalPairs++;
   if(diffToTrade8  != 0) totalPairs++;  if(diffToTrade9  != 0) totalPairs++;
   if(diffToTrade10 != 0) totalPairs++;

   double nd[11] = {diffToTrade,diffToTrade1,diffToTrade2,diffToTrade3,diffToTrade4,
                    diffToTrade5,diffToTrade6,diffToTrade7,diffToTrade8,diffToTrade9,diffToTrade10};

   if(reason_deint == 5)
     {
      // Parameters changed — unlock only changed sets
      clear_closed_tickets();
      for(int i = 0; i < 11; i++)
        {
         if(nd[i] != g_prev_diffToTrade[i])
           {
            g_level_filled[i]    = false;
            g_condition_prev[i]  = false;
            g_actual_open_gap[i] = 0;
            g_level_locked[i]    = false;
            for(int j = 0; j < orderNum; j++)
               if(od_1[j].lvl == i)
                 { ArrayResize(od_1[j].tckts,0); od_1[j].lvl=-1; od_1[j].openTime=0; }
            Print(">>> Set_",i," UNLOCKED: ",g_prev_diffToTrade[i]," -> ",nd[i]);
           }
         g_prev_diffToTrade[i] = nd[i];
        }
     }
   else
     {
      // Fresh attach — all unlocked
      int days = int((EXPIRY_DATE - TimeCurrent()) / 86400);
      Print("===========================================");
      Print("HRX EA v5");
      Print("EA Licensed to Account : ", ALLOWED_ACCOUNT);
      Print("License Valid Until    : ", TimeToString(EXPIRY_DATE, TIME_DATE|TIME_MINUTES));
      Print("Days Remaining         : ", days);
      Print("Open Overshoot         : ", openOvershoot);
      Print("Close Overshoot        : ", cutOvershoot);
      Print("Sym1 Filling Mode      : ", EnumToString(GetFillingMode(symbol1)));
      Print("Sym2 Filling Mode      : ", EnumToString(GetFillingMode(symbol2)));
      Print("===========================================");

      for(int i = 0; i < 11; i++)
        {
         g_level_filled[i]    = false;
         g_condition_prev[i]  = false;
         g_actual_open_gap[i] = 0;
         g_level_closing[i]   = false;
         g_level_locked[i]    = false;
        }

      for(int i = 0; i < 11; i++) g_prev_diffToTrade[i] = nd[i];
     }

   trade.SetExpertMagicNumber(magic_no);
   trade.SetDeviationInPoints(slippageAllowed);
   trade.SetTypeFilling(GetFillingMode(symbol1));
   trade.SetAsyncMode(false);
   trade.LogLevel(LOG_LEVEL_ERRORS);
   recoverStructure(); // Recover any open positions into structure
   CreateDashboard();
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   reason_deint = reason;
   Print("Reason deInIt: ", reason_deint);
   RemoveDashboard();
  }

//+------------------------------------------------------------------+
//| Trade Lock Check                                                  |
//+------------------------------------------------------------------+
bool isTradeLocked()
  {
   if(!g_trade_in_progress) return false;
   ulong now = GetTickCount();
   if(now - g_trade_lock_time > TRADE_LOCK_TIMEOUT_MS)
     { Print(">>> TRADE LOCK TIMEOUT — Force unlocking"); g_trade_in_progress = false; return false; }
   return true;
  }

//+------------------------------------------------------------------+
//| Helpers                                                           |
//+------------------------------------------------------------------+
double getTotalPnL()
  {
   double total = 0;
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t) && PositionGetInteger(POSITION_MAGIC) == magic_no)
         total += PositionGetDouble(POSITION_PROFIT);
     }
   return total;
  }

double getExpectedProfit()
  {
   double cp[11] = {diffToCut,diffToCut1,diffToCut2,diffToCut3,diffToCut4,
                    diffToCut5,diffToCut6,diffToCut7,diffToCut8,diffToCut9,diffToCut10};
   double exp = 0;
   for(int i = 0; i < 11; i++)
     {
      int ap = get_level_trades_pair(i);
      if(ap > 0 && g_actual_open_gap[i] > 0 && cp[i] > 0)
        {
         double cutLevel = cp[i] - cutOvershoot;
         double capture  = g_actual_open_gap[i] - cutLevel;
         if(capture > 0) exp += capture * ap * initialLot * 100;
        }
     }
   return NormalizeDouble(exp, 2);
  }

bool isHoldTimeComplete(int level)
  {
   if(minHoldMinutes <= 0) return true;
   for(int i = 0; i < orderNum; i++)
     {
      if(od_1[i].lvl == level && od_1[i].openTime > 0)
        {
         int mins = (int)((TimeCurrent() - od_1[i].openTime) / 60);
         if(mins < minHoldMinutes) return false;
         return true;
        }
     }
   return true;
  }

string getHoldStatus(int level)
  {
   if(minHoldMinutes <= 0) return "";
   for(int i = 0; i < orderNum; i++)
     {
      if(od_1[i].lvl == level && od_1[i].openTime > 0)
        {
         int mins = (int)((TimeCurrent() - od_1[i].openTime) / 60);
         if(mins < minHoldMinutes)
            return " [HOLD "+string(mins)+"/"+string(minHoldMinutes)+"min]";
        }
     }
   return "";
  }

bool isAllHoldComplete()
  {
   if(minHoldMinutes <= 0) return true;
   for(int i = 0; i < 11; i++)
      if(get_level_trades_pair(i) > 0 && !isHoldTimeComplete(i)) return false;
   return true;
  }

//+------------------------------------------------------------------+
//| Orphan Check                                                      |
//| Agar 1 leg open aur 1 closed → orphan close karo                 |
//+------------------------------------------------------------------+
void checkOrphanTrades()
  {
   if(g_trade_in_progress) return;
   for(int i = 0; i < orderNum; i++)
     {
      if(od_1[i].lvl == -1) continue;
      int level = od_1[i].lvl;
      if(g_level_closing[level]) continue;
      int total = ArraySize(od_1[i].tckts);
      if(total == 0 || getActiveOrdersCount(od_1[i].tckts) == 0) continue;

      for(int j = 0; j < total - 1; j += 2)
        {
         ulong t1 = od_1[i].tckts[j];
         ulong t2 = od_1[i].tckts[j+1];
         bool  a  = PositionSelectByTicket(t1);
         bool  b  = PositionSelectByTicket(t2);
         if(a && b)   continue;
         if(!a && !b) continue;
         if(a && !b) { Print(">>> ORPHAN | Closing t1=",t1); trade.PositionClose(t1); }
         else        { Print(">>> ORPHAN | Closing t2=",t2); trade.PositionClose(t2); }
         g_level_filled[level]    = false;
         g_actual_open_gap[level] = 0;
        }
     }
  }

//+------------------------------------------------------------------+
//| OnTick                                                            |
//+------------------------------------------------------------------+
void OnTick()
  {
   if(!CheckLicense()) { Alert("LICENSE VIOLATION! EA Stopped."); ExpertRemove(); return; }

   ulong now = GetTickCount();
   if(now - g_last_tick_time < (ulong)g_process_interval) return;
   g_last_tick_time = now;

   if(isTradeLocked()) { Print(">>> TRADE IN PROGRESS — waiting..."); return; }

   // Har tick check — agar level locked hai lekin positions hain toh recover karo
   recoverStructure();
   checkOrphanTrades();

   double dob = NormalizeDouble(SymbolInfoDouble(symbol1,SYMBOL_ASK) - SymbolInfoDouble(symbol2,SYMBOL_BID), 2);
   double dos = NormalizeDouble(SymbolInfoDouble(symbol1,SYMBOL_BID) - SymbolInfoDouble(symbol2,SYMBOL_ASK), 2);
   double dcb = NormalizeDouble(SymbolInfoDouble(symbol1,SYMBOL_BID) - SymbolInfoDouble(symbol2,SYMBOL_ASK), 2);
   double dcs = NormalizeDouble(SymbolInfoDouble(symbol1,SYMBOL_ASK) - SymbolInfoDouble(symbol2,SYMBOL_BID), 2);

   double openGap  = (tradeType == buy) ? dob : dos;
   double closeGap = (tradeType == buy) ? dcb : dcs;

   if(tradeType == buy)
     {
      Print(" Difference_Open  : ",openGap ," , ",symbol1,"  ",SymbolInfoDouble(symbol1,SYMBOL_ASK)," , ",symbol2,"  ",SymbolInfoDouble(symbol2,SYMBOL_BID));
      Print(" Difference_Close : ",closeGap," , ",symbol1,"  ",SymbolInfoDouble(symbol1,SYMBOL_BID)," , ",symbol2,"  ",SymbolInfoDouble(symbol2,SYMBOL_ASK));
     }
   else
     {
      Print(" Difference_Open  : ",openGap ," , ",symbol1,"  ",SymbolInfoDouble(symbol1,SYMBOL_BID)," , ",symbol2,"  ",SymbolInfoDouble(symbol2,SYMBOL_ASK));
      Print(" Difference_Close : ",closeGap," , ",symbol1,"  ",SymbolInfoDouble(symbol1,SYMBOL_ASK)," , ",symbol2,"  ",SymbolInfoDouble(symbol2,SYMBOL_BID));
     }

   // Status: CLOSE > OPEN (by actual positions) > PENDING (if value set) > blank
   Print(" Set_0  : "+string(get_level_trades_pair(0)) +"/"+string(numberOfPairs) +" | Open:"+string(diffToTrade) +" Cut:"+string(diffToCut) +(g_level_locked[0]?" [CLOSE]":(get_level_trades_pair(0)>0?" [OPEN]":(diffToTrade!=0?" [PENDING]":"")))  +getHoldStatus(0));
   Print(" Set_1  : "+string(get_level_trades_pair(1)) +"/"+string(numberOfPairs1)+" | Open:"+string(diffToTrade1)+" Cut:"+string(diffToCut1)+(g_level_locked[1]?" [CLOSE]":(get_level_trades_pair(1)>0?" [OPEN]":(diffToTrade1!=0?" [PENDING]":"")))  +getHoldStatus(1));
   Print(" Set_2  : "+string(get_level_trades_pair(2)) +"/"+string(numberOfPairs2)+" | Open:"+string(diffToTrade2)+" Cut:"+string(diffToCut2)+(g_level_locked[2]?" [CLOSE]":(get_level_trades_pair(2)>0?" [OPEN]":(diffToTrade2!=0?" [PENDING]":"")))  +getHoldStatus(2));
   Print(" Set_3  : "+string(get_level_trades_pair(3)) +"/"+string(numberOfPairs3)+" | Open:"+string(diffToTrade3)+" Cut:"+string(diffToCut3)+(g_level_locked[3]?" [CLOSE]":(get_level_trades_pair(3)>0?" [OPEN]":(diffToTrade3!=0?" [PENDING]":"")))  +getHoldStatus(3));
   Print(" Set_4  : "+string(get_level_trades_pair(4)) +"/"+string(numberOfPairs4)+" | Open:"+string(diffToTrade4)+" Cut:"+string(diffToCut4)+(g_level_locked[4]?" [CLOSE]":(get_level_trades_pair(4)>0?" [OPEN]":(diffToTrade4!=0?" [PENDING]":"")))  +getHoldStatus(4));
   Print(" Set_5  : "+string(get_level_trades_pair(5)) +"/"+string(numberOfPairs5)+" | Open:"+string(diffToTrade5)+" Cut:"+string(diffToCut5)+(g_level_locked[5]?" [CLOSE]":(get_level_trades_pair(5)>0?" [OPEN]":(diffToTrade5!=0?" [PENDING]":"")))  +getHoldStatus(5));
   Print(" Set_6  : "+string(get_level_trades_pair(6)) +"/"+string(numberOfPairs6)+" | Open:"+string(diffToTrade6)+" Cut:"+string(diffToCut6)+(g_level_locked[6]?" [CLOSE]":(get_level_trades_pair(6)>0?" [OPEN]":(diffToTrade6!=0?" [PENDING]":"")))  +getHoldStatus(6));
   Print(" Set_7  : "+string(get_level_trades_pair(7)) +"/"+string(numberOfPairs7)+" | Open:"+string(diffToTrade7)+" Cut:"+string(diffToCut7)+(g_level_locked[7]?" [CLOSE]":(get_level_trades_pair(7)>0?" [OPEN]":(diffToTrade7!=0?" [PENDING]":"")))  +getHoldStatus(7));
   Print(" Set_8  : "+string(get_level_trades_pair(8)) +"/"+string(numberOfPairs8)+" | Open:"+string(diffToTrade8)+" Cut:"+string(diffToCut8)+(g_level_locked[8]?" [CLOSE]":(get_level_trades_pair(8)>0?" [OPEN]":(diffToTrade8!=0?" [PENDING]":"")))  +getHoldStatus(8));
   Print(" Set_9  : "+string(get_level_trades_pair(9)) +"/"+string(numberOfPairs9)+" | Open:"+string(diffToTrade9)+" Cut:"+string(diffToCut9)+(g_level_locked[9]?" [CLOSE]":(get_level_trades_pair(9)>0?" [OPEN]":(diffToTrade9!=0?" [PENDING]":"")))  +getHoldStatus(9));
   Print(" Set_10 : "+string(get_level_trades_pair(10))+"/"+string(numberOfPairs10)+" | Open:"+string(diffToTrade10)+" Cut:"+string(diffToCut10)+(g_level_locked[10]?" [CLOSE]":(get_level_trades_pair(10)>0?" [OPEN]":(diffToTrade10!=0?" [PENDING]":"")))  +getHoldStatus(10));

   double ep = getExpectedProfit(), pnl = getTotalPnL();
   if(ep > 0 && pnl >= ep && isAllHoldComplete())
     {
      Print(">>> PROFIT TARGET HIT | PnL $",NormalizeDouble(pnl,2)," >= $",ep);
      closeAllTrades();
      for(int i=0;i<11;i++)
        {
         g_level_filled[i]    = false;
         g_condition_prev[i]  = true;
         g_actual_open_gap[i] = 0;
         g_level_locked[i]    = true;
        }
      Print(">>> ALL LEVELS LOCKED after profit — change diffToTrade to re-enable");
      return;
     }

   // Place trades
   if(diffToTrade   != 0 && placeTrade(numberOfPairs,  diffToTrade,   0,  dob, dos)) return;
   if(diffToTrade1  != 0 && placeTrade(numberOfPairs1, diffToTrade1,  1,  dob, dos)) return;
   if(diffToTrade2  != 0 && placeTrade(numberOfPairs2, diffToTrade2,  2,  dob, dos)) return;
   if(diffToTrade3  != 0 && placeTrade(numberOfPairs3, diffToTrade3,  3,  dob, dos)) return;
   if(diffToTrade4  != 0 && placeTrade(numberOfPairs4, diffToTrade4,  4,  dob, dos)) return;
   if(diffToTrade5  != 0 && placeTrade(numberOfPairs5, diffToTrade5,  5,  dob, dos)) return;
   if(diffToTrade6  != 0 && placeTrade(numberOfPairs6, diffToTrade6,  6,  dob, dos)) return;
   if(diffToTrade7  != 0 && placeTrade(numberOfPairs7, diffToTrade7,  7,  dob, dos)) return;
   if(diffToTrade8  != 0 && placeTrade(numberOfPairs8, diffToTrade8,  8,  dob, dos)) return;
   if(diffToTrade9  != 0 && placeTrade(numberOfPairs9, diffToTrade9,  9,  dob, dos)) return;
   if(diffToTrade10 != 0 && placeTrade(numberOfPairs10,diffToTrade10, 10, dob, dos)) return;

   // Close trades
   if(diffToCut   != 0) closeTrade(diffToCut,   0,  closeGap);
   if(diffToCut1  != 0) closeTrade(diffToCut1,  1,  closeGap);
   if(diffToCut2  != 0) closeTrade(diffToCut2,  2,  closeGap);
   if(diffToCut3  != 0) closeTrade(diffToCut3,  3,  closeGap);
   if(diffToCut4  != 0) closeTrade(diffToCut4,  4,  closeGap);
   if(diffToCut5  != 0) closeTrade(diffToCut5,  5,  closeGap);
   if(diffToCut6  != 0) closeTrade(diffToCut6,  6,  closeGap);
   if(diffToCut7  != 0) closeTrade(diffToCut7,  7,  closeGap);
   if(diffToCut8  != 0) closeTrade(diffToCut8,  8,  closeGap);
   if(diffToCut9  != 0) closeTrade(diffToCut9,  9,  closeGap);
   if(diffToCut10 != 0) closeTrade(diffToCut10, 10, closeGap);
  }

//+------------------------------------------------------------------+
//| closeTrade                                                        |
//+------------------------------------------------------------------+
void closeTrade(double cutTrade, int lvl, double closeGap)
  {
   if(!isHoldTimeComplete(lvl)) return;
   if(closeGap < cutTrade - cutOvershoot)
     {
      Print(">>> CUT Set_",lvl," | closeGap=",closeGap," < ",(cutTrade-cutOvershoot));
      string s1 = symbol1, s2 = symbol2;
      if(SymbolToClose == Sym2) { s1 = symbol2; s2 = symbol1; }
      close_trade_place_on_that_level(lvl, s1, s2);
     }
  }

//+------------------------------------------------------------------+
//| placeTrade                                                        |
//|                                                                   |
//| v4:                                                               |
//| - Async open: dono legs ek saath broker ko jaati hain            |
//| - Orphan checker handles single-leg situations                    |
//| - condition_prev: same-tick double fire rokta hai                 |
//| - After fill: agar aur pairs baaki hain toh reset for next pair  |
//+------------------------------------------------------------------+
bool placeTrade(int numberOfPair, double difftoTrade, int level, double dob, double dos)
  {
   double difference_open = (tradeType == buy) ? dob : dos;

   // [1] Permanent lock check
   if(g_level_locked[level]) return false;

   // [2] Filled check
   if(g_level_filled[level])
     {
      if(get_level_trades_pair(level) == 0)
        {
         g_level_filled[level]    = false;
         g_condition_prev[level]  = false;
         g_actual_open_gap[level] = 0;
         Print(">>> Set_",level," RESET — ready for next crossing");
        }
      else return false;
     }

   // [3] Overshoot condition
   bool conditionMet = (tradeType == buy)
                       ? (difference_open < difftoTrade - openOvershoot)
                       : (difference_open > difftoTrade + openOvershoot);

   if(!conditionMet) return false;

   // [4] Same-tick double fire guard
   if(g_condition_prev[level]) return false;

   g_condition_prev[level] = true;

   if(get_level_trades_pair(level) >= numberOfPair) return false;

   double trigger = (tradeType==buy) ? difftoTrade-openOvershoot : difftoTrade+openOvershoot;

   // FIX v5: Price re-check karo — sirf tab open karo jab price abhi bhi favorable ho
   double current_gap = (tradeType == buy)
                        ? NormalizeDouble(SymbolInfoDouble(symbol1,SYMBOL_ASK) - SymbolInfoDouble(symbol2,SYMBOL_BID), 2)
                        : NormalizeDouble(SymbolInfoDouble(symbol1,SYMBOL_BID) - SymbolInfoDouble(symbol2,SYMBOL_ASK), 2);

   bool still_valid = (tradeType == buy) ? (current_gap <= trigger) : (current_gap >= trigger);
   if(!still_valid)
     {
      Print(">>> SKIP Set_",level," — price moved away before execution | gap=",current_gap," trigger=",trigger);
      g_condition_prev[level] = false;
      return false;
     }

   Print(">>> TRIGGER Set_",level," gap=",difference_open," -> confirmed=",current_gap," crossed ",trigger,
         " (",get_level_trades_pair(level),"/",numberOfPair,")");

   g_trade_in_progress = true;
   g_trade_lock_time   = GetTickCount();

   string comment = "SET_" + string(level);
   string sym1 = symbol1, sym2 = symbol2;
   ulong  tckt = -1, tckt1 = -1;

   // [5] Async open — dono legs ek saath broker ko jaayengi
   if(tradeType == buy)
     {
      if(SymbolToTrade == Sym1)
        { tckt = placeBuyTrade(sym1,comment);  tckt1 = placeSellTrade(sym2,comment); }
      else
        { tckt1 = placeSellTrade(sym2,comment); tckt = placeBuyTrade(sym1,comment); }
     }
   else
     {
      if(SymbolToTrade == Sym1)
        { tckt = placeSellTrade(sym1,comment); tckt1 = placeBuyTrade(sym2,comment); }
      else
        { tckt1 = placeBuyTrade(sym2,comment); tckt = placeSellTrade(sym1,comment); }
     }

   // Async mode — ResultOrder unreliable, scan actual positions instead
   datetime now = TimeCurrent();
   scanAndRegisterTrades(level, now); // scan immediately — no delay

   int activePairs = get_level_trades_pair(level);
   if(activePairs > 0)
     {
      if(g_actual_open_gap[level] == 0) g_actual_open_gap[level] = difference_open;
      if(activePairs >= numberOfPair)
        {
         g_level_filled[level]   = true;
         g_condition_prev[level] = true;
         Print(">>> Set_",level," FILLED (",activePairs,"/",numberOfPair,") — waiting for cut");
        }
      else
        {
         g_condition_prev[level] = false;
         Print(">>> Set_",level," pair ",activePairs,"/",numberOfPair," done — waiting for next pair");
        }
      g_trade_in_progress = false;
      return true;
     }
   else
     {
      // No positions found — order failed, lock level
      Print(">>> FAIL Set_",level," — no positions found after async open, LOCKING");
      g_condition_prev[level] = false;
      g_level_locked[level]   = true;
      g_trade_in_progress     = false;
      return false;
     }
  }

//+------------------------------------------------------------------+
//| level_trade_add_to_structure                                      |
//+------------------------------------------------------------------+
void level_trade_add_to_structure(int level, ulong tckt, datetime openTime)
  {
   for(int i = 0; i < orderNum; i++)
     {
      if(od_1[i].lvl == -1 || od_1[i].lvl == level)
        {
         od_1[i].lvl=level; od_1[i].openTime=openTime;
         ArrayResize(od_1[i].tckts, ArraySize(od_1[i].tckts)+1);
         od_1[i].tckts[ArraySize(od_1[i].tckts)-1] = tckt;
         break;
        }
     }
  }

//+------------------------------------------------------------------+
//| Recover structure from open positions on EA attach / init        |
//+------------------------------------------------------------------+
void recoverStructure()
  {
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic_no) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(sym != symbol1 && sym != symbol2) continue;
      string cmt = PositionGetString(POSITION_COMMENT);

      // Extract level from comment "SET_X"
      int level = -1;
      if(StringFind(cmt,"SET_") == 0)
         level = (int)StringToInteger(StringSubstr(cmt, 4));
      if(level < 0 || level > 10) continue;

      // Check already in structure
      bool found = false;
      for(int j = 0; j < orderNum; j++)
        {
         if(od_1[j].lvl != level) continue;
         for(int k = 0; k < ArraySize(od_1[j].tckts); k++)
            if(od_1[j].tckts[k] == t) { found = true; break; }
         if(found) break;
        }
      if(!found)
        {
         level_trade_add_to_structure(level, t, (datetime)PositionGetInteger(POSITION_TIME));
         Print(">>> RECOVERED ticket ",t," SET_",level," (",sym,")");
        }

      // Unlock level so closeTrade can work
      // condition_prev = true taaki dobara trigger na ho
      if(g_level_locked[level])
        {
         g_level_locked[level]   = false;
         g_condition_prev[level] = true;  // FIX: prevent re-trigger after recovery
         Print(">>> SET_",level," UNLOCKED for recovery");
        }
     }
  }

//+------------------------------------------------------------------+
//| Scan open positions by comment and register to structure          |
//| Used after async open to reliably get tickets                     |
//+------------------------------------------------------------------+
void scanAndRegisterTrades(int level, datetime openTime)
  {
   string comment = "SET_" + string(level);
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic_no) continue;
      if(PositionGetString(POSITION_COMMENT) != comment) continue;

      // Check if already in structure
      bool found = false;
      for(int j = 0; j < orderNum; j++)
        {
         if(od_1[j].lvl != level) continue;
         for(int k = 0; k < ArraySize(od_1[j].tckts); k++)
            if(od_1[j].tckts[k] == t) { found = true; break; }
         if(found) break;
        }
      if(!found)
        { level_trade_add_to_structure(level, t, openTime); Print(">>> Registered ticket ",t," for Set_",level); }
     }
  }

//+------------------------------------------------------------------+
//| placeBuyTrade                                                     |
//| Async mode — no wait for broker confirmation                     |
//+------------------------------------------------------------------+
ulong placeBuyTrade(string symbol, string comment)
  {
   trade.SetTypeFilling(GetFillingMode(symbol));
   trade.SetAsyncMode(true);
   // FIX: price refetch at last moment before sending — minimizes slippage
   double price = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double sl=0, tp=0;
   if(stopLoss   != 0) sl = price - stopLoss   * 10 * SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(takeProfit != 0) tp = price + takeProfit  * 10 * SymbolInfoDouble(symbol, SYMBOL_POINT);
   // Pass 0 as price — broker uses best available market price
   bool result = trade.Buy(initialLot, symbol, 0, sl, tp, comment);
   ulong order = trade.ResultOrder();
   trade.SetAsyncMode(false);
   if(result) return order;
   Print(" Error Buy ",symbol,": ",GetLastError()); return -1;
  }

//+------------------------------------------------------------------+
//| placeSellTrade                                                    |
//| Async mode — no wait for broker confirmation                     |
//+------------------------------------------------------------------+
ulong placeSellTrade(string symbol, string comment)
  {
   trade.SetTypeFilling(GetFillingMode(symbol));
   trade.SetAsyncMode(true);
   // FIX: Pass 0 as price — broker uses best available market price
   double price = SymbolInfoDouble(symbol, SYMBOL_BID);
   double sl=0, tp=0;
   if(takeProfit != 0) tp = price - takeProfit * 10 * SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(stopLoss   != 0) sl = price + stopLoss   * 10 * SymbolInfoDouble(symbol, SYMBOL_POINT);
   bool result = trade.Sell(initialLot, symbol, 0, sl, tp, comment);
   ulong order = trade.ResultOrder();
   trade.SetAsyncMode(false);
   if(result) return order;
   Print(" Error Sell ",symbol,": ",GetLastError()); return -1;
  }

//+------------------------------------------------------------------+
//| closeAllTrades                                                    |
//| Async — sab ek saath close                                       |
//+------------------------------------------------------------------+
void closeAllTrades()
  {
   trade.SetAsyncMode(true);
   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t))
         if(PositionGetInteger(POSITION_MAGIC) == magic_no &&
            (PositionGetString(POSITION_SYMBOL) == symbol1 || PositionGetString(POSITION_SYMBOL) == symbol2))
           { if(!trade.PositionClose(t)) Print("Error: ",t," ",GetLastError()); else Print("Closed: ",t); }
     }
   trade.SetAsyncMode(false);
  }

//+------------------------------------------------------------------+
//| close_trade_place_on_that_level                                   |
//| Async — sab ek saath close, phir LOCK                            |
//+------------------------------------------------------------------+
bool close_trade_place_on_that_level(int level, string sym1, string sym2)
  {
   for(int i = 0; i < orderNum; i++)
     {
      if(od_1[i].lvl == level && ArraySize(od_1[i].tckts) != 0)
        {
         Print(">>> CLOSING Set_",level);
         g_level_closing[level] = true;
         trade.SetAsyncMode(true);  // simultaneous close
         for(int j = 0; j < ArraySize(od_1[i].tckts); j++)
           { ulong t=od_1[i].tckts[j]; if(PositionSelectByTicket(t)) { if(!trade.PositionClose(t)) Print("Err:",t); else Print("Closed:",t); } }
         trade.SetAsyncMode(false);
         g_level_closing[level]   = false;
         g_actual_open_gap[level] = 0;
         g_level_filled[level]    = false;
         g_condition_prev[level]  = true;
         g_level_locked[level]    = true;  // LOCK after cut
         Print(">>> Set_",level," CUT & LOCKED — change diffToTrade to re-enable");
         if(tradeOnSameLevel) { ArrayResize(od_1[i].tckts,0); od_1[i].lvl=-1; od_1[i].openTime=0; }
         return true;
        }
     }
   return false;
  }

//+------------------------------------------------------------------+
//| clear_closed_tickets                                              |
//+------------------------------------------------------------------+
void clear_closed_tickets()
  {
   for(int i = 0; i < orderNum; i++)
     {
      if(od_1[i].lvl==-1 || ArraySize(od_1[i].tckts)==0) continue;
      bool anyOpen=false;
      for(int j=0;j<ArraySize(od_1[i].tckts);j++) if(PositionSelectByTicket(od_1[i].tckts[j])) { anyOpen=true; break; }
      if(!anyOpen)
        { int lv=od_1[i].lvl; ArrayResize(od_1[i].tckts,0); od_1[i].lvl=-1; od_1[i].openTime=0; g_level_filled[lv]=false; g_actual_open_gap[lv]=0; }
     }
  }

//+------------------------------------------------------------------+
//| Utility                                                           |
//+------------------------------------------------------------------+
int getActiveOrdersCount(ulong &tckts[])
  { int c=0; for(int k=0;k<ArraySize(tckts);k++) if(PositionSelectByTicket(tckts[k])) c++; return c; }

int get_level_trades_pair(int level)
  { for(int i=0;i<orderNum;i++) if(od_1[i].lvl==level&&ArraySize(od_1[i].tckts)!=0) return int(getActiveOrdersCount(od_1[i].tckts)/2); return 0; }

bool check_trade_place_on_that_level(int level)
  { for(int i=0;i<orderNum;i++) if(od_1[i].lvl==level&&ArraySize(od_1[i].tckts)!=0) return true; return false; }
//+------------------------------------------------------------------+