"""
=============================================================
  Nifty 50 Scalping Bot v4 — Data-Driven Rebuild
  ─────────────────────────────────────────────
  Based on 2-session CSV analysis (May 4-5, 2026)

  STRATEGIES (6 total):
  1. Strong FVG Retest    — no BOS required
  2. ORB + EMA Confirm    — ORB break + EMA aligned
  3. EMA Stack            — Price>EMA9>EMA21>EMA50
  4. VWAP Band Break      — Price breaks ±1SD band
  5. VWAP Reclaim/Reject  — Price crosses VWAP
  6. EMA50 Bounce         — Bounce off EMA50

  FIXES FROM v3 ANALYSIS:
  - ORB now waits for retest (was entering immediately)
  - BOS removed (always 0 for index)
  - OBV gate removed (index has no volume)
  - Strong FVG trades without BOS
  - Trend: 5min alone sufficient if price confirms
  - RVOL threshold: 1.2x (was 1.5x)
  - STRONG_FVG_GAP: 10pts (was 20pts)
  - PCR: try both current + next expiry
  - EMA50 added as new S&R level
  - 6 new strategies added
=============================================================
"""

import time
import logging
import datetime
import csv
import os
import threading
import requests
import numpy as np
import pandas as pd
import pytz
from bs4 import BeautifulSoup
import config
from nifty_auto_bias import (
    get_combined_bias_nifty,
    pre_trade_check_nifty,
    format_bias_message_nifty,
    format_reversal_alert_nifty
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("nifty_v4.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PARAMETERS (updated from v3 analysis)
# ─────────────────────────────────────────────
SL_POINTS           = 10
TARGET_POINTS       = 8
TRAIL_DISTANCE      = 10
TRAIL_START         = 15
STRONG_FVG_GAP      = 10     # FIXED: was 20, now 10
STRONG_FVG_BODY     = 20     # FIXED: was 30, now 20
MIN_FVG_BODY        = 10     # FIXED: was 15, now 10
BREAKAWAY_GAP_OPEN  = 50
BREAKAWAY_GAP_INTRA = 25
ORB_END_TIME        = datetime.time(9, 45)
MAX_TRADES          = 15
CAPITAL_PER_TRADE   = 6500
DAILY_LOSS_LIMIT    = 3000
DAILY_PROFIT_TARGET = 2000
LOT_SIZE            = 65
OTM_OFFSET          = 100
MIN_RVOL            = 1.2    # FIXED: was 1.5, now 1.2
EMA50_TOLERANCE     = 20     # pts within EMA50 to trigger bounce
NIFTY_KEY           = "NSE_INDEX|Nifty 50"

IST = pytz.timezone("Asia/Kolkata")
def now_ist(): return datetime.datetime.now(IST)
def ist_time(): return now_ist().time()

TRADE_START   = datetime.time(9, 30)
TRADE_END     = datetime.time(14, 30)
EXPIRY_STOP   = datetime.time(13, 0)
REMINDER_TIME = datetime.time(9, 0)

# ─────────────────────────────────────────────
#  UPSTOX HEADERS
# ─────────────────────────────────────────────
def get_headers():
    return {
        "Accept"       : "application/json",
        "Authorization": f"Bearer {config.LIVE_TOKEN}"
    }

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    try:
        url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": config.CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.warning(f"TG: {resp.text}")
    except Exception as e:
        log.error(f"TG: {e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    send_telegram(f"{icon} <b>{title}</b>\n{body}")
    log.info(f"[TG] {title}")

def send_csv_files():
    files = [("scan_log_v4.csv","Nifty Scan v4"),
             ("trade_log_v4.csv","Nifty Trade v4")]
    send_telegram("📊 <b>Nifty v4 Daily CSVs</b>")
    sent = 0
    for fname, caption in files:
        path = f"/home/salverukrishna83/algo-trading/{fname}"
        if not os.path.exists(path): continue
        try:
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(path,"rb") as f:
                resp = requests.post(url, data={
                    "chat_id":config.CHAT_ID,"caption":caption
                }, files={"document":f}, timeout=30)
            if resp.json().get("ok"): sent+=1
        except Exception as e: log.error(f"CSV send: {e}")
    send_telegram(f"Sent {sent}/{len(files)} files — upload to Claude!")

# ─────────────────────────────────────────────
#  TELEGRAM LISTENER
# ─────────────────────────────────────────────
class TelegramListener:
    def __init__(self):
        self.bias = "neutral"
        self.last_update_id = 0
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={
                    "offset":self.last_update_id+1,"timeout":30
                }, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5); continue
                for update in resp.json().get("result",[]):
                    self.last_update_id = update["update_id"]
                    text = update.get("message",{}).get("text","").strip().lower()
                    if text.startswith("/bias"):
                        parts = text.split()
                        if len(parts)>=2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias = parts[1]
                            send_telegram(f"FII/DII Bias: {self.bias.upper()}")
                    elif text=="/status":
                        send_telegram(
                            f"Nifty Bot v4\n"
                            f"Bias: {self.bias.upper()}\n"
                            f"IST: {now_ist().strftime('%H:%M:%S')}")
                    elif text=="/report": send_csv_files()
                    elif text=="/help":
                        send_telegram(
                            "v4 Commands:\n/bias bullish|bearish|neutral\n"
                            "/status\n/report")
            except Exception as e:
                log.error(f"TG poll: {e}"); time.sleep(5)

# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_nifty_ltp():
    try:
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers=get_headers(),
            params={"instrument_key":NIFTY_KEY}, timeout=5
        )
        data = resp.json()
        if data["status"]=="success":
            key = list(data["data"].keys())[0]
            return float(data["data"][key]["last_price"])
        return None
    except Exception as e:
        log.error(f"LTP: {e}"); return None

def get_candles(interval_val=5):
    try:
        url  = (f"https://api.upstox.com/v3/historical-candle/intraday/"
                f"{NIFTY_KEY}/minutes/{interval_val}")
        resp = requests.get(url, headers=get_headers(), timeout=10)
        data = resp.json()
        if data["status"]!="success": return None
        candles = data["data"]["candles"]
        if not candles: return None
        df = pd.DataFrame(candles, columns=[
            "timestamp","open","high","low","close","volume","oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open","high","low","close","volume","oi"]:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        # Use OI as volume proxy for index
        df["volume"] = df["oi"].replace(0, df["volume"])
        df["volume"] = df["volume"].replace(0, 1)  # prevent div/0
        return df
    except Exception as e:
        log.error(f"Candle: {e}"); return None

def get_prev_day_ohlc():
    try:
        today   = datetime.date.today()
        from_dt = (today-datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        to_dt   = today.strftime("%Y-%m-%d")
        url     = (f"https://api.upstox.com/v3/historical-candle/"
                   f"{NIFTY_KEY}/days/1/{to_dt}/{from_dt}")
        resp    = requests.get(url, headers=get_headers(), timeout=10)
        data    = resp.json()
        if data["status"]!="success" or not data["data"]["candles"]:
            return None
        candles = data["data"]["candles"]
        prev    = candles[-2] if len(candles)>=2 else candles[-1]
        return {"open":float(prev[1]),"high":float(prev[2]),
                "low":float(prev[3]),"close":float(prev[4])}
    except Exception as e:
        log.error(f"Prev OHLC: {e}"); return None

# PCR with 30min cache + multi-expiry fallback
_pcr = {"val":None,"bias":"neutral","time":None}

def get_pcr():
    global _pcr
    try:
        now = datetime.datetime.now()
        if _pcr["time"] and (now-_pcr["time"]).seconds < 1800:
            return _pcr["val"], _pcr["bias"]

        today = datetime.date.today()
        # Try current week Thursday AND next week Thursday
        for days_offset in [0, 7]:
            days_to_thu = (3-today.weekday())%7 + days_offset
            if days_to_thu == 0 and days_offset == 0: days_to_thu = 7
            expiry = today + datetime.timedelta(days=days_to_thu)
            resp   = requests.get(
                "https://api.upstox.com/v2/option/chain",
                headers=get_headers(),
                params={"instrument_key":NIFTY_KEY,
                        "expiry_date":expiry.strftime("%Y-%m-%d")},
                timeout=10
            )
            data = resp.json()
            if data["status"]!="success" or not data.get("data"):
                continue
            pe_oi=ce_oi=0
            for r in data["data"]:
                pe=r.get("put_options",{})
                ce=r.get("call_options",{})
                if pe and pe.get("market_data"):
                    pe_oi+=pe["market_data"].get("oi",0)
                if ce and ce.get("market_data"):
                    ce_oi+=ce["market_data"].get("oi",0)
            if ce_oi>0:
                pcr  = round(pe_oi/ce_oi,2)
                bias = "bullish" if pcr>1.2 else "bearish" if pcr<0.8 else "neutral"
                _pcr = {"val":pcr,"bias":bias,"time":now}
                log.info(f"PCR: {pcr} ({bias}) expiry:{expiry}")
                return pcr, bias
        return None,"neutral"
    except Exception as e:
        log.error(f"PCR: {e}"); return None,"neutral"

# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_vwap_bands(df):
    df = df.copy()
    df["typical"] = (df["high"]+df["low"]+df["close"])/3
    df["volume"]  = df["volume"].replace(0,1)
    df["cum_tv"]  = (df["typical"]*df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"]/df["cum_vol"]
    df["cum_tv2"] = (((df["typical"]-df["vwap"])**2)*df["volume"]).cumsum()
    df["sd"]      = np.sqrt(df["cum_tv2"]/df["cum_vol"])
    df["vwap_u1"] = df["vwap"]+df["sd"]
    df["vwap_l1"] = df["vwap"]-df["sd"]
    df["vwap_u2"] = df["vwap"]+2*df["sd"]
    df["vwap_l2"] = df["vwap"]-2*df["sd"]
    return df

def calc_ema(df, periods=[9,21,50]):
    df = df.copy()
    for p in periods:
        df[f"ema{p}"] = df["close"].astype(float).ewm(span=p,adjust=False).mean()
    return df

def calc_rvol(df):
    if df is None or len(df)<5: return 1.5
    # Use OI change as proxy for Nifty index
    col = "oi" if "oi" in df.columns and df["oi"].sum()>0 else "volume"
    vals = df[col].replace(0,np.nan).fillna(method="ffill")
    avg  = float(vals.mean())
    cur  = float(vals.iloc[-1])
    return round(cur/avg,2) if avg>0 else 1.5

def detect_trend_relaxed(df, min_agree=3):
    if df is None or len(df)<4: return "neutral","Not enough",0
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh=sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    hl=sum(1 for i in range(1,len(lows))  if lows[i] >lows[i-1])
    ll=sum(1 for i in range(1,len(lows))  if lows[i] <lows[i-1])
    lh=sum(1 for i in range(1,len(highs)) if highs[i]<highs[i-1])
    bull=min(hh,hl); bear=min(ll,lh)
    if bull>=min_agree: return "bullish",f"HH:{hh} HL:{hl}",bull
    if bear>=min_agree: return "bearish",f"LL:{ll} LH:{lh}",bear
    return "neutral",f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}",0

def detect_trend_multi(df5,df15,df30):
    t5,_,_  = detect_trend_relaxed(df5)
    t15,_,_ = detect_trend_relaxed(df15)
    t30,_,_ = detect_trend_relaxed(df30)
    bull=[t5,t15,t30].count("bullish")
    bear=[t5,t15,t30].count("bearish")
    if bull>=2: return "bullish",f"{t5}/{t15}/{t30}","strong" if bull==3 else "moderate"
    if bear>=2: return "bearish",f"{t5}/{t15}/{t30}","strong" if bear==3 else "moderate"
    # FIXED: Allow 5min alone if strong
    if t5=="bullish": return "bullish",f"{t5}/{t15}/{t30}","weak"
    if t5=="bearish": return "bearish",f"{t5}/{t15}/{t30}","weak"
    return "neutral",f"{t5}/{t15}/{t30}","weak"

def detect_fvg(df):
    if df is None or len(df)<3: return None,"Not enough candles"
    candles=df.tail(15)
    for i in range(len(candles)-1,1,-1):
        c1=candles.iloc[i-2]; c2=candles.iloc[i-1]; c3=candles.iloc[i]
        body=abs(float(c2["close"])-float(c2["open"]))
        if body<MIN_FVG_BODY: continue
        c1h=float(c1["high"]); c1l=float(c1["low"])
        c3h=float(c3["high"]); c3l=float(c3["low"])
        if c1h<c3l:
            size=round(c3l-c1h,2)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {"type":"bullish","top":round(c3l,2),"bottom":round(c1h,2),
                    "mid":round((c3l+c1h)/2,2),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bullish FVG {size:.1f}pts"
        if c1l>c3h:
            size=round(c1l-c3h,2)
            strong=size>=STRONG_FVG_GAP and body>=STRONG_FVG_BODY
            return {"type":"bearish","top":round(c1l,2),"bottom":round(c3h,2),
                    "mid":round((c1l+c3h)/2,2),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bearish FVG {size:.1f}pts"
    return None,"No FVG in last 15 candles"

def detect_orb(df, orb_high, orb_low, ltp):
    if orb_high is None or orb_low is None:
        return None,"ORB not formed yet"
    if ltp:
        if ltp>orb_high and (ltp-orb_high)>=5:
            return {"type":"bullish","level":orb_high,
                    "size":round(ltp-orb_high,1)}, \
                   f"ORB bullish {ltp:.0f}>{orb_high:.0f}"
        if ltp<orb_low and (orb_low-ltp)>=5:
            return {"type":"bearish","level":orb_low,
                    "size":round(orb_low-ltp,1)}, \
                   f"ORB bearish {ltp:.0f}<{orb_low:.0f}"
    return None,f"No ORB | {orb_low:.0f}-{orb_high:.0f}"

# ─────────────────────────────────────────────
#  NEW STRATEGIES (from analysis)
# ─────────────────────────────────────────────
def detect_ema_stack(df_ema, ltp, trend_5m):
    """Price > EMA9 > EMA21 > EMA50 (bullish) or reverse (bearish)."""
    try:
        e9  = float(df_ema["ema9"].iloc[-1])
        e21 = float(df_ema["ema21"].iloc[-1])
        e50 = float(df_ema["ema50"].iloc[-1])
        if ltp>e9>e21>e50 and trend_5m=="bullish":
            return {"type":"bullish","e9":round(e9,1),"e21":round(e21,1),"e50":round(e50,1)}, \
                   f"EMA Stack bullish | P:{ltp:.0f}>E9:{e9:.0f}>E21:{e21:.0f}>E50:{e50:.0f}"
        if ltp<e9<e21<e50 and trend_5m=="bearish":
            return {"type":"bearish","e9":round(e9,1),"e21":round(e21,1),"e50":round(e50,1)}, \
                   f"EMA Stack bearish | P:{ltp:.0f}<E9:{e9:.0f}<E21:{e21:.0f}<E50:{e50:.0f}"
        return None, f"No EMA stack | E9:{e9:.0f} E21:{e21:.0f} E50:{e50:.0f}"
    except: return None,"EMA stack error"

def detect_ema_cross(df_ema, prev_df_ema):
    """EMA9 crosses EMA21."""
    try:
        e9  = float(df_ema["ema9"].iloc[-1])
        e21 = float(df_ema["ema21"].iloc[-1])
        pe9 = float(prev_df_ema["ema9"].iloc[-1])
        pe21= float(prev_df_ema["ema21"].iloc[-1])
        if pe9<=pe21 and e9>e21:
            return {"type":"bullish","e9":round(e9,1),"e21":round(e21,1)}, \
                   f"EMA9 crossed above EMA21 | {e9:.0f}>{e21:.0f}"
        if pe9>=pe21 and e9<e21:
            return {"type":"bearish","e9":round(e9,1),"e21":round(e21,1)}, \
                   f"EMA9 crossed below EMA21 | {e9:.0f}<{e21:.0f}"
        return None, f"No EMA cross | gap:{e9-e21:+.1f}pts"
    except: return None,"EMA cross error"

def detect_vwap_band_break(df_vwap, ltp, trend_5m):
    """Price breaks outside VWAP ±1SD band."""
    try:
        last = df_vwap.iloc[-1]
        prev = df_vwap.iloc[-2]
        vu1  = float(last["vwap_u1"])
        vl1  = float(last["vwap_l1"])
        p_ltp= float(prev["close"])
        if p_ltp<vu1 and ltp>vu1 and trend_5m=="bullish":
            return {"type":"bullish","level":round(vu1,1)}, \
                   f"Broke above VWAP +1SD {vu1:.0f}"
        if p_ltp>vl1 and ltp<vl1 and trend_5m=="bearish":
            return {"type":"bearish","level":round(vl1,1)}, \
                   f"Broke below VWAP -1SD {vl1:.0f}"
        return None, f"No VWAP band break | U1:{vu1:.0f} L1:{vl1:.0f}"
    except: return None,"VWAP band error"

def detect_vwap_cross(df_vwap, ltp):
    """Price crosses VWAP — reclaim or rejection."""
    try:
        last  = df_vwap.iloc[-1]
        prev  = df_vwap.iloc[-2]
        vwap  = float(last["vwap"])
        pvwap = float(prev["vwap"])
        pltp  = float(prev["close"])
        if pltp<pvwap and ltp>vwap:
            return {"type":"bullish","vwap":round(vwap,1)}, \
                   f"VWAP reclaim {vwap:.0f} — bullish"
        if pltp>pvwap and ltp<vwap:
            return {"type":"bearish","vwap":round(vwap,1)}, \
                   f"VWAP rejection {vwap:.0f} — bearish"
        return None, f"No VWAP cross | VWAP:{vwap:.0f} Price:{ltp:.0f}"
    except: return None,"VWAP cross error"

def detect_ema50_bounce(df_ema, ltp, trend_5m):
    """Price bounces off EMA50 support/resistance."""
    try:
        e50  = float(df_ema["ema50"].iloc[-1])
        dist = abs(ltp-e50)
        if dist<=EMA50_TOLERANCE:
            if trend_5m=="bullish" and ltp>e50:
                return {"type":"bullish","e50":round(e50,1)}, \
                       f"EMA50 bounce support {e50:.0f} | dist:{dist:.1f}pts"
            if trend_5m=="bearish" and ltp<e50:
                return {"type":"bearish","e50":round(e50,1)}, \
                       f"EMA50 rejection {e50:.0f} | dist:{dist:.1f}pts"
        return None, f"No EMA50 bounce | E50:{e50:.0f} dist:{dist:.1f}pts"
    except: return None,"EMA50 error"

def is_retesting(price, bottom, top):
    return bottom<=price<=top

def get_option_details(nifty_price, option_type):
    atm    = round(nifty_price/50)*50
    strike = atm+OTM_OFFSET if option_type=="CE" else atm-OTM_OFFSET
    today  = datetime.date.today()
    days   = (3-today.weekday())%7
    if days==0: days=7
    expiry = today+datetime.timedelta(days=days)
    return strike,expiry

def is_expiry_day():
    return datetime.date.today().weekday()==3

# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium, signal,
                 pcr, fii_bias, pre_bias, rvol, trend_strength, is_strong=False):
        self.trade_no     = trade_no
        self.strategy     = strategy
        self.direction    = direction
        self.entry_price  = entry_price
        self.option_type  = option_type
        self.strike       = strike
        self.expiry       = expiry
        self.premium      = premium
        self.signal       = signal
        self.pcr          = pcr
        self.fii_bias     = fii_bias
        self.pre_bias     = pre_bias
        self.rvol         = rvol
        self.trend_strength = trend_strength
        self.is_strong    = is_strong
        self.entry_time   = now_ist().strftime("%H:%M:%S IST")
        self.start_time   = time.time()
        self.be_moved     = False
        self.trailing     = is_strong
        self.best_price   = entry_price
        self.sl_price     = entry_price-SL_POINTS if direction=="bullish" else entry_price+SL_POINTS
        self.tgt_price    = entry_price+TARGET_POINTS if direction=="bullish" else entry_price-TARGET_POINTS

    def check(self, ltp):
        if self.trailing:
            if self.direction=="bullish" and ltp>self.best_price:
                self.best_price=ltp
                profit=ltp-self.entry_price
                if profit>=TRAIL_START:
                    new_sl=round(ltp-TRAIL_DISTANCE,2)
                    if new_sl>self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f}",f"Profit:+{profit:.0f}pts",
                            f"New SL:{new_sl:.0f}"])
            elif self.direction=="bearish" and ltp<self.best_price:
                self.best_price=ltp
                profit=self.entry_price-ltp
                if profit>=TRAIL_START:
                    new_sl=round(ltp+TRAIL_DISTANCE,2)
                    if new_sl<self.sl_price:
                        self.sl_price=new_sl
                        tg("TRAIL",f"Trade #{self.trade_no} Trail SL",
                           [f"Nifty:{ltp:.0f}",f"Profit:+{profit:.0f}pts",
                            f"New SL:{new_sl:.0f}"])
            if self.direction=="bullish" and ltp<=self.sl_price: return "sl"
            if self.direction=="bearish" and ltp>=self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half=(self.entry_price+self.tgt_price)/2
                if (self.direction=="bullish" and ltp>=half) or \
                   (self.direction=="bearish" and ltp<=half):
                    self.be_moved=True; self.sl_price=self.entry_price
                    tg("LOCK",f"Trade #{self.trade_no} Breakeven",
                       [f"Nifty:{ltp:.0f}",f"SL moved to:{self.entry_price:.0f}"])
            if self.direction=="bullish":
                if ltp>=self.tgt_price: return "target"
                if ltp<=self.sl_price:  return "sl"
            else:
                if ltp<=self.tgt_price: return "target"
                if ltp>=self.sl_price:  return "sl"
        return None

    def duration(self): return round((time.time()-self.start_time)/60,1)
    def calc_pnl(self, exit_price):
        pts=exit_price-self.entry_price if self.direction=="bullish" else self.entry_price-exit_price
        return round(pts*0.4*LOT_SIZE,0)

# ─────────────────────────────────────────────
#  CSV LOGS
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime","nifty_ltp","chg_from_open","chg_pct",
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    "rvol","vwap","vwap_u1","vwap_l1","price_vs_vwap",
    "ema9","ema21","ema50","price_vs_ema9","price_vs_ema21","price_vs_ema50",
    "ema9_vs_ema21",
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "orb_high","orb_low","orb_signal","orb_size",
    "ema_stack","ema_cross","vwap_band_break","vwap_cross","ema50_bounce",
    "pcr","pcr_bias","fii_bias","overall_bias",
    "entry_condition_met","strategy_triggered",
    "trades_today","daily_pnl","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","entry_time","exit_time",
    "pre_bias","fii_bias","pcr","trend_combined","trend_strength",
    "rvol_at_entry","direction","is_strong","exit_mode",
    "entry_nifty","exit_nifty","points_moved",
    "option_type","strike","expiry",
    "premium","lots","capital_used",
    "sl_points","target_points","pnl_est","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl","notes"
]

def init_logs():
    for fname,cols in [("scan_log_v4.csv",SCAN_COLS),
                        ("trade_log_v4.csv",TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname,"w",newline="") as f:
                csv.DictWriter(f,fieldnames=cols).writeheader()
    log.info("Nifty v4 logs initialised")

def write_scan(rec):
    with open("scan_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in SCAN_COLS}
        csv.DictWriter(f,fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("trade_log_v4.csv","a",newline="") as f:
        row={c:rec.get(c,"") for c in TRADE_COLS}
        csv.DictWriter(f,fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats, pre_bias, pcr):
    wr=(stats["wins"]/stats["trades"]*100) if stats["trades"]>0 else 0
    tg("SUMMARY","Nifty v4 DAILY SUMMARY",
       [f"Pre-bias   : {pre_bias.upper()}",
        f"PCR        : {pcr or 'N/A'}",
        f"Trades     : {stats['trades']}",
        f"Wins       : {stats['wins']}",
        f"Losses     : {stats['losses']}",
        f"Win rate   : {wr:.1f}%",
        f"P&L        : Rs.{stats['pnl']:+.0f}",
        f"By strategy: FVG:{stats.get('fvg',0)} ORB:{stats.get('orb',0)} "
        f"EMA:{stats.get('ema',0)} VWAP:{stats.get('vwap',0)}"])
    send_csv_files()

def open_trade(trade_no, strategy, direction, entry_price,
               pcr_val, tg_listener, pre_bias, is_strong,
               signal, rvol, trend_strength, risk_level):
    opt    = "CE" if direction=="bullish" else "PE"
    strike,expiry = get_option_details(entry_price,opt)
    premium= round(CAPITAL_PER_TRADE/LOT_SIZE,1)
    trade  = PaperTrade(
        trade_no=trade_no,strategy=strategy,
        direction=direction,entry_price=entry_price,
        option_type=opt,strike=strike,expiry=expiry,
        premium=premium,signal=signal,pcr=pcr_val,
        fii_bias=tg_listener.bias,pre_bias=pre_bias,
        rvol=rvol,trend_strength=trend_strength,is_strong=is_strong
    )
    mode = "Trailing" if is_strong else f"Fixed {TARGET_POINTS}pts"
    tg("ENTRY",f"NIFTY PAPER TRADE #{trade_no} — {strategy}",
       [f"Direction    : {direction.upper()}",
        f"Strength     : {trend_strength}",
        f"Option       : {opt} {strike} | {expiry}",
        f"Entry Nifty  : {entry_price:.1f}",
        f"SL           : {trade.sl_price:.1f} (-{SL_POINTS}pts)",
        f"Target       : {trade.tgt_price:.1f} (+{TARGET_POINTS}pts)",
        f"Exit mode    : {mode}",
        f"RVOL         : {rvol}x",
        f"Rev risk     : {risk_level}",
        f"Signal       : {signal}",
        f"Capital      : Rs.{premium*LOT_SIZE:.0f}",
        f"NOTE         : PAPER TRADE"])
    return trade

# ─────────────────────────────────────────────
#  WAIT FOR RETEST HELPER
# ─────────────────────────────────────────────
def wait_for_retest(bottom, top, timeout_min=10):
    """Wait for price to enter zone. Returns (ok, price)."""
    start = time.time()
    tg("WAIT",f"Waiting for retest {bottom:.0f}-{top:.0f}",
       [f"Timeout: {timeout_min} min"])
    while time.time()-start < timeout_min*60:
        ltp = get_nifty_ltp()
        if ltp and is_retesting(ltp, bottom, top):
            return True, ltp
        time.sleep(15)
    return False, None

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    init_logs()
    tg_listener = TelegramListener()
    tg_listener.start()

    stats = {
        "trades":0,"wins":0,"losses":0,"timeouts":0,
        "skipped":0,"pnl":0.0,"consec_loss":0,
        "fvg":0,"orb":0,"ema":0,"vwap":0
    }

    trade_no     = 0
    active_trade = None
    last_scan    = None
    pre_bias     = "neutral"
    pcr_val      = None
    pcr_bias     = "neutral"
    premarket_done = False
    reminder_sent  = False
    orb_high     = None
    orb_low      = None
    orb_formed   = False
    prev_ohlc    = None
    used_signals = set()
    open_price   = None
    closed_summary_sent = False
    prev_df5_ema = None   # for EMA cross detection

    send_telegram(
        f"Nifty Scalping Bot v4 Started\n"
        f"Strategies: Strong FVG | ORB+EMA | EMA Stack | "
        f"VWAP Band | VWAP Cross | EMA50 Bounce\n"
        f"SL:{SL_POINTS}pts TGT:{TARGET_POINTS}pts\n"
        f"RVOL min:{MIN_RVOL}x FVG gap:{STRONG_FVG_GAP}pts\n"
        f"Max:{MAX_TRADES} Loss:Rs.{DAILY_LOSS_LIMIT} Profit:Rs.{DAILY_PROFIT_TARGET}\n\n"
        f"Send /bias bullish|bearish|neutral before 9:30"
    )

    while True:
        t   = ist_time()
        now = now_ist()

        # Reminder
        if not reminder_sent and REMINDER_TIME<=t<TRADE_START:
            pcr_v,pcr_b = get_pcr()
            send_telegram(
                f"Nifty market opens in 30 min!\n"
                f"PCR: {pcr_v or 'N/A'} ({pcr_b})\n"
                f"Send /bias bullish|bearish|neutral"
            )
            reminder_sent=True

        # Outside hours
        if t<TRADE_START:
            if not premarket_done and t>=REMINDER_TIME:
                prev_ohlc = get_prev_day_ohlc()
                final_bias,bias_report = get_combined_bias_nifty(
                    config.LIVE_TOKEN,
                    prev_ohlc["close"] if prev_ohlc else None,
                    tg_listener.bias
                )
                pre_bias=final_bias; pcr_val=bias_report["pcr_val"]
                pcr_bias=bias_report["pcr_bias"]
                send_telegram(format_bias_message_nifty(bias_report))
                premarket_done=True
            time.sleep(30); continue

        premarket_done=False

        if t>=TRADE_END:
            if not closed_summary_sent:
                send_summary(stats,pre_bias,pcr_val)
                closed_summary_sent=True
                stats={"trades":0,"wins":0,"losses":0,"timeouts":0,
                       "skipped":0,"pnl":0.0,"consec_loss":0,
                       "fvg":0,"orb":0,"ema":0,"vwap":0}
                trade_no=0; active_trade=None; last_scan=None
                pre_bias="neutral"; pcr_val=None; orb_high=None
                orb_low=None; orb_formed=False; used_signals=set()
                tg_listener.bias="neutral"; reminder_sent=False
                premarket_done=False; open_price=None
                closed_summary_sent=False; prev_df5_ema=None
            time.sleep(60); continue

        closed_summary_sent=False

        # Guards
        if stats["trades"]>=MAX_TRADES: time.sleep(30*60); continue
        if stats["consec_loss"]>=3:
            tg("STOP","Risk Protection",[f"Losses:{stats['consec_loss']}"])
            send_summary(stats,pre_bias,pcr_val); time.sleep(16*3600); continue
        if stats["pnl"]<=-DAILY_LOSS_LIMIT:
            tg("STOP","Loss Limit",[f"P&L:Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_val); time.sleep(16*3600); continue
        if stats["pnl"]>=DAILY_PROFIT_TARGET:
            tg("DONE","Profit Target!",[f"P&L:Rs.{stats['pnl']:+.0f}"])
            send_summary(stats,pre_bias,pcr_val); time.sleep(16*3600); continue
        if is_expiry_day() and t>=EXPIRY_STOP: time.sleep(10*60); continue

        # Monitor active trade
        if active_trade is not None:
            ltp=get_nifty_ltp(); result=None
            if ltp: result=active_trade.check(ltp)
            if t>=TRADE_END: result="timeout"; ltp=ltp or active_trade.entry_price
            if result:
                exit_time=now.strftime("%H:%M:%S")
                duration=active_trade.duration()
                pnl=active_trade.calc_pnl(ltp)
                pts=round(ltp-active_trade.entry_price,2) if active_trade.direction=="bullish" \
                    else round(active_trade.entry_price-ltp,2)
                if result=="target": icon="WIN"; stats["wins"]+=1; stats["consec_loss"]=0
                elif result=="sl":   icon="LOSS"; stats["losses"]+=1; stats["consec_loss"]+=1
                else:                icon="TIME"; stats["timeouts"]+=1; stats["consec_loss"]=0
                stats["trades"]+=1; stats["pnl"]+=pnl
                tg(icon,f"TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy : {active_trade.strategy}",
                    f"Entry    : {active_trade.entry_price:.1f}",
                    f"Exit     : {ltp:.1f}",
                    f"Points   : {pts:+.1f}",
                    f"Duration : {duration}min",
                    f"P&L      : Rs.{pnl:+.0f}",
                    f"Day P&L  : Rs.{stats['pnl']:+.0f}",
                    f"Trades   : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date":datetime.date.today(),"trade_no":active_trade.trade_no,
                    "strategy":active_trade.strategy,
                    "entry_time":active_trade.entry_time,
                    "exit_time":now.strftime("%H:%M:%S"),
                    "pre_bias":pre_bias,"fii_bias":active_trade.fii_bias,
                    "pcr":active_trade.pcr,
                    "trend_combined":active_trade.trend_strength,
                    "trend_strength":active_trade.trend_strength,
                    "rvol_at_entry":active_trade.rvol,
                    "direction":active_trade.direction,
                    "is_strong":active_trade.is_strong,
                    "exit_mode":"Trail" if active_trade.trailing else "Fixed",
                    "entry_nifty":active_trade.entry_price,
                    "exit_nifty":round(ltp,2),"points_moved":pts,
                    "option_type":active_trade.option_type,
                    "strike":active_trade.strike,"expiry":active_trade.expiry,
                    "premium":active_trade.premium,"lots":1,
                    "capital_used":active_trade.premium*LOT_SIZE,
                    "sl_points":SL_POINTS,"target_points":TARGET_POINTS,
                    "pnl_est":pnl,"result":result,
                    "be_triggered":active_trade.be_moved,
                    "trail_triggered":active_trade.trailing,
                    "duration_min":duration,"consec_losses":stats["consec_loss"],
                    "daily_pnl":stats["pnl"],"notes":active_trade.signal
                })
                active_trade=None; time.sleep(2*60)
            else: time.sleep(15)
            continue

        # Fetch fresh data
        ltp   = get_nifty_ltp()
        df_5  = get_candles(5)
        df_15 = get_candles(15)
        df_30 = get_candles(30)
        if ltp is None or df_5 is None: time.sleep(15); continue
        if open_price is None: open_price=ltp

        # ORB formation
        if not orb_formed and t>=ORB_END_TIME:
            try:
                orb_df=df_5[df_5["timestamp"].dt.time<=ORB_END_TIME]
                if not orb_df.empty:
                    orb_high=float(orb_df["high"].max())
                    orb_low =float(orb_df["low"].min())
                    orb_formed=True
                    tg("ORB","Nifty ORB Formed v4",
                       [f"High:{orb_high:.0f} Low:{orb_low:.0f}",
                        f"Size:{orb_high-orb_low:.0f}pts",
                        f"Nifty now:{ltp:.0f}",
                        f"vs ORB high:{ltp-orb_high:+.0f}pts"])
            except Exception as e: log.error(f"ORB: {e}")

        # PCR refresh
        pcr_val,pcr_bias = get_pcr()

        # Calculate all indicators
        trend,trend_r,trend_strength = detect_trend_multi(df_5,df_15,df_30)
        t5,_,_  = detect_trend_relaxed(df_5)
        t15,_,_ = detect_trend_relaxed(df_15)
        t30,_,_ = detect_trend_relaxed(df_30)
        rvol    = calc_rvol(df_5)

        df5_vwap = calc_vwap_bands(df_5)
        df5_ema  = calc_ema(df_5)
        lr       = df5_vwap.iloc[-1]
        vwap     = round(float(lr["vwap"]),1)
        vu1      = round(float(lr["vwap_u1"]),1)
        vl1      = round(float(lr["vwap_l1"]),1)
        e9       = round(float(df5_ema["ema9"].iloc[-1]),1)
        e21      = round(float(df5_ema["ema21"].iloc[-1]),1)
        e50      = round(float(df5_ema["ema50"].iloc[-1]),1)

        # Detect all strategies
        fvg,    fvg_r   = detect_fvg(df_5)
        orb_s,  orb_r   = detect_orb(df_5, orb_high, orb_low, ltp)
        ema_stk,ema_sk_r= detect_ema_stack(df5_ema, ltp, t5)
        ema_cx, ema_cx_r= detect_ema_cross(df5_ema, prev_df5_ema) if prev_df5_ema is not None \
                          else (None, "No prev EMA data")
        vwap_bb,vwap_bb_r=detect_vwap_band_break(df5_vwap, ltp, t5)
        vwap_cx,vwap_cx_r=detect_vwap_cross(df5_vwap, ltp)
        ema50_b,ema50_r = detect_ema50_bounce(df5_ema, ltp, t5)

        # Store for next iteration EMA cross
        prev_df5_ema = df5_ema.copy()

        # RVOL check
        rvol_ok = rvol >= MIN_RVOL

        # 5-min scan log
        do_scan = (last_scan is None or (now_ist()-last_scan).seconds>=300)
        if do_scan:
            last_scan = now_ist()
            strats = []
            if fvg and fvg.get("strong"): strats.append("StrongFVG")
            if orb_s and ema_stk and orb_s["type"]==ema_stk["type"]: strats.append("ORB+EMA")
            if ema_stk:   strats.append("EMAStack")
            if vwap_bb:   strats.append("VWAPBand")
            if vwap_cx:   strats.append("VWAPCross")
            if ema50_b:   strats.append("EMA50Bounce")
            if ema_cx:    strats.append("EMACross")
            entry_met = len(strats)>0 and rvol_ok
            chg_open  = round(ltp-open_price,1) if open_price else 0
            chg_pct   = round((chg_open/open_price*100),2) if open_price else 0
            write_scan({
                "datetime":now.strftime("%Y-%m-%d %H:%M IST"),
                "nifty_ltp":round(ltp,1),"chg_from_open":chg_open,
                "chg_pct":chg_pct,"trend_5m":t5,"trend_15m":t15,
                "trend_30m":t30,"trend_combined":trend,"trend_strength":trend_strength,
                "rvol":rvol,"vwap":vwap,"vwap_u1":vu1,"vwap_l1":vl1,
                "price_vs_vwap":round(ltp-vwap,1),
                "ema9":e9,"ema21":e21,"ema50":e50,
                "price_vs_ema9":round(ltp-e9,1),
                "price_vs_ema21":round(ltp-e21,1),
                "price_vs_ema50":round(ltp-e50,1),
                "ema9_vs_ema21":round(e9-e21,1),
                "fvg_found":fvg is not None,
                "fvg_type":fvg["type"] if fvg else "",
                "fvg_strong":fvg["strong"] if fvg else "",
                "fvg_size":fvg["size"] if fvg else "",
                "orb_high":orb_high or "","orb_low":orb_low or "",
                "orb_signal":orb_s["type"] if orb_s else "",
                "orb_size":orb_s["size"] if orb_s else "",
                "ema_stack":ema_stk["type"] if ema_stk else "",
                "ema_cross":ema_cx["type"] if ema_cx else "",
                "vwap_band_break":vwap_bb["type"] if vwap_bb else "",
                "vwap_cross":vwap_cx["type"] if vwap_cx else "",
                "ema50_bounce":ema50_b["type"] if ema50_b else "",
                "pcr":pcr_val or "","pcr_bias":pcr_bias,
                "fii_bias":tg_listener.bias,"overall_bias":pre_bias,
                "entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "trades_today":stats["trades"],"daily_pnl":stats["pnl"],
                "reason":f"FVG:{fvg_r}|ORB:{orb_r}|EMAStk:{ema_sk_r}|VWAPBand:{vwap_bb_r}"
            })
            icon = "OK" if entry_met else "WAIT"
            tg(icon,f"NIFTY SCAN v4 {now.strftime('%H:%M')}",
               [f"Nifty      : {ltp:.0f} ({chg_pct:+.2f}%)",
                f"Trend      : {trend.upper()} ({trend_strength})",
                f"5m/15m/30m : {t5}/{t15}/{t30}",
                f"RVOL       : {rvol}x {'OK' if rvol_ok else 'LOW'}",
                f"VWAP       : {vwap:.0f} ({ltp-vwap:+.0f})",
                f"EMA9/21/50 : {e9:.0f}/{e21:.0f}/{e50:.0f}",
                f"EMA9-EMA21 : {e9-e21:+.0f}pts",
                f"FVG        : {fvg_r[:40] if fvg else 'NONE'}",
                f"ORB        : {orb_r[:40]}",
                f"EMA Stack  : {ema_sk_r[:40] if ema_stk else 'NONE'}",
                f"EMA Cross  : {ema_cx_r[:40] if ema_cx else 'NONE'}",
                f"VWAP Band  : {vwap_bb_r[:40] if vwap_bb else 'NONE'}",
                f"VWAP Cross : {vwap_cx_r[:40] if vwap_cx else 'NONE'}",
                f"EMA50 Bnce : {ema50_r[:40] if ema50_b else 'NONE'}",
                f"PCR        : {pcr_val or 'N/A'} ({pcr_bias})",
                f"Bias       : {pre_bias.upper()}",
                f"Signals    : {', '.join(strats) if strats else 'NONE'}"])

        # RVOL gate
        if not rvol_ok: time.sleep(60); continue

        # ── STRATEGY 1: STRONG FVG (no BOS needed) ───────────
        if fvg and fvg.get("strong") and "SFVG" not in used_signals:
            if fvg["type"]==trend and (pre_bias=="neutral" or pre_bias==trend):
                proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                    df_5,df_15,trend,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_sigs,summary,"StrongFVG",trend))
                if proceed:
                    retest_ok,ep = wait_for_retest(fvg["bottom"],fvg["top"])
                    if retest_ok:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"StrongFVG",trend,ep,pcr_val,
                            tg_listener,pre_bias,True,
                            f"Strong FVG {fvg['size']:.1f}pts",
                            rvol,trend_strength,risk)
                        used_signals.add("SFVG"); stats["fvg"]+=1
                        time.sleep(15); continue
                    else:
                        tg("TIME","StrongFVG retest timeout",
                           [f"Zone:{fvg['bottom']:.0f}-{fvg['top']:.0f}"])
                        stats["skipped"]+=1
                else: stats["skipped"]+=1

        # ── STRATEGY 2: ORB + EMA CONFIRM ────────────────────
        if orb_s and orb_formed and "ORB" not in used_signals:
            orb_dir = orb_s["type"]
            ema_ok  = (e9>e21 if orb_dir=="bullish" else e9<e21)
            bias_ok = (pre_bias=="neutral" or pre_bias==orb_dir)
            if ema_ok and bias_ok:
                proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                    df_5,df_15,orb_dir,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_sigs,summary,"ORB+EMA",orb_dir))
                if proceed:
                    # FIXED: wait for retest of ORB level
                    level  = orb_s["level"]
                    tg("WAIT",f"ORB+EMA — waiting retest of {level:.0f}",
                       [f"Breakout:{orb_dir.upper()} by {orb_s['size']:.1f}pts",
                        f"EMA9:{e9:.0f} EMA21:{e21:.0f} aligned ✅"])
                    retest_ok,ep = wait_for_retest(level-8, level+8, timeout_min=10)
                    if retest_ok:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"ORB+EMA",orb_dir,ep,pcr_val,
                            tg_listener,pre_bias,False,
                            f"ORB {orb_dir} {orb_s['size']:.1f}pts + EMA confirm",
                            rvol,trend_strength,risk)
                        used_signals.add("ORB"); stats["orb"]+=1
                        time.sleep(15); continue
                    else:
                        tg("TIME","ORB retest timeout",
                           [f"Level:{level:.0f} not retested in 10min"])
                        stats["skipped"]+=1
                else: stats["skipped"]+=1

        # ── STRATEGY 3: EMA STACK ────────────────────────────
        if ema_stk and "EMASTK" not in used_signals:
            stk_dir = ema_stk["type"]
            if pre_bias=="neutral" or pre_bias==stk_dir:
                proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                    df_5,df_15,stk_dir,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_sigs,summary,"EMAStack",stk_dir))
                if proceed:
                    cur = get_nifty_ltp()
                    if cur:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"EMAStack",stk_dir,cur,pcr_val,
                            tg_listener,pre_bias,False,
                            f"EMA Stack {stk_dir} E9:{ema_stk['e9']:.0f}>E21:{ema_stk['e21']:.0f}>E50:{ema_stk['e50']:.0f}",
                            rvol,trend_strength,risk)
                        used_signals.add("EMASTK"); stats["ema"]+=1
                        time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 4: VWAP BAND BREAK ──────────────────────
        if vwap_bb and "VWAPBB" not in used_signals:
            bb_dir = vwap_bb["type"]
            if pre_bias=="neutral" or pre_bias==bb_dir:
                proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                    df_5,df_15,bb_dir,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_sigs,summary,"VWAPBand",bb_dir))
                if proceed:
                    cur = get_nifty_ltp()
                    if cur:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"VWAPBand",bb_dir,cur,pcr_val,
                            tg_listener,pre_bias,False,
                            f"VWAP band break {bb_dir} level:{vwap_bb['level']:.0f}",
                            rvol,trend_strength,risk)
                        used_signals.add("VWAPBB"); stats["vwap"]+=1
                        time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 5: VWAP CROSS ───────────────────────────
        if vwap_cx and "VWAPCX" not in used_signals:
            cx_dir = vwap_cx["type"]
            if pre_bias=="neutral" or pre_bias==cx_dir:
                proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                    df_5,df_15,cx_dir,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_sigs,summary,"VWAPCross",cx_dir))
                if proceed:
                    cur = get_nifty_ltp()
                    if cur:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"VWAPCross",cx_dir,cur,pcr_val,
                            tg_listener,pre_bias,False,
                            f"VWAP cross {cx_dir} @ {vwap_cx['vwap']:.0f}",
                            rvol,trend_strength,risk)
                        used_signals.add("VWAPCX"); stats["vwap"]+=1
                        time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 6: EMA50 BOUNCE ─────────────────────────
        if ema50_b and "EMA50" not in used_signals:
            b50_dir = ema50_b["type"]
            if pre_bias=="neutral" or pre_bias==b50_dir:
                proceed,risk,summary,rev_sigs = pre_trade_check_nifty(
                    df_5,df_15,b50_dir,pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,prev_ohlc
                )
                send_telegram(format_reversal_alert_nifty(
                    risk,proceed,rev_sigs,summary,"EMA50Bounce",b50_dir))
                if proceed:
                    cur = get_nifty_ltp()
                    if cur:
                        trade_no+=1
                        active_trade=open_trade(
                            trade_no,"EMA50Bounce",b50_dir,cur,pcr_val,
                            tg_listener,pre_bias,False,
                            f"EMA50 bounce {b50_dir} @ {ema50_b['e50']:.0f}",
                            rvol,trend_strength,risk)
                        used_signals.add("EMA50"); stats["ema"]+=1
                        time.sleep(15); continue
                else: stats["skipped"]+=1

        time.sleep(60)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Nifty Bot v4 stopped")
        send_telegram("Nifty Bot v4 stopped.")
