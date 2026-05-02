"""
=============================================================
  NYSE SPY Options Scalping Bot v3 — COMPLETE
  ─────────────────────────────────────────────
  PATCHES:
  #1  ORB breakout detected AT formation
  #2  Trend relaxed 3/4 candles
  #3  FVG body filter fixed for SPY ($0.5)
  #4  Candle cache cleared every scan
  #5  Telegram gated — no alerts when closed
  #6  F&G refresh every 30min with fallback
  #7  RVOL filter min 1.5x
  #8  OBV direction confirmation
  #9  VWAP bands +-1SD +-2SD
  #10 Multi-timeframe trend 5m+15m+30m
  #11 Alpaca auto-bias pre-market
  #12 1PM IST bias reminder
  #13 35+ scan columns
  #14 EMA 9/21/50
  #15 Daily CSV auto-send
  #16 nyse_auto_bias integrated — reversal check before every trade
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
import alpaca_trade_api as tradeapi
import config
from nyse_auto_bias import (
    get_combined_bias,
    pre_trade_check,
    format_bias_message,
    format_reversal_alert
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("spy_v3.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PARAMETERS
# ─────────────────────────────────────────────
SL_POINTS           = 2.0
TARGET_POINTS       = 1.5
TRAIL_DISTANCE      = 1.5
TRAIL_START         = 2.0
STRONG_FVG_GAP      = 1.0
STRONG_FVG_BODY     = 0.5
MIN_FVG_BODY        = 0.5
BREAKAWAY_GAP_OPEN  = 2.0
BREAKAWAY_GAP_INTRA = 1.0
ORB_END_TIME        = datetime.time(10, 0)
MAX_TRADES          = 10
CAPITAL_PER_TRADE   = 500
DAILY_LOSS_LIMIT    = 1000
DAILY_PROFIT_TARGET = 750
SPY_SYMBOL          = "SPY"
MIN_RVOL            = 1.5
TREND_CANDLES_MIN   = 3

EST = pytz.timezone("US/Eastern")
IST = pytz.timezone("Asia/Kolkata")

def now_est(): return datetime.datetime.now(EST)
def now_ist(): return datetime.datetime.now(IST)
def est_time(): return now_est().time()
def ist_time(): return now_ist().time()

PREMARKET_START = datetime.time(4,  0)
MARKET_START    = datetime.time(9, 30)
MARKET_END      = datetime.time(16, 0)
IST_REMINDER    = datetime.time(13, 0)
IST_PREMARKET   = datetime.time(13, 30)

def get_alpaca():
    return tradeapi.REST(
        config.ALPACA_API_KEY,
        config.ALPACA_SECRET_KEY,
        config.ALPACA_BASE_URL
    )

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
            log.warning(f"TG failed: {resp.text}")
    except Exception as e:
        log.error(f"TG error: {e}")

def tg(icon, title, lines):
    body = "\n".join([f"  {l}" for l in lines])
    send_telegram(f"{icon} <b>{title}</b>\n{body}")
    log.info(f"[TG] {title}")

def send_csv_files():
    files = [
        ("spy_scan_log_v3.csv",  "SPY Scan Log v3"),
        ("spy_trade_log_v3.csv", "SPY Trade Log v3"),
    ]
    send_telegram("📊 <b>Daily CSV Report</b>\nSending log files...")
    sent = 0
    for fname, caption in files:
        path = f"/home/salverukrishna83/algo-trading/{fname}"
        if not os.path.exists(path):
            continue
        try:
            url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendDocument"
            with open(path, "rb") as f:
                resp = requests.post(url, data={
                    "chat_id": config.CHAT_ID,
                    "caption": caption
                }, files={"document": f}, timeout=30)
            if resp.json().get("ok"):
                sent += 1
        except Exception as e:
            log.error(f"File send error: {e}")
    send_telegram(f"Done! Sent {sent}/{len(files)} files")

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
        log.info("TG listener started")

    def _poll(self):
        while self._running:
            try:
                url  = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
                resp = requests.get(url, params={
                    "offset": self.last_update_id + 1,
                    "timeout": 30
                }, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5); continue
                for update in resp.json().get("result", []):
                    self.last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip().lower()
                    if text.startswith("/usbias"):
                        parts = text.split()
                        if len(parts) >= 2 and parts[1] in ["bullish","bearish","neutral"]:
                            self.bias = parts[1]
                            send_telegram(f"US Bias updated: {self.bias.upper()}")
                    elif text == "/usstatus":
                        send_telegram(
                            f"SPY Bot v3\n"
                            f"Running: YES\n"
                            f"Bias: {self.bias.upper()}\n"
                            f"EST: {now_est().strftime('%H:%M:%S')}\n"
                            f"IST: {now_ist().strftime('%H:%M:%S')}"
                        )
                    elif text == "/usreport":
                        send_csv_files()
                    elif text == "/ushelp":
                        send_telegram(
                            "SPY Bot v3 Commands:\n"
                            "/usbias bullish\n"
                            "/usbias bearish\n"
                            "/usbias neutral\n"
                            "/usstatus\n"
                            "/usreport"
                        )
            except Exception as e:
                log.error(f"TG poll: {e}")
                time.sleep(5)

# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_spy_ltp(api):
    try:
        trade = api.get_latest_trade(SPY_SYMBOL)
        return float(trade.price)
    except Exception as e:
        log.error(f"LTP error: {e}")
        return None

def get_candles(api, interval="5Min", limit=50):
    try:
        bars = api.get_bars(SPY_SYMBOL, interval, limit=limit, adjustment="raw").df
        if bars.empty: return None
        bars = bars.reset_index()
        bars.columns = [c.lower() for c in bars.columns]
        for col in ["open","high","low","close","volume"]:
            bars[col] = bars[col].astype(float)
        log.info(f"Fresh {len(bars)} bars [{interval}]")
        return bars
    except Exception as e:
        log.error(f"Candle error: {e}")
        return None

def get_prev_day_ohlc(api):
    try:
        bars = api.get_bars(SPY_SYMBOL, "1D", limit=3).df
        if len(bars) < 2: return None
        prev = bars.iloc[-2]
        return {
            "open": float(prev["open"]), "high": float(prev["high"]),
            "low": float(prev["low"]),   "close": float(prev["close"])
        }
    except Exception as e:
        log.error(f"Prev OHLC: {e}")
        return None

# ─────────────────────────────────────────────
#  F&G with 30min cache
# ─────────────────────────────────────────────
_fg = {"score":50,"rating":"neutral","sent":"neutral","time":None}

def fetch_fear_greed():
    global _fg
    try:
        now = datetime.datetime.now()
        if _fg["time"] and (now - _fg["time"]).seconds < 1800:
            return _fg["score"], _fg["rating"], _fg["sent"]
        resp  = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent":"Mozilla/5.0"}, timeout=10
        )
        data   = resp.json()
        score  = float(data["fear_and_greed"]["score"])
        rating = data["fear_and_greed"]["rating"].lower()
        sent   = "bullish" if score >= 60 else "bearish" if score <= 40 else "neutral"
        _fg    = {"score":score,"rating":rating,"sent":sent,"time":now}
        return score, rating, sent
    except Exception as e:
        log.error(f"F&G: {e}")
        return _fg["score"], _fg["rating"], _fg["sent"]

def fetch_us_news():
    try:
        resp  = requests.get(
            "https://finance.yahoo.com/topic/stock-market-news/",
            headers={"User-Agent":"Mozilla/5.0"}, timeout=10
        )
        soup  = BeautifulSoup(resp.text, "html.parser")
        heads = []
        for tag in soup.find_all(["h3","h4"], limit=30):
            text = tag.get_text(strip=True)
            if len(text) > 20 and any(w in text.lower() for w in
               ["s&p","spy","market","stocks","fed","rally","nasdaq"]):
                heads.append(text[:120])
        heads = list(dict.fromkeys(heads))[:8]
        bull  = ["rally","surge","gain","rise","bullish","positive","strong","up","boost"]
        bear  = ["fall","drop","decline","bearish","negative","weak","down","crash"]
        score = sum(1 for h in heads for w in bull if w in h.lower()) - \
                sum(1 for h in heads for w in bear if w in h.lower())
        sent  = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"
        return heads, sent, score
    except Exception as e:
        log.error(f"News: {e}")
        return [], "neutral", 0

# ─────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────
def calc_vwap_bands(df):
    df = df.copy()
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tv"]  = (df["typical"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"]    = df["cum_tv"] / df["cum_vol"]
    df["cum_tv2"] = (((df["typical"] - df["vwap"]) ** 2) * df["volume"]).cumsum()
    df["sd"]      = np.sqrt(df["cum_tv2"] / df["cum_vol"])
    df["vwap_u1"] = df["vwap"] + df["sd"]
    df["vwap_l1"] = df["vwap"] - df["sd"]
    df["vwap_u2"] = df["vwap"] + 2 * df["sd"]
    df["vwap_l2"] = df["vwap"] - 2 * df["sd"]
    return df

def calc_ema(df, periods=[9,21,50]):
    df = df.copy()
    for p in periods:
        df[f"ema{p}"] = df["close"].astype(float).ewm(span=p, adjust=False).mean()
    return df

def calc_rvol(df):
    if df is None or len(df) < 5: return 1.0
    avg = float(df["volume"].mean())
    cur = float(df["volume"].iloc[-1])
    return round(cur / avg, 2) if avg > 0 else 1.0

def calc_obv(df):
    if df is None or len(df) < 3: return "neutral"
    obv = [0]
    for i in range(1, len(df)):
        if float(df["close"].iloc[i]) > float(df["close"].iloc[i-1]):
            obv.append(obv[-1] + float(df["volume"].iloc[i]))
        elif float(df["close"].iloc[i]) < float(df["close"].iloc[i-1]):
            obv.append(obv[-1] - float(df["volume"].iloc[i]))
        else:
            obv.append(obv[-1])
    if obv[-1] > obv[-2] > obv[-3]: return "bullish"
    if obv[-1] < obv[-2] < obv[-3]: return "bearish"
    return "neutral"

def calc_cumulative_delta(df):
    if df is None or len(df) < 1: return 0
    delta = 0
    for _, row in df.iterrows():
        body = float(row["close"]) - float(row["open"])
        vol  = float(row["volume"])
        delta += vol if body > 0 else -vol if body < 0 else 0
    return round(delta, 0)

def detect_trend_relaxed(df, min_agree=3):
    if df is None or len(df) < 4: return "neutral", "Not enough candles", 0
    recent = df.tail(4)
    highs  = [float(x) for x in recent["high"].tolist()]
    lows   = [float(x) for x in recent["low"].tolist()]
    hh = sum(1 for i in range(1,len(highs)) if highs[i] > highs[i-1])
    hl = sum(1 for i in range(1,len(lows))  if lows[i]  > lows[i-1])
    ll = sum(1 for i in range(1,len(lows))  if lows[i]  < lows[i-1])
    lh = sum(1 for i in range(1,len(highs)) if highs[i] < highs[i-1])
    bull = min(hh, hl); bear = min(ll, lh)
    if bull >= min_agree: return "bullish", f"HH:{hh}/3 HL:{hl}/3", bull
    if bear >= min_agree: return "bearish", f"LL:{ll}/3 LH:{lh}/3", bear
    return "neutral", f"HH:{hh} HL:{hl} LL:{ll} LH:{lh}", 0

def detect_trend_multi(df5, df15, df30):
    t5,_,_   = detect_trend_relaxed(df5)
    t15,_,_  = detect_trend_relaxed(df15)
    t30,_,_  = detect_trend_relaxed(df30)
    bull = [t5,t15,t30].count("bullish")
    bear = [t5,t15,t30].count("bearish")
    if bull >= 2: return "bullish", f"{t5}/{t15}/{t30}", "strong" if bull==3 else "moderate"
    if bear >= 2: return "bearish", f"{t5}/{t15}/{t30}", "strong" if bear==3 else "moderate"
    return "neutral", f"{t5}/{t15}/{t30}", "weak"

def detect_bos(df, trend):
    if df is None or len(df) < 6: return False, 0
    recent = df.tail(10)
    lc     = float(recent["close"].iloc[-1])
    if trend == "bullish":
        sh = float(recent["high"].iloc[:-1].max())
        if lc > sh: return True, sh
    elif trend == "bearish":
        sl = float(recent["low"].iloc[:-1].min())
        if lc < sl: return True, sl
    return False, 0

def detect_fvg(df):
    if df is None or len(df) < 3: return None, "Not enough candles"
    candles = df.tail(15)
    for i in range(len(candles)-1, 1, -1):
        c1 = candles.iloc[i-2]; c2 = candles.iloc[i-1]; c3 = candles.iloc[i]
        body = abs(float(c2["close"]) - float(c2["open"]))
        if body < MIN_FVG_BODY: continue
        c1h = float(c1["high"]); c1l = float(c1["low"])
        c3h = float(c3["high"]); c3l = float(c3["low"])
        if c1h < c3l:
            size = round(c3l-c1h,3); strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            return {"type":"bullish","top":round(c3l,3),"bottom":round(c1h,3),
                    "mid":round((c3l+c1h)/2,3),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bullish FVG ${size:.3f}"
        if c1l > c3h:
            size = round(c1l-c3h,3); strong = size > STRONG_FVG_GAP and body > STRONG_FVG_BODY
            return {"type":"bearish","top":round(c1l,3),"bottom":round(c3h,3),
                    "mid":round((c1l+c3h)/2,3),"size":size,"strong":strong}, \
                   f"{'STRONG' if strong else 'WEAK'} Bearish FVG ${size:.3f}"
    return None, "No FVG in last 15 candles"

def detect_breakaway_gap(df, prev_close):
    if df is None or len(df) < 2: return None, "Not enough candles"
    first_open = float(df["open"].iloc[0])
    if prev_close:
        gap = abs(first_open - prev_close)
        if gap >= BREAKAWAY_GAP_OPEN:
            direction = "bullish" if first_open > prev_close else "bearish"
            return {"type":direction,"gap_type":"gap_open","size":round(gap,3),
                    "level":round(prev_close,3),"strong":True}, \
                   f"Gap open {direction} ${gap:.3f}"
    candles = df.tail(10)
    for i in range(len(candles)-1, 0, -1):
        curr = candles.iloc[i]; prev = candles.iloc[i-1]
        body = abs(float(curr["close"]) - float(curr["open"]))
        if body < BREAKAWAY_GAP_INTRA: continue
        if float(curr["low"]) > float(prev["high"]):
            return {"type":"bullish","gap_type":"intraday","size":round(body,3),
                    "level":float(prev["high"]),"strong":True}, \
                   f"Intraday breakaway bullish ${body:.3f}"
        if float(curr["high"]) < float(prev["low"]):
            return {"type":"bearish","gap_type":"intraday","size":round(body,3),
                    "level":float(prev["low"]),"strong":True}, \
                   f"Intraday breakaway bearish ${body:.3f}"
    return None, "No breakaway gap"

def detect_orb(df, orb_high, orb_low, current_price=None):
    if orb_high is None or orb_low is None: return None, "ORB not formed yet"
    price = current_price
    if price is None and df is not None and len(df) > 0:
        price = float(df.iloc[-1]["close"])
    if price:
        if price > orb_high and (price - orb_high) >= 0.3:
            return {"type":"bullish","level":orb_high,"size":round(price-orb_high,3)}, \
                   f"ORB bullish ${price:.2f} > ${orb_high:.2f}"
        if price < orb_low and (orb_low - price) >= 0.3:
            return {"type":"bearish","level":orb_low,"size":round(orb_low-price,3)}, \
                   f"ORB bearish ${price:.2f} < ${orb_low:.2f}"
    return None, f"No ORB | Range:${orb_low:.2f}-${orb_high:.2f}"

def detect_vwap_rejection(df5, trend, rvol):
    if df5 is None or len(df5) < 5: return None, "Not enough candles"
    if trend == "neutral": return None, "Trend neutral"
    if rvol < MIN_RVOL: return None, f"RVOL {rvol} < {MIN_RVOL}"
    df5   = calc_vwap_bands(df5)
    last  = df5.iloc[-1]; prev = df5.iloc[-2]
    vwap  = float(last["vwap"])
    vl1   = float(last["vwap_l1"]); vu1 = float(last["vwap_u1"])
    close = float(last["close"]); low = float(last["low"]); high = float(last["high"])
    vol   = float(last["volume"]); avg_vol = float(df5["volume"].mean())
    surge = vol > avg_vol * MIN_RVOL
    if trend == "bullish":
        if (vl1 <= low <= vwap or float(prev["low"]) <= vwap) and close > vwap and surge:
            return {"type":"bullish","vwap":round(vwap,3),"vwap_l1":round(vl1,3),
                    "vwap_u1":round(vu1,3),"volume":round(vol,0),
                    "avg_volume":round(avg_vol,0),"rvol":rvol}, \
                   f"VWAP bullish VWAP:${vwap:.2f} RVOL:{rvol}"
    if trend == "bearish":
        if (vwap <= high <= vu1 or float(prev["high"]) >= vwap) and close < vwap and surge:
            return {"type":"bearish","vwap":round(vwap,3),"vwap_l1":round(vl1,3),
                    "vwap_u1":round(vu1,3),"volume":round(vol,0),
                    "avg_volume":round(avg_vol,0),"rvol":rvol}, \
                   f"VWAP bearish VWAP:${vwap:.2f} RVOL:{rvol}"
    return None, f"No VWAP | ${vwap:.2f} surge:{surge}"

def is_retesting(price, bottom, top): return bottom <= price <= top

def get_option_details(spy_price, option_type):
    atm    = round(spy_price)
    strike = atm + 2 if option_type == "CALL" else atm - 2
    today  = datetime.date.today()
    days   = (4 - today.weekday()) % 7
    if days == 0: days = 7
    expiry = today + datetime.timedelta(days=days)
    return strike, expiry, expiry.strftime("%Y-%m-%d")

def is_expiry_day(): return datetime.date.today().weekday() == 4

# ─────────────────────────────────────────────
#  PAPER TRADE ENGINE
# ─────────────────────────────────────────────
class PaperTrade:
    def __init__(self, trade_no, strategy, direction, entry_price,
                 option_type, strike, expiry, premium, signal,
                 fg_score, user_bias, pre_bias, rvol, obv,
                 trend_strength, is_strong=False):
        self.trade_no     = trade_no
        self.strategy     = strategy
        self.direction    = direction
        self.entry_price  = entry_price
        self.option_type  = option_type
        self.strike       = strike
        self.expiry       = expiry
        self.premium      = premium
        self.signal       = signal
        self.fg_score     = fg_score
        self.user_bias    = user_bias
        self.pre_bias     = pre_bias
        self.rvol         = rvol
        self.obv          = obv
        self.trend_strength = trend_strength
        self.is_strong    = is_strong
        self.entry_time   = now_est().strftime("%H:%M:%S EST")
        self.start_time   = time.time()
        self.be_moved     = False
        self.trailing     = is_strong
        self.best_price   = entry_price
        self.sl_price     = entry_price - SL_POINTS if direction=="bullish" else entry_price + SL_POINTS
        self.tgt_price    = entry_price + TARGET_POINTS if direction=="bullish" else entry_price - TARGET_POINTS

    def check(self, ltp):
        if self.trailing:
            if self.direction == "bullish" and ltp > self.best_price:
                self.best_price = ltp
                profit = ltp - self.entry_price
                if profit >= TRAIL_START:
                    new_sl = round(ltp - TRAIL_DISTANCE, 3)
                    if new_sl > self.sl_price:
                        self.sl_price = new_sl
                        tg("📈", f"Trade #{self.trade_no} Trail SL",
                           [f"SPY: ${ltp:.2f}", f"Profit: +${profit:.2f}", f"New SL: ${new_sl:.2f}"])
            elif self.direction == "bearish" and ltp < self.best_price:
                self.best_price = ltp
                profit = self.entry_price - ltp
                if profit >= TRAIL_START:
                    new_sl = round(ltp + TRAIL_DISTANCE, 3)
                    if new_sl < self.sl_price:
                        self.sl_price = new_sl
                        tg("📉", f"Trade #{self.trade_no} Trail SL",
                           [f"SPY: ${ltp:.2f}", f"Profit: +${profit:.2f}", f"New SL: ${new_sl:.2f}"])
            if self.direction == "bullish" and ltp <= self.sl_price: return "sl"
            if self.direction == "bearish" and ltp >= self.sl_price: return "sl"
        else:
            if not self.be_moved:
                half = (self.entry_price + self.tgt_price) / 2
                if (self.direction=="bullish" and ltp>=half) or (self.direction=="bearish" and ltp<=half):
                    self.be_moved = True
                    self.sl_price = self.entry_price
                    tg("🔒", f"Trade #{self.trade_no} Breakeven",
                       [f"SPY: ${ltp:.2f}", f"New SL: ${self.entry_price:.2f}"])
            if self.direction == "bullish":
                if ltp >= self.tgt_price: return "target"
                if ltp <= self.sl_price:  return "sl"
            else:
                if ltp <= self.tgt_price: return "target"
                if ltp >= self.sl_price:  return "sl"
        return None

    def duration(self): return round((time.time() - self.start_time) / 60, 1)
    def calc_pnl(self, exit_price):
        pts = exit_price - self.entry_price if self.direction=="bullish" else self.entry_price - exit_price
        return round(pts * 0.4 * 100, 2)

# ─────────────────────────────────────────────
#  CSV LOGS 35+ columns
# ─────────────────────────────────────────────
SCAN_COLS = [
    "datetime_est","datetime_ist","spy_ltp","spy_change_from_open","spy_change_pct",
    "trend_5m","trend_15m","trend_30m","trend_combined","trend_strength",
    "rvol","obv_direction","cumulative_delta","volume_current","volume_avg",
    "vwap","vwap_upper1","vwap_lower1","vwap_upper2","vwap_lower2","price_vs_vwap",
    "ema9","ema21","ema50","price_vs_ema9","price_vs_ema21","price_vs_ema50",
    "fvg_found","fvg_type","fvg_strong","fvg_size",
    "bos_confirmed","breakaway_found","breakaway_type",
    "orb_high","orb_low","orb_signal","orb_breakout_size",
    "vwap_signal","vwap_rvol",
    "fear_greed_score","fear_greed_rating","news_sentiment",
    "user_bias","alpaca_auto_bias","overall_bias",
    "reversal_risk","reversal_signals",
    "session","entry_condition_met","strategy_triggered",
    "trades_today","daily_pnl","consec_losses","reason"
]

TRADE_COLS = [
    "date","trade_no","strategy","session",
    "entry_time_est","exit_time_est",
    "pre_bias","user_bias","alpaca_bias","fear_greed",
    "trend_combined","trend_strength",
    "rvol_at_entry","obv_at_entry",
    "reversal_risk","reversal_signals",
    "direction","is_strong","exit_mode",
    "entry_spy","exit_spy","points_moved",
    "option_type","strike","expiry",
    "premium_est","contracts","capital_usd",
    "sl_points","target_points","pnl_usd","result",
    "be_triggered","trail_triggered",
    "duration_min","consec_losses","daily_pnl","notes"
]

def init_logs():
    for fname, cols in [("spy_scan_log_v3.csv", SCAN_COLS),
                         ("spy_trade_log_v3.csv", TRADE_COLS)]:
        if not os.path.exists(fname):
            with open(fname, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()
    log.info("SPY v3 logs initialised")

def write_scan(rec):
    with open("spy_scan_log_v3.csv", "a", newline="") as f:
        row = {c: rec.get(c,"") for c in SCAN_COLS}
        csv.DictWriter(f, fieldnames=SCAN_COLS).writerow(row)

def write_trade(rec):
    with open("spy_trade_log_v3.csv", "a", newline="") as f:
        row = {c: rec.get(c,"") for c in TRADE_COLS}
        csv.DictWriter(f, fieldnames=TRADE_COLS).writerow(row)

def send_summary(stats, pre_bias, fg_score):
    wr = (stats["wins"]/stats["trades"]*100) if stats["trades"] > 0 else 0
    tg("📊","SPY v3 DAILY SUMMARY",
       [f"Pre-bias    : {pre_bias.upper()}",
        f"F&G         : {fg_score}",
        f"Trades      : {stats['trades']}",
        f"Wins        : {stats['wins']}",
        f"Losses      : {stats['losses']}",
        f"Win rate    : {wr:.1f}%",
        f"P&L         : ${stats['pnl']:+.2f}",
        f"FVG trades  : {stats.get('fvg_trades',0)}",
        f"ORB trades  : {stats.get('orb_trades',0)}",
        f"VWAP trades : {stats.get('vwap_trades',0)}",
        f"Strong trail: {stats.get('strong_trades',0)}"])
    send_csv_files()

def open_trade(trade_no, strategy, direction, entry_price,
               fg_score, tg_listener, pre_bias, alpaca_bias,
               is_strong, signal, session, rvol, obv,
               trend_strength, trend, risk_level):
    opt    = "CALL" if direction=="bullish" else "PUT"
    strike, expiry, exp_str = get_option_details(entry_price, opt)
    premium = CAPITAL_PER_TRADE / 100
    trade   = PaperTrade(
        trade_no=trade_no, strategy=strategy,
        direction=direction, entry_price=entry_price,
        option_type=opt, strike=strike, expiry=expiry,
        premium=premium, signal=signal, fg_score=fg_score,
        user_bias=tg_listener.bias, pre_bias=pre_bias,
        rvol=rvol, obv=obv, trend_strength=trend_strength,
        is_strong=is_strong
    )
    mode = "Trailing" if is_strong else f"Fixed ${TARGET_POINTS}"
    tg("🚀", f"SPY PAPER TRADE #{trade_no} — {strategy}",
       [f"Session      : {session.upper()}",
        f"Direction    : {direction.upper()}",
        f"Trend        : {trend} ({trend_strength})",
        f"Option       : {opt} ${strike} | {exp_str}",
        f"Entry        : ${entry_price:.2f}",
        f"SL           : ${trade.sl_price:.2f}",
        f"Exit mode    : {mode}",
        f"RVOL         : {rvol}x",
        f"OBV          : {obv}",
        f"Reversal risk: {risk_level}",
        f"Capital      : ${CAPITAL_PER_TRADE}",
        f"NOTE         : PAPER TRADE"])
    return trade

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    init_logs()
    api         = get_alpaca()
    tg_listener = TelegramListener()
    tg_listener.start()

    stats = {
        "trades":0,"wins":0,"losses":0,"timeouts":0,
        "skipped":0,"pnl":0.0,"consec_loss":0,
        "fvg_trades":0,"orb_trades":0,"vwap_trades":0,"strong_trades":0
    }

    trade_no     = 0
    active_trade = None
    last_scan    = None
    pre_bias     = "neutral"
    alpaca_bias  = "neutral"
    alpaca_chg   = 0
    fg_score     = 50
    fg_rating    = "neutral"
    news_sent    = "neutral"
    premarket_done = False
    reminder_sent  = False
    orb_high     = None
    orb_low      = None
    orb_formed   = False
    prev_ohlc    = None
    used_signals = set()
    open_price   = None
    closed_summary_sent = False

    send_telegram(
        f"SPY Scalping Bot v3 Started\n"
        f"Patches: 16 applied\n"
        f"Mode: PAPER TRADING\n"
        f"SL:${SL_POINTS} TGT:${TARGET_POINTS} Trail:${TRAIL_DISTANCE}\n"
        f"RVOL min:{MIN_RVOL}x FVG body:${MIN_FVG_BODY}\n"
        f"Max:{MAX_TRADES} trades Loss:${DAILY_LOSS_LIMIT} Profit:${DAILY_PROFIT_TARGET}\n"
        f"nyse_auto_bias: integrated\n\n"
        f"/usbias bullish|bearish|neutral\n"
        f"/usstatus\n/usreport"
    )

    while True:
        t_est = est_time()
        t_ist = ist_time()
        now   = now_est()

        session = ("premarket" if PREMARKET_START <= t_est < MARKET_START
                   else "regular" if MARKET_START <= t_est < MARKET_END
                   else "closed")

        # PATCH #12: 1PM IST reminder
        if not reminder_sent and IST_REMINDER <= t_ist < IST_PREMARKET:
            ab, chg = "neutral", 0
            if prev_ohlc:
                ltp = get_spy_ltp(api)
                if ltp and prev_ohlc:
                    chg = round(((ltp - prev_ohlc["close"]) / prev_ohlc["close"]) * 100, 2)
                    ab  = "bullish" if chg > 0.3 else "bearish" if chg < -0.3 else "neutral"
            fg_s, fg_r, _ = fetch_fear_greed()
            send_telegram(
                f"US Pre-market in 30 min!\n"
                f"Alpaca auto-bias: {ab.upper()} ({chg:+.2f}%)\n"
                f"Fear and Greed: {fg_s} ({fg_r})\n\n"
                f"Send /usbias bullish|bearish|neutral\n"
                f"Auto-bias used if not sent."
            )
            reminder_sent = True

        # PATCH #5: closed — no alerts
        if session == "closed":
            if not closed_summary_sent and t_est >= MARKET_END:
                send_summary(stats, pre_bias, fg_score)
                send_telegram("NYSE Closed. Bot sleeping.")
                closed_summary_sent = True
                stats = {"trades":0,"wins":0,"losses":0,"timeouts":0,
                         "skipped":0,"pnl":0.0,"consec_loss":0,
                         "fvg_trades":0,"orb_trades":0,"vwap_trades":0,"strong_trades":0}
                trade_no=0; active_trade=None; last_scan=None
                pre_bias="neutral"; alpaca_bias="neutral"
                orb_high=None; orb_low=None; orb_formed=False
                used_signals=set(); tg_listener.bias="neutral"
                reminder_sent=False; premarket_done=False
                open_price=None; closed_summary_sent=False
            time.sleep(60); continue

        closed_summary_sent = False

        # PRE-MARKET ANALYSIS using nyse_auto_bias
        if not premarket_done and session == "premarket":
            prev_ohlc = get_prev_day_ohlc(api)
            final_bias, bias_report = get_combined_bias(
                api,
                prev_ohlc["close"] if prev_ohlc else None,
                tg_listener.bias
            )
            pre_bias    = final_bias
            alpaca_bias = bias_report["alpaca_bias"]
            alpaca_chg  = bias_report["alpaca_chg_pct"]
            fg_score    = bias_report["fg_score"]
            fg_rating   = bias_report["fg_rating"]
            news_sent   = bias_report["news_bias"]
            send_telegram(format_bias_message(bias_report))
            premarket_done = True

        # GUARDS
        if stats["trades"] >= MAX_TRADES: time.sleep(30*60); continue
        if stats["consec_loss"] >= 3:
            tg("STOP","Risk Protection",[f"Losses:{stats['consec_loss']}","Paused today"])
            send_summary(stats, pre_bias, fg_score); time.sleep(16*3600); continue
        if stats["pnl"] <= -DAILY_LOSS_LIMIT:
            tg("STOP","Loss Limit",[f"P&L:${stats['pnl']:+.2f}"])
            send_summary(stats, pre_bias, fg_score); time.sleep(16*3600); continue
        if stats["pnl"] >= DAILY_PROFIT_TARGET:
            tg("DONE","Profit Target!",[f"P&L:${stats['pnl']:+.2f}"])
            send_summary(stats, pre_bias, fg_score); time.sleep(16*3600); continue

        # MONITOR ACTIVE TRADE
        if active_trade is not None:
            ltp    = get_spy_ltp(api)
            result = None
            if ltp: result = active_trade.check(ltp)
            if t_est >= MARKET_END: result="timeout"; ltp = ltp or active_trade.entry_price
            if result:
                exit_time = now.strftime("%H:%M:%S EST")
                duration  = active_trade.duration()
                pnl       = active_trade.calc_pnl(ltp)
                pts_moved = round(ltp-active_trade.entry_price,3) if active_trade.direction=="bullish" \
                            else round(active_trade.entry_price-ltp,3)
                if result=="target": icon="WIN"; stats["wins"]+=1; stats["consec_loss"]=0
                elif result=="sl":   icon="LOSS"; stats["losses"]+=1; stats["consec_loss"]+=1
                else:                icon="TIME"; stats["timeouts"]+=1; stats["consec_loss"]=0
                stats["trades"]+=1; stats["pnl"]+=pnl
                exit_mode = "Trail" if active_trade.trailing else "Fixed"
                tg(icon, f"SPY TRADE #{active_trade.trade_no} {result.upper()}",
                   [f"Strategy  : {active_trade.strategy}",
                    f"Direction : {active_trade.direction.upper()}",
                    f"Entry     : ${active_trade.entry_price:.2f}",
                    f"Exit      : ${ltp:.2f}",
                    f"Points    : ${pts_moved:+.3f}",
                    f"Duration  : {duration}min",
                    f"P&L       : ${pnl:+.2f}",
                    f"Day P&L   : ${stats['pnl']:+.2f}",
                    f"Trades    : {stats['trades']}/{MAX_TRADES}"])
                write_trade({
                    "date":datetime.date.today(),"trade_no":active_trade.trade_no,
                    "strategy":active_trade.strategy,"session":session,
                    "entry_time_est":active_trade.entry_time,"exit_time_est":exit_time,
                    "pre_bias":pre_bias,"user_bias":active_trade.user_bias,
                    "alpaca_bias":alpaca_bias,"fear_greed":active_trade.fg_score,
                    "trend_combined":active_trade.trend_strength,
                    "trend_strength":active_trade.trend_strength,
                    "rvol_at_entry":active_trade.rvol,"obv_at_entry":active_trade.obv,
                    "direction":active_trade.direction,"is_strong":active_trade.is_strong,
                    "exit_mode":exit_mode,"entry_spy":active_trade.entry_price,
                    "exit_spy":round(ltp,3),"points_moved":pts_moved,
                    "option_type":active_trade.option_type,"strike":active_trade.strike,
                    "expiry":active_trade.expiry,"premium_est":active_trade.premium,
                    "contracts":1,"capital_usd":CAPITAL_PER_TRADE,
                    "sl_points":SL_POINTS,"target_points":TARGET_POINTS,
                    "pnl_usd":pnl,"result":result,
                    "be_triggered":active_trade.be_moved,
                    "trail_triggered":active_trade.trailing,
                    "duration_min":duration,"consec_losses":stats["consec_loss"],
                    "daily_pnl":stats["pnl"],"notes":active_trade.signal
                })
                active_trade=None; time.sleep(2*60)
            else: time.sleep(15)
            continue

        # FETCH FRESH DATA
        ltp   = get_spy_ltp(api)
        df_5  = get_candles(api, "5Min",  50)
        df_15 = get_candles(api, "15Min", 30)
        df_30 = get_candles(api, "30Min", 20)

        if ltp is None or df_5 is None: time.sleep(15); continue
        if open_price is None and session=="regular": open_price = ltp

        # ORB FORMATION
        if not orb_formed and t_est >= ORB_END_TIME and session=="regular":
            try:
                orb_df = df_5[pd.to_datetime(df_5["timestamp"]).dt.time <= ORB_END_TIME]
                if not orb_df.empty:
                    orb_high = float(orb_df["high"].max())
                    orb_low  = float(orb_df["low"].min())
                    orb_formed = True
                    tg("ORB","SPY ORB Range Formed",
                       [f"High: ${orb_high:.2f}",f"Low: ${orb_low:.2f}",
                        f"Size: ${orb_high-orb_low:.2f}",f"SPY: ${ltp:.2f}"])
            except Exception as e: log.error(f"ORB: {e}")

        # F&G refresh
        fg_score, fg_rating, _ = fetch_fear_greed()

        # INDICATORS
        trend, trend_reason, trend_strength = detect_trend_multi(df_5, df_15, df_30)
        t5,_,_ = detect_trend_relaxed(df_5)
        t15,_,_ = detect_trend_relaxed(df_15)
        t30,_,_ = detect_trend_relaxed(df_30)
        rvol    = calc_rvol(df_5)
        obv     = calc_obv(df_5)
        cum_d   = calc_cumulative_delta(df_5)
        df5v    = calc_vwap_bands(df_5)
        lr      = df5v.iloc[-1]
        vwap    = round(float(lr["vwap"]),3)
        vu1     = round(float(lr["vwap_u1"]),3)
        vl1     = round(float(lr["vwap_l1"]),3)
        vu2     = round(float(lr["vwap_u2"]),3)
        vl2     = round(float(lr["vwap_l2"]),3)
        df5e    = calc_ema(df_5)
        ema9    = round(float(df5e["ema9"].iloc[-1]),3)
        ema21   = round(float(df5e["ema21"].iloc[-1]),3)
        ema50   = round(float(df5e["ema50"].iloc[-1]),3)

        # DETECTORS
        fvg,     fvg_r   = detect_fvg(df_5)
        bos,     bos_lvl = detect_bos(df_5, trend)
        bgap,    bgap_r  = detect_breakaway_gap(df_5, prev_ohlc["close"] if prev_ohlc else None)
        orb_sig, orb_r   = detect_orb(df_5, orb_high, orb_low, ltp)
        vwap_s,  vwap_r  = detect_vwap_rejection(df_5, trend, rvol)

        # 5-MIN SCAN LOG
        do_scan = (last_scan is None or (now_est()-last_scan).seconds >= 300)
        if do_scan:
            last_scan = now_est()
            strats    = []
            if fvg and bos: strats.append("FVG+BOS")
            if bgap:        strats.append("Breakaway")
            if orb_sig:     strats.append("ORB")
            if vwap_s:      strats.append("VWAP")
            entry_met = len(strats)>0 and trend!="neutral" and rvol>=MIN_RVOL
            chg_open  = round(ltp-open_price,3) if open_price else 0
            chg_pct   = round((chg_open/open_price*100),3) if open_price else 0
            write_scan({
                "datetime_est":now.strftime("%Y-%m-%d %H:%M EST"),
                "datetime_ist":now_ist().strftime("%Y-%m-%d %H:%M IST"),
                "spy_ltp":round(ltp,3),"spy_change_from_open":chg_open,
                "spy_change_pct":chg_pct,"trend_5m":t5,"trend_15m":t15,
                "trend_30m":t30,"trend_combined":trend,"trend_strength":trend_strength,
                "rvol":rvol,"obv_direction":obv,"cumulative_delta":cum_d,
                "volume_current":round(float(df_5["volume"].iloc[-1]),0),
                "volume_avg":round(float(df_5["volume"].mean()),0),
                "vwap":vwap,"vwap_upper1":vu1,"vwap_lower1":vl1,
                "vwap_upper2":vu2,"vwap_lower2":vl2,"price_vs_vwap":round(ltp-vwap,3),
                "ema9":ema9,"ema21":ema21,"ema50":ema50,
                "price_vs_ema9":round(ltp-ema9,3),"price_vs_ema21":round(ltp-ema21,3),
                "price_vs_ema50":round(ltp-ema50,3),
                "fvg_found":fvg is not None,"fvg_type":fvg["type"] if fvg else "",
                "fvg_strong":fvg["strong"] if fvg else "","fvg_size":fvg["size"] if fvg else "",
                "bos_confirmed":bos,"breakaway_found":bgap is not None,
                "breakaway_type":bgap["gap_type"] if bgap else "",
                "orb_high":orb_high or "","orb_low":orb_low or "",
                "orb_signal":orb_sig["type"] if orb_sig else "",
                "orb_breakout_size":orb_sig["size"] if orb_sig else "",
                "vwap_signal":vwap_s["type"] if vwap_s else "",
                "vwap_rvol":vwap_s["rvol"] if vwap_s else "",
                "fear_greed_score":fg_score,"fear_greed_rating":fg_rating,
                "news_sentiment":news_sent,"user_bias":tg_listener.bias,
                "alpaca_auto_bias":alpaca_bias,"overall_bias":pre_bias,
                "session":session,"entry_condition_met":entry_met,
                "strategy_triggered":",".join(strats),
                "trades_today":stats["trades"],"daily_pnl":stats["pnl"],
                "consec_losses":stats["consec_loss"],
                "reason":f"FVG:{fvg_r}|ORB:{orb_r}|VWAP:{vwap_r}"
            })
            ci = "OK" if entry_met else "WAIT"
            tg(ci, f"SPY SCAN {now.strftime('%H:%M')} EST",
               [f"SPY       : ${ltp:.2f} ({chg_pct:+.2f}%)",
                f"Session   : {session.upper()}",
                f"Trend     : {trend.upper()} ({trend_strength})",
                f"5m/15m/30m: {t5}/{t15}/{t30}",
                f"RVOL      : {rvol}x {'OK' if rvol>=MIN_RVOL else 'LOW'}",
                f"OBV       : {obv}",
                f"VWAP      : ${vwap:.2f} ({ltp-vwap:+.2f})",
                f"EMA9/21   : ${ema9:.2f}/${ema21:.2f}",
                f"FVG       : {fvg_r[:40] if fvg else 'NONE'}",
                f"BOS       : {'YES' if bos else 'NO'}",
                f"ORB       : {orb_r[:40]}",
                f"VWAP sig  : {vwap_r[:40]}",
                f"F&G       : {fg_score} ({fg_rating})",
                f"Bias      : {pre_bias.upper()}",
                f"Signals   : {', '.join(strats) if strats else 'NONE'}",
                f"IST       : {now_ist().strftime('%H:%M')}"])

        # RVOL GATE
        if rvol < MIN_RVOL and session=="regular": time.sleep(60); continue

        # ── STRATEGY 1: FVG + BOS ─────────────────────────────
        if fvg and bos and trend!="neutral" and "FVG" not in used_signals:
            if fvg["type"]==trend and (pre_bias=="neutral" or pre_bias==trend):
                if obv==trend or obv=="neutral":
                    # PATCH #16: Pre-trade reversal check
                    proceed, risk, summary, rev_signals = pre_trade_check(
                        df_5, df_15, trend, pre_bias,
                        prev_ohlc["close"] if prev_ohlc else None,
                        orb_high, orb_low, prev_ohlc
                    )
                    send_telegram(format_reversal_alert(
                        risk, proceed, rev_signals, summary, "FVG+BOS", trend))
                    if not proceed:
                        stats["skipped"]+=1; time.sleep(60); continue

                    is_strong = fvg["strong"]
                    retest_ok=False; ep=None; sw=time.time()
                    while time.time()-sw < 10*60:
                        cur = get_spy_ltp(api)
                        if cur and is_retesting(cur, fvg["bottom"], fvg["top"]):
                            retest_ok=True; ep=cur; break
                        time.sleep(15)
                    if retest_ok:
                        trade_no+=1
                        active_trade = open_trade(
                            trade_no,"FVG+BOS",trend,ep,
                            fg_score,tg_listener,pre_bias,alpaca_bias,
                            is_strong,f"FVG ${fvg['size']:.3f} BOS@${bos_lvl:.2f}",
                            session,rvol,obv,trend_strength,trend,risk
                        )
                        used_signals.add("FVG")
                        stats["fvg_trades"]+=1
                        if is_strong: stats["strong_trades"]+=1
                        time.sleep(15); continue
                    else: stats["skipped"]+=1
                else:
                    tg("SKIP","FVG — OBV Conflict",
                       [f"FVG:{fvg['type']} OBV:{obv} — skipping"])
                    stats["skipped"]+=1

        # ── STRATEGY 1B: BREAKAWAY GAP ────────────────────────
        if bgap and trend!="neutral" and "BGAP" not in used_signals:
            if bgap["type"]==trend and (pre_bias=="neutral" or pre_bias==trend):
                proceed, risk, summary, rev_signals = pre_trade_check(
                    df_5, df_15, trend, pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,
                    orb_high, orb_low, prev_ohlc
                )
                send_telegram(format_reversal_alert(
                    risk, proceed, rev_signals, summary, "BreakawayGap", trend))
                if not proceed:
                    stats["skipped"]+=1; time.sleep(60); continue
                level=bgap["level"]; retest_ok=False; ep=None; sw=time.time()
                while time.time()-sw < 10*60:
                    cur = get_spy_ltp(api)
                    if cur and is_retesting(cur, level-0.3, level+0.3):
                        retest_ok=True; ep=cur; break
                    time.sleep(15)
                if retest_ok:
                    trade_no+=1
                    active_trade = open_trade(
                        trade_no,"BreakawayGap",trend,ep,
                        fg_score,tg_listener,pre_bias,alpaca_bias,
                        True,f"Bgap {bgap['gap_type']} ${bgap['size']:.3f}",
                        session,rvol,obv,trend_strength,trend,risk
                    )
                    used_signals.add("BGAP")
                    stats["fvg_trades"]+=1; stats["strong_trades"]+=1
                    time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 2: ORB ───────────────────────────────────
        if orb_sig and orb_formed and "ORB" not in used_signals:
            if session=="regular" and (pre_bias=="neutral" or pre_bias==orb_sig["type"]):
                proceed, risk, summary, rev_signals = pre_trade_check(
                    df_5, df_15, orb_sig["type"], pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,
                    orb_high, orb_low, prev_ohlc
                )
                send_telegram(format_reversal_alert(
                    risk, proceed, rev_signals, summary, "ORB", orb_sig["type"]))
                if not proceed:
                    stats["skipped"]+=1; time.sleep(60); continue
                level=orb_sig["level"]; retest_ok=False; ep=None; sw=time.time()
                while time.time()-sw < 10*60:
                    cur = get_spy_ltp(api)
                    if cur and is_retesting(cur, level-0.3, level+0.3):
                        retest_ok=True; ep=cur; break
                    time.sleep(15)
                if retest_ok:
                    trade_no+=1
                    active_trade = open_trade(
                        trade_no,"ORB",orb_sig["type"],ep,
                        fg_score,tg_listener,pre_bias,alpaca_bias,
                        False,f"ORB {orb_sig['type']} ${orb_sig['size']:.3f}",
                        session,rvol,obv,trend_strength,trend,risk
                    )
                    used_signals.add("ORB"); stats["orb_trades"]+=1
                    time.sleep(15); continue
                else: stats["skipped"]+=1

        # ── STRATEGY 3: VWAP ──────────────────────────────────
        if vwap_s and "VWAP" not in used_signals:
            if pre_bias=="neutral" or pre_bias==vwap_s["type"]:
                proceed, risk, summary, rev_signals = pre_trade_check(
                    df_5, df_15, vwap_s["type"], pre_bias,
                    prev_ohlc["close"] if prev_ohlc else None,
                    orb_high, orb_low, prev_ohlc
                )
                send_telegram(format_reversal_alert(
                    risk, proceed, rev_signals, summary, "VWAP", vwap_s["type"]))
                if not proceed:
                    stats["skipped"]+=1; time.sleep(60); continue
                cur = get_spy_ltp(api)
                if cur:
                    trade_no+=1
                    active_trade = open_trade(
                        trade_no,"VWAP",vwap_s["type"],cur,
                        fg_score,tg_listener,pre_bias,alpaca_bias,
                        False,f"VWAP {vwap_s['type']} RVOL:{rvol}",
                        session,rvol,obv,trend_strength,trend,risk
                    )
                    used_signals.add("VWAP"); stats["vwap_trades"]+=1
                    time.sleep(15); continue

        time.sleep(60)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("SPY Bot v3 stopped")
        send_telegram("SPY Bot v3 stopped.")
